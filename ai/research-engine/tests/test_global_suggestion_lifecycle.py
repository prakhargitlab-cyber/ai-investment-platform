"""Tests for the V14 dedicated global auto-suggestion persistence lifecycle.

Categories A–O cover scan recording, immutability, append-only history,
current projection semantics, fingerprint deduplication, controlled-scan
gating, schema introspection, nullability, exit events, idempotency,
current projection freshness, best_buy_today inclusion, full-cycle
integration, and history immutability triggers.

Categories P–X extend coverage to multi-episode lifecycle uniqueness
(BUY → SELL → later BUY creates two suggestion rows), the dedicated
dashboard read model (global_opportunity_radar), and provider-free
guarantees on the GET /opportunities/current endpoint.
"""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from app.persistence import SqliteResearchPersistence, DisabledResearchPersistence


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_card(n=1, *, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
              fingerprint='fp-1', **updates):
    """Build a card dict matching the shape produced by prepare_cycle."""
    base = dict(
        global_instrument_id=str(UUID(int=n)),
        horizon=horizon,
        public_action=action,
        symbol=f'C{n}',
        company_name=f'Company {n}',
        opportunity_score=80.0,
        confidence=80.0,
        coverage=90.0,
        current_price=price,
        price_at_recommendation=price,
        rank_position=1,
        short_entry_low=980.0,
        short_entry_high=1020.0,
        short_target_1=1100.0,
        short_target_2=1200.0,
        short_invalidation=800.0,
        long_entry_low=1000.0,
        long_entry_high=1050.0,
        long_fair_value=1300.0,
        long_target=1500.0,
        long_invalidation=700.0,
        fingerprint=fingerprint,
        evidence_snapshot={'key': 'value'},
        top_positive_reasons=['QUALITY'],
        top_negative_reasons=[],
        data_state='FRESH',
        rule_engine_version='STOCK_RULE_ENGINE_V1',
        recommendation_engine_version='RECOMMENDATION_ENGINE_V1',
        rank_eligible=True,
    )
    base.update(**updates)
    return base


def make_scan(**updates):
    """Build a scan dict matching the shape persisted by run_global_opportunity_cycle."""
    base = dict(
        scan_id=str(uuid4()),
        started_at='2026-09-13T10:00:00+00:00',
        completed_at='2026-09-13T10:05:00+00:00',
        status='COMPLETED',
        market='NSE',
        exchange='NSE',
        universe_count=100,
        shortlist_count=20,
        evaluated_count=5,
        rank_eligible_count=3,
        suppressed_count=2,
        engine_version='STOCK_RULE_ENGINE_V1',
        controlled=False,
        failure_reason_code=None,
        created_at='2026-09-13T10:05:00+00:00',
    )
    base.update(**updates)
    return base


def fresh_store():
    return SqliteResearchPersistence(':memory:')


# ---------------------------------------------------------------------------
# A: record_global_market_scan persists counts from ranking
# ---------------------------------------------------------------------------
def test_A_record_global_market_scan_persists_counts():
    store = fresh_store()
    scan = make_scan(universe_count=500, shortlist_count=50, evaluated_count=10,
                     rank_eligible_count=8, suppressed_count=7)
    store.record_global_market_scan(scan)
    scans = store.global_market_scans()
    assert len(scans) == 1
    s = scans[0]
    assert s['scan_id'] == scan['scan_id']
    assert s['universe_count'] == 500
    assert s['shortlist_count'] == 50
    assert s['evaluated_count'] == 10
    assert s['rank_eligible_count'] == 8
    assert s['suppressed_count'] == 7
    assert s['status'] == 'COMPLETED'
    assert s['market'] == 'NSE'
    assert s['engine_version'] == 'STOCK_RULE_ENGINE_V1'
    assert s['controlled'] is False


# ---------------------------------------------------------------------------
# B: Original suggestion is immutable (UPDATE/DELETE ABORT); initial_action preserved
# ---------------------------------------------------------------------------
def test_B_global_suggestion_is_immutable():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM')
    store.persist_global_suggestion_lifecycle(scan, [card])
    suggestions = store._connection.execute(
        'SELECT suggestion_id, initial_action FROM global_stock_suggestion').fetchone()
    suggestion_id = suggestions['suggestion_id']
    assert suggestions['initial_action'] == 'STRONG_BUY'
    # UPDATE should ABORT
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute(
                'UPDATE global_stock_suggestion SET rank = 0 WHERE suggestion_id = ?',
                (suggestion_id,))
    # DELETE should ABORT
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute(
                'DELETE FROM global_stock_suggestion WHERE suggestion_id = ?',
                (suggestion_id,))


# ---------------------------------------------------------------------------
# C: History is append-only (BUY → PARTIAL_EXIT → SELL as 3 separate events)
# ---------------------------------------------------------------------------
def test_C_history_is_append_only_multi_event():
    store = fresh_store()
    scan = make_scan()
    gid = str(UUID(int=1))
    buy_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0, fingerprint='fp-buy')
    exit_card = make_card(1, action='PARTIAL_EXIT', horizon='SHORT_TERM', price=1200.0, fingerprint='fp-exit')
    sell_card = make_card(1, action='SELL', horizon='SHORT_TERM', price=800.0, fingerprint='fp-sell')
    store.persist_global_suggestion_lifecycle(scan, [buy_card])
    store.persist_global_suggestion_lifecycle(scan, [exit_card])
    store.persist_global_suggestion_lifecycle(scan, [sell_card])
    history = store._connection.execute(
        'SELECT action, action_price FROM global_stock_suggestion_history '
        'WHERE global_instrument_id = ? ORDER BY action_at',
        (gid,)).fetchall()
    assert len(history) == 3
    assert [h['action'] for h in history] == ['STRONG_BUY', 'PARTIAL_EXIT', 'SELL']
    assert [h['action_price'] for h in history] == [1000.0, 1200.0, 800.0]


# ---------------------------------------------------------------------------
# D: global_stock_suggestion_current has exactly one row per (instrument, horizon)
# ---------------------------------------------------------------------------
def test_D_current_one_row_per_instrument_horizon():
    store = fresh_store()
    scan = make_scan()
    # Two instruments, two horizons each
    cards = [
        make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', fingerprint='fp-1s'),
        make_card(1, action='STRONG_BUY', horizon='LONG_TERM', fingerprint='fp-1L'),
        make_card(2, action='BUY', horizon='SHORT_TERM', fingerprint='fp-2s'),
        make_card(2, action='BUY', horizon='LONG_TERM', fingerprint='fp-2L'),
    ]
    store.persist_global_suggestion_lifecycle(scan, cards)
    count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_current').fetchone()[0]
    assert count == 4
    # Re-run with same cards (idempotent dedup) — still 4
    store.persist_global_suggestion_lifecycle(scan, cards)
    count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_current').fetchone()[0]
    assert count == 4


# ---------------------------------------------------------------------------
# E: Fingerprint-based dedup — same fingerprint does NOT create a new history event
# ---------------------------------------------------------------------------
def test_E_fingerprint_dedup_no_duplicate_history():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0, fingerprint='fp-same')
    store.persist_global_suggestion_lifecycle(scan, [card])
    store.persist_global_suggestion_lifecycle(scan, [card])  # same fingerprint
    store.persist_global_suggestion_lifecycle(scan, [card])  # same fingerprint
    history = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_history WHERE suggestion_id = (SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id = ? AND horizon = ?)',
        (str(UUID(int=1)), 'SHORT_TERM')).fetchone()[0]
    assert history == 1  # only one event despite 3 cycles


