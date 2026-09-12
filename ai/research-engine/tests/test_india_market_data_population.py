from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

import app.main as main
from app.market_data_population import IndiaMarketDataPopulationJobs, _verified_yahoo_nse_mapping
from app.market_universe import IndiaMarketUniverseProvider, MarketUniverseInstrument, MarketUniverseUnavailable
from app.models import MarketPriceObservation
from app.portfolio_orchestration import PortfolioResearchOrchestrator, PortfolioServiceUnavailableError
from app.repository import ResearchRepository
from app.settings import Settings


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _reference_row(instrument_id: UUID, *, sector="Technology", symbol="INFY", status="ACTIVE", asset_type="EQUITY"):
    return {
        "globalInstrumentId": str(instrument_id),
        "symbol": symbol,
        "companyName": f"{symbol} Limited",
        "isin": "INE009A01021",
        "exchange": "NSE",
        "mic": "XNSE",
        "country": "IN",
        "currency": "INR",
        "assetType": asset_type,
        "status": status,
        "officialIndustry": "Information Technology" if sector else "Services",
        "canonicalSector": sector,
        "source": "NSE_INDICES_NIFTY500",
        "retrievedAt": NOW.isoformat(),
    }


def _instrument(instrument_id: UUID, *, ticker="INFY", sector="Technology") -> MarketUniverseInstrument:
    return MarketUniverseInstrument(
        global_instrument_id=instrument_id,
        ticker=ticker,
        company_name=f"{ticker} Limited",
        isin="INE009A01021",
        exchange="NSE",
        mic="XNSE",
        country="IN",
        currency="INR",
        asset_type="EQUITY",
        status="ACTIVE",
        region="INDIA",
        canonical_sector=sector,
        official_industry="Information Technology" if sector else "Services",
        source="NSE_INDICES_NIFTY500",
        retrieved_at=NOW,
    )


class _Universe:
    def __init__(self, values):
        self.values = values
        self.calls = []

    async def listings(self, **kwargs):
        self.calls.append(kwargs)
        return self.values


class _Repository:
    def __init__(self):
        self.rows = {}

    async def market_price_observations_for_instruments(self, instrument_ids):
        return {instrument_id: list(self.rows.get(instrument_id, {}).values()) for instrument_id in instrument_ids}

    async def upsert_market_price_observation_async(self, observation):
        key = (observation.provider, observation.observed_at)
        self.rows.setdefault(observation.instrument_id, {})[key] = observation


class _Orchestrator:
    def __init__(self, metadata, reconciled=None):
        self.metadata = metadata
        self.reconciled = reconciled or metadata
        self.get_calls = []
        self.reconcile_calls = []

    async def global_instrument_metadata(self, instrument_id, **kwargs):
        self.get_calls.append((instrument_id, kwargs))
        return self.metadata.get(instrument_id, {"providerMappings": []})

    async def reconcile_global_instrument_metadata(self, instrument_id, **kwargs):
        self.reconcile_calls.append((instrument_id, kwargs))
        return self.reconciled.get(instrument_id, {"providerMappings": []})


class _HistoricalProvider:
    provider_name = "YAHOO_FINANCE"

    def __init__(self, *, fail_symbols=(), empty_symbols=()):
        self.fail_symbols = set(fail_symbols)
        self.empty_symbols = set(empty_symbols)
        self.calls = []

    async def closes(self, instrument, *, start, end):
        self.calls.append((dict(instrument), start, end))
        symbol = instrument["structuredProviderTicker"]
        if symbol in self.fail_symbols:
            raise RuntimeError("fixture provider failure")
        if symbol in self.empty_symbols:
            return []
        return [MarketPriceObservation(
            instrument_id=UUID(instrument["globalInstrumentId"]),
            observed_at=NOW - timedelta(days=1),
            price=Decimal("100"),
            currency="INR",
            provider=self.provider_name,
            source_url=f"fixture://{symbol}",
            retrieved_at=NOW,
        )]


def _verified(symbol):
    return {"providerMappings": [{
        "provider": "YAHOO_FINANCE", "providerSymbol": symbol, "status": "VERIFIED"
    }]}


