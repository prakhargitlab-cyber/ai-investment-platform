from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

import app.main as main
from app.models import PortfolioResearchCompany
from app.portfolio_orchestration import (
    PortfolioResearchOrchestrator,
    WatchlistNotFoundError,
    WatchlistRegionMismatchError,
)
from app.settings import Settings
from app.watchlists import AddWatchlistInstrumentRequest, EnsureDefaultWatchlistRequest, watchlist_research_projection


@pytest.mark.asyncio
async def test_portfolio_watchlist_client_forwards_user_identity_and_uses_provider_free_contracts(caplog):
    watchlist_id = uuid4()
    instrument_id = uuid4()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/default/ensure"):
            return httpx.Response(200, json={"watchlistId": str(watchlist_id), "name": "WATCHLIST-IND", "region": "INDIA"})
        if request.method == "POST":
            return httpx.Response(200, json={"globalInstrumentId": str(instrument_id)})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        orchestrator = PortfolioResearchOrchestrator(
            SimpleNamespace(), Settings(portfolio_service_base_url="http://portfolio-service"), client=client
        )
        identity = {"X-AIP-User-Id": "browser-user", "X-AIP-User-Subject": "subject"}
        ensured = await orchestrator.ensure_default_watchlist("INDIA", identity_headers=identity)
        await orchestrator.add_watchlist_instrument(
            watchlist_id,
            {"globalInstrumentId": str(instrument_id), "sourcePeriod": "WEEK", "sourcePerformancePct": 13.43},
            identity_headers=identity,
        )
        await orchestrator.remove_watchlist_instrument(watchlist_id, instrument_id, identity_headers=identity)

    assert ensured["name"] == "WATCHLIST-IND"
    assert [call.url.path for call in calls] == [
        "/api/v1/watchlists/default/ensure",
        f"/api/v1/watchlists/{watchlist_id}/instruments",
        f"/api/v1/watchlists/{watchlist_id}/instruments/{instrument_id}",
    ]
    assert all(call.headers["X-AIP-User-Id"] == "browser-user" for call in calls)
    assert all("portfolio" not in call.url.path for call in calls)
    assert "browser-user" not in caplog.text


@pytest.mark.asyncio
async def test_watchlist_client_preserves_region_mismatch_and_ownership_errors():
    watchlist_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"code": "WATCHLIST_REGION_MISMATCH"})
        return httpx.Response(404, json={"code": "WATCHLIST_NOT_FOUND"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        orchestrator = PortfolioResearchOrchestrator(
            SimpleNamespace(), Settings(portfolio_service_base_url="http://portfolio-service"), client=client
        )
        with pytest.raises(WatchlistRegionMismatchError):
            await orchestrator.add_watchlist_instrument(watchlist_id, {"globalInstrumentId": str(uuid4())})
        with pytest.raises(WatchlistNotFoundError):
            await orchestrator.watchlist(watchlist_id)


@pytest.mark.asyncio
async def test_watchlist_research_projection_is_non_held_and_reuses_durable_company_read_model():
    watchlist_id = uuid4()
    instrument_id = uuid4()
    calls = []
    metadata = {
        "globalInstrumentId": str(instrument_id),
        "canonicalName": "Prime Focus Ltd.",
        "primarySymbol": "PFOCUS",
        "primaryExchange": "NSE",
        "country": "IN",
        "currency": "INR",
        "assetType": "EQUITY",
        "providerMappings": [],
    }

    async def read_state(value, **kwargs):
        calls.append((value, kwargs))
        return PortfolioResearchCompany(
            instrument_id=instrument_id,
            company_name="Prime Focus Ltd.",
            ticker="PFOCUS",
            exchange="NSE",
            status="RESOLVED_RESEARCH_AVAILABLE",
        )

    projected = await watchlist_research_projection(
        SimpleNamespace(read_global_company_state=read_state),
        {
            "watchlist": {"watchlistId": str(watchlist_id), "name": "WATCHLIST-IND", "region": "INDIA"},
            "instruments": [{
                "globalInstrumentId": str(instrument_id), "instrument": metadata,
                "sourcePeriod": "WEEK", "sourcePerformancePct": 13.43,
            }],
        },
        identity_headers={"X-AIP-User-Id": "owner"},
    )

    row = projected["instruments"][0]
    assert row["globalInstrumentId"] == str(instrument_id)
    assert row["held"] is False
    assert not {"quantity", "averageBuyPrice", "costBasis", "investedAmount", "portfolioPnl", "allocation", "broker"} & row.keys()
    assert row["sourcePeriod"] == "WEEK" and row["sourcePerformancePct"] == 13.43
    assert row["company"]["instrumentId"] == str(instrument_id)
    assert calls[0][1]["metadata"] is metadata
    assert "refresh" not in repr(calls).lower()


@pytest.mark.asyncio
async def test_watchlist_routes_are_authenticated_and_membership_payload_is_provider_neutral(monkeypatch):
    watchlist_id = uuid4()
    instrument_id = uuid4()
    observed = {}

    class Stub:
        async def ensure_default_watchlist(self, region, **kwargs):
            observed["ensure"] = (region, kwargs)
            return {"watchlistId": str(watchlist_id), "name": "WATCHLIST-IND", "region": region}

        async def add_watchlist_instrument(self, value, payload, **kwargs):
            observed["add"] = (value, payload, kwargs)
            return {"globalInstrumentId": payload["globalInstrumentId"]}

    monkeypatch.setattr(main, "portfolio_orchestrator", Stub())
    identity = dict(x_aip_user_id="owner", x_aip_user_issuer="gateway", x_aip_user_subject="subject")
    ensured = await main.ensure_default_research_watchlist(
        EnsureDefaultWatchlistRequest(region="INDIA"), **identity
    )
    request = AddWatchlistInstrumentRequest.model_validate({
        "globalInstrumentId": str(instrument_id), "sourcePeriod": "WEEK", "sourcePerformancePct": "13.43"
    })
    await main.add_research_watchlist_instrument(watchlist_id, request, **identity)

    assert ensured["name"] == "WATCHLIST-IND"
    assert observed["ensure"][1]["identity_headers"]["X-AIP-User-Id"] == "owner"
    assert observed["add"][1] == {
        "globalInstrumentId": str(instrument_id), "sourcePeriod": "WEEK", "sourcePerformancePct": 13.43
    }
    assert not any(value in repr(observed["add"][1]) for value in ("NSE", "SEC", "EODHD", "portfolioId", "quantity"))

    with pytest.raises(HTTPException) as denied:
        await main.ensure_default_research_watchlist(
            EnsureDefaultWatchlistRequest(region="USA"),
            x_aip_user_id=None, x_aip_user_issuer=None, x_aip_user_subject=None,
        )
    assert denied.value.status_code == 401


def test_watchlist_routes_are_explicit_and_distinct_from_targeted_research_routes():
    routes = {(route.path, method) for route in main.app.routes for method in getattr(route, "methods", set())}
    assert ("/api/v1/research/watchlists", "GET") in routes
    assert ("/api/v1/research/watchlists/default/ensure", "POST") in routes
    assert ("/api/v1/research/watchlists/{watchlist_id}/instruments", "POST") in routes
    assert ("/api/v1/research/watchlists/{watchlist_id}/instruments/{instrument_id}", "DELETE") in routes
    assert ("/api/v1/research/watchlists/{watchlist_id}/research", "GET") in routes
    assert ("/api/v1/research/readiness/{global_instrument_id}", "GET") in routes
    assert ("/api/v1/research/readiness/{global_instrument_id}/ensure", "POST") in routes
    assert ("/api/v1/research/prefetch", "POST") not in routes