# ---------------------------------------------------------------------------
# F: Controlled (non-production) scan records metadata but does NOT publish current
# ---------------------------------------------------------------------------
def test_F_controlled_scan_records_but_does_not_publish():
    store = fresh_store()
    # Controlled scan: records scan metadata, but skips suggestion writes
    controlled_scan = make_scan(controlled=True)
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM')
    store.persist_global_suggestion_lifecycle(controlled_scan, [card])
    scans = store.global_market_scans()
    assert len(scans) == 1
    assert scans[0]['controlled'] is True
    # No suggestions or current rows should exist
    assert store._connection.execute('SELECT COUNT(*) FROM global_stock_suggestion').fetchone()[0] == 0
    assert store._connection.execute('SELECT COUNT(*) FROM global_stock_suggestion_current').fetchone()[0] == 0


def test_F_failed_scan_records_but_does_not_publish():
    store = fresh_store()
    failed_scan = make_scan(status='FAILED', failure_reason_code='SCAN_ERROR')
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM')
    store.persist_global_suggestion_lifecycle(failed_scan, [card])
    scans = store.global_market_scans()
    assert len(scans) == 1
    assert scans[0]['status'] == 'FAILED'
    assert scans[0]['failure_reason_code'] == 'SCAN_ERROR'
    assert store._connection.execute('SELECT COUNT(*) FROM global_stock_suggestion').fetchone()[0] == 0


# ---------------------------------------------------------------------------
# G: DisabledResearchPersistence stubs return safe defaults
# ---------------------------------------------------------------------------
def test_G_disabled_persistence_stubs():
    disabled = DisabledResearchPersistence()
    assert disabled.global_market_scans() == []
    assert disabled.global_current_suggestions() == []
    assert disabled.persist_global_suggestion_lifecycle({}, []) is None
    assert disabled.record_global_market_scan({}) is None


# ---------------------------------------------------------------------------
# H: No user_id column in any of the 4 global suggestion tables
# ---------------------------------------------------------------------------
def test_H_no_user_id_column_in_any_table():
    store = fresh_store()
    for table in ('global_market_scan', 'global_stock_suggestion',
                  'global_stock_suggestion_history', 'global_stock_suggestion_current'):
        cols = [r[1] for r in store._connection.execute(f'PRAGMA table_info({table})').fetchall()]
        assert 'user_id' not in cols, f'{table} must not have user_id column'


# ---------------------------------------------------------------------------
# I: Nullable entry/fair-value/target/invalidation fields stored as NULL
# ---------------------------------------------------------------------------
def test_I_nullable_fields_stored_as_null():
    store = fresh_store()
    scan = make_scan()
    # Card with no range fields (short/invalidation keys missing)
    card = dict(
        global_instrument_id=str(UUID(int=1)),
        horizon='SHORT_TERM',
        public_action='STRONG_BUY',
        symbol='C1',
        company_name='Company 1',
        opportunity_score=80.0,
        confidence=80.0,
        coverage=90.0,
        current_price=1000.0,
        price_at_recommendation=1000.0,
        rank_position=1,
        fingerprint='fp-null',
        evidence_snapshot={},
        top_positive_reasons=[],
        top_negative_reasons=[],
        data_state='FRESH',
        rule_engine_version='STOCK_RULE_ENGINE_V1',
    )
    store.persist_global_suggestion_lifecycle(scan, [card])
    row = store._connection.execute(
        'SELECT entry_range_low, entry_range_high, fair_value, target_1, target_2, invalidation_price '
        'FROM global_stock_suggestion WHERE global_instrument_id = ? AND horizon = ?',
        (str(UUID(int=1)), 'SHORT_TERM')).fetchone()
    assert row['entry_range_low'] is None
    assert row['entry_range_high'] is None
    assert row['fair_value'] is None
    assert row['target_1'] is None
    assert row['target_2'] is None
    assert row['invalidation_price'] is None


# ---------------------------------------------------------------------------
# J: top_exit cards (PARTIAL_EXIT/SELL) generate history events with correct actions
# ---------------------------------------------------------------------------
def test_J_top_exit_cards_generate_history_events():
    store = fresh_store()
    scan = make_scan()
    buy_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0, fingerprint='fp-buy')
    partial_exit_card = make_card(1, action='PARTIAL_EXIT', horizon='SHORT_TERM', price=1200.0, fingerprint='fp-exit')
    sell_card = make_card(1, action='SELL', horizon='SHORT_TERM', price=800.0, fingerprint='fp-sell')
    store.persist_global_suggestion_lifecycle(scan, [buy_card])
    store.persist_global_suggestion_lifecycle(scan, [partial_exit_card])
    store.persist_global_suggestion_lifecycle(scan, [sell_card])
    actions = store._connection.execute(
        'SELECT action FROM global_stock_suggestion_history '
        'WHERE global_instrument_id = ? ORDER BY action_at',
        (str(UUID(int=1)),)).fetchall()
    assert [a['action'] for a in actions] == ['STRONG_BUY', 'PARTIAL_EXIT', 'SELL']
    # Current projection should reflect the latest action
    current = store._connection.execute(
        'SELECT latest_action FROM global_stock_suggestion_current WHERE global_instrument_id = ?',
        (str(UUID(int=1)),)).fetchone()
    assert current['latest_action'] == 'SELL'


# ---------------------------------------------------------------------------
# K: Idempotency — re-running with same scan_id + fingerprints produces no duplicates
# ---------------------------------------------------------------------------
def test_K_idempotent_rerun_no_duplicate_history():
    store = fresh_store()
    scan = make_scan()
    buy_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0, fingerprint='fp-b')
    buy_card_2 = make_card(2, action='BUY', horizon='LONG_TERM', price=500.0, fingerprint='fp-2')
    cards = [buy_card, buy_card_2]
    store.persist_global_suggestion_lifecycle(scan, cards)
    count1 = store._connection.execute('SELECT COUNT(*) FROM global_stock_suggestion_history').fetchone()[0]
    # Re-run with the same scan and same fingerprints
    store.persist_global_suggestion_lifecycle(scan, cards)
    count2 = store._connection.execute('SELECT COUNT(*) FROM global_stock_suggestion_history').fetchone()[0]
    assert count2 == count1  # no new history events


# ---------------------------------------------------------------------------
# L: Current projection reflects latest action, latest price, and latest rank
# ---------------------------------------------------------------------------
def test_L_current_reflects_latest_action_price_rank():
    store = fresh_store()
    scan = make_scan()
    buy_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                         fingerprint='fp-buy', rank_position=1)
    store.persist_global_suggestion_lifecycle(scan, [buy_card])
    # New cycle: partial exit at higher price, higher rank
    exit_card = make_card(1, action='PARTIAL_EXIT', horizon='SHORT_TERM', price=1250.0,
                          fingerprint='fp-exit', rank_position=3)
    new_scan = make_scan(scan_id=str(uuid4()))
    store.persist_global_suggestion_lifecycle(new_scan, [exit_card])
    current = store._connection.execute(
        'SELECT latest_action, latest_price, current_rank, suggestion_id FROM global_stock_suggestion_current WHERE global_instrument_id = ?',
        (str(UUID(int=1)),)).fetchone()
    assert current['latest_action'] == 'PARTIAL_EXIT'
    assert current['latest_price'] == 1250.0
    assert current['current_rank'] == 3
    # Original suggestion still exists and is immutable
    suggestion = store._connection.execute(
        'SELECT initial_action FROM global_stock_suggestion WHERE suggestion_id = ?',
        (current['suggestion_id'],)).fetchone()
    assert suggestion['initial_action'] == 'STRONG_BUY'


