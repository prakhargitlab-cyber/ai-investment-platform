"""ETF_RULE_ENGINE_V1: a new, dedicated, deterministic ETF scoring model.

This is NOT a reuse of ``app.stock_rule_engine.StockRuleEngineV1`` -- an ETF
is not a company, and nothing here treats it as one (no profitability,
order book, governance, or shareholding inputs). It follows the same
conventions where they genuinely transfer (versioned result, weighted
factor areas summing to 100, explicit missing-data handling, no LLM in the
authoritative scoring path), implemented independently.

Factor weights are fixed and sum to exactly 100; this is asserted at import
time so a bad edit fails immediately rather than silently skewing scores.
Any factor whose inputs are unavailable is marked UNAVAILABLE and excluded
from both the numerator and the denominator of the weighted average -- it
is never coerced to a zero or a neutral midpoint score. ``data_completeness``
reports what fraction of factor weight was actually available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from app.etf_evidence import EtfAuthority, EtfFact, EtfHoldingsSnapshot, EtfMetric, EtfNavObservation, etf_freshness
from app.etf_metrics import EtfDeterministicMetrics, EtfMetricId
from app.etf_readiness import EtfReadinessStatus, EtfReadinessSummary, EtfRequirementStatus

ETF_RULE_ENGINE_VERSION = "ETF_RULE_ENGINE_V1"


class EtfRuleEngineFactor(StrEnum):
    PERFORMANCE_MOMENTUM = "PERFORMANCE_MOMENTUM"
    RISK_DRAWDOWN_VOLATILITY = "RISK_DRAWDOWN_VOLATILITY"
    LIQUIDITY = "LIQUIDITY"
    TRACKING_QUALITY = "TRACKING_QUALITY"
    COST = "COST"
    NAV_PREMIUM_DISCOUNT = "NAV_PREMIUM_DISCOUNT"
    DIVERSIFICATION = "DIVERSIFICATION"
    DATA_COMPLETENESS = "DATA_COMPLETENESS"


ETF_RULE_ENGINE_FACTOR_WEIGHTS: Mapping[EtfRuleEngineFactor, int] = {
    EtfRuleEngineFactor.PERFORMANCE_MOMENTUM: 25,
    EtfRuleEngineFactor.RISK_DRAWDOWN_VOLATILITY: 20,
    EtfRuleEngineFactor.LIQUIDITY: 15,
    EtfRuleEngineFactor.TRACKING_QUALITY: 15,
    EtfRuleEngineFactor.COST: 10,
    EtfRuleEngineFactor.NAV_PREMIUM_DISCOUNT: 5,
    EtfRuleEngineFactor.DIVERSIFICATION: 5,
    EtfRuleEngineFactor.DATA_COMPLETENESS: 5,
}
if sum(ETF_RULE_ENGINE_FACTOR_WEIGHTS.values()) != 100:  # pragma: no cover - import guard
    raise AssertionError("ETF_RULE_ENGINE_V1 factor weights must sum to 100")


class EtfFactorStatus(StrEnum):
    SCORED = "SCORED"
    UNAVAILABLE = "UNAVAILABLE"


class EtfRiskGateCategory(StrEnum):
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    POOR_CHARACTERISTICS = "POOR_CHARACTERISTICS"


class EtfRiskGateSeverity(StrEnum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


class EtfRecommendation(StrEnum):
    STRONG_OPPORTUNITY = "STRONG_OPPORTUNITY"
    OPPORTUNITY = "OPPORTUNITY"
    WATCH = "WATCH"
    AVOID = "AVOID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class EtfConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True)
class EtfFactorScoreResult:
    factor: EtfRuleEngineFactor
    weight: int
    status: EtfFactorStatus
    raw_metric: str | None
    normalized_score: float | None  # 0-100, higher is better; None if UNAVAILABLE
    weighted_contribution: float | None
    freshness: str | None
    source_provenance: str | None
    confidence: float | None
    reason: str | None = None


@dataclass(frozen=True)
class EtfRiskGate:
    gate_id: str
    category: EtfRiskGateCategory
    severity: EtfRiskGateSeverity
    triggered: bool
    reason: str


@dataclass(frozen=True)
class EtfRuleEngineResult:
    rule_engine_version: str
    instrument_id: object
    as_of: datetime
    factor_results: tuple[EtfFactorScoreResult, ...]
    overall_score: float | None  # None only when zero factor weight was available
    data_completeness: float  # 0.0-1.0, fraction of total factor weight that was SCORED
    confidence: EtfConfidenceLevel


@dataclass(frozen=True)
class EtfRuleEngineInput:
    """Everything ETF_RULE_ENGINE_V1 needs, already fetched by the caller.

    This dataclass carries no persistence or acquisition dependency of its
    own, so it is trivially unit-testable with hand-built fixtures.
    """
    instrument_id: object
    readiness_statuses: Sequence[EtfRequirementStatus]
    readiness_summary: EtfReadinessSummary
    metrics: EtfDeterministicMetrics
    nav: EtfNavObservation | None = None
    market_price_fact: EtfFact | None = None
    trading_volume_fact: EtfFact | None = None
    bid_ask_spread_fact: EtfFact | None = None
    tracking_error_fact: EtfFact | None = None
    tracking_difference_fact: EtfFact | None = None
    expense_ratio_fact: EtfFact | None = None
    aum_fact: EtfFact | None = None
    holdings: EtfHoldingsSnapshot | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _requirement(statuses: Sequence[EtfRequirementStatus], requirement: str) -> EtfRequirementStatus | None:
    return next((status for status in statuses if status.requirement == requirement), None)


def _piecewise(value: float, points: Sequence[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation; ``points`` sorted ascending by input."""
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return points[-1][1]  # pragma: no cover - unreachable given the bounds checks above


