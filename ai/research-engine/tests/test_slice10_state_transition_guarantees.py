"""Guardian Review Slice 10 -- warm-run / incremental guarantee tests.

The six state-transition guarantees the review names are each already proven,
at various layers, by pre-existing (pre-this-engagement) test files:
  - READY_FRESH mandatory -> zero provider work:
    tests/test_slice10_warm_db_acceptance.py (full worker-cycle level) and
    tests/test_slice9_mini_universe_acceptance.py; also
    tests/test_market_data_population_bounded_concurrency.py's
    test_warm_fresh_instruments_perform_zero_provider_work_under_concurrency
    (this engagement's own Slice 3 addition, at the bounded-concurrency
    population layer introduced this task).
  - READY_STALE -> refresh only the stale requirement:
    tests/test_slice10_warm_db_acceptance.py's
    test_stale_lightweight_price_does_not_repeat_financial_or_governance_research.
  - TECHNICAL_FAILURE -> bounded retry, and
    GENUINELY_UNAVAILABLE -> terminal/not retried:
    tests/test_slice4_repair_semantics.py (full acquisition-service level,
    including durability across restarts) and
    tests/test_truthful_final_accounting.py (terminal-accounting level).
  - CURRENT_NEWS -> optional/background/non-blocking:
    proven throughout this engagement (Slice 6/9 included) and in
    tests/test_truthful_final_accounting.py's
    test_current_news_technical_failure_still_reaches_fully_analyzed.

This file adds the one thing that was missing: a single, compact,
directly-readable pass through all six guarantees together at the
investigate()/CycleCheckpoint level, so the whole checklist can be verified
in one place without cross-referencing five different test files. It reuses
this session's existing fixtures (test_stock_rule_engine._readiness,
app.deep_investigation.investigate/acquisition_budget) and does not
duplicate the deeper full-service/full-cycle assertions those other files
already make.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, DISPOSITION_STATE, terminal_meaning
from app.deep_investigation import acquisition_budget, investigate
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import TargetedEnsureResult
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
from test_stock_rule_engine import _readiness


# 1 -- READY_FRESH mandatory: investigate() never calls runtime.ensure() at
# all when nothing needs acquiring -------------------------------------------
@pytest.mark.asyncio
async def test_ready_fresh_mandatory_performs_zero_provider_work():
    readiness = _readiness({})  # every requirement defaults to READY_FRESH
    ensure = AsyncMock()
    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    assert ensure.await_count == 0
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed


# 2 -- READY_STALE: only the stale requirement is (re)requested, never a
# fresh sibling --------------------------------------------------------------
@pytest.mark.asyncio
async def test_ready_stale_refreshes_only_the_stale_requirement():
    readiness = _readiness({"LATEST_PRICE": Status.READY_STALE})
    requested: list[tuple[str, ...]] = []

    async def ensure(key, *, requirement_ids, **kwargs):
        requested.append(tuple(requirement_ids))
        return TargetedEnsureResult(readiness, tuple(requirement_ids), tuple(requirement_ids))

    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    all_requested = {rid for call in requested for rid in call}
    assert all_requested and all_requested <= {"LATEST_PRICE"}


# 3 -- MISSING: an acquisition is actually attempted for the missing
# requirement -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_requirement_triggers_acquisition():
    readiness = _readiness({"QUARTERLY_FINANCIALS": Status.MISSING})
    requested: list[tuple[str, ...]] = []

    async def ensure(key, *, requirement_ids, **kwargs):
        requested.append(tuple(requirement_ids))
        return TargetedEnsureResult(readiness, tuple(requirement_ids), ())

    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    assert any("QUARTERLY_FINANCIALS" in call for call in requested)


# 4 -- TECHNICAL_FAILURE -> bounded retry: below the attempt cap the row is
# NOT restorable (another attempt is still owed); at/above the cap it becomes
# restorable (the bounded repair budget is spent, and the outcome is
# reported, never retried forever). See DISPOSITION_STATE for the mapping
# from a technical disposition to RETRYABLE_FAILURE, and
# tests/test_slice4_repair_semantics.py for the full acquisition-level
# behavior this checkpoint bounding rule serves. -----------------------------
def test_technical_failure_is_retried_up_to_a_bound_then_reported():
    assert DISPOSITION_STATE["DEEP_TECHNICAL_FAILURE"] == CandidateState.RETRYABLE_FAILURE
    checkpoint = CycleCheckpoint.__new__(CycleCheckpoint)
    checkpoint.max_attempts = 3
    checkpoint._progress = {
        ("DEEP", "below-cap"): {"state": str(CandidateState.RETRYABLE_FAILURE), "attempts": 2},
        ("DEEP", "at-cap"): {"state": str(CandidateState.RETRYABLE_FAILURE), "attempts": 3},
    }
    # Below the cap: not yet restorable -- another repair attempt is owed.
    assert checkpoint.restorable("DEEP", "below-cap") is None
    # At the cap: restorable -- the bounded budget is spent, reported as-is.
    assert checkpoint.restorable("DEEP", "at-cap") is not None


# 5 -- GENUINELY_UNAVAILABLE: durable and terminal from the first attempt --
# never re-attempted regardless of attempt count, and its truthful terminal
# meaning is preserved across a checkpoint restore ---------------------------
def test_genuinely_unavailable_is_immediately_terminal_and_durable():
    assert DISPOSITION_STATE["DEEP_READINESS_NOT_MET"] == CandidateState.EVIDENCE_UNAVAILABLE
    assert terminal_meaning(CandidateState.EVIDENCE_UNAVAILABLE) == "DATA_GENUINELY_UNAVAILABLE"
    checkpoint = CycleCheckpoint.__new__(CycleCheckpoint)
    checkpoint.max_attempts = 3
    checkpoint._progress = {("DEEP", "genuinely-unavailable"):
                            {"state": str(CandidateState.EVIDENCE_UNAVAILABLE), "attempts": 1}}
    # Terminal on the very first attempt -- the recheck-schedule contract
    # (not a bounded-retry contract) governs when it is looked at again, and
    # a restart must restore it as-is rather than re-queue it for repair.
    assert checkpoint.restorable("DEEP", "genuinely-unavailable") is not None


# 6 -- CURRENT_NEWS: optional/background/non-blocking -- a failure recorded
# against it alone never prevents full_analysis_allowed ---------------------
@pytest.mark.asyncio
async def test_current_news_failure_never_blocks_full_analysis():
    readiness = _readiness({"CURRENT_NEWS": Status.MISSING})

    async def ensure(key, *, requirement_ids, **kwargs):
        return TargetedEnsureResult(readiness, tuple(requirement_ids), (),
                                    failures={rid: "SEARCH_PROVIDER_UNAVAILABLE" for rid in requirement_ids})

    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    assert eligibility.blocking_requirements == []