def _settings():
    return Settings(
        market_data_population_batch_size=2,
        market_data_population_request_interval_seconds=0,
        market_data_population_initial_lookback_days=400,
    )


@pytest.mark.asyncio
async def test_india_adapter_reads_all_nifty_pages_without_active_equity_fallback():
    first_id, later_id, null_sector_id = uuid4(), uuid4(), uuid4()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/market-universe/india/nifty500"
        page = request.url.params["page"]
        if page == "0":
            rows = [_reference_row(first_id)]
        else:
            rows = [_reference_row(later_id, symbol="TCS"), _reference_row(null_sector_id, symbol="SERV", sector=None)]
        return httpx.Response(200, json={"instruments": rows, "page": int(page), "size": 500, "totalElements": 3})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    orchestrator = PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client)
    values = await IndiaMarketUniverseProvider(orchestrator).listings(identity_headers={"X-AIP-User-Id": "admin"})

    assert [request.url.params["page"] for request in requests] == ["0", "1"]
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.params["size"] == "500" for request in requests)
    assert [value.global_instrument_id for value in values] == [first_id, later_id, null_sector_id]
    assert values[1].ticker == "TCS" and values[1].canonical_sector == "Technology"
    assert values[2].canonical_sector is None
    await client.aclose()


@pytest.mark.asyncio
async def test_india_adapter_excludes_invalid_identity_inactive_and_non_equity_rows():
    valid_id = uuid4()
    rows = [
        _reference_row(valid_id),
        _reference_row(uuid4(), status="INACTIVE"),
        _reference_row(uuid4(), asset_type="ETF"),
        {**_reference_row(uuid4()), "globalInstrumentId": None},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"instruments": rows, "totalElements": len(rows)})
    ))
    provider = IndiaMarketUniverseProvider(PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client))
    assert [value.global_instrument_id for value in await provider.listings()] == [valid_id]
    await client.aclose()


@pytest.mark.asyncio
async def test_india_adapter_never_returns_partial_page_on_universe_failure():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"instruments": [_reference_row(uuid4())], "totalElements": 2})
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = IndiaMarketUniverseProvider(PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client))
    with pytest.raises(MarketUniverseUnavailable, match="INDIA_NIFTY500_UNIVERSE_UNAVAILABLE"):
        await provider.listings()
    assert calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_verified_yahoo_mapping_populates_profileless_nonportfolio_stock_with_400_day_initial_window():
    instrument_id = uuid4()
    universe = _Universe([_instrument(instrument_id)])
    orchestrator = _Orchestrator({instrument_id: _verified("INFY.NS")})
    repository = _Repository()
    historical = _HistoricalProvider()
    jobs = IndiaMarketDataPopulationJobs(
        repository, universe, orchestrator, _settings(), historical_provider=historical, clock=lambda: NOW
    )

    identity = {
        "X-AIP-User-Id": "internal-market-data",
        "X-AIP-User-Issuer": "aip-internal",
        "X-AIP-User-Subject": "research-engine-market-data",
        "X-AIP-User-Roles": "ADMIN",
    }
    submitted = await jobs.submit(identity_headers=identity, correlation_id="test")
    result = await jobs.wait(submitted["jobId"])

    assert result["status"] == "COMPLETED"
    assert (result["universeSize"], result["attempted"], result["populated"], result["skipped"], result["failed"]) == (1, 1, 1, 0, 0)
    assert len(historical.calls) == 1
    payload, start, end = historical.calls[0]
    assert payload["globalInstrumentId"] == str(instrument_id)
    assert payload["structuredProviderTicker"] == "INFY.NS"
    assert NOW - start == timedelta(days=400) and end == NOW + timedelta(days=1)
    assert orchestrator.get_calls[0][1] == {
        "correlation_id": "test",
        "identity_headers": identity,
    }
    assert orchestrator.reconcile_calls == []
    assert len(repository.rows[instrument_id]) == 1

    repeated = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    repeated_result = await jobs.wait(repeated["jobId"])
    assert repeated_result["populated"] == 1
    assert historical.calls[1][1] == NOW - timedelta(days=400)
    assert len(repository.rows[instrument_id]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_days_ago", "expected_start_days_ago"),
    [
        pytest.param([0], 400, id="one-recent-observation"),
        pytest.param([30, 1], 400, id="shallow-recent-history"),
        pytest.param([366, 1], 0, id="year-capable-history"),
        pytest.param([500, 100], 99, id="year-capable-history-with-stale-latest"),
    ],
)
async def test_population_uses_year_depth_not_any_existing_row_for_incremental_start(
    observed_days_ago,
    expected_start_days_ago,
):
    instrument_id = uuid4()
    repository = _Repository()
    for days_ago in observed_days_ago:
        await repository.upsert_market_price_observation_async(MarketPriceObservation(
            instrument_id=instrument_id,
            observed_at=NOW - timedelta(days=days_ago),
            price=Decimal("90"),
            currency="INR",
            provider="YAHOO_FINANCE",
            source_url="fixture://existing",
            retrieved_at=NOW,
        ))
    historical = _HistoricalProvider()
    jobs = IndiaMarketDataPopulationJobs(
        repository,
        _Universe([_instrument(instrument_id)]),
        _Orchestrator({instrument_id: _verified("INFY.NS")}),
        _settings(),
        historical_provider=historical,
        clock=lambda: NOW,
    )

    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])

    assert result["status"] == "COMPLETED"
    assert result["populated"] == 1
    assert historical.calls[0][1] == NOW - timedelta(days=expected_start_days_ago)
    assert historical.calls[0][2] == NOW + timedelta(days=1)


