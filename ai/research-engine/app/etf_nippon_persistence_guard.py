"""Guarded, deterministic staging boundary for Nippon monthly ETF factsheet values.

This module produces auditable STAGED records, never EtfFact or database
writes. The existing ETF fact table is scoring-visible, so committing these
records before independent reviewed scheme-name equivalence would be an
identity bypass. A later, separately reviewed ingest contract must establish
a real approval capability before any EtfFact is constructed.

No network access, no scheduler, no writes, no auto-approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import re

from app.etf_nippon_factsheet_review import HEADINGS, MAX_PDF_BYTES
from app.etf_nippon_monthly_source import is_reviewed_path_shape, reporting_period_matches, month_candidate
from app.etf_nippon_nse_comparison import EXPECTED_ISINS, NseSchemeIdentityComparison
from app.etf_nippon_shared_review import SharedFactsheetReview

_SHA = re.compile(r"^[0-9a-f]{64}$")
_STAGING_METRICS = (
    ("AUM", "aum_crore", "INR_CRORE"),
    ("EXPENSE_RATIO", "monthly_average_expense_ratio_percent", "PERCENT"),
    ("TRACKING_ERROR", "tracking_error_1y_percent", "PERCENT_1Y"),
    ("TRACKING_DIFFERENCE", "tracking_difference_1y_percent", "PERCENT_1Y"),
)


@dataclass(frozen=True)
class StagedNipponMetric:
    ticker: str
    isin: str
    metric: str
    value: str
    unit: str
    report_date: date
    page: int
    source_url: str
    pdf_sha256: str
    nse_csv_sha256: str
    nse_csv_row: int
    nse_security_name: str
    scheme_heading: str
    review_status: str = "REVIEW_ONLY_NOT_INGESTIBLE"


@dataclass(frozen=True)
class StagedNipponFactsheet:
    status: str
    staging_id: str
    source_url: str
    pdf_sha256: str
    nse_csv_sha256: str
    reporting_date: date
    metrics: tuple[StagedNipponMetric, ...]
    identity_verified_for_scoring: bool = False
    eligible_for_etf_fact_persistence: bool = False


def stage_nippon_factsheet(
    reviewed: SharedFactsheetReview,
    comparisons: tuple[NseSchemeIdentityComparison, ...],
    *, raw_pdf_bytes: bytes,
    raw_nse_csv_bytes: bytes,
) -> StagedNipponFactsheet:
    """Create a deterministic review snapshot, validating both original files.

    A comparison cannot be supplied for a different PDF section, or forged
    merely by changing its ticker/ISIN. It must match a fresh recomputation
    from the provided original NSE CSV and review-only section metrics.
    """
    from app.etf_nippon_nse_comparison import compare_nse_csv_to_factsheet_sections

    if (reviewed.status != "DOCUMENT_REVIEW_ONLY" or reviewed.identity_verified
            or not 1 <= len(reviewed.metrics) <= 2
            or not isinstance(raw_pdf_bytes, bytes)
            or not raw_pdf_bytes.startswith(b"%PDF-")
            or not 0 < len(raw_pdf_bytes) <= MAX_PDF_BYTES
            or not is_reviewed_path_shape(reviewed.url)):
        raise ValueError("NIPPON_STAGING_UNVERIFIED_DOCUMENT")

    digest = sha256(raw_pdf_bytes).hexdigest()
    if reviewed.document_sha256 != digest or not _SHA.fullmatch(digest):
        raise ValueError("NIPPON_STAGING_PDF_HASH_MISMATCH")
    if len({m.ticker for m in reviewed.metrics}) != len(reviewed.metrics):
        raise ValueError("NIPPON_STAGING_DUPLICATE_TICKER")
    expected = compare_nse_csv_to_factsheet_sections(raw_nse_csv_bytes, reviewed.metrics)
    if not isinstance(comparisons, tuple) or comparisons != expected:
        raise ValueError("NIPPON_STAGING_NSE_EVIDENCE_MISMATCH")
    if len(expected) != len(reviewed.metrics):
        raise ValueError("NIPPON_STAGING_INCOMPLETE_IDENTITY")
    if any(c.status not in {"MATCH_EXACT_NAME_REVIEW_REQUIRED", "NAME_DIFFERS_REVIEW_REQUIRED"}
           or c.evidence_authenticated or c.identity_verified_for_scoring
           or c.nse_isin != EXPECTED_ISINS[c.ticker]
           or c.nse_symbol != c.ticker or not c.nse_scheme_name or c.nse_row_number is None
           for c in expected):
        raise ValueError("NIPPON_STAGING_IDENTITY_REVIEW_UNRESOLVED")
    candidate = month_candidate(reviewed.publication_month.year, reviewed.publication_month.month)
    if candidate.url != reviewed.url:
        raise ValueError("NIPPON_STAGING_MONTH_URL_MISMATCH")
    if not all(reporting_period_matches(candidate, m.report_date) for m in reviewed.metrics):
        raise ValueError("NIPPON_STAGING_REPORTING_PERIOD_MISMATCH")
    if len({m.report_date for m in reviewed.metrics}) != 1:
        raise ValueError("NIPPON_STAGING_REPORTING_PERIOD_CONFLICT")
    staging = []
    for metric, comparison in zip(reviewed.metrics, expected):
        if (metric.status != "DOCUMENT_REVIEW_ONLY" or metric.identity_verified
                or metric.scheme_heading != HEADINGS.get(metric.ticker)
                or metric.raw_pdf_sha256 != digest
                or not isinstance(metric.pdf_page, int) or metric.pdf_page < 1):
            raise ValueError("NIPPON_STAGING_SCHEME_PROVENANCE_CONFLICT")
        if not (metric.aum_crore > 0 and metric.average_aum_crore > 0
                and Decimal("0") <= metric.monthly_average_expense_ratio_percent <= Decimal("100")
                and metric.tracking_error_1y_percent >= 0):
            raise ValueError("NIPPON_STAGING_METRIC_VALUE_INVALID")
        for metric_name, field_name, unit in _STAGING_METRICS:
            value = getattr(metric, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("NIPPON_STAGING_METRIC_VALUE_INVALID")
            staging.append(StagedNipponMetric(
                ticker=metric.ticker, isin=comparison.expected_isin,
                metric=metric_name, value=str(value.normalize()), unit=unit,
                report_date=metric.report_date, page=metric.pdf_page,
                source_url=reviewed.url, pdf_sha256=digest,
                nse_csv_sha256=comparison.nse_csv_sha256,
                nse_csv_row=comparison.nse_row_number,
                nse_security_name=comparison.nse_scheme_name,
                scheme_heading=metric.scheme_heading,
            ))
    # Serialize canonical stable values: same original inputs => same staging id.
    payload = [
        {"ticker": r.ticker, "isin": r.isin, "metric": r.metric, "value": r.value,
         "unit": r.unit, "date": r.report_date.isoformat(), "page": r.page,
         "pdf_sha256": r.pdf_sha256, "nse_csv_sha256": r.nse_csv_sha256,
         "nse_row": r.nse_csv_row, "source_url": r.source_url,
         "scheme_heading": r.scheme_heading, "nse_security_name": r.nse_security_name}
        for r in staging
    ]
    identifier = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return StagedNipponFactsheet(
        status="REVIEW_ONLY_NOT_INGESTIBLE", staging_id=identifier,
        source_url=reviewed.url, pdf_sha256=digest, nse_csv_sha256=expected[0].nse_csv_sha256,
        reporting_date=reviewed.metrics[0].report_date, metrics=tuple(staging),
    )


def ensure_no_staging_conflict(existing: StagedNipponFactsheet, incoming: StagedNipponFactsheet) -> bool:
    """True for an identical repeat; reject different revisions for same month.

    A corrected publication must be handled through a separate explicit
    revision-review process, not silently overwrite the staged snapshot.
    """
    if (existing.source_url, existing.reporting_date) != (incoming.source_url, incoming.reporting_date):
        return False
    if existing.staging_id != incoming.staging_id:
        raise ValueError("NIPPON_STAGING_CONFLICTING_REVISION")
    return True


def persist_nippon_staging_to_etf_facts(*args, **kwargs):
    """Intentionally blocked until a separately approved ingestion API exists."""
    raise ValueError("NIPPON_TRUSTED_PERSISTENCE_NOT_AUTHORIZED")
