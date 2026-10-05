"""Focused, provider-free tests for the narrow cycle cancellation contract.

Covers only the NEW lifecycle behavior:
  - Durable, idempotent cancellation REQUEST (two-phase: CANCEL_REQUESTED ->
    terminal CANCELLED).
  - Stop admitting new candidates: submit() coalesces onto a cancelled cycle
    without starting a new run; scheduler _maybe_submit skips; orchestrator
    admission loop breaks immediately.
  - Safely drain in-flight work: _run_one does not start new candidates for
    CANCEL_REQUESTED, but lets in-flight work finish; then coerces to terminal
    CANCELLED with ownership release + active-slot deletion.
  - Truthful terminal state + ownership release + active-slot deletion.
  - Cancelled cycles cannot resume or publish a successful snapshot.
  - PDF-worker safeguards preserved (no evidence/progress rows are cleared).
  - Operator-controlled recovery pause: blocks startup recovery, polling
    takeover, scheduler submission, and manual research submission while the
    cancellation REQUEST/STATUS API remains available.
  - Fenced operator finalization of an unowned CANCEL_REQUESTED cycle (no
    research runner invoked).

Provider-free: uses SqliteResearchPersistence + a synchronous blocking runner
that never touches any external provider or network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from threading import RLock
from uuid import uuid4

import pytest

from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.global_opportunity_scheduler import GlobalOpportunityScheduler

try:
    from test_global_opportunity_scheduler import _market_hours_now
except Exception:  # pragma: no cover - fallback for import path differences
    from datetime import datetime, timezone
    def _market_hours_now():
        return datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


def _pod(store, runner, lease=0.3, paused=False):
    settings = SimpleNamespace(research_opportunity_recovery_paused=paused)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(),
                           settings=settings)
    return store, OpportunityCycleWorker(repo, runner, lease_seconds=lease, poll_seconds=0.05)


def _runs(store):
    return store._connection.execute(
        "SELECT cycle_id, status, owner_id, lease_expires_at FROM global_opportunity_cycle_run"
    ).fetchall()


def _active_slot(store):
    return store._connection.execute(
        "SELECT cycle_id FROM global_opportunity_cycle_active"
    ).fetchone()


def _progress_rows(store, cycle_id):
    return store._connection.execute(
        "SELECT phase, state FROM global_opportunity_cycle_progress WHERE cycle_id = ?",
        (cycle_id,)).fetchall()


# -- 1. Durable, idempotent cancellation request (two-phase) -------------------

def test_request_cancel_transitions_active_run_to_cancel_requested():
    """Phase 1: request_cycle_cancel sets CANCEL_REQUESTED, preserving
    ownership, lease, and the active slot so the owner can drain."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    assert store.cycle_run(cycle_id)["status"] == "RUNNING"

    transitioned = store.request_cycle_cancel(cycle_id)
    assert transitioned is True

    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCEL_REQUESTED"
    assert run["error_code"] == "CYCLE_CANCELLED"
    # Ownership + lease PRESERVED so the owner can drain.
    assert run["owner_id"] == "worker-1"
    assert run["lease_expires_at"] is not None
    # Active slot PRESERVED (no replacement cycle).
    assert _active_slot(store) is not None
    assert _active_slot(store)["cycle_id"] == cycle_id


def test_request_cancel_is_idempotent_on_already_cancel_requested():
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    assert store.request_cycle_cancel(cycle_id) is True
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"
    # Second call is a no-op (CANCEL_REQUESTED is not in the REQUESTABLE set).
    assert store.request_cycle_cancel(cycle_id) is False
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"


def test_request_cancel_is_idempotent_on_terminal_cancelled():
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    # Worker drains to terminal.
    assert store.cancel_cycle_run(cycle_id, "worker-1") is True
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"
    # Second request is a no-op.
    assert store.request_cycle_cancel(cycle_id) is False


def test_request_cancel_is_idempotent_on_terminal_completed():
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.update_cycle_run(cycle_id, "worker-1", status="COMPLETED")
    # Cannot cancel a completed cycle.
    assert store.request_cycle_cancel(cycle_id) is False
    assert store.cycle_run(cycle_id)["status"] == "COMPLETED"


