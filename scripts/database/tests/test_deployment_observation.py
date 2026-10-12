"""Opt-in native PostgreSQL tests; each test owns a random disposable database."""
import hashlib
import importlib.util
import inspect
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

spec = importlib.util.spec_from_file_location('observation', Path(__file__).parents[1] / 'deployment_observation.py')
obs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obs)
DSN = os.environ.get('AIP_TEST_POSTGRES_ADMIN_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='independently isolated PostgreSQL required')


@pytest.fixture
def conn():
    name = 'aip_observation_test_' + uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute('CREATE DATABASE ' + name)
    args = psycopg.conninfo.conninfo_to_dict(DSN)
    args['dbname'] = name
    c = psycopg.connect(**args, row_factory=dict_row)
    c.execute('CREATE SCHEMA research')
    c.execute('CREATE TABLE research.global_opportunity_top_selection (cycle_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, market TEXT NOT NULL, payload TEXT NOT NULL)')
    c.commit()
    try:
        yield c
    finally:
        c.close()
        admin.execute('DROP DATABASE ' + name + ' WITH (FORCE)')
        admin.close()


def put(c, event, job, status, generated='2026-10-11T00:00:00+00:00'):
    c.execute('INSERT INTO research.global_opportunity_top_selection VALUES (%s,%s,%s,%s)',
        (event, generated, 'ETF', json.dumps(dict(record_kind='CYCLE_JOB',cycle_id=job,status=status,updated_at=generated))))


def scan(c, durable=None):
    return obs.legacy_jobs(c, durable or {}, time.monotonic()+25, hashlib.sha256())


def test_extensive_history_plan_and_equivalence(conn):
    conn.execute("""INSERT INTO research.global_opportunity_top_selection
      SELECT lpad(n::text,9,'0'), '2026-10-11T00:00:00+00:00', 'NSE',
      json_build_object('record_kind','CYCLE_JOB','cycle_id',('job-'||(n%1000)),
        'status', CASE WHEN n>19000 THEN 'COMPLETED' ELSE 'RUNNING' END,
        'updated_at','2026-10-11T00:00:00+00:00','padding',repeat('x',2048))::text
      FROM generate_series(1,20000) n""")
    put(conn,'old','unresolved','ACCEPTED','2000-01-01T00:00:00+00:00')
    conn.execute('ANALYZE research.global_opportunity_top_selection')
    conn.commit()
    old="""WITH events AS (SELECT DISTINCT ON (payload::jsonb->>'cycle_id') payload::jsonb AS p
      FROM research.global_opportunity_top_selection WHERE payload::jsonb->>'record_kind'='CYCLE_JOB'
      ORDER BY payload::jsonb->>'cycle_id',generated_at DESC,cycle_id DESC)
      SELECT p->>'cycle_id' AS job FROM events WHERE p->>'status' NOT IN ('COMPLETED','FAILED','CANCELLED')"""
    plan=conn.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+old).fetchone()['QUERY PLAN'][0]
    expected={r['job'] for r in conn.execute(old).fetchall()}
    started=time.monotonic(); result=scan(conn); elapsed=time.monotonic()-started
    page=conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT * FROM research.global_opportunity_top_selection WHERE cycle_id > '000010000' ORDER BY cycle_id LIMIT 128").fetchone()['QUERY PLAN'][0]
    assert set(result['legacy_only_active'])==expected=={'unresolved'}
    assert result['events_scanned']==20001
    assert 'Sort' in json.dumps(plan) and 'Seq Scan' in json.dumps(plan)
    assert 'Index Scan' in json.dumps(page) and 'Sort' not in json.dumps(page)
    assert elapsed<25
    print(f"old_sql_ms={plan['Execution Time']} full_scan_seconds={elapsed:.3f} bounded_page_ms={page['Execution Time']} rows=20001")


def test_latest_tie_and_durable_authority(conn):
    put(conn,'a','legacy','RUNNING');put(conn,'z','legacy','CANCELLED')
    put(conn,'b','durable','RUNNING');put(conn,'c','pending','CANCEL_REQUESTED')
    result=scan(conn,{'durable':'COMPLETED'})
    assert result['legacy_only_active']==['pending']
    assert result['durable_backed_jobs']==1


