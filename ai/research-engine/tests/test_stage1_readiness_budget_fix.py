"""Regression coverage for the GLOBAL OPPORTUNITY STAGE-1 READINESS TIMEOUT
DIAGNOSIS + NARROW FIX task, and its STAGE-1 DEADLINE SEMANTICS follow-up.

Two independent root causes were found and fixed in the original task:

1. DIAGNOSTIC BUG (research_readiness.py / research_readiness_runtime.py):
   the post-timeout "what still needs acquisition" check used an inline
   duplicate status set that omitted READY_STALE, while the refresh
   planner's own target-selection set already included it. A requirement
   that timed out while READY_STALE was therefore silently dropped from
   `requirements=` in the research_readiness_ensure_timeout log and from
   result.failures, even though it never actually got re-acquired -- the
   exact mechanism that can produce `requirements=[]` after a full-budget
   timeout. Fixed with one shared REQUIREMENT_STATUSES_NEEDING_ACQUISITION
   constant, used by both the planner and the runtime.

2. SEQUENTIAL BUDGET EXHAUSTION (yahoo_mcp_acquisition.py): the Yahoo
   MCP-first executor attempts each Stage-1 target sequentially, each
   round trip bounded only by its own gateway timeout, uncoordinated with
   the shared 25s ensure() budget. A slow earlier target can starve a
   later one (structurally always SECTOR_MACRO last).

FOLLOW-UP (this file's newer tests): the first fix for #2 skipped a target
outright whenever less than the FULL normal gateway timeout (~10s)
remained, even if the remaining budget (e.g. 9s) was easily enough for a
request that normally completes in 1-2s. This was flagged as overly
conservative. Investigation confirmed the concern: the outer
asyncio.timeout(ensure_timeout_seconds) wrapped around the whole plan
already guarantees the shared 25s budget is never exceeded on its own --
it does not, by itself, need every individual call to be pre-gated by the
full gateway timeout. The fix replaces the hard gate with an effective
per-call timeout capped to whatever of the shared budget actually remains
(minus a small fixed margin for this target's own post-response work),
so a request starts whenever *meaningful* positive budget remains, and is
skipped up front only when even that capped timeout would be too small to
be worth attempting.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.research_readiness import (
    REQUIREMENT_STATUSES_NEEDING_ACQUISITION,
    ResearchReadinessService,
    ResearchRefreshPlanner,
    ResearchRequirementStatus,
)
from app.research_readiness_runtime import ResearchReadinessRuntime
from app.yahoo_mcp_acquisition import (
    McpFirstResearchCapabilityExecutor,
    _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS,
)

import test_research_readiness_runtime as rrt
import test_yahoo_mcp_acquisition as yma


# ---------------------------------------------------------------------------
# Root cause #1: diagnostic status-set dedup (READY_STALE must count as
# "still needs acquisition" everywhere, not just in the refresh planner).
# Unaffected by the deadline-semantics follow-up below; re-verified here to
# prove correctness requirement E ("preserve honest timeout diagnostics").
# ---------------------------------------------------------------------------

def test_requirement_statuses_needing_acquisition_includes_ready_stale_and_is_shared():
    """The single source of truth must include READY_STALE, and both the
    planner (which decides what to re-acquire) and the runtime (which
    decides what remains unresolved, including after a timeout) must
    consult that exact same set -- not independent, driftable copies."""
    assert ResearchRequirementStatus.READY_STALE in REQUIREMENT_STATUSES_NEEDING_ACQUISITION
    assert REQUIREMENT_STATUSES_NEEDING_ACQUISITION == {
        ResearchRequirementStatus.READY_STALE,
        ResearchRequirementStatus.PARTIAL,
        ResearchRequirementStatus.MISSING,
        ResearchRequirementStatus.CONFLICTING,
        ResearchRequirementStatus.FAILED,
    }
    assert ResearchRefreshPlanner._TARGET_STATUSES is REQUIREMENT_STATUSES_NEEDING_ACQUISITION
    assert ResearchReadinessRuntime._requires_acquisition(ResearchRequirementStatus.READY_STALE) is True


@pytest.mark.asyncio
async def test_timeout_diagnostic_reports_ready_stale_requirement_not_silently_dropped(monkeypatch, caplog):
    """[E] Integration proof the requirements=[] bug stays fixed. CURRENT_NEWS
    evidence is present but forced stale; the executor never returns within
    the tiny ensure budget, so ensure() times out. Both result.failures and
    the research_readiness_ensure_timeout log must still correctly report
    the stale, never-refreshed requirement -- not silently drop it."""
    monkeypatch.setattr(
        ResearchReadinessService,
        "_required_inputs_are_fresh",
        staticmethod(lambda *args, **kwargs: False),
    )
    source = rrt.StateDataSource(set())
    executor = rrt.BudgetExecutor(source)
    runtime = ResearchReadinessRuntime(
        rrt.RuntimeRepository(), source, executor, ensure_timeout_seconds=0.02
    )

    with caplog.at_level(logging.WARNING, logger="app.research_readiness_runtime"):
        result = await runtime.ensure(
            rrt.INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
        )

    assert result.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_STALE
    assert result.failures == {"CURRENT_NEWS": "EXTERNAL_RESULT_INCOMPLETE|ACQUISITION_TIMEOUT"}

    timeout_logs = [
        record.getMessage()
        for record in caplog.records
        if "research_readiness_ensure_timeout" in record.getMessage()
    ]
    assert len(timeout_logs) == 1, timeout_logs
    assert "requirements=['CURRENT_NEWS']" in timeout_logs[0], timeout_logs[0]


# ---------------------------------------------------------------------------
# Root cause #2 + deadline-semantics follow-up: effective per-call timeout
# capped to the remaining shared budget, in the sequential Yahoo MCP-first
# executor loop.
# ---------------------------------------------------------------------------

class RoutingGateway:
    """FakeGateway variant that returns a per-requirement result (so two
    different targets in one plan don't collide on a single canned
    response), can optionally delay before returning (to simulate one
    target's acquisition consuming most of the shared budget before a
    later target's turn), and records whatever `timeout_seconds` override
    execute_primary passed it -- so tests can assert on the exact
    effective timeout computed, not just whether a call happened."""

    def __init__(self, delays: dict | None = None, timeout_seconds: float = 10.0):
        self.calls = []
        self.delays = delays or {}
        # Mirrors HttpExternalResearchToolGateway's own attribute, which
        # execute_primary reads via getattr(...) as the normal (ceiling)
        # per-call timeout.
        self.timeout_seconds = timeout_seconds

    async def acquire_requirement(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        requirement_id = kwargs["requirement_id"]
        delay = self.delays.get(requirement_id, 0)
        if delay:
            await asyncio.sleep(delay)
        return yma.result(requirement=requirement_id)


@pytest.mark.asyncio
async def test_execute_primary_skips_target_when_deadline_already_past():
    """[C, part 1] A target reached after the shared deadline has already
    passed is skipped up front -- no Yahoo MCP network call attempted --
    rather than being started. The existing (unchanged) fallback design
    still hands it to the legacy executor."""
    repository, gateway, legacy = yma.FakeRepository(), RoutingGateway(), yma.FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()

    outcome = await executor.execute_primary(
        yma.INSTRUMENT_ID,
        (yma.target("LATEST_PRICE"),),
        jurisdiction="INDIA",
        correlation_id="deadline-already-past",
        identity_headers={},
        deadline=loop.time() - 1.0,
    )

    assert gateway.calls == []
    assert outcome.failures == {"LATEST_PRICE": "BUDGET_EXHAUSTED"}
    assert len(legacy.calls) == 1  # unchanged fallback path still tried


@pytest.mark.asyncio
async def test_execute_primary_skips_when_remaining_budget_is_near_zero_but_positive():
    """[C, part 2] A small *positive* amount of remaining budget -- not
    merely a past deadline -- that is still too small to fit even this
    target's own post-response work is also correctly treated as
    exhausted: zero provider call, not an attempt with a near-zero
    timeout."""
    repository, gateway, legacy = yma.FakeRepository(), RoutingGateway(), yma.FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()

    outcome = await executor.execute_primary(
        yma.INSTRUMENT_ID,
        (yma.target("LATEST_PRICE"),),
        jurisdiction="INDIA",
        correlation_id="near-zero-remaining",
        identity_headers={},
        deadline=loop.time() + 0.2,  # positive, but below the safety margin
    )

    assert gateway.calls == []
    assert outcome.failures == {"LATEST_PRICE": "BUDGET_EXHAUSTED"}


@pytest.mark.asyncio
async def test_effective_timeout_allows_fast_acquisition_below_full_gateway_timeout():
    """[A] The exact scenario raised in review: ensure budget=25s, 16s
    already elapsed, ~9s remaining, gateway timeout configured at 10s. The
    old rule (skip unless remaining > full gateway timeout) would have
    skipped this target even though it normally completes in 1-2s. The
    fixed rule must attempt it."""
    repository, gateway, legacy = yma.FakeRepository(), RoutingGateway(timeout_seconds=10.0), yma.FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()

    outcome = await executor.execute_primary(
        yma.INSTRUMENT_ID,
        (yma.target("LATEST_PRICE"),),
        jurisdiction="INDIA",
        correlation_id="fast-close-call",
        identity_headers={},
        deadline=loop.time() + 9.0,  # remaining < normal_timeout_seconds (10s)
    )

    assert len(gateway.calls) == 1
    assert outcome.satisfied_requirement_ids == ("LATEST_PRICE",)
    assert "BUDGET_EXHAUSTED" not in outcome.failures.values()


@pytest.mark.asyncio
async def test_effective_timeout_is_capped_to_remaining_budget_not_full_gateway_timeout():
    """[B] The per-call timeout actually handed to the gateway must be
    capped to (remaining budget - safety margin), strictly less than the
    normal/full gateway timeout when the remaining budget is the binding
    constraint -- proving the outer ensure() deadline, not the gateway's
    own configured timeout, is what is authoritative here."""
    repository, gateway, legacy = yma.FakeRepository(), RoutingGateway(timeout_seconds=10.0), yma.FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()
    before = loop.time()

    await executor.execute_primary(
        yma.INSTRUMENT_ID,
        (yma.target("LATEST_PRICE"),),
        jurisdiction="INDIA",
        correlation_id="capped-timeout",
        identity_headers={},
        deadline=before + 9.0,
    )

    assert len(gateway.calls) == 1
    recorded_timeout = gateway.calls[0][1]["timeout_seconds"]
    assert recorded_timeout is not None
    assert recorded_timeout < 10.0  # never the full normal gateway timeout
    # ~9s remaining minus the fixed safety margin, allowing for the small
    # amount of real wall-clock time that elapsed while computing it.
    expected = 9.0 - _RESPONSE_PROCESSING_SAFETY_MARGIN_SECONDS
    assert expected - 0.2 <= recorded_timeout <= expected


@pytest.mark.asyncio
async def test_execute_primary_attempts_all_targets_when_budget_is_healthy():
    """[D] Backward compatibility: with a generous deadline (or none at
    all, matching every pre-fix call site), every target is still
    attempted exactly as before."""
    repository, gateway, legacy = yma.FakeRepository(), RoutingGateway(), yma.FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()
    targets = (yma.target("LATEST_PRICE"), yma.target("SECTOR_MACRO"))

    outcome = await executor.execute_primary(
        yma.INSTRUMENT_ID, targets,
        jurisdiction="INDIA", correlation_id="healthy-budget", identity_headers={},
        deadline=loop.time() + 30.0,
    )

    assert {call[1]["requirement_id"] for call in gateway.calls} == {"LATEST_PRICE", "SECTOR_MACRO"}
    assert outcome.satisfied_requirement_ids == ("LATEST_PRICE", "SECTOR_MACRO")
    assert "BUDGET_EXHAUSTED" not in outcome.failures.values()
    assert legacy.calls == []

    # And with no deadline at all (every existing call site before this fix):
    repository2, gateway2, legacy2 = yma.FakeRepository(), RoutingGateway(), yma.FakeLegacy()
    executor2 = McpFirstResearchCapabilityExecutor(legacy2, repository2, gateway2, enabled=True)
    outcome2 = await executor2.execute_primary(
        yma.INSTRUMENT_ID, targets,
        jurisdiction="INDIA", correlation_id="no-deadline", identity_headers={},
    )
    assert len(gateway2.calls) == 2
    assert outcome2.satisfied_requirement_ids == ("LATEST_PRICE", "SECTOR_MACRO")
    assert gateway2.calls[0][1]["timeout_seconds"] is None
    assert gateway2.calls[1][1]["timeout_seconds"] is None


@pytest.mark.asyncio
async def test_execute_primary_skips_later_target_after_earlier_one_consumes_shared_budget():
    """[F] The core sequential-budget-exhaustion mechanism, still
    reproduced under the new effective-timeout rule: an earlier target's
    slow acquisition eats enough of the shared deadline that a later
    target's remaining budget drops below what is worth attempting, so it
    is skipped before a doomed network call -- and the existing
    (unchanged) fallback design still handles it exactly as any other
    unresolved target."""
    repository, legacy = yma.FakeRepository(), yma.FakeLegacy()
    gateway = RoutingGateway(delays={"LATEST_PRICE": 0.7}, timeout_seconds=5.0)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    loop = asyncio.get_event_loop()

    outcome = await executor.execute_primary(
        yma.INSTRUMENT_ID,
        (yma.target("LATEST_PRICE"), yma.target("SECTOR_MACRO")),
        jurisdiction="INDIA",
        correlation_id="starved-by-earlier-target",
        identity_headers={},
        deadline=loop.time() + 1.4,
    )

    assert len(gateway.calls) == 1, gateway.calls
    assert gateway.calls[0][1]["requirement_id"] == "LATEST_PRICE"
    assert outcome.satisfied_requirement_ids == ("LATEST_PRICE",)
    assert outcome.failures.get("SECTOR_MACRO") == "BUDGET_EXHAUSTED"
    assert len(legacy.calls) == 1  # unchanged fallback path still tried for SECTOR_MACRO
    # The completed target's acquisition genuinely persisted -- the
    # budget-aware skip only affected the target that would have been
    # starved.
    assert repository.structured
    assert repository.prices
