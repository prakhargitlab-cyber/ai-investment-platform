"""ETF Radar async lifecycle: proves the ETF POST cycle no longer depends on
synchronous request-lifetime execution, reusing the EXACT generic
OpportunityCycleWorker/cycle_checkpoint machinery Equity Radar uses (its own
'ETF' market namespace, never colliding with Equity's 'NSE' one).

Mirrors tests/test_cycle_cancellation.py's direct-worker testing style:
construct OpportunityCycleWorker against a real SqliteResearchPersistence,
drive it with asyncio directly (no FastAPI TestClient / HTTP layer -- that
layer is covered separately in tests/test_etf_radar_api.py, and the
background task's own event loop does not reliably outlive a single
TestClient call made without `with TestClient(app) as client:`).

No ETF Stage2 / company-specific acquisition is reachable here at all; the
runners below call only the already-tested, deterministic
run_etf_radar_cycle / save_etf_radar_cycle path (or trivial fakes), never
any provider.
"""
from __future__ import annotations

import asyncio
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence


def _pod(runner, *, lease=0.3, poll=0.05, market="ETF"):
    store = SqliteResearchPersistence()
    settings = SimpleNamespace(research_opportunity_recovery_paused=False)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), settings=settings)
    worker = OpportunityCycleWorker(repo, runner, lease_seconds=lease, poll_seconds=poll, market=market)
    return store, worker


def _run(coro):
    return asyncio.run(coro)


# -- 1. submit returns immediately -------------------------------------------

def test_submit_does_not_wait_for_runner_completion():
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow_runner(**params):
        entered.set()
        await gate.wait()
        return {"cycle_id": params.get("_etf_cycle_id", "x"), "universe_count": 0}

    async def scenario():
        store, worker = _pod(slow_runner)
        try:
            submitted = worker.submit({"top_n": 5})
            assert submitted["status"] == "ACCEPTED"
            cycle_id = submitted["cycle_id"]
            # The runner has (at most) just started; submit() itself never
            # awaited it, so the run is still non-terminal right now.
            await asyncio.wait_for(entered.wait(), timeout=2)
            run = store.cycle_run(cycle_id)
            assert run["status"] in ("ACCEPTED", "RUNNING")
            gate.set()
            await asyncio.sleep(0.3)
            run = store.cycle_run(cycle_id)
            assert run["status"] == "COMPLETED"
        finally:
            gate.set()
            await worker.close()

    _run(scenario())


# -- 2. status progression ----------------------------------------------------

