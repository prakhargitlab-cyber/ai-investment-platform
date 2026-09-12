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


class SourceClassification(StrEnum):
    OFFICIAL_COMPANY = "OFFICIAL_COMPANY"
    REGULATORY = "REGULATORY"
    EXCHANGE = "EXCHANGE"
    CUSTOMER = "CUSTOMER"
    PARTNER = "PARTNER"
    SUPPLIER = "SUPPLIER"
    REPUTABLE_NEWS = "REPUTABLE_NEWS"
    INVESTMENT_RESEARCH = "INVESTMENT_RESEARCH"
    OTHER = "OTHER"


class FetchStrategy(StrEnum):
    HTTP = "HTTP"
    PLAYWRIGHT = "PLAYWRIGHT"
    MANUAL = "MANUAL"


class SourceAccessStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    RESTRICTED = "RESTRICTED"
    MANUAL_ONLY = "MANUAL_ONLY"


class SourceMode(StrEnum):
    DEMO = "DEMO"
    REAL = "REAL"


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


class DocumentSubtype(StrEnum):
    INVESTOR_PRESENTATION = "INVESTOR_PRESENTATION"
    INVESTOR_RELEASE = "INVESTOR_RELEASE"
    CONFERENCE_CALL_MATERIAL = "CONFERENCE_CALL_MATERIAL"
    ORDER_CONTRACT_DISCLOSURE = "ORDER_CONTRACT_DISCLOSURE"
    CAPEX_CAPACITY_DISCLOSURE = "CAPEX_CAPACITY_DISCLOSURE"


class ShareholdingCategory(StrEnum):
    PROMOTER = "PROMOTER"
    PROMOTER_PLEDGE = "PROMOTER_PLEDGE"
    FII_FPI = "FII_FPI"
    DII = "DII"
    MUTUAL_FUNDS = "MUTUAL_FUNDS"
    INSURANCE = "INSURANCE"
    GOVERNMENT = "GOVERNMENT"
    PUBLIC_RETAIL = "PUBLIC_RETAIL"
    OTHERS = "OTHERS"


class PledgeMetricBasis(StrEnum):
    PERCENT_OF_PROMOTER_HOLDING = "PERCENT_OF_PROMOTER_HOLDING"
    PERCENT_OF_TOTAL_SHARES = "PERCENT_OF_TOTAL_SHARES"
    OTHER_EXPLICIT_SOURCE_BASIS = "OTHER_EXPLICIT_SOURCE_BASIS"


class ResearchEventType(StrEnum):
    ORDER_WIN = "ORDER_WIN"
    NEW_CONTRACT = "NEW_CONTRACT"
    CLIENT_WIN = "CLIENT_WIN"
    NEW_PLANT = "NEW_PLANT"
    CREDIT_RATING = "CREDIT_RATING"
    BORROWING_CHANGE = "BORROWING_CHANGE"
    MANAGEMENT_GUIDANCE = "MANAGEMENT_GUIDANCE"
    MAJOR_CORPORATE_ANNOUNCEMENT = "MAJOR_CORPORATE_ANNOUNCEMENT"
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
    GUIDANCE_MAINTAINED = "GUIDANCE_MAINTAINED"
    GUIDANCE_CUT = "GUIDANCE_CUT"
    REVENUE_GUIDANCE = "REVENUE_GUIDANCE"
    MARGIN_GUIDANCE = "MARGIN_GUIDANCE"
    INVESTMENT = "INVESTMENT"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    PROJECT_DELAY = "PROJECT_DELAY"
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


class EvidenceState(StrEnum):
    POSITIVE_EVIDENCE = "POSITIVE_EVIDENCE"
    NEUTRAL_EVIDENCE = "NEUTRAL_EVIDENCE"
    NEGATIVE_EVIDENCE = "NEGATIVE_EVIDENCE"
    MIXED_EVIDENCE = "MIXED_EVIDENCE"
    NO_EVIDENCE = "NO_EVIDENCE"


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
    provider_instrument_ids: dict[str, str] = Field(default_factory=dict)
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


