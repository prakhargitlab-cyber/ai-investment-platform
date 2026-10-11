"""Deterministic lifecycle tests: gated workers, no live providers or DB."""
import asyncio
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.research_fetching import HttpResearchFetcher, FetchError, PdfExtractionTimeoutError, TransportFetchError
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ResearchReadinessRuntime, CapabilityExecutionResult
from app.settings import Settings
from test_research_readiness_runtime import StateDataSource, RuntimeRepository, UpdatingExecutor, INSTRUMENT_ID
from test_research_engine import _pdf_network_fixture
from test_global_opportunity_baseline import setup_acquisition
from test_global_scanner import NOW
from test_stock_rule_engine import _readiness


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
async def test_owned_pdf_acquisition_past_deadline_is_bounded_not_accepted_late(monkeypatch, caplog):
    started, release = threading.Event(), threading.Event()
    class Page:
        def extract_text(self):
            started.set()
            assert release.wait(2)
            return "official financial statement"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    fetcher = HttpResearchFetcher(Settings())
    source = StateDataSource({"CURRENT_NEWS"})
    class Executor(UpdatingExecutor):
        async def execute_primary(self, key, targets, **kwargs):
            assert kwargs["deadline"] is None
            extracted = await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)
            assert extracted.text.endswith("official financial statement")
            # Simulated durable commit happens only AFTER the accepted result.
            source.missing.clear()
            return CapabilityExecutionResult(("OFFICIAL_DOCUMENT",), {})
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, Executor(source), ensure_timeout_seconds=.01)
    with caplog.at_level(logging.INFO):
        monkeypatch.setattr(logging.getLogger("app.research_readiness_runtime"), "level", logging.INFO)
        monkeypatch.setattr(logging.getLogger("app.research_fetching"), "level", logging.INFO)
        owner = asyncio.create_task(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
            requirement_ids=["CURRENT_NEWS"], wait_for_completion=True))
        try:
            await until(lambda: started.is_set() and "orchestration_wait_expired" in caplog.text)
            # `source.missing` is a deterministic checkpoint here: the only
            # line that clears it (`source.missing.clear()`, inside
            # Executor.execute_primary, below) runs strictly after the
            # worker thread's `release.wait(2)` returns, and `release` is
            # not set until the next line -- so this assertion cannot
            # observe a false pass regardless of scheduling.
            #
            # `owner.done()` is NOT such a checkpoint and must not be
            # asserted on here: once "orchestration_wait_expired" is logged,
            # nothing further in _execute_plan_bounded's cancel-and-drain
            # path needs the worker thread (which stays blocked on
            # `release.wait(2)`) to make progress, so the owning task can
            # finish unwinding and set owner.done()=True in the very same
            # scheduler pass that produced the log line -- before this
            # coroutine, parked in until()'s own polling loop, ever wakes up
            # to check. That race was observed directly in this sandbox
            # (intermittent here; the coarser default timer/scheduling
            # resolution on Windows made it fail far more consistently
            # there) and asserting on it added no coverage the rest of this
            # test doesn't already provide: `result = await owner` below
            # works identically whether owner is already done or not, and
            # the strict-deadline-contract outcome itself is verified by the
            # assertions on `result` immediately after.
            assert source.missing
            release.set()
            result = await owner
            # Strict deadline contract (adopted explicitly): a task whose
            # completion is only observed after ensure_timeout_seconds has
            # already elapsed and triggered cancellation must NOT be
            # accepted as an on-time result for THIS call, even though the
            # underlying worker thread (shielded inside
            # process_network_response_async, immune to asyncio
            # cancellation) keeps running to a real success in the
            # background. _execute_plan_until_ready's cancellation `finally`
            # cascades down into execute_primary, unwinding it via
            # CancelledError before its own commit line ever runs -- so this
            # owning call correctly reports the bounded acquisition timeout.
            # (The production on_late_result/evidence_committed() mechanism
            # in app.repository._run_official_filing_flight exists for a
            # DIFFERENT, still-running flight or a later retry to pick up
            # that late success -- never to resurrect the same already-
            # abandoned call, which is exactly what this test used to assert
            # before the OOM/stall-fix redesign (commit 1fd6ab2) made the
            # ensure_timeout_seconds + bounded-drain window authoritative.)
            assert result.failures == {"CURRENT_NEWS": "ACQUISITION_TIMEOUT"}
            assert result.readiness.for_requirement("CURRENT_NEWS").status != ResearchRequirementStatus.READY_FRESH
            assert "acquisition_timeout_result" in caplog.text
            # Resource-cleanup guarantee: the abandoned worker is still
            # properly reconciled (semaphore released, discard logged) once
            # it actually finishes, even though its result was discarded.
            await until(lambda: "pdf_extraction_discarded" in caplog.text)
            assert "ownership=DISCARDED" in caplog.text
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
            await fetcher._client.aclose()
            fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_abandoned_worker_never_publishes_extracted(monkeypatch, caplog, cancel):
    started, release = threading.Event(), threading.Event()
    class Page:
        def extract_text(self):
            started.set()
            assert release.wait(2)
            return "late extracted text"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    # Queue-admission and extraction-worker timeouts are two distinct,
    # independently configurable budgets in production (see
    # process_network_response_async's queue_timeout derivation) -- but
    # passing extraction_timeout_seconds explicitly collapses BOTH to the
    # same value, including admission, which this test never intends to
    # race against: admission here is uncontended (first and only call) and
    # is expected to be effectively instantaneous. On a slower/cold-started
    # process (observed on Windows: ~344ms before the semaphore acquire
    # resolved) a 10ms *admission* budget is not reliably survivable and
    # falsely raises PDF_EXTRACTION_QUEUE_TIMEOUT before the worker thread
    # is ever spawned -- `started` then never gets set, and the until()
    # below legitimately times out waiting for it. Setting the tight 10ms
    # budget only via the EXTRACTION-side settings default (and leaving
    # extraction_timeout_seconds=None, i.e. not overridden, for the
    # cancel=False call) keeps queue admission on its own generous default
    # budget while still forcing a fast, deterministic extraction-side
    # timeout -- the actual thing this test is about.
    fetcher = HttpResearchFetcher(Settings(research_official_document_extraction_timeout_seconds=.01))
    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        owner = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=1 if cancel else None))
        try:
            await until(started.is_set)
            if cancel:
                owner.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else PdfExtractionTimeoutError):
                await owner
            assert len(fetcher._active_pdf_extractions) == 1
            release.set()
            await until(lambda: not fetcher._active_pdf_extractions)
            assert "pdf_extraction_discarded" in caplog.text
            assert "fetch_extraction_complete " not in caplog.text
            assert "extractionStatus=EXTRACTED" not in caplog.text
            assert fetcher._pdf_extraction_semaphore._value == 1
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
            await until(lambda: not fetcher._active_pdf_extractions)
            await fetcher._client.aclose()
            fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_genuine_pdf_failure_and_network_timeout_remain_distinct(monkeypatch):
    def broken_pdf(_):
        raise ValueError("damaged PDF")
    monkeypatch.setattr("pypdf.PdfReader", broken_pdf)
    def network_timeout(request):
        raise httpx.ReadTimeout("offline simulated timeout", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(network_timeout))
    fetcher = HttpResearchFetcher(Settings(research_max_retries=0), client)
    try:
        with pytest.raises(FetchError, match="PDF_TEXT_EXTRACTION_FAILED"):
            await fetcher.process_network_response_async(_pdf_network_fixture())
        with pytest.raises(TransportFetchError, match="Fetch timed out"):
            await fetcher._request_with_retries("https://example.test/document.pdf")
        assert not fetcher._active_pdf_extractions
    finally:
        await client.aclose()
        fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_abandoned_worker_bounds_admission_without_starting_more_threads(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    class Page:
        def extract_text(self):
            calls.append(1)
            started.set()
            assert release.wait(2)
            return "text"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    # Same decoupling as test_abandoned_worker_never_publishes_extracted: the
    # FIRST call's admission is uncontended and must not race a 10ms budget
    # that was only ever meant to bound the (mocked-slow) extraction worker.
    # The second call below deliberately keeps extraction_timeout_seconds=.01
    # explicit -- that call's admission IS meant to be contended (the one
    # concurrency slot is already held by `first`'s worker), so collapsing
    # both budgets to 10ms there is correct and intended, not a race: it
    # will reach PDF_EXTRACTION_QUEUE_TIMEOUT regardless of whether that
    # takes 10ms or a few hundred ms on a slower host.
    fetcher = HttpResearchFetcher(Settings(research_official_document_extraction_timeout_seconds=.01))
    first = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture()))
    try:
        await until(started.is_set)
        with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
            await first
        with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_QUEUE_TIMEOUT"):
            await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=.01)
        assert calls == [1] and len(fetcher._active_pdf_extractions) == 1
        release.set()
        await until(lambda: not fetcher._active_pdf_extractions)
        result = await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)
        assert result.extraction_status == "EXTRACTED" and calls == [1, 1]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await until(lambda: not fetcher._active_pdf_extractions)
        await fetcher._client.aclose()
        fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_slow_owned_plan_does_not_block_fast_plan_and_cancellation_drains_children():
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    # ensure_timeout_seconds is intentionally generous (not the 10ms used
    # elsewhere in this module) and irrelevant to what this test proves:
    # `slow` is never meant to resolve via its OWN internal deadline here --
    # it is only ever ended by the explicit slow.cancel() below. This test
    # is about plan INDEPENDENCE (a slow owned plan must not block a
    # separate fast one) and cancellation cleanup, not the deadline contract
    # itself (that is covered by test_execute_plan_bounded_deadline_contract.py).
    # A 10ms budget made `assert not slow.done()` race the wall-clock time
    # genuinely spent constructing and running `fast_runtime.ensure()` to
    # completion in between -- on a slower host (observed on Windows) that
    # unrelated work alone can exceed 10ms, letting `slow` legitimately
    # self-time-out before this assertion runs, even though nothing about
    # plan independence or cancellation was actually broken. A budget this
    # test will never come close to exhausting removes that race while
    # leaving the assertion's meaning intact.
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=5.0)
    slow = asyncio.create_task(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"], wait_for_completion=True))
    await executor.started.wait()
    fast_source = StateDataSource({"CURRENT_NEWS"})
    fast_runtime = ResearchReadinessRuntime(RuntimeRepository(), fast_source, UpdatingExecutor(fast_source))
    fast = await fast_runtime.ensure(UUID(int=2), jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"], wait_for_completion=True)
    assert fast.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH
    assert not slow.done()
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    assert not runtime._flights
    assert source.missing == {"CURRENT_NEWS"}


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_25_deep_candidates_exceeding_acquisition_deadline_are_marked_timed_out(monkeypatch, caplog, incomplete):
    service, rows, _, _, tracker = setup_acquisition(monkeypatch, count=26)
    source = StateDataSource(set())
    committed = set()
    class Executor:
        async def execute_primary(self, key, targets, **kwargs):
            assert kwargs["deadline"] is None
            await asyncio.sleep(.004)  # exceeds the test request budget
            # Strict deadline contract: ensure_timeout_seconds=.001 elapses
            # while this sleep is still pending, so _execute_plan_bounded
            # cancels the owning task before this line (and the `committed`
            # commit it guards) is ever reached -- the `incomplete` branch
            # genuinely never gets a chance to run either way. A task whose
            # completion is only observed after its deadline must not be
            # accepted as an on-time success, so neither branch commits.
            if not incomplete:
                committed.add(key)
            return CapabilityExecutionResult(("FIXTURE_COMMIT",), {})
        async def execute_approved_fallbacks(self, *args):
            return CapabilityExecutionResult()
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, Executor(), ensure_timeout_seconds=.001)
    async def durable_read(key, **kwargs):
        overrides = {} if key in committed else {"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}
        return replace(_readiness(overrides=overrides), global_instrument_id=key)
    runtime.read = durable_read
    async def ensure(key, **kwargs):
        if kwargs.get("requirement_ids") is not None:
            if key == UUID(int=26):
                raise RuntimeError("isolated baseline failure")
            return await tracker(key, **kwargs)
        assert kwargs["wait_for_completion"] is True
        return await runtime.ensure(key, jurisdiction="INDIA", requirement_ids=None, wait_for_completion=True)
    service.readiness.ensure = ensure
    with caplog.at_level(logging.INFO, logger="app.research_readiness_runtime"):
        result = await service.run(rows, as_of=NOW, shortlist_limit=25)
    assert result.shortlist_count == result.deep_attempted_count == 25
    # Every one of the 25 candidates' deep acquisition genuinely exceeds the
    # ensure_timeout_seconds=.001 budget (the executor's own .004 sleep is
    # never allowed to finish before cancellation) -- under the strict
    # deadline contract this is deterministically an acquisition timeout for
    # all 25, identically regardless of `incomplete` (see the Executor
    # comment above: neither branch's commit is ever reached). This is an
    # intentional behavior change from the pre-OOM-fix design this test was
    # originally written against (commit aee3409), which awaited task
    # completion unconditionally past the stated budget; the OOM/stall-fix
    # redesign (commit 1fd6ab2) made the budget authoritative instead.
    assert result.deep_acquisition_timeout_count == 25
    assert result.rule_analyzed_count == 0
    assert result.deep_ready_count == 0
    assert result.deep_readiness_failed_count == 0
    assert result.suppressed_count == 25
    assert result.baseline_incomplete_count == 0
    # Candidate #26 is baseline-eligible but outside the 25-candidate
    # admission boundary (live baseline acquisition is now bounded to
    # admitted candidates only, same as deep investigation). It must be
    # explicitly DEFERRED before any live baseline acquisition is attempted
    # for it -- never counted as a live acquisition failure -- so its
    # injected RuntimeError("isolated baseline failure") must never fire.
    deferred = [d for d in result.diagnostics if d.status == "DEFERRED"]
    assert len(deferred) == 1
    assert deferred[0].global_instrument_id == UUID(int=26)
    assert sum(d.failure_reason == "BASELINE_ACQUISITION_FAILED" for d in result.diagnostics) == 0
    # Every non-deferred diagnostic (baseline- and deep-acquired) is one of
    # the 25 admitted identities; #26 never enters rule analysis, deep
    # research, or ranked output under any other disposition either.
    acquired_ids = {d.global_instrument_id for d in result.diagnostics if d.status != "DEFERRED"}
    assert acquired_ids <= {UUID(int=n) for n in range(1, 26)}
    assert UUID(int=26) not in acquired_ids
    # One default repair pass (Settings.research_opportunity_repair_passes
    # == 1, i.e. max_attempts == 2) retries every TECHNICAL_RETRYABLE
    # acquisition-timeout candidate exactly once (undoing and re-deriving
    # the same counters above), so each of the 25 candidates logs this
    # twice: 25 initial attempts + 25 repair-pass attempts, all identically
    # exceeding the .001 budget.
    assert caplog.text.count("orchestration_wait_expired") == 50
    assert not runtime._flights
    # No candidate ever reaches rule analysis (all 25 are suppressed on
    # acquisition timeout above), so nothing is ever rank-eligible, for
    # either parametrized branch.
    assert result.top_n == []
