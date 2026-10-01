"""Guardian Review (STAGE2_LIVE_RUN_DEFECTS_20260930.md, Issues 1/2/14) --
PDF extraction on_late_result callback.

process_network_response_async's caller-facing PdfExtractionTimeoutError does
NOT mean the underlying worker thread has stopped: it is shielded
(asyncio.shield) and keeps running until it genuinely finishes. Before this
fix, a successful late parse had nowhere to go but the logs
("ownership=DISCARDED") and the caller had no way to know when the worker
eventually finished, so its single-flight registry entry (repository.py's
_official_filing_flights, see test_official_filing_late_result_persistence.py)
was released immediately on timeout -- permitting a follower to start a brand
new duplicate download+extraction of the same still-in-flight document.

The fix adds an optional `on_late_result` callback to
process_network_response_async: when (and only when) this call itself times
out while the worker keeps running, the callback is invoked exactly once,
with either the worker's eventual FetchResult (success) or its raised
exception (failure), once the shielded task genuinely completes. It is never
invoked on ordinary success or on a non-timeout failure -- the caller already
has that outcome directly in those cases.

These tests are fetcher-level (HttpResearchFetcher in isolation); the
repository-level single-flight/persistence wiring that CONSUMES this callback
is covered separately in test_official_filing_late_result_persistence.py.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.research_fetching import FetchResult, HttpResearchFetcher, PdfExtractionTimeoutError
from app.settings import Settings
from test_research_engine import _pdf_network_fixture


async def _until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_on_late_result_delivers_eventual_success_after_caller_timeout(monkeypatch):
    """A caller that times out must still learn about the worker's eventual
    SUCCESS via on_late_result -- the whole point of the fix (a safely
    persistable late success must not simply disappear)."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "late-text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    late_results: list[object] = []

    async def on_late_result(outcome):
        late_results.append(outcome)

    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
        task = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=0.01, on_late_result=on_late_result,
        ))
        await _until(worker_started.is_set)
        await task

    assert late_results == []  # worker still running; callback not fired yet
    release_worker.set()
    await _until(lambda: len(late_results) == 1)
    assert isinstance(late_results[0], FetchResult)
    assert late_results[0].text == "late-text"


@pytest.mark.asyncio
async def test_on_late_result_delivers_eventual_failure_after_caller_timeout(monkeypatch):
    """A late worker FAILURE must also reach on_late_result (as the raised
    exception, not silently swallowed) so the caller knows the document
    remains genuinely retryable rather than assuming it is still in flight
    forever."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        raise ValueError("late worker failure")

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    late_results: list[object] = []

    async def on_late_result(outcome):
        late_results.append(outcome)

    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
        task = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=0.01, on_late_result=on_late_result,
        ))
        await _until(worker_started.is_set)
        await task

    release_worker.set()
    await _until(lambda: len(late_results) == 1)
    assert isinstance(late_results[0], ValueError)


@pytest.mark.asyncio
async def test_on_late_result_not_invoked_on_ordinary_success(monkeypatch):
    """When the caller does NOT time out, on_late_result must never fire --
    the caller already has the outcome directly via the normal return value."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))

    def fast_extract(_response, *, max_bytes=None):
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", fast_extract)

    late_results: list[object] = []

    async def on_late_result(outcome):
        late_results.append(outcome)

    result = await fetcher.process_network_response_async(
        _pdf_network_fixture(), extraction_timeout_seconds=2, on_late_result=on_late_result,
    )
    assert result.text == "text"
    # Give any (incorrectly) scheduled callback a chance to run before asserting absence.
    await asyncio.sleep(0.05)
    assert late_results == []


@pytest.mark.asyncio
async def test_on_late_result_not_invoked_on_ordinary_non_timeout_failure(monkeypatch):
    """A synchronous, non-timeout extraction failure must propagate directly
    (as today) without ever touching on_late_result."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))

    def fast_failure(_response, *, max_bytes=None):
        raise ValueError("immediate failure")

    monkeypatch.setattr(fetcher, "process_network_response", fast_failure)

    late_results: list[object] = []

    async def on_late_result(outcome):
        late_results.append(outcome)

    with pytest.raises(ValueError, match="immediate failure"):
        await fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=2, on_late_result=on_late_result,
        )
    await asyncio.sleep(0.05)
    assert late_results == []


@pytest.mark.asyncio
async def test_on_late_result_handler_exception_is_isolated(monkeypatch):
    """A bug in the caller's on_late_result handler itself must never escape
    as an unhandled-task-exception warning or otherwise destabilize the
    fetcher -- it is swallowed (and logged) exactly like any other
    best-effort observability hook."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    handler_called = threading.Event()

    async def broken_on_late_result(_outcome):
        handler_called.set()
        raise RuntimeError("handler bug")

    with pytest.raises(PdfExtractionTimeoutError):
        task = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=0.01, on_late_result=broken_on_late_result,
        ))
        await _until(worker_started.is_set)
        await task

    release_worker.set()
    await _until(handler_called.is_set)
    # A legitimate follow-up call must still be able to acquire the permit --
    # i.e. the handler's own bug did not leak the semaphore or crash the loop.
    result = await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)
    assert result.text == "text"
