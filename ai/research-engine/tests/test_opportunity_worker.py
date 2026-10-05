import asyncio
import json
import logging
from datetime import timedelta
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.recommendation_engine import RecommendationEngineV1, lifecycle
from test_recommendation_lifecycle import snapshot, publish
from test_global_scanner import NOW, instrument, persisted


@pytest.fixture
def store():
    return SqliteResearchPersistence()


@pytest.mark.asyncio
async def test_post_accepts_coalesces_and_keeps_previous_radar(monkeypatch):
    from app import main
    from test_opportunity_cycle_identity import _browser_request
    store = SqliteResearchPersistence()
    previous = publish(store, [snapshot()])
    # Also populate V14 dedicated global suggestion tables so radar has data
    from test_global_suggestion_lifecycle import make_card, make_scan
    prev_scan = make_scan()
    prev_card = make_card(1, action='STRONG_BUY', horizon='SHORT_TERM', price=1000.0,
                          fingerprint='fp-worker-prev')
    store.persist_global_suggestion_lifecycle(prev_scan, [prev_card])
    monkeypatch.setattr(main.repository, '_persistence', store)
    monkeypatch.delattr(main.app.state, 'opportunity_worker', raising=False)
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    async def run(*args, **kwargs):
        captured.append(kwargs)
        entered.set()
        await release.wait()
        return {'cycle_id': 'completed-test-cycle', 'universe_count': 500}
    monkeypatch.setattr('app.global_opportunity_cycle.run_global_opportunity_cycle', run)
    worker = main._opportunity_worker()
    try:
        response = await asyncio.wait_for(main.opportunity_cycle(main.OpportunityCycleRequest(), _browser_request()), 1)
        assert response.status_code == 202
        first = json.loads(response.body)
        assert first['status'] == 'ACCEPTED' and not release.is_set()
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await main.opportunity_cycle(main.OpportunityCycleRequest(), _browser_request())
        assert json.loads(duplicate.body)['cycle_id'] == first['cycle_id']
        assert json.loads(duplicate.body)['coalesced']
        assert len(captured) == 1
        assert (await main.opportunity_cycle_status(UUID(first['cycle_id'])))['status'] == 'RUNNING'
        # Radar reads dedicated V14 tables, not V13 top_selection
        radar = await main.opportunity_radar()
        assert radar['source_scan_id'] == prev_scan['scan_id']
        assert radar['best_buy_today'] is not None
        assert 'Authorization' not in captured[0]['identity_headers']
        assert captured[0]['identity_headers']['X-AIP-User-Roles'] == 'ADMIN'
        assert captured[0]['readiness_runtime'] is main.research_readiness_runtime
        release.set()
        await asyncio.wait_for(worker.queue.join(), 1)
        assert (await main.opportunity_cycle_status(UUID(first['cycle_id'])))['status'] == 'COMPLETED'
    finally:
        release.set()
        await worker.close()
        monkeypatch.delattr(main.app.state, 'opportunity_worker', raising=False)


