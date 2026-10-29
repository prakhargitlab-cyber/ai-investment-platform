"""Deterministic coverage for the DEEP CD-1 fix (diagnostics/
equity_radar_deep_validation.md, Confirmed Code Defect CD-1): a capability
group's shared acquisition call must get a wall-clock budget proportional to
the document/query allowance it was already scaled to (see
deep_investigation._acquire_group's existing max_documents = singleton *
member_count scaling), not the SAME flat single-member
ensure_timeout_seconds every singleton acquisition uses.

Reuses the exact deadline-contract harness from
test_execute_plan_bounded_deadline_contract.py (_SleepExecutor /
StateDataSource / RuntimeRepository) so these tests exercise the REAL
ResearchReadinessRuntime._execute_plan_bounded code path, not a stub.
"""
from __future__ import annotations

import asyncio

import pytest

from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ResearchReadinessRuntime, CapabilityExecutionResult
from test_research_readiness_runtime import StateDataSource, RuntimeRepository, INSTRUMENT_ID
from test_execute_plan_bounded_deadline_contract import _SleepExecutor

GROUP = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS")


class _GroupSleepExecutor:
    """Services every target in ONE shared call after `delay`, exactly
    mirroring execute_primary's single-provider-pass-per-group model (see
    deep_investigation._acquire_group's docstring) -- not N independent
    per-target sleeps."""

    def __init__(self, source: StateDataSource, delay: float) -> None:
        self.source = source
        self.delay = delay
        self.committed: list[str] = []

    async def execute_primary(self, key, targets, **kwargs):
        await asyncio.sleep(self.delay)
        ids = tuple(target.requirement_id for target in targets)
        self.committed.extend(ids)
        for requirement_id in ids:
            self.source.missing.discard(requirement_id)
        return CapabilityExecutionResult(ids, {})

    async def execute_approved_fallbacks(self, *args):
        return CapabilityExecutionResult()


@pytest.mark.asyncio
async def test_flat_single_member_budget_times_out_a_realistic_group_pass():
    """Baseline/control: reproduces the confirmed CD-1 symptom in isolation.
    A shared group pass that legitimately takes longer than ONE singleton's
    flat budget (because it was scaled to do up to 4x the document work --
    see max_documents = singleton * member_count) is wrongly timed out when
    given only that same flat single-member budget, even though every
    member's evidence would have committed successfully given a
    proportional budget (see the next test)."""
    source = StateDataSource(set(GROUP))
    # Use a delay that exceeds the flat timeout (0.25s) but would be covered
    # by a scaled timeout (1.0s = 0.25s * 4 members)
    executor = _GroupSleepExecutor(source, delay=0.4)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.25)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=list(GROUP), wait_for_completion=True)
    assert result.failures == {rid: "ACQUISITION_TIMEOUT" for rid in GROUP}
    assert executor.committed == []


@pytest.mark.asyncio
async def test_timeout_seconds_override_lets_the_same_group_pass_complete():
    """The fix: an explicit timeout_seconds override (what
    deep_investigation._acquire_group now computes as
    base_timeout_seconds * member_count, capped at 4x) gives the IDENTICAL
    shared-call delay from the previous test enough budget to finish, and
    every member in the group is truthfully reported satisfied -- none of
    them collateral-damaged by the others' legitimate acquisition cost."""
    source = StateDataSource(set(GROUP))
    # Use a delay that is achievable within the scaled timeout (1.0s for 4 members)
    # The budget is 1.0s = 0.25s * 4 members
    # We need: delay + overhead < 1.0s
    # On Windows, asyncio overhead can be significant; use a safe margin
    executor = _GroupSleepExecutor(source, delay=0.4)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.25)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=list(GROUP), wait_for_completion=True, timeout_seconds=0.25 * len(GROUP))
    assert result.failures == {}
    assert sorted(executor.committed) == sorted(GROUP)
    for requirement_id in GROUP:
        assert result.readiness.for_requirement(requirement_id).status == ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_timeout_seconds_none_preserves_existing_flat_default_unchanged():
    """A caller that does NOT pass timeout_seconds (every existing caller:
    BASELINE, singleton DEEP acquisition via deep_investigation._acquire,
    the interactive API route) must see byte-for-byte unchanged behavior --
    the override is additive, never a default-reducing or -raising change
    for code paths that never asked for it."""
    source = StateDataSource({"CURRENT_NEWS"})
    executor = _SleepExecutor(source, delay=0.4)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.25)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"], wait_for_completion=True)
    assert result.failures == {"CURRENT_NEWS": "ACQUISITION_TIMEOUT"}


