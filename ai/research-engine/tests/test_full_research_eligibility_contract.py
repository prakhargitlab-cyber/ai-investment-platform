"""Full-research eligibility contract (regression for DEV cycle 7c812519).

An applicable stock may only be full-analysis / rank eligible when every
research input the Rule Engine needs for trustworthy final scoring is present.
Explicit NOT_APPLICABLE / UNSUPPORTED stays exempt; missing evidence is never
simulated as not-applicable; a completed zero-result news check is explicit
coverage without a fabricated event; technical failures stay retryable.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_requirement_failures
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.news_intelligence import ProviderOutcome, aggregate_search
from app.research_applicability import classify_requirements
from app.research_readiness import ResearchRequirementRegistry, ResearchRequirementStatus as Status
from app.research_readiness_runtime import _incomplete_news_search_reason, _structured_fact_coverage
from app.stock_rule_engine import AreaScoreStatus, DecisionSignal, StockRuleEngineEligibilityPolicy, StockRuleEngineV1
from app.models import ResearchEventType

import test_research_readiness as rr
import test_stock_rule_engine as sre
from test_global_opportunity_ranker import inputs as ranker_inputs

POLICY = StockRuleEngineEligibilityPolicy()
REGISTRY = ResearchRequirementRegistry.default()


def _covering(requirement_id: str, *input_ids: str):
    return replace(rr.evidence(requirement_id), covered_input_ids=tuple(input_ids))


def _all_but(requirement_id: str, *missing: str):
    inputs = [item.input_id for item in REGISTRY.get(requirement_id).inputs if item.input_id not in missing]
    return (_covering(requirement_id, *inputs),)


def _gate(snapshot):
    _, readiness, plan = rr.assess(snapshot)
    return readiness, plan, POLICY.evaluate(readiness)


def _ranked(result):
    candidate, rule = ranker_inputs()
    return GlobalOpportunityRanker().score(
        candidate, result.model_copy(update={"global_instrument_id": rule.global_instrument_id}))


def _search_run(state: str):
    outcome = ProviderOutcome(provider="web", outcome=state, candidate_count=0, queries_planned=1,
                              queries_completed=0 if state in {"FAILED", "DEGRADED"} else 1,
                              failure_code="SEARCH_PROVIDER_UNAVAILABLE" if state == "FAILED" else None)
    return aggregate_search(sre.INSTRUMENT_ID, [outcome], started_at=sre.NOW - timedelta(minutes=5),
                            completed_at=sre.NOW - timedelta(minutes=1), qualifying_events=0)


def test_contract_registry_marks_rule_engine_scoring_inputs_mandatory():
    mandatory = {r.requirement_id: {i.input_id for i in r.inputs if i.importance.value == "MANDATORY"}
                 for r in REGISTRY.requirements}
    assert all(r.mandatory for r in REGISTRY.requirements)
    assert {"ROE", "ROCE", "MARGINS", "CASH_CONVERSION_OR_FCF_QUALITY"} <= mandatory["BUSINESS_QUALITY_FACTS"]
    assert {"QUARTERLY_REVENUE", "QUARTERLY_PAT", "QUARTERLY_EPS"} <= mandatory["QUARTERLY_FINANCIALS"]
    assert {"ORDER_BOOK_OR_MAJOR_CONTRACT", "CAPACITY_OR_CAPEX_OR_COMMISSIONING"} <= mandatory["ORDER_BOOK_CAPEX_GUIDANCE"]
    assert mandatory["CURRENT_NEWS"] == {"RELEVANT_CURRENT_EVENT_EVIDENCE"}
    assert "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" in mandatory["SHAREHOLDING"]
    # Structured statement-derived ROCE is visible to readiness exactly as scored.
    assert _structured_fact_coverage("roce", {}) == {"BUSINESS_QUALITY_FACTS": {"ROCE"}}


# A ---------------------------------------------------------------------------
def test_a_missing_applicable_shareholding_blocks_full_analysis_and_is_acquired():
    readiness, plan, gate = _gate(rr.complete_snapshot(omit={"SHAREHOLDING"}))
    assert readiness.for_requirement("SHAREHOLDING").status == Status.MISSING
    assert not gate.full_analysis_allowed and gate.blocking_requirements == ["SHAREHOLDING"]
    assert "SHAREHOLDING" in rr.target_ids(plan)
    # A period without ownership categories is not a complete shareholding dataset.
    readiness, _, gate = _gate(rr.complete_snapshot(overrides={"SHAREHOLDING": _all_but(
        "SHAREHOLDING", "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES")}))
    assert readiness.for_requirement("SHAREHOLDING").status == Status.PARTIAL
    assert gate.blocking_requirements == ["SHAREHOLDING"]
    # Rule Engine: same readiness never yields a rank-eligible complete result.
    result = StockRuleEngineV1().evaluate(sre._inputs(readiness=sre._readiness(
        {"SHAREHOLDING": Status.MISSING}), shareholding=()), allow_partial=False)
    assert result.partial and result.overall_score is None and not _ranked(result).rank_eligible


def test_a_shareholding_outside_indian_regime_is_explicitly_unsupported_not_blocking():
    _, _, gate = _gate(rr.complete_snapshot(omit={"SHAREHOLDING"}, supported=frozenset(
        r.requirement_id for r in REGISTRY.requirements if r.requirement_id != "SHAREHOLDING")))
    assert gate.full_analysis_allowed


# B ---------------------------------------------------------------------------
def test_b_missing_applicable_catalyst_or_capex_blocks_unless_explicitly_not_applicable():
    readiness, plan, gate = _gate(rr.complete_snapshot(omit={"ORDER_BOOK_CAPEX_GUIDANCE"}))
    assert readiness.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE").status == Status.MISSING
    assert gate.blocking_requirements == ["ORDER_BOOK_CAPEX_GUIDANCE"]
    assert "ORDER_BOOK_CAPEX_GUIDANCE" in rr.target_ids(plan)
    readiness, _, gate = _gate(rr.complete_snapshot(overrides={"ORDER_BOOK_CAPEX_GUIDANCE": _all_but(
        "ORDER_BOOK_CAPEX_GUIDANCE", "CAPACITY_OR_CAPEX_OR_COMMISSIONING", "MANAGEMENT_GUIDANCE")}))
    item = readiness.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE")
    assert item.status == Status.PARTIAL
    assert item.missing_reason == "MISSING_REQUIRED_INPUTS:CAPACITY_OR_CAPEX_OR_COMMISSIONING"
    assert not gate.full_analysis_allowed


def test_b_unknown_business_classification_is_not_simulated_as_not_applicable():
    applicability = classify_requirements(None, None, None)
    snapshot = replace(rr.complete_snapshot(overrides={"ORDER_BOOK_CAPEX_GUIDANCE": _all_but(
        "ORDER_BOOK_CAPEX_GUIDANCE", "ORDER_BOOK_OR_MAJOR_CONTRACT")}), applicability_by_requirement=applicability)
    readiness, _, gate = _gate(snapshot)
    assert readiness.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE").status == Status.PARTIAL
    assert not gate.full_analysis_allowed


# C ---------------------------------------------------------------------------
def test_c_news_check_never_executed_is_planned_but_optional_and_non_blocking():
    # CURRENT_NEWS is optional contextual research: a never-attempted (MISSING)
    # check is still automatically planned/acquired (mandatory for the refresh
    # planner), but its MISSING state must never appear as a blocking
    # requirement or prevent full_analysis_allowed / a scored, non-partial
    # Rule Engine result. Contrast with SHAREHOLDING/ORDER_BOOK_CAPEX_GUIDANCE
    # above (tests A/B), which still block when MISSING -- this exemption is
    # specific to CURRENT_NEWS.
    readiness, plan, gate = _gate(rr.complete_snapshot(omit={"CURRENT_NEWS"}))
    assert readiness.for_requirement("CURRENT_NEWS").status == Status.MISSING
    assert "CURRENT_NEWS" not in gate.blocking_requirements
    assert gate.full_analysis_allowed
    assert "CURRENT_NEWS" in rr.target_ids(plan)
    result = StockRuleEngineV1().evaluate(sre._inputs(readiness=sre._readiness(
        {"CURRENT_NEWS": Status.MISSING})), allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial
    assert result.decision_signal != DecisionSignal.INSUFFICIENT_DATA and result.overall_score is not None


# D ---------------------------------------------------------------------------
def test_d_completed_zero_result_news_check_is_explicit_coverage_without_fabricated_event():
    old_catalyst = sre._event(at=sre.NOW - timedelta(days=90))  # outside the 30-day news window
    value = replace(sre._inputs(events=(old_catalyst,)), news_search_run=_search_run("SUCCESS_EMPTY"))
    engine = StockRuleEngineV1()
    news = engine._news(value)
    assert news.status == AreaScoreStatus.READY_FRESH and news.applicable
    assert news.raw_score is None and news.metrics == [] and news.missing_inputs == []
    assert f"search-run:{value.news_search_run.run_id}" in news.evidence_references
    assert not any(str(old_catalyst.event_id) in ref for ref in news.evidence_references)
    result = engine.evaluate(value, allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial and result.overall_score is not None
    # Without a completed check the NEWS area is UNSCORABLE, but CURRENT_NEWS
    # is optional contextual research: the result must still be full/non-partial
    # and CURRENT_NEWS must never appear as a blocking requirement.
    unchecked = engine.evaluate(replace(value, news_search_run=None), allow_partial=False)
    assert unchecked.eligibility.full_analysis_allowed and not unchecked.partial
    assert "CURRENT_NEWS" not in unchecked.eligibility.blocking_requirements


def test_d_partial_or_failed_search_is_not_completed_coverage():
    for state in ("FAILED", "PARTIAL"):
        value = replace(sre._inputs(events=(sre._event(at=sre.NOW - timedelta(days=90)),)),
                        news_search_run=_search_run(state))
        assert StockRuleEngineV1()._news(value).status == AreaScoreStatus.UNSCORABLE


# E ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["ROCE", "MARGINS", "ROE", "CASH_CONVERSION_OR_FCF_QUALITY"])
def test_e_missing_quality_scoring_input_cannot_produce_complete_result(missing):
    readiness, _, gate = _gate(rr.complete_snapshot(overrides={
        "BUSINESS_QUALITY_FACTS": _all_but("BUSINESS_QUALITY_FACTS", missing)}))
    item = readiness.for_requirement("BUSINESS_QUALITY_FACTS")
    assert item.status == Status.PARTIAL and item.missing_reason == f"MISSING_REQUIRED_INPUTS:{missing}"
    assert gate.blocking_requirements == ["BUSINESS_QUALITY_FACTS"]
    result = StockRuleEngineV1().evaluate(sre._inputs(readiness=sre._readiness(
        {"BUSINESS_QUALITY_FACTS": Status.PARTIAL}, critical_pct=90)), allow_partial=False)
    quality = sre._area(result, sre.RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY)
    assert quality.status != AreaScoreStatus.READY_FRESH
    assert result.partial and result.overall_score is None and not _ranked(result).rank_eligible


# F ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["QUARTERLY_EPS", "QUARTERLY_PAT", "COMPARABLE_QUARTERS"])
def test_f_missing_quarterly_scoring_input_cannot_produce_complete_quarterly_area(missing):
    readiness, _, gate = _gate(rr.complete_snapshot(overrides={
        "QUARTERLY_FINANCIALS": _all_but("QUARTERLY_FINANCIALS", missing)}))
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").status == Status.PARTIAL
    assert "QUARTERLY_FINANCIALS" in gate.blocking_requirements and not gate.full_analysis_allowed
    result = StockRuleEngineV1().evaluate(sre._inputs(readiness=sre._readiness(
        {"QUARTERLY_FINANCIALS": Status.PARTIAL}, critical_pct=90)), allow_partial=False)
    quarterly = sre._area(result, sre.RuleEngineArea.QUARTERLY_EARNINGS_TREND)
    assert quarterly.status == AreaScoreStatus.PARTIAL and missing in quarterly.missing_inputs
    assert not result.eligibility.full_analysis_allowed and result.overall_score is None


# G ---------------------------------------------------------------------------
def test_g_explicit_not_applicable_remains_allowed():
    etf = classify_requirements("Funds", "Exchange Traded Fund", "CANONICAL", asset_type="ETF")
    readiness, plan, gate = _gate(replace(rr.complete_snapshot(omit={"ORDER_BOOK_CAPEX_GUIDANCE"}),
                                          applicability_by_requirement=etf))
    assert readiness.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE").status == Status.NOT_APPLICABLE
    assert gate.full_analysis_allowed and "ORDER_BOOK_CAPEX_GUIDANCE" not in rr.target_ids(plan)
    # Financial issuer: ORDER_BOOK concept and industrial ROCE are explicitly N/A,
    # every other required input is still required.
    bank = classify_requirements("Financial Services", "Banks", "CANONICAL")
    snapshot = replace(rr.complete_snapshot(overrides={
        "ORDER_BOOK_CAPEX_GUIDANCE": _all_but("ORDER_BOOK_CAPEX_GUIDANCE", "ORDER_BOOK_OR_MAJOR_CONTRACT"),
        "BUSINESS_QUALITY_FACTS": _all_but("BUSINESS_QUALITY_FACTS", "ROCE")}), applicability_by_requirement=bank)
    readiness, _, gate = _gate(snapshot)
    assert readiness.for_requirement("BUSINESS_QUALITY_FACTS").status == Status.READY_FRESH
    assert readiness.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE").status == Status.READY_FRESH
    assert gate.full_analysis_allowed
    snapshot = replace(rr.complete_snapshot(overrides={"ORDER_BOOK_CAPEX_GUIDANCE": _all_but(
        "ORDER_BOOK_CAPEX_GUIDANCE", "ORDER_BOOK_OR_MAJOR_CONTRACT", "CAPACITY_OR_CAPEX_OR_COMMISSIONING")}),
        applicability_by_requirement=bank)
    assert not _gate(snapshot)[2].full_analysis_allowed  # CAPEX stays applicable for banks


# H ---------------------------------------------------------------------------
def test_h_technical_acquisition_failure_stays_retryable_not_unavailable():
    readiness, _, gate = _gate(rr.complete_snapshot(omit={"SHAREHOLDING"},
                                                    failures={"SHAREHOLDING": "NETWORK_TIMEOUT"}))
    assert readiness.for_requirement("SHAREHOLDING").status == Status.FAILED
    assert classify_requirement_failures({"SHAREHOLDING": "NETWORK_TIMEOUT"},
                                         gate.blocking_requirements) == TECHNICAL_RETRYABLE
    assert classify_requirement_failures({"SHAREHOLDING": "DISCOVERY_NO_CANDIDATES"},
                                         gate.blocking_requirements) == EVIDENCE_UNAVAILABLE
    # An incomplete news search is recorded as a technical failure, never "no news".
    for state in ("FAILED", "PARTIAL"):
        reason = _incomplete_news_search_reason((_search_run(state), []))
        assert reason and classify_requirement_failures({"CURRENT_NEWS": reason}) == TECHNICAL_RETRYABLE
    assert _incomplete_news_search_reason((_search_run("SUCCESS_EMPTY"), [])) is None
    assert _incomplete_news_search_reason(None) is None


@pytest.mark.asyncio
async def test_h_runtime_records_failed_news_search_as_retryable_failure():
    from app.research_readiness_runtime import ExistingResearchCapabilityExecutor

    outcomes = {"FAILED": _search_run("FAILED"), "SUCCESS_EMPTY": _search_run("SUCCESS_EMPTY")}
    for state, run in outcomes.items():
        class _Repo:
            async def refresh_news_intelligence(self, _key, _run=run):
                return _run, []

        executor = ExistingResearchCapabilityExecutor(_Repo(), SimpleNamespace(), SimpleNamespace())
        target = SimpleNamespace(requirement_id="CURRENT_NEWS", excluded_input_ids=())
        result = await executor.execute_primary(sre.INSTRUMENT_ID, [target], jurisdiction="INDIA",
                                                correlation_id=None, identity_headers=None)
        if state == "FAILED":
            assert classify_requirement_failures(result.failures) == TECHNICAL_RETRYABLE
        else:
            assert "CURRENT_NEWS" not in result.failures


# I ---------------------------------------------------------------------------
def test_i_rule_engine_never_returns_full_result_with_applicable_unscorable_area():
    # Readiness claims catalysts are covered, but no material catalyst event can
    # be scored: the rules must not report a complete, rank-eligible result.
    immaterial = sre._event(event_type=ResearchEventType.MAJOR_CONTRACT, monetary=None,
                            classification=sre.SourceClassification.REPUTABLE_NEWS,
                            title="Contract mentioned", summary="A generic article mentions a contract.")
    value = sre._inputs(events=(sre._event(event_type=ResearchEventType.OTHER, monetary=None,
                                           title="Board meeting", summary="Board meeting held."), immaterial))
    engine = StockRuleEngineV1()
    assert POLICY.evaluate(value.readiness).full_analysis_allowed
    result = engine.evaluate(value, allow_partial=False)
    catalysts = sre._area(result, sre.RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS)
    assert catalysts.applicable and catalysts.status == AreaScoreStatus.UNSCORABLE
    assert not result.eligibility.full_analysis_allowed and result.partial
    assert result.eligibility.reason == "APPLICABLE_RULE_ENGINE_AREA_UNSCORABLE"
    assert "ORDER_BOOK_CAPEX_GUIDANCE" in result.eligibility.blocking_requirements
    assert result.overall_score is None and result.decision_signal == DecisionSignal.INSUFFICIENT_DATA
    assert not _ranked(result).rank_eligible
    labelled = engine.evaluate(value, allow_partial=True)
    assert labelled.partial and labelled.overall_score is not None
    assert labelled.decision_signal not in {DecisionSignal.STRONG_BUY, DecisionSignal.BUY}


# J ---------------------------------------------------------------------------
def test_j_fully_complete_fixture_remains_rank_eligible():
    _, _, gate = _gate(rr.complete_snapshot())
    assert gate.full_analysis_allowed and gate.blocking_requirements == []
    result = StockRuleEngineV1().evaluate(sre._inputs(), allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial
    assert result.overall_score is not None
    assert not [a for a in result.area_scores if a.applicable and a.status == AreaScoreStatus.UNSCORABLE]
    assert _ranked(result).rank_eligible
