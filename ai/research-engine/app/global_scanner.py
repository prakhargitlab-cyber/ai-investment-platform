"""Provider-free candidate selection. This is not an investment score or V1 analysis.

Scores use equal-weight available dimensions, renormalized over evidence only.
Confidence measures coverage/readiness, independently of positive/negative values.
The explicit as_of clock is part of the reproducible input state.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx
from app.models import ResearchBaseModel
from app.fact_precedence import SUPPORTED_FINANCIAL_SOURCE_TIERS
from app.persistence import ResearchPersistence
from app.technical_features import TechnicalFeatureEngine, TechnicalFeatureSnapshot
from app.sector_relative_strength import SectorContext, SectorRelativeStrengthEngine, SectorRelativeStrengthSnapshot


class EvidenceState(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PreScoreDimension(ResearchBaseModel):
    state: EvidenceState
    score: float | None = None
    coverage: float = 0


class GlobalScanCandidate(ResearchBaseModel):
    global_instrument_id: UUID
    market: str | None = None
    region: str | None = None
    country: str | None = None
    exchange: str | None = None
    currency: str | None = None
    asset_type: str | None = None
    status: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    verified_provider_mapping_status: str
    pre_score: float | None
    confidence: float
    critical_completeness: float
    price_data_available: bool
    financial_data_available: bool
    technical_history_available: bool
    sector_data_available: bool
    dimensions: dict[str, PreScoreDimension]
    missing_inputs: list[str]
    stale_inputs: list[str]
    eligible_for_deep_analysis: bool
    exclusion_reasons: list[str]


class GlobalScanResult(ResearchBaseModel):
    as_of: datetime
    pre_score_version: str = "GLOBAL_PRE_SCORE_V1"
    total_canonical_active_equities: int
    eligible_candidates: int
    excluded_candidates_by_reason: dict[str, int]
    deep_analysis_eligible_count: int
    candidates: list[GlobalScanCandidate]
    top_candidates: list[GlobalScanCandidate]
    deep_analysis_candidate_ids: list[UUID]


class StageBCandidate(ResearchBaseModel):
    global_instrument_id: UUID
    symbol: str | None
    pre_score: float | None
    feature_version: str = "GLOBAL_STAGE_B_V1"
    technical_feature_snapshot: TechnicalFeatureSnapshot
    sector_relative_strength_snapshot: SectorRelativeStrengthSnapshot
    technical_score: float | None
    sector_score: float | None
    stage_b_score: float | None
    confidence: float
    score_coverage: float
    score_weights: dict[str, float]


class EquityUniverse(Protocol):
    async def active_global_equities(self, **kwargs) -> list[dict]: ...


class CanonicalEquityUniverse:
    """Read canonical metadata only; no portfolio ownership or provider acquisition."""

    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client, self.base_url = client, base_url.rstrip("/")

    async def active_global_equities(self, *, correlation_id=None, identity_headers=None):
        headers = {k: v for k, v in (identity_headers or {}).items() if v}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        values, page = [], 0
        while True:
            response = await self.client.get(
                f"{self.base_url}/api/v1/instruments",
                params={"status": "ACTIVE", "assetType": "EQUITY", "page": page, "size": 500},
                headers=headers or None,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
                raise ValueError("Invalid canonical universe response")
            batch = payload["instruments"]
            total = int(payload.get("totalElements", len(values) + len(batch)))
            values.extend(batch)
            if len(values) >= total:
                return values
            if not batch:
                raise ValueError("Incomplete canonical universe pagination")
            page += 1


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


class GlobalPreScore:
    """Cheap readiness and sign heuristics; no valuation targets or V1 formulas.

    Critical inputs are a fresh trusted price and at least one fresh profitability
    metric. Optional dimensions never supply a synthetic zero. Quality metrics
    use only sign (positive=100, actual zero=50, negative=0), avoiding unit-dependent
    thresholds. No financial ratios or technical indicators are derived here.
    """

    def __init__(self, *, price_max_age=timedelta(days=7), financial_max_age=timedelta(days=550), history_points=50):
        if price_max_age.total_seconds() <= 0 or financial_max_age.total_seconds() <= 0 or history_points < 2:
            raise ValueError("Readiness limits must be positive; history_points must be >= 2")
        self.price_max_age = price_max_age
        self.financial_max_age = financial_max_age
        self.history_points = history_points

    def score(self, instrument, snapshots, facts, prices, *, as_of):
        as_of = _utc(as_of)
        missing, stale = set(), set()
        evidence = defaultdict(list)
        currency = instrument.get("currency")
        mappings = [m for m in instrument.get("providerMappings", []) if isinstance(m, dict)
                    and m.get("status") == "VERIFIED" and m.get("provider")
                    and (m.get("providerInstrumentId") or m.get("providerSymbol"))
                    and m.get("resolutionSource") != "BROKER_IMPORT_IDENTITY"]
        providers = {m["provider"] for m in mappings}
        for record in snapshots:
            mapped_ids = {str(m.get(key)) for m in mappings if m["provider"] == record.provider
                          for key in ("providerInstrumentId", "providerSymbol") if m.get(key)}
            if (record.provider not in providers or _utc(record.retrieved_at) > as_of
                    or record.provider_instrument_id not in mapped_ids or record.currency != currency):
                continue
            for key, value in record.snapshot.facts.items():
                if _utc(value.retrieved_at) > as_of:
                    continue
                stamp = value.as_of_date or value.published_at or record.market_as_of or record.retrieved_at
                if _utc(stamp) <= as_of:
                    evidence[key].append((value.value, _utc(stamp)))
        financial_evidence = defaultdict(list)
        for fact in facts:
            if fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS or str(fact.source_mode) != "REAL" or _utc(fact.value.retrieved_at) > as_of:
                continue
            stamp = fact.value.as_of_date or fact.value.published_at or fact.value.retrieved_at
            if fact.key.period_end:
                try:
                    stamp = datetime.fromisoformat(fact.key.period_end)
                except ValueError:
                    continue
            if _utc(stamp) <= as_of:
                metric = {"profit_margin": "profitMargin", "return_on_equity": "roe",
                          "net_income": "pat", "net_profit": "pat", "revenue_growth": "revenueGrowth",
                          "earnings_growth": "earningsGrowth", "equity": "total_equity"}.get(fact.key.metric, fact.key.metric)
                # Annual and quarterly amounts (or standalone and consolidated
                # statements) are not conflicting observations of one metric.
                basis_rank = {"CONSOLIDATED": 2, "UNKNOWN": 1}.get(fact.key.reporting_basis, 0)
                period_rank = {"ANNUAL": 2, "QUARTERLY": 1}.get(fact.key.period_type, 0)
                financial_evidence[metric].append((fact.value.value, _utc(stamp), basis_rank, period_rank))
        for metric, values in financial_evidence.items():
            selected = max((stamp, basis, period) for _, stamp, basis, period in values)
            evidence[metric].extend((value, stamp) for value, stamp, basis, period in values
                                    if (stamp, basis, period) == selected)

        def dimension(keys, *, quality=False, text=False, max_age=None):
            scores, ready, conflicts, old = [], 0, False, False
            for key in keys:
                values = evidence.get(key, [])
                valid = [(str(v).strip() if text and v is not None else _number(v), t) for v, t in values]
                valid = [(v, t) for v, t in valid if v is not None and v != ""]
                if not valid:
                    missing.add(key)
                    continue
                latest = max(t for _, t in valid)
                current = {v for v, t in valid if t == latest}
                if len(current) > 1:
                    conflicts = True
                    continue
                value = next(iter(current))
                is_stale = as_of - latest > (max_age or self.financial_max_age)
                if is_stale:
                    stale.add(key)
                    old = True
                else:
                    ready += 1
                scores.append((100 if value > 0 else 50 if value == 0 else 0) if quality else 100)
            state = (EvidenceState.CONFLICTING if conflicts else EvidenceState.STALE if old else
                     EvidenceState.MISSING if not scores else EvidenceState.PARTIAL if len(scores) < len(keys)
                     else EvidenceState.AVAILABLE)
            return PreScoreDimension(state=state, score=sum(scores)/len(scores) if scores and not conflicts else None,
                                     coverage=ready/len(keys) if not conflicts else 0)

        usable = [p for p in prices if p.provider in providers and _number(p.price) is not None and p.price > 0
                  and currency and p.currency == currency and _utc(p.observed_at) <= as_of and _utc(p.retrieved_at) <= as_of]
        latest = max((_utc(p.observed_at) for p in usable), default=None)
        price_conflict = len({p.price for p in usable if _utc(p.observed_at) == latest}) > 1
        fresh_price = latest is not None and as_of - latest <= self.price_max_age and not price_conflict
        price_state = (EvidenceState.CONFLICTING if price_conflict else EvidenceState.MISSING if latest is None
                       else EvidenceState.AVAILABLE if fresh_price else EvidenceState.STALE)
        if latest is None:
            missing.add("price")
        elif not fresh_price and not price_conflict:
            stale.add("price")
        days = len({_utc(p.observed_at).date() for p in usable})
        dimensions = {
            "PRICE_DATA_QUALITY": PreScoreDimension(state=price_state, score=100 if usable and not price_conflict else None, coverage=float(fresh_price)),
            "VALUATION_AVAILABILITY": dimension(["trailingPE", "priceToBook"], max_age=self.price_max_age),
            "PROFITABILITY_QUALITY": dimension(["profitMargin", "roe", "pat"], quality=True),
            "GROWTH_QUALITY": dimension(["revenueGrowth", "earningsGrowth"], quality=True),
            "BALANCE_SHEET_QUALITY": dimension(["total_equity"], quality=True),
            "TECHNICAL_DATA_READINESS": PreScoreDimension(state=EvidenceState.MISSING if not days else EvidenceState.STALE if not fresh_price else EvidenceState.AVAILABLE if days >= self.history_points else EvidenceState.PARTIAL,
                score=100 if days >= self.history_points else None, coverage=min(1, days/self.history_points) if fresh_price else 0),
            "SECTOR_DATA_READINESS": dimension(["sector"], text=True),
        }
        if days < self.history_points:
            missing.add("technicalHistory")
        profitability = dimensions["PROFITABILITY_QUALITY"]
        financial_available = profitability.score is not None
        financial_ready = profitability.coverage > 0 and profitability.state in {"AVAILABLE", "PARTIAL"}
        critical = (int(fresh_price) + int(financial_ready)) / 2
        readiness = sum(d.coverage for d in dimensions.values()) / len(dimensions)
        dimensions["FRESHNESS"] = PreScoreDimension(state=EvidenceState.STALE if stale else EvidenceState.AVAILABLE,
            score=None, coverage=readiness)
        dimensions["CRITICAL_COMPLETENESS"] = PreScoreDimension(state=EvidenceState.AVAILABLE if critical == 1 else EvidenceState.PARTIAL if critical else EvidenceState.MISSING,
            score=None, coverage=critical)
        symbol = instrument.get("ticker") or instrument.get("primarySymbol")
        name = instrument.get("canonicalName")
        reasons = []
        if instrument.get("status") != "ACTIVE": reasons.append("INACTIVE")
        if instrument.get("assetType") != "EQUITY": reasons.append("NON_EQUITY")
        if not (mappings and symbol and name and instrument.get("exchange") and currency): reasons.append("UNTRUSTED_CANONICAL_IDENTITY")
        if not usable: reasons.append("NO_USABLE_PRICE")
        elif price_conflict: reasons.append("CONFLICTING_PRICE")
        elif not fresh_price: reasons.append("STALE_PRICE")
        if not financial_ready: reasons.append("CRITICAL_FINANCIAL_EVIDENCE_UNREADY")
        scores = [d.score for d in dimensions.values() if d.score is not None and d.state in {"AVAILABLE", "PARTIAL"}]
        return GlobalScanCandidate(global_instrument_id=instrument["globalInstrumentId"], market=instrument.get("exchange"),
            region=instrument.get("region"), country=instrument.get("country"), exchange=instrument.get("exchange"),
            currency=currency, asset_type=instrument.get("assetType"), status=instrument.get("status"), symbol=symbol, company_name=name,
            verified_provider_mapping_status="VERIFIED" if mappings else "MISSING", pre_score=round(sum(scores)/len(scores), 6) if scores else None,
            confidence=round(100*readiness*critical, 6), critical_completeness=critical*100,
            price_data_available=bool(usable) and not price_conflict, financial_data_available=financial_available,
            technical_history_available=days >= self.history_points, sector_data_available=dimensions["SECTOR_DATA_READINESS"].score is not None,
            dimensions=dimensions, missing_inputs=sorted(missing), stale_inputs=sorted(stale),
            eligible_for_deep_analysis=not reasons, exclusion_reasons=reasons)


class GlobalScanner:
    def __init__(self, universe: EquityUniverse, persistence: ResearchPersistence, *, pre_score=None, batch_size=250):
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        self.universe, self.persistence = universe, persistence
        self.pre_score, self.batch_size = pre_score or GlobalPreScore(), batch_size

    def enrich_candidates(self, scan: GlobalScanResult, *, sector_contexts: dict[UUID, SectorContext] | None = None,
                          trusted_providers: dict[UUID, frozenset[str]] | None = None,
                          technical_engine: TechnicalFeatureEngine | None = None,
                          sector_engine: SectorRelativeStrengthEngine | None = None,
                          technical_weight: float = 0.7, sector_weight: float = 0.3) -> list[StageBCandidate]:
        """Optional Stage B; no universe enumeration, providers, refresh or V1.

        Enrich all deep-eligible candidates (not the entire canonical universe).
        Prices for candidates and explicitly mapped benchmarks share bounded SQL
        batches. Default weights emphasize technical evidence; they are selection
        defaults, not calibrated investment weights. Missing legs renormalize the
        score while reducing reported coverage/confidence. Phase 1 is untouched.
        """
        from math import isfinite

        if (not all(isfinite(v) and v >= 0 for v in (technical_weight, sector_weight))
                or technical_weight + sector_weight == 0):
            raise ValueError("Stage-B weights must be finite, nonnegative and have positive sum")
        technical_engine = technical_engine or TechnicalFeatureEngine()
        sector_engine = sector_engine or SectorRelativeStrengthEngine()
        contexts, providers = sector_contexts or {}, trusted_providers or {}
        candidates = [c for c in scan.candidates if c.eligible_for_deep_analysis]
        ids = {c.global_instrument_id for c in candidates}
        daily_histories = defaultdict(list)
        candidate_ids = sorted(ids, key=str)
        for offset in range(0, len(candidate_ids), self.batch_size):
            batch = set(candidate_ids[offset:offset+self.batch_size])
            for row in self.persistence.load_daily_market_bars(batch, provider="NSE"):
                if row.global_instrument_id in batch:
                    daily_histories[row.global_instrument_id].append(row)
        for candidate in candidates:
            context = contexts.get(candidate.global_instrument_id, SectorContext())
            for reference in (context.sector_benchmark, context.market_benchmark):
                if reference:
                    ids.add(reference.instrument_id)
        histories = defaultdict(list)
        ordered = sorted(ids, key=str)
        for offset in range(0, len(ordered), self.batch_size):
            batch = set(ordered[offset:offset+self.batch_size])
            for row in self.persistence.load_market_price_observations(batch):
                if row.instrument_id in batch:
                    histories[row.instrument_id].append(row)
        output = []
        total_weight = technical_weight + sector_weight
        for candidate in sorted(candidates, key=lambda c: str(c.global_instrument_id)):
            key = candidate.global_instrument_id
            technical = technical_engine.compute(key, histories[key], as_of=scan.as_of, currency=candidate.currency,
                                                 trusted_providers=providers.get(key), daily_bar_history=daily_histories[key])
            sector = sector_engine.compute(key, histories[key], as_of=scan.as_of, currency=candidate.currency,
                context=contexts.get(key), benchmark_histories=histories, trusted_providers=providers.get(key))
            scores = [(technical.technical_score, technical_weight), (sector.relative_strength_score, sector_weight)]
            available = [(score, weight) for score, weight in scores if score is not None and weight > 0]
            available_weight = sum(weight for _, weight in available)
            score = sum(value * weight for value, weight in available) / available_weight if available_weight else None
            output.append(StageBCandidate(global_instrument_id=key, symbol=candidate.symbol, pre_score=candidate.pre_score,
                technical_feature_snapshot=technical, sector_relative_strength_snapshot=sector,
                technical_score=technical.technical_score, sector_score=sector.relative_strength_score,
                stage_b_score=round(score, 8) if score is not None else None,
                confidence=round((technical.confidence * technical_weight + sector.confidence * sector_weight) / total_weight, 6),
                score_coverage=round(available_weight / total_weight * 100, 6),
                score_weights={"technical": technical_weight, "sector": sector_weight}))
        output.sort(key=lambda c: (-(c.stage_b_score if c.stage_b_score is not None else -1), -c.confidence, str(c.global_instrument_id)))
        return output

    async def scan(self, *, as_of: datetime, top_n: int, **universe_context) -> GlobalScanResult:
        if top_n < 0:
            raise ValueError("top_n must be nonnegative")
        instruments = await self.universe.active_global_equities(**universe_context)
        by_id = {}
        invalid = 0
        for item in instruments:
            try:
                key = UUID(str(item.get("globalInstrumentId")))
            except (ValueError, TypeError):
                invalid += 1
                continue
            if key in by_id and by_id[key] != item:
                raise ValueError(f"Conflicting canonical records for {key}")
            by_id[key] = item
        candidates = []
        ordered = sorted(by_id, key=str)
        for offset in range(0, len(ordered), self.batch_size):
            ids = set(ordered[offset:offset+self.batch_size])
            snapshots, facts, prices = defaultdict(list), defaultdict(list), defaultdict(list)
            for row in self.persistence.load_structured_market_snapshots(ids): snapshots[row.instrument_id].append(row)
            for row in self.persistence.load_financial_facts(ids): facts[row.key.instrument_id].append(row)
            for row in self.persistence.load_market_price_observations(ids): prices[row.instrument_id].append(row)
            for key in sorted(ids, key=str):
                candidates.append(self.pre_score.score(by_id[key], snapshots[key], facts[key], prices[key], as_of=as_of))
        candidates.sort(key=lambda c: (-(c.pre_score if c.pre_score is not None else -1), -c.confidence, -c.critical_completeness, str(c.global_instrument_id)))
        eligible = [c for c in candidates if c.eligible_for_deep_analysis]
        excluded = Counter(reason for c in candidates for reason in c.exclusion_reasons)
        if invalid: excluded["UNTRUSTED_CANONICAL_IDENTITY"] += invalid
        top = eligible[:top_n]
        return GlobalScanResult(as_of=_utc(as_of), total_canonical_active_equities=sum(i.get("status") == "ACTIVE" and i.get("assetType") == "EQUITY" for i in by_id.values()),
            eligible_candidates=len(eligible), excluded_candidates_by_reason=dict(sorted(excluded.items())),
            deep_analysis_eligible_count=len(eligible), candidates=candidates, top_candidates=top,
            deep_analysis_candidate_ids=[c.global_instrument_id for c in top])
