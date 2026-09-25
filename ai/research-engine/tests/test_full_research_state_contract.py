"""Full-research state contract: FULLY ANALYZED / DATA GENUINELY UNAVAILABLE /
TECHNICAL FAILURE -- no silent fourth state.

* A Rule Engine result that is not fully analyzed (an applicable required area
  UNSCORABLE / not full) never becomes RANK_FILTERED or ANALYZED; it ends as
  DEEP_READINESS_NOT_MET with an explicit failure class (retryable when
  technical).
* RANK_FILTERED stays reserved for a fully analyzed candidate that fails a
  real ranking/risk gate.
* Catalyst research is satisfied by a completed authoritative check, not by the
  company having a positive order-book/capex event.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from app.cycle_checkpoint import CandidateState, state_for_disposition
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason
from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus as Status
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter, TargetedEnsureResult
from app.stock_rule_engine import (AreaScoreStatus, RiskOverrideResult, RiskOverrideSeverity,
                                   StockRuleEngineEligibilityPolicy, StockRuleEngineV1)

import test_research_readiness_runtime as rt
import test_stock_rule_engine as sre
from test_full_research_eligibility_contract import _ranked, _search_run
from test_global_opportunity_acquisition import acquisition_service
from test_global_opportunity_ranker import inputs
from test_global_scanner import NOW, instrument, persisted

CATALYSTS = "ORDER_BOOK_CAPACITY_CATALYSTS"
REQ = "ORDER_BOOK_CAPEX_GUIDANCE"


# --------------------------------------------------------------------------- helpers
def _unscorable_rule(*, engine_flagged: bool = True):
    """A Rule Engine result with an applicable required area UNSCORABLE.

    engine_flagged=False models an inconsistent double that still claims full
    eligibility: the orchestrator must not trust the flag alone."""
    rule = inputs(1)[1]
    rule.area_scores = [a.model_copy(update={"status": "UNSCORABLE", "raw_score": None, "weighted_contribution": 0})
                        if str(a.area) == CATALYSTS else a for a in rule.area_scores]
    if engine_flagged:
        rule.partial = True
        rule.overall_score = None
        rule.eligibility = rule.eligibility.model_copy(update={
            "full_analysis_allowed": False, "blocking_requirements": [REQ],
            "reason": "APPLICABLE_RULE_ENGINE_AREA_UNSCORABLE"})
    return rule


async def _run_stage2(monkeypatch, rule, *, failures=None, observation=None):
    service, runtime, store = acquisition_service(monkeypatch)
    deep_calls = []
    if observation is not None:
        store.upsert_acquisition_observation(UUID(int=1), REQ, "NSE", *observation)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get("requirement_ids") is None:
            deep_calls.append(key)
            return TargetedEnsureResult(sre._readiness(), (), (), failures=failures or {})
        return TargetedEnsureResult(None, (), ())

    runtime.ensure.side_effect = ensure
    service.rule_engine.analyze.side_effect = None
    service.rule_engine.analyze.return_value = rule
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)
    return result, deep_calls


def _catalyst_item(rows, *, events=None, industry=None):
    profile = rt._profile()
    repo = rt.DurableRepositoryFixture(profile)
    if events is not None:
        repo.events = events
    repo.acquisition_observations_for = lambda _key: list(rows)
    adapter = RepositoryResearchReadinessAdapter(repo)
    metadata = {"canonicalSector": "Industrials", "updatedAt": rt.NOW.isoformat()}
    if industry:
        metadata["industry"] = industry
    adapter.remember_canonical_metadata(profile.instrument_id, metadata)
    readiness = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=rt.NOW)
    return readiness.for_requirement(REQ)


def _check(outcome, *, hours=1, reason=None):
    return {"requirement_id": REQ, "provider": "NSE", "outcome": outcome,
            "observed_at": (rt.NOW - timedelta(hours=hours)).isoformat(),
            "failure_reason": reason, "evidence_count": 0}


# --------------------------------------------------------------------------- 1
@pytest.mark.asyncio
async def test_1_unscorable_with_genuinely_unavailable_evidence_is_not_rank_filtered(monkeypatch):
    result, deep_calls = await _run_stage2(monkeypatch, _unscorable_rule())
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.disposition not in {"RANK_FILTERED", "ANALYZED"}
    assert not diagnostic.rank_eligible and diagnostic.status == "SUPPRESSED"
    assert diagnostic.failure_class == EVIDENCE_UNAVAILABLE
    assert diagnostic.acquisition_failures == {REQ: "RULE_AREA_UNSCORABLE"}
    assert diagnostic.suppression_reasons == [REQ]
    assert result.top_n == [] and result.rule_analyzed_count == 0 and result.evaluated_count == 0
    assert result.deep_readiness_failed_count == 1 and result.deep_technical_failure_count == 0
    assert result.deep_ready_count == 0
    assert state_for_disposition(diagnostic.disposition) == CandidateState.EVIDENCE_UNAVAILABLE
    assert len(deep_calls) == 1  # genuine: no repair retry


# --------------------------------------------------------------------------- 2
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cycle", "durable"])
async def test_2_unscorable_caused_by_technical_failure_stays_retryable(monkeypatch, source):
    kwargs = ({"failures": {REQ: "NETWORK_TIMEOUT"}} if source == "cycle" else
              {"observation": ("FAILED", NOW, None, "SOURCE_UNAVAILABLE:NSE:ConnectError", 0)})
    result, deep_calls = await _run_stage2(monkeypatch, _unscorable_rule(), **kwargs)
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET" and not diagnostic.rank_eligible
    assert diagnostic.failure_class == TECHNICAL_RETRYABLE
    assert classify_reason(diagnostic.acquisition_failures[REQ]) == TECHNICAL_RETRYABLE
    assert result.deep_technical_failure_count == 1 and result.deep_readiness_failed_count == 1
    assert result.deep_repair_attempted_count >= 1 and len(deep_calls) >= 2  # bounded repair re-ran it
    assert len(result.diagnostics) == 1  # exact accounting after repair


# --------------------------------------------------------------------------- 3
@pytest.mark.asyncio
async def test_3_fully_analyzed_stock_failing_real_risk_gate_is_rank_filtered(monkeypatch):
    rule = inputs(1)[1]
    rule.risk_overrides = [RiskOverrideResult(code="CONFIRMED_FRAUD_OR_ACCOUNTING_CRISIS",
                                              severity=RiskOverrideSeverity.CRITICAL)]
    result, _ = await _run_stage2(monkeypatch, rule)
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "RANK_FILTERED" and not diagnostic.rank_eligible
    assert result.rule_analyzed_count == 1 and result.deep_readiness_failed_count == 0


# --------------------------------------------------------------------------- 4
def test_4_completed_zero_result_catalyst_check_satisfies_coverage_without_event():
    item = _catalyst_item([_check("SUCCESS_EMPTY")], events=[])
    assert item.status == Status.READY_FRESH and item.missing_input_ids == ()
    assert all(ref.startswith("catalyst-check:NSE:") for ref in item.evidence_ids)
    assert set(item.concept_evidence_states.values()) <= {"READY_FRESH", "NOT_APPLICABLE"}
    # Rule Engine: explicit READY coverage with no score and no fabricated event.
    readiness = sre._readiness()
    readiness = replace(readiness, requirements=tuple(
        replace(r, evidence_ids=("catalyst-check:NSE:2026-09-10T11:00:00+00:00",)) if r.requirement_id == REQ else r
        for r in readiness.requirements))
    value = replace(sre._inputs(readiness=readiness, events=()), news_search_run=_search_run("SUCCESS_EMPTY"))
    engine = StockRuleEngineV1()
    area = sre._area(engine.evaluate(value, allow_partial=False), sre.RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS)
    assert area.status == AreaScoreStatus.READY_FRESH and area.raw_score is None
    assert area.metrics == [] and area.missing_inputs == []
    assert "catalyst-check:NSE:2026-09-10T11:00:00+00:00" in area.evidence_references
    result = engine.evaluate(value, allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial and result.overall_score is not None
    assert _ranked(result).rank_eligible


# --------------------------------------------------------------------------- 5
@pytest.mark.parametrize("rows", [[], [_check("SUCCESS_EMPTY", hours=5), _check("FAILED", hours=1, reason="SOURCE_UNAVAILABLE:NSE:X")],
                                  [{**_check("SUCCESS_EMPTY"), "provider": "READINESS_EXECUTOR"}]])
def test_5_catalyst_check_never_completed_cannot_satisfy_coverage(rows):
    item = _catalyst_item(rows, events=[])
    assert item.status in {Status.MISSING, Status.FAILED}
    assert not any(ref.startswith("catalyst-check:") for ref in item.evidence_ids)
    readiness = sre._readiness({REQ: item.status})
    assert REQ in StockRuleEngineEligibilityPolicy().evaluate(readiness).blocking_requirements


# --------------------------------------------------------------------------- 6
def test_6_technical_catalyst_acquisition_failure_remains_retryable():
    item = _catalyst_item([_check("FAILED", reason="SOURCE_UNAVAILABLE:NSE:ConnectError")], events=[])
    assert item.status == Status.FAILED
    assert classify_reason(item.missing_reason) == TECHNICAL_RETRYABLE
    assert classify_reason("RULE_AREA_UNSCORABLE") == EVIDENCE_UNAVAILABLE


# --------------------------------------------------------------------------- 7
def test_7_explicit_not_applicable_catalyst_concept_does_not_block_full_research():
    bank = _catalyst_item([_check("SUCCESS_EMPTY")], events=[], industry="Banks")
    assert bank.status == Status.READY_FRESH
    assert bank.concept_evidence_states["ORDER_BOOK"] == "NOT_APPLICABLE"
    assert "ORDER_BOOK_OR_MAJOR_CONTRACT" in bank.not_applicable_input_reasons
    # Not-applicable is explicit, never inferred from missing evidence: an
    # unchecked bank is still incomplete for its applicable CAPEX/GUIDANCE concepts.
    unchecked = _catalyst_item([], events=[], industry="Banks")
    assert unchecked.status == Status.MISSING


# --------------------------------------------------------------------------- 8
@pytest.mark.asyncio
@pytest.mark.parametrize("engine_flagged", [True, False])
async def test_8_no_rank_eligible_or_analyzed_result_contains_applicable_unscorable_area(monkeypatch, engine_flagged):
    result, _ = await _run_stage2(monkeypatch, _unscorable_rule(engine_flagged=engine_flagged))
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition not in {"ANALYZED", "RANK_FILTERED"} and not diagnostic.rank_eligible
    assert REQ in diagnostic.suppression_reasons


# --------------------------------------------------------------------------- 9
@pytest.mark.asyncio
async def test_9_fully_complete_candidate_still_analyzed_and_rank_eligible(monkeypatch):
    result, _ = await _run_stage2(monkeypatch, inputs(1)[1])
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "ANALYZED" and diagnostic.rank_eligible
    assert result.rule_analyzed_count == 1 and result.deep_readiness_failed_count == 0
    engine_result = StockRuleEngineV1().evaluate(sre._inputs(), allow_partial=False)
    assert engine_result.eligibility.full_analysis_allowed and _ranked(engine_result).rank_eligible
