"""Event-loop starvation fix regression: run_global_opportunity_cycle's two
previously-direct synchronous persistence/CPU passes --

  * repository.persistence.recommendation_states()
  * the final publish_opportunity_cycle(...) + persist_global_suggestion_
    lifecycle(...) pass (which itself synchronously runs prepare_cycle() /
    RecommendationEngineV1().evaluate(...))

-- now go through the module's existing `_blocking()` offload helper (the
same reuse of ResearchRepository._run_blocking_persistence already used
elsewhere in this file, e.g. for update_cycle_run) instead of running
directly on the asyncio event loop that also serves GET /health.

These tests use a repository double whose `_run_blocking_persistence`
genuinely dispatches to a worker thread -- mirroring the real, production
(Postgres-backed) ResearchRepository._run_blocking_persistence contract --
so thread identity can be observed directly, rather than relying on the
SQLite in-test fallback branch (which runs inline on purpose, by original
design, for the single-thread-affine SQLite adapter). The in-memory SQLite
store used here is given a check_same_thread=False connection so it can
safely be touched from whichever thread each call naturally lands on
(the worker thread for the two offloaded call sites; the caller's thread
for the pre-existing, out-of-scope, still-unoffloaded recommendation_
history() calls) -- mirroring how the real Postgres adapter's connection is
safe to reach from any worker thread. Access stays strictly sequential
throughout (never concurrent), exactly as in production.
"""
import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from types import SimpleNamespace

import pytest

from app import global_opportunity_cycle as cycle
from app.persistence import SqliteResearchPersistence

from test_global_opportunity_radar_contract import (
    _Clock, _FakeSource, _RankingWithEntries, _FakeRankingOrchestrator, _buy_entry,
)

MAIN_THREAD_ID = threading.get_ident()


def _thread_safe_store() -> SqliteResearchPersistence:
    store = SqliteResearchPersistence()
    store._connection = sqlite3.connect(':memory:', check_same_thread=False)
    store._connection.row_factory = sqlite3.Row
    store._connection.execute('PRAGMA foreign_keys = ON')
    store.migrate()
    return store


class _ThreadTrackingRepository:
    """A repository double whose _run_blocking_persistence genuinely offloads
    the operation to a worker thread and records which thread each named
    operation actually ran on."""

    def __init__(self, executor: ThreadPoolExecutor):
        self._executor = executor
        self.persistence = _thread_safe_store()
        self._persistence_worker_lock = RLock()
        self.thread_ids: dict[str, int] = {}

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        label = getattr(operation, '__qualname__', repr(operation))

        def _call():
            self.thread_ids[label] = threading.get_ident()
            return operation(*args, **kwargs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _call)


def _setup(monkeypatch, executor, n=3, top_n=4):
    entries = [_buy_entry(i + 1) for i in range(n)]
    captured = {}
    monkeypatch.setattr(cycle, 'datetime', _Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator',
                        lambda *a, **k: _FakeRankingOrchestrator(entries, captured))
    repo = _ThreadTrackingRepository(executor)
    return repo, repo.persistence, _FakeSource(n), top_n


# A + B --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recommendation_states_and_publish_run_off_the_event_loop_thread(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        repo, store, source, top_n = _setup(monkeypatch, executor)

        selection = await cycle.run_global_opportunity_cycle(repo, source, top_n=top_n)

        # A: recommendation_states() executed on a worker thread, not the
        # caller's (event-loop) thread.
        states_thread = repo.thread_ids.get('OpportunityPersistenceMixin.recommendation_states')
        assert states_thread is not None, "recommendation_states was not routed through the offload boundary"
        assert states_thread != MAIN_THREAD_ID

        # B: the publish closure (publish_opportunity_cycle + persist_global_
        # suggestion_lifecycle, including prepare_cycle()/RecommendationEngineV1)
        # also executed on a worker thread, not the caller's thread.
        publish_thread = next((tid for label, tid in repo.thread_ids.items() if label.endswith('_publish')), None)
        assert publish_thread is not None, "the publish operation was not routed through the offload boundary"
        assert publish_thread != MAIN_THREAD_ID

        # Sanity: the cycle still produced a real, correctly published result.
        assert selection['status'] == 'COMPLETED'
        assert len(selection['top_short_term']) == 3
    finally:
        executor.shutdown(wait=True)


