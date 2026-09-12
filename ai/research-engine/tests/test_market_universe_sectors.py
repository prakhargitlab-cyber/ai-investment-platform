from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.main as main
from app.market_universe_sectors import canonical_sector_counts


INDIA_SECTOR_COUNTS = {
    "Communication Services": 14,
    "Consumer Discretionary": 88,
    "Consumer Staples": 28,
    "Energy": 17,
    "Financials": 101,
    "Healthcare": 48,
    "Industrials": 75,
    "Materials": 55,
    "Real Estate": 11,
    "Technology": 27,
    "Utilities": 17,
}


def _listing(sector):
    instrument_id = uuid4()
    return SimpleNamespace(as_payload=lambda: {
        "globalInstrumentId": str(instrument_id),
        "canonicalName": f"Company {instrument_id}",
        "ticker": str(instrument_id)[:8],
        "exchange": "NSE",
        "country": "IN",
        "currency": "INR",
        "canonicalSector": sector,
    })


def test_canonical_sector_counts_excludes_unmapped_deduplicates_and_sorts():
    first, second = str(uuid4()), str(uuid4())
    instruments = [
        {"globalInstrumentId": first},
        {"globalInstrumentId": first},
        {"globalInstrumentId": second},
        {"globalInstrumentId": str(uuid4())},
    ]
    result = canonical_sector_counts(instruments, {
        first: "Materials",
        second: "Consumer Staples",
    })
    assert result == [
        {"name": "Consumer Staples", "instrumentCount": 1},
        {"name": "Materials", "instrumentCount": 1},
    ]
    assert "Basic Materials" not in str(result)


@pytest.mark.asyncio
async def test_india_sector_discovery_uses_exact_durable_canonical_values_and_counts(monkeypatch):
    listings = [
        _listing(sector)
        for sector, count in INDIA_SECTOR_COUNTS.items()
        for _ in range(count)
    ] + [_listing(None) for _ in range(17)]
    observed = {}

    async def durable_listings(**kwargs):
        observed.update(kwargs)
        return listings

    monkeypatch.setattr(main.india_market_universe_provider, "listings", durable_listings)
    monkeypatch.setattr(
        main.portfolio_orchestrator,
        "active_global_equities",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("India discovery must use Nifty cache")),
    )
    monkeypatch.setattr(
        main.repository,
        "structured_market_snapshots_for",
        lambda _ids: (_ for _ in ()).throw(AssertionError("India sectors are canonical upstream")),
    )

    response = await main.market_universe_sectors(
        region="INDIA",
        x_correlation_id="sector-discovery-test",
        x_aip_user_id="user",
        x_aip_user_issuer="gateway",
        x_aip_user_subject="subject",
    )

    assert response == {
        "region": "INDIA",
        "sectors": [
            {"name": name, "instrumentCount": count}
            for name, count in INDIA_SECTOR_COUNTS.items()
        ],
    }
    assert observed["correlation_id"] == "sector-discovery-test"
    assert observed["identity_headers"]["X-AIP-User-Id"] == "user"
    assert "Basic Materials" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["USA", "EUROPE"])
async def test_region_without_durable_sector_classification_returns_empty(monkeypatch, region):
    async def durable_equities(**_kwargs):
        return []

    monkeypatch.setattr(main.portfolio_orchestrator, "active_global_equities", durable_equities)
    monkeypatch.setattr(
        main.repository,
        "structured_market_snapshots_for",
        lambda ids: (_ for _ in ()).throw(AssertionError("empty universe needs no provider or snapshot work"))
        if ids else {},
    )
    response = await main.market_universe_sectors(
        region=region,
        x_aip_user_id="user",
        x_aip_user_issuer="gateway",
        x_aip_user_subject="subject",
    )
    assert response == {"region": region, "sectors": []}


def test_sector_discovery_route_is_read_only_get():
    routes = {
        (route.path, method)
        for route in main.app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/v1/research/market-universe/sectors", "GET") in routes
    assert ("/api/v1/research/market-universe/sectors", "POST") not in routes
