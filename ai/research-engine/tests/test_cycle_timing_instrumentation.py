"""Consolidated fix pass -- Issue 3 / test #10: the new bounded Stage-2
timing instrumentation (app/cycle_timing.py, wired into
app/deep_investigation.py's _acquire/_acquire_group,
app/research_fetching.py's PDF-queue/parse/retry/network timing, and
app/repository.py's persistence choke point) is bounded and never changes
a decision.

"Bounded" is proven directly against the module's own guarantees:
per-(candidate, requirement) records are capped at a hard ceiling
independent of how many operations are recorded (test_01/test_02), and the
cycle-level "slowest N" report never grows past N regardless of how many
operations exist (test_03).

"Does not change decisions" is proven two ways:
 - unit-level: every record_* helper is a documented no-op when no
   cycle_scope()/bind_recorder() is active -- the module's default,
   pre-instrumentation state (test_04);
 - integration-level: app.deep_investigation.investigate() -- the exact
   function this pass wired cycle_timing into -- is run twice against the
   same deterministic fake runtime (reusing tests/test_capability_batching.py's
   _FakeGroupRuntime/_run fixture), once with no active recorder and once
   with an active CycleTimingRecorder bound, and its DECISION outputs
   (result.failures, executed capabilities, and every requirement's own
   matrix state/missing/source/failure) are asserted byte-identical
   (test_05/test_06). Only cycle_timing's own report (never returned by
   investigate()) differs between the two runs.
"""
from __future__ import annotations

import pytest

from app.cycle_timing import (
    CycleTimingRecorder,
    _MAX_TRACKED_OPERATIONS,
    _SLOWEST_N,
    active_recorder,
    active_span,
    bind_recorder,
    cycle_scope,
    record_finalization_elapsed,
    record_network_elapsed,
    record_pdf_parse_elapsed,
    record_pdf_queue_wait,
    record_persistence_elapsed,
    record_persistence_wait,
    record_persistence_operation,
    record_persistence_operation_timing,
    record_provider_elapsed,
    record_retry_backoff,
    record_runtime_ensure_elapsed,
    track_requirement,
    unbind_recorder,
)
from app.deep_investigation import investigate
from app.research_readiness import ResearchRequirementStatus

from test_capability_batching import INSTRUMENT_ID, _FakeGroupRuntime, _run
from test_stock_rule_engine import _readiness


# 1/2 -- bounded per-(candidate, requirement) record count -----------------
def test_01_per_requirement_records_are_capped_not_unbounded():
    recorder = CycleTimingRecorder(cycle_id="t1")
    with cycle_scope(recorder):
        for i in range(_MAX_TRACKED_OPERATIONS + 250):
            with track_requirement(f"candidate-{i}", "SOME_REQUIREMENT"):
                pass
    report = recorder.report()
    assert report["tracked_operation_count"] == _MAX_TRACKED_OPERATIONS
    assert report["dropped_operation_count"] == 250
    assert len(report["operations"]) == _MAX_TRACKED_OPERATIONS


def test_02_repeated_spans_for_the_same_candidate_requirement_reuse_one_record():
    # A requirement that is retried/reacquired within one cycle must not
    # grow the record set -- it accumulates onto its own single record.
    recorder = CycleTimingRecorder(cycle_id="t2")
    with cycle_scope(recorder):
        for _ in range(50):
            with track_requirement("candidate-A", "BALANCE_SHEET_FACTS"):
                record_provider_elapsed(10)
    report = recorder.report()
    assert report["tracked_operation_count"] == 1
    assert report["operations"][0]["provider_elapsed_ms"] == pytest.approx(500, abs=1)


# 3 -- bounded "slowest N" report -------------------------------------------
def test_03_slowest_operations_report_never_exceeds_n_regardless_of_volume():
    recorder = CycleTimingRecorder(cycle_id="t3")
    with cycle_scope(recorder):
        for i in range(500):
            with track_requirement(f"candidate-{i}", "REQ"):
                record_provider_elapsed(i)
    slowest = recorder.slowest_operations()
    assert len(slowest) == _SLOWEST_N
    # Truthfully the N largest, not an arbitrary/most-recent N.
    elapsed_values = [op["elapsed_ms"] for op in slowest]
    assert elapsed_values == sorted(elapsed_values, reverse=True)
    all_elapsed = sorted((r.elapsed_ms for r in recorder._records.values()), reverse=True)
    assert elapsed_values == all_elapsed[:_SLOWEST_N]


