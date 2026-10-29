"""Paused manual ETF admission: no recovery, real durable slots, mocked providers."""
import asyncio
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.opportunity_worker import OpportunityCycleWorker, ManualEtfAdmissionError
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioServiceUnavailableError
from app.settings import Settings
from test_etf_preflight import ADMIN


@pytest.fixture
def repo():
    store = SqliteResearchPersistence()
    lock = RLock()
    async def blocking(fn, *args, **kwargs):
        with lock:
            return fn(*args, **kwargs)
    value = SimpleNamespace(persistence=store, _persistence_worker_lock=lock,
        _run_blocking_persistence=blocking, settings=SimpleNamespace(
            research_opportunity_recovery_paused=True,
            research_opportunity_scheduler_enabled=False,
            research_etf_manual_execution_enabled=True))
    yield value
    store._connection.close()


def test_gate_defaults_disabled():
    assert Settings.model_fields['research_etf_manual_execution_enabled'].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize('market,enabled,scheduler', [('NSE', True, False), ('ETF', False, False),
                                                     ('ETF', True, True), ('ETF', True, None)])
async def test_gate_blocks_without_durable_changes(repo, market, enabled, scheduler):
    repo.settings.research_etf_manual_execution_enabled = enabled
    repo.settings.research_opportunity_scheduler_enabled = scheduler
    worker = OpportunityCycleWorker(repo, AsyncMock(), market=market)
    before = list(repo.persistence._connection.iterdump())
    with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
        await worker.submit_manual_etf_async({})
    assert list(repo.persistence._connection.iterdump()) == before
    assert worker.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['ACCEPTED', 'RUNNING', 'CANCEL_REQUESTED'])
async def test_historical_slot_never_resumed_or_modified(repo, status):
    key = str(uuid4())
    repo.persistence.create_cycle_run(key, {}, market='ETF')
    if status != 'ACCEPTED':
        repo.persistence.claim_cycle_run(key, 'historical')
    if status == 'CANCEL_REQUESTED':
        repo.persistence.request_cycle_cancel(key)
    before = list(repo.persistence._connection.iterdump())
    runner = AsyncMock()
    worker = OpportunityCycleWorker(repo, runner, market='ETF')
    await worker.start_async()
    with pytest.raises(ManualEtfAdmissionError, match='ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'):
        await worker.submit_manual_etf_async({})
    await worker._take_over_async()
    assert worker.task is None
    runner.assert_not_called()
    assert list(repo.persistence._connection.iterdump()) == before


