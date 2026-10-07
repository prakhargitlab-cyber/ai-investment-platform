"""Deterministic ETF performance/risk metrics computed from persisted, canonical
daily market bars (``DailyMarketBar``, keyed by ``global_instrument_id``).

No LLM involvement anywhere in this module. Insufficient price history is
always explicit (``insufficient_history=True``, ``value=None``) and never
collapses into a numeric zero. All session-count windows below are
documented approximations of NSE trading sessions, not calendar days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from statistics import pstdev
from typing import Sequence


class EtfMetricId(StrEnum):
    RETURN_1M = "RETURN_1M"
    RETURN_3M = "RETURN_3M"
    RETURN_6M = "RETURN_6M"
    RETURN_1Y = "RETURN_1Y"
    ANNUALIZED_VOLATILITY = "ANNUALIZED_VOLATILITY"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    MOMENTUM_50_200 = "MOMENTUM_50_200"


# Approximate NSE trading-session counts for each nominal period. Documented
# and fixed, never silently adjusted per-instrument.
PERIOD_SESSIONS: dict[EtfMetricId, int] = {
    EtfMetricId.RETURN_1M: 21,
    EtfMetricId.RETURN_3M: 63,
    EtfMetricId.RETURN_6M: 126,
    EtfMetricId.RETURN_1Y: 252,
}

VOLATILITY_WINDOW_SESSIONS = 63
DRAWDOWN_WINDOW_SESSIONS = 252
MOMENTUM_SHORT_WINDOW = 50
MOMENTUM_LONG_WINDOW = 200
TRADING_SESSIONS_PER_YEAR = 252


@dataclass(frozen=True)
class EtfMetricValue:
    metric: EtfMetricId
    value: Decimal | None
    unit: str
    as_of_date: date | None
    sessions_used: int
    insufficient_history: bool
    reason: str | None = None


@dataclass(frozen=True)
class EtfDeterministicMetrics:
    instrument_id: object
    as_of_date: date | None
    values: dict[EtfMetricId, EtfMetricValue] = field(default_factory=dict)

    def get(self, metric: EtfMetricId) -> EtfMetricValue | None:
        return self.values.get(metric)


def usable_close_series(bars: Sequence) -> list[tuple[date, Decimal]]:
    """Ascending (trading_date, close) pairs.

    A bar with no close, or a non-positive close, is dropped rather than
    treated as a zero price -- a missing close is not evidence of a zero
    price, it is evidence of nothing.
    """
    usable = [(bar.trading_date, bar.close) for bar in bars if bar.close is not None and bar.close > 0]
    deduped: dict[date, Decimal] = {}
    for trading_date, close in usable:
        deduped[trading_date] = close
    return sorted(deduped.items(), key=lambda pair: pair[0])


def _period_return(series: list[tuple[date, Decimal]], metric: EtfMetricId) -> EtfMetricValue:
    sessions = PERIOD_SESSIONS[metric]
    if len(series) <= sessions:
        return EtfMetricValue(metric=metric, value=None, unit="PERCENT", as_of_date=None,
            sessions_used=len(series), insufficient_history=True, reason="INSUFFICIENT_PRICE_HISTORY")
    start_date, start_price = series[-(sessions + 1)]
    end_date, end_price = series[-1]
    change = (end_price - start_price) / start_price * Decimal(100)
    return EtfMetricValue(metric=metric, value=change, unit="PERCENT", as_of_date=end_date,
        sessions_used=sessions + 1, insufficient_history=False,
        reason=f"FROM_{start_date.isoformat()}_TO_{end_date.isoformat()}")


def compute_returns(series: list[tuple[date, Decimal]]) -> dict[EtfMetricId, EtfMetricValue]:
    return {metric: _period_return(series, metric) for metric in PERIOD_SESSIONS}


def compute_annualized_volatility(series: list[tuple[date, Decimal]]) -> EtfMetricValue:
    window = series[-(VOLATILITY_WINDOW_SESSIONS + 1):]
    if len(window) < VOLATILITY_WINDOW_SESSIONS + 1:
        return EtfMetricValue(metric=EtfMetricId.ANNUALIZED_VOLATILITY, value=None, unit="PERCENT",
            as_of_date=None, sessions_used=len(window), insufficient_history=True,
            reason="INSUFFICIENT_PRICE_HISTORY")
    daily_returns = [float((window[i][1] - window[i - 1][1]) / window[i - 1][1]) for i in range(1, len(window))]
    stdev = Decimal(str(pstdev(daily_returns)))
    annualized = stdev * Decimal(TRADING_SESSIONS_PER_YEAR).sqrt() * Decimal(100)
    return EtfMetricValue(metric=EtfMetricId.ANNUALIZED_VOLATILITY, value=annualized, unit="PERCENT",
        as_of_date=window[-1][0], sessions_used=len(window), insufficient_history=False)


def compute_max_drawdown(series: list[tuple[date, Decimal]]) -> EtfMetricValue:
    window = series[-DRAWDOWN_WINDOW_SESSIONS:] if len(series) > DRAWDOWN_WINDOW_SESSIONS else series
    if len(window) < 2:
        return EtfMetricValue(metric=EtfMetricId.MAX_DRAWDOWN, value=None, unit="PERCENT", as_of_date=None,
            sessions_used=len(window), insufficient_history=True, reason="INSUFFICIENT_PRICE_HISTORY")
    peak = window[0][1]
    max_dd = Decimal(0)
    for _, price in window:
        if price > peak:
            peak = price
        drawdown = (price - peak) / peak
        if drawdown < max_dd:
            max_dd = drawdown
    return EtfMetricValue(metric=EtfMetricId.MAX_DRAWDOWN, value=max_dd * Decimal(100), unit="PERCENT",
        as_of_date=window[-1][0], sessions_used=len(window), insufficient_history=False)


def compute_momentum(series: list[tuple[date, Decimal]]) -> EtfMetricValue:
    if len(series) < MOMENTUM_LONG_WINDOW:
        return EtfMetricValue(metric=EtfMetricId.MOMENTUM_50_200, value=None, unit="PERCENT", as_of_date=None,
            sessions_used=len(series), insufficient_history=True, reason="INSUFFICIENT_PRICE_HISTORY")
    closes = [price for _, price in series]
    short_avg = sum(closes[-MOMENTUM_SHORT_WINDOW:]) / Decimal(MOMENTUM_SHORT_WINDOW)
    long_avg = sum(closes[-MOMENTUM_LONG_WINDOW:]) / Decimal(MOMENTUM_LONG_WINDOW)
    value = (short_avg - long_avg) / long_avg * Decimal(100)
    return EtfMetricValue(metric=EtfMetricId.MOMENTUM_50_200, value=value, unit="PERCENT",
        as_of_date=series[-1][0], sessions_used=MOMENTUM_LONG_WINDOW, insufficient_history=False,
        reason="SHORT_MA_50_VS_LONG_MA_200")


def compute_etf_deterministic_metrics(instrument_id, bars: Sequence) -> EtfDeterministicMetrics:
    """Compute every deterministic metric from a single bar series.

    ``bars`` should already be scoped to one ``global_instrument_id`` by the
    caller (this module performs no instrument filtering of its own).
    """
    series = usable_close_series(bars)
    values: dict[EtfMetricId, EtfMetricValue] = {}
    values.update(compute_returns(series))
    values[EtfMetricId.ANNUALIZED_VOLATILITY] = compute_annualized_volatility(series)
    values[EtfMetricId.MAX_DRAWDOWN] = compute_max_drawdown(series)
    values[EtfMetricId.MOMENTUM_50_200] = compute_momentum(series)
    as_of = series[-1][0] if series else None
    return EtfDeterministicMetrics(instrument_id=instrument_id, as_of_date=as_of, values=values)
