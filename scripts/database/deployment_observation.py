"""Read-only bootstrap observation. Runs via stdin in the existing research pod.

No application repository/worker is instantiated. A complete repeatable-read
snapshot is mandatory; pagination bounds work per statement, never total coverage.
"""
import hashlib
import json
import time
from datetime import datetime

TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}
STATES = TERMINAL | {'ACCEPTED', 'RUNNING', 'PUBLISHED', 'CANCEL_REQUESTED'}


class Unknown(RuntimeError):
    pass


def pages(conn, table, keys, deadline, batch=1024, fingerprint=False):
    # Identifiers are internal constants, never provider/user input.
    last = None
    scanned = 0
    while True:
        if time.monotonic() >= deadline:
            raise Unknown(f'INCOMPLETE_OBSERVATION: {table}; scanned={scanned}')
        where = '' if last is None else ' WHERE (' + ','.join(keys) + ') > (' + ','.join(['%s'] * len(keys)) + ')'
        fields = '*'
        if fingerprint:
            extra = {'research.global_opportunity_cycle_run': ['status', 'owner_id'],
                     'research.research_refresh_runs': ['completed_at', 'started_at']}.get(table, [])
            # Hash the complete row server-side. No column/history is omitted;
            # large immutable checkpoint payloads no longer cross the network
            # and get escaped/hashed a second time in the observation process.
            fields = ','.join(keys + extra) + ",encode(sha256(convert_to(t::text,'UTF8')),'hex') AS row_fingerprint"
        rows = conn.execute('SELECT ' + fields + ' FROM ' + table + ' AS t' + where + ' ORDER BY ' + ','.join(keys) + ' LIMIT %s',
                            (() if last is None else last) + (batch,)).fetchall()
        if not rows:
            return
        scanned += len(rows)
        yield rows
        last = tuple(rows[-1][k] for k in keys)


def legacy_jobs(conn, durable, deadline, digest):
    latest = {}
    count = 0
    for rows in pages(conn, 'research.global_opportunity_top_selection', ['cycle_id'], deadline, batch=128):
        choices = {}
        for row in rows:
            count += 1
            digest.update(json.dumps(row, sort_keys=True, default=str).encode())
            try:
                p = json.loads(row['payload'])
                if not isinstance(p, dict):
                    raise ValueError()
                if p.get('record_kind') is None:
                    continue  # ordinary publication, not a job event
                if p.get('record_kind') != 'CYCLE_JOB':
                    raise ValueError()
                job = p['cycle_id']
                if not isinstance(job, str) or not job or p['status'] not in STATES:
                    raise ValueError()
                datetime.fromisoformat(row['generated_at'])
                datetime.fromisoformat(p['updated_at'])
            except (ValueError, TypeError, KeyError):
                raise Unknown('MALFORMED_LEGACY_EVENT') from None
            choices.setdefault(job, {})[row['cycle_id']] = (row['generated_at'], p)
        # Bounded merge, with DB text collation/tie ordering identical to the old
        # DISTINCT ON query. No Python timestamp reinterpretation or history sort.
        values, params = [], []
        for job, events in choices.items():
            if job in latest:
                event, generated, payload = latest[job]
                events[event] = (generated, payload)
            for event, (generated, _) in events.items():
                values.append('(%s::text,%s::text,%s::text)')
                params.extend([job, generated, event])
        if values:
            winners = conn.execute('SELECT DISTINCT ON (job) job,event,generated FROM (VALUES ' + ','.join(values)
                + ') AS v(job,generated,event) ORDER BY job,generated DESC,event DESC', params).fetchall()
            for w in winners:
                latest[w['job']] = (w['event'], w['generated'], choices[w['job']][w['event']][1])
    unresolved = sorted(job for job, (_, _, p) in latest.items() if job not in durable and p['status'] not in TERMINAL)
    return {'events_scanned': count, 'legacy_jobs': len(latest), 'legacy_only_active': unresolved,
            'durable_backed_jobs': sum(job in durable for job in latest)}


TABLES = {
    'global_opportunity_cycle_run': ['cycle_id'],
    'global_opportunity_cycle_active': ['market'],
    'global_opportunity_cycle_progress': ['cycle_id', 'phase', 'global_instrument_id'],
    'research_refresh_runs': ['refresh_run_id'],
    'research_news_search_runs': ['run_id'], 'etf_acquisition_attempts': ['attempt_id'],
    'etf_listing_observations': ['evidence_id'], 'etf_fact_observations': ['evidence_id'],
    'etf_nav_observations': ['evidence_id'], 'etf_holdings_snapshots': ['evidence_id'],
    'etf_holdings_positions': ['snapshot_id', 'position_number'], 'etf_radar_cycles': ['cycle_id'],
}