@pytest.mark.asyncio
async def test_manual_etf_leaves_equity_and_legacy_jobs_untouched_and_coalesces(repo):
    equity = str(uuid4())
    repo.persistence.create_cycle_run(equity, {}, market='NSE')
    repo.persistence.record_opportunity_job(dict(cycle_id='legacy', status='RUNNING',
        updated_at=datetime.now(timezone.utc).isoformat()))
    original = repo.persistence.cycle_run(equity)
    release = asyncio.Event()
    async def run(**kwargs):
        await release.wait()
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 1}
    worker = OpportunityCycleWorker(repo, run, market='ETF', poll_seconds=0.01)
    try:
        results = await asyncio.gather(*(worker.submit_manual_etf_async({}) for _ in range(8)))
        assert len({r['cycle_id'] for r in results}) == 1
        assert sum(bool(r.get('coalesced')) for r in results) == 7
        release.set()
        await asyncio.wait_for(worker.queue.join(), 2)
        assert repo.persistence.cycle_run(results[0]['cycle_id'])['status'] == 'COMPLETED'
        assert repo.persistence.cycle_run(equity) == original
        assert next(j for j in repo.persistence.opportunity_jobs() if j['cycle_id'] == 'legacy')['status'] == 'RUNNING'
        # Even an expired historical ETF slot appearing during idle polling stays untouched.
        old = str(uuid4())
        repo.persistence.create_cycle_run(old, {}, market='ETF')
        await asyncio.sleep(0.04)
        assert repo.persistence.cycle_run(old)['status'] == 'ACCEPTED'
        assert repo.persistence.cycle_run(old)['owner_id'] is None
        equity_worker = OpportunityCycleWorker(repo, AsyncMock(), market='NSE')
        assert (await equity_worker.submit_async({}))['paused'] is True
        assert equity_worker.task is None
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_two_workers_arbitrate_slot_and_restart_never_resumes(repo):
    runner = AsyncMock(side_effect=lambda **kw: None)
    hold = asyncio.Event()
    async def run(**kwargs):
        await hold.wait()
    workers = [OpportunityCycleWorker(repo, run, market='ETF') for _ in range(2)]
    try:
        results = await asyncio.gather(*(w.submit_manual_etf_async({}) for w in workers), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, ManualEtfAdmissionError) for r in results) == 1
        winner = next(r for r in results if isinstance(r, dict))
        await asyncio.sleep(0.01)
    finally:
        for worker in workers:
            await worker.close()
    replacement = OpportunityCycleWorker(repo, runner, market='ETF')
    await replacement.start_async()
    with pytest.raises(ManualEtfAdmissionError, match='ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'):
        await replacement.submit_manual_etf_async({})
    runner.assert_not_called()
    assert repo.persistence.cycle_run(winner['cycle_id'])['resume_count'] == 0
    job = next(j for j in repo.persistence.opportunity_jobs() if j['cycle_id'] == winner['cycle_id'])
    assert job['error_code'] == 'ETF_MANUAL_RUN_INTERRUPTED_REVIEW_REQUIRED'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code', [('empty', 'ETF_CANONICAL_UNIVERSE_EMPTY'),
    ('unavailable', 'ETF_CANONICAL_UNIVERSE_UNAVAILABLE'), ('malformed', 'ETF_CANONICAL_UNIVERSE_UNAVAILABLE')])
async def test_full_universe_failure_is_durable_and_preserves_results_without_nse_io(repo, failure, code):
    from app.etf_full_universe import run_full_etf_cycle
    now = datetime.now(timezone.utc)
    saved = {'cycle_id': 'previous', 'candidates': [{'reason': 'saved'}]}
    repo.persistence.save_etf_radar_cycle('previous', 'ETF_RADAR_V1', None, now, saved)
    source = AsyncMock(return_value=[] if failure == 'empty' else [None])
    if failure == 'unavailable':
        source.side_effect = PortfolioServiceUnavailableError('must not become a public error')
    transport = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('NSE called')))
    async def run(**kwargs):
        return await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=source,
            etf_catalog_onboarding_unsupported=True),
            checkpoint=kwargs['checkpoint'], cycle_id=kwargs['cycle_id'], identity_headers={}, client=transport)
    worker = OpportunityCycleWorker(repo, run, market='ETF')
    try:
        accepted = await worker.submit_manual_etf_async({})
        await asyncio.wait_for(worker.queue.join(), 2)
        run = repo.persistence.cycle_run(accepted['cycle_id'])
        assert (run['status'], run['error_code']) == ('FAILED', code)
        assert repo.persistence.latest_etf_radar_cycle() == saved
        transport.get.assert_not_called()
    finally:
        await worker.close()