@pytest.mark.asyncio
async def test_shallow_existing_history_with_zero_provider_rows_is_not_counted_as_populated():
    instrument_id = uuid4()
    repository = _Repository()
    await repository.upsert_market_price_observation_async(MarketPriceObservation(
        instrument_id=instrument_id,
        observed_at=NOW - timedelta(days=1),
        price=Decimal("90"),
        currency="INR",
        provider="YAHOO_FINANCE",
        source_url="fixture://existing",
        retrieved_at=NOW,
    ))
    historical = _HistoricalProvider(empty_symbols={"INFY.NS"})
    jobs = IndiaMarketDataPopulationJobs(
        repository,
        _Universe([_instrument(instrument_id)]),
        _Orchestrator({instrument_id: _verified("INFY.NS")}),
        _settings(),
        historical_provider=historical,
        clock=lambda: NOW,
    )

    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])

    assert historical.calls[0][1] == NOW - timedelta(days=400)
    assert result["status"] == "COMPLETED"
    assert result["populated"] == 0
    assert result["skipped"] == 1
    assert result["reasons"] == {"NO_VALID_HISTORY": 1}


@pytest.mark.asyncio
async def test_shallow_existing_history_with_returned_backfill_rows_is_populated_and_persisted():
    instrument_id = uuid4()
    repository = _Repository()
    recent = MarketPriceObservation(
        instrument_id=instrument_id,
        observed_at=NOW - timedelta(days=1),
        price=Decimal("90"),
        currency="INR",
        provider="YAHOO_FINANCE",
        source_url="fixture://existing",
        retrieved_at=NOW,
    )
    await repository.upsert_market_price_observation_async(recent)

    class BackfillProvider(_HistoricalProvider):
        async def closes(self, instrument, *, start, end):
            self.calls.append((dict(instrument), start, end))
            return [
                MarketPriceObservation(
                    instrument_id=instrument_id,
                    observed_at=NOW - timedelta(days=370),
                    price=Decimal("75"),
                    currency="INR",
                    provider=self.provider_name,
                    source_url="fixture://history",
                    retrieved_at=NOW,
                ),
                MarketPriceObservation(
                    instrument_id=instrument_id,
                    observed_at=recent.observed_at,
                    price=Decimal("100"),
                    currency="INR",
                    provider=self.provider_name,
                    source_url="fixture://history",
                    retrieved_at=NOW,
                ),
            ]

    historical = BackfillProvider()
    jobs = IndiaMarketDataPopulationJobs(
        repository,
        _Universe([_instrument(instrument_id)]),
        _Orchestrator({instrument_id: _verified("INFY.NS")}),
        _settings(),
        historical_provider=historical,
        clock=lambda: NOW,
    )

    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])

    assert historical.calls[0][1] == NOW - timedelta(days=400)
    assert result["populated"] == 1
    assert result["skipped"] == 0
    assert result["reasons"] == {}
    assert len(repository.rows[instrument_id]) == 2
    assert min(value.observed_at for value in repository.rows[instrument_id].values()) == NOW - timedelta(days=370)


