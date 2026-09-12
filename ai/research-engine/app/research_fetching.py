from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from functools import partial

import httpx
from io import BytesIO

from app.settings import Settings
from app.url_security import validate_public_http_url

logger = logging.getLogger(__name__)


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
    extraction_status: str = "EXTRACTED"


@dataclass(frozen=True)
class NetworkFetchResult:
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    headers: dict[str, str]
    is_redirect: bool
    encoding: str | None
    headers_elapsed_ms: int
    body_elapsed_ms: int
    network_elapsed_ms: int


class FetchError(RuntimeError):
    pass


class HttpStatusFetchError(FetchError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Source returned HTTP status {status_code}")


class TransportFetchError(FetchError):
    pass


class PdfExtractionTimeoutError(FetchError):
    pass


class DocumentSizeLimitExceeded(FetchError):
    def __init__(self, content_length: int | None, max_bytes: int):
        self.content_length = content_length
        self.max_bytes = max_bytes
        super().__init__("DOCUMENT_SIZE_LIMIT_EXCEEDED")


class RestrictedFetchError(HttpStatusFetchError):
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
        # PDF parsing is CPU/memory intensive. This gate deliberately covers
        # only the blocking parse phase, never async network download.
        self._pdf_extraction_semaphore = asyncio.Semaphore(settings.research_pdf_extraction_concurrency)
        self._active_pdf_extractions: set[asyncio.Future[FetchResult]] = set()
        self._pdf_extraction_executor = ThreadPoolExecutor(
            max_workers=settings.research_pdf_extraction_concurrency,
            thread_name_prefix="research-pdf-extraction",
        )

    async def fetch(self, url: str) -> FetchResult:
        network = await self.fetch_network(url)
        return await self.process_network_response_async(network)

    async def fetch_nse_shareholding_xbrl(self, url: str) -> FetchResult:
        """Fetch one official NSE XBRL artifact with its required XML context.

        This is intentionally separate from the generic fetch path: NSE's
        archive serves these public XML artifacts only when the request looks
        like an NSE-site XML navigation.  Network validation and response
        processing remain exactly the shared secured implementation.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
            "Accept": "application/xml,text/xml,*/*",
            "Referer": "https://www.nseindia.com/",
        }
        network = await self.fetch_network(
            url,
            headers=headers,
            max_bytes=self.settings.research_max_content_bytes,
        )
        return await self.process_network_response_async(network, max_bytes=self.settings.research_max_content_bytes)

    async def process_network_response_async(
        self,
        response: NetworkFetchResult,
        *,
        max_bytes: int | None = None,
        extraction_timeout_seconds: float | None = None,
    ) -> FetchResult:
        """Process response without blocking the event loop on PDF parsing.

        A timeout cannot stop a running Python worker thread. The permit is
        released only in that worker's done callback, so timed-out parsing
        remains globally bounded until the actual underlying work ends.
        """
        if getattr(response, "content_type", "") != "application/pdf":
            return self.process_network_response(response, max_bytes=max_bytes)
        queued_at = time.monotonic()
        logger.info(
            "pdf_extraction_queued host=%s path=%s documentBytes=%s configuredConcurrency=%s",
            _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
            self.settings.research_pdf_extraction_concurrency,
        )
        await self._pdf_extraction_semaphore.acquire()
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(loop.run_in_executor(
            self._pdf_extraction_executor,
            partial(self.process_network_response, response, max_bytes=max_bytes),
        ))
        self._active_pdf_extractions.add(task)
        started_at = time.monotonic()
        logger.info(
            "pdf_extraction_started host=%s path=%s documentBytes=%s queueWaitMs=%s activeExtractions=%s configuredConcurrency=%s",
            _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
            _elapsed_ms(queued_at), len(self._active_pdf_extractions), self.settings.research_pdf_extraction_concurrency,
        )

        def release_when_done(completed: asyncio.Future[FetchResult]) -> None:
            self._active_pdf_extractions.discard(completed)
            self._pdf_extraction_semaphore.release()
            logger.info(
                "pdf_extraction_worker_released host=%s path=%s extractionElapsedMs=%s activeExtractions=%s configuredConcurrency=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), _elapsed_ms(started_at),
                len(self._active_pdf_extractions), self.settings.research_pdf_extraction_concurrency,
            )
            # Retrieve late worker exceptions after a caller timeout so they
            # do not become unobserved-task warnings.
            if not completed.cancelled():
                try:
                    completed.exception()
                except Exception:
                    pass

        task.add_done_callback(release_when_done)
        timeout = extraction_timeout_seconds
        if timeout is None:
            timeout = self.settings.research_official_document_extraction_timeout_seconds
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            logger.info(
                "pdf_extraction_completed host=%s path=%s extractionElapsedMs=%s activeExtractions=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), _elapsed_ms(started_at),
                len(self._active_pdf_extractions),
            )
            return result
        except TimeoutError as exc:
            logger.warning(
                "pdf_extraction_timeout host=%s path=%s timeoutSeconds=%s activeExtractions=%s configuredConcurrency=%s",
                _safe_host(response.final_url),
                _safe_path(response.final_url),
                timeout,
                len(self._active_pdf_extractions),
                self.settings.research_pdf_extraction_concurrency,
            )
            raise PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT") from exc

    async def fetch_network(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> NetworkFetchResult:
        logger.info("fetch_validate_url_start host=%s path=%s", _safe_host(url), _safe_path(url))
        validate_public_http_url(url)
        logger.info("fetch_validate_url_complete host=%s path=%s", _safe_host(url), _safe_path(url))
        current_url = url
        for redirect_count in range(self.settings.research_max_redirects + 1):
            response = await self._request_with_retries(current_url, headers=headers, max_bytes=max_bytes)
            if response.is_redirect:
                if redirect_count >= self.settings.research_max_redirects:
                    raise FetchError("Maximum redirects exceeded")
                location = response.headers.get("location")
                if not location:
                    raise FetchError("Redirect without Location header")
                current_url = str(httpx.URL(response.final_url).join(location))
                validate_public_http_url(current_url)
                continue
            return response
        raise FetchError("Maximum redirects exceeded")

    async def _request_with_retries(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> NetworkFetchResult:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                logger.info("fetch_http_request_start host=%s path=%s", _safe_host(url), _safe_path(url))
                async with self._client.stream("GET", url, headers=headers) as response:
                    headers_elapsed_ms = _elapsed_ms(started)
                    logger.info(
                        "fetch_http_headers_received host=%s path=%s status=%s elapsedMs=%s",
                        _safe_host(url), _safe_path(url), response.status_code, headers_elapsed_ms,
                    )
                    if response.status_code in {401, 403, 407, 451}:
                        raise RestrictedFetchError(response.status_code)
                    if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                        raise HttpStatusFetchError(response.status_code)
                    content_length = _content_length(response.headers.get("content-length"))
                    if max_bytes is not None and content_length is not None and content_length > max_bytes:
                        raise DocumentSizeLimitExceeded(content_length, max_bytes)
                    content_parts: list[bytes] = []
                    bytes_read = 0
                    async for chunk in response.aiter_bytes():
                        bytes_read += len(chunk)
                        if max_bytes is not None and bytes_read > max_bytes:
                            raise DocumentSizeLimitExceeded(content_length, max_bytes)
                        content_parts.append(chunk)
                    content = b"".join(content_parts)
                    body_elapsed_ms = _elapsed_ms(started) - headers_elapsed_ms
                    logger.info(
                        "fetch_http_body_complete host=%s path=%s status=%s bodyBytes=%s bodyElapsedMs=%s totalNetworkElapsedMs=%s",
                        _safe_host(url), _safe_path(url), response.status_code, len(content), body_elapsed_ms, _elapsed_ms(started),
                    )
                    result = NetworkFetchResult(
                        final_url=str(response.url), status_code=response.status_code,
                        content_type=response.headers.get("content-type", "").split(";")[0].lower(), content=content,
                        headers=dict(response.headers),
                        is_redirect=response.is_redirect,
                        encoding=response.encoding,
                        headers_elapsed_ms=headers_elapsed_ms, body_elapsed_ms=body_elapsed_ms,
                        network_elapsed_ms=_elapsed_ms(started),
                    )
            except httpx.TimeoutException as exception:
                response = None
                last_error: Exception | None = exception
            except httpx.TransportError as exception:
                response = None
                last_error = exception
            else:
                last_error = None
                if result.status_code not in {408, 429, 500, 502, 503, 504}:
                    return result
                response = result
            if attempt >= self.settings.research_max_retries:
                if response is not None:
                    return response
                if isinstance(last_error, httpx.TimeoutException):
                    raise TransportFetchError("Fetch timed out") from last_error
                raise TransportFetchError("Fetch transport failed") from last_error
            retry_after = _retry_after_seconds(response.headers.get("retry-after")) if response else None
            delay = retry_after if retry_after is not None else min(2**attempt, 8)
            await asyncio.sleep(delay)
            attempt += 1

    def process_network_response(self, response: NetworkFetchResult, *, max_bytes: int | None = None) -> FetchResult:
        content_type = response.content_type
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
        processing_max_bytes = max_bytes if max_bytes is not None else self.settings.research_max_content_bytes
        if len(content) > processing_max_bytes:
            raise FetchError("Maximum content size exceeded")
        text = content.decode(response.encoding or "utf-8", errors="replace")
        extraction_status = "EXTRACTED"
        if content_type == "application/pdf":
            if not content.startswith(b"%PDF-"):
                raise FetchError("PDF_SIGNATURE_INVALID")
            logger.info("fetch_pdf_signature_valid host=%s path=%s documentBytes=%s", _safe_host(response.final_url), _safe_path(response.final_url), len(content))
            started = time.monotonic()
            logger.info("fetch_extraction_start host=%s path=%s documentBytes=%s", _safe_host(response.final_url), _safe_path(response.final_url), len(content))
            try:
                from pypdf import PdfReader
                reader = PdfReader(BytesIO(content))
                extracted_pages = [page.extract_text() or "" for page in reader.pages]
                text = "\n".join(f"[PDF_PAGE {index}]\n{page}" for index, page in enumerate(extracted_pages, start=1))
            except Exception as exc:
                raise FetchError("PDF_TEXT_EXTRACTION_FAILED") from exc
            if not any(page.strip() for page in extracted_pages):
                extraction_status = "PDF_SCANNED_OCR_REQUIRED"
            logger.info(
                "fetch_extraction_complete host=%s path=%s pageCount=%s extractionElapsedMs=%s extractionStatus=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), len(extracted_pages), _elapsed_ms(started), extraction_status,
            )
        return FetchResult(
            final_url=response.final_url,
            status_code=response.status_code,
            content_type=content_type,
            text=text,
            bytes_read=len(content),
            extraction_status=extraction_status,
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
            delta = parsedate_to_datetime(value) - datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return None
        return max(delta.total_seconds(), 0.0)


def _safe_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _safe_path(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).path or "/"
    except ValueError:
        return "/"


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
