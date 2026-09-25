"""Deterministic lifecycle tests: gated workers, no live providers or DB."""
import asyncio
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.research_fetching import HttpResearchFetcher, FetchError, PdfExtractionTimeoutError, TransportFetchError
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ResearchReadinessRuntime, CapabilityExecutionResult
from app.settings import Settings
from test_research_readiness_runtime import StateDataSource, RuntimeRepository, UpdatingExecutor, INSTRUMENT_ID
from test_research_engine import _pdf_network_fixture
from test_global_opportunity_baseline import setup_acquisition
from test_global_scanner import NOW
from test_stock_rule_engine import _readiness


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
async def test_owned_pdf_acquisition_outlives_wait_then_reassesses_committed_evidence(monkeypatch, caplog):
    started, release = threading.Event(), threading.Event()
    class Page:
        def extract_text(self):
            started.set()
            assert release.wait(2)
            return "official financial statement"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    fetcher = HttpResearchFetcher(Settings())
    source = StateDataSource({"CURRENT_NEWS"})
    class Executor(UpdatingExecutor):
        async def execute_primary(self, key, targets, **kwargs):
            assert kwargs["deadline"] is None
            extracted = await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)
            assert extracted.text.endswith("official financial statement")
            # Simulated durable commit happens only AFTER the accepted result.
            source.missing.clear()
            return CapabilityExecutionResult(("OFFICIAL_DOCUMENT",), {})
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, Executor(source), ensure_timeout_seconds=.01)
    with caplog.at_level(logging.INFO):
        monkeypatch.setattr(logging.getLogger("app.research_readiness_runtime"), "level", logging.INFO)
        monkeypatch.setattr(logging.getLogger("app.research_fetching"), "level", logging.INFO)
        owner = asyncio.create_task(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
            requirement_ids=["CURRENT_NEWS"], wait_for_completion=True))
        try:
            await until(lambda: started.is_set() and "orchestration_wait_expired" in caplog.text)
            assert not owner.done() and source.missing
            release.set()
            result = await owner
            assert result.failures == {}
            assert result.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH
            assert "readiness_re_evaluated" in caplog.text
            assert "ownership=ACCEPTED" in caplog.text
            assert "PDF_EXTRACTION_TIMEOUT" not in caplog.text
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
            await fetcher._client.aclose()
            fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_abandoned_worker_never_publishes_extracted(monkeypatch, caplog, cancel):
    started, release = threading.Event(), threading.Event()
    class Page:
        def extract_text(self):
            started.set()
            assert release.wait(2)
            return "late extracted text"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    fetcher = HttpResearchFetcher(Settings())
    with caplog.at_level(logging.INFO, logger="app.research_fetching"):
        owner = asyncio.create_task(fetcher.process_network_response_async(
            _pdf_network_fixture(), extraction_timeout_seconds=1 if cancel else .01))
        try:
            await until(started.is_set)
            if cancel:
                owner.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else PdfExtractionTimeoutError):
                await owner
            assert len(fetcher._active_pdf_extractions) == 1
            release.set()
            await until(lambda: not fetcher._active_pdf_extractions)
            assert "pdf_extraction_discarded" in caplog.text
            assert "fetch_extraction_complete " not in caplog.text
            assert "extractionStatus=EXTRACTED" not in caplog.text
            assert fetcher._pdf_extraction_semaphore._value == 1
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
            await until(lambda: not fetcher._active_pdf_extractions)
            await fetcher._client.aclose()
            fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_genuine_pdf_failure_and_network_timeout_remain_distinct(monkeypatch):
    def broken_pdf(_):
        raise ValueError("damaged PDF")
    monkeypatch.setattr("pypdf.PdfReader", broken_pdf)
    def network_timeout(request):
        raise httpx.ReadTimeout("offline simulated timeout", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(network_timeout))
    fetcher = HttpResearchFetcher(Settings(research_max_retries=0), client)
    try:
        with pytest.raises(FetchError, match="PDF_TEXT_EXTRACTION_FAILED"):
            await fetcher.process_network_response_async(_pdf_network_fixture())
        with pytest.raises(TransportFetchError, match="Fetch timed out"):
            await fetcher._request_with_retries("https://example.test/document.pdf")
        assert not fetcher._active_pdf_extractions
    finally:
        await client.aclose()
        fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_abandoned_worker_bounds_admission_without_starting_more_threads(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    class Page:
        def extract_text(self):
            calls.append(1)
            started.set()
            assert release.wait(2)
            return "text"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    fetcher = HttpResearchFetcher(Settings())
    first = asyncio.create_task(fetcher.process_network_response_async(
        _pdf_network_fixture(), extraction_timeout_seconds=.01))
    try:
        await until(started.is_set)
        with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
            await first
        with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_QUEUE_TIMEOUT"):
            await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=.01)
        assert calls == [1] and len(fetcher._active_pdf_extractions) == 1
        release.set()
        await until(lambda: not fetcher._active_pdf_extractions)
        result = await fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)
        assert result.extraction_status == "EXTRACTED" and calls == [1, 1]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await until(lambda: not fetcher._active_pdf_extractions)
        await fetcher._client.aclose()
        fetcher._pdf_extraction_executor.shutdown()