@pytest.mark.asyncio
async def test_overlapping_initial_backfill_uses_idempotent_observation_upsert():
    instrument_id = uuid4()
    repository = _Repository()
    recent = MarketPriceObservation(
        instrument_id=instrument_id,
        observed_at=NOW - timedelta(days=1),
        price=Decimal("90"),
        currency="INR",
        provider="YAHOO_FINANCE",
        source_url="fixture://existing",
        retrieved_at=NOW,
    )
    await repository.upsert_market_price_observation_async(recent)
    historical = _HistoricalProvider()
    jobs = IndiaMarketDataPopulationJobs(
        repository,
        _Universe([_instrument(instrument_id)]),
        _Orchestrator({instrument_id: _verified("INFY.NS")}),
        _settings(),
        historical_provider=historical,
        clock=lambda: NOW,
    )

    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])

    assert result["populated"] == 1
    assert historical.calls[0][1] == NOW - timedelta(days=400)
    assert len(repository.rows[instrument_id]) == 1
    stored = next(iter(repository.rows[instrument_id].values()))
    assert stored.observed_at == recent.observed_at
    assert stored.price == Decimal("100")


@pytest.mark.asyncio
async def test_year_capable_current_daily_series_is_a_successful_provider_date_noop():
    instrument_id = uuid4()
    repository = _Repository()
    for observed_at in (NOW - timedelta(days=365), NOW):
        await repository.upsert_market_price_observation_async(MarketPriceObservation(
            instrument_id=instrument_id,
            observed_at=observed_at,
            price=Decimal("100"),
            currency="INR",
            provider="YAHOO_FINANCE",
            source_url="fixture://existing",
            retrieved_at=NOW,
        ))
    historical = _HistoricalProvider(fail_symbols={"INFY.NS"})
    jobs = IndiaMarketDataPopulationJobs(
        repository,
        _Universe([_instrument(instrument_id)]),
        _Orchestrator({instrument_id: _verified("INFY.NS")}),
        _settings(),
        historical_provider=historical,
        clock=lambda: NOW + timedelta(hours=10),
    )

    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])

    assert historical.calls == []
    assert result["populated"] == 1
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["reasons"] == {}


@pytest.mark.asyncio
async def test_missing_mapping_uses_owner_reconciliation_but_never_trusts_resolved_or_constructs_ns():
    instrument_id = uuid4()
    resolved_only = {instrument_id: {"providerMappings": [{
        "provider": "YAHOO_FINANCE", "providerSymbol": "INFY.NS", "status": "RESOLVED"
    }]}}
    orchestrator = _Orchestrator({instrument_id: {"providerMappings": []}}, resolved_only)
    historical = _HistoricalProvider()
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), _Universe([_instrument(instrument_id)]), orchestrator, _settings(),
        historical_provider=historical, clock=lambda: NOW,
    )

    identity = {
        "X-AIP-User-Id": "internal-market-data",
        "X-AIP-User-Issuer": "aip-internal",
        "X-AIP-User-Subject": "research-engine-market-data",
        "X-AIP-User-Roles": "ADMIN",
    }
    job = await jobs.submit(identity_headers=identity)
    result = await jobs.wait(job["jobId"])

    assert result["reasons"] == {"NO_VERIFIED_HISTORICAL_MAPPING": 1}
    assert result["skipped"] == 1 and result["populated"] == 0
    assert len(orchestrator.reconcile_calls) == 1
    assert orchestrator.reconcile_calls[0][1]["identity_headers"] == identity
    assert historical.calls == []
    assert _verified_yahoo_nse_mapping({"providerMappings": []}) is None


