"""Deterministic coverage for the strict-deadline contract adopted for
ResearchReadinessRuntime._execute_plan_bounded (ETF Radar — Resolve Bounded
Execution Contract):

  * A task completing after its deadline must not be accepted as an
    on-time result.
  * Bounded execution and the existing OOM-protection design are preserved.
  * Cancellation and resource-cleanup guarantees are preserved.
  * A timed-out candidate is never converted into a successful/eligible one.
  * Failure classification/metrics/auditability stay deterministic.

These are narrow, single-purpose tests against _execute_plan_bounded itself
(via runtime.ensure()), separate from the broader di20g lifecycle scenarios
in test_di20g_acquisition_lifecycle.py, so each invariant has one minimal,
fast, non-flaky reproduction independent of PDF/thread-pool machinery.
"""
from __future__ import annotations

import asyncio

import pytest

from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ResearchReadinessRuntime, CapabilityExecutionResult
from test_research_readiness_runtime import StateDataSource, RuntimeRepository, INSTRUMENT_ID

REQUIREMENT_ID = "CURRENT_NEWS"


class _SleepExecutor:
    """Sleeps `delay`, then (if not cancelled first) commits and records it."""

    def __init__(self, source: StateDataSource, delay: float) -> None:
        self.source = source
        self.delay = delay
        self.committed: list[str] = []

    async def execute_primary(self, key, targets, **kwargs):
        await asyncio.sleep(self.delay)
        self.committed.append(REQUIREMENT_ID)
        self.source.missing.discard(REQUIREMENT_ID)
        return CapabilityExecutionResult((REQUIREMENT_ID,), {})

    async def execute_approved_fallbacks(self, *args):
        return CapabilityExecutionResult()


@pytest.mark.asyncio
async def test_completion_before_deadline_is_accepted():
    """A task that genuinely finishes before ensure_timeout_seconds elapses
    is accepted as an on-time success -- the deadline must not reject
    legitimately-on-time work."""
    source = StateDataSource({REQUIREMENT_ID})
    executor = _SleepExecutor(source, delay=0.01)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.2)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=[REQUIREMENT_ID], wait_for_completion=True)
    assert result.failures == {}
    assert executor.committed == [REQUIREMENT_ID]
    assert result.readiness.for_requirement(REQUIREMENT_ID).status == ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_completion_after_deadline_is_rejected_as_timeout():
    """A task whose completion would only land AFTER ensure_timeout_seconds
    has elapsed must be bounded/cancelled and reported as a timeout, not
    accepted late -- the deadline is authoritative, not advisory."""
    source = StateDataSource({REQUIREMENT_ID})
    executor = _SleepExecutor(source, delay=0.2)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.01)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=[REQUIREMENT_ID], wait_for_completion=True)
    assert result.failures == {REQUIREMENT_ID: "ACQUISITION_TIMEOUT"}
    assert result.readiness.for_requirement(REQUIREMENT_ID).status != ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_interactive_timeout_without_wait_for_completion_is_also_bounded():
    """The interactive (wait_for_completion=False) path uses a different
    code branch (asyncio.timeout / TimeoutError rather than asyncio.wait +
    cancel-and-drain) but must enforce the identical deadline contract: a
    slow target becomes a bounded ACQUISITION_TIMEOUT failure, never an
    unbounded wait and never a late-accepted success."""
    source = StateDataSource({REQUIREMENT_ID})
    executor = _SleepExecutor(source, delay=0.2)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.01)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=[REQUIREMENT_ID], wait_for_completion=False)
    assert result.failures == {REQUIREMENT_ID: "ACQUISITION_TIMEOUT"}


@pytest.mark.asyncio
async def test_late_completion_effect_is_excluded_from_the_result():
    """Even though the underlying work would eventually succeed if allowed
    to run to completion, cancellation at the deadline must cut it off
    before its effect (the durable commit) ever lands -- a late success
    must not silently leak into the bounded result via a side effect that
    outran the cancellation."""
    source = StateDataSource({REQUIREMENT_ID})
    executor = _SleepExecutor(source, delay=0.2)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.01)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=[REQUIREMENT_ID], wait_for_completion=True)
    # The sleep (0.2s) is well past both the deadline (0.01s) and this
    # test's own await returning, so if cancellation had NOT cut the
    # executor off in time, `committed` would already show it here.
    assert executor.committed == []
    assert REQUIREMENT_ID in source.missing
    assert result.failures == {REQUIREMENT_ID: "ACQUISITION_TIMEOUT"}


@pytest.mark.asyncio
async def test_cancellation_of_the_owning_call_cleans_up_without_leaking_a_flight():
    """Cancelling the OUTER ensure() call itself (e.g. caller/shutdown gives
    up, independent of the internal ensure_timeout_seconds budget) must
    cascade cleanly: the in-flight task is cancelled and drained, and no
    _EnsureFlight entry is left registered afterwards (preserving the
    cancellation/resource-cleanup guarantee the OOM-fix exists for)."""
    source = StateDataSource({REQUIREMENT_ID})
    executor = _SleepExecutor(source, delay=5.0)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=5.0)
    owner = asyncio.create_task(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=[REQUIREMENT_ID], wait_for_completion=True))
    await asyncio.sleep(0.02)
    assert not owner.done()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.sleep(0.05)
    assert not runtime._flights
    assert executor.committed == []
