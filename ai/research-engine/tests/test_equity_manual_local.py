from unittest.mock import AsyncMock
import asyncio
import pytest
from fastapi.testclient import TestClient
from app.opportunity_worker import OpportunityCycleWorker, ManualEtfAdmissionError
from test_etf_manual_execution import repo
from test_etf_preflight import ADMIN


@pytest.mark.asyncio
async def test_local_manual_equity_executes_without_recovering_old_work(repo):
    repo.settings.environment = 'LOCAL'
    repo.settings.research_equity_manual_execution_enabled = True
    async def run(**kwargs):
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 7}
    worker = OpportunityCycleWorker(repo, run, market='NSE')
    try:
        result = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
        await asyncio.wait_for(worker.queue.join(), 3)
        assert repo.persistence.cycle_run(result['cycle_id'])['status'] == 'COMPLETED'
        assert repo.settings.research_opportunity_recovery_paused is True
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('env,enabled,scheduler', [('PRD', True, False), ('LOCAL', False, False), ('LOCAL', True, True)])
async def test_local_equity_admission_restricted(repo, env, enabled, scheduler):
    repo.settings.environment = env
    repo.settings.research_equity_manual_execution_enabled = enabled
    repo.settings.research_opportunity_scheduler_enabled = scheduler
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='NSE')
    with pytest.raises(ManualEtfAdmissionError):
        await worker.submit_manual_equity_async({})
    assert repo.persistence.active_cycle_run('NSE') is None


def test_equity_503_contract_and_admin_gate(monkeypatch, repo):
    from app import main
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'environment', 'LOCAL')
    monkeypatch.setattr(main.settings, 'research_equity_manual_execution_enabled', False)

    client = TestClient(main.app)
    response = client.post('/api/v1/research/opportunities/cycles', json={'analysis_scope': 'FULL'})
    assert response.status_code == 503 and response.json() == {'detail': 'RECOVERY_PAUSED'}

    # Enable manual equity execution
    monkeypatch.setattr(main.settings, 'research_equity_manual_execution_enabled', True)
    # Without ADMIN headers, should get 401 (authorization required)
    response = client.post('/api/v1/research/opportunities/cycles', json={'analysis_scope': 'FULL'})
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize('shortlist_limit', [1, 25])
async def test_equity_ui_to_worker_execution_integration(monkeypatch, repo, shortlist_limit):
    """UI -> API -> worker -> status -> persisted-results, end to end.

    Exercises the real HTTP admission endpoint (not the worker method
    directly) under the actual LOCAL manual-equity gate, through to a
    COMPLETED durable cycle a status poll can retrieve (Completion
    Standard item 4 / Workstream 5's UI-request-to-worker-to-status
    checklist item), using an isolated in-memory fixture runner rather
    than any live research execution.
    """
    import httpx
    import app.main as main

    repo.settings.environment = 'LOCAL'
    repo.settings.research_opportunity_recovery_paused = True
    repo.settings.research_opportunity_scheduler_enabled = False
    repo.settings.research_equity_manual_execution_enabled = True

    async def fixture_cycle(**kwargs):
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 3, 'coverage': {'eligible': 1}}

    worker = OpportunityCycleWorker(repo, fixture_cycle, market='NSE')
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, '_opportunity_worker', lambda: worker)
    # _manual_equity_enabled() reads the module-level settings object,
    # not repository.settings -- mirror the same LOCAL/paused/enabled
    # gate there so the real admission check (not a bypass) is exercised.
    monkeypatch.setattr(main.settings, 'environment', 'LOCAL')
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'research_equity_manual_execution_enabled', True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test',
                                      headers=ADMIN) as client:
            accepted = await client.post('/api/v1/research/opportunities/cycles',
                json={'analysis_scope': 'BOUNDED', 'shortlist_limit': shortlist_limit})
            assert accepted.status_code == 202
            cycle_id = accepted.json()['cycle_id']
            assert accepted.json()['status'] == 'ACCEPTED'
            await asyncio.wait_for(worker.queue.join(), 3)
            status = await client.get(f'/api/v1/research/opportunities/cycles/{cycle_id}/status')
            assert status.status_code == 200
            assert status.json()['status'] == 'COMPLETED'
            assert repo.persistence.cycle_run(cycle_id)['status'] == 'COMPLETED'
            parameters = repo.persistence.cycle_run(cycle_id)['parameters']
            assert parameters['analysis_scope'] == 'BOUNDED'
            assert parameters['shortlist_limit'] == shortlist_limit
            # A second submission while this one is already terminal is a
            # fresh, independent cycle -- not an erroneous admission refusal.
            again = await client.post('/api/v1/research/opportunities/cycles',
                json={'analysis_scope': 'BOUNDED', 'shortlist_limit': shortlist_limit})
            assert again.status_code == 202
            assert again.json()['cycle_id'] != cycle_id
    finally:
        await worker.close()


@pytest.mark.parametrize('limit', [0, 101, None, 1.5])
def test_bounded_limit_rejected_before_worker_admission(monkeypatch, repo, limit):
    from app import main
    from unittest.mock import Mock

    worker_factory = Mock()
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, '_opportunity_worker', worker_factory)
    response = TestClient(main.app).post('/api/v1/research/opportunities/cycles',
        json={'analysis_scope': 'BOUNDED', 'shortlist_limit': limit}, headers=ADMIN)
    assert response.status_code == 422
    assert any(error['loc'] == ['body', 'shortlist_limit'] for error in response.json()['detail'])
    worker_factory.assert_not_called()
    assert repo.persistence.active_cycle_run('NSE') is None


def test_equity_active_cycle_conflict_reports_specific_reason_not_generic_pause(monkeypatch, repo):
    """The distinct ETF_ACTIVE_CYCLE_REQUIRES_REVIEW admission refusal must
    reach the client as its own stable code (409), not be collapsed into
    the unrelated RECOVERY_PAUSED signal (Workstream 2 item 6)."""
    from app import main
    from app.opportunity_worker import ManualEtfAdmissionError
    from unittest.mock import AsyncMock

    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'environment', 'LOCAL')
    monkeypatch.setattr(main.settings, 'research_equity_manual_execution_enabled', True)
    worker = AsyncMock()
    worker.submit_manual_equity_async.side_effect = ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
    monkeypatch.setattr(main, '_opportunity_worker', lambda: worker)

    client = TestClient(main.app)
    response = client.post('/api/v1/research/opportunities/cycles', json={'analysis_scope': 'FULL'}, headers=ADMIN)
    assert response.status_code == 409
    assert response.json() == {'detail': 'ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'}
