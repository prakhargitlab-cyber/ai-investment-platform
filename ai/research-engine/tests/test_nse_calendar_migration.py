"""Actual Flyway calendar data + frozen target evidence. No network or live DB.

SQLite fixtures execute the same V25 SQL that the Java Flyway test validates.
No separate Python holiday list is used to initialize calendar reference data.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.historical_market_data import HistoricalPricePopulationService, HistoricalPriceProviderError
from app.market_sessions import completed_sessions, latest_completed_session, market_session_status, session_bounds
from app.models import CompanyResearchProfile, DailyMarketBar, MarketPriceObservation
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness import DurableResearchSnapshot, ResearchReadinessService, ResearchRequirementStatus
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter, ResearchReadinessRuntime
from app.session_history import missing_history_ranges
from app.settings import Settings
from app.technical_features import TechnicalFeatureEngine, normalize_price_history, select_price_history


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / 'services/research-service/src/main/resources/db/migration/V25__nse_cash_equity_calendar_2025_2026.sql'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/nse_ushamart_history_20261018.json').read_text(encoding='utf-8'))
KEY = UUID(FIXTURE['instrument_id'])
NOW = datetime.fromisoformat(FIXTURE['as_of'])
BUDGET = date(2026, 2, 1)
URL = f"https://finance.yahoo.com/quote/{FIXTURE['provider_symbol']}/history"

# Exact missing dates from the frozen diagnostic, cross-checked against
# NSE/CMTR/65587 (2025) and NSE/CMTR/71775 (2026), not public-holiday guesses.
REPORTED_GAPS = tuple(date.fromisoformat(day) for day in (
    '2025-10-02','2025-10-22','2025-11-05','2025-12-25',
    '2026-01-26','2026-03-03','2026-03-26','2026-03-31','2026-04-03',
    '2026-04-14','2026-05-01','2026-05-28','2026-06-26','2026-09-14','2026-10-02'))


def prices(provider=None):
    def aware(value):
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return [MarketPriceObservation(**{**row,
        'provider': provider or row['provider'],
        'observed_at': aware(row['observed_at']), 'retrieved_at': aware(row['retrieved_at'])})
        for row in FIXTURE['observations']]


def profile():
    return CompanyResearchProfile(instrument_id=KEY, company_id=KEY, company_name='Usha Martin Limited',
        ticker='USHAMART', exchange='NSE', mic='XNSE', country='IN', currency='INR',
        provider_instrument_ids={'YAHOO_FINANCE': FIXTURE['provider_symbol']})


@pytest.fixture
def store():
    value = SqliteResearchPersistence()
    value._connection.executescript(MIGRATION.read_text(encoding='utf-8'))
    yield value
    value._connection.close()


def options(store, now=NOW):
    return dict(as_of=now, currency='INR', market='XNSE',
        schedules=store.load_market_schedules({'NSE','XNSE'}),
        exceptions=store.load_market_calendar_exceptions({'NSE','XNSE'}),
        provider_symbol=FIXTURE['provider_symbol'])


def history(store, rows, now=NOW):
    return normalize_price_history(KEY, rows, **options(store, now))


def assess(store, rows, now=NOW, bars=()):
    context = options(store, now)
    evidence = {name: [] for name in ('LATEST_PRICE','VALUATION_INPUTS','HISTORICAL_PRICE_SERIES')}
    RepositoryResearchReadinessAdapter._append_market_observations(evidence, rows,
        profile=profile(), now=now, schedules=context['schedules'], exceptions=context['exceptions'], bars=bars)
    source = SimpleNamespace(load_by_global_instrument_id=lambda *args: DurableResearchSnapshot(KEY, evidence))
    result = ResearchReadinessService(source).assess(KEY, jurisdiction='INDIA', now=now)
    return result.for_requirement('HISTORICAL_PRICE_SERIES')


def candle(day=BUDGET, provider='YAHOO_FINANCE', retrieved_at=NOW):
    return prices()[0].model_copy(update={'observed_at': datetime.combine(day, time.min, timezone.utc),
        'retrieved_at': retrieved_at, 'source_url': URL, 'provider': provider})


def plan(store, rows, now=NOW):
    context = options(store, now)
    return missing_history_ranges(history(store, rows, now).session_dates,
        market=context['market'], schedules=context['schedules'], exceptions=context['exceptions'],
        start=(now - timedelta(days=400)).date(), as_of=now)


@pytest.mark.parametrize('day', REPORTED_GAPS, ids=str)
def test_each_reported_gap_is_an_official_closed_session_not_an_acquisition_target(store, day):
    context = options(store)
    assert session_bounds('XNSE', context['schedules'], context['exceptions'], day) is None
    at = datetime.combine(day, time(6), timezone.utc)
    assert market_session_status('NSE', context['schedules'], context['exceptions'], at) == 'HOLIDAY'
    assert list(completed_sessions('NSE', context['schedules'], context['exceptions'], day, day, NOW)) == []
    assert missing_history_ranges([], market='NSE', schedules=context['schedules'],
        exceptions=context['exceptions'], start=day, as_of=at) == []


@pytest.mark.parametrize('day', [date(2025,1,1), date(2026,1,14), date(2026,1,16), date(2026,10,19)])
def test_ordinary_weekdays_remain_open(store, day):
    context = options(store)
    assert session_bounds('XNSE', context['schedules'], context['exceptions'], day) is not None
    assert market_session_status('NSE', context['schedules'], context['exceptions'],
        datetime.combine(day, time(6), timezone.utc)) == 'OPEN'


@pytest.mark.parametrize('day', [date(2025,3,8), date(2026,1,31), date(2026,10,18)])
def test_ordinary_weekends_and_mock_trading_do_not_require_candles(store, day):
    context = options(store)
    assert session_bounds('XNSE', context['schedules'], context['exceptions'], day) is None


@pytest.mark.parametrize('day,opening,closing', [
    (date(2025,2,1), time(3,45), time(10)),
    (date(2025,10,21), time(8,15), time(9,15)),
    (BUDGET, time(3,45), time(10)),
])
def test_verified_special_sessions_use_official_normal_market_hours(store, day, opening, closing):
    context = options(store)
    expected = (datetime.combine(day, opening, timezone.utc), datetime.combine(day, closing, timezone.utc))
    assert session_bounds('XNSE', context['schedules'], context['exceptions'], day) == expected
    assert market_session_status('NSE', context['schedules'], context['exceptions'], expected[0]) == 'OPEN'
    assert history(store, [candle(day)]).session_dates == (day,)


def test_future_muhurat_without_verified_hours_is_explicitly_unknown(store):
    context = options(store)
    pending = next(value for value in context['exceptions'] if value.trading_date == date(2026,11,8))
    assert pending.exception_type == 'SPECIAL_SESSION'
    assert pending.open_time is pending.close_time is None
    assert market_session_status('NSE', context['schedules'], context['exceptions'],
        datetime(2026,11,8,12,tzinfo=timezone.utc)) == 'UNKNOWN'


def test_unverified_weekday_special_session_never_uses_guessed_regular_hours(store):
    # Synthetic operator exception, not an asserted exchange holiday.
    store._connection.execute("INSERT INTO market_trading_calendar_exceptions "
        "(market_code,trading_date,exception_type) VALUES ('NSE','2026-10-21','SPECIAL_SESSION')")
    store._connection.commit()
    context = options(store)
    day = date(2026,10,21)
    assert session_bounds('XNSE', context['schedules'], context['exceptions'], day) is None
    assert missing_history_ranges([], market='XNSE', schedules=context['schedules'],
        exceptions=context['exceptions'], start=day,
        as_of=datetime(2026,10,21,12,tzinfo=timezone.utc)) == []


@pytest.mark.parametrize('daily_bars', [False, True])
def test_unresolved_special_session_cannot_silently_disappear_from_historical_readiness(store, daily_bars):
    at = datetime(2026,11,9,12,tzinfo=timezone.utc)
    context = options(store, at)
    # Build an explicitly synthetic series over every KNOWN completed session.
    # There is no guessed time/candle for the unresolved Nov 8 Muhurat session.
    days = [day for day, _ in completed_sessions('XNSE', context['schedules'], context['exceptions'],
        date(2026,1,1), at.date(), at)]
    if daily_bars:
        rows = []
        bars = [DailyMarketBar(global_instrument_id=KEY, trading_date=day, close='100',
            provider='NSE', provider_symbol='USHAMART', source_url='https://www.nseindia.com/history',
            source_mode='REAL', currency='INR', retrieved_at=at) for day in days]
    else:
        rows = [candle(day, retrieved_at=at) for day in days]
        bars = []
    selected, _, _, _ = select_price_history(KEY, rows, daily_bar_history=bars, **context)
    assert selected.missing_dates == (date(2026,11,8),)
    assert assess(store, rows, at, bars).status == ResearchRequirementStatus.PARTIAL
    technical = TechnicalFeatureEngine().compute(KEY, rows, daily_bar_history=bars, **context)
    assert 'HISTORY_SESSION_GAPS' in technical.missing_inputs
    # Missing calendar times require a reference-data update, not guessed
    # provider ranges or permission to skip the known trading date.
    assert missing_history_ranges(selected.session_dates, market='XNSE',
        schedules=context['schedules'], exceptions=context['exceptions'],
        start=days[0], as_of=at) == []


def test_empty_calendar_reproduces_the_real_fifteen_gap_failure():
    original = SqliteResearchPersistence()
    try:
        series = history(original, prices())
        assert len(series.observations) == 273 and series.rejected_count == 2
        assert series.missing_dates == REPORTED_GAPS
        value = assess(original, prices())
        assert value.status == ResearchRequirementStatus.PARTIAL
        assert value.missing_reason == 'INSUFFICIENT_COMPLETE_EVIDENCE'
    finally:
        original._connection.close()


@pytest.mark.parametrize('provider', ['YAHOO_FINANCE','YAHOO_FINANCE_MCP'])
def test_calendar_removes_false_gaps_but_requires_the_real_sunday_budget_candle(store, provider):
    rows = prices(provider)
    series = history(store, rows)
    # The additional Jan 15 election holiday also invalidates a provider bar
    # incorrectly dated on a closed session, without deleting the source row.
    assert len(series.observations) == 272 and series.rejected_count == 3
    assert series.missing_dates == (BUDGET,)
    assert series.session_dates[-1] == date(2026,10,16)
    assert assess(store, rows).status == ResearchRequirementStatus.PARTIAL
    assert plan(store, rows) == [(BUDGET, date(2026,2,2))]
    repaired = [*rows, candle(provider=provider)]
    assert not history(store, repaired).missing_dates
    value = assess(store, repaired)
    assert value.status == ResearchRequirementStatus.READY_FRESH
    assert value.coverage_pct == 100
    assert len(history(store, repaired).observations) == 273
    assert plan(store, repaired) == []
    technical = TechnicalFeatureEngine().compute(KEY, repaired, **options(store))
    assert technical.observation_count == 273
    assert 'HISTORY_SESSION_GAPS' not in technical.missing_inputs


def test_genuine_ordinary_weekday_gap_still_blocks_readiness_and_has_a_targeted_plan(store):
    missing = date(2026,10,15)
    rows = [row for row in prices() if not (row.source_url.endswith('/history') and row.observed_at.date() == missing)]
    rows.append(candle())
    assert history(store, rows).missing_dates == (missing,)
    assert assess(store, rows).status == ResearchRequirementStatus.PARTIAL
    assert plan(store, rows) == [(missing, date(2026,10,16))]


def test_fresh_quote_does_not_hide_a_stale_history_tail_and_holidays_extend_the_calendar(store):
    rows = [*prices(), candle()]
    at = datetime(2026,10,21,12,tzinfo=timezone.utc)
    rows.append(prices()[-1].model_copy(update={'observed_at': at - timedelta(minutes=1), 'retrieved_at': at}))
    assert not history(store, rows, at).missing_dates  # completeness and freshness remain separate
    assert assess(store, rows, at).status == ResearchRequirementStatus.READY_STALE
    context = options(store)
    assert latest_completed_session('XNSE', context['schedules'], context['exceptions'],
        datetime(2026,10,20,12,tzinfo=timezone.utc)) == datetime(2026,10,19,10,tzinfo=timezone.utc)


def persist_target(store):
    for row in prices():
        store.upsert_market_price_observation(row)
    repository = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    repository.profiles.append(profile())
    return repository


def freeze(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr('app.research_readiness_runtime.datetime', Clock)
    monkeypatch.setattr('app.research_readiness.datetime', Clock)


@pytest.mark.asyncio
async def test_real_missing_session_is_fetched_once_and_completed_history_is_reused_after_restart(store, monkeypatch):
    freeze(monkeypatch)
    repository = persist_target(store)
    requests = []

    class Provider:
        async def closes(self, instrument, *, start, end):
            requests.append((start.date(), end.date()))
            return [candle()]

    jobs = SimpleNamespace(settings=Settings(), population=HistoricalPricePopulationService(repository, Provider()))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    assert await executor._ensure_historical_prices(KEY) == 1
    assert requests == [(BUDGET, date(2026,2,2))]
    restarted = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    restarted.profiles.append(profile())
    jobs.population = HistoricalPricePopulationService(restarted, Provider())
    later = ExistingResearchCapabilityExecutor(restarted, SimpleNamespace(), jobs)
    assert await later._ensure_historical_prices(KEY) == 0
    later.execute_primary = AsyncMock(side_effect=AssertionError('Complete persisted evidence must be reused'))
    runtime = ResearchReadinessRuntime(restarted, RepositoryResearchReadinessAdapter(restarted), later)
    result = await runtime.ensure(KEY, jurisdiction='INDIA', requirement_ids=['HISTORICAL_PRICE_SERIES'])
    assert result.readiness.for_requirement('HISTORICAL_PRICE_SERIES').status == ResearchRequirementStatus.READY_FRESH
    assert result.planned_requirement_ids == ()
    later.execute_primary.assert_not_awaited()
    assert requests == [(BUDGET, date(2026,2,2))]
    restored = store.load_market_price_observations({KEY})
    assert len(restored) == 276 and all(row in restored for row in prices())


@pytest.mark.asyncio
async def test_failure_on_the_single_genuine_gap_keeps_all_existing_observations(store, monkeypatch):
    freeze(monkeypatch)
    repository = persist_target(store)
    requests = []

    class UnavailableProvider:
        async def closes(self, instrument, *, start, end):
            requests.append((start.date(),end.date()))
            raise HistoricalPriceProviderError('HISTORICAL_PRICE_PROVIDER_UNAVAILABLE')

    jobs = SimpleNamespace(settings=Settings(), population=HistoricalPricePopulationService(repository, UnavailableProvider()))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    with pytest.raises(HistoricalPriceProviderError, match='HISTORICAL_PRICE_PROVIDER_UNAVAILABLE'):
        await executor._ensure_historical_prices(KEY)
    assert requests == [(BUDGET,date(2026,2,2))]  # no hidden provider retry loop
    restored = store.load_market_price_observations({KEY})
    assert len(restored) == 275 and all(row in restored for row in prices())
    assert assess(store, restored).status == ResearchRequirementStatus.PARTIAL