# ---------------------------------------------------------------------------
# M: best_buy_today card is included in the cards list
# ---------------------------------------------------------------------------
def test_M_best_buy_today_included_in_cards():
    store = fresh_store()
    scan = make_scan()
    # Simulate selection with best_buy_today
    bb_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0, fingerprint='fp-bb')
    long_card = make_card(2, action='BUY', horizon='LONG_TERM', price=500.0, fingerprint='fp-long')
    selection = {
        'top_short_term': [bb_card],
        'top_long_term': [long_card],
        'top_exit': [],
        'best_buy_today': bb_card,
    }
    cards = list(selection.get('top_short_term', [])) + list(selection.get('top_long_term', [])) + \
        list(selection.get('top_exit', []))
    if selection.get('best_buy_today'):
        cards.append(selection['best_buy_today'])
    store.persist_global_suggestion_lifecycle(scan, cards)
    current = store.global_current_suggestions()
    suggestion_ids = {c['suggestion_id'] for c in current}
    # bb_card should have a suggestion in current
    assert len(current) >= 1  # at least one from top_short_term, bb might duplicate
    # Verify no errors, and both instruments present
    current_gids = {c['global_instrument_id'] for c in current}
    assert str(UUID(int=1)) in current_gids
    assert str(UUID(int=2)) in current_gids


# ---------------------------------------------------------------------------
# O: History immutability triggers fire on UPDATE/DELETE of history table
# ---------------------------------------------------------------------------
def test_O_history_immutability_triggers():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', fingerprint='fp-o')
    store.persist_global_suggestion_lifecycle(scan, [card])
    # UPDATE should ABORT
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('UPDATE global_stock_suggestion_history SET action = action')
    # DELETE should ABORT
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('DELETE FROM global_stock_suggestion_history')

    # Also verify scan and suggestion tables are immutable
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('UPDATE global_market_scan SET status = status')
    with pytest.raises(Exception, match='SUGGESTION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('DELETE FROM global_market_scan')


# ---------------------------------------------------------------------------
# N: Full integration via run_global_opportunity_cycle (mocked)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_N_full_cycle_integration(monkeypatch):
    from app import global_opportunity_cycle as cycle
    from app.persistence import SqliteResearchPersistence
    from test_global_scanner import instrument, persisted, NOW
    from test_global_opportunity_orchestration import setup
    from datetime import timedelta
    from uuid import UUID

    service, rows, pairs, store = setup(monkeypatch, 3)

    class Clock:
        @staticmethod
        def now(*a):
            return NOW

    monkeypatch.setattr(cycle, 'datetime', Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator', lambda *a, **k: service)

    from unittest.mock import AsyncMock
    source = AsyncMock()
    source.active_global_equities.return_value = rows
    source.sector_benchmark_contexts.return_value = {}

    result = await cycle.run_global_opportunity_cycle(
        service.repository, source, candidate_ids=None, top_n=2
    )
    # Scan was recorded
    scans = store.global_market_scans()
    assert len(scans) == 1
    assert scans[0]['status'] == 'COMPLETED'
    assert scans[0]['controlled'] is False
    assert scans[0]['universe_count'] == 3
    # Current suggestions were published
    current = store.global_current_suggestions()
    assert len(current) >= 1
    # At least one current suggestion should exist
    actions = {c['latest_action'] for c in current}
    assert actions.issubset({'STRONG_BUY', 'BUY', 'PARTIAL_EXIT', 'SELL'})
    # Best buy today symbol should be in current suggestions
    if result.get('best_buy_today'):
        bb_gid = result['best_buy_today']['global_instrument_id']
        current_gids = {c['global_instrument_id'] for c in current}
        assert bb_gid in current_gids


# ---------------------------------------------------------------------------
# P: Episode uniqueness — BUY → SELL → later BUY creates TWO suggestion rows
# ---------------------------------------------------------------------------
def test_P_sell_then_later_buy_creates_new_suggestion_episode():
    store = fresh_store()
    gid = str(UUID(int=1))
    horizon = 'SHORT_TERM'

    # Episode 1: BUY
    buy_card = make_card(1, action='STRONG_BUY', horizon=horizon, price=1000.0,
                         fingerprint='fp-buy-1')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [buy_card])

    suggestion_ids_1 = [r[0] for r in store._connection.execute(
        'SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchall()]
    assert len(suggestion_ids_1) == 1

    # Partial exit, then SELL — same episode
    exit_card = make_card(1, action='PARTIAL_EXIT', horizon=horizon, price=1200.0,
                          fingerprint='fp-exit-1')
    sell_card = make_card(1, action='SELL', horizon=horizon, price=800.0,
                          fingerprint='fp-sell-1')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [exit_card])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell_card])

    # Episode 2: new BUY months later
    new_buy = make_card(1, action='STRONG_BUY', horizon=horizon, price=950.0,
                        fingerprint='fp-buy-2')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [new_buy])

    suggestion_ids_2 = [r[0] for r in store._connection.execute(
        'SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchall()]
    assert len(suggestion_ids_2) == 2
    assert suggestion_ids_2[0] != suggestion_ids_2[1]
    # First episode's initial_action is STRONG_BUY
    first = store._connection.execute(
        'SELECT initial_action FROM global_stock_suggestion WHERE suggestion_id=?',
        (suggestion_ids_1[0],)).fetchone()
    assert first['initial_action'] == 'STRONG_BUY'


# ---------------------------------------------------------------------------
# Q: Both lifecycle histories remain intact after a new episode
# ---------------------------------------------------------------------------
def test_Q_both_lifecycle_histories_remain_intact():
    store = fresh_store()
    gid = str(UUID(int=1))
    horizon = 'SHORT_TERM'

    buy_card = make_card(1, action='STRONG_BUY', horizon=horizon, price=1000.0,
                         fingerprint='fp-buy-1')
    sell_card = make_card(1, action='SELL', horizon=horizon, price=800.0,
                          fingerprint='fp-sell-1')
    new_buy = make_card(1, action='STRONG_BUY', horizon=horizon, price=950.0,
                        fingerprint='fp-buy-2')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [buy_card])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell_card])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [new_buy])

    # Episode 1 history: STRONG_BUY → SELL
    suggestions = [r[0] for r in store._connection.execute(
        'SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchall()]
    assert len(suggestions) == 2

    ep1_history = store._connection.execute(
        'SELECT action FROM global_stock_suggestion_history WHERE suggestion_id=? ORDER BY action_at',
        (suggestions[0],)).fetchall()
    assert [h['action'] for h in ep1_history] == ['STRONG_BUY', 'SELL']

    ep2_history = store._connection.execute(
        'SELECT action FROM global_stock_suggestion_history WHERE suggestion_id=? ORDER BY action_at',
        (suggestions[1],)).fetchall()
    assert [h['action'] for h in ep2_history] == ['STRONG_BUY']


# ---------------------------------------------------------------------------
# R: Current projection points only to the newest active episode
# ---------------------------------------------------------------------------
def test_R_current_points_to_newest_active_episode():
    store = fresh_store()
    gid = str(UUID(int=1))
    horizon = 'SHORT_TERM'

    buy_card = make_card(1, action='STRONG_BUY', horizon=horizon, price=1000.0,
                         fingerprint='fp-buy-1')
    sell_card = make_card(1, action='SELL', horizon=horizon, price=800.0,
                          fingerprint='fp-sell-1')
    new_buy = make_card(1, action='STRONG_BUY', horizon=horizon, price=950.0,
                        fingerprint='fp-buy-2')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [buy_card])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell_card])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [new_buy])

    current = store._connection.execute(
        'SELECT suggestion_id, latest_action FROM global_stock_suggestion_current '
        'WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()
    # There should be two suggestion rows for this instrument/horizon
    suggestion_ids = [r[0] for r in store._connection.execute(
        'SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchall()]
    assert len(suggestion_ids) == 2
    # Current should point to the NEW episode's suggestion_id (not the first one)
    assert current['suggestion_id'] != suggestion_ids[0]
    assert current['latest_action'] == 'STRONG_BUY'
    # The new episode's suggestion_id has initial_action STRONG_BUY
    new_suggestion = store._connection.execute(
        'SELECT initial_action FROM global_stock_suggestion WHERE suggestion_id=?',
        (current['suggestion_id'],)).fetchone()
    assert new_suggestion['initial_action'] == 'STRONG_BUY'
    # Exactly one current row per instrument/horizon
    count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_current WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# S: Replay of the same BUY does NOT create a second episode
# ---------------------------------------------------------------------------
def test_S_replay_same_buy_does_not_create_second_episode():
    store = fresh_store()
    gid = str(UUID(int=1))
    horizon = 'SHORT_TERM'

    buy_card = make_card(1, action='STRONG_BUY', horizon=horizon, price=1000.0,
                         fingerprint='fp-buy-same')
    scan1 = make_scan(scan_id=str(uuid4()))
    store.persist_global_suggestion_lifecycle(scan1, [buy_card])
    store.persist_global_suggestion_lifecycle(scan1, [buy_card])  # replay same scan + fingerprint
    store.persist_global_suggestion_lifecycle(scan1, [buy_card])  # replay again

    suggestions = store._connection.execute(
        'SELECT suggestion_id FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchall()
    assert len(suggestions) == 1  # only one original suggestion

    history_count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_history WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()[0]
    assert history_count == 1  # fingerprint dedup — only one event


# ---------------------------------------------------------------------------
# T: Dashboard GET reads dedicated global suggestion persistence
# ---------------------------------------------------------------------------
def test_T_dashboard_reads_dedicated_global_persistence():
    store = fresh_store()
    scan = make_scan()
    # Populate dedicated V14 global suggestion tables
    short_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                           opportunity_score=85.0, fingerprint='fp-T-short')
    long_card = make_card(2, action='BUY', horizon='LONG_TERM', price=500.0,
                          opportunity_score=75.0, fingerprint='fp-T-long')
    # Instrument 3: BUY then SELL (SELL can't start an episode)
    buy3 = make_card(3, action='STRONG_BUY', horizon='SHORT_TERM', price=300.0,
                     opportunity_score=30.0, fingerprint='fp-T-buy3')
    sell3 = make_card(3, action='SELL', horizon='SHORT_TERM', price=350.0,
                      opportunity_score=50.0, fingerprint='fp-T-sell3')
    store.persist_global_suggestion_lifecycle(scan, [short_card, long_card, buy3])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell3])

    radar = store.global_opportunity_radar()
    # Must return the dedicated read model structure
    assert 'best_buy_today' in radar
    assert 'top_short_term' in radar
    assert 'top_long_term' in radar
    assert 'top_exit' in radar
    assert 'generated_at' in radar
    assert 'last_processed_at' in radar
    assert 'source_scan_id' in radar

    # best_buy_today is the strongest buy (short has score 85 > long 75)
    assert radar['best_buy_today'] is not None
    assert radar['best_buy_today']['public_action'] == 'STRONG_BUY'
    assert radar['best_buy_today']['global_instrument_id'] == str(UUID(int=1))

    # top_short_term has the short buy (instrument 3 was sold, not a buy)
    assert len(radar['top_short_term']) == 1
    assert radar['top_short_term'][0]['global_instrument_id'] == str(UUID(int=1))
    assert radar['top_short_term'][0]['public_action'] == 'STRONG_BUY'

    # top_long_term has the long buy
    assert len(radar['top_long_term']) == 1
    assert radar['top_long_term'][0]['global_instrument_id'] == str(UUID(int=2))
    assert radar['top_long_term'][0]['public_action'] == 'BUY'

    # top_exit has the sell
    assert len(radar['top_exit']) == 1
    assert radar['top_exit'][0]['global_instrument_id'] == str(UUID(int=3))
    assert radar['top_exit'][0]['public_action'] == 'SELL'

    # source_scan_id matches the latest scan
    assert radar['generated_at'] is not None
    assert radar['source_scan_id'] is not None


# ---------------------------------------------------------------------------
# U: Deleting/altering old top_selection payload does not remove dedicated suggestions
# ---------------------------------------------------------------------------
def test_U_deleting_top_selection_does_not_remove_dedicated_suggestions():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                     fingerprint='fp-U')

    # Populate V13 top_selection (old path) — the legacy payload
    from test_recommendation_lifecycle import snapshot, publish
    snap = snapshot(1)
    publish(store, [snap])

    # Populate V14 dedicated global suggestions
    store.persist_global_suggestion_lifecycle(scan, [card])

    # Verify old top_selection has data
    top_sel_count = store._connection.execute('SELECT COUNT(*) FROM global_opportunity_top_selection').fetchone()[0]
    assert top_sel_count > 0

    # Delete ALL old top_selection rows
    # (global_opportunity_top_selection has immutability triggers, so drop them first)
    store._connection.execute('DROP TRIGGER IF EXISTS immutable_global_opportunity_top_selection_update')
    store._connection.execute('DROP TRIGGER IF EXISTS immutable_global_opportunity_top_selection_delete')
    store._connection.execute('DELETE FROM global_opportunity_top_selection')
    store._connection.commit()
    top_sel_after = store._connection.execute(
        'SELECT COUNT(*) FROM global_opportunity_top_selection').fetchone()[0]
    assert top_sel_after == 0

    # Dedicated global suggestions must still be intact
    suggestion_count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion').fetchone()[0]
    assert suggestion_count == 1

    current_count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion_current').fetchone()[0]
    assert current_count == 1

    # Radar must still return the dedicated suggestion
    radar = store.global_opportunity_radar()
    assert radar['best_buy_today'] is not None
    assert len(radar['top_short_term']) == 1
    assert radar['top_short_term'][0]['global_instrument_id'] == str(UUID(int=1))


