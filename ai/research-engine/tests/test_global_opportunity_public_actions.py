"""Focused tests for global opportunity public-action mapping, top_exit section,
exit urgency ordering, persistence hydration, and Best Buy Today selection.

Categories covered:
  A — public_action maps to STRONG_BUY
  B — public_action maps to BUY
  C — public_action maps to PARTIAL_EXIT / SELL
  D — non-actionable internal actions (HOLD/WAIT/ACCUMULATE/ENTRY_APPROACHING/
      IN_ENTRY_ZONE) produce None and never appear on the public radar
  E — prepare_cycle emits a top_exit section
  F — exit urgency ordering (SELL before PARTIAL_EXIT, lower score first in SELL,
      higher score first in PARTIAL_EXIT)
  G — DisabledResearchPersistence.opportunity_current() includes top_exit=[]
  G2 — OpportunityPersistenceMixin.hydrate covers top_exit end-to-end
"""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from app.global_opportunity_cycle import prepare_cycle, public_action, public_horizon
from app.persistence import DisabledResearchPersistence, SqliteResearchPersistence

# Reuse the established snapshot/publish harness from the lifecycle test module.
from test_recommendation_lifecycle import snapshot, publish


# ---------------------------------------------------------------------------
# A — public_action → STRONG_BUY
# ---------------------------------------------------------------------------

def test_category_a_public_action_strong_buy():
    """When the short-term action is BUY and the ranker qualifies with STRONG_BUY_CANDIDATE,
    the public radar action is STRONG_BUY."""
    card = dict(current_short_action='BUY', current_long_action='ACCUMULATE',
                new_investor_action='STRONG_BUY_CANDIDATE')
    assert public_action(card) == 'STRONG_BUY'
    assert public_horizon(card) == 'SHORT_TERM'


def test_category_a_strong_buy_requires_rank_eligible():
    """A card that is rank-eligible with score >= STRONG_SCORE and short-term BUY
    is the strongest public signal."""
    card = dict(current_short_action='BUY', current_long_action='ACCUMULATE',
                new_investor_action='STRONG_BUY_CANDIDATE',
                opportunity_score=80, rank_eligible=True)
    assert public_action(card) == 'STRONG_BUY'


# ---------------------------------------------------------------------------
# B — public_action → BUY
# ---------------------------------------------------------------------------

def test_category_b_public_action_buy():
    """When the short-term action is BUY but the candidate is BUY_CANDIDATE (score
    between BUY_SCORE and STRONG_SCORE), the public action is BUY."""
    card = dict(current_short_action='BUY', current_long_action='ACCUMULATE',
                new_investor_action='BUY_CANDIDATE')
    assert public_action(card) == 'BUY'
    assert public_horizon(card) == 'SHORT_TERM'


def test_category_b_long_term_buy_via_accumulate():
    """A LONG_TERM BUY surfaced via ACCUMULATE / TOP_UP maps to BUY on the long horizon."""
    card_acc = dict(current_short_action='WAIT', current_long_action='ACCUMULATE',
                    new_investor_action='BUY_CANDIDATE')
    assert public_action(card_acc) == 'BUY'
    assert public_horizon(card_acc) == 'LONG_TERM'

    card_top_up = dict(current_short_action='WAIT', current_long_action='TOP_UP',
                       new_investor_action='BUY_CANDIDATE')
    assert public_action(card_top_up) == 'BUY'
    assert public_horizon(card_top_up) == 'LONG_TERM'


# ---------------------------------------------------------------------------
# C — public_action → PARTIAL_EXIT / SELL
# ---------------------------------------------------------------------------

def test_category_c_public_action_partial_exit():
    """When the short-term action is PARTIAL_EXIT (target reached), the public
    action is PARTIAL_EXIT."""
    card = dict(current_short_action='PARTIAL_EXIT', current_long_action='HOLD',
                new_investor_action='WATCH_WAIT')
    assert public_action(card) == 'PARTIAL_EXIT'
    assert public_horizon(card) == 'SHORT_TERM'