@pytest.mark.asyncio
async def test_owner_reconciliation_can_supply_verified_mapping_without_profile_or_fundamentals():
    instrument_id = uuid4()
    orchestrator = _Orchestrator(
        {instrument_id: {"providerMappings": []}},
        {instrument_id: _verified("RELIANCE.NS")},
    )
    historical = _HistoricalProvider()
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), _Universe([_instrument(instrument_id, ticker="RELIANCE")]), orchestrator, _settings(),
        historical_provider=historical, clock=lambda: NOW,
    )
    job = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(job["jobId"])
    assert result["populated"] == 1
    assert historical.calls[0][0]["structuredProviderTicker"] == "RELIANCE.NS"
    assert len(orchestrator.reconcile_calls) == 1


@pytest.mark.asyncio
async def test_null_sector_and_no_history_reasons_are_explicit(caplog):
    caplog.set_level("INFO", logger="app.market_data_population")
    classified_id, unclassified_id = uuid4(), uuid4()
    universe = _Universe([
        _instrument(unclassified_id, ticker="SERV", sector=None),
        _instrument(classified_id, ticker="EMPTY"),
    ])
    historical = _HistoricalProvider(empty_symbols={"EMPTY.NS"})
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), universe, _Orchestrator({classified_id: _verified("EMPTY.NS")}), _settings(),
        historical_provider=historical, clock=lambda: NOW,
    )
    job = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(job["jobId"])
    assert result["reasons"] == {"NO_CANONICAL_SECTOR": 1, "NO_VALID_HISTORY": 1}
    assert result["universeSize"] == 2 and result["attempted"] == 1 and result["skipped"] == 2
    assert f"event=POPULATION_JOB_STARTED jobId={job['jobId']} universeSize=2" in caplog.text
    assert f"event=POPULATION_JOB_COMPLETED jobId={job['jobId']} status=COMPLETED" in caplog.text
    assert "attempted=1 populated=0 skipped=2 failed=0" in caplog.text
    assert "NO_CANONICAL_SECTOR" in caplog.text
    assert "NO_VALID_HISTORY" in caplog.text


@pytest.mark.asyncio
async def test_one_yahoo_failure_does_not_abort_remaining_bounded_job(caplog):
    caplog.set_level("INFO", logger="app.market_data_population")
    bad_id, good_id = uuid4(), uuid4()
    universe = _Universe([_instrument(bad_id, ticker="BAD"), _instrument(good_id, ticker="GOOD")])
    orchestrator = _Orchestrator({bad_id: _verified("BAD.NS"), good_id: _verified("GOOD.NS")})
    historical = _HistoricalProvider(fail_symbols={"BAD.NS"})
    repository = _Repository()
    jobs = IndiaMarketDataPopulationJobs(
        repository, universe, orchestrator, _settings(), historical_provider=historical, clock=lambda: NOW
    )
    job = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(job["jobId"])
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert result["attempted"] == 2 and result["failed"] == 1 and result["populated"] == 1
    assert result["reasons"] == {"PROVIDER_UNAVAILABLE": 1}
    assert "HISTORICAL_PROVIDER_UNAVAILABLE" in caplog.text
    assert "PORTFOLIO_METADATA_UNAVAILABLE" not in caplog.text
    assert "HISTORICAL_PROVIDER_UNAVAILABLE:RuntimeError" in caplog.text
    assert good_id in repository.rows


