"""Consolidated fix pass -- Section H/I: the bounded, process-lifetime
background task registry that replaced Stage-2's phase-local
`pending_news_tasks` set (see app/background_task_registry.py and the
`finally:` block in GlobalOpportunityOrchestrator.run()).

Covers required tests:
  17. Optional news background execution does not block mandatory cycle
      completion.
  18. Background news task exception is observed safely.
  19. Background task registry is bounded/cleaned after completion.

Plus the CURRENT_NEWS call-site backpressure fallback in
app/deep_investigation.py (bounded, never loses evidence) and the
process-shutdown drain wired into app/main.py's research_lifespan.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.background_task_registry import BoundedBackgroundTaskRegistry


# 17 -- adding to the registry never blocks the submitter ------------------
@pytest.mark.asyncio
async def test_17_add_does_not_block_on_a_slow_task():
    registry = BoundedBackgroundTaskRegistry(max_tasks=8)
    started = asyncio.get_running_loop().time()

    async def _slow():
        await asyncio.sleep(0.3)
        return "done"

    task = registry.add(asyncio.ensure_future(_slow()))
    elapsed = asyncio.get_running_loop().time() - started
    # add() itself must return essentially immediately -- it must never
    # await the task it is tracking.
    assert elapsed < 0.05
    assert len(registry) == 1
    assert not task.done()

    await registry.drain()
    assert task.done() and task.result() == "done"
    assert len(registry) == 0


# 17b -- a whole "phase" (simulated) completes without waiting for news ----
@pytest.mark.asyncio
async def test_17b_simulated_phase_completes_before_background_news_finishes():
    registry = BoundedBackgroundTaskRegistry(max_tasks=8)
    news_finished = {"flag": False}

    async def _news():
        await asyncio.sleep(0.25)
        news_finished["flag"] = True

    async def _phase():
        # Mirrors _acquire_deep(): kick off optional news in the
        # background, then finish the mandatory work and return --
        # without a phase-local set draining it in a `finally:`.
        registry.add(asyncio.ensure_future(_news()))
        return "PHASE_COMPLETE"

    result = await _phase()
    assert result == "PHASE_COMPLETE"
    # The phase returned before the background news task could possibly
    # have finished.
    assert news_finished["flag"] is False
    await registry.drain()
    assert news_finished["flag"] is True


# 18 -- a failing background task's exception is retrieved, never unhandled -
@pytest.mark.asyncio
async def test_18_failing_task_exception_is_observed_and_logged(caplog):
    registry = BoundedBackgroundTaskRegistry(max_tasks=8)

    async def _boom():
        raise RuntimeError("SIMULATED_NEWS_ACQUISITION_FAILURE")

    loop = asyncio.get_running_loop()
    unhandled = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, ctx: unhandled.append(ctx))
    try:
        with caplog.at_level(logging.WARNING, logger="app.background_task_registry"):
            task = registry.add(asyncio.ensure_future(_boom()))
            await registry.drain()
        import gc
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert unhandled == []  # never an unhandled/never-retrieved task exception
    assert task.done() and isinstance(task.exception(), RuntimeError)
    assert any("background_task_exception" in r.message for r in caplog.records)
    assert len(registry) == 0  # cleaned up by the done-callback regardless


# 19 -- bounded capacity + cleanup after completion -------------------------
@pytest.mark.asyncio
async def test_19_registry_is_bounded_and_frees_capacity_on_completion():
    registry = BoundedBackgroundTaskRegistry(max_tasks=2)
    gate = asyncio.Event()

    async def _held():
        await gate.wait()

    registry.add(asyncio.ensure_future(_held()))
    registry.add(asyncio.ensure_future(_held()))
    assert len(registry) == 2
    assert registry.has_capacity() is False  # at cap

    gate.set()
    await registry.drain()
    assert len(registry) == 0
    assert registry.has_capacity() is True  # freed after completion

    # A fresh task can be added again now that capacity exists.
    async def _quick():
        return "ok"
    registry.add(asyncio.ensure_future(_quick()))
    await registry.drain()
    assert len(registry) == 0


# 19b -- deep_investigation's CURRENT_NEWS call site respects capacity -----
@pytest.mark.asyncio
async def test_19b_current_news_falls_back_to_inline_when_registry_at_capacity():
    from dataclasses import replace
    from app.deep_investigation import investigate
    from app.research_readiness import ResearchRequirementStatus
    from test_stock_rule_engine import INSTRUMENT_ID, _readiness
    from types import SimpleNamespace

    class _Runtime:
        def __init__(self, readiness):
            self.readiness = readiness
            self.observations = []
            self.repository = SimpleNamespace(record_acquisition_observation=self._record)

        async def _record(self, *a, **k):
            self.observations.append((a, k))

        async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
            return self.readiness

        async def ensure(self, instrument_id, *, jurisdiction, requirement_ids, identity_headers=None,
                          correlation_id=None, wait_for_completion=True):
            from app.research_readiness_runtime import TargetedEnsureResult
            requirement_id = requirement_ids[0]
            self.readiness = replace(self.readiness, requirements=tuple(
                replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
                if row.requirement_id == requirement_id else row for row in self.readiness.requirements))
            return TargetedEnsureResult(self.readiness, (requirement_id,), (f"NSE:{requirement_id}",))

    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING,
                            "CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    runtime = _Runtime(readiness)
    registry = BoundedBackgroundTaskRegistry(max_tasks=1)
    # Fill the registry to capacity with an unrelated held task first.
    gate = asyncio.Event()
    async def _held():
        await gate.wait()
    registry.add(asyncio.ensure_future(_held()))
    assert registry.has_capacity() is False

    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=registry)
    # No new background task was added (still just the one held task) --
    # CURRENT_NEWS was acquired inline instead, so it is already resolved
    # by the time investigate() returns, evidence never lost.
    assert len(registry) == 1
    assert matrix["CURRENT_NEWS"]["state"] == "READY_FRESH"

    gate.set()
    await registry.drain()