# ---------------------------------------------------------------------------
# V: Two different users receive the identical global projection
# ---------------------------------------------------------------------------
def test_V_two_users_receive_identical_projection():
    store = fresh_store()
    scan = make_scan()
    # Need a BUY before SELL (SELL can't start an episode)
    buy3 = make_card(3, action='STRONG_BUY', horizon='SHORT_TERM', price=300.0,
                     opportunity_score=30.0, fingerprint='fp-V-buy3')
    sell3 = make_card(3, action='SELL', horizon='SHORT_TERM', price=350.0,
                      opportunity_score=40.0, fingerprint='fp-V-sell')
    cards = [
        make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                  opportunity_score=90.0, fingerprint='fp-V-a'),
        make_card(2, action='BUY', horizon='SHORT_TERM', price=500.0,
                  opportunity_score=80.0, fingerprint='fp-V-b'),
        buy3,
        make_card(4, action='STRONG_BUY', horizon='LONG_TERM', price=200.0,
                  opportunity_score=85.0, fingerprint='fp-V-d'),
    ]
    store.persist_global_suggestion_lifecycle(scan, cards)
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell3])

    # Call the read model twice — both must produce identical output
    radar_1 = store.global_opportunity_radar()
    radar_2 = store.global_opportunity_radar()

    assert radar_1 == radar_2, 'Global projection must be identical across reads'

    # Verify BUY ordering: strongest first (score 90 > 80)
    short_buys = radar_1['top_short_term']
    assert len(short_buys) == 2  # instrument 3 was sold, not a buy
    assert short_buys[0]['opportunity_score'] >= short_buys[1]['opportunity_score']

    # Verify best_buy_today is the strongest across all horizons
    assert radar_1['best_buy_today']['opportunity_score'] == 90.0

    # Verify top_exit ordering: SELL before PARTIAL_EXIT
    exits = radar_1['top_exit']
    assert len(exits) == 1
    assert exits[0]['public_action'] == 'SELL'


