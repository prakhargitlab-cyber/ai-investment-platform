"""Provider-free reproductions of shared-budget and parser outcome defects."""
from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope, investigate
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository, _InstrumentRefreshGate
from app.research_fetching import FetchError
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.settings import Settings
from app.source_discovery import DiscoveryResult

import test_official_nse_financial_parsing as official
import test_research_readiness as readiness
from test_capability_batching import _FakeGroupRuntime
from test_di12c_shareholding_precedence import _DegradedSearchDiscovery
from test_shareholding import _snapshot, _nse_xbrl_fixture
from test_stock_rule_engine import _readiness, INSTRUMENT_ID


NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("title,expected", [
    ("Shareholders meeting: Scrutinizer report of Annual General Meeting; voting results", False),
    ("Annual General Meeting voting results", False),
    ("Annual scrutiniser's report and results", False),
    ("Annual scrutinizers report and result", False),
    ("Quarterly Results", True),
    ("Annual Financial Statements", True),
    ("Half-year unaudited results", True),
    ("Voting results and audited financial results for the quarter ended June 30, 2026", True),
])
def test_financial_discovery_distinguishes_voting_results_from_financial_results(title, expected):
    from app.source_discovery import _is_financial_result_announcement
    assert _is_financial_result_announcement(title) is expected


def repository(categories):
    profile = official._profile()
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False, research_search_enabled=True),
        persistence=SqliteResearchPersistence(":memory:"),
        search_discovery=_DegradedSearchDiscovery(),
    )
    repo.profiles = [profile]
    repo._instrument_refresh_gate = lambda *a, **k: _InstrumentRefreshGate(
        missing_categories=set(categories), shareholding_backfill_needed=False,
        shareholding_category_enrichment_needed=False, state="INCOMPLETE",
    )
    repo.financial_authority_upgrade_due = lambda *a: False
    repo._discovery = SimpleNamespace(discover=lambda *a: [])
    repo._official_filing_discovery = SimpleNamespace(discover=AsyncMock(return_value=[]))
    repo._official_shareholding_discovery = SimpleNamespace(discover=AsyncMock(return_value=[]))
    return repo, profile


def budget_for(profile, **kwargs):
    return RequirementAcquisitionBudget(
        profile.instrument_id, "ORDER_BOOK_CAPEX_GUIDANCE+SHAREHOLDING+GOVERNANCE_HISTORY",
        NOW, AsyncMock(return_value=False), **kwargs,
    )


@pytest.mark.asyncio
async def test_voting_attachment_is_not_selected_for_financial_acquisition():
    from app.source_discovery import OfficialFilingDiscovery
    profile = official._profile()
    discovery = OfficialFilingDiscovery()
    discovery._announcement_rows = AsyncMock(return_value=[
        {"symbol": "EXAMPLE", "desc": title, "an_dt": "01-Sep-2026 12:00:00",
         "attchmntFile": f"https://nsearchives.nseindia.com/corporate/{key}.pdf"}
        for key, title in [("voting", "Annual General Meeting voting results"),
                           ("financial", "Quarterly financial results")]
    ])
    token = _scope.set(budget_for(profile))
    try:
        selected = await discovery.discover(profile, {"FINANCIAL_RESULTS"}, set())
    finally:
        _scope.reset(token)
    assert [r.source.url for r in selected] == ["https://nsearchives.nseindia.com/corporate/financial.pdf"]


