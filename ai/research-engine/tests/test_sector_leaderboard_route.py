import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import app.main as main

@pytest.mark.asyncio
async def test_sector_route_joins_only_active_universe_and_skips_missing_inputs(monkeypatch):
    valid, missing, research_only = uuid4(), uuid4(), uuid4()
    class Fact: value = "Technology"
    class Snapshot: facts = {"sector": Fact()}
    class Record: snapshot = Snapshot()
    class Score: overall_score = 80; category_evidence = {}
    observed = {}
    async def active_global_equities(**kwargs):
        observed.update(kwargs)
        return [
            {"globalInstrumentId": str(valid), "canonicalName": "Valid", "ticker": "VAL", "exchange": "XNAS", "country": "US", "currency": "USD"},
            {"globalInstrumentId": str(missing), "canonicalName": "Missing", "ticker": "MISS", "exchange": "XNAS", "country": "US", "currency": "USD"},
        ]
    monkeypatch.setattr(main.portfolio_orchestrator, "active_global_equities", active_global_equities)
    monkeypatch.setattr(main.repository, "profile", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("leaderboard must not require a process-local profile")))
    monkeypatch.setattr(main.repository, "summary", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("leaderboard must not use profile-bound summary")))
    monkeypatch.setattr(main.repository, "persisted_canonical_read_model_score", lambda key: Score() if key == valid else None)
    monkeypatch.setattr(main.repository, "structured_market_snapshots_for", lambda _ids: {valid: [Record()]})
    result = await main.sector_leaderboard(x_correlation_id="test-correlation", x_aip_user_id="test-user")
    assert result["sectors"][0]["stocks"][0]["globalInstrumentId"] == str(valid)
    assert str(research_only) not in str(result)
    assert observed["correlation_id"] == "test-correlation"
    assert observed["identity_headers"]["X-AIP-User-Id"] == "test-user"

async def _async(value): return value


@pytest.mark.asyncio
async def test_sector_performance_route_uses_authenticated_universe_and_persisted_prices_only(monkeypatch):
    instrument_id = uuid4()
    class Fact: value = "Technology"
    class Snapshot: facts = {"sector": Fact()}
    class Record: snapshot = Snapshot()
    observed = {}
    async def active_global_equities(**kwargs):
        observed.update(kwargs)
        return [{"globalInstrumentId": str(instrument_id), "canonicalName": "Persisted", "ticker": "PST", "exchange": "XETR", "country": "DE", "currency": "EUR"}]
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    prices = [
        SimpleNamespace(observed_at=now - timedelta(days=8), price=Decimal("100"), currency="EUR"),
        SimpleNamespace(observed_at=now, price=Decimal("110"), currency="EUR"),
    ]
    monkeypatch.setattr(main.portfolio_orchestrator, "active_global_equities", active_global_equities)
    monkeypatch.setattr(main.repository, "structured_market_snapshots_for", lambda ids: {instrument_id: [Record()]})
    monkeypatch.setattr(main.repository, "market_price_observations_for", lambda ids: {instrument_id: prices})
    monkeypatch.setattr(main.repository, "persisted_canonical_read_model_score", lambda *_args: (_ for _ in ()).throw(AssertionError("performance must not use research score")))
    result = await main.sector_performance(region="EUROPE", sector="Technology", period="WEEK", limit=5, x_correlation_id="performance-test", x_aip_user_id="test-user")
    assert result["bestPerformers"][0]["globalInstrumentId"] == str(instrument_id)
    assert result["bestPerformers"][0]["performancePct"] == Decimal("10")
    assert result["worstPerformers"][0]["globalInstrumentId"] == str(instrument_id)
    assert observed["identity_headers"]["X-AIP-User-Id"] == "test-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("sector", [
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Healthcare",
    "Industrials",
    "Materials",
    "Real Estate",
    "Technology",
    "Utilities",
])
async def test_every_india_canonical_sector_ranks_full_week_universe_before_top_and_worst_limit(
    monkeypatch, sector
):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    instrument_ids = [uuid4() for _ in range(7)]
    payloads = [
        {
            "globalInstrumentId": str(instrument_id),
            "canonicalName": f"{sector} {index}",
            "ticker": f"S{index}",
            "exchange": "NSE",
            "country": "IN",
            "currency": "INR",
            "canonicalSector": sector,
        }
        for index, instrument_id in enumerate(instrument_ids)
    ]
    listings = [SimpleNamespace(as_payload=lambda value=value: value) for value in payloads]
    prices = {
        instrument_id: [
            SimpleNamespace(observed_at=now - timedelta(days=8), price=Decimal("100"), currency="INR"),
            SimpleNamespace(observed_at=now, price=Decimal(str(101 + index)), currency="INR"),
        ]
        for index, instrument_id in enumerate(instrument_ids)
    }

    async def durable_india_universe(**_kwargs):
        return listings

    monkeypatch.setattr(main.india_market_universe_provider, "listings", durable_india_universe)
    monkeypatch.setattr(main.repository, "market_price_observations_for", lambda ids: {
        instrument_id: prices[instrument_id] for instrument_id in ids
    })
    monkeypatch.setattr(
        main.repository,
        "structured_market_snapshots_for",
        lambda _ids: (_ for _ in ()).throw(AssertionError("India sector join is durable upstream")),
    )

    result = await main.sector_performance(
        region="INDIA",
        sector=sector,
        period="WEEK",
        limit=5,
        x_aip_user_id="test-user",
    )

    assert result["sector"] == sector
    assert len(result["bestPerformers"]) == 5
    assert len(result["worstPerformers"]) == 5
    assert [row["globalInstrumentId"] for row in result["bestPerformers"]] == [
        str(value) for value in reversed(instrument_ids[2:])
    ]
    assert [row["globalInstrumentId"] for row in result["worstPerformers"]] == [
        str(value) for value in instrument_ids[:5]
    ]