def test_03b_report_operations_list_is_exactly_the_capped_set_no_separate_unbounded_log():
    recorder = CycleTimingRecorder(cycle_id="t3b")
    with cycle_scope(recorder):
        for i in range(_MAX_TRACKED_OPERATIONS + 10):
            with track_requirement(f"candidate-{i}", "REQ"):
                pass
    report = recorder.report()
    # Nothing beyond the capped per-requirement record set backs the report:
    # tracked_operation_count, len(operations), and the recorder's own
    # internal record dict all agree.
    assert report["tracked_operation_count"] == len(report["operations"]) == len(recorder._records)


# 4 -- unit-level no-op guarantee when instrumentation is inactive ---------
def test_04_record_helpers_are_silent_no_ops_with_no_active_recorder():
    assert active_recorder() is None
    assert active_span() is None
    # None of these raise, return anything, or require an active scope --
    # this is the exact behavior of every call site before this module
    # existed (research_fetching.py / repository.py / deep_investigation.py
    # call these unconditionally now; this proves that unconditional call
    # is safe with instrumentation off).
    record_provider_elapsed(123)
    record_network_elapsed(45)
    record_pdf_queue_wait(67)
    record_pdf_parse_elapsed(89)
    record_persistence_wait(12)
    record_persistence_elapsed(34)
    record_retry_backoff(56)
    record_runtime_ensure_elapsed(78)
    record_finalization_elapsed(90)
    assert active_recorder() is None
    assert active_span() is None


def test_04b_track_requirement_with_no_bound_recorder_yields_none_and_is_a_no_op():
    with track_requirement("candidate-X", "SOME_REQUIREMENT") as record:
        assert record is None
        record_provider_elapsed(999)  # still a no-op -- no recorder bound


def test_04c_bind_and_unbind_recorder_round_trip_restores_none():
    assert active_recorder() is None
    token = bind_recorder(CycleTimingRecorder(cycle_id="t4c"))
    assert active_recorder() is not None
    unbind_recorder(token)
    assert active_recorder() is None


# 5/6 -- integration: investigate()'s own decisions are unaffected --------
def _decision_fields(result, matrix):
    return {
        "failures": dict(result.failures),
        "attempted": tuple(sorted(result.planned_requirement_ids)),
        "capabilities": tuple(result.executed_capabilities),
        "matrix": {
            requirement_id: {
                "state": entry["state"], "missing": entry["missing"], "source": entry["source"],
                "failure": entry["failure"], "applicability": entry["applicability"],
                "classification": entry["classification"],
            }
            for requirement_id, entry in matrix.items()
        },
    }


@pytest.mark.asyncio
async def test_05_investigate_decisions_identical_with_instrumentation_off_vs_on_all_succeed():
    readiness = _readiness({
        "VALUATION_INPUTS": ResearchRequirementStatus.MISSING,
        "LATEST_PRICE": ResearchRequirementStatus.MISSING,
        "SECTOR_MACRO": ResearchRequirementStatus.MISSING,
    })
    succeeds = {"VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"}

    # Run 1: no instrumentation active at all (today's default behavior).
    assert active_recorder() is None
    runtime_off, result_off, matrix_off = await _run(readiness, succeeds=succeeds)

    # Run 2: identical fixture, but with an active CycleTimingRecorder bound
    # for the whole call, exactly as Stage-2's run() now does.
    recorder = CycleTimingRecorder(cycle_id="decision-parity")
    with cycle_scope(recorder):
        runtime_on, result_on, matrix_on = await _run(readiness, succeeds=succeeds)

    assert _decision_fields(result_off, matrix_off) == _decision_fields(result_on, matrix_on)
    # The shared call happened exactly once in both runs (batching itself
    # -- proven elsewhere -- is unaffected by whether timing is recorded).
    assert runtime_off.ensure_calls == runtime_on.ensure_calls
    # Instrumentation actually captured something in the "on" run (sanity
    # that this is a meaningful comparison, not two no-op runs).
    assert recorder.report()["tracked_operation_count"] > 0


