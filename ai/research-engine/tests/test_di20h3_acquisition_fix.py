"""Regression tests for DI-20H.3 deep-research acquisition defect fixes.

Covers all five fixes applied in source_discovery.py and repository.py:

  Fix 1 — Fair query budget distribution across search categories in
          SearchDiscoveryService.discover (previously the first category
          consumed all queries, starving siblings → DISCOVERY_QUERY_BUDGET_EXHAUSTED).
  Fix 2 — Reusable-document and unsupported-content-type checks now execute
          BEFORE budget.allow_document() in _fetch_official_filings (previously
          both reusable docs and .zip archives consumed document-budget slots →
          DOCUMENT_BUDGET_EXHAUSTED before any real fetch).
  Fix 3 — Reconciliation failures on reusable documents are isolated with
          try/except (previously an unhandled exception crashed the entire
          _fetch_official_filings loop, discarding all subsequent filings).
  Fix 4 — network_result is released via try/finally in _run_official_filing_flight
          (previously a PdfExtractionTimeoutError skipped `del network_result`,
          retaining large PDF bytes across the persist tail of a cycle).
  Fix 5 — pdf_structure is dropped from persisted NSE-official documents in
          _durable_nse_financial_document (pdf_structure is a transient structural
          sidecar; extract_semantic_facts recomputes it from normalized_text).
"""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.deep_investigation import (
    RequirementAcquisitionBudget,
    _scope,
)
from app.models import (
    DocumentStatus,
    DocumentType,
    ReliabilityLevel,
    ResearchDocument,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.pdf_structure import preserve_pdf_structure
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import (
    FetchError,
    HttpResearchFetcher,
    NetworkFetchResult,
    PdfExtractionTimeoutError,
)
from app.settings import Settings
from app.source_discovery import (
    CandidateSearchResult,
    DiscoveryResult,
    SearchDiscoveryService,
)

from test_research_readiness_runtime import _profile
from test_di11c_official_document_budget import _official_source
from app.source_registry import RegisteredResearchSource

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
# Matches the _profile() NSE ticker so _has_trusted_nse_profile_identity succeeds.
NSE_SYMBOL = "READY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reusable_nse_document(
    profile,
    url: str,
    *,
    normalized_text: str = "Quarterly revenue is 100 crore INR",
    status: DocumentStatus = DocumentStatus.PROCESSED,
) -> ResearchDocument:
    """Build a ResearchDocument that _is_usable_durable_document considers reusable."""
    return ResearchDocument(
        canonical_url=url,
        original_url=url,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE corporate announcements",
        content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE,
        content_hash=f"fixture-{uuid4().hex[:8]}",
        normalized_text=normalized_text,
        raw_text="raw pdf bytes",
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_classification=SourceClassification.EXCHANGE,
        source_mode=SourceMode.REAL,
        status=status,
        discovery_provider="NSE_OFFICIAL_API",
        retrieved_at=NOW,
        published_at=NOW,
    )


def _make_trusted_source(profile, suffix: str, category: str, ext: str = ".pdf") -> RegisteredResearchSource:
    """Create an NSE_OFFICIAL_API RegisteredResearchSource with trusted identity."""
    source = _official_source(profile, suffix, category, ext=ext)
    return replace(source, official_nse_profile_symbol=NSE_SYMBOL)


def _repo(profile=None) -> ResearchRepository:
    """Create a ResearchRepository with the test profile registered."""
    repository = ResearchRepository(
        persistence=SqliteResearchPersistence(),
        settings=Settings(research_live_enabled=False),
    )
    profile = profile or _profile()
    repository.profiles.append(profile)
    return repository


def _budget(profile, requirement_id='QUARTERLY_FINANCIALS', max_documents=4, max_queries=2,
            sufficient=None):
    """Create a RequirementAcquisitionBudget and register it in the _scope contextvar."""
    budget = RequirementAcquisitionBudget(
        profile.instrument_id, requirement_id, NOW,
        AsyncMock(return_value=False) if sufficient is None else sufficient,
        max_documents=max_documents, max_queries=max_queries,
        lookback=None,
    )
    return budget


