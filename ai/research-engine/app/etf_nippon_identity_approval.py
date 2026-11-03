"""Explicit evidence-bound scheme/ISIN review packet; never a scoring capability.

NSE lists ticker+ISIN; the multi-scheme Nippon factsheet lists scheme headings.
Neither source independently joins the two. An additional authoritative,
scheme-labelled issuer document is required before review can be *prepared*.

This module performs no fetching, no authentication of retrieved bytes, no
human decision, no source registry mutation, no DB writes, no scoring changes.
A caller-supplied excerpt or approval string alone can never authorize ingest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from urllib.parse import urlsplit
from uuid import UUID

from app.etf_nippon_factsheet_review import HEADINGS
from app.etf_nippon_nse_comparison import EXPECTED_ISINS
from app.etf_nippon_persistence_guard import StagedNipponFactsheet
from app.etf_nippon_identity_preflight import SchemeIdentityPreflight

MAX_IDENTITY_DOCUMENT_BYTES = 5 * 1024 * 1024
_ISSUER_HOST = "mf.nipponindiaim.com"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISIN_LABEL = re.compile(r"(?im)^\s*(?:ISIN|ISIN Number)\s*[:\-]\s*([A-Z0-9]{12})\s*$")
_SCHEME_LABEL = re.compile(r"(?im)^\s*(?:Scheme|Scheme Name)\s*[:\-]\s*(.+?)\s*$")


@dataclass(frozen=True)
class IssuerSchemeIdentityEvidence:
    ticker: str
    isin: str
    scheme_heading: str
    source_url: str
    source_sha256: str
    # The exact bounded issuer document supplied to the review, not a
    # typed assertion. Cryptographic hash binds the review to its bytes.
    source_bytes: bytes
    reviewer_reference: str
    reviewer_id: str
    reviewed_at: datetime


@dataclass(frozen=True)
class SchemeIdentityReviewRecord:
    status: str
    ticker: str
    isin: str
    instrument_id: UUID
    scheme_heading: str
    source_url: str
    source_sha256: str
    pdf_sha256: str
    nse_csv_sha256: str
    staging_id: str
    reviewer_id: str
    review_reference: str
    reviewed_at: datetime
    # These flags are intentionally never set based on caller-supplied data.
    source_authenticated: bool = False
    human_approval_verified: bool = False
    identity_verified_for_scoring: bool = False
    eligible_for_etf_fact_persistence: bool = False


@dataclass(frozen=True)
class SchemeIdentityReviewPacket:
    status: str
    staging_id: str
    records: tuple[SchemeIdentityReviewRecord, ...]
    identity_verified_for_scoring: bool = False
    eligible_for_etf_fact_persistence: bool = False


def _issuer_url(url: str) -> bool:
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and u.hostname == _ISSUER_HOST
                and u.username is None and u.password is None and u.port is None
                and not u.query and not u.fragment and u.path.startswith("/")
                and u.path.lower().endswith(".pdf"))
    except ValueError:
        return False


def prepare_identity_review_packet(
    staged: StagedNipponFactsheet,
    preflights: tuple[SchemeIdentityPreflight, ...],
    evidence: tuple[IssuerSchemeIdentityEvidence, ...],
) -> SchemeIdentityReviewPacket:
    """Validate review inputs all-or-nothing; does NOT issue an approval.

    The external issuer document must explicitly label one scheme and one
    ISIN, unambiguously, in extractable text. The document's authenticity
    and the reviewer authorization are still outside this offline contract.
    """
    if (staged.status != "REVIEW_ONLY_NOT_INGESTIBLE"
            or staged.identity_verified_for_scoring
            or staged.eligible_for_etf_fact_persistence
            or not _SHA256.fullmatch(staged.staging_id)
            or not _SHA256.fullmatch(staged.pdf_sha256)
            or not _SHA256.fullmatch(staged.nse_csv_sha256)
            or not 1 <= len(preflights) <= 2
            or len(preflights) != len(evidence)):
        raise ValueError("NIPPON_IDENTITY_REVIEW_SCOPE_INVALID")
    staged_by_ticker = {}
    for metric in staged.metrics:
        if (metric.review_status != "REVIEW_ONLY_NOT_INGESTIBLE"
                or metric.ticker not in HEADINGS
                or metric.scheme_heading != HEADINGS[metric.ticker]
                or metric.isin != EXPECTED_ISINS[metric.ticker]
                or metric.pdf_sha256 != staged.pdf_sha256
                or metric.nse_csv_sha256 != staged.nse_csv_sha256
                or metric.source_url != staged.source_url
                or metric.report_date != staged.reporting_date):
            raise ValueError("NIPPON_IDENTITY_STAGED_EVIDENCE_CONFLICT")
        staged_by_ticker.setdefault(metric.ticker, []).append(metric)
    if (set(staged_by_ticker) != {p.ticker for p in preflights}
            or set(staged_by_ticker) != {e.ticker for e in evidence}
            or len({p.ticker for p in preflights}) != len(preflights)
            or len({e.ticker for e in evidence}) != len(evidence)
            or len({p.instrument_id for p in preflights}) != len(preflights)
            or any(len(rows) != 4 or len({m.metric for m in rows}) != 4
                   for rows in staged_by_ticker.values())):
        raise ValueError("NIPPON_IDENTITY_REVIEW_SCOPE_INVALID")

    by_preflight = {p.ticker: p for p in preflights}
    records = []
    for source in evidence:
        p = by_preflight[source.ticker]
        if (p.status != "READY_FOR_MANUAL_REVIEW" or p.identity_verified_for_scoring
                or p.ticker != source.ticker or p.isin != source.isin
                or p.isin != EXPECTED_ISINS[source.ticker]
                or p.instrument_id.int == 0
                or source.scheme_heading != HEADINGS[source.ticker]
                or not isinstance(source.reviewer_id, str) or not source.reviewer_id.strip()
                or not isinstance(source.reviewer_reference, str) or not source.reviewer_reference.strip()
                or not isinstance(source.reviewed_at, datetime)
                or source.reviewed_at.tzinfo is None
                or source.reviewed_at.utcoffset() is None
                or source.reviewed_at > datetime.now(timezone.utc)
                or not _issuer_url(source.source_url)):
            raise ValueError("NIPPON_IDENTITY_REVIEW_NOT_ESTABLISHED")
        if (not isinstance(source.source_bytes, bytes)
                or not 0 < len(source.source_bytes) <= MAX_IDENTITY_DOCUMENT_BYTES
                or sha256(source.source_bytes).hexdigest() != source.source_sha256
                or not _SHA256.fullmatch(source.source_sha256)):
            raise ValueError("NIPPON_IDENTITY_SOURCE_HASH_MISMATCH")
        # This contract accepts only a bounded plain-text evidence extraction
        # from an already reviewed issuer PDF, not arbitrary cross-page matches.
        # Human review must still authenticate the original document and
        # validate that its text extraction is faithful.
        try:
            text = source.source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("NIPPON_IDENTITY_SOURCE_TEXT_INVALID") from exc
        isins = _ISIN_LABEL.findall(text)
        names = _SCHEME_LABEL.findall(text)
        if (len(isins) != 1 or len(names) != 1
                or isins[0] != source.isin
                or " ".join(names[0].casefold().split()) !=
                   " ".join(source.scheme_heading.casefold().split())):
            raise ValueError("NIPPON_IDENTITY_LABELLED_SCHEME_ISIN_MISSING_OR_AMBIGUOUS")
        # The supplied text is not proof of originating from the claimed URL.
        records.append(SchemeIdentityReviewRecord(
            status="ISSUER_IDENTITY_EVIDENCE_PENDING_AUTHENTICATION",
            ticker=source.ticker, isin=source.isin, instrument_id=p.instrument_id,
            scheme_heading=source.scheme_heading, source_url=source.source_url,
            source_sha256=source.source_sha256, pdf_sha256=staged.pdf_sha256,
            nse_csv_sha256=staged.nse_csv_sha256, staging_id=staged.staging_id,
            reviewer_id=source.reviewer_id, review_reference=source.reviewer_reference,
            reviewed_at=source.reviewed_at,
        ))
    return SchemeIdentityReviewPacket(
        status="IDENTITY_EVIDENCE_REVIEW_ONLY",
        staging_id=staged.staging_id, records=tuple(records),
    )


def authorize_identity_for_scoring(*args, **kwargs):
    """No approval token or trusted persistence capability exists yet."""
    raise ValueError("NIPPON_IDENTITY_APPROVAL_NOT_AUTHORIZED")