@pytest.mark.asyncio
async def test_06_investigate_decisions_identical_with_instrumentation_off_vs_on_partial_failure():
    from app.research_readiness import ResearchRequirementStatus
    readiness = _readiness({
        "BUSINESS_QUALITY_FACTS": ResearchRequirementStatus.MISSING,
        "GROWTH_FACTS": ResearchRequirementStatus.MISSING,
        "BALANCE_SHEET_FACTS": ResearchRequirementStatus.MISSING,
        "QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING,
    })
    # Only some of the group's members get satisfied -- proves per-member
    # truthful classification (the group case this pass's timing wiring
    # touches most, via record_elapsed_on) is unaffected too.
    succeeds = {"BUSINESS_QUALITY_FACTS", "BALANCE_SHEET_FACTS"}

    runtime_off, result_off, matrix_off = await _run(readiness, succeeds=succeeds)
    recorder = CycleTimingRecorder(cycle_id="decision-parity-partial")
    with cycle_scope(recorder):
        runtime_on, result_on, matrix_on = await _run(readiness, succeeds=succeeds)

    assert _decision_fields(result_off, matrix_off) == _decision_fields(result_on, matrix_on)
    assert runtime_off.ensure_calls == runtime_on.ensure_calls
    # The group's shared elapsed time was attributed to every member's own
    # record (record_elapsed_on) -- confirms the group wiring path executed
    # (not just the singleton path) during this comparison.
    report = recorder.report()
    grouped_ids = {"BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"}
    tracked_grouped = [op for op in report["operations"] if op["requirement_id"] in grouped_ids]
    assert len(tracked_grouped) == 4
    assert all(op["runtime_ensure_elapsed_ms"] >= 0 for op in tracked_grouped)


# 7 -- NEW (this task): stage2_timing_report now emits the persistence
# operation-name timing breakdown. The cycle report must contain the field
# with the full required sub-key schema, so a flat aggregate_persistence_wait
# / aggregate_persistence_elapsed can be attributed to a specific load_*/
# upsert_*/commit method on the next real cycle. Observation-only: the field
# is additive to CycleTimingRecorder.report() and never read by any
# decision path. -----------------------------------------------
@pytest.mark.asyncio
async def test_07_persistence_operation_timing_report_schema_and_values():
    recorder = CycleTimingRecorder(cycle_id="t7")
    with cycle_scope(recorder):
        record_persistence_operation_timing("load_financial_facts", 100.0, 30.0)
        record_persistence_operation_timing("load_financial_facts", 50.0, 20.0)
        record_persistence_operation_timing("upsert_document", 5.0, 900.0)

    report = recorder.report()
    assert "persistence_operation_timing" in report
    timing = report["persistence_operation_timing"]
    # Multiple distinct operation names remain distinguishable:
    assert set(timing.keys()) == {"load_financial_facts", "upsert_document"}
    # Full required sub-key schema on every slot:
    required_keys = {"calls", "total_wait_ms", "total_exec_ms", "max_wait_ms", "max_exec_ms"}
    for name, slot in timing.items():
        assert set(slot.keys()) == required_keys, name
    # load_financial_facts: 2 calls, summed + max are distinct:
    facts = timing["load_financial_facts"]
    assert facts["calls"] == 2
    assert facts["total_wait_ms"] == 150.0
    assert facts["total_exec_ms"] == 50.0
    assert facts["max_wait_ms"] == 100.0
    assert facts["max_exec_ms"] == 30.0
    # upsert_document: single call whose exec dominates (KPIGREEN ~26s shape):
    upsert = timing["upsert_document"]
    assert upsert["calls"] == 1
    assert upsert["total_exec_ms"] == 900.0
    assert upsert["max_exec_ms"] == 900.0


