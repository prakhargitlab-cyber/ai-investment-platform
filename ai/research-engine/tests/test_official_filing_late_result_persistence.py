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
from uuid import uuid4

import pytest

from dataclasses import replace

from app.models import DocumentStatus
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.readiness_signals import evidence_notifications
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

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
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

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
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

    def blocking_then_fail(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
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
    def fast_success(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        return FetchResult(source.url, 200, "application/pdf", "fresh retry extracted document text content is long enough now", 65, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", fast_success)
    document, joined_in_flight = await repository._single_flight_official_filing(profile, source)
    assert joined_in_flight is False
    assert call_count() == 2
    assert document.canonical_url == source.url or document.original_url == source.url


@pytest.mark.asyncio
async def test_cancelled_caller_defers_key_cleanup_and_late_success_is_persisted(monkeypatch):
    """Regression test for the CancelledError gap (Track C-cancel).

    Before the fix, when the caller of _single_flight_official_filing was
    cancelled (asyncio.CancelledError, e.g. the interactive API path) while
    the shielded PDF extraction worker was still running, _run_official_filing_flight's
    except-PdfExtractionTimeoutError branch did NOT catch CancelledError, so
    ``defer_key_cleanup`` stayed False and the finally block popped the
    single-flight key immediately -- while on_late_result (registered only in
    process_network_response_async's TimeoutError branch, not its
    CancelledError branch) never fired. The shielded worker thread kept running
    with no late-result handler, and a follower arriving in that window would
    start a brand-new duplicate download+extraction.

    The fix: process_network_response_async now registers the same
    _deliver_late_result done-callback on its CancelledError branch as on its
    TimeoutError branch, and _run_official_filing_flight catches CancelledError
    alongside PdfExtractionTimeoutError to set defer_key_cleanup=True.

    This test verifies:
    (1) the key is NOT popped while the worker is still running post-cancellation
    (2) a follower joining in that window does NOT trigger a second download
    (3) when the worker eventually succeeds, _on_late_result fires, persists
        the late success, and pops the key
    """
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher(
        extraction_timeout_seconds=2.0,  # generous so only cancellation, not timeout, triggers
    )
    key = (profile.instrument_id, canonicalize_url(source.url))

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult(
            source.url, 200, "application/pdf",
            "CANCELLED_CALLER_LATE_SUCCESS quarterly results for testing", 50,
            extraction_status="EXTRACTED",
        )

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await _until(worker_started.is_set)

    # Cancel the leader while the worker thread is still running (shielded).
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # (1) The key must STILL be present: the worker is still running and
    #     defer_key_cleanup=True prevents the finally from popping it.
    assert key in repository._official_filing_flights
    assert call_count() == 1  # exactly one download

    # (2) A follower arriving now must JOIN the existing (cancelled) flight,
    #     not start a new download. asyncio.shield of a cancelled task
    #     raises CancelledError immediately without creating duplicate work.
    follower = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError):
        await follower

    # Still exactly one download -- no duplicate was created.
    assert call_count() == 1

    # (3) Let the worker genuinely finish with a SUCCESS. The
    #     _deliver_late_result callback (registered on CancelledError in
    #     process_network_response_async) must invoke _on_late_result,
    #     which persists the late success and pops the key.
    release_worker.set()
    await _until(lambda: key not in repository._official_filing_flights)

    # The late success must have been persisted.
    persisted = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source.url or doc.original_url == source.url
    ]
    assert len(persisted) == 1
    assert "CANCELLED_CALLER_LATE_SUCCESS" in (persisted[0].normalized_text or persisted[0].raw_text or "")


@pytest.mark.asyncio
async def test_cancelled_caller_late_failure_releases_key(monkeypatch):
    """Same as above but the late worker FAILS -- the key must still be
    released (on_late_result fires with the exception) so a future retry
    can start a fresh flight rather than being permanently stuck on a
    cancelled-flight placeholder."""
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher(
        extraction_timeout_seconds=2.0,
    )
    key = (profile.instrument_id, canonicalize_url(source.url))

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_then_fail(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        raise ValueError("late worker failure after caller cancelled")

    monkeypatch.setattr(fetcher, "process_network_response", blocking_then_fail)

    leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
    await _until(worker_started.is_set)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # Key still present while worker runs.
    assert key in repository._official_filing_flights
    assert call_count() == 1

    release_worker.set()
    await _until(lambda: key not in repository._official_filing_flights)

    # No document persisted for a late failure.
    assert not [d for d in repository.documents.values() if d.canonical_url == source.url]

    # Future retry is a genuinely fresh flight (new download).
    def fast_success(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        return FetchResult(source.url, 200, "application/pdf", "fresh retry after cancelled failure text content", 45, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", fast_success)
    document, joined_in_flight = await repository._single_flight_official_filing(profile, source)
    assert joined_in_flight is False
    assert call_count() == 2
    assert document.canonical_url == source.url or document.original_url == source.url


@pytest.mark.asyncio
async def test_queue_timeout_does_not_defer_key_cleanup_and_key_is_not_stranded(monkeypatch):
    """Regression test for the queue-timeout stranded-key bug.

    Before the fix, _run_official_filing_flight's except-PdfExtractionTimeoutError
    branch deferred single-flight key cleanup unconditionally for ANY
    PdfExtractionTimeoutError, including PDF_EXTRACTION_QUEUE_TIMEOUT. But a
    queue-timeout means this call never got a worker admitted into the
    executor at all -- no task exists, so on_late_result (registered only on
    an admitted worker's task) never fires, and nothing would ever pop the
    deferred key. Every subsequent request for that exact document would
    replay the same stale PDF_EXTRACTION_QUEUE_TIMEOUT forever.

    This reproduces the RUNTIME EXAMPLE 2 / 3 pattern (a second, different
    document queued behind a legitimately slow, successfully-completing
    extraction): document A occupies the sole extraction slot; document B
    (a DIFFERENT document, same instrument) times out merely waiting to be
    admitted.
    """
    profile = _profile()
    repository = ResearchRepository(
        persistence=SqliteResearchPersistence(),
        settings=Settings(
            research_live_enabled=False,
            research_pdf_extraction_concurrency=1,
            research_official_document_extraction_timeout_seconds=0.05,
        ),
    )
    repository.profiles.append(profile)
    fetcher = HttpResearchFetcher(repository.settings)
    repository._fetcher = fetcher

    source_a = replace(_official_source(profile, "queue-fixture-a", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)
    source_b = replace(_official_source(profile, "queue-fixture-b", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)

    network_a = NetworkFetchResult(final_url=source_a.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-a", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)
    network_b = NetworkFetchResult(final_url=source_b.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-b", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)

    async def fetch_network(url, *_args, **_kwargs):
        return network_a if url == source_a.url else network_b

    fetcher.fetch_network = fetch_network

    worker_a_started = threading.Event()
    release_worker_a = threading.Event()

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_a.url:
            worker_a_started.set()
            assert release_worker_a.wait(timeout=3)
            return FetchResult(source_a.url, 200, "application/pdf",
                                "document A eventually succeeded after the slot freed up",
                                60, extraction_status="EXTRACTED")
        raise AssertionError("document B must never reach process_network_response "
                              "while A holds the sole extraction slot")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    leader_a = asyncio.create_task(repository._single_flight_official_filing(profile, source_a))
    await _until(worker_a_started.is_set)

    # B tries to fetch while A holds the only extraction slot. B's own
    # queue-admission wait (bounded by the same tiny
    # research_official_document_extraction_timeout_seconds) expires before
    # A releases, so B must fail with PDF_EXTRACTION_QUEUE_TIMEOUT.
    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_QUEUE_TIMEOUT"):
        await repository._single_flight_official_filing(profile, source_b)

    key_b = (profile.instrument_id, canonicalize_url(source_b.url))
    # The fix: B's key must NOT be stranded -- nothing will ever call
    # on_late_result for B (it never got a worker), so the key must be
    # released immediately, not deferred.
    assert key_b not in repository._official_filing_flights

    # Free A's slot before retrying B -- otherwise B's retry would hit
    # QUEUE_TIMEOUT again for the mundane reason that A is still running,
    # which would prove nothing about whether B's key was stranded.
    release_worker_a.set()
    try:
        await leader_a
    except PdfExtractionTimeoutError:
        # A's own caller may also have timed out waiting on its worker
        # (same tiny budget); that is the pre-existing, already-tested
        # worker-timeout/late-result path and not what this test covers.
        pass

    # A subsequent attempt for B (e.g. the next retry/cycle) must start a
    # genuinely fresh flight -- not replay the same stale exception forever.
    def process_network_response_after(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_b.url:
            return FetchResult(source_b.url, 200, "application/pdf",
                                "document B succeeds on the fresh retry attempt",
                                55, extraction_status="EXTRACTED")
        raise AssertionError("unexpected call for A on the retry")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response_after)
    document_b, joined_in_flight_b = await repository._single_flight_official_filing(profile, source_b)
    assert joined_in_flight_b is False
    assert document_b.canonical_url == source_b.url or document_b.original_url == source_b.url


@pytest.mark.asyncio
async def test_cancellation_during_queue_admission_does_not_strand_key(monkeypatch):
    """Regression test: a caller cancelled while still waiting to be admitted
    into the extraction slot (no worker task created yet) must not have its
    single-flight key deferred -- nothing will ever call on_late_result for a
    call that never got a worker, so deferring would strand the key forever
    exactly like the queue-timeout case above."""
    profile = _profile()
    repository = ResearchRepository(
        persistence=SqliteResearchPersistence(),
        settings=Settings(
            research_live_enabled=False,
            research_pdf_extraction_concurrency=1,
            # Generous enough that our own cancellation -- not this timeout --
            # is what interrupts B's admission wait.
            research_official_document_extraction_timeout_seconds=5.0,
        ),
    )
    repository.profiles.append(profile)
    fetcher = HttpResearchFetcher(repository.settings)
    repository._fetcher = fetcher

    source_a = replace(_official_source(profile, "cancel-admission-a", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)
    source_b = replace(_official_source(profile, "cancel-admission-b", "FINANCIAL_RESULTS"),
                        official_nse_profile_symbol=NSE_SYMBOL)

    network_a = NetworkFetchResult(final_url=source_a.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-a", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)
    network_b = NetworkFetchResult(final_url=source_b.url, status_code=200, content_type="application/pdf",
                                    content=b"%PDF-b", headers={}, is_redirect=False, encoding=None,
                                    headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2)

    async def fetch_network(url, *_args, **_kwargs):
        return network_a if url == source_a.url else network_b

    fetcher.fetch_network = fetch_network

    worker_a_started = threading.Event()
    release_worker_a = threading.Event()

    def process_network_response(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        if response.final_url == source_a.url:
            worker_a_started.set()
            assert release_worker_a.wait(timeout=3)
            return FetchResult(source_a.url, 200, "application/pdf",
                                "document A eventually succeeds once its worker is released",
                                50, extraction_status="EXTRACTED")
        raise AssertionError("document B must never reach process_network_response")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response)

    leader_a = asyncio.create_task(repository._single_flight_official_filing(profile, source_a))
    await _until(worker_a_started.is_set)

    # B starts fetching while A holds the sole slot -- B will be stuck
    # waiting on the admission semaphore (no worker created for B yet).
    follower_b = asyncio.create_task(repository._single_flight_official_filing(profile, source_b))
    await asyncio.sleep(0.02)  # let B reach the semaphore wait

    # Cancel B specifically while it is still waiting for ADMISSION, not
    # while a worker is running.
    follower_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower_b

    key_b = (profile.instrument_id, canonicalize_url(source_b.url))
    # The fix: B's key must not be stranded -- B never got a worker, so
    # nothing will ever call on_late_result to pop a deferred key.
    # _run_official_filing_flight's own `finally` (where the pop happens)
    # runs on B's flight task, a tick after this test observes follower_b's
    # CancelledError, so poll briefly rather than asserting immediately.
    await _until(lambda: key_b not in repository._official_filing_flights)

    # A fresh retry for B must succeed normally (not replay a stale error).
    release_worker_a.set()
    await leader_a  # let A finish and free the slot (expected to succeed)

    def process_network_response_after(response, *, max_bytes=None, extraction_deadline_monotonic=None):
        return FetchResult(source_b.url, 200, "application/pdf",
                            "document B succeeds on the fresh retry attempt after cancellation",
                            55, extraction_status="EXTRACTED")

    monkeypatch.setattr(fetcher, "process_network_response", process_network_response_after)
    document_b, joined_in_flight_b = await repository._single_flight_official_filing(profile, source_b)
    assert joined_in_flight_b is False
    assert document_b.canonical_url == source_b.url or document_b.original_url == source_b.url


@pytest.mark.asyncio
async def test_late_success_notifies_still_running_acquisition_via_evidence_committed(monkeypatch):
    """Reproduces the UGROCAP/PARSVNATH/SHIPROCKET live pattern: a genuine
    execution-wait PDF_EXTRACTION_TIMEOUT fires at the caller's own
    timeout, the shielded worker later succeeds, and the late result is
    persisted via _on_late_result -- but nothing told a still-running
    ResearchReadinessRuntime._execute_plan_until_ready() acquisition for
    the SAME candidate that new evidence had landed, so it never got a
    chance to re-read readiness before concluding DEEP_READINESS_NOT_MET.

    ResearchReadinessRuntime._execute_plan_until_ready() already wakes up
    on exactly this signal -- asyncio.Event set by
    app.readiness_signals.evidence_committed(), delivered through the
    evidence_notifications(instrument_id, changed) context manager -- for
    the structured/Yahoo snapshot path
    (PortfolioOrchestrationService._reconcile_structured_market). This test
    proves the official-filing late-result path now fires the same signal,
    using the SAME context-manager mechanism the runtime relies on, without
    standing up the full runtime.
    """
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher()

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult(
            source.url, 200, "application/pdf",
            "UGROCAP quarterly revenue up strongly this period standalone net profit",
            80, extraction_status="EXTRACTED",
        )

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    changed = asyncio.Event()
    # Mirrors exactly how ResearchReadinessRuntime._execute_plan_until_ready
    # wraps its per-candidate acquisition task: the flight (and everything
    # it awaits, including _on_late_result's done-callback continuation) is
    # created INSIDE this context so a later evidence_committed() call for
    # this same instrument_id reaches this specific `changed` Event.
    with evidence_notifications(profile.instrument_id, changed):
        leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
        await _until(worker_started.is_set)

        with pytest.raises(PdfExtractionTimeoutError):
            await leader

        # No late success has landed yet -- the signal must not fire early.
        assert not changed.is_set()

        # Let the worker genuinely finish (its late SUCCESS, ~4-75s later
        # in production; immediate here since the fixture is deterministic).
        release_worker.set()
        await _until(lambda: changed.is_set())

    # Persisted exactly once via the normal ingestion path -- the wakeup
    # signal is observability for an already-durable result, not a second
    # parse of the same document.
    assert call_count() == 1
    persisted = [
        doc for doc in repository.documents.values()
        if doc.canonical_url == source.url or doc.original_url == source.url
    ]
    assert len(persisted) == 1
    assert persisted[0].status in (DocumentStatus.PARSED, DocumentStatus.PROCESSED)


@pytest.mark.asyncio
async def test_late_success_does_not_notify_a_different_instrument(monkeypatch):
    """evidence_committed() is identity-checked against the listener's own
    instrument_id (app/readiness_signals.py); a late result for one
    instrument must never wake a different instrument's acquisition."""
    repository, profile, source, fetcher, call_count = _repo_with_blocking_pdf_fetcher()

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult(
            source.url, 200, "application/pdf",
            "SHIPROCKET late success for a different candidate's listener",
            60, extraction_status="EXTRACTED",
        )

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    other_instrument_changed = asyncio.Event()
    other_instrument_id = uuid4()
    assert other_instrument_id != profile.instrument_id

    with evidence_notifications(other_instrument_id, other_instrument_changed):
        leader = asyncio.create_task(repository._single_flight_official_filing(profile, source))
        await _until(worker_started.is_set)

        with pytest.raises(PdfExtractionTimeoutError):
            await leader

        release_worker.set()
        key = (profile.instrument_id, canonicalize_url(source.url))
        await _until(lambda: key not in repository._official_filing_flights)

        # The late success landed (persisted), but for a DIFFERENT
        # instrument than the one this listener is scoped to.
        assert not other_instrument_changed.is_set()