def _lower_better(value: float, points: Sequence[tuple[float, float]]) -> float:
    return _piecewise(value, points)


def _score_performance_momentum(metrics: EtfDeterministicMetrics) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.PERFORMANCE_MOMENTUM]
    available = [metrics.get(m) for m in (EtfMetricId.RETURN_1M, EtfMetricId.RETURN_3M,
        EtfMetricId.RETURN_6M, EtfMetricId.RETURN_1Y) if metrics.get(m) and not metrics.get(m).insufficient_history]
    momentum = metrics.get(EtfMetricId.MOMENTUM_50_200)
    if not available and (momentum is None or momentum.insufficient_history):
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.PERFORMANCE_MOMENTUM, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance="DAILY_MARKET_BARS", confidence=None, reason="INSUFFICIENT_PRICE_HISTORY")
    return_points = [(-50.0, 0.0), (0.0, 50.0), (25.0, 100.0)]
    momentum_points = [(-20.0, 0.0), (0.0, 50.0), (20.0, 100.0)]
    sub_scores = [_piecewise(float(metric.value), return_points) for metric in available]
    if momentum is not None and not momentum.insufficient_history:
        sub_scores.append(_piecewise(float(momentum.value), momentum_points))
    score = sum(sub_scores) / len(sub_scores)
    freshness = metrics.as_of_date.isoformat() if metrics.as_of_date else None
    raw = ",".join(f"{m.metric}={m.value}" for m in available) + (f",MOMENTUM={momentum.value}" if momentum and not momentum.insufficient_history else "")
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.PERFORMANCE_MOMENTUM, weight=weight,
        status=EtfFactorStatus.SCORED, raw_metric=raw, normalized_score=score,
        weighted_contribution=score * weight / 100, freshness=freshness, source_provenance="DAILY_MARKET_BARS",
        confidence=min(1.0, len(sub_scores) / 5))


def _score_risk(metrics: EtfDeterministicMetrics) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.RISK_DRAWDOWN_VOLATILITY]
    drawdown = metrics.get(EtfMetricId.MAX_DRAWDOWN)
    volatility = metrics.get(EtfMetricId.ANNUALIZED_VOLATILITY)
    components = []
    if drawdown is not None and not drawdown.insufficient_history:
        # max_drawdown is <= 0; a deeper (more negative) drawdown scores lower.
        components.append(_lower_better(float(drawdown.value), [(-60.0, 0.0), (-10.0, 70.0), (0.0, 100.0)]))
    if volatility is not None and not volatility.insufficient_history:
        components.append(_lower_better(float(volatility.value), [(0.0, 100.0), (25.0, 60.0), (60.0, 0.0)]))
    if not components:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.RISK_DRAWDOWN_VOLATILITY, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance="DAILY_MARKET_BARS", confidence=None, reason="INSUFFICIENT_PRICE_HISTORY")
    score = sum(components) / len(components)
    freshness = metrics.as_of_date.isoformat() if metrics.as_of_date else None
    raw = f"DRAWDOWN={getattr(drawdown, 'value', None)},VOLATILITY={getattr(volatility, 'value', None)}"
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.RISK_DRAWDOWN_VOLATILITY, weight=weight,
        status=EtfFactorStatus.SCORED, raw_metric=raw, normalized_score=score,
        weighted_contribution=score * weight / 100, freshness=freshness, source_provenance="DAILY_MARKET_BARS",
        confidence=len(components) / 2)


