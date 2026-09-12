from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from fastapi import HTTPException

import app.main as main
from app.market_data_ensure import IndiaMarketDataEnsureService
from app.market_universe import MarketUniverseInstrument, MarketUniverseUnavailable
from app.portfolio_orchestration import PortfolioServiceUnavailableError
from app.settings import Settings


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _instrument(*, retrieved_at=NOW - timedelta(hours=1), sector="Technology"):
    return MarketUniverseInstrument(
        global_instrument_id=uuid4(), ticker="NONHELD", company_name="Non-held Limited",
        isin="INE000A01001", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        asset_type="EQUITY", status="ACTIVE", region="INDIA", canonical_sector=sector,
        official_industry="Information Technology", source="NSE_INDICES_NIFTY500",
        retrieved_at=retrieved_at,
    )


def _prices(latest=NOW - timedelta(hours=1)):
    return [
        SimpleNamespace(observed_at=latest - timedelta(days=370), price=Decimal("100")),
        SimpleNamespace(observed_at=latest, price=Decimal("110")),
    ]


class _Universe:
    def __init__(self, values=None, error=False):
        self.values = values or []
        self.error = error
        self.calls = []

    async def listings(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise MarketUniverseUnavailable("fixture")
        return self.values


class _SequencedUniverse:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def listings(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


class _Repository:
    def __init__(self, grouped=None, *, error=False):
        self.grouped = grouped or {}
        self.error = error
        self.calls = 0

    async def market_price_coverage_for_instruments(self, ids):
        self.calls += 1
        if self.error:
            raise RuntimeError("fixture coverage unavailable")
        result = {}
        for instrument_id in ids:
            values = sorted(self.grouped.get(instrument_id, []), key=lambda value: value.observed_at)
            usable = [value for value in values if value.price is not None and value.price > 0]
            if usable:
                result[instrument_id] = (usable[0].observed_at, usable[-1].observed_at, len(usable))
        return result


class _Jobs:
    def __init__(self, *, active=None, latest=None, submit_failures=0):
        self.active_job = active
        self.latest_job = latest
        self.submit_failures = submit_failures
        self.submit_calls = []

    def active(self):
        return self.active_job

    def latest(self):
        return self.latest_job

    async def submit(self, **kwargs):
        self.submit_calls.append(kwargs)
        if self.submit_failures:
            self.submit_failures -= 1
            raise RuntimeError("fixture population submit failed")
        self.active_job = {"jobId": "population-job", "status": "QUEUED", "region": "INDIA"}
        return self.active_job


class _Orchestrator:
    def __init__(self, *, block=False, fail=False, failure_cause=None):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.fail = fail
        self.failure_cause = failure_cause

    async def refresh_india_nifty500_reference(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            failure = PortfolioServiceUnavailableError("fixture")
            if self.failure_cause is not None:
                raise failure from self.failure_cause
            raise failure
        return {"activeUniverse": 500}


def _settings():
    return Settings(
        market_data_nifty_freshness_hours=12,
        market_data_historical_freshness_hours=72,
        market_data_ensure_retry_cooldown_minutes=15,
        market_data_population_retry_cooldown_hours=12,
        market_data_internal_user_id="00000000-0000-0000-0000-000000000777",
        market_data_internal_issuer="test-internal",
        market_data_internal_subject="research-engine-test",
    )


def _service(instrument, *, prices=None, jobs=None, orchestrator=None, universe=None):
    universe = universe or _Universe([instrument] if instrument else [])
    repository = _Repository({instrument.global_instrument_id: prices or []} if instrument else {})
    jobs = jobs or _Jobs()
    orchestrator = orchestrator or _Orchestrator()
    service = IndiaMarketDataEnsureService(
        repository, universe, orchestrator, jobs, _settings(), clock=lambda: NOW
    )
    return service, universe, repository, jobs, orchestrator


@pytest.mark.asyncio
async def test_fresh_universe_and_history_cause_no_background_work():
    instrument = _instrument()
    service, _, _, jobs, orchestrator = _service(instrument, prices=_prices())
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser-user"})
    assert result["universe"]["status"] == "FRESH"
    assert result["historicalPrices"]["status"] == "FRESH"
    assert result["historicalPrices"]["coveredInstruments"] == 1
    assert orchestrator.calls == []
    assert jobs.submit_calls == []


@pytest.mark.asyncio
async def test_stale_universe_starts_exactly_one_global_refresh_for_simultaneous_ensures_and_returns_promptly():
    instrument = _instrument(retrieved_at=NOW - timedelta(hours=13))
    orchestrator = _Orchestrator(block=True)
    service, _, _, _, _ = _service(instrument, prices=_prices(), orchestrator=orchestrator)

    first, second = await asyncio.wait_for(asyncio.gather(
        service.ensure(identity_headers={"X-AIP-User-Id": "browser-a", "X-AIP-User-Roles": "USER"}),
        service.ensure(identity_headers={"X-AIP-User-Id": "browser-b", "X-AIP-User-Roles": "ADMIN"}),
    ), timeout=0.2)
    assert first["universe"]["status"] == second["universe"]["status"] == "REFRESH_STARTED"
    await orchestrator.started.wait()
    assert len(orchestrator.calls) == 1
    internal_headers = orchestrator.calls[0]["identity_headers"]
    assert internal_headers == {
        "X-AIP-User-Id": "00000000-0000-0000-0000-000000000777",
        "X-AIP-User-Issuer": "test-internal",
        "X-AIP-User-Subject": "research-engine-test",
        "X-AIP-User-Roles": "ADMIN",
    }
    assert internal_headers["X-AIP-User-Id"] not in {"browser-a", "browser-b"}
    orchestrator.release.set()
    await service.wait_for_reference_refresh()


@pytest.mark.asyncio
async def test_empty_universe_starts_reference_refresh_and_failed_refresh_observes_retry_cooldown():
    orchestrator = _Orchestrator(fail=True)
    service, _, _, _, _ = _service(None, orchestrator=orchestrator)
    first = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert first["universe"]["status"] == "REFRESH_STARTED"
    await service.wait_for_reference_refresh()
    second = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert second["universe"]["status"] == "UNAVAILABLE"
    assert len(orchestrator.calls) == 1


@pytest.mark.asyncio
async def test_successful_stale_reference_refresh_chains_nonblocking_population_only_when_history_needs_it():
    instrument = _instrument(retrieved_at=NOW - timedelta(hours=13))
    jobs = _Jobs()
    service, _, _, jobs, orchestrator = _service(instrument, prices=[], jobs=jobs)
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert result["universe"]["status"] == "REFRESH_STARTED"
    assert jobs.submit_calls == []
    await service.wait_for_reference_refresh()
    assert len(orchestrator.calls) == 1
    assert len(jobs.submit_calls) == 1


@pytest.mark.asyncio
async def test_post_refresh_rereads_fresh_universe_and_submits_stale_history_once(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    fresh = replace(stale, retrieved_at=NOW)
    universe = _SequencedUniverse([[stale], [fresh]])
    repository = _Repository()
    jobs = _Jobs()
    orchestrator = _Orchestrator()
    service = IndiaMarketDataEnsureService(
        repository, universe, orchestrator, jobs, _settings(), clock=lambda: NOW
    )

    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert result["universe"]["status"] == "REFRESH_STARTED"
    await service.wait_for_reference_refresh()

    assert len(universe.calls) == 2
    assert universe.calls[1]["identity_headers"]["X-AIP-User-Roles"] == "ADMIN"
    assert len(jobs.submit_calls) == 1
    assert "event=REFERENCE_REFRESH_COMPLETED" in caplog.text
    assert "event=POST_REFRESH_UNIVERSE_READ_COMPLETED instrumentCount=1" in caplog.text
    assert "event=HISTORICAL_FRESHNESS_RESULT status=STALE" in caplog.text
    assert "event=POPULATION_SUBMIT_STARTED" in caplog.text
    assert "event=POPULATION_SUBMIT_COMPLETED jobId=population-job jobStatus=QUEUED" in caplog.text


@pytest.mark.asyncio
async def test_post_refresh_universe_read_failure_is_observable_and_later_ensure_retries(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    fresh = replace(stale, retrieved_at=NOW)
    universe = _SequencedUniverse([
        [stale],
        MarketUniverseUnavailable("fixture post-refresh read failure"),
        [fresh],
    ])
    jobs = _Jobs()
    orchestrator = _Orchestrator()
    service = IndiaMarketDataEnsureService(
        _Repository(), universe, orchestrator, jobs, _settings(), clock=lambda: NOW
    )

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert jobs.submit_calls == []
    assert "event=POST_REFRESH_UNIVERSE_READ_FAILED" in caplog.text
    assert "errorIdentifier=POST_REFRESH_UNIVERSE_UNAVAILABLE" in caplog.text

    retried = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert retried["historicalPrices"]["status"] == "POPULATION_STARTED"
    assert len(jobs.submit_calls) == 1
    assert len(orchestrator.calls) == 1


@pytest.mark.asyncio
async def test_exact_year_span_is_compatible_with_historical_freshness_requirement():
    instrument = _instrument()
    prices = [
        SimpleNamespace(observed_at=NOW - timedelta(days=365), price=Decimal("100")),
        SimpleNamespace(observed_at=NOW, price=Decimal("110")),
    ]
    service, _, _, jobs, orchestrator = _service(instrument, prices=prices)

    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser-user"})

    assert result["historicalPrices"]["status"] == "FRESH"
    assert result["historicalPrices"]["coveredInstruments"] == 1
    assert jobs.submit_calls == []
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_reference_refresh_timeout_logs_underlying_safe_transport_class(caplog):
    caplog.set_level("WARNING", logger="app.market_data_ensure")
    orchestrator = _Orchestrator(
        fail=True,
        failure_cause=httpx.ReadTimeout("fixture refresh timeout"),
    )
    service, _, _, _, _ = _service(None, orchestrator=orchestrator)

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert "event=REFERENCE_REFRESH_FAILED exceptionClass=ReadTimeout" in caplog.text
    assert "fixture refresh timeout" not in caplog.text


@pytest.mark.asyncio
async def test_post_refresh_unavailable_historical_coverage_is_explicit_and_does_not_submit(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    fresh = replace(stale, retrieved_at=NOW)
    jobs = _Jobs()
    service = IndiaMarketDataEnsureService(
        _Repository(error=True),
        _SequencedUniverse([[stale], [fresh]]),
        _Orchestrator(),
        jobs,
        _settings(),
        clock=lambda: NOW,
    )

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert jobs.submit_calls == []
    assert "event=HISTORICAL_COVERAGE_FAILED" in caplog.text
    assert "event=HISTORICAL_FRESHNESS_RESULT status=UNAVAILABLE" in caplog.text
    assert "errorIdentifier=HISTORICAL_COVERAGE_UNAVAILABLE" in caplog.text


@pytest.mark.asyncio
async def test_post_refresh_active_population_is_reused_without_duplicate_submit(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    active = {"jobId": "running-job", "status": "RUNNING", "region": "INDIA"}
    jobs = _Jobs(active=active)
    service = IndiaMarketDataEnsureService(
        _Repository(), _Universe([stale]), _Orchestrator(), jobs, _settings(), clock=lambda: NOW
    )

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert jobs.submit_calls == []
    assert "event=POPULATION_ACTIVE_REUSED jobId=running-job jobStatus=RUNNING" in caplog.text


@pytest.mark.asyncio
async def test_post_refresh_recent_terminal_job_preserves_population_cooldown(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    recent = {
        "jobId": "recent-job",
        "status": "COMPLETED",
        "completedAt": NOW - timedelta(hours=1),
    }
    jobs = _Jobs(latest=recent)
    service = IndiaMarketDataEnsureService(
        _Repository(), _Universe([stale]), _Orchestrator(), jobs, _settings(), clock=lambda: NOW
    )

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert jobs.submit_calls == []
    assert "event=POPULATION_RETRY_DEFERRED jobId=recent-job jobStatus=COMPLETED" in caplog.text


@pytest.mark.asyncio
async def test_post_refresh_submit_failure_is_observable_without_poisoning_refresh_retry_state(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    stale = _instrument(retrieved_at=NOW - timedelta(hours=13))
    fresh = replace(stale, retrieved_at=NOW)
    universe = _SequencedUniverse([[stale], [fresh], [fresh]])
    jobs = _Jobs(submit_failures=1)
    orchestrator = _Orchestrator()
    service = IndiaMarketDataEnsureService(
        _Repository(), universe, orchestrator, jobs, _settings(), clock=lambda: NOW
    )

    await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    await service.wait_for_reference_refresh()

    assert len(jobs.submit_calls) == 1
    assert service._last_refresh_failure_at is None
    assert "event=POPULATION_HANDOFF_FAILED" in caplog.text
    assert "errorIdentifier=POPULATION_HANDOFF_FAILED" in caplog.text

    retried = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert retried["historicalPrices"]["status"] == "POPULATION_STARTED"
    assert len(jobs.submit_calls) == 2
    assert len(orchestrator.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prices", [[], [SimpleNamespace(observed_at=NOW, price=Decimal("100"))], _prices(NOW - timedelta(hours=73))])
async def test_missing_incomplete_or_stale_history_starts_population(prices, caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    instrument = _instrument()
    service, _, _, jobs, orchestrator = _service(instrument, prices=prices)
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"}, correlation_id="ensure-test")
    assert result["universe"]["status"] == "FRESH"
    assert result["historicalPrices"]["status"] == "POPULATION_STARTED"
    assert result["historicalPrices"]["jobId"] == "population-job"
    assert len(jobs.submit_calls) == 1 and orchestrator.calls == []
    assert jobs.submit_calls[0]["identity_headers"]["X-AIP-User-Roles"] == "ADMIN"
    assert "event=POPULATION_SUBMIT_STARTED" in caplog.text
    assert "event=POPULATION_SUBMIT_COMPLETED jobId=population-job jobStatus=QUEUED" in caplog.text


@pytest.mark.asyncio
async def test_partial_universe_history_is_stale_until_all_eligible_candidates_are_covered():
    first, second = _instrument(), _instrument()
    universe = _Universe([first, second])
    repository = _Repository({first.global_instrument_id: _prices()})
    jobs = _Jobs()
    service = IndiaMarketDataEnsureService(
        repository, universe, _Orchestrator(), jobs, _settings(), clock=lambda: NOW
    )
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert result["historicalPrices"]["status"] == "POPULATION_STARTED"
    assert result["historicalPrices"]["eligibleInstruments"] == 2
    assert result["historicalPrices"]["coveredInstruments"] == 1


@pytest.mark.asyncio
async def test_running_population_is_reused_and_recent_completed_job_prevents_login_storm(caplog):
    caplog.set_level("INFO", logger="app.market_data_ensure")
    instrument = _instrument()
    running = {"jobId": "running-job", "status": "RUNNING", "region": "INDIA"}
    jobs = _Jobs(active=running)
    service, _, _, jobs, _ = _service(instrument, prices=[], jobs=jobs)
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert result["historicalPrices"] ["status"] == "RUNNING"
    assert result["historicalPrices"]["jobId"] == "running-job"
    assert jobs.submit_calls == []
    assert "event=POPULATION_ACTIVE_REUSED jobId=running-job jobStatus=RUNNING" in caplog.text

    jobs.active_job = None
    jobs.latest_job = {"jobId": "recent", "status": "COMPLETED", "completedAt": NOW - timedelta(hours=1)}
    caplog.clear()
    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})
    assert result["historicalPrices"]["status"] == "STALE"
    assert jobs.submit_calls == []
    assert "event=POPULATION_RETRY_DEFERRED jobId=recent jobStatus=COMPLETED" in caplog.text


@pytest.mark.asyncio
async def test_unavailable_universe_starts_reference_refresh_instead_of_terminating_ensure():
    jobs = _Jobs()
    orchestrator = _Orchestrator()
    service = IndiaMarketDataEnsureService(
        _Repository(), _Universe(error=True), orchestrator, jobs, _settings(), clock=lambda: NOW
    )

    result = await service.ensure(identity_headers={"X-AIP-User-Id": "browser"})

    assert result["universe"]["status"] == "REFRESH_STARTED"
    assert result["historicalPrices"]["status"] == "UNAVAILABLE"

    await service.wait_for_reference_refresh()

    assert len(orchestrator.calls) == 1
    internal_headers = orchestrator.calls[0]["identity_headers"]
    assert internal_headers == {
        "X-AIP-User-Id": "00000000-0000-0000-0000-000000000777",
        "X-AIP-User-Issuer": "test-internal",
        "X-AIP-User-Subject": "research-engine-test",
        "X-AIP-User-Roles": "ADMIN",
    }

    # This fixture deliberately keeps the universe unavailable even after the
    # reference refresh, so population must not start against an unreadable
    # universe.
    assert jobs.submit_calls == []


@pytest.mark.asyncio
async def test_ensure_route_accepts_normal_authenticated_user_without_admin(monkeypatch):
    observed = {}

    class Ensure:
        async def ensure(self, **kwargs):
            observed.update(kwargs)
            return {"region": "INDIA", "universe": {"status": "FRESH"}, "historicalPrices": {"status": "FRESH"}}

    monkeypatch.setattr(main, "market_data_ensure_service", Ensure())
    result = await main.ensure_market_data(
        region="INDIA", x_correlation_id="correlation", x_aip_user_id="user-id",
        x_aip_user_issuer="issuer", x_aip_user_subject="subject", x_aip_user_email="user@example.test",
        x_aip_user_display_name="User", x_aip_user_roles="USER",
    )
    assert result["universe"]["status"] == "FRESH"
    assert observed["identity_headers"]["X-AIP-User-Roles"] == "USER"
    assert observed["correlation_id"] == "correlation"

    with pytest.raises(HTTPException) as unauthorized:
        await main.ensure_market_data(
            region="INDIA", x_correlation_id=None, x_aip_user_id="user-id",
            x_aip_user_issuer=None, x_aip_user_subject="subject", x_aip_user_email=None,
            x_aip_user_display_name=None, x_aip_user_roles="USER",
        )
    assert unauthorized.value.status_code == 401