class ProvenancedValue(ResearchBaseModel):
    value: Any
    unit: str | None = None
    as_of_date: datetime | None = None
    period: str | None = None
    source_url: str
    source_name: str
    source_type: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    calculation_basis: str | None = None


class StructuredInstrumentResolution(ResearchBaseModel):
    instrument_id: UUID | None = None
    provider: str
    provider_ticker: str
    company_name: str
    exchange: str | None = None
    currency: str | None = None
    quote_type: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    resolved_at: datetime
    status: str = "RESOLVED"


class StructuredMarketSnapshot(ResearchBaseModel):
    resolution: StructuredInstrumentResolution
    status: str
    retrieved_at: datetime
    market_as_of: datetime | None = None
    source_name: str = "Yahoo Finance"
    source_type: str = "STRUCTURED_MARKET_PROVIDER"
    source_url: str
    facts: dict[str, ProvenancedValue] = Field(default_factory=dict)
    statement_facts: list[dict[str, Any]] = Field(default_factory=list)
    news: list[dict[str, Any]] = Field(default_factory=list)
    accepted_fields_count: int = 0
    safe_error_code: str | None = None


class StructuredMarketSnapshotRecord(ResearchBaseModel):
    """Durable latest successful structured-market evidence; never a financial fact."""
    instrument_id: UUID
    provider: str
    provider_instrument_id: str | None = None
    exchange: str | None = None
    mic: str | None = None
    currency: str | None = None
    quote_type: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    source_identity: str | None = None
    market_as_of: datetime | None = None
    retrieved_at: datetime
    persisted_at: datetime
    last_price_at: datetime | None = None
    last_valuation_at: datetime | None = None
    last_fundamentals_at: datetime | None = None
    last_analyst_at: datetime | None = None
    last_success_at: datetime | None = None
    last_provider_attempt_at: datetime | None = None
    acquisition_status: str = "SUCCESS"
    last_failure_code: str | None = None
    last_failure_message: str | None = None
    snapshot: StructuredMarketSnapshot


class MarketPriceObservation(ResearchBaseModel):
    """One durable, provider-neutral observed market price for an instrument."""
    instrument_id: UUID
    observed_at: datetime
    price: Decimal
    currency: str | None = None
    provider: str
    source_url: str
    retrieved_at: datetime


class PublicAnalyst(ResearchBaseModel):
    target_low_price: Decimal | None = None
    target_median_price: Decimal | None = None
    target_mean_price: Decimal | None = None
    target_high_price: Decimal | None = None
    analyst_count: int | None = None
    recommendation_mean: Decimal | None = None
    consensus: str | None = None
    currency: str | None = None
    provider: str
    provider_instrument_id: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    as_of: datetime | None = None
    retrieved_at: datetime | None = None
    freshness: str


class MarketFundamentals(ResearchBaseModel):
    market_cap: Decimal | None = None
    enterprise_value: Decimal | None = None
    trailing_pe: Decimal | None = None
    forward_pe: Decimal | None = None
    price_to_book: Decimal | None = None
    price_to_sales: Decimal | None = None
    ev_to_revenue: Decimal | None = None
    ev_to_ebitda: Decimal | None = None
    peg_ratio: Decimal | None = None
    trailing_eps: Decimal | None = None
    forward_eps: Decimal | None = None
    book_value_per_share: Decimal | None = None
    roe: Decimal | None = None
    roa: Decimal | None = None
    debt_to_equity: Decimal | None = None
    profit_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    revenue_growth: Decimal | None = None
    earnings_growth: Decimal | None = None
    total_cash: Decimal | None = None
    total_debt: Decimal | None = None
    free_cash_flow: Decimal | None = None
    operating_cash_flow: Decimal | None = None
    provider: str
    provider_instrument_id: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    as_of: datetime | None = None
    retrieved_at: datetime | None = None
    freshness: str
    metric_semantics: dict[str, str] = Field(default_factory=dict)