@pytest.mark.asyncio
async def test_ownership_feed_precedes_pdf_budget_consumption_and_survives_search_failure():
    repo, profile = repository({"SHAREHOLDING_PATTERN", "REGULATORY"})
    snapshot = _snapshot(profile.instrument_id).model_copy(update={
        "source_type": "NSE_SHAREHOLDING_XBRL", "values": [], "retrieved_at": NOW,
    })
    repo._official_shareholding_discovery.discover.return_value = [snapshot]
    repo._fetcher = SimpleNamespace(fetch_nse_shareholding_xbrl=AsyncMock(
        return_value=SimpleNamespace(text=_nse_xbrl_fixture())))
    budget = budget_for(profile, max_documents=2)

    async def consume_pdf_budget(*args):
        # Previously this ran first and starved all discovered XBRL snapshots.
        assert repo._category_has_qualifying_evidence(profile.instrument_id, "SHAREHOLDING_PATTERN")
        while await budget.allow_document():
            pass
        return True

    repo._fetch_official_filings = consume_pdf_budget
    token = _scope.set(budget)
    try:
        await repo._refresh_targeted(profile, set(), now=NOW)
    finally:
        _scope.reset(token)
    assert budget.documents_attempted == 2  # ceiling unchanged
    repo._fetcher.fetch_nse_shareholding_xbrl.assert_awaited_once()
    assert repo._search_discovery.discover_called
    assert "Shareholding Pattern" not in repo._search_discovery.discover_categories
    assert budget.failures and "SEARCH_PROVIDER" in budget.failures[-1]
    evidence = {"SHAREHOLDING": []}
    RepositoryResearchReadinessAdapter._append_shareholding(evidence, repo.shareholding_for(profile.instrument_id))
    _, result, _ = readiness.assess(readiness.complete_snapshot(
        overrides={"SHAREHOLDING": tuple(evidence["SHAREHOLDING"])},
        failures={"SHAREHOLDING": budget.failures[-1]},
    ))
    assert result.for_requirement("SHAREHOLDING").status == Status.READY_FRESH
    assert repo._persistence.load_shareholding_snapshots({profile.instrument_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["NETWORK_TIMEOUT", "PDF_EXTRACTION_TIMEOUT", "PARSER_FAILED", "NO_SUPPORTED_VALUES"])
async def test_xbrl_failure_without_ownership_is_durable_technical_not_empty(failure):
    repo, profile = repository({"SHAREHOLDING_PATTERN", "REGULATORY"})
    snapshot = _snapshot(profile.instrument_id).model_copy(update={
        "source_type": "NSE_SHAREHOLDING_XBRL", "values": [],
    })
    repo._official_shareholding_discovery.discover.return_value = [snapshot]
    fetch = (AsyncMock(return_value=SimpleNamespace(text="<xbrl/>")) if failure == "NO_SUPPORTED_VALUES"
             else AsyncMock(side_effect=FetchError(failure)))
    repo._fetcher = SimpleNamespace(fetch_nse_shareholding_xbrl=fetch)
    budget = budget_for(profile)
    token = _scope.set(budget)
    try:
        await repo._refresh_targeted(profile, set(), now=NOW)
    finally:
        _scope.reset(token)
    assert not repo.shareholding_for(profile.instrument_id)
    assert failure in budget.requirement_failures["SHAREHOLDING"]
    rows = repo._persistence.load_acquisition_observations(profile.instrument_id)
    ownership = [r for r in rows if r["requirement_id"] == "SHAREHOLDING"]
    assert len(ownership) == 1
    assert ownership[0]["outcome"] == "FAILED"
    assert failure in ownership[0]["failure_reason"]
    _, result, _ = readiness.assess(readiness.complete_snapshot(
        omit={"SHAREHOLDING"}, failures={"SHAREHOLDING": ownership[0]["failure_reason"]}))
    assert result.for_requirement("SHAREHOLDING").status == Status.FAILED


@pytest.mark.asyncio
async def test_ownership_specific_failure_survives_unrelated_group_search_degradation():
    class Runtime(_FakeGroupRuntime):
        async def ensure(self, *args, **kwargs):
            budget = _scope.get()
            budget.requirement_failures["SHAREHOLDING"] = "NSE_SHAREHOLDING_UNAVAILABLE:NETWORK_TIMEOUT"
            budget.failures.append("SEARCH_PROVIDER_DEGRADED")
            return await super().ensure(*args, **kwargs)

    runtime = Runtime(_readiness({"SHAREHOLDING": Status.MISSING}))
    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    row = next(kwargs for args, kwargs in runtime.observations if args[1] == "SHAREHOLDING")
    assert row["failure_reason"] == "NSE_SHAREHOLDING_UNAVAILABLE:NETWORK_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_stored_financial_document_reused_and_parser_outcome_is_truthful(supported):
    repo, profile = repository({"FINANCIAL_RESULTS"})
    source = official._trusted_source(profile)
    # Garbled numeric columns reproduce the observed unsupported result shape;
    # do not attempt to infer values or repair ambiguous OCR digits.
    text = (official.JUNE + " Capital Adequacy Ratio 18.25%" if supported else
            "Financial Results for the quarter ended June 30, 2026. "
            "Gross NPA 1,04,03452,9251,06,804 c) o/o of Gross NPA o.440.200.20")
    document = official._document(text)
    repo.documents[document.document_id] = document
    repo._persistence.upsert_document(document)
    repo._official_filing_discovery.discover.return_value = [DiscoveryResult("FINANCIAL_RESULTS", source)]
    repo._single_flight_official_filing = AsyncMock(side_effect=AssertionError("durable text must be reused"))
    await repo._refresh_targeted(profile, set(), now=NOW)
    repo._single_flight_official_filing.assert_not_called()
    facts = repo._persistence.load_financial_facts({profile.instrument_id})
    observation = next(r for r in repo._persistence.load_acquisition_observations(profile.instrument_id)
                       if r["requirement_id"] == "QUARTERLY_FINANCIALS")
    if supported:
        assert {"revenue", "pat", "capital_adequacy"} <= {f.key.metric for f in facts}
        assert observation["outcome"] == "SUCCESS"
    else:
        assert facts == []
        assert observation["outcome"] == "FAILED"
        assert observation["failure_reason"] == "PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS"
        _, result, _ = readiness.assess(readiness.complete_snapshot(
            omit={"BALANCE_SHEET_FACTS"}, failures={"BALANCE_SHEET_FACTS": observation["failure_reason"]}))
        assert result.for_requirement("BALANCE_SHEET_FACTS").status == Status.FAILED


@pytest.mark.asyncio
async def test_skipped_filings_do_not_consume_shared_slots_and_exhaustion_is_technical():
    from app.failure_taxonomy import classify_reason, TECHNICAL_RETRYABLE
    repo, profile = repository({"FINANCIAL_RESULTS"})
    repo.settings.research_official_document_max_attempts_per_refresh = 1
    source = official._trusted_source(profile)
    filings = [DiscoveryResult("FINANCIAL_RESULTS", replace(source,
        source_id=f"result-{i}", url=f"https://nsearchives.nseindia.com/corporate/result-{i}.pdf"))
        for i in range(5)]
    repo._single_flight_official_filing = AsyncMock(return_value=(official._document(official.JUNE), False))
    budget = budget_for(profile, max_documents=12)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)
    assert budget.documents_attempted == repo._single_flight_official_filing.await_count == 1
    assert budget.failures == ["DOCUMENT_BUDGET_EXHAUSTED"]
    assert classify_reason(budget.failures[0]) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_local_attempt_limit_cannot_become_authoritative_empty_governance_check():
    repo, profile = repository({"REGULATORY"})
    repo.settings.research_official_document_max_attempts_per_refresh = 1
    source = replace(official._trusted_source(profile), categories=("REGULATORY",))
    repo._official_filing_discovery.discover.return_value = [
        DiscoveryResult("REGULATORY", replace(source, source_id=f"filing-{i}",
            url=f"https://nsearchives.nseindia.com/corporate/filing-{i}.pdf"))
        for i in range(3)
    ]
    repo._single_flight_official_filing = AsyncMock(return_value=(official._document(official.JUNE), False))
    budget = budget_for(profile, max_documents=12)
    token = _scope.set(budget)
    try:
        await repo._refresh_targeted(profile, set(), now=NOW)
    finally:
        _scope.reset(token)
    assert budget.documents_attempted == 1
    observation = next(r for r in repo._persistence.load_acquisition_observations(profile.instrument_id)
                       if r["provider"] == "NSE" and r["requirement_id"] == "GOVERNANCE_HISTORY")
    assert observation["outcome"] == "FAILED"
    assert observation["failure_reason"] == "DOCUMENT_BUDGET_EXHAUSTED_PARTIAL_COVERAGE"