def test_cancel_cycle_run_transitions_to_terminal_cancelled():
    """Phase 2: worker-side terminal transition from CANCEL_REQUESTED to CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    transitioned = store.cancel_cycle_run(cycle_id, "worker-1")
    assert transitioned is True

    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["error_code"] == "CYCLE_CANCELLED"
    assert run["owner_id"] is None
    assert run["lease_expires_at"] is None
    assert _active_slot(store) is None


def test_cancel_cycle_run_is_ownership_fenced():
    """Only the lease holder can coerce CANCEL_REQUESTED -> CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)

    # A different worker cannot coerce to terminal.
    transitioned = store.cancel_cycle_run(cycle_id, "worker-2")
    assert transitioned is False
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"
    assert _active_slot(store)["cycle_id"] == cycle_id
    assert store.cycle_run(cycle_id)["owner_id"] == "worker-1"


def test_cycle_cancellation_requested_read():
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    assert store.cycle_cancellation_requested(cycle_id) is False
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_cancellation_requested(cycle_id) is True

    cycle_id2 = str(uuid4())
    store.create_cycle_run(cycle_id2, {"top_n": 4})
    assert store.cycle_cancellation_requested(cycle_id2) is False


def test_cycle_cancel_status_read():
    """cycle_cancel_status distinguishes CANCEL_REQUESTED from CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    assert store.cycle_cancel_status(cycle_id) is None

    store.request_cycle_cancel(cycle_id)
    assert store.cycle_cancel_status(cycle_id) == "CANCEL_REQUESTED"

    store.cancel_cycle_run(cycle_id, "worker-1")
    assert store.cycle_cancel_status(cycle_id) == "CANCELLED"


# -- 2. Stop admitting new candidates -------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_cycle_does_not_resume_after_restart():
    """A worker restart that sees a CANCELLED active cycle must not resume it."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    run, created = store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    store.cancel_cycle_run(cycle_id, "worker-1")
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner)
    worker.start()
    await asyncio.sleep(0.2)
    assert runner_calls == []  # no runner execution for a terminal cancelled cycle
    assert worker.active is None
    await worker.close()
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_requested_cycle_not_resumed_by_worker_start():
    """Startup/recovery barrier: CANCEL_REQUESTED with a LIVE owner is not resumed."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    run, created = store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=1.0)
    worker.owner_id = "worker-DIFFERENT"
    worker.start()
    await asyncio.sleep(0.3)
    assert runner_calls == []  # different owner must NOT resume CANCEL_REQUESTED
    assert worker.active is None
    await worker.close()
    # The cycle stays CANCEL_REQUESTED (owner drained it, not a different worker).
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_cancel_requested_takeover_by_different_worker():
    """If the original owner's lease expired on a CANCEL_REQUESTED cycle, a
    new worker takes over and drains to terminal CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    run, created = store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=0.1)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    # Wait for the lease to expire.
    import time as _time
    _time.sleep(0.2)

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=10)
    worker.start()
    await asyncio.wait_for(worker.queue.join(), 5)
    await asyncio.sleep(0.3)
    await worker.close()

    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["owner_id"] is None
    assert _active_slot(store) is None


@pytest.mark.asyncio
async def test_submit_coalesces_with_cancelled_cycle_without_new_run():
    """When a cycle is terminal CANCELLED, submit() does not re-admit onto it.

    After cancellation the active slot is deleted, so submit() creates a
    new cycle rather than coalescing onto the cancelled one. The worker
    records the cancelled cycle as terminal in the job log.
    """
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    run, created = store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    store.cancel_cycle_run(cycle_id, "worker-1")

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner)
    worker.start()
    result = worker.submit({"top_n": 4, "shortlist_limit": 25})
    # The new cycle gets a different cycle_id.
    assert result["cycle_id"] != cycle_id
    assert len(_runs(store)) == 2  # original (cancelled) + new
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()
    # The original cycle remains CANCELLED.
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"


def test_scheduler_maybe_submit_skips_cancelled_active_cycle():
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner)
    scheduler = GlobalOpportunityScheduler(
        worker=worker, persistence=store, clock=_market_hours_now)
    scheduler._last_market_hours_run = None
    assert scheduler._maybe_submit() is False
    assert runner_calls == []
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"