@pytest.mark.parametrize('enabled,scheduler,expected', [(False, False, 503), (True, True, 503), (True, False, 202)])
def test_admin_endpoint_routes_only_explicit_full_universe_gate(monkeypatch, enabled, scheduler, expected):
    # Full-universe execution is gated by its OWN flag
    # (research_etf_full_universe_execution_enabled), never by the bounded
    # research_etf_manual_execution_enabled flag -- see
    # test_bounded_manual_gate_never_authorizes_full_universe below for the
    # cross-check in the other direction.
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', enabled)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', scheduler)
    manual = AsyncMock(return_value={'cycle_id': str(uuid4()), 'status': 'ACCEPTED'})
    worker = SimpleNamespace(submit_manual_etf_async=manual, submit_async=Mock(side_effect=AssertionError('normal submission')))
    monkeypatch.setattr(main, '_etf_radar_worker', lambda: worker)
    client = TestClient(main.app)
    path = '/api/v1/etf-radar/full-universe-cycles'
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, json={}, headers=ADMIN | {'X-AIP-User-Roles': 'USER'}).status_code == 403
    assert client.post(path, json={'max_requests': 15001}, headers=ADMIN).status_code == 422
    assert client.post(path, json={}, headers=ADMIN).status_code == expected
    assert manual.await_count == (1 if expected == 202 else 0)
    assert client.post('/api/v1/etf-radar/cycles', json={}, headers=ADMIN).status_code == 503
    if expected == 202:
        assert manual.await_args.args[0]['full_universe_limits'] == dict(max_universe=2000, max_requests=1500, max_seconds=3600)
        manual.side_effect = ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
        blocked = client.post(path, json={}, headers=ADMIN)
        assert blocked.status_code == 409
        assert blocked.json() == {'detail': 'ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'}


def test_gate_defaults_disabled_full_universe():
    assert Settings.model_fields['research_etf_full_universe_execution_enabled'].default is False


@pytest.mark.parametrize('recovery_paused,full_universe,scheduler,expected', [
    (True, False, False, 503),   # Paused, flag off -> still blocked (pre-existing coverage).
    (True, True, False, 202),    # Paused, flag on, scheduler off -> authorized (pre-existing coverage).
    (False, False, True, 503),   # UNPAUSED production path, flag off -> must fail closed, not silently admit.
    (False, False, False, 503),  # UNPAUSED, flag off, scheduler also off -> still must fail closed.
    (False, True, True, 202),    # UNPAUSED, flag explicitly on -> production path may proceed.
])
def test_full_universe_gate_enforced_on_every_submission_branch(monkeypatch, recovery_paused, full_universe, scheduler, expected):
    # Regression for the production-path bypass: full_etf_cycle() used to
    # check research_etf_full_universe_execution_enabled only inside the
    # recovery-paused branch. When recovery_paused=False it routed straight
    # to the generic worker.submit_async(), which has no knowledge of this
    # scope-specific flag -- silently admitting a full-universe cycle
    # regardless of the flag's value. The flag must now gate BOTH branches.
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', recovery_paused)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', full_universe)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', scheduler)
    manual = AsyncMock(return_value={'cycle_id': str(uuid4()), 'status': 'ACCEPTED'})
    normal = AsyncMock(return_value={'cycle_id': str(uuid4()), 'status': 'ACCEPTED'})
    worker = SimpleNamespace(submit_manual_etf_async=manual, submit_async=normal)
    monkeypatch.setattr(main, '_etf_radar_worker', lambda: worker)
    client = TestClient(main.app)
    response = client.post('/api/v1/etf-radar/full-universe-cycles', json={}, headers=ADMIN)
    assert response.status_code == expected
    if expected != 202:
        assert response.json() == {'detail': 'FULL_UNIVERSE_EXECUTION_NOT_AUTHORIZED'}
        assert manual.await_count == 0
        assert normal.await_count == 0
    elif recovery_paused:
        assert manual.await_count == 1
        assert normal.await_count == 0
    else:
        assert normal.await_count == 1
        assert manual.await_count == 0



@pytest.mark.parametrize('bounded,full_universe,expected_full_universe_status', [
    (True, False, 503),   # Bounded alone must NEVER authorize full-universe.
    (False, True, 202),   # Full-universe flag alone is sufficient on its own.
    (True, True, 202),    # Both enabled: full-universe still works on its own flag.
])
def test_bounded_manual_gate_never_authorizes_full_universe(monkeypatch, bounded, full_universe, expected_full_universe_status):
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', bounded)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', full_universe)
    manual = AsyncMock(return_value={'cycle_id': str(uuid4()), 'status': 'ACCEPTED'})
    worker = SimpleNamespace(submit_manual_etf_async=manual, submit_async=Mock(side_effect=AssertionError('normal submission')))
    monkeypatch.setattr(main, '_etf_radar_worker', lambda: worker)
    client = TestClient(main.app)
    response = client.post('/api/v1/etf-radar/full-universe-cycles', json={}, headers=ADMIN)
    assert response.status_code == expected_full_universe_status
    assert manual.await_count == (1 if expected_full_universe_status == 202 else 0)
    if expected_full_universe_status != 202:
        assert response.json() == {'detail': 'FULL_UNIVERSE_EXECUTION_NOT_AUTHORIZED'}