@pytest.mark.parametrize('payload',['not-json','[]','{"record_kind":"CYCLE_JOB"}',
    '{"record_kind":"CYCLE_JOB","cycle_id":"x","status":"ALIEN","updated_at":"2026-01-01"}',
    '{"record_kind":"UNKNOWN"}'])
def test_malformed_never_ignored(conn,payload):
    conn.execute('INSERT INTO research.global_opportunity_top_selection VALUES (%s,%s,%s,%s)',('old','2000-01-01','ETF',payload))
    put(conn,'z','x','COMPLETED')
    with pytest.raises(obs.Unknown,match='MALFORMED'): scan(conn,{'x':'COMPLETED'})


def test_incomplete_scan_unknown(conn):
    with pytest.raises(obs.Unknown,match='INCOMPLETE'):
        obs.legacy_jobs(conn,{},time.monotonic()-1,hashlib.sha256())


def test_readonly_five_second_timeout(conn,monkeypatch):
    conn.commit()
    monkeypatch.setattr(obs,'TABLES',{})
    def delayed(c,*args):
        assert c.execute('SHOW statement_timeout').fetchone()['statement_timeout']=='5s'
        assert c.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
        c.execute('SELECT pg_sleep(6)')
    monkeypatch.setattr(obs,'legacy_jobs',delayed)
    start=time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled): obs.observe(conn)
    assert 4.5<time.monotonic()-start<7
    assert conn.info.transaction_status==psycopg.pq.TransactionStatus.IDLE


def test_readonly_blocks_writes_and_quiet_fingerprint(conn,monkeypatch):
    put(conn,'a','terminal','COMPLETED');conn.commit()
    monkeypatch.setattr(obs,'TABLES',{})
    a=obs.observe(conn); b=obs.observe(conn)
    assert a['status']=='QUIET_SNAPSHOT' and a['fingerprint']==b['fingerprint']
    original=obs.legacy_jobs
    def writing(c,*args):
        c.execute('DELETE FROM research.global_opportunity_top_selection')
        return original(c,*args)
    monkeypatch.setattr(obs,'legacy_jobs',writing)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction): obs.observe(conn)


def test_snapshot_excludes_concurrent_append_until_next_scan(conn):
    put(conn,'a','one','COMPLETED');conn.commit()
    conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    assert scan(conn)['events_scanned']==1
    with psycopg.connect(DSN, dbname=conn.info.dbname) as other:
        put(other,'b','new','RUNNING')
    assert scan(conn)['events_scanned']==1
    conn.rollback()
    assert scan(conn)['legacy_only_active']==['new']


@pytest.mark.parametrize('status,owner,expected',[
    ('COMPLETED',None,'QUIET_SNAPSHOT'),('CANCELLED',None,'QUIET_SNAPSHOT'),
    ('RUNNING','old','BLOCKED'),('CANCEL_REQUESTED','old','BLOCKED'),
    ('COMPLETED','old','BLOCKED'),('UNKNOWN',None,'UNKNOWN')])
def test_durable_authority_and_pending_work(conn,monkeypatch,status,owner,expected):
    conn.execute('CREATE TABLE research.global_opportunity_cycle_run (cycle_id TEXT PRIMARY KEY,status TEXT,owner_id TEXT)')
    conn.execute('INSERT INTO research.global_opportunity_cycle_run VALUES (%s,%s,%s)',('same',status,owner))
    put(conn,'event','same','RUNNING');conn.commit()
    monkeypatch.setattr(obs,'TABLES',{'global_opportunity_cycle_run':['cycle_id']})
    if expected=='UNKNOWN':
        with pytest.raises(obs.Unknown): obs.observe(conn)
    else:
        assert obs.observe(conn)['status']==expected