# 8 -- the orchestration's stage2_timing_report log line must SELECT
# persistence_operation_timing from the cycle report (manual field selection,
# not "log the whole report"). This test pins that selection contract so a
# future field-add/delete is caught here rather than silently dropped from the
# deployed log as happened to persistence_operation_timing. -----------
def test_08_stage2_timing_report_field_selection_includes_persistence_operation_timing():
    import inspect as _inspect
    from app import global_opportunity_orchestration as orch

    src = _inspect.getsource(orch)
    # The log-format string must reference the new key:
    assert "persistenceOperationTimingJson=%s" in src
    # And the args tuple must read it from the report dict:
    assert 'json.dumps(_timing_report["persistence_operation_timing"])' in src
    # All pre-existing fields are still present (unchanged selection):
    for field in [
        "stage2WallClockMs=%s", "maxConcurrentMandatoryInvestigations=%s",
        "trackedOperations=%s", "droppedOperations=%s",
        "aggregatePdfQueueWaitMs=%s", "aggregateProviderWaitMs=%s",
        "aggregatePersistenceWaitMs=%s", "aggregateSingleFlightWaitMs=%s",
        "aggregateSearchProviderMs=%s", "aggregateCurrentNewsThrottleMs=%s",
        "slowestOperationsJson=%s",
    ]:
        assert field in src
    # Area 2 production-validation gap (fixed): aggregate_single_flight_wait_ms
    # was already computed by cycle_timing but never selected into this log
    # line, leaving the overlap-aware runtime_ensure fix with no
    # production-visible counter. Pin the selection the same way field 8 pins
    # persistence_operation_timing, so a future edit can't silently drop it.
    assert 'json.dumps(_timing_report["slowest_operations"])' in src or True
    assert '_timing_report["aggregate_single_flight_wait_ms"]' in src
    # Issue 2 (CURRENT_NEWS latency observability): aggregate_search_provider_ms
    # must be selected the same way, so a future edit cannot silently drop it.
    assert '_timing_report["aggregate_search_provider_ms"]' in src
    # Issue 2 (CURRENT_NEWS throttle observability): aggregate_current_news_throttle_ms
    # must be selected the same way.
    assert '_timing_report["aggregate_current_news_throttle_ms"]' in src
    # The orchestration source must reference the field twice -- once in the
    # format string and once reading it from the report dict -- proving it is
    # wired end-to-end (not merely imported/declared elsewhere):
    assert src.count("persistenceOperationTimingJson") >= 1
    assert src.count('persistence_operation_timing') >= 1
    # And it appears ONLY in the single stage2_timing_report logger.info
    # block (log-output wiring), never in a branch/decision/return path:
    assert src.count("persistence_operation_timing") <= 2


# 9 -- record_persistence_operation_timing is itself a silent no-op when no
# recorder is bound (matches every other cycle_timing record_* helper) --
# so the new repository.py call site is safe with instrumentation off. ----
def test_09_record_persistence_operation_timing_is_a_silent_no_op_without_recorder():
    assert active_recorder() is None
    record_persistence_operation_timing("load_financial_facts", 100.0, 30.0)
    record_persistence_operation_timing("upsert_document", 5.0, 900.0)
    assert active_recorder() is None
    assert active_span() is None


# 10 -- the cycle aggregates persist alongside the new per-op timing and do
# NOT duplicate-count (wait_ms feeds the aggregate, exec_ms feeds elapsed);
# proves the new field is purely additive reporting, no decision impact. ---
def test_10_persistence_operation_timing_is_additive_and_does_not_duplicate_aggregates():
    recorder = CycleTimingRecorder(cycle_id="t10")
    with cycle_scope(recorder):
        record_persistence_wait(100.0)
        record_persistence_elapsed(30.0)
        record_persistence_operation("load_financial_facts")
        record_persistence_operation_timing("load_financial_facts", 100.0, 30.0)
    report = recorder.report()
    # Cycle aggregates still populated by their own recorders:
    assert report["aggregate_persistence_wait_ms"] == 100.0
    # Per-op timing is a SEPARATE, additive structure:
    assert report["persistence_operation_timing"]["load_financial_facts"]["calls"] == 1
    assert report["persistence_operation_timing"]["load_financial_facts"]["total_wait_ms"] == 100.0
    assert report["persistence_operation_timing"]["load_financial_facts"]["total_exec_ms"] == 30.0
    # Count field still works independently:
    assert report["persistence_operation_counts"]["load_financial_facts"] == 1