def test_full_universe_gate_independent_predicate_functions(monkeypatch):
    # Direct, HTTP-free confirmation that the two authorization predicates
    # read entirely disjoint settings fields (Stage C requirement: bounded
    # and full-universe execution must have distinguishable permissions).
    import app.main as main
    monkeypatch.setattr(main.settings, 'research_opportunity_recovery_paused', True)
    monkeypatch.setattr(main.settings, 'research_opportunity_scheduler_enabled', False)
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', True)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', False)
    assert main._manual_etf_enabled() is True
    assert main._full_universe_etf_enabled() is False
    monkeypatch.setattr(main.settings, 'research_etf_manual_execution_enabled', False)
    monkeypatch.setattr(main.settings, 'research_etf_full_universe_execution_enabled', True)
    assert main._manual_etf_enabled() is False
    assert main._full_universe_etf_enabled() is True


@pytest.mark.asyncio
async def test_lifespan_stays_inert_even_with_manual_gate(repo, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'settings', repo.settings)
    monkeypatch.setattr(main, 'repository', repo)
    for name in ('_opportunity_worker', '_etf_radar_worker', '_opportunity_scheduler'):
        monkeypatch.setattr(main, name, Mock(side_effect=AssertionError('startup recovery')))
    async with main.research_lifespan(SimpleNamespace(state=SimpleNamespace())):
        pass
    from app.global_opportunity_scheduler import GlobalOpportunityScheduler
    persistence = Mock()
    scheduler = GlobalOpportunityScheduler(OpportunityCycleWorker(repo, AsyncMock()), persistence)
    scheduler.start()
    assert scheduler.task is None and persistence.mock_calls == []


@pytest.mark.asyncio
async def test_manual_full_cycle_persists_real_fixture_analysis(repo):
    import httpx
    from app.etf_full_universe import run_full_etf_cycle
    from test_etf_full_universe import universe
    from test_etf_ingestion import NOW
    rows, csv = universe(7)
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith('.csv'):
            return httpx.Response(200, text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200, json={'data': {'timestamp': NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate': NOW.strftime('%d-%b-%Y'), 'data': [dict(symbol=r['primarySymbol'], series='EQ',
                identifier=r['primarySymbol']+'EQN', lastPrice=100, totalTradedVolume=500000, nav=100) for r in rows]}})
        row = next(r for r in rows if r['primarySymbol'] == request.url.params['symbol'])
        return httpx.Response(200, json=dict(symbol=row['primarySymbol'], isin=row['isin'], isETFSec='true',
            isDelisted='false', isSuspended='false', activeSeries=['EQ'], marketType='N'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async def run(**kwargs):
            return await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows),
                etf_catalog_onboarding_unsupported=True),
                checkpoint=kwargs['checkpoint'], cycle_id=kwargs['cycle_id'], identity_headers={}, client=client,
                clock=lambda: NOW, sleep=AsyncMock(), limits=kwargs['full_universe_limits'])
        worker = OpportunityCycleWorker(repo, run, market='ETF')
        try:
            accepted = await worker.submit_manual_etf_async({'full_universe_limits':
                dict(max_universe=7, max_requests=20, max_seconds=30)})
            await asyncio.wait_for(worker.queue.join(), 10)
            saved = repo.persistence.latest_etf_radar_cycle()
            assert repo.persistence.cycle_run(accepted['cycle_id'])['status'] == 'COMPLETED'
            assert saved['cycle_id'] == accepted['cycle_id']
            assert saved['coverage']['official'] == saved['coverage']['acquired'] == saved['coverage']['evaluated'] == 7
            assert len(saved['candidates']) == 7
            assert len(calls) == 9
            assert repo.persistence.active_cycle_run('ETF') is None
            assert repo.persistence.active_cycle_run('NSE') is None
        finally:
            await worker.close()