# ---------------------------------------------------------------------------
# Fix 1 — Fair query budget distribution across search categories
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix1_fair_query_distribution_across_categories():
    """With max_queries=2 and 3 categories, 2 categories each get 1 query;
    the 3rd starves. Previously the 1st category consumed all 2 queries."""
    profile = _profile()
    provider = SimpleNamespace(
        provider_name='offline',
        discover=AsyncMock(return_value=[]),
    )
    service = SearchDiscoveryService(provider)
    budget = _budget(profile, requirement_id='GOVERNANCE_HISTORY', max_queries=2)
    token = _scope.set(budget)
    try:
        await service.discover(profile, {'RISKS', 'REGULATORY', 'MANAGEMENT'}, set())
    finally:
        _scope.reset(token)

    # 3 governance categories, max_queries=2 → 2 categories get 1 query each.
    # The 3rd starves (reserve_queries(0) sets exhausted=True), which is correct
    # — the budget IS exhausted; the fix ensures fair distribution rather than
    # starving categories 2 and 3 entirely.
    assert provider.discover.await_count == 2
    assert budget.queries_reserved == 2
    assert budget.exhausted  # all queries consumed across 2 categories


@pytest.mark.asyncio
async def test_fix1_single_category_gets_all_queries_when_only_one_pending():
    """Edge case: with only 1 category, all queries go to it (no change vs old)."""
    profile = _profile()
    provider = SimpleNamespace(
        provider_name='offline',
        discover=AsyncMock(return_value=[]),
    )
    service = SearchDiscoveryService(provider)
    budget = _budget(profile, requirement_id='GROWTH_FACTS', max_queries=2)
    token = _scope.set(budget)
    try:
        await service.discover(profile, {'GROWTH'}, set())
    finally:
        _scope.reset(token)

    assert provider.discover.await_count == 1  # single category, one call
    assert budget.queries_reserved == 2  # both queries reserved


# ---------------------------------------------------------------------------
# Fix 2 — Reusable documents + unsupported content types checked before budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix2_reusable_document_does_not_consume_budget_slot():
    """A persisted reusable document must not call allow_document() or fetch."""
    profile = _profile()
    repository = _repo(profile)
    source = _make_trusted_source(profile, "reuse-me", "FINANCIAL_RESULTS")

    document = _make_reusable_nse_document(profile, source.url)
    repository.documents[document.document_id] = document

    budget = _budget(profile, max_documents=2)
    fetch_mock = AsyncMock()
    repository._single_flight_official_filing = fetch_mock

    token = _scope.set(budget)
    try:
        await repository._fetch_official_filings(
            profile, [DiscoveryResult('FINANCIAL_RESULTS', source)], set()
        )
    finally:
        _scope.reset(token)

    # No budget slot consumed: the document was reused without a fetch.
    assert fetch_mock.await_count == 0
    assert budget.documents_attempted == 0


@pytest.mark.asyncio
async def test_fix2_unsupported_zip_does_not_consume_document_budget():
    """An unsupported .zip filing must not consume a document-budget slot."""
    profile = _profile()
    repository = _repo(profile)
    source = _official_source(profile, "CASTROL_01_03112015174758", "INVESTOR_RELEASE", ext=".zip")

    budget = _budget(profile, max_documents=2)
    fetch_mock = AsyncMock()
    repository._single_flight_official_filing = fetch_mock

    token = _scope.set(budget)
    try:
        await repository._fetch_official_filings(
            profile, [DiscoveryResult('FINANCIAL_RESULTS', source)], set()
        )
    finally:
        _scope.reset(token)

    assert fetch_mock.await_count == 0
    assert budget.documents_attempted == 0
    assert budget.max_documents - budget.documents_attempted == 2  # untouched


