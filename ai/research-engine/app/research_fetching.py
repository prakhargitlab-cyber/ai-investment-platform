from __future__ import annotations

import asyncio
import gc
import itertools
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Awaitable, Callable, Protocol
from uuid import uuid4

import httpx
from io import BytesIO

from app.settings import Settings
from app.url_security import validate_public_http_url
from app.pdf_structure import PdfTextStructure, preserve_pdf_structure
from app import cycle_timing

logger = logging.getLogger(__name__)


def _is_pdf_content_type(raw_content_type: str | None) -> bool:
    return (raw_content_type or "").split(";")[0].strip().lower() == "application/pdf"


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
    pdf_structure: PdfTextStructure | None = None
    page_count: int | None = None
    extraction_elapsed_ms: int | None = None
    # Correctness-neutral observability only (see _current_rss_kb /
    # _process_max_rss_kb below): these never influence extraction
    # behavior, they only report on it. All None for non-PDF content and
    # whenever the underlying measurement could not be taken safely.
    rss_before_kb: int | None = None
    rss_after_kb: int | None = None
    rss_delta_kb: int | None = None
    # NOT a per-extraction peak -- see _process_max_rss_kb's docstring.
    process_max_rss_kb: int | None = None


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


# Forensic follow-up (Radar Stage-2 PDF content-size failures): the generic
# Settings.research_max_content_bytes ceiling (1.5 MB) is sized for ordinary
# HTML/XML catalyst pages, not PDFs. Several callers of HttpResearchFetcher.
# fetch() (news_acquisition.py, repository.py's shareholding/source-document
# paths) acquire PDFs through that same generic path WITHOUT passing an
# explicit max_bytes, so they silently fell back to the 1.5 MB default and
# rejected every valid PDF above it (observed: 3.66-20 MB legitimate filings
# all rejected with "Maximum content size exceeded", while sub-1.5 MB PDFs
# succeeded). This is a SEPARATE, hard-bounded ceiling that applies ONLY when
# the response content-type is application/pdf AND the caller did not already
# pass its own explicit max_bytes (the already-scoped
# research_official_document_max_bytes path -- repository.py's dedicated
# official-document fetch -- is unaffected: it always passes its own explicit
# value and is never routed through this fallback). It never changes the
# generic research_max_content_bytes limit applied to non-PDF content on the
# same call path, and it is a hard ceiling, not a configurable setting, so it
# cannot be silently raised away as a workaround.
_PDF_DOCUMENT_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB


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
        # Track the number of callers currently waiting to acquire the PDF
        # extraction semaphore (i.e. blocked on admission, not yet running).
        # This is distinct from _active_pdf_extractions (running workers) and
        # provides true queue-depth telemetry for capacity planning.
        self._queued_pdf_extractions: int = 0
        # Bounded, TTL-scoped record of URLs already known (this process) to
        # exceed a size limit, keyed by URL -> (recorded_at, content_length,
        # recorded_max_bytes). A later fetch of the SAME unchanged URL within
        # the same acquisition/cycle (and within the TTL) raises
        # DocumentSizeLimitExceeded immediately, without opening a connection
        # or reading any bytes -- an already-known oversized document is
        # never re-downloaded merely to rediscover its size. The TTL (not a
        # permanent cache) and LRU cap mirror the existing Slice 6 pattern in
        # OfficialFilingDiscovery (app/source_discovery.py), and bound this
        # strictly to a process-local, time-limited window so a future
        # legitimate retry (document identity/content changes, or simply a
        # later cycle after the TTL) is never permanently blocked.
        self._oversize_cache: "OrderedDict[str, tuple[float, int | None, int]]" = OrderedDict()
        self._oversize_cache_max = 256
        self._oversize_cache_ttl_seconds = 300.0
        # Track A — OOM mitigation: pypdf's PdfReader<->PageObject references
        # are cyclic, so CPython refcounting cannot reclaim them immediately
        # on ``del reader``. A targeted gc.collect() every N extractions breaks
        # these cycles and returns memory to the OS arena within the worker
        # thread. Unconditional gc.collect() after every extraction adds
        # ~42ms (small process) to ~260ms (large process with accumulated
        # state) per extraction -- acceptable as a fraction of the 12s timeout
        # but wasteful at the current rate of hundreds of PDFs per cycle.
        # Periodic collection (every Nth) amortizes the cost while still
        # bounding cyclic-garbage accumulation. See measurement in
        # tests/test_gc_collect_measurement.py.
        self._gc_counter = itertools.count()
        self._gc_interval = settings.research_pdf_extraction_gc_interval

    def _raise_if_known_oversize(self, url: str, max_bytes: int | None) -> None:
        if max_bytes is None:
            return
        entry = self._oversize_cache.get(url)
        if entry is None:
            return
        recorded_at, content_length, recorded_max_bytes = entry
        if time.monotonic() - recorded_at > self._oversize_cache_ttl_seconds:
            del self._oversize_cache[url]
            return
        self._oversize_cache.move_to_end(url)
        if content_length is not None:
            # An exact known Content-Length is an absolute fact: it proves
            # oversize against ANY max_bytes it exceeds, not just the one
            # recorded originally.
            if content_length > max_bytes:
                raise DocumentSizeLimitExceeded(content_length, max_bytes)
            return
        # Content-Length was absent/dishonest; the streaming limit only
        # proved the body is AT LEAST recorded_max_bytes long. That still
        # proves oversize for an equal-or-smaller current limit; a larger
        # current limit is not provably still oversize, so fall through to a
        # real (still size-bounded) fetch rather than guess.
        if max_bytes <= recorded_max_bytes:
            raise DocumentSizeLimitExceeded(None, max_bytes)

    def _record_known_oversize(self, url: str, content_length: int | None, max_bytes: int) -> None:
        self._oversize_cache[url] = (time.monotonic(), content_length, max_bytes)
        self._oversize_cache.move_to_end(url)
        while len(self._oversize_cache) > self._oversize_cache_max:
            self._oversize_cache.popitem(last=False)

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
        on_late_result: "Callable[[FetchResult | BaseException], Awaitable[None]] | None" = None,
    ) -> FetchResult:
        """Process response without blocking the event loop on PDF parsing.

        A timeout cannot stop a running Python worker thread. The permit is
        released only in that worker's done callback, so timed-out parsing
        remains globally bounded until the actual underlying work ends.

        `on_late_result`: invoked (once) with the eventual FetchResult or
        raised exception if -- and only if -- this call itself times out
        (PdfExtractionTimeoutError) while the underlying synchronous worker
        thread keeps running (see Guardian Review: late-result
        reconciliation). The caller uses this to persist a successful late
        parse instead of silently discarding it, and to know when it is safe
        to stop treating the document as still in flight. Not invoked at all
        on ordinary success or on a non-timeout failure -- the caller already
        has that outcome directly in those cases.
        """
        if getattr(response, "content_type", "") != "application/pdf":
            return self.process_network_response(response, max_bytes=max_bytes)
        queued_at = time.monotonic()
        extraction_id = uuid4()
        self._queued_pdf_extractions += 1
        logger.info(
            "pdf_extraction_queued host=%s path=%s documentBytes=%s configuredConcurrency=%s queuedExtractionCount=%s extractionId=%s",
            _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
            self.settings.research_pdf_extraction_concurrency, self._queued_pdf_extractions, extraction_id,
        )
        timeout = extraction_timeout_seconds
        if timeout is None:
            timeout = self.settings.research_official_document_extraction_timeout_seconds
        # Queue admission and the extraction itself are distinct failure
        # modes (Guardian Review Slice 4) and now use distinct, independently
        # configurable budgets: a document stuck behind other queued work is
        # not the same condition as a document whose own parse is slow/stuck,
        # and conflating them under one timeout value made the two
        # indistinguishable both in configuration and in elapsed-time
        # attribution.
        # An explicit per-call override (extraction_timeout_seconds) bounds
        # the whole operation, queueing included, exactly as before this
        # setting existed. Only when the caller has NOT overridden it does
        # queue admission fall back to its own, independently configurable
        # default -- decoupling ordinary queue saturation from the ordinary
        # extraction-timeout budget.
        queue_timeout = (
            extraction_timeout_seconds if extraction_timeout_seconds is not None
            else self.settings.research_pdf_extraction_queue_timeout_seconds
        )
        try:
            try:
                await asyncio.wait_for(self._pdf_extraction_semaphore.acquire(), timeout=queue_timeout)
            except TimeoutError as exc:
                # A stuck, non-cancellable worker must not make admission unbounded.
                # This is queue expiry, not network failure or a started extraction.
                logger.warning(
                    "pdf_extraction_queue_timeout host=%s path=%s documentBytes=%s timeoutSeconds=%s "
                    "outcome=EXTRACTION_TIMEOUT extractionId=%s",
                    _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
                    queue_timeout, extraction_id)
                raise PdfExtractionTimeoutError("PDF_EXTRACTION_QUEUE_TIMEOUT") from exc
            except asyncio.CancelledError as exc:
                # No worker task exists yet at this point -- admission to the
                # executor never completed, so nothing will ever be registered
                # to call on_late_result for this call. Tag the exception so a
                # caller (e.g. _run_official_filing_flight) that defers
                # single-flight cleanup on CancelledError -- expecting a
                # shielded worker to eventually finish and fire on_late_result --
                # can tell this apart from a cancellation that happens AFTER a
                # worker was admitted, where deferring is required. Tagging an
                # attribute does not change the exception's type, so every
                # existing except-asyncio.CancelledError handler elsewhere is
                # unaffected.
                exc._pdf_extraction_worker_never_started = True
                raise
        finally:
            self._queued_pdf_extractions -= 1
        loop = asyncio.get_running_loop()
        started_at = time.monotonic()
        # Timeout ownership matching worker lifetime: derive the worker's
        # own cooperative page-extraction deadline from the SAME `timeout`
        # value the async wait below uses to decide when THIS caller gives
        # up ownership (queue admission already happened above; this is the
        # separate extraction-execution budget). See
        # process_network_response's extraction_deadline_monotonic
        # docstring for why a cooperative, between-pages check is what
        # actually bounds a pypdf worker that cannot be forcibly cancelled.
        extraction_deadline_monotonic = started_at + timeout
        task = asyncio.ensure_future(loop.run_in_executor(
            self._pdf_extraction_executor,
            partial(
                self.process_network_response, response, max_bytes=max_bytes,
                extraction_deadline_monotonic=extraction_deadline_monotonic,
            ),
        ))
        self._active_pdf_extractions.add(task)
        abandoned = False
        discard_reported = False

        def report_discard(completed):
            nonlocal discard_reported
            if abandoned and completed.done() and not discard_reported:
                discard_reported = True
                logger.info("pdf_extraction_discarded host=%s path=%s ownership=DISCARDED extractionId=%s",
                            _safe_host(response.final_url), _safe_path(response.final_url), extraction_id)
        # Snapshot once: this is the one moment queueWaitMs means "time spent
        # waiting for a permit". Reusing _elapsed_ms(queued_at) later would
        # keep growing and no longer represent queue wait.
        queue_wait_ms = _elapsed_ms(queued_at)
        cycle_timing.record_pdf_queue_wait(queue_wait_ms)
        logger.info(
            "pdf_extraction_started host=%s path=%s documentBytes=%s queueWaitMs=%s activeExtractions=%s "
            "configuredConcurrency=%s configuredExtractionTimeoutSeconds=%s extractionId=%s",
            _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
            queue_wait_ms, len(self._active_pdf_extractions), self.settings.research_pdf_extraction_concurrency,
            timeout, extraction_id,
        )

        def release_when_done(completed: asyncio.Future[FetchResult]) -> None:
            self._active_pdf_extractions.discard(completed)
            self._pdf_extraction_semaphore.release()
            report_discard(completed)
            cycle_timing.record_pdf_parse_elapsed(_elapsed_ms(started_at))
            # Post-extraction pageCount/RSS are worker-thread facts, not a
            # claim about caller-facing acceptance -- only "fetch_extraction_complete
            # ... ownership=ACCEPTED" (below) may publish that. When the
            # caller already gave up, this is still the only place the
            # worker's own resource usage is ever observed, so log it
            # whenever the future finished cleanly (not cancelled, no
            # exception); leave both None otherwise rather than guessing.
            worker_page_count = worker_rss_after_kb = worker_rss_delta_kb = None
            if not completed.cancelled():
                # Root cause (Radar logs showing "Exception in callback ...
                # release_when_done" immediately before the already-structured
                # "pdf_extraction_failed ... outcome=EXTRACTION_ERROR" log):
                # Future.exception() never raises for a done, non-cancelled
                # future -- it just RETURNS the stored exception (or None) --
                # so the old `except Exception: pass / else: ...result()`
                # here always took the `else` branch regardless of outcome.
                # Future.result() DOES raise the stored exception, and this
                # done-callback has no enclosing try/except of its own, so
                # asyncio's event loop caught it as an unhandled callback
                # exception and logged a raw traceback for every failed
                # extraction -- duplicating, not replacing, the structured
                # FetchError/EXTRACTION_ERROR failure the caller's own
                # `except Exception as exc:` below already reports cleanly.
                # Calling .exception() still matters even when discarded: it
                # marks the exception retrieved so asyncio never separately
                # warns "Future exception was never retrieved".
                worker_exc = completed.exception()
                if worker_exc is None:
                    worker_result = completed.result()
                    worker_page_count = worker_result.page_count
                    worker_rss_after_kb = worker_result.rss_after_kb
                    worker_rss_delta_kb = worker_result.rss_delta_kb
                elif not isinstance(worker_exc, FetchError):
                    # An expected FetchError (content-size, signature,
                    # unsupported type, ...) is already a structured
                    # application failure handled by the caller -- nothing
                    # more to log here. A genuinely unexpected exception from
                    # the worker thread is not otherwise observed anywhere
                    # else, so it is still logged here, deliberately (never
                    # silently swallowed), but as a normal warning log rather
                    # than letting it escape this callback as an unhandled
                    # asyncio callback exception.
                    logger.warning(
                        "pdf_extraction_worker_unexpected_exception host=%s path=%s exception=%s",
                        _safe_host(response.final_url), _safe_path(response.final_url), type(worker_exc).__name__,
                    )
            logger.info(
                "pdf_extraction_worker_released host=%s path=%s extractionElapsedMs=%s activeExtractions=%s "
                "configuredConcurrency=%s pageCount=%s rssAfterKb=%s rssDeltaKb=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), _elapsed_ms(started_at),
                len(self._active_pdf_extractions), self.settings.research_pdf_extraction_concurrency,
                worker_page_count, worker_rss_after_kb, worker_rss_delta_kb,
            )

        task.add_done_callback(release_when_done)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            logger.info(
                "pdf_extraction_completed host=%s path=%s extractionElapsedMs=%s activeExtractions=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), _elapsed_ms(started_at),
                len(self._active_pdf_extractions),
            )
            # Only the async owner can publish accepted completion. A worker
            # thread cannot know whether its caller already abandoned the result.
            # This is the single consolidated, correlatable (extractionId)
            # record of the whole extraction for capacity-planning purposes:
            # contentBytes/pageCount/timing/RSS/outcome all together.
            logger.info(
                "fetch_extraction_complete host=%s path=%s documentBytes=%s pageCount=%s extractionElapsedMs=%s "
                "queueWaitMs=%s configuredExtractionTimeoutSeconds=%s rssBeforeKb=%s rssAfterKb=%s rssDeltaKb=%s "
                "processMaxRssKb=%s extractionStatus=%s outcome=SUCCESS ownership=ACCEPTED textBytes=%s extractionId=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), result.bytes_read, result.page_count,
                result.extraction_elapsed_ms, queue_wait_ms, timeout, result.rss_before_kb, result.rss_after_kb,
                result.rss_delta_kb, result.process_max_rss_kb, result.extraction_status, len(result.text), extraction_id)
            return result
        except TimeoutError as exc:
            abandoned = True
            report_discard(task)
            logger.warning(
                "pdf_extraction_timeout host=%s path=%s documentBytes=%s timeoutSeconds=%s queueWaitMs=%s "
                "workerElapsedMsSoFar=%s activeExtractions=%s configuredConcurrency=%s outcome=EXTRACTION_TIMEOUT "
                "extractionId=%s",
                _safe_host(response.final_url),
                _safe_path(response.final_url),
                len(response.content),
                timeout,
                queue_wait_ms,
                _elapsed_ms(started_at),
                len(self._active_pdf_extractions),
                self.settings.research_pdf_extraction_concurrency, extraction_id,
            )
            if on_late_result is not None:
                # The worker is still running (shielded); its eventual
                # success must not simply vanish because this caller gave
                # up. Deliver it once the worker truly finishes, however
                # long that takes -- this adds no new wait for anyone, it
                # only attaches a callback to a future that already exists.
                def _deliver_late_result(completed: "asyncio.Future[FetchResult]") -> None:
                    if completed.cancelled():
                        return
                    worker_exc = completed.exception()
                    value = worker_exc if worker_exc is not None else completed.result()
                    async def _run_late_callback() -> None:
                        try:
                            await on_late_result(value)
                        except Exception:
                            logger.exception(
                                "pdf_extraction_late_result_handler_failed host=%s path=%s extractionId=%s",
                                _safe_host(response.final_url), _safe_path(response.final_url), extraction_id,
                            )
                    asyncio.ensure_future(_run_late_callback())
                task.add_done_callback(_deliver_late_result)
            raise PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT") from exc
        except asyncio.CancelledError:
            abandoned = True
            report_discard(task)
            logger.info("pdf_extraction_abandoned host=%s path=%s reason=CALLER_CANCELLED extractionId=%s",
                        _safe_host(response.final_url), _safe_path(response.final_url), extraction_id)
            # Same late-result contract as TimeoutError: the shielded worker
            # thread is still running. If the caller provided on_late_result,
            # attach it as a done-callback so the eventual worker outcome is
            # not silently lost (e.g. a successful parse after the caller's
            # interactive request was cancelled). This adds no extra wait --
            # it only registers a callback on a future that already exists.
            if on_late_result is not None:
                def _deliver_late_result_cancelled(completed: "asyncio.Future[FetchResult]") -> None:
                    if completed.cancelled():
                        return
                    worker_exc = completed.exception()
                    value = worker_exc if worker_exc is not None else completed.result()
                    async def _run_late_callback_cancelled() -> None:
                        try:
                            await on_late_result(value)
                        except Exception:
                            logger.exception(
                                "pdf_extraction_late_result_handler_failed host=%s path=%s extractionId=%s",
                                _safe_host(response.final_url), _safe_path(response.final_url), extraction_id,
                            )
                    asyncio.ensure_future(_run_late_callback_cancelled())
                task.add_done_callback(_deliver_late_result_cancelled)
            raise
        except Exception as exc:
            logger.warning(
                "pdf_extraction_failed host=%s path=%s documentBytes=%s queueWaitMs=%s workerElapsedMsSoFar=%s "
                "configuredExtractionTimeoutSeconds=%s exception=%s outcome=EXTRACTION_ERROR extractionId=%s",
                _safe_host(response.final_url), _safe_path(response.final_url), len(response.content),
                queue_wait_ms, _elapsed_ms(started_at), timeout, type(exc).__name__, extraction_id)
            raise

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
            self._raise_if_known_oversize(current_url, max_bytes)
            try:
                response = await self._request_with_retries(current_url, headers=headers, max_bytes=max_bytes)
            except DocumentSizeLimitExceeded as exc:
                self._record_known_oversize(current_url, exc.content_length, exc.max_bytes)
                raise
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
        provider_started = time.monotonic()
        try:
            return await self.__request_with_retries_body(url, headers=headers, max_bytes=max_bytes)
        finally:
            cycle_timing.record_provider_elapsed((time.monotonic() - provider_started) * 1000)

    async def __request_with_retries_body(
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
                    # Only a caller that did NOT pass its own explicit ceiling
                    # (e.g. the already-scoped official-document path, which
                    # always does) falls back here -- and only a PDF response
                    # gets the dedicated document ceiling instead of silently
                    # inheriting whatever generic non-PDF default applies
                    # elsewhere; a non-PDF response on this same unscoped path
                    # is unaffected.
                    effective_max_bytes = max_bytes
                    if effective_max_bytes is None and _is_pdf_content_type(response.headers.get("content-type")):
                        effective_max_bytes = _PDF_DOCUMENT_MAX_BYTES
                    if effective_max_bytes is not None and content_length is not None and content_length > effective_max_bytes:
                        raise DocumentSizeLimitExceeded(content_length, effective_max_bytes)
                    content_parts: list[bytes] = []
                    bytes_read = 0
                    async for chunk in response.aiter_bytes():
                        bytes_read += len(chunk)
                        if effective_max_bytes is not None and bytes_read > effective_max_bytes:
                            raise DocumentSizeLimitExceeded(content_length, effective_max_bytes)
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
                    cycle_timing.record_network_elapsed(result.network_elapsed_ms)
                    if result.status_code == 200 and result.content_type == "application/pdf":
                        logger.info("radar_acquisition_count operation=pdf_downloaded count=1")
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
            cycle_timing.record_retry_backoff(delay * 1000)
            await asyncio.sleep(delay)
            attempt += 1

    def process_network_response(
        self,
        response: NetworkFetchResult,
        *,
        max_bytes: int | None = None,
        extraction_deadline_monotonic: float | None = None,
    ) -> FetchResult:
        """Parse a fetched response into FetchResult.

        `extraction_deadline_monotonic`: an optional time.monotonic() deadline
        for the PDF page-extraction loop specifically (ignored for non-PDF
        content, which is not CPU-bound the same way). pypdf has no
        cancellable/async parsing API and this method itself runs
        synchronously inside a worker thread (see
        process_network_response_async), so an external asyncio timeout
        cannot actually stop a page.extract_text() call already in
        progress -- it can only abandon the Future and move on while the
        thread keeps running. This deadline instead bounds the loop
        cooperatively, checked between pages (the only point this code can
        safely check it without changing pypdf's own parsing). Passing the
        SAME deadline the async caller already uses to decide when it gives
        up ownership keeps worker lifetime aligned with timeout ownership:
        a pathological multi-hundred-page PDF whose caller has already
        timed out stops consuming the sole extraction permit at roughly the
        same wall-clock point, instead of holding it for however long pypdf
        takes to finish every remaining page.
        """
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
        if max_bytes is not None:
            processing_max_bytes = max_bytes
        elif content_type == "application/pdf":
            processing_max_bytes = _PDF_DOCUMENT_MAX_BYTES
        else:
            processing_max_bytes = self.settings.research_max_content_bytes
        if len(content) > processing_max_bytes:
            raise FetchError("Maximum content size exceeded")
        text = content.decode(response.encoding or "utf-8", errors="replace")
        extraction_status = "EXTRACTED"
        rss_before_kb = rss_after_kb = None
        page_count: int | None = None
        if content_type == "application/pdf":
            if not content.startswith(b"%PDF-"):
                raise FetchError("PDF_SIGNATURE_INVALID")
            logger.info("fetch_pdf_signature_valid host=%s path=%s documentBytes=%s", _safe_host(response.final_url), _safe_path(response.final_url), len(content))
            started = time.monotonic()
            logger.info("fetch_extraction_start host=%s path=%s documentBytes=%s", _safe_host(response.final_url), _safe_path(response.final_url), len(content))
            document_bytes = len(content)
            # Correctness-neutral observability only -- see _current_rss_kb's
            # docstring. Measured around the blocking parse in THIS worker
            # thread since that is the only point with a reliable
            # before/after pairing; never influences extraction itself.
            rss_before_kb = _safe_instrumentation_call(_current_rss_kb)
            reader = None
            budget_exceeded = False
            total_page_count_at_budget_exceeded: int | None = None
            try:
                from pypdf import PdfReader
                from pypdf.errors import PdfReadError as _PypdfReadError
                reader = PdfReader(BytesIO(content))
                extracted_pages: list[str] = []
                for page in reader.pages:
                    # Bounded work (cooperative, between pages): see the
                    # `extraction_deadline_monotonic` docstring above --
                    # pypdf cannot be forcibly cancelled mid-parse, so this
                    # check between pages is what actually bounds the
                    # worker's lifetime for a pathological document. Stop
                    # extracting further pages rather than raising
                    # immediately so the reader is still released cleanly
                    # by the existing `finally` block below.
                    if (
                        extraction_deadline_monotonic is not None
                        and time.monotonic() > extraction_deadline_monotonic
                    ):
                        budget_exceeded = True
                        total_page_count_at_budget_exceeded = len(reader.pages)
                        break
                    extracted_pages.append(page.extract_text() or "")
                if not budget_exceeded:
                    text = "\n".join(f"[PDF_PAGE {index}]\n{page}" for index, page in enumerate(extracted_pages, start=1))
            except _PypdfReadError as exc:
                # Structural/malformed-PDF error (e.g. "Invalid object in
                # /Pages", "incorrect startxref pointer"). The PDF is
                # officially published but non-standard; extraction genuinely
                # cannot succeed. Distinguish from generic extraction
                # failures so downstream callers can classify it as a
                # parser/data-quality issue rather than a transient error.
                # Provenance (final_url, content hash) is preserved via the
                # FetchError message for durable audit.
                rss_after_kb = _safe_instrumentation_call(_current_rss_kb)
                logger.warning(
                    "fetch_pdf_extraction_failed host=%s path=%s errorType=%s",
                    _safe_host(response.final_url), _safe_path(response.final_url), type(exc).__name__,
                )
                _log_pdf_extraction_worker_metrics(
                    response, document_bytes=document_bytes, page_count=None, worker_elapsed_ms=_elapsed_ms(started),
                    rss_before_kb=rss_before_kb, rss_after_kb=rss_after_kb, parse_outcome="ERROR",
                )
                raise FetchError("PDF_PARSE_ERROR") from exc
            except Exception as exc:
                rss_after_kb = _safe_instrumentation_call(_current_rss_kb)
                logger.warning(
                    "fetch_pdf_extraction_failed host=%s path=%s errorType=%s",
                    _safe_host(response.final_url), _safe_path(response.final_url), type(exc).__name__,
                )
                _log_pdf_extraction_worker_metrics(
                    response, document_bytes=document_bytes, page_count=None, worker_elapsed_ms=_elapsed_ms(started),
                    rss_before_kb=rss_before_kb, rss_after_kb=rss_after_kb, parse_outcome="ERROR",
                )
                raise FetchError("PDF_TEXT_EXTRACTION_FAILED") from exc
            finally:
                # Release pypdf's internal structures promptly within the
                # worker thread (Track A — OOM fix). ``reader`` is the sole
                # reference to the PdfReader object; del'ing it makes it (and
                # its cyclic page↔reader graph) eligible for immediate
                # collection. Note: pypdf's PageObject↔PdfReader references
                # are cyclic, so CPython refcounting cannot free them
                # immediately on ``del`` alone -- a periodic gc.collect() is
                # needed to break the cycle (see _gc_counter below).
                #
                # NOTE: ``del content`` was previously here but is a no-op --
                # ``content`` is just a local alias of ``response.content``
                # (the NetworkFetchResult), which the caller releases via
                # ``del network_result`` in repository.py. Deleting the local
                # name does not free the underlying bytes buffer.
                if reader is not None:
                    del reader
                    # Bounded periodic GC: measurement (tests/test_gc_collect_measurement.py)
                    # showed pypdf leaves ~1,172 cyclic objects unreachable after
                    # del reader. CPython's automatic GC eventually collects these,
                    # but during sustained large-PDF processing in a long-lived
                    # thread pool the gen0 threshold may not trigger promptly enough,
                    # allowing RSS to creep upward. Running gc.collect() every Nth
                    # extraction amortizes the cost (~260ms per full collect on a
                    # process with accumulated state) across N extractions while
                    # still bounding cyclic-garbage accumulation. With
                    # research_pdf_extraction_gc_interval=4 and Stage2 concurrency=2,
                    # at most one gc.collect() overlaps any active Stage2 worker.
                    gc_count = next(self._gc_counter)
                    if gc_count % self._gc_interval == 0:
                        gc.collect()
            if budget_exceeded:
                # The caller's own ownership timeout (see
                # process_network_response_async) is derived from the same
                # extraction_timeout_seconds value as this deadline, so by
                # the time this fires the async caller has typically already
                # abandoned the result -- this is a structured, expected
                # outcome (like PDF_PARSE_ERROR), not an unexpected worker
                # exception. Partial text is deliberately discarded rather
                # than returned as a successful FetchResult: downstream only
                # distinguishes extraction_status == "EXTRACTED" from every
                # other value as FAILED (see repository.py), with no notion
                # of a partial-but-useful extraction, so claiming success
                # here would misrepresent an incomplete parse as a complete
                # one. Failing cleanly with a structured reason preserves
                # that existing contract instead of inventing new partial-
                # extraction semantics for this narrow fix.
                rss_after_kb = _safe_instrumentation_call(_current_rss_kb)
                logger.warning(
                    "fetch_pdf_extraction_budget_exceeded host=%s path=%s documentBytes=%s "
                    "pagesExtractedBeforeBudget=%s totalPages=%s",
                    _safe_host(response.final_url), _safe_path(response.final_url), document_bytes,
                    len(extracted_pages), total_page_count_at_budget_exceeded,
                )
                _log_pdf_extraction_worker_metrics(
                    response, document_bytes=document_bytes, page_count=len(extracted_pages),
                    worker_elapsed_ms=_elapsed_ms(started), rss_before_kb=rss_before_kb, rss_after_kb=rss_after_kb,
                    parse_outcome="BUDGET_EXCEEDED",
                )
                raise FetchError("PDF_EXTRACTION_BUDGET_EXCEEDED")
            if not any(page.strip() for page in extracted_pages):
                extraction_status = "PDF_SCANNED_OCR_REQUIRED"
            # extracted_pages (the list of per-page strings) is no longer
            # needed after the scanned-PDF check above. The full ``text``
            # string (built from these same pages) is already in FetchResult,
            # and preserve_pdf_structure() builds its structures from ``text``
            # (not from extracted_pages). Capture the page count we still
            # need for metrics/FetchResult, then drop the list to release its
            # memory before the FetchResult is constructed.
            page_count = len(extracted_pages)
            logger.info("radar_acquisition_count operation=pdf_parsed count=1")
            del extracted_pages
            rss_after_kb = _safe_instrumentation_call(_current_rss_kb)
            _log_pdf_extraction_worker_metrics(
                response, document_bytes=document_bytes, page_count=page_count, worker_elapsed_ms=_elapsed_ms(started),
                rss_before_kb=rss_before_kb, rss_after_kb=rss_after_kb, parse_outcome="SUCCESS",
            )
        # bytes_read is computed from response.content, which is still held
        # by the NetworkFetchResult. The caller in process_network_response_async
        # releases the NetworkFetchResult via `del network_result` in repository.py
        # (_run_official_filing_flight, line ~3090), which is the actual release
        # point for the raw PDF bytes buffer.
        bytes_read = len(response.content)
        return FetchResult(
            final_url=response.final_url,
            status_code=response.status_code,
            content_type=content_type,
            text=text,
            bytes_read=bytes_read,
            extraction_status=extraction_status,
            pdf_structure=preserve_pdf_structure(text) if content_type == "application/pdf" else None,
            page_count=page_count if content_type == "application/pdf" else None,
            extraction_elapsed_ms=_elapsed_ms(started) if content_type == "application/pdf" else None,
            rss_before_kb=rss_before_kb if content_type == "application/pdf" else None,
            rss_after_kb=rss_after_kb if content_type == "application/pdf" else None,
            rss_delta_kb=(
                rss_after_kb - rss_before_kb
                if content_type == "application/pdf" and rss_before_kb is not None and rss_after_kb is not None
                else None
            ),
            process_max_rss_kb=_safe_instrumentation_call(_process_max_rss_kb) if content_type == "application/pdf" else None,
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


# --- PDF extraction observability -------------------------------------
#
# Correctness-neutral instrumentation to gather evidence for a future
# decision on raising research_pdf_extraction_concurrency above 1. None of
# this changes extraction behavior, timeouts, or concurrency; every helper
# below is routed through _safe_instrumentation_call so a measurement
# failure can only ever produce a missing (None) value, never an
# exception that could interrupt PDF extraction.

def _current_rss_kb() -> int | None:
    """Current (not peak) resident set size in KiB for this process, read
    from /proc/self/status (Linux). This is the right measurement for a
    before/after delta -- unlike ru_maxrss (see _process_max_rss_kb) it can
    go down as well as up. Returns None when /proc is unavailable on this
    platform (e.g. non-Linux) rather than raising; callers should still
    prefer _safe_instrumentation_call for arbitrary parsing issues."""
    try:
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:      12345 kB"
                    return int(line.split()[1])
    except FileNotFoundError:
        return None
    return None


def _process_max_rss_kb() -> int | None:
    """Process-wide RSS high-water mark in KiB since process start
    (resource.getrusage(RUSAGE_SELF).ru_maxrss, already KiB on Linux).

    This is NOT a per-extraction peak: ru_maxrss only ever grows, is
    shared across every thread and every extraction the process has ever
    done, and is unaffected by memory this extraction has already freed.
    It is logged only as a separate, explicitly-named, process-lifetime
    diagnostic -- never attributed to one extraction -- because a true
    per-extraction peak cannot be sampled cheaply without a concurrent
    monitoring thread, which the task deliberately avoids introducing.
    Raises on any issue; callers must go through _safe_instrumentation_call."""
    try:
        import resource
    except ImportError:
        # The `resource` module is POSIX-only; unavailable on Windows.
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _safe_instrumentation_call(fn):
    """Run an observability helper (_current_rss_kb / _process_max_rss_kb)
    and swallow any exception it raises, returning None instead.

    Instrumentation must never fail PDF extraction: this is the one choke
    point all such measurements go through, so a broken /proc, a missing
    `resource` module (non-Linux), or any other surprise degrades to a
    missing data point rather than an extraction failure."""
    try:
        return fn()
    except Exception:
        return None


def _log_pdf_extraction_worker_metrics(
    response: NetworkFetchResult, *, document_bytes: int, page_count: int | None,
    worker_elapsed_ms: int, rss_before_kb: int | None, rss_after_kb: int | None, parse_outcome: str,
) -> None:
    """Best-effort, worker-thread-local observability for one PDF parse.

    Deliberately does NOT use the field name "extractionStatus" or claim
    "ownership=ACCEPTED": a worker thread cannot know whether its caller
    has already abandoned the result (see process_network_response_async),
    so this reports only mechanical parse facts (bytes/pages/timing/RSS),
    never caller-facing acceptance. Any failure here (e.g. a broken log
    handler) is swallowed -- it must never affect PDF extraction itself.
    """
    try:
        rss_delta_kb = (
            rss_after_kb - rss_before_kb if rss_before_kb is not None and rss_after_kb is not None else None
        )
        logger.info(
            "pdf_extraction_worker_metrics host=%s path=%s documentBytes=%s pageCount=%s workerElapsedMs=%s "
            "rssBeforeKb=%s rssAfterKb=%s rssDeltaKb=%s processMaxRssKb=%s parseOutcome=%s",
            _safe_host(response.final_url), _safe_path(response.final_url), document_bytes, page_count,
            worker_elapsed_ms, rss_before_kb, rss_after_kb, rss_delta_kb,
            _safe_instrumentation_call(_process_max_rss_kb), parse_outcome,
        )
    except Exception:
        pass
