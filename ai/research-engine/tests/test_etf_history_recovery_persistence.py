"""Synthetic observations only: no network, production data or research cycle."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.cycle_checkpoint import CycleOwnershipLost
from app.etf_history import EtfHistoryProvider, acquire_etf_history
from app.models import DailyMarketBar, MarketPriceObservation
from app.persistence import SqliteResearchPersistence


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
KEY, EQUITY_KEY = UUID(int=21), UUID(int=22)
ROW = dict(globalInstrumentId=str(KEY), status='ACTIVE', assetType='ETF', country='IN',
    primaryExchange='NSE', primarySymbol='TESTETF', canonicalName='Synthetic ETF',
    isin='IN0000000021', currency='INR', providerMappings=[dict(provider='NSE',
        providerSymbol='TESTETF', status='VERIFIED',
        resolutionSource='NSE_OFFICIAL_ETF_BOOTSTRAP', currency='INR')])
SOURCE = 'https://finance.yahoo.com/quote/TESTETF.NS/history?interval=1d&auto_adjust=false'


@pytest.fixture
def recovery_repo():
    store = SqliteResearchPersistence()

    async def dispatch(fn):
        return fn()

    repository = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(),
                                 _run_blocking_persistence=dispatch)
    cycle = str(UUID(int=10000))
    store.create_cycle_run(cycle, {}, market='ETF')
    assert store.claim_cycle_run(cycle, 'owner', lease_seconds=600)
    checkpoint = SimpleNamespace(cycle_id=cycle, owner_id='owner')
    yield repository, checkpoint
    store._connection.close()


def observation(**updates):
    # NSE-local midnight is 18:30 UTC on the previous date.
    return MarketPriceObservation(instrument_id=KEY,
        observed_at=datetime(2026, 11, 5, tzinfo=ZoneInfo('Asia/Kolkata')),
        price=Decimal('100'), currency='INR', provider='YAHOO_FINANCE',
        source_url=SOURCE, retrieved_at=NOW).model_copy(update=updates)


async def acquire(recovery_repo, observations, **kwargs):
    repository, checkpoint = recovery_repo
    provider = SimpleNamespace(closes=AsyncMock(return_value=observations))
    return await acquire_etf_history(repository, ROW, now=NOW, checkpoint=checkpoint,
        provider=provider, start=datetime(2026, 10, 1, tzinfo=timezone.utc), end=NOW,
        recovery_mode=True, **kwargs)


def saved(repository):
    return repository.persistence.load_daily_market_bars({KEY})


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', [
    datetime(2026, 11, 5, tzinfo=ZoneInfo('Asia/Kolkata')),
    datetime(2026, 11, 5, tzinfo=timezone.utc),
    datetime(2026, 11, 5, 10, tzinfo=timezone.utc),
])
async def test_recovery_uses_verified_completed_session_date(recovery_repo, stamp):
    result = await acquire(recovery_repo, [observation(observed_at=stamp)])
    assert result['observations'] == 1
    bar, = saved(recovery_repo[0])
    assert bar.trading_date.isoformat() == '2026-11-05'
    assert bar.source_mode == 'REAL'
    assert bar.open is bar.high is bar.low is bar.volume is None
    assert bar.global_instrument_id == KEY and bar.provider_symbol == 'TESTETF.NS'


@pytest.mark.asyncio
@pytest.mark.parametrize('updates,reason', [
    ({'instrument_id': EQUITY_KEY}, 'OBSERVATION_INSTRUMENT_MISMATCH'),
    ({'currency': 'USD'}, 'CURRENCY_MISMATCH'),
    ({'provider': 'UNVERIFIED'}, 'UNEXPECTED_PROVIDER'),
    ({'source_mode': 'MOCK'}, 'NON_REAL_HISTORY'),
    ({'source_url': 'http://finance.yahoo.com/quote/TESTETF.NS/history'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://finance.yahoo.com.evil.test/quote/TESTETF.NS/history'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://user:secret@finance.yahoo.com/quote/TESTETF.NS/history'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://finance.yahoo.com:8443/quote/TESTETF.NS/history'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://finance.yahoo.com/quote/OTHER.NS/history'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://finance.yahoo.com/quote/TESTETF.NS/history?auto_adjust=true'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'source_url': 'https://finance.yahoo.com/quote/TESTETF.NS/history?interval=1wk'}, 'UNVERIFIED_HISTORY_SOURCE'),
    ({'observed_at': NOW.replace(tzinfo=None)}, 'NAIVE_TIMESTAMP'),
    ({'retrieved_at': NOW.replace(tzinfo=None)}, 'NAIVE_TIMESTAMP'),
    ({'retrieved_at': NOW + timedelta(seconds=1)}, 'FUTURE_TIMESTAMP'),
    ({'observed_at': NOW + timedelta(days=1)}, 'FUTURE_TIMESTAMP'),
    ({'retrieved_at': datetime(2026, 11, 5, 9, tzinfo=timezone.utc)}, 'UNCOMPLETED_SESSION_OBSERVATION'),
    ({'observed_at': datetime(2026, 10, 31, tzinfo=timezone.utc)}, 'NOT_A_CONFIGURED_TRADING_SESSION'),
    ({'observed_at': datetime(2026, 11, 5, 8, tzinfo=timezone.utc)}, 'UNCOMPLETED_SESSION_OBSERVATION'),
    ({'price': Decimal('NaN')}, 'MALFORMED_OBSERVATION'),
    ({'price': Decimal('0')}, 'INVALID_CLOSE'),
])
async def test_recovery_rejects_injected_observations_atomically(recovery_repo, updates, reason):
    result = await acquire(recovery_repo, [observation(), observation(**updates)])
    assert result['status'] == 'TECHNICAL_FAILURE'
    assert reason in result['reason']
    assert saved(recovery_repo[0]) == []


@pytest.mark.asyncio
async def test_conflicting_same_session_is_not_last_value_wins(recovery_repo):
    result = await acquire(recovery_repo, [observation(), observation(price=Decimal('101'))])
    assert result['reason'].endswith('CONFLICTING_PROVIDER_SESSION')
    assert saved(recovery_repo[0]) == []


@pytest.mark.asyncio
async def test_matching_duplicate_is_one_observation(recovery_repo):
    result = await acquire(recovery_repo, [observation(), observation()])
    assert result['observations'] == 1
    assert len(saved(recovery_repo[0])) == 1


@pytest.mark.asyncio
async def test_insert_only_is_idempotent_and_preserves_richer_existing_bars(recovery_repo):
    repository, _ = recovery_repo
    existing = DailyMarketBar(global_instrument_id=KEY, trading_date='2026-11-05',
        open=Decimal('101'), high=Decimal('105'), low=Decimal('99'), close=Decimal('102'),
        volume=999, currency='INR', provider='YAHOO_FINANCE', provider_symbol='TESTETF.NS',
        source_mode='REAL', source_url=SOURCE, retrieved_at=NOW - timedelta(hours=1))
    repository.persistence.upsert_daily_market_bars([existing])
    first = await acquire(recovery_repo, [observation(), observation(
        observed_at=datetime(2026, 11, 4, tzinfo=timezone.utc))])
    after_first = [bar.model_dump() for bar in saved(repository)]
    second = await acquire(recovery_repo, [observation(price=Decimal('999')), observation(
        observed_at=datetime(2026, 11, 4, tzinfo=timezone.utc), price=Decimal('888'))])
    assert first['observations'] == 1 and first['preserved_conflicts'] == 1
    assert second['observations'] == 0 and second['preserved_conflicts'] == 2
    assert second['provider_status'] == 'SUCCESS_WITH_DATA'
    assert second['accepted_provider_bar_count'] == 2
    assert second['persisted_bar_count'] == 0
    assert second['write_policy'] == 'INSERT_ONLY'
    assert [bar.model_dump() for bar in saved(repository)] == after_first
    assert saved(repository)[1].model_dump() == existing.model_dump()


@pytest.mark.asyncio
async def test_recovery_never_changes_other_instrument_or_equity_state(recovery_repo):
    repository, _ = recovery_repo
    equity = DailyMarketBar(global_instrument_id=EQUITY_KEY, trading_date='2026-11-05',
        close=Decimal('250'), volume=1234, currency='INR', provider='NSE',
        provider_symbol='SYNTHETIC_EQUITY', source_mode='REAL',
        source_url='https://www.nseindia.com/', retrieved_at=NOW)
    repository.persistence.upsert_daily_market_bars([equity])
    store = repository.persistence
    equity_tables = ['research_events', 'global_financial_facts', 'global_opportunity_snapshot',
                     'stock_recommendation_history', 'recommendation_current_state']
    before = {table: [tuple(row) for row in store._connection.execute('SELECT * FROM ' + table)]
              for table in equity_tables}
    assert (await acquire(recovery_repo, [observation()]))['observations'] == 1
    assert store.load_daily_market_bars({EQUITY_KEY}) == [equity]
    assert before == {table: [tuple(row) for row in store._connection.execute('SELECT * FROM ' + table)]
                      for table in equity_tables}


@pytest.mark.asyncio
async def test_unverified_canonical_mapping_never_calls_provider(recovery_repo):
    repository, checkpoint = recovery_repo
    provider = SimpleNamespace(closes=AsyncMock(return_value=[observation()]))
    result = await acquire_etf_history(repository, ROW | {'providerMappings': []}, now=NOW,
        checkpoint=checkpoint, provider=provider, recovery_mode=True)
    assert result['status'] == 'TECHNICAL_FAILURE'
    assert 'CANONICAL' in result['reason']
    provider.closes.assert_not_awaited()
    assert saved(repository) == []


@pytest.mark.asyncio
async def test_missing_calendar_never_calls_provider(recovery_repo):
    repository, checkpoint = recovery_repo
    with repository.persistence._connection:
        repository.persistence._connection.execute('DELETE FROM market_trading_schedules')
    provider = SimpleNamespace(closes=AsyncMock(return_value=[observation()]))
    result = await acquire_etf_history(repository, ROW, now=NOW, checkpoint=checkpoint,
        provider=provider, recovery_mode=True)
    assert result['reason'] == 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE'
    provider.closes.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_quote_is_not_accepted_for_verified_canonical_etf(recovery_repo):
    repository, checkpoint = recovery_repo
    stamp = SimpleNamespace(to_pydatetime=lambda: observation().observed_at)
    history = SimpleNamespace(iterrows=lambda: [(stamp, {'Close': '100'})])
    ticker = SimpleNamespace(history=lambda **kwargs: history, info=dict(
        symbol='TESTETF.NS', quoteType='INDEX', exchange='NSI', currency='INR',
        isin=ROW['isin'], longName=ROW['canonicalName']))
    result = await acquire_etf_history(repository, ROW, now=NOW, checkpoint=checkpoint,
        provider=EtfHistoryProvider(lambda symbol: ticker), recovery_mode=True)
    assert result['status'] == 'TECHNICAL_FAILURE'
    assert result['reason'] == 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:QUOTE_TYPE_MISMATCH'
    assert saved(repository) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery_mode', [False, True])
@pytest.mark.parametrize('error', [CycleOwnershipLost('synthetic'), asyncio.CancelledError()])
async def test_ownership_loss_and_cancellation_are_never_provider_failures(recovery_repo, recovery_mode, error):
    repository, checkpoint = recovery_repo
    provider = SimpleNamespace(closes=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await acquire_etf_history(repository, ROW, now=NOW, checkpoint=checkpoint,
            provider=provider, recovery_mode=recovery_mode)
    assert saved(repository) == []


@pytest.mark.asyncio
async def test_lost_owner_before_persistence_rejects_observations(recovery_repo):
    repository, checkpoint = recovery_repo

    async def closes(*args, **kwargs):
        with repository.persistence._connection:
            repository.persistence._connection.execute(
                'UPDATE global_opportunity_cycle_run SET owner_id=? WHERE cycle_id=?',
                ('new-owner', checkpoint.cycle_id))
        return [observation()]

    with pytest.raises(CycleOwnershipLost):
        await acquire_etf_history(repository, ROW, now=NOW, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=closes), recovery_mode=True)
    assert saved(repository) == []


@pytest.mark.asyncio
async def test_lease_expiry_after_insert_rolls_back_transaction(recovery_repo, monkeypatch):
    import app.etf_history as history
    repository, checkpoint = recovery_repo
    original = history._insert_recovery_bars

    def expire_after_insert(store, bars):
        inserted = original(store, bars)
        store._connection.execute(
            'UPDATE global_opportunity_cycle_run SET lease_expires_at=? WHERE cycle_id=?',
            ('2000-01-01T00:00:00+00:00', checkpoint.cycle_id))
        return inserted

    monkeypatch.setattr(history, '_insert_recovery_bars', expire_after_insert)
    with pytest.raises(CycleOwnershipLost):
        await acquire(recovery_repo, [observation()])
    assert saved(repository) == []


@pytest.mark.asyncio
async def test_outside_requested_session_range_is_not_inserted(recovery_repo):
    result = await acquire(recovery_repo, [observation(
        observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc))])
    assert result['status'] == 'SUCCESS_EMPTY' and result['observations'] == 0
    assert saved(recovery_repo[0]) == []


@pytest.mark.asyncio
async def test_real_fetch_retrieval_after_frozen_horizon_is_valid(recovery_repo):
    result = await acquire(recovery_repo, [observation(retrieved_at=NOW + timedelta(seconds=5))],
        retrieval_clock=lambda: NOW + timedelta(seconds=10))
    assert result['observations'] == 1
    assert saved(recovery_repo[0])[0].retrieved_at == NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_validation_clock_does_not_extend_frozen_session_horizon(recovery_repo):
    # The validation clock may advance after the I/O, but the Saturday request
    # horizon and a later Monday's genuine session must never be confused.
    result = await acquire(recovery_repo, [observation(
        observed_at=NOW + timedelta(days=3), retrieved_at=NOW + timedelta(days=3))],
        retrieval_clock=lambda: NOW + timedelta(days=4))
    assert result['status'] == 'TECHNICAL_FAILURE'
    assert result['reason'].endswith('FUTURE_TIMESTAMP')
    assert saved(recovery_repo[0]) == []
