"""Regression tests for the FINAL Stage-2 correctness pass fixes.

Covers:
  - A. SHAREHOLDING path-B: Yahoo MCP is never invoked for SHAREHOLDING and the
    final aggregate gap is EVIDENCE_INSUFFICIENT_WITHIN_PLAN (not leaked
    EXTERNAL_CAPABILITY_UNSUPPORTED). Covered by test_slice2_shareholding_nse_first_authority.py.
  - B. repository.py: _market_session_cache AttributeError on partial-instance
    construction (boundary tests via __new__).
  - C. repository.py: _financial_document_parse_identity includes normalized_text
    + content_hash so textual document changes invalidate the parse receipt.
  - D. repository.py: _category_evidence_timing OFFICIAL_NSE-QUARTERLY-fact fallback
    so fresh persisted facts establish a freshness window even when the in-memory
    document cache lacks an NSE-authority document.
  - E. repository.py: _qualifying_category_evidence OFFICIAL_NSE-QUARTERLY-fact
    fallback so a fresh persisted fact makes the FINANCIAL_RESULTS category fresh.
  - F. repository.py: _quarterly_window_open freshness gate -- a quarterly-window-open
    signal must not re-open acquisition when qualifying evidence was retrieved
    in/after the new quarter (only evidence that predates the window, or absent
    evidence, forces a re-check).
  - G. repository.py: cross-batch host-transport cooldown arms on a transport
    failure (Issue 3) and blocks only NEW filings on the failed host, while a
    per-refresh attempt-budget retry of the same source URL is still permitted
    (the contract that lets future_refresh_can_retry coexist with cross-batch
    host-failure budgeting).
  - H. repository.py: queue-timeout retry respects the per-refresh attempt budget so
    a bounded retry cannot push a single document past max_attempts_per_refresh.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)
from app.models import (
    DocumentStatus,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchDocument,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.normalization import canonicalize_url, content_hash
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings


def _instrument_id() -> UUID:
    return UUID("44444444-4444-4444-4444-444444444444")


def _quarterly_fact(repository, instrument_id, period_end, retrieved_at, source_identity="doc-regression"):
    repository._persistence.upsert_financial_fact(FinancialFact(
        FinancialFactKey(instrument_id, "revenue", period_end, "QUARTERLY", None),
        ProvenancedValue(
            value=Decimal("100"), unit="INR crore",
            source_url=f"https://nse.example/{source_identity}.pdf",
            source_name="NSE", source_type="EXCHANGE_ANNOUNCEMENT",
            retrieved_at=retrieved_at, confidence=None,
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", source_identity, SourceMode.REAL,
    ))


def test_repository_partial_construction_invalidate_market_session_cache_is_noop():
    """Boundary construction (repository.__new__ bypasses __init__) must not
    raise AttributeError on _market_session_cache invalidation/clear."""
    repository = ResearchRepository.__new__(ResearchRepository)
    repository._persistence = SqliteResearchPersistence()
    # _invalidate_market_session_cache must be a safe no-op without __init__.
    repository._invalidate_market_session_cache()
    # And the persistence-write guard still recognizes write operations when
    # given the actual method object (the same classification _run_blocking_persistence
    # uses to decide whether to invalidate caches).
    assert repository._is_persistence_write(repository.upsert_daily_market_bar_async)


def test_parse_identity_invalidated_by_normalized_text_change():
    """A textual change to a ResearchDocument must change the financial-parse
    receipt identity, even though normalized_text is excluded from
    model_dump() (Pydantic exclude=True)."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    document_a = ResearchDocument(
        canonical_url=canonicalize_url("https://nse.example/r.pdf"),
        original_url="https://nse.example/r.pdf", title="R",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE", publisher="NSE", content_type="text/html",
        document_type="HTML",
        normalized_text="Quarterly results revenue 100 profit 10",
        content_hash=content_hash("Quarterly results revenue 100 profit 10"),
        status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_A,
    )
    document_b = document_a.model_copy(update={"normalized_text": "Quarterly results revenue 100 profit 90 CRAR 20"})
    id_a = repository._financial_document_parse_identity(document_a, [])
    id_b = repository._financial_document_parse_identity(document_b, [])
    assert id_a != id_b


def test_category_evidence_timing_uses_persisted_official_nse_quarterly_fact():
    """_category_evidence_timing returns durable timing from a persisted
    OFFICIAL_NSE QUARTERLY fact even when the document is not resident."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = repository.profiles[0].instrument_id
    retrieved = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    _quarterly_fact(repository, instrument_id, "2026-06-30", retrieved)
    timing = repository._category_evidence_timing(instrument_id, "FINANCIAL_RESULTS")
    assert timing[0] is not None
    assert timing[1] is not None


def test_persisted_official_nse_fact_makes_category_fresh():
    """A fresh persisted OFFICIAL_NSE QUARTERLY fact makes FINANCIAL_RESULTS
    fresh even with no qualifying document in the in-memory cache."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = repository.profiles[0].instrument_id
    now = datetime.now(timezone.utc)
    repository._category_refresh[(instrument_id, "FINANCIAL_RESULTS")] = now
    _quarterly_fact(repository, instrument_id, "2026-06-30", now)
    assert repository._category_is_fresh(instrument_id, "FINANCIAL_RESULTS", now)