@pytest.mark.asyncio
async def test_metadata_unauthorized_is_diagnosed_separately_without_logging_identity(caplog):
    caplog.set_level("INFO", logger="app.market_data_population")
    instrument_id = uuid4()

    class MetadataUnavailable(_Orchestrator):
        async def global_instrument_metadata(self, requested_id, **kwargs):
            self.get_calls.append((requested_id, kwargs))
            request = httpx.Request("GET", "http://portfolio-service/api/v1/instruments/fixture")
            response = httpx.Response(401, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise PortfolioServiceUnavailableError("fixture metadata unavailable") from exc

    orchestrator = MetadataUnavailable({})
    identity = {
        "X-AIP-User-Id": "sensitive-user-id",
        "X-AIP-User-Subject": "sensitive-subject",
        "X-AIP-User-Roles": "ADMIN",
        "Authorization": "Bearer sensitive-token",
    }
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), _Universe([_instrument(instrument_id)]), orchestrator, _settings(),
        historical_provider=_HistoricalProvider(), clock=lambda: NOW,
    )

    job = await jobs.submit(identity_headers=identity, correlation_id="metadata-auth-test")
    result = await jobs.wait(job["jobId"])

    assert orchestrator.get_calls[0][1]["identity_headers"] == identity
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert result["reasons"] == {"PROVIDER_UNAVAILABLE": 1}
    assert "failureStages" not in result
    assert "PORTFOLIO_METADATA_UNAVAILABLE" in caplog.text
    assert "PORTFOLIO_METADATA_UNAVAILABLE:HTTPStatusError" in caplog.text
    assert "HISTORICAL_PROVIDER_UNAVAILABLE" not in caplog.text
    assert "sensitive-user-id" not in caplog.text
    assert "sensitive-subject" not in caplog.text
    assert "sensitive-token" not in caplog.text


@pytest.mark.asyncio
async def test_reconciliation_unavailable_has_distinct_aggregate_diagnostic(caplog):
    caplog.set_level("INFO", logger="app.market_data_population")
    instrument_id = uuid4()

    class ReconcileUnavailable(_Orchestrator):
        async def reconcile_global_instrument_metadata(self, requested_id, **kwargs):
            self.reconcile_calls.append((requested_id, kwargs))
            raise PortfolioServiceUnavailableError("fixture reconcile unavailable")

    orchestrator = ReconcileUnavailable({instrument_id: {"providerMappings": []}})
    identity = {"X-AIP-User-Id": "internal", "X-AIP-User-Roles": "ADMIN"}
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), _Universe([_instrument(instrument_id)]), orchestrator, _settings(),
        historical_provider=_HistoricalProvider(), clock=lambda: NOW,
    )

    job = await jobs.submit(identity_headers=identity)
    result = await jobs.wait(job["jobId"])

    assert orchestrator.reconcile_calls[0][1]["identity_headers"] == identity
    assert result["reasons"] == {"PROVIDER_UNAVAILABLE": 1}
    assert "PORTFOLIO_RECONCILE_UNAVAILABLE" in caplog.text
    assert "PORTFOLIO_METADATA_UNAVAILABLE" not in caplog.text
    assert "HISTORICAL_PROVIDER_UNAVAILABLE" not in caplog.text


@pytest.mark.asyncio
async def test_universe_failure_is_reported_and_duplicate_concurrent_india_job_is_deduplicated():
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingUniverse:
        async def listings(self, **_kwargs):
            entered.set()
            await release.wait()
            raise MarketUniverseUnavailable("fixture")

    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), BlockingUniverse(), _Orchestrator({}), _settings(),
        historical_provider=_HistoricalProvider(), clock=lambda: NOW,
    )
    first = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    await entered.wait()
    duplicate = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    assert duplicate["jobId"] == first["jobId"]
    release.set()
    result = await jobs.wait(first["jobId"])
    assert result["status"] == "FAILED"
    assert result["reasons"] == {"UNIVERSE_UNAVAILABLE": 1}


@pytest.mark.asyncio
async def test_empty_india_universe_completes_with_safe_zero_status():
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(), _Universe([]), _Orchestrator({}), _settings(),
        historical_provider=_HistoricalProvider(), clock=lambda: NOW,
    )
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])
    assert result == {
        "jobId": submitted["jobId"],
        "status": "COMPLETED",
        "region": "INDIA",
        "universeSize": 0,
        "attempted": 0,
        "populated": 0,
        "skipped": 0,
        "failed": 0,
        "reasons": {},
        "startedAt": NOW,
        "completedAt": NOW,
    }