def test_migrations_missing_or_different_are_unknown(conn,monkeypatch):
    monkeypatch.setattr(obs,'TABLES',{})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute('CREATE TABLE portfolio.flyway_schema_history_portfolio (installed_rank INT,script TEXT,checksum INT,success BOOLEAN,type TEXT)')
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (1,'V1__test.sql',123,true,'SQL')")
    conn.commit()
    assert obs.observe(conn,{'V1__test.sql':123})['status']=='QUIET_SNAPSHOT'
    with pytest.raises(obs.Unknown,match='MIGRATION_MISMATCH'): obs.observe(conn,{'V1__test.sql':124})
    with pytest.raises(obs.Unknown,match='MIGRATION_MISMATCH'): obs.observe(conn,{'V1__test.sql':123,'V2__pending.sql':0})


def test_no_reference_to_removed_research_refresh_jobs_table():
    """V9__remove_legacy_research_refresh_jobs.sql dropped research.research_refresh_jobs.
    The observer must not reference it in TABLES, the fingerprint extra dict, or
    the BACKGROUND_REFRESH check."""
    source = inspect.getsource(obs)
    assert 'research_refresh_jobs' not in source, (
        'Observer references research_refresh_jobs which was removed by V9'
    )


def test_migrations_baseline_row_is_excluded_from_comparison(conn,monkeypatch):
    """Flyway baseline/schema rows have checksum IS NULL and type SCHEMA or
    BASELINE; they must not cause MIGRATION_MISMATCH."""
    monkeypatch.setattr(obs,'TABLES',{})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute('CREATE TABLE portfolio.flyway_schema_history_portfolio (installed_rank INT,script TEXT,checksum INT,success BOOLEAN,type TEXT)')
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (0,'\"portfolio\"',NULL,true,'SCHEMA')")
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (1,'V1__test.sql',123,true,'SQL')")
    conn.commit()
    assert obs.observe(conn,{'V1__test.sql':123})['status']=='QUIET_SNAPSHOT'


def test_migrations_failed_entry_is_unknown(conn,monkeypatch):
    """A failed (success=false) migration must cause MIGRATION_MISMATCH."""
    monkeypatch.setattr(obs,'TABLES',{})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute('CREATE TABLE portfolio.flyway_schema_history_portfolio (installed_rank INT,script TEXT,checksum INT,success BOOLEAN,type TEXT)')
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (1,'V1__test.sql',123,true,'SQL')")
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (2,'V2__failed.sql',456,false,'SQL')")
    conn.commit()
    with pytest.raises(obs.Unknown,match='MIGRATION_MISMATCH'): obs.observe(conn,{'V1__test.sql':123})


def test_migrations_skipped_when_not_expected(conn,monkeypatch):
    """When expected_migrations is None (portfolio-service not being deployed),
    the migration check is skipped entirely."""
    monkeypatch.setattr(obs,'TABLES',{})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute('CREATE TABLE portfolio.flyway_schema_history_portfolio (installed_rank INT,script TEXT,checksum INT,success BOOLEAN,type TEXT)')
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (1,'V1__test.sql',999,true,'SQL')")
    conn.commit()
    # No expected_migrations passed -> no migration check -> QUIET_SNAPSHOT
    assert obs.observe(conn)['status']=='QUIET_SNAPSHOT'


def test_migrations_modified_checksum_is_unknown(conn,monkeypatch):
    """A previously applied migration whose local checksum differs from the
    database value must cause MIGRATION_MISMATCH (not silently pass)."""
    monkeypatch.setattr(obs,'TABLES',{})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute('CREATE TABLE portfolio.flyway_schema_history_portfolio (installed_rank INT,script TEXT,checksum INT,success BOOLEAN,type TEXT)')
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (1,'V1__test.sql',123,true,'SQL')")
    conn.execute("INSERT INTO portfolio.flyway_schema_history_portfolio VALUES (2,'V2__baseline.sql',456,true,'SQL')")
    conn.commit()
    # V1 checksum matches but V2 has been modified in the local source
    with pytest.raises(obs.Unknown,match='MIGRATION_MISMATCH'): obs.observe(conn,{'V1__test.sql':123,'V2__baseline.sql':999})


