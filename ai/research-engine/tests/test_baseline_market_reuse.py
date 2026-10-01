"""Baseline readiness through real durable storage; all providers are local fakes."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

from app import portfolio_orchestration, research_readiness, research_readiness_runtime, structured_market
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.models import MarketPriceObservation, ProvenancedValue
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    BASELINE_REQUIREMENT_IDS, ExistingResearchCapabilityExecutor,
    RepositoryResearchReadinessAdapter, ResearchReadinessRuntime,
)
from app.settings import Settings
from app.structured_market import YahooFinanceProvider
from test_research_readiness_runtime import INSTRUMENT_ID, NOW, _profile, _structured_record


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class BaselineTicker:
    info = {
        "symbol": "READY.NS", "exchange": "NSI", "currency": "INR", "quoteType": "EQUITY",
        "regularMarketPrice": 250, "regularMarketTime": int(NOW.timestamp()),
        "trailingEps": 12, "trailingPE": 20, "priceToBook": 3,
        "sector": "Industrials", "industry": "Specialty Industrial Machinery",
    }

    def __init__(self):
        self.unrequested = []

    def __getattr__(self, name):
        self.unrequested.append(name)
        raise AssertionError(f"Baseline must not acquire {name}")


def history(store, *, end=NOW, count=150):
    for index in range(count):
        store.upsert_market_price_observation(MarketPriceObservation(
            instrument_id=INSTRUMENT_ID, observed_at=end - timedelta(days=count - index - 1),
            price=Decimal(250), currency="INR", provider="YAHOO_FINANCE",
            source_url="https://finance.yahoo.com/quote/READY.NS/history", retrieved_at=end,
        ))


@pytest_asyncio.fixture
async def harness(monkeypatch):
    for module in (portfolio_orchestration, research_readiness, research_readiness_runtime, structured_market):
        monkeypatch.setattr(module, "datetime", Clock)
    store = SqliteResearchPersistence()
    settings = Settings(research_demo_enabled=False, research_live_enabled=True, structured_provider_enabled=True)
    repo = ResearchRepository(persistence=store, settings=settings)
    repo.profiles.append(_profile())
    network = Mock(side_effect=AssertionError("Verified mapping must not search"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(network))
    ticker = BaselineTicker()
    factory = Mock(return_value=ticker)
    provider = YahooFinanceProvider(settings, client, ticker_factory=factory)
    orchestrator = PortfolioResearchOrchestrator(repo, settings, client=client, structured_provider=provider)
    executor = ExistingResearchCapabilityExecutor(repo, orchestrator, SimpleNamespace())
    adapter = RepositoryResearchReadinessAdapter(repo)
    runtime = ResearchReadinessRuntime(repo, adapter, executor)
    try:
        yield SimpleNamespace(store=store, repo=repo, runtime=runtime, executor=executor,
                              orchestrator=orchestrator, provider=provider, factory=factory,
                              ticker=ticker, network=network)
    finally:
        await client.aclose()


async def ensure(h, requirements=BASELINE_REQUIREMENT_IDS):
    # A test deadline detects accidental waiting; production retains its 25 s budget.
    return await asyncio.wait_for(h.runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=requirements,
    ), 3)


@pytest.mark.asyncio
async def test_fresh_durable_baseline_after_repository_restart_has_no_provider_work(harness):
    h = harness
    h.store.upsert_structured_market_snapshot(_structured_record(_profile()))
    history(h.store)
    # Durable rows were written after initialization, and survive another runtime.
    fresh_repo = ResearchRepository(persistence=h.store, settings=h.repo.settings)
    fresh_repo.profiles.append(_profile())
    h.runtime = ResearchReadinessRuntime(fresh_repo, RepositoryResearchReadinessAdapter(fresh_repo), h.executor)
    result = await ensure(h)
    assert result.planned_requirement_ids == ()
    assert result.executed_capabilities == ()
    assert not result.failures and not h.runtime._flights
    for requirement in BASELINE_REQUIREMENT_IDS:
        assert result.readiness.for_requirement(requirement).status == ResearchRequirementStatus.READY_FRESH
    h.factory.assert_not_called()
    h.network.assert_not_called()
    assert h.runtime.ensure_timeout_seconds == 25


@pytest.mark.asyncio
async def test_verified_mapping_baseline_collects_only_market_inputs_then_reuses(harness, caplog):
    h = harness
    history(h.store)
    h.repo.persist_yahoo_statement_facts_async = AsyncMock(side_effect=AssertionError("No statement work"))
    caplog.set_level("INFO", logger="app.structured_market")
    with caplog.at_level("INFO", logger="app.portfolio_orchestration"):
        first = await ensure(h)
        second = await ensure(h)
    assert not first.failures and not second.failures
    assert first.executed_capabilities == ("STRUCTURED_MARKET",)
    assert second.executed_capabilities == ()
    assert "structured_mapping_reused" in caplog.text
    assert "structured_provider_complete" in caplog.text
    assert "research_readiness_ensure_timeout" not in caplog.text
    assert not h.runtime._flights
    h.factory.assert_called_once_with("READY.NS")
    h.network.assert_not_called()
    h.repo.persist_yahoo_statement_facts_async.assert_not_called()
    assert not h.ticker.unrequested


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "stale"])
async def test_missing_or_stale_history_still_dispatches_historical_acquisition(harness, state):
    h = harness
    record = _structured_record(_profile())
    h.store.upsert_structured_market_snapshot(record.model_copy(update={
        "snapshot": record.snapshot.model_copy(update={"facts": {
            key: value for key, value in record.snapshot.facts.items() if key != "latestPrice"
        }}),
    }))
    if state == "stale":
        history(h.store, end=NOW - timedelta(days=30))
    async def acquire(_):
        history(h.store)
    h.executor._ensure_historical_prices = AsyncMock(side_effect=acquire)
    result = await ensure(h, ["HISTORICAL_PRICE_SERIES"])
    h.executor._ensure_historical_prices.assert_awaited_once_with(INSTRUMENT_ID)
    assert result.readiness.for_requirement("HISTORICAL_PRICE_SERIES").status == ResearchRequirementStatus.READY_FRESH
    assert not result.failures
    h.factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "stale", "invalid"])
async def test_latest_price_gap_still_acquires_and_wakes_on_completion(harness, state):
    h = harness
    if state != "missing":
        old = _structured_record(_profile())
        value = old.snapshot.facts["latestPrice"].model_copy(update={
            "as_of_date": NOW - timedelta(days=5),
            "retrieved_at": NOW - timedelta(days=5),
            "value": Decimal(0) if state == "invalid" else Decimal(250),
        })
        h.store.upsert_structured_market_snapshot(old.model_copy(update={
            "snapshot": old.snapshot.model_copy(update={"facts": {"latestPrice": value}}),
        }))
    h.repo.persist_yahoo_statement_facts_async = AsyncMock(side_effect=AssertionError("No statement work"))
    result = await ensure(h, ["LATEST_PRICE"])
    assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
    assert not result.failures
    h.factory.assert_called_once()
    assert not h.ticker.unrequested


@pytest.mark.asyncio
async def test_failed_baseline_provider_remains_retryable(harness):
    h = harness
    h.factory.side_effect = [TimeoutError("provider"), h.ticker]
    failed = await ensure(h, ["LATEST_PRICE"])
    assert failed.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.FAILED
    assert "STRUCTURED_PROVIDER_UNAVAILABLE" in failed.failures["LATEST_PRICE"]
    assert not h.repo.market_price_observations_for({INSTRUMENT_ID}).get(INSTRUMENT_ID)
    retried = await ensure(h, ["LATEST_PRICE"])
    assert retried.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
    assert not retried.failures and h.factory.call_count == 2


@pytest.mark.asyncio
async def test_whole_universe_baseline_entry_reuses_fresh_instrument(harness):
    h = harness
    h.store.upsert_structured_market_snapshot(_structured_record(_profile()))
    history(h.store)
    service = GlobalOpportunityOrchestrator(h.repo, h.store,
        profile_hydrator=lambda *args: True, readiness_runtime=h.runtime)
    outcomes, failures = await asyncio.wait_for(service._acquire_baseline_requirements(
        [SimpleNamespace(global_instrument_id=INSTRUMENT_ID)],
        {str(INSTRUMENT_ID): {"sector": "Industrials", "exchange": "NSE", "ticker": "READY"}},
        NOW, None,
    ), 3)
    assert not failures and outcomes[INSTRUMENT_ID].planned_requirement_ids == ()
    h.factory.assert_not_called()
    h.network.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_as_of", [True, False])
async def test_baseline_subset_preserves_old_research_provenance_without_refreshing_it(harness, explicit_as_of):
    h = harness
    old = _structured_record(_profile())
    old_time = NOW - timedelta(days=180)
    old_fact = ProvenancedValue(value=Decimal(17), source_url=old.source_url,
                               source_name="Yahoo Finance", as_of_date=old_time if explicit_as_of else None,
                               retrieved_at=old_time)
    h.store.upsert_structured_market_snapshot(old.model_copy(update={
        "snapshot": old.snapshot.model_copy(update={"facts": {"roce": old_fact}}),
    }))
    await ensure(h, ["LATEST_PRICE"])
    record = h.repo.structured_market_snapshots_for({INSTRUMENT_ID})[INSTRUMENT_ID][0]
    retained = record.snapshot.facts["roce"]
    assert retained == old_fact.model_copy(update={"as_of_date": old_time})
    assert record.snapshot.facts["latestPrice"].as_of_date == NOW


@pytest.mark.asyncio
async def test_timeout_in_history_does_not_misreport_completed_structured_capability(harness, caplog):
    h = harness
    h.runtime.ensure_timeout_seconds = 0.2
    async def pending(_):
        await asyncio.Event().wait()
    h.executor._ensure_historical_prices = AsyncMock(side_effect=pending)
    result = await ensure(h)
    assert result.failures.keys() == {"HISTORICAL_PRICE_SERIES"}
    assert "inFlight=['HISTORICAL_MARKET_DATA']" in caplog.text
    assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_baseline_refresh_cannot_reuse_or_restore_an_old_cached_quote(harness):
    h = harness
    ticker = SimpleNamespace(info=dict(BaselineTicker.info), news=[], income_stmt=None,
        balance_sheet=None, quarterly_income_stmt=None, quarterly_balance_sheet=None,
        cashflow=None, quarterly_cashflow=None)
    h.factory.return_value = ticker
    instrument = h.orchestrator._instrument_for_registered_profile(INSTRUMENT_ID)
    full = await h.provider.collect(instrument)
    ticker.info["regularMarketPrice"] = 275
    baseline = await h.provider.collect_baseline(instrument)
    assert baseline.facts["latestPrice"].value == Decimal(275)
    refreshed_full = await h.provider.collect(instrument)
    assert refreshed_full.facts["latestPrice"].value == Decimal(275)
    assert full.facts["latestPrice"].value == Decimal(250)
    assert h.factory.call_count == 3


@pytest.mark.asyncio
async def test_cold_baseline_does_not_cache_a_partial_snapshot_for_full_research(harness):
    h = harness
    instrument = h.orchestrator._instrument_for_registered_profile(INSTRUMENT_ID)
    await h.provider.collect_baseline(instrument)
    assert not h.ticker.unrequested
    # The existing full path still retrieves research data after a baseline call.
    ticker = SimpleNamespace(info=dict(BaselineTicker.info), news=[], income_stmt=None,
        balance_sheet=None, quarterly_income_stmt=None, quarterly_balance_sheet=None,
        cashflow=None, quarterly_cashflow=None)
    h.factory.return_value = ticker
    await h.provider.collect(instrument)
    assert h.factory.call_count == 2


@pytest.mark.asyncio
async def test_http_baseline_skips_news_and_unrequested_summary_modules(harness):
    h = harness
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/finance/quote"):
            return httpx.Response(200, json={"quoteResponse": {"result": [BaselineTicker.info]}})
        assert "quoteSummary" in request.url.path
        assert set(request.url.params["modules"].split(",")) == {
            "price", "summaryDetail", "defaultKeyStatistics", "financialData", "assetProfile",
        }
        return httpx.Response(200, json={"quoteSummary": {"result": []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = YahooFinanceProvider(h.repo.settings, client)
        snapshot = await provider.collect_baseline(h.orchestrator._instrument_for_registered_profile(INSTRUMENT_ID))
    assert snapshot.facts["latestPrice"].value == Decimal(250)
    assert len(requests) == 2 and not snapshot.news and not snapshot.statement_facts


@pytest.mark.asyncio
async def test_baseline_retains_mapping_identity_validation(harness):
    h = harness
    h.ticker.info = {**BaselineTicker.info, "symbol": "WRONG.NS"}
    result = await ensure(h, ["LATEST_PRICE"])
    assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.FAILED
    assert "PERSISTED_MAPPING_CONFLICT" in result.failures["LATEST_PRICE"]


@pytest.mark.asyncio
async def test_successful_partial_snapshot_does_not_satisfy_valuation_or_prevent_retry(harness):
    h = harness
    h.ticker.info = {key: value for key, value in BaselineTicker.info.items()
                     if key not in {"trailingEps", "trailingPE", "priceToBook"}}
    first = await ensure(h, ["VALUATION_INPUTS"])
    assert first.readiness.for_requirement("VALUATION_INPUTS").status == ResearchRequirementStatus.PARTIAL
    h.ticker.info = dict(BaselineTicker.info)
    second = await ensure(h, ["VALUATION_INPUTS"])
    assert second.readiness.for_requirement("VALUATION_INPUTS").status == ResearchRequirementStatus.READY_FRESH
    assert h.factory.call_count == 2