def _fresh_fact(fact: EtfFact | None, metric: EtfMetric, *, now: datetime) -> EtfFact | None:
    if fact is None:
        return None
    if etf_freshness(fact, metric, now=now) in {"FRESH", "STALE"}:
        return fact
    return None


def _score_liquidity(data: EtfRuleEngineInput) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.LIQUIDITY]
    volume_fact = _fresh_fact(data.trading_volume_fact, EtfMetric.TRADING_VOLUME, now=data.now)
    spread_fact = _fresh_fact(data.bid_ask_spread_fact, EtfMetric.BID_ASK_SPREAD, now=data.now)
    if volume_fact is None and spread_fact is None:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.LIQUIDITY, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance=None, confidence=None, reason="NO_FRESH_LIQUIDITY_EVIDENCE")
    components = []
    if volume_fact is not None:
        components.append(_piecewise(float(volume_fact.value), [(0.0, 0.0), (10000.0, 60.0), (500000.0, 100.0)]))
    if spread_fact is not None:
        components.append(_lower_better(float(spread_fact.value), [(0.0, 100.0), (1.0, 60.0), (5.0, 0.0)]))
    score = sum(components) / len(components)
    primary = volume_fact or spread_fact
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.LIQUIDITY, weight=weight, status=EtfFactorStatus.SCORED,
        raw_metric=f"VOLUME={getattr(volume_fact, 'value', None)},SPREAD={getattr(spread_fact, 'value', None)}",
        normalized_score=score, weighted_contribution=score * weight / 100,
        freshness=primary.as_of_date.isoformat() if primary.as_of_date else None,
        source_provenance=primary.provenance.provider, confidence=len(components) / 2)


def _score_tracking_quality(data: EtfRuleEngineInput) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.TRACKING_QUALITY]
    error_fact = _fresh_fact(data.tracking_error_fact, EtfMetric.TRACKING_ERROR, now=data.now)
    difference_fact = _fresh_fact(data.tracking_difference_fact, EtfMetric.TRACKING_DIFFERENCE, now=data.now)
    if error_fact is None and difference_fact is None:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.TRACKING_QUALITY, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance=None, confidence=None, reason="NO_FRESH_TRACKING_EVIDENCE")
    components = []
    if error_fact is not None:
        components.append(_lower_better(float(error_fact.value), [(0.0, 100.0), (1.0, 60.0), (3.0, 0.0)]))
    if difference_fact is not None:
        components.append(_lower_better(abs(float(difference_fact.value)), [(0.0, 100.0), (1.0, 60.0), (3.0, 0.0)]))
    score = sum(components) / len(components)
    primary = error_fact or difference_fact
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.TRACKING_QUALITY, weight=weight,
        status=EtfFactorStatus.SCORED,
        raw_metric=f"TRACKING_ERROR={getattr(error_fact, 'value', None)},TRACKING_DIFFERENCE={getattr(difference_fact, 'value', None)}",
        normalized_score=score, weighted_contribution=score * weight / 100,
        freshness=primary.as_of_date.isoformat() if primary.as_of_date else None,
        source_provenance=primary.provenance.provider, confidence=len(components) / 2)