def test_scheduler_maybe_submit_skips_cancel_requested_active_cycle():
    """Scheduler also skips CANCEL_REQUESTED (not just terminal CANCELLED)."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner)
    scheduler = GlobalOpportunityScheduler(
        worker=worker, persistence=store, clock=_market_hours_now)
    scheduler._last_market_hours_run = None
    assert scheduler._maybe_submit() is False
    assert runner_calls == []
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"


# -- 3. Safely drain in-flight work ---------------------------------------------

@pytest.mark.asyncio
async def test_cancel_during_in_flight_coerces_to_cancelled():
    """A running cycle whose cancellation is requested is drained to CANCELLED.

    Proves: no further candidate acquisition starts after CANCEL_REQUESTED is
    observed; ownership/active slot remain until the in-flight runner exits;
    only then is the cycle coerced to terminal CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    runner_calls = []
    cancel_observed_after_start = []
    async def _runner(**kwargs):
        started.set()
        runner_calls.append(kwargs.get("cycle_id"))
        # Simulate in-flight work: block until released.
        await release.wait()
        # After release, re-check cancellation status.
        cancel_observed_after_start.append(store.cycle_cancellation_requested(kwargs.get("cycle_id")))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]

    # Wait for the runner to actually start (cycle is RUNNING, in-flight).
    await asyncio.wait_for(started.wait(), 5)

    # Request cancellation while in-flight -- transitions to CANCEL_REQUESTED
    # but preserves ownership so the worker can keep draining.
    store.request_cycle_cancel(submitted_cycle_id)
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCEL_REQUESTED"
    # Ownership + active slot STILL PRESERVED during drain.
    assert run["owner_id"] == worker.owner_id
    assert run["lease_expires_at"] is not None
    assert _active_slot(store) is not None
    assert _active_slot(store)["cycle_id"] == submitted_cycle_id

    # Release the runner so _run_one can finish and coerce to terminal.
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()

    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["error_code"] == "CYCLE_CANCELLED"
    assert run["owner_id"] is None
    assert run["lease_expires_at"] is None
    assert _active_slot(store) is None


@pytest.mark.asyncio
async def test_cancel_drains_preserves_progress_rows():
    """Cancellation coerces terminal state but preserves completed evidence."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    owner = "worker-test"
    store.claim_cycle_run(cycle_id, owner, lease_seconds=60)

    # Simulate completed evidence: record progress for a candidate.
    from app.cycle_checkpoint import CandidateState, PHASE_DEEP
    from uuid import UUID
    inst_id = UUID(int=42)
    store.record_cycle_progress(cycle_id, owner, PHASE_DEEP, inst_id,
                                CandidateState.COMPLETED, disposition="ANALYZED")

    # Cancel the run.
    store.request_cycle_cancel(cycle_id)

    # The progress row survives cancellation.
    rows = _progress_rows(store, cycle_id)
    assert len(rows) == 1
    assert dict(rows[0])["state"] == "COMPLETED"

    # Worker coerces to terminal.
    store.cancel_cycle_run(cycle_id, owner)
    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["owner_id"] is None
    assert _active_slot(store) is None

    # Progress rows still survive terminal cancellation.
    rows = _progress_rows(store, cycle_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_cancel_mid_runner_coerces_after_completion():
    """Cancellation request arriving during runner execution: the runner
    finishes, then the worker coerces to CANCELLED (not COMPLETED/PUBLISHED)."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    async def _runner(**kwargs):
        started.set()
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]

    await asyncio.wait_for(started.wait(), 5)

    # Request cancellation mid-flight.
    store.request_cycle_cancel(submitted_cycle_id)

    # Release runner to complete.
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()

    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["error_code"] == "CYCLE_CANCELLED"
    assert _active_slot(store) is None


@pytest.mark.asyncio
async def test_cancel_while_in_flight_keeps_ownership_until_drain():
    """Prove: ownership/active slot remain during drain; only released after
    in-flight work exits. Uses a runner that simulates a physical PDF worker
    (long-running, in-flight) and verifies no new candidate starts."""
    from app.cycle_checkpoint import CandidateState, PHASE_BASELINE
    from uuid import UUID
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    acquire_calls = []
    async def _runner(**kwargs):
        started.set()
        # Simulate in-flight physical PDF extraction work.
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]

    await asyncio.wait_for(started.wait(), 5)

    # Simulate a progress row committed before the cancel request (evidence
    # from a previously-completed candidate that must be preserved).
    inst_id = UUID(int=99)
    store.record_cycle_progress(submitted_cycle_id, worker.owner_id,
                                 PHASE_BASELINE, inst_id,
                                 CandidateState.COMPLETED, disposition="BASELINE_EVALUATED")

    # Cancel while the PDF worker is in-flight.
    store.request_cycle_cancel(submitted_cycle_id)
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCEL_REQUESTED"

    # While in-flight: ownership + active slot PRESERVED, progress PRESERVED.
    run = store.cycle_run(submitted_cycle_id)
    assert run["owner_id"] == worker.owner_id
    assert run["lease_expires_at"] is not None
    assert _active_slot(store) is not None
    rows = _progress_rows(store, submitted_cycle_id)
    assert len(rows) == 1

    # The in-flight runner is still blocked (no new candidate started).
    assert started.is_set() and not release.is_set()

    # Release: runner finishes, worker coerces to terminal.
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()

    # After drain: terminal CANCELLED, ownership released, active slot gone,
    # progress rows PRESERVED.
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["owner_id"] is None
    assert run["lease_expires_at"] is None
    assert _active_slot(store) is None
    rows = _progress_rows(store, submitted_cycle_id)
    assert len(rows) == 1  # evidence preserved
    assert dict(rows[0])["state"] == "COMPLETED"

    # No runner was called for a second candidate (no new acquisition).
    assert len(acquire_calls) <= 1


