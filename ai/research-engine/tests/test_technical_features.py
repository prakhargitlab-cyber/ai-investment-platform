from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.models import MarketPriceObservation
from app.technical_features import TechnicalConfig, TechnicalFeatureEngine, PersistedVolumeObservation


NOW = datetime(2026, 9, 13, 18, tzinfo=timezone.utc)
STOCK = UUID(int=1)


def history(values, instrument_id=STOCK, currency="INR", end=NOW):
    # Observed weekday sessions, with no calendar interpolation by the engine.
    dates, day = [], end
    while len(dates) < len(values):
        if day.weekday() < 5:
            dates.append(day)
        day -= timedelta(days=1)
    dates.reverse()
    return [MarketPriceObservation(instrument_id=instrument_id, price=Decimal(str(value)), currency=currency,
        observed_at=stamp, retrieved_at=stamp, provider="PERSISTED", source_url="https://evidence.test/history")
        for value, stamp in zip(values, dates)]


def compute(values, **kwargs):
    return TechnicalFeatureEngine().compute(STOCK, history(values), as_of=NOW, currency="INR", **kwargs)


@pytest.mark.parametrize("values,state", [
    ([100 + .2*i for i in range(260)], "UPTREND"),
    ([200 - .2*i for i in range(260)], "DOWNTREND"),
    ([100]*260, "BASE_BUILDING"),
    ([100 + 10*(i % 2) for i in range(260)], "RANGE_BOUND"),
    ([100]*60 + [104], "BREAKOUT"),
    ([100 + .2*i for i in range(255)] + [151, 150, 149, 148, 147], "PULLBACK_IN_UPTREND"),
    ([100 + .2*i for i in range(259)] + [200], "OVEREXTENDED"),
    ([200 - .6*i for i in range(240)] + [56.6 + .3*i for i in range(20)], "REVERSAL_CANDIDATE"),
    ([100]*19, "INSUFFICIENT_DATA"),
])
def test_explicit_technical_states(values, state):
    result = compute(values)
    assert result.technical_state == state
    assert (result.technical_score is None) == (state == "INSUFFICIENT_DATA")


@pytest.mark.parametrize("count,readiness", [(19, "INSUFFICIENT_HISTORY"), (20, "SHORT_HISTORY"),
    (49, "SHORT_HISTORY"), (50, "MEDIUM_HISTORY"), (99, "MEDIUM_HISTORY"), (100, "EXTENDED_HISTORY"),
    (199, "EXTENDED_HISTORY"), (200, "FULL_HISTORY")])
def test_history_boundaries_and_exact_dma_math(count, readiness):
    result = compute(list(range(1, count+1)))
    assert result.history_readiness == readiness
    for window in (20, 50, 100, 200):
        value = getattr(result, f"dma{window}")
        if count >= window:
            expected = (count + count-window+1) / 2
            assert value == expected
            assert getattr(result, f"distance_to_dma{window}_pct") == pytest.approx((count/expected-1)*100)
        else:
            assert value is None
            assert result.feature_states[f"dma{window}"] == "MISSING"


def test_rsi_wilder_independent_known_worksheet():
    values = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    # First 14 changes: total gains 3.34, losses 1.40. No EMA warmup ambiguity.
    assert compute(values).rsi14 == pytest.approx(100*3.34/(3.34+1.40), abs=1e-7)
    # One Wilder update: gain=(3.34/14*13)/14; loss=(1.40/14*13+.28)/14.
    gain, loss = Decimal("3.34")/14*13/14, (Decimal("1.40")/14*13+Decimal(".28"))/14
    assert compute(values + [46.0]).rsi14 == pytest.approx(float(100*gain/(gain+loss)), abs=1e-7)
    assert compute(values[:14]).rsi14 is None
    assert compute([100]*20).rsi14 == 50
    assert compute(list(range(1, 21))).rsi14 == 100
    assert compute(list(range(21, 1, -1))).rsi14 == 0


def test_macd_linear_series_exact_and_independent_nonlinear_ema():
    result = compute(list(range(1, 61)))
    assert result.macd == pytest.approx(7)
    assert result.macd_signal == pytest.approx(7)
    assert result.macd_histogram == pytest.approx(0)
    values = [Decimal(100) + Decimal(i*i % 37) for i in range(70)]
    # Independent closed-form weighted sum, not the implementation's recursion.
    def ema_at(series, period, index):
        alpha = Decimal(2)/Decimal(period+1)
        decay = 1-alpha
        tail = index-period+1
        seed = sum(series[:period])/period
        return seed*decay**tail + sum(alpha*series[k]*decay**(index-k) for k in range(period, index+1))
    macds = [ema_at(values, 12, i)-ema_at(values, 26, i) for i in range(25, len(values))]
    expected_signal = ema_at(macds, 9, len(macds)-1)
    result = compute(values)
    assert result.macd == pytest.approx(float(macds[-1]), abs=1e-7)
    assert result.macd_signal == pytest.approx(float(expected_signal), abs=1e-7)
    assert result.macd_histogram == pytest.approx(float(macds[-1]-expected_signal), abs=1e-7)
    assert compute([100]*25).macd is None
    assert compute([100]*26).macd == 0
    assert compute([100]*33).macd_signal is None
    assert compute([100]*34).macd_signal == 0