def test_quarterly_window_does_not_force_recheck_when_evidence_retrieved_in_new_quarter():
    """Evidence retrieved in/after the new quarter suppresses the quarterly-window
    re-check; the window only forces recheck when evidence predates the new quarter."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = repository.profiles[0].instrument_id
    retrieved = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    _quarterly_fact(repository, instrument_id, "2026-06-30", retrieved)
    repository._mark_qualifying_categories_fresh(instrument_id, {"FINANCIAL_RESULTS"}, now=retrieved)
    now = retrieved
    assert not repository._quarterly_window_open(instrument_id, "FINANCIAL_RESULTS", now)
    assert "FINANCIAL_RESULTS" not in repository._missing_categories(
        repository.profiles[0], set(), now, {"FINANCIAL_RESULTS"},
    )


def test_quarterly_window_forces_recheck_when_evidence_predates_new_quarter():
    """Counterpart: evidence retrieved BEFORE the new quarter opened (stale
    relative to the window) keeps the window forcing a re-check."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = repository.profiles[0].instrument_id
    retrieved = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    _quarterly_fact(repository, instrument_id, "2026-06-30", retrieved)
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert repository._quarterly_window_open(instrument_id, "FINANCIAL_RESULTS", now)
    missing = repository._missing_categories(
        repository.profiles[0], set(), now, {"FINANCIAL_RESULTS"},
    )
    assert "FINANCIAL_RESULTS" in missing


def test_shareholding_final_gap_classifies_as_evidence_unavailable_not_technical():
    """The aggregate SHAREHOLDING failure after path-B (Yahoo unsupported + NSE
    document fallback exhausted) is EVIDENCE_INSUFFICIENT_WITHIN_PLAN, which
    classify_requirement_failures maps to EVIDENCE_UNAVAILABLE (permanent,
    NOT retried) -- never a leaked EXTERNAL_CAPABILITY_UNSUPPORTED."""
    assert classify_reason("EVIDENCE_INSUFFICIENT_WITHIN_PLAN") == EVIDENCE_UNAVAILABLE
    assert classify_reason("EXTERNAL_CAPABILITY_UNSUPPORTED") == EVIDENCE_UNAVAILABLE
    result = classify_requirement_failures(
        {"SHAREHOLDING": "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"}, ("SHAREHOLDING",)
    )
    assert result == EVIDENCE_UNAVAILABLE
    # A genuine transient stays retryable.
    assert classify_reason("NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_transient_transport_failure_arms_cross_batch_cooldown_but_same_url_can_retry():
    """A single TransportFetchError arms the cross-batch host cooldown (Issue 3:
    don't hammer a failed host with NEW filings in the next batch) -- the
    cooldown map must be non-empty after one failure. The cooldown blocks
    only NEW (unseen) URLs on the failed host, so a retry of the same source
    URL (governed by the per-refresh attempt budget) is still permitted,
    while a different source on the same host within the cooldown window is
    skipped (per test_nse_host_failure_budget_cross_batch.py)."""
    from app.research_fetching import HttpResearchFetcher, TransportFetchError
    from app.source_discovery import DiscoveryResult

    class FailingFetcher(HttpResearchFetcher):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls = 0

        async def fetch_network(self, _url, **_kwargs):
            self.calls += 1
            raise TransportFetchError("Fetch timed out")

    settings = Settings(research_live_enabled=True)
    fetcher = FailingFetcher(settings)
    repository = ResearchRepository(settings=settings, fetcher=fetcher)
    profile = next(p for p in repository.profiles if p.ticker == "RELIANCE")
    source = _make_official_source(profile, "single-transport")

    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set(),
    )
    assert fetcher.calls == 1
    # A transport failure MUST arm the cross-batch host cooldown (Issue 3).
    key = (profile.instrument_id, "nsearchives.nseindia.com")
    assert key in repository._official_host_cooldowns
    assert source.url in repository._official_host_failed_urls.get(key, set())

    # Same source URL retry is exempt from the cross-batch cooldown (per-refresh
    # attempt budget governs it), so a 2nd call for the same source re-tries.
    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set(),
    )
    assert fetcher.calls == 2


def _make_official_source(profile, suffix):
    from app.source_registry import RegisteredResearchSource
    return RegisteredResearchSource(
        source_id=f"nse-result-{suffix}",
        instrument_id=profile.instrument_id,
        url=f"https://nsearchives.nseindia.com/corporate/{suffix}.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements",
        publisher="NSE",
        reliability_level=ReliabilityLevel.LEVEL_A,
        domain="nsearchives.nseindia.com",
        company_id=profile.company_id,
        discovery_method="NSE_OFFICIAL_API",
        priority=1,
        categories=("FINANCIAL_RESULTS",),
    )


@pytest.mark.asyncio
async def test_queue_timeout_retry_respects_attempt_budget():
    """A bounded queue-timeout retry must not push a document past the
    per-refresh attempt budget (max_attempts_per_refresh)."""
    from app.research_fetching import HttpResearchFetcher, FetchResult
    from app.source_discovery import DiscoveryResult

    class SlowPdfFetcher(HttpResearchFetcher):
        def __init__(self, settings, client):
            super().__init__(settings, client)

        def process_network_response(self, response, *, max_bytes=None, extraction_deadline_monotonic=None):
            time.sleep(0.05)
            return FetchResult(response.final_url, 200, "text/html", "<html>unused</html>", len(response.content))

    settings = Settings(
        research_live_enabled=True,
        research_official_document_max_attempts_per_refresh=2,
        research_official_document_timeout_seconds=1.0,
        research_official_document_extraction_timeout_seconds=0.01,
    )
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-fast", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    repository = ResearchRepository(settings=settings, fetcher=SlowPdfFetcher(settings, client))
    profile = next(p for p in repository.profiles if p.ticker == "RELIANCE")
    sources = [_make_official_source(profile, f"attempt-budget-{i}") for i in range(3)]

    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", s) for s in sources], set(),
    )
    await asyncio.sleep(0.2)
    assert requests == [s.url for s in sources[:2]]
