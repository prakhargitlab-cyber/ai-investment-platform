"""Discover published Nippon ETF PDF links from an explicitly approved issuer listing.

No month/filename guessing, crawler, approval promotion, PDF download, database
write, or scoring occurs here. The listing URL is an exact, reviewer-supplied
capability: the caller must review it before setting enabled=True. There is no
default approved listing URL, and production does not call this module.

Returned URLs are UNVERIFIED_DISCOVERY_CANDIDATE, NOT approved PDF sources.
In particular, link order and filename do not establish the latest reporting
date. A separate authenticated document-content/date review is required.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

ISSUER_HOST = "mf.nipponindiaim.com"
FAMILIES = {
    "MONTHLY_FACTSHEET": "/InvestorServices/FactSheetsDocuments/",
    "ETF_DIFFERENTIATION": "/InvestorServices/SchemeDifferentiationTable/",
    "ETF_SID": "/InvestorServices/SIDETF/",
}
MAX_HTML_BYTES = 1024 * 1024
MAX_LINKS = 2000
MAX_CANDIDATES = 100


@dataclass(frozen=True)
class DiscoveredPdfCandidate:
    document_family: str
    url: str
    link_text: str
    status: str = "UNVERIFIED_DISCOVERY_CANDIDATE"
    source_authenticated: bool = False
    identity_verified_for_scoring: bool = False


@dataclass(frozen=True)
class ListingDiscoveryResult:
    listing_url: str
    candidates: tuple[DiscoveredPdfCandidate, ...]
    status: str = "REVIEW_ONLY"
    newest_document_confirmed: bool = False


def _issuer_url(url: str, *, exact_path: str | None = None) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == ISSUER_HOST
            and parts.netloc == ISSUER_HOST
            and parts.username is None and parts.password is None
            and parts.port is None
            and not parts.query and not parts.fragment
            and (exact_path is None or parts.path == exact_path)
        )
    except ValueError:
        return False


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self.total_links = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self.total_links += 1
        if self.total_links > MAX_LINKS:
            raise ValueError("NIPPON_LISTING_LINK_LIMIT")
        values = dict(attrs)
        self._href = values.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            if self._href is not None:
                self.links.append((self._href, " ".join("".join(self._text).split())[:160]))
            self._href = None
            self._text = []


def extract_published_pdf_links(
    listing_url: str,
    html_bytes: bytes,
    *,
    approved_listing_url: str,
) -> ListingDiscoveryResult:
    """Pure offline parser: only exact approved issuer listing and actual links.

    The approved listing is a reviewed *location*, never a PDF permission.
    It must be independently approved; this function cannot authenticate it.
    """
    if not _issuer_url(approved_listing_url) or listing_url != approved_listing_url:
        raise ValueError("NIPPON_LISTING_NOT_APPROVED")
    if not isinstance(html_bytes, bytes) or len(html_bytes) > MAX_HTML_BYTES:
        raise ValueError("NIPPON_LISTING_SIZE_LIMIT")
    parser = _AnchorParser()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    parser.close()

    candidates: list[DiscoveredPdfCandidate] = []
    seen: set[str] = set()
    for href, title in parser.links:
        target = urljoin(listing_url, href.strip())
        if not _issuer_url(target) or target in seen:
            continue
        path = urlsplit(target).path
        if not path.lower().endswith(".pdf"):
            continue
        family = next((family for family, prefix in FAMILIES.items()
                       if path.startswith(prefix)), None)
        if family is None:
            continue
        if len(candidates) >= MAX_CANDIDATES:
            raise ValueError("NIPPON_LISTING_CANDIDATE_LIMIT")
        seen.add(target)
        candidates.append(DiscoveredPdfCandidate(family, target, title))
    return ListingDiscoveryResult(listing_url, tuple(candidates))


async def discover_listing(
    *,
    approved_listing_url: str,
    enabled: bool = False,
    client: httpx.AsyncClient | None = None,
) -> ListingDiscoveryResult:
    """Optional bounded HTML retrieval. Disabled unless explicitly approved.

    Redirects (including login redirects) are always rejected. This function
    does NOT register/distribute discovered URLs to any production fetcher.
    """
    if not enabled:
        raise ValueError("NIPPON_LISTING_DISCOVERY_DISABLED")
    if not _issuer_url(approved_listing_url):
        raise ValueError("NIPPON_LISTING_NOT_APPROVED")
    owned_client = client is None
    if owned_client:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10)
    assert client is not None
    try:
        async with client.stream("GET", approved_listing_url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("NIPPON_LISTING_HTTP_NOT_OK")
            if response.url.__str__() != approved_listing_url:
                raise ValueError("NIPPON_LISTING_URL_CHANGED")
            if response.headers.get("content-type", "").split(";")[0].strip().lower() not in (
                "text/html", "application/xhtml+xml"
            ):
                raise ValueError("NIPPON_LISTING_NOT_HTML")
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > MAX_HTML_BYTES:
                    raise ValueError("NIPPON_LISTING_SIZE_LIMIT")
                payload.extend(chunk)
        return extract_published_pdf_links(
            approved_listing_url, bytes(payload),
            approved_listing_url=approved_listing_url,
        )
    finally:
        if owned_client:
            await client.aclose()
