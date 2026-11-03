"""Offline contracts for untrusted issuer listing discovery."""
import httpx
import pytest

from app.etf_nippon_listing_discovery import (
    extract_published_pdf_links, discover_listing,
)

PAGE = "https://mf.nipponindiaim.com/InvestorServices/ETFDocuments"
FS = ("https://mf.nipponindiaim.com/InvestorServices/"
      "FactSheetsDocuments/Nippon-FS-October-2026.pdf")
TABLE = ("https://mf.nipponindiaim.com/InvestorServices/"
         "SchemeDifferentiationTable/ETF-Scheme-Differentiation-Tabular-format-Aug-2026.pdf")


def parse(body: bytes, **kwargs):
    return extract_published_pdf_links(PAGE, body, approved_listing_url=PAGE, **kwargs)


def test_extract_only_real_published_issuer_links_no_date_guess():
    data = (b'<a href="/InvestorServices/FactSheetsDocuments/Nippon-FS-October-2026.pdf">'
            b'Latest Factsheet</a>'
            b'<a href="/InvestorServices/SchemeDifferentiationTable/'
            b'ETF-Scheme-Differentiation-Tabular-format-Aug-2026.pdf">'
            b'ETF table</a>')
    found = parse(data)
    assert [(c.document_family, c.url) for c in found.candidates] == [
        ("MONTHLY_FACTSHEET", FS), ("ETF_DIFFERENTIATION", TABLE)
    ]
    assert found.newest_document_confirmed is False
    assert all(c.status == "UNVERIFIED_DISCOVERY_CANDIDATE"
               and not c.identity_verified_for_scoring
               and not c.source_authenticated for c in found.candidates)


def test_empty_listing_does_not_invent_current_month_url():
    assert parse(b"<html>No PDF links yet</html>").candidates == ()


@pytest.mark.parametrize("href", [
    "https://evil.example/InvestorServices/FactSheetsDocuments/x.pdf",
    "http://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/x.pdf",
    "https://mf.nipponindiaim.com.evil.example/InvestorServices/FactSheetsDocuments/x.pdf",
    "https://mf.nipponindiaim.com:443/InvestorServices/FactSheetsDocuments/x.pdf",
    "//evil.example/InvestorServices/FactSheetsDocuments/x.pdf",
    "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/x.pdf?token=a",
    "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/x.pdf#hash",
    "https://mf.nipponindiaim.com/Other/x.pdf",
    "javascript:alert(1)",
    "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/x.html",
])
def test_unapproved_or_unsafe_links_excluded(href):
    body = f'<a href="{href}">file</a>'.encode()
    assert parse(body).candidates == ()


def test_deduplicate_same_url():
    assert len(parse(
        f'<a href="{FS}">First</a><a href="{FS}">Second</a>'.encode()
    ).candidates) == 1


@pytest.mark.parametrize("listing_url", [
    "https://evil.example/listing",
    "http://mf.nipponindiaim.com/listing",
    "https://mf.nipponindiaim.com/listing?token=x",
    "https://mf.nipponindiaim.com:8443/listing",
])
def test_listing_not_approved(listing_url):
    with pytest.raises(ValueError, match="NIPPON_LISTING_NOT_APPROVED"):
        extract_published_pdf_links(listing_url, b"", approved_listing_url=PAGE)


def test_exact_listing_location_required():
    with pytest.raises(ValueError, match="NIPPON_LISTING_NOT_APPROVED"):
        extract_published_pdf_links(
            "https://mf.nipponindiaim.com/different",
            b"", approved_listing_url=PAGE
        )


def test_html_limit_and_link_limit():
    with pytest.raises(ValueError, match="NIPPON_LISTING_SIZE_LIMIT"):
        parse(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="NIPPON_LISTING_LINK_LIMIT"):
        parse(b'<a href="x"></a>' * 501)


@pytest.mark.asyncio
async def test_disabled_does_not_call_network():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="Should not run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="NIPPON_LISTING_DISCOVERY_DISABLED"):
            await discover_listing(approved_listing_url=PAGE, client=client)
    assert calls == []


@pytest.mark.asyncio
async def test_explicit_opt_in_fetches_listing_and_returns_review_only_candidates():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content=f'<a href="{TABLE}">ETF table</a>'.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_listing(approved_listing_url=PAGE, enabled=True, client=client)
    assert calls == [PAGE]
    assert result.candidates[0].url == TABLE
    assert result.status == "REVIEW_ONLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 401, 403, 500])
async def test_redirect_login_and_errors_fail_closed(status):
    def handler(request):
        return httpx.Response(
            status, headers={"location": "https://evil.example/"}
            if status in (301, 302) else {}
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="NIPPON_LISTING_HTTP_NOT_OK"):
            await discover_listing(approved_listing_url=PAGE, enabled=True, client=client)


@pytest.mark.asyncio
async def test_wrong_content_type_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "application/pdf"},
                                  content=b"%PDF"))
    )) as client:
        with pytest.raises(ValueError, match="NIPPON_LISTING_NOT_HTML"):
            await discover_listing(approved_listing_url=PAGE, enabled=True, client=client)
