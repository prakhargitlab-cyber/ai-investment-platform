"""Guardian Review (STAGE2_LIVE_RUN_DEFECTS_20260930.md, Issues 1/2/14) --
single-flight key lifetime across a PDF extraction timeout.

Reproduces the exact production pattern in the evidence doc: AKMEFINTRADE's
PDF extraction worker ran ~158s after a 20s caller timeout and eventually
succeeded, but the successful parse was discarded (ownership=DISCARDED)
because _run_official_filing_flight's `finally` block popped the
single-flight registry key (repository._official_filing_flights) the instant
the caller's own PdfExtractionTimeoutError propagated -- well before the
underlying worker thread had actually finished. That premature pop is also
why CORONAREMEDIES's ~4.2MB PDF was downloaded and extracted twice within
minutes: the single-flight protection vanished before the first attempt's
worker thread had genuinely stopped.

The fix: on a PdfExtractionTimeoutError specifically, the key is left in
place (`defer_key_cleanup`); an `on_late_result` callback passed into
HttpResearchFetcher.process_network_response_async persists a late SUCCESS
(reusing the existing _ingest_registered_fetch_result_async path) or simply
notes a late FAILURE, and only THEN pops the key -- once the worker's real
outcome is known, not merely the caller's patience.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from dataclasses import replace

from app.models import DocumentStatus
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchResult, HttpResearchFetcher, NetworkFetchResult, PdfExtractionTimeoutError
from app.settings import Settings
from app.normalization import canonicalize_url

from test_research_readiness_runtime import _profile
from test_di11c_official_document_budget import _official_source

NSE_SYMBOL = "READY"  # matches _profile()'s ticker, so _has_trusted_nse_profile_identity succeeds


async def _until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


def _repo_with_blocking_pdf_fetcher(*, extraction_timeout_seconds: float = 0.02):
    """A repository wired to a real HttpResearchFetcher whose
    process_network_response is monkeypatched to block a real worker thread
    until released -- the same shape used by test_research_engine.py's
    process_network_response_async tests, but reached through the full
    repository single-flight path (_single_flight_official_filing ->
    _run_official_filing_flight)."""
    profile = _profile()
    repository = ResearchRepository(
        persistence=SqliteResearchPersistence(),
        settings=Settings(
            research_live_enabled=False,
            research_pdf_extraction_concurrency=1,
            research_official_document_extraction_timeout_seconds=extraction_timeout_seconds,
        ),
    )
    repository.profiles.append(profile)
    fetcher = HttpResearchFetcher(repository.settings)
    repository._fetcher = fetcher
    source = replace(
        _official_source(profile, "late-result-fixture", "FINANCIAL_RESULTS"),
        official_nse_profile_symbol=NSE_SYMBOL,
    )

    network_result = NetworkFetchResult(
        final_url=source.url, status_code=200, content_type="application/pdf",
        content=b"%PDF-fixture", headers={}, is_redirect=False, encoding=None,
        headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2,
    )
    call_count = 0

    async def fetch_network(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return network_result

    fetcher.fetch_network = fetch_network

    def call_count_fn():
        return call_count

    return repository, profile, source, fetcher, call_count_fn


@pytest.mark.asyncio
async def test_timeout_defers_single_flight_key_cleanup_and_late_success_is_persisted(monkeypatch):
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher()
    key = (profile.instrument_id, canonicalize_url(source.url))

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult(
            source.url, 200, "application/pdf", "AKMEFINTRADE quarterly revenue 42 crore INR standalone net profit up sharply", 80,
            extraction_status="EXTRACTED",
        )

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await _until(worker_started.is_set)

    with pytest.raises(PdfExtractionTimeoutError):
        await leader

    # Guardian Review fix: the key must still be present -- the worker is
    # still running in the background and a follower must join it, not
    # start a brand new duplicate download+extraction.
    assert key in repository._official_filing_flights
    assert call_count() == 1  # exactly one download so far

    # Let the worker genuinely finish (its late SUCCESS).
    release_worker.set()
    await _until(lambda: key not in repository._official_filing_flights)

    # The late success must have been persisted via the normal ingestion
    # path -- not merely logged and discarded.
    persisted = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source.url or doc.original_url == source.url
    ]
    assert len(persisted) == 1
    assert persisted[0].status in (DocumentStatus.PARSED, DocumentStatus.PROCESSED)
    assert "AKMEFINTRADE" in (persisted[0].normalized_text or persisted[0].raw_text or "")


@pytest.mark.asyncio
async def test_duplicate_request_during_timeout_window_joins_flight_not_new_extraction(monkeypatch):
    """CORONAREMEDIES-pattern regression: a second request for the SAME
    document arriving while the first's worker is still running (post
    caller-timeout) must join the existing flight rather than kick off a
    second download+extraction of the identical document."""
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher()

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult(source.url, 200, "application/pdf", "sufficiently long extracted document text content here", 60, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await _until(worker_started.is_set)
    with pytest.raises(PdfExtractionTimeoutError):
        await leader

    assert call_count() == 1

    # A follower arrives while the worker is still running in the background.
    follower = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await asyncio.sleep(0.01)
    with pytest.raises(PdfExtractionTimeoutError):
        await follower

    # Still exactly one download -- the follower joined the SAME flight
    # (asyncio.shield) instead of starting a second one.
    assert call_count() == 1

    release_worker.set()
    key = (profile.instrument_id, canonicalize_url(source.url))
    await _until(lambda: key not in repository._official_filing_flights)
    assert call_count() == 1


@pytest.mark.asyncio
async def test_late_failure_releases_key_and_future_retry_starts_fresh(monkeypatch):
    """A late FAILURE (not a late success) must still release the
    single-flight key, so a genuinely new future retry is not blocked
    forever by a document that never actually succeeded."""
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher()
    key = (profile.instrument_id, canonicalize_url(source.url))

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_then_fail(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        raise ValueError("late parse failure")

    monkeypatch.setattr(fetcher, "process_network_response", blocking_then_fail)

    leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await _until(worker_started.is_set)
    with pytest.raises(PdfExtractionTimeoutError):
        await leader

    assert key in repository._official_filing_flights
    release_worker.set()
    await _until(lambda: key not in repository._official_filing_flights)

    # No document should have been persisted for a late failure.
    assert not [d for d in repository.documents.values() if d.canonical_url == source.url]

    # A fresh retry now must be a genuinely new flight (new download), not a
    # permanently stuck registry entry.
    def fast_success(_response, *, max_bytes=None):
        return FetchResult(source.url, 200, "application/pdf", "fresh retry extracted document text content is long enough now", 65, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", fast_success)
    document, joined_in_flight = await repository._single_flight_official_filing(profile, source)
    assert joined_in_flight is False
    assert call_count() == 2
    assert document.canonical_url == source.url or document.original_url == source.url