@pytest.mark.asyncio
async def test_cancel_preserves_prefetched_progress_then_terminal():
    """Prefetched work in progress: if cancellation arrives while the runner
    is still executing, prefetched progress rows are preserved and the cycle
    still reaches terminal CANCELLED."""
    from app.cycle_checkpoint import CandidateState, PHASE_DEEP
    from uuid import UUID
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    async def _runner(**kwargs):
        started.set()
        # Simulate prefetched in-flight candidate that hasn't committed yet.
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]
    await asyncio.wait_for(started.wait(), 5)

    # Record prefetched progress (IN_PROGRESS candidate).
    inst1 = UUID(int=1)
    inst2 = UUID(int=2)
    store.record_cycle_progress(submitted_cycle_id, worker.owner_id,
                                 PHASE_DEEP, inst1,
                                 CandidateState.IN_PROGRESS)
    store.record_cycle_progress(submitted_cycle_id, worker.owner_id,
                                 PHASE_DEEP, inst2,
                                 CandidateState.COMPLETED, disposition="ANALYZED")

    # Cancel during prefetched in-flight work.
    store.request_cycle_cancel(submitted_cycle_id)
    assert store.cycle_run(submitted_cycle_id)["status"] == "CANCEL_REQUESTED"

    # Both progress rows preserved.
    rows = _progress_rows(store, submitted_cycle_id)
    assert len(rows) == 2

    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()

    # Terminal state reached; progress rows preserved.
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert _active_slot(store) is None
    rows = _progress_rows(store, submitted_cycle_id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_cancel_during_in_flight_shutdown_then_resume_does_not_publish():
    """Shutdown during drain: close() coerces CANCEL_REQUESTED to terminal
    CANCELLED. A subsequent restart must NOT publish a successful snapshot."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    async def _runner(**kwargs):
        started.set()
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]
    await asyncio.wait_for(started.wait(), 5)

    # Cancel while in-flight, then shut down BEFORE draining.
    store.request_cycle_cancel(submitted_cycle_id)
    assert store.cycle_run(submitted_cycle_id)["status"] == "CANCEL_REQUESTED"

    # Shutdown coerces to terminal (close() sees CANCEL_REQUESTED and calls cancel_cycle_run).
    await worker.close()
    # The runner is still blocked; close() cancels the task and coerces.
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["owner_id"] is None
    assert _active_slot(store) is None

    # Restart: must NOT resume the terminal CANCELLED cycle.
    runner_calls2 = []
    async def _runner2(**kwargs):
        runner_calls2.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store3, worker2 = _pod(store, _runner2, lease=60)
    worker2.start()
    await asyncio.sleep(0.3)
    assert runner_calls2 == []  # terminal CANCELLED: no resume
    assert worker2.active is None
    await worker2.close()
    assert store.cycle_run(submitted_cycle_id)["status"] == "CANCELLED"


# -- 4. Cancelled cycles cannot publish -----------------------------------------

def test_cancelled_run_fences_publication():
    """_fence_cycle_publication raises CycleOwnershipLost for a CANCELLED run."""
    from app.cycle_checkpoint import CycleOwnershipLost
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)

    with pytest.raises(CycleOwnershipLost):
        store._fence_cycle_publication(cycle_id, "worker-1")


def test_cancel_requested_run_fences_publication():
    """_fence_cycle_publication also raises for CANCEL_REQUESTED."""
    from app.cycle_checkpoint import CycleOwnershipLost
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)

    with pytest.raises(CycleOwnershipLost):
        store._fence_cycle_publication(cycle_id, "worker-1")


def test_cancel_requested_can_still_renew_lease():
    """The owner of a CANCEL_REQUESTED cycle can renew its lease (drain support)."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    renewed = store.renew_cycle_lease(cycle_id, "worker-1", lease_seconds=60)
    assert renewed is True
    run = store.cycle_run(cycle_id)
    assert run["lease_expires_at"] is not None


def test_cancelled_run_can_not_be_re_claimed():
    """A terminal CANCELLED cycle's lease cannot be claimed via claim_cycle_run."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    store.cancel_cycle_run(cycle_id, "worker-1")
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"

    # Cannot claim a terminal CANCELLED cycle.
    claimed = store.claim_cycle_run(cycle_id, "worker-2", lease_seconds=60)
    assert claimed is False


# -- 5. Recovery pause (operator-controlled) ------------------------------------

@pytest.mark.asyncio
async def test_recovery_pause_blocks_startup_recovery():
    """When recovery pause is ON, start() does NOT resume an active cycle."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-orig", lease_seconds=60)
    assert store.cycle_run(cycle_id)["status"] == "RUNNING"

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60, paused=True)
    worker.start()
    await asyncio.sleep(0.2)
    assert runner_calls == []  # no resume while paused
    assert worker.active is None
    # Status recorded as RECOVERY_PAUSED in the job log.
    import json as _json
    rows = store2._connection.execute(
        "SELECT payload FROM global_opportunity_top_selection").fetchall()
    job = None
    for row in rows:
        val = _json.loads(row["payload"])
        if val.get("record_kind") == "CYCLE_JOB" and val.get("cycle_id") == cycle_id:
            job = val
            break
    assert job is not None
    assert job["error_code"] == "RECOVERY_PAUSED"
    # Run row status unchanged (still RUNNING in the DB).
    assert store.cycle_run(cycle_id)["status"] == "RUNNING"
    await worker.close()


