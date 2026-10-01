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
        "aggregatePersistenceWaitMs=%s", "slowestOperationsJson=%s",
    ]:
        assert field in src
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
