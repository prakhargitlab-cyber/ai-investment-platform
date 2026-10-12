"""Offline regression tests for lease expiry, cancellation and publication."""
import asyncio
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, CycleOwnershipLost
from app.persistence import SqliteResearchPersistence
from app.opportunity_worker import OpportunityCycleWorker


def owned(*, market='NSE'):
    store = SqliteResearchPersistence()
    key = str(uuid4())
    store.create_cycle_run(key, {}, market=market)
    assert store.claim_cycle_run(key, 'old')
    return store, key


def expire(store, key):
    with store._connection:
        store._connection.execute("UPDATE global_opportunity_cycle_run SET lease_expires_at=? WHERE cycle_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), key))


@pytest.mark.parametrize('operation', ['progress', 'selection', 'complete', 'publish', 'renew', 'cancel', 'release'])
def test_expired_owner_is_fenced_before_takeover(operation):
    store, key = owned()
    expire(store, key)
    if operation == 'renew':
        assert not store.renew_cycle_lease(key, 'old')
    elif operation == 'cancel':
        store.request_cycle_cancel(key)
        assert not store.cancel_cycle_run(key, 'old')
    elif operation == 'release':
        store.release_cycle_run(key, 'old')
        assert store.cycle_run(key)['owner_id'] == 'old'
    else:
        with pytest.raises(CycleOwnershipLost):
            if operation == 'progress':
                store.record_cycle_progress(key, 'old', 'DEEP', 'x', CandidateState.IN_PROGRESS, start_attempt=True)
            elif operation == 'selection':
                store.update_cycle_run(key, 'old', selection={'deep_ids': ['x']})
            elif operation == 'complete':
                store.update_cycle_run(key, 'old', status='COMPLETED')
            else:
                store.publish_opportunity_cycle([], [], [], {'cycle_id': key}, fence=(key, 'old'))
    assert store.active_cycle_run()['cycle_id'] == key


def test_cancel_fences_admission_but_allows_existing_candidate_to_drain():
    store, key = owned()
    store.record_cycle_progress(key, 'old', 'DEEP', 'x', CandidateState.IN_PROGRESS, start_attempt=True)
    assert store.request_cycle_cancel(key)
    assert not store.request_cycle_cancel(key)
    with pytest.raises(CycleOwnershipLost):
        store.record_cycle_progress(key, 'old', 'DEEP', 'y', CandidateState.IN_PROGRESS, start_attempt=True)
    store.record_cycle_progress(key, 'old', 'DEEP', 'x', CandidateState.COMPLETED)
    assert not store.cancel_cycle_run_unowned(key)
    assert store.active_cycle_run() is not None
    assert store.cancel_cycle_run(key, 'old')
    assert store.active_cycle_run() is None
    assert not store.claim_cycle_run(key, 'replacement')


@pytest.mark.asyncio
@pytest.mark.parametrize('startup', [True, False])
async def test_dead_owner_cancel_recovery_never_calls_runner(startup):
    store, key = owned()
    store.request_cycle_cancel(key)
    runner = AsyncMock(side_effect=AssertionError('must not acquire'))
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), runner)
    try:
        if startup:
            expire(store, key)
            await worker.start_async()
        else:
            await worker.start_async()
            assert worker.active is None  # current owner is still alive
            expire(store, key)
            await worker._take_over_async()
        await asyncio.wait_for(worker.queue.join(), 2)
        assert store.cycle_run(key)['status'] == 'CANCELLED'
        assert store.active_cycle_run() is None
        runner.assert_not_called()
    finally:
        await worker.close()


@pytest.mark.parametrize('cancel_first', [True, False])
def test_cancellation_and_publication_have_one_winner(cancel_first):
    store, key = owned()
    selection = {'cycle_id': key, 'generated_at': datetime.now(timezone.utc).isoformat(), 'market': 'NSE'}
    if cancel_first:
        assert store.request_cycle_cancel(key)
        with pytest.raises(CycleOwnershipLost):
            store.publish_opportunity_cycle([], [], [], selection, fence=(key, 'old'))
        assert store.cycle_published_selection(key) is None
    else:
        store.publish_opportunity_cycle([], [], [], selection, fence=(key, 'old'))
        assert not store.request_cycle_cancel(key)
        assert store.cycle_run(key)['status'] == 'PUBLISHED'


def test_old_owner_cannot_publish_after_takeover():
    store, key = owned()
    expire(store, key)
    assert store.claim_cycle_run(key, 'new')
    with pytest.raises(CycleOwnershipLost):
        store.publish_opportunity_cycle([], [], [], {'cycle_id': key}, fence=(key, 'old'))
    assert store.cycle_published_selection(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [True, False])
