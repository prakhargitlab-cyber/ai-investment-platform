"""Bounded shared-document staging for Nippon monthly factsheets.

The returned data is REVIEW ONLY, never EtfFact evidence. A caller must
explicitly approve a public exact URL for BOTH requested ETF instruments
through ApprovedSourceRegistry and supply an injected fetcher that returns
the original PDF bytes. This coordinator cannot perform network I/O itself.

An approval for a Product Note or a URL shape match is never sufficient.
The live network adapter and identity-to-fund bridge are intentionally not
wired: those require separate review and tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Awaitable, Callable
from uuid import UUID

from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics, extract_review_only, MAX_PDF_BYTES
from app.etf_nippon_monthly_source import MonthlyFactsheetCandidate, is_reviewed_path_shape, reporting_period_matches
from app.etf_official_document_sources import AccessTier, ApprovedDocumentType, ApprovedSourceRegistry

MAX_SHARED_SCHEMES = 2


@dataclass(frozen=True)
class SharedFactsheetReview:
    status: str
    url: str
    document_sha256: str
    publication_month: date
    metrics: tuple[ReviewOnlyEtfMetrics, ...]
    identity_verified: bool = False


async def review_approved_shared_month(
    candidate: MonthlyFactsheetCandidate,
    registry: ApprovedSourceRegistry,
    instrument_ids_by_ticker: dict[str, UUID],
    fetch_pdf_once: Callable[[str], Awaitable[bytes]],
) -> SharedFactsheetReview:
    """Fetch at most one explicitly approved PDF and stage each scheme.

    The fetch callback is injected for offline tests; production wiring must
    use the existing bounded HTTP fetch/security machinery and separately
    enforce an approved monthly publication policy.
    """
    if not is_reviewed_path_shape(candidate.url):
        raise ValueError("NIPPON_MONTHLY_URL_NOT_REVIEWED_SHAPE")
    if not 1 <= len(instrument_ids_by_ticker) <= MAX_SHARED_SCHEMES:
        raise ValueError("SHARED_FACTSHEET_SCHEME_LIMIT")
    if len(set(instrument_ids_by_ticker.values())) != len(instrument_ids_by_ticker):
        raise ValueError("SHARED_FACTSHEET_DUPLICATE_IDENTITY")
    for ticker, instrument_id in instrument_ids_by_ticker.items():
        if ticker not in HEADINGS or not isinstance(instrument_id, UUID):
            raise ValueError("UNSUPPORTED_SHARED_FACTSHEET_IDENTITY")
        source = registry.source_for_url(instrument_id, candidate.url)
        if (source is None or source.access_tier is not AccessTier.PUBLIC_UNAUTHENTICATED
                or source.document_type is not ApprovedDocumentType.FACTSHEET
                or source.document_host != "mf.nipponindiaim.com"
                or source.ticker != ticker
                or not source.isin
                or not source.approval_reference.strip()):
            raise ValueError("MONTHLY_FACTSHEET_EXACT_PUBLIC_APPROVAL_REQUIRED")
    # All checks above precede the single callback invocation. No retry or
    # speculative fallback is performed in this coordinator.
    pdf_bytes = await fetch_pdf_once(candidate.url)
    if (not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-")
            or len(pdf_bytes) > MAX_PDF_BYTES):
        raise ValueError("INVALID_OR_OVERSIZED_SHARED_PDF")
    metrics = tuple(extract_review_only(pdf_bytes, ticker) for ticker in instrument_ids_by_ticker)
    if any(not reporting_period_matches(candidate, item.report_date) for item in metrics):
        raise ValueError("MONTHLY_FACTSHEET_REPORT_DATE_MISMATCH")
    digest = sha256(pdf_bytes).hexdigest()
    if any(item.raw_pdf_sha256 != digest or item.identity_verified for item in metrics):
        raise ValueError("SHARED_FACTSHEET_EVIDENCE_CONFLICT")
    return SharedFactsheetReview(
        status="DOCUMENT_REVIEW_ONLY", url=candidate.url,
        document_sha256=digest, publication_month=candidate.publication_month,
        metrics=metrics,
    )