# ---------------------------------------------------------------------------
# W: GET performs zero provider/acquisition/recompute calls
# ---------------------------------------------------------------------------
def test_W_dashboard_radar_performs_zero_provider_calls():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                     fingerprint='fp-W')
    store.persist_global_suggestion_lifecycle(scan, [card])

    # Spy on SQL statements via sqlite3's trace callback — captures all
    # SQL executed against the connection, including writes and provider tables.
    executed_sql = []
    store._connection.set_trace_callback(lambda stmt: executed_sql.append(str(stmt)[:120]))

    # Call the read model — it must only read, never acquire or recompute
    radar = store.global_opportunity_radar()
    store._connection.set_trace_callback(None)  # stop tracing

    # No INSERT/UPDATE/DELETE/MERGE/CREATE — only SELECT
    writes = [s for s in executed_sql if any(
        kw in s.upper() for kw in ('INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE'))]
    assert writes == [], f'global_opportunity_radar must not write: {writes}'

    # No provider-related tables referenced (yahoo, mcp, http, api, acquire, ensure, fetch)
    provider_tables = [s for s in executed_sql if any(
        kw in s.upper() for kw in ('YAHOO', 'MCP', 'ACQUIRE', 'ENSURE', 'FETCH', 'HTTP'))]
    assert provider_tables == [], f'global_opportunity_radar must not call providers: {provider_tables}'

    # Only reads from dedicated global suggestion tables
    dedicated_tables = [s for s in executed_sql if 'global_market_scan' in s.lower() or 'global_stock_suggestion_current' in s.lower()]
    assert len(dedicated_tables) >= 2, f'Should read from dedicated tables, got {executed_sql}'

    # Radar returned valid data
    assert radar['source_scan_id'] == scan['scan_id']
    assert radar['best_buy_today'] is not None