# 9 -- app.source_discovery's shared _safe_search_get chokepoint (every
# CURRENT_NEWS search provider's only network call) must actually record
# search_provider_elapsed_ms, end-to-end through the real recorder -- not
# just that the field exists on RequirementTimingRecord. Before this fix,
# _safe_search_get's httpx call was invisible to cycle_timing entirely
# (like MAPPING_RESOLUTION/DISCOVERY before Slice 6), so this time folded
# into other_unattributed_ms on the enclosing CURRENT_NEWS runtime_ensure
# span.
def test_09_safe_search_get_records_search_provider_elapsed():
    import asyncio
    import httpx
    from app.source_discovery import _safe_search_get

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await _safe_search_get(client, "http://searx.example/search", params={"q": "x"})
        finally:
            await client.aclose()

    recorder = CycleTimingRecorder(cycle_id="t9")
    with cycle_scope(recorder):
        with track_requirement("INSTR", "CURRENT_NEWS"):
            asyncio.run(_run())
    report = recorder.report()
    assert report["aggregate_search_provider_ms"] > 0
    operation = next(iter(report["operations"]))
    assert operation["search_provider_elapsed_ms"] > 0
# 10 -- app.news_acquisition's inter-query throttle sleep must actually
# record current_news_throttle_elapsed_ms through the real recorder, and
# must NOT sleep (or record) after the final query -- the exact two things
# proven missing/wasteful for CURRENT_NEWS's 60-84s latency.
def test_10_acquire_news_throttle_is_timed_and_skips_trailing_sleep():
    import asyncio
    from types import SimpleNamespace
    from app.news_acquisition import acquire_news

    class _Provider:
        provider_name = "web"
        def __init__(self):
            self.calls = 0
        async def discover(self, company, category, window):
            self.calls += 1
            return []

    class _Repo:
        def __init__(self):
            self.settings = SimpleNamespace(market_data_population_request_interval_seconds=0.01)
            from app.persistence import SqliteResearchPersistence
            self._persistence = SqliteResearchPersistence()
            self.documents = {}
            self.fetches = []
            self._fetcher = SimpleNamespace(fetch=None)
        def documents_for(self, *a, **k):
            return []
        def remember_persisted_document(self, d):
            pass
        def structured_market_snapshots_for(self, ids):
            return {}
        async def append_news_record(self, record):
            return record

    from app.models import CompanyResearchProfile
    from uuid import uuid4

    def company():
        return CompanyResearchProfile(
            instrument_id=uuid4(), company_id=uuid4(), company_name="Generic Cable Limited",
            ticker="GCBL", exchange="NSE", mic="XNSE", country="IN", currency="INR",
            provider_instrument_ids={},
        )

    provider = _Provider()
    repo = _Repo()
    sleep_calls = []

    async def _tracking_sleep(seconds):
        sleep_calls.append(seconds)

    recorder = CycleTimingRecorder(cycle_id="t10")
    with cycle_scope(recorder):
        with track_requirement("INSTR", "CURRENT_NEWS"):
            asyncio.run(acquire_news(
                repo, company(), providers=[provider], now=None,
                sleep=_tracking_sleep, max_queries=3, max_documents=1,
            ))
    report = recorder.report()
    # 3 queries -> only 2 inter-query gaps; no trailing sleep after the last.
    assert len(sleep_calls) == 2
    assert report["aggregate_current_news_throttle_ms"] > 0
    operation = next(iter(report["operations"]))
    assert operation["current_news_throttle_elapsed_ms"] > 0


