"""FULL discovery must not let status polling block Uvicorn's event loop."""

import asyncio
import threading
from uuid import UUID

import httpx
import pytest

from app.global_scanner import GlobalScanner
from app.persistence import SqliteResearchPersistence
from test_global_scanner import NOW, instrument


class _FixedUniverse:
    def __init__(self, rows):
        self.rows = rows

    async def active_global_equities(self, **unused):
        return self.rows


class _EventLoopGuardedLock:
    """Fail deterministically if an async route waits on this thread lock."""

    def __init__(self, event_loop_thread_id: int):
        self._event_loop_thread_id = event_loop_thread_id
        self._lock = threading.RLock()

    def __enter__(self):
        if threading.get_ident() == self._event_loop_thread_id:
            raise AssertionError("persistence lock acquired on the event-loop thread")
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


class _DiscoveryPersistence:
    def __init__(self, cycle_id: str):
        self.cycle_id = cycle_id
        self.discovery_holds_lock = threading.Event()
        self.release_discovery = threading.Event()
        self.status_thread_id: int | None = None
        self.run = {
            "cycle_id": cycle_id,
            "status": "RUNNING",
            "parameters": {"analysis_scope": "FULL"},
        }

    def load_structured_market_snapshots(self, instrument_ids):
        if not self.discovery_holds_lock.is_set():
            self.discovery_holds_lock.set()
            if not self.release_discovery.wait(timeout=5):
                raise RuntimeError("test discovery release was not signalled")
        return []

    def load_financial_facts(self, instrument_ids):
        return []

    def load_market_price_observations(self, instrument_ids):
        return []

    def opportunity_job(self, cycle_id: str):
        self.status_thread_id = threading.get_ident()
        if cycle_id != self.cycle_id:
            return None
        return {
            "cycle_id": cycle_id,
            "status": self.run["status"],
            "parameters": {"analysis_scope": "FULL"},
        }

    def opportunity_jobs(self):
        raise AssertionError("status endpoint performed an unscoped all-cycle read")

    def cycle_run(self, cycle_id: str):
        return dict(self.run) if cycle_id == self.cycle_id else None

    def cycle_cancel_status(self, cycle_id: str):
        if cycle_id != self.cycle_id or self.run["status"] == "RUNNING":
            return None
        return self.run["status"]

    def request_cycle_cancel(self, cycle_id: str):
        if cycle_id != self.cycle_id or self.run["status"] != "RUNNING":
            return False
        self.run["status"] = "CANCEL_REQUESTED"
        return True


class _ThreadOffloadingRepository:
    def __init__(self, persistence, event_loop_thread_id: int):
        self.persistence = persistence
        self._persistence_worker_lock = _EventLoopGuardedLock(event_loop_thread_id)
        self.status_dispatched = asyncio.Event()
        self.cancel_dispatched = asyncio.Event()

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        if getattr(operation, "__name__", "") == "opportunity_job":
            self.status_dispatched.set()
        if getattr(operation, "__name__", "") == "_request_cancel":
            self.cancel_dispatched.set()

        def invoke():
            with self._persistence_worker_lock:
                return operation(*args, **kwargs)

        return await asyncio.to_thread(invoke)