def test_status_progresses_through_valid_lifecycle():
    seen = []

    async def runner(**params):
        return {"cycle_id": str(uuid4()), "universe_count": 3}

    async def scenario():
        store, worker = _pod(runner)
        try:
            submitted = worker.submit({"top_n": 5})
            cycle_id = submitted["cycle_id"]
            assert submitted["status"] == "ACCEPTED"
            for _ in range(50):
                run = store.cycle_run(cycle_id)
                seen.append(run["status"])
                if run["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert seen[-1] == "COMPLETED"
            # Every transition observed is one of the valid lifecycle states.
            assert set(seen) <= {"ACCEPTED", "RUNNING", "COMPLETED"}
        finally:
            await worker.close()

    _run(scenario())


# -- 3. completed result is durably persisted ---------------------------------

def test_completed_result_is_persisted_by_the_runner():
    from datetime import datetime, timezone

    store = SqliteResearchPersistence()

    async def real_runner(**params):
        cycle_id = params.get("cycle_id") or uuid4()
        store.save_etf_radar_cycle(cycle_id, "ETF_RADAR_V1", params.get("correlation_id"),
            datetime.now(timezone.utc), {"radar_version": "ETF_RADAR_V1", "cycle_id": str(cycle_id), "ranked": []})
        return {"cycle_id": str(cycle_id), "universe_count": 0}

    async def scenario():
        settings = SimpleNamespace(research_opportunity_recovery_paused=False)
        repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), settings=settings)
        worker = OpportunityCycleWorker(repo, real_runner, lease_seconds=0.3, poll_seconds=0.05, market="ETF")
        try:
            submitted = worker.submit({"top_n": 5})
            cycle_id = submitted["cycle_id"]
            for _ in range(50):
                if store.cycle_run(cycle_id)["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            persisted = store.etf_radar_cycle(cycle_id)
            assert persisted is not None
            assert persisted["radar_version"] == "ETF_RADAR_V1"
        finally:
            await worker.close()

    _run(scenario())


# -- 4. cancellation terminalizes correctly -----------------------------------

def test_cancellation_requested_mid_run_terminalizes_to_cancelled():
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def runner(**params):
        entered.set()
        await gate.wait()
        return {"cycle_id": str(uuid4()), "universe_count": 0}

    async def scenario():
        store, worker = _pod(runner)
        try:
            submitted = worker.submit({"top_n": 5})
            cycle_id = submitted["cycle_id"]
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert store.request_cycle_cancel(cycle_id) is True
            gate.set()
            for _ in range(50):
                run = store.cycle_run(cycle_id)
                if run["status"] == "CANCELLED":
                    break
                await asyncio.sleep(0.02)
            assert store.cycle_run(cycle_id)["status"] == "CANCELLED"
        finally:
            gate.set()
            await worker.close()

    _run(scenario())


# -- 5. cancelled ETF cycle does not become FAILED due to worker shutdown ----

def test_cancel_requested_then_worker_shutdown_coerces_cancelled_not_failed():
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def runner(**params):
        entered.set()
        await gate.wait()  # never set -- shutdown interrupts it
        return {"cycle_id": str(uuid4()), "universe_count": 0}

    async def scenario():
        store, worker = _pod(runner)
        submitted = worker.submit({"top_n": 5})
        cycle_id = submitted["cycle_id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert store.request_cycle_cancel(cycle_id) is True
        await asyncio.sleep(0.1)
        await worker.close()
        assert store.cycle_run(cycle_id)["status"] == "CANCELLED"

    _run(scenario())


# -- 6. a genuine worker failure never becomes CANCELLED ----------------------

def test_genuine_runner_exception_becomes_failed_not_cancelled():
    async def failing_runner(**params):
        raise RuntimeError("simulated genuine failure, unrelated to cancellation")

    async def scenario():
        store, worker = _pod(failing_runner)
        try:
            submitted = worker.submit({"top_n": 5})
            cycle_id = submitted["cycle_id"]
            for _ in range(50):
                run = store.cycle_run(cycle_id)
                if run["status"] in ("FAILED", "CANCELLED"):
                    break
                await asyncio.sleep(0.02)
            run = store.cycle_run(cycle_id)
            assert run["status"] == "FAILED"
            assert run["error_code"] == "OPPORTUNITY_CYCLE_FAILED"
        finally:
            await worker.close()

    _run(scenario())


# -- 7. restart/recovery resumes the active run -------------------------------

def test_new_worker_instance_resumes_active_run_after_restart():
    calls = []

    async def runner(**params):
        calls.append(params)
        return {"cycle_id": str(uuid4()), "universe_count": 0}

    async def scenario():
        store = SqliteResearchPersistence()
        settings = SimpleNamespace(research_opportunity_recovery_paused=False)
        repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), settings=settings)
        # Simulate a crashed worker: a durable ACCEPTED run with no owner,
        # never drained.
        cycle_id = str(uuid4())
        store.create_cycle_run(cycle_id, {"top_n": 5}, market="ETF")

        worker2 = OpportunityCycleWorker(repo, runner, lease_seconds=0.3, poll_seconds=0.05, market="ETF")
        worker2.start()
        try:
            for _ in range(50):
                run = store.cycle_run(cycle_id)
                if run["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert store.cycle_run(cycle_id)["status"] == "COMPLETED"
        finally:
            await worker2.close()

    _run(scenario())


# -- 8. ETF and Equity cycles remain isolated ---------------------------------

def test_etf_and_equity_workers_do_not_interfere():
    async def etf_runner(**params):
        return {"cycle_id": str(uuid4()), "universe_count": 1}

    async def equity_runner(**params):
        return {"cycle_id": str(uuid4()), "universe_count": 2}

    async def scenario():
        store = SqliteResearchPersistence()
        settings = SimpleNamespace(research_opportunity_recovery_paused=False)
        repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), settings=settings)
        etf_worker = OpportunityCycleWorker(repo, etf_runner, lease_seconds=0.3, poll_seconds=0.05, market="ETF")
        equity_worker = OpportunityCycleWorker(repo, equity_runner, lease_seconds=0.3, poll_seconds=0.05, market="NSE")
        try:
            etf_submitted = etf_worker.submit({"top_n": 5})
            equity_submitted = equity_worker.submit({"top_n": 4})
            assert etf_submitted["cycle_id"] != equity_submitted["cycle_id"]
            for _ in range(50):
                etf_run = store.cycle_run(etf_submitted["cycle_id"])
                equity_run = store.cycle_run(equity_submitted["cycle_id"])
                if etf_run["status"] == "COMPLETED" and equity_run["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert store.cycle_run(etf_submitted["cycle_id"])["market"] == "ETF"
            assert store.cycle_run(equity_submitted["cycle_id"])["market"] == "NSE"
            assert store.active_cycle_run("ETF") is None
            assert store.active_cycle_run("NSE") is None
        finally:
            await etf_worker.close()
            await equity_worker.close()

    _run(scenario())
