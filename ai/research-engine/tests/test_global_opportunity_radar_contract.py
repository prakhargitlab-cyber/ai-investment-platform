"""UI-1C full-universe result-contract tests.

These tests drive the real ``run_global_opportunity_cycle`` (the production
Radar V2 entry point) with a stubbed orchestrator that yields a controlled set
of rank-eligible investigated entries, then assert that the *persisted* Global
Opportunity Radar and the GET /opportunities/current read model return EVERY
qualifying recommendation.

The point of the exercise is the separation of concerns demanded by UI-1C:

  A. universe coverage          = full canonical NSE universe
  B. priority shortlist         = processing order only
  C. bounded acquisition/working set = memory/provider safety
  D. qualifying recommendation count = however many genuinely qualify
  E. UI initial display          = 4 (client-side, presentation only)

``top_n`` at the public request layer is legacy display metadata. It must never
truncate the qualifying persisted set; production passes ``top_n=None`` to the
orchestrator (unbounded ranking) and publication iterates
``ranking.evaluated_entries``. There is no 4/25/100 cap between completed bounded
deep investigation and publication.
"""
import pathlib
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.global_opportunity_cycle import run_global_opportunity_cycle
from app.global_opportunity_orchestration import OpportunityEntry
from app.persistence import SqliteResearchPersistence
from test_global_scanner import instrument, NOW


class _Clock:
    @staticmethod
    def now(*args):
        return NOW


class _FakeSource:
    """Canonical source returning a non-empty NSE universe of `n` instruments."""

    def __init__(self, n):
        self._instruments = [instrument(i + 1) for i in range(n)]

    async def active_global_equities(self, identity_headers=None):
        return list(self._instruments)

    async def sector_benchmark_contexts(self, ids, identity_headers=None):
        return {}

    def register_global_profile_metadata(self, key, payload):
        return True


class _RankingWithEntries:
    """Minimal ranking stub carrying `n` rank-eligible evaluated entries."""

    def __init__(self, entries):
        self.evaluated_entries = list(entries)
        self.top_n = list(entries)          # production (unbounded) = ALL rank-eligible
        self.universe_count = max(len(entries), 1)
        self.shortlist_count = len(entries)
        self.deep_evaluated_count = len(entries)
        self.rank_eligible_count = len(entries)
        self.diagnostics = []
        self.review_evidence = {}
        self.candidate_nominations = {}
        self.investigation_matrix = {}
        self.rotation_after = None


class _FakeRankingOrchestrator:
    """Stand-in for GlobalOpportunityOrchestrator.run that captures the top_n
    it was called with and returns a ranking with a controlled number of
    rank-eligible entries -- without touching the real scanner/readiness chain."""

    def __init__(self, entries, captured):
        self._entries = entries
        self._captured = captured

    async def run(self, *args, **kwargs):
        self._captured['top_n'] = kwargs.get('top_n')
        self._captured['called'] = True
        return _RankingWithEntries(self._entries)


def _buy_entry(n):
    """An OpportunityEntry whose evidence state yields a SHORT_TERM STRONG_BUY
    card under RecommendationEngineV1 (BUY short-term, HOLD long-term), so every
    entry round-trips into the published top_short_term section."""
    return OpportunityEntry(
        rank=n,
        global_instrument_id=UUID(int=n),
        symbol=f'C{n}',
        company_name=f'Company {n}',
        rank_eligible=True,
        rule_engine_version='STOCK_RULE_ENGINE_V1',
        technical_feature_version='TFV1',
        sector_feature_version='SFV1',
        opportunity_ranker_version='GLOBAL_OPPORTUNITY_RANKER_V1',
        opportunity_score=80.0,
        opportunity_confidence=80.0,
        score_coverage=90.0,
        rule_engine_score=80.0,
        rule_engine_confidence=80.0,
        top_positive_reasons=['SUPPORT:QUALITY'],
        top_negative_reasons=[],
        evidence_state={
            'technical': {'technical_state': 'UPTREND', 'support_level': 980.0,
                          'resistance_level': 1150.0, 'atr14': 50.0, 'latest_price': 1000.0,
                          'history_end': NOW.isoformat()},
            # VALUATION raw_score < 60 keeps long_buy=False (long=HOLD), so each
            # card lands in SHORT_TERM only -- no short/long duplication.
            'rule': {'area_scores': [
                {'area': 'FUNDAMENTAL_BUSINESS_QUALITY', 'raw_score': 80},
                {'area': 'VALUATION', 'raw_score': 50},
                {'area': 'GROWTH', 'raw_score': 80},
                {'area': 'BALANCE_SHEET', 'raw_score': 80},
            ]},
        },
    )


async def _run_full_cycle(monkeypatch, n, top_n=4):
    """Run the production cycle with a stubbed orchestrator yielding `n`
    rank-eligible buy entries; return (selection, store, captured)."""
    from app import global_opportunity_cycle as cycle

    captured = {}
    entries = [_buy_entry(i + 1) for i in range(n)]
    monkeypatch.setattr(cycle, 'datetime', _Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator',
                        lambda *a, **k: _FakeRankingOrchestrator(entries, captured))

    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    selection = await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(n), top_n=top_n)
    return selection, store, captured