def test_checkpoint_fingerprint_covers_all_rows_and_payload_columns(conn,monkeypatch):
    conn.execute('CREATE TABLE research.global_opportunity_cycle_progress (cycle_id TEXT,phase TEXT,global_instrument_id TEXT,payload TEXT,PRIMARY KEY(cycle_id,phase,global_instrument_id))')
    conn.execute("INSERT INTO research.global_opportunity_cycle_progress SELECT 'c','BASELINE',lpad(n::text,8,'0'),repeat('payload-data-',400) FROM generate_series(1,35000) n")
    conn.commit()
    monkeypatch.setattr(obs,'TABLES',{'global_opportunity_cycle_progress':['cycle_id','phase','global_instrument_id']})
    started=time.monotonic(); before=obs.observe(conn); elapsed=time.monotonic()-started
    assert before['counts']['global_opportunity_cycle_progress']==35000
    assert before['status']=='QUIET_SNAPSHOT'
    sample=next(obs.pages(conn,'research.global_opportunity_cycle_progress',['cycle_id','phase','global_instrument_id'],time.monotonic()+25,fingerprint=True))[0]
    assert 'payload' not in sample and len(sample['row_fingerprint'])==64
    conn.rollback()
    conn.execute("UPDATE research.global_opportunity_cycle_progress SET payload='changed' WHERE global_instrument_id='00000001'")
    conn.commit()
    assert obs.observe(conn)['fingerprint']!=before['fingerprint']
    print(f'checkpoint_rows=35000 complete_hash_scan_seconds={elapsed:.3f}; all payload columns hashed in PostgreSQL')


# ---------------------------------------------------------------------------
# BACKGROUND_REFRESH lifecycle regression tests
# ---------------------------------------------------------------------------

