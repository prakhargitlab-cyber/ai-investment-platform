"""Durable ETF evidence contracts. No stock facts or readiness semantics."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from app.models import EtfHolding, EtfSubtype, ResearchBaseModel, ReliabilityLevel, SourceMode


class EtfMetric(StrEnum):
    IDENTITY = "IDENTITY"
    SUBTYPE = "SUBTYPE"
    ASSET_CLASS = "ASSET_CLASS"
    UNDERLYING_REFERENCE = "UNDERLYING_REFERENCE"
    BENCHMARK_INDEX = "BENCHMARK_INDEX"
    NAV = "NAV"
    AUM = "AUM"
    EXPENSE_RATIO = "EXPENSE_RATIO"
    MARKET_PRICE = "MARKET_PRICE"
    TRADING_VOLUME = "TRADING_VOLUME"
    BID = "BID"
    ASK = "ASK"
    BID_ASK_SPREAD = "BID_ASK_SPREAD"
    TRACKING_ERROR = "TRACKING_ERROR"
    TRACKING_DIFFERENCE = "TRACKING_DIFFERENCE"
    AMC_FUND_HOUSE = "AMC_FUND_HOUSE"
    FUND_INCEPTION_DATE = "FUND_INCEPTION_DATE"
    REPLICATION_METHOD = "REPLICATION_METHOD"
    UNDERLYING_HOLDINGS = "UNDERLYING_HOLDINGS"


class EtfAuthority(StrEnum):
    OFFICIAL_EXCHANGE = "OFFICIAL_EXCHANGE"
    OFFICIAL_FUND = "OFFICIAL_FUND"
    OTHER_AUTHORITATIVE = "OTHER_AUTHORITATIVE"
    SECONDARY = "SECONDARY"


class EtfAcquisitionOutcome(StrEnum):
    SUCCESS_WITH_DATA = "SUCCESS_WITH_DATA"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    GENUINELY_UNAVAILABLE = "GENUINELY_UNAVAILABLE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    # Unimplemented coverage is not evidence that a value does not exist.
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class EtfProvenance(ResearchBaseModel):
    provider: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_identity: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_locator: str | None = None
    authority: EtfAuthority
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    confidence: float | None = Field(default=None, ge=0, le=1)
    reliability_level: ReliabilityLevel | None = None
    source_mode: SourceMode = SourceMode.REAL


class EtfFact(ResearchBaseModel):
    instrument_id: UUID
    metric: EtfMetric
    value: Decimal | str
    unit: str | None = None
    as_of_date: date | None = None
    provenance: EtfProvenance

    @model_validator(mode="after")
    def validate_value(self):
        if self.metric in {EtfMetric.NAV, EtfMetric.UNDERLYING_HOLDINGS, EtfMetric.IDENTITY}:
            raise ValueError("Use the dedicated NAV, holdings or listing model")
        numeric = {EtfMetric.AUM, EtfMetric.EXPENSE_RATIO, EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME,
                   EtfMetric.BID, EtfMetric.ASK, EtfMetric.BID_ASK_SPREAD, EtfMetric.TRACKING_ERROR,
                   EtfMetric.TRACKING_DIFFERENCE}
        if self.metric in numeric:
            number = Decimal(self.value)
            if not number.is_finite() or (number < 0 and self.metric != EtfMetric.TRACKING_DIFFERENCE):
                raise ValueError("Invalid ETF numeric value")
            self.value = number
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Explicit text value required")
        if self.metric == EtfMetric.SUBTYPE:
            EtfSubtype(self.value)
        if self.metric == EtfMetric.FUND_INCEPTION_DATE:
            date.fromisoformat(self.value)
        return self


class EtfNavObservation(ResearchBaseModel):
    instrument_id: UUID
    nav: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    nav_date: date
    provenance: EtfProvenance


class EtfHoldingsSnapshot(ResearchBaseModel):
    instrument_id: UUID
    as_of_date: date
    provenance: EtfProvenance
    holdings: list[EtfHolding]

    @model_validator(mode="after")
    def validate_positions(self):
        for holding in self.holdings:
            if (holding.etf_instrument_id != self.instrument_id or holding.as_of_date != self.as_of_date
                    or holding.source_provider != self.provenance.provider):
                raise ValueError("Holding identity/date/provider must match its source snapshot")
        return self


class EtfListing(ResearchBaseModel):
    """Official listing identity; master UUID is absent until exact resolution."""
    exchange: str = "NSE"
    asset_type: str = "ETF"
    isin: str = Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    underlying_reference: str | None = None
    instrument_id: UUID | None = None
    provenance: EtfProvenance

    @model_validator(mode="after")
    def etf_only(self):
        if self.asset_type != "ETF" or self.exchange != "NSE":
            raise ValueError("NSE ETF identity only")
        return self


class EtfAcquisitionAttempt(ResearchBaseModel):
    attempt_id: UUID = Field(default_factory=uuid4)
    instrument_id: UUID | None = None
    metric: str
    provider: str
    attempted_at: AwareDatetime
    outcome: EtfAcquisitionOutcome
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    unavailability_evidence: EtfProvenance | None = None

    @model_validator(mode="after")
    def evidence_consistency(self):
        if (self.outcome == EtfAcquisitionOutcome.SUCCESS_WITH_DATA) != bool(self.evidence_ids):
            raise ValueError("Only SUCCESS_WITH_DATA has evidence ids")
        if self.outcome == EtfAcquisitionOutcome.GENUINELY_UNAVAILABLE:
            if self.unavailability_evidence is None or self.unavailability_evidence.authority == EtfAuthority.SECONDARY:
                raise ValueError("Genuine unavailability requires an explicit authoritative declaration")
        return self


def evidence_id(evidence) -> str:
    """Content revision identity, independent of repeat retrieval timestamps.

    Provider, source identity, period, exact values and provenance remain in the
    hash. Corrections create new revisions; repeats cannot rewrite originals.
    """
    payload = evidence.model_dump(mode="json")
    payload["provenance"].pop("retrieved_at")
    if isinstance(evidence, EtfFact) and isinstance(evidence.value, Decimal):
        payload["value"] = str(evidence.value.normalize())
    if isinstance(evidence, EtfNavObservation):
        payload["nav"] = str(evidence.nav.normalize())
    if "holdings" in payload:
        for holding, row in zip(evidence.holdings, payload["holdings"]):
            for field in ("weight_percentage", "quantity", "value"):
                if getattr(holding, field) is not None:
                    row[field] = str(getattr(holding, field).normalize())
        payload["holdings"].sort(key=lambda row: json.dumps(row, sort_keys=True))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_precedence(evidence):
    # Exchange and fund are equally primary. Never let secondary recency win.
    tier = {EtfAuthority.OFFICIAL_EXCHANGE: 3, EtfAuthority.OFFICIAL_FUND: 3,
            EtfAuthority.OTHER_AUTHORITATIVE: 2, EtfAuthority.SECONDARY: 1}[evidence.provenance.authority]
    as_of = getattr(evidence, "nav_date", None) or getattr(evidence, "as_of_date", None) or date.min
    return (tier, as_of, evidence.provenance.published_at or datetime.min.replace(tzinfo=timezone.utc),
            evidence.provenance.retrieved_at, evidence_id(evidence))


# ETF-only calendar-day maximum ages, not trading-session or stock policy.
# Stale authoritative facts remain selected; callers may show secondary data
# separately, but may not silently promote it to authoritative evidence.
ETF_MAX_AGE = {
    EtfMetric.NAV: timedelta(days=1), EtfMetric.MARKET_PRICE: timedelta(days=1),
    EtfMetric.TRADING_VOLUME: timedelta(days=1), EtfMetric.BID: timedelta(days=1),
    EtfMetric.ASK: timedelta(days=1), EtfMetric.BID_ASK_SPREAD: timedelta(days=1),
    EtfMetric.AUM: timedelta(days=35), EtfMetric.UNDERLYING_HOLDINGS: timedelta(days=35),
    EtfMetric.EXPENSE_RATIO: timedelta(days=90), EtfMetric.TRACKING_ERROR: timedelta(days=35),
    EtfMetric.TRACKING_DIFFERENCE: timedelta(days=35),
}


def etf_freshness(evidence, metric: EtfMetric, *, now: datetime) -> str:
    if evidence is None:
        return "MISSING"
    as_of = getattr(evidence, "nav_date", None) or getattr(evidence, "as_of_date", None)
    # Missing effective dates are not replaced by a successful fetch timestamp.
    if as_of is None:
        return "UNKNOWN_AS_OF"
    age = now.date() - as_of
    if age < timedelta(0):
        return "FUTURE_DATED"
    return "FRESH" if age <= ETF_MAX_AGE.get(metric, timedelta(days=365)) else "STALE"
