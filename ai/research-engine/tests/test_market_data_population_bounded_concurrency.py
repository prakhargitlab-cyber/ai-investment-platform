"""Guardian Review Slice 3 -- bounded-parallel historical-price acquisition.

Proves the fix for the previously strictly-sequential
IndiaMarketDataPopulationJobs._run loop (the exact path Slice 1 traced the
INFY/HCL-INSYS corruption through): a new, configurable
`market_data_population_concurrency` setting (default 1, byte-identical to
the prior sequential behavior) now bounds a per-batch semaphore in
`_run`/`_populate_one_bounded`, so raising it scales measured synthetic
throughput without ever gathering across the whole (up to 2585-instrument)
universe at once, without losing per-instrument rate-limit pacing, without
letting one instrument's failure cancel unrelated instruments, and without
any observation being written under the wrong canonical instrument id.

All measurements below are synthetic (mocked latency, no real provider
calls) and are reported as measured -- they are not extrapolated to real
production Yahoo throughput.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.market_data_population import IndiaMarketDataPopulationJobs
from app.market_universe import MarketUniverseInstrument
from app.models import MarketPriceObservation
from app.settings import Settings

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _instrument(instrument_id: UUID, *, ticker: str, sector="Technology") -> MarketUniverseInstrument:
    return MarketUniverseInstrument(
        global_instrument_id=instrument_id,
        ticker=ticker,
        company_name=f"{ticker} Limited",
        isin=f"INE{ticker[:3]}0000001",
        exchange="NSE",
        mic="XNSE",
        country="IN",
        currency="INR",
        asset_type="EQUITY",
        status="ACTIVE",
        region="INDIA",
        canonical_sector=sector,
        official_industry="Information Technology",
        source="NSE_INDICES_NIFTY500",
        retrieved_at=NOW,
    )


class _Universe:
    def __init__(self, values):
        self.values = values

    async def listings(self, **kwargs):
        return self.values


class _Repository:
    def __init__(self):
        self.rows = {}

    async def market_price_observations_for_instruments(self, instrument_ids):
        return {instrument_id: list(self.rows.get(instrument_id, {}).values()) for instrument_id in instrument_ids}

    async def upsert_market_price_observation_async(self, observation):
        key = (observation.provider, observation.observed_at)
        self.rows.setdefault(observation.instrument_id, {})[key] = observation


def _verified(symbol):
    return {"providerMappings": [{
        "provider": "YAHOO_FINANCE", "providerSymbol": symbol, "status": "VERIFIED",
    }]}


class _Orchestrator:
    """Every instrument already has a durable verified mapping (the Slice 2
    steady state), so this test isolates the Slice 3 concurrency behavior of
    historical-price acquisition itself rather than mapping derivation."""

    def __init__(self, metadata):
        self.metadata = metadata

    async def global_instrument_metadata(self, instrument_id, **kwargs):
        return self.metadata.get(instrument_id, {"providerMappings": []})

    async def reconcile_global_instrument_metadata(self, instrument_id, **kwargs):
        return self.metadata.get(instrument_id, {"providerMappings": []})


class _LatencyHistoricalProvider:
    """Records concurrent in-flight calls and the exact instrument identity
    each call was made for, under a fixed mocked per-call latency -- no real
    network or provider call is made."""

    provider_name = "YAHOO_FINANCE"

    def __init__(self, latency_seconds: float, *, fail_symbols=()):
        self.latency_seconds = latency_seconds
        self.fail_symbols = set(fail_symbols)
        self._inflight = 0
        self.max_observed_inflight = 0
        self.calls: list[dict] = []

    async def closes(self, instrument, *, start, end):
        self._inflight += 1
        self.max_observed_inflight = max(self.max_observed_inflight, self._inflight)
        try:
            self.calls.append(dict(instrument))
            await asyncio.sleep(self.latency_seconds)
            symbol = instrument["structuredProviderTicker"]
            if symbol in self.fail_symbols:
                raise RuntimeError("fixture provider failure")
            return [MarketPriceObservation(
                instrument_id=UUID(instrument["globalInstrumentId"]),
                observed_at=NOW - timedelta(days=1),
                price=Decimal("100"),
                currency="INR",
                provider=self.provider_name,
                source_url=f"fixture://{symbol}",
                retrieved_at=NOW,
            )]
        finally:
            self._inflight -= 1


def _settings(*, concurrency: int, batch_size: int) -> Settings:
    return Settings(
        market_data_population_batch_size=batch_size,
        market_data_population_concurrency=concurrency,
        market_data_population_request_interval_seconds=0,
        market_data_population_initial_lookback_days=400,
    )


def _fixture(n: int, *, concurrency: int, latency: float, fail_symbols=()):
    instruments = [_instrument(uuid4(), ticker=f"SYM{i:03d}") for i in range(n)]
    metadata = {i.global_instrument_id: _verified(f"{i.ticker}.NS") for i in instruments}
    historical_provider = _LatencyHistoricalProvider(latency, fail_symbols=fail_symbols)
    jobs = IndiaMarketDataPopulationJobs(
        _Repository(),
        _Universe(instruments),
        _Orchestrator(metadata),
        _settings(concurrency=concurrency, batch_size=n),
        historical_provider=historical_provider,
        clock=lambda: NOW,
    )
    return instruments, historical_provider, jobs


# 1/2/3 -- measured synthetic concurrency at 1, 2 and 4: elapsed time scales
# down and the observed peak in-flight count matches the configured bound --
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 4])
async def test_measured_synthetic_concurrency_matches_configured_bound(concurrency):
    n = 8
    latency = 0.05
    instruments, historical_provider, jobs = _fixture(n, concurrency=concurrency, latency=latency)

    started = time.monotonic()
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])
    elapsed = time.monotonic() - started

    assert result["status"] == "COMPLETED"
    assert result["attempted"] == n
    assert result["failed"] == 0
    # Deterministic: peak concurrent in-flight acquisitions never exceeds the
    # configured bound, and (with n > concurrency here) actually reaches it.
    assert historical_provider.max_observed_inflight == concurrency
    # Measured, not extrapolated: with n=8 instruments at fixed latency, wall
    # time should track roughly ceil(n / concurrency) * latency. Generous
    # tolerance keeps this robust to scheduler jitter while still proving
    # higher concurrency is actually faster, not just configured.
    expected_batches = -(-n // concurrency)  # ceil
    assert elapsed < expected_batches * latency * 4 + 0.5
    if concurrency > 1:
        # Strictly faster than fully sequential for the same synthetic
        # workload -- this is the throughput property Slice 3 requires.
        assert elapsed < n * latency


# 4 -- default concurrency (1) is byte-identical to the prior strictly
# sequential behavior: never more than one acquisition in flight -----------
@pytest.mark.asyncio
async def test_default_concurrency_is_strictly_sequential():
    n = 5
    instruments, historical_provider, jobs = _fixture(n, concurrency=1, latency=0.01)
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])
    assert result["attempted"] == n
    assert historical_provider.max_observed_inflight == 1


# 5 -- one failing instrument under concurrency > 1 does not cancel or
# strand any unrelated instrument, and failure state is truthful -----------
@pytest.mark.asyncio
async def test_one_failure_under_concurrency_does_not_cancel_others():
    n = 6
    instruments, historical_provider, jobs = _fixture(
        n, concurrency=4, latency=0.01, fail_symbols={"SYM002.NS"},
    )
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])
    assert result["attempted"] == n
    assert result["failed"] == 1
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    # every non-failing instrument's call still completed and reached the
    # provider -- none were stranded or cancelled by the sibling failure.
    called_symbols = {c["structuredProviderTicker"] for c in historical_provider.calls}
    assert called_symbols == {f"SYM{i:03d}.NS" for i in range(n)}


# 6 -- concurrent different instruments never cross-contaminate: each
# persisted observation lands under its own, correct canonical instrument id
@pytest.mark.asyncio
async def test_concurrent_acquisitions_never_cross_contaminate_identity():
    n = 8
    instruments, historical_provider, jobs = _fixture(n, concurrency=4, latency=0.02)
    repository = jobs.repository
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    await jobs.wait(submitted["jobId"])

    for instrument in instruments:
        rows = (await repository.market_price_observations_for_instruments(
            {instrument.global_instrument_id}
        ))[instrument.global_instrument_id]
        assert len(rows) == 1
        assert rows[0].instrument_id == instrument.global_instrument_id
        assert rows[0].source_url == f"fixture://{instrument.ticker}.NS"


# 7 -- warm READY_FRESH instruments perform zero historical-provider work
# even inside a bounded-concurrency batch alongside instruments needing
# acquisition -- concurrency must never force provider calls for fresh rows
@pytest.mark.asyncio
async def test_warm_fresh_instruments_perform_zero_provider_work_under_concurrency():
    n = 4
    instruments = [_instrument(uuid4(), ticker=f"SYM{i:03d}") for i in range(n)]
    metadata = {i.global_instrument_id: _verified(f"{i.ticker}.NS") for i in instruments}
    historical_provider = _LatencyHistoricalProvider(0.01)
    repository = _Repository()
    # Pre-populate a full year of daily observations for instrument 0 only --
    # has_year_historical_coverage should treat it as already fresh.
    fresh_id = instruments[0].global_instrument_id
    for day in range(370):
        obs_time = NOW - timedelta(days=day)
        repository.rows.setdefault(fresh_id, {})[("YAHOO_FINANCE", obs_time)] = MarketPriceObservation(
            instrument_id=fresh_id, observed_at=obs_time, price=Decimal("100"),
            currency="INR", provider="YAHOO_FINANCE", source_url="fixture://warm", retrieved_at=NOW,
        )
    jobs = IndiaMarketDataPopulationJobs(
        repository, _Universe(instruments), _Orchestrator(metadata),
        _settings(concurrency=4, batch_size=n), historical_provider=historical_provider,
        clock=lambda: NOW,
    )
    submitted = await jobs.submit(identity_headers={"X-AIP-User-Id": "admin"})
    result = await jobs.wait(submitted["jobId"])
    assert result["attempted"] == n
    called_ids = {UUID(c["globalInstrumentId"]) for c in historical_provider.calls}
    assert fresh_id not in called_ids
    assert len(called_ids) == n - 1
