"""Nippon monthly source: strict offline resolution and review-only extraction."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.etf_nippon_monthly_source import (
    bounded_candidates, is_reviewed_path_shape, month_candidate,
)
from app.etf_nippon_factsheet_review import extract_review_only

OCT_URL = ("https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/"
           "Nippon-FS-October-2026.pdf")
REAL_PDF = Path(__file__).resolve().parents[3] / ".tmp" / "etf-validation" / "Nippon-FS-October-2026.pdf"


def test_month_name_and_reporting_date_are_not_conflated():
    october = month_candidate(2026, 10)
    assert october.url == OCT_URL
    assert october.publication_month == date(2026, 10, 1)
    assert october.expected_reporting_date == date(2026, 9, 30)
    january = month_candidate(2027, 1)
    assert january.expected_reporting_date == date(2026, 12, 31)
    assert january.url.endswith("Nippon-FS-January-2027.pdf")
    assert [x.url.split("/")[-1] for x in bounded_candidates(date(2026, 11, 3))] == [
        "Nippon-FS-November-2026.pdf", "Nippon-FS-October-2026.pdf"
    ]
    assert is_reviewed_path_shape(OCT_URL)


@pytest.mark.parametrize("url", [
    "http://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/Nippon-FS-October-2026.pdf",
    "https://evil.example.com/InvestorServices/FactSheetsDocuments/Nippon-FS-October-2026.pdf",
    OCT_URL + "?auth=token",
    OCT_URL + "#fragment",
    OCT_URL.replace("October", "FakeMonth"),
    OCT_URL.replace("/InvestorServices/", "/unreviewed/"),
])
def test_url_pattern_does_not_accept_unreviewed_url_shapes(url):
    assert not is_reviewed_path_shape(url)


@pytest.mark.parametrize("year,month", [(2026, 0), (2026, 13), (0, 10), (2026, True)])
def test_reject_invalid_months(year, month):
    with pytest.raises(ValueError):
        month_candidate(year, month)


def _single_page_pdf(text: str) -> bytes:
    import io
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    content = "BT /F1 10 Tf 50 740 Td\n"
    for index, line in enumerate(text.splitlines()):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += ("0 -12 Td\n" if index else "") + f"({escaped}) Tj\n"
    content += "ET"
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_review_parser_is_explicitly_non_authoritative():
    text = ("Nippon India ETF Nifty 50 BeES\nFund Details\n"
            "Date as on September 30, 2026\nFund Size\n"
            "AAUM: ` 66,163.18 Cr\nAUM: ` 65,178.85 Cr\n"
            "Expense Ratio\nDirect 0.04%\n"
            "3 Tracking Error (%)\n1 Year 0.02\n"
            "5 Tracking Difference (%)\n1 Year -0.02")
    result = extract_review_only(_single_page_pdf(text), "NIFTYBEES")
    assert result.status == "DOCUMENT_REVIEW_ONLY"
    assert result.identity_verified is False
    assert result.aum_crore == Decimal("65178.85")
    assert result.average_aum_crore == Decimal("66163.18")
    assert result.monthly_average_expense_ratio_percent == Decimal("0.04")
    assert result.tracking_error_1y_percent == Decimal("0.02")
    assert result.tracking_difference_1y_percent == Decimal("-0.02")


def test_parser_rejects_ambiguous_metric():
    text = ("Nippon India ETF Nifty 50 BeES\nFund Details\n"
            "Date as on September 30, 2026\nFund Size\n"
            "AAUM: ` 66,163.18 Cr\nAUM: ` 65,178.85 Cr\nAUM: ` 12,000.00 Cr\n"
            "Expense Ratio\nDirect 0.04%\n"
            "3 Tracking Error (%)\n1 Year 0.02\n"
            "5 Tracking Difference (%)\n1 Year -0.02")
    with pytest.raises(ValueError, match="MISSING_OR_AMBIGUOUS_AUM"):
        extract_review_only(_single_page_pdf(text), "NIFTYBEES")


@pytest.mark.parametrize("ticker,expected_page,expected_aum,expected_expense", [
    ("NIFTYBEES", 207, "65178.85", "0.04"),
    ("JUNIORBEES", 216, "8653.84", "0.17"),
])
def test_user_provided_real_pdf_when_locally_available(ticker, expected_page, expected_aum, expected_expense):
    # This 13MB user-provided file is NOT committed to git; for repeatable
    # developer validation, place a copy at the gitignored location above.
    if not REAL_PDF.is_file():
        pytest.skip("genuine Nippon PDF not present in gitignored validation directory")
    result = extract_review_only(REAL_PDF.read_bytes(), ticker)
    assert result.pdf_page == expected_page
    assert result.report_date == date(2026, 9, 30)
    assert result.aum_crore == Decimal(expected_aum)
    assert result.monthly_average_expense_ratio_percent == Decimal(expected_expense)
    assert not result.identity_verified
