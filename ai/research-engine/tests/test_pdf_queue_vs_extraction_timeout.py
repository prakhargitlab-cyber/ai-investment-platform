"""Guardian Review Slice 4 -- PDF pipeline technical-failure semantics.

Proves the fix for the conflated PDF queue-admission and extraction
timeouts: app.research_fetching.HttpResearchFetcher previously bounded BOTH
"wait for an extraction-worker permit" and "the extraction itself" with the
SAME configured value (research_official_document_extraction_timeout_seconds),
so a document stuck behind other queued work (PDF_EXTRACTION_QUEUE_TIMEOUT)
and a document whose own parse is slow/stuck (PDF_EXTRACTION_TIMEOUT) were
indistinguishable in configuration -- tuning one always tuned the other.

The fix adds a distinct, independently configurable
`research_pdf_extraction_queue_timeout_seconds` setting used ONLY for queue
admission, while `research_official_document_extraction_timeout_seconds`
continues to bound the extraction itself. An explicit per-call
`extraction_timeout_seconds` override (the existing pre-fix call contract,
used by one production call site and several existing tests) still bounds
both, preserving exact backward compatibility for every caller that already
relies on that single-value override.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.research_fetching import FetchResult, HttpResearchFetcher, PdfExtractionTimeoutError
from app.settings import Settings
from test_research_engine import _pdf_network_fixture


async def _until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


# 1 -- a short queue timeout rejects a QUEUED caller quickly even though the
# extraction timeout configured for the SAME fetcher is generous ------------
@pytest.mark.asyncio
async def test_short_queue_timeout_independent_of_generous_extraction_timeout(monkeypatch):
    fetcher = HttpResearchFetcher(Settings(
        research_pdf_extraction_concurrency=1,
        research_pdf_extraction_queue_timeout_seconds=0.05,
        research_official_document_extraction_timeout_seconds=5.0,
    ))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    # First call occupies the sole extraction permit; no explicit override,
    # so it uses the fetcher's own (generous) extraction-timeout default.
    first = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture()))
    await _until(worker_started.is_set)

    # Second call queues behind it; no explicit override either, so it must
    # observe the fetcher's SHORT, independently configured queue timeout --
    # not the generous 5s extraction timeout.
    started_waiting = time.monotonic()
    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_QUEUE_TIMEOUT"):
        await fetcher.process_network_response_async(_pdf_network_fixture())
    elapsed = time.monotonic() - started_waiting
    assert elapsed < 1.0  # bounded by the 0.05s queue timeout, not the 5s extraction timeout

    # The first call is still legitimately running (its own extraction
    # timeout has not been reached) -- releasing it now lets it succeed,
    # proving the queue rejection above never touched the first call at all.
    release_worker.set()
    result = await first
    assert result.text == "text"


# 2 -- an explicit per-call override still bounds BOTH queue admission and
# extraction, exactly as before this setting existed (no behavior change for
# existing callers/tests that rely on the single-value override) ----------
@pytest.mark.asyncio
async def test_explicit_override_still_bounds_queue_admission_too(monkeypatch):
    fetcher = HttpResearchFetcher(Settings(
        research_pdf_extraction_concurrency=1,
        # A generous configured queue-timeout default that would NOT apply
        # here, since the call below passes its own override.
        research_pdf_extraction_queue_timeout_seconds=5.0,
    ))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    first = asyncio.create_task(fetcher.process_network_response_async(
        _pdf_network_fixture(), extraction_timeout_seconds=1))
    await _until(worker_started.is_set)

    started_waiting = time.monotonic()
    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_QUEUE_TIMEOUT"):
        await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=0.01)
    elapsed = time.monotonic() - started_waiting
    assert elapsed < 1.0  # honors the 0.01s override, not the 5s configured default

    release_worker.set()
    await first
