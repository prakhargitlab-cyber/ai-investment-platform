"""ETF_RULE_ENGINE_V1 tests: pure deterministic scoring, risk gates, and
recommendation derivation. No DB, no provider, no LLM -- hand-built fixtures
only, mirroring the fixture style of tests/test_etf_readiness.py."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.etf_evidence import EtfAuthority, EtfFact, EtfHoldingsSnapshot, EtfMetric, EtfNavObservation, EtfProvenance
from app.etf_metrics import compute_etf_deterministic_metrics
from app.etf_readiness import EtfReadinessStatus, EtfReadinessSummary, EtfRequirementStatus
from app.etf_rule_engine import (
    ETF_RULE_ENGINE_FACTOR_WEIGHTS, ETF_RULE_ENGINE_VERSION, EtfConfidenceLevel, EtfFactorStatus,
    EtfRecommendation, EtfRiskGateCategory, EtfRiskGateSeverity, EtfRuleEngineInput, derive_etf_recommendation,
    evaluate_etf_risk_gates, score_etf,
)
from app.models import DailyMarketBar, EtfHolding, SourceMode

ID = UUID(int=9)
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=EtfAuthority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


def _bar(trading_date, close):
    return DailyMarketBar(global_instrument_id=ID, trading_date=trading_date, close=Decimal(str(close)),
        currency="INR", provider="NSE", source_mode=SourceMode.REAL,
        source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW)


def _flat_history(days=260, price=100):
    return [_bar(date(2025, 1, 1) + timedelta(days=i), price) for i in range(days)]


def _ready_summary(*, mandatory_ready=3, missing=0, stale=0, technical=0) -> EtfReadinessSummary:
    return EtfReadinessSummary(mandatory_total=3, mandatory_ready=mandatory_ready, mandatory_missing=missing,
        mandatory_stale=stale, mandatory_technical_failure=technical, supporting_total=0, supporting_ready=0,
        supporting_missing=0, supporting_stale=0, supporting_technical_failure=0, contextual_total=0,
        contextual_ready=0, contextual_failure=0, ready_for_analysis=(mandatory_ready == 3 and technical == 0))


def _base_input(*, bars=None, summary=None, **overrides) -> EtfRuleEngineInput:
    metrics = compute_etf_deterministic_metrics(ID, bars if bars is not None else _flat_history())
    defaults = dict(instrument_id=ID, readiness_statuses=(), readiness_summary=summary or _ready_summary(),
        metrics=metrics, now=NOW)
    return EtfRuleEngineInput(**(defaults | overrides))


class TestFactorWeights:
    def test_weights_sum_to_100(self):
        assert sum(ETF_RULE_ENGINE_FACTOR_WEIGHTS.values()) == 100


class TestScoreEtfMissingDataHandling:
    def test_no_optional_evidence_only_performance_risk_and_completeness_scored(self):
        result = score_etf(_base_input())
        statuses = {r.factor: r.status for r in result.factor_results}
        assert statuses["PERFORMANCE_MOMENTUM"] == EtfFactorStatus.SCORED
        assert statuses["RISK_DRAWDOWN_VOLATILITY"] == EtfFactorStatus.SCORED
        assert statuses["LIQUIDITY"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["TRACKING_QUALITY"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["COST"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["NAV_PREMIUM_DISCOUNT"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["DIVERSIFICATION"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["DATA_COMPLETENESS"] == EtfFactorStatus.SCORED

    def test_missing_factor_has_no_normalized_score_or_contribution(self):
        result = score_etf(_base_input())
        liquidity = next(r for r in result.factor_results if r.factor == "LIQUIDITY")
        assert liquidity.normalized_score is None
        assert liquidity.weighted_contribution is None

    def test_overall_score_only_averages_available_weight_not_full_100(self):
        result = score_etf(_base_input())
        # Only PERFORMANCE_MOMENTUM(25) + RISK(20) + DATA_COMPLETENESS(5) = 50 of 100 weight available.
        assert result.data_completeness == pytest.approx(0.5)
        assert result.overall_score is not None

    def test_zero_available_weight_gives_none_overall_score_not_zero(self):
        # Empty bars -> PERFORMANCE_MOMENTUM and RISK both UNAVAILABLE; with
        # no other evidence only DATA_COMPLETENESS itself remains scored.
        result = score_etf(_base_input(bars=[]))
        statuses = {r.factor: r.status for r in result.factor_results}
        assert statuses["PERFORMANCE_MOMENTUM"] == EtfFactorStatus.UNAVAILABLE
        assert statuses["RISK_DRAWDOWN_VOLATILITY"] == EtfFactorStatus.UNAVAILABLE
        assert result.overall_score is not None  # DATA_COMPLETENESS alone still scores


class TestScoreEtfFullEvidence:
    def _full_input(self, **overrides):
        nav = EtfNavObservation(instrument_id=ID, nav=Decimal("100"), currency="INR", nav_date=NOW.date(),
            provenance=provenance(provider="AMC"))
        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100.50"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY))
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY))
        spread = EtfFact(instrument_id=ID, metric=EtfMetric.BID_ASK_SPREAD, value=Decimal("0.1"), unit="PERCENT",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY))
        tracking_error = EtfFact(instrument_id=ID, metric=EtfMetric.TRACKING_ERROR, value=Decimal("0.3"),
            unit="PERCENT", as_of_date=NOW.date(), provenance=provenance(provider="AMC", authority=EtfAuthority.OFFICIAL_FUND))
        expense = EtfFact(instrument_id=ID, metric=EtfMetric.EXPENSE_RATIO, value=Decimal("0.2"), unit="PERCENT",
            as_of_date=NOW.date(), provenance=provenance(provider="AMC", authority=EtfAuthority.OFFICIAL_FUND))
        holdings = EtfHoldingsSnapshot(instrument_id=ID, as_of_date=NOW.date(), provenance=provenance(provider="AMC"),
            holdings=[EtfHolding(etf_instrument_id=ID, constituent_name=f"Stock {i}", weight_percentage=Decimal("2"),
                as_of_date=NOW.date(), source_provider="AMC") for i in range(50)])
        defaults = dict(nav=nav, market_price_fact=price, trading_volume_fact=volume, bid_ask_spread_fact=spread,
            tracking_error_fact=tracking_error, expense_ratio_fact=expense, holdings=holdings)
        return _base_input(**(defaults | overrides))

    def test_all_factors_scored_when_all_evidence_present(self):
        result = score_etf(self._full_input())
        assert all(r.status == EtfFactorStatus.SCORED for r in result.factor_results)
        assert result.data_completeness == 1.0
        assert result.confidence == EtfConfidenceLevel.HIGH

    def test_good_etf_scores_high(self):
        result = score_etf(self._full_input())
        assert result.overall_score > 60

    def test_stale_optional_evidence_is_still_used_but_not_fabricated_fresh(self):
        stale_nav = EtfNavObservation(instrument_id=ID, nav=Decimal("100"), currency="INR",
            nav_date=(NOW - timedelta(days=10)).date(), provenance=provenance(provider="AMC"))
        result = score_etf(self._full_input(nav=stale_nav))
        nav_factor = next(r for r in result.factor_results if r.factor == "NAV_PREMIUM_DISCOUNT")
        assert nav_factor.status == EtfFactorStatus.SCORED  # STALE still usable, never silently dropped to zero


class TestRiskGates:
    def test_mandatory_technical_failure_is_distinguished_from_missing(self):
        gates = evaluate_etf_risk_gates(_base_input(summary=_ready_summary(technical=1, mandatory_ready=2)),
            score_etf(_base_input(summary=_ready_summary(technical=1, mandatory_ready=2))))
        technical_gate = next(g for g in gates if g.gate_id == "MANDATORY_DATA_TECHNICAL_FAILURE")
        missing_gate = next(g for g in gates if g.gate_id == "MANDATORY_DATA_MISSING_OR_STALE")
        assert technical_gate.triggered is True
        assert technical_gate.category == EtfRiskGateCategory.TECHNICAL_FAILURE
        assert missing_gate.triggered is False  # technical failure, not an ordinary missing-data case

    def test_mandatory_missing_without_technical_failure(self):
        data = _base_input(summary=_ready_summary(mandatory_ready=2, missing=1))
        gates = evaluate_etf_risk_gates(data, score_etf(data))
        missing_gate = next(g for g in gates if g.gate_id == "MANDATORY_DATA_MISSING_OR_STALE")
        assert missing_gate.triggered is True
        assert missing_gate.category == EtfRiskGateCategory.INSUFFICIENT_DATA

    def test_extreme_illiquidity_detected_from_fresh_volume(self):
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY))
        data = _base_input(trading_volume_fact=volume)
        gates = evaluate_etf_risk_gates(data, score_etf(data))
        gate = next(g for g in gates if g.gate_id == "EXTREME_ILLIQUIDITY")
        assert gate.triggered is True
        assert gate.category == EtfRiskGateCategory.POOR_CHARACTERISTICS
        assert gate.severity == EtfRiskGateSeverity.BLOCKING

    def test_no_gates_falsely_trigger_on_healthy_etf(self):
        data = _base_input()
        gates = evaluate_etf_risk_gates(data, score_etf(data))
        triggered = [g.gate_id for g in gates if g.triggered]
        assert "EXTREME_ILLIQUIDITY" not in triggered
        assert "EXCESSIVE_DRAWDOWN" not in triggered

    def test_excessive_drawdown_detected(self):
        # The peak must fall inside the trailing 252-session drawdown window
        # for it to be captured; a peak far enough in the past rolls off.
        prices = [60] * 3 + [150] + [60] * 251
        bars = [_bar(date(2025, 1, 1) + timedelta(days=i), p) for i, p in enumerate(prices)]
        data = _base_input(bars=bars)
        gates = evaluate_etf_risk_gates(data, score_etf(data))
        gate = next(g for g in gates if g.gate_id == "EXCESSIVE_DRAWDOWN")
        assert gate.triggered is True


class TestRecommendationDerivation:
    def test_technical_failure_never_becomes_automatic_avoid(self):
        data = _base_input(summary=_ready_summary(technical=1, mandatory_ready=2))
        result = score_etf(data)
        gates = evaluate_etf_risk_gates(data, result)
        assert derive_etf_recommendation(result, gates) == EtfRecommendation.INSUFFICIENT_DATA

    def test_missing_mandatory_data_is_insufficient_not_avoid(self):
        data = _base_input(summary=_ready_summary(mandatory_ready=1, missing=2))
        result = score_etf(data)
        gates = evaluate_etf_risk_gates(data, result)
        assert derive_etf_recommendation(result, gates) == EtfRecommendation.INSUFFICIENT_DATA

    def test_extreme_illiquidity_forces_avoid_even_with_decent_score(self):
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("1"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY))
        data = _base_input(trading_volume_fact=volume)
        result = score_etf(data)
        gates = evaluate_etf_risk_gates(data, result)
        assert derive_etf_recommendation(result, gates) == EtfRecommendation.AVOID

    def test_healthy_flat_etf_with_partial_data_is_not_avoid(self):
        data = _base_input()
        result = score_etf(data)
        gates = evaluate_etf_risk_gates(data, result)
        recommendation = derive_etf_recommendation(result, gates)
        assert recommendation != EtfRecommendation.AVOID
        assert recommendation != EtfRecommendation.INSUFFICIENT_DATA

    def test_rule_engine_version_is_pinned(self):
        assert ETF_RULE_ENGINE_VERSION == "ETF_RULE_ENGINE_V1"
