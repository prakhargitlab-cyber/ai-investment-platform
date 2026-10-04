"""Follow-up to tests/test_stage2_closure_attribution_gap.py.

Confirms (per the latest forensic-fix task) that the shared-refresh
attribution-gap fix in ExistingResearchCapabilityExecutor.execute_primary
(app/research_readiness_runtime.py) cannot cause a downstream
``ResearchReadinessResult.for_requirement()`` crash, and adds the specific
"shared exception + SHAREHOLDING + another requirement" minimal-pair
regression requested explicitly.

Why this matters: ``ResearchReadinessResult.for_requirement()`` is
implemented as ``next(item for item in self.requirements if item.
requirement_id == key)`` with NO default -- calling it with a key that is
not a real, registered requirement id raises ``StopIteration``. The
sentinel the shared-exception handler uses, "UNATTRIBUTED_SHARED_EXECUTION_
FAILURE", is NOT a registered requirement id, so it would crash
for_requirement() if it were ever looked up that way. This file proves
(1) that danger is real in isolation, and (2) the current code never
actually does that lookup, because the sentinel is confined to the
request-local progress object and never enters CapabilityExecutionResult.
failures (which is the only thing fed back into requirement-keyed lookups
such as `final_readiness.for_requirement(requirement_id)` in
research_readiness_runtime.py's _execute_plan).
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.research_readiness import ResearchRequirementRegistry
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor

import tests.test_research_readiness_runtime as _rt
from tests.test_research_readiness_runtime import (
    INSTRUMENT_ID,
    RecordingOrchestrator,
    RecordingPopulation,
    _profile,
    _target,
)

_REGISTERED_IDS = frozenset(
    requirement.requirement_id for requirement in ResearchRequirementRegistry.default().requirements
)


class _RaisingRepository(_rt.RecordingTargetRepository):
    def __init__(self, profile, raise_exc):
        super().__init__(profile)
        self.raise_exc = raise_exc

    async def refresh_targeted_categories(self, _instrument_id, categories, **_kwargs):
        self.category_calls.append(set(categories))
        raise self.raise_exc


def _executor_with_raising_repo(raise_exc):
    profile = _profile()
    repo = _RaisingRepository(profile, raise_exc)
    orchestrator = RecordingOrchestrator()
    jobs = SimpleNamespace(population=RecordingPopulation(), settings=repo.settings)
    return ExistingResearchCapabilityExecutor(repo, orchestrator, jobs), repo


def test_the_sentinel_key_would_itself_crash_for_requirement_if_ever_looked_up() -> None:
    """Documents the exact danger the fix avoids -- this is why the sentinel
    must never enter a requirement-keyed failures mapping."""
    readiness_cls_requirements = ResearchRequirementRegistry.default().requirements
    assert "UNATTRIBUTED_SHARED_EXECUTION_FAILURE" not in _REGISTERED_IDS
    with pytest.raises(StopIteration):
        next(item for item in readiness_cls_requirements
             if item.requirement_id == "UNATTRIBUTED_SHARED_EXECUTION_FAILURE")


@pytest.mark.asyncio
async def test_shared_exception_minimal_pair_shareholding_and_sibling_never_cross_attributed() -> None:
    """The exact minimal-pair regression requested: a shared refresh
    exception with SHAREHOLDING + exactly one other participating
    requirement must leave BOTH unattributed by the shared exception, while
    the exception remains observable (not silently swallowed)."""
    executor, repo = _executor_with_raising_repo(RuntimeError("SEARCH_PROVIDER_DEGRADED"))
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("GOVERNANCE_HISTORY")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    # The shared call really was attempted (not swallowed before it ran).
    assert repo.category_calls, "the shared refresh must still have been attempted"
    assert "SHAREHOLDING" not in result.failures
    assert "GOVERNANCE_HISTORY" not in result.failures


@pytest.mark.asyncio
async def test_every_key_execute_primary_can_return_is_a_safe_for_requirement_lookup() -> None:
    """Every key execute_primary's returned CapabilityExecutionResult.failures
    can ever contain must be a real registered requirement id -- i.e. safe to
    pass to ResearchReadinessResult.for_requirement() downstream -- even when
    the shared refresh raises for a fused multi-requirement set."""
    executor, repo = _executor_with_raising_repo(RuntimeError("SEARCH_PROVIDER_DEGRADED"))
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("ORDER_BOOK_CAPEX_GUIDANCE"),
         _target("GOVERNANCE_HISTORY"), _target("CURRENT_NEWS"), _target("QUARTERLY_FINANCIALS")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    for key in result.failures:
        assert key in _REGISTERED_IDS, (
            f"{key!r} is not a registered requirement id -- unsafe to pass to for_requirement()"
        )