async def test_etf_publication_is_fenced(cancelled):
    from app.etf_opportunity_cycle import run_etf_radar_cycle_async
    store, key = owned(market='ETF')
    if cancelled:
        store.request_cycle_cancel(key)
    else:
        expire(store, key)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    checkpoint = CycleCheckpoint(store, key, 'old')
    with pytest.raises(CycleOwnershipLost):
        await run_etf_radar_cycle_async(repo, None, candidate_ids=[uuid4()], cycle_id=key, checkpoint=checkpoint)
    assert store.latest_etf_radar_cycle() is None


@pytest.mark.asyncio
async def test_etf_missing_universe_preserves_previous_publication():
    from app.etf_opportunity_cycle import run_etf_radar_cycle_async
    from app.global_opportunity_cycle import UniverseUnavailableError
    store, key = owned(market='ETF')
    previous = {'cycle_id': 'previous', 'universe_count': 3}
    store.save_etf_radar_cycle('previous', 'v1', None, datetime.now(timezone.utc), previous)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    with pytest.raises(UniverseUnavailableError):
        await run_etf_radar_cycle_async(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=[])), cycle_id=key)
    assert store.latest_etf_radar_cycle() == previous


@pytest.mark.asyncio
async def test_etf_published_recovery_does_not_rediscover_universe():
    from app.etf_opportunity_cycle import run_etf_radar_cycle_async
    store, key = owned(market='ETF')
    saved = {'cycle_id': key, 'universe_count': 1}
    store.save_etf_radar_cycle(key, 'v1', None, datetime.now(timezone.utc), saved, fence=(key, 'old'))
    source = SimpleNamespace(active_global_etfs=AsyncMock(side_effect=AssertionError('must not rediscover')))
    result = await run_etf_radar_cycle_async(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()),
        source, cycle_id=key, checkpoint=CycleCheckpoint(store, key, 'old'))
    assert result == saved
    source.active_global_etfs.assert_not_called()


def test_postgres_nested_transaction_rolls_back_outer_failure():
    from app.postgres_persistence import _PostgresConnectionAdapter
    raw = Mock()
    connection = _PostgresConnectionAdapter(raw)
    with pytest.raises(ValueError):
        with connection:
            with connection:
                connection.execute('INSERT INTO example VALUES (?)', (1,))
            raw.commit.assert_not_called()
            raise ValueError('projection failed')
    raw.rollback.assert_called_once()
    raw.commit.assert_not_called()


def test_postgres_swallowed_statement_error_cannot_commit_partial_data():
    from app.postgres_persistence import _PostgresConnectionAdapter
    raw = Mock()
    raw.execute.side_effect = ValueError('invalid')
    connection = _PostgresConnectionAdapter(raw)
    with pytest.raises(RuntimeError, match='PERSISTENCE_TRANSACTION_ABORTED'):
        with connection:
            with pytest.raises(ValueError):
                connection.execute('bad sql')
    raw.rollback.assert_called_once()
    raw.commit.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_persistence_dispatch_bounded():
    import threading
    from app.repository import ResearchRepository
    repo = object.__new__(ResearchRepository)
    repo._persistence = object()  # production dispatch, no DB connection
    repo._persistence_worker_lock = RLock()
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert release.wait(3)
    first = asyncio.create_task(repo._run_blocking_persistence(slow))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        queued = asyncio.create_task(repo._run_blocking_persistence(second.set))
        await asyncio.sleep(0.01)
        assert not second.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await queued
        assert second.is_set()
    finally:
        release.set()


def test_suggestion_projection_rolls_back_all_rows_on_failure(monkeypatch):
    from test_global_suggestion_lifecycle import make_scan, make_card
    store = SqliteResearchPersistence()
    scan = make_scan()
    original = store._upsert_global_current
    def fail(*args):
        original(*args)
        raise ValueError('projection failed')
    monkeypatch.setattr(store, '_upsert_global_current', fail)
    with pytest.raises(ValueError):
        store.persist_global_suggestion_lifecycle(scan, [make_card()])
    for table in ('global_market_scan', 'global_stock_suggestion', 'global_stock_suggestion_history', 'global_stock_suggestion_current'):
        assert store._connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_new_empty_scan_does_not_relabel_historical_buy_as_current():
    from test_global_suggestion_lifecycle import make_scan, make_card
    store = SqliteResearchPersistence()
    old = make_scan()
    store.persist_global_suggestion_lifecycle(old, [make_card()])
    new = make_scan(completed_at='2026-09-14T10:05:00+00:00', evaluated_count=100, rank_eligible_count=0)
    store.persist_global_suggestion_lifecycle(new, [])
    # A newer controlled scan must not claim the production timestamp either.
    store.persist_global_suggestion_lifecycle(make_scan(completed_at='2026-09-15T10:05:00+00:00', controlled=True), [])
    result = store.global_opportunity_radar()
    assert result['source_scan_id'] == new['scan_id']
    assert result['best_buy_today'] is None
    assert result['top_short_term'] == []
    assert result['top_short_term_count'] == 0
    assert result['previous_recommendations'][0]['scan_id'] == old['scan_id']
    assert len(store.global_current_suggestions()) == 1  # retained history


