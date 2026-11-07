"""Deterministic budget contention; all checkpoints and HTTP transports are synthetic."""
import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cycle_checkpoint import CycleOwnershipLost
from app.etf_history_recovery import RecoveryBudget, live_recovery_provider
from scripts.recover_etf_history import RecoveryRepository
from test_etf_history_recovery import canonical
from test_etf_history_recovery_transport import synthetic_transport


class MemoryCheckpoint:
    owner_id, cycle_id = 'synthetic-owner', 'synthetic-budget'

    def __init__(self):
        self.owned, self.saved, self.on_save = True, [], None
        self.persistence = SimpleNamespace(renew_cycle_lease=lambda *args: self.owned)

    async def run(self):
        return {'status': 'RUNNING', 'owner_id': self.owner_id if self.owned else 'replacement'}

    async def _call(self, operation, *args):
        return operation(*args)

    async def save_selection(self, selection):
        if self.on_save:
            await self.on_save(selection)
        await asyncio.sleep(0)  # Expose competing reservations without a wall-clock delay.
        self.saved.append(copy.deepcopy(selection))


class PausedSpacing:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, seconds):
        self.entered.set()
        await self.release.wait()


def make_budget(limit, *, seed=0, sleep=None):
    checkpoint = MemoryCheckpoint()
    selection = {'limits': {'max_requests': 5, 'max_http_requests': limit},
                 'request_reservations': 2, 'http_reservations': seed}
    budget = RecoveryBudget(checkpoint, selection, sleep=sleep or AsyncMock(), monotonic=lambda: 10)
    return budget, checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 18])
@pytest.mark.parametrize('one_remaining', [False, True])
async def test_concurrent_http_reservations_never_exceed_ceiling(limit, one_remaining):
    spacing = PausedSpacing()
    seed = limit - 1 if one_remaining else 0
    budget, checkpoint = make_budget(limit, seed=seed, sleep=spacing)
    budget.http_last = 0
    tasks = [asyncio.create_task(budget.reserve_http()) for _ in range(limit + 7)]
    await asyncio.wait_for(spacing.entered.wait(), 3)
    spacing.release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
    assert sum(result is None for result in results) == limit - seed
    assert all(result is None or str(result) == 'ETF_HTTP_BUDGET_EXHAUSTED' for result in results)
    assert budget.selection['http_reservations'] == limit
    assert [saved['http_reservations'] for saved in checkpoint.saved] == list(range(seed + 1, limit + 1))
    assert budget.selection['request_reservations'] == 2
    assert budget.selection['limits'] == {'max_requests': 5, 'max_http_requests': limit}
    with pytest.raises(RuntimeError, match='ETF_HTTP_BUDGET_EXHAUSTED'):
        await budget.reserve_http()
    assert budget.selection['http_reservations'] == limit


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 18])
async def test_transport_threads_share_one_atomic_http_ceiling(monkeypatch, limit):
    spacing = PausedSpacing()
    budget, checkpoint = make_budget(limit, sleep=spacing)
    budget.http_last = 0
    session, _, settings = synthetic_transport(monkeypatch, canonical(1), statuses=())
    live_recovery_provider(budget, settings)
    tasks = [asyncio.create_task(asyncio.to_thread(session.get, 'https://synthetic.invalid/concurrent'))
             for _ in range(limit + 7)]
    await asyncio.wait_for(spacing.entered.wait(), 3)
    spacing.release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    successful_http = sum(not isinstance(result, BaseException) for result in results)
    assert all(not isinstance(result, BaseException) or str(result) == 'ETF_HTTP_BUDGET_EXHAUSTED'
               for result in results)
    # An exhaustion signal may stop a charged request before I/O. Such slots
    # remain consumed; every transport success still needs a saved reservation.
    assert len(session.requests) == successful_http <= limit
    assert budget.selection['http_reservations'] == limit
    assert [saved['http_reservations'] for saved in checkpoint.saved] == list(range(1, limit + 1))
    assert budget.selection['request_reservations'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['closed', 'abort', 'ownership'])
async def test_stop_or_ownership_loss_during_spacing_cannot_authorize_http(monkeypatch, stop):
    spacing = PausedSpacing()
    budget, checkpoint = make_budget(3, sleep=spacing)
    budget.http_last = 0
    session, _, settings = synthetic_transport(monkeypatch, canonical(1))
    live_recovery_provider(budget, settings)
    task = asyncio.create_task(asyncio.to_thread(session.get, 'https://synthetic.invalid/stop'))
    await asyncio.wait_for(spacing.entered.wait(), 3)
    if stop == 'closed':
        budget.closed = True
    elif stop == 'abort':
        budget.abort = 'YAHOO_ACCESS_RESTRICTED'
    else:
        checkpoint.owned = False
    spacing.release.set()
    error = CycleOwnershipLost if stop == 'ownership' else RuntimeError
    with pytest.raises(error):
        await asyncio.wait_for(task, 3)
    assert budget.selection['http_reservations'] == 0
    assert checkpoint.saved == [] and session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_holder', [False, True])