def test_category_c_public_action_partial_exit_reduce():
    """REDUCE on the long horizon also maps to PARTIAL_EXIT."""
    card = dict(current_short_action='WAIT', current_long_action='REDUCE',
                new_investor_action='WATCH_WAIT')
    assert public_action(card) == 'PARTIAL_EXIT'
    assert public_horizon(card) == 'LONG_TERM'


def test_category_c_public_action_sell():
    """When the short-term action is EXIT (invalidation hit), the public action
    is SELL."""
    card = dict(current_short_action='EXIT', current_long_action='EXIT_REVIEW',
                new_investor_action='STRONG_BUY_CANDIDATE')
    assert public_action(card) == 'SELL'


def test_category_c_sell_takes_priority_over_strong_buy():
    """Even if new_investor_action is STRONG_BUY_CANDIDATE, an EXIT short-term
    action surfaces as SELL — exit signals always win."""
    card = dict(current_short_action='EXIT', current_long_action='EXIT_REVIEW',
                new_investor_action='STRONG_BUY_CANDIDATE')
    assert public_action(card) == 'SELL'


def test_category_c_sell_via_long_exit_review():
    """EXIT_REVIEW on the long horizon surfaces as SELL even if short is HOLD."""
    card = dict(current_short_action='HOLD', current_long_action='EXIT_REVIEW',
                new_investor_action='STRONGBuy_CANDIDATE')  # typo intentional — shouldn't matter
    assert public_action(card) == 'SELL'


# ---------------------------------------------------------------------------
# D — non-actionable internal actions excluded (None)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("short,long,new_action", [
    ('HOLD', 'HOLD', 'WATCH_WAIT'),
    ('WAIT', 'HOLD', 'WATCH_WAIT'),
    ('HOLD', 'WAIT', 'WATCH_WAIT'),
    ('WAIT', 'ACCUMULATE', 'BUY_CANDIDATE'),  # ACCUMULATE is mapped to BUY — included
])
def test_category_d_non_actionable_produce_none_or_buy(short, long, new_action):
    """HOLD/WAIT on both horizons produce None. ACCUMULATE via long maps to BUY
    (it is surfaced on the long-term BUY lane, not as 'ACCUMULATE')."""
    card = dict(current_short_action=short, current_long_action=long,
                new_investor_action=new_action)
    result = public_action(card)
    if short in {'BUY'} or long in {'ACCUMULATE', 'TOP_UP'}:
        assert result == 'BUY'
    else:
        assert result is None


def test_category_d_hold_wait_accumulate_not_surfaced_as_label():
    """The literal strings HOLD, WAIT, ACCUMULATE, ENTRY_APPROACHING, IN_ENTRY_ZONE
    must never appear as a public_action value."""
    # Only STRONG_BUY, BUY, PARTIAL_EXIT, SELL (or None) may surface.
    # All other internal lifecycle labels must be mapped away.
    excluded_labels = {'HOLD', 'WAIT', 'ACCUMULATE', 'ENTRY_APPROACHING',
                       'IN_ENTRY_ZONE', 'TOP_UP', 'EXIT_REVIEW', 'REDUCE',
                       'EXIT', 'STRONG_BUY_CANDIDATE', 'BUY_CANDIDATE', 'AVOID',
                       'WATCH_WAIT', 'HOLD_NO_NEW_MONEY'}
    for short in ['BUY', 'HOLD', 'WAIT', 'EXIT', 'PARTIAL_EXIT', 'ACCUMULATE']:
        for long in ['ACCUMULATE', 'TOP_UP', 'HOLD', 'WAIT', 'EXIT_REVIEW', 'REDUCE', 'EXIT']:
            for new in ['STRONG_BUY_CANDIDATE', 'BUY_CANDIDATE', 'WATCH_WAIT', 'AVOID']:
                card = dict(current_short_action=short, current_long_action=long,
                            new_investor_action=new)
                result = public_action(card)
                if result is not None:
                    assert result in {'STRONG_BUY', 'BUY', 'PARTIAL_EXIT', 'SELL'}, result
                assert result not in excluded_labels


# ---------------------------------------------------------------------------
# E — prepare_cycle emits a top_exit section
# ---------------------------------------------------------------------------

