from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from app.settings import Settings
from app.url_security import validate_public_http_url


class ResearchFetcher(Protocol):
    async def fetch(self, url: str) -> "FetchResult":
        """Fetch permitted public content while respecting source limits and SSRF controls."""


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    status_code: int
    content_type: str
    text: str
    bytes_read: int


class FetchError(RuntimeError):
    pass


class RestrictedFetchError(FetchError):
    pass


class HttpResearchFetcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        timeout = httpx.Timeout(
            timeout=settings.research_request_timeout_seconds,
            connect=settings.research_connect_timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": settings.research_user_agent, "Accept-Encoding": "gzip, deflate, br"},
        )

    async def fetch(self, url: str) -> FetchResult:
        validate_public_http_url(url)
        current_url = url
        for redirect_count in range(self.settings.research_max_redirects + 1):
            response = await self._request_with_retries(current_url)
            if response.is_redirect:
                if redirect_count >= self.settings.research_max_redirects:
                    raise FetchError("Maximum redirects exceeded")
                location = response.headers.get("location")
                if not location:
                    raise FetchError("Redirect without Location header")
                current_url = str(response.url.join(location))
                validate_public_http_url(current_url)
                continue
            return await self._response_to_result(response)
        raise FetchError("Maximum redirects exceeded")

    async def _request_with_retries(self, url: str) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.get(url)
            except httpx.TimeoutException as exception:
                response = None
                last_error: Exception | None = exception
            else:
                last_error = None
                if response.status_code in {401, 403, 407, 451}:
                    raise RestrictedFetchError(f"Source returned restricted status {response.status_code}")
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    return response
            if attempt >= self.settings.research_max_retries:
                if response is not None:
                    return response
                raise FetchError("Fetch timed out") from last_error
            retry_after = _retry_after_seconds(response.headers.get("retry-after")) if response else None
            delay = retry_after if retry_after is not None else min(2**attempt, 8)
            await asyncio.sleep(delay)
            attempt += 1

    async def _response_to_result(self, response: httpx.Response) -> FetchResult:
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        allowed = content_type in {
            "text/html",
            "text/plain",
            "application/xml",
            "text/xml",
            "application/rss+xml",
            "application/pdf",
        }
        if not allowed:
            raise FetchError(f"Unsupported content type: {content_type or 'unknown'}")
        content = response.content
        if len(content) > self.settings.research_max_content_bytes:
            raise FetchError("Maximum content size exceeded")
        return FetchResult(
            final_url=str(response.url),
            status_code=response.status_code,
            content_type=content_type,
            text=response.text if content_type != "application/pdf" else "",
            bytes_read=len(content),
        )


class PlaywrightResearchFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.research_playwright_concurrency)

    async def fetch(self, url: str) -> FetchResult:
        validate_public_http_url(url)
        if not self.settings.research_playwright_enabled:
            raise RestrictedFetchError("Playwright fallback is disabled")
        async with self._semaphore:
            raise RestrictedFetchError("Playwright runtime is not bundled in Phase 3 default image")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            delta = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return None
        return max(delta, 0.0)
