"""V13 projection storage and V14 global suggestion lifecycle. All cycle writes publish atomically on the shared DB.

History and snapshot methods only INSERT; projection methods alone use UPDATE.
The payload preserves complete evidence alongside queryable identity/version columns.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

SNAPSHOT_FIELDS = {
    **dict.fromkeys('scanner_version rule_engine_version technical_version sector_version ranker_version price_as_of'.split(), 'TEXT'),
    **dict.fromkeys(('opportunity_score opportunity_confidence score_coverage rule_engine_score rank_position '
                    'technical_score sector_score valuation_score quality_score growth_score balance_sheet_score '
                    'quarterly_score catalyst_score news_score shareholding_score governance_score current_price').split(), 'REAL'),
    'rank_eligible': 'INTEGER',
    **dict.fromkeys('suppression_reasons top_positive_reasons top_negative_reasons evidence_state'.split(), 'JSON')}
HISTORY_FIELDS = {
    **dict.fromkeys('rule_engine_version ranker_version new_investor_action existing_holder_action short_term_action long_term_action'.split(), 'TEXT'),
    **dict.fromkeys(('price_at_recommendation opportunity_score confidence coverage short_entry_low short_entry_high '
                    'short_target_1 short_target_2 short_invalidation long_entry_low long_entry_high '
                    'long_fair_value long_target long_invalidation').split(), 'REAL'),
    **dict.fromkeys('top_positive_reasons top_negative_reasons evidence_snapshot'.split(), 'JSON')}
STATE_FIELDS = {
    **dict.fromkeys('short_term_state long_term_state current_short_action current_long_action lifecycle_status'.split(), 'TEXT'),
    **dict.fromkeys('price_at_recommendation current_price short_target_distance_pct long_target_distance_pct'.split(), 'REAL')}
EXTRA_FIELDS = {'global_opportunity_snapshot': SNAPSHOT_FIELDS, 'stock_recommendation_history': HISTORY_FIELDS,
                'recommendation_current_state': STATE_FIELDS}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS global_opportunity_snapshot (
 snapshot_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, global_instrument_id TEXT NOT NULL,
 market TEXT NOT NULL, generated_at TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(cycle_id, global_instrument_id)
);
CREATE INDEX IF NOT EXISTS opportunity_snapshot_instrument ON global_opportunity_snapshot(global_instrument_id, generated_at);
CREATE TABLE IF NOT EXISTS stock_recommendation_history (
 recommendation_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES global_opportunity_snapshot(snapshot_id),
 global_instrument_id TEXT NOT NULL, generated_at TEXT NOT NULL,
 recommendation_engine_version TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_history_instrument ON stock_recommendation_history(global_instrument_id, generated_at);
CREATE TABLE IF NOT EXISTS recommendation_current_state (
 global_instrument_id TEXT PRIMARY KEY,
 latest_recommendation_id TEXT NOT NULL REFERENCES stock_recommendation_history(recommendation_id),
 updated_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS global_opportunity_top_selection (
 cycle_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, market TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation_backtest_run (
 backtest_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
);
'''

for _table, _fields in EXTRA_FIELDS.items():
    _start = SCHEMA.index('CREATE TABLE IF NOT EXISTS ' + _table)
    _payload = SCHEMA.index('payload TEXT NOT NULL', _start)
    SCHEMA = SCHEMA[:_payload] + ', '.join(k + ' ' + ('TEXT' if v == 'JSON' else v) for k, v in _fields.items()) + ', ' + SCHEMA[_payload:]

for _table in ('global_opportunity_snapshot', 'stock_recommendation_history', 'global_opportunity_top_selection', 'recommendation_backtest_run'):
    for _operation in ('UPDATE', 'DELETE'):
        SCHEMA += f'''CREATE TRIGGER IF NOT EXISTS immutable_{_table}_{_operation.lower()}
            BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT, 'RECOMMENDATION_HISTORY_IS_IMMUTABLE'); END;\n'''

# V14: Dedicated global auto-suggestion lifecycle. No user_id ownership.
# global_market_scan and global_stock_suggestion are append-only; history is
# append-only with immutable triggers; current is the sole mutable projection.
SUGGESTION_SCHEMA = '''
CREATE TABLE IF NOT EXISTS global_market_scan (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETED','FAILED','IN_PROGRESS')),
    market TEXT NOT NULL,
    exchange TEXT,
    universe_count INTEGER NOT NULL,
    shortlist_count INTEGER NOT NULL,
    evaluated_count INTEGER NOT NULL,
    rank_eligible_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    controlled INTEGER NOT NULL DEFAULT 0,
    failure_reason_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_market_scan_created ON global_market_scan (created_at DESC);
CREATE TABLE IF NOT EXISTS global_stock_suggestion (
    suggestion_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL,
    global_instrument_id TEXT NOT NULL,
    symbol TEXT,
    company_name TEXT,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM','LONG_TERM')),
    initial_action TEXT NOT NULL CHECK (initial_action IN ('STRONG_BUY','BUY')),
    suggested_at TEXT NOT NULL,
    suggested_price REAL,
    rank INTEGER,
    opportunity_score REAL,
    confidence REAL,
    coverage REAL,
    data_state TEXT,
    entry_range_low REAL,
    entry_range_high REAL,
    fair_value REAL,
    target_1 REAL,
    target_2 REAL,
    invalidation_price REAL,
    reasons TEXT,
    risks TEXT,
    evidence_snapshot TEXT,
    engine_version TEXT NOT NULL,
    recommendation_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_suggestion_instrument ON global_stock_suggestion (global_instrument_id, horizon);
CREATE TABLE IF NOT EXISTS global_stock_suggestion_history (
    event_id TEXT PRIMARY KEY,
    suggestion_id TEXT NOT NULL REFERENCES global_stock_suggestion(suggestion_id),
    scan_id TEXT NOT NULL,
    global_instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('STRONG_BUY','BUY','PARTIAL_EXIT','SELL')),
    action_at TEXT NOT NULL,
    action_price REAL,
    rank INTEGER,
    opportunity_score REAL,
    confidence REAL,
    reasons TEXT,
    risks TEXT,
    evidence_snapshot TEXT,
    engine_version TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_suggestion_history_instrument ON global_stock_suggestion_history (global_instrument_id, horizon, action_at);
CREATE INDEX IF NOT EXISTS idx_global_suggestion_history_fingerprint ON global_stock_suggestion_history (suggestion_id, event_fingerprint);
CREATE TABLE IF NOT EXISTS global_stock_suggestion_current (
    global_instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM','LONG_TERM')),
    suggestion_id TEXT NOT NULL REFERENCES global_stock_suggestion(suggestion_id),
    latest_action TEXT NOT NULL CHECK (latest_action IN ('STRONG_BUY','BUY','PARTIAL_EXIT','SELL')),
    latest_action_at TEXT NOT NULL,
    latest_price REAL,
    original_suggested_at TEXT NOT NULL,
    original_suggested_price REAL,
    current_rank INTEGER,
    opportunity_score REAL,
    confidence REAL,
    coverage REAL,
    entry_range_low REAL,
    entry_range_high REAL,
    fair_value REAL,
    target_1 REAL,
    target_2 REAL,
    invalidation_price REAL,
    reasons TEXT,
    risks TEXT,
    evidence_snapshot TEXT,
    engine_version TEXT NOT NULL,
    last_scan_id TEXT NOT NULL,
    -- Full public-radar action/state detail (V15): real values already computed
    -- by prepare_cycle/RecommendationEngineV1 per cycle, previously dropped on
    -- write. Needed so global_opportunity_radar() can emit the same card shape
    -- opportunity_current() already does, from real data, with no fabrication.
    data_state TEXT,
    missing_areas TEXT,
    stale_areas TEXT,
    short_term_state TEXT,
    long_term_state TEXT,
    new_investor_action TEXT,
    existing_holder_action TEXT,
    current_short_action TEXT,
    current_long_action TEXT,
    PRIMARY KEY (global_instrument_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_global_suggestion_current_action ON global_stock_suggestion_current (latest_action, horizon);
'''

for _table in ('global_market_scan', 'global_stock_suggestion', 'global_stock_suggestion_history'):
    for _operation in ('UPDATE', 'DELETE'):
        SUGGESTION_SCHEMA += f'''CREATE TRIGGER IF NOT EXISTS immutable_{_table}_{_operation.lower()}
            BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT, 'SUGGESTION_HISTORY_IS_IMMUTABLE'); END;\n'''


def decode(row):
    if row is None:
        return None
    value = row['payload']
    return json.loads(value) if isinstance(value, str) else value


def _decode_json(value):
    """Normalize a V14 JSON column value across SQLite and PostgreSQL.

    PostgreSQL JSON/JSONB columns materialize as native Python list/dict from
    the DB driver; SQLite stores TEXT. Returns None for null, decodes only
    str/bytes/bytearray via json.loads, and passes list/dict/scalars through
    unchanged so already-decoded values are never stringified or re-parsed.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _row_get(row, key, default=None):
    """Read a column from a DB row, tolerating its absence.

    V15 (research-service Flyway migration) can lag behind a deployed
    research-engine image, or a SQLite dev/test row predating the widened
    schema may not carry these columns. `row[key]` raises KeyError for a
    missing key on both sqlite3.Row and the dict rows psycopg's dict_row
    factory returns, so a plain try/except here degrades to `default`
    instead of turning a not-yet-migrated column into a 500.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _decode_scan_row(row):
    if row is None:
        return None
    return dict(
        scan_id=row['scan_id'],
        started_at=row['started_at'],
        completed_at=row['completed_at'],
        status=row['status'],
        market=row['market'],
        exchange=row['exchange'],
        universe_count=row['universe_count'],
        shortlist_count=row['shortlist_count'],
        evaluated_count=row['evaluated_count'],
        rank_eligible_count=row['rank_eligible_count'],
        suppressed_count=row['suppressed_count'],
        engine_version=row['engine_version'],
        controlled=bool(row['controlled']),
        failure_reason_code=row['failure_reason_code'],
        created_at=row['created_at'],
    )


class OpportunityPersistenceMixin:
    def opportunity_rotation_after(self):
        """Read only the latest published cursor, without hydrating evidence/history."""
        offset = 0
        while True:
            # LIMIT matters for PostgreSQL's client-buffered cursor too.
            row = self._connection.execute(
                'SELECT payload FROM global_opportunity_top_selection ORDER BY generated_at DESC, cycle_id DESC LIMIT 1 OFFSET ?',
                (offset,)).fetchone()
            if row is None:
                return None
            offset += 1
            value = decode(row)
            if (not value.get('controlled_candidate_set', False)
                    and value.get('record_kind') != 'CYCLE_JOB'
                    and 'rotation_after' in value
                    and value.get('status', 'COMPLETED') == 'COMPLETED'):
                return value.get('rotation_after')

    def record_opportunity_job(self, value):
        # Existing immutable JSON publication log also stores separate job events.
        # Each event has its own storage PK; cycle_id in the payload is the request ID.
        payload = {**value, 'record_kind': 'CYCLE_JOB'}
        with self._connection:
            self._connection.execute('INSERT INTO global_opportunity_top_selection (cycle_id,generated_at,market,payload) VALUES (?,?,?,?)',
                (str(uuid4()), value['updated_at'], 'NSE', json.dumps(payload, sort_keys=True)))

    def opportunity_jobs(self):
        latest = {}
        for row in self._connection.execute('SELECT payload FROM global_opportunity_top_selection ORDER BY generated_at DESC, cycle_id DESC').fetchall():
            value = decode(row)
            if value.get('record_kind') == 'CYCLE_JOB':
                latest.setdefault(value['cycle_id'], value)
        # Cancellation is committed on the durable run, independently of the
        # worker's immutable job log. A stopped/old worker may never append a
        # cancellation event, or may append a stale RUNNING event afterward.
        # Project the authoritative state without rewriting historical events.
        for row in self._connection.execute(
            "SELECT cycle_id, status, updated_at, error_code, parameters FROM global_opportunity_cycle_run "
            "WHERE status IN ('CANCEL_REQUESTED','CANCELLED')"
        ).fetchall():
            run = dict(row)
            job = latest.setdefault(run['cycle_id'], {
                'cycle_id': run['cycle_id'], 'record_kind': 'CYCLE_JOB',
                'parameters': json.loads(run['parameters']),
            })
            job.update({key: run[key] for key in ('status', 'updated_at', 'error_code')})
        return list(latest.values())

    def recommendation_history(self, instrument_id=None):
        sql = 'SELECT payload FROM stock_recommendation_history'
        params = ()
        if instrument_id is not None:
            sql += ' WHERE global_instrument_id = ?'
            params = (str(instrument_id),)
        return [decode(r) for r in self._connection.execute(sql + ' ORDER BY generated_at, recommendation_id', params).fetchall()]

    def recommendation_states(self):
        return [decode(r) for r in self._connection.execute(
            'SELECT payload FROM recommendation_current_state ORDER BY global_instrument_id').fetchall()]

    def opportunity_current(self):
        # The flag already lives in the versioned payload: no schema change needed.
        selection = next((value for row in self._connection.execute(
            'SELECT payload FROM global_opportunity_top_selection ORDER BY generated_at DESC, cycle_id DESC').fetchall()
            if not (value := decode(row)).get('controlled_candidate_set', False)
            and value.get('record_kind') != 'CYCLE_JOB'
            and value.get('status', 'COMPLETED') == 'COMPLETED'), None)
        if selection is None:
            return dict(generated_at=None, best_buy_today=None, top_short_term=[], top_long_term=[], top_exit=[], previous_recommendations=[])
        snapshots = {s['global_instrument_id']: s for s in self.opportunity_snapshots(selection['cycle_id'])}
        states = {s['global_instrument_id']: s for s in self.recommendation_states()}
        def hydrate(card):
            key = card['global_instrument_id']
            state = states.get(key, {})
            # Older diagnostic runs may have updated the shared projection. Keep
            # the full-market publication authoritative when its history differs.
            if state.get('latest_recommendation_id') != card.get('latest_recommendation_id'):
                return card
            latest_id = state.get('latest_recommendation_id')
            history = decode(self._connection.execute(
                'SELECT payload FROM stock_recommendation_history WHERE recommendation_id = ?', (latest_id,)).fetchone()) if latest_id else None
            # Current public score/price come from the snapshot/projection, not an older recommendation.
            current = snapshots.get(key, {})
            scores = {k: current[k] for k in ('opportunity_score', 'opportunity_confidence', 'score_coverage', 'rule_engine_score') if k in current}
            return {**card, **current, **(history or {}), **scores, **state}
        for field in ('top_short_term', 'top_long_term', 'top_exit', 'previous_recommendations'):
            selection[field] = [hydrate(c) for c in selection[field]]
        if selection['best_buy_today']:
            selection['best_buy_today'] = hydrate(selection['best_buy_today'])
        return selection

    def opportunity_snapshots(self, cycle_id):
        return [decode(r) for r in self._connection.execute(
            'SELECT payload FROM global_opportunity_snapshot WHERE cycle_id = ? ORDER BY global_instrument_id', (cycle_id,)).fetchall()]

    def _insert_opportunity(self, table, columns, value):
        # Table/column names are internal constants, never caller SQL.
        extra = EXTRA_FIELDS.get(table, {})
        columns = (*columns, *extra)
        def stored(c):
            v = value.get(c)
            return json.dumps(v, sort_keys=True) if extra.get(c) == 'JSON' else int(v) if isinstance(v, bool) else v
        self._connection.execute(f"INSERT INTO {table} ({','.join(columns)},payload) VALUES ({','.join('?' for _ in range(len(columns)+1))})",
            tuple(stored(c) for c in columns) + (json.dumps(value, sort_keys=True, allow_nan=False),))

    def publish_opportunity_cycle(self, snapshots, recommendations, states, selection, *, build=None, fence=None):
        # fence=(cycle_id, owner_id): a resumable production cycle flips its
        # durable run to PUBLISHED in THIS transaction, and only while it still
        # owns the lease -- publication is exactly-once per cycle_id.
        with self._connection:
            if fence is not None:
                self._fence_cycle_publication(*fence)
            for s in snapshots:
                self._insert_opportunity('global_opportunity_snapshot',
                    ('snapshot_id', 'cycle_id', 'global_instrument_id', 'market', 'generated_at'), s)
            if build is not None:
                persisted = self.opportunity_snapshots(selection['cycle_id'])
                recommendations, states, selection = build(persisted)
            for r in recommendations:
                self._insert_opportunity('stock_recommendation_history',
                    ('recommendation_id', 'snapshot_id', 'global_instrument_id', 'generated_at', 'recommendation_engine_version', 'fingerprint'), r)
            for s in ([] if selection.get('controlled_candidate_set', False) else states):
                cols = ('global_instrument_id', 'latest_recommendation_id', 'updated_at', *STATE_FIELDS, 'payload')
                self._connection.execute(f"INSERT INTO recommendation_current_state ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
                    + 'ON CONFLICT(global_instrument_id) DO UPDATE SET ' + ','.join(f'{c}=excluded.{c}' for c in cols[1:]),
                    tuple(s.get(c) for c in cols[:-1]) + (json.dumps(s, sort_keys=True),))
            self._insert_opportunity('global_opportunity_top_selection', ('cycle_id', 'generated_at', 'market'), selection)
        return selection

    def save_backtest(self, result):
        with self._connection:
            self._insert_opportunity('recommendation_backtest_run', ('backtest_id', 'generated_at'), result)
        return result

    def backtests(self):
        return [decode(r) for r in self._connection.execute(
            'SELECT payload FROM recommendation_backtest_run ORDER BY generated_at DESC, backtest_id DESC').fetchall()]

    # ------------------------------------------------------------------
    # V14: Durable global auto-suggestion lifecycle (separate from user-owned
    # portfolio/watchlist recommendations). No user_id ownership column on
    # any global suggestion table.
    # ------------------------------------------------------------------

    @staticmethod
    def _suggest_json(value):
        return json.dumps(value, sort_keys=True, allow_nan=False) if value is not None else None

    @staticmethod
    def _suggest_ranges(card, horizon):
        """Extract horizon-appropriate entry/range/invalidation values from a card."""
        if horizon == 'SHORT_TERM':
            return dict(entry_range_low=card.get('short_entry_low'),
                        entry_range_high=card.get('short_entry_high'),
                        fair_value=None,
                        target_1=card.get('short_target_1'),
                        target_2=card.get('short_target_2'),
                        invalidation_price=card.get('short_invalidation'))
        return dict(entry_range_low=card.get('long_entry_low'),
                    entry_range_high=card.get('long_entry_high'),
                    fair_value=card.get('long_fair_value'),
                    target_1=card.get('long_target'),
                    target_2=None,
                    invalidation_price=card.get('long_invalidation'))

    def record_global_market_scan(self, scan):
        """Persist a completed market scan. Idempotent via INSERT OR IGNORE."""
        with self._connection:
            self._connection.execute("""INSERT OR IGNORE INTO global_market_scan
                (scan_id,started_at,completed_at,status,market,exchange,
                 universe_count,shortlist_count,evaluated_count,rank_eligible_count,
                 suppressed_count,engine_version,controlled,failure_reason_code,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(scan['scan_id']), scan['started_at'], scan['completed_at'], scan['status'],
                 scan.get('market'), scan.get('exchange'), int(scan['universe_count']),
                 int(scan['shortlist_count']), int(scan['evaluated_count']),
                 int(scan['rank_eligible_count']), int(scan['suppressed_count']),
                 scan['engine_version'], 1 if scan.get('controlled') else 0,
                 scan.get('failure_reason_code'),
                 scan.get('created_at', datetime.now(timezone.utc).isoformat())))

    def global_market_scans(self):
        """Read all scan records, most recent first."""
        rows = self._connection.execute(
            'SELECT * FROM global_market_scan ORDER BY completed_at DESC, scan_id DESC').fetchall()
        return [_decode_scan_row(r) for r in rows]

    def persist_global_suggestion_lifecycle(self, scan, cards):
        """Persist the complete global suggestion lifecycle after a production scan.

        Records the market scan, creates immutable original suggestions for new
        BUY/STRONG_BUY candidates, appends append-only history events with
        fingerprint-based deduplication (same fingerprint = no new event), and
        upserts the mutable current projection.

        Only successfully completed *production* (non-controlled) scans publish
        current global suggestions.
        """
        self.record_global_market_scan(scan)
        # Only production completed scans publish current global suggestions.
        if scan.get('controlled') or scan.get('status') != 'COMPLETED':
            return
        now = scan.get('completed_at', datetime.now(timezone.utc).isoformat())
        engine_version = scan.get('engine_version', 'STOCK_RULE_ENGINE_V1')
        seen = set()
        with self._connection:
            for card in cards:
                action = card.get('public_action')
                if action not in ('STRONG_BUY', 'BUY', 'PARTIAL_EXIT', 'SELL'):
                    continue
                key = (str(card['global_instrument_id']), card['horizon'])
                if key in seen:
                    continue
                seen.add(key)
                self._persist_single_suggestion(scan['scan_id'], card, now, engine_version)

    def _persist_single_suggestion(self, scan_id, card, now, engine_version):
        gid = str(card['global_instrument_id'])
        horizon = card['horizon']
        action = card['public_action']
        fingerprint = card.get('fingerprint', '')
        ranges = self._suggest_ranges(card, horizon)

        # Determine the active episode for this instrument/horizon.
        # The current projection is the authoritative pointer to the active
        # suggestion_id; history is the fallback when no current exists.
        current = self._connection.execute(
            'SELECT suggestion_id, latest_action FROM global_stock_suggestion_current '
            'WHERE global_instrument_id=? AND horizon=?',
            (gid, horizon)).fetchone()
        if current is not None:
            suggestion_id = current['suggestion_id']
            # If the current episode is closed (latest_action == SELL), a new
            # BUY/STRONG_BUY starts a fresh recommendation episode.
            if current['latest_action'] == 'SELL' and action in ('STRONG_BUY', 'BUY'):
                suggestion_id = str(uuid4())
                self._insert_global_suggestion(suggestion_id, scan_id, card, ranges, now, engine_version)
        else:
            # No current projection — check the latest history event to find
            # which suggestion_id is the most recent episode.
            latest = self._connection.execute(
                'SELECT suggestion_id, action FROM global_stock_suggestion_history '
                'WHERE global_instrument_id=? AND horizon=? '
                'ORDER BY action_at DESC, event_id DESC LIMIT 1',
                (gid, horizon)).fetchone()
            if latest is not None:
                suggestion_id = latest['suggestion_id']
                # If the last action was SELL (episode closed), a new BUY/STRONG_BUY
                # starts a fresh episode. Non-BUY actions have no open episode.
                if latest['action'] == 'SELL':
                    if action in ('STRONG_BUY', 'BUY'):
                        suggestion_id = str(uuid4())
                        self._insert_global_suggestion(suggestion_id, scan_id, card, ranges, now, engine_version)
                    else:
                        return
            else:
                # No prior history at all: only BUY/STRONG_BUY can start a lifecycle.
                if action not in ('STRONG_BUY', 'BUY'):
                    return
                suggestion_id = str(uuid4())
                self._insert_global_suggestion(suggestion_id, scan_id, card, ranges, now, engine_version)

        # Fingerprint-based deduplication: skip appending if the latest history
        # event already carries the same fingerprint (same evidence/actions/price bucket).
        last_fingerprint = self._connection.execute(
            'SELECT event_fingerprint FROM global_stock_suggestion_history '
            'WHERE suggestion_id=? ORDER BY action_at DESC, event_id DESC LIMIT 1',
            (suggestion_id,)).fetchone()
        if last_fingerprint is None or last_fingerprint[0] != fingerprint:
            self._insert_global_history_event(suggestion_id, scan_id, card, ranges, now, fingerprint, engine_version)
        self._upsert_global_current(suggestion_id, scan_id, card, ranges, now, engine_version)

    def _insert_global_suggestion(self, suggestion_id, scan_id, card, ranges, now, engine_version):
        with self._connection:
            self._connection.execute("""INSERT INTO global_stock_suggestion
                (suggestion_id,scan_id,global_instrument_id,symbol,company_name,horizon,
                 initial_action,suggested_at,suggested_price,rank,opportunity_score,
                 confidence,coverage,data_state,entry_range_low,entry_range_high,fair_value,
                 target_1,target_2,invalidation_price,reasons,risks,evidence_snapshot,
                 engine_version,recommendation_fingerprint,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (suggestion_id, str(scan_id), str(card['global_instrument_id']),
                 card.get('symbol'), card.get('company_name'), card['horizon'],
                 card['public_action'], now, card.get('price_at_recommendation'),
                 card.get('rank_position'), card.get('opportunity_score'),
                 card.get('confidence'), card.get('coverage'), card.get('data_state'),
                 ranges['entry_range_low'], ranges['entry_range_high'], ranges['fair_value'],
                 ranges['target_1'], ranges['target_2'], ranges['invalidation_price'],
                 self._suggest_json(card.get('top_positive_reasons')),
                 self._suggest_json(card.get('top_negative_reasons')),
                 self._suggest_json(card.get('evidence_snapshot')),
                 engine_version, card.get('fingerprint', ''),
                 datetime.now(timezone.utc).isoformat()))

    def _insert_global_history_event(self, suggestion_id, scan_id, card, ranges, now, fingerprint, engine_version):
        with self._connection:
            self._connection.execute("""INSERT INTO global_stock_suggestion_history
                (event_id,suggestion_id,scan_id,global_instrument_id,horizon,action,
                 action_at,action_price,rank,opportunity_score,confidence,reasons,
                 risks,evidence_snapshot,engine_version,event_fingerprint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid4()), str(suggestion_id), str(scan_id), str(card['global_instrument_id']),
                 card['horizon'], card['public_action'], now, card.get('current_price'),
                 card.get('rank_position'), card.get('opportunity_score'),
                 card.get('confidence'), self._suggest_json(card.get('top_positive_reasons')),
                 self._suggest_json(card.get('top_negative_reasons')),
                 self._suggest_json(card.get('evidence_snapshot')),
                 engine_version, fingerprint))

    def _upsert_global_current(self, suggestion_id, scan_id, card, ranges, now, engine_version):
        # Look up original suggestion values so they remain stable across
        # lifecycle transitions (e.g. BUY -> PARTIAL_EXIT -> SELL).
        orig = self._connection.execute(
            'SELECT suggested_at, suggested_price FROM global_stock_suggestion WHERE suggestion_id=?',
            (str(suggestion_id),)).fetchone()
        original_suggested_at = orig['suggested_at'] if orig else now
        original_suggested_price = orig['suggested_price'] if orig else card.get('price_at_recommendation')
        cols = ('global_instrument_id','horizon','suggestion_id','latest_action','latest_action_at',
                'latest_price','original_suggested_at','original_suggested_price','current_rank',
                'opportunity_score','confidence','coverage','entry_range_low','entry_range_high',
                'fair_value','target_1','target_2','invalidation_price','reasons','risks',
                'evidence_snapshot','engine_version','last_scan_id',
                'data_state','missing_areas','stale_areas','short_term_state','long_term_state',
                'new_investor_action','existing_holder_action','current_short_action','current_long_action')
        conflict_cols = cols[1:]  # everything after global_instrument_id,horizon (the PK)
        update_cols = cols[2:]    # everything after suggestion_id
        with self._connection:
            self._connection.execute(
                f"INSERT INTO global_stock_suggestion_current ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)}) "
                f"ON CONFLICT(global_instrument_id,horizon) DO UPDATE SET "
                + ','.join(f'{c}=excluded.{c}' for c in update_cols),
                (str(card['global_instrument_id']), card['horizon'], str(suggestion_id),
                 card['public_action'], now, card.get('current_price'),
                 original_suggested_at, original_suggested_price,
                 card.get('rank_position'), card.get('opportunity_score'),
                 card.get('confidence'), card.get('coverage'),
                 ranges['entry_range_low'], ranges['entry_range_high'], ranges['fair_value'],
                 ranges['target_1'], ranges['target_2'], ranges['invalidation_price'],
                 self._suggest_json(card.get('top_positive_reasons')),
                 self._suggest_json(card.get('top_negative_reasons')),
                 self._suggest_json(card.get('evidence_snapshot')),
                 engine_version, str(scan_id),
                 card.get('data_state'),
                 self._suggest_json(card.get('missing_areas')),
                 self._suggest_json(card.get('stale_areas')),
                 card.get('short_term_state'), card.get('long_term_state'),
                 card.get('new_investor_action'), card.get('existing_holder_action'),
                 card.get('current_short_action'), card.get('current_long_action')))

    def global_current_suggestions(self, *, horizon=None):
        """Read the mutable current projection. No user-scoped filtering.

        Joins back to global_stock_suggestion (the immutable per-suggestion
        record) for symbol/company_name: global_stock_suggestion_current
        never stored those columns itself, so the radar/current projection
        had no human-readable identity for a card at all -- only the raw
        global_instrument_id UUID. suggestion_id is a NOT NULL FK into
        global_stock_suggestion, and symbol/company_name are captured there
        for real at suggestion-creation time (global_opportunity_cycle.py's
        card construction, from the same scan snapshot used for scoring), so
        this is real persisted data, not a fabricated join.
        """
        sql = ('SELECT c.*, s.symbol AS symbol, s.company_name AS company_name '
               'FROM global_stock_suggestion_current c '
               'LEFT JOIN global_stock_suggestion s ON s.suggestion_id = c.suggestion_id')
        params = []
        if horizon is not None:
            sql += ' WHERE c.horizon = ?'
            params.append(horizon)
        sql += ' ORDER BY c.latest_action, c.horizon, c.global_instrument_id'
        rows = self._connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            result.append({
                'global_instrument_id': row['global_instrument_id'],
                'horizon': row['horizon'],
                'suggestion_id': row['suggestion_id'],
                'symbol': _row_get(row, 'symbol'),
                'company_name': _row_get(row, 'company_name'),
                'latest_action': row['latest_action'],
                'latest_action_at': row['latest_action_at'],
                'latest_price': row['latest_price'],
                'original_suggested_at': row['original_suggested_at'],
                'original_suggested_price': row['original_suggested_price'],
                'current_rank': row['current_rank'],
                'opportunity_score': row['opportunity_score'],
                'confidence': row['confidence'],
                'coverage': row['coverage'],
                'entry_range_low': row['entry_range_low'],
                'entry_range_high': row['entry_range_high'],
                'fair_value': row['fair_value'],
                'target_1': row['target_1'],
                'target_2': row['target_2'],
                'invalidation_price': row['invalidation_price'],
                'reasons': _decode_json(row['reasons']),
                'risks': _decode_json(row['risks']),
                'evidence_snapshot': _decode_json(row['evidence_snapshot']),
                'engine_version': row['engine_version'],
                'last_scan_id': row['last_scan_id'],
                'data_state': _row_get(row, 'data_state'),
                'missing_areas': _decode_json(_row_get(row, 'missing_areas')),
                'stale_areas': _decode_json(_row_get(row, 'stale_areas')),
                'short_term_state': _row_get(row, 'short_term_state'),
                'long_term_state': _row_get(row, 'long_term_state'),
                'new_investor_action': _row_get(row, 'new_investor_action'),
                'existing_holder_action': _row_get(row, 'existing_holder_action'),
                'current_short_action': _row_get(row, 'current_short_action'),
                'current_long_action': _row_get(row, 'current_long_action'),
            })
        return result

    def global_opportunity_radar(self):
        """Assemble the public Global Opportunity Radar from dedicated V14 persistence.

        Provider-free: reads only the persisted global suggestion/current/scan
        tables. No provider calls, acquisition, re-scan, or worker submission.
        Returns best_buy_today, top_short_term, top_long_term, top_exit,
        generated_at / last_processed_at, and source scan id.
        """
        current = self.global_current_suggestions()
        scans = self.global_market_scans()
        completed = [s for s in scans if s['status'] == 'COMPLETED']
        latest_scan = completed[0] if completed else None
        generated_at = latest_scan['completed_at'] if latest_scan else None

        def _card(row):
            # Real per-horizon range values, split back out to the short_*/long_*
            # fields the frontend Opportunity type requires. _suggest_ranges()
            # (the write side) is the mirror of this: it collapses whichever
            # horizon a suggestion is for into these same generic columns, so
            # only that horizon's fields are populated here -- the other side
            # is None, which is correct (OpportunityCard only ever reads the
            # side matching the card's own horizon).
            is_short = row['horizon'] == 'SHORT_TERM'
            positive_reasons = _decode_json(row['reasons']) or []
            negative_reasons = _decode_json(row['risks']) or []
            return {
                'global_instrument_id': row['global_instrument_id'],
                'horizon': row['horizon'],
                'public_action': row['latest_action'],
                'suggestion_id': row['suggestion_id'],
                'symbol': _row_get(row, 'symbol'),
                'company_name': _row_get(row, 'company_name'),
                'scan_id': row['last_scan_id'],
                'current_price': row['latest_price'],
                'price_at_recommendation': row['original_suggested_price'],
                'rank_position': row['current_rank'],
                'opportunity_score': row['opportunity_score'],
                'confidence': row['confidence'],
                'coverage': row['coverage'],
                # Aliases for the frontend Opportunity contract (same real values).
                'opportunity_confidence': row['confidence'],
                'score_coverage': row['coverage'],
                'entry_range_low': row['entry_range_low'],
                'entry_range_high': row['entry_range_high'],
                'fair_value': row['fair_value'],
                'target_1': row['target_1'],
                'target_2': row['target_2'],
                'invalidation_price': row['invalidation_price'],
                'short_entry_low': row['entry_range_low'] if is_short else None,
                'short_entry_high': row['entry_range_high'] if is_short else None,
                'short_target_1': row['target_1'] if is_short else None,
                'short_target_2': row['target_2'] if is_short else None,
                'short_invalidation': row['invalidation_price'] if is_short else None,
                'long_entry_low': row['entry_range_low'] if not is_short else None,
                'long_entry_high': row['entry_range_high'] if not is_short else None,
                'long_fair_value': row['fair_value'] if not is_short else None,
                'long_target': row['target_1'] if not is_short else None,
                'long_invalidation': row['invalidation_price'] if not is_short else None,
                'reasons': positive_reasons,
                'risks': negative_reasons,
                'top_positive_reasons': positive_reasons,
                'top_negative_reasons': negative_reasons,
                'evidence_snapshot': _decode_json(row['evidence_snapshot']),
                'engine_version': row['engine_version'],
                'latest_action_at': row['latest_action_at'],
                'data_state': _row_get(row, 'data_state'),
                'missing_areas': _decode_json(_row_get(row, 'missing_areas')) or [],
                'stale_areas': _decode_json(_row_get(row, 'stale_areas')) or [],
                # Static horizon labels: prepare_cycle hardcodes these same two
                # strings for every card (global_opportunity_cycle.py), they are
                # not per-row data.
                'short_horizon': '1 week\u20133 months',
                'long_horizon': '6\u201312 months',
                'short_term_state': _row_get(row, 'short_term_state'),
                'long_term_state': _row_get(row, 'long_term_state'),
                'new_investor_action': _row_get(row, 'new_investor_action'),
                'existing_holder_action': _row_get(row, 'existing_holder_action'),
                'current_short_action': _row_get(row, 'current_short_action'),
                'current_long_action': _row_get(row, 'current_long_action'),
            }

        cards = [_card(r) for r in current]
        buy_cards = [c for c in cards if c['public_action'] in ('STRONG_BUY', 'BUY')]
        exit_cards = [c for c in cards if c['public_action'] in ('PARTIAL_EXIT', 'SELL')]

        def buy_order(c):
            return (-(c['opportunity_score'] or 0),
                    -(c['confidence'] or 0),
                    c.get('rank_position') or 9999,
                    c['global_instrument_id'])

        def exit_urgency(c):
            action = c['public_action']
            score = c['opportunity_score'] or 0
            # SELL (thesis broken) is more urgent than PARTIAL_EXIT (target reached).
            # Within SELL: lower score = more deterioration = more urgent.
            # Within PARTIAL_EXIT: higher score = more unrealised gain = more urgent.
            if action == 'SELL':
                return (0, score, c['global_instrument_id'])
            return (1, -score, c['global_instrument_id'])

        buy_cards_sorted = sorted(buy_cards, key=buy_order)
        short_buy = [c for c in buy_cards_sorted if c['horizon'] == 'SHORT_TERM']
        long_buy = [c for c in buy_cards_sorted if c['horizon'] == 'LONG_TERM']
        exit_sorted = sorted(exit_cards, key=exit_urgency)

        best_buy_today = buy_cards_sorted[0] if buy_cards_sorted else None

        # RADAR V2 UI-1: return ALL qualifying recommendations, not just the
        # first four. The UI display count (first 4 with More/Show-less) is a
        # purely presentation concern. Count fields provide bounded metadata so
        # clients can decide whether to surface a "More" affordance without
        # guessing array lengths.
        return dict(
            best_buy_today=best_buy_today,
            top_short_term=short_buy,
            top_long_term=long_buy,
            top_exit=exit_sorted,
            top_short_term_count=len(short_buy),
            top_long_term_count=len(long_buy),
            top_exit_count=len(exit_sorted),
            previous_recommendations=self._radar_previous_recommendations(),
            generated_at=generated_at,
            last_processed_at=generated_at,
            source_scan_id=latest_scan['scan_id'] if latest_scan else None,
        )

    def _radar_previous_recommendations(self):
        """Real persisted previous-recommendation cards for the Global Opportunity
        Radar response contract (frontend Radar.previous_recommendations).

        global_opportunity_radar() itself reads the newer V14 durable suggestion
        tables (global_current_suggestions / global_market_scans), which have no
        concept of "previous recommendations" -- that field is only computed and
        persisted by prepare_cycle()/publish_opportunity_cycle() into the
        global_opportunity_top_selection payload, and already hydrated the same
        way by opportunity_current(). This mirrors that same lookup+hydrate for
        just this one field, without touching opportunity_current() or changing
        how best_buy_today/top_short_term/top_long_term/top_exit are computed.

        Returns [] whenever no completed, non-controlled cycle has been
        persisted yet, or that cycle recorded no previous recommendations --
        never a fabricated substitute for real history.
        """
        selection = next((value for row in self._connection.execute(
            'SELECT payload FROM global_opportunity_top_selection ORDER BY generated_at DESC, cycle_id DESC').fetchall()
            if not (value := decode(row)).get('controlled_candidate_set', False)
            and value.get('record_kind') != 'CYCLE_JOB'
            and value.get('status', 'COMPLETED') == 'COMPLETED'), None)
        if selection is None:
            return []
        cards = selection.get('previous_recommendations') or []
        if not cards:
            return []
        states = {s['global_instrument_id']: s for s in self.recommendation_states()}
        snapshots = {s['global_instrument_id']: s for s in self.opportunity_snapshots(selection['cycle_id'])}

        def hydrate(card):
            key = card['global_instrument_id']
            state = states.get(key, {})
            if state.get('latest_recommendation_id') != card.get('latest_recommendation_id'):
                return card
            latest_id = state.get('latest_recommendation_id')
            history = decode(self._connection.execute(
                'SELECT payload FROM stock_recommendation_history WHERE recommendation_id = ?', (latest_id,)).fetchone()) if latest_id else None
            current = snapshots.get(key, {})
            scores = {k: current[k] for k in ('opportunity_score', 'opportunity_confidence', 'score_coverage', 'rule_engine_score') if k in current}
            return {**card, **current, **(history or {}), **scores, **state}

        return [hydrate(c) for c in cards]
