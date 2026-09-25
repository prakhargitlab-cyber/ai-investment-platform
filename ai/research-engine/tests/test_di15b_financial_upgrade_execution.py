"""Production executor composition; all provider boundaries are local doubles."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.fact_precedence import FactSourceTier
from app.portfolio_orchestration import _refresh_profile_from_global_instrument
from app.research_fetching import FetchResult
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter, ResearchReadinessRuntime,
)
from app.source_discovery import OfficialFilingDiscovery
from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor
from test_di15_financial_authority_upgrade import _repo, _facts, _seed
from test_official_nse_financial_parsing import JUNE
from test_yahoo_mcp_acquisition import result as mcp_result


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    original = httpx.AsyncClient.send

    async def send(client, *args, **kwargs):
        assert isinstance(client._transport, httpx.MockTransport), "Live HTTP is forbidden"
        return await original(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)


class Gateway:
    timeout_seconds = 10.0

    def __init__(self, repo, profile, order):
        self.repo, self.profile, self.order = repo, profile, order
        self.attempts_at_yahoo_start = []

    async def acquire_requirement(self, profile, *, requirement_id, **kwargs):
        self.order.append("YAHOO")
        self.attempts_at_yahoo_start.append(dict(self.repo._financial_authority_attempts))
        now = datetime.now(timezone.utc).isoformat()
        facts = [{
            "metric": fact.key.metric, "value": str(fact.value.value), "unit": fact.value.unit,
            "periodEnd": fact.key.period_end, "periodType": fact.key.period_type,
            "reportingBasis": fact.key.reporting_basis or "STANDALONE",
            "asOf": now, "publishedAt": now, "confidence": 0.8,
            "sourceUrl": "https://finance.yahoo.com/quote/EXAMPLE.NS", "rawFieldOrigin": fact.key.metric,
        } for fact in _facts(self.repo)]
        return mcp_result(requirement_id, globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
                          observedAt=now, retrievedAt=now, financialFacts=facts,
                          structuredFacts=[], marketObservations=[])


def _runtime(repo, profile, order):
    # Matches main.py, including the wrapper DI-15's tests omitted.
    profile.provider_instrument_ids["YAHOO_FINANCE"] = "EXAMPLE.NS"
    legacy = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None)
    wrapper = McpFirstResearchCapabilityExecutor(legacy, repo, Gateway(repo, profile, order), enabled=True)
    return ResearchReadinessRuntime(repo, RepositoryResearchReadinessAdapter(repo), wrapper)


def _nse(repo, profile, order, mode="empty"):
    url = "https://nsearchives.nseindia.com/corporate/synthetic-financial-results.pdf"

    def listing(request):
        assert request.url.params["symbol"] == profile.provider_instrument_ids["NSE"]
        order.append("NSE_DISCOVERY")
        if mode == "unavailable":
            return httpx.Response(503)
        rows = [] if mode == "empty" else [{
            "symbol": profile.ticker, "sm_name": profile.company_name, "isin": profile.isin,
            "desc": "Financial Results", "attchmntText": "Quarterly financial results",
            "attchmntFile": url, "an_dt": "01-Aug-2026 10:00:00",
        }]
        return httpx.Response(200, json=rows)

    repo._official_filing_discovery = OfficialFilingDiscovery(client=httpx.AsyncClient(transport=httpx.MockTransport(listing)))

    class Fetcher:
        async def fetch(self, fetched_url):
            assert fetched_url == url
            order.append("NSE_FETCH")
            text = JUNE if mode == "valid" else "Financial results announcement without a supported numerical table."
            text = f"<html><title>Financial results</title><main>{profile.company_name} {profile.isin} {text}</main></html>"
            return FetchResult(url, 200, "text/html", text, len(text))

    repo._fetcher = Fetcher()

    def forbidden(*args, **kwargs):
        pytest.fail("Authority-only upgrade must not invoke broad search/shareholding")

    repo._discovery.discover = forbidden
    repo._official_shareholding_discovery.discover = forbidden


def _ensure(runtime, profile):
    return asyncio.run(runtime.ensure(profile.instrument_id, jurisdiction="INDIA", requirement_ids=["QUARTERLY_FINANCIALS"]))


@pytest.mark.parametrize("mode", ["empty", "unavailable", "unparseable"])
def test_production_executor_attempts_nse_before_accepting_fresh_yahoo(mode):
    repo, profile = _repo()
    _seed(repo, _facts(repo))
    before = repo.financial_facts_for(profile.instrument_id)
    order = []
    _nse(repo, profile, order, mode)
    runtime = _runtime(repo, profile, order)
    assert asyncio.run(runtime.read(profile.instrument_id, jurisdiction="INDIA")).for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert order == [] and not repo._financial_authority_attempts
    result = _ensure(runtime, profile)
    assert order[0] == "NSE_DISCOVERY" and "YAHOO" not in order
    assert "NSE:QUARTERLY_FINANCIALS" in result.executed_capabilities
    assert repo.financial_facts_for(profile.instrument_id) == before
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").source != "NSE"
    assert profile.instrument_id in repo._financial_authority_attempts
    observations = [row for row in repo.acquisition_observations_for(profile.instrument_id) if row["provider"] == "NSE"]
    assert len(observations) == 1
    assert observations[0]["outcome"] == ("FAILED" if mode == "unavailable" else "SUCCESS_EMPTY")
    assert observations[0]["evidence_count"] == 0
    _ensure(runtime, profile)
    assert order.count("NSE_DISCOVERY") == 1


def test_production_executor_discovers_parses_persists_and_reuses_authority():
    repo, profile = _repo()
    _seed(repo, _facts(repo))
    order = []
    _nse(repo, profile, order, "valid")
    runtime = _runtime(repo, profile, order)
    result = _ensure(runtime, profile)
    assert order == ["NSE_DISCOVERY", "NSE_FETCH"]
    facts = repo.financial_facts_for(profile.instrument_id)
    assert facts and all(f.source_tier == FactSourceTier.OFFICIAL_NSE for f in facts)
    assert len(facts) == len({f.key for f in facts})
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").source == "NSE"
    assert len(repo.documents_for(profile.instrument_id)) == 1
    official = [row for row in repo.acquisition_observations_for(profile.instrument_id) if row["provider"] == "NSE"]
    assert official[0]["outcome"] == "SUCCESS" and official[0]["evidence_count"] == len(facts)
    repo._financial_authority_attempts.clear()  # Prove completeness, not throttle, prevents reacquisition.
    _ensure(runtime, profile)
    assert order == ["NSE_DISCOVERY", "NSE_FETCH"]
    document = repo.documents_for(profile.instrument_id)[0]
    assert repo._reconcile_persisted_official_financial_document(document) == (True, 0)
    assert repo.financial_facts_for(profile.instrument_id) == facts
    _seed(repo, _facts(repo))
    assert repo.financial_facts_for(profile.instrument_id) == facts


def test_missing_financials_attempt_nse_then_yahoo_without_second_nse_attempt():
    repo, profile = _repo()
    order = []
    _nse(repo, profile, order, "unavailable")
    runtime = _runtime(repo, profile, order)
    result = _ensure(runtime, profile)
    assert order == ["NSE_DISCOVERY", "YAHOO"]
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert all(f.source_tier == FactSourceTier.YAHOO for f in repo.financial_facts_for(profile.instrument_id))
    assert runtime.executor.gateway.attempts_at_yahoo_start == [repo._financial_authority_attempts]


def test_trusted_nse_normal_mcp_success_cannot_advance_authority_throttle():
    repo, profile = _repo()
    order = []
    _nse(repo, profile, order)
    runtime = _runtime(repo, profile, order)
    result = asyncio.run(runtime.ensure(profile.instrument_id, jurisdiction="INDIA", requirement_ids=["GROWTH_FACTS"]))
    assert order == ["YAHOO"]
    assert "YAHOO_FINANCE_MCP:GROWTH_FACTS" in result.executed_capabilities
    assert not repo._financial_authority_attempts
    assert all(row["provider"] != "NSE" for row in repo.acquisition_observations_for(profile.instrument_id))


@pytest.mark.parametrize("status", ["RESOLVED", "PENDING", "VERIFIED"])
def test_canonical_trust_controls_real_executor_path(status):
    repo, profile = _repo()
    _refresh_profile_from_global_instrument(profile, {
        "canonicalName": profile.company_name, "primarySymbol": profile.ticker,
        "primaryExchange": "NSE", "country": "IN", "currency": "INR",
        "providerMappings": [{"provider": "NSE", "providerSymbol": "EXAMPLE", "status": status}],
    })
    order = []
    _nse(repo, profile, order)
    _ensure(_runtime(repo, profile, order), profile)
    assert ("NSE_DISCOVERY" in order) == (status == "VERIFIED")
    assert (profile.instrument_id in repo._financial_authority_attempts) == (status == "VERIFIED")
    assert "YAHOO" in order


def test_non_nse_yahoo_execution_does_not_mark_authority_attempt():
    repo, profile = _repo()
    profile.exchange = "BSE"
    order = []
    _nse(repo, profile, order)
    _ensure(_runtime(repo, profile, order), profile)
    assert order == ["YAHOO"]
    assert not repo._financial_authority_attempts


def test_cancellation_before_repository_attempt_does_not_start_throttle(monkeypatch):
    repo, profile = _repo()
    _seed(repo, _facts(repo))
    order = []
    _nse(repo, profile, order)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(repo, "refresh_targeted_categories", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _ensure(_runtime(repo, profile, order), profile)
    assert not order and not repo._financial_authority_attempts
