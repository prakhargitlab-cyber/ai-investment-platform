from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.structured_market import StructuredProviderError
from test_baseline_market_reuse import harness
from test_research_readiness_runtime import INSTRUMENT_ID, NOW, _profile, _structured_record


def record(*, price_due=False, valuation_due=False):
    value = _structured_record(_profile())
    old = NOW - timedelta(days=30)
    facts = {key: fact.model_copy(update={"as_of_date": NOW, "retrieved_at": NOW})
             for key, fact in value.snapshot.facts.items()}
    for key in ("latestPrice",) if price_due else ():
        facts[key] = facts[key].model_copy(update={"as_of_date": old, "retrieved_at": old})
    for key in ("trailingPE", "priceToBook") if valuation_due else ():
        facts[key] = facts[key].model_copy(update={"as_of_date": old, "retrieved_at": old})
    return value.model_copy(update={
        "last_price_at": old if price_due else NOW, "last_valuation_at": old if valuation_due else NOW,
        "last_fundamentals_at": NOW, "last_success_at": NOW,
        "snapshot": value.snapshot.model_copy(update={"facts": facts, "market_as_of": old if price_due else NOW,
                                                      "retrieved_at": NOW}),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("price_due,valuation_due", [(False, False), (True, False), (False, True), (True, True)])
async def test_requested_classes_refresh_independently_and_warm_call_reuses(harness, price_due, valuation_due):
    h = harness
    before = record(price_due=price_due, valuation_due=valuation_due)
    h.store.upsert_structured_market_snapshot(before)
    expected = ({"PRICE"} if price_due else set()) | ({"VALUATION"} if valuation_due else set())
    async def collect(instrument, classes):
        assert classes == expected
        fresh = record().snapshot
        keys = ({"latestPrice"} if "PRICE" in classes else set()) | (
            {"trailingPE", "priceToBook"} if "VALUATION" in classes else set())
        return fresh.model_copy(update={"facts": {key: fresh.facts[key] for key in keys}})
    provider = SimpleNamespace(provider_name="NSE_STRUCTURED", collect_classes=AsyncMock(side_effect=collect))
    h.orchestrator.structured_provider = provider
    first = await h.orchestrator.ensure_structured_market(INSTRUMENT_ID, {"PRICE", "VALUATION"}, baseline_only=True)
    assert first.due_classes == expected and first.error is None
    assert provider.collect_classes.await_count == int(bool(expected))
    second = await h.orchestrator.ensure_structured_market(INSTRUMENT_ID, {"PRICE", "VALUATION"}, baseline_only=True)
    assert not second.due_classes and provider.collect_classes.await_count == int(bool(expected))
    after = h.repo.structured_market_snapshots_for({INSTRUMENT_ID})[INSTRUMENT_ID][0]
    for key in ({"latestPrice"} if not price_due else set()) | ({"trailingPE", "priceToBook"} if not valuation_due else set()):
        assert after.snapshot.facts[key] == before.snapshot.facts[key]


@pytest.mark.asyncio
async def test_failure_keeps_stale_class_retryable_without_invalidating_fresh_other_class(harness):
    h = harness
    before = record(valuation_due=True)
    h.store.upsert_structured_market_snapshot(before)
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect_classes=AsyncMock(
        side_effect=StructuredProviderError("UPSTREAM_TIMEOUT")))
    h.orchestrator.structured_provider = provider
    for _ in range(2):
        result = await h.orchestrator.ensure_structured_market(INSTRUMENT_ID, {"PRICE", "VALUATION"}, baseline_only=True)
        assert result.due_classes == {"VALUATION"} and result.error == "UPSTREAM_TIMEOUT"
    assert provider.collect_classes.await_count == 2
    after = h.repo.structured_market_snapshots_for({INSTRUMENT_ID})[INSTRUMENT_ID][0]
    assert after.last_valuation_at == before.last_valuation_at
    assert after.last_price_at == before.last_price_at


@pytest.mark.asyncio
async def test_successful_partial_response_does_not_advance_unreturned_class(harness):
    h = harness
    before = record(valuation_due=True)
    h.store.upsert_structured_market_snapshot(before)
    snapshot = record().snapshot
    h.orchestrator.structured_provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect_classes=AsyncMock(
        return_value=snapshot.model_copy(update={"facts": {"latestPrice": snapshot.facts["latestPrice"]}})))
    for _ in range(2):
        result = await h.orchestrator.ensure_structured_market(INSTRUMENT_ID, {"VALUATION"}, baseline_only=True)
        assert result.due_classes == {"VALUATION"}
    after = h.repo.structured_market_snapshots_for({INSTRUMENT_ID})[INSTRUMENT_ID][0]
    assert after.last_valuation_at == before.last_valuation_at


@pytest.mark.asyncio
async def test_baseline_info_does_not_suppress_missing_statement_fallback(harness):
    h = harness
    h.store.upsert_structured_market_snapshot(record())
    h.provider.collect = AsyncMock(return_value=record().snapshot)
    result = await h.orchestrator.ensure_structured_market(INSTRUMENT_ID, {"FUNDAMENTALS"})
    h.provider.collect.assert_awaited_once()
    assert result.due_classes == {"FUNDAMENTALS"}
