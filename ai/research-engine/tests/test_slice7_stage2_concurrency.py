"""Slice 7: bounded Stage-2 look-ahead concurrency with backpressure.

Deterministic latency model (asyncio.sleep): most acquisitions are fast, every
7th is slow, one is a transient technical failure. For concurrency 1..4 we
measure elapsed time, peak in-flight acquisitions, live acquisition payloads
(weakref count -- application-owned retained state), provider calls, and
require identical results.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from collections import Counter
from uuid import UUID

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_DEEP
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from test_global_opportunity_acquisition import acquisition_service
from test_global_scanner import NOW, instrument, persisted
from test_stock_rule_engine import _readiness

N = 28
FAST, SLOW = 0.01, 0.12


class SimulatedCrash(BaseException):
    """Process death."""


class Payload:
    """Stands in for a candidate's heavyweight acquisition state."""

    def __init__(self, live):
        self.data = bytearray(32 * 1024)
        live.add(self)


def _service(monkeypatch, concurrency, *, fail_once=(), crash_at=None):
    service, runtime, store = acquisition_service(monkeypatch)
    service.repository.settings.research_stage2_concurrency = concurrency
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)
    stats = {"active": 0, "peak": 0, "calls": Counter(), "live": weakref.WeakSet(), "peak_live": 0}
    for n in range(1, N + 1):
        persisted(store, instrument(n))

    async def ensure(key, **kwargs):
        if kwargs.get("requirement_ids") is not None:
            return TargetedEnsureResult(None, (), ())
        stats["calls"][key] += 1
        if crash_at is not None and key == UUID(int=crash_at) and stats["calls"][key] == 1:
            raise SimulatedCrash()
        stats["active"] += 1
        stats["peak"] = max(stats["peak"], stats["active"])
        payload = Payload(stats["live"])
        stats["peak_live"] = max(stats["peak_live"], len(stats["live"]))
        try:
            await asyncio.sleep(SLOW if key.int % 7 == 0 else FAST)
        finally:
            stats["active"] -= 1
        del payload
        if key.int in fail_once and stats["calls"][key] == 1:
            return TargetedEnsureResult(not_ready, (), (), failures={"VALUATION_INPUTS": "NETWORK_TIMEOUT"})
        return TargetedEnsureResult(_readiness(), (), ())
    runtime.ensure.side_effect = ensure
    return service, store, stats


def _comparable(result):
    data = result.model_dump(mode="json")
    for key in ("generated_at", "stage2_concurrency", "stage2_peak_inflight"):
        data.pop(key, None)
    return data


@pytest.mark.asyncio
async def test_concurrency_1_to_4_benchmark_is_bounded_and_equivalent(monkeypatch, capsys):
    rows = [instrument(n) for n in range(1, N + 1)]
    table, baseline = [], None
    for concurrency in (1, 2, 3, 4):
        service, _, stats = _service(monkeypatch, concurrency, fail_once={5})
        started = time.perf_counter()
        result = await service.run(rows, as_of=NOW, shortlist_limit=N)
        elapsed = time.perf_counter() - started
        assert stats["peak"] <= concurrency, (concurrency, stats["peak"])
        assert stats["peak_live"] <= concurrency
        assert result.stage2_peak_inflight <= concurrency
        assert len(stats["live"]) == 0  # nothing retained after the run
        assert sum(stats["calls"].values()) == N + 1  # N + one bounded repair of #5
        assert len(result.diagnostics) == N and result.rule_analyzed_count == N
        if baseline is None:
            baseline = (elapsed, _comparable(result))
        else:
            assert _comparable(result) == baseline[1]  # identical outcome and ordering
        table.append((concurrency, round(elapsed, 3), stats["peak"], stats["peak_live"], sum(stats["calls"].values())))
    with capsys.disabled():
        print("\nSTAGE2_BENCHMARK concurrency elapsed_s peak_active peak_live_payloads provider_calls")
        for row in table:
            print("STAGE2_BENCHMARK", *row)
    elapsed = {row[0]: row[1] for row in table}
    assert elapsed[2] < elapsed[1] * 0.75
    assert elapsed[4] <= elapsed[2]


@pytest.mark.asyncio
async def test_slow_candidate_does_not_serialize_independent_work(monkeypatch):
    rows = [instrument(n) for n in range(1, 15)]
    timings = {}
    for concurrency in (1, 3):
        service, _, _ = _service(monkeypatch, concurrency)
        started = time.perf_counter()
        await service.run(rows, as_of=NOW, shortlist_limit=14)
        timings[concurrency] = time.perf_counter() - started
    assert timings[3] < timings[1] * 0.7


