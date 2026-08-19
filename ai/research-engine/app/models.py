from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ResearchBaseModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, use_enum_values=True)


class SourceType(StrEnum):
    COMPANY_WEBSITE = "COMPANY_WEBSITE"
    INVESTOR_RELATIONS = "INVESTOR_RELATIONS"
    EXCHANGE_ANNOUNCEMENT = "EXCHANGE_ANNOUNCEMENT"
    REGULATORY_FILING = "REGULATORY_FILING"
    GOVERNMENT_PROCUREMENT = "GOVERNMENT_PROCUREMENT"
    RSS = "RSS"
    NEWS = "NEWS"
    SEARCH_DISCOVERY = "SEARCH_DISCOVERY"


class FetchStrategy(StrEnum):
    HTTP = "HTTP"
    PLAYWRIGHT = "PLAYWRIGHT"
    MANUAL = "MANUAL"


class SourceAccessStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    RESTRICTED = "RESTRICTED"
    MANUAL_ONLY = "MANUAL_ONLY"


class ReliabilityLevel(StrEnum):
    LEVEL_A = "LEVEL_A"
    LEVEL_B = "LEVEL_B"
    LEVEL_C = "LEVEL_C"
    LEVEL_D = "LEVEL_D"
    LEVEL_E = "LEVEL_E"


class DocumentStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    PARSED = "PARSED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    PROCESSED = "PROCESSED"


class DocumentType(StrEnum):
    HTML = "HTML"
    TEXT = "TEXT"
    RSS_XML = "RSS_XML"
    PDF_REFERENCE = "PDF_REFERENCE"
    UNKNOWN = "UNKNOWN"


class ResearchEventType(StrEnum):
    NEW_ORDER = "NEW_ORDER"
    ORDER_BACKLOG_CHANGE = "ORDER_BACKLOG_CHANGE"
    NEW_CUSTOMER = "NEW_CUSTOMER"
    CUSTOMER_EXPANSION = "CUSTOMER_EXPANSION"
    MAJOR_CUSTOMER = "MAJOR_CUSTOMER"
    CUSTOMER_LOSS = "CUSTOMER_LOSS"
    MAJOR_CONTRACT = "MAJOR_CONTRACT"
    GOVERNMENT_CONTRACT = "GOVERNMENT_CONTRACT"
    CAPEX = "CAPEX"
    FACTORY_EXPANSION = "FACTORY_EXPANSION"
    CAPACITY_EXPANSION = "CAPACITY_EXPANSION"
    NEW_FACILITY = "NEW_FACILITY"
    GEOGRAPHIC_EXPANSION = "GEOGRAPHIC_EXPANSION"
    ACQUISITION = "ACQUISITION"
    PARTNERSHIP = "PARTNERSHIP"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    GUIDANCE_RAISED = "GUIDANCE_RAISED"
    GUIDANCE_LOWERED = "GUIDANCE_LOWERED"
    REVENUE_GUIDANCE = "REVENUE_GUIDANCE"
    MARGIN_GUIDANCE = "MARGIN_GUIDANCE"
    INVESTMENT = "INVESTMENT"
    DEBT_CHANGE = "DEBT_CHANGE"
    FUNDING = "FUNDING"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    REGULATORY_EVENT = "REGULATORY_EVENT"
    EARNINGS_RELEASE = "EARNINGS_RELEASE"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    OTHER = "OTHER"


class EventImpact(StrEnum):
    STRONG_POSITIVE = "STRONG_POSITIVE"
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    STRONG_NEGATIVE = "STRONG_NEGATIVE"
    UNCERTAIN = "UNCERTAIN"


