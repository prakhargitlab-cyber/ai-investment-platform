"""Pure deterministic features over persisted daily candles or close fallback.

No adjusted-close, OHLC or volume semantics are inferred from the price table.
Candle dates remain exchange DATEs; fallback uses UTC observation dates. Returns
use observed-session offsets; all percentages are percentage units, not ratios.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Iterable, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field

from app.models import DailyMarketBar, MarketPriceObservation, ResearchBaseModel


TECHNICAL_FEATURE_VERSION = "TECHNICAL_FEATURES_V2"
TechnicalState = Literal["UPTREND", "DOWNTREND", "BASE_BUILDING", "BREAKOUT",
                         "PULLBACK_IN_UPTREND", "REVERSAL_CANDIDATE", "RANGE_BOUND",
                         "OVEREXTENDED", "INSUFFICIENT_DATA"]


@dataclass(frozen=True)
class TechnicalConfig:
    """Selection heuristics, not calibrated financial forecasts.

    1% breakout buffers small price noise; 2% retreat/3% MA proximity defines
    a bounded pullback. 0.02% per observation and a 5% band define a flat base.
    10% DMA20 distance plus RSI70 flags extension. These are explicit starting
    tolerances, configurable for later validation, not universal market rules.
    """
    max_age_days: int = 7
    return_lookbacks: tuple[int, ...] = (5, 21, 63, 126, 252)
    level_lookback: int = 20
    year_observations: int = 252
    slope_threshold_pct: float = 0.02
    breakout_buffer_pct: float = 1.0
    pullback_retreat_pct: float = 2.0
    pullback_proximity_pct: float = 3.0
    base_range_pct: float = 5.0
    extension_distance_pct: float = 10.0
    extension_rsi: float = 70.0
    volume_confirmation_ratio: float = 1.5
    volume_contraction_ratio: float = 0.75
    score_weights: tuple[float, ...] = (50.0, 30.0, 20.0)
    momentum_full_scale_pct: float = 20.0
    extension_penalty: float = 15.0
    volume_breakout_bonus: float = 5.0

    def __post_init__(self):
        if (self.max_age_days < 1 or self.level_lookback < 2 or self.year_observations < 2
                or len(self.return_lookbacks) != 5 or any(n < 1 for n in self.return_lookbacks)
                or tuple(sorted(set(self.return_lookbacks))) != self.return_lookbacks
                or len(self.score_weights) != 3 or sum(self.score_weights) <= 0):
            raise ValueError("Invalid feature lookbacks or weights")
        for name, value in asdict(self).items():
            numbers = value if isinstance(value, tuple) else (value,)
            if any(not isfinite(v) or v < 0 for v in numbers):
                raise ValueError(f"Invalid technical configuration: {name}")
        if self.momentum_full_scale_pct == 0:
            raise ValueError("Momentum scale must be positive")
        if not 0 <= self.volume_contraction_ratio < 1 < self.volume_confirmation_ratio:
            raise ValueError("Volume thresholds must bracket one")


class PersistedVolumeObservation(ResearchBaseModel):
    """Optional durable volume input; no volume is manufactured from quote counts."""
    instrument_id: UUID
    observed_at: datetime
    retrieved_at: datetime
    volume: Decimal | None
    provider: str
    source_url: str


@dataclass(frozen=True)
class PriceHistory:
    observations: tuple[MarketPriceObservation | DailyMarketBar, ...]
    conflicting_dates: tuple[date, ...]
    current_conflict: bool
    rejected_count: int
    duplicate_count: int
    currency: str | None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def finite_number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and isfinite(float(number)) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def normalize_price_history(instrument_id: UUID, observations: Iterable[MarketPriceObservation], *,
                            as_of: datetime, currency: str | None = None,
                            trusted_providers: frozenset[str] | None = None) -> PriceHistory:
    """Latest timestamp per UTC date; identical ties collapse, unequal ties fail.

    This extends Phase 1's latest-timestamp conflict gate to each historical day.
    Conflicted historical dates are omitted with diagnostics. A conflicted latest
    date blocks current features instead of silently falling back to yesterday.
    Retrieval timestamps must also be known at as_of (no look-ahead).
    """
    grouped = defaultdict(list)
    rejected = 0
    currencies = set()
    for row in observations:
        price = finite_number(row.price)
        if (row.instrument_id != instrument_id or utc(row.observed_at) > utc(as_of)
                or utc(row.retrieved_at) > utc(as_of) or price is None or price <= 0
                or (trusted_providers is not None and row.provider not in trusted_providers)
                or (currency is not None and row.currency != currency)):
            rejected += 1
            continue
        currencies.add(row.currency)
        grouped[utc(row.observed_at).date()].append(row)
    if len(currencies) > 1:
        return PriceHistory((), tuple(sorted(grouped)), True, rejected, 0, currency)
    output, conflicts = [], []
    duplicates = 0
    for day, rows in sorted(grouped.items()):
        duplicates += len(rows) - 1
        latest = max(utc(row.observed_at) for row in rows)
        current = [row for row in rows if utc(row.observed_at) == latest]
        if len({row.price for row in current}) != 1:
            conflicts.append(day)
            continue
        # No provider is assumed more authoritative. Only identical-price ties
        # use provenance to select a stable representative.
        output.append(min(current, key=lambda row: (row.provider, row.source_url, utc(row.retrieved_at))))
    return PriceHistory(tuple(output), tuple(conflicts), bool(conflicts and max(grouped) in conflicts),
                        rejected, duplicates, currency or next(iter(currencies), None))


class TechnicalFeatureSnapshot(ResearchBaseModel):
    global_instrument_id: UUID
    as_of: datetime
    feature_version: str = TECHNICAL_FEATURE_VERSION
    configuration: dict
    price_basis: str = "CANONICAL_PERSISTED_PRICE_UNADJUSTED"
    extrema_basis: str = "ROLLING_CLOSE_EXTREMA"
    technical_input_source: str = "CLOSE_ONLY_FALLBACK"
    source_diagnostics: list[str] = Field(default_factory=list)
    daily_bar_observation_count: int = 0
    history_trading_start: date | None = None
    history_trading_end: date | None = None
    feature_readiness: dict[str, str] = Field(default_factory=dict)
    ohlcv_feature_coverage: float = 0
    observation_count: int
    history_start: datetime | None = None
    history_end: datetime | None = None
    history_readiness: str
    currency: str | None = None
    latest_price: float | None = None
    dma20: float | None = None
    dma50: float | None = None
    dma100: float | None = None
    dma200: float | None = None
    distance_to_dma20_pct: float | None = None
    distance_to_dma50_pct: float | None = None
    distance_to_dma100_pct: float | None = None
    distance_to_dma200_pct: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    adx14: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    return1_w: float | None = Field(default=None, alias="return1W")
    return1_m: float | None = Field(default=None, alias="return1M")
    return3_m: float | None = Field(default=None, alias="return3M")
    return6_m: float | None = Field(default=None, alias="return6M")
    return1_y: float | None = Field(default=None, alias="return1Y")
    trend_slope20: float | None = None
    trend_slope50: float | None = None
    volume_average20: float | None = None
    current_volume: int | Decimal | None = None
    volume_ratio20: float | None = None
    volume_state: str = "UNAVAILABLE"
    volume_expansion: bool | None = None
    volume_contraction: bool | None = None
    breakout_volume_confirmed: bool | None = None
    reversal_volume_confirmed: bool | None = None
    distance_from52_week_high_pct: float | None = Field(default=None, alias="distanceFrom52WeekHighPct")
    distance_from52_week_low_pct: float | None = Field(default=None, alias="distanceFrom52WeekLowPct")
    support_level: float | None = None
    resistance_level: float | None = None
    distance_to_support_pct: float | None = None
    distance_to_resistance_pct: float | None = None
    higher_highs_higher_lows: bool | None = None
    lower_highs_lower_lows: bool | None = None
    breakout_state: str = "INSUFFICIENT_DATA"
    technical_state: TechnicalState = "INSUFFICIENT_DATA"
    technical_score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    confidence: float = 0
    feature_states: dict[str, str] = Field(default_factory=dict)
    missing_inputs: list[str] = Field(default_factory=list)
    stale_inputs: list[str] = Field(default_factory=list)
    conflicting_dates: list[date] = Field(default_factory=list)
    rejected_observation_count: int = 0
    duplicate_observation_count: int = 0


def percentage(value: float, reference: float) -> float:
    return (value / reference - 1) * 100


def normalize_daily_history(instrument_id, bars, *, as_of, currency, trusted_providers):
    """NSE REAL candles only; never assemble a candle from different sources.

    Dates remain exchange DATEs. Latest known retrieval wins a correction;
    unequal OHLCV at the same retrieval time is a conflict, not a tie-break.
    A latest-date conflict blocks fallback to a different price source.
    """
    grouped = defaultdict(list)
    rejected = duplicates = 0
    local_day = utc(as_of).astimezone(ZoneInfo('Asia/Kolkata')).date()
    for bar in bars:
        if (bar.global_instrument_id != instrument_id or bar.provider != 'NSE' or bar.source_mode != 'REAL'
                or (trusted_providers is not None and bar.provider not in trusted_providers)
                or (currency is not None and bar.currency != currency)
                or utc(bar.retrieved_at) > utc(as_of) or bar.trading_date > local_day):
            rejected += 1
            continue
        grouped[bar.trading_date].append(bar)
    currencies = {bar.currency for group in grouped.values() for bar in group}
    if len(currencies) > 1:
        return PriceHistory((), tuple(sorted(grouped)), True, rejected, 0, currency), bool(grouped)
    output, conflicts = [], []
    for day, group in sorted(grouped.items()):
        duplicates += len(group) - 1
        stamp = max(utc(bar.retrieved_at) for bar in group)
        current = [bar for bar in group if utc(bar.retrieved_at) == stamp]
        values = {(bar.open, bar.high, bar.low, bar.close, bar.previous_close, bar.volume, bar.turnover) for bar in current}
        close = finite_number(current[0].close)
        if len(values) != 1 or close is None or close <= 0:
            conflicts.append(day)
        else:
            output.append(min(current, key=lambda bar: (bar.provider_symbol or '', bar.source_url)))
    return PriceHistory(tuple(output), tuple(conflicts), bool(conflicts and max(grouped) in conflicts),
        rejected, duplicates, currency or next(iter(currencies), None)), bool(grouped)


def _wilder(values, period=14):
    if len(values) < period:
        return []
    output = [sum(values[:period]) / period]
    for value in values[period:]:
        output.append((output[-1] * (period - 1) + value) / period)
    return output


def _atr_adx(candles, period=14):
    """15 candles seed ATR14; 28 candles seed ADX14 (14 DX values).

    First candle provides the actual prior close/high/low, not a fabricated TR.
    Equal positive up/down movement yields neither +DM nor -DM. Zero TR or
    zero DI sum yields DX=0, so a flat market has ATR=ADX=0 once ready.
    """
    tr, plus, minus = [], [], []
    for previous, current in zip(candles, candles[1:]):
        high, low, previous_close = float(current.high), float(current.low), float(previous.close)
        tr.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        up, down = high - float(previous.high), float(previous.low) - low
        plus.append(up if up > 0 and up > down else 0.0)
        minus.append(down if down > 0 and down > up else 0.0)
    ranges, positive, negative = _wilder(tr, period), _wilder(plus, period), _wilder(minus, period)
    dx = []
    for total, up, down in zip(ranges, positive, negative):
        plus_di, minus_di = (100 * up / total, 100 * down / total) if total else (0.0, 0.0)
        denominator = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    adx = _wilder(dx, period)
    return (max(0.0, ranges[-1]) if ranges else None, _clamp(adx[-1]) if adx else None)


def _ema(values: list[float], period: int) -> list[float]:
    """SMA seed, then alpha=2/(period+1); result begins at period-1."""
    if len(values) < period:
        return []
    output = [sum(values[:period]) / period]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _rsi(values: list[float], period=14) -> float | None:
    if len(values) <= period:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(v, 0) for v in changes[:period]) / period
    loss = sum(max(-v, 0) for v in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _slope(values: list[float]) -> float:
    """OLS slope as percent of window mean per observed session."""
    center = (len(values) - 1) / 2
    mean = sum(values) / len(values)
    slope = sum((i - center) * (v - mean) for i, v in enumerate(values)) / sum((i - center)**2 for i in range(len(values)))
    return slope / mean * 100


def _clamp(value: float) -> float:
    return min(100.0, max(0.0, value))


class TechnicalFeatureEngine:
    def __init__(self, config: TechnicalConfig | None = None):
        self.config = config or TechnicalConfig()

    def compute(self, instrument_id: UUID, observations: Iterable[MarketPriceObservation], *, as_of: datetime,
                currency: str | None = None, trusted_providers: frozenset[str] | None = None,
                volume_history: Iterable[PersistedVolumeObservation] = (),
                daily_bar_history: Iterable[DailyMarketBar] = ()) -> TechnicalFeatureSnapshot:
        cfg = self.config
        daily_history, use_daily = normalize_daily_history(instrument_id, daily_bar_history,
            as_of=as_of, currency=currency, trusted_providers=trusted_providers)
        fallback = None
        selection_reason = None
        if use_daily and not daily_history.current_conflict:
            daily_stale = bool(daily_history.observations) and (utc(as_of).astimezone(ZoneInfo('Asia/Kolkata')).date()
                - daily_history.observations[-1].trading_date > timedelta(days=cfg.max_age_days))
            if len(daily_history.observations) < 20 or daily_stale:
                fallback = normalize_price_history(instrument_id, observations, as_of=as_of,
                    currency=currency, trusted_providers=trusted_providers)
                if (len(fallback.observations) >= 20 and not fallback.current_conflict and
                        utc(as_of) - utc(fallback.observations[-1].observed_at) <= timedelta(days=cfg.max_age_days)):
                    use_daily = False
                    selection_reason = 'NSE_DAILY_HISTORY_STALE' if daily_stale else 'NSE_DAILY_HISTORY_BELOW_20'
        history = daily_history if use_daily else fallback or normalize_price_history(instrument_id,
            observations, as_of=as_of, currency=currency, trusted_providers=trusted_providers)
        rows = history.observations
        prices = [float(row.close if use_daily else row.price) for row in rows]
        # Display metadata only; candle calculations use the exchange DATE directly.
        def stamp(row):
            return datetime.combine(row.trading_date, time.min, ZoneInfo('Asia/Kolkata')) if use_daily else utc(row.observed_at)
        n = len(prices)
        readiness = ("FULL_HISTORY" if n >= 200 else "EXTENDED_HISTORY" if n >= 100 else
                     "MEDIUM_HISTORY" if n >= 50 else "SHORT_HISTORY" if n >= 20 else "INSUFFICIENT_HISTORY")
        result = TechnicalFeatureSnapshot(global_instrument_id=instrument_id, as_of=utc(as_of), configuration=asdict(cfg),
            observation_count=n, history_readiness=readiness, currency=history.currency,
            history_start=stamp(rows[0]) if rows else None, history_end=stamp(rows[-1]) if rows else None,
            conflicting_dates=list(history.conflicting_dates), rejected_observation_count=history.rejected_count,
            duplicate_observation_count=history.duplicate_count)
        result.daily_bar_observation_count = len(daily_history.observations)
        if selection_reason:
            result.source_diagnostics.append(selection_reason)
        if use_daily:
            result.technical_input_source = 'DAILY_MARKET_BAR_NSE'
            result.price_basis = 'PERSISTED_NSE_DAILY_CLOSE_UNADJUSTED'
            result.history_trading_start = rows[0].trading_date if rows else None
            result.history_trading_end = rows[-1].trading_date if rows else None
        if daily_history.rejected_count:
            result.source_diagnostics.append('INELIGIBLE_DAILY_BARS_EXCLUDED')
        if use_daily and history.conflicting_dates:
            result.source_diagnostics.append('MIXED_NOT_ALLOWED')
        # A latest-price conflict invalidates current features, even with a long
        # historical tail. History metadata and conflict diagnostics are retained.
        usable = bool(rows) and not history.current_conflict
        stale = bool(rows) and ((utc(as_of).astimezone(ZoneInfo('Asia/Kolkata')).date() - rows[-1].trading_date
            if use_daily else utc(as_of) - utc(rows[-1].observed_at)) > timedelta(days=cfg.max_age_days))
        if stale:
            result.stale_inputs.append("PRICE_HISTORY")
        if usable:
            result.latest_price = prices[-1]
            for period in (20, 50, 100, 200):
                if n >= period:
                    dma = sum(prices[-period:]) / period
                    setattr(result, f"dma{period}", dma)
                    setattr(result, f"distance_to_dma{period}_pct", percentage(prices[-1], dma))
            result.rsi14 = _rsi(prices)
            slow, fast = _ema(prices, 26), _ema(prices, 12)
            if slow:
                macd_series = [a - b for a, b in zip(fast[14:], slow)]
                result.macd = macd_series[-1]
                signal = _ema(macd_series, 9)
                if signal:
                    result.macd_signal = signal[-1]
                    result.macd_histogram = result.macd - result.macd_signal
            for field, lookback in zip(("return1_w", "return1_m", "return3_m", "return6_m", "return1_y"), cfg.return_lookbacks):
                if n > lookback:
                    setattr(result, field, percentage(prices[-1], prices[-lookback-1]))
            for period in (20, 50):
                if n >= period:
                    setattr(result, f"trend_slope{period}", _slope(prices[-period:]))
            if n >= cfg.year_observations:
                result.distance_from52_week_high_pct = percentage(prices[-1], max(prices[-cfg.year_observations:]))
                result.distance_from52_week_low_pct = percentage(prices[-1], min(prices[-cfg.year_observations:]))
            if n > cfg.level_lookback:
                window = prices[-cfg.level_lookback-1:-1]
                result.support_level, result.resistance_level = min(window), max(window)
                result.distance_to_support_pct = percentage(prices[-1], result.support_level)
                result.distance_to_resistance_pct = percentage(prices[-1], result.resistance_level)
            if n >= cfg.level_lookback * 2:
                before, after = prices[-2*cfg.level_lookback:-cfg.level_lookback], prices[-cfg.level_lookback:]
                result.higher_highs_higher_lows = max(after) > max(before) and min(after) > min(before)
                result.lower_highs_lower_lows = max(after) < max(before) and min(after) < min(before)
            if use_daily:
                self._candles(result, rows, history.conflicting_dates)
            else:
                self._volume(result, rows, volume_history, trusted_providers)
            if not stale and n >= 20:
                self._classify(result, prices)
                self._score(result)
        self._volume_signals(result)
        for name, minimum in [('RSI14', 15), ('MA20', 20), ('MA50', 50), ('MA100', 100), ('MA200', 200),
                              ('BREAKOUT', cfg.level_lookback + 1)]:
            result.feature_readiness[name] = 'AVAILABLE' if usable and n >= minimum else 'INSUFFICIENT_HISTORY'
        for name in ('ATR14', 'ADX14'):
            result.feature_readiness.setdefault(name, 'MISSING_OHLC')
        result.feature_readiness.setdefault('VOLUME20', 'AVAILABLE' if result.volume_ratio20 is not None else 'MISSING_VOLUME')
        if history.current_conflict:
            result.feature_readiness = {key: 'CONFLICTING' for key in result.feature_readiness}
        elif stale:
            result.feature_readiness = {key: 'STALE' if value == 'AVAILABLE' else value for key, value in result.feature_readiness.items()}
        result.ohlcv_feature_coverage = 100 * sum(value is not None for value in
            (result.atr14, result.adx14, result.volume_ratio20)) / 3
        feature_names = ["latest_price", "dma20", "dma50", "dma100", "dma200", "rsi14", "macd", "macd_signal",
                         "macd_histogram", "adx14", "atr14", "atr_pct", "trend_slope20", "trend_slope50",
                         "return1_w", "return1_m", "return3_m", "return6_m", "return1_y",
                         "volume_average20", "volume_ratio20", "support_level", "resistance_level",
                         "distance_to_support_pct", "distance_to_resistance_pct", "higher_highs_higher_lows",
                         "lower_highs_lower_lows", "distance_from52_week_high_pct", "distance_from52_week_low_pct",
                         *(f"distance_to_dma{p}_pct" for p in (20, 50, 100, 200))]
        for name in feature_names:
            alias = TechnicalFeatureSnapshot.model_fields[name].alias or name
            value = getattr(result, name)
            result.feature_states[alias] = ("CONFLICTING" if history.current_conflict else
                "MISSING" if value is None else "STALE" if stale else "AVAILABLE")
            if value is None:
                result.missing_inputs.append(alias)
        if not use_daily or 'MISSING_OHLC' in result.feature_readiness.values():
            result.missing_inputs.append("PERSISTED_OHLC")
        if result.volume_average20 is None:
            result.missing_inputs.append("PERSISTED_VOLUME_HISTORY")
        core = ["dma20", "dma50", "dma100", "dma200", "rsi14", "macd_signal", "return1_m", "return3_m",
                "return6_m", "return1_y", "trend_slope20", "trend_slope50"]
        coverage = sum(getattr(result, field) is not None for field in core) / len(core)
        conflict_factor = n / (n + len(history.conflicting_dates)) if n else 0
        result.confidence = round(100 * coverage * conflict_factor * (0.5 if stale else 1), 6) if usable else 0
        result.missing_inputs = sorted(set(result.missing_inputs))
        # Rounding is only at the output boundary, after state/score decisions.
        for name in TechnicalFeatureSnapshot.model_fields:
            value = getattr(result, name)
            if isinstance(value, float):
                setattr(result, name, round(value, 8))
        return result

    def _candles(self, result, rows, conflicts):
        # Restart warmup after a missing/invalid candle or a rejected date;
        # never bridge the missing dependency with another source's close.
        suffix = []
        cutoff = max(conflicts) if conflicts else None
        missing = bool(conflicts)
        for row in rows:
            values = [finite_number(getattr(row, key)) for key in ('open', 'high', 'low', 'close')]
            if (cutoff is not None and row.trading_date <= cutoff) or any(v is None or v <= 0 for v in values) or row.high < row.low:
                suffix = []
                missing = True
            else:
                suffix.append(row)
        result.atr14, result.adx14 = _atr_adx(suffix)
        if result.atr14 is not None:
            result.atr_pct = 100 * result.atr14 / result.latest_price
        for name, value in [('ATR14', result.atr14), ('ADX14', result.adx14)]:
            result.feature_readiness[name] = 'AVAILABLE' if value is not None else 'MISSING_OHLC' if missing else 'INSUFFICIENT_HISTORY'
        result.current_volume = rows[-1].volume
        result.feature_readiness['VOLUME20'] = 'INSUFFICIENT_HISTORY'
        if len(rows) >= 21:
            recent = rows[-21:]
            # Do not compress conflicted dates out of a volume baseline.
            if any(recent[0].trading_date <= day <= recent[-1].trading_date for day in conflicts):
                result.feature_readiness['VOLUME20'] = 'CONFLICTING'
            elif any(row.volume is None for row in recent[:-1]):
                result.feature_readiness['VOLUME20'] = 'MISSING_VOLUME'
            else:
                average = sum(Decimal(row.volume) for row in recent[:-1]) / 20
                result.volume_average20 = float(average)
                if recent[-1].volume is None:
                    result.feature_readiness['VOLUME20'] = 'MISSING_VOLUME'
                elif average == 0:
                    result.feature_readiness['VOLUME20'] = 'ZERO_BASELINE'
                    result.missing_inputs.append('NONZERO_VOLUME_BASELINE')
                else:
                    result.volume_ratio20 = float(Decimal(recent[-1].volume) / average)
                    result.feature_readiness['VOLUME20'] = 'AVAILABLE'

    def _volume_signals(self, result):
        ratio = result.volume_ratio20
        if ratio is not None:
            result.volume_expansion = ratio >= self.config.volume_confirmation_ratio
            result.volume_contraction = ratio <= self.config.volume_contraction_ratio
            result.volume_state = 'EXPANSION' if result.volume_expansion else 'CONTRACTION' if result.volume_contraction else 'NORMAL'
        price_signal = result.breakout_state in {'PRICE_BREAKOUT', 'VOLUME_CONFIRMED'}
        reversal = result.technical_state == 'REVERSAL_CANDIDATE'
        if ratio is not None and price_signal:
            result.breakout_volume_confirmed = result.volume_expansion
        if ratio is not None and reversal:
            result.reversal_volume_confirmed = result.volume_expansion
        result.feature_readiness['VOLUME_CONFIRMATION'] = ('NOT_APPLICABLE' if not (price_signal or reversal)
            else 'MISSING_VOLUME' if ratio is None else 'AVAILABLE')

    def _volume(self, result, prices, volumes, trusted_providers):
        grouped = defaultdict(list)
        for row in volumes:
            number = finite_number(row.volume)
            if (row.instrument_id == result.global_instrument_id and number is not None and number >= 0
                    and utc(row.observed_at) <= result.as_of and utc(row.retrieved_at) <= result.as_of
                    and (trusted_providers is None or row.provider in trusted_providers)):
                grouped[utc(row.observed_at).date()].append(row)
        daily = {}
        for day, rows in sorted(grouped.items()):
            latest = max(utc(row.observed_at) for row in rows)
            values = {row.volume for row in rows if utc(row.observed_at) == latest}
            if len(values) == 1:
                daily[day] = float(next(iter(values)))
            else:
                result.missing_inputs.append(f"CONFLICTING_VOLUME:{day.isoformat()}")
        # Prior 20 completed observations; current volume never enters its own baseline.
        if prices:
            current = daily.get(utc(prices[-1].observed_at).date())
            result.current_volume = Decimal(str(current)) if current is not None else None
        if len(prices) >= 21:
            days = [utc(row.observed_at).date() for row in prices[-21:-1]]
            if all(day in daily for day in days):
                result.volume_average20 = sum(daily[day] for day in days) / 20
                latest = daily.get(utc(prices[-1].observed_at).date())
                if latest is not None and result.volume_average20 > 0:
                    result.volume_ratio20 = latest / result.volume_average20
                elif result.volume_average20 == 0:
                    result.missing_inputs.append("NONZERO_VOLUME_BASELINE")

    def _classify(self, r, prices):
        cfg, price = self.config, prices[-1]
        broad_up = (r.dma50 is not None and r.trend_slope50 > cfg.slope_threshold_pct
                    and (r.dma200 is None or r.dma50 > r.dma200))
        breakout = r.resistance_level is not None and percentage(price, r.resistance_level) > cfg.breakout_buffer_pct
        breakdown = r.support_level is not None and percentage(price, r.support_level) < -cfg.breakout_buffer_pct
        r.breakout_state = ("VOLUME_CONFIRMED" if breakout and r.volume_ratio20 is not None and r.volume_ratio20 >= cfg.volume_confirmation_ratio
                            else "PRICE_BREAKOUT" if breakout else "PRICE_BREAKDOWN" if breakdown else "NONE")
        if r.distance_to_dma20_pct >= cfg.extension_distance_pct and r.rsi14 >= cfg.extension_rsi:
            r.technical_state = "OVEREXTENDED"
        elif breakout:
            r.technical_state = "BREAKOUT"
        elif (broad_up and price > r.dma50 and percentage(price, max(prices[-6:-1])) <= -cfg.pullback_retreat_pct
              and min(abs(r.distance_to_dma20_pct), abs(r.distance_to_dma50_pct)) <= cfg.pullback_proximity_pct):
            r.technical_state = "PULLBACK_IN_UPTREND"
        elif (r.trend_slope50 is not None and r.trend_slope50 < -cfg.slope_threshold_pct
              and r.trend_slope20 > cfg.slope_threshold_pct and price > r.dma20):
            r.technical_state = "REVERSAL_CANDIDATE"
        elif broad_up and price > r.dma50:
            r.technical_state = "UPTREND"
        elif r.dma50 is not None and price < r.dma50 and r.trend_slope50 < -cfg.slope_threshold_pct:
            r.technical_state = "DOWNTREND"
        elif abs(r.trend_slope20) <= cfg.slope_threshold_pct and percentage(max(prices[-20:]), min(prices[-20:])) <= cfg.base_range_pct:
            r.technical_state = "BASE_BUILDING"
        else:
            r.technical_state = "RANGE_BOUND"

    def _score(self, r):
        cfg = self.config
        alignments = [percentage(r.latest_price, d) for d in (r.dma20, r.dma50) if d is not None]
        if r.dma200 is not None:
            alignments.append(percentage(r.dma50, r.dma200))
        components = {"trendAlignment": sum(100 if v > 0 else 0 if v < 0 else 50 for v in alignments) / len(alignments)}
        momentum = [r.rsi14] if r.rsi14 is not None else []
        if r.return1_m is not None:
            momentum.append(_clamp(50 + 50 * r.return1_m / cfg.momentum_full_scale_pct))
        if momentum:
            components["momentum"] = sum(momentum) / len(momentum)
        if r.support_level is not None:
            span = r.resistance_level - r.support_level
            components["pricePosition"] = _clamp(100 * (r.latest_price - r.support_level) / span) if span else 50.0
        weights = dict(zip(("trendAlignment", "momentum", "pricePosition"), cfg.score_weights))
        total = sum(weights[key] for key in components)
        score = sum(value * weights[key] for key, value in components.items()) / total if total else None
        if score is not None:
            if r.technical_state == "OVEREXTENDED":
                score -= cfg.extension_penalty
            if r.breakout_state == "VOLUME_CONFIRMED":
                score += cfg.volume_breakout_bonus
            r.technical_score = round(_clamp(score), 8)
        r.score_components = {key: round(value, 8) for key, value in components.items()}