@pytest.mark.asyncio
async def test_full_discovery_status_poll_does_not_starve_health_requests(monkeypatch):
    """Reproduce the production lock ordering without a 2,614-stock run.

    Discovery holds the production-style serialized persistence lock in a
    worker thread. A concurrent status HTTP request must wait for that lock in
    another worker thread, leaving Uvicorn's event loop free to serve both
    health endpoints. The scan must still account for every universe member.
    """
    from app import main

    cycle_id = "1b68e1c3-6b13-462a-9359-e73d3a91e3b2"
    persistence = _DiscoveryPersistence(cycle_id)
    repository = _ThreadOffloadingRepository(persistence, threading.get_ident())
    monkeypatch.setattr(main, "repository", repository)

    rows = [instrument(index) for index in range(1, 129)]
    scanner = GlobalScanner(
        _FixedUniverse(rows),
        persistence,
        batch_size=1,
        run_blocking=repository._run_blocking_persistence,
    )
    scan_task = asyncio.create_task(scanner.scan(as_of=NOW, top_n=0))

    transport = httpx.ASGITransport(app=main.app)
    try:
        assert await asyncio.to_thread(persistence.discovery_holds_lock.wait, 1)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status_task = asyncio.create_task(
                client.get(f"/api/v1/research/opportunities/cycles/{cycle_id}/status")
            )
            await asyncio.wait_for(repository.status_dispatched.wait(), timeout=1)
            assert not status_task.done(), "status read should be queued behind active discovery"

            live, ready = await asyncio.wait_for(
                asyncio.gather(client.get("/health/live"), client.get("/health")),
                timeout=1,
            )
            assert live.status_code == 200
            assert ready.status_code == 200

            persistence.release_discovery.set()
            status = await asyncio.wait_for(status_task, timeout=2)
            assert status.status_code == 200
            assert status.json()["cycle_id"] == cycle_id

        result = await asyncio.wait_for(scan_task, timeout=2)
        assert len(result.candidates) == len(rows) == 128
        assert persistence.status_thread_id != threading.get_ident()
    finally:
        persistence.release_discovery.set()
        if not scan_task.done():
            scan_task.cancel()
        await asyncio.gather(scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_request_remains_available_during_full_discovery(monkeypatch):
    from app import main

    cycle_id = "1b68e1c3-6b13-462a-9359-e73d3a91e3b2"
    persistence = _DiscoveryPersistence(cycle_id)
    repository = _ThreadOffloadingRepository(persistence, threading.get_ident())
    monkeypatch.setattr(main, "repository", repository)
    scanner = GlobalScanner(
        _FixedUniverse([instrument(index) for index in range(1, 17)]),
        persistence,
        batch_size=1,
        run_blocking=repository._run_blocking_persistence,
    )
    scan_task = asyncio.create_task(scanner.scan(as_of=NOW, top_n=0))
    headers = {
        "X-AIP-User-Id": "admin",
        "X-AIP-User-Issuer": "test",
        "X-AIP-User-Subject": "admin",
        "X-AIP-User-Roles": "ADMIN",
    }

    try:
        assert await asyncio.to_thread(persistence.discovery_holds_lock.wait, 1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test", headers=headers
        ) as client:
            cancel_task = asyncio.create_task(
                client.delete(f"/api/v1/research/opportunities/cycles/{cycle_id}/cancel")
            )
            await asyncio.wait_for(repository.cancel_dispatched.wait(), timeout=1)
            assert not cancel_task.done(), "cancellation should be queued behind active discovery"

            # The cancellation wait itself must not monopolize request serving.
            live = await asyncio.wait_for(client.get("/health/live"), timeout=1)
            assert live.status_code == 200

            persistence.release_discovery.set()
            response = await asyncio.wait_for(cancel_task, timeout=2)
            assert response.status_code == 200
            assert response.json()["status"] == "CANCEL_REQUESTED"
            assert response.json()["cancelled"] is True

        result = await asyncio.wait_for(scan_task, timeout=2)
        assert len(result.candidates) == 16
    finally:
        persistence.release_discovery.set()
        if not scan_task.done():
            scan_task.cancel()
        await asyncio.gather(scan_task, return_exceptions=True)


def test_single_cycle_status_read_does_not_reconstruct_other_cycles(monkeypatch):
    store = SqliteResearchPersistence()
    target = "target-cycle"
    other = "other-cycle"
    store.record_opportunity_job({
        "cycle_id": other,
        "status": "COMPLETED",
        "updated_at": "2026-10-06T10:00:00+00:00",
        "parameters": {},
    })
    store.record_opportunity_job({
        "cycle_id": target,
        "status": "RUNNING",
        "updated_at": "2026-10-06T10:01:00+00:00",
        "parameters": {"analysis_scope": "FULL"},
    })
    reconstructed = []
    monkeypatch.setattr(
        store,
        "cycle_status_snapshot",
        lambda cycle_id: reconstructed.append(cycle_id) or {"deep_completed": 40, "deep_denominator": 100},
    )

    value = store.opportunity_job(target)

    assert value["cycle_id"] == target
    assert value["authoritative_progress"] == {"deep_completed": 40, "deep_denominator": 100}
    assert reconstructed == [target]


@pytest.mark.asyncio
async def test_offloaded_discovery_exception_is_awaited_and_propagated(monkeypatch):
    persistence = _DiscoveryPersistence("unused")
    persistence.release_discovery.set()

    def fail(instrument_ids):
        raise RuntimeError("DISCOVERY_READ_FAILED")

    monkeypatch.setattr(persistence, "load_structured_market_snapshots", fail)
    repository = _ThreadOffloadingRepository(persistence, threading.get_ident())
    scanner = GlobalScanner(
        _FixedUniverse([instrument(1)]),
        persistence,
        batch_size=1,
        run_blocking=repository._run_blocking_persistence,
    )

    with pytest.raises(RuntimeError, match="DISCOVERY_READ_FAILED"):
        await scanner.scan(as_of=NOW, top_n=0)