@pytest.mark.asyncio
async def test_historical_mapping_timeout_does_not_persist_mapping_or_discard_durable_prices():
    from app.historical_market_data import HistoricalPriceProviderError
    from app.research_readiness_runtime import ExistingResearchCapabilityExecutor
    from app.failure_taxonomy import classify_reason, TECHNICAL_RETRYABLE
    from test_research_readiness_runtime import _target
    from app.models import MarketPriceObservation
    from decimal import Decimal

    repo, profile = repository(set())
    observation = MarketPriceObservation(instrument_id=profile.instrument_id, observed_at=NOW,
        price=Decimal("123"), currency="INR", provider="NSE", source_url="https://www.nseindia.com", retrieved_at=NOW)
    repo._persistence.upsert_market_price_observation(observation)
    population = AsyncMock()
    executor = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), SimpleNamespace(population=population))
    executor._derive_verified_nse_yahoo_mapping = AsyncMock(side_effect=HistoricalPriceProviderError("DOWNSTREAM_TIMEOUT"))
    result = await executor.execute_primary(profile.instrument_id, [_target("HISTORICAL_PRICE_SERIES")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None)
    assert result.failures == {"HISTORICAL_PRICE_SERIES": "DOWNSTREAM_TIMEOUT"}
    assert classify_reason(result.failures["HISTORICAL_PRICE_SERIES"]) == TECHNICAL_RETRYABLE
    assert "YAHOO_FINANCE" not in profile.provider_instrument_ids
    population.populate.assert_not_called()
    retained = repo._persistence.load_market_price_observations({profile.instrument_id})
    assert len(retained) == 1 and retained[0].price == Decimal("123")