@pytest.mark.asyncio
async def test_explicit_manual_run_retains_cancellation_and_lease_fencing(repo):
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(**kwargs):
        entered.set()
        await release.wait()
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 1}
    worker = OpportunityCycleWorker(repo, run, market='ETF', lease_seconds=1)
    try:
        result = await worker.submit_manual_etf_async({})
        await asyncio.wait_for(entered.wait(), 2)
        current = repo.persistence.cycle_run(result['cycle_id'])
        assert current['owner_id'] == worker.owner_id and current['lease_expires_at']
        repo.persistence.request_cycle_cancel(result['cycle_id'])
        with pytest.raises(ManualEtfAdmissionError, match='ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'):
            await worker.submit_manual_etf_async({})
        release.set()
        await asyncio.wait_for(worker.queue.join(), 2)
        assert repo.persistence.cycle_run(result['cycle_id'])['status'] == 'CANCELLED'
    finally:
        release.set()
        await worker.close()


def _scoped_repo(**settings_overrides):
    """A real-worker repo fixture, like the module-level `repo` fixture, but
    with explicitly controlled ETF admission settings -- for exercising the
    scope-aware admission gate's combinations directly (bounded flag,
    full-universe flag, recovery paused, scheduler enabled), independent of
    this file's single shared fixture."""
    store = SqliteResearchPersistence()
    lock = RLock()
    async def blocking(fn, *args, **kwargs):
        with lock:
            return fn(*args, **kwargs)
    defaults = dict(research_opportunity_recovery_paused=True, research_opportunity_scheduler_enabled=False,
        research_etf_manual_execution_enabled=False, research_etf_full_universe_execution_enabled=False)
    defaults.update(settings_overrides)
    return SimpleNamespace(persistence=store, _persistence_worker_lock=lock,
        _run_blocking_persistence=blocking, settings=SimpleNamespace(**defaults))


