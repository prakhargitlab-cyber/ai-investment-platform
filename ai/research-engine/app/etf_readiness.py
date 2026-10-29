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
from app.etf_metrics import EtfMetricId, compute_etf_deterministic_metrics

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


def _check_identity_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """Require an owned, canonical listing and age its durable observation."""
    # A positively observed metadata conflict invalidates even fresh cached
    # quotes. Only a strictly newer successful verification clears the conflict;
    # timestamps tied by a coarse clock fail closed, independent of UUID ordering.
    checks = [a for a in store.etf_attempts(instrument_id) if a.metric == "IDENTITY" and a.provider == "NSE"]
    conflicts = [a.attempted_at for a in checks if a.reason == "NSE_METADATA_IDENTITY_CONFLICT"]
    verified = [a.attempted_at for a in checks if a.reason == "NSE_METADATA_IDENTITY_VERIFIED"
                and a.outcome == EtfAcquisitionOutcome.SUCCESS_WITH_DATA]
    if conflicts and (not verified or max(conflicts) >= max(verified)):
        return EtfRequirementStatus(requirement="IDENTITY", role="MANDATORY", applicability="APPLICABLE",
            status=EtfReadinessStatus.TECHNICAL_FAILURE, reason="NSE_METADATA_IDENTITY_CONFLICT")
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
                              now=now or datetime.now(timezone.utc))
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


def _missing_market_evidence(store, instrument_id, metric):
    attempts = [a for a in store.etf_attempts(instrument_id) if a.metric == metric]
    latest = max((a.attempted_at for a in attempts), default=None)
    failures = [a for a in attempts if a.attempted_at == latest
                and a.outcome == EtfAcquisitionOutcome.TECHNICAL_FAILURE]
    return EtfRequirementStatus(requirement=metric, role="MANDATORY", applicability="APPLICABLE",
        status=EtfReadinessStatus.TECHNICAL_FAILURE if failures else EtfReadinessStatus.MISSING,
        reason=failures[0].reason if failures else None)


def _check_market_price_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """Check if MARKET_PRICE is ready."""
    facts = store.etf_facts(instrument_id, EtfMetric.MARKET_PRICE)
    if not facts:
        return _missing_market_evidence(store, instrument_id, "MARKET_PRICE")

    fact = facts[0]
    now = now or datetime.now(timezone.utc)
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


def _check_trading_volume_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """Check if TRADING_VOLUME (LIQUIDITY) is ready."""
    facts = store.etf_facts(instrument_id, EtfMetric.TRADING_VOLUME)
    if not facts:
        return _missing_market_evidence(store, instrument_id, "TRADING_VOLUME")

    fact = facts[0]
    now = now or datetime.now(timezone.utc)
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


def _check_supporting_ready(store: SqliteResearchPersistence, instrument_id, metric: EtfMetric, *, now: datetime | None = None) -> EtfRequirementStatus:
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
    now = now or datetime.now(timezone.utc)
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


def _check_nav_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """NAV is persisted via the dedicated EtfNavObservation evidence model
    (store.etf_navs), not the generic EtfFact model (store.etf_facts) that
    _check_supporting_ready reads for every other ETC3_SUPPORTING metric --
    route NAV separately so acquired NAV observations are actually seen."""
    navs = store.etf_navs(instrument_id)

    if not navs:
        return EtfRequirementStatus(
            requirement="NAV",
            role="SUPPORTING",
            applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
        )

    nav = navs[0]
    now = now or datetime.now(timezone.utc)
    freshness = etf_freshness(nav, EtfMetric.NAV, now=now)

    return EtfRequirementStatus(
        requirement="NAV",
        role="SUPPORTING",
        applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE"
               else EtfReadinessStatus.MISSING,
        evidence_id=nav.provenance.provider if nav.provenance else None,
        provider=nav.provenance.provider if nav.provenance else None,
        as_of_date=nav.nav_date,
        freshness=freshness,
    )


# Derived price-history metrics (HISTORICAL_RETURNS/VOLATILITY/DRAWDOWN/
# INDEX_MOMENTUM) have no EtfMetric/EtfFact evidence model of their own --
# they are computed deterministically from DailyMarketBar, never acquired
# or manually uploaded. "Fresh" here means the underlying bar series itself
# was updated recently, not a fixed per-metric provider contract.
_DERIVED_METRIC_MAX_AGE_DAYS = 5

# Requirements named in ETC3_SUPPORTING for which no trusted data source is
# defined anywhere in the repository yet. These are reported as
# NOT_IMPLEMENTED (visibly, in the returned status list) rather than
# fabricated or silently dropped.
_NOT_IMPLEMENTED_SUPPORTING = frozenset({"INDEX_VALUATION", "INDEX_CONSTITUENTS"})


def _derived_metric_status(requirement: str, metric_value, *, now: datetime) -> EtfRequirementStatus:
    if metric_value is None or metric_value.insufficient_history or metric_value.value is None:
        return EtfRequirementStatus(
            requirement=requirement, role="SUPPORTING", applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
            reason=(metric_value.reason if metric_value is not None else None) or "INSUFFICIENT_PRICE_HISTORY",
        )
    age_days = (now.date() - metric_value.as_of_date).days
    if age_days < 0:
        status, freshness = EtfReadinessStatus.TECHNICAL_FAILURE, "FUTURE_DATED"
    elif age_days <= _DERIVED_METRIC_MAX_AGE_DAYS:
        status, freshness = EtfReadinessStatus.READY_FRESH, "FRESH"
    else:
        status, freshness = EtfReadinessStatus.READY_STALE, "STALE"
    return EtfRequirementStatus(
        requirement=requirement, role="SUPPORTING", applicability="APPLICABLE", status=status,
        evidence_id="DAILY_MARKET_BARS", provider="DAILY_MARKET_BARS",
        as_of_date=metric_value.as_of_date, freshness=freshness,
    )