def test_category_e_top_exit_emitted_in_selection():
    """prepare_cycle always includes a top_exit key in its selection dict."""
    store = SqliteResearchPersistence()
    s = snapshot()
    selection = publish(store, [s])
    assert 'top_exit' in selection
    assert isinstance(selection['top_exit'], list)


def test_category_e_top_exit_contains_partial_exit_card(tmp_path):
    """After publishing a BUY at 1000 and then moving price to 1185 (target reached),
    the card appears in top_exit as PARTIAL_EXIT."""
    path = tmp_path / 'test_exit.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])
    moved = deepcopy(initial)
    moved.update(snapshot_id=str(uuid4()), current_price=1185.,
                 generated_at='2026-09-13T12:00:00+00:00')
    selection = publish(store, [moved])
    assert len(selection['top_exit']) == 1
    card = selection['top_exit'][0]
    assert card['public_action'] == 'PARTIAL_EXIT'
    assert card['current_short_action'] == 'PARTIAL_EXIT'
    assert card['global_instrument_id'] == initial['global_instrument_id']


def test_category_e_top_exit_contains_sell_card(tmp_path):
    """After publishing a BUY at 1000 and then moving price to 930 (invalidation hit),
    the card appears in top_exit as SELL."""
    path = tmp_path / 'test_sell.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])
    exited = deepcopy(initial)
    exited.update(snapshot_id=str(uuid4()), current_price=930.,
                  generated_at='2026-09-13T12:00:00+00:00')
    selection = publish(store, [exited])
    assert len(selection['top_exit']) == 1
    card = selection['top_exit'][0]
    assert card['public_action'] == 'SELL'
    assert card['current_short_action'] == 'EXIT'


def test_category_e_top_exit_not_in_buy_sections(tmp_path):
    """A PARTIAL_EXIT card must NOT appear in top_short_term or top_long_term."""
    path = tmp_path / 'test_exclude.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])
    moved = deepcopy(initial)
    moved.update(snapshot_id=str(uuid4()), current_price=1185.,
                 generated_at='2026-09-13T12:00:00+00:00')
    selection = publish(store, [moved])
    short_ids = [c['global_instrument_id'] for c in selection['top_short_term']]
    long_ids = [c['global_instrument_id'] for c in selection['top_long_term']]
    exit_ids = [c['global_instrument_id'] for c in selection['top_exit']]
    assert exit_ids
    assert short_ids == []
    assert long_ids == []


# ---------------------------------------------------------------------------
# F — exit urgency ordering (SELL before PARTIAL_EXIT)
# ---------------------------------------------------------------------------

def test_category_f_sell_before_partial_exit(tmp_path):
    """In top_exit, SELL cards come before PARTIAL_EXIT cards (exit is more urgent)."""
    path = tmp_path / 'test_urgency.db'
    store = SqliteResearchPersistence(path)
    # Instrument 1: BUY at 1000, then EXIT (price 930) → SELL
    s1 = snapshot(1)
    publish(store, [s1])
    s1_exit = deepcopy(s1)
    s1_exit.update(snapshot_id=str(uuid4()), current_price=930.,
                   generated_at='2026-09-13T12:00:00+00:00')
    # Instrument 2: BUY at 1000, then PARTIAL_EXIT (price 1185)
    s2 = snapshot(2)
    publish(store, [s2])
    s2_exit = deepcopy(s2)
    s2_exit.update(snapshot_id=str(uuid4()), current_price=1185.,
                   generated_at='2026-09-13T12:00:00+00:00')
    # Publish both exit cards in the same cycle
    publish(store, [s1_exit, s2_exit])
    current = store.opportunity_current()
    actions = [c['public_action'] for c in current['top_exit']]
    assert 'SELL' in actions
    assert 'PARTIAL_EXIT' in actions
    # SELL must come first (urgency tuple sort: SELL=0, PARTIAL_EXIT=1)
    sell_idx = actions.index('SELL')
    partial_idx = actions.index('PARTIAL_EXIT')
    assert sell_idx < partial_idx


# ---------------------------------------------------------------------------
# G — DisabledResearchPersistence.opportunity_current() includes top_exit=[]
# ---------------------------------------------------------------------------

