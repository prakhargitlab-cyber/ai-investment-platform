from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.global_opportunity_ranker import GlobalOpportunityRanker, WEIGHTS, RANKER_VERSION
from app.global_scanner import StageBCandidate
from app.sector_relative_strength import SectorRelativeStrengthSnapshot
from app.technical_features import TechnicalFeatureSnapshot
from app.stock_rule_engine import StockRuleEngineResult, AreaScoreResult, RiskOverrideResult, STOCK_RULE_ENGINE_AREA_WEIGHTS

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def inputs(n=1, core=80, tech=60, sector=40, confidence=90):
    key = UUID(int=n)
    technical = TechnicalFeatureSnapshot(global_instrument_id=key, as_of=NOW, configuration={},
        observation_count=200, history_readiness='FULL', technical_score=tech, confidence=80,
        technical_state='UPTREND')
    relative = SectorRelativeStrengthSnapshot(global_instrument_id=key, as_of=NOW, configuration={},
        relative_strength_score=sector, confidence=70,
        sector_state='NEUTRAL' if sector is not None else 'INSUFFICIENT_DATA')
    candidate = StageBCandidate(global_instrument_id=key, symbol='PROVENANCE', pre_score=99,
        technical_feature_snapshot=technical, sector_relative_strength_snapshot=relative,
        technical_score=tech, sector_score=sector, stage_b_score=99, confidence=99,
        score_coverage=100, score_weights={})
    rule = StockRuleEngineResult(global_instrument_id=key, calculated_at=NOW, input_fingerprint='fixture',
        overall_score=75, quality_score=99, opportunity_score=99, risk_score=20,
        confidence_score=confidence, confidence='HIGH', decision_signal='HOLD', partial=False,
        eligibility=dict(full_analysis_allowed=True, partial_analysis_allowed=True, reason='READY'),
        area_scores=[AreaScoreResult(area=name, weight=weight, raw_score=core, applicable=True,
            status='READY_FRESH') for name, weight in STOCK_RULE_ENGINE_AREA_WEIGHTS.items()])
    return candidate, rule


def area(rule, name):
    return next(a for a in rule.area_scores if a.area == name)


def test_full_evidence_hand_calculation_and_no_double_counting():
    c, r = inputs()
    result = GlobalOpportunityRanker().score(c, r)
    assert sum(WEIGHTS.values()) == 100
    assert dict(WEIGHTS) == {str(k):v for k,v in STOCK_RULE_ENGINE_AREA_WEIGHTS.items()
        if k not in {'PRICE_TECHNICAL', 'SECTOR_MACRO'}} | {'TECHNICAL':7, 'SECTOR':3}
    assert result.opportunity_score == 77.4  # (90*80 + 7*60 + 3*40)/100
    assert result.opportunity_confidence == 88.7  # (90*90 + 7*80 + 3*70)/100
    assert result.score_coverage == 100 and result.rank_eligible
    assert result.rule_engine_score == 75 and result.ranker_version == RANKER_VERSION
    # These aggregate scores and replaced V1 slots cannot alter the composition.
    c.pre_score = c.stage_b_score = 0
    r.quality_score = r.opportunity_score = 0
    area(r, 'PRICE_TECHNICAL').raw_score = area(r, 'SECTOR_MACRO').raw_score = 0
    assert GlobalOpportunityRanker().score(c, r) == result
    payload = result.model_dump(by_alias=True)
    assert 'opportunityScore' in payload and 'rankEligible' in payload
    assert 'decisionSignal' not in payload


def test_missing_sector_renormalization():
    c, r = inputs(sector=None)
    result = GlobalOpportunityRanker().score(c, r)
    assert result.opportunity_score == pytest.approx(7620/97)
    assert result.score_coverage == 97
    assert result.opportunity_confidence == 86.6
    assert result.rank_eligible and 'UNAVAILABLE:SECTOR' in result.top_negative_reasons


def test_zero_is_not_missing():
    c, r = inputs(core=0, tech=0, sector=0)
    zero = GlobalOpportunityRanker().score(c, r)
    assert zero.opportunity_score == 0 and zero.score_coverage == 100 and zero.rank_eligible
    area(r, 'SHAREHOLDING').raw_score = None
    missing = GlobalOpportunityRanker().score(c, r)
    assert missing.opportunity_score == 0 and missing.score_coverage == 96


def test_non_applicable_shareholding_removed_from_denominator():
    c, r = inputs()
    area(r, 'SHAREHOLDING').status = 'NOT_APPLICABLE'
    area(r, 'SHAREHOLDING').applicable = False
    area(r, 'SHAREHOLDING').raw_score = None
    result = GlobalOpportunityRanker().score(c, r)
    assert result.score_coverage == 100
    assert result.opportunity_score == pytest.approx(7420/96)
    assert result.opportunity_confidence == pytest.approx(8510/96)
    assert 'MISSING:SHAREHOLDING' not in result.top_negative_reasons


