"""Deterministic, provider-free public-company stock rule engine.

``STOCK_RULE_ENGINE_V1`` consumes only durable records that already passed the
Research Readiness boundary.  It never imports or invokes a provider adapter.
Every scored metric names its V1 rule and evidence, and the input fingerprint
makes a stored result reproducible and safely reusable.

V1 aggregation
--------------
* Each area is a weighted mean of the available, applicable sub-rules below.
  Missing optional metrics are omitted rather than scored as zero.
* ``overallScore`` is the top-level weighted mean, normalized over scorable and
  applicable areas.  The configured area weights remain fixed and total 100.
* ``qualityScore`` uses quality (16), growth (14), balance sheet (9), quarterly
  trend (9), and governance (5), normalized over scorable members.
* ``opportunityScore`` uses valuation (18), catalysts (8), technical (7),
  current events (7), and sector/macro (3), normalized over scorable members.
* ``riskScore`` is 100 minus a normalized resilience score made from balance
  sheet (30), governance (30), quarterly trend (15), current events (15), and
  business quality (10).  A higher risk score therefore means more risk.
* ``confidenceScore`` is independent of every stock score: mandatory/critical
  coverage 40%, overall coverage 20%, source authority 20%, freshness 20%, less
  15 points per conflict (maximum 30).

Decision thresholds before gates/overrides are 85 STRONG_BUY, 75 BUY,
65 ACCUMULATE, 50 HOLD, 35 REDUCE, 20 AVOID, otherwise EXIT_REVIEW.  Partial
analysis is explicitly requested and capped at HOLD.  Critical overrides sit
above the weighted score and block positive decisions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from statistics import median, pstdev
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from app.news_intelligence import EventImpactFeature, SearchRun
from uuid import UUID

from pydantic import Field

from app.fact_precedence import FinancialFact, FactSourceTier, fact_source_authority
from app.models import (
    CompanyResearchProfile,
    EventImpact,
    MarketPriceObservation,
    ReliabilityLevel,
    ResearchBaseModel,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    ShareholdingCategory,
    ShareholdingSnapshot,
    SourceClassification,
    SourceMode,
    StructuredMarketSnapshotRecord,
    TimeHorizon,
)
from app.research_readiness import (
    ResearchReadinessResult,
    ResearchRequirementReadiness,
    ResearchRequirementStatus,
    ResearchSourceTier,
    RuleEngineArea,
)


STOCK_RULE_ENGINE_VERSION = "STOCK_RULE_ENGINE_V1"
CURRENT_NEWS_WINDOW_DAYS = 30

STOCK_RULE_ENGINE_AREA_WEIGHTS: Mapping[RuleEngineArea, int] = {
    RuleEngineArea.VALUATION: 18,
    RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY: 16,
    RuleEngineArea.GROWTH: 14,
    RuleEngineArea.BALANCE_SHEET: 9,
    RuleEngineArea.QUARTERLY_EARNINGS_TREND: 9,
    RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS: 8,
    RuleEngineArea.PRICE_TECHNICAL: 7,
    RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS: 7,
    RuleEngineArea.SHAREHOLDING: 4,
    RuleEngineArea.MANAGEMENT_GOVERNANCE: 5,
    RuleEngineArea.SECTOR_MACRO: 3,
}
if sum(STOCK_RULE_ENGINE_AREA_WEIGHTS.values()) != 100:  # pragma: no cover - import guard
    raise RuntimeError("STOCK_RULE_ENGINE_V1 area weights must total exactly 100")


class AreaScoreStatus(StrEnum):
    READY_FRESH = "READY_FRESH"
    READY_STALE = "READY_STALE"
    PARTIAL = "PARTIAL"
    CONFLICTING = "CONFLICTING"
    UNSCORABLE = "UNSCORABLE"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DecisionSignal(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    ACCUMULATE = "ACCUMULATE"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    AVOID = "AVOID"
    EXIT_REVIEW = "EXIT_REVIEW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RiskOverrideSeverity(StrEnum):
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RuleMetricResult(ResearchBaseModel):
    metric: str
    value: Any = None
    unit: str | None = None
    score: float = Field(ge=0, le=100)
    rule: str
    configured_subrule_weight: int = Field(default=1, gt=0)
    applied_weight_pct: float = Field(default=100, ge=0, le=100)
    source: str
    source_url: str | None = None
    as_of: datetime | None = None
    evidence_references: list[str] = Field(default_factory=list)


class RuleSourceReference(ResearchBaseModel):
    source_provider: str | None = None
    source_tier: ResearchSourceTier | None = None
    source_url: str
    as_of: datetime | None = None
    retrieved_at: datetime | None = None
    evidence_references: list[str] = Field(default_factory=list)


class AreaScoreResult(ResearchBaseModel):
    area: RuleEngineArea
    weight: int
    raw_score: float | None = Field(default=None, ge=0, le=100)
    weighted_contribution: float = Field(default=0, ge=0, le=100)
    status: AreaScoreStatus
    applicable: bool
    metrics: list[RuleMetricResult] = Field(default_factory=list)
    positive_factors: list[str] = Field(default_factory=list)
    negative_factors: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    source_references: list[RuleSourceReference] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class RiskOverrideResult(ResearchBaseModel):
    code: str
    severity: RiskOverrideSeverity
    effect: str = "BLOCK_BUY"
    evidence_ids: list[str] = Field(default_factory=list)


class AnalysisEligibility(ResearchBaseModel):
    full_analysis_allowed: bool
    partial_analysis_allowed: bool
    blocking_requirements: list[str] = Field(default_factory=list)
    reason: str


class StockRuleEngineResult(ResearchBaseModel):
    rule_engine_version: str = STOCK_RULE_ENGINE_VERSION
    calculated_at: datetime
    global_instrument_id: UUID
    input_as_of: datetime | None = None
    input_fingerprint: str
    overall_score: float | None = Field(default=None, ge=0, le=100)
    quality_score: float | None = Field(default=None, ge=0, le=100)
    opportunity_score: float | None = Field(default=None, ge=0, le=100)
    risk_score: float | None = Field(default=None, ge=0, le=100)
    confidence_score: float = Field(ge=0, le=100)
    confidence: ConfidenceLevel
    decision_signal: DecisionSignal
    partial: bool
    cache_hit: bool = False
    eligibility: AnalysisEligibility
    area_scores: list[AreaScoreResult] = Field(default_factory=list)
    risk_overrides: list[RiskOverrideResult] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Datum:
    value: Decimal
    source: str
    source_url: str | None
    as_of: datetime | None
    evidence_id: str
    unit: str | None = None
    authority: int = 0


@dataclass(frozen=True)
class StockRuleEngineInput:
    profile: CompanyResearchProfile
    readiness: ResearchReadinessResult
    financial_facts: tuple[FinancialFact, ...]
    structured_snapshots: tuple[StructuredMarketSnapshotRecord, ...]
    market_prices: tuple[MarketPriceObservation, ...]
    events: tuple[ResearchEvent, ...]
    shareholding: tuple[ShareholdingSnapshot, ...]
    canonical_metadata: Mapping[str, Any]
    evaluated_at: datetime
    news_features: tuple[EventImpactFeature, ...] = ()
    news_search_run: SearchRun | None = None


class StockRuleEngineEligibilityPolicy:
    """One deterministic gate shared by the API and UI action contract."""

    CRITICAL_REQUIREMENTS = (
        "VALUATION_INPUTS",
        "BUSINESS_QUALITY_FACTS",
        "BALANCE_SHEET_FACTS",
        "QUARTERLY_FINANCIALS",
        "LATEST_PRICE",
    )
    FULL_STATUSES = frozenset(
        {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}
    )
    PARTIAL_STATUSES = frozenset(
        {
            ResearchRequirementStatus.READY_FRESH,
            ResearchRequirementStatus.READY_STALE,
            ResearchRequirementStatus.PARTIAL,
        }
    )

    def evaluate(self, readiness: ResearchReadinessResult) -> AnalysisEligibility:
        by_id = {item.requirement_id: item for item in readiness.requirements}
        mandatory_blocking = sorted(
            item.requirement_id
            for item in readiness.requirements
            if item.mandatory and item.requirement_id != 'CURRENT_NEWS' and item.status not in self.FULL_STATUSES and item.status != ResearchRequirementStatus.NOT_APPLICABLE
        )
        critical = [by_id.get(key) for key in self.CRITICAL_REQUIREMENTS]
        critical_blocking = sorted(
            key
            for key, item in zip(self.CRITICAL_REQUIREMENTS, critical)
            if item is None or (item.status not in self.FULL_STATUSES and item.status != ResearchRequirementStatus.NOT_APPLICABLE)
        )
        full = (
            readiness.critical_completeness_pct >= 80
            and not mandatory_blocking
            and not critical_blocking
        )
        usable_critical = sum(
            1 for item in critical if item is not None and item.status in self.PARTIAL_STATUSES
        )
        latest = by_id.get("LATEST_PRICE")
        conflict = any(
            item is not None and item.status == ResearchRequirementStatus.CONFLICTING
            for item in critical
        )
        partial = (
            not full
            and readiness.critical_completeness_pct >= 50
            and usable_critical >= 3
            and latest is not None
            and latest.status in self.PARTIAL_STATUSES
            and not conflict
        )
        blockers = sorted(set(mandatory_blocking + critical_blocking))
        reason = (
            "FULL_CRITICAL_AND_MANDATORY_INPUTS_AVAILABLE"
            if full
            else "MINIMUM_SAFE_PARTIAL_GATE_AVAILABLE"
            if partial
            else "MINIMUM_SAFE_CRITICAL_INPUTS_UNAVAILABLE"
        )
        return AnalysisEligibility(
            full_analysis_allowed=full,
            partial_analysis_allowed=partial,
            blocking_requirements=blockers,
            reason=reason,
        )


class StockRuleEngineInputAdapter:
    """Loads existing durable public-company data; it has no provider handles."""

    def __init__(self, repository, readiness_adapter) -> None:
        self.repository = repository
        self.readiness_adapter = readiness_adapter

    async def load(
        self,
        profile: CompanyResearchProfile,
        readiness: ResearchReadinessResult,
        *,
        now: datetime | None = None,
    ) -> StockRuleEngineInput:
        instrument_id = profile.instrument_id
        facts, snapshots, prices = await asyncio.gather(
            self.repository.financial_facts_for_instruments({instrument_id}),
            self.repository.structured_market_snapshots_for_instruments({instrument_id}),
            self.repository.market_price_observations_for_instruments({instrument_id}),
        )
        evaluated_at = _aware(now or datetime.now(timezone.utc))
        from app.news_intelligence import EventImpactFeature, SearchRun
        news_loader = getattr(self.repository, 'news_records_for', None)
        features = news_loader(instrument_id, EventImpactFeature, as_of=evaluated_at) if callable(news_loader) else []
        runs = news_loader(instrument_id, SearchRun, as_of=evaluated_at) if callable(news_loader) else []
        return StockRuleEngineInput(
            profile=profile,
            readiness=readiness,
            financial_facts=tuple(facts.get(instrument_id, ())),
            structured_snapshots=tuple(snapshots.get(instrument_id, ())),
            market_prices=tuple(prices.get(instrument_id, ())),
            events=tuple(
                self.repository.events_for(instrument_id, source_mode=SourceMode.REAL)
            ),
            shareholding=tuple(self.repository.shareholding_for(instrument_id, limit=8)),
            canonical_metadata=self.readiness_adapter.canonical_metadata_for(instrument_id),
            evaluated_at=evaluated_at,
            news_features=tuple(features), news_search_run=runs[-1] if runs else None,
        )


class StockRuleEngineService:
    """Application service for deterministic scoring and exact-input caching."""

    def __init__(self, repository, readiness_adapter) -> None:
        self.repository = repository
        self.input_adapter = StockRuleEngineInputAdapter(repository, readiness_adapter)
        self.engine = StockRuleEngineV1()
        self.eligibility_policy = StockRuleEngineEligibilityPolicy()

    async def analyze(
        self,
        profile: CompanyResearchProfile,
        readiness: ResearchReadinessResult,
        *,
        allow_partial: bool,
        now: datetime | None = None,
    ) -> StockRuleEngineResult:
        inputs = await self.input_adapter.load(profile, readiness, now=now)
        fingerprint = self.engine.input_fingerprint(inputs, allow_partial=allow_partial)
        cached = await self.repository.stock_rule_engine_result(
            profile.instrument_id, STOCK_RULE_ENGINE_VERSION, fingerprint
        )
        if cached is not None:
            return StockRuleEngineResult.model_validate(cached).model_copy(
                update={"cache_hit": True}
            )
        result = self.engine.evaluate(inputs, allow_partial=allow_partial, fingerprint=fingerprint)
        await self.repository.persist_stock_rule_engine_result(
            result.model_dump(mode="json", by_alias=False)
        )
        return result


class StockRuleEngineV1:
    """Versioned sub-rule evaluators over one immutable durable input snapshot."""

    def __init__(self) -> None:
        self.eligibility_policy = StockRuleEngineEligibilityPolicy()

    def input_fingerprint(self, value: StockRuleEngineInput, *, allow_partial: bool) -> str:
        payload = {
            "version": STOCK_RULE_ENGINE_VERSION,
            # Fingerprint schema for the V1 payload. This preserves exact-cache
            # safety when explainability fields evolve before a new score rule.
            "fingerprintContract": "STOCK_RULE_ENGINE_V1_INPUT_2_NEWS",
            "newsFeatures": [f.model_dump(mode='json') for f in sorted(value.news_features,key=lambda f:str(f.feature_id))],
            "newsSearch": value.news_search_run.model_dump(mode='json') if value.news_search_run else None,
            "newsEvaluationDate": value.evaluated_at.isoformat() if value.news_features or value.news_search_run else None,
            "analysisMode": "PARTIAL_ALLOWED" if allow_partial else "FULL_REQUIRED",
            # Aging current-news eligibility changes at UTC day boundaries.
            "evaluationDate": value.evaluated_at.date().isoformat(),
            "globalInstrumentId": str(value.profile.instrument_id),
            "profile": {
                "country": value.profile.country,
                "exchange": value.profile.exchange,
                "mic": value.profile.mic,
                "ticker": value.profile.ticker,
                "sector": _sector(value),
            },
            "readiness": [
                {
                    "id": item.requirement_id,
                    "status": item.status.value,
                    "coverage": item.coverage_pct,
                    "criticalCoverage": item.critical_coverage_pct,
                    "source": item.source,
                    "sourceTier": item.source_tier.value if item.source_tier else None,
                    "sourceUrl": item.source_url,
                    "asOf": _iso(item.as_of),
                    "evidence": sorted(item.evidence_ids),
                    "missing": sorted(item.missing_input_ids),
                    "conflict": item.conflict_reason,
                }
                for item in sorted(value.readiness.requirements, key=lambda item: item.requirement_id)
            ],
            "financialFacts": [
                {
                    "metric": fact.key.metric,
                    "periodEnd": fact.key.period_end,
                    "periodType": fact.key.period_type,
                    "basis": fact.key.reporting_basis,
                    "value": str(fact.value.value),
                    "unit": fact.value.unit,
                    "source": fact.source_provider,
                    "sourceIdentity": fact.source_identity,
                    "sourceTier": int(fact.source_tier),
                    "sourceUrl": fact.value.source_url,
                    "asOf": _iso(fact.value.as_of_date or _period_datetime(fact.key.period_end)),
                }
                for fact in sorted(
                    value.financial_facts,
                    key=lambda item: (
                        _metric_key(item.key.metric), item.key.period_type,
                        item.key.period_end or "", item.key.reporting_basis or "",
                    ),
                )
                if fact.source_mode == SourceMode.REAL
            ],
            "structured": [
                {
                    "provider": record.provider,
                    "providerId": record.provider_instrument_id,
                    "marketAsOf": _iso(record.market_as_of),
                    "facts": {
                        key: {
                            "value": str(item.value),
                            "asOf": _iso(item.as_of_date),
                            "source": item.source_name,
                            "url": item.source_url,
                        }
                        for key, item in sorted(record.snapshot.facts.items())
                        if item.value is not None
                    },
                }
                for record in sorted(value.structured_snapshots, key=lambda item: item.provider)
            ],
            "prices": [
                {
                    "at": _iso(item.observed_at),
                    "price": str(item.price),
                    "provider": item.provider,
                }
                for item in _usable_prices(value.market_prices)
            ],
            "events": [
                {
                    "id": str(item.event_id),
                    "type": _enum_text(item.event_type),
                    "date": _iso(item.event_date or item.published_at),
                    "title": item.title,
                    "summary": item.summary,
                    "impact": _enum_text(item.impact),
                    "horizon": _enum_text(item.time_horizon),
                    "confidence": item.confidence,
                    "status": _enum_text(item.status),
                    "classification": _enum_text(item.source_classification),
                    "reliability": _enum_text(item.reliability),
                    "sourceUrl": item.source_url,
                    "monetary": str(item.monetary_value) if item.monetary_value is not None else None,
                    "percentage": str(item.percentage_value) if item.percentage_value is not None else None,
                    "capacity": str(item.capacity_value) if item.capacity_value is not None else None,
                    "counterparty": item.counterparty,
                    "customer": item.customer,
                    "location": item.location,
                }
                for item in sorted(value.events, key=lambda item: str(item.event_id))
            ],
            "shareholding": [
                {
                    "id": str(item.id),
                    "periodEnd": _iso(item.period_end),
                    "provider": item.source_provider,
                    "url": item.source_url,
                    "values": sorted(
                        (_enum_text(entry.category), str(entry.percentage), entry.metric_basis)
                        for entry in item.values
                    ),
                }
                for item in sorted(value.shareholding, key=lambda item: item.period_end)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def evaluate(
        self,
        value: StockRuleEngineInput,
        *,
        allow_partial: bool,
        fingerprint: str | None = None,
    ) -> StockRuleEngineResult:
        eligibility = self.eligibility_policy.evaluate(value.readiness)
        areas = [
            self._valuation(value),
            self._quality(value),
            self._growth(value),
            self._balance_sheet(value),
            self._quarterly(value),
            self._catalysts(value),
            self._technical(value),
            self._news(value),
            self._shareholding(value),
            self._governance(value),
            self._sector_macro(value),
        ]
        scorable = [item for item in areas if item.applicable and item.raw_score is not None]
        raw_overall = _area_weighted_score(scorable)
        quality = _area_weighted_score(
            scorable,
            areas={
                RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY,
                RuleEngineArea.GROWTH,
                RuleEngineArea.BALANCE_SHEET,
                RuleEngineArea.QUARTERLY_EARNINGS_TREND,
                RuleEngineArea.MANAGEMENT_GOVERNANCE,
            },
        )
        opportunity = _area_weighted_score(
            scorable,
            areas={
                RuleEngineArea.VALUATION,
                RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS,
                RuleEngineArea.PRICE_TECHNICAL,
                RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS,
                RuleEngineArea.SECTOR_MACRO,
            },
        )
        risk_resilience = _custom_area_score(
            scorable,
            {
                RuleEngineArea.BALANCE_SHEET: 30,
                RuleEngineArea.MANAGEMENT_GOVERNANCE: 30,
                RuleEngineArea.QUARTERLY_EARNINGS_TREND: 15,
                RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS: 15,
                RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY: 10,
            },
        )
        risk = _round_score(100 - risk_resilience) if risk_resilience is not None else None
        confidence_score = self._confidence(value.readiness)
        confidence = _confidence_level(confidence_score)
        overrides = self._risk_overrides(value)

        permitted = eligibility.full_analysis_allowed or (
            allow_partial and eligibility.partial_analysis_allowed
        )
        partial = not eligibility.full_analysis_allowed
        overall = raw_overall if permitted else None
        decision = (
            self._decision(
                raw_overall,
                value.readiness,
                confidence,
                partial=partial,
                overrides=overrides,
            )
            if permitted
            else DecisionSignal.INSUFFICIENT_DATA
        )
        missing = sorted(
            set(eligibility.blocking_requirements)
            | {missing for area in areas for missing in area.missing_inputs}
        )
        evidence = sorted(
            {reference for area in areas for reference in area.evidence_references}
            | {reference for override in overrides for reference in override.evidence_ids}
        )
        return StockRuleEngineResult(
            calculated_at=value.evaluated_at,
            global_instrument_id=value.profile.instrument_id,
            input_as_of=_input_as_of(value),
            input_fingerprint=fingerprint or self.input_fingerprint(value, allow_partial=allow_partial),
            overall_score=overall,
            quality_score=quality if permitted else None,
            opportunity_score=opportunity if permitted else None,
            risk_score=risk if permitted else None,
            confidence_score=confidence_score,
            confidence=confidence,
            decision_signal=decision,
            partial=partial,
            eligibility=eligibility,
            area_scores=areas,
            risk_overrides=overrides,
            missing_inputs=missing,
            evidence_references=evidence,
        )

    def _valuation(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        structured = _structured_data(value)
        from app.valuation_evidence import materialize_valuation
        for name, fact in materialize_valuation(value.structured_snapshots,value.market_prices,now=value.evaluated_at).items():
            structured[_metric_key(name)] = _Datum(Decimal(str(fact.value)),fact.source_name,fact.source_url,
                fact.as_of_date,'derived:'+name,'RATIO',0)
        pe = _pick(structured, "trailingpe", "pe", "pricetoearnings")
        forward_pe = _pick(structured, "forwardpe")
        pb = _pick(structured, "pricetobook", "pb")
        ev_ebitda = _pick(structured, "evtoebitda", "enterprisevaluetoebitda")
        peg = _pick(structured, "pegratio", "peg")
        market_cap = _pick(structured, "marketcap")
        fcf = _pick(structured, "freecashflow") or _latest_fact(value, ("free_cash_flow",))
        financial = _is_financial(value)
        if pe and pe.value > 0:
            metrics.append((_metric_result("TRAILING_PE", pe, _lower_better(pe.value, ((8, 92), (15, 78), (25, 58), (40, 32), (60, 12))), "TRAILING_PE_V1", "RATIO"), 25))
            earnings_yield = _derived_datum(Decimal("100") / pe.value, "PERCENT", pe, "derived:earnings-yield")
            metrics.append((_metric_result("EARNINGS_YIELD", earnings_yield, _higher_better(earnings_yield.value, ((1.5, 10), (2.5, 30), (4, 55), (6.5, 78), (10, 92))), "EARNINGS_YIELD_V1", "PERCENT"), 10))
        if forward_pe and forward_pe.value > 0:
            metrics.append((_metric_result("FORWARD_PE", forward_pe, _lower_better(forward_pe.value, ((8, 94), (15, 80), (25, 58), (40, 30), (60, 10))), "FORWARD_PE_V1", "RATIO"), 15))
        if pb and pb.value > 0:
            points = ((0.8, 94), (1.5, 82), (3, 60), (5, 38), (8, 15)) if financial else ((1, 88), (2, 76), (4, 56), (8, 30), (12, 12))
            metrics.append((_metric_result("PRICE_TO_BOOK", pb, _lower_better(pb.value, points), "PRICE_TO_BOOK_FINANCIAL_V1" if financial else "PRICE_TO_BOOK_GENERAL_V1", "RATIO"), 18 if financial else 8))
        if ev_ebitda and ev_ebitda.value > 0 and not financial:
            metrics.append((_metric_result("EV_EBITDA", ev_ebitda, _lower_better(ev_ebitda.value, ((5, 94), (8, 80), (12, 62), (20, 35), (30, 12))), "EV_EBITDA_NON_FINANCIAL_V1", "RATIO"), 15))
        if peg and peg.value > 0:
            metrics.append((_metric_result("PEG", peg, _lower_better(peg.value, ((0.5, 90), (1, 82), (1.5, 65), (2.5, 38), (4, 15))), "PEG_V1", "RATIO"), 10))
        if fcf and market_cap and market_cap.value > 0:
            fcf_yield = _derived_datum(fcf.value / market_cap.value * 100, "PERCENT", fcf, "derived:fcf-yield", market_cap)
            metrics.append((_metric_result("FCF_YIELD", fcf_yield, _higher_better(fcf_yield.value, ((-5, 5), (0, 25), (2, 48), (5, 72), (8, 90))), "FCF_YIELD_V1", "PERCENT"), 15))
        missing = [] if metrics else ["VALUATION_BASIS"]
        return self._finish(value, RuleEngineArea.VALUATION, metrics, missing)

    def _quality(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        structured = _structured_data(value)
        roe = _pick(structured, "roe", "returnonequity") or _latest_fact(value, ("roe", "return_on_equity"))
        roce = _pick(structured, "roce", "returnoncapitalemployed") or _latest_fact(value, ("roce", "return_on_capital_employed"))
        operating_margin = _pick(structured, "operatingmargin") or _latest_fact(value, ("operating_margin", "ebitda_margin"))
        net_margin = _pick(structured, "profitmargin", "netmargin") or _latest_fact(value, ("profit_margin", "net_margin"))
        for name, datum, weight, rule in (
            ("ROE", roe, 20, "ROE_V1"),
            ("ROCE", roce if not _is_financial(value) else None, 20, "ROCE_NON_FINANCIAL_V1"),
            ("OPERATING_MARGIN", operating_margin, 12, "OPERATING_MARGIN_V1"),
            ("NET_MARGIN", net_margin, 10, "NET_MARGIN_V1"),
        ):
            if datum:
                normalized = _as_percent(datum.value)
                normalized_datum = _derived_datum(normalized, "PERCENT", datum, f"normalized:{name.lower()}")
                metrics.append((_metric_result(name, normalized_datum, _higher_better(normalized, ((0, 15), (5, 38), (10, 58), (18, 78), (28, 94))), rule, "PERCENT"), weight))

        annual_pat = _fact_series(value, ("pat", "net_income", "net_profit"), "ANNUAL")
        annual_revenue = _fact_series(value, ("revenue", "total_revenue"), "ANNUAL")
        margins = _aligned_ratios(annual_pat, annual_revenue, multiplier=Decimal("100"))
        if len(margins) >= 3:
            stability = Decimal(str(pstdev(float(item) for item in margins[-5:])))
            datum = _derived_from_many(Decimal("100") - stability, "INDEX", [*annual_pat[-5:], *annual_revenue[-5:]], "derived:margin-stability")
            metrics.append((_metric_result("MARGIN_STABILITY", datum, _lower_better(stability, ((2, 92), (5, 78), (10, 55), (20, 25), (35, 8))), "MARGIN_STABILITY_STDDEV_V1", "PERCENT_STDDEV", display_value=stability), 12))
        ocf = _latest_fact(value, ("operating_cash_flow", "cash_flow_from_operating_activities"), period_type="ANNUAL")
        pat = annual_pat[-1] if annual_pat else _latest_fact(value, ("pat", "net_income", "net_profit"), period_type="ANNUAL")
        if ocf and pat and pat.value != 0:
            conversion = ocf.value / abs(pat.value)
            datum = _derived_datum(conversion, "RATIO", ocf, "derived:cash-conversion", pat)
            metrics.append((_metric_result("CASH_CONVERSION", datum, _higher_better(conversion, ((0, 8), (0.5, 40), (0.8, 65), (1, 82), (1.3, 94))), "OPERATING_CASH_TO_PAT_V1", "RATIO"), 16))
        fcf = _pick(structured, "freecashflow") or _latest_fact(value, ("free_cash_flow",), period_type="ANNUAL")
        if fcf and pat and pat.value != 0:
            quality = fcf.value / abs(pat.value)
            datum = _derived_datum(quality, "RATIO", fcf, "derived:fcf-quality", pat)
            metrics.append((_metric_result("FCF_QUALITY", datum, _higher_better(quality, ((-0.5, 5), (0, 25), (0.5, 55), (0.8, 75), (1.1, 90))), "FCF_TO_PAT_V1", "RATIO"), 10))
        if len(annual_pat) >= 3:
            consistency = Decimal(sum(1 for item in annual_pat[-5:] if item.value > 0)) / Decimal(len(annual_pat[-5:])) * 100
            datum = _derived_from_many(consistency, "PERCENT", annual_pat[-5:], "derived:profitability-consistency")
            metrics.append((_metric_result("PROFITABILITY_CONSISTENCY", datum, float(consistency), "POSITIVE_PAT_PERIOD_SHARE_V1", "PERCENT"), 16))
        missing = [] if metrics else ["QUALITY_FACTS"]
        return self._finish(value, RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY, metrics, missing)

    def _growth(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        annual_revenue = _fact_series(value, ("revenue", "total_revenue"), "ANNUAL")
        annual_earnings = _fact_series(value, ("pat", "net_income", "net_profit"), "ANNUAL")
        if not annual_earnings:
            annual_earnings = _fact_series(value, ("eps",), "ANNUAL")
        for name, series, weight in (
            ("REVENUE_CAGR", annual_revenue, 27),
            ("EARNINGS_CAGR", annual_earnings, 27),
        ):
            cagr = _series_cagr(series)
            if cagr is not None:
                datum = _derived_from_many(cagr, "PERCENT", series, f"derived:{name.lower()}")
                metrics.append((_metric_result(name, datum, _growth_score(cagr), f"{name}_MULTI_YEAR_V1", "PERCENT"), weight))
        quarterly_revenue = _fact_series(value, ("revenue", "total_revenue"), "QUARTERLY")
        quarterly_earnings = _fact_series(value, ("pat", "net_income", "net_profit"), "QUARTERLY")
        if not quarterly_earnings:
            quarterly_earnings = _fact_series(value, ("eps",), "QUARTERLY")
        for name, series, weight in (
            ("REVENUE_YOY", quarterly_revenue, 15),
            ("EARNINGS_YOY", quarterly_earnings, 15),
        ):
            comparison = _comparable_yoy(series)
            if comparison:
                change, latest, prior = comparison
                datum = _derived_datum(change, "PERCENT", latest, f"derived:{name.lower()}", prior)
                metrics.append((_metric_result(name, datum, _growth_score(change), f"{name}_COMPARABLE_QUARTER_V1", "PERCENT"), weight))
        qoq = _sequential_change(quarterly_revenue)
        if qoq:
            change, latest, prior = qoq
            datum = _derived_datum(change, "PERCENT", latest, "derived:revenue-qoq", prior)
            metrics.append((_metric_result("RECENT_REVENUE_QOQ", datum, _growth_score(change), "RECENT_QOQ_LIMITED_WEIGHT_V1", "PERCENT"), 6))
        consistency = _growth_consistency(annual_revenue, annual_earnings)
        if consistency:
            score, source_data = consistency
            datum = _derived_from_many(score, "PERCENT", source_data, "derived:growth-consistency")
            metrics.append((_metric_result("MULTI_YEAR_GROWTH_CONSISTENCY", datum, float(score), "POSITIVE_ANNUAL_CHANGE_SHARE_V1", "PERCENT"), 10))
        return self._finish(value, RuleEngineArea.GROWTH, metrics, [] if metrics else ["MULTI_PERIOD_GROWTH_HISTORY"])

    def _balance_sheet(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        if _is_financial(value):
            capital = _latest_fact(value, ("capital_adequacy", "capital_adequacy_ratio"))
            gross_npa = _latest_fact(value, ("gross_npa", "gross_npa_ratio"))
            net_npa = _latest_fact(value, ("net_npa", "net_npa_ratio"))
            if capital:
                pct = _as_percent(capital.value)
                metrics.append((_metric_result("CAPITAL_ADEQUACY", _derived_datum(pct, "PERCENT", capital, "normalized:capital-adequacy"), _higher_better(pct, ((9, 20), (12, 50), (15, 72), (18, 90))), "FINANCIAL_CAPITAL_ADEQUACY_V1", "PERCENT"), 50))
            for name, datum, weight in (("GROSS_NPA", gross_npa, 25), ("NET_NPA", net_npa, 25)):
                if datum:
                    pct = _as_percent(datum.value)
                    metrics.append((_metric_result(name, _derived_datum(pct, "PERCENT", datum, f"normalized:{name.lower()}"), _lower_better(pct, ((1, 92), (2, 78), (4, 52), (7, 25), (12, 8))), f"{name}_FINANCIAL_V1", "PERCENT"), weight))
            return self._finish(value, RuleEngineArea.BALANCE_SHEET, metrics, [] if metrics else ["FINANCIAL_SECTOR_CAPITAL_OR_ASSET_QUALITY"], applicable=True)

        structured = _structured_data(value)
        debt_equity = _pick(structured, "debttoequity")
        debt = _pick(structured, "totaldebt") or _latest_fact(value, ("debt_or_borrowings", "total_debt"), period_type="ANNUAL")
        equity = _latest_fact(value, ("equity", "total_equity"), period_type="ANNUAL")
        cash = _pick(structured, "totalcash") or _latest_fact(value, ("cash_and_cash_equivalents", "cash_and_equivalents"), period_type="ANNUAL")
        if debt_equity:
            de_pct = debt_equity.value if abs(debt_equity.value) > 5 else debt_equity.value * 100
            datum = _derived_datum(de_pct, "PERCENT", debt_equity, "normalized:debt-equity")
        elif debt and equity and equity.value > 0:
            de_pct = debt.value / equity.value * 100
            datum = _derived_datum(de_pct, "PERCENT", debt, "derived:debt-equity", equity)
        else:
            datum = None
        if datum:
            metrics.append((_metric_result("DEBT_TO_EQUITY", datum, _lower_better(datum.value, ((0, 95), (30, 85), (60, 68), (100, 45), (200, 18))), "INDUSTRIAL_DEBT_TO_EQUITY_V1", "PERCENT"), 28))
        if debt and cash and equity and equity.value > 0:
            net_debt_equity = (debt.value - cash.value) / equity.value * 100
            nd = _derived_datum(net_debt_equity, "PERCENT", debt, "derived:net-debt-equity", cash, equity)
            metrics.append((_metric_result("NET_DEBT_TO_EQUITY", nd, _lower_better(net_debt_equity, ((-20, 98), (0, 90), (30, 75), (80, 48), (150, 18))), "NET_DEBT_TO_EQUITY_V1", "PERCENT"), 18))
        ebitda = _latest_fact(value, ("ebitda", "ebit", "operating_income"), period_type="ANNUAL")
        finance_cost = _latest_fact(value, ("finance_cost", "interest_expense"), period_type="ANNUAL")
        if ebitda and finance_cost and finance_cost.value > 0:
            coverage = ebitda.value / finance_cost.value
            cov = _derived_datum(coverage, "RATIO", ebitda, "derived:interest-coverage", finance_cost)
            metrics.append((_metric_result("INTEREST_COVERAGE", cov, _higher_better(coverage, ((0.5, 5), (1, 18), (2, 45), (4, 70), (8, 92))), "EBITDA_INTEREST_COVERAGE_V1", "RATIO"), 24))
        current_assets = _latest_fact(value, ("current_assets",), period_type="ANNUAL")
        current_liabilities = _latest_fact(value, ("current_liabilities",), period_type="ANNUAL")
        if current_assets and current_liabilities and current_liabilities.value > 0:
            current_ratio = current_assets.value / current_liabilities.value
            current = _derived_datum(current_ratio, "RATIO", current_assets, "derived:current-ratio", current_liabilities)
            metrics.append((_metric_result("CURRENT_RATIO", current, _higher_better(current_ratio, ((0.5, 8), (0.9, 35), (1.2, 65), (1.8, 88), (3, 82))), "CURRENT_RATIO_NON_FINANCIAL_V1", "RATIO"), 15))
        debt_series = _fact_series(value, ("debt_or_borrowings", "total_debt"), "ANNUAL")
        if len(debt_series) >= 2 and debt_series[-2].value > 0:
            trend = (debt_series[-1].value / debt_series[-2].value - 1) * 100
            debt_trend = _derived_datum(trend, "PERCENT", debt_series[-1], "derived:debt-trend", debt_series[-2])
            metrics.append((_metric_result("DEBT_TREND", debt_trend, _lower_better(trend, ((-20, 95), (0, 78), (10, 58), (30, 30), (60, 8))), "ANNUAL_DEBT_TREND_V1", "PERCENT"), 15))
        return self._finish(value, RuleEngineArea.BALANCE_SHEET, metrics, [] if metrics else ["BALANCE_SHEET_LEVERAGE_OR_LIQUIDITY"])

    def _quarterly(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        revenue = _fact_series(value, ("revenue", "total_revenue"), "QUARTERLY")
        earnings = _fact_series(value, ("pat", "net_income", "net_profit"), "QUARTERLY")
        eps = _fact_series(value, ("eps",), "QUARTERLY")
        ebitda = _fact_series(value, ("ebitda", "operating_income", "operating_profit"), "QUARTERLY")
        for name, series, weight in (
            ("REVENUE_YOY", revenue, 25),
            ("PAT_YOY", earnings, 25),
            ("EPS_YOY", eps, 15),
        ):
            comparison = _comparable_yoy(series)
            if comparison:
                change, latest, prior = comparison
                datum = _derived_datum(change, "PERCENT", latest, f"derived:quarterly-{name.lower()}", prior)
                metrics.append((_metric_result(name, datum, _growth_score(change), f"COMPARABLE_QUARTER_{name}_V1", "PERCENT"), weight))
        for name, series, weight in (("REVENUE_QOQ", revenue, 5), ("PAT_QOQ", earnings, 5)):
            comparison = _sequential_change(series)
            if comparison:
                change, latest, prior = comparison
                datum = _derived_datum(change, "PERCENT", latest, f"derived:quarterly-{name.lower()}", prior)
                metrics.append((_metric_result(name, datum, _growth_score(change), f"SEQUENTIAL_{name}_LIMITED_WEIGHT_V1", "PERCENT"), weight))
        margins = _aligned_ratios(ebitda, revenue, multiplier=Decimal("100"))
        if len(margins) >= 2:
            change = margins[-1] - margins[-2]
            datum = _derived_from_many(change, "PERCENTAGE_POINTS", [*ebitda[-2:], *revenue[-2:]], "derived:quarterly-margin-trend")
            metrics.append((_metric_result("OPERATING_MARGIN_TREND", datum, _higher_better(change, ((-8, 8), (-3, 30), (0, 55), (2, 75), (5, 92))), "QUARTERLY_MARGIN_TREND_V1", "PERCENTAGE_POINTS"), 15))
        if len(earnings) >= 3:
            consistency = Decimal(sum(1 for item in earnings[-4:] if item.value > 0)) / Decimal(len(earnings[-4:])) * 100
            datum = _derived_from_many(consistency, "PERCENT", earnings[-4:], "derived:quarterly-consistency")
            metrics.append((_metric_result("EARNINGS_CONSISTENCY", datum, float(consistency), "POSITIVE_QUARTER_SHARE_V1", "PERCENT"), 10))
        return self._finish(value, RuleEngineArea.QUARTERLY_EARNINGS_TREND, metrics, [] if metrics else ["COMPARABLE_QUARTERLY_RESULTS"])

    def _catalysts(self, value: StockRuleEngineInput) -> AreaScoreResult:
        relevant = [event for event in value.events if _material_catalyst(event)]
        metrics = [(_event_metric(event, "MATERIAL_CATALYST_EVENT_V1"), 1) for event in relevant]
        return self._finish(value, RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS, metrics, [] if metrics else ["MATERIAL_ISSUER_RELEVANT_CATALYST_EVIDENCE"])

    def _technical(self, value: StockRuleEngineInput) -> AreaScoreResult:
        prices = _usable_prices(value.market_prices)
        if len(prices) < 50:
            return self._finish(value, RuleEngineArea.PRICE_TECHNICAL, [], ["FIFTY_OBSERVATION_TECHNICAL_BASIS"])
        latest = prices[-1]
        latest_datum = _price_datum(latest)
        metrics: list[tuple[RuleMetricResult, int]] = []
        basis50 = Decimal(str(median(float(item.price) for item in prices[-50:])))
        relative50 = (latest.price / basis50 - 1) * 100
        rel50 = _derived_datum(relative50, "PERCENT", latest_datum, "derived:price-vs-median-50")
        metrics.append((_metric_result("PRICE_VS_50_OBSERVATION_MEDIAN", rel50, _trend_score(relative50), "PRICE_VS_50_OBSERVATION_MEDIAN_V1", "PERCENT"), 35))
        if len(prices) >= 150:
            basis150 = Decimal(str(median(float(item.price) for item in prices[-150:])))
            relative150 = (latest.price / basis150 - 1) * 100
            rel150 = _derived_datum(relative150, "PERCENT", latest_datum, "derived:price-vs-median-150")
            metrics.append((_metric_result("PRICE_VS_150_OBSERVATION_MEDIAN", rel150, _trend_score(relative150), "PRICE_VS_150_OBSERVATION_MEDIAN_V1", "PERCENT"), 35))
        window = prices[-150:] if len(prices) >= 150 else prices[-50:]
        peak = max(item.price for item in window)
        drawdown = (latest.price / peak - 1) * 100
        drawdown_datum = _derived_datum(drawdown, "PERCENT", latest_datum, "derived:drawdown")
        metrics.append((_metric_result("DRAWDOWN_FROM_OBSERVATION_PEAK", drawdown_datum, _higher_better(drawdown, ((-50, 5), (-30, 25), (-15, 52), (-5, 75), (0, 90))), "PRICE_DRAWDOWN_V1", "PERCENT"), 20))
        start = _price_datum(window[0])
        trend = (latest.price / window[0].price - 1) * 100
        trend_datum = _derived_datum(trend, "PERCENT", latest_datum, "derived:price-trend", start)
        metrics.append((_metric_result("OBSERVATION_WINDOW_TREND", trend_datum, _trend_score(trend), "DURABLE_PRICE_TREND_V1", "PERCENT"), 10))
        return self._finish(value, RuleEngineArea.PRICE_TECHNICAL, metrics, [])

    def _news(self, value: StockRuleEngineInput) -> AreaScoreResult:
        from app.news_intelligence import aggregate_impact, search_state
        normalized = aggregate_impact(value.news_features, value.evaluated_at)
        state = search_state(value.news_search_run, value.evaluated_at)
        if normalized is not None or state == 'READY_NO_EVENTS':
            impact = normalized if normalized is not None else 0
            refs = sorted(str(f.feature_id) for f in value.news_features) if normalized is not None else ['search-run:'+str(value.news_search_run.run_id)]
            metric = RuleMetricResult(metric='COMPANY_NEWS_IMPACT',value=impact,unit='IMPACT_MINUS100_PLUS100',score=50+impact/2,
                rule='COMPANY_EXPOSURE_IMPACT_V2',source='PERSISTED_NEWS_INTELLIGENCE',evidence_references=refs)
            result=self._finish(value,RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS,[(metric,1)],[])
            if normalized is not None and state not in {'READY_WITH_EVENTS','READY_NO_EVENTS'}:
                # Stale discovery reduces coverage/confidence, but does not
                # expire a still-relevant, independently persisted event.
                result=result.model_copy(update={'status':AreaScoreStatus.PARTIAL})
            return result
        events: list[ResearchEvent] = []
        seen: set[tuple[str, str, str]] = set()
        for event in value.events:
            event_at = event.event_date or event.published_at
            if event.status == ResearchLifecycleStatus.REJECTED or event_at is None:
                continue
            age = value.evaluated_at - _aware(event_at)
            if age < timedelta(0) or age > timedelta(days=CURRENT_NEWS_WINDOW_DAYS):
                continue
            if not _issuer_or_proven_exposure(event, value):
                continue
            key = (event.source_url.casefold(), event.title.strip().casefold(), event_at.date().isoformat())
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
        metrics = [(_event_metric(event, "CURRENT_EVENT_IMPACT_V1"), 1) for event in events]
        return self._finish(value, RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS, metrics, [] if metrics else ["RELEVANT_CURRENT_EVENT_WITHIN_30_DAYS"])

    def _shareholding(self, value: StockRuleEngineInput) -> AreaScoreResult:
        if not _is_india(value.profile):
            return self._finish(value, RuleEngineArea.SHAREHOLDING, [], [], applicable=False)
        if not value.shareholding:
            return self._finish(value, RuleEngineArea.SHAREHOLDING, [], ["LATEST_VALID_SHAREHOLDING_PERIOD"])
        snapshots = sorted(value.shareholding, key=lambda item: item.period_end)
        latest = snapshots[-1]
        latest_values = {entry.category: entry for entry in latest.values}
        metrics: list[tuple[RuleMetricResult, int]] = []
        promoter = latest_values.get(ShareholdingCategory.PROMOTER)
        if promoter:
            datum = _shareholding_datum(latest, promoter.percentage, _enum_text(promoter.category))
            metrics.append((_metric_result("PROMOTER_HOLDING", datum, _higher_better(datum.value, ((0, 25), (20, 45), (40, 65), (55, 78), (70, 82))), "PROMOTER_HOLDING_LEVEL_V1", "PERCENT"), 25))
        pledge = latest_values.get(ShareholdingCategory.PROMOTER_PLEDGE)
        if pledge:
            datum = _shareholding_datum(latest, pledge.percentage, _enum_text(pledge.category))
            metrics.append((_metric_result("PROMOTER_PLEDGE", datum, _lower_better(datum.value, ((0, 95), (5, 75), (15, 48), (30, 20), (50, 5))), "PROMOTER_PLEDGE_V1", "PERCENT"), 35))
        if len(snapshots) >= 2:
            previous_values = {entry.category: entry for entry in snapshots[-2].values}
            for category, name, weight in (
                (ShareholdingCategory.PROMOTER, "PROMOTER_TREND", 20),
                (ShareholdingCategory.FII_FPI, "FII_FPI_TREND", 10),
                (ShareholdingCategory.DII, "DII_TREND", 10),
            ):
                current = latest_values.get(category)
                previous = previous_values.get(category)
                if current and previous:
                    change = current.percentage - previous.percentage
                    datum = _shareholding_datum(latest, change, name, extra_snapshot=snapshots[-2])
                    metrics.append((_metric_result(name, datum, _higher_better(change, ((-5, 15), (-2, 35), (0, 55), (2, 75), (5, 92))), f"{name}_QUARTERLY_V1", "PERCENTAGE_POINTS"), weight))
        return self._finish(value, RuleEngineArea.SHAREHOLDING, metrics, [] if metrics else ["STRUCTURED_OWNERSHIP_VALUES"])

    def _governance(self, value: StockRuleEngineInput) -> AreaScoreResult:
        events = [event for event in value.events if _governance_event(event)]
        metrics: list[tuple[RuleMetricResult, int]] = []
        for event in events:
            text = f"{event.title} {event.summary}".casefold()
            resolved = any(_contains_term(text, term) for term in _RESOLUTION_TERMS) or event.status == ResearchLifecycleStatus.REJECTED
            if resolved:
                score = Decimal("60") if event.status != ResearchLifecycleStatus.REJECTED else Decimal("65")
            else:
                score = Decimal(str(_event_metric_score(event)))
            datum = _event_datum(event)
            metrics.append((_metric_result("GOVERNANCE_EVIDENCE", datum, float(score), "UNRESOLVED_GOVERNANCE_HISTORY_V1" if not resolved else "RESOLVED_GOVERNANCE_EVIDENCE_V1", "EVENT"), 1))
        latest = value.shareholding[-1] if value.shareholding else None
        if latest:
            pledge = next((entry for entry in latest.values if entry.category == ShareholdingCategory.PROMOTER_PLEDGE), None)
            if pledge:
                datum = _shareholding_datum(latest, pledge.percentage, "PROMOTER_PLEDGE_GOVERNANCE")
                metrics.append((_metric_result("PROMOTER_PLEDGE_GOVERNANCE", datum, _lower_better(datum.value, ((0, 95), (5, 75), (15, 45), (30, 18), (50, 5))), "PROMOTER_PLEDGE_GOVERNANCE_V1", "PERCENT"), 1))
        return self._finish(value, RuleEngineArea.MANAGEMENT_GOVERNANCE, metrics, [] if metrics else ["STRUCTURED_GOVERNANCE_EVIDENCE"])

    def _sector_macro(self, value: StockRuleEngineInput) -> AreaScoreResult:
        metrics: list[tuple[RuleMetricResult, int]] = []
        sector = _sector(value)
        if sector:
            datum = _Datum(Decimal("50"), "CANONICAL_IDENTITY", None, value.evaluated_at, f"canonical-sector:{value.profile.instrument_id}:{sector.casefold()}", "CATEGORY", 4)
            metrics.append((_metric_result("CANONICAL_SECTOR_BASIS", datum, 50, "CANONICAL_SECTOR_NEUTRAL_BASIS_V1", "CATEGORY", display_value=sector), 20))
        structured = _structured_data(value)
        performance = _pick(structured, "sectorperformance", "sectorreturn")
        if performance:
            pct = _as_percent(performance.value)
            metrics.append((_metric_result("SECTOR_PERFORMANCE", _derived_datum(pct, "PERCENT", performance, "normalized:sector-performance"), _trend_score(pct), "DURABLE_SECTOR_PERFORMANCE_V1", "PERCENT"), 45))
        macro_events = [event for event in value.events if _is_macro_event(event) and _proven_macro_exposure(event, value)]
        metrics.extend((_event_metric(event, "PROVEN_MACRO_EXPOSURE_IMPACT_V1"), 35) for event in macro_events)
        return self._finish(value, RuleEngineArea.SECTOR_MACRO, metrics, [] if metrics else ["CANONICAL_SECTOR_OR_DURABLE_MACRO_EXPOSURE"])

    def _finish(
        self,
        value: StockRuleEngineInput,
        area: RuleEngineArea,
        weighted_metrics: Sequence[tuple[RuleMetricResult, int]],
        missing: Sequence[str],
        *,
        applicable: bool = True,
    ) -> AreaScoreResult:
        readiness = [item for item in value.readiness.requirements if item.rule_engine_area == area]
        source_references = _readiness_source_references(readiness)
        readiness_evidence = {
            evidence_id for item in readiness for evidence_id in item.evidence_ids
        }
        readiness_missing = [entry for item in readiness for entry in item.missing_input_ids]
        missing_inputs = sorted(set(missing) | set(readiness_missing))
        if readiness and all(item.status == ResearchRequirementStatus.NOT_APPLICABLE for item in readiness):
            return AreaScoreResult(area=area, weight=STOCK_RULE_ENGINE_AREA_WEIGHTS[area],
                status=AreaScoreStatus.NOT_APPLICABLE, applicable=False,
                evidence_references=[], source_references=[])
        if not applicable or (readiness and all(item.status == ResearchRequirementStatus.UNSUPPORTED for item in readiness)):
            return AreaScoreResult(
                area=area,
                weight=STOCK_RULE_ENGINE_AREA_WEIGHTS[area],
                status=AreaScoreStatus.UNSUPPORTED,
                applicable=False,
                evidence_references=sorted(readiness_evidence),
                source_references=source_references,
            )
        total_weight = sum(weight for _, weight in weighted_metrics)
        metrics = [
            item.model_copy(
                update={
                    "configured_subrule_weight": weight,
                    "applied_weight_pct": _round_score(
                        Decimal(weight) * 100 / Decimal(total_weight)
                    ),
                }
            )
            for item, weight in weighted_metrics
        ]
        if not metrics:
            return AreaScoreResult(
                area=area,
                weight=STOCK_RULE_ENGINE_AREA_WEIGHTS[area],
                status=AreaScoreStatus.UNSCORABLE,
                applicable=True,
                evidence_references=sorted(readiness_evidence),
                source_references=source_references,
                missing_inputs=missing_inputs,
            )
        raw = _round_score(sum(Decimal(str(item.score)) * weight for item, weight in weighted_metrics) / Decimal(total_weight))
        if any(item.status == ResearchRequirementStatus.CONFLICTING for item in readiness):
            status = AreaScoreStatus.CONFLICTING
        elif any(item.status in {ResearchRequirementStatus.MISSING, ResearchRequirementStatus.PARTIAL, ResearchRequirementStatus.FAILED, ResearchRequirementStatus.REFRESHING} for item in readiness) or missing:
            status = AreaScoreStatus.PARTIAL
        elif any(item.status == ResearchRequirementStatus.READY_STALE for item in readiness):
            status = AreaScoreStatus.READY_STALE
        else:
            status = AreaScoreStatus.READY_FRESH
        evidence = sorted(
            readiness_evidence
            | {ref for item in metrics for ref in item.evidence_references}
        )
        positives = [f"{item.metric}: {item.score:.2f}/100" for item in metrics if item.score >= 65]
        negatives = [f"{item.metric}: {item.score:.2f}/100" for item in metrics if item.score <= 40]
        return AreaScoreResult(
            area=area,
            weight=STOCK_RULE_ENGINE_AREA_WEIGHTS[area],
            raw_score=raw,
            weighted_contribution=_round_score(raw * STOCK_RULE_ENGINE_AREA_WEIGHTS[area] / 100),
            status=status,
            applicable=True,
            metrics=metrics,
            positive_factors=positives,
            negative_factors=negatives,
            evidence_references=evidence,
            source_references=source_references,
            missing_inputs=missing_inputs,
        )

    @staticmethod
    def _confidence(readiness: ResearchReadinessResult) -> float:
        supported = [item for item in readiness.requirements if item.status not in {ResearchRequirementStatus.UNSUPPORTED, ResearchRequirementStatus.NOT_APPLICABLE}]
        authority_points = {
            ResearchSourceTier.OFFICIAL: 100,
            ResearchSourceTier.REGULATORY: 100,
            ResearchSourceTier.TRUSTED_MARKET_DATA: 90,
            ResearchSourceTier.LICENSED_STRUCTURED: 80,
            ResearchSourceTier.APPROVED_SECONDARY: 65,
            ResearchSourceTier.APPROVED_EXTERNAL_TOOL: 50,
            ResearchSourceTier.USER_UPLOAD: 40,
            ResearchSourceTier.UNVERIFIED: 20,
        }
        freshness_points = {
            ResearchRequirementStatus.READY_FRESH: 100,
            ResearchRequirementStatus.READY_STALE: 55,
            ResearchRequirementStatus.PARTIAL: 40,
            ResearchRequirementStatus.CONFLICTING: 20,
            ResearchRequirementStatus.REFRESHING: 20,
            ResearchRequirementStatus.MISSING: 0,
            ResearchRequirementStatus.FAILED: 0,
        }
        sourced = [authority_points[item.source_tier] for item in supported if item.source_tier]
        authority = sum(sourced) / len(sourced) if sourced else 0
        freshness = sum(freshness_points.get(item.status, 0) for item in supported) / len(supported) if supported else 0
        conflicts = sum(item.status == ResearchRequirementStatus.CONFLICTING for item in supported)
        score = (
            Decimal(readiness.critical_completeness_pct) * Decimal("0.40")
            + Decimal(readiness.overall_completeness_pct) * Decimal("0.20")
            + Decimal(str(authority)) * Decimal("0.20")
            + Decimal(str(freshness)) * Decimal("0.20")
            - Decimal(min(30, conflicts * 15))
        )
        return _round_score(_clamp(score))

    @staticmethod
    def _decision(
        score: float | None,
        readiness: ResearchReadinessResult,
        confidence: ConfidenceLevel,
        *,
        partial: bool,
        overrides: Sequence[RiskOverrideResult],
    ) -> DecisionSignal:
        if score is None:
            return DecisionSignal.INSUFFICIENT_DATA
        if any(item.effect == "BLOCK_BUY" for item in overrides):
            if any(item.severity == RiskOverrideSeverity.CRITICAL for item in overrides):
                return DecisionSignal.EXIT_REVIEW
            return DecisionSignal.AVOID
        signal = (
            DecisionSignal.STRONG_BUY if score >= 85 else
            DecisionSignal.BUY if score >= 75 else
            DecisionSignal.ACCUMULATE if score >= 65 else
            DecisionSignal.HOLD if score >= 50 else
            DecisionSignal.REDUCE if score >= 35 else
            DecisionSignal.AVOID if score >= 20 else
            DecisionSignal.EXIT_REVIEW
        )
        critical_fresh = all(
            readiness.for_requirement(key).status == ResearchRequirementStatus.READY_FRESH
            for key in StockRuleEngineEligibilityPolicy.CRITICAL_REQUIREMENTS
        )
        if signal == DecisionSignal.STRONG_BUY and (
            readiness.critical_completeness_pct < 100
            or confidence != ConfidenceLevel.HIGH
            or not critical_fresh
        ):
            signal = DecisionSignal.BUY
        if any(item.status == ResearchRequirementStatus.CONFLICTING and item.mandatory for item in readiness.requirements) and signal in {DecisionSignal.STRONG_BUY, DecisionSignal.BUY}:
            signal = DecisionSignal.HOLD
        if partial and signal in {DecisionSignal.STRONG_BUY, DecisionSignal.BUY, DecisionSignal.ACCUMULATE}:
            signal = DecisionSignal.HOLD
        return signal

    def _risk_overrides(self, value: StockRuleEngineInput) -> list[RiskOverrideResult]:
        overrides: list[RiskOverrideResult] = []
        from app.news_intelligence import impact_at, latest_known_features
        for feature in latest_known_features(value.news_features,value.evaluated_at):
            if feature.severe_validated and impact_at(feature,value.evaluated_at) is not None:
                overrides.append(RiskOverrideResult(code='VALIDATED_'+feature.event_type,severity=RiskOverrideSeverity.CRITICAL,
                    evidence_ids=[str(feature.feature_id)]))
        authoritative = [event for event in value.events if _authoritative_unresolved_event(event)]
        for event in authoritative:
            text = f"{event.title} {event.summary}".casefold()
            evidence = [f"event:{event.event_id}:GOVERNANCE_HISTORY"]
            if any(term in text for term in _FRAUD_TERMS) and event.impact == EventImpact.STRONG_NEGATIVE:
                overrides.append(RiskOverrideResult(code="CONFIRMED_FRAUD_OR_ACCOUNTING_CRISIS", severity=RiskOverrideSeverity.CRITICAL, evidence_ids=evidence))
            elif event.event_type == ResearchEventType.REGULATORY_EVENT and event.impact == EventImpact.STRONG_NEGATIVE and any(term in text for term in _CRITICAL_REGULATORY_TERMS):
                overrides.append(RiskOverrideResult(code="CRITICAL_REGULATORY_ACTION", severity=RiskOverrideSeverity.CRITICAL, evidence_ids=evidence))
            elif event.event_type == ResearchEventType.MANAGEMENT_CHANGE and event.impact == EventImpact.STRONG_NEGATIVE and any(term in text for term in ("auditor", "resignation", "qualification")):
                overrides.append(RiskOverrideResult(code="SEVERE_UNRESOLVED_GOVERNANCE", severity=RiskOverrideSeverity.HIGH, evidence_ids=evidence))
        if not _is_financial(value):
            extreme = _extreme_balance_sheet_evidence(value)
            if extreme:
                overrides.append(RiskOverrideResult(code="EXTREME_BALANCE_SHEET_STRESS", severity=RiskOverrideSeverity.CRITICAL, evidence_ids=extreme))
        negative_guidance = [event for event in authoritative if event.event_type in {ResearchEventType.GUIDANCE_CUT, ResearchEventType.ORDER_CANCELLED, ResearchEventType.PROJECT_DELAY}]
        independent = {event.independence_key or event.source_url for event in negative_guidance}
        if len(independent) >= 2:
            overrides.append(RiskOverrideResult(code="BROKEN_INVESTMENT_THESIS", severity=RiskOverrideSeverity.HIGH, evidence_ids=sorted(f"event:{event.event_id}:GOVERNANCE_HISTORY" for event in negative_guidance)))
        unique: dict[str, RiskOverrideResult] = {}
        for item in overrides:
            unique.setdefault(item.code, item)
        return list(unique.values())


def analysis_eligibility_response(readiness: ResearchReadinessResult) -> dict[str, Any]:
    return StockRuleEngineEligibilityPolicy().evaluate(readiness).model_dump(
        mode="json", by_alias=True
    )


def _readiness_source_references(
    readiness: Sequence[ResearchRequirementReadiness],
) -> list[RuleSourceReference]:
    grouped: dict[
        tuple[str | None, ResearchSourceTier | None, str, datetime | None, datetime | None],
        set[str],
    ] = {}
    for item in readiness:
        if not item.source_url:
            continue
        key = (
            item.source,
            item.source_tier,
            item.source_url,
            item.as_of,
            item.retrieved_at,
        )
        grouped.setdefault(key, set()).update(item.evidence_ids)
    return [
        RuleSourceReference(
            source_provider=source,
            source_tier=source_tier,
            source_url=source_url,
            as_of=as_of,
            retrieved_at=retrieved_at,
            evidence_references=sorted(evidence_ids),
        )
        for (source, source_tier, source_url, as_of, retrieved_at), evidence_ids in sorted(
            grouped.items(), key=lambda entry: (
                entry[0][0] or "",
                entry[0][2],
                _iso(entry[0][3]) or "",
                _iso(entry[0][4]) or "",
            )
        )
    ]


def _structured_data(value: StockRuleEngineInput) -> dict[str, _Datum]:
    selected: dict[str, _Datum] = {}
    for record in value.structured_snapshots:
        authority = _provider_authority(record.provider, record.source_type, record.source_name)
        for raw_key, fact in record.snapshot.facts.items():
            number = _decimal(fact.value)
            if number is None:
                continue
            if raw_key == "latestPrice" and number <= 0:
                continue
            key = _metric_key(raw_key)
            datum = _Datum(
                number,
                fact.source_name or record.source_name or record.provider,
                fact.source_url or record.source_url,
                fact.as_of_date or record.market_as_of,
                f"structured:{record.provider}:{raw_key}:{record.retrieved_at.isoformat()}",
                fact.unit,
                authority,
            )
            existing = selected.get(key)
            if existing is None or (datum.authority, datum.as_of or datetime.min.replace(tzinfo=timezone.utc), datum.evidence_id) > (existing.authority, existing.as_of or datetime.min.replace(tzinfo=timezone.utc), existing.evidence_id):
                selected[key] = datum
    return selected


def _pick(values: Mapping[str, _Datum], *keys: str) -> _Datum | None:
    return next((values[_metric_key(key)] for key in keys if _metric_key(key) in values), None)


def _latest_fact(
    value: StockRuleEngineInput,
    metrics: Sequence[str],
    *,
    period_type: str | None = None,
) -> _Datum | None:
    series = _fact_series(value, metrics, period_type)
    return series[-1] if series else None


def _fact_series(
    value: StockRuleEngineInput,
    metrics: Sequence[str],
    period_type: str | None,
) -> list[_Datum]:
    keys = {_metric_key(metric) for metric in metrics}
    selected: dict[str, tuple[int, _Datum]] = {}
    for fact in value.financial_facts:
        if fact.source_mode != SourceMode.REAL or _metric_key(fact.key.metric) not in keys:
            continue
        if period_type and fact.key.period_type.upper() != period_type.upper():
            continue
        number = _decimal(fact.value.value)
        as_of = fact.value.as_of_date or _period_datetime(fact.key.period_end)
        if number is None or as_of is None:
            continue
        datum = _Datum(
            number,
            fact.source_provider,
            fact.value.source_url,
            as_of,
            f"financial:{fact.source_identity}:{fact.key.metric}:{fact.key.period_end}:{fact.key.period_type}",
            fact.value.unit,
            fact_source_authority(fact.source_tier),
        )
        period_key = f"{as_of.isoformat()}:{fact.key.reporting_basis or ''}"
        existing = selected.get(period_key)
        if existing is None or datum.authority > existing[0]:
            selected[period_key] = (datum.authority, datum)
    return sorted((entry[1] for entry in selected.values()), key=lambda item: item.as_of or datetime.min.replace(tzinfo=timezone.utc))


def _metric_result(
    name: str,
    datum: _Datum,
    score: float | Decimal,
    rule: str,
    unit: str | None,
    *,
    display_value: Any | None = None,
) -> RuleMetricResult:
    return RuleMetricResult(
        metric=name,
        value=_json_number(datum.value) if display_value is None else display_value,
        unit=unit or datum.unit,
        score=_round_score(score),
        rule=rule,
        source=datum.source,
        source_url=datum.source_url,
        as_of=datum.as_of,
        evidence_references=sorted(set(datum.evidence_id.split("|"))),
    )


def _derived_datum(value: Decimal, unit: str, primary: _Datum, evidence_id: str, *others: _Datum) -> _Datum:
    values = (primary, *others)
    return _Datum(
        value,
        primary.source,
        primary.source_url or next((item.source_url for item in others if item.source_url), None),
        max((item.as_of for item in values if item.as_of), default=None),
        "|".join([evidence_id, *(item.evidence_id for item in values)]),
        unit,
        max(item.authority for item in values),
    )


def _derived_from_many(value: Decimal, unit: str, inputs: Sequence[_Datum], evidence_id: str) -> _Datum:
    primary = inputs[-1]
    return _derived_datum(value, unit, primary, evidence_id, *inputs[:-1])


def _price_datum(value: MarketPriceObservation) -> _Datum:
    return _Datum(value.price, value.provider, value.source_url, _aware(value.observed_at), f"price:{value.provider}:{value.observed_at.isoformat()}", value.currency, _provider_authority(value.provider, None, None))


def _shareholding_datum(snapshot: ShareholdingSnapshot, number: Decimal, label: str, *, extra_snapshot: ShareholdingSnapshot | None = None) -> _Datum:
    ids = [f"shareholding:{snapshot.id}"]
    if extra_snapshot:
        ids.append(f"shareholding:{extra_snapshot.id}")
    return _Datum(number, snapshot.source_provider, snapshot.source_url, snapshot.period_end, "|".join(ids), "PERCENT", 5 if snapshot.source_provider.upper() == "NSE" else 3)


def _event_datum(event: ResearchEvent) -> _Datum:
    event_at = event.event_date or event.published_at or event.detected_at
    return _Datum(Decimal(str(_event_metric_score(event))), _enum_text(event.source_classification), event.source_url, _aware(event_at), f"event:{event.event_id}", "EVENT", _event_authority(event))


def _event_metric(event: ResearchEvent, rule: str) -> RuleMetricResult:
    datum = _event_datum(event)
    return _metric_result(_enum_text(event.event_type), datum, _event_metric_score(event), rule, "EVENT", display_value={"direction": _enum_text(event.impact), "confidence": event.confidence})


def _event_metric_score(event: ResearchEvent) -> float:
    direction = {
        EventImpact.STRONG_POSITIVE: Decimal("1"),
        EventImpact.POSITIVE: Decimal("0.6"),
        EventImpact.NEUTRAL: Decimal("0"),
        EventImpact.UNCERTAIN: Decimal("0"),
        EventImpact.NEGATIVE: Decimal("-0.6"),
        EventImpact.STRONG_NEGATIVE: Decimal("-1"),
    }[event.impact]
    severity = Decimal("1") if event.event_type in {
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
        ResearchEventType.ORDER_CANCELLED,
        ResearchEventType.PROJECT_DELAY,
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_CUT,
        ResearchEventType.REGULATORY_EVENT,
        ResearchEventType.MANAGEMENT_CHANGE,
    } else Decimal("0.75")
    if event.monetary_value is not None or event.capacity_value is not None or event.percentage_value is not None:
        severity = min(Decimal("1"), severity + Decimal("0.1"))
    reliability = {
        ReliabilityLevel.LEVEL_A: Decimal("1"),
        ReliabilityLevel.LEVEL_B: Decimal("0.85"),
        ReliabilityLevel.LEVEL_C: Decimal("0.65"),
        ReliabilityLevel.LEVEL_D: Decimal("0.4"),
        ReliabilityLevel.LEVEL_E: Decimal("0.2"),
    }[event.reliability]
    duration = {
        TimeHorizon.IMMEDIATE: Decimal("0.75"),
        TimeHorizon.SHORT_TERM: Decimal("0.85"),
        TimeHorizon.MEDIUM_TERM: Decimal("1"),
        TimeHorizon.LONG_TERM: Decimal("1"),
        TimeHorizon.UNKNOWN: Decimal("0.6"),
    }[event.time_horizon]
    probability = Decimal(str(event.confidence)) * reliability
    # V1 has no durable priced-in field. It applies a documented neutral factor
    # of 1.0 instead of guessing one.
    return _round_score(_clamp(Decimal("50") + direction * Decimal("50") * severity * probability * duration))


def _material_catalyst(event: ResearchEvent) -> bool:
    if event.source_mode != SourceMode.REAL or event.status != ResearchLifecycleStatus.VALIDATED or event.confidence < 0.6:
        return False
    if event.reliability not in {ReliabilityLevel.LEVEL_A, ReliabilityLevel.LEVEL_B}:
        return False
    if event.event_type not in _CATALYST_TYPES:
        return False
    explicit_materiality = any((event.monetary_value is not None, event.capacity_value is not None, event.percentage_value is not None, bool(event.customer), bool(event.counterparty)))
    authoritative = event.source_classification in _AUTHORITATIVE_CLASSIFICATIONS
    return explicit_materiality or authoritative


def _issuer_or_proven_exposure(event: ResearchEvent, value: StockRuleEngineInput) -> bool:
    if event.source_mode != SourceMode.REAL or event.confidence < 0.5:
        return False
    return _proven_macro_exposure(event, value) if _is_macro_event(event) else True


def _is_macro_event(event: ResearchEvent) -> bool:
    text = f"{event.title} {event.summary}".casefold()
    return any(_contains_term(text, term) for term in _MACRO_TERMS)


def _proven_macro_exposure(event: ResearchEvent, value: StockRuleEngineInput) -> bool:
    text = f"{event.title} {event.summary} {event.location or ''} {event.counterparty or ''}".casefold()
    sector = (_sector(value) or "").casefold()
    exposures = {
        "energy": ("oil", "crude", "gas", "commodity"),
        "airline": ("oil", "fuel", "jet fuel"),
        "aviation": ("oil", "fuel", "jet fuel"),
        "financial": ("interest rate", "central bank", "credit"),
        "bank": ("interest rate", "central bank", "credit"),
        "materials": ("commodity", "tariff", "supply chain"),
        "industrial": ("tariff", "supply chain", "commodity"),
        "technology": ("sanction", "tariff", "supply chain", "currency"),
        "consumer": ("currency", "commodity", "interest rate"),
        "automobile": ("commodity", "tariff", "supply chain"),
    }
    allowed = {term for label, terms in exposures.items() if label in sector for term in terms}
    # A durable structured relationship is required: both a recognized sector
    # sensitivity and that same sensitivity in the event evidence.
    return bool(allowed and any(_contains_term(text, term) for term in allowed))


def _governance_event(event: ResearchEvent) -> bool:
    text = f"{event.title} {event.summary}".casefold()
    return event.event_type in {ResearchEventType.MANAGEMENT_CHANGE, ResearchEventType.REGULATORY_EVENT, ResearchEventType.CREDIT_RATING, ResearchEventType.GUIDANCE_CUT} or any(term in text for term in _GOVERNANCE_TERMS)


def _authoritative_unresolved_event(event: ResearchEvent) -> bool:
    text = f"{event.title} {event.summary}".casefold()
    return (
        event.source_mode == SourceMode.REAL
        and event.status == ResearchLifecycleStatus.VALIDATED
        and event.reliability == ReliabilityLevel.LEVEL_A
        and event.source_classification in _AUTHORITATIVE_CLASSIFICATIONS
        and event.confidence >= 0.75
        and not any(_contains_term(text, term) for term in _RESOLUTION_TERMS)
    )


def _extreme_balance_sheet_evidence(value: StockRuleEngineInput) -> list[str]:
    structured = _structured_data(value)
    de = _pick(structured, "debttoequity")
    if de:
        de_pct = de.value if abs(de.value) > 5 else de.value * 100
    else:
        debt = _latest_fact(value, ("debt_or_borrowings", "total_debt"), period_type="ANNUAL")
        equity = _latest_fact(value, ("equity", "total_equity"), period_type="ANNUAL")
        if not debt or not equity or equity.value <= 0:
            return []
        de_pct = debt.value / equity.value * 100
        de = _derived_datum(de_pct, "PERCENT", debt, "derived:override-debt-equity", equity)
    ebitda = _latest_fact(value, ("ebitda", "ebit", "operating_income"), period_type="ANNUAL")
    finance_cost = _latest_fact(value, ("finance_cost", "interest_expense"), period_type="ANNUAL")
    if de_pct <= 300 or not ebitda or not finance_cost or finance_cost.value <= 0 or ebitda.value / finance_cost.value >= 1:
        return []
    # Both facts must originate from an official/regulatory durable tier.
    if de.authority < fact_source_authority(FactSourceTier.OFFICIAL_NSE) or ebitda.authority < fact_source_authority(FactSourceTier.OFFICIAL_NSE) or finance_cost.authority < fact_source_authority(FactSourceTier.OFFICIAL_NSE):
        return []
    return sorted(set(de.evidence_id.split("|") + ebitda.evidence_id.split("|") + finance_cost.evidence_id.split("|")))


def _area_weighted_score(values: Sequence[AreaScoreResult], *, areas: set[RuleEngineArea] | None = None) -> float | None:
    included = [item for item in values if item.raw_score is not None and (areas is None or item.area in areas)]
    total = sum(item.weight for item in included)
    if not total:
        return None
    return _round_score(sum(Decimal(str(item.raw_score)) * item.weight for item in included) / Decimal(total))


def _custom_area_score(values: Sequence[AreaScoreResult], weights: Mapping[RuleEngineArea, int]) -> float | None:
    included = [item for item in values if item.raw_score is not None and item.area in weights]
    total = sum(weights[item.area] for item in included)
    if not total:
        return None
    return _round_score(sum(Decimal(str(item.raw_score)) * weights[item.area] for item in included) / Decimal(total))


def _comparable_yoy(series: Sequence[_Datum]) -> tuple[Decimal, _Datum, _Datum] | None:
    if len(series) < 2:
        return None
    latest = series[-1]
    candidates = [item for item in series[:-1] if item.as_of and latest.as_of and 300 <= (latest.as_of - item.as_of).days <= 430]
    if not candidates:
        return None
    prior = min(candidates, key=lambda item: abs((latest.as_of - item.as_of).days - 365))
    if prior.value == 0:
        return None
    return (latest.value / abs(prior.value) - 1) * 100, latest, prior


def _sequential_change(series: Sequence[_Datum]) -> tuple[Decimal, _Datum, _Datum] | None:
    if len(series) < 2 or series[-2].value == 0:
        return None
    return (series[-1].value / abs(series[-2].value) - 1) * 100, series[-1], series[-2]


def _series_cagr(series: Sequence[_Datum]) -> Decimal | None:
    if len(series) < 3 or series[0].value <= 0 or series[-1].value <= 0 or not series[0].as_of or not series[-1].as_of:
        return None
    years = Decimal(str((series[-1].as_of - series[0].as_of).days / 365.25))
    if years <= Decimal("1"):
        return None
    return (Decimal(str(math.pow(float(series[-1].value / series[0].value), 1 / float(years)))) - 1) * 100


def _growth_consistency(*series_values: Sequence[_Datum]) -> tuple[Decimal, list[_Datum]] | None:
    changes: list[bool] = []
    evidence: list[_Datum] = []
    for series in series_values:
        for previous, current in zip(series[-5:-1], series[-4:]):
            changes.append(current.value >= previous.value)
            evidence.extend((previous, current))
    if not changes:
        return None
    return Decimal(sum(changes)) / Decimal(len(changes)) * 100, evidence


def _aligned_ratios(numerators: Sequence[_Datum], denominators: Sequence[_Datum], *, multiplier: Decimal) -> list[Decimal]:
    denominator_by_date = {item.as_of.date(): item for item in denominators if item.as_of and item.value != 0}
    ratios: list[Decimal] = []
    for numerator in numerators:
        denominator = denominator_by_date.get(numerator.as_of.date()) if numerator.as_of else None
        if denominator:
            ratios.append(numerator.value / denominator.value * multiplier)
    return ratios


def _usable_prices(values: Iterable[MarketPriceObservation]) -> list[MarketPriceObservation]:
    return sorted(
        (
            item for item in values
            if item.price is not None and Decimal(str(item.price)).is_finite() and item.price > 0
        ),
        key=lambda item: item.observed_at,
    )


def _lower_better(value: Decimal, points: Sequence[tuple[float, float]]) -> float:
    return _piecewise(value, points)


def _higher_better(value: Decimal, points: Sequence[tuple[float, float]]) -> float:
    return _piecewise(value, points)


def _piecewise(value: Decimal, points: Sequence[tuple[float, float]]) -> float:
    ordered = [(Decimal(str(x)), Decimal(str(y))) for x, y in points]
    if value <= ordered[0][0]:
        return _round_score(ordered[0][1])
    if value >= ordered[-1][0]:
        return _round_score(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            ratio = (value - left_x) / (right_x - left_x)
            return _round_score(left_y + ratio * (right_y - left_y))
    return 50.0


def _growth_score(value: Decimal) -> float:
    return _higher_better(value, ((-30, 5), (-10, 25), (0, 45), (10, 65), (20, 80), (40, 95)))


def _trend_score(value: Decimal) -> float:
    # Momentum gets no extra reward beyond +30%; V1 avoids rewarding parabolic
    # distance indefinitely.
    return _higher_better(value, ((-40, 8), (-20, 25), (-5, 45), (0, 55), (10, 72), (30, 88), (60, 78)))


def _provider_authority(provider: str | None, source_type: str | None, source_name: str | None) -> int:
    text = f"{provider or ''} {source_type or ''} {source_name or ''}".upper()
    if "SEC" in text or "REGULATORY" in text:
        return 5
    if "NSE" in text or "EXCHANGE" in text or "COMPANY FILING" in text:
        return 4
    if "EODHD" in text or "LICENSED" in text:
        return 3
    if "YAHOO" in text:
        return 2
    return 1


def _event_authority(event: ResearchEvent) -> int:
    if event.source_classification in {SourceClassification.REGULATORY, SourceClassification.EXCHANGE}:
        return 5
    if event.source_classification == SourceClassification.OFFICIAL_COMPANY:
        return 4
    if event.source_classification == SourceClassification.REPUTABLE_NEWS:
        return 2
    return 1


def _sector(value: StockRuleEngineInput) -> str | None:
    metadata = value.canonical_metadata
    raw = metadata.get("canonicalSector") or metadata.get("sector") or metadata.get("industrySector")
    if raw:
        return str(raw).strip()
    for key in ("sector", "industry"):
        for record in value.structured_snapshots:
            item = next((fact for raw_key, fact in record.snapshot.facts.items() if _metric_key(raw_key) == key and fact.value), None)
            if item:
                return str(item.value).strip()
    return None


def _is_financial(value: StockRuleEngineInput) -> bool:
    text = f"{_sector(value) or ''} {value.canonical_metadata.get('industry') or ''}".casefold()
    return any(token in text for token in ("bank", "financial", "nbfc", "insurance", "credit service"))


def _is_india(profile: CompanyResearchProfile) -> bool:
    return profile.country.strip().upper() in {"IN", "IND", "INDIA"} or profile.exchange.strip().upper() in {"NSE", "XNSE", "BSE", "XBOM"}


def _input_as_of(value: StockRuleEngineInput) -> datetime | None:
    dates: list[datetime] = []
    dates.extend(item.observed_at for item in _usable_prices(value.market_prices))
    dates.extend(item.value.as_of_date or _period_datetime(item.key.period_end) for item in value.financial_facts if item.value.as_of_date or item.key.period_end)
    dates.extend(item.market_as_of for item in value.structured_snapshots if item.market_as_of)
    dates.extend(item.event_date or item.published_at for item in value.events if item.event_date or item.published_at)
    dates.extend(item.period_end for item in value.shareholding)
    return max((_aware(item) for item in dates if item is not None), default=None)


def _confidence_level(score: float) -> ConfidenceLevel:
    return ConfidenceLevel.HIGH if score >= 80 else ConfidenceLevel.MEDIUM if score >= 55 else ConfidenceLevel.LOW


def _metric_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _contains_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", text) is not None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _as_percent(value: Decimal) -> Decimal:
    return value * 100 if abs(value) <= Decimal("2") else value


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("100"), value))


def _round_score(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _json_number(value: Decimal) -> float:
    return float(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _period_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            return None
    return _aware(parsed)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None


_AUTHORITATIVE_CLASSIFICATIONS = {
    SourceClassification.OFFICIAL_COMPANY,
    SourceClassification.REGULATORY,
    SourceClassification.EXCHANGE,
}
_CATALYST_TYPES = {
    ResearchEventType.ORDER_WIN,
    ResearchEventType.NEW_CONTRACT,
    ResearchEventType.CLIENT_WIN,
    ResearchEventType.NEW_PLANT,
    ResearchEventType.MAJOR_CORPORATE_ANNOUNCEMENT,
    ResearchEventType.NEW_ORDER,
    ResearchEventType.ORDER_BACKLOG_CHANGE,
    ResearchEventType.NEW_CUSTOMER,
    ResearchEventType.CUSTOMER_EXPANSION,
    ResearchEventType.MAJOR_CUSTOMER,
    ResearchEventType.CUSTOMER_LOSS,
    ResearchEventType.MAJOR_CONTRACT,
    ResearchEventType.GOVERNMENT_CONTRACT,
    ResearchEventType.CAPEX,
    ResearchEventType.FACTORY_EXPANSION,
    ResearchEventType.CAPACITY_EXPANSION,
    ResearchEventType.NEW_FACILITY,
    ResearchEventType.GEOGRAPHIC_EXPANSION,
    ResearchEventType.ACQUISITION,
    ResearchEventType.PARTNERSHIP,
    ResearchEventType.PRODUCT_LAUNCH,
    ResearchEventType.GUIDANCE_RAISED,
    ResearchEventType.GUIDANCE_LOWERED,
    ResearchEventType.GUIDANCE_MAINTAINED,
    ResearchEventType.GUIDANCE_CUT,
    ResearchEventType.ORDER_CANCELLED,
    ResearchEventType.PROJECT_DELAY,
}
_MACRO_TERMS = (
    "war", "sanction", "tariff", "interest rate", "central bank", "commodity",
    "oil", "crude", "currency", "supply chain", "geopolitical",
)
_GOVERNANCE_TERMS = (
    "fraud", "accounting", "auditor", "regulatory action", "enforcement",
    "litigation", "promoter pledge", "related party", "governance",
)
_FRAUD_TERMS = ("confirmed fraud", "accounting fraud", "accounting crisis", "financial misstatement")
_CRITICAL_REGULATORY_TERMS = ("license revoked", "trading ban", "insolvency", "criminal enforcement", "operations suspended")
_RESOLUTION_TERMS = ("resolved", "remediated", "cleared", "settled and closed", "no wrongdoing")