@pytest.mark.asyncio
async def test_population_routes_require_admin_start_only_india_and_return_status(monkeypatch):
    observed = {}
    job_id = uuid4()

    class Jobs:
        async def submit(self, **kwargs):
            observed.update(kwargs)
            return {"jobId": str(job_id), "status": "QUEUED", "region": "INDIA", "reasons": {}}

        def get(self, requested_job_id):
            return {"jobId": str(requested_job_id), "status": "RUNNING", "region": "INDIA", "reasons": {}}

    monkeypatch.setattr(main, "market_data_population_jobs", Jobs())
    result = await main.start_market_data_population(
        region="INDIA", x_correlation_id="correlation", x_aip_user_id="admin-id",
        x_aip_user_issuer="issuer", x_aip_user_subject="subject", x_aip_user_email=None,
        x_aip_user_display_name=None, x_aip_user_roles="USER,ADMIN",
    )
    assert result["jobId"] == str(job_id)
    assert observed["correlation_id"] == "correlation"
    assert observed["identity_headers"]["X-AIP-User-Id"] == "admin-id"
    status_result = await main.market_data_population_status(
        job_id=job_id, x_aip_user_id="admin-id", x_aip_user_issuer="issuer",
        x_aip_user_subject="subject", x_aip_user_roles="ADMIN"
    )
    assert status_result["status"] == "RUNNING"

    with pytest.raises(HTTPException) as unsupported:
        await main.start_market_data_population(
            region="USA", x_correlation_id=None, x_aip_user_id="admin-id", x_aip_user_issuer="issuer",
            x_aip_user_subject="subject", x_aip_user_email=None, x_aip_user_display_name=None, x_aip_user_roles="ADMIN",
        )
    assert unsupported.value.status_code == 400
    with pytest.raises(HTTPException) as forbidden:
        await main.start_market_data_population(
            region="INDIA", x_correlation_id=None, x_aip_user_id="user-id", x_aip_user_issuer="issuer",
            x_aip_user_subject="subject", x_aip_user_email=None, x_aip_user_display_name=None, x_aip_user_roles="USER",
        )
    assert forbidden.value.status_code == 403


@pytest.mark.asyncio
async def test_india_sector_performance_is_profileless_provider_free_and_excludes_null_sector(monkeypatch):
    ranked_id, null_sector_id = uuid4(), uuid4()
    values = [_instrument(ranked_id, ticker="NONHELD"), _instrument(null_sector_id, ticker="SERV", sector=None)]

    async def listings(**_kwargs):
        return values

    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("Sector Performance GET must not start provider or population work")

    monkeypatch.setattr(main.india_market_universe_provider, "listings", listings)
    monkeypatch.setattr(main.portfolio_orchestrator, "active_global_equities", forbidden_async)
    monkeypatch.setattr(main.repository, "structured_market_snapshots_for", lambda *_args: (_ for _ in ()).throw(AssertionError("India sector comes from Nifty cache")))
    monkeypatch.setattr(main.repository, "profile", lambda *_args: (_ for _ in ()).throw(AssertionError("No profile dependency")))
    monkeypatch.setattr(main.repository, "summary", lambda *_args: (_ for _ in ()).throw(AssertionError("No summary dependency")))
    monkeypatch.setattr(main.repository, "persisted_canonical_read_model_score", lambda *_args: (_ for _ in ()).throw(AssertionError("No score dependency")))
    monkeypatch.setattr(main.market_data_population_jobs, "submit", forbidden_async)
    monkeypatch.setattr(main.market_data_population_jobs.population, "populate", forbidden_async)

    prices = [
        SimpleNamespace(observed_at=NOW - timedelta(days=3), price=Decimal("100"), currency="INR"),
        SimpleNamespace(observed_at=NOW, price=Decimal("108"), currency="INR"),
    ]
    monkeypatch.setattr(main.repository, "market_price_observations_for", lambda ids: {
        ranked_id: prices, null_sector_id: prices
    })

    result = await main.sector_performance(
        region="INDIA", sector="Technology", period="DAY", limit=5,
        x_correlation_id="performance", x_aip_user_id="user", x_aip_user_issuer=None,
        x_aip_user_subject=None, x_aip_user_email=None, x_aip_user_display_name=None,
        x_aip_user_roles="USER",
    )
    assert [row["globalInstrumentId"] for row in result["bestPerformers"]] == [str(ranked_id)]
    assert result["bestPerformers"][0]["ticker"] == "NONHELD"
    assert str(null_sector_id) not in str(result)