@pytest.mark.asyncio
async def test_worker_failure_is_persisted_and_next_job_can_run():
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())
    runner = AsyncMock(side_effect=[RuntimeError('secret provider details'), {'cycle_id': 'next'}])
    worker = OpportunityCycleWorker(repo, runner)
    try:
        first = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        failed = next(v for v in store.opportunity_jobs() if v['cycle_id'] == first['cycle_id'])
        assert failed['status'] == 'FAILED' and 'secret' not in json.dumps(failed)
        second = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        assert next(v for v in store.opportunity_jobs() if v['cycle_id'] == second['cycle_id'])['status'] == 'COMPLETED'
        assert store.opportunity_current()['generated_at'] is None
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_restart_marks_abandoned_request_failed():
    store = SqliteResearchPersistence()
    store.record_opportunity_job({'cycle_id': 'abandoned', 'status': 'RUNNING', 'updated_at': NOW.isoformat()})
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), AsyncMock())
    worker.start()
    try:
        assert store.opportunity_jobs()[0]['status'] == 'FAILED'
        assert store.opportunity_jobs()[0]['error_code'] == 'WORKER_RESTARTED'
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_cancelled_error_produces_worker_stopped(store, caplog):
    """Step 3.4: CancelledError → FAILED / WORKER_STOPPED (unchanged behavior)."""
    caplog.set_level(logging.ERROR, logger="app.opportunity_worker")
    async def cancelled_runner(**kwargs):
        raise asyncio.CancelledError()
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), cancelled_runner)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        failed = next(v for v in store.opportunity_jobs() if v['cycle_id'] == job['cycle_id'])
        assert failed['status'] == 'FAILED' and failed['error_code'] == 'WORKER_STOPPED'
        # CancelledError must NOT trigger the OPPORTUNITY_CYCLE_FAILED logging path
        assert not any("opportunity_cycle_failed" in r.message for r in caplog.records)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_runner_exception_logs_traceback_and_error_type(store, caplog):
    """Step 3.2: exception is logged server-side with traceback and errorType."""
    caplog.set_level(logging.ERROR, logger="app.opportunity_worker")
    async def boom(**kwargs):
        raise RuntimeError("secret-token/database-connection-failed")
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), boom)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        # Logger.exception emits at ERROR with exc_info → traceback in log records
        failure_records = [r for r in caplog.records if "opportunity_cycle_failed" in r.message]
        assert len(failure_records) == 1
        assert any("errorType=RuntimeError" in r.message for r in failure_records)
        assert any("cycleId=" in r.message for r in failure_records)
        assert any(r.exc_info is not None for r in failure_records)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_runner_exception_does_not_persist_raw_message(store, caplog):
    """Step 3.3: raw exception text is NOT in the persisted/public job payload."""
    caplog.set_level(logging.ERROR, logger="app.opportunity_worker")
    async def boom(**kwargs):
        raise KeyError("secret-key-not-in-payload")
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), boom)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        persisted = store.opportunity_jobs()
        failed = next(v for v in persisted if v['cycle_id'] == job['cycle_id'])
        assert failed['status'] == 'FAILED'
        assert failed['error_code'] == 'OPPORTUNITY_CYCLE_FAILED'
        job_json = json.dumps(failed)
        assert 'secret-key-not-in-payload' not in job_json
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_successful_job_persists_completed(store):
    """Step 3.5: successful runner execution → COMPLETED (unchanged behavior)."""
    async def ok(**kwargs):
        return {'cycle_id': 'xyz-cycles', 'universe_count': 42}
    worker = OpportunityCycleWorker(SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), ok)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        completed = next(v for v in store.opportunity_jobs() if v['cycle_id'] == job['cycle_id'])
        assert completed['status'] == 'COMPLETED'
        assert completed['result_cycle_id'] == 'xyz-cycles'
        assert completed['universe_count'] == 42
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_prior_active_stock_reviewed_outside_discovery_without_extra_ensure(monkeypatch):
    from test_global_opportunity_acquisition import acquisition_service
    from test_global_opportunity_ranker import inputs
    from test_stock_rule_engine import _readiness
    from app.research_readiness_runtime import TargetedEnsureResult
    service, runtime, store = acquisition_service(monkeypatch)
    runtime.ensure.return_value = TargetedEnsureResult(_readiness(), (), ())
    rows = [instrument(i) for i in (1, 2, 3)]
    for row in rows: persisted(store, row)
    service.rule_engine.analyze.side_effect = lambda profile, *a, **k: inputs(profile.instrument_id.int)[1]
    result = await service.run(rows, as_of=NOW, shortlist_limit=1, review_ids=[UUID(int=2), UUID(int=3)])
    # Bounded-admission design: live baseline ensure is now bounded to the
    # admitted shortlist too (not the full eligible universe), so only the
    # single shortlisted candidate (1) receives baseline + deep ensure.
    # Reviewed stocks (2, 3) share that same admission boundary and are
    # excluded from it on purpose: being outside the shortlist, they get
    # only a persisted-only, price-lifecycle review (DEFERRED) -- no ensure()
    # of any kind ("without extra ensure"), and no rule_engine.analyze() call
    # either, so they never enter evaluated_entries. Total: 1 baseline + 1 deep
    # ensure, and exactly 1 analyze() call (the admitted candidate only).
    assert runtime.ensure.await_count == 2
    assert service.rule_engine.analyze.await_count == 1
    assert {r.global_instrument_id.int for r in result.evaluated_entries} == {1}
    deferred = {d.global_instrument_id.int for d in result.diagnostics if d.disposition == "DEFERRED"}
    assert deferred == {2, 3}
    assert {str(UUID(int=2)), str(UUID(int=3))} <= set(result.review_evidence)
    assert result.shortlist_count == 1


def test_partial_exit_final_exit_and_history_immutability():
    store = SqliteResearchPersistence()
    s = snapshot()
    publish(store, [s])
    original = store.recommendation_history()[0]
    from uuid import uuid4
    for offset, price, action in [(1, 1185., 'PARTIAL_EXIT'), (2, 1200., 'EXIT')]:
        next_snapshot = snapshot()
        next_snapshot.update(current_price=price, generated_at=(NOW + timedelta(days=offset)).isoformat())
        publish(store, [next_snapshot])
        assert store.recommendation_states()[0]['current_short_action'] == action
        assert store.recommendation_history()[-1]['short_term_action'] == action
        assert store.recommendation_history()[0] == original
    assert store.recommendation_states()[0]['short_term_state'] == 'TARGET_REACHED'
    assert len(store.recommendation_history()) == 3


@pytest.mark.asyncio
async def test_universe_unavailable_maps_to_universe_unavailable(store, caplog):
    """Problem C worker mapping: a UniverseUnavailableError from the runner is
    mapped to FAILED / UNIVERSE_UNAVAILABLE (distinct from
    OPPORTUNITY_CYCLE_FAILED) and logged with errorType=UniverseUnavailableError.
    The raw message is never persisted into the public job payload."""
    from app.global_opportunity_cycle import UniverseUnavailableError
    caplog.set_level(logging.ERROR, logger="app.opportunity_worker")

    async def no_universe(**kwargs):
        raise UniverseUnavailableError("empty NSE universe (detail)")

    worker = OpportunityCycleWorker(
        SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()),
        no_universe)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 1)
        failed = next(v for v in store.opportunity_jobs()
                      if v['cycle_id'] == job['cycle_id'])
        assert failed['status'] == 'FAILED'
        assert failed['error_code'] == 'UNIVERSE_UNAVAILABLE'
        # Distinct from the generic OPPORTUNITY_CYCLE_FAILED path.
        assert failed['error_code'] != 'OPPORTUNITY_CYCLE_FAILED'
        # Public job payload must not carry the raw message.
        assert 'empty NSE universe' not in json.dumps(failed)
        # Logged with the explicit errorType (not the generic Exception handler).
        failure_records = [r for r in caplog.records
                           if "opportunity_cycle_failed" in r.message]
        assert any("errorType=UniverseUnavailableError" in r.message
                   for r in failure_records)
    finally:
        await worker.close()


@pytest.mark.parametrize('long_action', ['HOLD', 'TOP_UP'])
def test_short_partial_exit_independent_of_long(long_action):
    r = RecommendationEngineV1().evaluate(snapshot())
    r['long_term_action'] = long_action
    state = lifecycle(r, r, 1185, {'review': True})
    assert state['current_short_action'] == 'PARTIAL_EXIT'
    assert state['current_long_action'] == long_action
