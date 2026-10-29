"""Targeted refresh lifecycle tests. All persistence is disposable local SQLite."""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models import CompanyResearchProfile
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings


class ControlledPersistence:
    """Exercise the real serialized/threaded persistence dispatch, with barriers."""

    def __init__(self, store):
        self.store = store
        self.pause = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name):
        return getattr(self.store, name)

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("Test persistence barrier was not released")

    def start_refresh_run(self, **kwargs):
        run = self.store.start_refresh_run(**kwargs)
        if self.pause == "start":
            self.wait()  # committed, but the caller has not received its run handle
        return run

    def complete_refresh_run(self, run, **kwargs):
        if self.pause == "complete":
            self.wait()
        return self.store.complete_refresh_run(run, **kwargs)


@pytest.fixture
def refresh(tmp_path):
    store = SqliteResearchPersistence(tmp_path / "refresh.sqlite")
    # Only the fixture changes SQLite's thread affinity. The real repository
    # dispatch serializes every operation, just as it does for PostgreSQL.
    store._connection.close()
    store._connection = sqlite3.connect(store.database_path, check_same_thread=False)
    store._connection.row_factory = sqlite3.Row
    persistence = ControlledPersistence(store)
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=persistence,
    )
    profile = CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="Lifecycle Test",
        ticker="TEST", exchange="NSE", mic="XNSE", country="IN", currency="INR",
    )
    repository.profiles.append(profile)
    repository._refresh_live = AsyncMock()
    yield repository, persistence, profile
    persistence.release.set()
    store._connection.close()


def rows(persistence):
    return [dict(row) for row in persistence.store._connection.execute(
        "SELECT * FROM research_refresh_runs ORDER BY started_at, refresh_run_id"
    ).fetchall()]


def start(repository, profile, correlation="lifecycle-test"):
    return asyncio.create_task(repository.refresh_targeted_categories(
        profile.instrument_id, {"CATALYSTS"}, correlation_id=correlation, allow_demo=False,
    ))


async def entered(persistence):
    assert await asyncio.to_thread(persistence.entered.wait, 2)


@pytest.mark.asyncio
async def test_cancellation_during_committed_start_closes_only_owned_run(refresh):
    repository, persistence, profile = refresh
    persistence.pause = "start"
    task = start(repository, profile)
    await entered(persistence)
    task.cancel()
    await asyncio.sleep(0)
    persistence.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    row, = rows(persistence)
    assert row["status"] == "FAILED"
    assert row["safe_error_code"] == "ACQUISITION_CANCELLED"
    assert row["completed_at"] is not None
    assert row["correlation_id"] == "lifecycle-test"
    repository._refresh_live.assert_not_called()
    assert not repository._instrument_refresh_flights


@pytest.mark.asyncio
async def test_exception_after_start_is_terminal_instead_of_leaking_running(refresh):
    repository, persistence, profile = refresh
    def broken_count(*args, **kwargs):
        raise ValueError("Simulated evidence read failure")
    repository.document_count_for = broken_count
    with pytest.raises(ValueError, match="Simulated evidence read failure"):
        await start(repository, profile)
    row, = rows(persistence)
    assert row["status"] == "FAILED"
    assert row["safe_error_code"] == "ValueError"
    assert row["completed_at"] is not None
    repository._refresh_live.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_failure_persistence(refresh):
    repository, persistence, profile = refresh
    persistence.pause = "complete"
    provider_entered = asyncio.Event()
    async def provider(*args, **kwargs):
        provider_entered.set()
        await asyncio.Event().wait()
    repository._refresh_live = AsyncMock(side_effect=provider)
    task = start(repository, profile)
    await asyncio.wait_for(provider_entered.wait(), 2)
    task.cancel()
    await entered(persistence)
    task.cancel()
    persistence.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    row, = rows(persistence)
    assert row["status"] == "FAILED"
    assert row["safe_error_code"] == "ACQUISITION_CANCELLED"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_cancel_while_success_write_is_queued_preserves_completed_work(refresh):
    repository, persistence, profile = refresh
    provider_entered = asyncio.Event()
    async def provider(*args, **kwargs):
        await repository._persistence_dispatch_slot.acquire()
        provider_entered.set()
    repository._refresh_live = AsyncMock(side_effect=provider)
    task = start(repository, profile)
    await asyncio.wait_for(provider_entered.wait(), 2)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    repository._persistence_dispatch_slot.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    row, = rows(persistence)
    assert row["status"] == "COMPLETED"
    assert row["completed_at"] is not None
    assert row["safe_error_code"] is None