async def test_cancellation_before_charge_releases_lock_without_consuming_slot(cancel_holder):
    spacing = PausedSpacing()
    budget, checkpoint = make_budget(1, sleep=spacing)
    budget.http_last = 0
    holder = asyncio.create_task(budget.reserve_http())
    await asyncio.wait_for(spacing.entered.wait(), 3)
    waiter = asyncio.create_task(budget.reserve_http())
    await asyncio.sleep(0)
    cancelled, survivor = (holder, waiter) if cancel_holder else (waiter, holder)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert budget.selection['http_reservations'] == 0
    spacing.release.set()
    await asyncio.wait_for(survivor, 3)
    assert budget.selection['http_reservations'] == 1
    assert len(checkpoint.saved) == 1
    with pytest.raises(RuntimeError, match='ETF_HTTP_BUDGET_EXHAUSTED'):
        await budget.reserve_http()


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 18])
@pytest.mark.parametrize('cancel_save', [False, True])
async def test_failed_or_cancelled_save_retains_charge_and_blocks_waiters(limit, cancel_save):
    budget, checkpoint = make_budget(limit)
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed_save(selection):
        entered.set()
        await release.wait()
        raise OSError('SYNTHETIC_CHECKPOINT_FAILURE')

    checkpoint.on_save = failed_save
    holder = asyncio.create_task(budget.reserve_http())
    await asyncio.wait_for(entered.wait(), 3)
    waiter = asyncio.create_task(budget.reserve_http())
    await asyncio.sleep(0)
    if cancel_save:
        holder.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel_save else OSError):
        await holder
    with pytest.raises(RuntimeError, match='ETF_RECOVERY_STOPPED'):
        await asyncio.wait_for(waiter, 3)
    assert budget.closed and budget.selection['http_reservations'] == 1
    assert checkpoint.saved == []


@pytest.mark.asyncio
async def test_cancelled_save_drains_persistence_worker_before_unlocking():
    budget, checkpoint = make_budget(3)
    repository = RecoveryRepository(None)
    loop, entered, release, durable = asyncio.get_running_loop(), asyncio.Event(), threading.Event(), []

    def persist(selection):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        durable.append(selection['http_reservations'])

    async def save(selection):
        await repository._run_blocking_persistence(persist, selection)

    checkpoint.on_save = save
    holder = asyncio.create_task(budget.reserve_http())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        holder.cancel()
        waiter = asyncio.create_task(budget.reserve_http())
        await asyncio.sleep(0)
        assert not holder.done() and not waiter.done()
        assert budget.selection['http_reservations'] == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(holder, 3)
    with pytest.raises(RuntimeError, match='ETF_RECOVERY_STOPPED'):
        await asyncio.wait_for(waiter, 3)
    assert durable == [1] and budget.selection['http_reservations'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['save_failure', 'closed_after_save', 'abort_after_save'])
async def test_transport_never_runs_after_unsuccessful_reservation(monkeypatch, stop):
    budget, checkpoint = make_budget(3)
    session, _, settings = synthetic_transport(monkeypatch, canonical(1))
    live_recovery_provider(budget, settings)

    async def stop_save(selection):
        if stop == 'save_failure':
            raise OSError('SYNTHETIC_CHECKPOINT_FAILURE')
        if stop == 'closed_after_save':
            budget.closed = True
        else:
            budget.abort = 'YAHOO_ACCESS_RESTRICTED'

    checkpoint.on_save = stop_save
    with pytest.raises((RuntimeError, OSError)):
        await asyncio.wait_for(asyncio.to_thread(session.get, 'https://synthetic.invalid/no-admission'), 3)
    assert session.requests == [] and budget.selection['http_reservations'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 18])
