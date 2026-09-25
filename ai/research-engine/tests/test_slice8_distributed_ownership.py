"""Slice 8: scheduler / distributed ownership across replicas.

Deployment evidence: research-engine is scalingClass NEEDS_DISTRIBUTED_LOCK and
inherits global.rolloutStrategy RollingUpdate maxSurge=1 (values.yaml; only
values-azure.yaml sets Recreate), so during every rollout two pods overlap.
Each 'pod' here is its own repository + worker + connection to ONE shared
SQLite database file; process-local worker.active cannot see the other pod.
"""
from __future__ import annotations

import asyncio
from threading import RLock
from types import SimpleNamespace

import pytest

from app.global_opportunity_scheduler import GlobalOpportunityScheduler
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from test_global_opportunity_scheduler import _market_hours_now


def _pod(db_path, runner, lease=0.3):
    store = SqliteResearchPersistence(db_path)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    return store, OpportunityCycleWorker(repo, runner, lease_seconds=lease, poll_seconds=0.05)


def _runs(store):
    return store._connection.execute("SELECT cycle_id, status FROM global_opportunity_cycle_run").fetchall()


def _blocking_runner(gate, release, calls):
    async def runner(**kwargs):
        calls.append(kwargs.get("cycle_id"))
        gate.set()
        await release.wait()
        return {"cycle_id": kwargs["cycle_id"], "universe_count": 1}
    return runner


@pytest.mark.asyncio
async def test_scheduler_tick_on_other_pod_does_not_create_second_cycle(tmp_path):
    db = tmp_path / "research.db"
    gate, release, calls = asyncio.Event(), asyncio.Event(), []
    store_a, worker_a = _pod(db, _blocking_runner(gate, release, calls))
    store_b, worker_b = _pod(db, _blocking_runner(asyncio.Event(), release, calls))
    first = worker_a.submit({"top_n": 4, "shortlist_limit": 25, "candidate_ids": None})
    await asyncio.wait_for(gate.wait(), 5)
    scheduler_b = GlobalOpportunityScheduler(worker=worker_b, persistence=store_b, clock=_market_hours_now)
    assert not scheduler_b.active  # pod B's process-local view sees nothing...
    for _ in range(3):  # ...multiple ticks on pod B coalesce onto pod A's cycle
        scheduler_b._last_market_hours_run = None
        scheduler_b._maybe_submit()
    assert len(_runs(store_a)) == 1 and _runs(store_a)[0][0] == first["cycle_id"]
    assert calls == [first["cycle_id"]]
    release.set()
    await asyncio.wait_for(worker_a.queue.join(), 5)
    await worker_a.close()
    await worker_b.close()
    assert store_b.cycle_run(first["cycle_id"])["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_multiple_ticks_same_pod_coalesce(tmp_path):
    gate, release, calls = asyncio.Event(), asyncio.Event(), []
    store, worker = _pod(tmp_path / "r.db", _blocking_runner(gate, release, calls))
    scheduler = GlobalOpportunityScheduler(worker=worker, persistence=store, clock=_market_hours_now)
    ids = set()
    for _ in range(5):
        scheduler._last_market_hours_run = None
        scheduler._maybe_submit()
        ids.update(r[0] for r in _runs(store))
        await asyncio.sleep(0)
    assert len(ids) == 1 and len(_runs(store)) == 1
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()


@pytest.mark.asyncio
async def test_concurrent_creation_from_two_pods_yields_one_active_cycle(tmp_path):
    db = tmp_path / "race.db"
    store_a = SqliteResearchPersistence(db)
    store_b = SqliteResearchPersistence(db)
    run_a, created_a = store_a.create_cycle_run("cycle-a", {"top_n": 4})
    run_b, created_b = store_b.create_cycle_run("cycle-b", {"top_n": 4})
    assert created_a and not created_b and run_b["cycle_id"] == "cycle-a"
    assert len(_runs(store_a)) == 1  # the loser's run row rolled back with its slot insert
    assert store_a.claim_cycle_run("cycle-a", "pod-a", lease_seconds=60)
    assert not store_b.claim_cycle_run("cycle-a", "pod-b", lease_seconds=60)  # live lease: fenced


@pytest.mark.asyncio
async def test_restart_recovery_does_not_race_new_submission(tmp_path):
    db = tmp_path / "recover.db"
    # Pod A accepted a cycle and died (no worker running, lease never taken).
    store_a = SqliteResearchPersistence(db)
    run, _ = store_a.create_cycle_run("orphan", {"top_n": 4, "correlation_id": "x"})
    store_a.record_opportunity_job({"cycle_id": "orphan", "status": "ACCEPTED", "updated_at": "2026-09-22T00:00:00+00:00",
                                    "parameters": {"top_n": 4}})
    release, calls = asyncio.Event(), []
    release.set()
    store_b, worker_b = _pod(db, _blocking_runner(asyncio.Event(), release, calls))
    worker_b.start()                       # recovery enqueues the SAME cycle ...
    duplicate = worker_b.submit({"top_n": 4})   # ... and a racing submission coalesces onto it
    assert duplicate["cycle_id"] == "orphan" and duplicate.get("coalesced")
    await asyncio.wait_for(worker_b.queue.join(), 5)
    await worker_b.close()
    assert calls == ["orphan"]
    assert [r[0] for r in _runs(store_b)] == ["orphan"]
    assert store_b.cycle_run("orphan")["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_dead_owner_is_taken_over_after_lease_expiry_only(tmp_path):
    db = tmp_path / "takeover.db"
    gate, never, calls = asyncio.Event(), asyncio.Event(), []
    store_a, worker_a = _pod(db, _blocking_runner(gate, never, calls), lease=0.3)
    job = worker_a.submit({"top_n": 4})
    await asyncio.wait_for(gate.wait(), 5)
    for task in [t for t in asyncio.all_tasks() if t.get_name().startswith("opportunity-lease-")]:
        task.cancel()                      # pod A "dies": heartbeat stops, lease not released
    await asyncio.sleep(0.05)
    release, calls_b = asyncio.Event(), []
    release.set()
    store_b, worker_b = _pod(db, _blocking_runner(asyncio.Event(), release, calls_b), lease=0.3)
    worker_b.start()
    await asyncio.sleep(0.1)
    assert calls_b == []                   # lease still live: no takeover yet
    await asyncio.sleep(0.5)               # lease expires -> idle poll takes over the SAME cycle
    await asyncio.wait_for(worker_b.queue.join(), 5)
    await worker_b.close()
    assert calls_b == [job["cycle_id"]]
    assert store_b.cycle_run(job["cycle_id"])["status"] == "COMPLETED"
    assert store_b.cycle_run(job["cycle_id"])["resume_count"] == 1
    # The zombie pod A finally stops: it must not overwrite the completed status.
    await worker_a.close()
    status = next(j for j in store_b.opportunity_jobs() if j["cycle_id"] == job["cycle_id"])
    assert status["status"] == "COMPLETED"
    assert store_b.cycle_run(job["cycle_id"])["status"] == "COMPLETED"
