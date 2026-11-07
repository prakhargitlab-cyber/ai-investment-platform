"""Actual PostgreSQL enforcement on an explicitly named disposable database.

No application migration, provider or live database is used. Set the dedicated
AIP_TEST_ETF_DISCOVERY_DSN only for an isolated local PostgreSQL test container.
All schemas/rows below are synthetic test fixtures, never market evidence.
"""
import copy
import os
from threading import RLock
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
import pytest

from app.etf_history_discovery import discover_etf_history_recovery
from app.postgres_persistence import PostgresResearchPersistence, _PostgresConnectionAdapter
from test_etf_history_recovery import SOURCE_ID, START, END, NOW, bar, source


@pytest.fixture
def pg_repo():
    dsn = os.environ.get('AIP_TEST_ETF_DISCOVERY_DSN')
    if not dsn:
        pytest.skip('explicit disposable discovery PostgreSQL database required')
    config = conninfo_to_dict(dsn)
    if (config.get('dbname') != 'etf_history_discovery_test'
            or config.get('host') != '127.0.0.1'
            or not config.get('port') or config['port'] == '5432'):
        pytest.fail('REFUSED: discovery tests require the dedicated disposable loopback database')
    connection = psycopg.connect(dsn, row_factory=dict_row)
    schema = 'discovery_test_' + uuid4().hex
    connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    connection.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
    connection.execute('''
        CREATE TABLE global_opportunity_cycle_run (
            cycle_id text PRIMARY KEY, market text NOT NULL, status text NOT NULL,
            parameters text NOT NULL, as_of text, selection text, owner_id text,
            lease_expires_at text, resume_count integer DEFAULT 0, error_code text,
            created_at text NOT NULL, updated_at text NOT NULL);
        CREATE TABLE global_opportunity_cycle_active (
            market text PRIMARY KEY, cycle_id text REFERENCES global_opportunity_cycle_run);
        CREATE TABLE global_opportunity_cycle_progress (
            cycle_id text REFERENCES global_opportunity_cycle_run, phase text,
            global_instrument_id text, state text, disposition text, failure_reason text,
            attempts integer DEFAULT 0, payload text, updated_at text,
            PRIMARY KEY(cycle_id,phase,global_instrument_id));
        CREATE TABLE global_daily_market_bars (
            global_instrument_id uuid NOT NULL, trading_date date NOT NULL,
            open_price numeric(38,12), high_price numeric(38,12), low_price numeric(38,12),
            close_price numeric(38,12), previous_close numeric(38,12), volume bigint,
            turnover numeric(38,12), currency text, provider text NOT NULL,
            provider_symbol text, source_mode text, source_url text, retrieved_at text,
            PRIMARY KEY(global_instrument_id,trading_date,provider));
        CREATE TABLE market_trading_schedules (
            market_code text, mic text, country_code text, timezone text,
            trading_day integer, regular_open_time time, regular_close_time time, enabled boolean);
        CREATE TABLE market_trading_calendar_exceptions (
            market_code text, trading_date date, exception_type text,
            open_time time, close_time time, reason text);
        CREATE TABLE etf_radar_cycles (
            cycle_id text PRIMARY KEY, radar_version text, correlation_id text,
            as_of text, payload text, created_at text);
        CREATE TABLE equity_marker(id integer PRIMARY KEY, payload text);
        INSERT INTO equity_marker VALUES (1,'synthetic preserved equity');
        INSERT INTO market_trading_schedules
            SELECT 'NSE','XNSE','IN','Asia/Kolkata',i,'09:15'::time,'15:30'::time,true
            FROM generate_series(0,4) i;
    ''')
    connection.commit()
    store = PostgresResearchPersistence.__new__(PostgresResearchPersistence)
    # Exercise the real production adapter/context and inherited read methods;
    # the minimal schema is a fixture, not the service's migration/bootstrap.
    store._connection = _PostgresConnectionAdapter(connection)

    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(),
                           _run_blocking_persistence=dispatch, dsn=dsn, schema=schema)
    try:
        yield repo
    finally:
        connection.rollback()
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        connection.commit()
        connection.close()


@pytest.mark.parametrize('statement', [
    "INSERT INTO equity_marker VALUES(2,'must fail')",
    "UPDATE equity_marker SET payload='must fail'",
    'DELETE FROM equity_marker',
    'ALTER TABLE equity_marker ADD COLUMN forbidden text',
    'CREATE TABLE forbidden_table(id integer)',
])
def test_postgres_rejects_persistent_writes_in_read_only_snapshot(pg_repo, statement):
    store = pg_repo.persistence
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with store.read_only_snapshot():
            store._connection.execute(statement)
    assert store._connection.connection.info.transaction_status == TransactionStatus.IDLE
    with store._connection:
        assert store._connection.execute('SELECT * FROM equity_marker').fetchall() == [
            {'id': 1, 'payload': 'synthetic preserved equity'}]


