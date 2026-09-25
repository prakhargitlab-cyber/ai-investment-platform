"""CURRENT_NEWS is optional contextual research (narrow correction).

Prior behavior exempted only a CURRENT_NEWS *technical/provider search
failure* (PARTIAL / FAILED) from the fundamental eligibility gates, while
MISSING (never-attempted) CURRENT_NEWS still blocked full_analysis_allowed /
the Rule Engine / rank_eligible. That contradicted the intended full-research
contract, under which CURRENT_NEWS must never by itself block eligibility in
ANY truthful state.

This file proves the corrected, blanket, state-independent CURRENT_NEWS
exemption (see app.stock_rule_engine._current_news_is_optional_and_non_blocking)
end to end: eligibility policy -> Rule Engine evaluation -> orchestration
completeness gate -> ranker rank_eligible -- while confirming mandatory
fundamental requirements are completely unaffected (test F) and CURRENT_NEWS
diagnostics remain visible (test G).
"""
from dataclasses import replace

import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.news_intelligence import ProviderOutcome, aggregate_search
from app.research_readiness import ResearchRequirementStatus
from app.stock_rule_engine import AreaScoreStatus, DecisionSignal, StockRuleEngineEligibilityPolicy, StockRuleEngineV1

from test_stock_rule_engine import NOW, INSTRUMENT_ID, _inputs, _readiness
from test_global_opportunity_ranker import inputs as ranker_inputs

POLICY = StockRuleEngineEligibilityPolicy()
ENGINE = StockRuleEngineV1()


def _ranked(result):
    candidate, rule = ranker_inputs()
    return GlobalOpportunityRanker().score(
        candidate, result.model_copy(update={"global_instrument_id": rule.global_instrument_id}))


def _search_run(state: str):
    outcome = ProviderOutcome(provider="web", outcome=state, candidate_count=0, queries_planned=1,
        queries_completed=0 if state in {"FAILED", "DEGRADED"} else 1,
        failure_code="SEARCH_PROVIDER_UNAVAILABLE" if state == "FAILED" else None)
    from datetime import timedelta
    return aggregate_search(INSTRUMENT_ID, [outcome], started_at=NOW - timedelta(minutes=5),
        completed_at=NOW - timedelta(minutes=1), qualifying_events=0)


def _assert_fully_unblocked(status):
    readiness = _readiness({"CURRENT_NEWS": status})

    # Eligibility policy gate.
    eligibility = POLICY.evaluate(readiness)
    assert eligibility.full_analysis_allowed
    assert "CURRENT_NEWS" not in eligibility.blocking_requirements

    # Rule Engine evaluation. (Default fixture events/facts -- not overridden
    # to an empty events tuple -- so only CURRENT_NEWS varies; an empty events
    # tuple would separately make ORDER_BOOK_CAPACITY_CATALYSTS UNSCORABLE,
    # which is an unrelated, still-blocking area and not what this proves.)
    result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=False)
    assert result.eligibility.full_analysis_allowed
    assert not result.partial
    assert result.overall_score is not None
    assert result.decision_signal != DecisionSignal.INSUFFICIENT_DATA
    assert "CURRENT_NEWS" not in result.eligibility.blocking_requirements

    # Orchestration completeness gate -- no DEEP_READINESS_NOT_MET solely for news.
    incomplete = GlobalOpportunityOrchestrator._incomplete_analysis(result, readiness)
    assert incomplete is None

    # Ranker eligibility.
    scored = _ranked(result)
    assert scored.rank_eligible
    assert "CURRENT_NEWS" not in scored.eligibility_reasons
    return result, eligibility, scored


# A -----------------------------------------------------------------------------
def test_a_current_news_missing_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.MISSING)


# B -----------------------------------------------------------------------------
def test_b_current_news_partial_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.PARTIAL)


# C -----------------------------------------------------------------------------
def test_c_current_news_failed_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.FAILED)