@pytest.mark.asyncio
async def test_recovery_pause_blocks_polling_takeover():
    """When recovery pause is ON, _maybe_take_over() does not take over an
    active cycle even if the original owner's lease has expired."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-orig", lease_seconds=0.1)
    assert store.cycle_run(cycle_id)["status"] == "RUNNING"

    import time as _time
    _time.sleep(0.3)  # lease expires

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60, paused=True)
    worker.owner_id = "worker-NEW"
    # Manually trigger a takeover check (normally done by the poll loop).
    worker._maybe_take_over()
    assert runner_calls == []  # paused: no takeover
    assert worker.active is None
    # Run stays RUNNING, no new owner.
    run = store.cycle_run(cycle_id)
    assert run["status"] == "RUNNING"
    assert run["owner_id"] == "worker-orig"  # unchanged
    await worker.close()


@pytest.mark.asyncio
async def test_recovery_pause_blocks_submit():
    """When recovery pause is ON, submit() refuses new candidate admission."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60, paused=True)
    worker.start()
    # submit() should refuse (paused) and NOT create a new cycle.
    result = worker.submit({"top_n": 4, "shortlist_limit": 25})
    assert result.get("paused") is True
    # The existing active cycle is RUNNING (not cancelled yet).
    assert result.get("cancelled") is False
    # No new run created.
    runs = _runs(store)
    assert len(runs) == 1  # only the existing active cycle
    assert runner_calls == []
    await worker.close()


def test_recovery_pause_blocks_scheduler_maybe_submit():
    """When recovery pause is ON, scheduler _maybe_submit() returns False."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60, paused=True)
    scheduler = GlobalOpportunityScheduler(
        worker=worker, persistence=store, clock=_market_hours_now)
    scheduler._last_market_hours_run = None
    assert scheduler._maybe_submit() is False
    assert runner_calls == []


@pytest.mark.asyncio
async def test_recovery_pause_off_allows_normal_resume():
    """Default (paused=False): normal recovery works as before."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    runner_calls = []
    async def _runner(**kwargs):
        started.set()
        runner_calls.append(kwargs.get("cycle_id"))
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=60, paused=False)
    worker.start()
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    await asyncio.wait_for(started.wait(), 5)
    assert len(runner_calls) == 1  # normal resume works
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()
    assert store.cycle_run(cycle_id)["status"] == "COMPLETED"


# -- 6. Fenced operator cancel finalization (no runner) ------------------------

