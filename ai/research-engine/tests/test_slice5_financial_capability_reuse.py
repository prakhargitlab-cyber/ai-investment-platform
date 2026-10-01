"""Guardian Review Slice 5 -- financial acquisition capability reuse.

Verifies (rather than re-implements) that GROWTH_FACTS, QUARTERLY_FINANCIALS,
BUSINESS_QUALITY_FACTS and BALANCE_SHEET_FACTS already share ONE physical
filing acquisition when requested together, for a REAL
ExistingResearchCapabilityExecutor (not the fake runtime used by
tests/test_capability_batching.py, which proves the same property one layer
up, at the runtime.ensure()-call level).

Traced root cause of why this already holds: in
app.research_readiness_runtime.ExistingResearchCapabilityExecutor.
execute_primary, ALL FOUR of these requirement_ids fall under
`_FINANCIAL_REQUIREMENTS`, and for INDIA jurisdiction the branch that handles
them unconditionally does exactly one
`repository_categories.add("FINANCIAL_RESULTS")` -- a set, so requesting one
member or all four adds the identical single entry -- followed by exactly
ONE `await self.repository.refresh_targeted_categories(...)` call at the
bottom of the method, regardless of how many of the four were requested.
That single call is itself instrument-level single-flighted
(ResearchRepository._instrument_refresh_flights) and performs one discovery
+ fetch pass for the FINANCIAL_RESULTS category, so no separate physical
document fetch happens per financial requirement.

Per-requirement classification/finalization independence (one requirement's
classification never falsely marking another READY) is proven separately in
tests/test_capability_batching.py's test_c_partial_group_failure_stays_independent
and is not re-proven here.
"""
from __future__ import annotations

import pytest

from app.research_readiness_runtime import ExistingResearchCapabilityExecutor
from test_research_readiness_runtime import (
    INSTRUMENT_ID,
    RecordingOrchestrator,
    RecordingPopulation,
    RecordingTargetRepository,
    _profile,
    _target,
)
from types import SimpleNamespace


def _executor():
    repo = RecordingTargetRepository(_profile())
    orchestrator = RecordingOrchestrator()
    jobs = SimpleNamespace(population=RecordingPopulation(), settings=repo.settings)
    return ExistingResearchCapabilityExecutor(repo, orchestrator, jobs), repo


# 1 -- all four grouped financial requirements together still trigger only
# ONE repository.refresh_targeted_categories call (one physical acquisition)
@pytest.mark.asyncio
async def test_all_four_financial_requirements_together_share_one_physical_call():
    executor, repo = _executor()
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [
            _target("GROWTH_FACTS"),
            _target("QUARTERLY_FINANCIALS"),
            _target("BUSINESS_QUALITY_FACTS"),
            _target("BALANCE_SHEET_FACTS"),
        ],
        jurisdiction="INDIA",
        correlation_id="slice5",
        identity_headers=None,
    )
    assert result.executed_capabilities == ("FINANCIALS",)
    # Exactly one physical refresh_targeted_categories call for all four --
    # not four (one per requirement) and not even two.
    assert len(repo.category_calls) == 1
    assert repo.category_calls == [{"FINANCIAL_RESULTS"}]


# 2 -- a single financial requirement alone triggers the identical single
# category set -- confirming the shared call isn't an artifact of batching
# more requirement_ids in, but the SAME one physical category either way
@pytest.mark.asyncio
async def test_single_financial_requirement_uses_the_same_shared_category():
    executor, repo = _executor()
    await executor.execute_primary(
        INSTRUMENT_ID, [_target("BALANCE_SHEET_FACTS")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    assert repo.category_calls == [{"FINANCIAL_RESULTS"}]
