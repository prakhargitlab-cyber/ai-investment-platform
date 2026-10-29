"""History dispatch metadata cannot certify a successful completed attempt."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.deep_investigation import investigate
from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason
from app.research_readiness_runtime import ResearchReadinessRuntime, TargetedEnsureResult
from test_research_readiness_runtime import (
    INSTRUMENT_ID, RuntimeRepository, StateDataSource, UpdatingExecutor,
)


HISTORY = "HISTORICAL_PRICE_SERIES"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,planned,dispatched,current_failure,expected", [
    ("prior-timeout", False, False, None, "ACQUISITION_BACKOFF|DOWNSTREAM_TIMEOUT"),
    # Completion is known inside this test double, but no trustworthy completion
    # receipt reaches investigate() through the existing result contract.
    ("completed-empty", True, True, None, "ACQUISITION_BACKOFF|DOWNSTREAM_TIMEOUT"),
    ("dispatched-only", True, True, None, "ACQUISITION_BACKOFF|DOWNSTREAM_TIMEOUT"),
    ("current-timeout", True, True, "ACQUISITION_TIMEOUT", "ACQUISITION_TIMEOUT"),
    ("skipped", True, True, "ACQUISITION_NOT_DUE", "ACQUISITION_NOT_DUE"),
    ("skipped-without-outcome", True, False, None, "ACQUISITION_BACKOFF|DOWNSTREAM_TIMEOUT"),
    ("failed", True, True, "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE", "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE"),
    ("cancelled-outcome", True, True, "CANCELLED_BY_OWNER", "CANCELLED_BY_OWNER"),
])
async def test_unproven_history_completion_preserves_current_or_prior_failure(
    scenario, planned, dispatched, current_failure, expected,
):
    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, instrument_id):
            return [{"requirement_id": HISTORY, "provider": "YAHOO_FINANCE_MCP", "outcome": "FAILED",
                     "observed_at": datetime(2026, 10, 16, 19, 53, 38, tzinfo=timezone.utc).isoformat(),
                     "failure_reason": "DOWNSTREAM_TIMEOUT"}]

    source = StateDataSource({HISTORY})
    runtime = ResearchReadinessRuntime(Repo(), source, UpdatingExecutor(source, update_primary=False))
    attempts = []
    provider = AsyncMock(return_value=0)

    async def ensure(instrument_id, **kwargs):
        readiness = await runtime.read(instrument_id, jurisdiction="INDIA")
        if kwargs["requirement_ids"] != (HISTORY,):
            return TargetedEnsureResult(readiness, (), ())
        attempts.append(kwargs["requirement_ids"])
        if scenario == "completed-empty":
            assert await provider() == 0
        return TargetedEnsureResult(readiness, (HISTORY,) if planned else (),
                                    ("HISTORICAL_MARKET_DATA",) if dispatched else (),
                                    failures={HISTORY: current_failure} if current_failure else {})

    runtime.ensure = ensure
    # A technical prior timeout or empty response must not permanently stop a
    # later explicit investigation, and neither attempt makes readiness fresh.
    for _ in range(2):
        result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
        assert result.failures[HISTORY] == expected
        assert classify_reason(expected) == TECHNICAL_RETRYABLE
        assert result.readiness.for_requirement(HISTORY).missing_reason
    assert len(attempts) == 2
    assert provider.await_count == (2 if scenario == "completed-empty" else 0)


@pytest.mark.asyncio
async def test_history_cancellation_propagates_without_incomplete_classification():
    source = StateDataSource({HISTORY})
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, UpdatingExecutor(source, update_primary=False))

    async def ensure(instrument_id, **kwargs):
        if kwargs["requirement_ids"] == (HISTORY,):
            raise asyncio.CancelledError()
        return TargetedEnsureResult(await runtime.read(instrument_id, jurisdiction="INDIA"), (), ())

    runtime.ensure = ensure
    with pytest.raises(asyncio.CancelledError):
        await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
