"""Pure, date-aligned stock/sector/market relative returns.

Option B: missing durable benchmark mappings/history remain unavailable. A
sector performer leaderboard is not a sector index and is never substituted.
Comparisons are local-currency price returns (no invented FX conversion).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Iterable, Literal, Mapping
from uuid import UUID

from pydantic import Field

from app.models import DailyMarketBar, MarketPriceObservation, ResearchBaseModel
from app.sector_leaderboard import normalize_sector
from app.technical_features import normalize_price_history, normalize_daily_history, percentage, utc
from zoneinfo import ZoneInfo


SECTOR_FEATURE_VERSION = "SECTOR_RELATIVE_STRENGTH_V1"


@dataclass(frozen=True)
class BenchmarkReference:
    instrument_id: UUID
    currency: str
    trusted_providers: frozenset[str] | None = None


@dataclass(frozen=True)
class SectorContext:
    """Authoritative classification and explicit canonical mappings, supplied by caller.

    India uses canonicalSector from the persisted NSE universe. Other markets
    may use their persisted canonical classification. No company-name inference.
    source/as_of retain classification provenance; future metadata is rejected.
    """
    sector: str | None = None
    source: str | None = None
    as_of: datetime | None = None
    region: str | None = None
    sector_benchmark: BenchmarkReference | None = None
    market_benchmark: BenchmarkReference | None = None
    mapping_version: str | None = None
    sector_mapping_status: str | None = None
    market_mapping_status: str | None = None


@dataclass(frozen=True)
class SectorRelativeStrengthConfig:
    return_lookbacks: tuple[int, ...] = (5, 21, 63, 126)
    period_weights: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    max_age_days: int = 7
    # Normalize horizon lengths before classifying direction: 0.02 percentage
    # points/observed session is a configurable noise band, not a forecast.
    material_edge_per_observation_pct: float = 0.02
    improvement_per_observation_pct: float = 0.02
    # An average 0.2pp/session edge reaches score100, -0.2 reaches zero.
    full_scale_edge_per_observation_pct: float = 0.2

    def __post_init__(self):
        if (len(self.return_lookbacks) != 4 or any(n < 1 for n in self.return_lookbacks)
                or tuple(sorted(set(self.return_lookbacks))) != self.return_lookbacks
                or len(self.period_weights) != 4 or sum(self.period_weights) <= 0 or self.max_age_days < 1
                or self.full_scale_edge_per_observation_pct <= 0):
            raise ValueError("Invalid sector configuration")
        for value in asdict(self).values():
            if any(not isfinite(v) or v < 0 for v in (value if isinstance(value, tuple) else (value,))):
                raise ValueError("Sector thresholds/weights must be finite and nonnegative")


class SectorRelativeStrengthSnapshot(ResearchBaseModel):
    global_instrument_id: UUID
    as_of: datetime
    feature_version: str = SECTOR_FEATURE_VERSION
    configuration: dict
    benchmark_mapping_version: str | None = None
    benchmark_states: dict[str, str] = Field(default_factory=dict)
    history_sources: dict[str, str] = Field(default_factory=dict)
    sector: str | None = None
    classification_source: str | None = None
    classification_as_of: datetime | None = None
    region: str | None = None
    sector_benchmark_id: UUID | None = None
    market_benchmark_id: UUID | None = None
    return_basis: str = "DATE_ALIGNED_LOCAL_CURRENCY_PRICE_RETURN_PCT"
    stock_return1_w: float | None = Field(default=None, alias="stockReturn1W")
    stock_return1_m: float | None = Field(default=None, alias="stockReturn1M")
    stock_return3_m: float | None = Field(default=None, alias="stockReturn3M")
    stock_return6_m: float | None = Field(default=None, alias="stockReturn6M")
    sector_return1_w: float | None = Field(default=None, alias="sectorReturn1W")
    sector_return1_m: float | None = Field(default=None, alias="sectorReturn1M")
    sector_return3_m: float | None = Field(default=None, alias="sectorReturn3M")
    sector_return6_m: float | None = Field(default=None, alias="sectorReturn6M")
    market_return1_w: float | None = Field(default=None, alias="marketReturn1W")
    market_return1_m: float | None = Field(default=None, alias="marketReturn1M")
    market_return3_m: float | None = Field(default=None, alias="marketReturn3M")
    market_return6_m: float | None = Field(default=None, alias="marketReturn6M")
    relative_vs_sector1_w: float | None = Field(default=None, alias="relativeVsSector1W")
    relative_vs_sector1_m: float | None = Field(default=None, alias="relativeVsSector1M")
    relative_vs_sector3_m: float | None = Field(default=None, alias="relativeVsSector3M")
    relative_vs_sector6_m: float | None = Field(default=None, alias="relativeVsSector6M")
    relative_vs_market1_w: float | None = Field(default=None, alias="relativeVsMarket1W")
    relative_vs_market1_m: float | None = Field(default=None, alias="relativeVsMarket1M")
    relative_vs_market3_m: float | None = Field(default=None, alias="relativeVsMarket3M")
    relative_vs_market6_m: float | None = Field(default=None, alias="relativeVsMarket6M")
    comparison_windows: dict[str, tuple[date, date]] = Field(default_factory=dict)
    feature_states: dict[str, str] = Field(default_factory=dict)
    sector_state: Literal["LEADING", "IMPROVING", "NEUTRAL", "WEAKENING", "LAGGING", "INSUFFICIENT_DATA"] = "INSUFFICIENT_DATA"
    relative_strength_score: float | None = None
    confidence: float = 0
    missing_inputs: list[str] = Field(default_factory=list)
    stale_inputs: list[str] = Field(default_factory=list)


class SectorRelativeStrengthEngine:
    def __init__(self, config: SectorRelativeStrengthConfig | None = None):
        self.config = config or SectorRelativeStrengthConfig()

    def compute(self, instrument_id: UUID, stock_history: Iterable[MarketPriceObservation], *, as_of: datetime,
                context: SectorContext | None = None, currency: str | None = None,
                benchmark_histories: Mapping[UUID, Iterable[MarketPriceObservation]] | None = None,
                trusted_providers: frozenset[str] | None = None,
                daily_bar_histories: Mapping[UUID, Iterable[DailyMarketBar]] | None = None) -> SectorRelativeStrengthSnapshot:
        cfg, context = self.config, context or SectorContext()
        histories = benchmark_histories or {}
        daily = daily_bar_histories or {}
        classification_valid = bool(context.sector and context.source and context.as_of is not None and utc(context.as_of) <= utc(as_of))
        result = SectorRelativeStrengthSnapshot(global_instrument_id=instrument_id, as_of=utc(as_of), configuration=asdict(cfg),
            sector=normalize_sector(context.sector)[1] if classification_valid else None,
            classification_source=context.source if classification_valid else None,
            classification_as_of=utc(context.as_of) if classification_valid else None, region=context.region,
            sector_benchmark_id=context.sector_benchmark.instrument_id if context.sector_benchmark and classification_valid else None,
            market_benchmark_id=context.market_benchmark.instrument_id if context.market_benchmark else None)
        result.benchmark_mapping_version = context.mapping_version
        if not classification_valid:
            result.missing_inputs.append("AUTHORITATIVE_SECTOR_CLASSIFICATION")
        stock_dates, stock_conflict, stock_stale, source = _dated_history(instrument_id, stock_history,
            daily.get(instrument_id, ()), as_of, currency, trusted_providers, cfg.max_age_days, stock=True)
        result.history_sources['stock'] = source
        if stock_stale:
            result.stale_inputs.append("STOCK_HISTORY")
        if stock_conflict:
            result.missing_inputs.append("CONFLICTING_STOCK_PRICE")
        benchmark_data = {}
        for name, reference in (("sector", context.sector_benchmark if classification_valid else None), ("market", context.market_benchmark)):
            if reference is None:
                result.missing_inputs.append(f"{name.upper()}_BENCHMARK_MAPPING")
                result.benchmark_states[name] = ('NO_SECTOR_CLASSIFICATION' if name == 'sector' and not classification_valid
                    else getattr(context, f'{name}_mapping_status') or
                    ('UNMAPPED_SECTOR_BENCHMARK' if name == 'sector' else 'BENCHMARK_IDENTITY_UNAVAILABLE'))
                continue
            if reference.instrument_id == instrument_id or not reference.currency:
                result.missing_inputs.append(f"INVALID_{name.upper()}_BENCHMARK_MAPPING")
                result.benchmark_states[name] = 'BENCHMARK_IDENTITY_UNAVAILABLE'
                continue
            dates, conflict, stale, source = _dated_history(reference.instrument_id, histories.get(reference.instrument_id, ()),
                daily.get(reference.instrument_id, ()), as_of, reference.currency, reference.trusted_providers, cfg.max_age_days)
            result.history_sources[name] = source
            if not dates or conflict:
                result.missing_inputs.append(f"{name.upper()}_HISTORY" if not conflict else f"CONFLICTING_{name.upper()}_PRICE")
                result.benchmark_states[name] = 'BENCHMARK_HISTORY_UNAVAILABLE'
                continue
            if stale:
                result.stale_inputs.append(f"{name.upper()}_HISTORY")
                result.benchmark_states[name] = 'STALE_BENCHMARK_HISTORY'
                continue
            benchmark_data[name] = dates
            result.benchmark_states[name] = 'INSUFFICIENT_OVERLAP'
        edges = {}
        for suffix, lookback, weight in zip(("1_w", "1_m", "3_m", "6_m"), cfg.return_lookbacks, cfg.period_weights):
            if not stock_conflict and len(stock_dates) > lookback:
                ordered = sorted(stock_dates)
                dates = ordered[-lookback-1], ordered[-1]
                stock_return = percentage(stock_dates[dates[1]], stock_dates[dates[0]])
                setattr(result, f"stock_return{suffix}", stock_return)
                result.comparison_windows[suffix.replace("_", "").upper()] = dates
                period_edges = []
                for name in ("sector", "market"):
                    rows = benchmark_data.get(name, {})
                    if not stock_stale and dates[0] in rows and dates[1] in rows:
                        value = percentage(rows[dates[1]], rows[dates[0]])
                        setattr(result, f"{name}_return{suffix}", value)
                        setattr(result, f"relative_vs_{name}{suffix}", stock_return - value)
                        period_edges.append((stock_return - value) / lookback)
                        result.benchmark_states[name] = 'AVAILABLE'
                    elif rows and not stock_stale:
                        result.missing_inputs.append(f"{name.upper()}_ALIGNED_DATES_{suffix.replace('_', '').upper()}")
                if period_edges:
                    edges[suffix] = (sum(period_edges) / len(period_edges), weight)
        # Require at least two horizons to claim consistency. Missing benchmark
        # legs are omitted, never assigned a zero relative return.
        if len(edges) >= 2 and sum(weight for _, weight in edges.values()) > 0:
            average = sum(edge * weight for edge, weight in edges.values()) / sum(weight for _, weight in edges.values())
            result.relative_strength_score = min(100, max(0, 50 + 50 * average / cfg.full_scale_edge_per_observation_pct))
            # sectorState describes the stock's relative leadership with sector
            # context. Market-only evidence may score partially, but is not a sector state.
            sector_periods = sum(getattr(result, f"relative_vs_sector{s}") is not None for s in ("1_w", "1_m", "3_m", "6_m"))
            if sector_periods >= 2:
                values = [v for v, _ in edges.values()]
                if all(v > cfg.material_edge_per_observation_pct for v in values):
                    result.sector_state = "LEADING"
                elif all(v < -cfg.material_edge_per_observation_pct for v in values):
                    result.sector_state = "LAGGING"
                else:
                    short = [edges[s][0] for s in ("1_w", "1_m") if s in edges]
                    long = [edges[s][0] for s in ("3_m", "6_m") if s in edges]
                    change = sum(short)/len(short) - sum(long)/len(long) if short and long else 0
                    result.sector_state = ("IMPROVING" if change > cfg.improvement_per_observation_pct else
                                           "WEAKENING" if change < -cfg.improvement_per_observation_pct else "NEUTRAL")
        relative_fields = []
        for name, field in SectorRelativeStrengthSnapshot.model_fields.items():
            if name.startswith(("stock_return", "sector_return", "market_return", "relative_vs_")):
                value = getattr(result, name)
                alias = field.alias or name
                result.feature_states[alias] = "MISSING" if value is None else "STALE" if stock_stale else "AVAILABLE"
                if value is None:
                    result.missing_inputs.append(alias)
                else:
                    setattr(result, name, round(value, 8))
                if name.startswith("relative_vs_"):
                    relative_fields.append(value)
        result.confidence = round(100 * sum(v is not None for v in relative_fields) / 8, 6)
        if result.relative_strength_score is not None:
            result.relative_strength_score = round(result.relative_strength_score, 8)
        result.missing_inputs = sorted(set(result.missing_inputs))
        result.stale_inputs = sorted(set(result.stale_inputs))
        return result


def _dated_history(key, closes, bars, as_of, currency, providers, max_age, stock=False):
    """Keep daily DATEs intact; no timestamp shift, filling, or nearest-date match."""
    history, present = normalize_daily_history(key, bars, as_of=as_of, currency=currency, trusted_providers=providers)
    today = utc(as_of).astimezone(ZoneInfo('Asia/Kolkata')).date()
    stale = bool(history.observations) and today - history.observations[-1].trading_date > timedelta(days=max_age)
    fallback = None
    if present and stock and not history.current_conflict and (len(history.observations) < 20 or stale):
        fallback = normalize_price_history(key, closes, as_of=as_of, currency=currency, trusted_providers=providers)
        if (len(fallback.observations) >= 20 and not fallback.current_conflict and
                utc(as_of) - utc(fallback.observations[-1].observed_at) <= timedelta(days=max_age)):
            present = False
    if present:
        return ({row.trading_date:float(row.close) for row in history.observations}, history.current_conflict,
            stale, 'DAILY_MARKET_BAR_NSE')
    fallback = fallback or normalize_price_history(key, closes, as_of=as_of, currency=currency, trusted_providers=providers)
    stale = bool(fallback.observations) and utc(as_of) - utc(fallback.observations[-1].observed_at) > timedelta(days=max_age)
    return ({utc(row.observed_at).date():float(row.price) for row in fallback.observations},
        fallback.current_conflict, stale, 'CLOSE_ONLY_FALLBACK')
