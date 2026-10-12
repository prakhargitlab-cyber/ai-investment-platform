"""Deployment pause is a no-recovery-I/O boundary, not a status transition."""
import asyncio
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.cycle_checkpoint import CandidateState
from app.global_opportunity_scheduler import GlobalOpportunityScheduler
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence


@pytest.mark.asyncio
@pytest.mark.parametrize('market', ['NSE', 'ETF'])
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_paused_startup_never_reads_or_changes_durable_state(market, asynchronous):
    store = SqliteResearchPersistence()
    store.record_opportunity_job(dict(cycle_id='legacy', status='RUNNING',
                                     updated_at=datetime.now(timezone.utc).isoformat()))
    key = str(uuid4())
    store.create_cycle_run(key, {}, market=market)
    store.claim_cycle_run(key, 'old')
    store.record_cycle_progress(key, 'old', 'DEEP', 'candidate', CandidateState.IN_PROGRESS, start_attempt=True)
    before = '\n'.join(store._connection.iterdump())
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(),
                           settings=SimpleNamespace(research_opportunity_recovery_paused=True))
    runner = AsyncMock()
    worker = OpportunityCycleWorker(repo, runner, market=market)
    queries = []
    store._connection.set_trace_callback(queries.append)
    tasks = set(asyncio.all_tasks())
    if asynchronous:
        await worker.start_async()
    else:
        worker.start()
    await asyncio.sleep(0)
    assert queries == []
    assert worker.task is None and worker.active is None
    assert set(asyncio.all_tasks()) == tasks
    runner.assert_not_called()
    store._connection.set_trace_callback(None)
    assert '\n'.join(store._connection.iterdump()) == before
    # Explicit submissions are also refused and leave every persisted row intact.
    result = await worker.submit_async({}) if asynchronous else worker.submit({})
    assert result['paused'] is True
    assert '\n'.join(store._connection.iterdump()) == before
    assert worker.task is None
    await worker.close()


@pytest.mark.asyncio
async def test_paused_lifespan_does_not_construct_workers_or_scheduler(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', True)
    class ForbiddenRepository:
        @property
        def persistence(self):
            raise AssertionError('Paused lifespan accessed persistence')
    monkeypatch.setattr(main, 'repository', ForbiddenRepository())
    for name in ('_opportunity_worker', '_etf_radar_worker', '_opportunity_scheduler'):
        monkeypatch.setattr(main, name, Mock(side_effect=AssertionError(name)))
    application = SimpleNamespace(state=SimpleNamespace())
    tasks = set(asyncio.all_tasks())
    async with main.research_lifespan(application):
        assert set(asyncio.all_tasks()) == tasks
        assert await main.health() == {'status': 'ok', 'service': main.settings.service_name}
    assert set(asyncio.all_tasks()) == tasks


@pytest.mark.asyncio
async def test_direct_scheduler_start_is_inert_when_paused():
    worker = SimpleNamespace(repository=SimpleNamespace(settings=SimpleNamespace(research_opportunity_recovery_paused=True)))
    scheduler = GlobalOpportunityScheduler(worker, Mock())
    scheduler.start()
    assert scheduler.task is None
    assert scheduler.persistence.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('controlled', [False, True])
@pytest.mark.parametrize('etf', [False, True])
async def test_paused_cycle_endpoints_reject_before_runner(monkeypatch, controlled, etf):
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main, 'repository', SimpleNamespace(persistence=SqliteResearchPersistence()))
    monkeypatch.setattr(main, '_etf_radar_worker', Mock(side_effect=AssertionError('worker created')))
    monkeypatch.setattr(main, '_opportunity_worker', Mock(side_effect=AssertionError('worker created')))
    request = Request({'type': 'http', 'headers': []})
    cls = main.EtfRadarCycleRequest if etf else main.OpportunityCycleRequest
    handler = main.etf_radar_cycle if etf else main.opportunity_cycle
    with pytest.raises(HTTPException) as error:
        await handler(cls(candidate_ids=[uuid4()] if controlled else None), request)
    assert (error.value.status_code, error.value.detail) == (503, 'RECOVERY_PAUSED')
