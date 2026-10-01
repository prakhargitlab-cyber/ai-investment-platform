"""Focused tests for correctness-neutral PDF extraction observability.

Scope: app.research_fetching's PDF extraction path (HttpResearchFetcher.
process_network_response / process_network_response_async) now logs
structured contentBytes/pageCount/queueWaitMs/configured-timeout/
extractionElapsedMs/RSS-before/after/delta/outcome fields for every PDF
extraction, using only stdlib facilities (/proc/self/status for current
RSS, resource.getrusage for the process-wide high-water mark) -- no new
dependency, no behavior change to concurrency, timeouts, page/marker
handling, or PdfTextStructure semantics.

Provider-free: no real NSE/Yahoo/network calls, no opportunity cycle, no
DB reset, no deploy.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from app.research_fetching import (
    FetchResult,
    HttpResearchFetcher,
    NetworkFetchResult,
    PdfExtractionTimeoutError,
    _current_rss_kb,
    _process_max_rss_kb,
)
from app.settings import Settings
from test_research_engine import _pdf_network_fixture


async def _until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


class _Page:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


# 1. Successful extraction emits pageCount/contentBytes/timing fields ------
def test_successful_extraction_logs_page_count_content_bytes_and_timing(monkeypatch, caplog):
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(
        pages=[_Page("Statement of results"), _Page("Notes to accounts")]))
    fetcher = HttpResearchFetcher(Settings())
    network = _pdf_network_fixture()

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        result = fetcher.process_network_response(network)

    assert result.page_count == 2
    assert result.bytes_read == len(network.content)
    assert result.extraction_elapsed_ms is not None

    metrics_records = [r.message for r in caplog.records if r.message.startswith("pdf_extraction_worker_metrics")]
    assert len(metrics_records) == 1
    message = metrics_records[0]
    assert f"documentBytes={len(network.content)}" in message
    assert "pageCount=2" in message
    assert "workerElapsedMs=" in message
    assert "parseOutcome=SUCCESS" in message
    # RSS is best-effort: in this sandbox /proc/self/status is available,
    # so real (non-None) values are expected, but the test does not
    # require any specific number -- only that the fields are present and,
    # when available, numeric and consistent with each other.
    assert "rssBeforeKb=" in message and "rssAfterKb=" in message and "rssDeltaKb=" in message
    if result.rss_before_kb is not None and result.rss_after_kb is not None:
        assert result.rss_delta_kb == result.rss_after_kb - result.rss_before_kb
    else:
        assert result.rss_delta_kb is None


@pytest.mark.asyncio
async def test_successful_async_extraction_logs_consolidated_fetch_extraction_complete(monkeypatch, caplog):
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[_Page("Board meeting outcome")]))
    fetcher = HttpResearchFetcher(Settings())
    network = _pdf_network_fixture()

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        result = await fetcher.process_network_response_async(network)

    assert result.page_count == 1
    complete_records = [r.message for r in caplog.records if r.message.startswith("fetch_extraction_complete")]
    assert len(complete_records) == 1
    message = complete_records[0]
    for field in (
        f"documentBytes={len(network.content)}", "pageCount=1", "extractionElapsedMs=", "queueWaitMs=",
        "configuredExtractionTimeoutSeconds=", "rssBeforeKb=", "rssAfterKb=", "rssDeltaKb=",
        "processMaxRssKb=", "extractionStatus=EXTRACTED", "outcome=SUCCESS", "ownership=ACCEPTED",
    ):
        assert field in message, f"missing {field!r} in: {message}"


# 2. Timeout behavior is unchanged -----------------------------------------
@pytest.mark.asyncio
async def test_extraction_timeout_behavior_and_semaphore_retention_unchanged(monkeypatch, caplog):
    """A slow worker still trips PDF_EXTRACTION_TIMEOUT at the configured
    bound, and the semaphore permit is NOT released until the worker
    actually finishes -- exactly as before this instrumentation slice."""
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf",
                            "late text", 10, page_count=1)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        task = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=0.05))
        await _until(worker_started.is_set)
        with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
            await task
        # The stuck worker still holds the permit -- unchanged semantics.
        assert fetcher._pdf_extraction_semaphore._value == 0
        assert len(fetcher._active_pdf_extractions) == 1
        release_worker.set()
        await _until(lambda: not fetcher._active_pdf_extractions)
        assert fetcher._pdf_extraction_semaphore._value == 1

    timeout_records = [r.message for r in caplog.records if r.message.startswith("pdf_extraction_timeout")]
    assert len(timeout_records) == 1
    assert "outcome=EXTRACTION_TIMEOUT" in timeout_records[0]


# 3. Worker finishing after caller timeout: eventual completion            #
#    instrumentation remains correct, and the ownership invariant holds ---
@pytest.mark.asyncio
async def test_worker_completion_after_timeout_reports_page_count_and_rss_without_claiming_acceptance(monkeypatch, caplog):
    release_worker = threading.Event()
    worker_started = threading.Event()

    def blocking_extract(_response, *, max_bytes=None):
        worker_started.set()
        assert release_worker.wait(timeout=3)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf",
                            "late extracted text", 10, page_count=3,
                            rss_before_kb=1000, rss_after_kb=1050, rss_delta_kb=50)

    fetcher = HttpResearchFetcher(Settings())
    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        task = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=0.05))
        await _until(worker_started.is_set)
        with pytest.raises(PdfExtractionTimeoutError):
            await task
        release_worker.set()
        await _until(lambda: not fetcher._active_pdf_extractions)

    released_records = [r.message for r in caplog.records if r.message.startswith("pdf_extraction_worker_released")]
    assert len(released_records) == 1
    message = released_records[0]
    # The actual worker elapsed time and post-extraction pageCount/RSS ARE
    # reported once the worker genuinely finishes...
    assert "extractionElapsedMs=" in message
    assert "pageCount=3" in message
    assert "rssAfterKb=1050" in message
    assert "rssDeltaKb=50" in message
    # ...but ownership/acceptance is never implied from this worker-release
    # path: only the async owner may publish an accepted completion, and
    # this call was abandoned by its caller.
    assert "extractionStatus=EXTRACTED" not in caplog.text
    assert "fetch_extraction_complete " not in caplog.text
    assert "ownership=ACCEPTED" not in caplog.text


# 4. Instrumentation failure never fails extraction -------------------------
def test_rss_measurement_failure_does_not_fail_extraction(monkeypatch, caplog):
    def _boom():
        raise OSError("simulated /proc read failure")

    monkeypatch.setattr("app.research_fetching._current_rss_kb", _boom)
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[_Page("Quarterly results")]))
    fetcher = HttpResearchFetcher(Settings())
    network = _pdf_network_fixture()

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        result = fetcher.process_network_response(network)

    # Extraction still fully succeeds; only the RSS fields degrade to None.
    assert result.extraction_status == "EXTRACTED"
    assert result.text.endswith("Quarterly results")
    assert result.page_count == 1
    assert result.rss_before_kb is None
    assert result.rss_after_kb is None
    assert result.rss_delta_kb is None
    metrics_records = [r.message for r in caplog.records if r.message.startswith("pdf_extraction_worker_metrics")]
    assert len(metrics_records) == 1
    assert "rssBeforeKb=None" in metrics_records[0] and "rssAfterKb=None" in metrics_records[0]


def test_process_max_rss_measurement_failure_does_not_fail_extraction(monkeypatch):
    monkeypatch.setattr("app.research_fetching._process_max_rss_kb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[_Page("text")]))
    fetcher = HttpResearchFetcher(Settings())

    result = fetcher.process_network_response(_pdf_network_fixture())

    assert result.extraction_status == "EXTRACTED"
    assert result.process_max_rss_kb is None


def test_extraction_error_path_is_also_instrumentation_safe(monkeypatch, caplog):
    """A genuine parse failure must still raise FetchError, even when RSS
    measurement is simultaneously broken -- instrumentation must not mask
    or replace the real error, nor itself raise in the except branch."""
    def _boom():
        raise OSError("simulated /proc read failure")

    monkeypatch.setattr("app.research_fetching._current_rss_kb", _boom)

    def _broken_reader(_content):
        raise ValueError("corrupt PDF")

    monkeypatch.setattr("pypdf.PdfReader", _broken_reader)
    fetcher = HttpResearchFetcher(Settings())

    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        with pytest.raises(Exception) as excinfo:
            fetcher.process_network_response(_pdf_network_fixture())
    assert "PDF_TEXT_EXTRACTION_FAILED" in str(excinfo.value)
    error_records = [r.message for r in caplog.records if r.message.startswith("pdf_extraction_worker_metrics")]
    assert len(error_records) == 1
    assert "parseOutcome=ERROR" in error_records[0]


# 5. Concurrency remains configured/defaulted exactly as before ------------
def test_concurrency_default_and_override_unchanged():
    assert Settings().research_pdf_extraction_concurrency == 1
    default_fetcher = HttpResearchFetcher(Settings())
    assert default_fetcher._pdf_extraction_semaphore._value == 1
    assert default_fetcher._pdf_extraction_executor._max_workers == 1

    custom_fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=2))
    assert custom_fetcher._pdf_extraction_semaphore._value == 2
    assert custom_fetcher._pdf_extraction_executor._max_workers == 2


# 6. Extraction timeout, queue timeout and concurrency defaults are each  #
#    independently configurable and untouched by this observability work #
#    (see app/settings.py's research_official_document_extraction_timeout_seconds
#    and research_pdf_extraction_queue_timeout_seconds comments) --------
def test_extraction_timeout_queue_timeout_and_concurrency_defaults_unchanged():
    settings = Settings()
    assert settings.research_official_document_extraction_timeout_seconds == 12.0
    assert settings.research_pdf_extraction_queue_timeout_seconds == 12.0
    assert settings.research_pdf_extraction_concurrency == 1


def test_current_rss_and_process_max_rss_helpers_are_sane_or_none():
    """Sanity check for the two raw (unguarded) helpers on this Linux
    sandbox -- either a positive KiB reading or, if genuinely unavailable,
    None; never a negative or nonsensical value."""
    current = _current_rss_kb()
    assert current is None or current > 0
    peak = _process_max_rss_kb()
    assert peak is None or peak > 0
