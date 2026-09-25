"""Offline lifetime/scheduling regressions; no providers or production database."""
import asyncio
import logging
import weakref
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.global_scanner import GlobalScanner, GlobalPreScore
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from test_global_opportunity_baseline import setup_acquisition
from test_global_opportunity_ranker import inputs
from test_global_scanner import instrument, NOW


class Payload:
    def __init__(self, live):
        self.data = bytearray(64 * 1024)
        live.add(self)


@pytest.mark.asyncio
@pytest.mark.parametrize("survivors", [True, False])
async def test_full_universe_streams_evidence_and_releases_baseline(monkeypatch, caplog, survivors):
    service, _, pairs, store, tracker = setup_acquisition(monkeypatch, count=1)
    # Sequential deep-payload contract (exactly one live deep payload). Bounded
    # look-ahead (research_stage2_concurrency > 1) is covered by
    # tests/test_slice7_stage2_concurrency.py with an O(concurrency) bound.
    service.repository.settings.research_stage2_concurrency = 1
    key = UUID(int=1)
    template = GlobalPreScore().score(
        instrument(1), store.load_structured_market_snapshots({key}),
        store.load_financial_facts({key}), store.load_market_price_observations({key}), as_of=NOW)
    rows = [instrument(n) for n in range(1, 2579)]
    for n in range(1, 8):
        pairs[UUID(int=n)] = inputs(n, core=70+n)
    baseline_live, scan_live, deep_live = weakref.WeakSet(), weakref.WeakSet(), weakref.WeakSet()
    peaks = {"baseline": 0, "scan": 0, "tasks": 0, "deep": 0}
    baseline_done = 0
    scan_calls = 0
    starting_tasks = len(asyncio.all_tasks())

    async def ensure(key, **kwargs):
        nonlocal baseline_done
        if kwargs.get("requirement_ids") is None:
            peaks["deep"] += 1
            assert len(baseline_live) == 0
            assert not deep_live  # no previous deep payload, including failed gates
            payload = Payload(deep_live)
            await asyncio.sleep(0)
            result = await tracker(key, **kwargs)
            return SimpleNamespace(readiness=result.readiness, failures=result.failures, payload=payload)
        payload = Payload(baseline_live)
        peaks["baseline"] = max(peaks["baseline"], len(baseline_live))
        peaks["tasks"] = max(peaks["tasks"], len(asyncio.all_tasks()) - starting_tasks)
        await asyncio.sleep(0)
        baseline_done += 1
        return SimpleNamespace(planned_requirement_ids=("QUARTERLY_FINANCIALS",), readiness=payload)

    def snapshots(ids):
        nonlocal scan_calls
        assert len(ids) == 1  # before materializing any heavyweight evidence
        assert len(scan_live) == 0  # previous instrument's evidence is collectible
        if baseline_done:
            assert baseline_done == 2578
            assert len(baseline_live) == 0  # nothing retained across the transition
            assert len(asyncio.all_tasks()) == starting_tasks
        scan_calls += 1
        payload = Payload(scan_live)
        peaks["scan"] = max(peaks["scan"], len(scan_live))
        return [SimpleNamespace(instrument_id=next(iter(ids)), payload=payload)]

    def score(self, row, snapshots, facts, prices, *, as_of):
        assert len(snapshots) == 1
        key = UUID(row["globalInstrumentId"])
        return template.model_copy(update={
            "global_instrument_id": key, "pre_score": 100-key.int/10000,
            "eligible_for_deep_analysis": survivors, "eligible_for_acquisition": True})

    # Analyst reads are also single-instrument and compact; this test supplies
    # no analyst evidence. The real scanner path itself is not mocked.
    async def analysts(ids):
        assert len(ids) == 1
        return {}

    service.readiness.ensure = ensure
    monkeypatch.setattr(store, "load_structured_market_snapshots", snapshots)
    monkeypatch.setattr(store, "load_financial_facts", lambda ids: [])
    monkeypatch.setattr(store, "load_market_price_observations", lambda ids: [])
    monkeypatch.setattr(service.repository, "structured_market_snapshots_for_instruments", analysts)
    monkeypatch.setattr(GlobalPreScore, "score", score)
    with caplog.at_level(logging.INFO, logger="app.global_scanner"):
        result = await service.run(rows, as_of=NOW, shortlist_limit=7, top_n=3)
    assert baseline_done == 2578
    assert scan_calls == 2 * 2578  # all instruments, both scans, no sampling
    assert peaks["baseline"] <= service._BASELINE_CONCURRENCY
    assert peaks["tasks"] <= service._BASELINE_CONCURRENCY
    assert peaks["scan"] == 1
    assert not baseline_live and not scan_live and not deep_live
    assert result.universe_count == 2578
    assert result.baseline_ready_count == 2578
    assert result.shortlist_count == (7 if survivors else 0)
    assert peaks["deep"] == (7 if survivors else 0)
    assert [entry.global_instrument_id for entry in result.top_n] == (
        [UUID(int=n) for n in (7, 6, 5)] if survivors else [])
    assert "stage2_progress: completed=2578/2578 failed=0 active=0/1" in caplog.text