def test_expired_owner_cannot_write_suggestion_projection():
    from test_global_suggestion_lifecycle import make_scan, make_card
    store, key = owned()
    store.publish_opportunity_cycle([], [], [], {'cycle_id': key, 'generated_at': '2026-01-01', 'market': 'NSE'}, fence=(key, 'old'))
    expire(store, key)
    with pytest.raises(CycleOwnershipLost):
        store.persist_global_suggestion_lifecycle(make_scan(scan_id=key), [make_card()], fence=(key, 'old'))
    assert store.global_market_scans() == []


def test_status_uses_durable_state_after_late_owner_log():
    store, key = owned()
    store.update_cycle_run(key, 'old', status='COMPLETED')
    store.record_opportunity_job(dict(cycle_id=key, status='RUNNING', updated_at='2099-01-01', parameters={}))
    assert store.opportunity_job(key)['status'] == 'COMPLETED'


def test_suggestion_history_supports_postgres_dictionary_rows():
    from test_global_suggestion_lifecycle import make_scan, make_card
    store = SqliteResearchPersistence()
    # sqlite dict rows mimic psycopg's configured dict_row, including inability
    # to access a named column with [0]. No PostgreSQL server is contacted.
    store._connection.row_factory = lambda cursor, row: {column[0]: value for column, value in zip(cursor.description, row)}
    store.persist_global_suggestion_lifecycle(make_scan(), [make_card()])
    store.persist_global_suggestion_lifecycle(make_scan(completed_at='2026-09-14T10:05:00+00:00'), [make_card()])
    assert len(store.global_current_suggestions()) == 1


@pytest.mark.asyncio
async def test_async_start_and_submit_keep_event_loop_responsive():
    import threading
    from app.repository import ResearchRepository
    from test_global_opportunity_cycle_event_loop_offload import _thread_safe_store
    class ThreadStore(SqliteResearchPersistence):
        __module__ = 'app.postgres_persistence'  # select the production dispatcher
    store = _thread_safe_store()
    store.__class__ = ThreadStore
    repo = object.__new__(ResearchRepository)
    repo._persistence = store
    repo._persistence_worker_lock = RLock()
    main_thread = threading.get_ident()
    observed = []
    original = store.create_cycle_run
    entered, release = threading.Event(), threading.Event()
    def slow_create(*args, **kwargs):
        observed.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    store.create_cycle_run = slow_create
    runner_release = asyncio.Event()
    async def runner(**kwargs):
        await runner_release.wait()
        return {'cycle_id': kwargs['cycle_id']}
    worker = OpportunityCycleWorker(repo, runner)
    submission = asyncio.create_task(worker.submit_async({}))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)  # this must run while the SQL stand-in blocks
        assert not submission.done()
        release.set()
        result = await asyncio.wait_for(submission, 3)
        assert main_thread not in observed
        assert result['status'] == 'ACCEPTED'
    finally:
        release.set()
        runner_release.set()
        await worker.close()


def test_publication_expiring_mid_transaction_rolls_back(monkeypatch):
    store, key = owned()
    clock_sql = store._lease_clock_sql
    def build(_snapshots):
        monkeypatch.setattr(store, '_lease_clock_sql', lambda: "'2099-01-01T00:00:00.000000+00:00'")
        return [], [], {'cycle_id': key, 'generated_at': '2026-01-01', 'market': 'NSE'}
    with pytest.raises(CycleOwnershipLost):
        store.publish_opportunity_cycle([], [], [], {'cycle_id': key}, build=build, fence=(key, 'old'))
    monkeypatch.setattr(store, '_lease_clock_sql', clock_sql)
    assert store.cycle_published_selection(key) is None
    assert store.cycle_run(key)['status'] == 'RUNNING'


def test_published_cycle_cannot_be_failed_or_reselected():
    store, key = owned()
    store.publish_opportunity_cycle([], [], [], {'cycle_id': key, 'generated_at': '2026-01-01', 'market': 'NSE'}, fence=(key, 'old'))
    for fields in ({'status': 'FAILED'}, {'selection': {'deep_ids': ['other']}}):
        with pytest.raises(CycleOwnershipLost):
            store.update_cycle_run(key, 'old', **fields)
    assert store.cycle_run(key)['status'] == 'PUBLISHED'
