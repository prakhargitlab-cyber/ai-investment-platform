"""Yahoo MCP-first acquisition behind the 5A ExternalResearchToolGateway seam.

The read-side readiness and rule-engine paths never import or call this module.
Only the targeted readiness executor uses it, after DB-first planning has found
a stale or missing requirement. Yahoo acquisition priority is kept separate
from the existing durable fact authority/merge rules.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID, uuid5, NAMESPACE_URL

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
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
from app.research_readiness import ExternalResearchToolAuthorization, ResearchRefreshTarget
from app.research_readiness_runtime import CapabilityExecutionProgress, CapabilityExecutionResult


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
        self, result: YahooMcpNormalizedResult, profile: CompanyResearchProfile
    ) -> int:
        if result.global_instrument_id != profile.instrument_id:
            raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
        if result.symbol.upper() != profile.provider_instrument_ids.get(
            "YAHOO_FINANCE", ""
        ).upper():
            raise ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT")
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
            last_price_at=now if "latestPrice" in facts else None,
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
            source_independence_key=article.url.casefold(),
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
            independence_key=f"{YAHOO_FINANCE_MCP}:{article.url.casefold()}",
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


class McpFirstResearchCapabilityExecutor:
    """Try configured Yahoo MCP capabilities, then call the unchanged executor."""

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

    async def execute_primary(
        self,
        global_instrument_id: UUID,
        targets: Sequence[ResearchRefreshTarget],
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
    ) -> CapabilityExecutionResult:
        completed: set[str] = set()
        executed: list[str] = []
        mcp_failures: dict[str, str] = {}
        profile = self.repository.profile(global_instrument_id)
        request_id = correlation_id or str(global_instrument_id)
        if self.enabled:
            for target in targets:
                route = self.priority.route(jurisdiction, target.requirement_id)
                if not route.providers or route.providers[0] != YAHOO_FINANCE_MCP:
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
                try:
                    result = await self.gateway.acquire_requirement(
                        profile,
                        region=jurisdiction,
                        requirement_id=target.requirement_id,
                        authorization=authorization,
                        request_id=request_id,
                    )
                    await self.persister.persist(result, profile)
                except ExternalMcpAcquisitionError as exc:
                    mcp_failures[target.requirement_id] = exc.safe_code
                    if progress is not None:
                        progress.failed(target.requirement_id, exc.safe_code)
                    continue
                except Exception:
                    # Provider and persistence details never escape targeted ensure.
                    # The unchanged regional provider receives the requirement.
                    mcp_failures[target.requirement_id] = "EXTERNAL_PROVIDER_UNAVAILABLE"
                    if progress is not None:
                        progress.failed(
                            target.requirement_id, "EXTERNAL_PROVIDER_UNAVAILABLE"
                        )
                    continue
                if result.acquisition_outcome == "SUCCESS_EMPTY":
                    # Empty acquisition metadata is not event evidence. Try approved fallbacks.
                    continue
                completed.add(target.requirement_id)
                if progress is not None:
                    progress.satisfied(target.requirement_id)

        remaining = tuple(target for target in targets if target.requirement_id not in completed)
        legacy = (
            await self.legacy_executor.execute_primary(
                global_instrument_id,
                remaining,
                jurisdiction=jurisdiction,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
                progress=progress,
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
        return CapabilityExecutionResult(
            (*executed, *legacy.executed_capabilities),
            failures,
            tuple(sorted(completed | set(legacy.satisfied_requirement_ids))),
        )

    async def execute_approved_fallbacks(
        self, global_instrument_id: UUID, targets: Sequence[ResearchRefreshTarget]
    ) -> CapabilityExecutionResult:
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
