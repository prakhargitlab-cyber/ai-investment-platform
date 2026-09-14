"""V13 projection storage. All cycle writes publish atomically on the shared DB.

History and snapshot methods only INSERT; projection methods alone use UPDATE.
The payload preserves complete evidence alongside queryable identity/version columns.
"""
import json

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


def decode(row):
    if row is None:
        return None
    value = row['payload']
    return json.loads(value) if isinstance(value, str) else value


class OpportunityPersistenceMixin:
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
        selection = decode(self._connection.execute(
            'SELECT payload FROM global_opportunity_top_selection ORDER BY generated_at DESC, cycle_id DESC LIMIT 1').fetchone())
        if selection is None:
            return dict(generated_at=None, best_buy_today=None, top_short_term=[], top_long_term=[], previous_recommendations=[])
        snapshots = {s['global_instrument_id']: s for s in self.opportunity_snapshots(selection['cycle_id'])}
        states = {s['global_instrument_id']: s for s in self.recommendation_states()}
        def hydrate(card):
            key = card['global_instrument_id']
            state = states.get(key, {})
            latest_id = state.get('latest_recommendation_id')
            history = decode(self._connection.execute(
                'SELECT payload FROM stock_recommendation_history WHERE recommendation_id = ?', (latest_id,)).fetchone()) if latest_id else None
            # Current public score/price come from the snapshot/projection, not an older recommendation.
            current = snapshots.get(key, {})
            scores = {k: current[k] for k in ('opportunity_score', 'opportunity_confidence', 'score_coverage', 'rule_engine_score') if k in current}
            return {**card, **current, **(history or {}), **scores, **state}
        for field in ('top_short_term', 'top_long_term', 'previous_recommendations'):
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

    def publish_opportunity_cycle(self, snapshots, recommendations, states, selection, *, build=None):
        with self._connection:
            for s in snapshots:
                self._insert_opportunity('global_opportunity_snapshot',
                    ('snapshot_id', 'cycle_id', 'global_instrument_id', 'market', 'generated_at'), s)
            if build is not None:
                persisted = self.opportunity_snapshots(selection['cycle_id'])
                recommendations, states, selection = build(persisted)
            for r in recommendations:
                self._insert_opportunity('stock_recommendation_history',
                    ('recommendation_id', 'snapshot_id', 'global_instrument_id', 'generated_at', 'recommendation_engine_version', 'fingerprint'), r)
            for s in states:
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
