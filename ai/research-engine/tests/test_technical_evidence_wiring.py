"""Task A: wire the already-computed TechnicalFeatureSnapshot into
evidence_state['technical'] so RecommendationEngineV1.ranges() -- which
already reads technical.get('support_level')/('resistance_level')/('atr14')
-- can actually see it. Before this wiring, _evidence_state() only forwarded
latest_price/history_end/feature_version, so short-term ranges were always
None/insufficient regardless of how much real technical evidence existed.

No recomputation happens in this layer: every value is forwarded verbatim
from the canonical TechnicalFeatureSnapshot the scanner already builds.
"""
from decimal import Decimal
from uuid import UUID

import pytest

from app.recommendation_engine import ranges
from test_global_opportunity_orchestration import setup, NOW
from test_global_opportunity_ranker import inputs


def test_A_support_level_reaches_evidence_state(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    enriched.technical_feature_snapshot.support_level = 90.0
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["support_level"] == 90.0


def test_B_resistance_level_reaches_evidence_state(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    enriched.technical_feature_snapshot.resistance_level = 110.0
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["resistance_level"] == 110.0


def test_C_atr14_reaches_evidence_state(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    enriched.technical_feature_snapshot.atr14 = 2.5
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["atr14"] == 2.5


def test_D_valid_support_resistance_atr_produces_real_short_term_ranges(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    snapshot = enriched.technical_feature_snapshot
    snapshot.support_level, snapshot.resistance_level, snapshot.atr14 = 90.0, 110.0, 2.0
    evidence = service._evidence_state(enriched, rule, None)
    result = ranges({"evidence_state": evidence, "current_price": 95})
    assert result["short_entry_low"] == 90.0
    assert result["short_entry_high"] == pytest.approx(92.7)  # min(95, 90*1.03)
    assert result["short_target_1"] == 110.0
    assert result["short_target_2"] == 112.0  # resistance + atr
    assert result["short_invalidation"] == 88.0  # support - atr


def test_E_missing_support_resistance_stays_insufficient(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    assert enriched.technical_feature_snapshot.support_level is None
    assert enriched.technical_feature_snapshot.resistance_level is None
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["support_level"] is None
    assert evidence["technical"]["resistance_level"] is None
    result = ranges({"evidence_state": evidence, "current_price": 95})
    assert result["short_entry_low"] is None
    assert result["short_target_1"] is None


def test_F_unrelated_technical_fields_do_not_alter_scoring(monkeypatch):
    from app.global_opportunity_ranker import GlobalOpportunityRanker
    enriched, rule = inputs(1)
    before = GlobalOpportunityRanker().score(enriched, rule)
    snapshot = enriched.technical_feature_snapshot
    # Populate every newly-forwarded field with a real value; none of these
    # feed GlobalOpportunityRanker.score(), which only reads technical_score/
    # sector_score/rule area scores -- the raw indicator values are new
    # *evidence*, not new *ranking inputs*.
    snapshot.support_level, snapshot.resistance_level, snapshot.atr14 = 90.0, 110.0, 2.0
    snapshot.rsi14, snapshot.macd, snapshot.macd_signal, snapshot.macd_histogram = 65.0, 1.2, 0.9, 0.3
    snapshot.adx14, snapshot.technical_state, snapshot.breakout_state = 30.0, "BREAKOUT", "VOLUME_CONFIRMED"
    snapshot.dma20 = snapshot.dma50 = snapshot.dma100 = snapshot.dma200 = 95.0
    snapshot.volume_ratio20, snapshot.volume_state = 2.0, "EXPANSION"
    after = GlobalOpportunityRanker().score(enriched, rule)
    assert after.opportunity_score == before.opportunity_score
    assert after.opportunity_confidence == before.opportunity_confidence
    assert after.score_coverage == before.score_coverage
    assert after.rank_eligible == before.rank_eligible


@pytest.mark.asyncio
async def test_G_short_term_ranges_deterministic_across_repeated_runs(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch, 1)
    snapshot = pairs[UUID(int=1)][0].technical_feature_snapshot
    snapshot.support_level, snapshot.resistance_level, snapshot.atr14 = 90.0, 110.0, 2.0
    first = await service.run(rows, as_of=NOW)
    second = await service.run(rows, as_of=NOW)
    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(exclude={"generated_at"})
    technical = first.top_n[0].evidence_state["technical"]
    assert technical["support_level"] == 90.0 and technical["resistance_level"] == 110.0 and technical["atr14"] == 2.0


def test_H_wiring_is_a_pure_forward_no_hidden_clock_or_recomputation(monkeypatch):
    """This layer must not introduce look-ahead of its own: it is a pure
    function of the snapshot object, independent of wall-clock time or any
    other input. (Point-in-time correctness of the snapshot's own values is
    TechnicalFeatureEngine's responsibility, tested in test_ohlcv_technical_features.py.)
    """
    service, *_ = setup(monkeypatch, 1)
    enriched, _ = inputs(1)
    enriched.technical_feature_snapshot.support_level = 90.0
    enriched.technical_feature_snapshot.resistance_level = 110.0
    first = service._technical_evidence(enriched.technical_feature_snapshot)
    service.clock = lambda: NOW.replace(year=2099)  # simulate time moving far forward
    second = service._technical_evidence(enriched.technical_feature_snapshot)
    assert first == second


def test_current_volume_decimal_normalized_to_float(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    enriched.technical_feature_snapshot.current_volume = Decimal("123456")
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["current_volume"] == 123456.0
    assert isinstance(evidence["technical"]["current_volume"], float)


def test_missing_stale_metadata_preserved(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    snapshot = enriched.technical_feature_snapshot
    snapshot.missing_inputs = ["ATR14", "ADX14"]
    snapshot.stale_inputs = ["PRICE_HISTORY"]
    snapshot.history_readiness = "SHORT_HISTORY"
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["technical"]["missing_inputs"] == ["ATR14", "ADX14"]
    assert evidence["technical"]["stale_inputs"] == ["PRICE_HISTORY"]
    assert evidence["technical"]["history_readiness"] == "SHORT_HISTORY"
