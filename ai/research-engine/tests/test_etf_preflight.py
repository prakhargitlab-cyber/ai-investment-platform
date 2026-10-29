"""Offline bounded preflight and the existing durable ETF worker integration."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.etf_ingestion import canonical_etfs, etf_preflight
from app.etf_opportunity_cycle import run_etf_radar_cycle_async
from app.opportunity_worker import OpportunityCycleWorker
from test_etf_ingestion import repo, ingest, ID, META, NOW

@pytest.fixture(autouse=True)
def compatible_dispatch(repo):
    async def dispatch(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    repo._run_blocking_persistence = dispatch


ADMIN = {'X-AIP-User-Id': str(UUID(int=1)), 'X-AIP-User-Issuer': 'test',
         'X-AIP-User-Subject': 'admin', 'X-AIP-User-Roles': 'ADMIN'}


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['fresh', 'missing', 'stale', 'identity_mismatch'])
async def test_preflight_read_only_and_truthful(repo, variant):
    if variant != 'missing':
        await ingest(repo)
    row = META | ({'isin': 'CONFLICT'} if variant == 'identity_mismatch' else {})
    before = '\n'.join(repo.persistence._connection.iterdump())
    queries = []
    repo.persistence._connection.set_trace_callback(queries.append)
    result = etf_preflight(repo.persistence, [row], now=NOW + timedelta(days=4 if variant == 'stale' else 0))
    repo.persistence._connection.set_trace_callback(None)
    assert result['status'] == ('READY' if variant == 'fresh' else 'BLOCKED')
    assert result['candidate_count'] == 1
    assert '\n'.join(repo.persistence._connection.iterdump()) == before
    assert not any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for q in queries)
    if variant == 'fresh':
        item = result['instruments'][0]
        assert item['summary']['mandatory_ready'] == 3
        assert item['summary']['supporting_missing'] > 0
        price = next(s for s in item['readiness'] if s['requirement'] == 'MARKET_PRICE')
        assert price['provider'] == 'NSE' and price['as_of_date'] == NOW.date()


@pytest.mark.asyncio
@pytest.mark.parametrize('headers,expected', [({}, 401), (ADMIN | {'X-AIP-User-Roles': 'USER'}, 403), (ADMIN, 200)])
async def test_preflight_authorization_and_no_worker(repo, monkeypatch, headers, expected):
    import app.main as main
    metadata = AsyncMock(return_value=META)
    monkeypatch.setattr(main, 'portfolio_orchestrator', SimpleNamespace(global_instrument_metadata=metadata))
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, '_etf_radar_worker', lambda: pytest.fail('preflight started worker'))
    from httpx import AsyncClient, ASGITransport
    before = '\n'.join(repo.persistence._connection.iterdump())
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.get('/api/v1/etf-radar/preflight', params={'instrument_ids': str(ID)}, headers=headers)
    assert response.status_code == expected
    if expected == 200:
        assert response.json()['status'] == 'BLOCKED'
        metadata.assert_awaited_once()
    else:
        metadata.assert_not_called()
    assert '\n'.join(repo.persistence._connection.iterdump()) == before


@pytest.mark.parametrize('ids', [[], [str(ID)] * 2, [str(UUID(int=i+1)) for i in range(6)], ['bad-id']])
def test_preflight_invalid_selection(repo, monkeypatch, ids):
    import app.main as main
    metadata = AsyncMock(return_value=META)
    monkeypatch.setattr(main, 'portfolio_orchestrator', SimpleNamespace(global_instrument_metadata=metadata))
    monkeypatch.setattr(main, 'repository', repo)
    response = TestClient(main.app).get('/api/v1/etf-radar/preflight', params=[('instrument_ids', v) for v in ids], headers=ADMIN)
    assert response.status_code == 422
    metadata.assert_not_called()


@pytest.mark.asyncio
async def test_canonical_source_is_point_lookup_never_enumeration():
    metadata = AsyncMock(return_value=META)
    source = SimpleNamespace(global_instrument_metadata=metadata,
                             active_global_etfs=AsyncMock(side_effect=AssertionError('enumeration')))
    assert await canonical_etfs(source, [ID], identity_headers=ADMIN) == [META]
    metadata.assert_awaited_once_with(ID, identity_headers=ADMIN, correlation_id=None)
    source.active_global_etfs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['complete', 'cancel', 'evidence_changed'])
async def test_bounded_worker_publication_is_fenced_and_scoped(repo, monkeypatch, mode):
    import app.etf_ingestion as module
    await ingest(repo)
    original = module.etf_preflight
    clock = [NOW]
    monkeypatch.setattr(module, 'etf_preflight', lambda store, rows: original(store, rows, now=clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    async def metadata(*args, **kwargs):
        entered.set()
        await release.wait()
        return META
    source = SimpleNamespace(global_instrument_metadata=metadata)
    async def runner(**kwargs):
        return await run_etf_radar_cycle_async(repo, source, identity_headers=ADMIN, **kwargs)
    worker = OpportunityCycleWorker(repo, runner, market='ETF')
    try:
        job = await worker.submit_async({'candidate_ids': [str(ID)], 'verify_canonical_candidates': True, 'top_n': None})
        await asyncio.wait_for(entered.wait(), 2)
        if mode == 'cancel':
            repo.persistence.request_cycle_cancel(job['cycle_id'])
        elif mode == 'evidence_changed':
            # Change only the isolated fixture's readiness clock after admission.
            clock[0] = NOW + timedelta(days=4)
        release.set()
        await asyncio.wait_for(worker.queue.join(), 5)
        run = repo.persistence.cycle_run(job['cycle_id'])
        assert run['status'] == {'complete': 'COMPLETED', 'cancel': 'CANCELLED', 'evidence_changed': 'FAILED'}[mode]
        saved = repo.persistence.etf_radar_cycle(job['cycle_id'])
        assert bool(saved) == (mode == 'complete')
        if saved:
            assert len(saved['candidates']) == 1
        assert repo.persistence.active_cycle_run('NSE') is None
    finally:
        release.set()
        await worker.close()


@pytest.mark.asyncio
async def test_controlled_endpoint_uses_durable_worker_and_pause(repo, monkeypatch):
    import app.main as main
    import app.etf_ingestion as module
    from starlette.requests import Request
    await ingest(repo)
    original = module.etf_preflight
    clock = [NOW]
    monkeypatch.setattr(module, 'etf_preflight', lambda store, rows: original(store, rows, now=clock[0]))
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, 'portfolio_orchestrator', SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META)))
    submit = AsyncMock(return_value={'cycle_id': str(UUID(int=90)), 'status': 'ACCEPTED'})
    monkeypatch.setattr(main, '_etf_radar_worker', lambda: SimpleNamespace(submit_async=submit))
    request = Request({'type': 'http', 'headers': [(k.lower().encode(), v.encode()) for k,v in ADMIN.items()]})
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    with pytest.raises(HTTPException) as error:
        await main.controlled_etf_cycle(main.EtfEvidenceRequest(instrument_ids=[ID]), request)
    assert error.value.status_code == 503
    submit.assert_not_called()
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', False)
    response = await main.controlled_etf_cycle(main.EtfEvidenceRequest(instrument_ids=[ID]), request)
    assert response.status_code == 202
    submit.assert_awaited_once_with({'top_n': None, 'candidate_ids': [str(ID)], 'verify_canonical_candidates': True})


def _paused_controlled_cycle_request():
    from starlette.requests import Request
    return Request({'type': 'http', 'headers': [(k.lower().encode(), v.encode()) for k, v in ADMIN.items()]})


async def _run_controlled_cycle(repo, monkeypatch, *, environment, scheduler_enabled, manual_enabled,
                                 full_universe_enabled=False):
    """Shared harness for the bounded-manual-execution authorization matrix
    below: wires a mocked worker exposing BOTH submit_async and
    submit_manual_etf_async, drives /controlled-cycles with the given
    settings, and returns (response_or_exception, submit_async_mock,
    submit_manual_etf_async_mock) so each test asserts exactly which one
    (if either) was actually invoked."""
    import app.main as main
    import app.etf_ingestion as module
    await ingest(repo)
    original = module.etf_preflight
    monkeypatch.setattr(module, 'etf_preflight', lambda store, rows: original(store, rows, now=NOW))
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, 'portfolio_orchestrator', SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META)))
    submit_async = AsyncMock(return_value={'cycle_id': str(UUID(int=90)), 'status': 'ACCEPTED'})
    submit_manual = AsyncMock(return_value={'cycle_id': str(UUID(int=91)), 'status': 'ACCEPTED'})
    monkeypatch.setattr(main, '_etf_radar_worker',
        lambda: SimpleNamespace(submit_async=submit_async, submit_manual_etf_async=submit_manual))
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'environment', environment)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', scheduler_enabled)
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', manual_enabled)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', full_universe_enabled)
    request = _paused_controlled_cycle_request()
    try:
        response = await main.controlled_etf_cycle(main.EtfEvidenceRequest(instrument_ids=[ID]), request)
    except HTTPException as exc:
        response = exc
    return response, submit_async, submit_manual


@pytest.mark.asyncio
async def test_controlled_cycle_manual_gate_admits_bounded_execution_while_paused(repo, monkeypatch):
    """Positive case: LOCAL environment, scheduler disabled, manual flag
    enabled -- a controlled (explicit-candidate) ETF cycle is admitted
    through the worker's own BOUNDED manual-admission path even though the
    scheduler is paused, exactly mirroring Equity's existing LOCAL-only
    manual-execution design. The generic submit_async path must NOT be
    used for this authorized paused submission."""
    response, submit_async, submit_manual = await _run_controlled_cycle(
        repo, monkeypatch, environment='LOCAL', scheduler_enabled=False, manual_enabled=True)
    assert not isinstance(response, HTTPException)
    assert response.status_code == 202
    submit_manual.assert_awaited_once_with(
        {'top_n': None, 'candidate_ids': [str(ID)], 'verify_canonical_candidates': True})
    submit_async.assert_not_called()


@pytest.mark.asyncio
async def test_controlled_cycle_rejects_paused_execution_when_manual_flag_disabled(repo, monkeypatch):
    """Negative case: the deployed flag stays false -> paused execution
    remains rejected, exactly as before this gate existed."""
    response, submit_async, submit_manual = await _run_controlled_cycle(
        repo, monkeypatch, environment='LOCAL', scheduler_enabled=False, manual_enabled=False)
    assert isinstance(response, HTTPException) and response.status_code == 503
    assert response.detail == 'RECOVERY_PAUSED'
    submit_async.assert_not_called()
    submit_manual.assert_not_called()


@pytest.mark.asyncio
async def test_controlled_cycle_rejects_paused_execution_outside_local_environment(repo, monkeypatch):
    """Negative case: every other precondition satisfied, but the
    environment is not LOCAL -- paused execution remains rejected."""
    response, submit_async, submit_manual = await _run_controlled_cycle(
        repo, monkeypatch, environment='PRODUCTION', scheduler_enabled=False, manual_enabled=True)
    assert isinstance(response, HTTPException) and response.status_code == 503
    assert response.detail == 'RECOVERY_PAUSED'
    submit_async.assert_not_called()
    submit_manual.assert_not_called()


@pytest.mark.asyncio
async def test_controlled_cycle_rejects_paused_execution_when_scheduler_enabled(repo, monkeypatch):
    """Negative case: the scheduler being enabled blocks the manual override
    even with LOCAL environment and the flag enabled."""
    response, submit_async, submit_manual = await _run_controlled_cycle(
        repo, monkeypatch, environment='LOCAL', scheduler_enabled=True, manual_enabled=True)
    assert isinstance(response, HTTPException) and response.status_code == 503
    assert response.detail == 'RECOVERY_PAUSED'
    submit_async.assert_not_called()
    submit_manual.assert_not_called()


@pytest.mark.asyncio
async def test_controlled_cycle_full_universe_flag_never_authorizes_bounded_execution(repo, monkeypatch):
    """Cross-check (the reverse of the existing full-universe-side
    isolation test): enabling ONLY the full-universe flag must never admit
    a bounded controlled-cycle submission while paused -- the two scopes
    stay independently authorizable."""
    response, submit_async, submit_manual = await _run_controlled_cycle(
        repo, monkeypatch, environment='LOCAL', scheduler_enabled=False, manual_enabled=False,
        full_universe_enabled=True)
    assert isinstance(response, HTTPException) and response.status_code == 503
    assert response.detail == 'RECOVERY_PAUSED'
    submit_async.assert_not_called()
    submit_manual.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_reports_manual_execution_authorized_flag(repo, monkeypatch):
    """The preflight response itself exposes manual_execution_authorized so
    a caller (or the UI) can distinguish 'paused, no override' from 'paused,
    override active' without inferring it from a 503 vs 202 outcome."""
    import app.main as main
    import app.etf_ingestion as module
    await ingest(repo)
    original = module.etf_preflight
    monkeypatch.setattr(module, 'etf_preflight', lambda store, rows: original(store, rows, now=NOW))
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, 'portfolio_orchestrator', SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META)))
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'environment', 'LOCAL')
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', False)
    request = _paused_controlled_cycle_request()
    blocked = await main._controlled_etf_preflight([ID], request)
    assert blocked['recovery_paused'] is True
    assert blocked['manual_execution_authorized'] is False
    assert blocked['can_submit'] is False
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', True)
    authorized = await main._controlled_etf_preflight([ID], request)
    assert authorized['recovery_paused'] is True
    assert authorized['manual_execution_authorized'] is True
    assert authorized['can_submit'] is True


@pytest.mark.asyncio
async def test_worker_close_exits_when_cleanup_consumes_cancellation(repo, monkeypatch):
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='ETF')
    entered = asyncio.Event()
    async def cleanup(_value):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return  # Models cancellation consumed while awaiting heartbeat cleanup.
    monkeypatch.setattr(worker, '_run_one', cleanup)
    worker.queue.put_nowait({'cycle_id': str(ID)})
    worker.task = asyncio.create_task(worker._work())
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(worker.close())
    done, _ = await asyncio.wait({closing}, timeout=0.2)
    if not done:
        worker.task.cancel()  # Tear down the broken implementation without hanging pytest.
        await closing
    assert done, 'worker ignored shutdown after cleanup consumed cancellation'
    assert worker.task is None
    assert worker.queue.empty()
