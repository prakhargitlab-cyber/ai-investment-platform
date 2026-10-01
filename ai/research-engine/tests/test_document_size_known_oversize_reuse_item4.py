"""Stage2 Final Fix -- Item 4: document size limit, known-oversize reuse.

HttpResearchFetcher already rejected an oversized document BEFORE reading
the body whenever Content-Length was present and honest, and already
enforced a streaming byte limit (stopping mid-read, never after a full
download) when Content-Length was missing/dishonest -- see
test_official_content_length_limit_rejects_before_body_read and
test_official_streaming_limit_handles_missing_or_dishonest_content_length in
tests/test_research_engine.py, both of which still pass unmodified.

The gap this item closes: the SAME unchanged URL, once already proven
oversized, was downloaded again from scratch on every subsequent request
within one acquisition/cycle (no record of the outcome was kept anywhere --
a size-rejected fetch never persists a ResearchDocument row, so
_reusable_official_document's lookup always misses). HttpResearchFetcher now
keeps a small, bounded, TTL-scoped (not permanent) known-oversize record
keyed by URL, so a later fetch of the identical URL within the TTL raises
DocumentSizeLimitExceeded immediately -- without opening a connection or
reading a single byte -- while a different URL, a larger size limit than
was ever proven exceeded, or a later request after the TTL window, all
still get a full, real fetch attempt (never a permanently blocked retry).
"""
from __future__ import annotations

import time

import httpx
import pytest

from app.research_fetching import DocumentSizeLimitExceeded, HttpResearchFetcher
from app.settings import Settings

URL = "https://nsearchives.nseindia.com/corporate/known-oversize.pdf"


def _settings(max_bytes: int = 10) -> Settings:
    return Settings(research_max_retries=0, research_official_document_max_bytes=max_bytes)


def _fetcher(requests: list[str], *, headers: dict[str, str], body: bytes = b"%PDF-body") -> HttpResearchFetcher:
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers=headers, content=body, request=request)
    return HttpResearchFetcher(_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_known_oversize_url_with_honest_content_length_is_not_refetched() -> None:
    requests: list[str] = []
    fetcher = _fetcher(requests, headers={"content-type": "application/pdf", "content-length": "11"})
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    # The second call for the identical URL never opened a connection.
    assert requests == [URL]


@pytest.mark.asyncio
async def test_known_oversize_url_with_dishonest_or_missing_content_length_is_not_refetched() -> None:
    requests: list[str] = []
    fetcher = _fetcher(requests, headers={"content-type": "application/pdf"}, body=b"%PDF-" + b"x" * 10)
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    assert requests == [URL]


@pytest.mark.asyncio
async def test_different_url_is_independently_fetched() -> None:
    requests: list[str] = []
    fetcher = _fetcher(requests, headers={"content-type": "application/pdf", "content-length": "11"})
    other_url = "https://nsearchives.nseindia.com/corporate/different.pdf"
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(other_url, max_bytes=10)
    assert requests == [URL, other_url]


@pytest.mark.asyncio
async def test_larger_future_size_limit_is_not_assumed_still_oversize() -> None:
    # The streaming-unknown-size case only proves the body is AT LEAST the
    # previously-enforced max_bytes long -- it must not be replayed against
    # a genuinely larger limit that the document was never proven to exceed.
    requests: list[str] = []
    fetcher = _fetcher(requests, headers={"content-type": "application/pdf"}, body=b"%PDF-" + b"x" * 10)
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=5)
    # A later legitimate call with a larger limit must still get a real
    # fetch attempt, not an incorrectly-reused oversize verdict.
    result = await fetcher.fetch_network(URL, max_bytes=1000)
    assert result.status_code == 200
    assert requests == [URL, URL]


@pytest.mark.asyncio
async def test_known_oversize_record_expires_and_allows_future_retry() -> None:
    requests: list[str] = []
    fetcher = _fetcher(requests, headers={"content-type": "application/pdf", "content-length": "11"})
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    assert requests == [URL]
    # Simulate the TTL window having elapsed (a later, legitimate
    # acquisition/cycle) rather than altering any production timeout.
    recorded_at, content_length, recorded_max_bytes = fetcher._oversize_cache[URL]
    fetcher._oversize_cache[URL] = (
        recorded_at - fetcher._oversize_cache_ttl_seconds - 1.0, content_length, recorded_max_bytes,
    )
    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch_network(URL, max_bytes=10)
    # The expired record did not block a fresh, real retry.
    assert requests == [URL, URL]
