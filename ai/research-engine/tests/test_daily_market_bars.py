from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models import DailyMarketBar, MarketPriceObservation
from app.persistence import SqliteResearchPersistence, DisabledResearchPersistence, persistence_from_settings
from app.postgres_persistence import PostgresResearchPersistence, _PostgresConnectionAdapter
from app.repository import ResearchRepository
from app.settings import Settings


KEY = UUID(int=1)
DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def bar(**updates):
    return DailyMarketBar.model_validate(dict(global_instrument_id=KEY, trading_date=DAY,
        open="100.123456789012", high="105.123456789012", low="99.123456789012", close="103.123456789012",
        previous_close=None, volume=None, turnover=None, currency="INR", provider="PUBLIC_SOURCE",
        provider_symbol="ABC", source_mode="REAL", source_url="https://example.test/bars", retrieved_at=NOW) | updates)


def test_fresh_sqlite_bootstrap_schema_indexes_and_repeat_startup(tmp_path):
    path = tmp_path / "fresh.sqlite"
    store = persistence_from_settings(Settings(research_persistence_enabled=True, research_database_backend="sqlite", research_database_name=str(path)))
    columns = store._connection.execute("PRAGMA table_info(global_daily_market_bars)").fetchall()
    assert {r['name'] for r in columns} == {"global_instrument_id", "trading_date", "open_price", "high_price", "low_price",
        "close_price", "previous_close", "volume", "turnover", "currency", "provider", "provider_symbol", "source_mode", "source_url", "retrieved_at"}
    assert [r['name'] for r in sorted(columns, key=lambda r:r['pk']) if r['pk']] == ["global_instrument_id", "trading_date", "provider"]
    indexes = store._connection.execute("PRAGMA index_list(global_daily_market_bars)").fetchall()
    assert any(r['unique'] for r in indexes)
    assert any(r['name'] == 'idx_daily_market_bars_date_instrument' for r in indexes)
    store.upsert_daily_market_bar(bar())
    store.migrate()
    assert SqliteResearchPersistence(path).load_daily_market_bars({KEY}) == [bar()]


def test_existing_sqlite_database_receives_table_without_manual_ddl(tmp_path, monkeypatch):
    import app.persistence as persistence
    original = persistence._sqlite_schema
    schema = original()
    # Bootstrap an earlier SQLite schema using its normal mechanism, then reopen
    # with the current bootstrap function. Existing close-only rows survive.
    previous = schema[schema.index('    CREATE TABLE IF NOT EXISTS research_acquisition_observations'):]
    path = tmp_path / 'upgrade.sqlite'
    monkeypatch.setattr(persistence, '_sqlite_schema', lambda: previous)
    old = SqliteResearchPersistence(path)
    price = MarketPriceObservation(instrument_id=KEY, observed_at=NOW, retrieved_at=NOW, price=Decimal(100),
        currency='INR', provider='EXISTING', source_url='https://example.test')
    old.upsert_market_price_observation(price)
    old._connection.close()
    monkeypatch.setattr(persistence, '_sqlite_schema', original)
    current = SqliteResearchPersistence(path)
    assert current.load_daily_market_bars({KEY}) == []
    assert current.load_market_price_observations({KEY}) == [price]


def test_insert_round_trip_preserves_decimals_date_utc_and_nulls():
    store = SqliteResearchPersistence()
    original = bar(retrieved_at=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))))
    store.upsert_daily_market_bar(original)
    loaded = store.load_daily_market_bars({KEY})[0]
    assert loaded == original
    assert loaded.open == Decimal('100.123456789012')
    assert type(loaded.trading_date) is date and loaded.retrieved_at.utcoffset() == timedelta(0)
    assert loaded.volume is loaded.turnover is loaded.previous_close is None
    assert loaded.model_dump(by_alias=True)['globalInstrumentId'] == KEY
    assert store.load_market_price_observations({KEY}) == []


def test_batch_idempotence_correction_and_provider_symbol_not_identity():
    store = SqliteResearchPersistence()
    first, second = bar(), bar(provider='SECOND_SOURCE')
    assert store.upsert_daily_market_bars([first, second]) == 2
    assert store.upsert_daily_market_bars([first, second]) == 2
    corrected = bar(open=101, high=111, low=98, close=108, previous_close=102, volume=0,
        turnover=Decimal('1234567890123.123456789012'), provider_symbol='RENAMED', source_url='https://example.test/corrected',
        source_mode='DEMO', retrieved_at=NOW+timedelta(days=1))
    store.upsert_daily_market_bar(corrected)
    assert store.load_daily_market_bars({KEY}) == [corrected, second]
    assert store.load_daily_market_bars({KEY})[0].volume == 0
    # A partial correction clears missing fields rather than preserving stale values.
    store.upsert_daily_market_bar(bar(provider_symbol=None))
    loaded = store.load_daily_market_bars({KEY})[0]
    assert loaded.volume is loaded.turnover is loaded.previous_close is loaded.provider_symbol is None


