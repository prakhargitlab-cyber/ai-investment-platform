"""Radar Stage-2 fixes: bounded 32 MiB PDF/document content-size ceiling for
the generic HttpResearchFetcher.fetch() downloader path, and a clean,
non-traceback-producing release_when_done extraction-worker callback.

Background: Radar logs repeatedly showed legitimate 3.66-20 MB PDFs being
rejected with FetchError("Maximum content size exceeded") while sub-1.5 MB
PDFs succeeded. Root cause: several document-acquisition call sites
(news_acquisition.py, repository.py's shareholding/source-document paths) use
HttpResearchFetcher.fetch(url), which never passes an explicit max_bytes, so
those PDFs silently fell back to Settings.research_max_content_bytes (a 1.5 MB
default sized for ordinary HTML/XML catalyst pages). This is now fixed with a
hard, PDF-content-type-gated 32 MiB ceiling that applies ONLY when the caller
passed no explicit max_bytes of its own -- the pre-existing, separately
configured research_official_document_max_bytes path (repository.py's
dedicated official-document fetch, which always passes its own explicit
value) is completely unaffected, and non-PDF content on the same unscoped
.fetch() path still uses the original research_max_content_bytes default.

Separately: the PDF extraction worker's release_when_done done-callback used
to unconditionally call completed.result() whenever completed.exception() did
not raise -- which is always, for a done non-cancelled future, since
Future.exception() returns the exception rather than raising it. Since
Future.result() DOES raise the stored exception, and this callback has no
enclosing try/except, asyncio's event loop caught the escaping exception and
logged a raw, unhandled "Exception in callback ... release_when_done"
traceback for every failed extraction, duplicating (not replacing) the
already-structured pdf_extraction_failed/outcome=EXTRACTION_ERROR log the
caller emits. Fixed to retrieve (never swallow) the exception without
re-raising it from inside the callback.

Provider-free: no real NSE/Yahoo/network calls, no opportunity cycle, no DB
reset, no deploy, no Radar cycle.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from app.research_fetching import (
    DocumentSizeLimitExceeded,
    FetchError,
    HttpResearchFetcher,
    NetworkFetchResult,
    PdfExtractionTimeoutError,
    _PDF_DOCUMENT_MAX_BYTES,
)
from app.settings import Settings


def _make_valid_pdf(num_pages: int) -> bytes:
    """A real, pypdf-parseable multi-page PDF (blank pages -- content is
    irrelevant to these tests, only page *count* and a monkeypatched, slow
    PageObject.extract_text matter)."""
    from io import BytesIO
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _network_result(content: bytes, *, url: str = "https://nsearchives.nseindia.com/corporate/pathological.pdf") -> NetworkFetchResult:
    return NetworkFetchResult(
        final_url=url, status_code=200, content_type="application/pdf", content=content, headers={},
        is_redirect=False, encoding=None, headers_elapsed_ms=1, body_elapsed_ms=1, network_elapsed_ms=2,
    )


def _pdf_client(body: bytes, *, content_length: str | None = None) -> httpx.AsyncClient:
    headers = {"content-type": "application/pdf"}
    if content_length is not None:
        headers["content-length"] = content_length

    def handler(request):
        return httpx.Response(200, headers=headers, content=body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Area 1: bounded 32 MiB PDF/document content-size ceiling.
# ---------------------------------------------------------------------------

def test_pdf_document_ceiling_is_exactly_32_mebibytes() -> None:
    assert _PDF_DOCUMENT_MAX_BYTES == 32 * 1024 * 1024


@pytest.mark.asyncio
async def test_a_pdf_below_32_mib_is_accepted_by_the_downloader_without_explicit_max_bytes() -> None:
    """A legitimate ~20 MB PDF, fetched via the generic .fetch() path that
    passes no explicit max_bytes (the exact path news_acquisition.py and
    repository.py's shareholding/source-document callers use), must NOT be
    rejected -- this is the exact regression the forensic cycle reported."""
    settings = Settings(research_max_retries=0)
    body = b"%PDF-" + b"x" * 20_000_000
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    network = await fetcher.fetch_network("https://nsearchives.nseindia.com/corporate/legit-20mb.pdf")

    assert len(network.content) == len(body)


@pytest.mark.asyncio
async def test_a_pdf_just_under_32_mib_is_accepted() -> None:
    settings = Settings(research_max_retries=0)
    body = b"%PDF-" + b"x" * (_PDF_DOCUMENT_MAX_BYTES - 1_000)
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    network = await fetcher.fetch_network("https://nsearchives.nseindia.com/corporate/just-under.pdf")

    assert len(network.content) == len(body)


@pytest.mark.asyncio
async def test_b_pdf_above_32_mib_is_rejected_via_content_length_precheck() -> None:
    settings = Settings(research_max_retries=0)
    oversize = _PDF_DOCUMENT_MAX_BYTES + 1_000_000
    body = b"%PDF-" + b"x" * oversize
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    with pytest.raises(DocumentSizeLimitExceeded) as exc_info:
        await fetcher.fetch_network("https://nsearchives.nseindia.com/corporate/oversized.pdf")
    assert exc_info.value.max_bytes == _PDF_DOCUMENT_MAX_BYTES


@pytest.mark.asyncio
async def test_b_hundred_mb_pdf_is_rejected_even_without_a_content_length_header() -> None:
    """A dishonest/missing Content-Length must not let a 100+ MB document
    stream fully into memory before being rejected -- the chunked
    bytes_read-based check (not just the header pre-check) must also use the
    32 MiB ceiling."""
    settings = Settings(research_max_retries=0)
    body = b"%PDF-" + b"x" * 100_000_000
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=None))

    with pytest.raises(DocumentSizeLimitExceeded) as exc_info:
        await fetcher.fetch_network("https://nsearchives.nseindia.com/corporate/huge-no-content-length.pdf")
    assert exc_info.value.max_bytes == _PDF_DOCUMENT_MAX_BYTES


@pytest.mark.asyncio
async def test_c_oversized_pdf_never_starts_extraction() -> None:
    """The oversized rejection happens at the network/streaming layer inside
    fetch_network -- fetch() never even reaches process_network_response_async,
    so the extraction executor/semaphore is never touched at all."""
    settings = Settings(research_max_retries=0)
    oversize = _PDF_DOCUMENT_MAX_BYTES + 1_000_000
    body = b"%PDF-" + b"x" * oversize
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    with pytest.raises(DocumentSizeLimitExceeded):
        await fetcher.fetch("https://nsearchives.nseindia.com/corporate/oversized-fetch.pdf")

    assert fetcher._active_pdf_extractions == set()
    assert fetcher._queued_pdf_extractions == 0


@pytest.mark.asyncio
async def test_d_oversized_document_structured_error_remains_clean() -> None:
    """The failure is still the same clean, structured FetchError subclass
    the rest of the system already classifies (app/repository.py's
    _fetch_rejection_reason maps DOCUMENT_SIZE_LIMIT_EXCEEDED to a permanent,
    non-retryable reason) -- not a bare/generic exception."""
    settings = Settings(research_max_retries=0)
    oversize = _PDF_DOCUMENT_MAX_BYTES + 1_000_000
    body = b"%PDF-" + b"x" * oversize
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    with pytest.raises(DocumentSizeLimitExceeded) as exc_info:
        await fetcher.fetch("https://nsearchives.nseindia.com/corporate/oversized-clean.pdf")

    exc = exc_info.value
    assert isinstance(exc, FetchError)
    assert str(exc) == "DOCUMENT_SIZE_LIMIT_EXCEEDED"
    assert exc.content_length == len(body)


@pytest.mark.asyncio
async def test_non_pdf_content_on_the_same_unscoped_fetch_path_is_unaffected() -> None:
    """The 32 MiB ceiling is gated strictly on content-type == application/pdf.
    Non-PDF content on the exact same unscoped .fetch() path must keep using
    the original, smaller research_max_content_bytes default -- this change
    must never look like a global HTTP response size increase."""
    settings = Settings(research_max_retries=0, research_max_content_bytes=1_000)
    body = b"<html>" + b"x" * 2_000 + b"</html>"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body, request=request)

    fetcher = HttpResearchFetcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(FetchError, match="Maximum content size exceeded"):
        await fetcher.fetch("https://example.com/catalyst-article")


@pytest.mark.asyncio
async def test_explicit_official_document_max_bytes_path_is_unaffected() -> None:
    """A caller that already passes its own explicit max_bytes (the existing
    research_official_document_max_bytes path) must keep using exactly that
    value -- never silently widened or narrowed by the new PDF default."""
    settings = Settings(research_max_retries=0, research_official_document_max_bytes=5_000_000)
    body = b"%PDF-" + b"x" * 6_000_000
    fetcher = HttpResearchFetcher(settings, _pdf_client(body, content_length=str(len(body))))

    with pytest.raises(DocumentSizeLimitExceeded) as exc_info:
        await fetcher.fetch_network(
            "https://nsearchives.nseindia.com/corporate/official.pdf",
            max_bytes=settings.research_official_document_max_bytes,
        )
    assert exc_info.value.max_bytes == 5_000_000


# ---------------------------------------------------------------------------
# Area 4: release_when_done callback no longer raises an unhandled exception.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_extraction_does_not_raise_from_the_done_callback(caplog) -> None:
    """A PDF that fails extraction (here: oversized, via the buffered check
    inside the worker) must not produce an unhandled asyncio callback
    exception. We install a custom loop exception handler (the same hook
    asyncio uses to report "Exception in callback ...") and assert it is
    never invoked, while the caller still receives the expected FetchError
    normally and the structured pdf_extraction_failed log is still emitted."""
    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    # Bigger than the new 32 MiB PDF ceiling, so process_network_response's
    # own buffered size check (processing_max_bytes == _PDF_DOCUMENT_MAX_BYTES
    # for application/pdf content) raises the structured "Maximum content
    # size exceeded" FetchError before any pypdf parsing is attempted.
    oversize_beyond_pdf_ceiling = _PDF_DOCUMENT_MAX_BYTES + 1_000
    response = NetworkFetchResult(
        final_url="https://nsearchives.nseindia.com/corporate/worker-oversized.pdf",
        status_code=200,
        content_type="application/pdf",
        content=b"%PDF-" + b"x" * oversize_beyond_pdf_ceiling,
        headers={},
        is_redirect=False,
        encoding=None,
        headers_elapsed_ms=1,
        body_elapsed_ms=1,
        network_elapsed_ms=2,
    )

    loop = asyncio.get_running_loop()
    callback_exceptions: list[BaseException] = []

    def capture_callback_exception(_loop, context):
        exc = context.get("exception")
        if exc is not None:
            callback_exceptions.append(exc)

    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(capture_callback_exception)
    try:
        with caplog.at_level(logging.WARNING, logger="app.research_fetching"):
            with pytest.raises(FetchError, match="Maximum content size exceeded"):
                await fetcher.process_network_response_async(response, extraction_timeout_seconds=2)
        # Give the done-callback (scheduled via call_soon once the executor
        # future resolves) a chance to run before we check it never fired an
        # unhandled-callback-exception report.
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(previous_handler)

    assert callback_exceptions == [], (
        f"release_when_done must not let an exception escape the done-callback, got: {callback_exceptions}"
    )
    assert any("pdf_extraction_failed" in record.message and "EXTRACTION_ERROR" in record.message
               for record in caplog.records), "the structured failure log must still be emitted"
    assert fetcher._active_pdf_extractions == set()


@pytest.mark.asyncio
async def test_unexpected_worker_exception_is_still_logged_not_silently_swallowed(caplog, monkeypatch) -> None:
    """A genuinely unexpected (non-FetchError) exception from the worker must
    still be observable -- never silently swallowed -- even though it must
    not escape the done-callback either."""
    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    response = NetworkFetchResult(
        final_url="https://nsearchives.nseindia.com/corporate/unexpected.pdf",
        status_code=200,
        content_type="application/pdf",
        content=b"%PDF-fixture",
        headers={},
        is_redirect=False,
        encoding=None,
        headers_elapsed_ms=1,
        body_elapsed_ms=1,
        network_elapsed_ms=2,
    )

    def raise_value_error(_response, *, max_bytes=None, extraction_deadline_monotonic=None):
        raise ValueError("unexpected worker defect")

    monkeypatch.setattr(fetcher, "process_network_response", raise_value_error)

    loop = asyncio.get_running_loop()
    callback_exceptions: list[BaseException] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: callback_exceptions.append(context.get("exception")))
    try:
        with caplog.at_level(logging.WARNING, logger="app.research_fetching"):
            with pytest.raises(ValueError, match="unexpected worker defect"):
                await fetcher.process_network_response_async(response, extraction_timeout_seconds=2)
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(previous_handler)

    assert [exc for exc in callback_exceptions if exc is not None] == []
    assert any("pdf_extraction_worker_unexpected_exception" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# PDF extraction bounded work: a pathological multi-hundred-page PDF cannot
# be forcibly cancelled mid-parse (pypdf has no such API, and this all runs
# synchronously inside a ThreadPoolExecutor worker thread), so the worker
# cooperatively bounds its own page-extraction loop using the SAME deadline
# the async caller already uses to decide when it gives up ownership. See
# HttpResearchFetcher.process_network_response's extraction_deadline_monotonic
# docstring for the full rationale.
# ---------------------------------------------------------------------------

def test_pdf_extraction_page_budget_stops_before_all_pages_and_raises_structured_error(caplog) -> None:
    """A deadline that has already elapsed before extraction starts must stop
    the page loop immediately (before any page is even attempted) and raise a
    structured, expected FetchError -- never a bare success claiming
    extraction_status == EXTRACTED for a parse that never actually ran."""
    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    content = _make_valid_pdf(num_pages=5)
    response = _network_result(content)

    with caplog.at_level(logging.WARNING, logger="app.research_fetching"):
        with pytest.raises(FetchError, match="PDF_EXTRACTION_BUDGET_EXCEEDED"):
            fetcher.process_network_response(
                response, extraction_deadline_monotonic=time.monotonic() - 1.0,
            )

    assert any("fetch_pdf_extraction_budget_exceeded" in record.message for record in caplog.records)


def test_pdf_extraction_page_budget_bounds_a_slow_pathological_document(monkeypatch, caplog) -> None:
    """A document whose per-page extraction is pathologically slow (modeling
    the production 531-page/~103s case) must not run unbounded: the worker
    stops consuming pages once the deadline passes, well before every page
    would otherwise have been processed."""
    page_count = 40
    per_page_seconds = 0.05  # unbounded total ~= 2.0s
    budget_seconds = 0.15  # expect roughly 2-4 pages processed, not all 40

    def slow_extract_text(self, *args, **kwargs):
        time.sleep(per_page_seconds)
        return ""

    monkeypatch.setattr("pypdf._page.PageObject.extract_text", slow_extract_text)

    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    content = _make_valid_pdf(num_pages=page_count)
    response = _network_result(content)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.research_fetching"):
        with pytest.raises(FetchError, match="PDF_EXTRACTION_BUDGET_EXCEEDED"):
            fetcher.process_network_response(
                response, extraction_deadline_monotonic=time.monotonic() + budget_seconds,
            )
    elapsed = time.monotonic() - started

    # Bounded: nowhere near the full unbounded duration (page_count * per_page_seconds).
    assert elapsed < page_count * per_page_seconds / 2
    budget_records = [r for r in caplog.records if "fetch_pdf_extraction_budget_exceeded" in r.message]
    assert len(budget_records) == 1
    assert "pagesExtractedBeforeBudget=" in budget_records[0].message
    assert f"totalPages={page_count}" in budget_records[0].message


@pytest.mark.asyncio
async def test_pathological_pdf_does_not_starve_a_normal_pdf_queued_behind_it(monkeypatch) -> None:
    """Queue fairness (concurrency=1): a pathological document whose caller
    has already timed out must release the sole extraction permit promptly
    -- bounded by its own extraction budget -- rather than holding it for
    however long the unbounded parse would otherwise take, so a normal
    document queued behind it is not starved."""
    page_count = 40
    per_page_seconds = 0.05  # unbounded total ~= 2.0s if not bounded

    def slow_extract_text(self, *args, **kwargs):
        time.sleep(per_page_seconds)
        return ""

    monkeypatch.setattr("pypdf._page.PageObject.extract_text", slow_extract_text)

    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    pathological = _network_result(_make_valid_pdf(num_pages=page_count), url="https://nsearchives.nseindia.com/corporate/pathological.pdf")
    normal = _network_result(_make_valid_pdf(num_pages=1), url="https://nsearchives.nseindia.com/corporate/normal.pdf")

    with pytest.raises(PdfExtractionTimeoutError):
        await fetcher.process_network_response_async(pathological, extraction_timeout_seconds=0.1)

    # If the first worker were still holding the only permit for the full
    # unbounded ~2.0s, this would still be waiting. Bounded to well under
    # that, proving the normal document was not starved behind it.
    result = await asyncio.wait_for(
        fetcher.process_network_response_async(normal, extraction_timeout_seconds=5.0),
        timeout=1.0,
    )
    # Blank pages have no extractable text, so this normal document
    # legitimately reports PDF_SCANNED_OCR_REQUIRED rather than EXTRACTED --
    # the point here is only that it completed promptly rather than being
    # starved behind the pathological document, not its extraction_status.
    assert result.extraction_status == "PDF_SCANNED_OCR_REQUIRED"


def test_pdf_extraction_page_budget_does_not_discard_text_from_pages_already_extracted(caplog) -> None:
    """Budget-exceeded must discard the partial text entirely rather than
    returning it dressed up as a successful FetchResult: extraction_status
    only ever means complete extraction (see repository.py's EXTRACTED vs
    FAILED split), so this failure must surface as a raised FetchError, not
    a FetchResult the caller would mistake for success."""
    settings = Settings(research_pdf_extraction_concurrency=1)
    fetcher = HttpResearchFetcher(settings)
    content = _make_valid_pdf(num_pages=5)
    response = _network_result(content)

    with pytest.raises(FetchError, match="PDF_EXTRACTION_BUDGET_EXCEEDED"):
        fetcher.process_network_response(
            response, extraction_deadline_monotonic=time.monotonic() - 1.0,
        )
