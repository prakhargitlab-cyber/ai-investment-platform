"""Review-only extraction from a Nippon multi-scheme monthly factsheet.

This module DOES NOT create EtfFact, confirm scheme ISIN, approve a URL, or
bypass the strict official-document ingestion identity gate. Figures here
are untrusted until separately bound and reviewed with official identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
import hashlib
import re

from pypdf import PdfReader

# Only these exact headings are in scope for the first bounded rollout.
HEADINGS = {
    "NIFTYBEES": "Nippon India ETF Nifty 50 BeES",
    "JUNIORBEES": "Nippon India ETF Nifty Next 50 Junior BeES",
}
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 500


@dataclass(frozen=True)
class ReviewOnlyEtfMetrics:
    status: str
    ticker: str
    scheme_heading: str
    pdf_page: int  # 1-based physical PDF page; not the printed page label
    report_date: date
    aum_crore: Decimal
    average_aum_crore: Decimal
    monthly_average_expense_ratio_percent: Decimal
    tracking_error_1y_percent: Decimal
    tracking_difference_1y_percent: Decimal
    raw_pdf_sha256: str
    identity_verified: bool = False  # never promoted by this parser


def _one(text: str, pattern: str, reason: str) -> str:
    matches = re.findall(pattern, text, flags=re.I | re.M)
    if len(matches) != 1:
        raise ValueError(reason)
    return matches[0]


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def extract_review_only(pdf_bytes: bytes, ticker: str) -> ReviewOnlyEtfMetrics:
    if ticker not in HEADINGS:
        raise ValueError("UNSUPPORTED_BOUNDED_NIPPON_ETF")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("INVALID_OR_OVERSIZED_FACTSHEET")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError("FACTSHEET_PAGE_LIMIT")
        # Match a scheme's exact leading title, not a contents-page mention.
        # Reject duplicates; page ordering is not an identity proof.
        found = []
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            leading = [line.strip() for line in text.splitlines()[:12]]
            if HEADINGS[ticker] in leading and "Fund Details" in text and "Fund Size" in text:
                found.append((index, text))
        if len(found) != 1:
            raise ValueError("MISSING_OR_AMBIGUOUS_SCHEME_PAGE")
        index, text = found[0]
        heading = HEADINGS[ticker]
        report_raw = _one(text, r"Date as on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                          "MISSING_OR_AMBIGUOUS_REPORT_DATE")
        reporting_date = datetime.strptime(report_raw, "%B %d, %Y").date()
        aum = _decimal(_one(text, r"^AUM:\s*[`‘’₹]?\s*([\d,]+\.\d+)\s*Cr\s*$",
                            "MISSING_OR_AMBIGUOUS_AUM"))
        aaum = _decimal(_one(text, r"^AAUM:\s*[`‘’₹]?\s*([\d,]+\.\d+)\s*Cr\s*$",
                             "MISSING_OR_AMBIGUOUS_AVERAGE_AUM"))
        expense = _decimal(_one(text, r"^Expense Ratio\s*\nDirect\s+(\d+(?:\.\d+)?)%",
                               "MISSING_OR_AMBIGUOUS_EXPENSE_RATIO"))
        tracking_error = _decimal(_one(text, r"Tracking Error \(%\)\s*\n1 Year\s+(-?\d+(?:\.\d+)?)",
                                      "MISSING_OR_AMBIGUOUS_TRACKING_ERROR"))
        tracking_difference = _decimal(_one(text, r"Tracking Difference \(%\)\s*\n1 Year\s+(-?\d+(?:\.\d+)?)",
                                           "MISSING_OR_AMBIGUOUS_TRACKING_DIFFERENCE"))
        if aum <= 0 or aaum <= 0 or not 0 <= expense <= 100:
            raise ValueError("INVALID_FACTSHEET_METRIC")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("FACTSHEET_PARSE_FAILED") from exc
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker=ticker, scheme_heading=heading,
        pdf_page=index + 1, report_date=reporting_date, aum_crore=aum,
        average_aum_crore=aaum, monthly_average_expense_ratio_percent=expense,
        tracking_error_1y_percent=tracking_error,
        tracking_difference_1y_percent=tracking_difference,
        raw_pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
