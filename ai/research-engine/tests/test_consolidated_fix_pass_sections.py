"""Consolidated fix pass -- regression tests locking in the Sections A, B,
D, E, F, G, J invariants that a careful trace found were ALREADY correctly
implemented by the existing architecture (readiness importance tiers ->
StockRuleEngineEligibilityPolicy -> rule.partial -> ranker's
V1_ANALYSIS_NOT_ELIGIBLE gate -> RecommendationEngineV1's `qualified` check),
plus the two concrete defects this pass actually fixed (Sections C/D's
MISSING:<OPTIONAL_AREA> misclassification, and Section H's phase-end
CURRENT_NEWS drain -- both covered by their own dedicated test files:
test_step8b_ranker_reason_consistency.py and
test_background_task_registry*.py).

No production code in stock_rule_engine.py, research_readiness.py, or
recommendation_engine.py needed to change for the tests in this file: they
exist to make the following invariants explicit and regression-proof, not
because a defect was found in them. See the final consolidated-fix-pass
report for the full per-section reasoning.
"""
from __future__ import annotations

import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.recommendation_engine import RecommendationEngineV1
from app.research_readiness import ResearchRequirementStatus
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy, StockRuleEngineV1

from test_stock_rule_engine import INSTRUMENT_ID, _inputs, _readiness
from test_global_opportunity_ranker import inputs as ranker_inputs, area as ranker_area

POLICY = StockRuleEngineEligibilityPolicy()
ENGINE = StockRuleEngineV1()


def _ranked(rule_result):
    candidate, rule = ranker_inputs()
    return GlobalOpportunityRanker().score(
        candidate, rule_result.model_copy(update={"global_instrument_id": rule.global_instrument_id}))


# 1 -- mandatory research incomplete => not rank eligible --------------------
def test_01_mandatory_incomplete_quarterly_financials_blocks_rank_eligibility():
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING})
    eligibility = POLICY.evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert "QUARTERLY_FINANCIALS" in eligibility.blocking_requirements

    rule_result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=True)
    assert rule_result.partial
    assert not _ranked(rule_result).rank_eligible


# 2 -- mandatory technical (LATEST_PRICE) failure => not rank eligible -------
def test_02_mandatory_latest_price_failure_blocks_rank_eligibility():
    readiness = _readiness({"LATEST_PRICE": ResearchRequirementStatus.MISSING})
    eligibility = POLICY.evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert "LATEST_PRICE" in eligibility.blocking_requirements

    rule_result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=True)
    assert rule_result.partial
    assert not _ranked(rule_result).rank_eligible


# 3 -- mandatory genuinely unavailable => truthful terminal accounting -------
def test_03_mandatory_unscorable_area_never_reads_as_fully_analyzed():
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING})
    rule_result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=True)
    # _incomplete_analysis is the single truthful-terminal-accounting gate
    # between the Rule Engine and Stage B / ranking: a real mandatory gap
    # must never be silently treated as a complete, fully-analyzed result.
    incomplete = GlobalOpportunityOrchestrator._incomplete_analysis(rule_result, readiness)
    assert incomplete is not None
    assert incomplete != []


def test_03b_current_news_alone_never_triggers_incomplete_analysis():
    # The CURRENT_NEWS exemption inside _incomplete_analysis (see its own
    # docstring/comment) must not be accidentally widened: this is the
    # control proving test 03's assertion is meaningful, not just always-true.
    readiness = _readiness({"CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    rule_result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=False)
    assert GlobalOpportunityOrchestrator._incomplete_analysis(rule_result, readiness) is None


# 9-extra -- the generic reason-builder fix (Section C/D) covers an ordinary
# fundamental area too, not just MANAGEMENT_GOVERNANCE/NEWS_GEOPOLITICAL_EVENTS
def test_09_extra_generic_fix_also_covers_an_arbitrary_ready_area():
    c, r = ranker_inputs()
    ranker_area(r, 'VALUATION').raw_score = None  # status stays READY_FRESH (verified-clean/no-score)
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:VALUATION' not in result.top_negative_reasons


# 10 -- a PARTIAL area with usable evidence is never mislabeled MISSING ------
def test_10_partial_area_with_real_score_is_not_mislabeled_missing():
    c, r = ranker_inputs()
    area = ranker_area(r, 'BALANCE_SHEET')
    area.status = 'PARTIAL'
    area.raw_score = 55  # usable evidence exists for part of the area
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:BALANCE_SHEET' not in result.top_negative_reasons
    # Still contributes to scoring like any other usable area -- being
    # PARTIAL does not silently drop it from the score either.
    assert result.score_coverage == 100


def test_10b_partial_area_with_no_score_is_excluded_not_mislabeled_missing():
    c, r = ranker_inputs()
    area = ranker_area(r, 'BALANCE_SHEET')
    area.status = 'PARTIAL'
    area.raw_score = None  # verified-clean/checked-but-unscored, same as the governance case
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:BALANCE_SHEET' not in result.top_negative_reasons
    assert result.score_coverage < 100  # honestly excluded from the denominator, not scored


# 11 -- rule.partial is a coherent, mandatory-completeness-only signal ------
def test_11_partial_field_tracks_only_mandatory_completeness():
    full_readiness = _readiness()
    full_result = ENGINE.evaluate(_inputs(readiness=full_readiness), allow_partial=True)
    assert full_result.partial is False

    # An OPTIONAL-tier gap (CURRENT_NEWS) alone must not flip `partial` true.
    news_missing = _readiness({"CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    news_result = ENGINE.evaluate(_inputs(readiness=news_missing), allow_partial=True)
    assert news_result.partial is False

    # A genuine MANDATORY-tier gap must.
    mandatory_missing = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING})
    mandatory_result = ENGINE.evaluate(_inputs(readiness=mandatory_missing), allow_partial=True)
    assert mandatory_result.partial is True


# 12 -- recommendation cannot become BUY_CANDIDATE from mandatory-incomplete research
def _snapshot(rank_eligible, score=95, confidence=90, coverage=100):
    return {
        'global_instrument_id': str(INSTRUMENT_ID), 'market': 'NSE', 'symbol': 'TEST', 'company_name': 'Test',
        'generated_at': '2026-09-10T12:00:00+00:00', 'rule_engine_version': 'v1', 'ranker_version': 'v1',
        'opportunity_score': score, 'top_positive_reasons': [], 'top_negative_reasons': [],
        'rank_eligible': rank_eligible, 'opportunity_confidence': confidence, 'score_coverage': coverage,
        'current_price': 100.0, 'evidence_state': {'rule': {}, 'technical': {}},
    }


def test_12_rank_ineligible_never_becomes_buy_candidate_even_with_high_score():
    result = RecommendationEngineV1().evaluate(_snapshot(rank_eligible=False, score=95, confidence=95, coverage=100))
    assert result['new_investor_action'] not in {'BUY_CANDIDATE', 'STRONG_BUY_CANDIDATE'}
    assert result['new_investor_action'] == 'WATCH_WAIT'


def test_12b_rank_eligible_with_qualifying_score_can_become_buy_candidate():
    # Control: proves test 12 is a real, meaningful gate -- not just an
    # engine that always returns WATCH_WAIT regardless of input.
    result = RecommendationEngineV1().evaluate(_snapshot(rank_eligible=True, score=95, confidence=95, coverage=100))
    assert result['new_investor_action'] in {'BUY_CANDIDATE', 'STRONG_BUY_CANDIDATE'}
