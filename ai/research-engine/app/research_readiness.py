"""Provider-neutral research readiness and targeted refresh planning contracts.

This module deliberately contains no provider clients and no portfolio access.  A
readiness request starts with one canonical global instrument ID, reads a durable
snapshot, classifies every registered requirement, and returns a plan containing
only required facts that need work.  Provider adapters remain behind the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from app.research_applicability import RequirementApplicability


class RuleEngineArea(StrEnum):
    VALUATION = "VALUATION"
    FUNDAMENTAL_BUSINESS_QUALITY = "FUNDAMENTAL_BUSINESS_QUALITY"
    GROWTH = "GROWTH"
    BALANCE_SHEET = "BALANCE_SHEET"
    QUARTERLY_EARNINGS_TREND = "QUARTERLY_EARNINGS_TREND"
    ORDER_BOOK_CAPACITY_CATALYSTS = "ORDER_BOOK_CAPACITY_CATALYSTS"
    PRICE_TECHNICAL = "PRICE_TECHNICAL"
    NEWS_GEOPOLITICAL_EVENTS = "NEWS_GEOPOLITICAL_EVENTS"
    SHAREHOLDING = "SHAREHOLDING"
    MANAGEMENT_GOVERNANCE = "MANAGEMENT_GOVERNANCE"
    SECTOR_MACRO = "SECTOR_MACRO"


RULE_ENGINE_AREA_WEIGHTS = MappingProxyType(
    {
        RuleEngineArea.VALUATION: Decimal("0.18"),
        RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY: Decimal("0.16"),
        RuleEngineArea.GROWTH: Decimal("0.14"),
        RuleEngineArea.BALANCE_SHEET: Decimal("0.09"),
        RuleEngineArea.QUARTERLY_EARNINGS_TREND: Decimal("0.09"),
        RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS: Decimal("0.08"),
        RuleEngineArea.PRICE_TECHNICAL: Decimal("0.07"),
        RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS: Decimal("0.07"),
        RuleEngineArea.SHAREHOLDING: Decimal("0.04"),
        RuleEngineArea.MANAGEMENT_GOVERNANCE: Decimal("0.05"),
        RuleEngineArea.SECTOR_MACRO: Decimal("0.03"),
    }
)


class ResearchRequirementStatus(StrEnum):
    READY_FRESH = "READY_FRESH"
    READY_STALE = "READY_STALE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REFRESHING = "REFRESHING"
    FAILED = "FAILED"


class ResearchSupportedAction(StrEnum):
    FIND_DATA = "FIND_DATA"
    UPLOAD_EVIDENCE = "UPLOAD_EVIDENCE"
    RUN_PARTIAL_ANALYSIS = "RUN_PARTIAL_ANALYSIS"


class ResearchRequirementImportance(StrEnum):
    """How strongly a concrete input contributes to data completeness."""

    MANDATORY = "MANDATORY"
    IMPORTANT = "IMPORTANT"
    SUPPORTING = "SUPPORTING"


class FreshnessMode(StrEnum):
    MARKET_SESSION_AWARE = "MARKET_SESSION_AWARE"
    DAILY_INCREMENTAL = "DAILY_INCREMENTAL"
    RELEASE_AWARE_QUARTERLY = "RELEASE_AWARE_QUARTERLY"
    REPORTING_CALENDAR_AWARE_ANNUAL = "REPORTING_CALENDAR_AWARE_ANNUAL"
    QUARTERLY = "QUARTERLY"
    EVENT_DRIVEN_BOUNDED = "EVENT_DRIVEN_BOUNDED"
    ROLLING_EVENT_WINDOW = "ROLLING_EVENT_WINDOW"
    UNRESOLVED_HISTORY = "UNRESOLVED_HISTORY"
    FACT_SPECIFIC_DAILY = "FACT_SPECIFIC_DAILY"


class FreshnessDateBasis(StrEnum):
    AS_OF_OR_RETRIEVED = "AS_OF_OR_RETRIEVED"
    EVENT_OR_PUBLICATION = "EVENT_OR_PUBLICATION"


class ResearchSourceTier(StrEnum):
    OFFICIAL = "OFFICIAL"
    REGULATORY = "REGULATORY"
    TRUSTED_MARKET_DATA = "TRUSTED_MARKET_DATA"
    LICENSED_STRUCTURED = "LICENSED_STRUCTURED"
    APPROVED_SECONDARY = "APPROVED_SECONDARY"
    APPROVED_EXTERNAL_TOOL = "APPROVED_EXTERNAL_TOOL"
    USER_UPLOAD = "USER_UPLOAD"
    UNVERIFIED = "UNVERIFIED"


_SOURCE_TIER_AUTHORITY = {
    ResearchSourceTier.OFFICIAL: 0,
    ResearchSourceTier.REGULATORY: 1,
    ResearchSourceTier.TRUSTED_MARKET_DATA: 2,
    ResearchSourceTier.LICENSED_STRUCTURED: 3,
    ResearchSourceTier.APPROVED_SECONDARY: 4,
    ResearchSourceTier.APPROVED_EXTERNAL_TOOL: 5,
    ResearchSourceTier.USER_UPLOAD: 6,
    ResearchSourceTier.UNVERIFIED: 7,
}


@dataclass(frozen=True)
class ResearchRequirementInput:
    input_id: str
    importance: ResearchRequirementImportance
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_id", _normalized_key(self.input_id, "input_id"))
        if not isinstance(self.importance, ResearchRequirementImportance):
            object.__setattr__(
                self, "importance", ResearchRequirementImportance(self.importance)
            )


@dataclass(frozen=True)
class ResearchRequirement:
    requirement_id: str
    rule_engine_area: RuleEngineArea
    mandatory: bool
    freshness_policy_id: str
    minimum_evidence_count: int = 1
    supported_actions: tuple[ResearchSupportedAction, ...] = (
        ResearchSupportedAction.FIND_DATA,
        ResearchSupportedAction.UPLOAD_EVIDENCE,
        ResearchSupportedAction.RUN_PARTIAL_ANALYSIS,
    )
    importance: ResearchRequirementImportance | None = None
    inputs: tuple[ResearchRequirementInput, ...] = ()

    def __post_init__(self) -> None:
        requirement_id = _normalized_key(self.requirement_id, "requirement_id")
        policy_id = _normalized_key(self.freshness_policy_id, "freshness_policy_id")
        if self.minimum_evidence_count < 1:
            raise ValueError("minimum_evidence_count must be positive")
        object.__setattr__(self, "requirement_id", requirement_id)
        object.__setattr__(self, "freshness_policy_id", policy_id)
        object.__setattr__(self, "supported_actions", tuple(self.supported_actions))
        importance = self.importance or (
            ResearchRequirementImportance.MANDATORY
            if self.mandatory
            else ResearchRequirementImportance.IMPORTANT
        )
        if not isinstance(importance, ResearchRequirementImportance):
            importance = ResearchRequirementImportance(importance)
        inputs = tuple(self.inputs)
        input_ids = [item.input_id for item in inputs]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError(f"Concrete input IDs must be unique for {requirement_id}")
        object.__setattr__(self, "importance", importance)
        object.__setattr__(self, "inputs", inputs)


class ResearchRequirementRegistry:
    """Immutable registry of data requirements used by deterministic rules."""

    def __init__(
        self,
        requirements: Sequence[ResearchRequirement],
        area_weights: Mapping[RuleEngineArea, Decimal] = RULE_ENGINE_AREA_WEIGHTS,
    ) -> None:
        by_id = {requirement.requirement_id: requirement for requirement in requirements}
        if len(by_id) != len(requirements):
            raise ValueError("Research requirement IDs must be unique")
        if set(area_weights) != set(RuleEngineArea):
            raise ValueError("Every Rule Engine area must have exactly one configured weight")
        if sum(area_weights.values(), Decimal("0")) != Decimal("1.00"):
            raise ValueError("Rule Engine area weights must total 1.00")
        missing_areas = set(RuleEngineArea) - {item.rule_engine_area for item in requirements}
        if missing_areas:
            raise ValueError(f"Requirements are missing Rule Engine areas: {sorted(missing_areas)}")
        self._requirements = tuple(requirements)
        self._by_id = MappingProxyType(by_id)
        self._area_weights = MappingProxyType(dict(area_weights))

    @property
    def requirements(self) -> tuple[ResearchRequirement, ...]:
        return self._requirements

    @property
    def area_weights(self) -> Mapping[RuleEngineArea, Decimal]:
        return self._area_weights

    def get(self, requirement_id: str) -> ResearchRequirement:
        return self._by_id[_normalized_key(requirement_id, "requirement_id")]

    def for_area(self, area: RuleEngineArea) -> tuple[ResearchRequirement, ...]:
        return tuple(item for item in self._requirements if item.rule_engine_area == area)

    @classmethod
    def default(cls) -> "ResearchRequirementRegistry":
        mandatory = ResearchRequirementImportance.MANDATORY
        important = ResearchRequirementImportance.IMPORTANT
        supporting = ResearchRequirementImportance.SUPPORTING
        input_ = ResearchRequirementInput
        return cls(
            (
                ResearchRequirement(
                    "VALUATION_INPUTS",
                    RuleEngineArea.VALUATION,
                    True,
                    "VALUATION_INPUTS",
                    inputs=(
                        input_("LATEST_USABLE_PRICE", mandatory),
                        input_("EARNINGS_BASIS", mandatory),
                        input_("PE", important),
                        input_("PB", important),
                        input_("EV_EBITDA", supporting),
                        input_("FCF_YIELD", supporting),
                        input_("HISTORICAL_OR_PEER_VALUATION", supporting),
                    ),
                ),
                ResearchRequirement(
                    "BUSINESS_QUALITY_FACTS",
                    RuleEngineArea.FUNDAMENTAL_BUSINESS_QUALITY,
                    True,
                    "ANNUAL_FINANCIALS",
                    inputs=(
                        input_("PROFITABILITY_HISTORY", mandatory),
                        input_("ROE", important),
                        input_("ROCE", important),
                        input_("MARGINS", important),
                        input_("CASH_CONVERSION_OR_FCF_QUALITY", important),
                    ),
                ),
                ResearchRequirement(
                    "GROWTH_FACTS",
                    RuleEngineArea.GROWTH,
                    True,
                    "QUARTERLY_FINANCIALS",
                    inputs=(
                        input_("REVENUE_HISTORY", mandatory),
                        input_("EARNINGS_HISTORY", mandatory),
                        input_("QUARTERLY_YOY_QOQ_TRENDS", important),
                        input_("ANNUAL_CAGR_INPUTS", important),
                    ),
                ),
                ResearchRequirement(
                    "BALANCE_SHEET_FACTS",
                    RuleEngineArea.BALANCE_SHEET,
                    True,
                    "QUARTERLY_FINANCIALS",
                    inputs=(
                        input_("DEBT", mandatory),
                        input_("EQUITY", mandatory),
                        input_("CASH", important),
                        input_("INTEREST_COVERAGE_INPUTS", supporting),
                        input_("LIQUIDITY_CURRENT_RATIO_INPUTS", supporting),
                    ),
                ),
                ResearchRequirement(
                    "QUARTERLY_FINANCIALS",
                    RuleEngineArea.QUARTERLY_EARNINGS_TREND,
                    True,
                    "QUARTERLY_FINANCIALS",
                    inputs=(
                        input_("LATEST_QUARTERLY_RESULT", mandatory),
                        input_("COMPARABLE_QUARTERS", mandatory),
                        input_("QUARTERLY_REVENUE", mandatory),
                        input_("QUARTERLY_PAT", mandatory),
                        input_("QUARTERLY_EBITDA_OR_OPERATING_PROFIT", important),
                        input_("QUARTERLY_EPS", important),
                        input_("QUARTERLY_MARGINS", important),
                    ),
                ),
                ResearchRequirement(
                    "ORDER_BOOK_CAPEX_GUIDANCE",
                    RuleEngineArea.ORDER_BOOK_CAPACITY_CATALYSTS,
                    False,
                    "ORDER_BOOK_CAPEX_GUIDANCE",
                    importance=important,
                    inputs=(
                        input_("MATERIAL_CATALYST_EVIDENCE", mandatory),
                        input_("ORDER_BOOK_OR_MAJOR_CONTRACT", important),
                        input_("CAPACITY_OR_CAPEX_OR_COMMISSIONING", important),
                        input_("MANAGEMENT_GUIDANCE", supporting),
                    ),
                ),
                ResearchRequirement(
                    "LATEST_PRICE",
                    RuleEngineArea.PRICE_TECHNICAL,
                    True,
                    "LATEST_PRICE",
                    inputs=(input_("LATEST_USABLE_PRICE", mandatory),),
                ),
                ResearchRequirement(
                    "HISTORICAL_PRICE_SERIES",
                    RuleEngineArea.PRICE_TECHNICAL,
                    True,
                    "HISTORICAL_PRICE_SERIES",
                    inputs=(
                        input_("DURABLE_PRICE_OBSERVATIONS", mandatory),
                        input_("FIFTY_OBSERVATION_TECHNICAL_BASIS", important),
                        input_("ONE_HUNDRED_FIFTY_OBSERVATION_TECHNICAL_BASIS", supporting),
                    ),
                ),
                ResearchRequirement(
                    "CURRENT_NEWS",
                    RuleEngineArea.NEWS_GEOPOLITICAL_EVENTS,
                    False,
                    "CURRENT_NEWS",
                    inputs=(input_("RELEVANT_CURRENT_EVENT_EVIDENCE", mandatory),),
                ),
                ResearchRequirement(
                    "SHAREHOLDING",
                    RuleEngineArea.SHAREHOLDING,
                    False,
                    "SHAREHOLDING",
                    importance=important,
                    inputs=(
                        input_("LATEST_VALID_SHAREHOLDING_PERIOD", mandatory),
                        input_("PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES", important),
                        input_("PROMOTER_PLEDGE", supporting),
                    ),
                ),
                ResearchRequirement(
                    "GOVERNANCE_HISTORY",
                    RuleEngineArea.MANAGEMENT_GOVERNANCE,
                    True,
                    "GOVERNANCE_HISTORY",
                    inputs=(input_("GOVERNANCE_EVIDENCE", mandatory),),
                ),
                ResearchRequirement(
                    "SECTOR_MACRO",
                    RuleEngineArea.SECTOR_MACRO,
                    True,
                    "SECTOR_MACRO",
                    inputs=(
                        input_("CANONICAL_SECTOR", mandatory),
                        input_("SECTOR_PERFORMANCE", important),
                        input_("RELEVANT_MACRO_EVENT_EXPOSURE", supporting),
                    ),
                ),
            )
        )


@dataclass(frozen=True)
class ResearchEvidence:
    evidence_id: str
    requirement_id: str
    source: str
    source_tier: ResearchSourceTier
    retrieved_at: datetime
    as_of: datetime | None = None
    published_at: datetime | None = None
    event_date: datetime | None = None
    valid_until: datetime | None = None
    fact_key: str | None = None
    value_fingerprint: str | None = None
    complete: bool = True
    confidence: float | None = None
    unresolved: bool = False
    source_url: str | None = None
    covered_input_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        evidence_id = str(self.evidence_id).strip()
        if not evidence_id:
            raise ValueError("evidence_id is required")
        source = str(self.source).strip()
        if not source:
            raise ValueError("source is required")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        for name in ("retrieved_at", "as_of", "published_at", "event_date", "valid_until"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "requirement_id", _normalized_key(self.requirement_id, "requirement_id"))
        object.__setattr__(self, "source", source.upper())
        object.__setattr__(
            self,
            "covered_input_ids",
            tuple(_normalized_key(value, "covered_input_id") for value in self.covered_input_ids),
        )
        if not isinstance(self.source_tier, ResearchSourceTier):
            object.__setattr__(self, "source_tier", ResearchSourceTier(self.source_tier))


@dataclass(frozen=True)
class FreshnessPolicy:
    policy_id: str
    mode: FreshnessMode
    maximum_age: timedelta | None
    date_basis: FreshnessDateBasis = FreshnessDateBasis.AS_OF_OR_RETRIEVED
    scoring_window: timedelta | None = None
    retain_while_unresolved: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _normalized_key(self.policy_id, "policy_id"))
        if self.maximum_age is not None and self.maximum_age <= timedelta(0):
            raise ValueError("maximum_age must be positive")
        if self.scoring_window is not None and self.scoring_window <= timedelta(0):
            raise ValueError("scoring_window must be positive")

    def evidence_time(self, evidence: ResearchEvidence) -> datetime | None:
        if self.date_basis == FreshnessDateBasis.EVENT_OR_PUBLICATION:
            return evidence.event_date or evidence.published_at
        return evidence.as_of or evidence.published_at or evidence.retrieved_at

    def age(self, evidence: ResearchEvidence, now: datetime) -> timedelta | None:
        _require_aware(now, "now")
        anchor = self.evidence_time(evidence)
        return None if anchor is None else now.astimezone(timezone.utc) - anchor.astimezone(timezone.utc)

    def is_fresh(self, evidence: ResearchEvidence, now: datetime) -> bool:
        _require_aware(now, "now")
        if self.retain_while_unresolved and evidence.unresolved:
            return True
        if evidence.valid_until is not None:
            return now.astimezone(timezone.utc) <= evidence.valid_until.astimezone(timezone.utc)
        age = self.age(evidence, now)
        if age is None or age < timedelta(0):
            return False
        return self.maximum_age is None or age <= self.maximum_age


class FreshnessPolicyRegistry:
    """Fact-specific policies; there is intentionally no broad research TTL."""

    def __init__(self, policies: Sequence[FreshnessPolicy]) -> None:
        by_id = {policy.policy_id: policy for policy in policies}
        if len(by_id) != len(policies):
            raise ValueError("Freshness policy IDs must be unique")
        self._by_id = MappingProxyType(by_id)

    def get(self, policy_id: str) -> FreshnessPolicy:
        return self._by_id[_normalized_key(policy_id, "policy_id")]

    @classmethod
    def default(cls) -> "FreshnessPolicyRegistry":
        day = timedelta(days=1)
        return cls(
            (
                FreshnessPolicy(
                    "LATEST_PRICE",
                    FreshnessMode.MARKET_SESSION_AWARE,
                    timedelta(minutes=15),
                    description="Use exchange/session valid-until when supplied; otherwise expire by minutes.",
                ),
                FreshnessPolicy(
                    "HISTORICAL_PRICE_SERIES",
                    FreshnessMode.DAILY_INCREMENTAL,
                    timedelta(hours=36),
                    description="Advance the durable daily series incrementally after the next expected session.",
                ),
                FreshnessPolicy(
                    "QUARTERLY_FINANCIALS",
                    FreshnessMode.RELEASE_AWARE_QUARTERLY,
                    timedelta(days=120),
                    description="Use the issuer release calendar through valid-until when known.",
                ),
                FreshnessPolicy(
                    "ANNUAL_FINANCIALS",
                    FreshnessMode.REPORTING_CALENDAR_AWARE_ANNUAL,
                    timedelta(days=400),
                    description="Use the issuer reporting calendar through valid-until when known.",
                ),
                FreshnessPolicy("SHAREHOLDING", FreshnessMode.QUARTERLY, timedelta(days=120)),
                FreshnessPolicy(
                    "ORDER_BOOK_CAPEX_GUIDANCE",
                    FreshnessMode.EVENT_DRIVEN_BOUNDED,
                    timedelta(days=30),
                ),
                FreshnessPolicy("VALUATION_INPUTS", FreshnessMode.FACT_SPECIFIC_DAILY, day),
                FreshnessPolicy(
                    "CURRENT_NEWS",
                    FreshnessMode.ROLLING_EVENT_WINDOW,
                    timedelta(days=1),
                    date_basis=FreshnessDateBasis.EVENT_OR_PUBLICATION,
                    scoring_window=timedelta(days=30),
                ),
                FreshnessPolicy(
                    "GOVERNANCE_HISTORY",
                    FreshnessMode.UNRESOLVED_HISTORY,
                    timedelta(days=365),
                    date_basis=FreshnessDateBasis.EVENT_OR_PUBLICATION,
                    retain_while_unresolved=True,
                ),
                FreshnessPolicy("SECTOR_MACRO", FreshnessMode.FACT_SPECIFIC_DAILY, day),
            )
        )


@dataclass(frozen=True)
class ExternalResearchToolAuthorization:
    """Serializable grant issued only after the fallback policy permits a target."""

    global_instrument_id: UUID
    requirement_id: str
    permitted_provider_ids: tuple[str, ...]
    issued_at: datetime
    issued_by: str = "ProviderFallbackPolicy"
    authorized: bool = True

    def __post_init__(self) -> None:
        _require_global_instrument_id(self.global_instrument_id)
        object.__setattr__(self, "requirement_id", _normalized_key(self.requirement_id, "requirement_id"))
        providers = tuple(
            _normalized_key(value, "permitted_provider_id") for value in self.permitted_provider_ids
        )
        if not providers:
            raise ValueError("at least one permitted external provider is required")
        object.__setattr__(self, "permitted_provider_ids", providers)
        _require_aware(self.issued_at, "issued_at")

    def as_gateway_payload(self) -> dict[str, object]:
        return {
            "authorized": self.authorized,
            "issuedBy": self.issued_by,
            "globalInstrumentId": str(self.global_instrument_id),
            "requirementId": self.requirement_id,
            "permittedProviderIds": list(self.permitted_provider_ids),
            "issuedAt": self.issued_at.isoformat(),
        }


@dataclass(frozen=True)
class ProviderFallbackPolicy:
    trigger_statuses: frozenset[ResearchRequirementStatus] = frozenset(
        {
            ResearchRequirementStatus.MISSING,
            ResearchRequirementStatus.PARTIAL,
            ResearchRequirementStatus.CONFLICTING,
            ResearchRequirementStatus.FAILED,
        }
    )
    low_confidence_below: float = 0.70
    allow_external_tool_gateway: bool = True

    def permits(self, status: ResearchRequirementStatus, confidence: float | None = None) -> bool:
        return (
            status in self.trigger_statuses
            or (confidence is not None and confidence < self.low_confidence_below)
        ) and self.allow_external_tool_gateway

    def authorize_external_tool(
        self,
        *,
        global_instrument_id: UUID,
        requirement_id: str,
        status: ResearchRequirementStatus,
        confidence: float | None,
        permitted_provider_ids: Sequence[str],
        now: datetime | None = None,
    ) -> ExternalResearchToolAuthorization | None:
        """Issue a narrow MCP gateway grant only for a policy-approved fallback."""
        if not self.permits(status, confidence):
            return None
        return ExternalResearchToolAuthorization(
            global_instrument_id=global_instrument_id,
            requirement_id=requirement_id,
            permitted_provider_ids=tuple(permitted_provider_ids),
            issued_at=now or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class ProviderAuthority:
    source: str
    source_tier: ResearchSourceTier

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalized_key(self.source, "source"))


@dataclass(frozen=True)
class ProviderAuthorityPolicy:
    requirement_id: str
    jurisdiction: str
    authorities: tuple[ProviderAuthority, ...]
    fallback_policy: ProviderFallbackPolicy = field(default_factory=ProviderFallbackPolicy)
    selection_semantics: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _normalized_key(self.requirement_id, "requirement_id"))
        object.__setattr__(self, "jurisdiction", _normalized_key(self.jurisdiction, "jurisdiction"))
        object.__setattr__(self, "authorities", tuple(self.authorities))
        sources = [authority.source for authority in self.authorities]
        if len(sources) != len(set(sources)):
            raise ValueError("Provider authority sources must be unique within a policy")

    def rank(self, evidence: ResearchEvidence) -> tuple[int, int]:
        explicit = next(
            (index for index, authority in enumerate(self.authorities) if authority.source == evidence.source),
            None,
        )
        if explicit is not None:
            return explicit, _SOURCE_TIER_AUTHORITY[evidence.source_tier]
        return len(self.authorities) + 1, _SOURCE_TIER_AUTHORITY[evidence.source_tier]


class ProviderAuthorityRegistry:
    """Selects authority by fact and jurisdiction, never by one global provider rank."""

    def __init__(self, policies: Sequence[ProviderAuthorityPolicy]) -> None:
        by_key = {(item.requirement_id, item.jurisdiction): item for item in policies}
        if len(by_key) != len(policies):
            raise ValueError("Provider authority policies must be unique by requirement and jurisdiction")
        self._by_key = MappingProxyType(by_key)

    def policy_for(self, requirement_id: str, jurisdiction: str) -> ProviderAuthorityPolicy:
        requirement = _normalized_key(requirement_id, "requirement_id")
        region = _normalized_key(jurisdiction, "jurisdiction")
        policy = self._by_key.get((requirement, region)) or self._by_key.get((requirement, "GLOBAL"))
        if policy is None:
            raise KeyError(
                f"No fact-specific provider authority policy for {requirement} in {region}"
            )
        return policy

    @classmethod
    def default(cls) -> "ProviderAuthorityRegistry":
        policies: list[ProviderAuthorityPolicy] = []
        financial_requirements = (
            "VALUATION_INPUTS",
            "BUSINESS_QUALITY_FACTS",
            "GROWTH_FACTS",
            "BALANCE_SHEET_FACTS",
            "QUARTERLY_FINANCIALS",
        )
        financial_sources = {
            "GLOBAL": (
                ("REGULATORY_FILING", ResearchSourceTier.REGULATORY),
                ("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                ("LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED),
                ("APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY),
                ("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                ("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
            ),
            "INDIA": (
                ("NSE", ResearchSourceTier.OFFICIAL),
                ("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                ("LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED),
                ("APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY),
                ("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                ("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
            ),
            "USA": (
                ("SEC_EDGAR", ResearchSourceTier.REGULATORY),
                ("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                ("LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED),
                ("APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY),
                ("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                ("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
            ),
            "EUROPE": (
                ("REGULATORY_FILING", ResearchSourceTier.REGULATORY),
                ("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                ("EODHD", ResearchSourceTier.LICENSED_STRUCTURED),
                ("APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY),
                ("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                ("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
            ),
        }
        for requirement_id in financial_requirements:
            for region, sources in financial_sources.items():
                policies.append(
                    ProviderAuthorityPolicy(
                        requirement_id,
                        region,
                        tuple(ProviderAuthority(source, tier) for source, tier in sources),
                    )
                )

        market_sources = tuple(
            ProviderAuthority(source, tier)
            for source, tier in (
                ("CONFIGURED_MARKET_DATA", ResearchSourceTier.TRUSTED_MARKET_DATA),
                ("EXCHANGE_MARKET_DATA", ResearchSourceTier.OFFICIAL),
                ("LICENSED_MARKET_DATA", ResearchSourceTier.LICENSED_STRUCTURED),
                ("YAHOO_FINANCE", ResearchSourceTier.APPROVED_SECONDARY),
            )
        )
        for requirement_id in ("LATEST_PRICE", "HISTORICAL_PRICE_SERIES"):
            policies.append(
                ProviderAuthorityPolicy(
                    requirement_id,
                    "GLOBAL",
                    market_sources,
                    selection_semantics="Choose by exchange, market session, and required freshness.",
                )
            )

        policies.extend(
            (
                ProviderAuthorityPolicy(
                    "SHAREHOLDING",
                    "INDIA",
                    (
                        ProviderAuthority("NSE", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority("LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED),
                        ProviderAuthority("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "SHAREHOLDING",
                    "GLOBAL",
                    (
                        ProviderAuthority("REGULATORY_FILING", ResearchSourceTier.REGULATORY),
                        ProviderAuthority("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority(
                            "LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED
                        ),
                        ProviderAuthority(
                            "APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY
                        ),
                        ProviderAuthority(
                            "APPROVED_EXTERNAL_TOOL",
                            ResearchSourceTier.APPROVED_EXTERNAL_TOOL,
                        ),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "ORDER_BOOK_CAPEX_GUIDANCE",
                    "GLOBAL",
                    (
                        ProviderAuthority("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority("REGULATORY_FILING", ResearchSourceTier.REGULATORY),
                        ProviderAuthority(
                            "APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY
                        ),
                        ProviderAuthority(
                            "APPROVED_EXTERNAL_TOOL",
                            ResearchSourceTier.APPROVED_EXTERNAL_TOOL,
                        ),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "CURRENT_NEWS",
                    "GLOBAL",
                    (
                        ProviderAuthority("SEARCH_COVERAGE", ResearchSourceTier.APPROVED_SECONDARY),
                        ProviderAuthority("REGULATORY_FILING", ResearchSourceTier.REGULATORY),
                        ProviderAuthority("OFFICIAL_COMPANY", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority("REPUTABLE_NEWS", ResearchSourceTier.APPROVED_SECONDARY),
                        ProviderAuthority("APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "GOVERNANCE_HISTORY",
                    "GLOBAL",
                    (
                        ProviderAuthority(
                            "REGULATOR_OR_COURT_RECORD", ResearchSourceTier.REGULATORY
                        ),
                        ProviderAuthority("COMPANY_FILING", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority(
                            "REPUTABLE_NEWS", ResearchSourceTier.APPROVED_SECONDARY
                        ),
                        ProviderAuthority(
                            "APPROVED_EXTERNAL_TOOL",
                            ResearchSourceTier.APPROVED_EXTERNAL_TOOL,
                        ),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "SECTOR_MACRO",
                    "GLOBAL",
                    (
                        ProviderAuthority(
                            "OFFICIAL_STATISTICS_OR_CENTRAL_BANK",
                            ResearchSourceTier.OFFICIAL,
                        ),
                        ProviderAuthority(
                            "EXCHANGE_OR_INDEX_PROVIDER", ResearchSourceTier.TRUSTED_MARKET_DATA
                        ),
                        ProviderAuthority(
                            "LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED
                        ),
                        ProviderAuthority(
                            "REPUTABLE_NEWS", ResearchSourceTier.APPROVED_SECONDARY
                        ),
                        ProviderAuthority(
                            "APPROVED_EXTERNAL_TOOL",
                            ResearchSourceTier.APPROVED_EXTERNAL_TOOL,
                        ),
                        ProviderAuthority("USER_UPLOAD", ResearchSourceTier.USER_UPLOAD),
                    ),
                ),
                ProviderAuthorityPolicy(
                    "CANONICAL_IDENTITY",
                    "GLOBAL",
                    (
                        ProviderAuthority("ISIN_OR_PERMANENT_ID", ResearchSourceTier.OFFICIAL),
                        ProviderAuthority("EXACT_EXCHANGE_AND_SYMBOL", ResearchSourceTier.REGULATORY),
                        ProviderAuthority("NAME_CONFIRMATION", ResearchSourceTier.UNVERIFIED),
                    ),
                    ProviderFallbackPolicy(allow_external_tool_gateway=False),
                ),
            )
        )
        return cls(policies)


@dataclass(frozen=True)
class DurableResearchSnapshot:
    """One DB-first read result; it never contains newly fetched provider data."""

    global_instrument_id: UUID
    evidence_by_requirement: Mapping[str, Sequence[ResearchEvidence]] = field(default_factory=dict)
    supported_requirement_ids: frozenset[str] | None = None
    refreshing_requirement_ids: frozenset[str] = frozenset()
    failure_reasons: Mapping[str, str] = field(default_factory=dict)
    applicability_by_requirement: Mapping[str, RequirementApplicability] = field(default_factory=dict)
    acquisition_observations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_global_instrument_id(self.global_instrument_id)
        normalized: dict[str, tuple[ResearchEvidence, ...]] = {}
        for requirement_id, values in self.evidence_by_requirement.items():
            key = _normalized_key(requirement_id, "requirement_id")
            evidence = tuple(values)
            if any(item.requirement_id != key for item in evidence):
                raise ValueError(f"Evidence does not match requirement {key}")
            normalized[key] = evidence
        supported = self.supported_requirement_ids
        if supported is not None:
            supported = frozenset(_normalized_key(value, "requirement_id") for value in supported)
        refreshing = frozenset(
            _normalized_key(value, "requirement_id") for value in self.refreshing_requirement_ids
        )
        failures = {
            _normalized_key(key, "requirement_id"): str(value)
            for key, value in self.failure_reasons.items()
        }
        object.__setattr__(self, "evidence_by_requirement", MappingProxyType(normalized))
        object.__setattr__(self, "supported_requirement_ids", supported)
        object.__setattr__(self, "refreshing_requirement_ids", refreshing)
        object.__setattr__(self, "failure_reasons", MappingProxyType(failures))

    def evidence_for(self, requirement_id: str) -> tuple[ResearchEvidence, ...]:
        return tuple(self.evidence_by_requirement.get(_normalized_key(requirement_id, "requirement_id"), ()))


class ResearchReadinessDataSource(Protocol):
    """Durable DB reader implemented by a later persistence adapter."""

    def load_by_global_instrument_id(
        self,
        global_instrument_id: UUID,
        requirements: Sequence[ResearchRequirement],
    ) -> DurableResearchSnapshot:
        ...


@dataclass(frozen=True)
class ConflictResolution:
    selected: ResearchEvidence | None
    conflict_reason: str | None = None


class ResearchConflictResolver:
    """Detect disagreement only for the same canonical fact at equal authority."""

    def resolve(
        self,
        requirement: ResearchRequirement,
        evidence: Sequence[ResearchEvidence],
        authority_policy: ProviderAuthorityPolicy,
    ) -> ConflictResolution:
        if not evidence:
            return ConflictResolution(None)
        ordered = sorted(
            evidence,
            key=lambda item: (
                authority_policy.rank(item),
                -(item.as_of or item.published_at or item.retrieved_at).timestamp(),
                -item.retrieved_at.timestamp(),
                item.evidence_id,
            ),
        )
        selected = ordered[0]
        by_fact: dict[str, list[ResearchEvidence]] = {}
        for item in evidence:
            if item.fact_key and item.value_fingerprint is not None:
                by_fact.setdefault(item.fact_key, []).append(item)
        for fact_key, candidates in sorted(by_fact.items()):
            best_rank = min(authority_policy.rank(item) for item in candidates)
            peers = [item for item in candidates if authority_policy.rank(item) == best_rank]
            values = {str(item.value_fingerprint).strip().casefold() for item in peers}
            if len(values) > 1:
                ids = ",".join(sorted(item.evidence_id for item in peers))
                return ConflictResolution(selected, f"EQUAL_AUTHORITY_CONFLICT:{fact_key}:{ids}")
        return ConflictResolution(selected)


class ResearchCoverageService:
    """Separates durable history from evidence eligible for current rules."""

    def score_input_evidence(
        self,
        requirement: ResearchRequirement,
        evidence: Sequence[ResearchEvidence],
        freshness_policy: FreshnessPolicy,
        now: datetime,
    ) -> tuple[ResearchEvidence, ...]:
        _require_aware(now, "now")
        if freshness_policy.scoring_window is None:
            return tuple(evidence)
        cutoff = now.astimezone(timezone.utc) - freshness_policy.scoring_window
        eligible: list[ResearchEvidence] = []
        for item in evidence:
            event_at = item.event_date or item.published_at
            if event_at is None:
                continue
            normalized = event_at.astimezone(timezone.utc)
            if cutoff <= normalized <= now.astimezone(timezone.utc):
                eligible.append(item)
        return tuple(eligible)

    def governance_history(self, snapshot: DurableResearchSnapshot) -> tuple[ResearchEvidence, ...]:
        """Return durable governance evidence without applying the current-news window."""
        return snapshot.evidence_for("GOVERNANCE_HISTORY")

    @staticmethod
    def covered_input_ids(
        requirement: ResearchRequirement,
        evidence: Sequence[ResearchEvidence],
    ) -> frozenset[str]:
        if not requirement.inputs:
            return frozenset()
        all_inputs = frozenset(item.input_id for item in requirement.inputs)
        covered: set[str] = set()
        for item in evidence:
            if not item.complete:
                continue
            # Empty coverage is the backwards-compatible aggregate-evidence
            # contract used by existing adapters and fixtures.
            covered.update(item.covered_input_ids or all_inputs)
        return frozenset(value for value in covered if value in all_inputs)


class ResearchDataConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True)
class ResearchRequirementReadiness:
    requirement_id: str
    rule_engine_area: RuleEngineArea
    mandatory: bool
    status: ResearchRequirementStatus
    source: str | None
    source_tier: ResearchSourceTier | None
    as_of: datetime | None
    retrieved_at: datetime | None
    age: timedelta | None
    freshness_policy: FreshnessPolicy
    evidence_ids: tuple[str, ...]
    missing_reason: str | None
    conflict_reason: str | None
    supported_actions: tuple[ResearchSupportedAction, ...]
    importance: ResearchRequirementImportance
    source_url: str | None
    covered_input_ids: tuple[str, ...]
    missing_input_ids: tuple[str, ...]
    coverage_pct: int
    critical_coverage_pct: int
    applicability: str = "APPLICABLE"
    applicability_reason: str | None = None
    classification: str | None = None
    classification_source: str | None = None
    not_applicable_input_reasons: Mapping[str, str] = field(default_factory=dict)
    acquisition_observation: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResearchReadinessResult:
    global_instrument_id: UUID
    requirements: tuple[ResearchRequirementReadiness, ...]
    generated_at: datetime
    overall_status: ResearchRequirementStatus = ResearchRequirementStatus.PARTIAL
    overall_completeness_pct: int = 0
    critical_completeness_pct: int = 0
    confidence: ResearchDataConfidence = ResearchDataConfidence.LOW
    confidence_pct: int = 0

    def __post_init__(self) -> None:
        _require_global_instrument_id(self.global_instrument_id)
        _require_aware(self.generated_at, "generated_at")

    def for_requirement(self, requirement_id: str) -> ResearchRequirementReadiness:
        key = _normalized_key(requirement_id, "requirement_id")
        return next(item for item in self.requirements if item.requirement_id == key)

    @property
    def mandatory_ready(self) -> bool:
        return all(
            not item.mandatory or item.status in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.NOT_APPLICABLE}
            for item in self.requirements
        )


class ResearchReadinessService:
    """Read durable evidence once and classify it without provider side effects."""

    def __init__(
        self,
        data_source: ResearchReadinessDataSource,
        requirement_registry: ResearchRequirementRegistry | None = None,
        freshness_registry: FreshnessPolicyRegistry | None = None,
        authority_registry: ProviderAuthorityRegistry | None = None,
        conflict_resolver: ResearchConflictResolver | None = None,
        coverage_service: ResearchCoverageService | None = None,
    ) -> None:
        self.data_source = data_source
        self.requirement_registry = requirement_registry or ResearchRequirementRegistry.default()
        self.freshness_registry = freshness_registry or FreshnessPolicyRegistry.default()
        self.authority_registry = authority_registry or ProviderAuthorityRegistry.default()
        self.conflict_resolver = conflict_resolver or ResearchConflictResolver()
        self.coverage_service = coverage_service or ResearchCoverageService()

    def assess(
        self,
        global_instrument_id: UUID,
        *,
        jurisdiction: str = "GLOBAL",
        now: datetime | None = None,
    ) -> ResearchReadinessResult:
        instrument_id = _require_global_instrument_id(global_instrument_id)
        evaluated_at = now or datetime.now(timezone.utc)
        _require_aware(evaluated_at, "now")
        requirements = self.requirement_registry.requirements
        snapshot = self.data_source.load_by_global_instrument_id(instrument_id, requirements)
        if snapshot.global_instrument_id != instrument_id:
            raise ValueError("Durable snapshot globalInstrumentId does not match the request")
        classified = tuple(
            self._classify(requirement, snapshot, jurisdiction, evaluated_at)
            for requirement in requirements
        )
        overall_completeness = self._overall_completeness(classified)
        critical_completeness = self._critical_completeness(classified)
        overall_status = self._overall_status(classified)
        confidence_pct = self._confidence_pct(
            classified, overall_completeness, critical_completeness
        )
        confidence = (
            ResearchDataConfidence.HIGH
            if confidence_pct >= 80
            else ResearchDataConfidence.MEDIUM
            if confidence_pct >= 55
            else ResearchDataConfidence.LOW
        )
        return ResearchReadinessResult(
            instrument_id,
            classified,
            evaluated_at,
            overall_status=overall_status,
            overall_completeness_pct=overall_completeness,
            critical_completeness_pct=critical_completeness,
            confidence=confidence,
            confidence_pct=confidence_pct,
        )

    def _classify(
        self,
        requirement: ResearchRequirement,
        snapshot: DurableResearchSnapshot,
        jurisdiction: str,
        now: datetime,
    ) -> ResearchRequirementReadiness:
        policy = self.freshness_registry.get(requirement.freshness_policy_id)
        applicability = snapshot.applicability_by_requirement.get(requirement.requirement_id, RequirementApplicability())
        def with_applicability(value):
            return replace(value, applicability=applicability.state,
                applicability_reason=applicability.reason, classification=applicability.classification,
                classification_source=applicability.source,
                not_applicable_input_reasons=applicability.excluded_inputs,
                acquisition_observation=snapshot.acquisition_observations.get(requirement.requirement_id))
        news_state = snapshot.acquisition_observations.get(requirement.requirement_id, {}).get('news_readiness')
        if requirement.requirement_id == 'CURRENT_NEWS' and news_state in {'PARTIAL_SEARCH','FAILED_SEARCH','STALE_SEARCH'}:
            state = {'PARTIAL_SEARCH':ResearchRequirementStatus.PARTIAL,'FAILED_SEARCH':ResearchRequirementStatus.FAILED,
                'STALE_SEARCH':ResearchRequirementStatus.READY_STALE}[news_state]
            return with_applicability(self._result(requirement,policy,state,missing_reason=news_state))
        if applicability.state == "NOT_APPLICABLE":
            return with_applicability(self._result(requirement, policy, ResearchRequirementStatus.NOT_APPLICABLE))
        if applicability.excluded_inputs:
            requirement = replace(requirement, inputs=tuple(
                item for item in requirement.inputs if item.input_id not in applicability.excluded_inputs))
        supported = (
            snapshot.supported_requirement_ids is None
            or requirement.requirement_id in snapshot.supported_requirement_ids
        )
        if not supported:
            return self._result(
                requirement,
                policy,
                ResearchRequirementStatus.UNSUPPORTED,
                missing_reason="REQUIREMENT_UNSUPPORTED_FOR_INSTRUMENT",
            )

        durable_evidence = snapshot.evidence_for(requirement.requirement_id)
        eligible = self.coverage_service.score_input_evidence(requirement, durable_evidence, policy, now)
        authority = self.authority_registry.policy_for(requirement.requirement_id, jurisdiction)
        resolution = self.conflict_resolver.resolve(requirement, eligible, authority)
        selected = resolution.selected
        covered_inputs = self.coverage_service.covered_input_ids(requirement, eligible)
        missing_inputs = tuple(
            item.input_id for item in requirement.inputs if item.input_id not in covered_inputs
        )
        mandatory_inputs = tuple(
            item.input_id
            for item in requirement.inputs
            if item.importance == ResearchRequirementImportance.MANDATORY
        )
        missing_mandatory_inputs = tuple(
            item_id for item_id in mandatory_inputs if item_id not in covered_inputs
        )

        if requirement.requirement_id in snapshot.refreshing_requirement_ids:
            status = ResearchRequirementStatus.REFRESHING
            missing_reason = None
        elif resolution.conflict_reason:
            status = ResearchRequirementStatus.CONFLICTING
            missing_reason = None
        elif not eligible:
            failure = snapshot.failure_reasons.get(requirement.requirement_id)
            status = ResearchRequirementStatus.FAILED if failure else ResearchRequirementStatus.MISSING
            observed_empty = any(row.get("outcome") == "SUCCESS_EMPTY" for row in
                snapshot.acquisition_observations.get(requirement.requirement_id, {}).get("history", []))
            missing_reason = failure or ("NO_QUALIFYING_CURRENT_EVENTS" if observed_empty else None) or (
                "NO_EVIDENCE_IN_CURRENT_NEWS_WINDOW"
                if policy.scoring_window is not None and durable_evidence
                else "NO_DURABLE_EVIDENCE"
            )
        elif sum(1 for item in eligible if item.complete) < requirement.minimum_evidence_count:
            status = ResearchRequirementStatus.PARTIAL
            missing_reason = "INSUFFICIENT_COMPLETE_EVIDENCE"
        elif missing_mandatory_inputs:
            status = ResearchRequirementStatus.PARTIAL
            missing_reason = "MISSING_REQUIRED_INPUTS:" + ",".join(missing_mandatory_inputs)
        elif self._required_inputs_are_fresh(
            requirement, eligible, authority, policy, now
        ):
            status = ResearchRequirementStatus.READY_FRESH
            missing_reason = None
        else:
            status = ResearchRequirementStatus.READY_STALE
            missing_reason = "FRESHNESS_POLICY_EXPIRED"

        # Report the actual mandatory freshness blocker, not a newer supporting
        # input (e.g. June finance cost beside March debt/equity for TMCV).
        if status == ResearchRequirementStatus.READY_STALE:
            blockers = []
            for input_id in mandatory_inputs:
                candidates = [e for e in eligible if e.complete and input_id in
                    (e.covered_input_ids or tuple(i.input_id for i in requirement.inputs))]
                if candidates:
                    chosen = min(candidates,key=lambda e:(authority.rank(e),
                        -(e.as_of or e.published_at or e.retrieved_at).timestamp(),-e.retrieved_at.timestamp(),e.evidence_id))
                    if not policy.is_fresh(chosen,now): blockers.append((input_id,chosen))
            if blockers:
                selected = min((e for _,e in blockers),key=lambda e:(policy.evidence_time(e),e.evidence_id))
                missing_reason += ':' + ','.join(sorted(i for i,_ in blockers))

        return with_applicability(self._result(
            requirement,
            policy,
            status,
            selected=selected,
            evidence=eligible,
            missing_reason=missing_reason,
            conflict_reason=resolution.conflict_reason,
            now=now,
            covered_input_ids=covered_inputs,
            missing_input_ids=missing_inputs,
        ))

    @staticmethod
    def _result(
        requirement: ResearchRequirement,
        policy: FreshnessPolicy,
        status: ResearchRequirementStatus,
        *,
        selected: ResearchEvidence | None = None,
        evidence: Sequence[ResearchEvidence] = (),
        missing_reason: str | None = None,
        conflict_reason: str | None = None,
        now: datetime | None = None,
        covered_input_ids: frozenset[str] = frozenset(),
        missing_input_ids: Sequence[str] = (),
    ) -> ResearchRequirementReadiness:
        actions = requirement.supported_actions
        if status in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.REFRESHING, ResearchRequirementStatus.NOT_APPLICABLE}:
            actions = ()
        elif status == ResearchRequirementStatus.UNSUPPORTED:
            actions = tuple(action for action in actions if action != ResearchSupportedAction.FIND_DATA)
        age = policy.age(selected, now) if selected is not None and now is not None else None
        coverage_pct, critical_coverage_pct = _requirement_coverage(
            requirement, covered_input_ids, bool(evidence)
        )
        return ResearchRequirementReadiness(
            requirement_id=requirement.requirement_id,
            rule_engine_area=requirement.rule_engine_area,
            mandatory=requirement.mandatory,
            status=status,
            source=selected.source if selected else None,
            source_tier=selected.source_tier if selected else None,
            as_of=selected.as_of if selected else None,
            retrieved_at=selected.retrieved_at if selected else None,
            age=age,
            freshness_policy=policy,
            evidence_ids=tuple(item.evidence_id for item in evidence),
            missing_reason=missing_reason,
            conflict_reason=conflict_reason,
            supported_actions=actions,
            importance=requirement.importance or ResearchRequirementImportance.IMPORTANT,
            source_url=selected.source_url if selected else None,
            covered_input_ids=tuple(sorted(covered_input_ids)),
            missing_input_ids=tuple(missing_input_ids),
            coverage_pct=coverage_pct,
            critical_coverage_pct=critical_coverage_pct,
        )

    @staticmethod
    def _required_inputs_are_fresh(
        requirement: ResearchRequirement,
        evidence: Sequence[ResearchEvidence],
        authority: ProviderAuthorityPolicy,
        policy: FreshnessPolicy,
        now: datetime,
    ) -> bool:
        mandatory = tuple(
            item.input_id
            for item in requirement.inputs
            if item.importance == ResearchRequirementImportance.MANDATORY
        )
        if not mandatory:
            selected = ResearchConflictResolver().resolve(requirement, evidence, authority).selected
            return selected is not None and policy.is_fresh(selected, now)
        all_input_ids = frozenset(item.input_id for item in requirement.inputs)
        for input_id in mandatory:
            candidates = [
                item
                for item in evidence
                if item.complete
                and input_id in (frozenset(item.covered_input_ids) or all_input_ids)
            ]
            if not candidates:
                return False
            selected = sorted(
                candidates,
                key=lambda item: (
                    authority.rank(item),
                    -(item.as_of or item.published_at or item.retrieved_at).timestamp(),
                    -item.retrieved_at.timestamp(),
                    item.evidence_id,
                ),
            )[0]
            if not policy.is_fresh(selected, now):
                return False
        return True

    def _overall_completeness(
        self, requirements: Sequence[ResearchRequirementReadiness]
    ) -> int:
        weighted = Decimal("0")
        included_weight = Decimal("0")
        for area, area_weight in self.requirement_registry.area_weights.items():
            values = [item for item in requirements if item.rule_engine_area == area]
            supported = [
                item for item in values if item.status != ResearchRequirementStatus.NOT_APPLICABLE
            ]
            if not supported:
                continue
            area_pct = Decimal(sum(item.coverage_pct for item in supported)) / Decimal(
                len(supported)
            )
            weighted += area_weight * area_pct
            included_weight += area_weight
        return 0 if included_weight == 0 else int((weighted / included_weight).quantize(Decimal("1")))

    def _critical_completeness(
        self, requirements: Sequence[ResearchRequirementReadiness]
    ) -> int:
        weighted = Decimal("0")
        included_weight = Decimal("0")
        for area, area_weight in self.requirement_registry.area_weights.items():
            values = [
                item
                for item in requirements
                if item.rule_engine_area == area and item.mandatory and item.status != ResearchRequirementStatus.NOT_APPLICABLE
            ]
            if not values:
                continue
            area_pct = Decimal(sum(item.critical_coverage_pct for item in values)) / Decimal(
                len(values)
            )
            weighted += area_weight * area_pct
            included_weight += area_weight
        return 0 if included_weight == 0 else int((weighted / included_weight).quantize(Decimal("1")))

    @staticmethod
    def _overall_status(
        requirements: Sequence[ResearchRequirementReadiness],
    ) -> ResearchRequirementStatus:
        supported = [
            item for item in requirements if item.status != ResearchRequirementStatus.NOT_APPLICABLE
        ]
        if not supported:
            return ResearchRequirementStatus.NOT_APPLICABLE
        mandatory = [item for item in supported if item.mandatory]
        if any(item.status == ResearchRequirementStatus.CONFLICTING for item in mandatory):
            return ResearchRequirementStatus.CONFLICTING
        if any(item.status == ResearchRequirementStatus.REFRESHING for item in supported):
            return ResearchRequirementStatus.REFRESHING
        if any(item.status == ResearchRequirementStatus.FAILED for item in mandatory):
            return ResearchRequirementStatus.FAILED
        if all(item.status == ResearchRequirementStatus.READY_FRESH for item in supported):
            return ResearchRequirementStatus.READY_FRESH
        return ResearchRequirementStatus.PARTIAL

    @staticmethod
    def _confidence_pct(
        requirements: Sequence[ResearchRequirementReadiness],
        overall_completeness: int,
        critical_completeness: int,
    ) -> int:
        selected = [item for item in requirements if item.source_tier is not None]
        tier_scores = {
            ResearchSourceTier.OFFICIAL: 100,
            ResearchSourceTier.REGULATORY: 100,
            ResearchSourceTier.TRUSTED_MARKET_DATA: 90,
            ResearchSourceTier.LICENSED_STRUCTURED: 85,
            ResearchSourceTier.APPROVED_SECONDARY: 70,
            ResearchSourceTier.APPROVED_EXTERNAL_TOOL: 60,
            ResearchSourceTier.USER_UPLOAD: 50,
            ResearchSourceTier.UNVERIFIED: 20,
        }
        authority = (
            sum(tier_scores[item.source_tier] for item in selected) / len(selected)
            if selected
            else 0
        )
        fresh = (
            100
            * sum(item.status == ResearchRequirementStatus.READY_FRESH for item in selected)
            / len(selected)
            if selected
            else 0
        )
        score = (
            critical_completeness * 0.45
            + overall_completeness * 0.25
            + authority * 0.20
            + fresh * 0.10
        )
        score -= 20 * sum(
            item.status == ResearchRequirementStatus.CONFLICTING for item in requirements
        )
        score -= 10 * sum(
            item.mandatory and item.status == ResearchRequirementStatus.FAILED
            for item in requirements
        )
        return max(0, min(100, round(score)))


@dataclass(frozen=True)
class ResearchRefreshTarget:
    requirement_id: str
    rule_engine_area: RuleEngineArea
    reason: ResearchRequirementStatus
    authority_policy: ProviderAuthorityPolicy
    existing_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResearchRefreshPlan:
    global_instrument_id: UUID
    targets: tuple[ResearchRefreshTarget, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_global_instrument_id(self.global_instrument_id)
        _require_aware(self.created_at, "created_at")


class ResearchRefreshPlanner:
    """Plan mandatory targeted work; planning itself never invokes a provider."""

    _TARGET_STATUSES = frozenset(
        {
            ResearchRequirementStatus.READY_STALE,
            ResearchRequirementStatus.PARTIAL,
            ResearchRequirementStatus.MISSING,
            ResearchRequirementStatus.CONFLICTING,
            ResearchRequirementStatus.FAILED,
        }
    )

    def __init__(self, authority_registry: ProviderAuthorityRegistry | None = None) -> None:
        self.authority_registry = authority_registry or ProviderAuthorityRegistry.default()

    def plan(
        self,
        readiness: ResearchReadinessResult,
        *,
        jurisdiction: str = "GLOBAL",
        requirement_ids: Sequence[str] | None = None,
        include_non_mandatory: bool = False,
    ) -> ResearchRefreshPlan:
        selected = (
            None
            if requirement_ids is None
            else frozenset(_normalized_key(value, "requirement_id") for value in requirement_ids)
        )
        targets = tuple(
            ResearchRefreshTarget(
                requirement_id=item.requirement_id,
                rule_engine_area=item.rule_engine_area,
                reason=item.status,
                authority_policy=self.authority_registry.policy_for(item.requirement_id, jurisdiction),
                existing_evidence_ids=item.evidence_ids,
            )
            for item in readiness.requirements
            if item.status in self._TARGET_STATUSES
            and (selected is None or item.requirement_id in selected)
            and (item.mandatory or include_non_mandatory or selected is not None)
        )
        return ResearchRefreshPlan(readiness.global_instrument_id, targets, readiness.generated_at)


class ExternalResearchToolGateway(Protocol):
    """Provider-neutral MCP seam; fallback authorization is decided upstream."""

    async def acquire_requirement(
        self,
        profile: Any,
        *,
        region: str,
        requirement_id: str,
        authorization: ExternalResearchToolAuthorization,
        request_id: str,
    ) -> Any:
        """Invoke one policy-authorized configured capability for a verified mapping."""
        ...

    async def find_evidence(
        self,
        global_instrument_id: UUID,
        requirement: ResearchRequirement,
        authority_policy: ProviderAuthorityPolicy,
        *,
        authorization: ExternalResearchToolAuthorization,
    ) -> Sequence[ResearchEvidence]:
        ...


def _normalized_key(value: str, field_name: str) -> str:
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _requirement_coverage(
    requirement: ResearchRequirement,
    covered_input_ids: frozenset[str],
    has_evidence: bool,
) -> tuple[int, int]:
    if not requirement.inputs:
        value = 100 if has_evidence else 0
        return value, value
    weights = {
        ResearchRequirementImportance.MANDATORY: 3,
        ResearchRequirementImportance.IMPORTANT: 2,
        ResearchRequirementImportance.SUPPORTING: 1,
    }
    denominator = sum(weights[item.importance] for item in requirement.inputs)
    numerator = sum(
        weights[item.importance]
        for item in requirement.inputs
        if item.input_id in covered_input_ids
    )
    mandatory = [
        item for item in requirement.inputs
        if item.importance == ResearchRequirementImportance.MANDATORY
    ]
    mandatory_covered = sum(item.input_id in covered_input_ids for item in mandatory)
    overall = round(100 * numerator / denominator) if denominator else 0
    critical = round(100 * mandatory_covered / len(mandatory)) if mandatory else overall
    return overall, critical


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_global_instrument_id(value: UUID) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError("canonical globalInstrumentId is required")
    return value
