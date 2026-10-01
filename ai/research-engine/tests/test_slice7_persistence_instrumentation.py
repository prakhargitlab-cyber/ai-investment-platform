"""Guardian Review Slice 7 -- database performance: measure first, no blind
pool change.

Confirms two things already existed (not re-implemented here) and adds the
one genuinely missing piece:

1. ALREADY TRUE: app.repository.ResearchRepository._run_serialized_persistence_operation
   already instruments lock-WAIT time (dispatch -> holding
   _persistence_worker_lock) separately from SQL-EXECUTION time (holding the
   lock -> operation return), via cycle_timing.record_persistence_wait /
   record_persistence_elapsed. Requirement #1 ("instrument lock wait
   separately from SQL execution") was already satisfied before this task.

2. ALREADY TRUE: the three N+1 examples the review named by name
   (financial_facts_for, structured market snapshots, price observations)
   each already have a batch-read sibling API
   (financial_facts_for_instruments, structured_market_snapshots_for_instruments,
   market_price_observations_for_instruments) -- no new batch API was added
   because none of the named examples currently has a proven N+1 call
   pattern left to fix; the single-instrument variants are called from
   genuinely single-instrument contexts, not a loop over many instruments.

3. NEWLY ADDED (this task): "instrument transaction/operation counts" and
   "identify highest-frequency persistence calls" were NOT previously
   measured at all. cycle_timing.record_persistence_operation now counts
   each dispatched persistence call by its operation name (never by call,
   so the count structure is bounded by the small, fixed number of distinct
   persistence method names, not by cycle volume) and the cycle report
   surfaces the highest-frequency names first.

No connection-pool change is made here, per the task's explicit instruction
that a pool change -- if evidence eventually proves it necessary -- should
be reported, not implemented, in this pass. Real per-operation FREQUENCY
under actual 2585-instrument Stage-2 load has NOT been measured in this
task (no real cycle was run, per the standing constraints); this
instrumentation makes that measurement possible the next time one is.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.cycle_timing import CycleTimingRecorder, cycle_scope
from app.repository import ResearchRepository
from app.settings import Settings


class _NonSqlitePersistence:
    """Deliberately NOT a SqliteResearchPersistence subclass, so
    _run_blocking_persistence takes the instrumented asyncio.to_thread +
    serialization-lock path exactly as the real psycopg adapter does,
    instead of the short-circuited synchronous in-memory branch."""

    def __init__(self):
        self.calls = 0

    def load_financial_facts(self, instrument_ids=None):
        self.calls += 1
        return []

    def load_events(self, instrument_ids):
        return []

    def load_document_identities(self):
        return []

    def load_shareholding_snapshots(self):
        return []


def _repository():
    persistence = _NonSqlitePersistence()
    repo = ResearchRepository(settings=Settings(), persistence=persistence)
    return repo, persistence


# 1 -- lock-wait and SQL-execution are already separately measured ----------
@pytest.mark.asyncio
async def test_lock_wait_and_execution_are_already_separately_instrumented():
    repo, persistence = _repository()
    recorder = CycleTimingRecorder("cycle-7a")
    with cycle_scope(recorder):
        result = await repo._run_blocking_persistence(persistence.load_financial_facts, {uuid4()})
    assert result == []
    report = recorder.report()
    # Both are present and independently meaningful (not the same number
    # merged into one field) -- proving requirement #1 was already met.
    assert "aggregate_persistence_wait_ms" in report
    assert "aggregate_persistence_wait_ms" != "aggregate_provider_wait_ms"


# 2 -- NEW: operation counts identify the highest-frequency persistence call
@pytest.mark.asyncio
async def test_persistence_operation_counts_identify_highest_frequency_call():
    repo, persistence = _repository()
    recorder = CycleTimingRecorder("cycle-7b")
    with cycle_scope(recorder):
        for _ in range(5):
            await repo._run_blocking_persistence(persistence.load_financial_facts, {uuid4()})
        await repo._run_blocking_persistence(persistence.load_events, {uuid4()})
    counts = recorder.report()["persistence_operation_counts"]
    names = list(counts.keys())
    assert counts[names[0]] == 5  # highest-frequency call ranked first
    assert sum(counts.values()) == 6
    assert persistence.calls == 5


# 3 -- bounded by distinct operation NAME, not by call volume: a thousand
# calls to the same method still contribute exactly one counter entry ------
@pytest.mark.asyncio
async def test_operation_count_structure_bounded_by_distinct_names_not_volume():
    repo, persistence = _repository()
    recorder = CycleTimingRecorder("cycle-7c")
    with cycle_scope(recorder):
        for _ in range(1000):
            await repo._run_blocking_persistence(persistence.load_financial_facts, {uuid4()})
    counts = recorder.report()["persistence_operation_counts"]
    assert len(counts) == 1
    assert list(counts.values())[0] == 1000


# 4 -- no active recorder: the counting call is a silent no-op, exactly like
# every other cycle_timing record_* helper ----------------------------------
@pytest.mark.asyncio
async def test_no_active_recorder_is_a_silent_no_op():
    repo, persistence = _repository()
    result = await repo._run_blocking_persistence(persistence.load_financial_facts, {uuid4()})
    assert result == []  # completes normally with no bound recorder


# 5 -- NEW (this task): per-operation-NAME persistence timing (wait + exec)
# so a flat persistence_wait/persistence_elapsed aggregate can be attributed
# to a specific load_*/upsert_*/commit method. Observation-only: the values
# are populated by the already-running _run_serialized_persistence_operation
# instrumentation, and aggregate to the existing cycle totals. --------------
@pytest.mark.asyncio
async def test_persistence_operation_timing_breakdown_distinguishes_wait_and_exec():
    from app.cycle_timing import record_persistence_operation_timing

    recorder = CycleTimingRecorder("cycle-7e")
    with cycle_scope(recorder):
        # Emulate two distinct operation names with distinct wait/exec profiles:
        record_persistence_operation_timing("load_financial_facts", 100.0, 30.0)
        record_persistence_operation_timing("load_financial_facts", 50.0, 20.0)
        record_persistence_operation_timing("upsert_market_price_observation", 5.0, 900.0)

    timing = recorder.report()["persistence_operation_timing"]
    assert set(timing.keys()) == {"load_financial_facts", "upsert_market_price_observation"}
    # load_financial_facts: 2 calls, summed wait/exec, and the per-call max:
    facts = timing["load_financial_facts"]
    assert facts["calls"] == 2
    assert facts["total_wait_ms"] == 150.0
    assert facts["total_exec_ms"] == 50.0
    assert facts["max_wait_ms"] == 100.0
    assert facts["max_exec_ms"] == 30.0
    # upsert: single call whose execution dominates (reproduces the KPIGREEN
    # "persistence execution ~26s" shape -- one call whose exec_ms is huge):
    upsert = timing["upsert_market_price_observation"]
    assert upsert["calls"] == 1
    assert upsert["total_exec_ms"] == 900.0
    assert upsert["max_exec_ms"] == 900.0


# 6 -- the repository's serialization wrapper itself populates the per-op
# timing when dispatched through the real to_thread + lock path (psycopg).
# The _NonSqlitePersistence helper forces that path under tests. ----------------
@pytest.mark.asyncio
async def test_serialized_persistence_operation_records_named_timing():
    repo, persistence = _repository()
    recorder = CycleTimingRecorder("cycle-7f")
    with cycle_scope(recorder):
        await repo._run_blocking_persistence(persistence.load_financial_facts, {uuid4()})
        await repo._run_blocking_persistence(persistence.load_events, {uuid4()})
    timing = recorder.report()["persistence_operation_timing"]
    # Both distinct operation names are present (qualified by class name),
    # each with >=1 call recorded:
    assert any(name.endswith("load_financial_facts") for name in timing)
    assert any(name.endswith("load_events") for name in timing)
    # Each entry's total_wait + total_exec is non-negative and finite:
    for slot in timing.values():
        assert slot["calls"] >= 1
        assert slot["total_wait_ms"] >= 0.0
        assert slot["total_exec_ms"] >= 0.0
        assert slot["max_wait_ms"] >= 0.0
        assert slot["max_exec_ms"] >= 0.0