def test_missing_ohlc_volume_and_actual_zero_volume():
    rows = history([100]*30)
    engine = TechnicalFeatureEngine()
    result = engine.compute(STOCK, rows, as_of=NOW)
    assert result.adx14 is result.atr14 is result.atr_pct is None
    assert result.volume_average20 is result.volume_ratio20 is None
    volumes = [PersistedVolumeObservation(instrument_id=STOCK, observed_at=r.observed_at,
        retrieved_at=r.retrieved_at, volume=100 if i < 29 else 0, provider=r.provider, source_url=r.source_url) for i, r in enumerate(rows)]
    result = engine.compute(STOCK, rows, as_of=NOW, volume_history=volumes)
    assert result.volume_average20 == 100
    assert result.volume_ratio20 == 0
    assert result.feature_states["volumeRatio20"] == "AVAILABLE"
    zero = [r.model_copy(update={"volume": Decimal(0)}) for r in volumes]
    result = engine.compute(STOCK, rows, as_of=NOW, volume_history=zero)
    assert result.volume_average20 == 0 and result.volume_ratio20 is None
    assert "NONZERO_VOLUME_BASELINE" in result.missing_inputs


def test_latest_conflict_is_not_averaged_or_replaced_by_prior_close():
    rows = history(range(100, 160))
    conflict = rows[-1].model_copy(update={"price": Decimal(999), "provider": "OTHER"})
    engine = TechnicalFeatureEngine()
    result = engine.compute(STOCK, rows+[conflict], as_of=NOW)
    assert result.latest_price is result.dma20 is result.technical_score is None
    assert result.technical_state == "INSUFFICIENT_DATA" and result.confidence == 0
    assert result.feature_states["latestPrice"] == "CONFLICTING"
    assert result == engine.compute(STOCK, list(reversed(rows+[conflict])), as_of=NOW)


def test_same_date_deduplication_latest_timestamp_provenance_and_no_fill():
    rows = history(range(100, 160))
    engine = TechnicalFeatureEngine()
    first = engine.compute(STOCK, rows, as_of=NOW)
    duplicate = rows[-1].model_copy(update={"provider": "OTHER"})
    repeated = engine.compute(STOCK, [*reversed(rows), duplicate], as_of=NOW)
    assert repeated.observation_count == 60 and repeated.dma50 == first.dma50
    assert repeated.duplicate_observation_count == 1
    later = rows[-1].model_copy(update={"price": Decimal(160), "observed_at": rows[-1].observed_at+timedelta(minutes=1)})
    assert engine.compute(STOCK, rows+[later], as_of=NOW).latest_price == 160
    sparse = [r for i, r in enumerate(rows) if i % 2]
    assert engine.compute(STOCK, sparse, as_of=NOW).observation_count == 30


def test_stale_future_currency_and_provider_filtering():
    engine = TechnicalFeatureEngine()
    rows = history(range(100, 160), end=NOW-timedelta(days=20))
    result = engine.compute(STOCK, rows, as_of=NOW)
    assert result.dma50 is not None and result.feature_states["dma50"] == "STALE"
    assert result.technical_state == "INSUFFICIENT_DATA" and result.technical_score is None
    assert result.stale_inputs == ["PRICE_HISTORY"]
    rows = history(range(100, 160))
    future = rows[-1].model_copy(update={"retrieved_at": NOW+timedelta(days=1), "price": Decimal(999)})
    result = engine.compute(STOCK, rows+[future], as_of=NOW)
    assert result.latest_price == 159 and result.rejected_observation_count == 1
    assert engine.compute(STOCK, rows, as_of=NOW, currency="USD").latest_price is None
    assert engine.compute(STOCK, rows, as_of=NOW, trusted_providers=frozenset()).latest_price is None


def test_return_offsets_support_and_year_readiness():
    result = compute(list(range(1, 254)))
    for field, offset in [("return1_w", 5), ("return1_m", 21), ("return3_m", 63), ("return6_m", 126), ("return1_y", 252)]:
        assert getattr(result, field) == pytest.approx((253/(253-offset)-1)*100)
    assert result.support_level == 233 and result.resistance_level == 252
    assert result.distance_to_support_pct == pytest.approx((253/233-1)*100)
    assert result.higher_highs_higher_lows is True and result.lower_highs_lower_lows is False
    assert compute([100]*251).distance_from52_week_high_pct is None
    assert compute([100]*252).distance_from52_week_high_pct == 0
    assert compute([100]*252).return1_y is None


def test_deterministic_pure_execution_and_configuration(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Network invoked"))
    rows = history([100+.1*i for i in range(260)])
    original = [r.model_dump() for r in rows]
    engine = TechnicalFeatureEngine()
    first = engine.compute(STOCK, rows, as_of=NOW)
    assert first == engine.compute(STOCK, reversed(rows), as_of=NOW)
    assert original == [r.model_dump() for r in rows]
    assert first.feature_version == "TECHNICAL_FEATURES_V2"
    with pytest.raises(ValueError): TechnicalConfig(momentum_full_scale_pct=0)
    with pytest.raises(ValueError): TechnicalConfig(score_weights=(float("nan"), 1, 1))