@pytest.mark.asyncio
async def test_retry_after_cancel_creates_new_run_and_preserves_cancelled_attempt(refresh):
    repository, persistence, profile = refresh
    repository._refresh_live = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await start(repository, profile, "cancelled-attempt")
    before, = rows(persistence)
    repository._refresh_live = AsyncMock()
    await start(repository, profile, "retry-attempt")
    after = rows(persistence)
    assert len(after) == 2
    assert next(row for row in after if row["refresh_run_id"] == before["refresh_run_id"]) == before
    assert next(row for row in after if row["correlation_id"] == "retry-attempt")["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_new_repository_does_not_reconcile_unowned_incomplete_refresh(refresh):
    repository, persistence, profile = refresh
    persistence.start_refresh_run(instrument_id=profile.instrument_id,
        company_id=profile.company_id, correlation_id="unknown-worker", mode="TARGETED_LIVE")
    before, = rows(persistence)
    recovered = ResearchRepository(settings=repository.settings, persistence=persistence)
    recovered.profiles.append(profile)
    recovered._refresh_live = AsyncMock()
    await start(recovered, profile, "new-execution")
    assert next(row for row in rows(persistence) if row["refresh_run_id"] == before["refresh_run_id"]) == before
    assert before["status"] == "RUNNING" and before["completed_at"] is None


@pytest.mark.asyncio
async def test_cancelled_follower_does_not_close_another_callers_live_refresh(refresh):
    repository, persistence, profile = refresh
    provider_entered, release = asyncio.Event(), asyncio.Event()
    async def provider(*args, **kwargs):
        provider_entered.set()
        await release.wait()
    repository._refresh_live = AsyncMock(side_effect=provider)
    owner = start(repository, profile, "owner")
    await asyncio.wait_for(provider_entered.wait(), 2)
    follower = start(repository, profile, "follower")
    await asyncio.sleep(0)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    row, = rows(persistence)
    assert row["status"] == "RUNNING" and row["completed_at"] is None
    assert not owner.done()
    release.set()
    await owner
    assert rows(persistence)[0]["status"] == "COMPLETED"
    assert repository._refresh_live.await_count == 1


@pytest.mark.asyncio
async def test_failed_terminal_write_is_not_presented_as_success(refresh):
    repository, persistence, profile = refresh
    def fail_completion(*args, **kwargs):
        raise RuntimeError("Simulated terminal write failure")
    persistence.complete_refresh_run = fail_completion
    with pytest.raises(RuntimeError, match="Simulated terminal write failure"):
        await start(repository, profile)
    row, = rows(persistence)
    assert row["status"] == "RUNNING" and row["completed_at"] is None
    assert profile.instrument_id not in repository.last_refresh


def test_unowned_refresh_still_blocks_deployment_observer(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts/database/deployment_observation.py"
    spec = importlib.util.spec_from_file_location("refresh_observer", path)
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    monkeypatch.setattr(observer, "TABLES", {"research_refresh_runs": ["refresh_run_id"]})
    monkeypatch.setattr(observer, "pages", lambda *a, **k: iter([[{
        "refresh_run_id": str(uuid4()), "completed_at": None,
        "started_at": "2000-01-01T00:00:00+00:00",
    }]]))
    monkeypatch.setattr(observer, "legacy_jobs", lambda *a: {"legacy_only_active": []})
    class Connection:
        def execute(self, query):
            result = {"transaction_read_only": "on"} if query.startswith("SHOW") else {"now": datetime.now(timezone.utc)}
            return SimpleNamespace(fetchone=lambda: result)
        def rollback(self):
            pass
    result = observer.observe(Connection())
    assert result["status"] == "BLOCKED"
    assert result["reasons"] == ["BACKGROUND_REFRESH"]