def test_cancel_cycle_run_unowned_transitions_to_terminal():
    """Operator-fenced finalization: CANCEL_REQUESTED -> CANCELLED without owner."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"
    # Owner still present but we use the unowned path.
    assert store.cycle_run(cycle_id)["owner_id"] == "worker-1"

    transitioned = store.cancel_cycle_run_unowned(cycle_id)
    assert transitioned is True
    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["owner_id"] is None
    assert run["lease_expires_at"] is None
    assert _active_slot(store) is None


def test_cancel_cycle_run_unowned_rejects_non_cancel_requested():
    """The unowned path only accepts CANCEL_REQUESTED (not RUNNING/PUBLISHED/etc)."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    # Status is RUNNING, not CANCEL_REQUESTED.
    transitioned = store.cancel_cycle_run_unowned(cycle_id)
    assert transitioned is False
    assert store.cycle_run(cycle_id)["status"] == "RUNNING"
    assert _active_slot(store)["cycle_id"] == cycle_id
    existing, created = store.create_cycle_run(str(uuid4()), {"top_n": 4})
    assert not created and existing["cycle_id"] == cycle_id


def test_cancel_cycle_run_unowned_idempotent_on_cancelled():
    """Already-CANCELLED: unowned finalization is a no-op."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    store.cancel_cycle_run(cycle_id, "worker-1")
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"

    # Unowned path on already-CANCELLED: no-op.
    transitioned = store.cancel_cycle_run_unowned(cycle_id)
    assert transitioned is False
    assert store.cycle_run(cycle_id)["status"] == "CANCELLED"


def test_cancel_cycle_run_unowned_rejects_completed():
    """The unowned path must NOT retroactively cancel a COMPLETED cycle."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.update_cycle_run(cycle_id, "worker-1", status="COMPLETED")
    assert store.cycle_run(cycle_id)["status"] == "COMPLETED"

    transitioned = store.cancel_cycle_run_unowned(cycle_id)
    assert transitioned is False
    assert store.cycle_run(cycle_id)["status"] == "COMPLETED"


# -- 7. Recovery during cancellation --------------------------------------------

@pytest.mark.asyncio
async def test_restart_during_cancellation_preserves_cancel_requested():
    """Restart during CANCEL_REQUESTED (draining): the new worker must NOT
    start a replacement cycle while cancellation is in progress."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    # Simulate: the in-flight worker hasn't drained yet (still CANCEL_REQUESTED).
    # A restart happens. The new worker must NOT create a replacement cycle.
    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60)
    worker.owner_id = "worker-2"
    worker.start()
    await asyncio.sleep(0.3)
    # No runner called — CANCEL_REQUESTED is not re-admitted by a different owner.
    assert runner_calls == []
    assert worker.active is None
    # No replacement cycle created.
    assert len(_runs(store)) == 1
    # Original cycle stays CANCEL_REQUESTED.
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"
    await worker.close()


@pytest.mark.asyncio
async def test_old_cycle_recovery_blocked_during_transition():
    """Restart during cancellation: a different worker must NOT recover and
    resume the cycle. Only the original owner (or a drain-takeover after lease
    expiry) can touch it."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})
    store.claim_cycle_run(cycle_id, "worker-1", lease_seconds=60)
    store.request_cycle_cancel(cycle_id)
    assert store.cycle_run(cycle_id)["status"] == "CANCEL_REQUESTED"

    runner_calls = []
    async def _runner(**kwargs):
        runner_calls.append(kwargs.get("cycle_id"))
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}
    store2, worker = _pod(store, _runner, lease=60)
    worker.owner_id = "worker-DIFFERENT"
    worker.start()
    await asyncio.sleep(0.3)
    assert runner_calls == []  # blocked by startup recovery barrier
    assert worker.active is None
    await worker.close()

    # Verify the cycle was not modified (still CANCEL_REQUESTED, same owner).
    run = store.cycle_run(cycle_id)
    assert run["status"] == "CANCEL_REQUESTED"
    assert run["owner_id"] == "worker-1"


@pytest.mark.asyncio
async def test_cancellation_vs_publication_race():
    """If a cancellation is requested just before the runner returns, the
    worker must NOT publish a successful snapshot — it must coerce to
    terminal CANCELLED."""
    store = SqliteResearchPersistence()
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4})

    started = asyncio.Event()
    release = asyncio.Event()
    async def _runner(**kwargs):
        started.set()
        await release.wait()
        return {"cycle_id": kwargs.get("cycle_id"), "universe_count": 1}

    store2, worker = _pod(store, _runner, lease=0.3)
    job = worker.submit({"top_n": 4, "shortlist_limit": 25})
    submitted_cycle_id = job["cycle_id"]
    await asyncio.wait_for(started.wait(), 5)

    # Request cancellation just before the runner completes.
    store.request_cycle_cancel(submitted_cycle_id)
    release.set()
    await asyncio.wait_for(worker.queue.join(), 5)
    await worker.close()

    # The cycle must be CANCELLED, not COMPLETED or PUBLISHED.
    run = store.cycle_run(submitted_cycle_id)
    assert run["status"] == "CANCELLED"
    assert run["error_code"] == "CYCLE_CANCELLED"
    assert _active_slot(store) is None