async def test_failed_http_acquisition_never_refunds_or_increases_budget(monkeypatch, limit):
    budget, checkpoint = make_budget(limit)
    session, _, settings = synthetic_transport(monkeypatch, canonical(1))
    attempted = []

    def failed_request(*args, **kwargs):
        attempted.append(1)
        raise OSError('SYNTHETIC_HTTP_FAILURE')

    session.request = failed_request
    live_recovery_provider(budget, settings)
    for _ in range(limit):
        with pytest.raises(OSError, match='SYNTHETIC_HTTP_FAILURE'):
            await asyncio.to_thread(session.get, 'https://synthetic.invalid/failure')
    with pytest.raises(RuntimeError, match='ETF_HTTP_BUDGET_EXHAUSTED'):
        await asyncio.to_thread(session.get, 'https://synthetic.invalid/exhausted')
    assert len(attempted) == budget.selection['http_reservations'] == limit
    assert checkpoint.saved[-1]['http_reservations'] == limit


@pytest.mark.asyncio
async def test_reservation_lock_is_released_before_http_io(monkeypatch):
    budget, _ = make_budget(2)
    session, _, settings = synthetic_transport(monkeypatch, canonical(1))
    loop, first_entered, second_entered = asyncio.get_running_loop(), asyncio.Event(), asyncio.Event()
    release, guard, calls = threading.Event(), threading.Lock(), []

    def blocking_request(*args, **kwargs):
        with guard:
            index = len(calls)
            calls.append(index)
        loop.call_soon_threadsafe((first_entered if index == 0 else second_entered).set)
        if index == 0:
            assert release.wait(3)
        return SimpleNamespace(status_code=200)

    session.request = blocking_request
    live_recovery_provider(budget, settings)
    first = asyncio.create_task(asyncio.to_thread(session.get, 'https://synthetic.invalid/first'))
    try:
        await asyncio.wait_for(first_entered.wait(), 3)
        second = asyncio.create_task(asyncio.to_thread(session.get, 'https://synthetic.invalid/second'))
        await asyncio.wait_for(second_entered.wait(), 3)
        assert not first.done() and budget.selection['http_reservations'] == 2
        await second
    finally:
        release.set()
    await asyncio.wait_for(first, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['reserve', 'provider_factory'])
async def test_foreign_event_loop_is_rejected_before_counter_or_transport_changes(monkeypatch, operation):
    budget, checkpoint = make_budget(3)
    session, factory, settings = synthetic_transport(monkeypatch, canonical(1))
    live_recovery_provider(budget, settings)

    async def foreign():
        if operation == 'reserve':
            await budget.reserve_http()
        else:
            live_recovery_provider(budget, settings)

    with pytest.raises(RuntimeError, match='ETF_RECOVERY_BUDGET_EVENT_LOOP_MISMATCH'):
        await asyncio.to_thread(lambda: asyncio.run(foreign()))
    assert budget.selection['http_reservations'] == 0
    assert factory == [10] and session.requests == [] and checkpoint.saved == []


@pytest.mark.asyncio
async def test_competing_thread_event_loops_cannot_bind_independent_budget_locks():
    budget, checkpoint = make_budget(3)
    start = threading.Barrier(2)

    def reserve_from_thread():
        start.wait(timeout=3)
        return asyncio.run(budget.reserve_http())

    results = await asyncio.wait_for(asyncio.gather(
        asyncio.to_thread(reserve_from_thread), asyncio.to_thread(reserve_from_thread),
        return_exceptions=True), 5)
    assert sum(result is None for result in results) == 1
    assert sum(str(result) == 'ETF_RECOVERY_BUDGET_EVENT_LOOP_MISMATCH' for result in results) == 1
    assert budget.selection['http_reservations'] == 1
    assert [saved['http_reservations'] for saved in checkpoint.saved] == [1]


@pytest.mark.asyncio
async def test_blocking_transport_on_owner_loop_fails_instead_of_deadlocking(monkeypatch):
    budget, _ = make_budget(3)
    session, _, settings = synthetic_transport(monkeypatch, canonical(1))
    live_recovery_provider(budget, settings)
    with pytest.raises(RuntimeError, match='ETF_RECOVERY_HTTP_REQUIRES_WORKER_THREAD'):
        session.get('https://synthetic.invalid/wrong-thread')
    assert session.requests == [] and budget.selection['http_reservations'] == 0


def test_retired_owner_loop_blocks_transport_without_scheduling_a_coroutine(monkeypatch):
    async def create():
        budget, _ = make_budget(3)
        session, _, settings = synthetic_transport(monkeypatch, canonical(1))
        live_recovery_provider(budget, settings)
        return budget, session

    budget, session = asyncio.run(create())
    with pytest.raises(RuntimeError, match='ETF_RECOVERY_STOPPED'):
        session.get('https://synthetic.invalid/retired-loop')
    assert session.requests == [] and budget.selection['http_reservations'] == 0