# D -----------------------------------------------------------------------------
def test_d_usable_ready_news_behavior_is_unchanged():
    # READY_FRESH / READY_STALE CURRENT_NEWS is untouched by this change: it
    # was never blocking before and remains fully scorable/usable now, and a
    # real qualifying event still contributes to the NEWS area score exactly
    # as before.
    for status in (ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE):
        readiness = _readiness({"CURRENT_NEWS": status})
        eligibility = POLICY.evaluate(readiness)
        assert eligibility.full_analysis_allowed
        assert "CURRENT_NEWS" not in eligibility.blocking_requirements
    result = ENGINE.evaluate(_inputs(readiness=_readiness()), allow_partial=False)
    news_area = next(item for item in result.area_scores if item.area == "NEWS_GEOPOLITICAL_EVENTS")
    assert news_area.raw_score is not None  # the default fixture's qualifying event still scores.
    assert result.eligibility.full_analysis_allowed and not result.partial


# E -----------------------------------------------------------------------------
def test_e_success_empty_news_remains_truthful_and_non_blocking():
    # A completed zero-result news check (SUCCESS_EMPTY) stays an explicit,
    # truthful READY area with no fabricated event/metric -- this was already
    # non-blocking, and this test pins that SUCCESS_EMPTY's *meaning* is
    # unchanged by the CURRENT_NEWS optional-eligibility correction.
    from datetime import timedelta
    from test_stock_rule_engine import _event
    old_catalyst = _event(at=NOW - timedelta(days=90))  # outside the 30-day news window
    value = replace(_inputs(events=(old_catalyst,)), news_search_run=_search_run("SUCCESS_EMPTY"))
    news = ENGINE._news(value)
    assert news.status == AreaScoreStatus.READY_FRESH
    assert news.raw_score is None and news.metrics == []  # zero-result stays zero-result, never fabricated
    assert not any(str(old_catalyst.event_id) in ref for ref in news.evidence_references)
    result = ENGINE.evaluate(value, allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial and result.overall_score is not None


# F -----------------------------------------------------------------------------
def test_f_missing_mandatory_fundamental_requirement_still_blocks():
    # Proof that the correction is scoped to CURRENT_NEWS only: any other
    # mandatory/applicable fundamental requirement continues to block exactly
    # as before, even when CURRENT_NEWS itself is fully READY.
    for requirement_id in ("LATEST_PRICE", "SHAREHOLDING", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"):
        readiness = _readiness({requirement_id: ResearchRequirementStatus.MISSING})
        eligibility = POLICY.evaluate(readiness)
        assert not eligibility.full_analysis_allowed
        assert requirement_id in eligibility.blocking_requirements
        result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=False)
        assert result.partial and result.overall_score is None
        assert not _ranked(result).rank_eligible


# G -----------------------------------------------------------------------------
def test_g_current_news_diagnostics_remain_visible_despite_non_blocking_eligibility():
    # CURRENT_NEWS absence/failure stays truthfully visible in readiness state
    # and in the NEWS area's own status/missing_inputs -- only its power to
    # gate eligibility/ranking was removed. Uses the NEWS area rule directly
    # (with no qualifying event in scope) so this is isolated from the
    # unrelated ORDER_BOOK_CAPACITY_CATALYSTS area, which also goes UNSCORABLE
    # on an empty events tuple and is not part of what this proves.
    for status in (ResearchRequirementStatus.MISSING, ResearchRequirementStatus.PARTIAL, ResearchRequirementStatus.FAILED):
        readiness = _readiness({"CURRENT_NEWS": status})
        assert readiness.for_requirement("CURRENT_NEWS").status == status  # truthful readiness state preserved
        value = replace(_inputs(events=[]), readiness=readiness)
        news_area = ENGINE._news(value)
        assert news_area.status == AreaScoreStatus.UNSCORABLE  # still visible as unscored, not silently hidden
        assert "RELEVANT_CURRENT_EVENT_WITHIN_30_DAYS" in news_area.missing_inputs  # still surfaced in diagnostics
        # Eligibility/ranking are nonetheless fully unblocked by CURRENT_NEWS alone.
        eligibility = POLICY.evaluate(readiness)
        assert eligibility.full_analysis_allowed
        assert "CURRENT_NEWS" not in eligibility.blocking_requirements