def _score_cost(data: EtfRuleEngineInput) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.COST]
    fact = _fresh_fact(data.expense_ratio_fact, EtfMetric.EXPENSE_RATIO, now=data.now)
    if fact is None:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.COST, weight=weight, status=EtfFactorStatus.UNAVAILABLE,
            raw_metric=None, normalized_score=None, weighted_contribution=None, freshness=None,
            source_provenance=None, confidence=None, reason="NO_FRESH_EXPENSE_RATIO_EVIDENCE")
    score = _lower_better(float(fact.value), [(0.0, 100.0), (0.5, 70.0), (1.5, 20.0), (3.0, 0.0)])
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.COST, weight=weight, status=EtfFactorStatus.SCORED,
        raw_metric=f"EXPENSE_RATIO={fact.value}", normalized_score=score, weighted_contribution=score * weight / 100,
        freshness=fact.as_of_date.isoformat() if fact.as_of_date else None, source_provenance=fact.provenance.provider,
        confidence=1.0)


def _score_nav_premium_discount(data: EtfRuleEngineInput) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.NAV_PREMIUM_DISCOUNT]
    nav = data.nav if data.nav is not None and etf_freshness(data.nav, EtfMetric.NAV, now=data.now) in {"FRESH", "STALE"} else None
    price = _fresh_fact(data.market_price_fact, EtfMetric.MARKET_PRICE, now=data.now)
    if nav is None or price is None or nav.nav == 0:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.NAV_PREMIUM_DISCOUNT, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance=None, confidence=None, reason="NAV_OR_MARKET_PRICE_UNAVAILABLE")
    premium_pct = float((price.value - nav.nav) / nav.nav * 100)
    score = _lower_better(abs(premium_pct), [(0.0, 100.0), (1.0, 70.0), (5.0, 0.0)])
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.NAV_PREMIUM_DISCOUNT, weight=weight,
        status=EtfFactorStatus.SCORED, raw_metric=f"PREMIUM_DISCOUNT_PCT={premium_pct}", normalized_score=score,
        weighted_contribution=score * weight / 100, freshness=price.as_of_date.isoformat() if price.as_of_date else None,
        source_provenance=price.provenance.provider, confidence=1.0)


def _score_diversification(data: EtfRuleEngineInput) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.DIVERSIFICATION]
    snapshot = data.holdings
    if snapshot is None or not snapshot.holdings:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.DIVERSIFICATION, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance=None, confidence=None, reason="NO_HOLDINGS_SNAPSHOT")
    weighted = [h for h in snapshot.holdings if h.weight_percentage is not None]
    if not weighted:
        return EtfFactorScoreResult(factor=EtfRuleEngineFactor.DIVERSIFICATION, weight=weight,
            status=EtfFactorStatus.UNAVAILABLE, raw_metric=None, normalized_score=None, weighted_contribution=None,
            freshness=None, source_provenance=None, confidence=None, reason="HOLDINGS_LACK_WEIGHTS")
    top10 = sum(sorted((float(h.weight_percentage) for h in weighted), reverse=True)[:10])
    score = _lower_better(top10, [(0.0, 100.0), (40.0, 70.0), (70.0, 30.0), (100.0, 0.0)])
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.DIVERSIFICATION, weight=weight,
        status=EtfFactorStatus.SCORED, raw_metric=f"TOP10_CONCENTRATION_PCT={top10}", normalized_score=score,
        weighted_contribution=score * weight / 100, freshness=snapshot.as_of_date.isoformat(),
        source_provenance=snapshot.provenance.provider, confidence=1.0)


def _score_data_completeness(scored_so_far: Sequence[EtfFactorScoreResult]) -> EtfFactorScoreResult:
    weight = ETF_RULE_ENGINE_FACTOR_WEIGHTS[EtfRuleEngineFactor.DATA_COMPLETENESS]
    other_weight = sum(w for factor, w in ETF_RULE_ENGINE_FACTOR_WEIGHTS.items()
                        if factor != EtfRuleEngineFactor.DATA_COMPLETENESS)
    available_weight = sum(r.weight for r in scored_so_far if r.status == EtfFactorStatus.SCORED)
    coverage_pct = 100.0 * available_weight / other_weight if other_weight else 0.0
    return EtfFactorScoreResult(factor=EtfRuleEngineFactor.DATA_COMPLETENESS, weight=weight,
        status=EtfFactorStatus.SCORED, raw_metric=f"OTHER_FACTOR_WEIGHT_COVERAGE_PCT={coverage_pct}",
        normalized_score=coverage_pct, weighted_contribution=coverage_pct * weight / 100, freshness=None,
        source_provenance="ETF_RULE_ENGINE_V1_SELF_REPORT", confidence=1.0)


