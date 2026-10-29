"""Manual Equity cycle: lease starvation, lapse without successor, supersession,
terminal preservation and explicit operator resume.

Production (cycle 86f0567d-978e-4fcc-a9c5-337ed729db69, 2026-10-29): a healthy,
never-restarted research-engine pod ran a manual FULL Equity cycle under the
LOCAL manual gate (recovery paused, scheduler disabled). Its lease lapsed while
Stage 2 was still running; the next fenced progress write raised
CycleOwnershipLost, the worker logged opportunity_cycle_ownership_lost and then
opportunity_cycle_status_suppressed reason=SUPERSEDED -- although no other
worker existed or could exist (takeover is disabled while recovery is paused).
The cycle stayed RUNNING with an expired lease and an occupied active slot, and
every new manual submission was refused with ETF_ACTIVE_CYCLE_REQUIRES_REVIEW.

These tests use the real SQLite cycle store (DB-clock leases and fencing) and
an in-process worker; no provider, cluster or live cycle is involved.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.cycle_checkpoint import CandidateState, CycleOwnershipLost, PHASE_DEEP
from app.opportunity_worker import ManualEtfAdmissionError, OpportunityCycleWorker
from test_etf_manual_execution import repo  # noqa: F401 (fixture)


def _manual_gate(repo):
    repo.settings.environment = 'LOCAL'
    repo.settings.research_opportunity_recovery_paused = True
    repo.settings.research_opportunity_scheduler_enabled = False
    repo.settings.research_equity_manual_execution_enabled = True


def _expire_lease(repo, cycle_id):
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec='microseconds')
    with repo.persistence._connection:
        repo.persistence._connection.execute(
            "UPDATE global_opportunity_cycle_run SET lease_expires_at = ? WHERE cycle_id = ?", (past, cycle_id))


def _job(repo, cycle_id):
    return next(j for j in repo.persistence.opportunity_jobs() if j['cycle_id'] == cycle_id)


async def _settle(worker, timeout=5):
    await asyncio.wait_for(worker.queue.join(), timeout)


# -- long-running cycle: renewal must not queue behind persistence backlog ----

@pytest.mark.asyncio
async def test_long_running_cycle_keeps_lease_while_persistence_queue_is_saturated(repo):
    """The heartbeat renews through the repository's priority lease path, so a
    saturated general persistence queue (Stage 2 backlog) cannot starve it
    past expiry. Fencing is unchanged: the renewal is the same owner-fenced
    UPDATE, still serialized on the shared connection lock."""
    _manual_gate(repo)
    backlog_open = asyncio.Event()
    general_blocking = repo._run_blocking_persistence

    async def saturated_general_queue(fn, *args, **kwargs):
        # Ordinary persistence waits behind a long backlog while the runner is busy.
        await backlog_open.wait()
        return await general_blocking(fn, *args, **kwargs)

    async def priority_lease_path(fn, *args, **kwargs):
        with repo._persistence_worker_lock:
            return fn(*args, **kwargs)

    async def long_stage2(**kwargs):
        repo._run_blocking_persistence = saturated_general_queue
        try:
            await asyncio.sleep(1.2)  # four lease periods of pure Stage 2 work
        finally:
            repo._run_blocking_persistence = general_blocking
            backlog_open.set()
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 1}

    repo._run_lease_persistence = priority_lease_path
    worker = OpportunityCycleWorker(repo, long_stage2, market='NSE', lease_seconds=0.3)
    try:
        submitted = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
        await _settle(worker)
        run = repo.persistence.cycle_run(submitted['cycle_id'])
        assert run['status'] == 'COMPLETED'
        assert _job(repo, submitted['cycle_id'])['status'] == 'COMPLETED'
        assert repo.persistence.active_cycle_run('NSE') is None
    finally:
        await worker.close()


# -- lease lapsed with no successor: truthful, resumable, never terminal ------

@pytest.mark.asyncio
async def test_lapsed_lease_without_successor_records_truthful_resumable_status(repo):
    _manual_gate(repo)

    async def stage2_that_loses_its_lease(**kwargs):
        _expire_lease(repo, kwargs['cycle_id'])
        await kwargs['checkpoint'].start(PHASE_DEEP, 'instrument-1')  # first fenced write after lapse
        raise AssertionError('fenced write must not succeed after the lease lapsed')

    worker = OpportunityCycleWorker(repo, stage2_that_loses_its_lease, market='NSE')
    try:
        submitted = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
        await _settle(worker)
        cycle_id = submitted['cycle_id']
        run = repo.persistence.cycle_run(cycle_id)
        # Never fabricated COMPLETED/FAILED; the durable run stays resumable.
        assert run['status'] == 'RUNNING' and run['owner_id'] == worker.owner_id
        assert repo.persistence.active_cycle_run('NSE')['cycle_id'] == cycle_id
        assert repo.persistence.cycle_published_selection(cycle_id) is None
        # Not suppressed as SUPERSEDED: the job reports why it stopped.
        job = _job(repo, cycle_id)
        assert job['status'] == 'RUNNING' and job['error_code'] == 'OWNERSHIP_LOST'
    finally:
        await worker.close()


# -- genuine supersession and terminal states stay fenced ---------------------

@pytest.mark.asyncio
async def test_genuinely_superseded_owner_never_publishes_any_status(repo):
    _manual_gate(repo)
    other_owner = 'research-engine:other-replica'

    async def superseded(**kwargs):
        cycle_id = kwargs['cycle_id']
        _expire_lease(repo, cycle_id)
        assert repo.persistence.claim_cycle_run(cycle_id, other_owner, lease_seconds=300)
        await kwargs['checkpoint'].start(PHASE_DEEP, 'instrument-1')

    worker = OpportunityCycleWorker(repo, superseded, market='NSE')
    try:
        submitted = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
        before = _job(repo, submitted['cycle_id'])
        await _settle(worker)
        run = repo.persistence.cycle_run(submitted['cycle_id'])
        assert run['status'] == 'RUNNING' and run['owner_id'] == other_owner
        job = _job(repo, submitted['cycle_id'])
        assert job.get('error_code') is None and job['status'] == 'RUNNING'
        assert job['updated_at'] == _job(repo, submitted['cycle_id'])['updated_at']
        assert before['cycle_id'] == job['cycle_id']
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_lost_lease_cannot_overwrite_a_terminal_completed_status(repo):
    _manual_gate(repo)

    async def completed_elsewhere(**kwargs):
        cycle_id = kwargs['cycle_id']
        # Another (now-finished) owner completed the cycle under its own lease.
        _expire_lease(repo, cycle_id)
        assert repo.persistence.claim_cycle_run(cycle_id, 'research-engine:finisher', lease_seconds=300)
        repo.persistence.update_cycle_run(cycle_id, 'research-engine:finisher', status='COMPLETED')
        await kwargs['checkpoint'].start(PHASE_DEEP, 'instrument-1')

    worker = OpportunityCycleWorker(repo, completed_elsewhere, market='NSE')
    try:
        submitted = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
        await _settle(worker)
        run = repo.persistence.cycle_run(submitted['cycle_id'])
        assert run['status'] == 'COMPLETED' and run['owner_id'] is None
        assert _job(repo, submitted['cycle_id']).get('error_code') != 'OWNERSHIP_LOST'
    finally:
        await worker.close()


# -- explicit operator resume of the SAME interrupted cycle -------------------

async def _interrupted_cycle(repo):
    """Reproduce the production end state: RUNNING, lease lapsed, slot held,
    one candidate already durably finished under the original lease."""
    async def interrupted(**kwargs):
        checkpoint = kwargs['checkpoint']
        await checkpoint.start(PHASE_DEEP, 'done-before-lapse')
        await checkpoint.finish(PHASE_DEEP, 'done-before-lapse', CandidateState.COMPLETED,
                                disposition='ANALYZED', payload={'kept': True})
        _expire_lease(repo, kwargs['cycle_id'])
        await checkpoint.start(PHASE_DEEP, 'next-candidate')

    worker = OpportunityCycleWorker(repo, interrupted, market='NSE')
    submitted = await worker.submit_manual_equity_async({'analysis_scope': 'FULL'})
    await _settle(worker)
    return worker, submitted['cycle_id']


@pytest.mark.asyncio
async def test_operator_resume_continues_the_same_cycle_from_its_checkpoint(repo):
    _manual_gate(repo)
    worker, cycle_id = await _interrupted_cycle(repo)
    seen = {}

    async def resumed_runner(**kwargs):
        checkpoint = kwargs['checkpoint']
        await checkpoint.load()
        seen['cycle_id'] = kwargs['cycle_id']
        seen['restorable'] = checkpoint.restorable(PHASE_DEEP, 'done-before-lapse')
        await checkpoint.start(PHASE_DEEP, 'next-candidate')
        await checkpoint.finish(PHASE_DEEP, 'next-candidate', CandidateState.COMPLETED, disposition='ANALYZED')
        return {'cycle_id': kwargs['cycle_id'], 'universe_count': 2}

    worker.runner = resumed_runner
    try:
        accepted = await worker.resume_interrupted_manual_equity_async(cycle_id)
        assert accepted['cycle_id'] == cycle_id and accepted['resumed'] is True
        await _settle(worker)
        assert seen['cycle_id'] == cycle_id
        assert seen['restorable'] is not None and seen['restorable']['payload'] == {'kept': True}
        run = repo.persistence.cycle_run(cycle_id)
        assert run['status'] == 'COMPLETED'
        assert repo.persistence.active_cycle_run('NSE') is None
        assert [j['cycle_id'] for j in repo.persistence.opportunity_jobs()] == [cycle_id]  # no new cycle
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_operator_resume_refuses_a_live_lease_cancelled_or_unknown_cycle(repo):
    _manual_gate(repo)
    worker, cycle_id = await _interrupted_cycle(repo)
    try:
        with pytest.raises(ManualEtfAdmissionError, match='OPPORTUNITY_CYCLE_NOT_ACTIVE'):
            await worker.resume_interrupted_manual_equity_async('00000000-0000-0000-0000-000000000000')
        # A live owner (fresh lease held elsewhere) is never displaced.
        assert repo.persistence.claim_cycle_run(cycle_id, 'research-engine:live', lease_seconds=300)
        with pytest.raises(ManualEtfAdmissionError, match='OPPORTUNITY_CYCLE_LEASE_ACTIVE'):
            await worker.resume_interrupted_manual_equity_async(cycle_id)
        assert repo.persistence.cycle_run(cycle_id)['owner_id'] == 'research-engine:live'
        _expire_lease(repo, cycle_id)
        assert repo.persistence.request_cycle_cancel(cycle_id)
        with pytest.raises(ManualEtfAdmissionError, match='OPPORTUNITY_CYCLE_CANCELLED'):
            await worker.resume_interrupted_manual_equity_async(cycle_id)
        assert repo.persistence.cycle_run(cycle_id)['status'] == 'CANCEL_REQUESTED'
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('env,paused,scheduler,enabled', [
    ('PRD', True, False, True), ('LOCAL', False, False, True),
    ('LOCAL', True, True, True), ('LOCAL', True, False, False)])
async def test_operator_resume_requires_the_manual_equity_gate(repo, env, paused, scheduler, enabled):
    _manual_gate(repo)
    worker, cycle_id = await _interrupted_cycle(repo)
    repo.settings.environment = env
    repo.settings.research_opportunity_recovery_paused = paused
    repo.settings.research_opportunity_scheduler_enabled = scheduler
    repo.settings.research_equity_manual_execution_enabled = enabled
    try:
        with pytest.raises(ManualEtfAdmissionError, match='ETF_MANUAL_EXECUTION_DISABLED'):
            await worker.resume_interrupted_manual_equity_async(cycle_id)
        assert repo.persistence.cycle_run(cycle_id)['status'] == 'RUNNING'
    finally:
        await worker.close()


def test_resume_endpoint_is_admin_gated_and_maps_refusals(monkeypatch, repo):
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from app import main
    from test_etf_preflight import ADMIN

    _manual_gate(repo)
    worker = OpportunityCycleWorker(repo, AsyncMock(), market='NSE')
    worker.resume_interrupted_manual_equity_async = AsyncMock(
        side_effect=ManualEtfAdmissionError('OPPORTUNITY_CYCLE_LEASE_ACTIVE'))
    monkeypatch.setattr(main, 'repository', repo)
    monkeypatch.setattr(main, '_opportunity_worker', lambda: worker)
    client = TestClient(main.app)
    path = '/api/v1/research/opportunities/cycles/86f0567d-978e-4fcc-a9c5-337ed729db69/resume'
    assert client.post(path).status_code in (401, 403)
    worker.resume_interrupted_manual_equity_async.assert_not_awaited()
    response = client.post(path, headers=ADMIN)
    assert response.status_code == 409 and response.json() == {'detail': 'OPPORTUNITY_CYCLE_LEASE_ACTIVE'}
