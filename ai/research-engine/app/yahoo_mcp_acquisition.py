"""Targeted Yahoo acquisition behind the ExternalResearchToolGateway seam.

The read-side readiness and rule-engine paths never import or call this module.
Only the targeted readiness executor uses it, after DB-first planning has found
a stale or missing requirement. NSE financial inputs use official-first gap fill.
Acquisition priority is kept separate
from the existing durable fact authority/merge rules.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID, uuid5, NAMESPACE_URL

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.financial_gap_fill import FINANCIAL_GAP_REQUIREMENTS, load_financial_gap_state
from app.normalization import content_hash
from app.models import (
    CompanyResearchProfile,
    DocumentStatus,
    DocumentType,
    EventImpact,
    MarketPriceObservation,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceMode,
    SourceType,
    StructuredInstrumentResolution,
    StructuredMarketSnapshot,
    StructuredMarketSnapshotRecord,
    TimeHorizon,
)
from app.deep_investigation import acquisition_budget
from app.research_readiness import (
    ExternalResearchToolAuthorization, ResearchRefreshTarget, ResearchRequirementStatus,
    REQUIREMENT_STATUSES_NEEDING_ACQUISITION, ResearchReadinessService,
)
from app.research_readiness_runtime import (
    CapabilityExecutionProgress, CapabilityExecutionResult, RepositoryResearchReadinessAdapter,
)

logger = logging.getLogger(__name__)

YAHOO_FINANCE_MCP = "YAHOO_FINANCE_MCP"
_SUPPORTED_REGIONS = frozenset({"INDIA", "USA", "EUROPE"})
_MCP_FIRST_REQUIREMENTS = frozenset(
    {
        "LATEST_PRICE",
        "HISTORICAL_PRICE_SERIES",
        "VALUATION_INPUTS",
        "BUSINESS_QUALITY_FACTS",
        "GROWTH_FACTS",
        "BALANCE_SHEET_FACTS",
        "QUARTERLY_FINANCIALS",
        "CURRENT_NEWS",
        "ORDER_BOOK_CAPEX_GUIDANCE",
        "SHAREHOLDING",
        "SECTOR_MACRO",
    }
)

# Structural fact requirements whose provider contract is NSE-authoritative and
# whose absence after a *completed* Yahoo acquisition (gateway returned a
# well-formed, schema-valid result that yielded zero persistable facts) is a
# deterministic evidence gap -- not a transient empty response that a same-cycle
# repair retry could ever fill. This covers the gap-fill financial requirements
# (NSE is the authoritative source and already attempted via the official-first
# financial path / legacy fallback) plus VALUATION_INPUTS (MCP-first, also
# NSE-authoritative via the NSE_OR_APPROVED_STRUCTURED regional fallback above).
#
# When the provider call itself fails transiently it raises a distinct, retryable
# code (DOWNSTREAM_TIMEOUT / EXTERNAL_PROVIDER_UNAVAILABLE / EXTERNAL_SCHEMA_INVALID
# / EXTERNAL_IDENTITY_CONFLICT) -- those are NOT qualified here and stay
# TECHNICAL_RETRYABLE. Only the "provider responded successfully but facts are
# absent" outcome is rewritten to the permanent canonical reason
# PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS (see failure_taxonomy.PERMANENT_REASONS
# and research_readiness_runtime's matching treatment of an authoritatively-empty
# primary provider result).
_DETERMINISTIC_FINANCIAL_REQUIREMENTS = frozenset(
    (*FINANCIAL_GAP_REQUIREMENTS, "VALUATION_INPUTS")
)


def _qualify_empty_financial_result(requirement_id: str, safe_code: str) -> str:
    """Disambiguate an empty-but-completed provider result from a transient one.

    bare ``EXTERNAL_RESULT_INCOMPLETE`` is produced exclusively after a completed,
    schema-valid provider call that persisted zero facts (see YahooMcpResultPersister.
    persist()'s ``written < 1`` raise and the post-success recheck). For the
    structural financial requirements the gateway already exhausts its own
    transient retries internally (``_call`` retries DOWNSTREAM_TIMEOUT /
    EXTERNAL_PROVIDER_UNAVAILABLE / rate-limits up to max_retries before surfacing
    those distinct codes), so a successful ``acquire_requirement`` return with zero
    facts is a deterministic absence -- rewrite it to the permanent canonical
    reason so the aggregate does not mislabel it as a retryable technical failure.
    Every other producer of bare EXTERNAL_RESULT_INCOMPLETE (e.g. a HISTORICAL_PRICE
    _SERIES zero-facts response, whose gap could be transient by nature) is left
    TECHNICAL_RETRYABLE unchanged. Genuine technical codes never pass through here.
    """
    if (
        safe_code == "EXTERNAL_RESULT_INCOMPLETE"
        and requirement_id.strip().upper() in _DETERMINISTIC_FINANCIAL_REQUIREMENTS
    ):
        return "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
    return safe_code


class ExternalMcpAcquisitionError(Exception):
    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _Fact(_WireModel):
    metric: str
    value: Any
    unit: str | None = None
    as_of: datetime | None = Field(default=None, alias="asOf")
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    source_url: str = Field(alias="sourceUrl")
    confidence: float = Field(ge=0, le=1)
    raw_field_origin: str | None = Field(default=None, alias="rawFieldOrigin")


class _FinancialFact(_Fact):
    period_end: str = Field(alias="periodEnd")
    period_type: str = Field(alias="periodType")
    reporting_basis: str = Field(alias="reportingBasis")


class _Observation(_WireModel):
    observed_at: datetime = Field(alias="observedAt")
    price: Decimal
    currency: str | None = None

    @field_validator("price")
    @classmethod
    def positive_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("market price must be positive and finite")
        return value


class _Article(_WireModel):
    headline: str
    url: str
    published_at: datetime = Field(alias="publishedAt")
    publisher: str
    issuer_symbol: str = Field(alias="issuerSymbol")
    summary: str | None = None
    event_type: str | None = Field(default=None, alias="eventType")


class _CompanyProfile(_WireModel):
    company_name: str | None = Field(default=None, alias="companyName")
    sector: str | None = None
    industry: str | None = None


class _Shareholding(_WireModel):
    period_end: datetime = Field(alias="periodEnd")
    promoter_holding_percent: Decimal | None = Field(default=None, alias="promoterHoldingPercent")
    promoter_pledge_percent: Decimal | None = Field(default=None, alias="promoterPledgePercent")
    promoter_pledge_basis: str | None = Field(default=None, alias="promoterPledgeBasis")
    fii_fpi_percent: Decimal | None = Field(default=None, alias="fiiFpiPercent")
    dii_percent: Decimal | None = Field(default=None, alias="diiPercent")
    public_retail_percent: Decimal | None = Field(default=None, alias="publicRetailPercent")
    institutional_ownership_percent: Decimal | None = Field(
        default=None, alias="institutionalOwnershipPercent"
    )


class YahooMcpNormalizedResult(_WireModel):
    adapter_version: str = Field(alias="adapterVersion")
    provider_id: str = Field(alias="providerId")
    source_tier: str = Field(alias="sourceTier")
    source_tool: str = Field(alias="sourceTool")
    region: str
    requirement_id: str = Field(alias="requirementId")
    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    symbol: str
    exchange: str | None = None
    currency: str | None = None
    retrieved_at: datetime = Field(alias="retrievedAt")
    observed_at: datetime | None = Field(default=None, alias="observedAt")
    source_url: str = Field(alias="sourceUrl")
    confidence: float = Field(ge=0, le=1)
    freshness: str
    structured_facts: tuple[_Fact, ...] = Field(alias="structuredFacts")
    financial_facts: tuple[_FinancialFact, ...] = Field(alias="financialFacts")
    acquisition_outcome: str = Field(default="SUCCESS", alias="acquisitionOutcome")
    market_observations: tuple[_Observation, ...] = Field(alias="marketObservations")
    company_profile: _CompanyProfile | None = Field(default=None, alias="companyProfile")
    news: tuple[_Article, ...] = ()
    events: tuple[_Article, ...] = ()
    shareholding: _Shareholding | None = None

    @field_validator("provider_id")
    @classmethod
    def yahoo_only(cls, value: str) -> str:
        if value != YAHOO_FINANCE_MCP:
            raise ValueError("unexpected external provider")
        return value


@dataclass(frozen=True)
class ProviderAcquisitionRoute:
    region: str
    requirement_id: str
    providers: tuple[str, ...]


class McpFirstProviderPriority:
    """Central acquisition order. Durable evidence authority lives elsewhere."""

    _REGIONAL_FALLBACKS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
        "INDIA": {
            "LATEST_PRICE": ("YAHOO_FINANCE_REST",),
            "HISTORICAL_PRICE_SERIES": ("YAHOO_FINANCE_REST",),
            "VALUATION_INPUTS": ("NSE_OR_APPROVED_STRUCTURED",),
            "BUSINESS_QUALITY_FACTS": ("NSE",),
            "GROWTH_FACTS": ("NSE",),
            "BALANCE_SHEET_FACTS": ("NSE",),
            "QUARTERLY_FINANCIALS": ("NSE",),
            "CURRENT_NEWS": ("GLOBAL_NEWS_SEARCH", "NSE"),
            "ORDER_BOOK_CAPEX_GUIDANCE": ("NSE",),
            "SHAREHOLDING": ("NSE_XBRL",),
            "SECTOR_MACRO": ("EXISTING_APPROVED_RESEARCH",),
        },
        "USA": {
            "LATEST_PRICE": ("YAHOO_FINANCE_REST",),
            "HISTORICAL_PRICE_SERIES": ("YAHOO_FINANCE_REST",),
            "VALUATION_INPUTS": ("SEC_EDGAR_OR_APPROVED_STRUCTURED",),
            "BUSINESS_QUALITY_FACTS": ("SEC_EDGAR",),
            "GROWTH_FACTS": ("SEC_EDGAR",),
            "BALANCE_SHEET_FACTS": ("SEC_EDGAR",),
            "QUARTERLY_FINANCIALS": ("SEC_EDGAR",),
            "CURRENT_NEWS": ("GLOBAL_NEWS_SEARCH",),
            "ORDER_BOOK_CAPEX_GUIDANCE": ("SEC_EDGAR_OR_APPROVED_RESEARCH",),
            "SHAREHOLDING": ("UNAVAILABLE",),
            "SECTOR_MACRO": ("EXISTING_APPROVED_RESEARCH",),
        },
        "EUROPE": {
            "LATEST_PRICE": ("YAHOO_FINANCE_REST",),
            "HISTORICAL_PRICE_SERIES": ("YAHOO_FINANCE_REST",),
            "VALUATION_INPUTS": ("EODHD_OR_APPROVED_STRUCTURED",),
            "BUSINESS_QUALITY_FACTS": ("EODHD",),
            "GROWTH_FACTS": ("EODHD",),
            "BALANCE_SHEET_FACTS": ("EODHD",),
            "QUARTERLY_FINANCIALS": ("EODHD",),
            "CURRENT_NEWS": ("GLOBAL_NEWS_SEARCH",),
            "ORDER_BOOK_CAPEX_GUIDANCE": ("EODHD_OR_APPROVED_RESEARCH",),
            "SHAREHOLDING": ("UNAVAILABLE",),
            "SECTOR_MACRO": ("EXISTING_APPROVED_RESEARCH",),
        },
    }

    def route(self, region: str, requirement_id: str) -> ProviderAcquisitionRoute:
        normalized_region = region.strip().upper()
        normalized_requirement = requirement_id.strip().upper()
        fallbacks = self._REGIONAL_FALLBACKS.get(normalized_region, {}).get(
            normalized_requirement, ()
        )
        providers = (
            (YAHOO_FINANCE_MCP, *fallbacks)
            if normalized_region in _SUPPORTED_REGIONS
            and normalized_requirement in _MCP_FIRST_REQUIREMENTS
            else fallbacks
        )
        return ProviderAcquisitionRoute(normalized_region, normalized_requirement, providers)


class ExternalResearchToolGatewayClient(Protocol):
    async def acquire_requirement(
        self,
        profile: CompanyResearchProfile,
        *,
        region: str,
        requirement_id: str,
        authorization: ExternalResearchToolAuthorization,
        request_id: str,
        timeout_seconds: float | None = None,
        financial_gap_fill: bool = False,
    ) -> YahooMcpNormalizedResult: ...


class HttpExternalResearchToolGateway:
    """Narrow internal HTTP command; callers cannot submit a provider tool name."""

    def __init__(self, base_url: str, timeout_seconds: float, service_identity: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.service_identity = service_identity

    async def acquire_requirement(
        self,
        profile: CompanyResearchProfile,
        *,
        region: str,
        requirement_id: str,
        authorization: ExternalResearchToolAuthorization,
        request_id: str,
        timeout_seconds: float | None = None,
        financial_gap_fill: bool = False,
    ) -> YahooMcpNormalizedResult:
        symbol = profile.provider_instrument_ids.get("YAHOO_FINANCE")
        if not symbol:
            raise ExternalMcpAcquisitionError("VERIFIED_YAHOO_MAPPING_REQUIRED")
        payload = {
            "providerId": YAHOO_FINANCE_MCP,
            "region": region,
            "requirementId": requirement_id,
            "globalInstrumentId": str(profile.instrument_id),
            "providerSymbol": symbol,
            "expectedExchange": profile.exchange,
            "expectedCurrency": profile.currency,
            "authorization": authorization.as_gateway_payload(),
        }
        if financial_gap_fill:
            payload["financialGapFill"] = True
        headers = {
            "X-Request-ID": request_id,
            "X-Correlation-ID": request_id,
            "X-AIP-Service-Identity": self.service_identity,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/internal/v1/external-research/acquire",
                    json=payload,
                    headers=headers,
                    # Per-request override: caller may cap this specific call to
                    # whatever of a shared ensure() budget genuinely remains,
                    # without changing the client's own default timeout (used
                    # whenever no override is given -- every existing caller).
                    timeout=timeout_seconds if timeout_seconds is not None else self.timeout_seconds,
                )
        except httpx.TimeoutException as exc:
            raise ExternalMcpAcquisitionError("DOWNSTREAM_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ExternalMcpAcquisitionError("EXTERNAL_PROVIDER_UNAVAILABLE") from exc
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ExternalMcpAcquisitionError("EXTERNAL_SCHEMA_INVALID") from exc
        if not isinstance(envelope, dict):
            raise ExternalMcpAcquisitionError("EXTERNAL_SCHEMA_INVALID")
        if response.status_code >= 400 or envelope.get("ok") is not True:
            code = str((envelope.get("error") or {}).get("code") or "EXTERNAL_PROVIDER_UNAVAILABLE")
            raise ExternalMcpAcquisitionError(code)
        try:
            result = YahooMcpNormalizedResult.model_validate(envelope.get("data"))
        except ValidationError as exc:
            raise ExternalMcpAcquisitionError("EXTERNAL_SCHEMA_INVALID") from exc
        if result.global_instrument_id != profile.instrument_id:
            raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
        return result


class YahooMcpResultPersister:
    """Translate the wire contract into existing provider-neutral durable models."""

    def __init__(self, repository) -> None:
        self.repository = repository

    async def persist(
        self, result: YahooMcpNormalizedResult, profile: CompanyResearchProfile, *, financial_gaps=None
    ) -> int:
        if result.global_instrument_id != profile.instrument_id:
            raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
        if result.symbol.upper() != profile.provider_instrument_ids.get(
            "YAHOO_FINANCE", ""
        ).upper():
            raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
        if financial_gaps is not None:
            # The tool has capability-level granularity. Do not persist its
            # quote/summary/other-category payload alongside requested facts.
            selected, ambiguous = {}, set()
            for item in result.financial_facts:
                fact = self._financial_fact(item, result, profile)
                if not financial_gaps.accepts(result.requirement_id, fact, profile.currency):
                    continue
                previous = selected.get(fact.key)
                if previous is not None and (Decimal(str(previous.value)), previous.unit) != (fact.value.value, fact.value.unit):
                    ambiguous.add(fact.key)
                selected[fact.key] = item
            selected = tuple(item for key, item in selected.items() if key not in ambiguous)
            result = result.model_copy(update={"financial_facts": selected,
                "structured_facts": (), "market_observations": (), "news": (),
                "events": (), "shareholding": None, "company_profile": None})
        written = 0
        if result.structured_facts:
            await self._persist_structured(result, profile)
            written += len(result.structured_facts)
        financial = [
            self._financial_fact(item, result, profile)
            for item in result.financial_facts
        ]
        if financial:
            written += await self.repository.persist_international_financial_facts_async(financial)
            # A lower-tier fact may be rejected because an official value already exists.
            # The valid provider result still satisfies acquisition without downgrading it.
            if written == 0:
                written += len(financial)
        for item in result.market_observations:
            await self.repository.upsert_market_price_observation_async(
                MarketPriceObservation(
                    instrument_id=profile.instrument_id,
                    observed_at=_aware(item.observed_at),
                    price=item.price,
                    currency=item.currency or result.currency or profile.currency,
                    provider=YAHOO_FINANCE_MCP,
                    source_url=result.source_url,
                    retrieved_at=_aware(result.retrieved_at),
                )
            )
            written += 1
        for article in (*result.news, *result.events):
            await self._persist_article(article, result, profile)
            written += 1
        if result.shareholding is not None:
            await self._persist_shareholding(result.shareholding, result, profile)
            written += 1
        empty_news = result.requirement_id == "CURRENT_NEWS" and result.acquisition_outcome == "SUCCESS_EMPTY" and not result.news
        if written < 1 and not empty_news:
            raise ExternalMcpAcquisitionError("EXTERNAL_RESULT_INCOMPLETE")
        recorder = getattr(self.repository, "record_acquisition_observation", None)
        if callable(recorder):
            await recorder(profile.instrument_id, result.requirement_id, YAHOO_FINANCE_MCP,
                "SUCCESS_EMPTY" if empty_news else "SUCCESS", result.retrieved_at, result.source_url, evidence_count=written)
        return written

    async def _persist_structured(
        self, result: YahooMcpNormalizedResult, profile: CompanyResearchProfile
    ) -> None:
        facts = {
            item.metric: ProvenancedValue(
                value=item.value,
                unit=item.unit,
                as_of_date=_aware(item.as_of) if item.as_of else result.observed_at,
                source_url=item.source_url,
                source_name="Yahoo Finance MCP",
                source_type="EXTERNAL_MCP_PROVIDER",
                published_at=_aware(item.published_at) if item.published_at else None,
                retrieved_at=_aware(result.retrieved_at),
                confidence=item.confidence,
                calculation_basis=(
                    f"{result.source_tool}:{item.raw_field_origin}"
                    if item.raw_field_origin
                    else result.source_tool
                ),
            )
            for item in result.structured_facts
        }
        resolution = StructuredInstrumentResolution(
            instrument_id=profile.instrument_id,
            provider=YAHOO_FINANCE_MCP,
            provider_ticker=result.symbol,
            company_name=profile.company_name,
            exchange=result.exchange,
            currency=result.currency,
            confidence=result.confidence,
            resolved_at=_aware(result.retrieved_at),
            status="VERIFIED_MAPPING_VALIDATED",
        )
        snapshot = StructuredMarketSnapshot(
            resolution=resolution,
            status="STRUCTURED_PROVIDER_AVAILABLE",
            retrieved_at=_aware(result.retrieved_at),
            market_as_of=_aware(result.observed_at) if result.observed_at else None,
            source_name="Yahoo Finance MCP",
            source_type="EXTERNAL_MCP_PROVIDER",
            source_url=result.source_url,
            facts=facts,
            statement_facts=[],
            news=[],
            accepted_fields_count=len(facts),
        )
        now = datetime.now(timezone.utc)
        record = StructuredMarketSnapshotRecord(
            instrument_id=profile.instrument_id,
            provider=YAHOO_FINANCE_MCP,
            provider_instrument_id=result.symbol,
            exchange=result.exchange,
            mic=profile.mic,
            currency=result.currency,
            source_url=result.source_url,
            source_name="Yahoo Finance MCP",
            source_type="EXTERNAL_MCP_PROVIDER",
            source_identity=f"{YAHOO_FINANCE_MCP}:{result.symbol}:{result.source_tool}",
            market_as_of=snapshot.market_as_of,
            retrieved_at=snapshot.retrieved_at,
            persisted_at=now,
            last_price_at=snapshot.market_as_of if "latestPrice" in facts else None,
            last_valuation_at=(
                now
                if any(key in facts for key in ("trailingPE", "forwardPE", "priceToBook"))
                else None
            ),
            last_fundamentals_at=(
                now
                if any(key in facts for key in ("trailingEps", "roe", "roa", "roce", "sector"))
                else None
            ),
            last_success_at=now,
            last_provider_attempt_at=now,
            acquisition_status="SUCCESS",
            snapshot=snapshot,
        )
        await self.repository.persist_structured_market_snapshot_async(record)

    @staticmethod
    def _financial_fact(
        item: _FinancialFact,
        result: YahooMcpNormalizedResult,
        profile: CompanyResearchProfile,
    ) -> FinancialFact:
        return FinancialFact(
            FinancialFactKey(
                profile.instrument_id,
                item.metric,
                item.period_end,
                item.period_type,
                item.reporting_basis,
            ),
            ProvenancedValue(
                value=Decimal(str(item.value)),
                unit=item.unit,
                as_of_date=_aware(item.as_of) if item.as_of else _period_datetime(item.period_end),
                source_url=item.source_url,
                source_name="Yahoo Finance MCP",
                source_type="EXTERNAL_MCP_PROVIDER",
                published_at=_aware(item.published_at) if item.published_at else None,
                retrieved_at=_aware(result.retrieved_at),
                confidence=item.confidence,
                calculation_basis=(
                    f"{result.source_tool}:{item.raw_field_origin}"
                    if item.raw_field_origin
                    else result.source_tool
                ),
            ),
            FactSourceTier.YAHOO,
            YAHOO_FINANCE_MCP,
            (
                f"{YAHOO_FINANCE_MCP}:{result.symbol}:{result.source_tool}:"
                f"{item.metric}:{item.period_end}:{item.period_type}"
            ),
            SourceMode.REAL,
        )

    async def _persist_article(
        self,
        article: _Article,
        result: YahooMcpNormalizedResult,
        profile: CompanyResearchProfile,
    ) -> None:
        identity = f"{article.url}|{article.headline}|{article.published_at.date().isoformat()}"
        document_id = uuid5(NAMESPACE_URL, identity)
        document = ResearchDocument(
            document_id=document_id,
            canonical_url=article.url,
            original_url=article.url,
            title=article.headline,
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.REPUTABLE_NEWS,
            source_name="Yahoo Finance MCP",
            publisher=article.publisher,
            published_at=_aware(article.published_at),
            retrieved_at=_aware(result.retrieved_at),
            content_type="application/json",
            document_type=DocumentType.TEXT,
            raw_text=None,
            normalized_text=None,
            content_hash=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            instrument_id=profile.instrument_id,
            company_id=profile.company_id,
            country=profile.country,
            exchange=profile.exchange,
            status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_B,
            entity_resolution_confidence=0.99,
            source_mode=SourceMode.REAL,
            freshness="REAL",
            discovery_provider=YAHOO_FINANCE_MCP,
            # Deterministic SHA-256 identity (repository's established
            # convention, e.g. repository.py's source_independence_key=
            # content_hash(...)) over the case-insensitive canonical source
            # URL. Must never be the raw URL: source_independence_key is a
            # fixed-width CHAR(64) column and a raw URL is neither
            # guaranteed to be 64 characters nor safe to store there.
            source_independence_key=content_hash(article.url.casefold()),
        )
        event_type = _event_type(article.event_type)
        event = ResearchEvent(
            event_id=uuid5(NAMESPACE_URL, f"event|{identity}"),
            instrument_id=profile.instrument_id,
            company_id=profile.company_id,
            event_type=event_type,
            event_date=_aware(article.published_at),
            detected_at=_aware(result.retrieved_at),
            title=article.headline,
            summary=article.summary or article.headline,
            source_document_id=document_id,
            source_url=article.url,
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.REPUTABLE_NEWS,
            reliability=ReliabilityLevel.LEVEL_B,
            source_mode=SourceMode.REAL,
            confidence=result.confidence,
            impact=EventImpact.UNCERTAIN,
            time_horizon=TimeHorizon.UNKNOWN,
            status=ResearchLifecycleStatus.VALIDATED,
            raw_evidence_reference=article.headline,
            published_at=_aware(article.published_at),
            retrieved_at=_aware(result.retrieved_at),
            # research_events.independence_key is also a fixed-width CHAR(64)
            # column (same defect class as source_independence_key above): hash
            # the same provider-qualified composite identity that was
            # previously stored raw, via the shared content_hash() helper, so
            # the key stays deterministic per (provider, case-insensitive URL)
            # -- preserving the existing provider-distinction semantics -- while
            # always being exactly 64 lowercase hex characters.
            independence_key=content_hash(f"{YAHOO_FINANCE_MCP}:{article.url.casefold()}"),
        )
        await self.repository.persist_external_mcp_evidence_async(document, event)

    async def _persist_shareholding(
        self,
        value: _Shareholding,
        result: YahooMcpNormalizedResult,
        profile: CompanyResearchProfile,
    ) -> None:
        exact = {
            ShareholdingCategory.PROMOTER: value.promoter_holding_percent,
            ShareholdingCategory.PROMOTER_PLEDGE: value.promoter_pledge_percent,
            ShareholdingCategory.FII_FPI: value.fii_fpi_percent,
            ShareholdingCategory.DII: value.dii_percent,
            ShareholdingCategory.PUBLIC_RETAIL: value.public_retail_percent,
        }
        values = [
            ShareholdingSnapshotValue(
                category=category,
                percentage=percentage,
                metric_basis=(
                    value.promoter_pledge_basis
                    if category == ShareholdingCategory.PROMOTER_PLEDGE
                    else None
                ),
                raw_source_label=category.value,
                source_locator=result.source_tool,
            )
            for category, percentage in exact.items()
            if percentage is not None
        ]
        snapshot = ShareholdingSnapshot(
            instrument_id=profile.instrument_id,
            period_end=_aware(value.period_end),
            filing_basis="YAHOO_MCP_EXACT_NORMALIZED_FIELDS",
            source_provider=YAHOO_FINANCE_MCP,
            source_type="EXTERNAL_MCP_PROVIDER",
            source_identity_key=(
                f"{YAHOO_FINANCE_MCP}:{result.symbol}:"
                f"{value.period_end.date().isoformat()}"
            ),
            source_url=result.source_url,
            retrieved_at=_aware(result.retrieved_at),
            confidence=Decimal(str(result.confidence)),
            reliability_level=ReliabilityLevel.LEVEL_B,
            source_mode=SourceMode.REAL,
            values=values,
        )
        await self.repository.persist_external_mcp_shareholding_async(snapshot)


# Small, fixed buffer reserved out of the shared ensure() budget for a
# target's own post-response work (JSON/pydantic validation + the
# persistence write) after its network call returns -- not a connection
# timeout, and deliberately much smaller than the ~10s normal gateway
# timeout it is subtracted from. Also used as the minimum remaining
# budget below which starting a request is not worth attempting.
_RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS = 0.5


class McpFirstResearchCapabilityExecutor:
    """Use NSE-first financial gaps; retain MCP-first routing for other targets."""

    def __init__(
        self,
        legacy_executor,
        repository,
        gateway: ExternalResearchToolGatewayClient,
        *,
        enabled: bool,
        priority: McpFirstProviderPriority | None = None,
    ) -> None:
        self.legacy_executor = legacy_executor
        self.repository = repository
        self.gateway = gateway
        self.enabled = enabled
        self.priority = priority or McpFirstProviderPriority()
        self.persister = YahooMcpResultPersister(repository)
        # Candidate/cycle-scoped reuse for Yahoo MCP acquisitions, keyed by
        # the caller-supplied acquisition_context (asyncio.current_task(),
        # exactly the same explicit-token pattern already used for direct
        # yfinance reuse in app/structured_market.py -- never an implicit
        # wall-clock cache). A hit replays either the prior success result
        # (no second network round trip for an identical symbol+requirement
        # already acquired in this exact candidate/cycle) or the prior
        # failure's safe_code (bounded negative reuse -- no repeated
        # immediate retry against a provider that already timed out/failed
        # for this exact context). A different context (a different
        # candidate, or a later legitimate refresh cycle, since
        # asyncio.current_task() differs once this investigate() call has
        # returned and a new one begins) always gets a fresh attempt.
        self._context_results: "OrderedDict[object, dict[tuple, Any]]" = OrderedDict()
        self._context_results_max = 64

    def _context_result_slot(self, acquisition_context: object) -> dict[tuple, Any]:
        slot = self._context_results.get(acquisition_context)
        if slot is None:
            slot = {}
            self._context_results[acquisition_context] = slot
        self._context_results.move_to_end(acquisition_context)
        while len(self._context_results) > self._context_results_max:
            self._context_results.popitem(last=False)
        return slot

    async def _financial_state(self, instrument_id):
        return await self.repository._run_blocking_persistence(
            load_financial_gap_state, self.repository, instrument_id)

    def _uses_nse_financial_gaps(self, profile):
        return (self.enabled and profile.country.upper() in {"IN", "IND", "INDIA"}
                and profile.exchange.upper() in {"NSE", "XNSE"} and bool(profile.provider_instrument_ids.get("NSE"))
                and getattr(self.legacy_executor, "official_financial_provider", None) is not None
                and callable(getattr(self.legacy_executor, "acquire_official_financials", None)))

    async def _observe_failure(self, instrument_id, requirement_id, provider, reason):
        recorder = getattr(self.repository, "record_acquisition_observation", None)
        if callable(recorder):
            await recorder(instrument_id, requirement_id, provider, "FAILED",
                           datetime.now(timezone.utc), failure_reason=reason)

    async def _execute_financial_gaps(self, instrument_id, targets, *, correlation_id,
                                     identity_headers, progress, deadline):
        """NSE structured -> durable gaps -> filtered Yahoo -> existing fallback."""
        state = await self._financial_state(instrument_id)
        satisfied = {"READY_FRESH", "NOT_APPLICABLE"}
        profile = self.repository.profile(instrument_id)
        upgrade_due = getattr(self.repository, "financial_authority_upgrade_due", None)
        authority_upgrade = (any(target.requirement_id == "QUARTERLY_FINANCIALS"
                                 and target.reason == ResearchRequirementStatus.READY_FRESH for target in targets)
                             and callable(upgrade_due)
                             and await self.repository._run_blocking_persistence(
                                 upgrade_due, profile, datetime.now(timezone.utc)))
        due = [target for target in targets if
               state.readiness.for_requirement(target.requirement_id).status not in satisfied
               or (target.requirement_id == "QUARTERLY_FINANCIALS" and authority_upgrade)]
        executed, failures = [], {}
        if due:
            capability = "NSE:STRUCTURED_FINANCIALS"
            executed.append(capability)
            if progress is not None:
                progress.executed(capability)
            try:
                await self.legacy_executor.acquire_official_financials(instrument_id)
            except Exception as exc:
                for target in due:
                    reason = type(exc).__name__
                    failures[target.requirement_id] = reason
                    await self._observe_failure(instrument_id, target.requirement_id, "NSE", reason)
                    if progress is not None:
                        progress.failed(target.requirement_id, reason)
            if progress is not None:
                progress.completed(capability)
            state = await self._financial_state(instrument_id)

        context = asyncio.current_task()
        slot = self._context_result_slot(context) if context is not None else {}
        for target in targets:
            requirement_id = target.requirement_id
            row = state.readiness.for_requirement(requirement_id)
            if row.status in satisfied or not state.supported_inputs(requirement_id):
                continue
            capability = f"{YAHOO_FINANCE_MCP}:{requirement_id}"
            authorization = target.authority_policy.fallback_policy.authorize_external_tool(
                global_instrument_id=instrument_id, requirement_id=requirement_id,
                status=row.status, confidence=0.0, permitted_provider_ids=(YAHOO_FINANCE_MCP,))
            call_timeout = None
            if deadline is not None:
                call_timeout = min(getattr(self.gateway, "timeout_seconds", 10.0),
                    deadline - asyncio.get_running_loop().time() - _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS)
            try:
                if authorization is None:
                    raise ExternalMcpAcquisitionError("EXTERNAL_FALLBACK_NOT_AUTHORIZED")
                if call_timeout is not None and call_timeout <= _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS:
                    raise ExternalMcpAcquisitionError("BUDGET_EXHAUSTED")
                executed.append(capability)
                if progress is not None:
                    progress.executed(capability)
                key = (instrument_id, requirement_id, "FINANCIAL_GAPS")
                result = slot.get(key)
                if isinstance(result, tuple):
                    raise ExternalMcpAcquisitionError(result[1])
                if result is None:
                    try:
                        result = await self.gateway.acquire_requirement(profile, region="INDIA",
                            requirement_id=requirement_id, authorization=authorization,
                            request_id=correlation_id or str(instrument_id), timeout_seconds=call_timeout,
                            financial_gap_fill=True)
                    except ExternalMcpAcquisitionError as exc:
                        slot[key] = ("FAILED", exc.safe_code)
                        raise
                    slot[key] = result
                if result.requirement_id != requirement_id or result.region != "INDIA":
                    raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
                await self.persister.persist(result, profile, financial_gaps=state)
            except Exception as exc:
                reason = exc.safe_code if isinstance(exc, ExternalMcpAcquisitionError) else "EXTERNAL_PROVIDER_UNAVAILABLE"
                # Producer-site qualification (Root Cause A, extended to this
                # SECOND bare-EXTERNAL_RESULT_INCOMPLETE producer). This
                # function (_execute_financial_gaps) is the PRIMARY acquisition
                # path for QUARTERLY_FINANCIALS/GROWTH_FACTS/BUSINESS_QUALITY_FACTS
                # /BALANCE_SHEET_FACTS -- every `requirement_id` reached here is
                # already one of FINANCIAL_GAP_REQUIREMENTS (the caller filters
                # to that set before invoking this loop). execute_primary's two
                # producer sites already apply this same qualifier for
                # VALUATION_INPUTS and the generic post-repair-budget recheck;
                # without it here too, a completed-but-empty Yahoo financial-gap
                # call stayed bare EXTERNAL_RESULT_INCOMPLETE -> TECHNICAL_RETRYABLE
                # even though the provider genuinely returned zero facts -- the
                # exact deterministic-absence case this qualifier exists to
                # recognize. Left unqualified, repair kept re-attempting a
                # provider that had already proven (this same cycle) it has no
                # facts to give, which is a plausible contributor to
                # repair_attempted>0/repair_recovered=0 for these candidates.
                # _qualify_empty_financial_result is a no-op for every other
                # reason (genuine technical codes, EXTERNAL_PROVIDER_UNAVAILABLE,
                # composite reasons, etc.) -- see its docstring.
                # The audit observation (_observe_failure) and progress
                # reporting intentionally keep the RAW provider-reported
                # reason -- that is what actually happened and audit
                # observations must not be rewritten (see NO REGRESSION
                # RULES). Only the `failures` entry that feeds
                # classify_requirement_failures/repair-retryability is
                # qualified, matching how execute_primary's own producer
                # site treats this same distinction.
                qualified_reason = _qualify_empty_financial_result(requirement_id, reason)
                failures[requirement_id] = "|".join(filter(None, (failures.get(requirement_id), qualified_reason)))
                await self._observe_failure(instrument_id, requirement_id, YAHOO_FINANCE_MCP, reason)
                if progress is not None:
                    progress.failed(requirement_id, reason)
            if progress is not None:
                progress.completed(capability)
            # Provider SUCCESS is never a claim of requirement completeness.
            state = await self._financial_state(instrument_id)

        remaining = tuple(target for target in targets
                          if state.readiness.for_requirement(target.requirement_id).status not in satisfied)
        if remaining:
            legacy = await self.legacy_executor.execute_primary(instrument_id, remaining,
                jurisdiction="INDIA", correlation_id=correlation_id, identity_headers=identity_headers,
                progress=progress, deadline=deadline, official_financials_attempted=True)
            executed.extend(legacy.executed_capabilities)
            for key, reason in legacy.failures.items():
                failures[key] = "|".join(filter(None, (failures.get(key), reason)))
            state = await self._financial_state(instrument_id)
        completed = tuple(target.requirement_id for target in targets
                          if state.readiness.for_requirement(target.requirement_id).status in satisfied)
        for key in completed:
            failures.pop(key, None)
            if progress is not None:
                progress.satisfied(key)
        return CapabilityExecutionResult(tuple(dict.fromkeys(executed)), failures, completed)

    async def execute_primary(
        self,
        global_instrument_id: UUID,
        targets: Sequence[ResearchRefreshTarget],
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
        deadline: float | None = None,
    ) -> CapabilityExecutionResult:
        completed: set[str] = set()
        # Root-cause fix (Stage-2 final closure, Root Cause A): a requirement
        # whose own dedicated/authorized acquisition plan has fully run its
        # course this cycle (every legitimate source tried, genuine
        # evidence-absence verdict reached) must never be re-dispatched into
        # the legacy/capability-grouped executor "just in case" -- that
        # grouped path shares ONE RequirementAcquisitionBudget across
        # unrelated capabilities (e.g. SHAREHOLDING batched with
        # ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY, which genuinely do
        # use generic search/document discovery). Re-dispatching a
        # plan-exhausted requirement there both wastes a full redundant
        # acquisition pass (the long SHAREHOLDING single-flight wait) and
        # risks deep_investigation._finalize()'s budget.failures fallback
        # borrowing an unrelated group member's SEARCH_PROVIDER_* failure as
        # this requirement's own final reason. Unlike `completed`, this set
        # does NOT feed satisfied_requirement_ids -- a plan-exhausted
        # requirement is excluded from further acquisition this cycle, not
        # claimed as satisfied.
        plan_exhausted: set[str] = set()
        executed: list[str] = []
        mcp_failures: dict[str, str] = {}
        profile = self.repository.profile(global_instrument_id)
        request_id = correlation_id or str(global_instrument_id)
        financial_result = CapabilityExecutionResult()
        if jurisdiction == "INDIA" and self._uses_nse_financial_gaps(profile):
            financial_targets = tuple(target for target in targets if target.requirement_id in FINANCIAL_GAP_REQUIREMENTS)
            if financial_targets:
                financial_result = await self._execute_financial_gaps(global_instrument_id, financial_targets,
                    correlation_id=correlation_id, identity_headers=identity_headers, progress=progress, deadline=deadline)
                targets = tuple(target for target in targets if target.requirement_id not in FINANCIAL_GAP_REQUIREMENTS)
        authority_checked: set[str] = set()
        upgrade_due = getattr(self.repository, "financial_authority_upgrade_due", None)
        if (self.enabled and jurisdiction == "INDIA"
                and any(target.requirement_id == "QUARTERLY_FINANCIALS" for target in targets)
                and callable(upgrade_due)
                and await self.repository._run_blocking_persistence(
                    upgrade_due, profile, datetime.now(timezone.utc),
                )):
            # main.py installs this wrapper around the regional executor.
            # Yahoo success must not consume an outstanding NSE authority
            # upgrade before the repository ever sees FINANCIAL_RESULTS.
            capability = "NSE:QUARTERLY_FINANCIALS"
            executed.append(capability)
            if progress is not None:
                progress.executed(capability)
            try:
                await self.repository.refresh_targeted_categories(
                    global_instrument_id, {"FINANCIAL_RESULTS"},
                    correlation_id=correlation_id, allow_demo=False,
                    authority_upgrade_categories={"FINANCIAL_RESULTS"},
                )
            except Exception:
                mcp_failures["QUARTERLY_FINANCIALS"] = "NSE_FINANCIAL_UPGRADE_UNAVAILABLE"
                if progress is not None:
                    progress.failed("QUARTERLY_FINANCIALS", "NSE_FINANCIAL_UPGRADE_UNAVAILABLE")
            authority_checked.add("QUARTERLY_FINANCIALS")
            # Reassess actual persisted evidence, including retained secondary
            # fallback. Never infer completion from document/transport success.
            readiness = await self.repository._run_blocking_persistence(
                ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                global_instrument_id, jurisdiction=jurisdiction,
            )
            if readiness.for_requirement("QUARTERLY_FINANCIALS").status not in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
                completed.add("QUARTERLY_FINANCIALS")
                if progress is not None:
                    progress.satisfied("QUARTERLY_FINANCIALS")
        # This target's own network timeout is capped to whatever of the
        # shared ensure() budget genuinely remains (bounded above by the
        # gateway's own normal timeout), minus the small fixed margin above
        # for this target's own post-response work -- rather than refusing to
        # even start unless the FULL normal gateway timeout still fits. The
        # outer asyncio.timeout(ensure_timeout_seconds) wrapped around the
        # whole plan (ResearchReadinessRuntime._execute_plan_bounded) already
        # guarantees the shared ensure() budget itself is never exceeded even
        # without this; capping the per-call timeout exists purely so a
        # request that provably cannot get a useful answer in the time left is
        # skipped up front -- cleanly attributed as BUDGET_EXHAUSTED, no socket
        # opened -- instead of being started and either left for the coarser
        # outer cancellation to cut off mid-flight, or left running its own
        # full, uncoordinated timeout regardless of how little shared budget
        # is actually left.
        normal_timeout_seconds = getattr(self.gateway, "timeout_seconds", 10.0)
        # Scoped to the current task -- one candidate's one investigate()
        # call runs its capability groups sequentially within a single
        # asyncio task (see app/deep_investigation.py's investigate() loop),
        # so this identifies exactly "this candidate's current cycle",
        # mirroring the acquisition_context already used for direct
        # yfinance reuse. A new task (a different candidate, or this same
        # instrument's next refresh cycle) always gets a fresh, empty slot.
        shareholding_targets: tuple = ()
        # Issue 1 diagnostic (0dfbe654 SHAREHOLDING=EXTERNAL_CAPABILITY_UNSUPPORTED):
        # fires immediately before the ONE branch below that decides whether
        # SHAREHOLDING is routed through the dedicated NSE-first path (never
        # reaches the external Yahoo gateway for SHAREHOLDING at all) or falls
        # into the generic per-target loop further down (where Yahoo is
        # attempted and the downstream gateway service's own capability
        # decision -- see HttpExternalResearchToolGateway.acquire_requirement,
        # not anything decided in this repository -- can come back as
        # EXTERNAL_CAPABILITY_UNSUPPORTED). profile.provider_instrument_ids
        # only ever holds a symbol for a provider that already passed this
        # function's VERIFIED/RESOLVED trust gate at hydration time (the raw
        # per-mapping status strings are not retained past hydration), so its
        # keys are exactly "the verified provider mappings" at this point.
        # Purely additive observability -- no behavior change.
        if any(target.requirement_id == "SHAREHOLDING" for target in targets):
            logger.info(
                "shareholding_routing_decision instrument_id=%s country=%s exchange=%s "
                "ticker=%s jurisdiction=%s nseSymbol=%s bseSymbol=%s verifiedProviderMappings=%s "
                "route=%s",
                global_instrument_id, profile.country, profile.exchange, profile.ticker, jurisdiction,
                profile.provider_instrument_ids.get("NSE"), profile.provider_instrument_ids.get("BSE"),
                dict(profile.provider_instrument_ids),
                "NSE_FIRST" if jurisdiction == "INDIA" else "GENERIC_PROVIDER_LOOP",
            )
        if jurisdiction == "INDIA":
            shareholding_targets = tuple(
                target for target in targets if target.requirement_id == "SHAREHOLDING"
            )
            if shareholding_targets:
                # SHAREHOLDING exact source order (Slice 2 follow-up):
                # NSE structured/XBRL ONLY -> reassess -> Yahoo MCP ONLY if
                # a genuine gap remains -> reassess -> NSE official
                # document/PDF ONLY if a genuine gap still remains ->
                # reassess. SHAREHOLDING is pulled out of `targets` here so
                # the Yahoo-first loop below can never reach it ahead of
                # NSE; this call goes directly to the repository (not the
                # fused legacy/CapabilityExecutor) so the dedicated NSE feed
                # and the official document fallback can be invoked as two
                # separate, narrowly-scoped phases instead of one fused
                # pass. Yahoo is NOT removed as a fallback, only
                # re-sequenced strictly between the two NSE phases.
                targets = tuple(
                    target for target in targets if target.requirement_id != "SHAREHOLDING"
                )
                # This requirement's authoritative (NSE) source gets its
                # turn(s) this cycle regardless of outcome -- never let the
                # final catch-all legacy fallback further below invoke any
                # shareholding acquisition a second time in the same refresh.
                authority_checked.add("SHAREHOLDING")
                structured_capability = "NSE:SHAREHOLDING_STRUCTURED"
                executed.append(structured_capability)
                if progress is not None:
                    progress.executed(structured_capability)
                try:
                    await self.repository.refresh_targeted_categories(
                        global_instrument_id, {"SHAREHOLDING_PATTERN"},
                        correlation_id=correlation_id, allow_demo=False,
                        shareholding_phase="STRUCTURED_ONLY",
                    )
                except Exception as exc:
                    mcp_failures["SHAREHOLDING"] = "|".join(
                        filter(None, (mcp_failures.get("SHAREHOLDING"), type(exc).__name__)))
                if progress is not None:
                    progress.completed(structured_capability)
                # Reassess actual persisted evidence (field-level, via
                # shareholding_period_groups()/shareholding_field_merge())
                # before deciding whether Yahoo is even needed. Never infer
                # completion from document/transport success.
                readiness = await self.repository._run_blocking_persistence(
                    ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                    global_instrument_id, jurisdiction=jurisdiction,
                )
                if readiness.for_requirement("SHAREHOLDING").status not in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
                    completed.add("SHAREHOLDING")
                else:
                    # A genuine gap remains after NSE structured. Yahoo MCP is
                    # NOT a registered, supported capability for SHAREHOLDING
                    # (the YahooFinanceCapabilityRegistry from_json hardcodes
                    # SHAREHOLDING state = UNSUPPORTED across every region, and
                    # YahooMcpGateway.invoke() consequently raises
                    # EXTERNAL_CAPABILITY_UNSUPPORTED for it) -- see
                    # failure_taxonomy.PERMANENT_REASONS. Routing SHAREHOLDING
                    # through the generic Yahoo-first loop would therefore
                    # always fail with that bare provider error and leak it as
                    # the final SHAREHOLDING outcome (wrong aggregate
                    # semantics: an intermediate provider outcome, not a
                    # genuine evidence-absence verdict). Do NOT re-append
                    # shareholding_targets here; fall through to the dedicated
                    # NSE DOCUMENT_ONLY phase below, which is the only source
                    # with authority remaining after NSE structured. A genuine
                    # gap that survives that phase is reported as the aggregate
                    # EVIDENCE_INSUFFICIENT_WITHIN_PLAN (permanent, not retried),
                    # not as a Yahoo provider error.
                    pass
                    # shareholding_targets is deliberately NOT re-appended to
                    # `targets`: the generic per-target loop below must never
                    # reach SHAREHOLDING.
        acquisition_context = asyncio.current_task()
        reuse_slot = (
            self._context_result_slot(acquisition_context)
            if acquisition_context is not None
            else None
        )
        if self.enabled:
            for target in targets:
                if target.requirement_id in completed:
                    continue
                if target.requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE" and (
                    target.excluded_input_ids
                    or (jurisdiction == "INDIA" and profile.provider_instrument_ids.get("NSE"))
                ):
                    # The MCP umbrella cannot express concept exclusions. The
                    # category-aware executor preserves scope, tries official
                    # NSE disclosures, then uses its approved source fallback.
                    continue
                route = self.priority.route(jurisdiction, target.requirement_id)
                if not route.providers or route.providers[0] != YAHOO_FINANCE_MCP:
                    continue
                call_timeout_seconds: float | None = None
                if deadline is not None:
                    remaining = deadline - asyncio.get_event_loop().time()
                    call_timeout_seconds = min(
                        normal_timeout_seconds,
                        remaining - _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS,
                    )
                    if call_timeout_seconds <= _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS:
                        mcp_failures[target.requirement_id] = "BUDGET_EXHAUSTED"
                        if progress is not None:
                            progress.failed(target.requirement_id, "BUDGET_EXHAUSTED")
                        continue
                authorization = target.authority_policy.fallback_policy.authorize_external_tool(
                    global_instrument_id=global_instrument_id,
                    requirement_id=target.requirement_id,
                    status=target.reason,
                    confidence=0.0,
                    permitted_provider_ids=(YAHOO_FINANCE_MCP,),
                )
                if authorization is None:
                    mcp_failures[target.requirement_id] = "EXTERNAL_FALLBACK_NOT_AUTHORIZED"
                    if progress is not None:
                        progress.failed(
                            target.requirement_id, "EXTERNAL_FALLBACK_NOT_AUTHORIZED"
                        )
                    continue
                capability = f"{YAHOO_FINANCE_MCP}:{target.requirement_id}"
                executed.append(capability)
                if progress is not None:
                    progress.executed(capability)
                reuse_key = (global_instrument_id, target.requirement_id)
                cached_outcome = reuse_slot.get(reuse_key) if reuse_slot is not None else None
                if isinstance(cached_outcome, tuple) and cached_outcome[0] == "FAILED":
                    # Same candidate/cycle, same symbol+requirement: a prior
                    # attempt already failed/timed out. Reuse that bounded
                    # negative outcome instead of making another immediate
                    # call against a provider that just failed -- never a
                    # permanent cache, since a new acquisition_context (a new
                    # candidate, or this instrument's next refresh cycle)
                    # always starts with an empty slot.
                    mcp_failures[target.requirement_id] = cached_outcome[1]
                    if progress is not None:
                        progress.failed(target.requirement_id, cached_outcome[1])
                    continue
                if cached_outcome is not None:
                    # Same candidate/cycle already acquired this exact
                    # symbol+requirement successfully -- reuse the result
                    # rather than repeating an identical provider call.
                    result = cached_outcome
                else:
                    try:
                        result = await self.gateway.acquire_requirement(
                            profile,
                            region=jurisdiction,
                            requirement_id=target.requirement_id,
                            authorization=authorization,
                            request_id=request_id,
                            timeout_seconds=call_timeout_seconds,
                        )
                        # Stage-2 follow-up (Objective B): FinancialGapState.
                        # accepts() is the metric+period+period_type+
                        # reporting_basis+unit-aware filter already proven in
                        # the narrow NSE-financial-gap-fill path
                        # (_execute_financial_gaps). That path pulls its own
                        # financial targets out of `targets` before this
                        # generic loop ever runs (see the INDIA +
                        # _uses_nse_financial_gaps() branch above), so this
                        # only fires when that narrow path's own
                        # preconditions were NOT met (e.g. no NSE official
                        # financial provider configured, or a non-INDIA
                        # instrument) -- the exact "generic financial
                        # fallback" the task says must not bypass this
                        # filtering. A single whole-requirement Yahoo call is
                        # still made (the provider contract cannot request
                        # one metric at a time), but the returned payload is
                        # filtered to only the FinancialFactKeys genuinely
                        # still missing before anything is persisted -- an
                        # already-authoritative NSE fact is never replaced,
                        # and unrelated metrics the requirement did not
                        # actually need are discarded, not written.
                        if target.requirement_id in FINANCIAL_GAP_REQUIREMENTS:
                            gap_state = await self._financial_state(global_instrument_id)
                            await self.persister.persist(result, profile, financial_gaps=gap_state)
                        else:
                            await self.persister.persist(result, profile)
                    except ExternalMcpAcquisitionError as exc:
                        # A completed provider call that still produced zero usable
                        # facts is a deterministic evidence gap for the structural
                        # financial requirements (NSE-authoritative, gap-fill done),
                        # not a transient failure to retry. Rewrite it to the
                        # permanent canonical reason BEFORE recording, so the
                        # aggregate does not misclassify it as TECHNICAL_RETRYABLE.
                        # Genuine transient codes (DOWNSTREAM_TIMEOUT /
                        # EXTERNAL_PROVIDER_UNAVAILABLE / EXTERNAL_SCHEMA_INVALID /
                        # EXTERNAL_IDENTITY_CONFLICT / BUDGET_EXHAUSTED / etc.) are
                        # returned unchanged by the qualifier and stay retryable.
                        safe_code = _qualify_empty_financial_result(target.requirement_id, exc.safe_code)
                        if target.requirement_id == "SHAREHOLDING":
                            # Issue 1 diagnostic: the TRUE emission site for a
                            # SHAREHOLDING EXTERNAL_CAPABILITY_UNSUPPORTED (or
                            # any other) final reason. exc.safe_code is read
                            # verbatim from HttpExternalResearchToolGateway.
                            # acquire_requirement()'s HTTP response envelope --
                            # this repository never constructs that string
                            # itself. Reaching this except block at all already
                            # proves jurisdiction != "INDIA" here (the INDIA
                            # pre-pass above pulls SHAREHOLDING out of `targets`
                            # before this generic loop ever runs), and that
                            # route.providers[0] == YAHOO_FINANCE_MCP was
                            # selected for it (checked just above this try/except).
                            logger.info(
                                "shareholding_final_reason_emitted instrument_id=%s requirement_id=%s "
                                "country=%s exchange=%s ticker=%s jurisdiction=%s nseSymbol=%s bseSymbol=%s "
                                "verifiedProviderMappings=%s selectedProvider=%s finalReason=%s",
                                global_instrument_id, target.requirement_id,
                                profile.country, profile.exchange, profile.ticker, jurisdiction,
                                profile.provider_instrument_ids.get("NSE"),
                                profile.provider_instrument_ids.get("BSE"),
                                dict(profile.provider_instrument_ids), route.providers[0], safe_code,
                            )
                        if reuse_slot is not None:
                            reuse_slot[reuse_key] = ("FAILED", safe_code)
                        mcp_failures[target.requirement_id] = safe_code
                        if progress is not None:
                            progress.failed(target.requirement_id, safe_code)
                        continue
                    except Exception:
                        # Provider and persistence details never escape targeted ensure.
                        # The unchanged regional provider receives the requirement.
                        if reuse_slot is not None:
                            reuse_slot[reuse_key] = ("FAILED", "EXTERNAL_PROVIDER_UNAVAILABLE")
                        mcp_failures[target.requirement_id] = "EXTERNAL_PROVIDER_UNAVAILABLE"
                        if progress is not None:
                            progress.failed(
                                target.requirement_id, "EXTERNAL_PROVIDER_UNAVAILABLE"
                            )
                        continue
                    else:
                        if reuse_slot is not None:
                            reuse_slot[reuse_key] = result
                if result.acquisition_outcome == "SUCCESS_EMPTY":
                    # Empty acquisition metadata is not event evidence. Try approved fallbacks.
                    continue
                if acquisition_budget(global_instrument_id) is not None:
                    # An active deep-investigation targeted-repair budget means
                    # the caller specifically asked for this still-missing
                    # mandatory input. Yahoo's own self-reported "SUCCESS" only
                    # means *something* was written (e.g. an unrelated
                    # financial metric, or a news response with zero articles)
                    # -- it does not mean the targeted gap was actually closed.
                    # Re-verify against durable readiness before excluding the
                    # target from the legacy/authoritative fallback, the same
                    # way the QUARTERLY_FINANCIALS authority-upgrade branch
                    # above already does. Outside an active repair budget
                    # (ordinary/routine refresh) this extra round trip is
                    # skipped and existing behavior is unchanged.
                    readiness = await self.repository._run_blocking_persistence(
                        ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                        global_instrument_id, jurisdiction=jurisdiction,
                    )
                    if readiness.for_requirement(target.requirement_id).status in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
                        # Stage-2 follow-up (Objective C): a bare
                        # EXTERNAL_RESULT_INCOMPLETE here is ambiguous across
                        # every requirement that reaches this generic
                        # post-success recheck, so it must stay
                        # TECHNICAL_RETRYABLE for all of them (no blanket
                        # reclassification). HISTORICAL_PRICE_SERIES is the
                        # one case this block can make a deterministic call
                        # about: the provider call already succeeded
                        # (written >= 1 was persisted above) and durable
                        # evidence -- re-read fresh, right here -- still does
                        # not cover the required window. An identical retry
                        # against the exact same persisted gap cannot
                        # discover history that was already proven not to
                        # exist in that successful response, so this is
                        # evidence-unavailable for *this* acquisition, not a
                        # transient failure to keep retrying. See
                        # failure_taxonomy.PERMANENT_REASONS for the matching
                        # entry; a genuine network/provider error for this
                        # requirement is caught separately above (the
                        # ExternalMcpAcquisitionError / bare Exception
                        # branches) and keeps its own retryable reason.
                        reason = "EXTERNAL_RESULT_INCOMPLETE"
                        if target.requirement_id == "HISTORICAL_PRICE_SERIES":
                            reason = "EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT"
                        elif target.requirement_id.strip().upper() in _DETERMINISTIC_FINANCIAL_REQUIREMENTS:
                            # Producer-side qualification (not a global taxonomy
                            # reclassification): the provider call completed and the
                            # NSE-authoritative financial path + legacy fallback
                            # already ran (this generic post-success recheck only
                            # fires under an active repair budget, see the
                            # acquisition_budget guard above), so a still-unsatisfied
                            # structural-financial requirement is a deterministic
                            # absence rather than a retryable transient gap.
                            # Genuine technical codes are caught on the branches
                            # above and never reach here.
                            reason = "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
                        mcp_failures[target.requirement_id] = reason
                        if progress is not None:
                            progress.failed(target.requirement_id, reason)
                        continue
                completed.add(target.requirement_id)
                if progress is not None:
                    progress.satisfied(target.requirement_id)

        if jurisdiction == "INDIA" and shareholding_targets and "SHAREHOLDING" not in completed:
            # Yahoo was attempted (or skipped as unauthorized/budget-
            # exhausted) above. Reassess persisted evidence once more before
            # ever touching the official NSE document/PDF path -- the last
            # resort, only for fields genuinely still missing after both
            # NSE structured and Yahoo.
            readiness = await self.repository._run_blocking_persistence(
                ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                global_instrument_id, jurisdiction=jurisdiction,
            )
            if readiness.for_requirement("SHAREHOLDING").status not in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
                completed.add("SHAREHOLDING")
            else:
                document_capability = "NSE:SHAREHOLDING_DOCUMENT"
                executed.append(document_capability)
                if progress is not None:
                    progress.executed(document_capability)
                try:
                    await self.repository.refresh_targeted_categories(
                        global_instrument_id, {"SHAREHOLDING_PATTERN"},
                        correlation_id=correlation_id, allow_demo=False,
                        shareholding_phase="DOCUMENT_ONLY",
                    )
                except Exception as exc:
                    mcp_failures["SHAREHOLDING"] = "|".join(
                        filter(None, (mcp_failures.get("SHAREHOLDING"), type(exc).__name__)))
                if progress is not None:
                    progress.completed(document_capability)
                readiness = await self.repository._run_blocking_persistence(
                    ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                    global_instrument_id, jurisdiction=jurisdiction,
                )
                if readiness.for_requirement("SHAREHOLDING").status not in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
                    completed.add("SHAREHOLDING")
                else:
                    # Every legitimate source with authority to resolve
                    # SHAREHOLDING -- NSE structured/XBRL, NSE official
                    # document/PDF -- has now genuinely been tried this cycle
                    # and the gap remains. This is a genuine evidence-absence
                    # verdict, NOT an intermediate provider outcome: do not
                    # leak any stale Yahoo provider error (e.g.
                    # EXTERNAL_CAPABILITY_UNSUPPORTED, which can only ever
                    # appear here if a prior code path or observation
                    # recorded it) as the final SHAREHOLDING reason. Report
                    # the aggregate, plan-grounded classification instead. Both
                    # are PERMANENT_REASONS (EVIDENCE_UNAVAILABLE, not retried),
                    # but the aggregate reason truthfully describes the
                    # candidate-level state rather than a single provider's
                    # self-reported unsupported state.
                    mcp_failures["SHAREHOLDING"] = "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
                    plan_exhausted.add("SHAREHOLDING")

        remaining = tuple(target for target in targets
                          if target.requirement_id not in completed | authority_checked | plan_exhausted)
        legacy = (
            await self.legacy_executor.execute_primary(
                global_instrument_id,
                remaining,
                jurisdiction=jurisdiction,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
                progress=progress,
                deadline=deadline,
            )
            if remaining
            else CapabilityExecutionResult()
        )
        failures = dict(legacy.failures)
        for requirement_id, safe_code in mcp_failures.items():
            if requirement_id in failures:
                failures[requirement_id] = f"{safe_code}|{failures[requirement_id]}"
            else:
                # A legacy executor may complete normally while finding no
                # evidence. Retain the concrete MCP result until the runtime
                # re-reads durable evidence and can prove the requirement was
                # satisfied by that fallback.
                failures[requirement_id] = safe_code
        # Final blocking state comes from committed evidence. Keep attempt
        # failures in observations/progress even when the regional path wins.
        if mcp_failures and callable(getattr(self.repository, "_run_blocking_persistence", None)):
            for key, reason in mcp_failures.items():
                provider = "NSE" if reason == "NSE_FINANCIAL_UPGRADE_UNAVAILABLE" else YAHOO_FINANCE_MCP
                await self._observe_failure(global_instrument_id, key, provider, reason)
            current = await self.repository._run_blocking_persistence(
                ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository)).assess,
                global_instrument_id, jurisdiction=jurisdiction, evidence_only=True)
            failures = {key: value for key, value in failures.items()
                        if current.for_requirement(key).status not in {
                            ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.NOT_APPLICABLE}}
        return CapabilityExecutionResult(
            (*financial_result.executed_capabilities, *executed, *legacy.executed_capabilities),
            {**financial_result.failures, **failures},
            tuple(sorted(completed | set(legacy.satisfied_requirement_ids) | set(financial_result.satisfied_requirement_ids))),
        )

    async def execute_approved_fallbacks(
        self, global_instrument_id: UUID, targets: Sequence[ResearchRefreshTarget]
    ) -> CapabilityExecutionResult:
        if self._uses_nse_financial_gaps(self.repository.profile(global_instrument_id)):
            # These requirements already attempted the scoped Yahoo fallback.
            # Do not follow it with a whole-category direct-Yahoo acquisition.
            targets = tuple(target for target in targets if target.requirement_id not in FINANCIAL_GAP_REQUIREMENTS)
            if not targets:
                return CapabilityExecutionResult()
        return await self.legacy_executor.execute_approved_fallbacks(global_instrument_id, targets)


def _event_type(value: str | None) -> ResearchEventType:
    normalized = str(value or "").strip().upper()
    try:
        return ResearchEventType(normalized)
    except ValueError:
        return ResearchEventType.OTHER


def _period_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None
        else value.astimezone(timezone.utc)
    )
