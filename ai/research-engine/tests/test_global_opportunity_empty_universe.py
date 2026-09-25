"""Focused invariant tests: Global Opportunity Radar -- empty-universe guard.

Problem C: a production (candidate_ids=None) cycle whose canonical NSE
universe itself resolves to no active equities (universe_count==0) must fail
explicitly (UniverseUnavailableError) and must NEVER overwrite the persisted
Global Opportunity Radar with an empty projection.

DI-9B: a production cycle with a NON-empty canonical universe that simply
produces zero rankable opportunities (no candidates reached deep analysis,
or none were rank-eligible, and there are no prior active recommendations to
review) is NOT an unavailable universe. That case must complete normally and
publish a real, valid empty COMPLETED radar: generated_at and source_scan_id
set, universe_count preserved (>0), best_buy_today=None, and
top_short_term/top_long_term/top_exit all [].

The guard lives in app.global_opportunity_cycle.run_global_opportunity_cycle
ahead of publish_opportunity_cycle (V13 top_selection) and
persist_global_suggestion_lifecycle (V14 scan/suggestions). The orchestrator's
run() does not itself persist scans, so an aborted (ARM1) cycle leaves the
persisted radar (and the last COMPLETED scan) untouched. OpportunityCycleWorker
maps the ARM1 error to error_code='UNIVERSE_UNAVAILABLE'.
"""
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.persistence import SqliteResearchPersistence
from app.global_opportunity_cycle import UniverseUnavailableError
from test_global_scanner import NOW, instrument


class _EmptyRanking:
    """Minimal ranking stub for the 'universe present but no eligible
    candidates' case: non-zero universe_count, empty evaluated_entries/top_n."""

    universe_count = 1
    evaluated_entries: list = []
    deep_evaluated_count = 0
    rank_eligible_count = 0
    shortlist_count = 0
    preliminary_eligible_count = 0
    top_n: list = []
    diagnostics: list = []
    review_evidence: dict = {}


class _FakeOrchestrator:
    """Stand-in for GlobalOpportunityOrchestrator.run that returns an empty
    ranking without touching the real scanner/readiness chain (no providers)."""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        return _EmptyRanking()


class _FakeSource:
    """Canonical source returning a NON-empty universe (so the universe_count==0
    arm does not fire) but with no persisted facts -> no deep-ready candidates."""

    async def active_global_equities(self, identity_headers=None):
        return [instrument(1)]

    async def sector_benchmark_contexts(self, ids, identity_headers=None):
        return {}

    def register_global_profile_metadata(self, key, payload):
        return True


class _Clock:
    @staticmethod
    def now(*args):
        return NOW


@pytest.mark.asyncio
async def test_empty_universe_raises_and_preserves_persisted_radar(monkeypatch):
    """C1: candidate_ids=None + empty canonical NSE universe (universe_count==0)
    must raise UniverseUnavailableError and must NOT overwrite an existing
    persisted radar, nor record a new COMPLETED market scan."""
    from app import global_opportunity_cycle as cycle
    from test_global_opportunity_orchestration import setup
    from test_recommendation_lifecycle import snapshot, publish

    service, rows, pairs, store = setup(monkeypatch, 1)

    # Seed a non-empty radar so we can prove it is preserved, not overwritten.
    publish(store, [snapshot()])
    before = store.opportunity_current()
    assert before["generated_at"] is not None, "radar fixture must be seeded"
    before_scan_count = len(store.global_market_scans())

    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator",
                        lambda *a, **k: service)

    source = AsyncMock()
    source.active_global_equities.return_value = []          # empty universe
    source.sector_benchmark_contexts.return_value = {}

    with pytest.raises(UniverseUnavailableError):
        await cycle.run_global_opportunity_cycle(
            service.repository, source, candidate_ids=None, top_n=2,
            identity_headers={"X-AIP-User-Id": "server"})

    # The persisted radar (opportunity_current) is untouched: same generated_at.
    after = store.opportunity_current()
    assert after["generated_at"] == before["generated_at"]
    # No new market scan was persisted for the aborted (empty) cycle.
    assert len(store.global_market_scans()) == before_scan_count


