"""No live HTTP: network integration is exercised via httpx.MockTransport."""
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics
from app.etf_nippon_monthly_acquisition import NipponMonthlyReviewAcquisition
from app.etf_nippon_monthly_source import month_candidate
from app.etf_official_document_fetch import BoundedOfficialDocumentFetcher
from app.etf_official_document_sources import (
    AccessTier, ApprovedDocumentSource, ApprovedDocumentType, ApprovedSourceRegistry,
)
from app.settings import Settings
import app.etf_nippon_shared_review as shared

CANDIDATE = month_candidate(2026, 10)
IDS = {"NIFTYBEES": UUID(int=10), "JUNIORBEES": UUID(int=11)}
ISINS = {"NIFTYBEES": "INF204KB14I2", "JUNIORBEES": "INF732E01045"}


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 740 Td (Testing original PDF bytes) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


PDF = pdf()


def registry(*, access=AccessTier.PUBLIC_UNAUTHENTICATED, missing=()):
    return ApprovedSourceRegistry([
        ApprovedDocumentSource(
            instrument_id=instrument, isin=ISINS[ticker], ticker=ticker,
            issuer_name="Nippon India Mutual Fund",
            document_url=CANDIDATE.url, document_host="mf.nipponindiaim.com",
            document_type=ApprovedDocumentType.FACTSHEET,
            approval_reference="offline-test-only-do-not-enable-live",
            access_tier=access,
        )
        for ticker, instrument in IDS.items() if ticker not in missing
    ])


def extraction(pdf_bytes, ticker):
    assert pdf_bytes == PDF and ticker in HEADINGS
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker=ticker, scheme_heading=HEADINGS[ticker],
        pdf_page=207 if ticker == "NIFTYBEES" else 216,
        report_date=date(2026, 9, 30), aum_crore=Decimal("1"),
        average_aum_crore=Decimal("2"), monthly_average_expense_ratio_percent=Decimal("0.04"),
        tracking_error_1y_percent=Decimal("0.02"),
        tracking_difference_1y_percent=Decimal("-0.02"),
        raw_pdf_sha256=sha256(pdf_bytes).hexdigest(),
    )


def setup(reg, enabled, handler):
    settings = Settings(research_etf_official_document_fetch_enabled=enabled)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = BoundedOfficialDocumentFetcher(reg, settings, client=client)
    return NipponMonthlyReviewAcquisition(reg, settings, fetcher=fetcher)


@pytest.mark.asyncio
async def test_one_http_request_for_two_approved_etfs(monkeypatch):
    monkeypatch.setattr(shared, "extract_review_only", extraction)
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    result = await setup(registry(), True, handle).review(CANDIDATE, IDS)
    assert calls == [CANDIDATE.url]
    assert result.status == "DOCUMENT_REVIEW_ONLY"
    assert result.document_sha256 == sha256(PDF).hexdigest()
    assert all(not row.identity_verified for row in result.metrics)


@pytest.mark.asyncio
async def test_disabled_flag_prevents_even_approved_requests():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    with pytest.raises(ValueError, match="MONTHLY_REVIEW_FETCH_DISABLED"):
        await setup(registry(), False, handle).review(CANDIDATE, IDS)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reg", [
    ApprovedSourceRegistry(),
    registry(access=AccessTier.REQUIRES_AUTHENTICATION),
    registry(missing=("JUNIORBEES",)),
])
async def test_missing_or_restricted_approval_blocks_network(reg):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    with pytest.raises(ValueError, match="MONTHLY_FACTSHEET_EXACT_PUBLIC_APPROVAL_REQUIRED"):
        await setup(reg, True, handle).review(CANDIDATE, IDS)
    assert calls == []


@pytest.mark.asyncio
async def test_cross_host_redirect_denied_even_for_approved_document():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if request.url.host == "mf.nipponindiaim.com":
            return httpx.Response(302, headers={"location": "https://other.example.com/file.pdf"})
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    with pytest.raises(ValueError, match="CROSS_HOST_REDIRECT_REJECTED"):
        await setup(registry(), True, handle).review(CANDIDATE, IDS)
    assert CANDIDATE.url in calls


@pytest.mark.asyncio
async def test_html_login_page_denied():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"<html>login</html>",
                              headers={"content-type": "text/html"})
    with pytest.raises(ValueError, match="UNEXPECTED_CONTENT_TYPE"):
        await setup(registry(), True, handle).review(CANDIDATE, IDS)
    assert calls == [CANDIDATE.url]
