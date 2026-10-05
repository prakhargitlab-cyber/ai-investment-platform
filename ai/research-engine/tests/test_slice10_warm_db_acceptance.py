"""Slice 10: WARM-DB acceptance -- the same representative workload with durable
fresh evidence already present, plus real ResearchReadinessRuntime planning.
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from app.cycle_checkpoint import PHASE_DEEP
from app.opportunity_worker import OpportunityCycleWorker
from test_slice9_mini_universe_acceptance import COUNT, build_process, case_of, universe  # noqa: F401 (fixture)


async def _cycle(u, monkeypatch):
    repo, runner = build_process(u, monkeypatch)
    worker = OpportunityCycleWorker(repo, runner, lease_seconds=5, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    await asyncio.wait_for(worker.queue.join(), 120)
    await worker.close()
    assert u.store.cycle_run(job["cycle_id"])["status"] == "COMPLETED"
    return job["cycle_id"], repo


@pytest.mark.asyncio
async def test_warm_db_reuses_durable_evidence_documents_and_rule_results(universe, monkeypatch, capsys):
    u = universe
    u.crashed = True  # no crash in this workload
    first_cycle, _ = await _cycle(u, monkeypatch)            # populates the DB (fresh run)
    before = {name: Counter(c) for name, c in u.calls.items()}
    documents_before = u.store._connection.execute("SELECT COUNT(*) FROM research_documents").fetchone()[0]
    second_cycle, repo = await _cycle(u, monkeypatch)        # new pod, warm DB
    delta = {name: u.calls[name] - before[name] for name in u.calls}
    assert second_cycle != first_cycle
    selection = u.store.cycle_published_selection(second_cycle)
    reviewed = len(selection["previous_recommendations"])
    reinvestigated = len(u.store.cycle_run(second_cycle)["selection"]["deep_ids"])
    # Previously recommended instruments are re-scored by the existing review
    # path (no acquisition by design); only unresolved ones are re-investigated.
    assert reviewed == 66 and reinvestigated == COUNT - 66
    # Fresh concepts: zero provider acquisition.
    assert sum(delta["provider"].values()) == 0
    # Completed durable documents: no download, no parse, no new rows.
    assert sum(delta["ingested"].values()) == 0
    assert u.store._connection.execute("SELECT COUNT(*) FROM research_documents").fetchone()[0] == documents_before
    # Unchanged evidence -> durable rule-engine cache, no re-evaluation.
    # Reviewed instruments now take the persisted-only, price-lifecycle review
    # path (never calling rule_engine.analyze -- see
    # global_opportunity_orchestration.py's "Reviews share the same admission
    # boundary as discovery" comment), and the reinvestigated ones reuse their
    # durable rule result upstream of rule_engine.analyze entirely, so neither
    # group calls it in a warm cycle: cache_hits, like evaluated, is zero.
    assert sum(delta["evaluated"].values()) == 0 and sum(delta["cache_hits"].values()) == 0
    # Stage-1 baseline is re-evaluated (cheap readiness read) but acquires nothing.
    assert sum(delta["provider"].values()) == 0
    with capsys.disabled():
        print("\nWARM_DB second cycle: reviewed=%d reinvestigated=%d provider=%d ingested=%d evaluated=%d cache_hits=%d deep=%d" % (
            reviewed, reinvestigated, sum(delta["provider"].values()), sum(delta["ingested"].values()),
            sum(delta["evaluated"].values()), sum(delta["cache_hits"].values()), sum(delta["deep"].values())))


# ---- real readiness runtime planning ------------------------------------------
@pytest.mark.asyncio
async def test_all_fresh_concepts_execute_no_capability():
    from test_research_readiness_runtime import StateDataSource, UpdatingExecutor, _runtime, INSTRUMENT_ID
    source = StateDataSource(set())
    executor = UpdatingExecutor(source)
    result = await _runtime(source, executor).ensure(INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=None)
    assert result.planned_requirement_ids == () and executor.primary_calls == executor.fallback_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [{"LATEST_PRICE"}, {"LATEST_PRICE", "HISTORICAL_PRICE_SERIES"}])
async def test_stale_lightweight_price_does_not_repeat_financial_or_governance_research(stale):
    from test_research_readiness_runtime import StateDataSource, UpdatingExecutor, _runtime, INSTRUMENT_ID
    source = StateDataSource(set(stale))
    executor = UpdatingExecutor(source)
    result = await _runtime(source, executor).ensure(INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=None)
    planned = set(result.planned_requirement_ids)
    executed = set().union(*executor.primary_calls, *executor.fallback_calls) if (executor.primary_calls or executor.fallback_calls) else set()
    expensive = {"QUARTERLY_FINANCIALS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "BUSINESS_QUALITY_FACTS",
                 "GOVERNANCE_HISTORY", "SHAREHOLDING", "CURRENT_NEWS"}
    assert planned <= stale and not (planned & expensive)
    assert not (executed & expensive)
