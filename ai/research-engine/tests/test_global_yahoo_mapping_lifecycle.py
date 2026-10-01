"""Guardian Review Slice 2 -- systemic VERIFIED_YAHOO_MAPPING_REQUIRED fix.

Root cause (proven, not inferred): Stage-2's own profile-hydration path
(app.global_scanner.CanonicalEquityUniverse.active_global_equities ->
app.portfolio_orchestration.PortfolioResearchOrchestrator.
register_global_profile_metadata) reads ONLY portfolio-service's canonical
identity listing (GET /api/v1/instruments) -- "no portfolio ownership or
provider acquisition" (see CanonicalEquityUniverse's own docstring). That
listing never carries providerMappings, so
CompanyResearchProfile.provider_instrument_ids["YAHOO_FINANCE"] is left
empty for essentially every Stage-2 candidate that is not also someone's
portfolio holding (portfolio positions are the ONLY path that already
carries a portfolio-service-verified Yahoo mapping). Since 2585 real NSE
instruments are overwhelmingly not portfolio holdings, this is exactly why
75/76 real candidates in the diagnosed cycle failed
HISTORICAL_PRICE_SERIES with VERIFIED_YAHOO_MAPPING_REQUIRED.

The fix (app.structured_market.derive_and_verify_nse_yahoo_mapping,
app.research_readiness_runtime.ExistingResearchCapabilityExecutor.
_derive_verified_nse_yahoo_mapping, wired into _ensure_historical_prices)
adds a GENERIC global fallback: when no mapping exists yet, derive
`f"{profile.ticker}.NS"` from the canonical NSE symbol and verify it
through the exact same live-path identity gate
(YahooFinanceProvider._resolve_verified_nse_candidate ->
_trusted_nse_candidate_reason) -- never a fabricated mapping, never a
symbol- or company-specific hack. On success the verified mapping is
written onto the process-local CompanyResearchProfile object so every
later ensure() call for that instrument (a warm run) reuses it without
re-verifying.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from app.historical_market_data import HistoricalPricePopulationService, YahooHistoricalPriceProvider
from app.models import CompanyResearchProfile
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.structured_market import YahooFinanceProvider
from app.repository import ResearchRepository
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor
from app.settings import Settings


def _response(data, status=200):
    return httpx.Response(status, json=data)


def _fresh_nse_profile(instrument_id: UUID, *, ticker="ZENTEC", isin="INE251B01027",
                        company_name="Zen Technologies Limited") -> CompanyResearchProfile:
    """A canonical-identity-only profile, exactly what
    register_global_profile_metadata hydrates from the global-instruments
    listing for a non-portfolio-held Stage-2 candidate: NO
    provider_instrument_ids["YAHOO_FINANCE"] at all."""
    return CompanyResearchProfile(
        instrument_id=instrument_id, company_id=uuid4(), company_name=company_name,
        ticker=ticker, exchange="NSE", mic="XNSE", country="IN", currency="INR", isin=isin,
        provider_instrument_ids={"NSE": ticker},
    )


class _FakeHistoricalTicker:
    def __init__(self, price="1523.40"):
        self._price = price
        self.info = {"symbol": "ZENTEC.NS", "longName": "Zen Technologies Limited",
                     "exchange": "NSI", "currency": "INR", "isin": "INE251B01027"}

    def history(self, **_kwargs):
        observed_at = datetime(2026, 9, 20, tzinfo=timezone.utc)

        class Timestamp:
            def to_pydatetime(self):
                return observed_at

        class History:
            def iterrows(self):
                return [(Timestamp(), {"Close": self._price})]
        h = History()
        h._price = self._price
        return h


# 1 -- fresh non-portfolio NSE stock reaches historical acquisition without
# any portfolio-specific providerMappings payload ---------------------------
@pytest.mark.asyncio
async def test_01_fresh_non_portfolio_nse_stock_derives_and_verifies_mapping(tmp_path):
    instrument_id = uuid4()
    persistence = SqliteResearchPersistence(tmp_path / "lifecycle.db")
    repository = ResearchRepository(settings=Settings(), persistence=persistence)
    profile = _fresh_nse_profile(instrument_id)
    repository.profiles.append(profile)
    assert "YAHOO_FINANCE" not in profile.provider_instrument_ids  # precondition: no mapping yet

    searches = []

    def handler(request):
        if "/finance/search" in request.url.path:
            searches.append(request.url.params.get("q"))
            return _response({"quotes": [{
                "symbol": "ZENTEC.NS", "quoteType": "EQUITY", "longname": "Zen Technologies Limited",
                "exchange": "NSI", "currency": "INR", "isin": "INE251B01027",
            }]})
        return _response({"quoteSummary": {"result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    structured_provider = YahooFinanceProvider(repository.settings, client)
    orchestrator = PortfolioResearchOrchestrator(
        repository, repository.settings, client=client, structured_provider=structured_provider,
    )
    historical_provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeHistoricalTicker())
    population = HistoricalPricePopulationService(persistence, historical_provider)
    from types import SimpleNamespace
    jobs = SimpleNamespace(population=population, settings=repository.settings)
    executor = ExistingResearchCapabilityExecutor(repository, orchestrator, jobs)

    written = await executor._ensure_historical_prices(instrument_id)

    assert searches == ["ZENTEC.NS"]
    assert written == 1
    assert profile.provider_instrument_ids["YAHOO_FINANCE"] == "ZENTEC.NS"
    rows = persistence.load_market_price_observations({instrument_id})
    assert len(rows) == 1 and rows[0].price == Decimal("1523.40")


# 2 -- warm reuse: once derived, no repeated verification search fires ------
@pytest.mark.asyncio
async def test_02_warm_run_reuses_durable_mapping_without_reverifying(tmp_path):
    instrument_id = uuid4()
    persistence = SqliteResearchPersistence(tmp_path / "lifecycle2.db")
    repository = ResearchRepository(settings=Settings(), persistence=persistence)
    profile = _fresh_nse_profile(instrument_id)
    repository.profiles.append(profile)

    searches = []

    def handler(request):
        if "/finance/search" in request.url.path:
            searches.append(request.url.params.get("q"))
            return _response({"quotes": [{
                "symbol": "ZENTEC.NS", "quoteType": "EQUITY", "longname": "Zen Technologies Limited",
                "exchange": "NSI", "currency": "INR", "isin": "INE251B01027",
            }]})
        return _response({"quoteSummary": {"result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    structured_provider = YahooFinanceProvider(repository.settings, client)
    orchestrator = PortfolioResearchOrchestrator(
        repository, repository.settings, client=client, structured_provider=structured_provider,
    )
    historical_provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeHistoricalTicker())
    population = HistoricalPricePopulationService(persistence, historical_provider)
    from types import SimpleNamespace
    jobs = SimpleNamespace(population=population, settings=repository.settings)
    executor = ExistingResearchCapabilityExecutor(repository, orchestrator, jobs)

    await executor._ensure_historical_prices(instrument_id)
    assert len(searches) == 1
    # Second (warm) run: has_year_historical_coverage is false for one
    # observation, so populate() is invoked again for the next window --
    # what matters here is that the mapping search is NOT repeated.
    await executor._ensure_historical_prices(instrument_id)
    assert len(searches) == 1


# 3 -- verification failure never fabricates a mapping ----------------------
@pytest.mark.asyncio
async def test_03_verification_failure_raises_truthfully_no_fabricated_mapping(tmp_path):
    instrument_id = uuid4()
    persistence = SqliteResearchPersistence(tmp_path / "lifecycle3.db")
    repository = ResearchRepository(settings=Settings(), persistence=persistence)
    profile = _fresh_nse_profile(instrument_id, ticker="AMBIGCO", isin="INE999Z01018",
                                  company_name="Ambiguous Corp Limited")
    repository.profiles.append(profile)

    def handler(request):
        if "/finance/search" in request.url.path:
            # Yahoo returns a DIFFERENT company/ISIN under the derived symbol.
            return _response({"quotes": [{
                "symbol": "AMBIGCO.NS", "quoteType": "EQUITY", "longname": "Totally Unrelated Ltd",
                "exchange": "NSI", "currency": "INR", "isin": "INE000X00000",
            }]})
        return _response({"quoteSummary": {"result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    structured_provider = YahooFinanceProvider(repository.settings, client)
    orchestrator = PortfolioResearchOrchestrator(
        repository, repository.settings, client=client, structured_provider=structured_provider,
    )
    historical_provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeHistoricalTicker())
    population = HistoricalPricePopulationService(persistence, historical_provider)
    from types import SimpleNamespace
    jobs = SimpleNamespace(population=population, settings=repository.settings)
    executor = ExistingResearchCapabilityExecutor(repository, orchestrator, jobs)

    with pytest.raises(ValueError, match="VERIFIED_HISTORICAL_MAPPING_REQUIRED"):
        await executor._ensure_historical_prices(instrument_id)
    assert "YAHOO_FINANCE" not in profile.provider_instrument_ids
    assert persistence.load_market_price_observations({instrument_id}) == []


# 4 -- non-NSE profile: derivation is never attempted (scoped to NSE only) --
@pytest.mark.asyncio
async def test_04_non_nse_profile_never_attempts_ns_derivation(tmp_path):
    instrument_id = uuid4()
    persistence = SqliteResearchPersistence(tmp_path / "lifecycle4.db")
    repository = ResearchRepository(settings=Settings(), persistence=persistence)
    profile = CompanyResearchProfile(
        instrument_id=instrument_id, company_id=uuid4(), company_name="Nvidia Corporation",
        ticker="NVDA", exchange="XNAS", mic="XNAS", country="US", currency="USD",
        isin="US67066G1040", provider_instrument_ids={},
    )
    repository.profiles.append(profile)

    calls = []

    def handler(request):
        calls.append(request.url.path)
        return _response({"quotes": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    structured_provider = YahooFinanceProvider(repository.settings, client)
    orchestrator = PortfolioResearchOrchestrator(
        repository, repository.settings, client=client, structured_provider=structured_provider,
    )
    historical_provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeHistoricalTicker())
    population = HistoricalPricePopulationService(persistence, historical_provider)
    from types import SimpleNamespace
    jobs = SimpleNamespace(population=population, settings=repository.settings)
    executor = ExistingResearchCapabilityExecutor(repository, orchestrator, jobs)

    with pytest.raises(ValueError, match="VERIFIED_HISTORICAL_MAPPING_REQUIRED"):
        await executor._ensure_historical_prices(instrument_id)
    assert calls == []  # no NVDA.NS derivation attempt for a non-NSE profile