@pytest.mark.asyncio
async def test_crash_with_lookahead_in_flight_resumes_without_duplicates(monkeypatch):
    rows = [instrument(n) for n in range(1, N + 1)]
    service, store, stats = _service(monkeypatch, 3, crash_at=10)
    store.create_cycle_run("c7", {})
    store.claim_cycle_run("c7", "a")
    with pytest.raises(SimulatedCrash):
        await service.run(rows, as_of=NOW, shortlist_limit=N, checkpoint=await CycleCheckpoint(store, "c7", "a").load())
    progress = store.cycle_progress("c7", PHASE_DEEP)
    in_progress = [k for k, v in progress.items() if v["state"] == CandidateState.IN_PROGRESS]
    assert 1 <= len(in_progress) <= 4 * 3  # bounded by the look-ahead depth (4 x concurrency)
    await asyncio.sleep(0)
    assert len(stats["live"]) == 0  # in-flight acquisitions were cancelled, not orphaned
    completed_before = {k for k, v in progress.items() if v["state"] == CandidateState.COMPLETED}
    calls_before = Counter(stats["calls"])
    store._connection.execute("UPDATE global_opportunity_cycle_run SET owner_id='b' WHERE cycle_id='c7'")
    result = await service.run(rows, as_of=NOW, shortlist_limit=N, checkpoint=await CycleCheckpoint(store, "c7", "b").load())
    resumed = stats["calls"] - calls_before
    assert all(resumed[UUID(k[1])] == 0 for k in completed_before)
    assert len(result.diagnostics) == N and result.rule_analyzed_count == N
    final = store.cycle_progress("c7", PHASE_DEEP)
    assert {v["state"] for v in final.values()} == {CandidateState.COMPLETED}


@pytest.mark.asyncio
async def test_benchmark_with_real_document_ingestion(monkeypatch, capsys):
    """Each acquisition ingests 2 synthetic PDF filings through the production
    prepare -> apply -> persist path (default document-cache bounds)."""
    import gc
    from app.models import DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
    from test_slice1_document_retention import _pdf_body

    count = 40
    rows = [instrument(n) for n in range(1, count + 1)]
    table, baseline = [], None
    for concurrency in (1, 2, 3, 4):
        service, runtime, store = acquisition_service(monkeypatch)
        repo = service.repository
        repo.settings.research_stage2_concurrency = concurrency
        for n in range(1, count + 1):
            persisted(store, instrument(n))
        tracked = {"docs": [], "structures": [], "provider": 0, "discovery": 0, "active": 0, "peak": 0,
                   "peak_structures": 0}

        def live(refs):
            return sum(ref() is not None for ref in refs)

        async def ensure(key, **kwargs):
            if kwargs.get("requirement_ids") is not None:
                return TargetedEnsureResult(None, (), ())
            tracked["provider"] += 1
            tracked["discovery"] += 1
            tracked["active"] += 1
            tracked["peak"] = max(tracked["peak"], tracked["active"])
            try:
                for doc in range(2):
                    await asyncio.sleep(SLOW if key.int % 7 == 0 else FAST)
                    document = repo._prepare_ingested_document(
                        original_url=f"https://nsearchives.nseindia.com/corporate/X{key.int}_{doc}.pdf",
                        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_name="NSE", publisher="NSE",
                        content_type="application/pdf", body=_pdf_body(key.int * 10 + doc, lines=300),
                        reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
                        source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
                        expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
                        trusted_profile_identity=None)
                    document.instrument_id = key
                    tracked["docs"].append(weakref.ref(document))
                    tracked["structures"].append(weakref.ref(document.pdf_structure))
                    await repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED)
                    del document
                    tracked["peak_structures"] = max(tracked["peak_structures"], live(tracked["structures"]))
            finally:
                tracked["active"] -= 1
            return TargetedEnsureResult(_readiness(), (), ())
        runtime.ensure.side_effect = ensure
        started = time.perf_counter()
        result = await service.run(rows, as_of=NOW, shortlist_limit=count)
        elapsed = time.perf_counter() - started
        gc.collect()
        stats = repo.documents.stats()
        cache_limit = repo.settings.research_document_cache_max_documents
        assert tracked["peak"] <= concurrency
        assert stats["evictable_documents"] <= cache_limit and stats["evictable_bytes"] <= repo.settings.research_document_cache_max_bytes
        assert tracked["peak_structures"] <= cache_limit + stats["pinned_documents"] + concurrency
        assert len(result.diagnostics) == count and result.rule_analyzed_count == count
        if baseline is None:
            baseline = _comparable(result)
        else:
            assert _comparable(result) == baseline
        table.append((concurrency, round(elapsed, 3), tracked["peak"], stats["evictable_documents"],
                      live(tracked["structures"]), tracked["peak_structures"], tracked["provider"],
                      tracked["discovery"], result.rule_analyzed_count))
    with capsys.disabled():
        print("\nSTAGE2_DOC_BENCHMARK conc elapsed_s peak_active retained_bodies retained_pdf_structures "
              "peak_pdf_structures provider_calls discovery_calls analyzed")
        for row in table:
            print("STAGE2_DOC_BENCHMARK", *row)
