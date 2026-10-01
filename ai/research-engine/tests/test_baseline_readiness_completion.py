"""Readiness completion follows committed requested evidence, not a provider tail."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.readiness_signals import evidence_committed
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    CapabilityExecutionResult, ExistingResearchCapabilityExecutor, ResearchReadinessRuntime,
)
from app.settings import Settings
from app.structured_market import StructuredProviderError
from test_research_readiness_runtime import INSTRUMENT_ID, RuntimeRepository, StateDataSource, _profile


class RefreshingSource(StateDataSource):
    def __init__(self, missing):
        super().__init__(missing)
        self.refreshing = frozenset()
        self.rechecked = asyncio.Event()

    def load_by_global_instrument_id(self, instrument_id, requirements):
        snapshot = super().load_by_global_instrument_id(instrument_id, requirements)
        if self.refreshing:
            self.rechecked.set()
        return replace(snapshot, refreshing_requirement_ids=self.refreshing)

    def mark_refreshing(self, instrument_id, requirement_ids):
        self.refreshing = frozenset(requirement_ids)

    def finish_refresh(self, instrument_id, failures=None):
        self.refreshing = frozenset()
        super().finish_refresh(instrument_id, failures)


class SignallingExecutor:
    def __init__(self, source):
        self.source = source
        self.started = asyncio.Event()
        self.commit = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.failure = None
        self.calls = 0
        self.progress = None

    async def execute_primary(self, instrument_id, targets, *, progress, **kwargs):
        self.calls += 1
        self.progress = progress
        progress.executed("STRUCTURED_MARKET")
        self.started.set()
        try:
            await self.commit.wait()
            evidence_committed(instrument_id)
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure:
            progress.failed("LATEST_PRICE", self.failure)
        return CapabilityExecutionResult(("STRUCTURED_MARKET",), {"LATEST_PRICE": self.failure} if self.failure else {})

    async def execute_approved_fallbacks(self, *args):
        return CapabilityExecutionResult()


def start(runtime, requirements=("LATEST_PRICE",), **kwargs):
    return asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=requirements, **kwargs,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_mode", [False, True])
async def test_empty_requirements_return_while_structured_market_still_in_flight(completion_mode, caplog):
    source = RefreshingSource({"LATEST_PRICE"})
    executor = SignallingExecutor(source)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)
    owner = start(runtime, wait_for_completion=completion_mode)
    try:
        await asyncio.wait_for(executor.started.wait(), 1)
        source.missing.clear()
        executor.commit.set()
        result = await asyncio.wait_for(asyncio.shield(owner), 1)
        assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
        assert result.failures == {}
        assert result.executed_capabilities == ("STRUCTURED_MARKET",)
        assert executor.progress.satisfied_requirement_ids == set()
        assert executor.cancelled and not executor.release.is_set()
        assert INSTRUMENT_ID not in runtime._flights
        assert "research_readiness_ensure_timeout" not in caplog.text
        assert runtime.ensure_timeout_seconds == 25.0
    finally:
        executor.release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_requirement_remains_despite_commit_and_refreshing_overlay():
    source = RefreshingSource({"LATEST_PRICE"})
    executor = SignallingExecutor(source)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)
    owner = start(runtime)
    try:
        await asyncio.wait_for(executor.started.wait(), 1)
        executor.commit.set()  # A commit that supplies no qualifying price.
        await asyncio.wait_for(source.rechecked.wait(), 1)
        assert not owner.done()
        assert not executor.cancelled
        source.missing.clear()
        executor.release.set()  # Normal provider completion also re-evaluates.
        result = await asyncio.wait_for(asyncio.shield(owner), 1)
        assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
        assert not executor.cancelled
    finally:
        executor.release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_satisfied_price_does_not_release_a_second_requested_requirement():
    source = RefreshingSource({"LATEST_PRICE", "SECTOR_MACRO"})
    executor = SignallingExecutor(source)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)
    owner = start(runtime, ("LATEST_PRICE", "SECTOR_MACRO"))
    try:
        await asyncio.wait_for(executor.started.wait(), 1)
        source.missing.discard("LATEST_PRICE")
        executor.commit.set()
        await asyncio.wait_for(source.rechecked.wait(), 1)
        assert not owner.done()
        source.missing.clear()
        executor.release.set()
        result = await asyncio.wait_for(asyncio.shield(owner), 1)
        assert not result.failures
        assert result.readiness.for_requirement("SECTOR_MACRO").status == ResearchRequirementStatus.READY_FRESH
    finally:
        executor.release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_failure_after_unsatisfying_commit_is_preserved():
    source = RefreshingSource({"LATEST_PRICE"})
    executor = SignallingExecutor(source)
    executor.failure = "STRUCTURED_PROVIDER_UNAVAILABLE"
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)
    owner = start(runtime)
    try:
        await asyncio.wait_for(executor.started.wait(), 1)
        executor.commit.set()
        await asyncio.wait_for(source.rechecked.wait(), 1)
        executor.release.set()
        result = await asyncio.wait_for(asyncio.shield(owner), 1)
        assert result.failures == {"LATEST_PRICE": executor.failure}
        assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.FAILED
        assert not executor.cancelled
    finally:
        executor.release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_real_structured_commit_boundary_wakes_readiness_or_preserves_failure(provider_failure):
    source = RefreshingSource({"LATEST_PRICE"})
    repository = RuntimeRepository()
    repository.profile = lambda _: _profile()
    repository.structured_market_snapshots_for_instruments = AsyncMock(return_value={})
    # The runtime accesses session metadata too; keep this test's data source
    # independent and provide market data at the orchestrator boundary below.
    repository.record_structured_market_failure_async = AsyncMock()
    tail_started = asyncio.Event()
    tail_cancelled = asyncio.Event()
    async def statement_tail(*args):
        tail_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tail_cancelled.set()
    repository.persist_yahoo_statement_facts_async = statement_tail
    provider = SimpleNamespace(provider_name="fixture", collect=AsyncMock(
        side_effect=StructuredProviderError("NETWORK_TIMEOUT") if provider_failure else None,
        return_value=object(),
    ))
    orchestrator = PortfolioResearchOrchestrator(repository, Settings(structured_provider_enabled=True), structured_provider=provider)
    async def persist(*args):
        source.missing.clear()
    orchestrator._persist_structured_snapshot = persist
    async def ensure(instrument_id, requested_classes, **kwargs):
        return await orchestrator._reconcile_structured_market(
            instrument_id, {"assetType": "EQUITY", "mic": "XNSE"},
            records=[], market_data=({}, {}), requested_classes=requested_classes, force_requested=True,
        )
    orchestrator.ensure_structured_market = ensure
    executor = ExistingResearchCapabilityExecutor(repository, orchestrator, SimpleNamespace())
    runtime = ResearchReadinessRuntime(repository, source, executor)
    try:
        result = await asyncio.wait_for(runtime.ensure(INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["LATEST_PRICE"]), 1)
        provider.collect.assert_awaited_once()
        if provider_failure:
            assert result.failures == {"LATEST_PRICE": "NETWORK_TIMEOUT"}
            assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.FAILED
            repository.record_structured_market_failure_async.assert_awaited_once()
            assert not tail_started.is_set()
        else:
            assert not result.failures
            assert result.readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
            assert tail_started.is_set() and tail_cancelled.is_set()
    finally:
        await orchestrator._client.aclose()


@pytest.mark.asyncio
async def test_single_flight_followers_share_early_completion_without_duplicate_provider():
    # This adapter deliberately exposes durable state while another owner is
    # refreshing, so both callers join the same pending acquisition.
    source = StateDataSource({"LATEST_PRICE"})
    executor = SignallingExecutor(source)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)
    owner = start(runtime)
    follower = None
    try:
        await asyncio.wait_for(executor.started.wait(), 1)
        follower = start(runtime)
        await asyncio.sleep(0)
        source.missing.clear()
        executor.commit.set()
        first, second = await asyncio.wait_for(asyncio.gather(owner, follower), 1)
        assert executor.calls == 1
        assert not first.reused_single_flight and second.reused_single_flight
        assert not first.failures and not second.failures
    finally:
        executor.release.set()
        tasks = [task for task in (owner, follower) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