# ---------------------------------------------------------------------------
# A/B: request top_n=4 must NOT truncate the published qualifying set to 4
#      (>4 rank-eligible survive publication, in deterministic rank order).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize('n', [9, 30])
async def test_production_cycle_top_n_4_publishes_all_rank_eligible_not_truncated(monkeypatch, n):
    """UI-1C A/B/C: a production cycle requested with top_n=4 publishes ALL
    qualifying rank-eligible entries -- 9 (>4) and 30 (>25 shortlist), so neither
    the legacy display count (4) nor the shortlist size (25) becomes a cap."""
    selection, store, captured = await _run_full_cycle(monkeypatch, n, top_n=4)

    # D (functional): production invokes the orchestrator unbounded (top_n=None),
    # never the legacy top_n=100 workaround.
    assert captured['called'] is True
    assert captured['top_n'] is None

    # A/B/C: every qualifying rank-eligible entry is published -- not truncated.
    assert len(selection['top_short_term']) == n
    assert selection['best_buy_today'] is not None  # Best Buy Today present

    # The persisted radar (GET /opportunities/current) returns the full set too.
    radar = store.global_opportunity_radar()
    assert len(radar['top_short_term']) == n
    assert radar['top_short_term_count'] == n

    # No artificial fill/truncation: counts equal collection lengths (UI-1C E).
    for field, count_field in [('top_short_term', 'top_short_term_count'),
                               ('top_long_term', 'top_long_term_count'),
                               ('top_exit', 'top_exit_count')]:
        assert radar[count_field] == len(radar[field])


# ---------------------------------------------------------------------------
# D: no magic 100 (or top_n=100 workaround) exists in the production path.
# ---------------------------------------------------------------------------
def test_production_cycle_passes_unbounded_top_n_to_orchestrator():
    """UI-1C D: the production cycle must request an UNBOUNDED ranking from the
    orchestrator. The orchestrator call site must pass top_n=None, not a
    numeric cap such as 100."""
    import app.global_opportunity_cycle as cycle_mod

    source = pathlib.Path(cycle_mod.__file__).read_text(encoding='utf-8')
    assert 'top_n=None, review_ids=review_ids' in source
    assert 'top_n=100' not in source


def test_no_magic_100_cap_anywhere_in_production_orchestration():
    """UI-1C D: no 'top_n=100' literal remains in either production module that
    forms the Radar V2 result contract."""
    import app.global_opportunity_cycle as cycle_mod
    import app.global_opportunity_orchestration as orch_mod

    for mod in (cycle_mod, orch_mod):
        src = pathlib.Path(mod.__file__).read_text(encoding='utf-8')
        assert 'top_n=100' not in src, f'magic 100 cap present in {mod.__file__}'
        # No read/publication-time truncation of the qualifying list.
        assert "e for e in entries if e.rank_eligible][:top_n]" not in src


# ---------------------------------------------------------------------------
# Best Buy Today: the single highest-ranked qualifying recommendation.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_best_buy_today_is_highest_ranked_qualifying_entry(monkeypatch):
    """UI-1C: Best Buy Today is rank_position 1 and is a member of the
    published top_short_term set; it is never artificially padded."""
    selection, store, _ = await _run_full_cycle(monkeypatch, 9)

    bb = selection['best_buy_today']
    assert bb is not None
    assert bb['rank_position'] == 1
    short_ids = {c['global_instrument_id'] for c in selection['top_short_term']}
    assert bb['global_instrument_id'] in short_ids

    radar = store.global_opportunity_radar()
    assert radar['best_buy_today'] is not None
    assert radar['best_buy_today']['rank_position'] == 1
    assert radar['best_buy_today']['global_instrument_id'] == bb['global_instrument_id']


# ---------------------------------------------------------------------------
# M (backend portion): 0 qualify -> persist 0, no artificial fill.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_rank_eligible_persists_empty_radar_with_no_fill(monkeypatch):
    """UI-1C M: when 0 candidates qualify, the cycle publishes a valid empty
    COMPLETED radar with zero counts and empty lists -- never artificially
    filled, never truncated."""
    from app import global_opportunity_cycle as cycle

    captured = {}
    monkeypatch.setattr(cycle, 'datetime', _Clock)
    # Empty ranking but a NON-empty universe (universe_count > 0) -> completes
    # normally (not UniverseUnavailableError) with nothing published.
    fake = _FakeRankingOrchestrator([], captured)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator', lambda *a, **k: fake)

    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    selection = await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(5), top_n=4)

    assert selection['status'] == 'COMPLETED'
    assert selection['best_buy_today'] is None
    assert selection['top_short_term'] == []
    assert len(store.global_current_suggestions()) == 0

    radar = store.global_opportunity_radar()
    assert radar['top_short_term'] == []
    assert radar['top_short_term_count'] == 0
    assert radar['top_long_term_count'] == 0
    assert radar['top_exit_count'] == 0
    assert radar['best_buy_today'] is None