def observe(conn, expected_migrations=None, seconds=60):
    deadline = time.monotonic() + seconds
    digest = hashlib.sha256()
    durable, active, counts = {}, [], {}
    # Incomplete refresh runs may be live workers or historical orphans.
    # No durable worker identity, heartbeat or lease distinguishes them.
    # Scheduler/recovery flags and ingress isolation do not drain existing work;
    # stable pod identity, quiet logs and fingerprints do not prove termination.
    # Every incomplete run must block deployment regardless of age.
    # All rows remain fingerprinted so completion or new starts are detected.
    conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    try:
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        if conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] != 'on':
            raise Unknown('READ_ONLY_REQUIRED')
        now = conn.execute('SELECT now()').fetchone()['now']
        for table, keys in TABLES.items():
            counts[table] = 0
            digest.update(table.encode())
            for rows in pages(conn, 'research.' + table, keys, deadline, fingerprint=True):
                for r in rows:
                    counts[table] += 1
                    digest.update(json.dumps(r, sort_keys=True, default=str).encode())
                    if table == 'global_opportunity_cycle_run':
                        if r['status'] not in STATES:
                            raise Unknown('UNKNOWN_DURABLE_STATE')
                        durable[r['cycle_id']] = r['status']
                        if r['status'] not in TERMINAL or r['owner_id'] is not None:
                            active.append('DURABLE_WORK')
                    elif table == 'global_opportunity_cycle_active':
                        active.append('ACTIVE_SLOT')
                    elif table in ('research_refresh_runs',) and r['completed_at'] is None:
                        # Fail-closed: every incomplete refresh run blocks. The
                        # refresh worker does not heartbeat or update progress
                        # during execution, so there is no reliable DB-only
                        # signal that distinguishes a live worker from an orphan.
                        active.append('BACKGROUND_REFRESH')
        jobs = legacy_jobs(conn, durable, deadline, digest)
        if jobs['legacy_only_active']:
            active.append('LEGACY_WORK')
        if expected_migrations is not None:
            rows = conn.execute(
                'SELECT script,checksum,success,type FROM portfolio.flyway_schema_history_portfolio ORDER BY installed_rank'
            ).fetchall()
            # Flyway baseline/schema rows have checksum IS NULL and type in
            # ('SCHEMA', 'BASELINE') — they are not versioned migrations and
            # must not participate in the comparison. Any row with a NULL
            # checksum that is NOT a schema/baseline row is corrupt and must
            # be rejected. Versioned rows must all be success=true; a failed
            # migration (success=false) is an explicit failure.
            baseline = [r for r in rows if r['checksum'] is None and r['type'] in ('SCHEMA', 'BASELINE')]
            versioned = [r for r in rows if r['checksum'] is not None]
            unexpected_null = [r for r in rows if r['checksum'] is None and r['type'] not in ('SCHEMA', 'BASELINE')]
            if unexpected_null:
                raise Unknown('MIGRATION_MISMATCH')
            actual = {r['script']: r['checksum'] for r in versioned if r['success']}
            if len(actual) != len(versioned) or actual != expected_migrations:
                raise Unknown('MIGRATION_MISMATCH')
        if time.monotonic() >= deadline:
            raise Unknown('INCOMPLETE_OBSERVATION')
        return {'status': 'BLOCKED' if active else 'QUIET_SNAPSHOT', 'reasons': sorted(set(active)),
                'fingerprint': digest.hexdigest(), 'counts': counts, **jobs}
    finally:
        conn.rollback()


def main(expected_migrations=None):
    # Only Settings is imported; never main, persistence constructors or workers.
    import psycopg
    from psycopg.rows import dict_row
    from app.settings import Settings
    s = Settings()
    try:
        if s.research_database_backend != 'postgres' or s.research_database_schema != 'research':
            raise Unknown('UNSUPPORTED_DATABASE')
        with psycopg.connect(host=s.research_database_host, port=s.research_database_port,
                dbname=s.research_database_name, user=s.research_database_user,
                password=s.research_database_password or '', sslmode=s.research_database_ssl_mode,
                connect_timeout=5, options='-c default_transaction_read_only=on -c statement_timeout=5000',
                row_factory=dict_row) as conn:
            result = observe(conn, expected_migrations)
    except Exception as exc:
        # Never print exception connection strings, credentials or malformed payloads.
        result = {'status': 'UNKNOWN', 'reason': str(exc) if isinstance(exc, Unknown) else type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'QUIET_SNAPSHOT' else 2