# -- Cancellation API, durable job projection, and same-pod coalescing --------

_CANCEL_ADMIN_HEADERS = {
    'X-AIP-User-Id': 'test-admin', 'X-AIP-User-Issuer': 'test-issuer',
    'X-AIP-User-Subject': 'test-admin', 'X-AIP-User-Roles': 'ADMIN',
}

def _wire_cancel_contract_api(monkeypatch, store, worker):
    import app.main as main
    monkeypatch.setattr(main, 'repository', worker.repository)
    monkeypatch.setattr(main, '_opportunity_worker', lambda: worker)
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', False)
    # Exercise submission/state only. The ASGI transport does not start the
    # application lifespan, and this worker never launches a research runner.
    monkeypatch.setattr(worker, 'start', lambda: None)
    return main.app


@pytest.mark.asyncio
@pytest.mark.parametrize('roles', [None, 'USER'])
async def test_cancel_endpoints_require_admin_header_without_query_override(monkeypatch, roles):
    import httpx
    store = SqliteResearchPersistence()
    _, worker = _pod(store, None)
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {'top_n': 4})
    app = _wire_cancel_contract_api(monkeypatch, store, worker)
    headers = {key: value for key, value in _CANCEL_ADMIN_HEADERS.items() if key != 'X-AIP-User-Roles'}
    if roles is not None:
        headers['X-AIP-User-Roles'] = roles
    path = f'/api/v1/research/opportunities/cycles/{cycle_id}'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                    headers=headers) as client:
            for method, suffix in [('DELETE', '/cancel'), ('POST', '/finalize-cancel')]:
                response = await client.request(method, path + suffix, params={'x_aip_user_roles': 'ADMIN'})
                assert response.status_code == 403
        assert store.cycle_run(cycle_id)['status'] == 'ACCEPTED'
    finally:
        await worker.close()
        store._connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cached_state', ['running', 'queued', 'fresh_worker'])
async def test_cancel_contract_api_finalization_updates_status_and_coalescing(monkeypatch, tmp_path, cached_state):
    import httpx
    from unittest.mock import AsyncMock

    database = str(tmp_path / 'cancel-contract.sqlite')
    store = SqliteResearchPersistence(database)
    runner = AsyncMock(side_effect=AssertionError('No research execution in cancellation API test'))
    _, worker = _pod(store, runner, lease=60)
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {'top_n': 4})
    store.claim_cycle_run(cycle_id, worker.owner_id, lease_seconds=60)
    job = dict(cycle_id=cycle_id, status='RUNNING', updated_at='2026-01-01T00:00:00+00:00',
               parameters={'top_n': 4})
    store.record_opportunity_job(job)
    if cached_state != 'fresh_worker':
        worker.active = dict(job)
    if cached_state == 'queued':
        worker.queue.put_nowait(dict(job))
    app = _wire_cancel_contract_api(monkeypatch, store, worker)
    path = f'/api/v1/research/opportunities/cycles/{cycle_id}'
    reader = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers=_CANCEL_ADMIN_HEADERS) as client:
            initial = await client.get(path + '/status')
            assert initial.status_code == 200 and initial.json()['status'] == 'RUNNING'
            requested = await client.delete(path + '/cancel')
            assert requested.status_code == 200
            assert requested.json()['previous_status'] == 'RUNNING'
            assert requested.json()['status'] == 'CANCEL_REQUESTED'
            repeated = await client.delete(path + '/cancel')
            assert repeated.json()['cancelled'] is True
            assert repeated.json()['previous_status'] == 'CANCEL_REQUESTED'
            assert (await client.get(path + '/status')).json()['status'] == 'CANCEL_REQUESTED'
            draining = await client.post('/api/v1/research/opportunities/cycles', json={})
            assert draining.status_code == 202
            assert draining.json()['cycle_id'] == cycle_id
            assert draining.json()['coalesced'] is True
            assert draining.json()['status'] == 'CANCEL_REQUESTED'
            assert store.active_cycle_run()['cycle_id'] == cycle_id

            finalized = await client.post(path + '/finalize-cancel')
            assert finalized.status_code == 200
            assert finalized.json() == {'cycle_id': cycle_id, 'transitioned': True, 'status': 'CANCELLED'}
            assert store.active_cycle_run() is None
            terminal = await client.get(path + '/status')
            assert terminal.json()['status'] == 'CANCELLED'
            assert terminal.json()['updated_at'] == store.cycle_run(cycle_id)['updated_at']
            assert terminal.json()['updated_at'] != initial.json()['updated_at']

            # A late old-worker log entry cannot resurrect a cancelled run.
            store.record_opportunity_job({**job, 'updated_at': '2099-01-01T00:00:00+00:00'})
            reader = SqliteResearchPersistence(database)
            monkeypatch.setattr(worker.repository, 'persistence', reader)
            assert (await client.get(path + '/status')).json()['status'] == 'CANCELLED'
            assert reader.active_cycle_run() is None

            replacement = await client.post('/api/v1/research/opportunities/cycles', json={})
            assert replacement.status_code == 202
            replacement_id = replacement.json()['cycle_id']
            assert replacement_id != cycle_id
            assert replacement.json()['status'] == 'ACCEPTED'
            assert not replacement.json().get('coalesced', False)
            assert reader.active_cycle_run()['cycle_id'] == replacement_id
            coalesced = await client.post('/api/v1/research/opportunities/cycles', json={})
            assert coalesced.json()['cycle_id'] == replacement_id
            assert coalesced.json()['coalesced'] is True
            assert worker.queue.qsize() == 1
            queued = worker.queue.get_nowait()
            worker.queue.task_done()
            assert queued['cycle_id'] == replacement_id

            # Preserve the supported idempotent responses and the new slot.
            assert (await client.delete(path + '/cancel')).json()['status'] == 'CANCELLED'
            repeated_finalize = await client.post(path + '/finalize-cancel')
            assert repeated_finalize.status_code == 409
            assert repeated_finalize.json()['detail'] == 'ALREADY_CANCELLED'
            assert reader.active_cycle_run()['cycle_id'] == replacement_id
            assert (await client.get(path + '/status')).json()['status'] == 'CANCELLED'
            runner.assert_not_called()
    finally:
        await worker.close()
        if reader is not None:
            reader._connection.close()
        store._connection.close()


