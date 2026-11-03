"""Fail-closed preflight for a future Nippon scheme-to-canonical-ETF identity bridge.

A printed AMC scheme heading is not an ISIN. The bridge requires BOTH a
separately reviewed, instrument-scoped AMC heading->ISIN attestation and a
fresh persisted NSE official listing (verified by the existing readiness
gate) with the same ISIN/ticker. This module does not alter facts, source
approvals, the existing strict document parser, or recommendation scoring.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.etf_evidence import EtfAuthority
from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics
from app.etf_readiness import EtfReadinessStatus, _check_identity_ready

_ALLOWED = frozenset({"NIFTYBEES", "JUNIORBEES"})


@dataclass(frozen=True)
class SchemeIdentityAttestation:
    ticker: str
    isin: str
    scheme_heading: str
    nse_instrument_id: UUID
    review_reference: str
    reviewed_source_url: str


@dataclass(frozen=True)
class SchemeIdentityPreflight:
    status: str  # "READY_FOR_MANUAL_REVIEW" (not verified ingestible facts)
    ticker: str
    isin: str
    instrument_id: UUID
    review_reference: str
    nse_listing_source_url: str
    identity_verified_for_scoring: bool = False


def preflight_scheme_identity(
    store, metrics: ReviewOnlyEtfMetrics, attestation: SchemeIdentityAttestation,
    *, now: datetime | None = None,
) -> SchemeIdentityPreflight:
    """Require independent exchange listing plus explicit human-supplied binding.

    Failures are errors; passing preflight is NEVER authorization to persist
    review-only document values. The PDF itself lacks a labelled ISIN.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("SCHEME_IDENTITY_CLOCK_INVALID")
    if (metrics.status != "DOCUMENT_REVIEW_ONLY" or metrics.identity_verified
            or metrics.ticker not in _ALLOWED
            or metrics.ticker != attestation.ticker
            or metrics.scheme_heading != HEADINGS[metrics.ticker]
            or attestation.scheme_heading != metrics.scheme_heading
            or not isinstance(attestation.nse_instrument_id, UUID)
            or not attestation.review_reference.strip()
            or not attestation.reviewed_source_url.startswith("https://")):
        raise ValueError("SCHEME_TO_ISIN_REVIEW_NOT_ESTABLISHED")
    from app.etf_document_parser import document_host, NSE_HOSTS
    if document_host(attestation.reviewed_source_url) not in NSE_HOSTS:
        raise ValueError("SCHEME_IDENTITY_REVIEW_SOURCE_NOT_EXCHANGE")
    readiness = _check_identity_ready(store, attestation.nse_instrument_id, now=now)
    if readiness.status is not EtfReadinessStatus.READY_FRESH:
        raise ValueError("SCHEME_IDENTITY_NSE_LISTING_NOT_FRESH")
    all_listings = store.etf_listings()
    owned = [item for item in all_listings if item.instrument_id == attestation.nse_instrument_id]
    if len(owned) != 1:
        raise ValueError("SCHEME_IDENTITY_LISTING_NOT_UNIQUE")
    listing = owned[0]
    p = listing.provenance
    if (listing.asset_type != "ETF" or listing.exchange != "NSE"
            or listing.symbol != attestation.ticker or listing.isin != attestation.isin
            or p.provider != "NSE" or p.authority is not EtfAuthority.OFFICIAL_EXCHANGE
            or p.source_mode != "REAL"
            or document_host(p.source_url) not in NSE_HOSTS
            or any(item.isin == attestation.isin and item.instrument_id != attestation.nse_instrument_id
                   for item in all_listings)):
        raise ValueError("SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT")
    return SchemeIdentityPreflight(
        status="READY_FOR_MANUAL_REVIEW", ticker=attestation.ticker,
        isin=attestation.isin, instrument_id=attestation.nse_instrument_id,
        review_reference=attestation.review_reference,
        nse_listing_source_url=p.source_url,
    )


def preflight_shared_review_identities(
    store, reviewed_document, attestations_by_ticker: dict[str, SchemeIdentityAttestation],
    *, now: datetime | None = None,
) -> tuple[SchemeIdentityPreflight, ...]:
    """All-or-nothing identity preflight for a shared factsheet, no persistence.

    A review result with any missing or duplicate attestation fails as a unit.
    Nothing in the return value authorizes downstream score/readiness changes.
    """
    if (reviewed_document.status != "DOCUMENT_REVIEW_ONLY"
            or reviewed_document.identity_verified
            or not 1 <= len(reviewed_document.metrics) <= 2):
        raise ValueError("SHARED_SCHEME_REVIEW_INVALID")
    tickers = [metric.ticker for metric in reviewed_document.metrics]
    if len(set(tickers)) != len(tickers) or set(tickers) != set(attestations_by_ticker):
        raise ValueError("SHARED_SCHEME_IDENTITY_ATTESTATIONS_INCOMPLETE")
    validated = tuple(
        preflight_scheme_identity(store, metric, attestations_by_ticker[metric.ticker], now=now)
        for metric in reviewed_document.metrics
    )
    if len({item.instrument_id for item in validated}) != len(validated):
        raise ValueError("SHARED_SCHEME_CANONICAL_IDENTITY_CONFLICT")
    return validated
