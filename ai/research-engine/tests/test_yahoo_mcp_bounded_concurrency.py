"""Regression coverage for bounded concurrency in
McpFirstResearchCapabilityExecutor.execute_primary (YAHOO_FINANCE_MCP
sequential-call latency investigation, continuation).

LATEST_PRICE, HISTORICAL_PRICE_SERIES, VALUATION_INPUTS, and SECTOR_MACRO
were traced to be independent per instrument within one execute_primary
call (see app/yahoo_mcp_acquisition.py's _CONCURRENT_ELIGIBLE_REQUIREMENTS
docstring) and are now dispatched concurrently -- but only when `deadline`
is None (the background-cycle-owner path; BASELINE's own call), never when
a real interactive deadline is in play (see
test_stage1_readiness_budget_fix.py's own sequential-budget-consumption
contract, which must stay exactly as it was). These tests prove the actual
overlap, that it stays bounded to exactly the four eligible requirements,
that mixed success/timeout/failure/identity-rejection outcomes classify
correctly despite running concurrently, and that cancelling the owning
call cancels every in-flight sibling.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import CapabilityExecutionResult
from app.yahoo_mcp_acquisition import (
    ExternalMcpAcquisitionError,
    HttpExternalResearchToolGateway,
    McpFirstResearchCapabilityExecutor,
    _CONCURRENT_ELIGIBLE_REQUIREMENTS,
)
from test_yahoo_mcp_acquisition import FakeRepository, FakeLegacy, INSTRUMENT_ID, NOW, profile, result, target


class TimedGateway:
    """Records call/start/end monotonic times per requirement_id, sleeps a
    configurable delay, raises a configured outcome (if an Exception) or
    returns a configured/default success result, and tracks which calls
    were cancelled mid-flight."""

    def __init__(self, delays=None, outcomes=None):
        self.delays = delays or {}
        self.outcomes = outcomes or {}
        self.calls: list[str] = []
        self.starts: dict[str, float] = {}
        self.ends: dict[str, float] = {}
        self.cancelled: set[str] = set()

    async def acquire_requirement(self, profile_arg, *, region, requirement_id, authorization,
                                   request_id, timeout_seconds=None, financial_gap_fill=False,
                                   timing_hook=None):
        self.calls.append(requirement_id)
        self.starts[requirement_id] = time.monotonic()
        try:
            await asyncio.sleep(self.delays.get(requirement_id, 0.02))
        except asyncio.CancelledError:
            self.cancelled.add(requirement_id)
            raise
        self.ends[requirement_id] = time.monotonic()
        outcome = self.outcomes.get(requirement_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else result(requirement_id)


def _executor(gateway, repository=None, legacy=None):
    return McpFirstResearchCapabilityExecutor(
        legacy or FakeLegacy(), repository or FakeRepository(), gateway, enabled=True,
    )


def _targets(*requirement_ids):
    return tuple(target(requirement=r) for r in requirement_ids)


@pytest.mark.asyncio
async def test_four_independent_requirements_overlap_in_wall_clock_time():
    """The background-owner call pattern (deadline=None) must genuinely run
    the four eligible requirements concurrently, not just avoid crashing:
    every call's start must precede every other call's end."""
    gateway = TimedGateway(delays={r: 0.03 for r in _CONCURRENT_ELIGIBLE_REQUIREMENTS})
    executor = _executor(gateway)
    requirement_ids = sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)
    await executor.execute_primary(
        INSTRUMENT_ID, _targets(*requirement_ids),
        jurisdiction="INDIA", correlation_id="overlap-check", identity_headers=None,
        deadline=None,
    )
    assert sorted(gateway.calls) == requirement_ids
    latest_start = max(gateway.starts.values())
    earliest_end = min(gateway.ends.values())
    # If these ran strictly sequentially, the last call's start would be
    # AFTER every earlier call's end. Concurrent dispatch means the last
    # call starts before the first call has even finished.
    assert latest_start < earliest_end, (gateway.starts, gateway.ends)


@pytest.mark.asyncio
async def test_concurrency_does_not_extend_beyond_the_four_eligible_requirements():
    """A fifth, non-eligible MCP-first requirement (CURRENT_NEWS), listed
    BEFORE the eligible group, must still run through the ordinary,
    strictly sequential inline-await path: it must fully finish before any
    eligible target's task is even created, let alone started -- proving
    concurrency is bounded to exactly the four named requirements and does
    not silently widen to whatever else reaches this loop."""
    gateway = TimedGateway(delays={**{r: 0.03 for r in _CONCURRENT_ELIGIBLE_REQUIREMENTS}, "CURRENT_NEWS": 0.01})
    executor = _executor(gateway)
    eligible = sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)
    await executor.execute_primary(
        INSTRUMENT_ID, _targets("CURRENT_NEWS", *eligible),
        jurisdiction="INDIA", correlation_id="bounded-check", identity_headers=None,
        deadline=None,
    )
    assert gateway.calls[0] == "CURRENT_NEWS"
    # CURRENT_NEWS is awaited inline, synchronously, before the loop even
    # reaches (let alone creates a task for) any eligible target -- so it
    # must be fully finished before any of them starts.
    assert gateway.ends["CURRENT_NEWS"] <= min(gateway.starts[r] for r in eligible)
    # The eligible group still genuinely overlaps with each other.
    assert max(gateway.starts[r] for r in eligible) < min(gateway.ends[r] for r in eligible)


@pytest.mark.asyncio
async def test_mixed_outcomes_classify_correctly_despite_concurrent_dispatch():
    """Success, timeout, generic failure, and identity-rejection outcomes
    for four concurrently-dispatched targets must each land in the right
    bucket -- concurrency must not blur which target got which result."""
    outcomes = {
        "LATEST_PRICE": None,  # success
        "HISTORICAL_PRICE_SERIES": ExternalMcpAcquisitionError("DOWNSTREAM_TIMEOUT"),
        "VALUATION_INPUTS": ExternalMcpAcquisitionError("EXTERNAL_PROVIDER_UNAVAILABLE"),
        "SECTOR_MACRO": ExternalMcpAcquisitionError("EXTERNAL_IDENTITY_CONFLICT"),
    }
    gateway = TimedGateway(delays={r: 0.01 for r in outcomes}, outcomes=outcomes)
    executor = _executor(gateway)
    capability_result = await executor.execute_primary(
        INSTRUMENT_ID, _targets(*outcomes),
        jurisdiction="INDIA", correlation_id="mixed-outcomes", identity_headers=None,
        deadline=None,
    )
    assert "LATEST_PRICE" not in capability_result.failures
    assert capability_result.failures["HISTORICAL_PRICE_SERIES"] == "DOWNSTREAM_TIMEOUT"
    assert capability_result.failures["VALUATION_INPUTS"] == "EXTERNAL_PROVIDER_UNAVAILABLE"
    assert capability_result.failures["SECTOR_MACRO"] == "EXTERNAL_IDENTITY_CONFLICT"


@pytest.mark.asyncio
async def test_cancelling_the_owning_call_cancels_every_in_flight_sibling():
    """Cancelling the coroutine that owns execute_primary (mirroring
    ResearchReadinessRuntime._execute_plan_bounded's cancel-and-drain) must
    cancel every concurrently-dispatched eligible target, not leave some
    running -- preserving the existing cancellation/drain guarantee."""
    gateway = TimedGateway(delays={r: 5.0 for r in _CONCURRENT_ELIGIBLE_REQUIREMENTS})
    executor = _executor(gateway)
    owner = asyncio.create_task(executor.execute_primary(
        INSTRUMENT_ID, _targets(*_CONCURRENT_ELIGIBLE_REQUIREMENTS),
        jurisdiction="INDIA", correlation_id="cancel-check", identity_headers=None,
        deadline=None,
    ))
    await asyncio.sleep(0.05)
    assert sorted(gateway.calls) == sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.sleep(0.02)
    assert gateway.cancelled == set(_CONCURRENT_ELIGIBLE_REQUIREMENTS)


@pytest.mark.asyncio
async def test_interactive_deadline_disables_concurrency_for_the_same_requirements():
    """The exact same four requirements, called with a real (non-None)
    interactive deadline, must NOT overlap -- concurrency is gated to
    deadline is None (see test_stage1_readiness_budget_fix.py's sequential
    shared-budget-consumption contract, which this must not disturb)."""
    gateway = TimedGateway(delays={r: 0.02 for r in _CONCURRENT_ELIGIBLE_REQUIREMENTS})
    executor = _executor(gateway)
    requirement_ids = sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)
    deadline = asyncio.get_event_loop().time() + 5.0
    await executor.execute_primary(
        INSTRUMENT_ID, _targets(*requirement_ids),
        jurisdiction="INDIA", correlation_id="interactive-no-concurrency", identity_headers=None,
        deadline=deadline,
    )
    assert sorted(gateway.calls) == requirement_ids
    latest_start = max(gateway.starts.values())
    earliest_end = min(gateway.ends.values())
    # Strictly sequential: the last call cannot start before the first has
    # already finished.
    assert latest_start >= earliest_end, (gateway.starts, gateway.ends)


@pytest.mark.asyncio
@respx.mock
async def test_global_gateway_request_concurrency_is_bounded_process_wide():
    """HttpExternalResearchToolGateway is the ONE shared/singleton instance
    for the whole process (constructed once in main.py). BASELINE's own
    intra-instrument concurrency (this file's other tests) can now put up
    to 4 requests for a single instrument in flight at once, and
    _BASELINE_CONCURRENCY=8 instrument-workers run concurrently -- an
    unbounded worst case of 8*4=32 simultaneous requests against the
    downstream mcp-gateway service (4x the previously-tolerated ceiling of
    8). research_mcp_gateway_max_concurrency bounds the gateway's own
    actual in-flight request count process-wide, independent of which
    instrument-worker or per-instrument task issued each one. This test
    drives 20 concurrent acquire_requirement() calls (more than both the
    old 8-worker ceiling and the configured limit) against a single
    gateway configured with max_concurrent_requests=5, and proves the
    observed in-flight count never exceeds 5."""
    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def _responder(request):
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.03)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json={
            "ok": True,
            "data": result("LATEST_PRICE").model_dump(mode="json", by_alias=True),
        })

    respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(side_effect=_responder)
    gateway = HttpExternalResearchToolGateway(
        "http://mcp-gateway", 5.0, "research-engine", max_concurrent_requests=5,
    )
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    calls = [
        gateway.acquire_requirement(
            profile(), region="INDIA", requirement_id="LATEST_PRICE",
            authorization=grant, request_id=f"global-concurrency-{i}",
        )
        for i in range(20)
    ]
    await asyncio.gather(*calls)
    assert peak_in_flight <= 5, peak_in_flight
    # And it did genuinely achieve concurrency up to that cap -- not
    # accidentally serialized down to 1 by some other lock.
    assert peak_in_flight == 5, peak_in_flight


@pytest.mark.asyncio
async def test_sibling_readiness_recheck_error_does_not_corrupt_other_targets():
    """Items 3+4 of the global-concurrency review: an unexpected exception
    raised while re-verifying ONE eligible requirement against an active
    deep-investigation repair budget (_recheck_against_active_repair_
    budget) must not escape execute_primary (asyncio.gather does not
    cancel siblings just because one raised -- only cancelling the
    *awaiter* does), and must not prevent the OTHER concurrently-dispatched
    requirements -- whose own rechecks succeed -- from being correctly
    marked completed. The failing one is left unclassified (neither
    completed nor a hard failure) rather than wrongly marked done, so its
    already-persisted evidence is not silently lost either way."""

    class RecheckRepository:
        def __init__(self, fail_on_call):
            self._profile = profile()
            self.fail_on_call = fail_on_call
            self.calls = 0

        def profile(self, _instrument_id):
            return self._profile

        async def _run_blocking_persistence(self, _fn, *_args, **_kwargs):
            self.calls += 1
            if self.calls == self.fail_on_call:
                raise RuntimeError("boom-readiness-recheck")

            class _Status:
                status = ResearchRequirementStatus.READY_FRESH

            class _Readiness:
                def for_requirement(self, _requirement_id):
                    return _Status()

            return _Readiness()

        async def persist_structured_market_snapshot_async(self, _record):
            pass

        async def upsert_market_price_observation_async(self, _observation):
            pass

        async def persist_international_financial_facts_async(self, facts):
            return len(facts)

        async def persist_external_mcp_evidence_async(self, _document, _event):
            pass

        async def persist_external_mcp_shareholding_async(self, _snapshot):
            return True

    repository = RecheckRepository(fail_on_call=2)
    gateway = TimedGateway(delays={r: 0.01 for r in _CONCURRENT_ELIGIBLE_REQUIREMENTS})
    executor = _executor(gateway, repository=repository)
    budget = RequirementAcquisitionBudget(
        instrument_id=INSTRUMENT_ID, requirement_id="LATEST_PRICE",
        as_of=NOW, sufficient=lambda: _async_false(),
    )
    token = _scope.set(budget)
    try:
        capability_result = await executor.execute_primary(
            INSTRUMENT_ID, _targets(*sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)),
            jurisdiction="INDIA", correlation_id="sibling-error-check", identity_headers=None,
            deadline=None,
        )
    finally:
        _scope.reset(token)

    # All four gateway calls actually ran (the recheck failure happens
    # AFTER a successful acquire+persist, never short-circuiting it).
    assert sorted(gateway.calls) == sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)
    # Exactly one recheck failed, and it is excluded from BOTH
    # satisfied_requirement_ids and failures -- left unclassified, not
    # wrongly marked done and not corrupting the aggregate result.
    assert len(capability_result.satisfied_requirement_ids) == 3
    assert capability_result.failures == {}
    satisfied = set(capability_result.satisfied_requirement_ids)
    unclassified = _CONCURRENT_ELIGIBLE_REQUIREMENTS - satisfied
    assert len(unclassified) == 1


async def _async_false():
    return False


@pytest.mark.asyncio
async def test_simulated_baseline_throughput_improves_under_concurrent_dispatch():
    """Section 10 deterministic performance-regression scenario: simulate
    several BASELINE instrument-workers, each needing the four eligible
    requirements, dispatched against the SAME shared, globally-bounded
    HttpExternalResearchToolGateway (max_concurrent_requests=8, matching
    _BASELINE_CONCURRENCY). Compares simulated total wall time for the
    corrected concurrent-eligible dispatch (deadline=None) against a
    strictly sequential baseline (deadline=<real value>, which this review
    must NOT touch -- see test_interactive_deadline_disables_concurrency_
    for_the_same_requirements). This does not claim the historic ~20-minute
    full-universe figure; it only proves, with controlled mocked delays and
    call counts (no network), that the corrected path does meaningfully
    less total wall-clock work than fully sequential per-instrument
    dispatch for the same requirement set and the same global concurrency
    cap."""
    import respx

    call_count = {"n": 0}

    async def _responder(request):
        call_count["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={
            "ok": True, "data": result("LATEST_PRICE").model_dump(mode="json", by_alias=True),
        })

    instrument_count = 3
    eligible = sorted(_CONCURRENT_ELIGIBLE_REQUIREMENTS)

    with respx.mock:
        respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(side_effect=_responder)
        gateway = HttpExternalResearchToolGateway(
            "http://mcp-gateway", 5.0, "research-engine", max_concurrent_requests=8,
        )
        # Pre-warm the lazily-created httpx.AsyncClient BEFORE the timed
        # section. httpx.AsyncClient() construction (SSL context / certifi
        # loading) takes ~0.5s and is serialized by
        # HttpExternalResearchToolGateway._client_lock, so without this warm-up
        # the first wave of concurrent calls is blocked on client creation --
        # an environment/transport cost unrelated to the concurrent-dispatch
        # behavior this test measures. In production the client is a
        # process-wide singleton warmed on first request; we mirror that
        # here so the elapsed assertion isolates the dispatch logic itself.
        await gateway._get_client()
        executors = [_executor(gateway) for _ in range(instrument_count)]
        started = asyncio.get_event_loop().time()
        await asyncio.gather(*(
            executor.execute_primary(
                INSTRUMENT_ID, _targets(*eligible),
                jurisdiction="INDIA", correlation_id=f"perf-check-{i}", identity_headers=None,
                deadline=None,
            )
            for i, executor in enumerate(executors)
        ))
        concurrent_elapsed = asyncio.get_event_loop().time() - started

    assert call_count["n"] == instrument_count * len(eligible)
    # 12 total calls, 8-slot global cap, 0.05s each -> 2 "waves" -> ~0.1s,
    # nowhere near 12 * 0.05s = 0.6s fully sequential.
    assert concurrent_elapsed < 0.3, concurrent_elapsed

    # Strictly sequential comparison point: same work, same gateway cap,
    # but deadline is a real (non-None) value, which this review must
    # leave disabling concurrency exactly as before.
    call_count["n"] = 0
    with respx.mock:
        respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(side_effect=_responder)
        gateway_sequential = HttpExternalResearchToolGateway(
            "http://mcp-gateway", 5.0, "research-engine", max_concurrent_requests=8,
        )
        # Pre-warm the httpx.AsyncClient so the one-time SSL-context
        # creation cost (~0.5s) does not pollute the sequential baseline
        # measurement (same rationale as the concurrent section above).
        await gateway_sequential._get_client()
        executors = [_executor(gateway_sequential) for _ in range(instrument_count)]
        deadline = asyncio.get_event_loop().time() + 5.0
        started = asyncio.get_event_loop().time()
        for i, executor in enumerate(executors):
            await executor.execute_primary(
                INSTRUMENT_ID, _targets(*eligible),
                jurisdiction="INDIA", correlation_id=f"perf-sequential-{i}", identity_headers=None,
                deadline=deadline,
            )
        sequential_elapsed = asyncio.get_event_loop().time() - started

    assert call_count["n"] == instrument_count * len(eligible)
    # Sequential: 12 calls * 0.05s each, strictly one after another.
    assert sequential_elapsed >= 0.5, sequential_elapsed
    # The corrected concurrent path does the same total work in a small
    # fraction of the strictly-sequential wall time.
    assert concurrent_elapsed < sequential_elapsed / 2, (concurrent_elapsed, sequential_elapsed)
