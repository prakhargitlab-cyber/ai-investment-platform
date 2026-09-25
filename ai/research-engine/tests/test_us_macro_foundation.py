"""Focused coverage for extending the existing macro framework with Fed
policy rate + US CPI. Reuses macro_observation / macro_persistence /
macro_acquisition / global_data_provider and the V18 macro_observations
schema unchanged -- acquisition + persistence + read model only.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.persistence import SqliteResearchPersistence
from app.global_data_provider import SingleFlightTTLCache
from app.macro_observation import (
    MacroObservation, MacroProviderError, MacroProviderConfigurationError,
    FED_POLICY_RATE, US_CPI, macro_freshness_state,
)
from app.macro_acquisition import get_macro_observation
from app.us_macro_provider import FedPolicyRateProvider, UsCpiProvider, FRED_OBSERVATIONS_ENDPOINT

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _fred_transport(observations):
    def handler(request):
        assert request.url.params.get("file_type") == "json"
        return httpx.Response(200, json={"observations": observations})
    return httpx.MockTransport(handler)


def _fed_provider(observations):
    client = httpx.AsyncClient(transport=_fred_transport(observations))
    return FedPolicyRateProvider("test-key", client=client)


def _cpi_provider(observations):
    client = httpx.AsyncClient(transport=_fred_transport(observations))
    return UsCpiProvider("test-key", client=client)


# -- Fed observation parse/persist/provenance ---------------------------------

@pytest.mark.asyncio
async def test_fed_observation_parses_and_persists_with_provenance():
    provider = _fed_provider([{"date": "2026-09-23", "value": "4.00"}])
    observation = await provider.fetch(NOW)
    assert observation.indicator == FED_POLICY_RATE
    assert observation.region == "US"
    assert observation.actual_value == 4.00
    assert observation.unit == "PERCENT"
    assert observation.provenance == "OFFICIAL_GOVERNMENT"
    assert observation.source_url == "https://fred.stlouisfed.org/series/DFEDTARU"

    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(observation)
    persisted = repo.load_macro_observation(FED_POLICY_RATE, region="US")
    assert persisted.actual_value == 4.00 and persisted.period == "2026-09-23"
    assert persisted.provider == "FED_POLICY_RATE_FRED"


# -- US CPI parse/persist/provenance ------------------------------------------

@pytest.mark.asyncio
async def test_us_cpi_observation_parses_and_persists_with_provenance():
    provider = _cpi_provider([{"date": "2026-08-01", "value": "2.90"}])
    observation = await provider.fetch(NOW)
    assert observation.indicator == US_CPI
    assert observation.region == "US"
    assert observation.actual_value == 2.90
    assert observation.unit == "PERCENT_YOY"
    assert observation.provenance == "OFFICIAL_GOVERNMENT"

    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(observation)
    persisted = repo.load_macro_observation(US_CPI, region="US")
    assert persisted.actual_value == 2.90 and persisted.period == "2026-08-01"


# -- global reuse / single-flight ---------------------------------------------

@pytest.mark.asyncio
async def test_twenty_five_concurrent_instrument_consumers_fetch_fed_rate_once():
    calls = []
    class CountingProvider:
        async def fetch(self, now):
            calls.append(1)
            await asyncio.sleep(0.01)
            return MacroObservation(indicator=FED_POLICY_RATE, region="US", period="2026-09-23", actual_value=4.0,
                unit="PERCENT", observed_at=now, source="Fed", source_url="https://example.test/fed",
                provider="FED_POLICY_RATE_FRED")

    repo = SqliteResearchPersistence()
    cache = SingleFlightTTLCache(ttl_seconds=60)
    provider = CountingProvider()
    results = await asyncio.gather(*[
        get_macro_observation(repo, provider, cache, FED_POLICY_RATE, now=NOW, region="US") for _ in range(25)
    ])
    assert len(calls) == 1
    assert all(r.actual_value == 4.0 for r in results)
    assert len(repo.load_macro_observations(FED_POLICY_RATE, region="US")) == 1


# -- fresh persistence reuse ---------------------------------------------------

@pytest.mark.asyncio
async def test_persisted_fresh_us_cpi_observation_is_reused_without_fetch():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=US_CPI, region="US", period="2026-08-01",
        actual_value=2.90, unit="PERCENT_YOY", observed_at=NOW - timedelta(days=2), source="BLS",
        source_url="https://example.test/cpi", provider="US_CPI_FRED"))

    calls = []
    class ShouldNotBeCalled:
        async def fetch(self, now):
            calls.append(1)
            raise AssertionError("provider must not be called for fresh evidence")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, ShouldNotBeCalled(), cache, US_CPI, now=NOW,
        ttl_seconds=35 * 24 * 3600, region="US")
    assert result.actual_value == 2.90
    assert calls == []


# -- stale refresh --------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_fed_observation_triggers_refresh():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=FED_POLICY_RATE, region="US", period="2026-06-01",
        actual_value=4.25, unit="PERCENT", observed_at=NOW - timedelta(days=100), source="Fed",
        source_url="https://example.test/fed", provider="FED_POLICY_RATE_FRED"))

    calls = []
    class RefreshedProvider:
        async def fetch(self, now):
            calls.append(1)
            return MacroObservation(indicator=FED_POLICY_RATE, region="US", period="2026-09-23", actual_value=4.00,
                unit="PERCENT", observed_at=now, source="Fed", source_url="https://example.test/fed",
                provider="FED_POLICY_RATE_FRED")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, RefreshedProvider(), cache, FED_POLICY_RATE, now=NOW,
        ttl_seconds=56 * 24 * 3600, region="US")
    assert calls == [1]
    assert result.period == "2026-09-23" and result.actual_value == 4.00


# -- provider failure preserves last valid observation -------------------------

@pytest.mark.asyncio
async def test_provider_failure_preserves_last_valid_fed_observation():
    repo = SqliteResearchPersistence()
    original = MacroObservation(indicator=FED_POLICY_RATE, region="US", period="2026-06-01", actual_value=4.25,
        unit="PERCENT", observed_at=NOW - timedelta(days=100), source="Fed", source_url="https://example.test/fed",
        provider="FED_POLICY_RATE_FRED")
    repo.upsert_macro_observation(original)

    class FailingProvider:
        async def fetch(self, now):
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:http_status_502")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, FailingProvider(), cache, FED_POLICY_RATE, now=NOW,
        ttl_seconds=56 * 24 * 3600, region="US")
    assert result.actual_value == 4.25 and result.period == "2026-06-01"
    persisted = repo.load_macro_observation(FED_POLICY_RATE, region="US")
    assert persisted.actual_value == 4.25 and persisted.observed_at == original.observed_at


# -- expected remains NULL when unavailable ------------------------------------

@pytest.mark.asyncio
async def test_expected_value_remains_null_for_fed_and_cpi():
    fed = await _fed_provider([{"date": "2026-09-23", "value": "4.00"}]).fetch(NOW)
    cpi = await _cpi_provider([{"date": "2026-08-01", "value": "2.90"}]).fetch(NOW)
    assert fed.expected_value is None and fed.previous_value is None and fed.surprise is None
    assert cpi.expected_value is None and cpi.previous_value is None and cpi.surprise is None


@pytest.mark.asyncio
async def test_fred_missing_or_dot_value_never_fabricates():
    for observations in ([{"date": "2026-09-23", "value": "."}], [{"date": "2026-09-23"}], []):
        provider = _fed_provider(observations)
        with pytest.raises(MacroProviderError):
            await provider.fetch(NOW)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
@pytest.mark.asyncio
async def test_fred_http_failures_never_become_a_fabricated_value(status):
    def handler(request):
        return httpx.Response(status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FedPolicyRateProvider("test-key", client=client)
    with pytest.raises(MacroProviderError):
        await provider.fetch(NOW)


def test_provider_not_configured_without_api_key_raises():
    with pytest.raises(MacroProviderConfigurationError):
        FedPolicyRateProvider(None)
    with pytest.raises(MacroProviderConfigurationError):
        UsCpiProvider("")


# -- no company financial-fact contamination -----------------------------------

def test_us_macro_observations_do_not_enter_financial_facts_or_add_a_table():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=FED_POLICY_RATE, region="US", period="2026-09-23",
        actual_value=4.0, unit="PERCENT", observed_at=NOW, source="Fed", source_url="https://x.test",
        provider="FED_POLICY_RATE_FRED"))
    repo.upsert_macro_observation(MacroObservation(indicator=US_CPI, region="US", period="2026-08-01",
        actual_value=2.9, unit="PERCENT_YOY", observed_at=NOW, source="BLS", source_url="https://x.test",
        provider="US_CPI_FRED"))
    from uuid import UUID
    assert repo.load_financial_facts({UUID(int=1)}) == []
    assert repo._connection.execute("SELECT COUNT(*) AS n FROM global_financial_facts").fetchone()["n"] == 0
    # Same V18 table, both regions coexist -- IN and US rows share one schema.
    rows = repo._connection.execute("SELECT indicator, region FROM macro_observations ORDER BY indicator").fetchall()
    assert {(r["indicator"], r["region"]) for r in rows} == {
        (FED_POLICY_RATE, "US"), (US_CPI, "US"),
    }


# -- no readiness/ranking changes ----------------------------------------------

def test_us_macro_modules_are_not_imported_by_rule_engine_or_ranking():
    import app.stock_rule_engine as sre
    import app.global_opportunity_orchestration as goo
    import app.global_opportunity_ranker as ranker
    for module in (sre, goo, ranker):
        text = open(module.__file__, encoding="utf-8").read()
        assert "us_macro_provider" not in text
        assert "FED_POLICY_RATE" not in text
        assert "US_CPI" not in text
