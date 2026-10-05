"""FINAL TARGETED TASK -- same-candidate PDF queue-timeout recovery.

Reproduces the remaining demonstrated defect from the SHIPROCKET/UGROCAP live
examples: when document A legitimately occupies the sole PDF extraction slot
(research_pdf_extraction_concurrency=1) longer than another document B's
queue-admission deadline, B used to receive a bounded, truthful
PDF_EXTRACTION_QUEUE_TIMEOUT and then simply move on -- Part C already
guarantees B's single-flight key is released (not stranded), but nothing
gave B a chance to retry once A's slot actually freed up within the SAME
candidate acquisition batch (app.repository.ResearchRepository._fetch_official_filings).

The fix lives in _fetch_official_filings's per-worker dispatch
(_fetch_with_bounded_queue_timeout_retry): a PDF_EXTRACTION_QUEUE_TIMEOUT
specifically (never PDF_EXTRACTION_TIMEOUT, never any other FetchError) gets
exactly one retry, coordinated purely through the existing PDF extraction
semaphore's own admission wait -- no new timeout constant, no sleep.

A is pinned onto the sole extraction slot via a DIRECT
_single_flight_official_filing(profile, source_a) call, started and
confirmed running (worker_a_started) BEFORE B's _fetch_official_filings
batch is even created. This makes the scenario deterministic: without it,
two _fetch_official_filings workers created at the same time race for
which cursor-dispatched document reaches the shared semaphore first, which
is exactly the ambiguity a bounded-retry regression test cannot afford.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from dataclasses import replace

from app.normalization import canonicalize_url
from app.repository import ResearchRepository
from app.research_fetching import FetchResult, HttpResearchFetcher, NetworkFetchResult, PdfExtractionTimeoutError
from app.settings import Settings
from app.source_discovery import DiscoveryResult

from test_research_readiness_runtime import _profile
from test_di11c_official_document_budget import _official_source

NSE_SYMBOL = "READY"  # matches _profile()'s ticker, so _has_trusted_nse_profile_identity succeeds


async def _until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


def _repo_with_two_documents(*, extraction_timeout_seconds: float = 0.05):
    """Two official filings (A, B) for the SAME instrument, sharing one
    HttpResearchFetcher with research_pdf_extraction_concurrency=1.

    NOTE: _run_official_filing_flight always passes
    research_official_document_extraction_timeout_seconds as the
    ``extraction_timeout_seconds`` keyword into
    process_network_response_async, and that per-call value -- being
    never None in production -- always wins over
    research_pdf_extraction_queue_timeout_seconds inside
    process_network_response_async's own
    ``queue_timeout = extraction_timeout_seconds if extraction_timeout_seconds
    is not None else self.settings.research_pdf_extraction_queue_timeout_seconds``.
    So the ONE setting that actually governs admission-wait AND
    execution-wait for this call path today is
    research_official_document_extraction_timeout_seconds; the dedicated
    queue-timeout setting is effectively unreachable here. This fixture
    deliberately uses that one real knob rather than the unused one, to
    stay honest about what the production code actually does."""
    profile = _profile()
    repository = ResearchRepository(
        settings=Settings(
            research_live_enabled=False,
            research_pdf_extraction_concurrency=1,
            research_official_document_extraction_timeout_seconds=extraction_timeout_seconds,
            research_official_document_fetch_concurrency=2,
        ),
    )
    repository.profiles.append(profile)
    fetcher = HttpResearchFetcher(repository.settings)
    repository._fetcher = fetcher

    source_a = replace(_official_source(profile, "queue-retry-a", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)
    source_b = replace(_official_source(profile, "queue-retry-b", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)

    network_a = NetworkFetchResult(final_url=source_a.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-a", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)
    network_b = NetworkFetchResult(final_url=source_b.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-b", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)

    fetch_network_calls: dict[str, int] = {"A": 0, "B": 0}

    async def fetch_network(url, *_args, **_kwargs):
        if url == source_a.url:
            fetch_network_calls["A"] += 1
            return network_a
        fetch_network_calls["B"] += 1
        return network_b

    fetcher.fetch_network = fetch_network

    return repository, profile, source_a, source_b, fetcher, fetch_network_calls


@pytest.mark.asyncio
async def test_queue_timeout_follower_gets_bounded_retry_and_succeeds_once_slot_frees(monkeypatch):
    """Pattern A: B queue-times-out once, A frees the slot shortly after,
    B's single bounded retry succeeds in the SAME _fetch_official_filings
    batch -- not a future Radar cycle. No duplicate B extraction."""
    repository, profile, source_a, source_b, fetcher, fetch_network_calls = _repo_with_two_documents()

    worker_a_started = threading.Event()
    release_worker_a = threading.Event()
    process_b_calls = {"count": 0}

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_a.url:
            worker_a_started.set()
            assert release_worker_a.wait(timeout=3)
            return FetchResult(source_a.url, 200, "application/pdf",
                                "document A quarterly revenue up strongly standalone net profit",
                                70, extraction_status="EXTRACTED")
        process_b_calls["count"] += 1
        return FetchResult(source_b.url, 200, "application/pdf",
                            "document B succeeds once the retry finds the slot free",
                            65, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    # Pin A onto the sole extraction slot deterministically, independent of
    # _fetch_official_filings' own worker-dispatch scheduling.
    leader_a = asyncio.create_task(repository._single_flight_official_filing(profile, source_a))
    await _until(worker_a_started.is_set)

    filings = [DiscoveryResult(source_b.categories[0], source_b)]
    batch = asyncio.create_task(repository._fetch_official_filings(profile, filings, set()))

    # Let B's FIRST queue-admission attempt genuinely expire (bounded by
    # research_pdf_extraction_queue_timeout_seconds=0.05) before freeing A --
    # otherwise this would only prove normal admission, not the retry path.
    await asyncio.sleep(0.08)
    release_worker_a.set()

    await asyncio.wait_for(batch, timeout=3)
    try:
        await leader_a  # let A's own flight finish too
    except PdfExtractionTimeoutError:
        # A's own direct caller uses the same short extraction_timeout_seconds
        # budget, so it may also give up before its worker (held open by
        # release_worker_a) actually returns -- irrelevant to this test,
        # which only cares about B's retry behavior.
        pass

    # Exactly one retry: two flights for B (the original queue-timed-out
    # attempt plus one retry), each re-downloading since no worker was ever
    # admitted for the first -- but only ONE extraction worker ever actually
    # ran for B.
    assert fetch_network_calls["B"] == 2
    assert process_b_calls["count"] == 1

    persisted_b = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source_b.url or doc.original_url == source_b.url
    ]
    assert len(persisted_b) == 1
    key_b = (profile.instrument_id, canonicalize_url(source_b.url))
    assert key_b not in repository._official_filing_flights


@pytest.mark.asyncio
async def test_queue_timeout_follower_second_queue_timeout_is_bounded_no_third_attempt(monkeypatch):
    """Pattern B: B queue-times-out, its one retry ALSO queue-times-out
    (A still has not freed the slot) -> a truthful, bounded technical
    failure. No third attempt, no duplicate worker, no stranded key."""
    repository, profile, source_a, source_b, fetcher, fetch_network_calls = _repo_with_two_documents()

    worker_a_started = threading.Event()
    release_worker_a = threading.Event()

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_a.url:
            worker_a_started.set()
            assert release_worker_a.wait(timeout=3)
            return FetchResult(source_a.url, 200, "application/pdf",
                                "document A still running through both of B's attempts",
                                70, extraction_status="EXTRACTED")
        raise AssertionError("document B must never reach process_network_response -- "
                              "A must still hold the sole slot through BOTH of B's attempts")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    leader_a = asyncio.create_task(repository._single_flight_official_filing(profile, source_a))
    await _until(worker_a_started.is_set)

    filings = [DiscoveryResult(source_b.categories[0], source_b)]
    batch = asyncio.create_task(repository._fetch_official_filings(profile, filings, set()))

    # Hold A well past TWO queue-timeout windows (first attempt + one retry)
    # before releasing, so the retry itself genuinely queue-times-out too.
    asyncio.get_running_loop().call_later(0.25, release_worker_a.set)

    await asyncio.wait_for(batch, timeout=3)
    try:
        await leader_a
    except PdfExtractionTimeoutError:
        pass

    # Bounded: exactly one retry attempted (two fetch_network calls total),
    # never a third.
    assert fetch_network_calls["B"] == 2
    persisted_b = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source_b.url or doc.original_url == source_b.url
    ]
    assert len(persisted_b) == 0
    key_b = (profile.instrument_id, canonicalize_url(source_b.url))
    # Bounded failure must not strand the key either -- a later cycle can
    # still retry fresh.
    assert key_b not in repository._official_filing_flights


@pytest.mark.asyncio
async def test_genuine_execution_timeout_is_not_retried_by_the_queue_timeout_mechanism(monkeypatch):
    """Scenario C: a genuine execution-wait PDF_EXTRACTION_TIMEOUT (a worker
    IS alive, shielded, pursuing the Part D late-result/evidence_committed
    path) must never be retried by this NEW mechanism -- that would start a
    duplicate worker for a document that is already being extracted."""
    repository, profile, source_a, source_b, fetcher, fetch_network_calls = _repo_with_two_documents(
        extraction_timeout_seconds=0.05,
    )

    process_a_calls = {"count": 0}
    release_worker_a = threading.Event()

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        process_a_calls["count"] += 1
        assert release_worker_a.wait(timeout=3)
        return FetchResult(source_a.url, 200, "application/pdf",
                            "document A finishes late via the existing Part D late-result path",
                            70, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    filings = [DiscoveryResult(source_a.categories[0], source_a)]
    batch = asyncio.create_task(repository._fetch_official_filings(profile, filings, set()))
    # A's own caller gives up (extraction_timeout_seconds=0.05) while the
    # worker is still genuinely running -- _fetch_official_filings' worker
    # loop must NOT see this as PDF_EXTRACTION_QUEUE_TIMEOUT and must NOT
    # retry (which would submit a second, duplicate worker for A).
    await asyncio.sleep(0.2)
    assert process_a_calls["count"] == 1  # no duplicate worker submitted while the first is still alive

    release_worker_a.set()
    await asyncio.wait_for(batch, timeout=3)

    # The Part D late-result path persists A asynchronously (download
    # released, extraction result ingested via asyncio.to_thread); give it a
    # bounded window to actually land before asserting, rather than racing
    # the event loop's own teardown.
    await _until(
        lambda: any(
            doc.canonical_url == source_a.url or doc.original_url == source_a.url
            for doc in repository.documents.values()
        ),
        timeout=2.0,
    )

    # Still exactly one worker ever ran for A, and the late success was
    # still persisted via the untouched Part D path.
    assert process_a_calls["count"] == 1
    assert fetch_network_calls["A"] == 1


@pytest.mark.asyncio
async def test_pdf_parse_error_is_not_retried_and_keeps_existing_taxonomy(monkeypatch):
    """Scenario D: PDF_PARSE_ERROR is a plain FetchError raised by the
    extraction worker itself (research_fetching.py's PdfReadError branch),
    never a PdfExtractionTimeoutError. _fetch_with_bounded_queue_timeout_retry
    only ever intercepts PdfExtractionTimeoutError whose message is exactly
    "PDF_EXTRACTION_QUEUE_TIMEOUT" -- a bare FetchError("PDF_PARSE_ERROR")
    does not match that except clause at all, so it must propagate through
    unchanged, exactly once, with no retry and no broadened taxonomy."""
    from app.research_fetching import FetchError

    repository, profile, source_a, source_b, fetcher, fetch_network_calls = _repo_with_two_documents()

    process_a_calls = {"count": 0}

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        process_a_calls["count"] += 1
        raise FetchError("PDF_PARSE_ERROR")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    filings = [DiscoveryResult(source_a.categories[0], source_a)]
    completed_without_failure = await repository._fetch_official_filings(profile, filings, set())

    # Exactly one attempt -- no retry of a non-queue-timeout FetchError.
    assert process_a_calls["count"] == 1
    assert fetch_network_calls["A"] == 1
    assert completed_without_failure is False
    persisted_a = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source_a.url or doc.original_url == source_a.url
    ]
    assert len(persisted_a) == 0
    key_a = (profile.instrument_id, canonicalize_url(source_a.url))
    assert key_a not in repository._official_filing_flights


@pytest.mark.asyncio
async def test_cancellation_during_bounded_retry_leaves_no_stranded_key_or_tasks(monkeypatch):
    """Scenario E: if the whole candidate acquisition is cancelled while B's
    bounded retry is in flight (waiting on the real extraction semaphore),
    Part C's single-flight cleanup must still run for that retry attempt --
    no stranded _official_filing_flights key, and no orphaned worker task
    left behind once the cancellation has propagated."""
    repository, profile, source_a, source_b, fetcher, fetch_network_calls = _repo_with_two_documents()

    worker_a_started = threading.Event()
    release_worker_a = threading.Event()

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_a.url:
            worker_a_started.set()
            assert release_worker_a.wait(timeout=3)
            return FetchResult(source_a.url, 200, "application/pdf",
                                "document A holds the slot through B's cancellation",
                                70, extraction_status="EXTRACTED")
        raise AssertionError("document B must never reach process_network_response -- "
                              "the batch is cancelled before A ever frees the slot")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    leader_a = asyncio.create_task(repository._single_flight_official_filing(profile, source_a))
    await _until(worker_a_started.is_set)

    filings = [DiscoveryResult(source_b.categories[0], source_b)]
    batch = asyncio.create_task(repository._fetch_official_filings(profile, filings, set()))

    # Let B's first queue-admission attempt genuinely expire, so the retry
    # itself is the one in flight (waiting on the semaphore again) when the
    # whole batch gets cancelled.
    await asyncio.sleep(0.08)
    batch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch

    # Cancelling the WORKER'''s await only cancels that caller'''s own
    # asyncio.shield(flight) wait point (Part C'''s single-flight semantics
    # intentionally protect the shared flight task from one caller'''s
    # cancellation, in case another caller is also relying on it) -- it does
    # not instantly kill the retry'''s underlying flight task, which keeps
    # running in the background until its own admission wait resolves. So
    # "no stranded key" means the key clears once that background flight
    # genuinely finishes (bounded by the same extraction_timeout_seconds
    # budget), not that it vanishes the instant cancel() is called.
    release_worker_a.set()
    key_b = (profile.instrument_id, canonicalize_url(source_b.url))
    await _until(lambda: key_b not in repository._official_filing_flights, timeout=2.0)

    try:
        await leader_a
    except PdfExtractionTimeoutError:
        pass