@pytest.mark.asyncio
async def test_full_universe_enabled_bounded_disabled_admits_full_rejects_bounded():
    """A real OpportunityCycleWorker (not a mocked endpoint) admits a
    FULL_UNIVERSE-scope submission when only research_etf_full_universe_
    execution_enabled is set, and rejects a BOUNDED-scope submission under
    the exact same settings -- the two flags are independent, and the
    worker's own admission gate (not just app.main's) must honor that."""
    repo = _scoped_repo(research_etf_full_universe_execution_enabled=True)
    worker = OpportunityCycleWorker(repo, AsyncMock(return_value={'cycle_id': 'x', 'universe_count': 0}), market='ETF')
    try:
        accepted = await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
        assert accepted['status'] == 'ACCEPTED'
        await asyncio.wait_for(worker.queue.join(), 2)
    finally:
        await worker.close()

    repo2 = _scoped_repo(research_etf_full_universe_execution_enabled=True)
    worker2 = OpportunityCycleWorker(repo2, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker2.submit_manual_etf_async({}, scope='BOUNDED')
    finally:
        await worker2.close()


@pytest.mark.asyncio
async def test_bounded_enabled_full_universe_disabled_admits_bounded_rejects_full():
    """Mirror of the above: only research_etf_manual_execution_enabled set
    admits BOUNDED and rejects FULL_UNIVERSE under identical settings."""
    repo = _scoped_repo(research_etf_manual_execution_enabled=True)
    worker = OpportunityCycleWorker(repo, AsyncMock(return_value={'cycle_id': 'x', 'universe_count': 0}), market='ETF')
    try:
        accepted = await worker.submit_manual_etf_async({}, scope='BOUNDED')
        assert accepted['status'] == 'ACCEPTED'
        await asyncio.wait_for(worker.queue.join(), 2)
    finally:
        await worker.close()

    repo2 = _scoped_repo(research_etf_manual_execution_enabled=True)
    worker2 = OpportunityCycleWorker(repo2, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_FULL_UNIVERSE_EXECUTION_DISABLED'):
            await worker2.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
    finally:
        await worker2.close()


@pytest.mark.asyncio
async def test_both_scopes_disabled_rejects_both():
    repo = _scoped_repo()
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='BOUNDED')
        with pytest.raises(ManualEtfAdmissionError, match='ETF_FULL_UNIVERSE_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_scheduler_enabled_rejects_both_scopes():
    """The scheduler-disabled precondition is shared by both scopes: turning
    the scheduler on blocks admission even with both flags enabled."""
    repo = _scoped_repo(research_etf_manual_execution_enabled=True, research_etf_full_universe_execution_enabled=True,
        research_opportunity_scheduler_enabled=True)
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='BOUNDED')
        with pytest.raises(ManualEtfAdmissionError, match='ETF_FULL_UNIVERSE_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_recovery_not_paused_rejects_paused_manual_admission():
    """Both scopes share the recovery-paused precondition: with recovery NOT
    paused, admission is rejected even with the relevant flag enabled (this
    submission path exists only for the explicitly-authorized, paused
    diagnostic flow -- day-to-day production cycles use submit_async)."""
    repo = _scoped_repo(research_opportunity_recovery_paused=False,
        research_etf_manual_execution_enabled=True, research_etf_full_universe_execution_enabled=True)
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='BOUNDED')
        with pytest.raises(ManualEtfAdmissionError, match='ETF_FULL_UNIVERSE_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_caller_cannot_forge_or_escalate_execution_scope():
    """scope is a keyword-only server-side argument, never inferred from
    `parameters`: an invalid value is rejected outright, and including a
    'full_universe_limits'-shaped field in `parameters` under an unspecified
    (default BOUNDED) scope never escalates admission to full-universe
    authorization -- only the explicit `scope` argument matters."""
    repo = _scoped_repo(research_etf_full_universe_execution_enabled=True)
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='ETF')
    try:
        with pytest.raises(ValueError, match="invalid ETF execution scope"):
            await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE; DROP')
        # Only full-universe is enabled; a bounded-shaped call that merely
        # carries a full_universe_limits field, with no explicit scope, must
        # still be evaluated as BOUNDED (the default) and rejected.
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker.submit_manual_etf_async({'full_universe_limits': {'max_universe': 2000}})
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_single_active_etf_cycle_protection_applies_across_both_scopes():
    """The existing single-active-cycle arbitration (ETF_ACTIVE_CYCLE_
    REQUIRES_REVIEW / ETF_WORKER_BUSY) must still work unchanged, and applies
    uniformly regardless of which scope occupies the active slot: a
    FULL_UNIVERSE cycle still blocks a subsequent BOUNDED submission (and
    vice versa) -- the two scopes do not get independent worker slots."""
    entered, release = asyncio.Event(), asyncio.Event()
    async def run(**kwargs):
        entered.set()
        await release.wait()
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 1}
    repo = _scoped_repo(research_etf_manual_execution_enabled=True, research_etf_full_universe_execution_enabled=True)
    worker = OpportunityCycleWorker(repo, run, market='ETF')
    try:
        accepted = await worker.submit_manual_etf_async({}, scope='FULL_UNIVERSE')
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ManualEtfAdmissionError, match='ETF_ACTIVE_CYCLE_REQUIRES_REVIEW'):
            await worker.submit_manual_etf_async({}, scope='BOUNDED')
        repo.persistence.request_cycle_cancel(accepted['cycle_id'])
        release.set()
        await asyncio.wait_for(worker.queue.join(), 2)
        assert repo.persistence.cycle_run(accepted['cycle_id'])['status'] == 'CANCELLED'
    finally:
        release.set()
        await worker.close()