@pytest.mark.asyncio
async def test_non_empty_universe_zero_evaluated_completes_and_publishes_valid_empty_radar(monkeypatch):
    """C2 (DI-9B): candidate_ids=None + NON-empty universe (universe_count>0)
    but zero evaluated/rankable candidates (no candidates reached deep
    analysis -- e.g. all Stage-2 candidates were suppressed before scoring --
    and there are no prior active recommendations to review) must NOT raise
    UniverseUnavailableError. The cycle must complete normally and publish a
    real, valid empty COMPLETED radar: the authoritative universe was
    evaluated successfully, there are simply no qualifying opportunities
    today. This is the exact false-failure this task fixes: production cycle
    221dcf0d-a4e0-46b3-b90b-3f48ad44b23d proved universe_count=2578 with
    Stage-1/Stage-2 both completing, yet evaluated_entries ended up empty and
    the worker incorrectly surfaced UNIVERSE_UNAVAILABLE, leaving the Radar
    unpersisted."""
    from app import global_opportunity_cycle as cycle

    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _FakeOrchestrator)

    # Fresh persistence: no prior recommendations -> no review snapshots.
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    selection = await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=None, top_n=2)

    # The cycle completed normally -- no UniverseUnavailableError, no
    # NO_RANKABLE_CANDIDATES or any other new error code (out of scope here).
    assert selection["status"] == "COMPLETED"
    assert selection["generated_at"] is not None
    assert selection["universe_count"] == 1          # the non-zero universe_count is preserved
    assert selection["best_buy_today"] is None
    assert selection["top_short_term"] == []
    assert selection["top_long_term"] == []
    assert selection["top_exit"] == []
    assert selection["controlled_candidate_set"] is False

    # A real, valid empty radar was actually persisted (not just returned).
    radar = store.global_opportunity_radar()
    assert radar["generated_at"] is not None
    assert radar["source_scan_id"] is not None
    assert radar["best_buy_today"] is None
    assert radar["top_short_term"] == []
    assert radar["top_long_term"] == []
    assert radar["top_exit"] == []

    scans = store.global_market_scans()
    assert len(scans) == 1
    assert scans[0]["status"] == "COMPLETED"
    assert scans[0]["universe_count"] == 1
    assert scans[0]["controlled"] is False

    # The V13 top_selection projection was also published (not skipped).
    current = store.opportunity_current()
    assert current["generated_at"] is not None


@pytest.mark.asyncio
async def test_controlled_candidate_cycle_with_empty_ranking_completes_normally(monkeypatch):
    """C3: a controlled/diagnostic cycle (candidate_ids given) was already
    exempt from both empty-universe guard arms before this task (both arms
    require candidate_ids is None), and remains so after removing ARM2's
    false-failure semantics -- an empty ranking on a controlled cycle simply
    completes, exactly as before. This is a regression guard that DI-9B's
    change to ARM2 does not alter controlled-cycle behavior at all."""
    from app import global_opportunity_cycle as cycle

    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _FakeOrchestrator)

    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    selection = await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=[UUID(int=1)], top_n=2)

    assert selection["status"] == "COMPLETED"
    assert selection["controlled_candidate_set"] is True
    assert selection["best_buy_today"] is None
    assert selection["top_short_term"] == []
    assert selection["top_long_term"] == []
    assert selection["top_exit"] == []

    # Controlled scans are recorded but never publish current global
    # suggestions (persist_global_suggestion_lifecycle short-circuits on
    # scan['controlled']) -- unchanged from prior behavior.
    scans = store.global_market_scans()
    assert len(scans) == 1
    assert scans[0]["controlled"] is True