@pytest.mark.parametrize('severity', ['HIGH', 'CRITICAL'])
def test_existing_risk_override_suppresses_eligibility_not_diagnostic_score(severity):
    c, r = inputs()
    r.risk_overrides = [RiskOverrideResult(code='EXTREME_BALANCE_SHEET_STRESS', severity=severity)]
    result = GlobalOpportunityRanker().score(c, r)
    assert not result.rank_eligible and result.opportunity_score == 77.4
    assert result.top_negative_reasons[0] == 'V1_RISK_OVERRIDE:EXTREME_BALANCE_SHEET_STRESS'


@pytest.mark.parametrize('status', ['READY_STALE', 'CONFLICTING'])
def test_critical_evidence_gate(status):
    c, r = inputs()
    area(r, 'BALANCE_SHEET').status = status
    result = GlobalOpportunityRanker().score(c, r)
    assert not result.rank_eligible and result.score_coverage == 91
    assert any(code.startswith('CRITICAL_') for code in result.eligibility_reasons)


@pytest.mark.parametrize('kind', ['stale', 'conflict'])
def test_current_price_gate(kind):
    c, r = inputs()
    if kind == 'stale': c.technical_feature_snapshot.stale_inputs = ['PRICE_HISTORY']
    else: c.technical_feature_snapshot.feature_states = {'latestPrice':'CONFLICTING'}
    result = GlobalOpportunityRanker().score(c, r)
    assert not result.rank_eligible and result.score_coverage == 90


def test_optional_stale_sector_is_excluded_not_zero_or_gate():
    c, r = inputs()
    c.sector_relative_strength_snapshot.sector_state = 'INSUFFICIENT_DATA'
    c.sector_relative_strength_snapshot.stale_inputs = ['SECTOR_HISTORY']
    result = GlobalOpportunityRanker().score(c, r)
    assert result.rank_eligible and result.score_coverage == 97
    assert result.opportunity_score == pytest.approx(7620/97)


def test_partial_and_missing_v1_fail_closed():
    c, r = inputs()
    r.partial = True
    assert not GlobalOpportunityRanker().score(c, r).rank_eligible
    missing = GlobalOpportunityRanker().score(c, None)
    assert not missing.rank_eligible and missing.rule_engine_score is None
    assert missing.eligibility_reasons == ['V1_RESULT_MISSING']


def test_deterministic_reasons_and_repeat_without_mutation_or_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    c, r = inputs()
    before = c.model_dump(), r.model_dump()
    ranker = GlobalOpportunityRanker()
    first = ranker.score(c, r)
    assert first == ranker.score(c, r)
    assert before == (c.model_dump(), r.model_dump())
    r.area_scores.reverse()
    assert first == ranker.score(c, r)
    assert first.top_positive_reasons == ['SUPPORT:BALANCE_SHEET', 'SUPPORT:FUNDAMENTAL_BUSINESS_QUALITY',
        'SUPPORT:GROWTH', 'SUPPORT:MANAGEMENT_GOVERNANCE', 'SUPPORT:NEWS_GEOPOLITICAL_EVENTS',
        'SUPPORT:ORDER_BOOK_CAPACITY_CATALYSTS']
    assert first.top_negative_reasons == ['WEAK:SECTOR']


def test_sort_score_confidence_coverage_rule_score_and_uuid():
    pairs = [inputs(n, core=50, tech=50, sector=50, confidence=100) for n in range(1,7)]
    # Equal opportunity 50 throughout except candidate 6 at 80.
    pairs[0][1].overall_score = 80
    pairs[1][1].overall_score = 80  # UUID 1 beats UUID 2.
    pairs[2][1].overall_score = 70
    # Same confidence as full rows (88.7), but coverage 97 vs 100.
    c, r = pairs[3]
    c.sector_score = c.sector_relative_strength_snapshot.relative_strength_score = None
    c.sector_relative_strength_snapshot.sector_state = 'INSUFFICIENT_DATA'
    c.technical_feature_snapshot.confidence = 100
    for i in range(3): pairs[i][1].confidence_score = 90
    r.confidence_score = (8870-700)/90
    pairs[4][1].confidence_score = 20
    c, r = inputs(6, core=80, tech=80, sector=80, confidence=0)
    pairs[5] = c, r
    ranker = GlobalOpportunityRanker()
    rules = {r.global_instrument_id:r for _,r in pairs}
    rows = ranker.rank([c for c,_ in reversed(pairs)], rules)
    assert [r.global_instrument_id.int for r in rows] == [6,1,2,3,4,5]
    assert rows == ranker.rank([c for c,_ in pairs], rules)


def test_identity_duplicates_and_versions_rejected():
    c, r = inputs()
    ranker = GlobalOpportunityRanker()
    with pytest.raises(ValueError, match='DUPLICATE_RANKER_INSTRUMENT'):
        ranker.rank([c,c], {r.global_instrument_id:r})
    r.global_instrument_id = UUID(int=2)
    with pytest.raises(ValueError, match='IDENTITY_MISMATCH'): ranker.score(c,r)
    r.global_instrument_id = c.global_instrument_id
    r.rule_engine_version = 'FUTURE'
    with pytest.raises(ValueError, match='UNSUPPORTED_RULE_ENGINE_VERSION'): ranker.score(c,r)
    assert ranker.rank([], {}) == []