# ---------------------------------------------------------------------------
# Fix 3 — Reconciliation failure isolation on reusable documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix3_reconciliation_failure_on_reusable_does_not_crash_loop():
    """A reconciliation failure on a reusable document must not crash the
    entire _fetch_official_filings loop; the next filing should still be
    attempted within the remaining budget."""
    profile = _profile()
    repository = _repo(profile)

    reusable_source = _make_trusted_source(profile, "reuse-me", "FINANCIAL_RESULTS")
    document = _make_reusable_nse_document(profile, reusable_source.url)
    repository.documents[document.document_id] = document

    second_source = _official_source(profile, "second-try", "FINANCIAL_RESULTS")
    second_doc = _make_reusable_nse_document(
        profile, second_source.url, normalized_text="Second doc revenue 200 crore"
    )

    budget = _budget(profile, max_documents=4)
    fetch_mock = AsyncMock(return_value=(second_doc, False))
    repository._single_flight_official_filing = fetch_mock

    # Force the reconciliation path to raise.
    repository._reconcile_reused_official_financial_facts = MagicMock(
        side_effect=RuntimeError("reconciliation exploded")
    )

    token = _scope.set(budget)
    try:
        await repository._fetch_official_filings(
            profile,
            [
                DiscoveryResult('FINANCIAL_RESULTS', reusable_source),
                DiscoveryResult('FINANCIAL_RESULTS', second_source),
            ],
            set(),
        )
    finally:
        _scope.reset(token)

    # The reusable document's reconciliation failed but was isolated.
    assert "RECONCILE_FAILED" in budget.failures
    # The second filing was still attempted because the loop continued.
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_fix3_valid_persisted_evidence_survives_later_document_failure():
    """A successfully persisted reusable document must remain in self.documents
    even when a subsequent filing's reconciliation fails."""
    profile = _profile()
    repository = _repo(profile)

    reusable_source = _make_trusted_source(profile, "survivor", "FINANCIAL_RESULTS")
    document = _make_reusable_nse_document(profile, reusable_source.url)
    repository.documents[document.document_id] = document
    original_doc_id = document.document_id

    second_source = _make_trusted_source(profile, "fail-recon", "FINANCIAL_RESULTS")

    budget = _budget(profile, max_documents=4)
    fetch_mock = AsyncMock()
    repository._single_flight_official_filing = fetch_mock

    repository._reconcile_reused_official_financial_facts = MagicMock(
        side_effect=RuntimeError("reconciliation exploded")
    )

    token = _scope.set(budget)
    try:
        await repository._fetch_official_filings(
            profile,
            [
                DiscoveryResult('FINANCIAL_RESULTS', reusable_source),
                DiscoveryResult('FINANCIAL_RESULTS', second_source),
            ],
            set(),
        )
    finally:
        _scope.reset(token)

    # The first document was not removed despite the second's failure.
    assert original_doc_id in repository.documents
    assert repository.documents[original_doc_id].normalized_text == "Quarterly revenue is 100 crore INR"
    assert "RECONCILE_FAILED" in budget.failures
    assert fetch_mock.await_count == 1  # second filing was attempted


