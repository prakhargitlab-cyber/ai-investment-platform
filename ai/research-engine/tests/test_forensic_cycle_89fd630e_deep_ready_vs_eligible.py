"""Forensic analysis of controlled cycle 89fd630e-5212-4018-ba6d-09889f83822e.

Issue 8 -- DEEP_READY=6 BUT ELIGIBLE=3:
25 Stage-2 candidates: deep_ready=6, rule_analyzed=6, evaluated=6, eligible=3.

Trace (app/global_opportunity_orchestration.py's Stage-2 body, read in full):

1. `deep_ready_count` increments as soon as
   `rule_engine.eligibility_policy.evaluate(readiness_result).full_analysis_allowed`
   is True -- i.e. deep readiness/evidence sufficiency, nothing about
   scoring yet.
2. `rule_engine.analyze()` then runs. If `_incomplete_analysis()` finds an
   applicable required rule area came back UNSCORABLE despite readiness
   being nominally sufficient, `deep_ready_count` is explicitly
   decremented and the candidate is reclassified DEEP_READINESS_NOT_MET
   (never happened in this cycle: rule_analyzed(6) == deep_ready(6), so
   this correction fired zero times here).
3. For every candidate that reaches `ranker.score()`,
   `GlobalOpportunityRanker.score()` computes `rank_eligible = not gates`
   -- an INTENTIONALLY SEPARATE Stage-B scoring gate (CRITICAL_STALE_PRICE,
   CRITICAL_CONFLICTING_PRICE, NO_SCORABLE_EVIDENCE -- see
   app/global_opportunity_ranker.py), concerned with price-data staleness/
   conflict and scorable-weight coverage for RANKING, not with deep-
   readiness/evidence completeness (already settled by step 1-2).

Conclusion: deep_ready=6 -> eligible=3 is NOT a readiness/repair defect.
It is the documented, already-tested, intentional Stage-B ranking gate
(`rank_eligible=not gates`) rejecting 3 of the 6 fully-evidenced,
fully-analyzed candidates on scoring-layer grounds (stale/conflicting price
data, or no scorable technical/sector weight) -- a transition the business
contract explicitly wants kept separate from deep-readiness. This is
pinned here as a named regression so a future change cannot silently
conflate the two gates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.global_scanner import StageBCandidate
from app.sector_relative_strength import SectorRelativeStrengthSnapshot
from app.stock_rule_engine import AreaScoreResult, STOCK_RULE_ENGINE_AREA_WEIGHTS, StockRuleEngineResult
from app.technical_features import TechnicalFeatureSnapshot

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def _deep_ready_candidate(n: int, *, stale_price: bool = False) -> tuple:
    """A candidate that is fully deep-ready (every rule area READY_FRESH,
    full_analysis_allowed=True, rule_engine.analyze() succeeded) -- exactly
    `deep_ready_count`/`rule_analyzed_count`/`evaluated_count`'s state --
    optionally with a stale current price (a Stage-B-only concern)."""
    key = UUID(int=n)
    technical = TechnicalFeatureSnapshot(global_instrument_id=key, as_of=NOW, configuration={},
        observation_count=200, history_readiness='FULL', technical_score=60, confidence=80,
        technical_state='UPTREND', latest_price=100,
        stale_inputs=['PRICE_HISTORY'] if stale_price else [])
    relative = SectorRelativeStrengthSnapshot(global_instrument_id=key, as_of=NOW, configuration={},
        relative_strength_score=40, confidence=70, sector_state='NEUTRAL')
    candidate = StageBCandidate(global_instrument_id=key, symbol='FORENSIC', pre_score=99,
        technical_feature_snapshot=technical, sector_relative_strength_snapshot=relative,
        technical_score=60, sector_score=40, stage_b_score=99, confidence=99,
        score_coverage=100, score_weights={})
    rule = StockRuleEngineResult(global_instrument_id=key, calculated_at=NOW, input_fingerprint='fixture',
        overall_score=75, quality_score=99, opportunity_score=99, risk_score=20,
        confidence_score=90, confidence='HIGH', decision_signal='HOLD', partial=False,
        eligibility=dict(full_analysis_allowed=True, partial_analysis_allowed=True, reason='READY'),
        area_scores=[AreaScoreResult(area=name, weight=weight, raw_score=80, applicable=True,
            status='READY_FRESH') for name, weight in STOCK_RULE_ENGINE_AREA_WEIGHTS.items()])
    return candidate, rule


def test_deep_ready_and_fully_analyzed_but_stale_price_is_rank_filtered_not_a_readiness_defect() -> None:
    """Reproduces the exact forensic gap for one candidate: readiness was
    sufficient and the rule engine fully analyzed it (this candidate counts
    toward deep_ready/rule_analyzed/evaluated), yet it is correctly excluded
    from `eligible` by the SEPARATE, intentional Stage-B price-staleness
    gate -- not by any readiness/repair/technical-failure mechanism."""
    candidate, rule = _deep_ready_candidate(1, stale_price=True)
    result = GlobalOpportunityRanker().score(candidate, rule)
    assert not result.rank_eligible
    assert any(code.startswith('CRITICAL_') for code in result.eligibility_reasons)


def test_deep_ready_and_fully_analyzed_with_clean_price_is_eligible() -> None:
    """The counterpart: the same fully-evidenced, fully-analyzed candidate
    with no Stage-B gate tripped IS rank_eligible -- confirming the gate
    pinned above is the (only) thing that moved the other candidate from
    deep_ready to suppressed, not some readiness regression."""
    candidate, rule = _deep_ready_candidate(2, stale_price=False)
    result = GlobalOpportunityRanker().score(candidate, rule)
    assert result.rank_eligible