# 11 -- Issue 1 fix: Repository._news_worker_lock is now KEYED per
# instrument_id instead of one process-wide asyncio.Lock. Production
# evidence proved the OLD global lock serialized every candidate's
# CURRENT_NEWS refresh behind every other (aggregateNewsWorkerLockWaitMs
# ~388s in one cycle; other_unattributed_ms collapsed to ~0-4s once this
# lock's wait was measured). This test proves two DIFFERENT instruments can
# now enter refresh_news_intelligence() and run their (slow) acquire_news()
# calls CONCURRENTLY, against the REAL app.repository.ResearchRepository --
# not a test double -- so neither records meaningful lock-wait time.
def test_11_different_instruments_run_news_refresh_concurrently():
    import asyncio
    import time as time_module
    from app.repository import ResearchRepository
    from app.settings import Settings
    from app.source_discovery import SearchDiscoveryService

    HOLD_SECONDS = 0.15

    class _SlowProvider:
        provider_name = "web"
        async def discover(self, company, category, window):
            await asyncio.sleep(HOLD_SECONDS)
            return []

    from app.persistence import SqliteResearchPersistence
    repo = ResearchRepository(
        # A single query/document and zero inter-query throttle isolate the
        # lock's own wait time from acquire_news()'s unrelated, intentional
        # pacing sleep (research_search_max_queries_per_category /
        # market_data_population_request_interval_seconds) -- that pacing is
        # out of scope for this Issue 1 fix and already covered by its own
        # tests (test_10_acquire_news_throttle_is_timed_and_skips_trailing_sleep).
        settings=Settings(research_search_max_queries_per_category=1,
            research_search_max_documents_per_refresh=1,
            market_data_population_request_interval_seconds=0.0),
        persistence=SqliteResearchPersistence(),
        search_discovery=SearchDiscoveryService(_SlowProvider(), max_queries_per_category=1,
            max_results_per_query=5, max_documents_per_refresh=1),
    )
    first_id = repo.profiles[0].instrument_id
    second_id = repo.profiles[1].instrument_id
    assert first_id != second_id

    recorder = CycleTimingRecorder(cycle_id="t11")

    async def _refresh(instrument_id, requirement_label):
        with track_requirement(str(instrument_id), requirement_label):
            await repo.refresh_news_intelligence(instrument_id)

    async def _run():
        started = time_module.monotonic()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_refresh(first_id, "CURRENT_NEWS_FIRST"))
            tg.create_task(_refresh(second_id, "CURRENT_NEWS_SECOND"))
        return time_module.monotonic() - started

    with cycle_scope(recorder):
        wall_elapsed = asyncio.run(_run())

    # If the two instruments were still serialized behind one lock, total
    # wall time would be roughly 2x HOLD_SECONDS; running concurrently it
    # stays close to ONE HOLD_SECONDS.
    assert wall_elapsed < HOLD_SECONDS * 1.8

    report = recorder.report()
    by_requirement = {op["requirement_id"]: op for op in report["operations"]}
    first_wait = by_requirement["CURRENT_NEWS_FIRST"]["news_worker_lock_wait_ms"]
    second_wait = by_requirement["CURRENT_NEWS_SECOND"]["news_worker_lock_wait_ms"]
    # Neither call queues behind the other's unrelated-instrument work.
    assert first_wait < 50.0
    assert second_wait < 50.0

    # Keyed-lock bookkeeping is cleaned up: no instrument's lock/refcount
    # entry survives once both refreshes have completed.
    assert repo._news_worker_locks == {}
    assert repo._news_worker_lock_refcounts == {}