def create_refresh_runs_table(conn):
    """Create research_refresh_runs with the production schema."""
    conn.execute("""
        CREATE TABLE research.research_refresh_runs (
            refresh_run_id UUID PRIMARY KEY,
            instrument_id UUID NOT NULL,
            company_id UUID NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            completed_at TIMESTAMP WITH TIME ZONE,
            status VARCHAR(40) NOT NULL,
            mode VARCHAR(40) NOT NULL,
            documents_discovered INTEGER NOT NULL DEFAULT 0,
            documents_accepted INTEGER NOT NULL DEFAULT 0,
            events_extracted INTEGER NOT NULL DEFAULT 0,
            events_created INTEGER NOT NULL DEFAULT 0,
            events_updated INTEGER NOT NULL DEFAULT 0,
            deduplicated_count INTEGER NOT NULL DEFAULT 0,
            correlation_id VARCHAR(120),
            safe_error_code VARCHAR(120),
            safe_error_message VARCHAR(500),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)
    conn.execute('CREATE INDEX idx_refresh_runs_status ON research.research_refresh_runs (status)')


def insert_refresh_run(conn, refresh_run_id, started_at, completed_at, status, mode='TARGETED_LIVE',
                       documents_discovered=0, events_extracted=0, updated_at=None):
    """Insert a refresh run row with the given lifecycle fields."""
    if updated_at is None:
        updated_at = started_at
    conn.execute("""
        INSERT INTO research.research_refresh_runs
            (refresh_run_id, instrument_id, company_id, started_at, completed_at, status, mode,
             documents_discovered, events_extracted, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (refresh_run_id, '00000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000002', started_at, completed_at, status, mode,
          documents_discovered, events_extracted, updated_at))


def observe_refresh_only(conn):
    """Run observe() with only research_refresh_runs in TABLES, no expected migrations."""
    original_tables = obs.TABLES
    obs.TABLES = {'research_refresh_runs': ['refresh_run_id']}
    try:
        return obs.observe(conn, expected_migrations=None)
    finally:
        obs.TABLES = original_tables


def test_refresh_any_incomplete_blocks_deployment(conn):
    """1. Any refresh run with completed_at IS NULL blocks deployment — the
    observer cannot distinguish a live worker from a historical orphan via DB
    state alone (no heartbeat, no lease, no progress updates). Fail-closed."""
    create_refresh_runs_table(conn)
    conn.commit()
    now = datetime.now(timezone.utc)
    recent = now - timedelta(seconds=30)
    insert_refresh_run(conn, '11111111-1111-1111-1111-111111111111',
                       recent, None, 'RUNNING')
    conn.commit()
    result = observe_refresh_only(conn)
    assert result['status'] == 'BLOCKED'
    assert 'BACKGROUND_REFRESH' in result['reasons']


def test_refresh_unchanged_old_running_still_blocks(conn):
    """NEW: An unchanged RUNNING refresh older than 600 seconds that is still
    executing must NOT be classified as safe based on age alone. The refresh
    worker has no heartbeat or progress update, so elapsed time is not evidence
    of death."""
    create_refresh_runs_table(conn)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    # Simulate a worker that started 10 days ago and never updated progress
    # (started_at == updated_at, 0 documents/events) — this is exactly the
    # pattern of our 6 historical orphaned rows, but it could also be a
    # legitimately active worker that is simply slow or blocked.
    insert_refresh_run(conn, '22222222-2222-2222-2222-222222222222',
                       old, None, 'RUNNING', documents_discovered=0, events_extracted=0)
    conn.commit()
    result = observe_refresh_only(conn)
    assert result['status'] == 'BLOCKED'
    assert 'BACKGROUND_REFRESH' in result['reasons']


def test_refresh_unknown_status_blocks(conn):
    """4. A contradictory/unknown status with no completed_at is ambiguous — fail-closed."""
    create_refresh_runs_table(conn)
    now = datetime.now(timezone.utc)
    recent = now - timedelta(seconds=30)
    insert_refresh_run(conn, '55555555-5555-5555-5555-555555555555',
                       recent, None, 'PAUSED')
    conn.commit()
    result = observe_refresh_only(conn)
    assert result['status'] == 'BLOCKED'
    assert 'BACKGROUND_REFRESH' in result['reasons']


def test_refresh_all_incomplete_rows_remain_in_fingerprint(conn):
    """7. All incomplete rows remain in the fingerprint — a completion during
    the quiet period changes the fingerprint and is caught by Wait-DeploymentQuiet."""
    create_refresh_runs_table(conn)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    insert_refresh_run(conn, '44444444-4444-4444-4444-444444444444', old, None, 'RUNNING')
    conn.commit()
    r1 = observe_refresh_only(conn)
    assert r1['status'] == 'BLOCKED'
    assert r1['counts']['research_refresh_runs'] == 1
    assert r1['fingerprint'] and len(r1['fingerprint']) == 64
    # If the row were completed, the fingerprint would change
    conn.execute("UPDATE research.research_refresh_runs SET completed_at=%s, status='COMPLETED' WHERE refresh_run_id=%s",
                 (now, '44444444-4444-4444-4444-444444444444'))
    conn.commit()
    r2 = observe_refresh_only(conn)
    assert r2['fingerprint'] != r1['fingerprint']
    assert r2['counts']['research_refresh_runs'] == 1
    assert r2['status'] == 'QUIET_SNAPSHOT'


def test_refresh_completed_runs_do_not_block(conn):
    """Completed refresh runs (completed_at IS NOT NULL) do not block."""
    create_refresh_runs_table(conn)
    now = datetime.now(timezone.utc)
    insert_refresh_run(conn, '66666666-6666-6666-6666-666666666666',
                       now - timedelta(hours=2), now - timedelta(hours=1), 'COMPLETED')
    insert_refresh_run(conn, '77777777-7777-7777-7777-777777777777',
                       now - timedelta(hours=1), now - timedelta(minutes=30), 'COMPLETED')
    conn.commit()
    result = observe_refresh_only(conn)
    assert result['status'] == 'QUIET_SNAPSHOT'
    assert 'BACKGROUND_REFRESH' not in result.get('reasons', [])


# ---------------------------------------------------------------------------
# Flyway validation hardening regression tests
# ---------------------------------------------------------------------------

FLYWAY_CREATE = (
    "CREATE TABLE portfolio.flyway_schema_history_portfolio "
    "(installed_rank INT, script TEXT, checksum INT, success BOOLEAN, type TEXT, "
    "description TEXT, version TEXT, installed_by TEXT, installed_on TIMESTAMP, execution_time INT)"
)


def _flyway_insert(conn, rank, script, checksum, success, type_):
    conn.execute(
        "INSERT INTO portfolio.flyway_schema_history_portfolio "
        "(installed_rank, script, checksum, success, type, description, version, installed_by, installed_on, execution_time) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)",
        (rank, script, checksum, success, type_, 'desc', str(rank), 'investment', 0))


def test_flyway_legitimate_baseline_accepted(conn, monkeypatch):
    """5. A legitimate Flyway baseline (type=SCHEMA, checksum IS NULL, success=true)
    is accepted alongside matching versioned migrations."""
    monkeypatch.setattr(obs, 'TABLES', {})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute(FLYWAY_CREATE)
    _flyway_insert(conn, 0, '"portfolio"', None, True, 'SCHEMA')
    _flyway_insert(conn, 1, 'V1__baseline.sql', None, True, 'BASELINE')
    _flyway_insert(conn, 2, 'V2__next.sql', 99, True, 'SQL')
    conn.commit()
    assert obs.observe(conn, {'V2__next.sql': 99})['status'] == 'QUIET_SNAPSHOT'


def test_flyway_null_checksum_non_baseline_rejected(conn, monkeypatch):
    """6. A row with checksum IS NULL but type='SQL' (not SCHEMA/BASELINE)
    is corrupt and must be rejected with MIGRATION_MISMATCH."""
    monkeypatch.setattr(obs, 'TABLES', {})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute(FLYWAY_CREATE)
    _flyway_insert(conn, 1, 'V1__corrupt.sql', None, True, 'SQL')
    conn.commit()
    with pytest.raises(obs.Unknown, match='MIGRATION_MISMATCH'):
        obs.observe(conn, {'V1__corrupt.sql': 42})


def test_flyway_schema_row_with_checksum_treated_as_versioned(conn, monkeypatch):
    """A SCHEMA-type row with a non-NULL checksum is treated as versioned and
    must match expected_migrations exactly."""
    monkeypatch.setattr(obs, 'TABLES', {})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute(FLYWAY_CREATE)
    _flyway_insert(conn, 0, '"portfolio"', 777, True, 'SCHEMA')
    conn.commit()
    with pytest.raises(obs.Unknown, match='MIGRATION_MISMATCH'):
        obs.observe(conn, {})
    assert obs.observe(conn, {'"portfolio"': 777})['status'] == 'QUIET_SNAPSHOT'


def test_flyway_complete_fingerprint_remains_intact(conn, monkeypatch):
    """8. Complete fingerprints and migration checks remain intact."""
    monkeypatch.setattr(obs, 'TABLES', {'research_refresh_runs': ['refresh_run_id']})
    conn.execute('CREATE SCHEMA portfolio')
    conn.execute(FLYWAY_CREATE)
    create_refresh_runs_table(conn)
    now = datetime.now(timezone.utc)
    insert_refresh_run(conn, '88888888-8888-8888-8888-888888888888',
                       now - timedelta(days=10), now - timedelta(days=10), 'COMPLETED')
    insert_refresh_run(conn, '99999999-9999-9999-9999-999999999999',
                       now - timedelta(days=5), now - timedelta(days=5), 'COMPLETED')
    _flyway_insert(conn, 0, '"portfolio"', None, True, 'SCHEMA')
    _flyway_insert(conn, 1, 'V1__test.sql', 123, True, 'SQL')
    conn.commit()
    result = obs.observe(conn, {'V1__test.sql': 123})
    assert result['status'] == 'QUIET_SNAPSHOT'
    assert result['fingerprint'] and len(result['fingerprint']) == 64
    assert result['counts']['research_refresh_runs'] == 2