class QuarterlyResult(ResearchBaseModel):
    period: str
    document_title: str | None = None
    extraction_status: str = "EXTRACTED"
    reporting_basis: str | None = None
    result_date: datetime | None = None
    revenue: ProvenancedValue | None = None
    revenue_yoy_percent: ProvenancedValue | None = None
    revenue_qoq_percent: ProvenancedValue | None = None
    ebitda: ProvenancedValue | None = None
    ebitda_margin: ProvenancedValue | None = None
    ebitda_yoy_percent: ProvenancedValue | None = None
    pat: ProvenancedValue | None = None
    pat_yoy_percent: ProvenancedValue | None = None
    pat_qoq_percent: ProvenancedValue | None = None
    eps: ProvenancedValue | None = None
    debt_or_borrowings: ProvenancedValue | None = None
    exceptional_items: str | None = None
    segment_information: str | None = None
    management_commentary: list[str] = Field(default_factory=list)
    yoy_summary: str | None = None
    nim: ProvenancedValue | None = None
    roa: ProvenancedValue | None = None
    roe: ProvenancedValue | None = None
    gross_npa: ProvenancedValue | None = None
    net_npa: ProvenancedValue | None = None
    deposits: ProvenancedValue | None = None
    advances: ProvenancedValue | None = None
    capital_adequacy: ProvenancedValue | None = None
    credit_cost: ProvenancedValue | None = None
    source_name: str
    source_url: str
    source_type: str
    published_at: datetime | None = None
    retrieved_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class FinancialResultPeriod(ResearchBaseModel):
    period: str
    period_type: str
    reporting_basis: str | None = None
    revenue: ProvenancedValue | None = None
    operating_income: ProvenancedValue | None = None
    ebit: ProvenancedValue | None = None
    ebitda: ProvenancedValue | None = None
    pat: ProvenancedValue | None = None
    eps: ProvenancedValue | None = None
    source_name: str
    source_url: str
    source_type: str
    published_at: datetime | None = None
    retrieved_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class FinancialStatementPeriod(ResearchBaseModel):
    """One persisted, source-backed balance-sheet or cash-flow period."""
    period: str
    period_type: str
    reporting_basis: str | None = None
    metrics: dict[str, ProvenancedValue] = Field(default_factory=dict)


class ShareholdingChange(ResearchBaseModel):
    category: str
    current: ProvenancedValue
    previous: ProvenancedValue
    current_period: str
    previous_period: str
    change_percentage_points: Decimal
    source_date: datetime | None = None


class ShareholdingSnapshotValue(ResearchBaseModel):
    id: UUID = Field(default_factory=uuid4)
    category: ShareholdingCategory
    percentage: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))
    metric_basis: str | None = None
    raw_source_label: str | None = None
    source_locator: str | None = None
    evidence_text: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ShareholdingSnapshot(ResearchBaseModel):
    id: UUID = Field(default_factory=uuid4)
    instrument_id: UUID
    period_end: datetime
    filing_basis: str | None = None
    source_provider: str
    source_type: str
    source_identity_key: str
    source_url: str
    research_document_id: UUID | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    reliability_level: ReliabilityLevel
    source_mode: SourceMode
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    values: list[ShareholdingSnapshotValue] = Field(default_factory=list)

    @field_validator("values")
    @classmethod
    def pledge_requires_explicit_basis(cls, values: list[ShareholdingSnapshotValue]) -> list[ShareholdingSnapshotValue]:
        for value in values:
            if value.category == ShareholdingCategory.PROMOTER_PLEDGE and not value.metric_basis:
                raise ValueError("PROMOTER_PLEDGE requires an explicit metric basis")
        return values