@pytest.mark.asyncio
async def test_shortlist_and_ranking_equal_previous_batch_evaluation(monkeypatch):
    service, rows, _, _, _ = setup_acquisition(monkeypatch, count=32)
    bounded = await service.run(rows, as_of=NOW, shortlist_limit=9, top_n=4)
    original_init = GlobalScanner.__init__

    def previous_batch_init(self, *args, **kwargs):
        kwargs["batch_size"] = 250
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(GlobalScanner, "__init__", previous_batch_init)
    previous = await service.run(rows, as_of=NOW, shortlist_limit=9, top_n=4)
    assert bounded.model_dump() == previous.model_dump()


@pytest.mark.asyncio
async def test_enrichment_releases_histories_and_matches_batch_algorithm(monkeypatch):
    service, rows, _, store, _ = setup_acquisition(monkeypatch, count=20)
    # setup_acquisition stubs enrichment; restore the implementation for this test.
    monkeypatch.undo()
    universe = SimpleNamespace(active_global_equities=None)
    async def active(**kwargs):
        return rows
    universe.active_global_equities = active
    scanner = GlobalScanner(universe, store)
    scan = await scanner.scan(as_of=NOW, top_n=0)
    expected = scanner._enrich_candidate_batch(scan)
    original = store.load_market_price_observations
    live = weakref.WeakValueDictionary()
    reads = []

    def histories(ids):
        assert not live
        reads.append(len(ids))
        values = original(ids)
        for value in values:
            live[id(value)] = value
        return values

    monkeypatch.setattr(store, "load_market_price_observations", histories)
    actual = scanner.enrich_candidates(scan)
    assert actual == expected
    assert reads == [1] * 20
    assert not live


@pytest.mark.asyncio
async def test_individual_deep_failure_isolated(monkeypatch):
    service, rows, pairs, _, _ = setup_acquisition(monkeypatch, count=5)
    async def analyze(profile, readiness, **kwargs):
        if profile.instrument_id == UUID(int=2):
            raise ValueError("private evidence must not escape")
        return pairs[profile.instrument_id][1]
    service.rule_engine.analyze = analyze
    result = await service.run(rows, as_of=NOW, shortlist_limit=5, top_n=5)
    assert result.rule_exception_count == 1
    assert result.rule_analyzed_count == 4
    assert len(result.top_n) == 4
    assert any(d.global_instrument_id == UUID(int=2) and d.disposition == "RULE_ENGINE_EXCEPTION"
               for d in result.diagnostics)


@pytest.mark.asyncio
async def test_scan_failure_propagates_to_existing_cycle_failure_boundary(monkeypatch, caplog):
    service, rows, _, store, tracker = setup_acquisition(monkeypatch, count=2)
    original = store.load_financial_facts
    def broken(ids):
        if tracker.baseline_calls:
            raise RuntimeError("synthetic storage failure")
        return original(ids)
    monkeypatch.setattr(store, "load_financial_facts", broken)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="synthetic storage failure"):
        await service.run(rows, as_of=NOW)
    assert "stage2_failed: completed=0/2 failed=1" in caplog.text
