from uuid import uuid4

import httpx
import pytest

from app.international_fundamentals import InternationalFundamentalsResult, SecEdgarFundamentalProvider, EodhdFundamentalProvider
from app.models import CompanyResearchProfile
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.settings import Settings


def profile(**changes):
    value = dict(instrument_id=uuid4(), company_id=uuid4(), company_name="Example", ticker="MSFT", exchange="XNAS", mic="XNAS", country="US", currency="USD")
    value.update(changes)
    return CompanyResearchProfile(**value)


class Provider:
    def __init__(self, result=None, error=None): self.result, self.error = result, error
    async def collect(self, _profile):
        if self.error: raise self.error
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "ticker"), [("SEC_CIK", "0000789019"), ("EODHD", "AIXA.F")])
async def test_verified_mapping_is_written_once_through_portfolio_boundary(monkeypatch, provider_id, ticker):
    captured = []
    async def handler(request):
        captured.append(request)
        return httpx.Response(200, json={})
    p = profile()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Provider(InternationalFundamentalsResult([], {provider_id: ticker}))
    monkeypatch.setattr("app.portfolio_orchestration.international_provider_for", lambda *_args, **_kwargs: provider)
    orchestrator = PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client)
    await orchestrator.refresh_international_fundamentals(
        p,
        correlation_id="mapping-correlation",
        identity_headers={"X-AIP-User-Id": "internal-research", "X-AIP-User-Roles": "ADMIN"},
    )
    assert len(captured) == 1
    assert captured[0].method == "PUT"
    assert captured[0].url.path == f"/api/v1/instruments/{p.instrument_id}/provider-mappings/verified"
    assert captured[0].headers["x-aip-user-id"] == "internal-research"
    assert captured[0].headers["x-aip-user-roles"] == "ADMIN"
    assert captured[0].headers["x-correlation-id"] == "mapping-correlation"
    payload = __import__("json").loads(captured[0].content)
    assert payload["provider"] == provider_id and payload["providerInstrumentId"] == ticker
    assert payload["resolutionSource"] == "RESEARCH_ENGINE_VERIFIED_FUNDAMENTALS" and payload["confidence"] == 0.90
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_or_failed_provider_never_writes_mapping(monkeypatch):
    calls = []
    async def handler(request): calls.append(request); return httpx.Response(200)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)); p = profile()
    orchestrator = PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client)
    monkeypatch.setattr("app.portfolio_orchestration.international_provider_for", lambda *_args, **_kwargs: Provider(InternationalFundamentalsResult([], {})))
    await orchestrator.refresh_international_fundamentals(p)
    monkeypatch.setattr("app.portfolio_orchestration.international_provider_for", lambda *_args, **_kwargs: Provider(error=RuntimeError("verification failed")))
    with pytest.raises(RuntimeError): await orchestrator.refresh_international_fundamentals(p)
    assert calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_trusted_sec_cik_bypasses_ticker_discovery(monkeypatch):
    p = profile(provider_instrument_ids={"SEC_CIK": "0000789019"}); calls = []
    async def forbidden(*_args): raise AssertionError("CIK discovery must not run")
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"facts": {"us-gaap": {}}})
    provider = SecEdgarFundamentalProvider(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(provider, "_resolve_cik", forbidden)
    result = await provider.collect(p)
    assert result.verified_provider_ids == {"SEC_CIK": "0000789019"}
    assert calls == ["/api/xbrl/companyfacts/CIK0000789019.json"]


@pytest.mark.asyncio
async def test_trusted_eodhd_symbol_bypasses_ticker_fallback():
    p = profile(provider_instrument_ids={"EODHD": "AIXA.F"}); paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"General": {"Exchange": "XNAS", "CurrencyCode": "USD"}, "Financials": {}})
    provider = EodhdFundamentalProvider(Settings(eodhd_api_key="x", eodhd_base_url="https://eod.test"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await provider.collect(p)
    assert result.verified_provider_ids == {"EODHD": "AIXA.F"}
    assert paths == ["/fundamentals/AIXA.F"]


@pytest.mark.asyncio
async def test_india_never_enters_mapping_boundary(monkeypatch):
    p = profile(country="IN", exchange="NSE", mic="XNSE")
    monkeypatch.setattr("app.portfolio_orchestration.international_provider_for", lambda *_args, **_kwargs: None)
    orchestrator = PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings())
    assert await orchestrator.refresh_international_fundamentals(p) is None