class ValuationAssessment(ResearchBaseModel):
    state: str = "UNKNOWN"
    reason: str
    current_pe: ProvenancedValue | None = None
    sector_pe: ProvenancedValue | None = None
    peer_pe: ProvenancedValue | None = None
    historical_pe: ProvenancedValue | None = None
    roe: ProvenancedValue | None = None
    roce: ProvenancedValue | None = None
    state_evidence: "ValuationStateEvidence | None" = None


class ValuationBenchmark(ResearchBaseModel):
    kind: str
    value: ProvenancedValue


class ValuationStateEvidence(ResearchBaseModel):
    primary_metric: str
    current_value: ProvenancedValue
    benchmarks: list[ValuationBenchmark]
    benchmark_value: Decimal
    comparison_ratio: Decimal
    comparison_method: str
    explanation: str


class SourceDiversity(ResearchBaseModel):
    sources_found: int = 0
    domains_found: int = 0
    official_sources: int = 0
    exchange_sources: int = 0
    company_sources: int = 0
    secondary_sources: int = 0


class EtfResearchProfile(ResearchBaseModel):
    instrument_id: UUID
    fund_id: UUID
    fund_name: str
    ticker: str
    exchange: str
    mic: str
    provider: str | None = None
    provider_instrument_id: str | None = None
    isin: str | None = None
    currency: str | None = None
    fund_provider: str | None = None
    underlying_index: str | None = None
    known_domains: list[str] = Field(default_factory=list)
    facts: dict[str, ProvenancedValue] = Field(default_factory=dict)


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
    source_classification: SourceClassification = SourceClassification.OTHER
    source_name: str
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    language: str | None = None
    content_type: str
    document_type: DocumentType
    document_subtype: DocumentSubtype | None = None
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
    source_mode: SourceMode = SourceMode.DEMO
    freshness: str = "DEMO"
    discovered_at: datetime | None = None
    discovery_provider: str | None = None
    source_independence_key: str | None = None
    duplicate_of_document_id: UUID | None = None


class ResearchEvidenceSource(ResearchBaseModel):
    publisher: str | None = None
    url: str
    source_type: SourceClassification
    published_at: datetime | None = None
    retrieved_at: datetime
    reliability: ReliabilityLevel
    source_mode: SourceMode
    document_id: UUID
    source_name: str
    canonical_url: str
    independent: bool = True


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
    source_classification: SourceClassification = SourceClassification.OTHER
    reliability: ReliabilityLevel
    source_mode: SourceMode = SourceMode.DEMO
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
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    supporting_sources: list[ResearchEvidenceSource] = Field(default_factory=list)
    independence_key: str | None = None

    @field_validator("raw_evidence_reference")
    @classmethod
    def limit_evidence(cls, value: str) -> str:
        return value[:500]


class CategoryEvidence(ResearchBaseModel):
    category: str
    status: EvidenceState
    score: int | None = Field(default=None, ge=0, le=100)
    event_count: int = 0
    source_count: int = 0
    independent_source_count: int = 0
    has_conflict: bool = False
    # Category-linked evidence is deliberately separate from the compact
    # ``recent_events`` list.  A scored category must remain explainable even
    # when its event falls outside that general-purpose top-ten slice.
    supporting_events: list[ResearchEvent] = Field(default_factory=list)


class CatalystScore(ResearchBaseModel):
    instrument_id: UUID
    overall_score: int = Field(ge=0, le=100)
    buckets: dict[str, int | None]
    category_evidence: dict[str, CategoryEvidence] = Field(default_factory=dict)
    aggregation_rule: str = "Overall score is calculated from validated evidence events only; NO_EVIDENCE categories are omitted and are not treated as score 50."
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
    shareholding_snapshots: list[ShareholdingSnapshot] = Field(default_factory=list)
    shareholding_freshness: str = "UNAVAILABLE"
    latest_quarterly_result: QuarterlyResult | None = None
    financial_result_history: list[FinancialResultPeriod] = Field(default_factory=list)
    balance_sheet_history: list[FinancialStatementPeriod] = Field(default_factory=list)
    cash_flow_history: list[FinancialStatementPeriod] = Field(default_factory=list)


