"""Cross-cycle durable reuse and technical failure regressions; no live providers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from decimal import Decimal

import httpx
import pytest

from app import repository as repository_module
from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason
from app.models import DocumentStatus, MarketPriceObservation
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchResult, PdfExtractionTimeoutError
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter, ResearchReadinessRuntime
from app.historical_market_data import HistoricalPricePopulationService
from app.settings import Settings
from app.source_discovery import (
    DiscoveryResult, SearchDateWindow, SearchDiscoveryService,
    SearchDiscoveryStats, SearchProviderError, SearxngSearchDiscoveryProvider,
)
from test_official_nse_financial_parsing import JUNE, _document, _profile, _trusted_source


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def repo(persistence):
    result = ResearchRepository(persistence=persistence, settings=Settings(
        research_live_enabled=True, research_demo_enabled=False,
        research_search_enabled=False,
    ))
    result.profiles.append(_profile())
    return result


def persist_result(repository):
    document = _document(JUNE).model_copy(update={"retrieved_at": NOW - timedelta(days=1)})
    repository._persistence.upsert_document(document)
    repository.remember_persisted_document(document)
    repository._persist_official_financial_facts(document)
    return document


async def fetch_source(repository, source):
    profile = repository.profile(_profile().instrument_id)
    return await repository._fetch_registered_source(profile, source, expected_profile=profile)


@pytest.mark.asyncio
async def test_second_cycle_reuses_durable_evidence_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    persistence = SqliteResearchPersistence(str(tmp_path / "research.sqlite"))
    first = repo(persistence)
    document = persist_result(first)
    await first._reconcile_incomplete_persisted_official_financial_facts(_profile())
    second = repo(persistence)
    second.settings.research_search_enabled = True
    second._search_discovery.discover = AsyncMock(side_effect=AssertionError("must not search"))
    discovery = AsyncMock(side_effect=AssertionError("must not rediscover"))
    second._official_filing_discovery.discover = discovery
    second._fetcher.fetch = AsyncMock(side_effect=AssertionError("must not download/extract"))
    second._incomplete_persisted_official_financial_documents = AsyncMock(
        side_effect=AssertionError("must not reparse unchanged durable inputs"))
    await second._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    assert second._category_is_fresh(_profile().instrument_id, "FINANCIAL_RESULTS", NOW)
    assert second.financial_facts_for(_profile().instrument_id)
    assert second.documents_for(_profile().instrument_id)[0].document_id == document.document_id
    discovery.assert_not_called()
    second._fetcher.fetch.assert_not_called()
    second._search_discovery.discover.assert_not_called()


@pytest.mark.asyncio
async def test_worker_sees_document_persisted_after_its_startup(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    persistence = SqliteResearchPersistence()
    earlier_worker = repo(persistence)
    writer = repo(persistence)
    document = persist_result(writer)
    assert earlier_worker.documents_for(_profile().instrument_id) == []
    earlier_worker._official_filing_discovery.discover = AsyncMock(side_effect=AssertionError("must reuse"))
    await earlier_worker._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    assert earlier_worker.documents_for(_profile().instrument_id)[0].document_id == document.document_id
    earlier_worker._official_filing_discovery.discover.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_legacy_financial_facts_reused_without_stored_parser_text(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    document = persist_result(repository)
    document.normalized_text = None
    document.title = None
    repository._persistence._connection.execute(
        "UPDATE research_documents SET normalized_text=NULL, title=NULL WHERE document_id=?",
        (str(document.document_id),),
    )
    repository._persistence._connection.commit()
    restarted = repo(repository._persistence)
    restarted._official_filing_discovery.discover = AsyncMock(side_effect=AssertionError("fresh facts are durable evidence"))
    await restarted._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    restarted._official_filing_discovery.discover.assert_not_called()
    assert restarted._category_is_fresh(_profile().instrument_id, "FINANCIAL_RESULTS", NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "stale", "invalid"])
async def test_nonreusable_evidence_still_acquires(state, monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    if state != "missing":
        document = _document(JUNE).model_copy(update={
            "retrieved_at": NOW - timedelta(days=800),
            "normalized_text": JUNE.replace("2026", "2024").replace("2025", "2023"),
            "status": DocumentStatus.FAILED if state == "invalid" else DocumentStatus.PROCESSED,
        })
        repository._persistence.upsert_document(document)
        repository.remember_persisted_document(document)
        if state == "stale":
            repository._persist_official_financial_facts(document)
    repository._official_filing_discovery.discover = AsyncMock(return_value=[])
    await repository._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    repository._official_filing_discovery.discover.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["corrupt", "missing"])
async def test_reconciliation_receipt_invalidates_on_fact_change(change):
    repository = repo(SqliteResearchPersistence())
    persist_result(repository)
    await repository._reconcile_incomplete_persisted_official_financial_facts(_profile())
    assert repository._financial_reconciliation_unchanged(_profile())
    fact = repository.financial_facts_for(_profile().instrument_id)[0]
    if change == "corrupt":
        corrupt = replace(fact, value=fact.value.model_copy(update={"value": "999999"}))
        repository._persistence.upsert_financial_fact(corrupt, allow_same_tier_correction=True)
    else:
        repository._persistence._connection.execute("DELETE FROM global_financial_facts WHERE instrument_id=?", (str(_profile().instrument_id),))
        repository._persistence._connection.commit()
    assert not repository._financial_reconciliation_unchanged(_profile())
    await repository._reconcile_incomplete_persisted_official_financial_facts(_profile())
    assert repository._financial_reconciliation_unchanged(_profile())
    assert next(f for f in repository.financial_facts_for(_profile().instrument_id) if f.key == fact.key).value.value == fact.value.value


@pytest.mark.asyncio
async def test_reconciliation_receipt_invalidates_on_text_or_provenance_change():
    repository = repo(SqliteResearchPersistence())
    document = persist_result(repository)
    await repository._reconcile_incomplete_persisted_official_financial_facts(_profile())
    assert repository._financial_reconciliation_unchanged(_profile())
    document.normalized_text += " corrected disclosure"
    assert not repository._financial_reconciliation_unchanged(_profile())
    document.normalized_text = JUNE
    document.discovery_provider = "SEARCH_DISCOVERY"
    assert not repository._financial_reconciliation_unchanged(_profile())


@pytest.mark.asyncio
async def test_registered_official_path_reuses_extraction_before_fetch():
    repository = repo(SqliteResearchPersistence())
    document = persist_result(repository)
    await repository._reconcile_incomplete_persisted_official_financial_facts(_profile())
    repository._fetcher.fetch = AsyncMock(side_effect=AssertionError("no download or extraction"))
    result = await fetch_source(repository, _trusted_source(_profile()))
    assert result.document_id == document.document_id
    repository._fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_queue_failure_survives_durable_observation(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    repository._official_filing_discovery.discover = AsyncMock(return_value=[
        DiscoveryResult("FINANCIAL_RESULTS", _trusted_source(_profile()))])
    repository._single_flight_official_filing = AsyncMock(side_effect=PdfExtractionTimeoutError("PDF_EXTRACTION_QUEUE_TIMEOUT"))
    await repository._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    observation = repository._latest_acquisition_observation(_profile().instrument_id, "QUARTERLY_FINANCIALS", "NSE")
    assert observation["outcome"] == "FAILED"
    assert "PDF_EXTRACTION_QUEUE_TIMEOUT" in observation["failure_reason"]
    assert classify_reason(observation["failure_reason"]) == TECHNICAL_RETRYABLE
    assert (_profile().instrument_id, "FINANCIAL_RESULTS") not in repository._category_successful_no_change_checks


@pytest.mark.asyncio
async def test_degraded_provider_attempts_bounded_and_recover_after_backoff(monkeypatch):
    tick = [1.0]
    monkeypatch.setattr("app.source_discovery.time.monotonic", lambda: tick[0])
    responses = [{"results": [], "unresponsive_engines": [["a", "timeout"], ["b", "timeout"]]}, {"results": []}]
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=responses[0 if len(calls) == 1 else 1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = SearxngSearchDiscoveryProvider("https://search.example/search", client=client)
        service = SearchDiscoveryService(provider, max_queries_per_category=3)
        for _ in range(2):
            with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER_DEGRADED"):
                await service.discover(_profile(), {"GUIDANCE", "CAPEX"}, set())
        assert len(calls) == 1
        tick[0] += 31
        assert await provider.discover(_profile(), "GUIDANCE", SearchDateWindow(year=2026, query_limit=1)) == []
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_partial_degraded_results_do_not_become_successful_absence():
    def handle(request):
        return httpx.Response(200, json={"results": [{"url": "https://unapproved.example/result", "title": "irrelevant", "engine": "a"}], "unresponsive_engines": [["b", "timeout"]]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = SearxngSearchDiscoveryProvider("https://search.example/search", client=client)
        service = SearchDiscoveryService(provider, allowed_domains=["nseindia.com"])
        with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER_DEGRADED"):
            await service.discover(_profile(), {"GUIDANCE"}, set())
        assert service.last_stats.provider_failure_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failures", [0, 1])
async def test_empty_search_classification_preserves_provider_condition(provider_failures):
    repository = repo(SqliteResearchPersistence())
    repository._search_discovery = SimpleNamespace(
        provider=SimpleNamespace(provider_name="fixture"),
        last_stats=SearchDiscoveryStats(provider_failure_count=provider_failures),
        discover=AsyncMock(return_value=[]),
    )
    successful = await repository._refresh_search_discovery(_profile(), {"GUIDANCE"}, set())
    reason = repository.last_live_error[_profile().instrument_id]
    assert successful is (provider_failures == 0)
    assert reason == ("SEARCH_PROVIDER_DEGRADED" if provider_failures else "SEARCH_RETURNED_ZERO_RESULTS")


@pytest.mark.asyncio
async def test_same_acquisition_budget_does_not_retry_known_degraded_provider():
    provider = SimpleNamespace(provider_name="fixture", discover=AsyncMock(side_effect=SearchProviderError("SEARCH_PROVIDER_DEGRADED")))
    service = SearchDiscoveryService(provider)
    budget = RequirementAcquisitionBudget(_profile().instrument_id, "GOVERNANCE_HISTORY", NOW, AsyncMock(return_value=False))
    token = _scope.set(budget)
    try:
        for _ in range(2):
            with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER_DEGRADED"):
                await service.discover(_profile(), {"GUIDANCE"}, set())
        provider.discover.assert_awaited_once()
    finally:
        _scope.reset(token)


@pytest.mark.asyncio
async def test_partial_search_failure_survives_another_candidates_stats():
    repository = repo(SqliteResearchPersistence())
    stats = SearchDiscoveryStats(provider_failure_count=1)
    repository._search_discovery = SimpleNamespace(
        provider=SimpleNamespace(provider_name="fixture"), last_stats=stats,
        discover=AsyncMock(return_value=[DiscoveryResult("GUIDANCE", _trusted_source(_profile()))]),
    )
    async def fetch(*args, **kwargs):
        # A sibling candidate completes while this candidate fetches its document.
        repository._search_discovery.last_stats = SearchDiscoveryStats()
        return _document(JUNE)
    repository._fetch_registered_source = fetch
    assert not await repository._refresh_search_discovery(_profile(), {"GUIDANCE"}, set())
    assert repository.last_live_error[_profile().instrument_id] == "SEARCH_PROVIDER_DEGRADED"
    assert stats.documents_fetched == 1


@pytest.mark.asyncio
async def test_direct_fetch_sees_post_startup_document_and_url_tracking_alias(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    persistence = SqliteResearchPersistence()
    reader = repo(persistence)
    document = persist_result(repo(persistence))
    reader._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("must reuse durable extraction")))
    source = replace(_trusted_source(_profile()), url=document.canonical_url + "?utm_source=listing#attachment")
    reused = await fetch_source(reader, source)
    assert reused.document_id == document.document_id
    reader._fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_parsed_metadata_keeps_fact_period_eligibility(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    document = persist_result(repository)
    repository._persistence._connection.execute(
        "UPDATE research_documents SET status='PARSED', normalized_text=NULL, title=NULL WHERE document_id=?", (str(document.document_id),))
    repository._persistence._connection.commit()
    restarted = repo(repository._persistence)
    restarted._official_filing_discovery.discover = AsyncMock(side_effect=AssertionError("no new quarter"))
    restarted._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("facts already durable")))
    await restarted._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    reused = await fetch_source(restarted, _trusted_source(_profile()))
    assert reused.document_id == document.document_id
    assert restarted._category_evidence_timing(document.instrument_id, "FINANCIAL_RESULTS")[1].date().isoformat() == "2026-06-30"
    assert not restarted._category_is_eligible_to_check(document.instrument_id, "FINANCIAL_RESULTS", NOW)[0]


@pytest.mark.asyncio
async def test_no_new_quarter_uses_parsed_period_when_title_date_regex_misses(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    document = _document(JUNE).model_copy(update={"retrieved_at": NOW - timedelta(days=20), "published_at": NOW - timedelta(days=21)})
    repository._persistence.upsert_document(document)
    repository.remember_persisted_document(document)
    repository._persist_official_financial_facts(document)
    repository.settings.research_quarterly_freshness_seconds = 1
    repository._official_filing_discovery.discover = AsyncMock(side_effect=AssertionError("quarter window has not opened"))
    assert not repository._category_is_eligible_to_check(document.instrument_id, "FINANCIAL_RESULTS", NOW)[0]
    await repository._refresh_live(document.instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    repository._official_filing_discovery.discover.assert_not_called()


@pytest.mark.asyncio
async def test_second_targeted_financial_pass_reuses_incomplete_cycle_evidence(tmp_path, monkeypatch):
    from test_official_nse_financial_parsing import _rolling_financial_documents
    from app.normalization import content_hash
    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    persistence = SqliteResearchPersistence(str(tmp_path / "interrupted.sqlite"))
    writer = repo(persistence)
    run = persistence.start_refresh_run(instrument_id=_profile().instrument_id, company_id=_profile().company_id, correlation_id="interrupted-cycle", mode="LIVE")
    persist_result(writer)
    historical = _rolling_financial_documents()[1].model_copy(update={
        "canonical_url": "https://nsearchives.nseindia.com/corporate/prior-result.pdf",
        "original_url": "https://nsearchives.nseindia.com/corporate/prior-result.pdf",
        "retrieved_at": NOW - timedelta(days=1), "content_hash": content_hash("prior-result"),
    })
    persistence.upsert_document(historical)
    writer.remember_persisted_document(historical)
    writer._persist_official_financial_facts(historical)
    # The owning run is deliberately never completed.
    assert run.refresh_run_id
    for _ in range(2):
        reader = repo(persistence)
        reader._official_filing_discovery.discover = AsyncMock(side_effect=AssertionError("warm pass must not discover"))
        reader._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("warm pass must not extract")))
        adapter = RepositoryResearchReadinessAdapter(reader)
        executor = ExistingResearchCapabilityExecutor(reader, SimpleNamespace(), SimpleNamespace())
        runtime = ResearchReadinessRuntime(reader, adapter, executor)
        before = await runtime.read(_profile().instrument_id, jurisdiction="INDIA")
        assert before.for_requirement("GROWTH_FACTS").status == "READY_FRESH", before.for_requirement("GROWTH_FACTS")
        result = await runtime.ensure(_profile().instrument_id, jurisdiction="INDIA", requirement_ids=("QUARTERLY_FINANCIALS", "GROWTH_FACTS"))
        assert result.planned_requirement_ids == ()
        assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == "READY_FRESH"
        assert result.readiness.for_requirement("GROWTH_FACTS").status == "READY_FRESH"
        reader._official_filing_discovery.discover.assert_not_called()


@pytest.mark.asyncio
async def test_new_quarter_acquires_only_new_document_and_retains_prior_facts(monkeypatch):
    from test_official_nse_financial_parsing import _multi_period_statement
    monkeypatch.setattr(repository_module, "datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    old = persist_result(repository)
    before = {fact.key: fact.value.value for fact in repository.financial_facts_for(old.instrument_id)}
    later = datetime(2026, 11, 15, tzinfo=timezone.utc)
    class LaterClock(Clock):
        @classmethod
        def now(cls, tz=None):
            return later
    monkeypatch.setattr(repository_module, "datetime", LaterClock)
    source = replace(_trusted_source(_profile()), url="https://nsearchives.nseindia.com/corporate/september-result.pdf", official_title="Financial Results", official_published_at=later - timedelta(days=1))
    text = _multi_period_statement("30 September 2026", "30 June 2026", "30 September 2025", "31 March 2026")
    repository._official_filing_discovery.discover = AsyncMock(return_value=[DiscoveryResult("FINANCIAL_RESULTS", source), DiscoveryResult("FINANCIAL_RESULTS", _trusted_source(_profile()))])
    repository._fetcher = SimpleNamespace(fetch=AsyncMock(return_value=FetchResult(source.url, 200, "application/pdf", text, len(text))))
    original = repository._official_financial_fact_candidates
    def only_new(document, **kwargs):
        assert document.document_id != old.document_id, "old periods must not be parsed again"
        return original(document, **kwargs)
    monkeypatch.setattr(repository, "_official_financial_fact_candidates", only_new)
    await repository._refresh_live(old.instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    repository._fetcher.fetch.assert_awaited_once_with(source.url)
    after = {fact.key: fact.value.value for fact in repository.financial_facts_for(old.instrument_id)}
    assert all(after[key] == value for key, value in before.items())
    assert any(key.period_end == "2026-09-30" for key in after)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", [DocumentStatus.FAILED, DocumentStatus.PARSED, "UNTRUSTED"])
async def test_successful_retry_repairs_duplicate_row_and_is_durable(legacy_status):
    persistence = SqliteResearchPersistence()
    repository = repo(persistence)
    prior = _document(JUNE).model_copy(update={"status": legacy_status, "normalized_text": None})
    if legacy_status == "UNTRUSTED":
        prior = prior.model_copy(update={"status": DocumentStatus.PROCESSED, "normalized_text": JUNE, "discovery_provider": "SEARCH_DISCOVERY"})
    persistence.upsert_document(prior)
    source = _trusted_source(_profile())
    repository._fetcher = SimpleNamespace(fetch=AsyncMock(return_value=FetchResult(source.url, 200, "application/pdf", JUNE, len(JUNE))))
    result = await fetch_source(repository, source)
    assert result.document_id == prior.document_id
    assert persistence.load_document(prior.document_id).normalized_text
    assert persistence.load_document(prior.document_id).status == DocumentStatus.PROCESSED
    assert repository.financial_facts_for(prior.instrument_id)
    restarted = repo(persistence)
    restarted._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("repair must survive restart")))
    assert (await fetch_source(restarted, source)).document_id == prior.document_id
    assert len(persistence.load_documents()) == 1


@pytest.mark.asyncio
async def test_unsupported_parse_receipt_survives_restart_without_advancing_freshness(monkeypatch):
    monkeypatch.setattr(repository_module, "datetime", Clock)
    persistence = SqliteResearchPersistence()
    repository = repo(persistence)
    source = _trusted_source(_profile())
    unsupported = "Financial results attachment contains a narrative statement with no supported numeric statement table."
    repository._fetcher = SimpleNamespace(fetch=AsyncMock(return_value=FetchResult(source.url, 200, "application/pdf", unsupported, len(unsupported))))
    document = await fetch_source(repository, source)
    assert repository._financial_document_parse_unchanged(document) == "UNSUPPORTED"
    restarted = repo(persistence)
    restarted._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("unchanged PDF must not extract")))
    restarted._official_filing_discovery.discover = AsyncMock(return_value=[DiscoveryResult("FINANCIAL_RESULTS", source)])
    parser = Mock(side_effect=AssertionError("unchanged unsupported text must not parse"))
    with monkeypatch.context() as scoped:
        scoped.setattr("app.financial_projection.project_semantic_financial_facts", parser)
        await restarted._refresh_live(_profile().instrument_id, set(), requested_categories={"FINANCIAL_RESULTS"})
    parser.assert_not_called()
    observation = restarted._latest_acquisition_observation(_profile().instrument_id, "QUARTERLY_FINANCIALS", "NSE")
    assert observation["outcome"] == "FAILED"
    assert observation["failure_reason"] == "PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS"
    assert not restarted._category_is_fresh(_profile().instrument_id, "FINANCIAL_RESULTS", NOW)
    assert (_profile().instrument_id, "FINANCIAL_RESULTS") not in restarted._category_successful_no_change_checks
    monkeypatch.setattr(repository_module, "FINANCIAL_PARSER_VERSION", "test-next-version")
    assert restarted._financial_document_parse_unchanged(document) is None
    original = restarted._reconcile_persisted_official_financial_document
    restarted._reconcile_persisted_official_financial_document = Mock(wraps=original)
    await fetch_source(restarted, source)
    restarted._reconcile_persisted_official_financial_document.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["publication", "content"])
async def test_new_provider_revision_retries_same_locator_after_unsupported_parse(revision):
    repository = repo(SqliteResearchPersistence())
    source = replace(_trusted_source(_profile()), official_title="Financial Results", official_published_at=NOW - timedelta(days=2))
    text = "Financial results attachment with narrative information only; numeric statement tables are unavailable."
    repository._fetcher = SimpleNamespace(fetch=AsyncMock(return_value=FetchResult(source.url, 200, "application/pdf", text, len(text))))
    original = await fetch_source(repository, source)
    revised = replace(source, official_title="Revised Financial Results", official_published_at=NOW)
    repository._fetcher.fetch.return_value = FetchResult(source.url, 200, "application/pdf", JUNE, len(JUNE))
    if revision == "publication":
        updated = await fetch_source(repository, revised)
        assert repository._fetcher.fetch.await_count == 2
    else:
        # A caller has already received changed content at the same locator.
        # Deduplication must not discard that newly validated provider input.
        profile = repository.profile(_profile().instrument_id)
        updated = await repository._ingest_registered_fetch_result_async(profile, source, repository._fetcher.fetch.return_value, expected_profile=profile)
    assert updated.document_id == original.document_id
    assert repository.financial_facts_for(original.instrument_id)


@pytest.mark.asyncio
async def test_transient_extraction_failure_does_not_write_unsupported_receipt():
    repository = repo(SqliteResearchPersistence())
    source = _trusted_source(_profile())
    repository._fetcher = SimpleNamespace(fetch=AsyncMock(side_effect=[PdfExtractionTimeoutError("PDF_EXTRACTION_TIMEOUT"), FetchResult(source.url, 200, "application/pdf", JUNE, len(JUNE))]))
    with pytest.raises(PdfExtractionTimeoutError):
        await fetch_source(repository, source)
    assert repository.acquisition_observations_for(_profile().instrument_id) == []
    await fetch_source(repository, source)
    assert repository.financial_facts_for(_profile().instrument_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_prefix", [False, True])
async def test_partial_price_history_requests_only_missing_ranges_and_merges(tmp_path, monkeypatch, empty_prefix):
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    persistence = SqliteResearchPersistence(str(tmp_path / "prices.sqlite"))
    repository = repo(persistence)
    repository.profile(_profile().instrument_id).provider_instrument_ids["YAHOO_FINANCE"] = "EXAMPLE.NS"
    first, last = NOW - timedelta(days=200), NOW - timedelta(days=2)
    def observation(at, provider="YAHOO_FINANCE"):
        return MarketPriceObservation(instrument_id=_profile().instrument_id, observed_at=at, price=Decimal("100"), provider=provider, source_url="https://finance.yahoo.com/quote/EXAMPLE.NS/history", retrieved_at=NOW)
    for at in (first, last):
        persistence.upsert_market_price_observation(observation(at))
    persistence.upsert_market_price_observation(observation(NOW, "NSE"))
    calls = []
    async def closes(instrument, *, start, end):
        calls.append((start, end))
        return [] if empty_prefix and end == first else [observation(start), observation(end - timedelta(days=1))]
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", closes=closes)
    jobs = SimpleNamespace(settings=Settings(market_data_population_initial_lookback_days=400), population=HistoricalPricePopulationService(repository, provider))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    await executor._ensure_historical_prices(_profile().instrument_id)
    assert calls == [(NOW - timedelta(days=400), first), (last + timedelta(days=1), NOW + timedelta(days=1))]
    assert {first, last, NOW} <= {value.observed_at for value in persistence.load_market_price_observations({_profile().instrument_id})}
    reader = repo(persistence)
    reader.profile(_profile().instrument_id).provider_instrument_ids["YAHOO_FINANCE"] = "EXAMPLE.NS"
    jobs.population = HistoricalPricePopulationService(reader, provider)
    executor = ExistingResearchCapabilityExecutor(reader, SimpleNamespace(), jobs)
    assert await executor._ensure_historical_prices(_profile().instrument_id) == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_price_backfill_remains_retryable(monkeypatch):
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    repository.profile(_profile().instrument_id).provider_instrument_ids["YAHOO_FINANCE"] = "EXAMPLE.NS"
    population = SimpleNamespace(populate=AsyncMock(side_effect=RuntimeError("NETWORK_TIMEOUT")))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), SimpleNamespace(settings=Settings(), population=population))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="NETWORK_TIMEOUT"):
            await executor._ensure_historical_prices(_profile().instrument_id)
    assert population.populate.await_count == 2
    assert repository._latest_acquisition_observation(_profile().instrument_id, "HISTORICAL_PRICE_BACKFILL", "YAHOO_FINANCE") is None


@pytest.mark.asyncio
async def test_receipt_rechecks_missing_comparable_fact_owned_by_another_document():
    repository = repo(SqliteResearchPersistence())
    document = persist_result(repository)
    connection = repository._persistence._connection
    connection.execute("UPDATE global_financial_facts SET source_identity='other-official-document' WHERE metric='revenue'")
    connection.commit()
    repository._reconcile_persisted_official_financial_document(document)
    assert repository._financial_document_parse_unchanged(document) == "PARSED"
    connection.execute("DELETE FROM global_financial_facts WHERE metric='revenue'")
    connection.commit()
    assert repository._financial_document_parse_unchanged(document) is None
    await repository._reconcile_incomplete_persisted_official_financial_facts(_profile())
    assert any(fact.key.metric == "revenue" for fact in repository.financial_facts_for(document.instrument_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [None, "ANNUAL", "QUARTERLY"])
async def test_growth_uses_each_periods_existing_freshness_policy(stale, monkeypatch):
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    repository = repo(SqliteResearchPersistence())
    prototypes = repository._official_financial_fact_candidates(_document(JUNE))
    periods = {
        "ANNUAL": ["2026-03-31", "2025-03-31"],
        "QUARTERLY": ["2026-06-30", "2026-03-31"],
    }
    if stale == "ANNUAL":
        periods["ANNUAL"] = ["2025-03-31", "2024-03-31"]
    elif stale == "QUARTERLY":
        periods["QUARTERLY"] = ["2025-12-31", "2025-09-30"]
    for period_type, dates in periods.items():
        for metric in ("revenue", "pat"):
            prototype = next(fact for fact in prototypes if fact.key.metric == metric and fact.key.period_type == period_type)
            for period in dates:
                repository._persistence.upsert_financial_fact(replace(prototype, key=replace(prototype.key, period_end=period)))
    runtime = ResearchReadinessRuntime(repository, RepositoryResearchReadinessAdapter(repository), SimpleNamespace())
    readiness = await runtime.read(_profile().instrument_id, jurisdiction="INDIA")
    growth = readiness.for_requirement("GROWTH_FACTS")
    assert growth.status == ("READY_STALE" if stale else "READY_FRESH")
    assert growth.coverage_pct == 100
    plan = runtime.planner.plan(readiness, jurisdiction="INDIA", requirement_ids=("GROWTH_FACTS",))
    assert bool(plan.targets) is bool(stale)