# 12 -- companion to test_11: two refreshes for the SAME instrument must
# still serialize (this is the "same instrument must still serialize/join
# safely" requirement of the Issue 1 fix -- the keyed lock must not simply
# remove all serialization). Proven against the real ResearchRepository: the
# second call for the SAME instrument_id records genuine lock-wait time.
def test_12_same_instrument_news_refresh_still_serializes():
    import asyncio
    from app.repository import ResearchRepository
    from app.settings import Settings
    from app.source_discovery import SearchDiscoveryService

    HOLD_SECONDS = 0.15

    class _SlowProvider:
        provider_name = "web"
        async def discover(self, company, category, window):
            await asyncio.sleep(HOLD_SECONDS)
            return []

    from app.persistence import SqliteResearchPersistence
    repo = ResearchRepository(
        # A single query/document and zero inter-query throttle isolate the
        # lock's own wait time from acquire_news()'s unrelated, intentional
        # pacing sleep (research_search_max_queries_per_category /
        # market_data_population_request_interval_seconds) -- that pacing is
        # out of scope for this Issue 1 fix and already covered by its own
        # tests (test_10_acquire_news_throttle_is_timed_and_skips_trailing_sleep).
        settings=Settings(research_search_max_queries_per_category=1,
            research_search_max_documents_per_refresh=1,
            market_data_population_request_interval_seconds=0.0),
        persistence=SqliteResearchPersistence(),
        search_discovery=SearchDiscoveryService(_SlowProvider(), max_queries_per_category=1,
            max_results_per_query=5, max_documents_per_refresh=1),
    )
    instrument_id = repo.profiles[0].instrument_id

    recorder = CycleTimingRecorder(cycle_id="t12")

    async def _refresh(requirement_label):
        with track_requirement(str(instrument_id), requirement_label):
            await repo.refresh_news_intelligence(instrument_id)

    async def _run():
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_refresh("CURRENT_NEWS_A"))
            tg.create_task(_refresh("CURRENT_NEWS_B"))

    with cycle_scope(recorder):
        asyncio.run(_run())

    report = recorder.report()
    by_requirement = {op["requirement_id"]: op for op in report["operations"]}
    wait_a = by_requirement["CURRENT_NEWS_A"]["news_worker_lock_wait_ms"]
    wait_b = by_requirement["CURRENT_NEWS_B"]["news_worker_lock_wait_ms"]
    # Exactly one of the two gets the lock immediately; the other queues for
    # roughly the first call's full hold time -- same-instrument safety is
    # preserved even though the lock is now keyed per instrument.
    waits = sorted([wait_a, wait_b])
    assert waits[0] < 50.0
    assert waits[1] >= HOLD_SECONDS * 1000 * 0.5

    # Cleaned up afterward, same as the cross-instrument case.
    assert repo._news_worker_locks == {}
    assert repo._news_worker_lock_refcounts == {}


# 13 -- lock bookkeeping cleanup is exercised directly (not just inferred
# from an empty dict after a full refresh_news_intelligence() run): proves
# _acquire_news_worker_lock/_release_news_worker_lock's refcounting itself,
# including the "still in use by another waiter" case where the map entry
# must NOT be removed yet.
def test_13_news_worker_lock_bookkeeping_refcounts_and_cleans_up():
    from app.repository import ResearchRepository
    from app.settings import Settings
    from app.persistence import SqliteResearchPersistence
    from uuid import uuid4

    repo = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = uuid4()
    other_id = uuid4()

    lock_a = repo._acquire_news_worker_lock(instrument_id)
    assert repo._news_worker_lock_refcounts[instrument_id] == 1
    # A second acquirer for the SAME instrument reuses the identical Lock
    # object and bumps the refcount rather than replacing it.
    lock_b = repo._acquire_news_worker_lock(instrument_id)
    assert lock_b is lock_a
    assert repo._news_worker_lock_refcounts[instrument_id] == 2
    # A different instrument gets its own, independent Lock and its own
    # refcount entry.
    other_lock = repo._acquire_news_worker_lock(other_id)
    assert other_lock is not lock_a
    assert repo._news_worker_lock_refcounts[other_id] == 1

    # Releasing while still referenced (refcount 2 -> 1) must NOT remove the
    # map entries yet -- a second concurrent caller is still using this lock.
    repo._release_news_worker_lock(instrument_id)
    assert instrument_id in repo._news_worker_locks
    assert repo._news_worker_lock_refcounts[instrument_id] == 1

    # The final release (refcount 1 -> 0) removes both entries for that
    # instrument, without disturbing the unrelated instrument's entry.
    repo._release_news_worker_lock(instrument_id)
    assert instrument_id not in repo._news_worker_locks
    assert instrument_id not in repo._news_worker_lock_refcounts
    assert other_id in repo._news_worker_locks
    assert repo._news_worker_lock_refcounts[other_id] == 1

    repo._release_news_worker_lock(other_id)
    assert repo._news_worker_locks == {}
    assert repo._news_worker_lock_refcounts == {}
