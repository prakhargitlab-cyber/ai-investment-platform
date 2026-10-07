"""Deterministic ETF metrics tests: no DB, no provider -- pure functions
over hand-built DailyMarketBar series."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.etf_metrics import (
    EtfMetricId, compute_annualized_volatility, compute_etf_deterministic_metrics,
    compute_max_drawdown, compute_momentum, compute_returns, usable_close_series,
)
from app.models import DailyMarketBar, SourceMode

ID = UUID(int=7)
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _bar(trading_date: date, close, **overrides) -> DailyMarketBar:
    defaults = dict(global_instrument_id=ID, trading_date=trading_date,
        close=Decimal(str(close)) if close is not None else None, currency="INR", provider="NSE",
        source_mode=SourceMode.REAL, source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW)
    return DailyMarketBar(**(defaults | overrides))


def _series(prices, start=date(2025, 1, 1)):
    return [_bar(start + timedelta(days=i), price) for i, price in enumerate(prices)]


class TestUsableCloseSeries:
    def test_drops_missing_and_nonpositive_closes(self):
        bars = [_bar(date(2025, 1, 1), 100), _bar(date(2025, 1, 2), None), _bar(date(2025, 1, 3), 102)]
        series = usable_close_series(bars)
        assert [d for d, _ in series] == [date(2025, 1, 1), date(2025, 1, 3)]

    def test_sorts_ascending_regardless_of_input_order(self):
        bars = [_bar(date(2025, 1, 3), 102), _bar(date(2025, 1, 1), 100)]
        series = usable_close_series(bars)
        assert [d for d, _ in series] == [date(2025, 1, 1), date(2025, 1, 3)]


class TestPeriodReturns:
    def test_insufficient_history_is_explicit_not_zero(self):
        series = usable_close_series(_series([100] * 10))
        returns = compute_returns(series)
        value = returns[EtfMetricId.RETURN_1M]
        assert value.insufficient_history is True
        assert value.value is None
        assert value.reason == "INSUFFICIENT_PRICE_HISTORY"

    def test_1m_return_computed_from_exact_window(self):
        prices = [100] * 21 + [110]
        series = usable_close_series(_series(prices))
        returns = compute_returns(series)
        value = returns[EtfMetricId.RETURN_1M]
        assert value.insufficient_history is False
        assert value.value == Decimal("10")

    def test_negative_return_is_negative_not_clamped(self):
        prices = [100] * 21 + [80]
        series = usable_close_series(_series(prices))
        value = compute_returns(series)[EtfMetricId.RETURN_1M]
        assert value.value == Decimal("-20")


class TestVolatility:
    def test_insufficient_history(self):
        series = usable_close_series(_series([100] * 10))
        result = compute_annualized_volatility(series)
        assert result.insufficient_history is True
        assert result.value is None

    def test_zero_volatility_for_flat_series(self):
        series = usable_close_series(_series([100] * 70))
        result = compute_annualized_volatility(series)
        assert result.insufficient_history is False
        assert result.value == Decimal(0)

    def test_oscillating_series_has_positive_volatility(self):
        prices = [100 + (5 if i % 2 == 0 else -5) for i in range(70)]
        series = usable_close_series(_series(prices))
        result = compute_annualized_volatility(series)
        assert result.value > Decimal(0)


class TestMaxDrawdown:
    def test_insufficient_history(self):
        result = compute_max_drawdown([])
        assert result.insufficient_history is True
        assert result.value is None

    def test_monotonic_rise_has_zero_drawdown(self):
        series = usable_close_series(_series([100 + i for i in range(30)]))
        result = compute_max_drawdown(series)
        assert result.value == Decimal(0)

    def test_drop_from_peak_is_captured(self):
        prices = [100, 150] + [75] * 5
        series = usable_close_series(_series(prices))
        result = compute_max_drawdown(series)
        # Peak of 150 then a drop to 75 is a -50% drawdown; drawdowns are
        # always <= 0, never reported as a positive magnitude.
        assert result.value == Decimal(-50)


class TestMomentum:
    def test_insufficient_history_below_200_sessions(self):
        series = usable_close_series(_series([100] * 150))
        result = compute_momentum(series)
        assert result.insufficient_history is True
        assert result.value is None

    def test_rising_short_average_is_positive_momentum(self):
        prices = [100] * 150 + list(range(100, 150))
        series = usable_close_series(_series(prices))
        result = compute_momentum(series)
        assert result.insufficient_history is False
        assert result.value > Decimal(0)


class TestComputeEtfDeterministicMetrics:
    def test_empty_bars_yields_all_insufficient_and_no_as_of(self):
        metrics = compute_etf_deterministic_metrics(ID, [])
        assert metrics.as_of_date is None
        assert all(value.insufficient_history for value in metrics.values.values())

    def test_full_history_populates_every_metric(self):
        prices = [100 + i * 0.1 for i in range(260)]
        metrics = compute_etf_deterministic_metrics(ID, _series(prices))
        assert metrics.as_of_date == date(2025, 1, 1) + timedelta(days=259)
        for metric_id in EtfMetricId:
            value = metrics.get(metric_id)
            assert value is not None
            assert value.insufficient_history is False
            assert value.value is not None