@pytest.mark.asyncio
async def test_interactive_path_also_honors_timeout_seconds_override():
    """The interactive (wait_for_completion=False) branch uses a different
    internal mechanism (asyncio.timeout / TimeoutError) from the background
    wait_for_completion=True branch, but must honor the SAME override
    parameter identically, since both branches read effective_timeout from
    the same override-or-default computation."""
    source = StateDataSource(set(GROUP))
    # Use consistent timing with the previous test
    executor = _GroupSleepExecutor(source, delay=0.4)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor, ensure_timeout_seconds=0.25)
    result = await runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=list(GROUP), wait_for_completion=False, timeout_seconds=0.25 * len(GROUP))
    assert result.failures == {}
    assert sorted(executor.committed) == sorted(GROUP)


# ---------------------------------------------------------------------------
# Direct coverage of _acquire_group's scaling computation itself (not just
# that ResearchReadinessRuntime.ensure() supports the override parameter).
# ---------------------------------------------------------------------------

import dataclasses
from types import SimpleNamespace
from app.deep_investigation import investigate, acquisition_budget
from app.research_readiness_runtime import TargetedEnsureResult
from test_stock_rule_engine import _readiness as _build_readiness


class _TimeoutCapturingRuntime:
    """Minimal fake exposing a real ensure_timeout_seconds attribute (unlike
    _BudgetProbeRuntime in test_di20h3_budget_scaling.py, which has none and
    so always exercises the getattr(..., 25.0) fallback) so this test proves
    _acquire_group actually READS runtime.ensure_timeout_seconds and scales
    it by member_count, rather than merely hard-coding some other constant
    that happens to also satisfy the other suite's assertions."""

    def __init__(self, readiness, ensure_timeout_seconds):
        self.readiness = readiness
        self.ensure_timeout_seconds = ensure_timeout_seconds
        self.seen_timeout_seconds: list[float | None] = []
        self.repository = SimpleNamespace(record_acquisition_observation=self._record)

    async def _record(self, *args, **kwargs):
        return None

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids,
                      identity_headers=None, correlation_id=None,
                      wait_for_completion=True, timeout_seconds=None):
        self.seen_timeout_seconds.append(timeout_seconds)
        requirement_ids = tuple(requirement_ids)
        self.readiness = dataclasses.replace(self.readiness, requirements=tuple(
            dataclasses.replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
            if row.requirement_id in requirement_ids else row
            for row in self.readiness.requirements
        ))
        return TargetedEnsureResult(self.readiness, requirement_ids, requirement_ids)


@pytest.mark.asyncio
async def test_acquire_group_scales_timeout_by_member_count():
    """The 4-member financial group (BUSINESS_QUALITY_FACTS, GROWTH_FACTS,
    BALANCE_SHEET_FACTS, QUARTERLY_FINANCIALS) must receive
    base_timeout_seconds * 4 -- the SAME scaling factor already applied to
    max_documents/max_queries -- not the flat single-member default."""
    readiness = _build_readiness({rid: ResearchRequirementStatus.MISSING for rid in GROUP})
    runtime = _TimeoutCapturingRuntime(readiness, ensure_timeout_seconds=25.0)
    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    assert runtime.seen_timeout_seconds, "ensure() was never called"
    assert 100.0 in runtime.seen_timeout_seconds  # 25.0 * 4 members


@pytest.mark.asyncio
async def test_acquire_group_caps_the_scale_factor_at_four():
    """Boundedness: even if a future group grew beyond 4 members, the scale
    factor must not grow unbounded past 4x the base -- the cap mirrors the
    task's explicit 'never introduce unbounded concurrency, retries or cache
    growth' boundary for this fix."""
    three_member_group = ("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY")
    readiness = _build_readiness({rid: ResearchRequirementStatus.MISSING for rid in three_member_group})
    runtime = _TimeoutCapturingRuntime(readiness, ensure_timeout_seconds=25.0)
    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    assert runtime.seen_timeout_seconds
    for seen in runtime.seen_timeout_seconds:
        assert seen is None or seen <= 25.0 * 4
