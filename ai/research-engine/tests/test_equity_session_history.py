"""Behavioral regressions over durable fixtures; no provider/network access."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.historical_market_data import HistoricalPricePopulationService
from app.market_sessions import MarketCalendarException, completed_sessions, session_bounds
from app.models import CompanyResearchProfile, DailyMarketBar, MarketPriceObservation, ProvenancedValue
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness import FreshnessPolicyRegistry
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter
from app.session_history import missing_history_ranges, validate_daily_closes
from app.settings import Settings
from app.technical_features import TechnicalFeatureEngine, normalize_price_history


NOW = datetime(2026, 10, 17, 16, tzinfo=timezone.utc)  # Saturday
KEY = UUID(int=520)
URL = 'https://finance.yahoo.com/quote/EXAMPLE.NS/history?interval=1d&auto_adjust=false'


@pytest.fixture
def store():
    value = SqliteResearchPersistence()
    yield value
    value._connection.close()


def profile():
    return CompanyResearchProfile(instrument_id=KEY, company_id=KEY, company_name='Example Limited',
        ticker='EXAMPLE', exchange='NSE', mic='XNSE', country='IN', currency='INR',
        provider_instrument_ids={'YAHOO_FINANCE': 'EXAMPLE.NS'})


def calendar(store):
    return store.load_market_schedules({'NSE'}), []


def dates(store, start=date(2026, 1, 1), now=NOW, exceptions=()):
    schedules, _ = calendar(store)
    return list(dict(completed_sessions('NSE', schedules, exceptions, start, now.date(), now)))


def candle(day, *, provider='YAHOO_FINANCE', local=False, price='100', now=NOW, **changes):
    stamp = datetime.combine(day, time.min, ZoneInfo('Asia/Kolkata') if local else timezone.utc)
    return MarketPriceObservation(instrument_id=KEY, observed_at=stamp, retrieved_at=now,
        provider=provider, currency='INR', price=Decimal(price), source_url=URL).model_copy(update=changes)


def options(store, now=NOW, exceptions=()):
    schedules, _ = calendar(store)
    return dict(as_of=now, currency='INR', market='NSE', schedules=schedules,
                exceptions=exceptions, provider_symbol='EXAMPLE.NS')


def evidence(store, rows, *, now=NOW, exceptions=(), bars=()):
    result = {key: [] for key in ('LATEST_PRICE', 'VALUATION_INPUTS', 'HISTORICAL_PRICE_SERIES')}
    schedules, _ = calendar(store)
    RepositoryResearchReadinessAdapter._append_market_observations(result, rows,
        profile=profile(), now=now, schedules=schedules, exceptions=exceptions, bars=bars)
    return result


def test_duplicate_providers_and_quotes_do_not_inflate_sessions(store):
    days = dates(store)[-60:]
    rows = [candle(day) for day in days] + [candle(day, provider='YAHOO_FINANCE_MCP', local=True) for day in days]
    quote = candle(days[-1], observed_at=NOW - timedelta(minutes=1), source_url='https://finance.yahoo.com/quote/EXAMPLE.NS')
    rows += [quote.model_copy(update={'observed_at': NOW - timedelta(seconds=index)}) for index in range(100)]
    normalized = normalize_price_history(KEY, rows, **options(store))
    assert len(normalized.observations) == 60
    assert normalized.duplicate_count == 60
    assert normalized.rejected_count == 100
    value = evidence(store, rows)['HISTORICAL_PRICE_SERIES'][0]
    assert 'FIFTY_OBSERVATION_TECHNICAL_BASIS' in value.covered_input_ids
    assert 'ONE_HUNDRED_FIFTY_OBSERVATION_TECHNICAL_BASIS' not in value.covered_input_ids
    technical = TechnicalFeatureEngine().compute(KEY, rows, **options(store))
    assert technical.observation_count == 60
    assert technical.history_end == value.as_of
    # Normalizing never overwrites the original timestamp/date convention.
    assert rows[0].observed_at.hour == 0


@pytest.mark.parametrize('count', [50, 150])
@pytest.mark.parametrize('provider', ['YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'])
def test_valid_session_thresholds_are_shared_by_readiness_and_technicals(store, count, provider):
    rows = [candle(day, provider=provider) for day in dates(store)[-count:]]
    value = evidence(store, rows)['HISTORICAL_PRICE_SERIES'][0]
    technical = TechnicalFeatureEngine().compute(KEY, rows, **options(store))
    assert value.complete
    assert technical.observation_count == count
    assert technical.dma50 == 100
    assert 'FIFTY_OBSERVATION_TECHNICAL_BASIS' in value.covered_input_ids
    assert ('ONE_HUNDRED_FIFTY_OBSERVATION_TECHNICAL_BASIS' in value.covered_input_ids) == (count >= 150)


def test_fresh_quote_cannot_refresh_stale_history(store):
    rows = [candle(day) for day in dates(store) if day < date(2026, 10, 1)]
    fresh_quote = candle(date(2026, 10, 16), observed_at=NOW - timedelta(minutes=1),
        source_url='https://finance.yahoo.com/quote/EXAMPLE.NS')
    before = evidence(store, rows)['HISTORICAL_PRICE_SERIES'][0]
    after = evidence(store, [*rows, fresh_quote])
    historical = after['HISTORICAL_PRICE_SERIES'][0]
    assert historical.as_of == before.as_of
    assert historical.valid_until == before.valid_until
    assert not FreshnessPolicyRegistry.default().get('HISTORICAL_PRICE_SERIES').is_fresh(historical, NOW)
    assert after['LATEST_PRICE'][0].as_of == fresh_quote.observed_at
    technical = TechnicalFeatureEngine().compute(KEY, [*rows, fresh_quote], **options(store))
    assert 'PRICE_HISTORY' in technical.stale_inputs


def test_midnight_labels_weekend_and_holiday_use_completed_sessions(store):
    holiday = MarketCalendarException('NSE', date(2026, 10, 19), 'CLOSED')
    monday = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    rows = [candle(date(2026, 10, 16), local=True, now=monday)]
    value = evidence(store, rows, now=monday, exceptions=[holiday])['HISTORICAL_PRICE_SERIES'][0]
    assert value.as_of == datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
    assert value.valid_until == datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    assert FreshnessPolicyRegistry.default().get('HISTORICAL_PRICE_SERIES').is_fresh(value, monday)
    assert not missing_history_ranges([date(2026, 10, 16)], start=date(2026, 10, 16),
        listing_date=None, market='NSE', schedules=calendar(store)[0], exceptions=[holiday], as_of=monday)


@pytest.mark.parametrize('changes', [
    {'currency': 'USD'}, {'instrument_id': UUID(int=521)}, {'observed_at': NOW + timedelta(days=1)},
    {'retrieved_at': NOW + timedelta(days=1)}, {'observed_at': datetime(2026, 10, 16)},
    {'source_url': 'https://finance.yahoo.com/quote/WRONG.NS/history'},
    {'source_url': 'https://finance.yahoo.com/quote/EXAMPLE.BO/history'},
    {'source_url': URL.replace('auto_adjust=false', 'auto_adjust=true')},
    {'source_url': URL.replace('interval=1d', 'interval=1wk')},
    {'source_url': 'https://other.example/quote/EXAMPLE.NS/history'},
])
def test_history_identity_currency_basis_and_timestamp_fail_closed(store, changes):
    value = validate_daily_closes(KEY, [candle(date(2026, 10, 16), **changes)], **options(store))
    assert not value.observations
    assert value.rejected_count == 1


def test_provider_disagreement_is_not_resolved_by_clock_conventions(store):
    rows = [candle(date(2026, 10, 16)), candle(date(2026, 10, 16), provider='YAHOO_FINANCE_MCP', local=True, price='101')]
    result = normalize_price_history(KEY, rows, **options(store))
    assert result.conflicting_dates == (date(2026, 10, 16),)
    assert result.current_conflict
    assert not result.observations


def test_internal_gap_is_shared_by_readiness_and_technical_analysis(store):
    days = dates(store)[-60:]
    gap = days[30]
    rows = [candle(day) for day in days if day != gap]
    history = normalize_price_history(KEY, rows, **options(store))
    assert history.missing_dates == (gap,)
    assert not evidence(store, rows)['HISTORICAL_PRICE_SERIES'][0].complete
    technical = TechnicalFeatureEngine().compute(KEY, rows, **options(store))
    assert technical.observation_count == 59
    assert 'HISTORY_SESSION_GAPS' in technical.missing_inputs


def test_durable_daily_bars_also_satisfy_the_same_readiness_view(store):
    bars = [DailyMarketBar(global_instrument_id=KEY, trading_date=day, close=Decimal('100'),
        provider='NSE', provider_symbol='EXAMPLE', source_url='https://nseindia.com/history',
        source_mode='REAL', currency='INR', retrieved_at=NOW) for day in dates(store)[-60:]]
    value = evidence(store, [], bars=bars)['HISTORICAL_PRICE_SERIES'][0]
    technical = TechnicalFeatureEngine().compute(KEY, [], daily_bar_history=bars, **options(store))
    assert technical.observation_count == 60
    assert 'FIFTY_OBSERVATION_TECHNICAL_BASIS' in value.covered_input_ids


def test_planning_finds_prefix_internal_gap_and_tail_without_valid_overlaps(store):
    known = [date(2026, 10, 12), date(2026, 10, 14)]
    plan = missing_history_ranges(known, start=date(2026, 10, 1), listing_date=date(2026, 10, 9),
        market='NSE', schedules=calendar(store)[0], exceptions=[], as_of=NOW)
    assert plan == [(date(2026, 10, 9), date(2026, 10, 10)),
                    (date(2026, 10, 13), date(2026, 10, 14)),
                    (date(2026, 10, 15), date(2026, 10, 17))]


def test_open_session_does_not_require_or_accept_an_unfinished_candle(store):
    friday_open = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
    rows = [candle(date(2026, 10, 15), now=friday_open), candle(date(2026, 10, 16), now=friday_open)]
    history = validate_daily_closes(KEY, rows, **options(store, now=friday_open))
    assert history.session_dates == (date(2026, 10, 15),)
    assert history.rejected_count == 1
    assert missing_history_ranges(history.session_dates, market='NSE', schedules=calendar(store)[0],
        exceptions=[], start=date(2026, 10, 15), as_of=friday_open) == []


def test_missing_calendar_cannot_certify_session_coverage(store):
    settings = options(store)
    settings['schedules'] = []
    result = validate_daily_closes(KEY, [candle(date(2026, 10, 16))], **settings)
    assert not result.observations
    assert result.valid_until is None


@pytest.mark.asyncio
async def test_ranged_provider_overdelivery_does_not_overwrite_valid_candles(store):
    original = candle(date(2026, 10, 12))
    store.upsert_market_price_observation(original)

    class Provider:
        async def closes(self, instrument, *, start, end):
            return [original.model_copy(update={'price': Decimal('999')}), candle(date(2026, 10, 13))]

    service = HistoricalPricePopulationService(store, Provider())
    assert await service.populate([{'globalInstrumentId': str(KEY), 'currency': 'INR', 'exchange': 'NSE'}],
        start=datetime(2026, 10, 13, tzinfo=timezone.utc), end=datetime(2026, 10, 14, tzinfo=timezone.utc)) == 1
    assert original in store.load_market_price_observations({KEY})


@pytest.mark.asyncio
async def test_ranged_acquisition_reuses_durable_candles_after_new_worker_cycle(store, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr('app.research_readiness_runtime.datetime', Clock)
    repository = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    p = profile()
    p.listing_date = date(2026, 10, 9)
    repository.profiles.append(p)
    known = [candle(date(2026, 10, 12)), candle(date(2026, 10, 14))]
    for row in known:
        await repository.upsert_market_price_observation_async(row)
    calls = []

    class Provider:
        async def closes(self, instrument, *, start, end):
            calls.append((start.date(), end.date()))
            return [candle(day) for day in dates(store, start=start.date()) if day < end.date()]

    jobs = SimpleNamespace(settings=Settings(), population=HistoricalPricePopulationService(repository, Provider()))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    assert await executor._ensure_historical_prices(KEY) == 4
    assert calls == [(date(2026, 10, 9), date(2026, 10, 10)),
                     (date(2026, 10, 13), date(2026, 10, 14)),
                     (date(2026, 10, 15), date(2026, 10, 17))]
    # A new repository/worker reuses persisted data, not in-memory READY flags.
    restarted = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    restarted.profiles.append(p)
    jobs.population = HistoricalPricePopulationService(restarted, Provider())
    later = ExistingResearchCapabilityExecutor(restarted, SimpleNamespace(), jobs)
    assert await later._ensure_historical_prices(KEY) == 0
    assert len(calls) == 3
    restored = store.load_market_price_observations({KEY})
    for original in known:
        assert original in restored


@pytest.mark.parametrize('timestamps', [{}, {'verifiedAt': 'not-a-timestamp'},
    {'verifiedAt': '2026-10-01T12:00:00+00:00'}, {'sectorVerifiedAt': '2025-10-01T12:00:00+00:00'},
    {'updatedAt': '2026-09-01T12:00:00+00:00'}, {'retrievedAt': '2026-08-01T12:00:00+00:00'}])
def test_canonical_sector_missing_timestamp_is_never_replaced_with_now(timestamps):
    adapter = RepositoryResearchReadinessAdapter(SimpleNamespace())
    adapter.remember_canonical_metadata(KEY, {'canonicalSector': 'Industrials', **timestamps})
    result = {'SECTOR_MACRO': []}
    adapter._append_canonical_sector(result, KEY, profile())
    timestamp = next(iter(timestamps.values()), None)
    if not timestamp or timestamp == 'not-a-timestamp':
        assert not result['SECTOR_MACRO']
    else:
        assert result['SECTOR_MACRO'][0].as_of == datetime.fromisoformat(timestamp)
        if timestamp.startswith('2025-'):
            assert not FreshnessPolicyRegistry.default().get('SECTOR_MACRO').is_fresh(
                result['SECTOR_MACRO'][0], NOW)


def test_unknown_age_canonical_sector_does_not_fall_back_to_a_fresh_provider_sector():
    adapter = RepositoryResearchReadinessAdapter(SimpleNamespace())
    adapter.remember_canonical_metadata(KEY, {'canonicalSector': 'Industrials'})
    fallback = SimpleNamespace(snapshot=SimpleNamespace(facts={
        'sector': ProvenancedValue(value='Technology', as_of_date=NOW, retrieved_at=NOW,
                                  source_url=URL, source_name='Yahoo Finance'),
    }))
    result = {'SECTOR_MACRO': []}
    adapter._append_canonical_sector(result, KEY, profile(), structured=[fallback])
    assert result['SECTOR_MACRO'] == []
    assert adapter.canonical_metadata_for(KEY)['canonicalSector'] == 'Industrials'


@pytest.mark.asyncio
async def test_mcp_history_uses_authorized_ranged_fallback_instead_of_default_lookback(store):
    from unittest.mock import AsyncMock
    from app.research_readiness import ProviderAuthorityRegistry, ResearchRefreshTarget, ResearchRequirementRegistry, ResearchRequirementStatus
    from app.research_readiness_runtime import CapabilityExecutionResult
    from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor

    repository = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    repository.profiles.append(profile())
    legacy = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), SimpleNamespace())
    legacy.execute_primary = AsyncMock(return_value=CapabilityExecutionResult(('HISTORICAL_MARKET_DATA',), {}))
    gateway = SimpleNamespace(acquire_requirement=AsyncMock(side_effect=AssertionError('No broad MCP history request')))
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    requirement = ResearchRequirementRegistry.default().get('HISTORICAL_PRICE_SERIES')
    target = ResearchRefreshTarget(requirement.requirement_id, requirement.rule_engine_area,
        ResearchRequirementStatus.READY_STALE, ProviderAuthorityRegistry.default().policy_for(requirement.requirement_id, 'INDIA'), ())
    result = await executor.execute_primary(KEY, [target], jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    gateway.acquire_requirement.assert_not_called()
    assert legacy.execute_primary.await_args.args[1] == (target,)
    assert result.executed_capabilities == ('HISTORICAL_MARKET_DATA',)