def test_large_batches_filter_order_and_no_n_plus_one():
    store = SqliteResearchPersistence()
    rows = [bar(global_instrument_id=UUID(int=i), provider=provider, trading_date=DAY+timedelta(days=day))
            for i in range(1, 504) for provider in ('B', 'A') for day in (1, 0)]
    queries = []
    store._connection.set_trace_callback(queries.append)
    store.upsert_daily_market_bars(list(reversed(rows)))
    assert sum(q.startswith('INSERT INTO global_daily_market_bars') for q in queries) == (len(rows)+49)//50
    queries.clear()
    ids = {UUID(int=i) for i in range(1, 502)}
    loaded = store.load_daily_market_bars(ids, start_date=DAY, end_date=DAY, provider='B')
    assert [b.global_instrument_id.int for b in loaded] == list(range(1, 502))
    assert all(b.trading_date == DAY and b.provider == 'B' for b in loaded)
    assert len(queries) == 2 and all('global_instrument_id IN (' in q for q in queries)
    all_rows = store.load_daily_market_bars({KEY, UUID(int=2)})
    assert [(r.global_instrument_id.int,r.trading_date,r.provider) for r in all_rows] == sorted((r.global_instrument_id.int,r.trading_date,r.provider) for r in all_rows)


def test_empty_ids_and_empty_batch_issue_no_queries():
    store = SqliteResearchPersistence()
    store.upsert_daily_market_bar(bar())
    queries = []
    store._connection.set_trace_callback(queries.append)
    assert store.load_daily_market_bars(set()) == []
    assert store.upsert_daily_market_bars([]) == 0
    assert queries == []
    with pytest.raises(ValueError): store.load_daily_market_bars(None)
    with pytest.raises(ValueError): store.load_daily_market_bars({KEY}, start_date=DAY+timedelta(days=1), end_date=DAY)
    with pytest.raises(ValueError): store.load_daily_market_bars({KEY}, provider=' ')
    with pytest.raises(ValueError): store.load_daily_market_bars({KEY}, start_date=NOW)


@pytest.mark.parametrize('updates', [
    {'volume': -1}, {'volume': 1.5}, {'volume': True}, {'volume': 9223372036854775808},
    {'high': 90, 'low': 100}, {'open': 0}, {'close': -1}, {'previous_close': 0}, {'turnover': -1},
    {'currency': ' '}, {'provider': ' '}, {'trading_date': NOW}, {'trading_date': '2026-09-01T00:00:00Z'},
    {'retrieved_at': NOW.replace(tzinfo=None)}, {'close': 'NaN'}, {'turnover': 'Infinity'},
    {'close': '1.1234567890123'}, {'source_mode': 'UNKNOWN'}, {'global_instrument_id': None},
])
def test_invalid_domain_evidence_rejected(updates):
    with pytest.raises(ValidationError): bar(**updates)


def test_partial_rows_zero_turnover_and_bigint_volume_are_valid():
    store = SqliteResearchPersistence()
    value = bar(open=None, high=None, low=None, close=None, turnover=0, volume=9223372036854775807)
    store.upsert_daily_market_bar(value)
    assert store.load_daily_market_bars({KEY}) == [value]


@pytest.mark.parametrize('column,value', [('volume', -1), ('high_price', '1'), ('close_price', '0'),
    ('currency',' '), ('provider',' '), ('turnover','-1')])
def test_database_constraints_reject_invalid_updates(column, value):
    store = SqliteResearchPersistence()
    store.upsert_daily_market_bar(bar())
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(f'UPDATE global_daily_market_bars SET {column}=?', (value,))
    store._connection.rollback()
    assert store.load_daily_market_bars({KEY}) == [bar()]


def test_batch_validates_all_inputs_before_writing_and_last_correction_wins():
    store = SqliteResearchPersistence()
    invalid = bar().model_copy(update={'volume': -1})
    with pytest.raises(ValidationError): store.upsert_daily_market_bars([bar(), invalid])
    assert store.load_daily_market_bars({KEY}) == []
    corrected = bar(close=104)
    assert store.upsert_daily_market_bars([bar(), corrected]) == 1
    assert store.load_daily_market_bars({KEY}) == [corrected]


def test_postgres_uses_same_parameterized_batch_boundary():
    queries = []
    class Connection:
        def execute(self, sql, params=None): queries.append((sql, params)); return self
        def fetchall(self): return []
        def commit(self): pass
        def rollback(self): pass
    store = PostgresResearchPersistence.__new__(PostgresResearchPersistence)
    store._connection = _PostgresConnectionAdapter(Connection())
    store.upsert_daily_market_bars([bar(global_instrument_id=UUID(int=i)) for i in range(1, 102)])
    assert [len(params) for _,params in queries] == [750, 750, 15]
    assert all('%s' in sql and '?' not in sql for sql,_ in queries)
    queries.clear()
    assert store.load_daily_market_bars({KEY}, start_date=DAY, end_date=DAY, provider="a' OR 1=1 --") == []
    assert "a' OR 1=1 --" not in queries[0][0]
    assert queries[0][1] == [str(KEY), DAY.isoformat(), DAY.isoformat(), "a' OR 1=1 --"]


@pytest.mark.asyncio
async def test_repository_boundary_and_disabled_mode_are_provider_free(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a,**k: pytest.fail('provider/network invoked'))
    store = SqliteResearchPersistence()
    repo = ResearchRepository.__new__(ResearchRepository)
    repo._persistence = store
    await repo.upsert_daily_market_bar_async(bar())
    assert await repo.upsert_daily_market_bars_async([bar(provider='SECOND')]) == 1
    assert await repo.daily_market_bars_for_instruments({KEY,UUID(int=2)}, start_date=DAY, end_date=DAY, provider='SECOND') == {
        KEY: [bar(provider='SECOND')], UUID(int=2): []}
    assert repo.daily_market_bars_for({KEY}) == {KEY: store.load_daily_market_bars({KEY})}
    disabled = DisabledResearchPersistence()
    assert disabled.load_daily_market_bars({KEY}) == []
    assert disabled.upsert_daily_market_bars([bar()]) == 0
    assert disabled.upsert_daily_market_bar(bar()) is None