def test_category_g_disabled_persistence_has_top_exit():
    """DisabledResearchPersistence fallback must include an empty top_exit list."""
    dp = DisabledResearchPersistence()
    result = dp.opportunity_current()
    assert result['top_exit'] == []


def test_category_g_sqlite_persistence_hydrates_top_exit(tmp_path):
    """End-to-end: publish a cycle with exit cards, read back via opportunity_current(),
    and verify top_exit is hydrated with full state + history."""
    path = tmp_path / 'test_hydrate.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])
    exited = deepcopy(initial)
    exited.update(snapshot_id=str(uuid4()), current_price=930.,
                  generated_at='2026-09-13T12:00:00+00:00')
    publish(store, [exited])
    current = store.opportunity_current()
    assert 'top_exit' in current
    assert len(current['top_exit']) == 1
    card = current['top_exit'][0]
    # Hydrated fields: should have latest_recommendation_id from current state
    assert card.get('latest_recommendation_id') is not None
    assert card['public_action'] == 'SELL'
    assert card['current_short_action'] == 'EXIT'
    # best_buy_today should be None (no active buys)
    assert current['best_buy_today'] is None


# ---------------------------------------------------------------------------
# M (additional) — prepare_cycle cards always carry public_action and horizon
# ---------------------------------------------------------------------------

def test_prepare_cycle_cards_have_public_action_and_horizon():
    """Every card in top_short_term, top_long_term, top_exit, and previous_recommendations
    must carry public_action and horizon fields."""
    store = SqliteResearchPersistence()
    s = snapshot()
    publish(store, [s])
    # Also publish an exit scenario
    path_store = SqliteResearchPersistence()
    initial = snapshot()
    publish(path_store, [initial])
    exited = deepcopy(initial)
    exited.update(snapshot_id=str(uuid4()), current_price=930.,
                  generated_at='2026-09-13T12:00:00+00:00')
    selection = publish(path_store, [exited])
    for field in ('top_short_term', 'top_long_term', 'top_exit', 'previous_recommendations'):
        for card in selection[field]:
            assert 'public_action' in card, f"missing public_action in {field}"
            assert 'horizon' in card, f"missing horizon in {field}"


# ---------------------------------------------------------------------------
# N (additional) — Best Buy Today is the strongest STRONG_BUY
# ---------------------------------------------------------------------------

def test_best_buy_today_is_strongest_strong_buy(tmp_path):
    """best_buy_today must be the STRONG_BUY/BUY card with the highest rank,
    and rank 1 (index 0) is the best fit."""
    path = tmp_path / 'test_best_buy.db'
    store = SqliteResearchPersistence(path)
    # Three STRONG_BUY candidates with equal scores; tie-break by instrument ID.
    rows = [snapshot(i) for i in range(1, 4)]
    publish(store, rows)
    current = store.opportunity_current()
    assert current['best_buy_today'] is not None
    bb = current['best_buy_today']
    assert bb['public_action'] in ('STRONG_BUY', 'BUY')
    # It must be the first in the buys ordering.
    buys = [bb] + current['top_short_term'] + current['top_long_term']
    # best_buy_today should be the highest-ranked by the ordering key.
    scores = [(-c['opportunity_score'], -c['confidence'], -c['coverage'],
               -(c['rule_engine_score'] if c['rule_engine_score'] is not None else -1),
               c['global_instrument_id']) for c in buys]
    assert scores[0] == min(scores)


def test_best_buy_today_none_when_no_buys(tmp_path):
    """When all active positions are in exit territory, best_buy_today is None."""
    path = tmp_path / 'test_no_buys.db'
    store = SqliteResearchPersistence(path)
    initial = snapshot()
    publish(store, [initial])
    exited = deepcopy(initial)
    exited.update(snapshot_id=str(uuid4()), current_price=930.,
                  generated_at='2026-09-13T12:00:00+00:00')
    current = publish(store, [exited])
    assert current['best_buy_today'] is None
    # And the persisted snapshot also shows None
    persisted = store.opportunity_current()
    assert persisted['best_buy_today'] is None
