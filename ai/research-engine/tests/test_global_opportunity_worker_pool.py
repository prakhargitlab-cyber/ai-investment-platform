"""Memory-safe regression tests for the bounded baseline-acquisition worker-pool.

These tests exercise ``GlobalOpportunityOrchestrator._acquire_baseline_requirements``
in isolation. ``_acquire_one`` is driven through its mocked dependencies
(profile_hydrator / repository.profile / readiness_adapter / readiness.ensure)
so the *worker-pool* scheduling invariants can be asserted deterministically
without real network or database I/O.

Invariants asserted (all required by the OOM remediation spec):
  1. The pool never materialises O(N) acquisition coroutines -- peak in-flight
     workers stay <= ``_BASELINE_CONCURRENCY`` even for very large universes.
  2. Max simultaneous baseline acquisitions never exceeds the configured
     concurrency (and tracks the constant, so it is not hard-coded).
  3. Every eligible candidate is eventually processed (no silent drops).
  4. Per-candidate failures are isolated (one failure never aborts the pass).
  5. Ordering / result determinism is preserved (keyed by global_instrument_id,
     deterministic per-candidate result across runs).
  6. Cancellation / shutdown drains worker tasks cleanly (no orphaned tasks).
  7. Fairness: the pool processes ALL eligible candidates it is handed -- there
     is no shortlist / sampling cap applied inside baseline acquisition
     (shortlist_limit never caps baseline evaluation).
  8. (Task C) progress logging emits start/progress/complete milestones so a
     full-market cycle cannot look "hung" when it is merely slow.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from test_global_scanner import NOW

import app.global_opportunity_orchestration as orch_mod
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator


def _candidates(n):
    """StageBCandidate stand-ins: ``_acquire_one`` only reads global_instrument_id."""
    return [SimpleNamespace(global_instrument_id=UUID(int=i)) for i in range(1, n + 1)]


def _make_service(monkeypatch, *, concurrency=None, delay=0.02, fail_keys=()):
    """Build an orchestrator whose baseline acquisition path is fully mocked.

    ``readiness_runtime`` is left as None (so acquisition_enabled=False at the
    orchestrator level), but ``_acquire_baseline_requirements`` is driven
    directly here; the mocks replace only what ``_acquire_one`` touches.
    """
    repo = MagicMock()
    store = MagicMock()
    service = GlobalOpportunityOrchestrator(
        repo,
        store,
        profile_hydrator=lambda key, payload: True,
        readiness_runtime=None,
        clock=lambda: NOW,
    )
    service.repository.profile = lambda key: SimpleNamespace(instrument_id=key)
    service.readiness_adapter = MagicMock()  # remember_canonical_metadata -> noop
    if concurrency is not None:
        service._BASELINE_CONCURRENCY = concurrency
    # jurisdiction_for_profile is a module-level import binding used by _acquire_one
    monkeypatch.setattr(orch_mod, "jurisdiction_for_profile", lambda profile: "IN")

    state = {"active": 0, "max_active": 0, "done": 0}
    fail = set(fail_keys)

    async def fake_ensure(key, **kwargs):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        if delay:
            await asyncio.sleep(delay)
        state["active"] -= 1
        state["done"] += 1
        if key in fail:
            raise RuntimeError("simulated provider failure")
        return SimpleNamespace(planned_requirement_ids=(str(key),), readiness=object())

    service.readiness = MagicMock()
    service.readiness.ensure = fake_ensure
    return service, state


@pytest.mark.asyncio
async def test_worker_pool_not_oN_tasks(monkeypatch):
    # N >> concurrency: peak in-flight workers must stay O(concurrency), not O(N).
    service, state = _make_service(monkeypatch, delay=0.02)
    cands = _candidates(1000)
    results, failures = await service._acquire_baseline_requirements(cands, {}, NOW, {})
    assert state["done"] == 1000
    assert len(results) == 1000
    assert failures == {}
    assert state["max_active"] <= service._BASELINE_CONCURRENCY  # bounded == 8
    assert state["max_active"] >= 2  # genuinely concurrent, not serial


@pytest.mark.asyncio
async def test_worker_pool_max_simultaneous_respects_configurable_concurrency(monkeypatch):
    # Lowering the configured concurrency must actually lower the observed peak.
    service, state = _make_service(monkeypatch, concurrency=2, delay=0.02)
    cands = _candidates(60)
    results, failures = await service._acquire_baseline_requirements(cands, {}, NOW, {})
    assert len(results) == 60
    assert state["max_active"] <= 2  # tracks the configured constant
    assert state["max_active"] >= 2


@pytest.mark.asyncio
async def test_worker_pool_processes_all_candidates(monkeypatch):
    service, state = _make_service(monkeypatch, delay=0.0)
    cands = _candidates(50)
    results, failures = await service._acquire_baseline_requirements(cands, {}, NOW, {})
    assert state["done"] == 50
    assert set(results.keys()) == {c.global_instrument_id for c in cands}
    assert failures == {}


@pytest.mark.asyncio
async def test_worker_pool_failures_are_isolated(monkeypatch):
    cands = _candidates(3)
    fail_key = cands[1].global_instrument_id
    service, state = _make_service(monkeypatch, delay=0.01, fail_keys=(fail_key,))
    results, failures = await service._acquire_baseline_requirements(cands, {}, NOW, {})
    # The failing candidate is recorded as a failure; the other two succeed.
    assert fail_key in failures
    assert failures[fail_key] == "BASELINE_ACQUISITION_FAILED"
    assert results[fail_key] is None  # _acquire_one returns (key, None, reason)
    ok = {k for k, v in results.items() if v is not None}
    assert ok == {cands[0].global_instrument_id, cands[2].global_instrument_id}
    assert state["done"] == 3  # every candidate was attempted


@pytest.mark.asyncio
async def test_worker_pool_ordering_and_result_determinism(monkeypatch):
    cands = _candidates(40)
    expected = {c.global_instrument_id for c in cands}

    def fresh():
        return _make_service(monkeypatch, delay=0.015)

    service_a, _ = fresh()
    res_a, _ = await service_a._acquire_baseline_requirements(cands, {}, NOW, {})
    service_b, _ = fresh()
    res_b, _ = await service_b._acquire_baseline_requirements(cands, {}, NOW, {})

    # Keyed by global_instrument_id: same candidate set, deterministic per-key result.
    assert set(res_a.keys()) == expected
    assert set(res_b.keys()) == expected
    assert {k: res_a[k] for k in res_a} == {k: res_b[k] for k in res_b}
    assert all(res_a[k].planned_requirement_ids == (str(k),) for k in res_a)
    assert all(not hasattr(value, "readiness") for value in res_a.values())


@pytest.mark.asyncio
async def test_worker_pool_cancellation_drains_cleanly(monkeypatch):
    # A never-released gate blocks every worker once it starts; cancellation must
    # drain (cancel + gather) every in-flight task so none is orphaned.
    release = asyncio.Event()
    state = {"active": 0, "max_active": 0, "done": 0}

    async def blocking_ensure(key, **kwargs):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            await release.wait()
        finally:
            state["active"] -= 1
        state["done"] += 1
        return key

    service = GlobalOpportunityOrchestrator(
        MagicMock(),
        MagicMock(),
        profile_hydrator=lambda key, payload: True,
        readiness_runtime=None,
        clock=lambda: NOW,
    )
    service.repository.profile = lambda key: SimpleNamespace(instrument_id=key)
    service.readiness_adapter = MagicMock()
    monkeypatch.setattr(orch_mod, "jurisdiction_for_profile", lambda profile: "IN")
    service.readiness = MagicMock()
    service.readiness.ensure = blocking_ensure

    cands = _candidates(5)
    task = asyncio.ensure_future(
        service._acquire_baseline_requirements(cands, {}, NOW, {})
    )
    # Wait until at least one worker is actually in-flight (blocked on the gate).
    for _ in range(200):
        if state["active"] >= 1:
            break
        await asyncio.sleep(0.01)
    assert state["active"] >= 1, "workers never started"

    alive_before = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert len(alive_before) >= 1  # at least one worker task was seeded

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0)  # let the drain (gather) finalise

    alive_after = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert alive_after == [], f"orphaned worker tasks remain: {len(alive_after)}"
    assert state["active"] == 0  # every in-flight worker unwound via cancel


@pytest.mark.asyncio
async def test_worker_pool_no_shortlist_cap_all_eligible_evaluated(monkeypatch):
    # Fairness invariant at the worker-pool level: every eligible candidate handed
    # to the pool is processed. There is no shortlist/sampling cap inside
    # _acquire_baseline_requirements -- shortlist_limit is applied only AFTER
    # baseline evaluation by the ranker (see test_global_opportunity_orchestration).
    assert GlobalOpportunityOrchestrator._BASELINE_CONCURRENCY == 8
    service, state = _make_service(monkeypatch, delay=0.0)
    cands = _candidates(137)
    results, failures = await service._acquire_baseline_requirements(cands, {}, NOW, {})
    assert len(results) == 137  # no cap; all eligible baselines evaluated
    assert state["done"] == 137


@pytest.mark.asyncio
async def test_worker_pool_progress_logging(caplog, monkeypatch):
    # Task C: a full-market cycle must be observable (start/progress/complete) so
    # "slow" is distinguishable from "hung".
    caplog.set_level(logging.INFO, logger=orch_mod.logger.name)
    service, state = _make_service(monkeypatch, delay=0.0)
    results, failures = await service._acquire_baseline_requirements(
        _candidates(120), {}, NOW, {}
    )
    assert len(results) == 120
    assert "baseline_acquisition_start" in caplog.text
    assert "baseline_acquisition_progress" in caplog.text
    assert "baseline_acquisition_complete" in caplog.text
