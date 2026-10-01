"""STEP 8B -- ranker top_negative_reasons/MISSING consistency for a
verified-clean (READY_FRESH/READY_STALE, no raw_score) Rule Engine area.

Root cause: GlobalOpportunityRanker.score()'s per-area loop treated
"READY_FRESH/PARTIAL but area.raw_score is None" as an unhandled case that
fell through to `negatives.add('MISSING:' + name)`. Before STEP 8A this
combination could never actually occur -- StockRuleEngineV1._finish() only
ever returns READY_FRESH/PARTIAL when it has computed a real raw_score from
non-empty metrics; an empty-metrics area is always UNSCORABLE, which
correctly falls through to MISSING. STEP 8A's governance verified-clean-check
upgrade (StockRuleEngineV1._governance) introduced the first real case of
READY_FRESH/READY_STALE with raw_score=None: an authoritative check
completed and found nothing adverse, so the area is genuinely covered, but
deliberately carries no fabricated score. The ranker's generic reason
builder didn't know about this state and mislabeled it MISSING, producing
exactly the observed contradiction: rank_eligible=true, suppression_reasons
empty, but top_negative_reasons still contains 'MISSING:MANAGEMENT_GOVERNANCE'.

The same pattern was already latent (pre-STEP-8A, never exercised by any
existing ranker test) for StockRuleEngineV1._news's identical clean
current-news-search upgrade on NEWS_GEOPOLITICAL_EVENTS -- confirming this is
a generic reason-builder defect, not governance-specific. The fix in
app/global_opportunity_ranker.py adds an explicit branch: READY_FRESH/PARTIAL
with raw_score=None is excluded from `values` (unchanged from before -- it
was never scorable) and no longer added to `negatives`. Every other branch
(STALE/CONFLICTING, scored READY_FRESH/PARTIAL, true UNSCORABLE/absent-area
MISSING) is untouched.
"""
from __future__ import annotations

import pytest

from test_global_opportunity_ranker import inputs, area
from app.global_opportunity_ranker import GlobalOpportunityRanker


# 1 -- governance READY_FRESH + verified-clean evidence => no MISSING -------
def test_01_governance_ready_fresh_verified_clean_no_missing_reason():
    c, r = inputs()
    area(r, 'MANAGEMENT_GOVERNANCE').raw_score = None  # status stays READY_FRESH
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:MANAGEMENT_GOVERNANCE' not in result.top_negative_reasons
    assert result.rank_eligible


# 2 -- governance READY_STALE + verified-clean evidence => no MISSING -------
def test_02_governance_ready_stale_verified_clean_no_missing_reason():
    c, r = inputs()
    area(r, 'MANAGEMENT_GOVERNANCE').status = 'READY_STALE'
    area(r, 'MANAGEMENT_GOVERNANCE').raw_score = None
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:MANAGEMENT_GOVERNANCE' not in result.top_negative_reasons
    # Pre-existing, unrelated STALE-evidence gating for a CRITICAL_AREA is
    # untouched by this fix: a genuinely stale critical area still gates.
    assert 'STALE:MANAGEMENT_GOVERNANCE' in result.top_negative_reasons
    assert not result.rank_eligible
    assert 'CRITICAL_STALE:MANAGEMENT_GOVERNANCE' in result.eligibility_reasons


# 3 -- genuinely missing/unscorable governance keeps its MISSING reason -----
def test_03_genuinely_unscorable_governance_keeps_missing_reason():
    c, r = inputs()
    area(r, 'MANAGEMENT_GOVERNANCE').status = 'UNSCORABLE'
    area(r, 'MANAGEMENT_GOVERNANCE').raw_score = None
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:MANAGEMENT_GOVERNANCE' in result.top_negative_reasons


# 4 -- adverse governance evidence (real, scored) is unaffected -------------
def test_04_adverse_governance_evidence_still_scores_and_flags_weak():
    c, r = inputs()
    area(r, 'MANAGEMENT_GOVERNANCE').raw_score = 20  # status stays READY_FRESH, real score
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:MANAGEMENT_GOVERNANCE' not in result.top_negative_reasons
    assert 'WEAK:MANAGEMENT_GOVERNANCE' in result.top_negative_reasons
    # A real score still contributes to the opportunity composition.
    assert result.score_coverage == 100


# 5 -- the same fix generically covers CURRENT_NEWS (NEWS_GEOPOLITICAL_EVENTS) --
def test_05_current_news_clean_search_stays_optional_non_blocking():
    c, r = inputs()
    area(r, 'NEWS_GEOPOLITICAL_EVENTS').raw_score = None  # status stays READY_FRESH
    result = GlobalOpportunityRanker().score(c, r)
    assert 'MISSING:NEWS_GEOPOLITICAL_EVENTS' not in result.top_negative_reasons
    assert result.rank_eligible


# 6 -- no change to rank eligibility/suppression/scoring, only the reason ---
def test_06_only_the_contradictory_reason_is_removed():
    c, r = inputs()
    area(r, 'MANAGEMENT_GOVERNANCE').raw_score = None
    result = GlobalOpportunityRanker().score(c, r)
    # Governance's weight (5) is excluded from available_weight exactly as it
    # would be for any other non-contributing area -- this was already true
    # before the fix (raw_score=None was never added to `values`), so these
    # numeric/eligibility fields are unaffected by the fix.
    assert result.score_coverage == 95
    # Governance's weight (5) drops out of both the opportunity-score
    # numerator and its denominator, exactly as it already did before this
    # fix (raw_score=None was never added to `values` either way): remaining
    # V1-area weight 85 at raw_score=80, TECHNICAL 7 at 60, SECTOR 3 at 40.
    assert result.opportunity_score == pytest.approx((85*80 + 7*60 + 3*40) / 95)
    assert result.rank_eligible
    assert result.eligibility_reasons == []
    assert 'MISSING:MANAGEMENT_GOVERNANCE' not in result.top_negative_reasons