class TimeHorizon(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"
    UNKNOWN = "UNKNOWN"


class ResearchLifecycleStatus(StrEnum):
    DETECTED = "DETECTED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


class SourceRateLimitPolicy(ResearchBaseModel):
    requests_per_minute: int = 6
    min_delay_seconds: float = 10.0


class ResearchSourceProvider(ResearchBaseModel):
    source_id: str
    source_type: SourceType
    source_name: str
    supported_countries: list[str] = Field(default_factory=list)
    supported_markets: list[str] = Field(default_factory=list)
    fetch_strategy: FetchStrategy
    reliability_level: ReliabilityLevel
    rate_limit_policy: SourceRateLimitPolicy = Field(default_factory=SourceRateLimitPolicy)
    javascript_required: bool = False
    automatic_access: SourceAccessStatus = SourceAccessStatus.MANUAL_ONLY
    base_url: AnyHttpUrl | None = None


class CompanyResearchProfile(ResearchBaseModel):
    instrument_id: UUID
    company_id: UUID
    company_name: str
    aliases: list[str] = Field(default_factory=list)
    isin: str | None = None
    ticker: str
    exchange: str
    mic: str
    country: str
    currency: str
    known_domains: list[str] = Field(default_factory=list)
    official_website: AnyHttpUrl | None = None
    investor_relations_url: AnyHttpUrl | None = None
    press_release_url: AnyHttpUrl | None = None
    annual_reports_url: AnyHttpUrl | None = None
    exchange_announcements_url: AnyHttpUrl | None = None
    regulatory_filings_url: AnyHttpUrl | None = None
    rss_feeds: list[AnyHttpUrl] = Field(default_factory=list)


class EntityResolution(ResearchBaseModel):
    instrument_id: UUID | None
    company_id: UUID | None
    confidence: float = Field(ge=0.0, le=1.0)
    matched_on: list[str] = Field(default_factory=list)


class ResearchDocument(ResearchBaseModel):
    document_id: UUID = Field(default_factory=uuid4)
    canonical_url: str
    original_url: str
    title: str | None = None
    source_type: SourceType
    source_name: str
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    language: str | None = None
    content_type: str
    document_type: DocumentType
    raw_text: str | None = Field(default=None, exclude=True)
    normalized_text: str | None = Field(default=None, exclude=True)
    content_hash: str
    instrument_id: UUID | None = None
    company_id: UUID | None = None
    country: str | None = None
    exchange: str | None = None
    status: DocumentStatus = DocumentStatus.DISCOVERED
    reliability_level: ReliabilityLevel
    entity_resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NormalizedNumber(ResearchBaseModel):
    original: str
    value: Decimal
    unit: str | None = None
    currency: str | None = None


class ResearchEvent(ResearchBaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    instrument_id: UUID
    company_id: UUID
    event_type: ResearchEventType
    event_date: datetime | None = None
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: str
    summary: str
    source_document_id: UUID
    source_url: str
    source_type: SourceType
    reliability: ReliabilityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    impact: EventImpact
    time_horizon: TimeHorizon
    currency: str | None = None
    monetary_value: Decimal | None = None
    monetary_original: str | None = None
    percentage_value: Decimal | None = None
    percentage_original: str | None = None
    customer: str | None = None
    counterparty: str | None = None
    location: str | None = None
    capacity_value: Decimal | None = None
    capacity_unit: str | None = None
    status: ResearchLifecycleStatus = ResearchLifecycleStatus.VALIDATED
    raw_evidence_reference: str

    @field_validator("raw_evidence_reference")
    @classmethod
    def limit_evidence(cls, value: str) -> str:
        return value[:500]


class CatalystScore(ResearchBaseModel):
    instrument_id: UUID
    overall_score: int = Field(ge=0, le=100)
    buckets: dict[str, int]
    research_confidence: int = Field(ge=0, le=100)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchSummary(ResearchBaseModel):
    profile: CompanyResearchProfile
    catalyst_score: CatalystScore
    recent_events: list[ResearchEvent]
    documents: list[ResearchDocument]
    last_refresh_at: datetime | None = None
    data_freshness: str
    demo: bool
    source_mix: dict[str, int]


class PlatformEvent(ResearchBaseModel):
    event_type: str
    version: int = 1
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any]
