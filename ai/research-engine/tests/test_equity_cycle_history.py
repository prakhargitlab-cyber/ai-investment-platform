"""Provider-free discovery and historical progress regressions."""
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.cycle_checkpoint import CandidateState, PHASE_BASELINE, PHASE_DEEP
from app.persistence import SqliteResearchPersistence


@pytest.fixture
def store():
    persistence = SqliteResearchPersistence()
    yield persistence
    persistence._connection.close()


def terminal_run(store, status, *, market='NSE', day=1, cycle_id=None):
    cycle_id = cycle_id or str(uuid4())
    store.create_cycle_run(cycle_id, {'analysis_scope': 'FULL'}, market=market,
                           now=datetime(2026, 10, day, tzinfo=timezone.utc))
    # Lifecycle fixture only; no worker or provider is started.
    with store._connection:
        store._connection.execute(
            'UPDATE global_opportunity_cycle_run SET status=? WHERE cycle_id=?',
            (status, cycle_id))
        store._connection.execute(
            'DELETE FROM global_opportunity_cycle_active WHERE cycle_id=?', (cycle_id,))
    return cycle_id


def test_recent_cycles_include_terminal_equity_runs_and_exclude_etfs(store, monkeypatch):
    ids = [terminal_run(store, status, day=day)
           for day, status in enumerate(('CANCELLED', 'FAILED', 'COMPLETED'), start=1)]
    terminal_run(store, 'COMPLETED', market='ETF', day=4)
    monkeypatch.setattr(store, 'cycle_status_snapshot', lambda *_: pytest.fail('Discovery loaded checkpoints'))
    result = store.recent_opportunity_cycles(limit=2)
    assert [row['cycle_id'] for row in result['cycles']] == ids[:0:-1]
    assert [row['status'] for row in result['cycles']] == ['COMPLETED', 'FAILED']
    assert result['active_cycle'] is None
    assert all(row['analysis_scope'] == 'FULL' for row in result['cycles'])
    assert all('selection' not in row and 'authoritative_progress' not in row for row in result['cycles'])
    assert store.recent_opportunity_cycles(limit=3)['cycles'][-1]['status'] == 'CANCELLED'


def test_active_run_is_returned_even_outside_recent_window(store):
    newest = terminal_run(store, 'CANCELLED', day=17)
    old_active = str(uuid4())
    store.create_cycle_run(old_active, {'analysis_scope': 'FULL'},
                           now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    store.claim_cycle_run(old_active, 'owner')
    # An ETF active slot must not replace the Equity slot.
    store.create_cycle_run(str(uuid4()), {}, market='ETF')
    result = store.recent_opportunity_cycles(limit=1)
    assert [row['cycle_id'] for row in result['cycles']] == [newest]
    assert result['active_cycle']['cycle_id'] == old_active
    assert result['active_cycle']['status'] == 'RUNNING'


def test_history_order_uses_creation_time_and_stable_id_tiebreak(store):
    terminal_run(store, 'FAILED', day=1, cycle_id='old')
    terminal_run(store, 'CANCELLED', day=2, cycle_id='a')
    terminal_run(store, 'COMPLETED', day=2, cycle_id='b')
    with store._connection:
        store._connection.execute(
            "UPDATE global_opportunity_cycle_run SET updated_at='2099-01-01' WHERE cycle_id='old'")
    assert [row['cycle_id'] for row in store.recent_opportunity_cycles()['cycles']] == ['b', 'a', 'old']


@pytest.mark.parametrize('limit', [0, -1, 51])
def test_discovery_rejects_unbounded_limits(store, limit):
    with pytest.raises(ValueError):
        store.recent_opportunity_cycles(limit=limit)


@pytest.mark.asyncio
async def test_discovery_and_historical_status_preserve_cancelled_checkpoints(store, monkeypatch):
    import app.main as main

    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {'analysis_scope': 'FULL'})
    store.claim_cycle_run(cycle_id, 'owner')
    store.update_cycle_run(cycle_id, 'owner', selection={
        'universe': ['instrument'], 'universe_count': 1, 'deep_ids': ['instrument'], 'phase': 'DEEP'})
    store.record_cycle_progress(cycle_id, 'owner', PHASE_BASELINE, 'instrument', CandidateState.COMPLETED,
                                disposition='BASELINE_READY', payload={'has_outcome': True, 'ready': True})
    store.record_cycle_progress(cycle_id, 'owner', PHASE_DEEP, 'instrument', CandidateState.COMPLETED,
                                disposition='ANALYZED', start_attempt=True,
                                payload={'diagnostic': {'disposition': 'ANALYZED', 'rank_eligible': True}})
    store.request_cycle_cancel(cycle_id)
    assert store.cancel_cycle_run(cycle_id, 'owner')
    before = store.cycle_progress(cycle_id)
    calls = []

    async def blocking(operation, *args, **kwargs):
        calls.append(operation.__name__)
        return operation(*args, **kwargs)

    monkeypatch.setattr(main, 'repository', SimpleNamespace(
        persistence=store, _persistence_worker_lock=RLock(), _run_blocking_persistence=blocking))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        history = await client.get('/api/v1/research/opportunities/cycles?limit=1')
        status = await client.get(f'/api/v1/research/opportunities/cycles/{cycle_id}/status')
        invalid = [await client.get(f'/api/v1/research/opportunities/cycles?limit={limit}')
                   for limit in (0, 51, 'invalid')]
    assert history.status_code == status.status_code == 200
    assert history.json()['active_cycle'] is None
    assert history.json()['cycles'][0]['cycle_id'] == cycle_id
    value = status.json()
    assert value['status'] == 'CANCELLED'
    assert value['authoritative_progress']['baseline_completed'] == 1
    assert value['authoritative_progress']['deep_ready'] == 1
    assert value['authoritative_progress']['overall_percentage'] is None
    assert store.cycle_progress(cycle_id) == before
    assert all(response.status_code == 422 for response in invalid)
    assert calls == ['recent_opportunity_cycles', 'opportunity_job']


@pytest.mark.asyncio
async def test_discovery_without_persistence_returns_empty_history(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, 'repository', SimpleNamespace(persistence=SimpleNamespace()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.get('/api/v1/research/opportunities/cycles')
    assert response.status_code == 200
    assert response.json() == {'cycles': [], 'active_cycle': None}