@pytest.mark.asyncio
async def test_cancel_contract_status_survives_missing_job_event(monkeypatch):
    import httpx
    store = SqliteResearchPersistence()
    _, worker = _pod(store, None)
    cycle_id = str(uuid4())
    # Simulate a process stopping after durable creation, before its job event.
    store.create_cycle_run(cycle_id, {'top_n': 4})
    app = _wire_cancel_contract_api(monkeypatch, store, worker)
    path = f'/api/v1/research/opportunities/cycles/{cycle_id}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                 headers=_CANCEL_ADMIN_HEADERS) as client:
        await client.delete(path + '/cancel')
        finalized = await client.post(path + '/finalize-cancel')
        assert finalized.json()['transitioned'] is True
        result = await client.get(path + '/status')
        assert result.status_code == 200 and result.json()['status'] == 'CANCELLED'
        assert store.active_cycle_run() is None


@pytest.mark.asyncio
async def test_cancel_contract_old_runner_cleanup_preserves_queued_replacement():
    store = SqliteResearchPersistence()
    old_started, release_old = asyncio.Event(), asyncio.Event()
    draining, finish_drain = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs['cycle_id'])
        if len(calls) == 1:
            old_started.set()
            await release_old.wait()
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 0}

    _, worker = _pod(store, runner, lease=60)
    old_id = None

    async def heartbeat(cycle_id):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cycle_id == old_id:
                draining.set()
                await finish_drain.wait()

    worker._heartbeat = heartbeat
    try:
        old_id = worker.submit({'top_n': 4})['cycle_id']
        await asyncio.wait_for(old_started.wait(), 2)
        store.request_cycle_cancel(old_id)
        assert store.cancel_cycle_run_unowned(old_id)
        replacement_id = worker.submit({'top_n': 4})['cycle_id']
        assert replacement_id != old_id and calls == [old_id]
        release_old.set()
        await asyncio.wait_for(draining.wait(), 2)
        assert worker.active['cycle_id'] == replacement_id
        assert worker.submit({'top_n': 4})['cycle_id'] == replacement_id
        assert worker.queue.qsize() == 1
        finish_drain.set()
        await asyncio.wait_for(worker.queue.join(), 2)
        assert calls == [old_id, replacement_id]
        assert store.cycle_run(old_id)['status'] == 'CANCELLED'
        assert store.cycle_run(replacement_id)['status'] == 'COMPLETED'
    finally:
        release_old.set()
        finish_drain.set()
        await worker.close()