# C --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cycle_waits_for_publication_to_finish_before_returning(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        repo, store, source, top_n = _setup(monkeypatch, executor)
        completed = {'publish': False}
        real_publish = store.publish_opportunity_cycle

        def _slow_publish(*args, **kwargs):
            import time
            time.sleep(0.05)  # give a fire-and-forget bug a real window to race against
            result = real_publish(*args, **kwargs)
            completed['publish'] = True
            return result

        monkeypatch.setattr(store, 'publish_opportunity_cycle', _slow_publish)

        selection = await cycle.run_global_opportunity_cycle(repo, source, top_n=top_n)

        # Not fire-and-forget: publish_opportunity_cycle had actually finished
        # by the time the cycle returned.
        assert completed['publish'] is True
        # Not parallel publication: exactly one publish ran, and the returned
        # selection matches what is actually now persisted.
        radar = store.global_opportunity_radar()
        assert radar['top_short_term_count'] == len(selection['top_short_term']) == 3
    finally:
        executor.shutdown(wait=True)


# D ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recommendation_states_exception_propagates_not_swallowed(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        repo, store, source, top_n = _setup(monkeypatch, executor)

        def _boom():
            raise RuntimeError("RECOMMENDATION_STATES_BOOM")
        monkeypatch.setattr(store, 'recommendation_states', _boom)

        with pytest.raises(RuntimeError, match="RECOMMENDATION_STATES_BOOM"):
            await cycle.run_global_opportunity_cycle(repo, source, top_n=top_n)
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_publish_opportunity_cycle_exception_propagates_not_swallowed(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        repo, store, source, top_n = _setup(monkeypatch, executor)

        def _boom(*args, **kwargs):
            raise RuntimeError("PUBLISH_BOOM")
        monkeypatch.setattr(store, 'publish_opportunity_cycle', _boom)

        with pytest.raises(RuntimeError, match="PUBLISH_BOOM"):
            await cycle.run_global_opportunity_cycle(repo, source, top_n=top_n)
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_persist_global_suggestion_lifecycle_exception_propagates_not_swallowed(monkeypatch):
    # The second call inside the offloaded publish closure must also surface
    # its exception, proving the whole closure -- not just the first call --
    # is awaited/observed by the caller.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        repo, store, source, top_n = _setup(monkeypatch, executor)

        def _boom(*args, **kwargs):
            raise RuntimeError("LIFECYCLE_BOOM")
        monkeypatch.setattr(store, 'persist_global_suggestion_lifecycle', _boom)

        with pytest.raises(RuntimeError, match="LIFECYCLE_BOOM"):
            await cycle.run_global_opportunity_cycle(repo, source, top_n=top_n)
    finally:
        executor.shutdown(wait=True)


# E ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blocking_helper_falls_back_to_inline_when_boundary_absent(monkeypatch):
    """SimpleNamespace repository doubles used throughout the existing test
    suite (e.g. test_global_opportunity_radar_contract.py) have no
    _run_blocking_persistence at all; _blocking() must still work inline,
    exactly like before this fix, so those existing controlled-cycle tests
    remain valid without modification."""
    entries = [_buy_entry(1)]
    captured = {}
    monkeypatch.setattr(cycle, 'datetime', _Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator',
                        lambda *a, **k: _FakeRankingOrchestrator(entries, captured))
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    selection = await cycle.run_global_opportunity_cycle(repo, _FakeSource(1), top_n=4)

    assert selection['status'] == 'COMPLETED'
    assert len(selection['top_short_term']) == 1