@pytest.mark.asyncio
async def test_slow_owned_plan_does_not_block_fast_plan_and_cancellation_drains_children():
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=.01)
    slow = asyncio.create_task(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"], wait_for_completion=True))
    await executor.started.wait()
    fast_source = StateDataSource({"CURRENT_NEWS"})
    fast_runtime = ResearchReadinessRuntime(RuntimeRepository(), fast_source, UpdatingExecutor(fast_source))
    fast = await fast_runtime.ensure(UUID(int=2), jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"], wait_for_completion=True)
    assert fast.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH
    assert not slow.done()
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    assert not runtime._flights
    assert source.missing == {"CURRENT_NEWS"}


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_25_deep_candidates_use_completion_not_request_budget_and_account_for_baseline(monkeypatch, caplog, incomplete):
    service, rows, _, _, tracker = setup_acquisition(monkeypatch, count=26)
    source = StateDataSource(set())
    committed = set()
    class Executor:
        async def execute_primary(self, key, targets, **kwargs):
            assert kwargs["deadline"] is None
            await asyncio.sleep(.004)  # exceeds the test request budget
            if not incomplete:
                committed.add(key)
            return CapabilityExecutionResult(("FIXTURE_COMMIT",), {})
        async def execute_approved_fallbacks(self, *args):
            return CapabilityExecutionResult()
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, Executor(), ensure_timeout_seconds=.001)
    async def durable_read(key, **kwargs):
        overrides = {} if key in committed else {"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}
        return replace(_readiness(overrides=overrides), global_instrument_id=key)
    runtime.read = durable_read
    async def ensure(key, **kwargs):
        if kwargs.get("requirement_ids") is not None:
            if key == UUID(int=26):
                raise RuntimeError("isolated baseline failure")
            return await tracker(key, **kwargs)
        assert kwargs["wait_for_completion"] is True
        return await runtime.ensure(key, jurisdiction="INDIA", requirement_ids=None, wait_for_completion=True)
    service.readiness.ensure = ensure
    with caplog.at_level(logging.INFO, logger="app.research_readiness_runtime"):
        result = await service.run(rows, as_of=NOW, shortlist_limit=25)
    assert result.shortlist_count == result.deep_attempted_count == 25
    assert result.deep_acquisition_timeout_count == 0
    assert result.rule_analyzed_count == (0 if incomplete else 25)
    assert result.deep_ready_count == (0 if incomplete else 25)
    assert result.deep_readiness_failed_count == (25 if incomplete else 0)
    assert result.suppressed_count == (26 if incomplete else 1)
    assert result.baseline_incomplete_count == 1
    assert sum(d.failure_reason == "BASELINE_ACQUISITION_FAILED" for d in result.diagnostics) == 1
    assert caplog.text.count("orchestration_wait_expired") == 25
    assert not runtime._flights
    if incomplete:
        assert result.top_n == []
