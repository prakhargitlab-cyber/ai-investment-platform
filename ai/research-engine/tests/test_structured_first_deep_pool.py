"""Whole-universe baseline fairness and explicit deferral before expensive work."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.deep_investigation import build_plan, deferred_investigation
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.research_readiness import ResearchRequirementStatus as Status, RuleEngineArea
from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS, TargetedEnsureResult
from app.stock_rule_engine import AreaScoreStatus, StockRuleEngineV1, StockRuleEngineEligibilityPolicy
from test_global_opportunity_baseline import setup_acquisition
from test_global_scanner import NOW
from test_stock_rule_engine import _inputs, _readiness


@pytest.mark.asyncio
async def test_production_discovery_caps_deep_work_after_every_baseline_and_is_deterministic(monkeypatch):
    async def run(reverse):
        service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=12)
        acquired = []
        reads = []
        current = {}
        narrative = {"GOVERNANCE_HISTORY", "ORDER_BOOK_CAPEX_GUIDANCE"}

        async def read(key, **kwargs):
            reads.append(key)
            return current.get(key, replace(_readiness({name: Status.MISSING for name in narrative}),
                                             global_instrument_id=key))

        async def ensure(key, *, requirement_ids=None, **kwargs):
            if set(requirement_ids or ()) == BASELINE_REQUIREMENT_IDS:
                return await tracker(key, requirement_ids=requirement_ids, **kwargs)
            # This stands in for official acquisition: never called by baseline.
            acquired.append((key, set(requirement_ids)))
            current[key] = replace(_readiness(), global_instrument_id=key)
            return TargetedEnsureResult(current[key], tuple(requirement_ids), ("OFFICIAL_DOCUMENT",))

        service.readiness.read = read
        service.readiness.ensure = ensure
        service.repository.refresh_targeted_categories = AsyncMock(side_effect=AssertionError("Unexpected PDF path"))
        result = await service.run(list(reversed(rows)) if reverse else rows, as_of=NOW,
                                   shortlist_limit=4, top_n=None, discovery_v2=True)
        assert len(tracker.baseline_calls) == 4
        assert result.baseline_evaluated_count == 12
        assert result.acquisition_deferred_count == 8
        selected = {key for key, _ in acquired}
        assert {key for key, _ in tracker.baseline_calls} == selected
        assert len(selected) == result.deep_candidate_count == result.deep_attempted_count == 4
        assert set(reads) == selected
        assert all(requirements == narrative for _, requirements in acquired)
        deferred = [d for d in result.diagnostics if d.disposition == "DEFERRED"]
        assert len(deferred) == 8
        for diagnostic in deferred:
            assert diagnostic.failure_reason is None and diagnostic.failure_class is None
            assert diagnostic.acquisition_failures == {}
            state = result.investigation_matrix[str(diagnostic.global_instrument_id)]
            assert state["disposition"] == "DEFERRED" and not state["rule_evaluated"]
            assert state["requirements"]["GOVERNANCE_HISTORY"]["attempted"] is False
        return [key for key, _ in acquired], [row.global_instrument_id for row in result.top_n]

    assert await run(False) == await run(True)


@pytest.mark.parametrize("underlying", [Status.MISSING, Status.FAILED, Status.READY_STALE])
def test_deferral_is_a_scheduling_state_without_changing_evidence_or_provider_outcomes(underlying):
    readiness = _readiness({"GOVERNANCE_HISTORY": underlying, "ORDER_BOOK_CAPEX_GUIDANCE": underlying})
    plan = build_plan(readiness, deep_selected=False)
    assert set(plan.deferred_evidence) == {"GOVERNANCE_HISTORY", "ORDER_BOOK_CAPEX_GUIDANCE"}
    assert plan.acquisition_needed == ()
    assert readiness.for_requirement("GOVERNANCE_HISTORY").status == underlying
    state = deferred_investigation()["requirements"]["GOVERNANCE_HISTORY"]
    assert state == {"acquisition_state": "DEFERRED", "attempted": False}
    assert "outcome" not in state and "failure_reason" not in state
    deep = build_plan(readiness)
    assert not deep.deferred_evidence
    assert set(deep.acquisition_needed) == set(plan.deferred_evidence)
    assert not StockRuleEngineEligibilityPolicy().evaluate(readiness).full_analysis_allowed or underlying == Status.READY_STALE


def test_deep_narrative_still_requires_genuine_coverage_and_incomplete_gate_stays_closed():
    readiness = _readiness({"GOVERNANCE_HISTORY": Status.MISSING, "ORDER_BOOK_CAPEX_GUIDANCE": Status.MISSING})
    value = _inputs(readiness=readiness, events=[], shareholding=[])
    engine = StockRuleEngineV1()
    areas = [engine._catalysts(value), engine._governance(value)]
    assert all(area.status == AreaScoreStatus.UNSCORABLE and area.raw_score is None for area in areas)
    result = SimpleNamespace(area_scores=areas, eligibility=SimpleNamespace(
        full_analysis_allowed=True, blocking_requirements=[]), partial=False)
    assert GlobalOpportunityOrchestrator._incomplete_analysis(result, readiness) == [
        "GOVERNANCE_HISTORY", "ORDER_BOOK_CAPEX_GUIDANCE"]


def test_actual_completed_empty_check_is_distinct_from_deferred():
    from app.stock_rule_engine import CATALYST_CHECK_PREFIX
    readiness = _readiness()
    readiness = replace(readiness, requirements=tuple(replace(row,
        evidence_ids=(CATALYST_CHECK_PREFIX + "genuine-check",)) if row.requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE"
        else row for row in readiness.requirements))
    area = StockRuleEngineV1()._catalysts(_inputs(readiness=readiness, events=[]))
    assert area.status == AreaScoreStatus.READY_FRESH
    assert area.raw_score is None and not area.metrics
    assert "ORDER_BOOK_CAPEX_GUIDANCE" not in build_plan(readiness, deep_selected=False).deferred_evidence