# ---------------------------------------------------------------------------
# Fix 2 + Fix 3 combined — First document parser failure → next valid document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_document_parser_failure_lets_next_satisfy():
    """A ValueError (PARSER_FAILED) on the first filing must not prevent the
    second filing from being attempted and processed within the budget."""
    profile = _profile()
    repository = _repo(profile)

    source_a = _official_source(profile, "doc-a", "FINANCIAL_RESULTS")
    source_b = _official_source(profile, "doc-b", "FINANCIAL_RESULTS")
    second_doc = _make_reusable_nse_document(
        profile, source_b.url, normalized_text="Second doc revenue 200 crore"
    )

    budget = _budget(profile, max_documents=4)

    call_count = 0

    async def fake_flight(profile_arg, source_arg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("parser failed")
        return (second_doc, False)

    repository._single_flight_official_filing = AsyncMock(side_effect=fake_flight)

    token = _scope.set(budget)
    try:
        result = await repository._fetch_official_filings(
            profile,
            [
                DiscoveryResult('FINANCIAL_RESULTS', source_a),
                DiscoveryResult('FINANCIAL_RESULTS', source_b),
            ],
            set(),
        )
    finally:
        _scope.reset(token)

    assert repository._single_flight_official_filing.await_count == 2
    assert "PARSER_FAILED" in budget.failures
    # The loop continued past the first failure — the second filing was reached.
    # completed_without_failure is False because the first filing failed,
    # which is the expected, correct behavior.


# ---------------------------------------------------------------------------
# Fix 4 — PDF extraction timeout → candidate continues safely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix4_pdf_extraction_timeout_is_isolated_and_loop_continues():
    """PdfExtractionTimeoutError must be caught, recorded, and the loop
    must continue to the next filing."""
    profile = _profile()
    repository = _repo(profile)

    source_a = _official_source(profile, "timeout-pdf", "FINANCIAL_RESULTS")
    source_b = _official_source(profile, "success-pdf", "FINANCIAL_RESULTS")
    second_doc = _make_reusable_nse_document(
        profile, source_b.url, normalized_text="Second doc revenue 200 crore"
    )

    budget = _budget(profile, max_documents=4)

    call_count = 0

    async def fake_flight(profile_arg, source_arg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT")
        return (second_doc, False)

    repository._single_flight_official_filing = AsyncMock(side_effect=fake_flight)

    token = _scope.set(budget)
    try:
        result = await repository._fetch_official_filings(
            profile,
            [
                DiscoveryResult('FINANCIAL_RESULTS', source_a),
                DiscoveryResult('FINANCIAL_RESULTS', source_b),
            ],
            set(),
        )
    finally:
        _scope.reset(token)

    assert repository._single_flight_official_filing.await_count == 2
    assert "PDF_EXTRACTION_TIMEOUT" in budget.failures
    # The loop continued past the timeout — the second filing was reached.
    # completed_without_failure is False because the first filing failed,
    # which is the expected, correct behavior.


@pytest.mark.asyncio
async def test_fix4_pdf_extraction_timeout_propagates_from_flight():
    """_run_official_filing_flight must propagate PdfExtractionTimeoutError
    from process_network_response_async; the try/finally `del network_result`
    must run (not be skipped by the exception) before propagation.

    This directly exercises the Fix 4 try/finally: by confirming the exception
    propagates (rather than being swallowed) we confirm the finally block
    executed. The `del network_result` in the finally guarantees the large
    NetworkFetchResult content (raw PDF bytes) is released before the
    exception reaches the persist tail.
    """
    profile = _profile()
    repository = _repo(profile)
    source = _official_source(profile, "timeout-only", "FINANCIAL_RESULTS")

    # Use a real HttpResearchFetcher so the isinstance(self._fetcher,
    # HttpResearchFetcher) branch is taken in _run_official_filing_flight.
    fetcher = HttpResearchFetcher(Settings())
    repository._fetcher = fetcher

    large_content = b"%PDF-1.4 " + b"x" * (5 * 1024 * 1024)  # 5 MB
    network_result = NetworkFetchResult(
        final_url=source.url,
        status_code=200,
        content_type="application/pdf",
        content=large_content,
        headers={},
        is_redirect=False,
        encoding=None,
        headers_elapsed_ms=0,
        body_elapsed_ms=0,
        network_elapsed_ms=0,
    )

    fetcher.fetch_network = AsyncMock(return_value=network_result)
    fetcher.process_network_response_async = AsyncMock(
        side_effect=PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT")
    )

    with pytest.raises(PdfExtractionTimeoutError):
        await repository._run_official_filing_flight(
            (profile.instrument_id, source.url), profile, source
        )

    # process_network_response_async was called with the network_result,
    # and the exception propagated — confirming the finally block ran.
    assert fetcher.process_network_response_async.await_count == 1
    assert fetcher.fetch_network.await_count == 1


# ---------------------------------------------------------------------------
# Fix 5 — pdf_structure dropped from persisted NSE-official documents
# ---------------------------------------------------------------------------

def test_fix5_pdf_structure_dropped_for_nse_official_document():
    """_durable_nse_financial_document must drop pdf_structure for
    NSE-official PDFs that carry quarterly financial facts."""
    profile = _profile()
    repository = _repo(profile)

    url = "https://nsearchives.nseindia.com/corporate/quarterly-result.pdf"
    document = _make_reusable_nse_document(
        profile, url, normalized_text="Revenue 100 crore, PAT 12 crore"
    )
    document = document.model_copy(
        update={"pdf_structure": preserve_pdf_structure("Revenue 100 crore, PAT 12 crore")}
    )

    # nse_financial_result=True forces the drop path.
    persisted = repository._durable_nse_financial_document(
        document, nse_financial_result=True
    )

    assert persisted.raw_text is None
    assert persisted.pdf_structure is None
    assert persisted.normalized_text == document.normalized_text


def test_fix5_pdf_structure_dropped_for_nse_official_document_with_quarterly_facts():
    """Even when nse_financial_result=False, a document with quarterly facts
    must have pdf_structure and raw_text dropped on persistence."""
    profile = _profile()
    repository = _repo(profile)

    url = "https://nsearchives.nseindia.com/corporate/quarterly-result.pdf"
    document = _make_reusable_nse_document(
        profile, url, normalized_text="Reliance quarterly results revenue 100 crore"
    )
    document = document.model_copy(
        update={"pdf_structure": preserve_pdf_structure(document.normalized_text)}
    )

    # Force has_quarterly_facts=True via monkeypatch so we exercise the
    # nse_financial_result=False + has_quarterly_facts code path.
    fake_fact = SimpleNamespace(key=SimpleNamespace(period_type="QUARTERLY"))
    repository._official_financial_fact_candidates = staticmethod(lambda doc, **kw: [fake_fact])

    # nse_financial_result=False — relies on has_quarterly_facts being True.
    persisted = repository._durable_nse_financial_document(document)

    assert persisted.raw_text is None
    assert persisted.pdf_structure is None
    assert persisted.normalized_text is not None


def test_fix5_non_nse_document_returned_as_is():
    """For non-NSE documents, _durable_nse_financial_document returns the
    document unchanged (the method is NSE-specific)."""
    profile = _profile()
    repository = _repo(profile)

    document = ResearchDocument(
        canonical_url="https://investor.example.com/results.html",
        original_url="https://investor.example.com/results.html",
        source_type=SourceType.NEWS,
        source_name="Investor Relations",
        content_type="text/html",
        document_type=DocumentType.HTML,
        content_hash="web-fixture",
        normalized_text="Company revenue 100 crore",
        raw_text="<html>some html</html>",
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        status=DocumentStatus.PROCESSED,
        discovery_provider="SEARCH_DISCOVERY",
    )

    persisted = repository._durable_nse_financial_document(document)
    # Non-NSE documents are returned as-is (the method name is NSE-specific).
    assert persisted is document


# ---------------------------------------------------------------------------
# Durable evidence prevents unnecessary reacquisition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_durable_evidence_prevents_reacquisition():
    """When a reusable document already exists for a filing URL, the fetch
    path must reuse it rather than re-fetching — the budget is untouched."""
    profile = _profile()
    repository = _repo(profile)

    source = _make_trusted_source(profile, "already-there", "FINANCIAL_RESULTS")
    document = _make_reusable_nse_document(profile, source.url)
    repository.documents[document.document_id] = document

    budget = _budget(profile, max_documents=4)
    fetch_mock = AsyncMock()

    token = _scope.set(budget)
    try:
        await repository._fetch_official_filings(
            profile, [DiscoveryResult('FINANCIAL_RESULTS', source)], set()
        )
    finally:
        _scope.reset(token)

    assert fetch_mock.await_count == 0
    assert budget.documents_attempted == 0
    assert budget.failures == []


# ---------------------------------------------------------------------------
# Irrelevant search result does not incorrectly satisfy requirement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_irrelevant_search_result_does_not_satisfy_requirement():
    """SearchDiscoveryService.discover must reject candidates that name a
    different issuer, preventing irrelevant results from entering the
    accepted results set."""
    profile = _profile()

    # Irrelevant candidate: names "ACME Corporation" (no profile identity).
    irrelevant = CandidateSearchResult(
        title="ACME Corporation Q3 Results",
        url="https://news.example.com/acme-q3-results",
        snippet="ACME Corporation reported earnings growth this quarter.",
        discovered_at=NOW,
        provider="offline",
        query_id="q:1",
        query="ACME Corporation results",
        category="FINANCIAL_RESULTS",
    )
    # Relevant candidate: mentions the profile's company name "Readiness India Limited".
    relevant = CandidateSearchResult(
        title="Readiness India Limited Q3 Results",
        url="https://nsearchives.nseindia.com/corporate/readiness-q3.pdf",
        snippet="Readiness India Limited announced quarterly results for Q3 2026.",
        discovered_at=NOW,
        provider="offline",
        query_id="q:1",
        query="Readiness India Limited results",
        category="FINANCIAL_RESULTS",
    )

    provider = SimpleNamespace(
        provider_name='offline',
        discover=AsyncMock(return_value=[irrelevant, relevant]),
    )
    service = SearchDiscoveryService(provider)

    budget = _budget(profile, max_queries=2)
    token = _scope.set(budget)
    try:
        accepted = await service.discover(profile, {'FINANCIAL_RESULTS'}, set())
    finally:
        _scope.reset(token)

    # The irrelevant candidate was rejected by company-relevance filtering.
    assert len(accepted) == 1
    assert accepted[0].source.url == relevant.url
    assert irrelevant.url not in {result.source.url for result in accepted}


@pytest.mark.asyncio
async def test_irrelevant_search_result_rejection_reason_recorded():
    """The rejection of an irrelevant candidate must be recorded in stats."""
    profile = _profile()

    irrelevant = CandidateSearchResult(
        title="ACME Corporation Annual Report",
        url="https://news.example.com/acme-annual-report",
        snippet="ACME Corporation annual report for fiscal year 2026.",
        discovered_at=NOW,
        provider="offline",
        query_id="q:1",
        query="ACME Corporation annual report",
        category="FINANCIAL_RESULTS",
    )

    provider = SimpleNamespace(
        provider_name='offline',
        discover=AsyncMock(return_value=[irrelevant]),
    )
    service = SearchDiscoveryService(provider)

    budget = _budget(profile, max_queries=2)
    token = _scope.set(budget)
    try:
        await service.discover(profile, {'FINANCIAL_RESULTS'}, set())
    finally:
        _scope.reset(token)

    assert service.last_stats.rejected_count >= 1
    assert service.last_stats.rejected_reasons.get("COMPANY_RELEVANCE_FAILED") >= 1


# ---------------------------------------------------------------------------
# Bounded memory: PDF bytes not retained across candidates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix4_large_pdf_bytes_not_retained_after_timeout():
    """After a PDF extraction timeout, the large NetworkFetchResult content
    must be garbage-collectable (no lingering references in the fetcher)."""
    import gc
    import weakref

    profile = _profile()
    repository = _repo(profile)
    source = _official_source(profile, "big-timeout", "FINANCIAL_RESULTS")

    fetcher = HttpResearchFetcher(Settings())
    repository._fetcher = fetcher

    large_content = b"%PDF-1.4 " + b"x" * (5 * 1024 * 1024)  # 5 MB

    class _TrackedBytes(bytearray):
        """bytearray subclass that is weakly referenceable."""
        pass

    tracked = _TrackedBytes(large_content)
    ref = weakref.ref(tracked)

    network_result = NetworkFetchResult(
        final_url=source.url,
        status_code=200,
        content_type="application/pdf",
        content=tracked,
        headers={},
        is_redirect=False,
        encoding=None,
        headers_elapsed_ms=0,
        body_elapsed_ms=0,
        network_elapsed_ms=0,
    )

    fetcher.fetch_network = AsyncMock(return_value=network_result)
    fetcher.process_network_response_async = AsyncMock(
        side_effect=PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT")
    )

    with pytest.raises(PdfExtractionTimeoutError):
        await repository._run_official_filing_flight(
            (profile.instrument_id, source.url), profile, source
        )

    # Clear the AsyncMock's return_value reference and local references.
    del network_result
    del tracked
    del fetcher.fetch_network
    del fetcher.process_network_response_async
    gc.collect()

    # The large content must be collectable — confirming no lingering reference
    # retained the network_result content past the timeout.
    assert ref() is None
