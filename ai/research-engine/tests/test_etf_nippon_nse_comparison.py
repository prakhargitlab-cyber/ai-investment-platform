"""Deterministic, no-network NSE identity comparison tests."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics
from app.etf_nippon_nse_comparison import compare_nse_csv_to_factsheet_sections, review_report

CSV = (
    b"SYMBOL,NAME OF COMPANY,ISIN NUMBER\n"
    b"NIFTYBEES,Nippon India ETF Nifty 50 BeES,INF204KB14I2\n"
    b"JUNIORBEES,Nippon India ETF Nifty Next 50 Junior BeES,INF732E01045\n"
)


def metric(ticker):
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker=ticker, scheme_heading=HEADINGS[ticker],
        pdf_page=207 if ticker == "NIFTYBEES" else 216, report_date=date(2026, 9, 30),
        aum_crore=Decimal("100"), average_aum_crore=Decimal("101"),
        monthly_average_expense_ratio_percent=Decimal("0.04"),
        tracking_error_1y_percent=Decimal("0.02"),
        tracking_difference_1y_percent=Decimal("-0.02"),
        raw_pdf_sha256="a"*64,
    )


METRICS = (metric("NIFTYBEES"), metric("JUNIORBEES"))


def test_exact_rows_are_still_review_only():
    report = review_report(CSV, METRICS)
    assert report["nse_csv_sha256"] == sha256(CSV).hexdigest()
    assert not report["identity_verified_for_scoring"]
    assert not report["evidence_authenticated"]
    assert len(report["schemes"]) == 2
    assert all(s["status"] == "MATCH_EXACT_NAME_REVIEW_REQUIRED" for s in report["schemes"])
    assert [s["nse_row_number"] for s in report["schemes"]] == [2, 3]


def test_exchange_name_difference_not_silently_normalized_to_match():
    changed = CSV.replace(b"Nippon India ETF Nifty 50 BeES", b"Other ETF Nifty 50 BeES")
    rows = compare_nse_csv_to_factsheet_sections(changed, METRICS)
    assert rows[0].status == "NAME_DIFFERS_REVIEW_REQUIRED"
    assert rows[1].status == "MATCH_EXACT_NAME_REVIEW_REQUIRED"


def test_wrong_isin_never_passes():
    changed = CSV.replace(b"INF204KB14I2", b"INF999999999")
    rows = compare_nse_csv_to_factsheet_sections(changed, METRICS)
    assert rows[0].status == "EXCHANGE_SYMBOL_ISIN_CONFLICT"


def test_missing_or_duplicate_row_fails_closed():
    missing = b"SYMBOL,NAME OF COMPANY,ISIN NUMBER\n"
    assert compare_nse_csv_to_factsheet_sections(missing, METRICS)[0].status == "EXCHANGE_ROW_MISSING"
    duplicate = CSV + b"NIFTYBEES,Duplicate,INF204KB14I2\n"
    assert compare_nse_csv_to_factsheet_sections(duplicate, METRICS)[0].status == "EXCHANGE_ROW_AMBIGUOUS"


@pytest.mark.parametrize("invalid", [
    b"FOO,BAR\nA,B\n",
    b"SYMBOL,NAME OF COMPANY,ISIN NUMBER\nNIFTYBEES,Name,Isin,extra\n",
    b"\xff\xf0",
    b"",
])
def test_bad_or_ambiguous_csv_rejected(invalid):
    with pytest.raises(ValueError):
        compare_nse_csv_to_factsheet_sections(invalid, METRICS)


def test_review_only_identity_cannot_be_forced_true():
    with pytest.raises(ValueError, match="NSE_SCHEME_COMPARISON_SCOPE_INVALID"):
        compare_nse_csv_to_factsheet_sections(CSV,
            (replace(METRICS[0], identity_verified=True),))


def test_identical_ticker_duplicates_scope_rejected():
    with pytest.raises(ValueError, match="NSE_SCHEME_COMPARISON_SCOPE_INVALID"):
        compare_nse_csv_to_factsheet_sections(CSV, (METRICS[0], METRICS[0]))


def test_actual_nse_etf_security_list_columns_and_underlying_name():
    # Production-format header reported in anonymous NSE CSV on 2026-11-03.
    # Abbreviated SecurityName is NOT silently treated as the AMC scheme.
    actual_csv = (
        b"Symbol,Underlying Asset,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue,ETF Underlying,Underlying Key\n"
        b"NIFTYBEES,Nifty 50,NIPINDETFNIFTYBEES,08-Jan-02,1,INF204KB14I2,1,EQUITY,Nifty 50\n"
        b"JUNIORBEES,Nippon India ETF Nifty Next 50 Junior BeES,NIP IND ETF JUNIOR BEES,06-Mar-03,1,INF732E01045,1.25,EQUITY,Nifty Next 50\n"
    )
    report = review_report(actual_csv, METRICS)
    assert report["nse_csv_sha256"] == sha256(actual_csv).hexdigest()
    assert [s["nse_isin"] for s in report["schemes"]] == ["INF204KB14I2", "INF732E01045"]
    assert [s["status"] for s in report["schemes"]] == [
        "NAME_DIFFERS_REVIEW_REQUIRED", "NAME_DIFFERS_REVIEW_REQUIRED"
    ]
    assert report["schemes"][0]["nse_underlying_asset"] == "Nifty 50"
    assert report["schemes"][0]["underlying_asset_matches_scheme_heading"] is False
    assert report["schemes"][1]["underlying_asset_matches_scheme_heading"] is True
    assert not report["evidence_authenticated"]
    assert not report["identity_verified_for_scoring"]


def test_underlying_asset_is_not_accepted_as_security_name_replacement():
    csv_data = (
        b"Symbol,Underlying Asset,ISINNumber\n"
        b"JUNIORBEES,Nippon India ETF Nifty Next 50 Junior BeES,INF732E01045\n"
    )
    with pytest.raises(ValueError, match="NSE_IDENTITY_CSV_COLUMN_MISSING_OR_AMBIGUOUS"):
        compare_nse_csv_to_factsheet_sections(csv_data, (METRICS[1],))