# ---------------------------------------------------------------------------
# X: Closed SELL lifecycle followed by later BUY creates a new suggestion episode
# ---------------------------------------------------------------------------
def test_X_closed_sell_then_buy_creates_new_episode():
    store = fresh_store()
    gid = str(UUID(int=1))
    horizon = 'SHORT_TERM'

    # First episode: BUY → SELL
    buy1 = make_card(1, action='STRONG_BUY', horizon=horizon, price=1000.0,
                     fingerprint='fp-X-buy1')
    sell1 = make_card(1, action='SELL', horizon=horizon, price=800.0,
                      fingerprint='fp-X-sell1')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [buy1])
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [sell1])

    # Current is SELL (closed)
    cur = store._connection.execute(
        'SELECT latest_action FROM global_stock_suggestion_current WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()
    assert cur['latest_action'] == 'SELL'

    # Months later: new BUY — must create a new suggestion_id
    buy2 = make_card(1, action='STRONG_BUY', horizon=horizon, price=950.0,
                     fingerprint='fp-X-buy2')
    store.persist_global_suggestion_lifecycle(make_scan(scan_id=str(uuid4())), [buy2])

    # Two original suggestion rows
    count = store._connection.execute(
        'SELECT COUNT(*) FROM global_stock_suggestion WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()[0]
    assert count == 2

    # Current points to the new episode
    cur2 = store._connection.execute(
        'SELECT suggestion_id, latest_action FROM global_stock_suggestion_current WHERE global_instrument_id=? AND horizon=?',
        (gid, horizon)).fetchone()
    assert cur2['latest_action'] == 'STRONG_BUY'

    # The new episode's history starts with STRONG_BUY
    new_history = store._connection.execute(
        'SELECT action FROM global_stock_suggestion_history WHERE suggestion_id=? ORDER BY action_at',
        (cur2['suggestion_id'],)).fetchall()
    assert [h['action'] for h in new_history] == ['STRONG_BUY']


# ---------------------------------------------------------------------------
# Category Y: V14 JSON column normalization (SQLite TEXT vs PostgreSQL decoded)
#
# PostgreSQL JSON/JSONB columns materialize as native Python list/dict from the
# DB driver, while SQLite stores TEXT. global_current_suggestions and the radar
# projection must decode TEXT and pass through already-decoded values without
# re-parsing. Regression for the live 500 (json.loads(list) -> TypeError).
# ---------------------------------------------------------------------------
def test_Y_decode_json_helper_normalizes_both_representations():
    from app.opportunity_persistence import _decode_json

    assert _decode_json(None) is None
    # SQLite / text representation
    assert _decode_json('["a", "b"]') == ['a', 'b']
    assert _decode_json('{"k": 1}') == {'k': 1}
    assert _decode_json(b'["a"]') == ['a']
    assert _decode_json(bytearray(b'[true]')) == [True]
    # PostgreSQL already-decoded representation (passed through, never re-parsed)
    assert _decode_json(['a', 'b']) == ['a', 'b']
    assert _decode_json({'k': 1}) == {'k': 1}
    # scalars pass through
    assert _decode_json(42) == 42
    assert _decode_json(3.14) == 3.14
    assert _decode_json(True) is True


def test_Y_current_suggestions_decodes_sqlite_text_json():
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, top_positive_reasons=['QUALITY', 'VALUE'])
    store.persist_global_suggestion_lifecycle(scan, [card])

    rows = store.global_current_suggestions()
    assert len(rows) == 1
    row = rows[0]
    # SQLite stores reasons/evidence_snapshot as JSON TEXT; reads must decode to objects
    assert row['reasons'] == ['QUALITY', 'VALUE']
    assert isinstance(row['reasons'], list)
    assert row['risks'] == []
    assert row['evidence_snapshot'] == {'key': 'value'}
    assert isinstance(row['evidence_snapshot'], dict)


def test_Y_current_suggestions_handles_postgresql_decoded_json(monkeypatch):
    # Simulate a PostgreSQL row: JSON columns arrive as native list/dict (not TEXT),
    # which previously crashed global_current_suggestions with TypeError.
    store = fresh_store()
    now = '2026-09-13T10:05:00+00:00'
    pg_row = dict(
        global_instrument_id=str(UUID(int=1)), horizon='SHORT_TERM', suggestion_id=str(uuid4()),
        latest_action='STRONG_BUY', latest_action_at=now, latest_price=1000.0,
        original_suggested_at=now, original_suggested_price=1000.0, current_rank=1,
        opportunity_score=80.0, confidence=80.0, coverage=90.0,
        entry_range_low=980.0, entry_range_high=1020.0, fair_value=1300.0,
        target_1=1100.0, target_2=1200.0, invalidation_price=800.0,
        reasons=['QUALITY', 'VALUE'],          # PG native list
        risks={'volatility': 'high'},          # PG native dict
        evidence_snapshot={'price': 1000.0},   # PG native dict
        engine_version='STOCK_RULE_ENGINE_V1', last_scan_id=str(uuid4()),
        # V15 widened columns (real-data radar fields) -- must be present on
        # any row global_current_suggestions() projects, PostgreSQL included.
        data_state=None, missing_areas=[], stale_areas=[],
        short_term_state=None, long_term_state=None,
        new_investor_action=None, existing_holder_action=None,
        current_short_action=None, current_long_action=None,
    )

    class _Result:
        def fetchall(self):
            return [pg_row]

    class _FakeConn:
        def execute(self, *a, **k):
            return _Result()

    # Simulate a PostgreSQL driver returning native list/dict for JSON columns.
    monkeypatch.setattr(store, '_connection', _FakeConn())
    rows = store.global_current_suggestions()  # previously raised TypeError
    assert len(rows) == 1
    row = rows[0]
    assert row['reasons'] == ['QUALITY', 'VALUE']
    assert row['risks'] == {'volatility': 'high'}
    assert row['evidence_snapshot'] == {'price': 1000.0}


def test_Y_radar_projection_handles_postgresql_decoded_json(monkeypatch):
    store = fresh_store()
    scan = make_scan()
    store.record_global_market_scan(scan)
    now = '2026-09-13T10:05:00+00:00'
    pg_card = dict(
        global_instrument_id=str(UUID(int=1)), horizon='SHORT_TERM', suggestion_id=str(uuid4()),
        latest_action='STRONG_BUY', latest_action_at=now, latest_price=1000.0,
        original_suggested_at=now, original_suggested_price=1000.0, current_rank=1,
        opportunity_score=80.0, confidence=80.0, coverage=90.0,
        entry_range_low=980.0, entry_range_high=1020.0, fair_value=1300.0,
        target_1=1100.0, target_2=1200.0, invalidation_price=800.0,
        reasons=['QUALITY'],                   # PG native list
        risks={'volatility': 'high'},          # PG native dict
        evidence_snapshot={'price': 1000.0},   # PG native dict
        engine_version='STOCK_RULE_ENGINE_V1', last_scan_id=str(uuid4()),
        # V15 widened columns (real-data radar fields) -- _card() indexes
        # these unconditionally, so any row reaching it (PostgreSQL included)
        # must carry them.
        data_state=None, missing_areas=[], stale_areas=[],
        short_term_state=None, long_term_state=None,
        new_investor_action=None, existing_holder_action=None,
        current_short_action=None, current_long_action=None,
    )
    monkeypatch.setattr(store, 'global_current_suggestions', lambda **k: [pg_card])
    radar = store.global_opportunity_radar()
    assert radar['source_scan_id'] == scan['scan_id']
    best = radar['best_buy_today']
    assert best is not None
    assert best['reasons'] == ['QUALITY']
    assert best['risks'] == {'volatility': 'high'}
    assert best['evidence_snapshot'] == {'price': 1000.0}


# ---------------------------------------------------------------------------
# AB: a deployed research-engine image can run ahead of the Flyway migration
#     that adds the V15 columns (schema-history shows V14 applied, V15 not
#     yet run against this environment's database). global_current_suggestions
#     and global_opportunity_radar()'s _card() must degrade to None/[] for
#     the nine new fields rather than KeyError -- reproduces the production
#     500 (KeyError: 'data_state' in global_opportunity_radar) seen when the
#     column is genuinely absent from the row, not merely NULL.
# ---------------------------------------------------------------------------

def test_AB_current_suggestions_tolerates_pre_v15_schema(monkeypatch):
    store = fresh_store()
    now = '2026-09-13T10:05:00+00:00'
    # No data_state/missing_areas/.../current_long_action keys at all --
    # what `SELECT *` returns from a table that has never had V15 applied.
    pre_v15_row = dict(
        global_instrument_id=str(UUID(int=1)), horizon='SHORT_TERM', suggestion_id=str(uuid4()),
        latest_action='STRONG_BUY', latest_action_at=now, latest_price=1000.0,
        original_suggested_at=now, original_suggested_price=1000.0, current_rank=1,
        opportunity_score=80.0, confidence=80.0, coverage=90.0,
        entry_range_low=980.0, entry_range_high=1020.0, fair_value=1300.0,
        target_1=1100.0, target_2=1200.0, invalidation_price=800.0,
        reasons=['QUALITY'], risks={}, evidence_snapshot={'price': 1000.0},
        engine_version='STOCK_RULE_ENGINE_V1', last_scan_id=str(uuid4()),
    )

    class _Result:
        def fetchall(self):
            return [pre_v15_row]

    class _FakeConn:
        def execute(self, *a, **k):
            return _Result()

    monkeypatch.setattr(store, '_connection', _FakeConn())
    rows = store.global_current_suggestions()  # previously raised KeyError: 'data_state'
    assert len(rows) == 1
    row = rows[0]
    for key in ('data_state', 'short_term_state', 'long_term_state', 'new_investor_action',
                'existing_holder_action', 'current_short_action', 'current_long_action'):
        assert row[key] is None
    assert row['missing_areas'] is None
    assert row['stale_areas'] is None


def test_AB_radar_tolerates_pre_v15_schema(monkeypatch):
    store = fresh_store()
    scan = make_scan()
    store.record_global_market_scan(scan)
    now = '2026-09-13T10:05:00+00:00'
    pre_v15_card = dict(
        global_instrument_id=str(UUID(int=1)), horizon='SHORT_TERM', suggestion_id=str(uuid4()),
        latest_action='STRONG_BUY', latest_action_at=now, latest_price=1000.0,
        original_suggested_at=now, original_suggested_price=1000.0, current_rank=1,
        opportunity_score=80.0, confidence=80.0, coverage=90.0,
        entry_range_low=980.0, entry_range_high=1020.0, fair_value=1300.0,
        target_1=1100.0, target_2=1200.0, invalidation_price=800.0,
        reasons=['QUALITY'], risks={}, evidence_snapshot={'price': 1000.0},
        engine_version='STOCK_RULE_ENGINE_V1', last_scan_id=str(uuid4()),
        # deliberately omits every V15 column -- reproduces the live 500
    )
    monkeypatch.setattr(store, 'global_current_suggestions', lambda **k: [pre_v15_card])
    radar = store.global_opportunity_radar()  # previously raised KeyError: 'data_state'
    best = radar['best_buy_today']
    assert best is not None
    assert best['data_state'] is None
    assert best['missing_areas'] == []
    assert best['stale_areas'] == []
    assert best['short_term_state'] is None
    assert best['new_investor_action'] is None


# ---------------------------------------------------------------------------
# Z: global_opportunity_radar() always includes previous_recommendations,
#    backed by real persisted cycle history when it exists (never fabricated)
# ---------------------------------------------------------------------------

def test_Z_disabled_persistence_radar_includes_empty_previous_recommendations():
    """DisabledResearchPersistence has no cycle history to draw from, so
    previous_recommendations must be present and empty -- never omitted."""
    radar = DisabledResearchPersistence().global_opportunity_radar()
    assert 'previous_recommendations' in radar
    assert radar['previous_recommendations'] == []


def test_Z_radar_includes_previous_recommendations_key_with_no_completed_cycle():
    """Same V14-only scenario as test_T: no completed cycle has ever been
    published to global_opportunity_top_selection, so previous_recommendations
    is present and empty rather than missing entirely. This is the exact
    regression the fix targets: the frontend crashed because the key was
    absent from the response, not because it was merely empty."""
    store = fresh_store()
    scan = make_scan()
    card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', fingerprint='fp-Z1')
    store.persist_global_suggestion_lifecycle(scan, [card])
    radar = store.global_opportunity_radar()
    assert 'previous_recommendations' in radar
    assert radar['previous_recommendations'] == []


def test_Z_radar_hydrates_real_persisted_previous_recommendations(tmp_path):
    """End-to-end: publish two opportunity cycles for the same instrument via
    prepare_cycle/publish_opportunity_cycle (the real production write path),
    so the second cycle's selection legitimately records the instrument in
    previous_recommendations (it was in old_states from cycle 1). Verifies
    global_opportunity_radar() returns that real persisted card, hydrated
    with current recommendation state -- not [] and not a fabricated
    substitute for missing history."""
    from test_recommendation_lifecycle import snapshot, publish
    path = tmp_path / 'test_radar_previous.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])  # cycle 1: no prior state yet -> previous_recommendations == []
    moved = deepcopy(initial)
    moved.update(snapshot_id=str(uuid4()), generated_at='2026-09-13T12:00:00+00:00')
    publish(store, [moved])  # cycle 2: instrument now has prior state -> appears in previous_recommendations

    radar = store.global_opportunity_radar()
    assert 'previous_recommendations' in radar
    assert len(radar['previous_recommendations']) == 1
    card = radar['previous_recommendations'][0]
    assert card['global_instrument_id'] == initial['global_instrument_id']
    # Hydrated the same way opportunity_current() hydrates: carries live state.
    assert card.get('latest_recommendation_id') is not None


def test_Z_radar_previous_recommendations_matches_opportunity_current():
    """global_opportunity_radar() and opportunity_current() read the same
    underlying persisted cycle history for previous_recommendations, so they
    must agree on which instruments appear there."""
    from test_recommendation_lifecycle import snapshot, publish
    store = SqliteResearchPersistence()
    initial = snapshot()
    publish(store, [initial])
    moved = deepcopy(initial)
    moved.update(snapshot_id=str(uuid4()), generated_at='2026-09-13T12:00:00+00:00')
    publish(store, [moved])

    radar_ids = sorted(c['global_instrument_id'] for c in store.global_opportunity_radar()['previous_recommendations'])
    current_ids = sorted(c['global_instrument_id'] for c in store.opportunity_current()['previous_recommendations'])
    assert radar_ids == current_ids == [initial['global_instrument_id']]


# ---------------------------------------------------------------------------
# AA: global_opportunity_radar()'s top_short_term/top_long_term/best_buy_today
#     cards carry the FULL Opportunity contract with real values (previously
#     only global_instrument_id/horizon/public_action/price/score/confidence/
#     coverage/reasons/risks/evidence_snapshot were emitted; action fields,
#     per-horizon ranges, data-quality fields and opportunity_confidence/
#     score_coverage were entirely absent, causing a frontend crash reading
#     item.opportunity_confidence.toFixed(1)). Every new value here is
#     round-tripped from a real persisted card, never fabricated.
# ---------------------------------------------------------------------------

def _publish_real_cycle_and_suggestions(store, snapshots, *, scan_overrides=None):
    """Mirror run_global_opportunity_cycle's own two-step publish exactly:
    prepare_cycle/publish_opportunity_cycle first (global_opportunity_top_selection
    + recommendation_current_state), then persist_global_suggestion_lifecycle
    with the selection's own real cards (V14 tables) -- the same cards list
    run_global_opportunity_cycle builds in ai/research-engine/app/global_opportunity_cycle.py.
    Returns (selection, cards)."""
    from test_recommendation_lifecycle import publish
    selection = publish(store, snapshots)
    scan = make_scan(scan_id=selection['cycle_id'], completed_at=snapshots[0]['generated_at'],
                      **(scan_overrides or {}))
    cards = (list(selection.get('top_short_term', [])) + list(selection.get('top_long_term', []))
             + list(selection.get('top_exit', [])))
    if selection.get('best_buy_today'):
        cards.append(selection['best_buy_today'])
    store.persist_global_suggestion_lifecycle(scan, cards)
    return selection, cards


def test_AA_radar_short_term_card_carries_full_real_detail(tmp_path):
    """A SHORT_TERM radar card matches its real source card field-for-field on
    every previously-missing field, and never fabricates the opposite horizon's
    ranges (those must be None, since this row is not a LONG_TERM suggestion)."""
    from test_recommendation_lifecycle import snapshot
    path = tmp_path / 'test_radar_short_detail.db'
    store = SqliteResearchPersistence(path)
    s = snapshot()
    selection, cards = _publish_real_cycle_and_suggestions(store, [s])
    assert selection['top_short_term'], 'fixture must produce a real short-term BUY card'
    source = selection['top_short_term'][0]

    radar = store.global_opportunity_radar()
    assert radar['top_short_term'], 'radar must surface the same published short-term card'
    card = next(c for c in radar['top_short_term'] if c['global_instrument_id'] == source['global_instrument_id'])

    assert card['opportunity_confidence'] == source['opportunity_confidence']
    assert card['score_coverage'] == source['score_coverage']
    assert card['new_investor_action'] == source['new_investor_action']
    assert card['existing_holder_action'] == source['existing_holder_action']
    assert card['current_short_action'] == source['current_short_action']
    assert card['current_long_action'] == source['current_long_action']
    assert card['short_term_state'] == source['short_term_state']
    assert card['long_term_state'] == source['long_term_state']
    assert card['data_state'] == source['data_state']
    # global_stock_suggestion_current never stored symbol/company_name itself;
    # the radar joins back to global_stock_suggestion (real data captured at
    # suggestion-creation time) for these -- regression for cards rendering
    # with no visible company name/ticker at all.
    assert card['symbol'] == source.get('symbol')
    assert card['company_name'] == source.get('company_name')
    assert card['top_positive_reasons'] == source['top_positive_reasons']
    assert card['top_negative_reasons'] == source['top_negative_reasons']
    assert card['missing_areas'] == source['missing_areas']
    assert card['stale_areas'] == source['stale_areas']
    assert card['short_horizon'] == source['short_horizon']
    assert card['long_horizon'] == source['long_horizon']
    assert card['short_entry_low'] == source['short_entry_low']
    assert card['short_entry_high'] == source['short_entry_high']
    assert card['short_target_1'] == source['short_target_1']
    assert card['short_target_2'] == source['short_target_2']
    assert card['short_invalidation'] == source['short_invalidation']
    # Not this card's horizon: must be None, never a fabricated substitute.
    assert card['long_entry_low'] is None
    assert card['long_entry_high'] is None
    assert card['long_fair_value'] is None
    assert card['long_target'] is None
    assert card['long_invalidation'] is None


def test_AA_radar_long_term_card_carries_full_real_detail(tmp_path):
    """A LONG_TERM radar card populates long_* ranges from the real source card
    and leaves short_* ranges as None (not this card's horizon)."""
    from test_recommendation_lifecycle import snapshot
    path = tmp_path / 'test_radar_long_detail.db'
    store = SqliteResearchPersistence(path)
    s = snapshot()
    # Base snapshot() qualifies for BOTH short-term BUY and long-term ACCUMULATE,
    # and public_horizon() only ever tags a card with ONE horizon (short wins
    # when both qualify -- see global_opportunity_cycle.py). Force the
    # short-term action to WAIT (proven by
    # test_recommendation_lifecycle.test_actions_independent_and_missing_not_negative
    # to still leave long_term_action == 'ACCUMULATE') so this card is
    # unambiguously LONG_TERM-only and V14's dedup-by-(instrument, horizon)
    # doesn't drop its long-term persistence.
    s['evidence_state']['technical']['technical_state'] = 'OVEREXTENDED'
    selection, cards = _publish_real_cycle_and_suggestions(store, [s])
    assert selection['top_long_term'], 'fixture must produce a real long-term ACCUMULATE/TOP_UP card'
    source = selection['top_long_term'][0]

    radar = store.global_opportunity_radar()
    assert radar['top_long_term'], 'radar must surface the same published long-term card'
    card = next(c for c in radar['top_long_term'] if c['global_instrument_id'] == source['global_instrument_id'])

    assert card['opportunity_confidence'] == source['opportunity_confidence']
    assert card['score_coverage'] == source['score_coverage']
    assert card['long_entry_low'] == source['long_entry_low']
    assert card['long_entry_high'] == source['long_entry_high']
    assert card['long_fair_value'] == source['long_fair_value']
    assert card['long_target'] == source['long_target']
    assert card['long_invalidation'] == source['long_invalidation']
    assert card['short_entry_low'] is None
    assert card['short_target_1'] is None
    assert card['short_invalidation'] is None
    assert card['symbol'] == source.get('symbol')


def test_AA_radar_best_buy_today_has_opportunity_confidence(tmp_path):
    """The exact field that crashed the frontend (item.opportunity_confidence.
    toFixed(1)) is present and correct on best_buy_today too, not just the
    short/long term lists."""
    from test_recommendation_lifecycle import snapshot
    path = tmp_path / 'test_radar_best_buy.db'
    store = SqliteResearchPersistence(path)
    s = snapshot()
    selection, cards = _publish_real_cycle_and_suggestions(store, [s])
    assert selection['best_buy_today'] is not None

    radar = store.global_opportunity_radar()
    assert radar['best_buy_today'] is not None
    assert radar['best_buy_today']['opportunity_confidence'] == selection['best_buy_today']['opportunity_confidence']
    assert radar['best_buy_today']['score_coverage'] == selection['best_buy_today']['score_coverage']
    assert radar['best_buy_today']['symbol'] == selection['best_buy_today'].get('symbol')


def test_AA_disabled_persistence_radar_still_has_all_new_keys():
    """DisabledResearchPersistence's stub never carries a public card, but the
    contract shape (via previous_recommendations) must still be internally
    consistent -- covered by test_Z; this just confirms nothing here regressed
    the disabled-persistence stub's own shape."""
    radar = DisabledResearchPersistence().global_opportunity_radar()
    assert radar['top_short_term'] == []
    assert radar['top_long_term'] == []
    assert radar['top_exit'] == []
    assert radar['previous_recommendations'] == []
    # Count fields present and zero on the empty/disabled path.
    assert radar['top_short_term_count'] == 0
    assert radar['top_long_term_count'] == 0
    assert radar['top_exit_count'] == 0


# ---------------------------------------------------------------------------
# AC: RADAR V2 UI-1 — ALL qualifying results are returned (not truncated to 4)
#     Count fields are present and accurate. Short/long-term sections are
#     independent. Ordering is deterministic (no re-rank on read).
# ---------------------------------------------------------------------------
def test_AC_radar_returns_all_qualifying_short_term_not_truncated_to_4():
    """9 distinct SHORT_TERM buy cards → all 9 returned (previously [:4])."""
    store = fresh_store()
    scan = make_scan()
    cards = [make_card(n, action='STRONG_BUY', horizon='SHORT_TERM',
                       opportunity_score=float(90 + n),
                       fingerprint=f'fp-ac-short-{n}') for n in range(1, 10)]
    store.persist_global_suggestion_lifecycle(scan, cards)
    radar = store.global_opportunity_radar()
    assert len(radar['top_short_term']) == 9
    assert radar['top_short_term_count'] == 9
    assert len(radar['top_long_term']) == 0
    assert radar['top_long_term_count'] == 0


def test_AC_radar_returns_all_qualifying_long_term_not_truncated_to_4():
    """9 distinct LONG_TERM buy cards → all 9 returned (previously [:4])."""
    store = fresh_store()
    scan = make_scan()
    cards = [make_card(n, action='BUY', horizon='LONG_TERM',
                       opportunity_score=float(90 + n),
                       fingerprint=f'fp-ac-long-{n}') for n in range(1, 10)]
    store.persist_global_suggestion_lifecycle(scan, cards)
    radar = store.global_opportunity_radar()
    assert len(radar['top_long_term']) == 9
    assert radar['top_long_term_count'] == 9
    assert len(radar['top_short_term']) == 0
    assert radar['top_short_term_count'] == 0


def test_AC_radar_short_and_long_sections_are_independent():
    """5 SHORT_TERM + 5 LONG_TERM buys, all returned, counts match each section."""
    store = fresh_store()
    scan = make_scan()
    short_cards = [make_card(n, action='STRONG_BUY', horizon='SHORT_TERM',
                             opportunity_score=float(90 + n),
                             fingerprint=f'fp-ac-ind-short-{n}') for n in range(1, 6)]
    long_cards = [make_card(n, action='BUY', horizon='LONG_TERM',
                            opportunity_score=float(80 + n),
                            fingerprint=f'fp-ac-ind-long-{n}') for n in range(6, 11)]
    store.persist_global_suggestion_lifecycle(scan, short_cards + long_cards)
    radar = store.global_opportunity_radar()
    assert len(radar['top_short_term']) == 5
    assert len(radar['top_long_term']) == 5
    assert radar['top_short_term_count'] == 5
    assert radar['top_long_term_count'] == 5
    short_ids = {c['global_instrument_id'] for c in radar['top_short_term']}
    long_ids = {c['global_instrument_id'] for c in radar['top_long_term']}
    assert short_ids == {str(UUID(int=n)) for n in range(1, 6)}
    assert long_ids == {str(UUID(int=n)) for n in range(6, 11)}


def test_AC_radar_empty_store_returns_zero_counts_and_empty_lists():
    """No scans / no current suggestions → all lists empty, counts zero."""
    store = fresh_store()
    radar = store.global_opportunity_radar()
    assert radar['top_short_term'] == []
    assert radar['top_long_term'] == []
    assert radar['top_exit'] == []
    assert radar['top_short_term_count'] == 0
    assert radar['top_long_term_count'] == 0
    assert radar['top_exit_count'] == 0
    assert radar['best_buy_today'] is None
    assert radar['previous_recommendations'] == []


def test_AC_radar_ordering_is_deterministic_across_reads():
    """Two consecutive reads return identical results — no re-rank on read."""
    store = fresh_store()
    scan = make_scan()
    cards = [make_card(n, action='STRONG_BUY', horizon='SHORT_TERM',
                       opportunity_score=float(90 + n),
                       fingerprint=f'fp-ac-det-{n}') for n in range(1, 10)]
    store.persist_global_suggestion_lifecycle(scan, cards)
    radar_1 = store.global_opportunity_radar()
    radar_2 = store.global_opportunity_radar()
    assert radar_1 == radar_2
    # Ordering is by opportunity_score descending (higher score = earlier).
    scores = [c['opportunity_score'] for c in radar_1['top_short_term']]
    assert scores == sorted(scores, reverse=True)


def test_AC_radar_count_fields_match_array_lengths():
    """Count fields always equal the actual array length, even below and above 4."""
    store = fresh_store()
    scan = make_scan()
    for horizon, n_range in [('SHORT_TERM', range(1, 6)), ('LONG_TERM', range(6, 11))]:
        cards = [make_card(n, action='STRONG_BUY', horizon=horizon,
                           opportunity_score=float(90 + n),
                           fingerprint=f'fp-ac-cnt-{horizon}-{n}') for n in n_range]
        store.persist_global_suggestion_lifecycle(scan, cards)
    radar = store.global_opportunity_radar()
    assert radar['top_short_term_count'] == len(radar['top_short_term']) == 5
    assert radar['top_long_term_count'] == len(radar['top_long_term']) == 5
    assert radar['top_exit_count'] == len(radar['top_exit']) == 0