class PortfolioResearchCompany(ResearchBaseModel):
    instrument_id: UUID | None = None
    company_id: UUID | None = None
    company_name: str
    ticker: str | None = None
    exchange: str | None = None
    isin: str | None = None
    provider: str | None = None
    provider_instrument_id: str | None = None
    listing_provider: str | None = None
    listing_symbol: str | None = None
    primary_exchange: str | None = None
    verified_provider_mappings: dict[str, str] = Field(default_factory=dict)
    asset_type: str | None = None
    status: str
    catalyst_score: int | None = None
    confidence: int | None = None
    evidence_coverage: dict[str, str] = Field(default_factory=dict)
    durable_category_evidence: dict[str, CategoryEvidence] = Field(default_factory=dict)
    latest_event: ResearchEvent | None = None
    positive_events_count: int = 0
    negative_events_count: int = 0
    neutral_events_count: int = 0
    document_count: int = 0
    event_count: int = 0
    source_count: int = 0
    last_refresh: datetime | None = None
    freshness: str = "UNAVAILABLE"
    mode: str = "UNAVAILABLE"
    missing_categories: list[str] = Field(default_factory=list)
    etf_profile: EtfResearchProfile | None = None
    current_price: Decimal | None = None
    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    target1: Decimal | None = None
    target2: Decimal | None = None
    risk_invalidation_level: Decimal | None = None
    potential_upside_pct: Decimal | None = None
    potential_downside_pct: Decimal | None = None
    risk_reward_ratio: Decimal | None = None
    safe_error_code: str | None = None
    safe_error_message: str | None = None
    latest_quarterly_result: QuarterlyResult | None = None
    financial_result_history: list[FinancialResultPeriod] = Field(default_factory=list)
    balance_sheet_history: list[FinancialStatementPeriod] = Field(default_factory=list)
    cash_flow_history: list[FinancialStatementPeriod] = Field(default_factory=list)
    quarterly_result_status: str = "NOT_AVAILABLE"
    shareholding_changes: list[ShareholdingChange] = Field(default_factory=list)
    shareholding_snapshots: list[ShareholdingSnapshot] = Field(default_factory=list)
    shareholding_freshness: str = "UNAVAILABLE"
    ownership_increases: list[str] = Field(default_factory=list)
    valuation: ValuationAssessment = Field(default_factory=lambda: ValuationAssessment(
        state="UNKNOWN", reason="Insufficient comparable public valuation evidence."
    ))
    current_quarter_catalysts: list[ResearchEvent] = Field(default_factory=list)
    source_diversity: SourceDiversity = Field(default_factory=SourceDiversity)
    structured_market: StructuredMarketSnapshot | None = None
    structured_provider_status: str | None = None
    public_analyst: PublicAnalyst | None = None
    market_fundamentals: MarketFundamentals | None = None
    price_change: Decimal | None = None
    price_change_percent: Decimal | None = None
    price_direction: str = "UNKNOWN"
    market_status: str = "UNKNOWN"
    price_freshness: str = "NEVER_FETCHED"
    market_as_of: datetime | None = None


class PortfolioResearchSummary(ResearchBaseModel):
    portfolio_id: UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    companies_requested: int = 0
    companies_resolved: int = 0
    companies_succeeded: int = 0
    companies_degraded: int = 0
    companies_failed: int = 0
    documents_created: int = 0
    events_created: int = 0
    deduplicated_count: int = 0
    companies: list[PortfolioResearchCompany] = Field(default_factory=list)
    total_companies: int = 0
    completed: int = 0
    partial: int = 0
    failed: int = 0
    unsupported: int = 0
    in_progress: int = 0


class PlatformEvent(ResearchBaseModel):
    event_type: str
    version: int = 1
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any]