def _etf_deterministic_metrics(store: SqliteResearchPersistence, instrument_id):
    bars = [bar for bar in store.load_daily_market_bars({instrument_id}) if bar.global_instrument_id == instrument_id]
    return compute_etf_deterministic_metrics(instrument_id, bars)


def _check_historical_returns_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """HISTORICAL_RETURNS is ready once at least the shortest (1M) period return is computable."""
    metrics = _etf_deterministic_metrics(store, instrument_id)
    return _derived_metric_status("HISTORICAL_RETURNS", metrics.get(EtfMetricId.RETURN_1M), now=now or datetime.now(timezone.utc))


def _check_volatility_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    metrics = _etf_deterministic_metrics(store, instrument_id)
    return _derived_metric_status("VOLATILITY", metrics.get(EtfMetricId.ANNUALIZED_VOLATILITY), now=now or datetime.now(timezone.utc))


def _check_drawdown_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    metrics = _etf_deterministic_metrics(store, instrument_id)
    return _derived_metric_status("DRAWDOWN", metrics.get(EtfMetricId.MAX_DRAWDOWN), now=now or datetime.now(timezone.utc))


def _check_momentum_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    metrics = _etf_deterministic_metrics(store, instrument_id)
    return _derived_metric_status("INDEX_MOMENTUM", metrics.get(EtfMetricId.MOMENTUM_50_200), now=now or datetime.now(timezone.utc))


def _check_concentration_ready(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> EtfRequirementStatus:
    """CONCENTRATION is derived from the same UNDERLYING_HOLDINGS snapshot and
    freshness policy already used elsewhere -- not a separate evidence model."""
    snapshots = store.etf_holdings(instrument_id)
    if not snapshots:
        return EtfRequirementStatus(requirement="CONCENTRATION", role="SUPPORTING", applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING)
    snapshot = snapshots[0]
    if not any(holding.weight_percentage is not None for holding in snapshot.holdings):
        return EtfRequirementStatus(requirement="CONCENTRATION", role="SUPPORTING", applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING, reason="HOLDINGS_LACK_WEIGHTS")
    freshness = etf_freshness(snapshot, EtfMetric.UNDERLYING_HOLDINGS, now=now or datetime.now(timezone.utc))
    return EtfRequirementStatus(
        requirement="CONCENTRATION", role="SUPPORTING", applicability="APPLICABLE",
        status=EtfReadinessStatus.READY_FRESH if freshness == "FRESH"
               else EtfReadinessStatus.READY_STALE if freshness == "STALE" else EtfReadinessStatus.MISSING,
        evidence_id=snapshot.provenance.provider, provider=snapshot.provenance.provider,
        as_of_date=snapshot.as_of_date, freshness=freshness,
    )


_DERIVED_METRIC_CHECKS = {
    "HISTORICAL_RETURNS": _check_historical_returns_ready,
    "VOLATILITY": _check_volatility_ready,
    "DRAWDOWN": _check_drawdown_ready,
    "INDEX_MOMENTUM": _check_momentum_ready,
    "CONCENTRATION": _check_concentration_ready,
}


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


def evaluate_etf_readiness(store: SqliteResearchPersistence, instrument_id, *, now: datetime | None = None) -> tuple[list[EtfRequirementStatus], EtfReadinessSummary]:
    """Evaluate ETF readiness for a single instrument.

    Returns a list of requirement statuses and a summary.

    Per ETF-3 contract:
    - MANDATORY: IDENTITY, MARKET_PRICE, TRADING_VOLUME must be READY_FRESH
    - SUPPORTING: NAV, AUM, TER, etc. - missing/stale is non-blocking
    - CONTEXTUAL: CURRENT_NEWS - technical failure is non-blocking
    """
    statuses = []

    # Check mandatory requirements
    identity_status = _check_identity_ready(store, instrument_id, now=now)
    statuses.append(identity_status)

    market_price_status = _check_market_price_ready(store, instrument_id, now=now)
    statuses.append(market_price_status)

    volume_status = _check_trading_volume_ready(store, instrument_id, now=now)
    statuses.append(volume_status)

    # Check supporting requirements. Every name in ETC3_SUPPORTING must be
    # handled by exactly one of: a direct EtfMetric/EtfFact check, a derived
    # price-history check, or an explicit NOT_IMPLEMENTED declaration -- a
    # requirement that silently matches none of these previously vanished
    # from the returned status list instead of being reported as missing.
    for metric in ETC3_SUPPORTING:
        if metric == "NAV":
            # NAV uses a dedicated evidence model (EtfNavObservation /
            # store.etf_navs), not the generic EtfFact model that the
            # EtfMetric(metric)/_check_supporting_ready branch below reads --
            # route it separately rather than falling through that branch.
            statuses.append(_check_nav_ready(store, instrument_id, now=now))
            continue
        try:
            m = EtfMetric(metric)
            statuses.append(_check_supporting_ready(store, instrument_id, m, now=now))
            continue
        except ValueError:
            pass
        if metric in _DERIVED_METRIC_CHECKS:
            statuses.append(_DERIVED_METRIC_CHECKS[metric](store, instrument_id, now=now))
        elif metric in _NOT_IMPLEMENTED_SUPPORTING:
            statuses.append(EtfRequirementStatus(requirement=metric, role="SUPPORTING", applicability="APPLICABLE",
                status=EtfReadinessStatus.NOT_IMPLEMENTED, reason="NO_TRUSTED_DATA_SOURCE_DEFINED"))
        else:
            raise AssertionError(f"ETC3_SUPPORTING requirement {metric!r} has no readiness check wired up")

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
