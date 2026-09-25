"""Focused coverage for the India macro foundation iteration (RBI repo rate,
India CPI): acquisition + persistence + read model only. Nothing here is
consumed by the Rule Engine, readiness, or ranking.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.persistence import SqliteResearchPersistence
from app.global_data_provider import SingleFlightTTLCache
from app.macro_observation import (
    MacroObservation, MacroProviderError, MacroProviderConfigurationError,
    RBI_REPO_RATE, INDIA_CPI, macro_freshness_state,
)
from app.macro_acquisition import get_macro_observation
from app.india_macro_provider import RbiRepoRateProvider, IndiaCpiProvider

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _ogd_transport(records):
    def handler(request):
        return httpx.Response(200, json={"records": records})
    return httpx.MockTransport(handler)


def _rbi_provider(records, **kwargs):
    client = httpx.AsyncClient(transport=_ogd_transport(records))
    return RbiRepoRateProvider("https://api.data.gov.in/resource/rbi-repo-rate", "test-key", client=client, **kwargs)


def _cpi_provider(records, **kwargs):
    client = httpx.AsyncClient(transport=_ogd_transport(records))
    return IndiaCpiProvider("https://api.data.gov.in/resource/india-cpi", "test-key", client=client, **kwargs)


# -- 1. RBI observation parses/persists with provenance ----------------------

@pytest.mark.asyncio
async def test_rbi_observation_parses_and_persists_with_provenance():
    provider = _rbi_provider([{"period": "2026-08", "repo_rate": "6.50"}])
    observation = await provider.fetch(NOW)
    assert observation.indicator == RBI_REPO_RATE
    assert observation.actual_value == 6.50
    assert observation.provenance == "OFFICIAL_GOVERNMENT"
    assert observation.source_url == provider.endpoint

    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(observation)
    persisted = repo.load_macro_observation(RBI_REPO_RATE)
    assert persisted.actual_value == 6.50 and persisted.period == "2026-08"
    assert persisted.provenance == "OFFICIAL_GOVERNMENT"
    assert persisted.provider == "RBI_REPO_RATE_DATA_GOV_IN"


# -- 2. CPI observation parses/persists with provenance -----------------------

@pytest.mark.asyncio
async def test_cpi_observation_parses_and_persists_with_provenance():
    provider = _cpi_provider([{"period": "2026-08", "inflation_rate": "3.65"}])
    observation = await provider.fetch(NOW)
    assert observation.indicator == INDIA_CPI
    assert observation.actual_value == 3.65
    assert observation.provenance == "OFFICIAL_GOVERNMENT"

    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(observation)
    persisted = repo.load_macro_observation(INDIA_CPI)
    assert persisted.actual_value == 3.65 and persisted.period == "2026-08"


# -- 3. 25 simulated instrument consumers -> no duplicate provider acquisition

@pytest.mark.asyncio
async def test_twenty_five_concurrent_instrument_consumers_fetch_once():
    calls = []
    class CountingProvider:
        async def fetch(self, now):
            calls.append(1)
            await asyncio.sleep(0.01)
            return MacroObservation(indicator=RBI_REPO_RATE, region="IN", period="2026-08", actual_value=6.5,
                unit="PERCENT", observed_at=now, source="RBI", source_url="https://example.test/rbi",
                provider="RBI_REPO_RATE_DATA_GOV_IN")

    repo = SqliteResearchPersistence()
    cache = SingleFlightTTLCache(ttl_seconds=60)
    provider = CountingProvider()
    results = await asyncio.gather(*[
        get_macro_observation(repo, provider, cache, RBI_REPO_RATE, now=NOW) for _ in range(25)
    ])
    assert len(calls) == 1  # one fetch served all 25 simulated instruments
    assert all(r.actual_value == 6.5 for r in results)
    assert len(repo.load_macro_observations(RBI_REPO_RATE)) == 1  # persisted exactly once


# -- 4. persisted fresh observation is reused ---------------------------------

@pytest.mark.asyncio
async def test_persisted_fresh_observation_is_reused_without_fetch():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=INDIA_CPI, region="IN", period="2026-08",
        actual_value=3.65, unit="PERCENT", observed_at=NOW - timedelta(days=2), source="MoSPI",
        source_url="https://example.test/cpi", provider="INDIA_CPI_DATA_GOV_IN"))

    calls = []
    class ShouldNotBeCalled:
        async def fetch(self, now):
            calls.append(1)
            raise AssertionError("provider must not be called for fresh evidence")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, ShouldNotBeCalled(), cache, INDIA_CPI, now=NOW,
        ttl_seconds=35 * 24 * 3600)
    assert result.actual_value == 3.65
    assert calls == []


# -- 5. stale observation becomes eligible for refresh ------------------------

@pytest.mark.asyncio
async def test_stale_observation_triggers_refresh():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=INDIA_CPI, region="IN", period="2026-06",
        actual_value=3.40, unit="PERCENT", observed_at=NOW - timedelta(days=100), source="MoSPI",
        source_url="https://example.test/cpi", provider="INDIA_CPI_DATA_GOV_IN"))

    calls = []
    class RefreshedProvider:
        async def fetch(self, now):
            calls.append(1)
            return MacroObservation(indicator=INDIA_CPI, region="IN", period="2026-08", actual_value=3.65,
                unit="PERCENT", observed_at=now, source="MoSPI", source_url="https://example.test/cpi",
                provider="INDIA_CPI_DATA_GOV_IN")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, RefreshedProvider(), cache, INDIA_CPI, now=NOW,
        ttl_seconds=35 * 24 * 3600)
    assert calls == [1]
    assert result.period == "2026-08" and result.actual_value == 3.65


def test_freshness_state_classification():
    fresh = MacroObservation(indicator=RBI_REPO_RATE, region="IN", period="2026-08", actual_value=6.5,
        unit="PERCENT", observed_at=NOW - timedelta(days=10), source="RBI", source_url="https://x.test",
        provider="RBI_REPO_RATE_DATA_GOV_IN")
    assert macro_freshness_state(fresh, NOW) == "READY_FRESH"
    stale = fresh.model_copy(update={"observed_at": NOW - timedelta(days=400)})
    assert macro_freshness_state(stale, NOW) == "READY_STALE"
    assert macro_freshness_state(None, NOW) == "MISSING"


# -- 6. provider failure preserves last valid observation ---------------------

@pytest.mark.asyncio
async def test_provider_failure_preserves_last_valid_observation():
    repo = SqliteResearchPersistence()
    original = MacroObservation(indicator=RBI_REPO_RATE, region="IN", period="2026-06", actual_value=6.25,
        unit="PERCENT", observed_at=NOW - timedelta(days=100), source="RBI", source_url="https://example.test/rbi",
        provider="RBI_REPO_RATE_DATA_GOV_IN")
    repo.upsert_macro_observation(original)

    class FailingProvider:
        async def fetch(self, now):
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:http_status_502")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    result = await get_macro_observation(repo, FailingProvider(), cache, RBI_REPO_RATE, now=NOW,
        ttl_seconds=60 * 24 * 3600)
    # Best-effort answer is the untouched last-valid observation.
    assert result.actual_value == 6.25 and result.period == "2026-06"
    # Persistence itself was never touched by the failed attempt.
    persisted = repo.load_macro_observation(RBI_REPO_RATE)
    assert persisted.actual_value == 6.25 and persisted.observed_at == original.observed_at


@pytest.mark.asyncio
async def test_provider_failure_with_no_prior_observation_raises():
    repo = SqliteResearchPersistence()
    class FailingProvider:
        async def fetch(self, now):
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE")
    cache = SingleFlightTTLCache(ttl_seconds=60)
    with pytest.raises(MacroProviderError):
        await get_macro_observation(repo, FailingProvider(), cache, RBI_REPO_RATE, now=NOW)
    assert repo.load_macro_observation(RBI_REPO_RATE) is None  # nothing fabricated


def test_provider_not_configured_raises_configuration_error():
    with pytest.raises(MacroProviderConfigurationError):
        RbiRepoRateProvider(None, "key")
    with pytest.raises(MacroProviderConfigurationError):
        IndiaCpiProvider("https://api.data.gov.in/resource/x", None)


# -- 7. missing expected value remains NULL -----------------------------------

@pytest.mark.asyncio
async def test_expected_value_remains_null_when_provider_does_not_supply_it():
    provider = _rbi_provider([{"period": "2026-08", "repo_rate": "6.50"}])
    observation = await provider.fetch(NOW)
    assert observation.expected_value is None
    assert observation.previous_value is None
    assert observation.surprise is None
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(observation)
    persisted = repo.load_macro_observation(RBI_REPO_RATE)
    assert persisted.expected_value is None


@pytest.mark.parametrize("field", ["repo_rate"])
@pytest.mark.asyncio
async def test_missing_required_field_never_fabricates_a_value(field):
    provider = _rbi_provider([{"period": "2026-08"}])  # value field absent
    with pytest.raises(MacroProviderError):
        await provider.fetch(NOW)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
@pytest.mark.asyncio
async def test_ogd_provider_http_failures_never_become_a_fabricated_value(status):
    def handler(request):
        return httpx.Response(status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RbiRepoRateProvider("https://api.data.gov.in/resource/rbi-repo-rate", "test-key", client=client)
    with pytest.raises(MacroProviderError):
        await provider.fetch(NOW)


# -- 8. macro observations never enter global_financial_facts -----------------

def test_macro_observations_table_is_distinct_from_financial_facts():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_observation(MacroObservation(indicator=RBI_REPO_RATE, region="IN", period="2026-08",
        actual_value=6.5, unit="PERCENT", observed_at=NOW, source="RBI", source_url="https://x.test",
        provider="RBI_REPO_RATE_DATA_GOV_IN"))
    from uuid import UUID
    assert repo.load_financial_facts({UUID(int=1)}) == []
    assert repo._connection.execute("SELECT COUNT(*) AS n FROM global_financial_facts").fetchone()["n"] == 0
    assert repo._connection.execute("SELECT COUNT(*) AS n FROM macro_observations").fetchone()["n"] == 1
    # No column of macro_observations is instrument-scoped.
    columns = {row["name"] for row in repo._connection.execute("PRAGMA table_info(macro_observations)").fetchall()}
    assert "instrument_id" not in columns


# -- 9. no ranking/readiness behavior changes ---------------------------------

def test_macro_modules_are_not_imported_by_rule_engine_or_ranking():
    import app.stock_rule_engine as sre
    import app.global_opportunity_orchestration as goo
    import app.global_opportunity_ranker as ranker
    for module in (sre, goo, ranker):
        source = module.__file__
        text = open(source, encoding="utf-8").read()
        assert "macro_observation" not in text
        assert "macro_acquisition" not in text
        assert "india_macro_provider" not in text
