"""Guardian Review Slice 6 -- timing instrumentation inside runtime.ensure().

Proves the fix for the proven instrumentation gap: the real stage2 timing
report showed almost all elapsed time attributed only to the coarse
runtime_ensure_elapsed_ms, with every fine-grained sub-metric reading 0.0 for
the slowest operations. Tracing the code confirmed why: two real network call
sites -- app.structured_market.YahooFinanceProvider._resolve_verified_nse_candidate
(MAPPING_RESOLUTION, the exact call Slice 2's global Yahoo-mapping fallback
depends on) and app.source_discovery.OfficialFilingDiscovery._announcement_rows
(DISCOVERY, the exact NSE-endpoint call Slice 8 requires stays otherwise
unchanged) -- use their own raw httpx clients entirely outside
app.research_fetching's already-instrumented fetch path, so neither one had
ANY cycle_timing attribution before this fix. The initial runtime.read() at
the top of app.deep_investigation.investigate() (READINESS_LOAD) and the
evidence_only reload inside _requirement_sufficient (FINAL_READINESS_RELOAD)
were similarly unattributed.

This does not claim full coverage of every stage the Guardian Review named
(PERSISTENCE_EXECUTION/PARSER/etc. already existed under other field names;
PROVIDER_NETWORK/DOCUMENT_DOWNLOAD remain merged into the existing
provider_elapsed_ms/network_elapsed_ms fields) -- seeing REMAINING RISKS in
the final report for what is still outstanding.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.cycle_timing import (
    CycleTimingRecorder,
    cycle_scope,
    record_discovery_elapsed,
    record_final_readiness_reload_elapsed,
    record_mapping_resolution_elapsed,
    record_readiness_load_elapsed,
    track_requirement,
)
from app.settings import Settings
from app.source_discovery import OfficialFilingDiscovery
from app.structured_market import StructuredProviderError, YahooFinanceProvider


def _profile_dict(instrument_id="fixture-instrument"):
    return {"instrumentId": instrument_id, "isin": "INE251B01027", "tradingCurrency": "INR", "currency": "INR"}


# 1 -- the four new record_* helpers populate their own fields and the
# matching cycle-level aggregate, and are additive (not overwriting) -------
def test_new_record_helpers_populate_fields_and_cycle_aggregates():
    recorder = CycleTimingRecorder("cycle-1")
    with cycle_scope(recorder):
        with track_requirement("cand-1", "HISTORICAL_PRICE_SERIES") as span:
            record_mapping_resolution_elapsed(12.5)
            record_mapping_resolution_elapsed(3.5)
            record_discovery_elapsed(7.0)
            record_readiness_load_elapsed(4.0)
            record_final_readiness_reload_elapsed(2.0)
        assert span.mapping_resolution_elapsed_ms == 16.0
        assert span.discovery_elapsed_ms == 7.0
        assert span.readiness_load_elapsed_ms == 4.0
        assert span.final_readiness_reload_elapsed_ms == 2.0
    report = recorder.report()
    assert report["aggregate_discovery_ms"] == 7.0
    assert report["aggregate_readiness_load_ms"] == 4.0
    assert report["aggregate_final_readiness_reload_ms"] == 2.0


# 2 -- OTHER_UNATTRIBUTED is the elapsed time not explained by any leaf
# sub-metric, clamped at 0, and None until the span completes --------------
def test_other_unattributed_is_elapsed_minus_attributed_leaves():
    recorder = CycleTimingRecorder("cycle-2")
    with cycle_scope(recorder):
        with track_requirement("cand-2", "GROWTH_FACTS") as span:
            assert span.other_unattributed_ms is None  # not completed yet
            record_mapping_resolution_elapsed(10.0)
            record_discovery_elapsed(5.0)
    # completed: elapsed_ms is now set by complete_requirement()
    assert span.elapsed_ms is not None
    assert span.other_unattributed_ms == round(max(0.0, span.elapsed_ms - 15.0), 3)
    assert span.other_unattributed_ms >= 0.0


# 3 -- silent no-ops with no active recorder, exactly like every existing
# record_* helper (mirrors test_04 in test_cycle_timing_instrumentation.py)
def test_new_helpers_are_silent_no_ops_with_no_active_recorder():
    record_mapping_resolution_elapsed(999.0)
    record_discovery_elapsed(999.0)
    record_readiness_load_elapsed(999.0)
    record_final_readiness_reload_elapsed(999.0)  # must not raise


# 4 -- integration: a real NSE discovery network call, under an active span,
# is attributed to discovery_elapsed_ms/aggregate_discovery_ms; a cache hit
# for the SAME symbol on a second call contributes nothing further --------
@pytest.mark.asyncio
async def test_real_discovery_network_call_is_attributed_end_to_end():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[])

    recorder = CycleTimingRecorder("cycle-3")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = OfficialFilingDiscovery(client=client)
        with cycle_scope(recorder):
            with track_requirement("cand-3", "QUARTERLY_FINANCIALS") as span:
                await discovery._announcement_rows("RELIANCE")
            assert span.discovery_elapsed_ms > 0.0
            after_first = span.discovery_elapsed_ms
            with track_requirement("cand-3", "QUARTERLY_FINANCIALS"):
                await discovery._announcement_rows("RELIANCE")  # TTL cache hit
    assert span.discovery_elapsed_ms == after_first  # unchanged: no new network work
    assert len(calls) == 1
    assert recorder.report()["aggregate_discovery_ms"] > 0.0


# 5 -- integration: a real Yahoo mapping-resolution network call, under an
# active span, is attributed to mapping_resolution_elapsed_ms regardless of
# whether the call ultimately succeeds or is rejected ----------------------
@pytest.mark.asyncio
async def test_real_mapping_resolution_network_call_is_attributed_even_on_rejection():
    def handler(request):
        return httpx.Response(200, json={"quotes": [{
            "symbol": "OTHERCO.NS", "quoteType": "EQUITY", "longname": "Unrelated Company",
            "exchange": "NSI", "currency": "INR",
        }]})

    recorder = CycleTimingRecorder("cycle-4")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = YahooFinanceProvider(Settings(), client)
        with cycle_scope(recorder):
            with track_requirement("cand-4", "HISTORICAL_PRICE_SERIES") as span:
                with pytest.raises(StructuredProviderError):
                    await provider._resolve_verified_nse_candidate(
                        _profile_dict(), "Zen Technologies Limited", "ZENTEC.NS",
                    )
            assert span.mapping_resolution_elapsed_ms > 0.0
    assert recorder.report()["aggregate_mapping_resolution_ms"] > 0.0


# 6 -- integration: investigate()'s own initial readiness load is attributed
# even though it runs before any per-requirement span exists yet -----------
@pytest.mark.asyncio
async def test_investigate_readiness_load_reaches_cycle_aggregate_before_any_span():
    from test_capability_batching import _run
    from app.research_readiness import ResearchRequirementStatus
    from test_stock_rule_engine import _readiness

    recorder = CycleTimingRecorder("cycle-5")
    readiness = _readiness({"GROWTH_FACTS": ResearchRequirementStatus.MISSING})
    with cycle_scope(recorder):
        await _run(readiness, succeeds={"GROWTH_FACTS"})
    report = recorder.report()
    # The initial read genuinely happened (recorded, even if fast) -- proven
    # by it being a finite, non-negative number rather than absent/None.
    assert isinstance(report["aggregate_readiness_load_ms"], float)
    assert report["aggregate_readiness_load_ms"] >= 0.0
