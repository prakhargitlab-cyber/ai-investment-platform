"""Review-only Nippon staging cannot write ETF facts or promote identities."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics
from app.etf_nippon_monthly_source import month_candidate
from app.etf_nippon_nse_comparison import compare_nse_csv_to_factsheet_sections
from app.etf_nippon_shared_review import SharedFactsheetReview
from app.etf_nippon_persistence_guard import (
    stage_nippon_factsheet, ensure_no_staging_conflict,
    persist_nippon_staging_to_etf_facts,
)

PDF = b"%PDF-1.4\noriginal-fixture-bytes"
CSV = (
    b"Symbol,Underlying Asset,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue,ETF Underlying,Underlying Key\n"
    b"NIFTYBEES,Nifty 50,NIPINDETFNIFTYBEES,08-Jan-02,1,INF204KB14I2,1,EQUITY,Nifty 50\n"
    b"JUNIORBEES,Nippon India ETF Nifty Next 50 Junior BeES,NIP IND ETF JUNIOR BEES,06-Mar-03,1,INF732E01045,1.25,EQUITY,Nifty Next 50\n"
)
CANDIDATE = month_candidate(2026, 10)
PDF_SHA = sha256(PDF).hexdigest()


def metric(ticker):
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker=ticker, scheme_heading=HEADINGS[ticker],
        pdf_page=207 if ticker == "NIFTYBEES" else 216,
        report_date=date(2026, 9, 30),
        aum_crore=Decimal("65178.85") if ticker == "NIFTYBEES" else Decimal("8653.84"),
        average_aum_crore=Decimal("66163.18"),
        monthly_average_expense_ratio_percent=Decimal("0.04"),
        tracking_error_1y_percent=Decimal("0.02"),
        tracking_difference_1y_percent=Decimal("-0.02"),
        raw_pdf_sha256=PDF_SHA,
    )


def fixtures():
    metrics = (metric("NIFTYBEES"), metric("JUNIORBEES"))
    reviewed = SharedFactsheetReview(
        status="DOCUMENT_REVIEW_ONLY", url=CANDIDATE.url,
        document_sha256=PDF_SHA, publication_month=CANDIDATE.publication_month,
        metrics=metrics,
    )
    comparisons = compare_nse_csv_to_factsheet_sections(CSV, metrics)
    return reviewed, comparisons


def stage():
    reviewed, comparisons = fixtures()
    return stage_nippon_factsheet(reviewed, comparisons, raw_pdf_bytes=PDF,
                                  raw_nse_csv_bytes=CSV)


def test_staging_is_deterministic_and_remains_not_ingestible():
    a, b = stage(), stage()
    assert a == b
    assert ensure_no_staging_conflict(a, b)
    assert a.status == "REVIEW_ONLY_NOT_INGESTIBLE"
    assert not a.eligible_for_etf_fact_persistence
    assert not a.identity_verified_for_scoring
    assert len(a.metrics) == 8
    assert {(m.ticker, m.page) for m in a.metrics} == {
        ("NIFTYBEES", 207), ("JUNIORBEES", 216)}
    assert {m.metric for m in a.metrics} == {
        "AUM", "EXPENSE_RATIO", "TRACKING_ERROR", "TRACKING_DIFFERENCE"}
    assert {m.nse_csv_sha256 for m in a.metrics} == {sha256(CSV).hexdigest()}
    assert {m.pdf_sha256 for m in a.metrics} == {PDF_SHA}
    assert all(m.review_status == "REVIEW_ONLY_NOT_INGESTIBLE" for m in a.metrics)


def test_tampered_original_document_rejected():
    reviewed, comparisons = fixtures()
    with pytest.raises(ValueError, match="NIPPON_STAGING_PDF_HASH_MISMATCH"):
        stage_nippon_factsheet(reviewed, comparisons, raw_pdf_bytes=PDF+b"tampered",
                              raw_nse_csv_bytes=CSV)


def test_tampered_original_exchange_csv_rejected():
    reviewed, comparisons = fixtures()
    with pytest.raises(ValueError, match="NIPPON_STAGING_NSE_EVIDENCE_MISMATCH"):
        stage_nippon_factsheet(reviewed, comparisons, raw_pdf_bytes=PDF,
                              raw_nse_csv_bytes=CSV.replace(b"NIFTYBEES", b"OTHERBEES"))


def test_forged_comparison_identity_cannot_be_used():
    reviewed, comparisons = fixtures()
    forged = (replace(comparisons[0], expected_isin="INF999999999"), comparisons[1])
    with pytest.raises(ValueError, match="NIPPON_STAGING_NSE_EVIDENCE_MISMATCH"):
        stage_nippon_factsheet(reviewed, forged, raw_pdf_bytes=PDF, raw_nse_csv_bytes=CSV)


def test_reviewed_period_mismatch_rejected():
    reviewed, comparisons = fixtures()
    bad = replace(reviewed, metrics=(
        replace(reviewed.metrics[0], report_date=date(2026, 8, 31)), reviewed.metrics[1]
    ))
    # NSE comparison is still consistent with the identities, but the period is not.
    with pytest.raises(ValueError, match="NIPPON_STAGING_REPORTING_PERIOD_MISMATCH"):
        stage_nippon_factsheet(bad, comparisons, raw_pdf_bytes=PDF, raw_nse_csv_bytes=CSV)


def test_duplicate_section_rejected():
    reviewed, comparisons = fixtures()
    bad = replace(reviewed, metrics=(reviewed.metrics[0], reviewed.metrics[0]))
    with pytest.raises(ValueError, match="NIPPON_STAGING_DUPLICATE_TICKER"):
        stage_nippon_factsheet(bad, comparisons, raw_pdf_bytes=PDF, raw_nse_csv_bytes=CSV)


def test_modified_metric_requires_explicit_revision_review():
    a = stage()
    reviewed, _ = fixtures()
    revised = replace(reviewed, metrics=(
        replace(reviewed.metrics[0], aum_crore=Decimal("65000.00")), reviewed.metrics[1]
    ))
    new_comparisons = compare_nse_csv_to_factsheet_sections(CSV, revised.metrics)
    b = stage_nippon_factsheet(revised, new_comparisons,
                              raw_pdf_bytes=PDF, raw_nse_csv_bytes=CSV)
    with pytest.raises(ValueError, match="NIPPON_STAGING_CONFLICTING_REVISION"):
        ensure_no_staging_conflict(a, b)


def test_wrong_source_url_not_accepted():
    reviewed, comparisons = fixtures()
    with pytest.raises(ValueError, match="NIPPON_STAGING_UNVERIFIED_DOCUMENT"):
        stage_nippon_factsheet(replace(reviewed, url="https://example.com/a.pdf"),
                              comparisons, raw_pdf_bytes=PDF, raw_nse_csv_bytes=CSV)


def test_trusted_store_persistence_explicitly_blocked_without_review():
    class NeverWriteStore:
        def save_etf_evidence(self, evidence):
            raise AssertionError("trusted evidence store must never be written")
    with pytest.raises(ValueError, match="NIPPON_TRUSTED_PERSISTENCE_NOT_AUTHORIZED"):
        persist_nippon_staging_to_etf_facts(NeverWriteStore(), stage())
