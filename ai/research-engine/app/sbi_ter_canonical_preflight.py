"""Read-only canonical identity preflight for PR #17 SBI/NSE review candidates.

Exact ISIN+current-symbol joins are necessary but not sufficient: require an
active canonical NSE ETF and a fresh official NSE listing, using the existing
ETF identity readiness gate. Even successful results are NOT approved NSDL
scheme-code→ISIN mappings and cannot be consumed by the TER resolver.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.etf_document_parser import NSE_HOSTS, document_host
from app.etf_evidence import EtfAuthority
from app.etf_readiness import EtfReadinessStatus, _check_identity_ready
from app.sbi_ter_nse_circular_review import (
    SbiNseIdentityReviewCandidate, SbiNseReconciliationReport,
)


@dataclass(frozen=True)
class SbiCanonicalPreflightRow:
    nsdl_scheme_code: str
    isin: str
    nse_symbol: str
    status: str
    canonical_instrument_id: UUID | None
    reason: str
    nse_listing_source_url: str | None = None
    # Must never be promoted into a ReviewedSbiSchemeIdentity automatically.
    approved_for_persistence: bool = False


@dataclass(frozen=True)
class SbiCanonicalPreflightReport:
    status: str
    rows: tuple[SbiCanonicalPreflightRow, ...]
    nse_pdf_sha256: str
    ter_workbook_sha256: str
    approved_for_persistence: bool = False


def preflight_sbi_candidates(
    report: SbiNseReconciliationReport, canonical_etfs, store,
    *, now: datetime | None = None,
) -> SbiCanonicalPreflightReport:
    """Audit all candidates against active canonical rows and official NSE listings.

    Input canonical rows use the existing Java service's canonical contract:
    globalInstrumentId, assetType, status, primaryExchange, isin, primarySymbol.
    This function reads store.etf_listings and store.etf_attempts but never writes.
    Invalid/untrusted candidate reports fail closed, not as successes.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("SBI_CANONICAL_PREFLIGHT_CLOCK_INVALID")
    if (not isinstance(report, SbiNseReconciliationReport)
            or report.status != "REVIEW_ONLY_NO_DIRECT_CODE_ISIN_PROOF"
            or report.approved_for_persistence
            or not report.candidate_rows):
        raise ValueError("SBI_CANONICAL_REVIEW_REPORT_INVALID")
    seen_codes, seen_isins, seen_symbols = set(), set(), set()
    for candidate in report.candidate_rows:
        if (not isinstance(candidate, SbiNseIdentityReviewCandidate)
                or candidate.approved_for_persistence
                or candidate.status != "REVIEW_ONLY_EXACT_NAME_JOIN_NOT_CODE_ISIN_PROOF"
                or candidate.nse_pdf_sha256 != report.nse_pdf_sha256
                or candidate.ter_workbook_sha256 != report.ter_workbook_sha256
                or candidate.nse_effective_date != report.nse_effective_date
                or candidate.nse_effective_date > now.date()
                or candidate.nsdl_scheme_code in seen_codes
                or candidate.isin in seen_isins
                or candidate.new_nse_symbol in seen_symbols):
            raise ValueError("SBI_CANONICAL_CANDIDATE_AMBIGUOUS_OR_UNTRUSTED")
        seen_codes.add(candidate.nsdl_scheme_code)
        seen_isins.add(candidate.isin)
        seen_symbols.add(candidate.new_nse_symbol)

    canonical = list(canonical_etfs)
    listings = list(store.etf_listings())
    rows = []
    for candidate in report.candidate_rows:
        code, isin, symbol = (
            candidate.nsdl_scheme_code, candidate.isin, candidate.new_nse_symbol
        )
        # Detect collisions even if the conflicting canonical record is inactive.
        relevant = [item for item in canonical if (
            item.get("isin") == isin or item.get("primarySymbol") == symbol
        )]
        exact = [item for item in relevant if (
            item.get("isin") == isin and item.get("primarySymbol") == symbol
            and item.get("assetType") == "ETF" and item.get("status") == "ACTIVE"
            and (item.get("primaryExchange") or item.get("exchange")) == "NSE"
        )]
        uuid = None
        if len(relevant) != 1 or len(exact) != 1:
            status = "MISSING_CANONICAL_IDENTITY" if not relevant else "CONFLICT"
            reason = "CANONICAL_PAIR_ABSENT" if not relevant else "CANONICAL_IDENTITY_COLLISION_OR_INACTIVE"
        else:
            try:
                uuid = UUID(str(exact[0]["globalInstrumentId"]))
                if uuid.int == 0 or any(
                    other is not exact[0] and other.get("globalInstrumentId") == str(uuid)
                    for other in canonical
                ):
                    raise ValueError("Duplicate or empty canonical UUID")
            except (ValueError, TypeError, KeyError):
                uuid = None
                status, reason = "CONFLICT", "CANONICAL_UUID_INVALID_OR_DUPLICATE"
            else:
                # Check globally for NSE listing collisions: same ISIN or symbol
                # cannot belong to another instrument, including unresolved rows.
                colliding = [entry for entry in listings if (
                    entry.isin == isin or entry.symbol == symbol or entry.instrument_id == uuid
                )]
                exact_listings = [entry for entry in colliding if (
                    entry.instrument_id == uuid and entry.isin == isin
                    and entry.symbol == symbol
                )]
                if not colliding:
                    status, reason = "MISSING_NSE_LISTING", "OFFICIAL_NSE_LISTING_ABSENT"
                elif len(colliding) != 1 or len(exact_listings) != 1:
                    status, reason = "CONFLICT", "NSE_LISTING_IDENTITY_COLLISION"
                else:
                    listing = exact_listings[0]
                    provenance = listing.provenance
                    if (listing.asset_type != "ETF" or listing.exchange != "NSE"
                            or provenance.provider != "NSE"
                            or provenance.authority != EtfAuthority.OFFICIAL_EXCHANGE
                            or provenance.source_mode != "REAL"
                            or document_host(provenance.source_url) not in NSE_HOSTS):
                        status, reason = "CONFLICT", "NSE_LISTING_PROVENANCE_UNTRUSTED"
                    else:
                        readiness = _check_identity_ready(store, uuid, now=now)
                        if readiness.status is not EtfReadinessStatus.READY_FRESH:
                            status, reason = (
                                "NSE_LISTING_NOT_FRESH", readiness.reason or readiness.status.value
                            )
                        else:
                            status, reason = (
                                "VERIFIED_CANONICAL_CANDIDATE", "FRESH_OFFICIAL_NSE_LISTING_MATCH"
                            )
        rows.append(SbiCanonicalPreflightRow(
            nsdl_scheme_code=code, isin=isin, nse_symbol=symbol,
            status=status, canonical_instrument_id=uuid,
            reason=reason,
            nse_listing_source_url=(
                exact_listings[0].provenance.source_url
                if uuid is not None and "exact_listings" in locals()
                and status == "VERIFIED_CANONICAL_CANDIDATE" else None
            ),
        ))
        # Avoid leaking the previous candidate's local binding.
        if "exact_listings" in locals():
            del exact_listings
    return SbiCanonicalPreflightReport(
        status="REVIEW_ONLY_CANONICAL_PREFLIGHT",
        rows=tuple(sorted(rows, key=lambda item: item.nsdl_scheme_code)),
        nse_pdf_sha256=report.nse_pdf_sha256,
        ter_workbook_sha256=report.ter_workbook_sha256,
    )
