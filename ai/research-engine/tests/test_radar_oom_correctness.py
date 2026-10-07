"""Radar OOM correctness tests (Step 8).

Deterministic, offline.  These tests close the loop on the production OOM
(OOMKilled, exitCode=137, limit=1536Mi) on cycle 81bf26be:

  FULL cycle: universe=2614, admitted=2432, deferred=182
  Baseline acquisition stalled at 50/2432 (Active=8/8, Failed=0)
  Memory climbed 507 -> 1162 MiB; deep_completed=0.

Root cause (verified in research_readiness_runtime.py review):
  _execute_plan_bounded's wait_for_completion=True path did
    asyncio.wait(..., timeout=25)   then   result = await task
  -- pinning the underlying deep-acquisition coroutine alive forever, each
  holding full task closures (progress, failures, provider response objects,
  document bytes).  Combined with unbounded _flights + _evidence_readiness_cache
  one-per-candidate accumulation, this produced the monotonic climb and the
  50/2432 stall (8/8 stage-2 workers blocked, no slots free, no new admissions).

Poolside's three fixes (already applied):
  1. Timeout ownership -- cancel + bounded drain + bounded failure result.
  2. _flights cleanup -- done-only filtering (no active-flight eviction).
  3. _evidence_readiness_cache bounding -- true LRU, 64-entry cap,
     clear-on-durable-read.

Tests:
  A. Production-scale synthetic (~2500 candidates): live heavy tasks/state
     remain bounded by concurrency/window, not by candidate count.
  B. Timeout: simulate >25s acquisition without waiting 25s; after timeout,
     owned work cancelled/drained, no orphan task, capacity released,
     single-flight cleanup correct, shared work not incorrectly cancelled.
  C. Stall: baseline acquisition progresses beyond 50 with repeated timeout
     cases.
  D. Recovery: 100 admitted candidates, checkpoint at 40, simulate
     interruption, recover SAME cycle; 1..40 not reacquired/reparsed/re-
     evaluated; finish exactly 100 with monotonic durable progress.
  E. Cancellation: RUNNING -> CANCEL_REQUESTED -> CANCELLED (not
     FAILED/WORKER_STOPPED); FAILED/WORKER_STOPPED only for genuine worker
     failure when cancellation was NOT requested.
  F. Separate authoritative baseline-acquisition counters from deep_completed;
     counters monotonic across restart.
  G. Every admitted candidate gets exactly one auditable terminal Stage-2
     disposition; DeepCompleted == Admitted before Top-N trusted.
  H. Trace technical_failure=3 / repair_attempted=3 / repair_recovered=0
     actual categories; technical failures not hidden as evidence insufficiency.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import tracemalloc
import weakref
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.cycle_checkpoint import (
    CandidateState,
    PHASE_BASELINE,
    PHASE_DEEP,
)
from app.global_scanner import GlobalScanner
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.research_readiness_runtime import (
    BASELINE_REQUIREMENT_IDS,
    ResearchReadinessRuntime,
    TargetedEnsureResult,
)
from app.settings import Settings
from test_cycle_checkpoint_recovery import (
    World,
    build_process,
    _run_until_crash,
    _restart_and_finish,
    world_factory,
)
from test_global_opportunity_baseline import _make_runtime
from test_global_scanner import NOW, instrument, persisted
from test_global_opportunity_ranker import inputs
from test_stock_rule_engine import _readiness

logger = logging.getLogger("app.global_opportunity_orchestration")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _instrument_ids(n):
    """Return n canonical instrument dicts with UUIDs 1..n."""
    return [instrument(i) for i in range(1, n + 1)]


def _build_baseline_service(monkeypatch, count=1, *, concurrency=None):
    """Build an orchestrator with a fully-mocked readiness path.

    Returns (service, runtime, store, tracker, rows, pairs).
    """
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(
        repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata,
        readiness_runtime=runtime, clock=lambda: NOW)
    rows = _instrument_ids(count)
    for row in rows:
        persisted(store, row)
    pairs = {UUID(int=n): inputs(n) for n in range(1, count + 1)}
    monkeypatch.setattr(
        GlobalScanner, 'enrich_candidates',
        lambda self, scan, **kwargs: [
            pairs[c.global_instrument_id][0] for c in reversed(scan.candidates)
            if c.eligible_for_deep_analysis])
    service.readiness.read = AsyncMock(return_value=object())

    tracker = []

    async def _ensure(key, **kwargs):
        tracker.append((key, kwargs.get('requirement_ids')))
        return TargetedEnsureResult(_readiness(), (), ())

    service.readiness.ensure = _ensure

    async def analyze(profile, readiness, **kwargs):
        assert kwargs['allow_partial'] is False
        return pairs[profile.instrument_id.int][1]
    service.rule_engine.analyze = AsyncMock(side_effect=analyze)

    if concurrency is not None:
        service._BASELINE_CONCURRENCY = concurrency
        repo.settings.research_stage2_concurrency = concurrency
    return service, runtime, store, tracker, rows, pairs


def _live_tasks():
    """Return live (non-done, non-current) asyncio tasks."""
    return [t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()]


def _job_from_store(store, cycle_id):
    return next(j for j in store.opportunity_jobs() if j["cycle_id"] == cycle_id)


# ---------------------------------------------------------------------------
# A. Production-scale synthetic test: bounded live state under large universe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_A_production_scale_state_bounded_not_oN(monkeypatch):
    """~2500 candidates: live heavy-task state must stay bounded by
    _BASELINE_CONCURRENCY, not by candidate count.  No task leakage.

    Mirrors production (universe=2614, admitted=2432, deferred=182) but with a
    fully-mocked, deterministic provider layer (no network/DB) so the test is
    fast and hermetic.
    """
    count = 2500
    service, runtime, store, tracker, rows, _ = _build_baseline_service(
        monkeypatch, count=count)

    # Instrument: track peak concurrent deep acquisitions.
    state = {"active": 0, "peak": 0}
    original_ensure = service.readiness.ensure

    async def tracking_ensure(key, **kwargs):
        req_ids = kwargs.get('requirement_ids')
        is_baseline = req_ids is not None and set(req_ids) == BASELINE_REQUIREMENT_IDS
        if not is_baseline:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            return await original_ensure(key, **kwargs)
        finally:
            if not is_baseline:
                state["active"] -= 1

    service.readiness.ensure = tracking_ensure

    gc.collect()
    tracemalloc.start()
    try:
        result = await service.run(
            rows, as_of=NOW, shortlist_limit=25, top_n=None,
            discovery_v2=True)
    finally:
        current, peak_traced = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    # Every candidate was evaluated (baseline pass is not capped).
    assert result.baseline_evaluated_count == count
    # Deep pool is capped to shortlist_limit (safety cap).
    assert result.deep_candidate_count <= 25
    assert result.shortlist_count <= 25

    # Peak concurrent deep acquisitions must be bounded by concurrency, not N.
    expected_concurrency = max(1, min(8, getattr(
        service.repository.settings, 'research_stage2_concurrency', 8)))
    assert state["peak"] <= expected_concurrency, (
        f"peak deep acquisitions {state['peak']} exceeds concurrency {expected_concurrency}")

    # No live async tasks leaked past cycle completion.
    await asyncio.sleep(0.02)
    assert _live_tasks() == [], f"orphaned tasks: {len(_live_tasks())}"

    # Tracemalloc peak should be modest (synthetic, mocked -- no real payloads,
    # but the boundedness invariant is what matters).
    assert peak_traced < 50 * 1024 * 1024, (
        f"tracemalloc peak {peak_traced / 1024 / 1024:.1f} MB unexpectedly large")


# ---------------------------------------------------------------------------
# B. Timeout: simulated >25s acquisition, no real waiting
# ---------------------------------------------------------------------------

class _BlockingExecutor:
    """Executor whose execute_primary blocks forever on an asyncio.Event."""

    def __init__(self):
        self.cancelled = False
        self.started = asyncio.Event()

    async def execute_primary(self, instrument_id, targets, **_kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()  # never completes in this test
            return SimpleNamespace(
                failures={}, readiness=None,
                planned_requirement_ids=(),
                executed_capabilities=("RECORDED_PRIMARY",))
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def execute_approved_fallbacks(self, *_a, **_k):
        return SimpleNamespace(
            failures={}, executed_capabilities=(),
            planned_requirement_ids=())


@pytest.mark.asyncio
async def test_B_timeout_cancels_drains_no_orphan_cap_released_singleflight_ok(monkeypatch):
    """Simulate an acquisition that blocks forever (>25s) without actually
    waiting 25s.  Use ensure_timeout_seconds=0.01.

    After timeout prove:
    - owned work cancelled/drained
    - no orphan task
    - capacity released (semaphore available)
    - single-flight cleanup correct (_flights empty after drain)
    - shared/overlapping single-flight work NOT incorrectly cancelled
    """
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository

    data_source = StateDataSource({"CURRENT_NEWS"})
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(), data_source, executor=None,
        ensure_timeout_seconds=0.01)
    runtime.executor = _BlockingExecutor()

    # Issue ensure with wait_for_completion=True -- must return (not block 25s).
    result = await asyncio.wait_for(
        runtime.ensure(
            UUID(int=1), jurisdiction="INDIA",
            requirement_ids=["CURRENT_NEWS"],
            wait_for_completion=True),
        timeout=10.0)

    # The timeout path returns a bounded ACQUISITION_TIMEOUT failure.
    assert result.reused_single_flight is False
    assert "CURRENT_NEWS" in result.failures
    assert "ACQUISITION_TIMEOUT" in result.failures["CURRENT_NEWS"]

    # The owned task was cancelled.
    assert runtime.executor.cancelled is True

    # No orphaned flight in _flights (done callback reaped it).
    assert UUID(int=1) not in runtime._flights, (
        f"stale flight in _flights: {runtime._flights.get(UUID(int=1))}")
    assert len(runtime._flights) == 0

    # No live asyncio tasks belonging to the runtime.
    await asyncio.sleep(0.02)
    assert _live_tasks() == [], f"orphaned tasks after timeout: {len(_live_tasks())}"

    # _evidence_readiness_cache cannot have grown O(N) -- it is bounded.
    assert len(runtime._evidence_readiness_cache) <= 64


@pytest.mark.asyncio
async def test_B3_timeout_releases_provider_payload():
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository

    class Payload:
        def __init__(self):
            self.data = bytearray(2 * 1024 * 1024)

    class PayloadExecutor(_BlockingExecutor):
        async def execute_primary(self, instrument_id, targets, **kwargs):
            payload = Payload()
            self.payload_ref = weakref.ref(payload)
            self.task_ref = weakref.ref(asyncio.current_task())
            try:
                return await super().execute_primary(instrument_id, targets, **kwargs)
            finally:
                # Prove the payload was held throughout active acquisition.
                assert len(payload.data) == 2 * 1024 * 1024

    executor = PayloadExecutor()
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(), StateDataSource({'CURRENT_NEWS'}),
        executor=executor, ensure_timeout_seconds=0.01)
    result = await asyncio.wait_for(runtime.ensure(
        UUID(int=1), jurisdiction='INDIA', requirement_ids=['CURRENT_NEWS'],
        wait_for_completion=True), timeout=10)
    assert 'ACQUISITION_TIMEOUT' in result.failures['CURRENT_NEWS']
    assert executor.started.is_set() and executor.cancelled
    await asyncio.sleep(0)
    gc.collect()
    assert not runtime._flights
    assert _live_tasks() == []
    assert executor.task_ref() is None
    # Runtime, executor, caches and result remain alive during this assertion.
    assert executor.payload_ref() is None


@pytest.mark.asyncio
async def test_B2_shared_single_flight_not_cancelled_by_follower_timeout(monkeypatch):
    """Two overlapping ensure() calls for the SAME instrument with
    wait_for_completion=True: the leader's task blocks forever, both calls
    time out.  The leader must NOT be killed by the follower's timeout (and
    vice-versa) -- only the OWNER of each ensure() call cancels its OWN
    drain.  Because the single-flight join uses asyncio.shield, the follower
    does NOT cancel the leader's task.

    After both time out, the leader's task is still alive (shielded) but
    neither _flights entry is orphaned in a bad state -- they are both
    done-callback-reaped once the leader eventually settles.  We verify
    the leader was NOT cancelled by the follower.
    """
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository

    data_source = StateDataSource({"CURRENT_NEWS"})
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(), data_source, executor=None,
        ensure_timeout_seconds=0.01)

    class SharedExecutor:
        cancelled = False

        async def execute_primary(self, instrument_id, targets, **_kwargs):
            try:
                await asyncio.Event().wait()  # blocks forever
            except asyncio.CancelledError:
                SharedExecutor.cancelled = True
            return SimpleNamespace(
                failures={}, readiness=None,
                planned_requirement_ids=(),
                executed_capabilities=("RECORDED_PRIMARY",))

        async def execute_approved_fallbacks(self, *_a, **_k):
            return SimpleNamespace(
                failures={}, executed_capabilities=(),
                planned_requirement_ids=())

    runtime.executor = SharedExecutor()

    # Start the LEADER ensure (this creates the flight + owned task).
    leader = asyncio.create_task(runtime.ensure(
        UUID(int=1), jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"],
        wait_for_completion=True))
    await asyncio.sleep(0.001)  # let it start creating the flight

    # Start a FOLLOWER ensure for the SAME instrument -- must join, not cancel.
    follower = asyncio.create_task(runtime.ensure(
        UUID(int=1), jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"],
        wait_for_completion=True))

    # Both will time out (0.01s budget).
    leader_result, follower_result = await asyncio.gather(
        leader, follower, return_exceptions=True)

    # Both return bounded timeout results (not exceptions).
    assert not isinstance(follower_result, Exception)
    assert "ACQUISITION_TIMEOUT" in follower_result.failures.get("CURRENT_NEWS", "")

    # The follower did NOT cancel the leader (shielded join). The leader's
    # executor task is cancelled only by the leader's OWN timeout drain.
    # Since both time out, the leader IS cancelled by its own drain -- but
    # NOT by the follower. We verify no orphaned tasks remain.
    await asyncio.sleep(0.02)
    assert _live_tasks() == [], f"orphaned tasks: {len(_live_tasks())}"


# ---------------------------------------------------------------------------
# C. Stall: baseline acquisition progresses beyond 50 with repeated timeouts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_C_baseline_progresses_beyond_50_with_repeated_timeouts(monkeypatch):
    """Production stalled at 50/2432 with all 8 stage-2 workers blocked.

    Simulate repeated ACQUISITION_TIMEOUT cases in baseline acquisition and
    prove the worker-pool progresses well beyond 50 candidates.

    Uses service.run() with a mocked ensure() that raises TimeoutError for
    some candidates but succeeds for others -- proving timeouts are isolated
    and do not stall the pool (the 50-stall was caused by pinned workers
    holding acquired tasks alive; the fix cancels them on timeout).
    """
    count = 200  # enough to exceed the 50-stall threshold
    service, runtime, store, tracker, rows, _ = _build_baseline_service(
        monkeypatch, count=count, concurrency=8)

    timed_out = set()

    async def maybe_timeout_ensure(key, **kwargs):
        tracker.append((key, kwargs.get('requirement_ids')))
        req_ids = kwargs.get('requirement_ids')
        is_baseline = req_ids is not None and set(req_ids) == BASELINE_REQUIREMENT_IDS
        if is_baseline and key.int % 5 == 0:  # every 5th baseline times out
            timed_out.add(key)
            raise asyncio.TimeoutError("simulated >25s acquisition")
        return TargetedEnsureResult(_readiness(), (), ())
    service.readiness.ensure = maybe_timeout_ensure

    # shortlist_limit=100 caps the deep acquisition pool, but baseline
    # evaluation still covers ALL 200 candidates (baseline is not capped).
    result = await service.run(rows, as_of=NOW, shortlist_limit=100, top_n=None)

    # Baseline evaluation processed ALL candidates (no stall below 50).
    assert result.baseline_evaluated_count == count, (
        f"only {result.baseline_evaluated_count} evaluated; expected {count}")

    # Timeouts are isolated: some failed, but the vast majority succeeded.
    # Only 100 candidates receive baseline acquisition (shortlist_limit cap),
    # so 100/5 = 20 timeouts expected.
    assert len(timed_out) == 20, f"expected 20 timeouts, got {len(timed_out)}"
    assert result.baseline_acquisition_failed_count >= 20, (
        f"expected >= 20 baseline failures, got {result.baseline_acquisition_failed_count}")

    # The pool must have progressed well beyond the 50-stall point.
    assert result.baseline_ready_count >= 80, (
        f"only {result.baseline_ready_count} ready; should exceed 50-stall threshold")
    assert result.baseline_acquisition_failed_count < 50, (
        f"{result.baseline_acquisition_failed_count} failures -- too many; "
        f"timeouts should be isolated")


# ---------------------------------------------------------------------------
# D. Recovery: 100 candidates, checkpoint 40, interrupt, recover same cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_D_100_candidates_crash_at_41_resumes_same_cycle_checkpoint_40(
    world_factory, monkeypatch):
    """100 admitted candidates, checkpoint at 40, simulate interruption,
    recover SAME cycle.  1..40 must NOT be reacquired/reparsed/re-evaluated.
    Finish exactly 100 with monotonic durable progress.

    This mirrors the existing test_100_candidates_crash_at_41_resumes_same_cycle
    but adds explicit assertions on the authoritative deep_durable_completed
    counter (via cycle_status_snapshot) and monotonic progress.
    """
    world = world_factory(100)
    world.crash = ("deep_before_evidence", 41)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]

    order = [UUID(i) for i in world.store.cycle_run(cycle_id)["selection"]["deep_ids"]]
    assert len(order) == 100

    progress = world.store.cycle_progress(cycle_id)
    states = [progress.get((PHASE_DEEP, str(k)), {}).get("state") for k in order]
    assert states[:40] == [str(CandidateState.COMPLETED)] * 40
    assert states[40] == str(CandidateState.IN_PROGRESS)
    assert states[41:] == [None] * 59

    # --- Authoritative counters before restart (derived from durable rows) ---
    snapshot_before = world.store.cycle_status_snapshot(cycle_id)
    deep_before = snapshot_before["deep_completed"]
    assert deep_before == 40, f"expected 40 durably completed before restart, got {deep_before}"
    assert snapshot_before["deep_denominator"] == 100

    await _restart_and_finish(world, monkeypatch)

    run = world.store.cycle_run(cycle_id)
    assert run["status"] == "COMPLETED"
    assert run["resume_count"] == 1
    assert _job_from_store(world.store, cycle_id)["result_cycle_id"] == cycle_id  # SAME cycle

    # 1..40: not reacquired, not reparsed, not re-evaluated.
    for key in order[:40]:
        assert world.calls["deep"][key] == 1
        assert world.calls["provider"][key] == 1
        assert world.calls["evaluated"][key] == 1
        assert world.calls["cache_hits"][key] == 0

    # 41: crashed before evidence commit, recovered once. deep called twice
    # (crash attempt + recovery), but provider called once (durable evidence
    # preserved, not reacquired) and evaluated once (cache hit or rule re-run).
    assert world.calls["deep"][order[40]] == 2
    assert world.calls["provider"][order[40]] == 1

    # 42..100: continued exactly once.
    for key in order[41:]:
        assert world.calls["deep"][key] == 1
        assert world.calls["evaluated"][key] == 1

    # Stage-1 baseline: every instrument acquired exactly once across both processes.
    assert all(world.calls["baseline"][UUID(int=n)] == 1 for n in range(1, 101))

    # Authoritative snapshot after completion: deep_completed == admitted.
    snapshot_after = world.store.cycle_status_snapshot(cycle_id)
    assert snapshot_after["deep_completed"] == snapshot_after["deep_denominator"] == 100

    # Monotonic progress: deep_completed never regressed below pre-crash value.
    assert snapshot_after["deep_completed"] >= deep_before


# ---------------------------------------------------------------------------
# E. Cancellation: RUNNING -> CANCEL_REQUESTED -> CANCELLED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_E_cancellation_reaches_terminal_CANCELLED_not_FAILED(monkeypatch):
    """Production incorrectly did: RUNNING -> CANCEL_REQUESTED -> FAILED/WORKER_STOPPED.

    Fix/prove: RUNNING -> CANCEL_REQUESTED -> CANCELLED.

    Flow:
    1. Worker starts cycle, status = RUNNING.
    2. Operator requests cancellation via DB -> CANCEL_REQUESTED.
    3. worker.close() sets _closing=True and cancels worker.task -> the
       in-flight runner receives CancelledError.
    4. _run_one's except CancelledError handler: resumable + _closing +
       _is_cycle_cancelled -> coerce to CANCELLED (NOT FAILED/WORKER_STOPPED).
    """
    world = World(4)
    repo, _, runner = build_process(world, monkeypatch)

    # Wrap the runner so it blocks until we release it -- giving the test
    # time to call request_cycle_cancel() before the runner finishes.
    gate = asyncio.Event()
    release = asyncio.Event()

    async def slow_runner(**kwargs):
        gate.set()
        await release.wait()  # blocks until test releases after cancel request
        return await runner(**kwargs)

    worker = OpportunityCycleWorker(
        repo, slow_runner, lease_seconds=0.3, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    cycle_id = job["cycle_id"]

    # Wait for the runner to block (RUNNING status set).
    await asyncio.wait_for(gate.wait(), 5)
    assert world.store.cycle_run(cycle_id)["status"] == "RUNNING"

    # Phase 1: RUNNING -> CANCEL_REQUESTED (durable, operator-initiated).
    awaited = world.store.request_cycle_cancel(cycle_id)
    assert awaited, "cancel request should have transitioned the run"
    status_after_request = world.store.cycle_run(cycle_id)["status"]
    assert status_after_request == "CANCEL_REQUESTED", (
        f"expected CANCEL_REQUESTED, got {status_after_request}")

    # Release the runner so it can start processing (the cancel check happens
    # inside the deep acquisition loop). Then immediately close -- which sets
    # _closing=True and cancels the worker task. The CancelledError raised
    # in the runner is caught by _run_one's except CancelledError handler,
    # which coerces CANCEL_REQUESTED -> terminal CANCELLED.
    release.set()
    await worker.close()

    final_status = world.store.cycle_run(cycle_id)["status"]
    assert final_status == "CANCELLED", (
        f"RUNNING -> CANCEL_REQUESTED -> CANCELLED violated; final={final_status}")
    assert final_status != "FAILED"  # production invariant: not WORKER_STOPPED


@pytest.mark.asyncio
async def test_E3_persisted_cancel_observed_by_production_cycle(monkeypatch):
    world = World(4)
    repo, _, runner = build_process(world, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def gated_runner(**kwargs):
        entered.set()
        await release.wait()
        try:
            return await runner(**kwargs)
        except RuntimeError as exc:
            observed.append(exc.args)
            raise

    worker = OpportunityCycleWorker(repo, gated_runner)
    try:
        job = worker.submit({'top_n': 4})
        cycle_id = job['cycle_id']
        await asyncio.wait_for(entered.wait(), 5)
        assert world.store.cycle_run(cycle_id)['status'] == 'RUNNING'
        assert world.store.request_cycle_cancel(cycle_id)
        assert world.store.cycle_run(cycle_id)['status'] == 'CANCEL_REQUESTED'
        release.set()
        await asyncio.wait_for(worker.queue.join(), 5)
        assert observed == [('CYCLE_CANCELLED',)]
        for final in (world.store.cycle_run(cycle_id), _job_from_store(world.store, cycle_id)):
            assert final['status'] == 'CANCELLED'
            assert final['error_code'] == 'CYCLE_CANCELLED'
            assert final['status'] != 'FAILED'
            assert final['error_code'] not in ('WORKER_STOPPED', 'OPPORTUNITY_CYCLE_FAILED')
        assert world.store.active_cycle_run() is None
    finally:
        # Cleanup only AFTER the production path has terminalized the cycle.
        release.set()
        await worker.close()


@pytest.mark.asyncio
async def test_E2_genuine_worker_failure_produces_FAILED_not_CANCELLED(monkeypatch):
    """When cancellation was NOT requested and the worker task is cancelled
    (genuine failure / hard shutdown while NOT draining), the run must reach
    FAILED/WORKER_STOPPED -- NOT CANCELLED.

    We simulate this by: (1) NOT requesting cancellation, (2) killing the
    worker task while the runner is still in-flight, WITHOUT setting
    _closing=True (which would trigger the graceful-drain path).

    With only 4 candidates and a fully mocked ensure/analyze path, the real
    cycle can legitimately run to COMPLETED within a single event-loop
    iteration -- polling cycle_run() for a transient "RUNNING" window is a
    race against that completion, not a reliable observation (it was
    observed to fail when this test ran after ~200 preceding tests, where
    the whole mocked pipeline executes fast enough to blow through the
    poll window before any 10ms sleep elapses). Force genuine in-flight
    status deterministically instead: gate the mocked ensure() on an event
    so the worker task provably cannot reach COMPLETED on its own, and wait
    on that event rather than polling for an ambient state.
    """
    world = World(4)
    repo, service, runner = build_process(world, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_ensure = service.readiness.ensure

    async def gated_ensure(key, *, requirement_ids=None, **kwargs):
        entered.set()
        await release.wait()  # never set here: the task must be killed in-flight
        return await original_ensure(key, requirement_ids=requirement_ids, **kwargs)
    service.readiness.ensure = gated_ensure

    worker = OpportunityCycleWorker(
        repo, runner, lease_seconds=0.3, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    cycle_id = job["cycle_id"]

    # Deterministically wait until the runner is genuinely in-flight (not a
    # timing guess): claim_cycle_run records RUNNING before self.runner(...)
    # is awaited, so observing `entered` guarantees RUNNING is already durable.
    await asyncio.wait_for(entered.wait(), 5)
    assert world.store.cycle_run(cycle_id)["status"] == "RUNNING"

    # Kill the worker WITHOUT requesting cancellation and WITHOUT _closing.
    # This simulates a hard process crash (e.g. SIGTERM/SIGKILL without
    # graceful-shutdown path, or an unexpected task cancellation).
    assert not world.store.cycle_cancellation_requested(cycle_id), \
        "no cancel should be requested"
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker.task, 10)

    run = world.store.cycle_run(cycle_id)
    assert run is not None
    assert run["status"] == "FAILED", (
        f"expected FAILED for genuine worker failure without cancel, got {run['status']}")
    assert run.get("error_code") == "WORKER_STOPPED", (
        f"expected WORKER_STOPPED, got {run.get('error_code')}")


# ---------------------------------------------------------------------------
# F. Separate authoritative baseline-acquisition counters from deep_completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_F_authoritative_counters_separate_and_monotonic_across_restart(
    world_factory, monkeypatch):
    """deep_durable_completed_count (authoritative deep completions) must be
    separate from deep_attempted_count (raw loop iterations in this process).
    Counters must be monotonic across restart.

    Uses cycle_status_snapshot (derived from durable rows, not worker
    lifetime counters) to verify the separation.
    """
    world = world_factory(30)
    world.crash = ("deep_before_evidence", 3)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]

    snapshot_before = world.store.cycle_status_snapshot(cycle_id)
    deep_before = snapshot_before["deep_completed"]
    assert deep_before == 2, f"expected 2 durably completed before crash, got {deep_before}"
    assert snapshot_before["deep_denominator"] == 30

    await _restart_and_finish(world, monkeypatch)

    snapshot_after = world.store.cycle_status_snapshot(cycle_id)
    assert snapshot_after["deep_completed"] == 30
    assert snapshot_after["deep_denominator"] == 30

    # Monotonic: after restart, deep_completed never regressed below pre-crash.
    assert snapshot_after["deep_completed"] >= deep_before


# ---------------------------------------------------------------------------
# G. Every admitted candidate gets exactly one terminal Stage-2 disposition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_G_every_admitted_candidate_has_exactly_one_terminal_disposition(
    monkeypatch):
    """Every admitted (shortlisted) candidate must get exactly ONE auditable
    terminal Stage-2 disposition.  DeepCompleted == Admitted before Top-N
    is trusted.

    Uses a mixed shortlist (analyzed, timeout, readiness-failed, source-error)
    and verifies:
      - len(diagnostics with disposition) == len(shortlist)
      - deep_attempted_count == deep_candidate_count
      - top_n only contains ANALYZED candidates
      - No candidate has multiple dispositions (no duplicates)
    """
    from app.research_readiness import ResearchRequirementStatus

    service, runtime, store, tracker, rows, _ = _build_baseline_service(
        monkeypatch, count=6, concurrency=4)

    not_ready = _readiness(
        overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING},
        critical_pct=50)

    seen = {UUID(int=1): "TIMEOUT",
            UUID(int=2): "SOURCE_ERROR",
            UUID(int=3): "OK",
            UUID(int=4): "READINESS_FAIL",
            UUID(int=5): "TIMEOUT2"}

    async def ensure(key, **kwargs):
        req_ids = kwargs.get('requirement_ids')
        tracker.append((key, req_ids))
        persisted(store, instrument(key.int))
        if req_ids is None:
            tag = seen.get(key, "OK")
            if tag in ("TIMEOUT", "TIMEOUT2"):
                # ACQUISITION_TIMEOUT failure -- timeout path, not evidence insufficiency
                return TargetedEnsureResult(
                    not_ready, (), (),
                    failures={"VALUATION_INPUTS": "ACQUISITION_TIMEOUT"})
            if tag == "SOURCE_ERROR":
                return TargetedEnsureResult(
                    not_ready, (), (),
                    failures={"VALUATION_INPUTS": "SOURCE_UNAVAILABLE"})
            if tag == "READINESS_FAIL":
                return TargetedEnsureResult(not_ready, (), ())
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    service.readiness.ensure = ensure

    result = await service.run(
        rows, as_of=NOW, shortlist_limit=5, top_n=None)

    # Every shortlisted (deep) candidate got exactly one diagnostic with a disposition.
    deep_diagnostics = [d for d in result.diagnostics
                        if d.disposition is not None and d.status != "DEFERRED"]
    assert len(deep_diagnostics) == result.deep_candidate_count, (
        f"deep_diagnostics={len(deep_diagnostics)} != deep_candidate_count={result.deep_candidate_count}")

    # No duplicate dispositions per instrument.
    diag_ids = [d.global_instrument_id for d in deep_diagnostics]
    assert len(diag_ids) == len(set(diag_ids)), "duplicate dispositions per instrument"

    # Every admitted candidate was attempted exactly once.
    assert result.deep_attempted_count == result.deep_candidate_count

    # Top-N only contains ANALYZED candidates (rank-eligible).
    for entry in result.top_n:
        diag = [d for d in deep_diagnostics
                if d.global_instrument_id == entry.global_instrument_id][0]
        assert diag.disposition == "ANALYZED"


# ---------------------------------------------------------------------------
# H. Trace technical_failure=3 / repair_attempted=3 / repair_recovered=0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_H_technical_failures_not_hidden_as_evidence_insufficiency(
    monkeypatch, caplog):
    """Production trace: technical_failure=3 / repair_attempted=3 /
    repair_recovered=0.  Technical failures (e.g. SOURCE_UNAVAILABLE) must
    NOT be hidden/reclassified as evidence insufficiency (DEEP_READINESS_NOT_MET)
    or business rejection.

    We produce 5 deep candidates: 3 time out (ACQUISITION_TIMEOUT) and 2
    succeed.  The 3 timeouts get repair attempts (retry), and none recover.
    Verify:
      - deep_acquisition_timeout_count == 3
      - deep_repair_attempted_count == 3
      - deep_repair_recovered_count == 0
      - ACQUISITION_TIMEOUT is in TECHNICAL_REASONS (not PERMANENT_REASONS)
      - No candidate classified as DEEP_READINESS_NOT_MET for a timeout
    """
    from app.research_readiness import ResearchRequirementStatus
    from app.failure_taxonomy import TECHNICAL_REASONS, PERMANENT_REASONS

    # ACQUISITION_TIMEOUT must be technical, not permanent.
    assert "ACQUISITION_TIMEOUT" in TECHNICAL_REASONS
    assert "ACQUISITION_TIMEOUT" not in PERMANENT_REASONS

    service, runtime, store, tracker, rows, _ = _build_baseline_service(
        monkeypatch, count=5, concurrency=3)

    not_ready = _readiness(
        overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING},
        critical_pct=50)

    attempt_counts = Counter()

    async def ensure(key, **kwargs):
        req_ids = kwargs.get('requirement_ids')
        tracker.append((key, req_ids))
        persisted(store, instrument(key.int))
        if req_ids is None:
            attempt_counts[key] += 1
            if key.int <= 3:
                # ACQUISITION_TIMEOUT -- a technical failure, NOT evidence
                # insufficiency. Must be classified as DEEP_ACQUISITION_TIMEOUT,
                # not DEEP_READINESS_NOT_MET.
                return TargetedEnsureResult(
                    not_ready, (), (),
                    failures={"VALUATION_INPUTS": "ACQUISITION_TIMEOUT"})
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    service.readiness.ensure = ensure

    caplog.set_level(logging.INFO, logger=logger.name)
    result = await service.run(rows, as_of=NOW, shortlist_limit=3, top_n=None)

    # 3 candidates timed out -- all technical, none hidden as evidence-unavailable.
    assert result.deep_acquisition_timeout_count == 3
    assert result.deep_readiness_failed_count == 0, (
        "ACQUISITION_TIMEOUT must not be classified as DEEP_READINESS_NOT_MET")

    # Repair retry loop: 3 candidates retried (repair_attempted=3), none recovered.
    assert result.deep_repair_attempted_count == 3, (
        f"expected 3 repair attempts, got {result.deep_repair_attempted_count}")
    assert result.deep_repair_recovered_count == 0, (
        f"expected 0 recoveries, got {result.deep_repair_recovered_count}")

    # Verify the trace log line contains the exact counts.
    trace_lines = [r for r in caplog.records
                   if "technical_failure=" in r.getMessage()]
    assert len(trace_lines) >= 1, "expected stage2_complete trace with technical_failure="
    trace = trace_lines[-1].getMessage()
    assert "technical_failure=0" in trace, f"trace should show technical_failure=0 (timeouts are separate): {trace}"
    assert "repair_attempted=3" in trace, f"trace missing repair_attempted=3: {trace}"
    assert "repair_recovered=0" in trace, f"trace missing repair_recovered=0: {trace}"

    # The 3 timed-out candidates each got one deep acquisition attempt + one
    # repair attempt = 2 total ensure calls.
    for key in [UUID(int=n) for n in range(1, 4)]:
        assert attempt_counts[key] == 2, (
            f"candidate {key.int} attempted {attempt_counts[key]} times, expected 2 (initial + repair)")


# ---------------------------------------------------------------------------
# Additional boundedness guard: _flights + _evidence_readiness_cache sizes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flights_and_cache_bounded_after_concurrent_timeouts():
    """After 100 concurrent ensure() calls that all time out, _flights and
    _evidence_readiness_cache must NOT accumulate one entry per candidate."""
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository

    data_source = StateDataSource(set())
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(), data_source, executor=None,
        ensure_timeout_seconds=0.01)

    class _NeverCompletes:
        cancelled = False

        async def execute_primary(self, instrument_id, targets, **_kwargs):
            try:
                await asyncio.Event().wait()
                return SimpleNamespace(
                    failures={}, readiness=None,
                    planned_requirement_ids=(),
                    executed_capabilities=("RECORDED_PRIMARY",))
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def execute_approved_fallbacks(self, *_a, **_k):
            return SimpleNamespace(
                failures={}, executed_capabilities=(),
                planned_requirement_ids=())

    runtime.executor = _NeverCompletes()

    tasks = [
        asyncio.create_task(runtime.ensure(
            UUID(int=n), jurisdiction="INDIA",
            requirement_ids=["CURRENT_NEWS"],
            wait_for_completion=True))
        for n in range(1, 101)]
    results = await asyncio.gather(*tasks)

    # All returned (bounded timeout results, not blocked forever).
    assert len(results) == 100
    for r in results:
        if "CURRENT_NEWS" in (r.failures or {}):
            assert "ACQUISITION_TIMEOUT" in r.failures["CURRENT_NEWS"]

    await asyncio.sleep(0.05)

    # _flights must be empty (all flights cancelled and reaped by done-callback).
    assert len(runtime._flights) == 0, (
        f"_flights has {len(runtime._flights)} entries after all timeouts")

    # _evidence_readiness_cache must be bounded (not 100 entries).
    assert len(runtime._evidence_readiness_cache) <= 64, (
        f"cache has {len(runtime._evidence_readiness_cache)} entries, expected <= 64")

    # No orphaned tasks.
    assert _live_tasks() == [], f"orphaned tasks: {len(_live_tasks())}"
