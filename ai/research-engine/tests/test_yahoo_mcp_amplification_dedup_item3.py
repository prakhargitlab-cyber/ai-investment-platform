"""Stage2 Final Fix -- Item 3: Yahoo MCP request amplification dedup.

Runtime evidence: 939 requests / 530 failures in one observation window,
with repeated identical symbol+tool calls (some get_financials
symbol/tool combinations called ~8 times). McpFirstResearchCapabilityExecutor
now reuses the same acquisition_context concept already used for direct
yfinance reuse (app/structured_market.py): asyncio.current_task() identifies
"this candidate's current investigate() cycle" (capability groups run
sequentially within one task -- see app/deep_investigation.py), and a
per-context, per-(instrument, requirement_id) slot avoids repeating an
identical Yahoo MCP call within that one cycle, while never caching across
cycles/candidates (a new task always starts with an empty slot) and never
permanently caching a failure.
"""
from __future__ import annotations

import asyncio

import pytest

from app.yahoo_mcp_acquisition import (
    ExternalMcpAcquisitionError,
    McpFirstResearchCapabilityExecutor,
)

from tests.test_yahoo_mcp_acquisition import (
    INSTRUMENT_ID,
    FakeGateway,
    FakeLegacy,
    FakeRepository,
    result,
    target,
)


@pytest.mark.asyncio
async def test_same_cycle_reuses_one_gateway_call_for_identical_requirement() -> None:
    repository, gateway, legacy = FakeRepository(), FakeGateway(), FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    # Two independent requirement-group calls in the SAME candidate/cycle
    # (same task) both asking for LATEST_PRICE -- exactly the overlapping-
    # requirement-group pattern already fixed for yfinance in Item 2.
    first = await executor.execute_primary(
        INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
        correlation_id="cycle-1", identity_headers={},
    )
    second = await executor.execute_primary(
        INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
        correlation_id="cycle-1", identity_headers={},
    )
    assert len(gateway.calls) == 1
    assert first.satisfied_requirement_ids == ("LATEST_PRICE",)
    assert second.satisfied_requirement_ids == ("LATEST_PRICE",)


@pytest.mark.asyncio
async def test_different_cycle_task_is_independent() -> None:
    repository, gateway, legacy = FakeRepository(), FakeGateway(), FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    async def run():
        return await executor.execute_primary(
            INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
            correlation_id="separate-candidate", identity_headers={},
        )

    # Two separate asyncio tasks -- a different candidate, or this same
    # instrument's own next refresh cycle -- each must independently call
    # the gateway; no cross-task reuse.
    await asyncio.create_task(run())
    await asyncio.create_task(run())
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_same_cycle_failure_is_reused_and_bounded_not_retried() -> None:
    repository = FakeRepository()
    gateway = FakeGateway(ExternalMcpAcquisitionError("DOWNSTREAM_TIMEOUT"))
    legacy = FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    first = await executor.execute_primary(
        INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
        correlation_id="cycle-2", identity_headers={},
    )
    second = await executor.execute_primary(
        INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
        correlation_id="cycle-2", identity_headers={},
    )
    # Only one real gateway call for this exact (instrument, requirement) in
    # this one cycle, even though two overlapping calls asked for it; the
    # second reuses the bounded negative outcome instead of an immediate
    # second retry against the provider that just failed.
    assert len(gateway.calls) == 1
    assert first.failures["LATEST_PRICE"] == "DOWNSTREAM_TIMEOUT"
    assert second.failures["LATEST_PRICE"] == "DOWNSTREAM_TIMEOUT"


@pytest.mark.asyncio
async def test_new_cycle_after_failure_is_retryable() -> None:
    repository = FakeRepository()
    gateway = FakeGateway(ExternalMcpAcquisitionError("DOWNSTREAM_TIMEOUT"))
    legacy = FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    async def run():
        return await executor.execute_primary(
            INSTRUMENT_ID, (target(),), jurisdiction="INDIA",
            correlation_id="retry", identity_headers={},
        )

    await asyncio.create_task(run())
    assert len(gateway.calls) == 1
    # A new candidate/cycle (new task) gets its own fresh attempt, not the
    # stale negative result from the previous cycle.
    await asyncio.create_task(run())
    assert len(gateway.calls) == 2