def score_etf(data: EtfRuleEngineInput) -> EtfRuleEngineResult:
    """Pure, deterministic ETF scoring. No I/O, no LLM, no randomness."""
    factor_results = [
        _score_performance_momentum(data.metrics),
        _score_risk(data.metrics),
        _score_liquidity(data),
        _score_tracking_quality(data),
        _score_cost(data),
        _score_nav_premium_discount(data),
        _score_diversification(data),
    ]
    factor_results.append(_score_data_completeness(factor_results))
    scored = [r for r in factor_results if r.status == EtfFactorStatus.SCORED]
    available_weight = sum(r.weight for r in scored)
    overall_score = (sum(r.weighted_contribution for r in scored) * 100 / available_weight) if available_weight else None
    data_completeness = available_weight / 100
    if data_completeness >= 0.8:
        confidence = EtfConfidenceLevel.HIGH
    elif data_completeness >= 0.5:
        confidence = EtfConfidenceLevel.MEDIUM
    else:
        confidence = EtfConfidenceLevel.LOW
    return EtfRuleEngineResult(rule_engine_version=ETF_RULE_ENGINE_VERSION, instrument_id=data.instrument_id,
        as_of=data.now, factor_results=tuple(factor_results), overall_score=overall_score,
        data_completeness=data_completeness, confidence=confidence)


def evaluate_etf_risk_gates(data: EtfRuleEngineInput, result: EtfRuleEngineResult) -> list[EtfRiskGate]:
    """Deterministic risk gates, independent of (but informed by) the score.

    Every gate distinguishes a provider/infra TECHNICAL_FAILURE, from
    genuinely INSUFFICIENT_DATA, from an actual POOR_CHARACTERISTICS signal
    -- a provider timeout must never become an automatic AVOID.
    """
    gates: list[EtfRiskGate] = []
    summary = data.readiness_summary
    mandatory_technical = summary.mandatory_technical_failure > 0
    gates.append(EtfRiskGate(gate_id="MANDATORY_DATA_TECHNICAL_FAILURE", category=EtfRiskGateCategory.TECHNICAL_FAILURE,
        severity=EtfRiskGateSeverity.BLOCKING, triggered=mandatory_technical,
        reason="One or more mandatory ETF-3 requirements (IDENTITY/MARKET_PRICE/TRADING_VOLUME) reported TECHNICAL_FAILURE."))
    gates.append(EtfRiskGate(gate_id="MANDATORY_DATA_MISSING_OR_STALE", category=EtfRiskGateCategory.INSUFFICIENT_DATA,
        severity=EtfRiskGateSeverity.BLOCKING, triggered=(not summary.ready_for_analysis) and not mandatory_technical,
        reason="Mandatory ETF-3 requirements are not all READY_FRESH."))
    insufficient_history = (data.metrics.get(EtfMetricId.RETURN_1M) is None
                             or data.metrics.get(EtfMetricId.RETURN_1M).insufficient_history)
    gates.append(EtfRiskGate(gate_id="INSUFFICIENT_PRICE_HISTORY", category=EtfRiskGateCategory.INSUFFICIENT_DATA,
        severity=EtfRiskGateSeverity.WARNING, triggered=insufficient_history,
        reason="Fewer than one month of usable daily closes; return-based scoring is degraded."))
    volume_fact = _fresh_fact(data.trading_volume_fact, EtfMetric.TRADING_VOLUME, now=data.now)
    extreme_illiquidity = volume_fact is not None and volume_fact.value < Decimal(1000)
    gates.append(EtfRiskGate(gate_id="EXTREME_ILLIQUIDITY", category=EtfRiskGateCategory.POOR_CHARACTERISTICS,
        severity=EtfRiskGateSeverity.BLOCKING, triggered=extreme_illiquidity,
        reason="Fresh trading volume is below the 1,000 shares/session floor."))
    drawdown = data.metrics.get(EtfMetricId.MAX_DRAWDOWN)
    excessive_drawdown = drawdown is not None and not drawdown.insufficient_history and drawdown.value <= Decimal(-50)
    gates.append(EtfRiskGate(gate_id="EXCESSIVE_DRAWDOWN", category=EtfRiskGateCategory.POOR_CHARACTERISTICS,
        severity=EtfRiskGateSeverity.WARNING, triggered=excessive_drawdown,
        reason="Trailing max drawdown is 50% or worse."))
    nav = data.nav if data.nav is not None and etf_freshness(data.nav, EtfMetric.NAV, now=data.now) in {"FRESH", "STALE"} else None
    price = _fresh_fact(data.market_price_fact, EtfMetric.MARKET_PRICE, now=data.now)
    abnormal_premium = False
    if nav is not None and price is not None and nav.nav:
        abnormal_premium = abs(float((price.value - nav.nav) / nav.nav * 100)) > 5.0
    gates.append(EtfRiskGate(gate_id="ABNORMAL_NAV_PREMIUM_DISCOUNT", category=EtfRiskGateCategory.POOR_CHARACTERISTICS,
        severity=EtfRiskGateSeverity.WARNING, triggered=abnormal_premium,
        reason="Market price deviates more than 5% from NAV."))
    aum_fact = _fresh_fact(data.aum_fact, EtfMetric.AUM, now=data.now)
    very_low_aum = aum_fact is not None and aum_fact.unit == "INR" and aum_fact.value < Decimal(100000000)
    gates.append(EtfRiskGate(gate_id="VERY_LOW_AUM", category=EtfRiskGateCategory.POOR_CHARACTERISTICS,
        severity=EtfRiskGateSeverity.WARNING, triggered=very_low_aum,
        reason="Fresh AUM evidence is below INR 10 crore."))
    error_fact = _fresh_fact(data.tracking_error_fact, EtfMetric.TRACKING_ERROR, now=data.now)
    poor_tracking = error_fact is not None and error_fact.value > Decimal(2)
    gates.append(EtfRiskGate(gate_id="POOR_TRACKING_QUALITY", category=EtfRiskGateCategory.POOR_CHARACTERISTICS,
        severity=EtfRiskGateSeverity.WARNING, triggered=poor_tracking,
        reason="Fresh tracking error exceeds 2%."))
    gates.append(EtfRiskGate(gate_id="INSUFFICIENT_COMPLETENESS", category=EtfRiskGateCategory.INSUFFICIENT_DATA,
        severity=EtfRiskGateSeverity.BLOCKING, triggered=result.data_completeness < 0.3,
        reason="Fewer than 30% of factor weight had available evidence."))
    return gates