def test_postgres_nested_reads_share_repeatable_read_snapshot(pg_repo):
    store = pg_repo.persistence
    with store.read_only_snapshot():
        # Normal persistence methods nest connection contexts. They cannot end
        # the outer read-only transaction or observe concurrent committed rows.
        with store._connection:
            assert store._connection.execute('SELECT count(*) n FROM equity_marker').fetchone()['n'] == 1
        with psycopg.connect(pg_repo.dsn) as other:
            other.execute(sql.SQL('INSERT INTO {}.equity_marker VALUES(2,%s)').format(
                sql.Identifier(pg_repo.schema)), ('synthetic concurrent row',))
        with store._connection:
            assert store._connection.execute('SELECT count(*) n FROM equity_marker').fetchone()['n'] == 1
            assert store._connection.execute("SHOW transaction_read_only").fetchone()['transaction_read_only'] == 'on'
            assert store._connection.execute("SHOW transaction_isolation").fetchone()['transaction_isolation'] == 'repeatable read'
    with store._connection:
        assert store._connection.execute('SELECT count(*) n FROM equity_marker').fetchone()['n'] == 2
        assert store._connection.execute('SHOW default_transaction_read_only').fetchone()['default_transaction_read_only'] == 'off'


def test_read_only_snapshot_refuses_pending_work_without_committing_it(pg_repo):
    store = pg_repo.persistence
    store._connection.execute("INSERT INTO equity_marker VALUES(2,'pending caller work')")
    with pytest.raises(RuntimeError, match='REQUIRES_IDLE_CONNECTION'):
        with store.read_only_snapshot():
            pytest.fail('must refuse an already open transaction')
    assert store._connection.connection.info.transaction_status == TransactionStatus.INTRANS
    store._connection.connection.rollback()
    with store._connection:
        assert store._connection.execute('SELECT count(*) n FROM equity_marker').fetchone()['n'] == 1


@pytest.mark.asyncio
async def test_postgres_discovery_is_enforced_read_only_and_preserves_state(pg_repo, monkeypatch):
    rows = await source(pg_repo, count=2)
    store = pg_repo.persistence
    store.upsert_daily_market_bars([bar(rows[0], START)])
    source_before = copy.deepcopy(store.cycle_run(SOURCE_ID))
    progress_before = copy.deepcopy(store.cycle_progress(SOURCE_ID))
    bars_before = store.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows})
    sql_seen, original = [], store._connection.execute
    def traced(statement, params=None):
        sql_seen.append(statement)
        return original(statement, params)
    monkeypatch.setattr(store._connection, 'execute', traced)
    result = await discover_etf_history_recovery(pg_repo, SOURCE_ID, start=START, end=END, now=NOW)
    assert len(result['candidates']) == 2
    assert result['execution_authorized'] is False and result['provider_requests'] == 0
    assert sql_seen.count('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY') == 1
    assert all(s.startswith(('SET TRANSACTION ', 'SELECT ')) for s in sql_seen)
    assert store.cycle_run(SOURCE_ID) == source_before
    assert store.cycle_progress(SOURCE_ID) == progress_before
    assert store.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows}) == bars_before
    assert store._connection.connection.info.transaction_status == TransactionStatus.IDLE


@pytest.mark.asyncio
async def test_postgres_discovery_cannot_bypass_write_prohibition(pg_repo, monkeypatch):
    await source(pg_repo, count=1)
    def accidental_write(store, *args, **kwargs):
        store._connection.execute("UPDATE equity_marker SET payload='forbidden callback write'")
        pytest.fail('PostgreSQL must reject the accidental callback write')
    monkeypatch.setattr('app.etf_history_discovery.coverage_details', accidental_write)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await discover_etf_history_recovery(pg_repo, SOURCE_ID, start=START, end=END, now=NOW)
    with pg_repo.persistence._connection:
        assert pg_repo.persistence._connection.execute('SELECT payload FROM equity_marker').fetchone()['payload'] == 'synthetic preserved equity'


@pytest.mark.asyncio
async def test_empty_source_contract_is_an_error_not_an_empty_shortlist(pg_repo):
    await source(pg_repo, count=1)
    with pg_repo.persistence._connection:
        pg_repo.persistence._connection.execute(
            'UPDATE global_opportunity_cycle_run SET selection=? WHERE cycle_id=?',
            ('{"universe":[]}', SOURCE_ID))
    before = copy.deepcopy(pg_repo.persistence.cycle_run(SOURCE_ID))
    with pytest.raises(ValueError, match='ETF_RECOVERY_SOURCE_UNIVERSE_UNAVAILABLE'):
        await discover_etf_history_recovery(pg_repo, SOURCE_ID, start=START, end=END, now=NOW)
    assert pg_repo.persistence.cycle_run(SOURCE_ID) == before
    assert pg_repo.persistence._connection.connection.info.transaction_status == TransactionStatus.IDLE
