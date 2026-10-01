"""ETF-specific readiness evaluation; no stock readiness semantics.

ETF readiness is distinct from stock readiness due to:
- Different identity requirements (ETF vs equity security)
- Different mandatory requirements (LIQUIDITY via volume, not shareholding)
- Different evidence models (EtfFact, EtfNavObservation, EtfListing)
- Different freshness policies (NAV 1 day, AUM 35 days, etc.)

This module consumes existing ETF-2 acquisition/persistence interfaces and
returns deterministic readiness states without implementing new providers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from types import SimpleNamespace

from pydantic import ValidationError

from app.etf_evidence import EtfMetric, etf_freshness, ETF_MAX_AGE, EtfAcquisitionOutcome, EtfListing
from app.etf_domain import EtfSubtype

if TYPE_CHECKING:
    from app.models import EtfResearchProfile
    from app.persistence import SqliteResearchPersistence


class EtfReadinessStatus(StrEnum):
    """ETF requirement status states."""
    READY_FRESH = "READY_FRESH"
    READY_STALE = "READY_STALE"
    MISSING = "MISSING"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    GENUINELY_UNAVAILABLE = "GENUINELY_UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNKNOWN_AS_OF = "UNKNOWN_AS_OF"


@dataclass(frozen=True)
class EtfRequirementStatus:
    """Status of a single ETF readiness requirement."""
    requirement: str
    role: str  # MANDATORY, SUPPORTING, CONTEXTUAL
    applicability: str  # APPLICABLE, NOT_APPLICABLE
    status: EtfReadinessStatus
    evidence_id: str | None = None
    provider: str | None = None
    as_of_date: datetime.date | None = None
    freshness: str = "UNKNOWN"
    reason: str | None = None

    @property
    def is_ready(self) -> bool:
        """Requirement is satisfied for analysis purposes."""
        return self.status in {EtfReadinessStatus.READY_FRESH, EtfReadinessStatus.READY_STALE,
                               EtfReadinessStatus.UNKNOWN_AS_OF}


@dataclass(frozen=True)
class EtfReadinessSummary:
    """Aggregate ETF readiness counts."""
    mandatory_total: int
    mandatory_ready: int
    mandatory_missing: int
    mandatory_stale: int
    mandatory_technical_failure: int

    supporting_total: int
    supporting_ready: int
    supporting_missing: int
    supporting_stale: int
    supporting_technical_failure: int

    contextual_total: int
    contextual_ready: int
    contextual_failure: int

    ready_for_analysis: bool

    @property
    def is_ready(self) -> bool:
        return self.ready_for_analysis


# Locked ETF-3 requirement contract
ETC3_MANDATORY = frozenset({"IDENTITY", "MARKET_PRICE", "TRADING_VOLUME"})
ETC3_SUPPORTING = frozenset({
    "NAV", "AUM", "EXPENSE_RATIO", "BENCHMARK_INDEX", "AMC_FUND_HOUSE",
    "BID_ASK_SPREAD", "TRACKING_ERROR", "TRACKING_DIFFERENCE",
    "FUND_INCEPTION_DATE", "REPLICATION_METHOD", "UNDERLYING_HOLDINGS",
    "CONCENTRATION", "INDEX_CONSTITUENTS", "HISTORICAL_RETURNS",
    "VOLATILITY", "DRAWDOWN", "INDEX_VALUATION", "INDEX_MOMENTUM",
})
ETC3_CONTEXTUAL = frozenset({"CURRENT_NEWS"})


def _etf_requirement_role(requirement: str) -> str:
    """Return the role for an ETF requirement."""
    if requirement in ETC3_MANDATORY:
        return "MANDATORY"
    if requirement in ETC3_SUPPORTING:
        return "SUPPORTING"
    if requirement in ETC3_CONTEXTUAL:
        return "CONTEXTUAL"
    return "SUPPORTING"


def _check_identity_ready(store: SqliteResearchPersistence, instrument_id) -> EtfRequirementStatus:
    """Require an owned, canonical listing and age its durable observation."""
    listings = [listing for listing in store.etf_listings()
                if listing.instrument_id == instrument_id and listing.instrument_id is not None]
    if not listings:
        from app.etf_evidence import EtfAcquisitionOutcome as Outcome
        attempts = store.etf_attempts(instrument_id)
        for attempt in attempts:
            if attempt.metric == "IDENTITY" and attempt.outcome == Outcome.TECHNICAL_FAILURE:
                return EtfRequirementStatus(
                    requirement="IDENTITY",
                    role="MANDATORY",
                    applicability="APPLICABLE",
                    status=EtfReadinessStatus.TECHNICAL_FAILURE,
                    reason=attempt.reason,
                )
        return EtfRequirementStatus(
            requirement="IDENTITY",
            role="MANDATORY",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
            reason="NO_INSTRUMENT_SCOPED_LISTING",
        )

    try:
        # Revalidate durable/test representations, including model_copy inputs.
        listings = [EtfListing.model_validate(item.model_dump()) for item in listings]
        if any(item.symbol != item.symbol.strip() or item.provenance.provider != "NSE"
               or item.provenance.authority != "OFFICIAL_EXCHANGE"
               or item.provenance.source_mode != "REAL" for item in listings):
            raise ValueError("Noncanonical or nonauthoritative ETF listing")
        if len({(item.isin, item.symbol) for item in listings}) != 1:
            raise ValueError("Conflicting canonical listing identities")
    except (ValidationError, ValueError):
        return EtfRequirementStatus(
            requirement="IDENTITY",
            role="MANDATORY",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.TECHNICAL_FAILURE,
            reason="ETF_LISTING_IDENTITY_CONFLICT",
        )

    listing = max(listings, key=lambda item: (item.provenance.published_at or item.provenance.retrieved_at,
                                             item.provenance.retrieved_at))
    # Listings have no effective-date field. Age the publication when supplied,
    # otherwise the persisted successful observation, never an attempt time.
    # The observation date is not reported as a fabricated source/as-of date.
    observed_at = listing.provenance.published_at or listing.provenance.retrieved_at
    freshness = etf_freshness(SimpleNamespace(as_of_date=observed_at.date()), EtfMetric.IDENTITY,
                              now=datetime.now(timezone.utc))
    return EtfRequirementStatus(
        requirement="IDENTITY",
        role="MANDATORY",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE"
               else EtfReadinessStatus.MISSING,
        evidence_id=listing.provenance.provider if listing.provenance else None,
        provider=listing.provenance.provider if listing.provenance else None,
        as_of_date=None,
        freshness=freshness,
        reason="LISTING_PUBLICATION_DATE" if listing.provenance.published_at else "LISTING_OBSERVATION_DATE",
    )


def _check_market_price_ready(store: SqliteResearchPersistence, instrument_id) -> EtfRequirementStatus:
    """Check if MARKET_PRICE is ready."""
    facts = store.etf_facts(instrument_id, EtfMetric.MARKET_PRICE)
    if not facts:
        return EtfRequirementStatus(
            requirement="MARKET_PRICE",
            role="MANDATORY",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
        )

    fact = facts[0]
    now = datetime.now(timezone.utc)
    freshness = etf_freshness(fact, EtfMetric.MARKET_PRICE, now=now)

    return EtfRequirementStatus(
        requirement="MARKET_PRICE",
        role="MANDATORY",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE"
               else EtfReadinessStatus.MISSING,
        evidence_id=fact.provenance.provider if fact.provenance else None,
        provider=fact.provenance.provider if fact.provenance else None,
        as_of_date=fact.as_of_date,
        freshness=freshness,
    )


def _check_trading_volume_ready(store: SqliteResearchPersistence, instrument_id) -> EtfRequirementStatus:
    """Check if TRADING_VOLUME (LIQUIDITY) is ready."""
    facts = store.etf_facts(instrument_id, EtfMetric.TRADING_VOLUME)
    if not facts:
        return EtfRequirementStatus(
            requirement="TRADING_VOLUME",
            role="MANDATORY",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
        )

    fact = facts[0]
    now = datetime.now(timezone.utc)
    freshness = etf_freshness(fact, EtfMetric.TRADING_VOLUME, now=now)

    # Zero volume is valid evidence (not missing)
    return EtfRequirementStatus(
        requirement="TRADING_VOLUME",
        role="MANDATORY",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE"
               else EtfReadinessStatus.MISSING,
        evidence_id=fact.provenance.provider if fact.provenance else None,
        provider=fact.provenance.provider if fact.provenance else None,
        as_of_date=fact.as_of_date,
        freshness=freshness,
    )


def _check_supporting_ready(store: SqliteResearchPersistence, instrument_id, metric: EtfMetric) -> EtfRequirementStatus:
    """Check if a supporting metric is ready."""
    facts = store.etf_facts(instrument_id, metric)

    if not facts:
        return EtfRequirementStatus(
            requirement=metric.value,
            role="SUPPORTING",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
        )

    fact = facts[0]
    now = datetime.now(timezone.utc)
    freshness = etf_freshness(fact, metric, now=now)

    return EtfRequirementStatus(
        requirement=metric.value,
        role="SUPPORTING",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE"
               else EtfReadinessStatus.MISSING,
        evidence_id=fact.provenance.provider if fact.provenance else None,
        provider=fact.provenance.provider if fact.provenance else None,
        as_of_date=fact.as_of_date,
        freshness=freshness,
    )


def _check_contextual_ready(store: SqliteResearchPersistence, instrument_id) -> EtfRequirementStatus:
    """Check if CURRENT_NEWS is ready."""
    # CURRENT_NEWS for ETFs follows stock readiness behavior
    # Technical failure is non-blocking but tracked
    attempts = store.etf_attempts(instrument_id)
    for attempt in attempts:
        if attempt.metric == "CURRENT_NEWS" and attempt.outcome == EtfAcquisitionOutcome.TECHNICAL_FAILURE:
            return EtfRequirementStatus(
                requirement="CURRENT_NEWS",
                role="CONTEXTUAL",
                applicability="APPLICABLE",
                status=EtfReadinessStatus.TECHNICAL_FAILURE,
                reason=attempt.reason,
            )

    # If no evidence and not a failure, treat as MISSING
    return EtfRequirementStatus(
        requirement="CURRENT_NEWS",
        role="CONTEXTUAL",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.MISSING,
    )


def evaluate_etf_readiness(store: SqliteResearchPersistence, instrument_id) -> tuple[list[EtfRequirementStatus], EtfReadinessSummary]:
    """Evaluate ETF readiness for a single instrument.

    Returns a list of requirement statuses and a summary.

    Per ETF-3 contract:
    - MANDATORY: IDENTITY, MARKET_PRICE, TRADING_VOLUME must be READY_FRESH
    - SUPPORTING: NAV, AUM, TER, etc. - missing/stale is non-blocking
    - CONTEXTUAL: CURRENT_NEWS - technical failure is non-blocking
    """
    statuses = []

    # Check mandatory requirements
    identity_status = _check_identity_ready(store, instrument_id)
    statuses.append(identity_status)

    market_price_status = _check_market_price_ready(store, instrument_id)
    statuses.append(market_price_status)

    volume_status = _check_trading_volume_ready(store, instrument_id)
    statuses.append(volume_status)

    # Check supporting requirements
    for metric in ETC3_SUPPORTING:
        try:
            m = EtfMetric(metric)
            status = _check_supporting_ready(store, instrument_id, m)
            statuses.append(status)
        except ValueError:
            pass

    # Check contextual requirements
    contextual_status = _check_contextual_ready(store, instrument_id)
    statuses.append(contextual_status)

    # Calculate summary
    # For mandatory requirements, only READY_FRESH counts as ready
    # STALE or any other status blocks readiness
    mandatory_count = sum(1 for s in statuses if s.role == "MANDATORY")
    mandatory_ready = sum(1 for s in statuses if s.role == "MANDATORY" and s.status == EtfReadinessStatus.READY_FRESH)
    mandatory_missing = sum(1 for s in statuses if s.role == "MANDATORY" and s.status == EtfReadinessStatus.MISSING)
    mandatory_stale = sum(1 for s in statuses if s.role == "MANDATORY" and s.status == EtfReadinessStatus.READY_STALE)
    mandatory_technical = sum(1 for s in statuses if s.role == "MANDATORY" and s.status == EtfReadinessStatus.TECHNICAL_FAILURE)

    supporting_count = sum(1 for s in statuses if s.role == "SUPPORTING")
    supporting_ready = sum(1 for s in statuses if s.role == "SUPPORTING" and s.is_ready)
    supporting_missing = sum(1 for s in statuses if s.role == "SUPPORTING" and s.status == EtfReadinessStatus.MISSING)
    supporting_stale = sum(1 for s in statuses if s.role == "SUPPORTING" and s.status == EtfReadinessStatus.READY_STALE)
    supporting_technical = sum(1 for s in statuses if s.role == "SUPPORTING" and s.status == EtfReadinessStatus.TECHNICAL_FAILURE)

    contextual_count = sum(1 for s in statuses if s.role == "CONTEXTUAL")
    contextual_ready = sum(1 for s in statuses if s.role == "CONTEXTUAL" and s.is_ready)
    contextual_failure = sum(1 for s in statuses if s.role == "CONTEXTUAL" and s.status == EtfReadinessStatus.TECHNICAL_FAILURE)

    # READY_FRESH for all mandatory = ready for analysis
    ready_for_analysis = (mandatory_ready == mandatory_count and mandatory_technical == 0)

    summary = EtfReadinessSummary(
        mandatory_total=mandatory_count,
        mandatory_ready=mandatory_ready,
        mandatory_missing=mandatory_missing,
        mandatory_stale=mandatory_stale,
        mandatory_technical_failure=mandatory_technical,
        supporting_total=supporting_count,
        supporting_ready=supporting_ready,
        supporting_missing=supporting_missing,
        supporting_stale=supporting_stale,
        supporting_technical_failure=supporting_technical,
        contextual_total=contextual_count,
        contextual_ready=contextual_ready,
        contextual_failure=contextual_failure,
        ready_for_analysis=ready_for_analysis,
    )

    return statuses, summary


def is_etf_ready_for_analysis(store: SqliteResearchPersistence, instrument_id) -> bool:
    """Simple check - is ETF ready for analysis?

    Requires all mandatory requirements to be ready.
    """
    _, summary = evaluate_etf_readiness(store, instrument_id)
    return summary.ready_for_analysis