def derive_etf_recommendation(result: EtfRuleEngineResult, gates: Sequence[EtfRiskGate]) -> EtfRecommendation:
    """Deterministic recommendation from the score and the risk gates.

    A provider/infra TECHNICAL_FAILURE never becomes an automatic AVOID; it
    is reported as INSUFFICIENT_DATA so a timeout cannot read as an
    investment opinion. The rule engine's score is authoritative -- nothing
    here calls out to an LLM, and nothing downstream may override this
    result with an LLM-generated recommendation.
    """
    if any(gate.triggered and gate.category == EtfRiskGateCategory.TECHNICAL_FAILURE for gate in gates):
        return EtfRecommendation.INSUFFICIENT_DATA
    if any(gate.triggered and gate.category == EtfRiskGateCategory.INSUFFICIENT_DATA
           and gate.severity == EtfRiskGateSeverity.BLOCKING for gate in gates):
        return EtfRecommendation.INSUFFICIENT_DATA
    if result.overall_score is None:
        return EtfRecommendation.INSUFFICIENT_DATA
    poor_blocking = [gate for gate in gates if gate.triggered and gate.category == EtfRiskGateCategory.POOR_CHARACTERISTICS
                      and gate.severity == EtfRiskGateSeverity.BLOCKING]
    if poor_blocking:
        return EtfRecommendation.AVOID
    poor_warnings = sum(1 for gate in gates if gate.triggered and gate.category == EtfRiskGateCategory.POOR_CHARACTERISTICS)
    score = result.overall_score
    if poor_warnings >= 2:
        return EtfRecommendation.AVOID if score < 50 else EtfRecommendation.WATCH
    if score >= 75:
        return EtfRecommendation.STRONG_OPPORTUNITY if poor_warnings == 0 else EtfRecommendation.OPPORTUNITY
    if score >= 60:
        return EtfRecommendation.OPPORTUNITY
    if score >= 40:
        return EtfRecommendation.WATCH
    return EtfRecommendation.AVOID
