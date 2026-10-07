"""Slices 2 + 3: durable per-candidate checkpoint and same-cycle recovery.

Deterministic, offline. A "restart" builds a NEW ResearchRepository,
orchestrator and worker (new lease owner) over the SAME SQLite store: only
the database survives, exactly like a pod restart. A crash is a BaseException
(process death) so no `except Exception` boundary can swallow it.

Instrumented call counts (per instrument):
  baseline   -- baseline readiness.ensure (acquisition) calls
  deep       -- deep investigations (deep acquisition entry)
  provider   -- durable-evidence acquisitions (skipped when facts already durable)
  evaluated  -- rule-engine evaluations (cache misses)
  cache_hits -- rule-engine durable-cache hits
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from functools import partial
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import app.deep_investigation as deep_investigation
from app import global_opportunity_cycle as cycle
from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_BASELINE, PHASE_DEEP
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, ProvenancedValue
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_scanner import GlobalScanner
from app.models import SourceMode
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS, TargetedEnsureResult
from app.settings import Settings
from test_global_opportunity_baseline import _make_runtime
from test_global_opportunity_empty_universe import _Clock
from test_global_opportunity_ranker import inputs
from test_global_scanner import NOW, instrument, persisted
from test_stock_rule_engine import _readiness


@pytest.mark.asyncio
async def test_resume_count_is_preclaim_at_enqueue_and_increments_once_on_takeover(caplog):
    from datetime import datetime, timezone
    store = SqliteResearchPersistence()
    cycle_id = 'resume-count-timing'
    store.create_cycle_run(cycle_id, {'top_n': 4})
    assert store.claim_cycle_run(cycle_id, 'dead-owner', lease_seconds=1,
                                now=datetime.now(timezone.utc) - timedelta(seconds=10))
    assert store.cycle_run(cycle_id)['resume_count'] == 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner(**kwargs):
        assert kwargs['cycle_id'] == cycle_id
        entered.set()
        await release.wait()
        return {'cycle_id': cycle_id}

    worker = OpportunityCycleWorker(
        SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()),
        runner, owner_id='replacement-owner')
    caplog.set_level('INFO', logger='app.opportunity_worker')
    worker.start()
    try:
        # start() enqueues synchronously; _run_one has not claimed yet.
        assert store.cycle_run(cycle_id)['resume_count'] == 0
        assert any('opportunity_cycle_resume_enqueued' in r.message and 'resumeCount=0' in r.message
                   for r in caplog.records)
        await asyncio.wait_for(entered.wait(), 5)
        assert store.cycle_run(cycle_id)['resume_count'] == 1
        assert any('opportunity_cycle_claimed' in r.message and 'resumeCount=1' in r.message
                   for r in caplog.records)
        assert store.claim_cycle_run(cycle_id, worker.owner_id)
        assert store.cycle_run(cycle_id)['resume_count'] == 1
        release.set()
        await asyncio.wait_for(worker.queue.join(), 5)
        assert store.cycle_run(cycle_id)['status'] == 'COMPLETED'
        assert store.cycle_run(cycle_id)['resume_count'] == 1
    finally:
        release.set()
        await worker.close()


class SimulatedCrash(BaseException):
    """Process death."""


def _fact(key: UUID) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(key, "revenue", "2026-06-30", "QUARTERLY", "CONSOLIDATED"),
        ProvenancedValue(value=Decimal("100"), unit="INR crore", as_of_date=NOW - timedelta(days=60),
                         source_url="https://nsearchives.nseindia.com/fixture.pdf", source_name="NSE",
                         source_type="EXCHANGE_ANNOUNCEMENT", published_at=NOW - timedelta(days=30),
                         retrieved_at=NOW - timedelta(days=1), confidence=0.98),
        FactSourceTier.OFFICIAL_NSE, "NSE", f"fixture-{key}", SourceMode.REAL)


class World:
    """The durable world (one SQLite DB) plus crash-injection and call counters.

    Counters and crash plan survive 'restarts'; everything else is rebuilt."""

    def __init__(self, count: int) -> None:
        self.store = SqliteResearchPersistence()
        self.rows = [instrument(n) for n in range(1, count + 1)]
        for row in self.rows:
            persisted(self.store, row)
        self.pairs = {UUID(int=n): inputs(n, core=60 + n % 30) for n in range(1, count + 1)}
        self.calls = {name: Counter() for name in ("baseline", "deep", "provider", "evaluated", "cache_hits")}
        self.crash: tuple[str, int] | None = None
        self._seen: dict[str, list] = {}
        self.crashed = False

    def maybe_crash(self, point: str, key) -> None:
        seen = self._seen.setdefault(point, [])
        if key not in seen:
            seen.append(key)
        if not self.crashed and self.crash is not None and self.crash[0] == point and seen.index(key) + 1 == self.crash[1]:
            self.crashed = True
            raise SimulatedCrash(f"{point}#{self.crash[1]}")


def build_process(world: World, monkeypatch):
    """One 'pod': repository + orchestrator + readiness wired to world.store."""
    repo = ResearchRepository(persistence=world.store)
    # Exact crash-position assertions below assume sequential deep processing;
    # look-ahead crash/resume is covered by test_slice7_stage2_concurrency.py.
    repo.settings.research_stage2_concurrency = 1
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(repo, world.store, profile_hydrator=hydrator.register_global_profile_metadata,
                                            readiness_runtime=runtime, clock=lambda: NOW)
    pairs = world.pairs
    monkeypatch.setattr(GlobalScanner, "enrich_candidates", lambda self, scan, **kwargs: [
        pairs[c.global_instrument_id][0] for c in reversed(scan.candidates) if c.eligible_for_deep_analysis])
    service.readiness.read = AsyncMock(return_value=_readiness())

    async def ensure(key, *, requirement_ids=None, **kwargs):
        if requirement_ids is not None and set(requirement_ids) == BASELINE_REQUIREMENT_IDS:
            world.calls["baseline"][key] += 1
            world.maybe_crash("baseline", key)
        return TargetedEnsureResult(_readiness(), (), ())
    service.readiness.ensure = ensure

    async def analyze(profile, readiness, **kwargs):
        key = profile.instrument_id
        rule = pairs[key][1]
        cached = await repo.stock_rule_engine_result(key, rule.rule_engine_version, rule.input_fingerprint)
        if cached is not None:
            world.calls["cache_hits"][key] += 1
            return rule.model_copy(update={"cache_hit": True})
        world.maybe_crash("rule_before_persist", key)
        world.calls["evaluated"][key] += 1
        await repo.persist_stock_rule_engine_result(rule.model_dump(mode="json"))
        world.maybe_crash("rule_after_persist", key)
        return rule
    service.rule_engine.analyze = analyze
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", lambda *a, **kw: service)
    source = SimpleNamespace(active_global_equities=AsyncMock(return_value=world.rows),
                             sector_benchmark_contexts=AsyncMock(return_value={}),
                             register_global_profile_metadata=service.profile_hydrator)
    runner = partial(cycle.run_global_opportunity_cycle, repo, source, readiness_runtime=service.readiness)
    return repo, service, runner


@pytest.fixture
def world_factory(monkeypatch):
    monkeypatch.setattr(cycle, "datetime", _Clock)
    original_investigate = deep_investigation.investigate
    original_start, original_finish = CycleCheckpoint.start, CycleCheckpoint.finish
    worlds: list[World] = []

    async def investigate(runtime, key, **kwargs):
        world = worlds[-1]
        world.calls["deep"][key] += 1
        world.maybe_crash("deep_before_evidence", key)
        if not world.store.load_financial_facts({key}):
            world.calls["provider"][key] += 1
            world.store.reconcile_financial_facts_for_source(key, f"fixture-{key}", [_fact(key)])
        world.maybe_crash("deep_after_evidence_commit", key)
        return await original_investigate(runtime, key, **kwargs)

    async def start(self, phase, instrument_id):
        if phase == PHASE_DEEP:
            worlds[-1].maybe_crash("before_candidate_start", instrument_id)
        return await original_start(self, phase, instrument_id)

    async def finish(self, phase, instrument_id, state, **kwargs):
        if phase == PHASE_DEEP:
            worlds[-1].maybe_crash("before_candidate_checkpoint", instrument_id)
        return await original_finish(self, phase, instrument_id, state, **kwargs)

    monkeypatch.setattr(deep_investigation, "investigate", investigate)
    monkeypatch.setattr(CycleCheckpoint, "start", start)
    monkeypatch.setattr(CycleCheckpoint, "finish", finish)

    def make(count: int) -> World:
        world = World(count)
        worlds.append(world)
        return world
    return make


async def _run_until_crash(world, monkeypatch, *, lease_seconds=0.3):
    repo, _, runner = build_process(world, monkeypatch)
    worker = OpportunityCycleWorker(repo, runner, lease_seconds=lease_seconds, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    with pytest.raises(SimulatedCrash):
        await asyncio.wait_for(worker.task, 60)
    return job, worker


async def _restart_and_finish(world, monkeypatch, *, lease_seconds=0.3):
    await asyncio.sleep(lease_seconds + 0.1)  # the dead owner's lease expires
    repo, _, runner = build_process(world, monkeypatch)
    worker = OpportunityCycleWorker(repo, runner, lease_seconds=lease_seconds, poll_seconds=0.05)
    worker.start()
    try:
        await asyncio.wait_for(worker.queue.join(), 60)
    finally:
        await worker.close()
    return worker


def _job(store, cycle_id):
    return next(j for j in store.opportunity_jobs() if j["cycle_id"] == cycle_id)


def _publications(store, cycle_id):
    rows = store._connection.execute(
        "SELECT payload FROM global_opportunity_top_selection WHERE cycle_id = ?", (cycle_id,)).fetchall()
    return len(rows)


def _assert_no_duplicates(store, cycle_id, count):
    snapshots = store._connection.execute(
        "SELECT global_instrument_id, COUNT(*) FROM global_opportunity_snapshot WHERE cycle_id = ? GROUP BY 1",
        (cycle_id,)).fetchall()
    assert all(n == 1 for _, n in snapshots)
    history = store._connection.execute(
        "SELECT global_instrument_id, COUNT(*) FROM stock_recommendation_history GROUP BY 1").fetchall()
    assert all(n == 1 for _, n in history)
    facts = store._connection.execute(
        "SELECT instrument_id, COUNT(*) FROM global_financial_facts GROUP BY 1").fetchall()
    assert len(facts) == count and all(n == 1 for _, n in facts)
    assert _publications(store, cycle_id) == 1


# ---------------------------------------------------------------- A -------
@pytest.mark.asyncio
async def test_A_candidate_completes_normally_with_durable_checkpoint(world_factory, monkeypatch):
    world = world_factory(12)
    repo, _, runner = build_process(world, monkeypatch)
    worker = OpportunityCycleWorker(repo, runner, lease_seconds=5, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    await asyncio.wait_for(worker.queue.join(), 60)
    await worker.close()
    cycle_id = job["cycle_id"]
    run = world.store.cycle_run(cycle_id)
    assert run["status"] == "COMPLETED" and run["owner_id"] is None
    assert world.store.active_cycle_run() is None
    progress = world.store.cycle_progress(cycle_id)
    deep = {k[1]: v for k, v in progress.items() if k[0] == PHASE_DEEP}
    base = {k[1]: v for k, v in progress.items() if k[0] == PHASE_BASELINE}
    assert len(deep) == len(base) == 12
    assert {v["state"] for v in deep.values()} == {CandidateState.COMPLETED}
    assert {v["disposition"] for v in deep.values()} <= {"ANALYZED", "RANK_FILTERED"}
    assert all(v["attempts"] == 1 for v in deep.values())
    assert all(v["payload"]["rule"] and v["payload"]["enriched"] for v in deep.values())
    finished = _job(world.store, cycle_id)
    assert finished["status"] == "COMPLETED" and finished["result_cycle_id"] == cycle_id
    _assert_no_duplicates(world.store, cycle_id, 12)
    # Idempotent checkpoint write: replaying a final write changes nothing.
    key, row = next(iter(deep.items()))
    world.store._connection.execute("UPDATE global_opportunity_cycle_run SET owner_id='x' WHERE cycle_id=?", (cycle_id,))
    for _ in range(2):
        world.store.record_cycle_progress(cycle_id, "x", PHASE_DEEP, key, CandidateState.COMPLETED,
                                          disposition=row["disposition"], payload=row["payload"])
    again = world.store.cycle_progress(cycle_id)[(PHASE_DEEP, key)]
    assert again["state"] == row["state"] and again["attempts"] == row["attempts"] and again["payload"] == row["payload"]


# ------------------------------------------------------------- B..E -------
@pytest.mark.asyncio
@pytest.mark.parametrize("point, expected_state_after_crash", [
    ("before_candidate_start", None),                         # B
    ("deep_before_evidence", CandidateState.IN_PROGRESS),     # C
    ("deep_after_evidence_commit", CandidateState.IN_PROGRESS),  # D
    ("before_candidate_checkpoint", CandidateState.IN_PROGRESS),  # D (evidence + rule done, checkpoint not)
    ("rule_after_persist", CandidateState.IN_PROGRESS),       # E
])
async def test_B_to_E_crash_points_resume_same_cycle_without_duplicates(world_factory, monkeypatch, point,
                                                                       expected_state_after_crash):
    world = world_factory(10)
    world.crash = (point, 5)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]
    selection = world.store.cycle_run(cycle_id)["selection"]
    order = [UUID(i) for i in selection["deep_ids"]]
    target = order[4]
    row = world.store.cycle_progress(cycle_id).get((PHASE_DEEP, str(target)))
    assert (row["state"] if row else None) == expected_state_after_crash
    completed_before = [k for k in order[:4]]
    assert all(world.store.cycle_progress(cycle_id)[(PHASE_DEEP, str(k))]["state"] == CandidateState.COMPLETED
               for k in completed_before)
    assert _publications(world.store, cycle_id) == 0

    await _restart_and_finish(world, monkeypatch)
    run = world.store.cycle_run(cycle_id)
    assert run["status"] == "COMPLETED" and run["resume_count"] == 1
    assert _job(world.store, cycle_id)["result_cycle_id"] == cycle_id  # SAME cycle
    for key in completed_before:  # not reacquired, not re-evaluated
        assert world.calls["deep"][key] == 1 and world.calls["provider"][key] == 1
        assert world.calls["evaluated"][key] == 1 and world.calls["cache_hits"][key] == 0
    # The interrupted candidate: recovered idempotently.
    assert world.calls["provider"][target] == 1          # durable evidence never reacquired
    assert world.calls["evaluated"][target] == 1         # rule result evaluated at most once
    if point == "rule_after_persist" or point == "before_candidate_checkpoint":
        assert world.calls["cache_hits"][target] == 1    # durable rule result reused
    for key in order[5:]:
        assert world.calls["deep"][key] == 1 and world.calls["evaluated"][key] == 1
    assert all(n == 1 for n in world.calls["baseline"].values())  # baseline never repeated
    _assert_no_duplicates(world.store, cycle_id, 10)
    deep_rows = {k[1]: v for k, v in world.store.cycle_progress(cycle_id).items() if k[0] == PHASE_DEEP}
    assert {v["state"] for v in deep_rows.values()} == {CandidateState.COMPLETED}


# ------------------------------------------------ Slice 3: 100 candidates --
@pytest.mark.asyncio
async def test_100_candidates_crash_at_41_resumes_same_cycle(world_factory, monkeypatch):
    world = world_factory(100)
    world.crash = ("deep_before_evidence", 41)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]
    order = [UUID(i) for i in world.store.cycle_run(cycle_id)["selection"]["deep_ids"]]
    assert len(order) == 100
    progress = world.store.cycle_progress(cycle_id)
    states = [progress.get((PHASE_DEEP, str(k)), {}).get("state") for k in order]
    assert states[:40] == [CandidateState.COMPLETED] * 40
    assert states[40] == CandidateState.IN_PROGRESS
    assert states[41:] == [None] * 59  # pending: never started
    calls_before = {name: Counter(c) for name, c in world.calls.items()}

    worker = await _restart_and_finish(world, monkeypatch)
    assert _job(world.store, cycle_id)["status"] == "COMPLETED"
    assert _job(world.store, cycle_id)["result_cycle_id"] == cycle_id
    assert world.store.cycle_run(cycle_id)["resume_count"] == 1
    resumed_calls = {name: world.calls[name] - calls_before[name] for name in world.calls}
    # 1..40: not reacquired, not reparsed, not re-evaluated.
    for key in order[:40]:
        assert resumed_calls["deep"][key] == 0 and resumed_calls["provider"][key] == 0
        assert resumed_calls["evaluated"][key] == 0 and resumed_calls["cache_hits"][key] == 0
    # 41: recovered once.
    assert resumed_calls["deep"][order[40]] == 1 and world.calls["provider"][order[40]] == 1
    # 42..100: continued exactly once.
    for key in order[41:]:
        assert world.calls["deep"][key] == 1 and world.calls["evaluated"][key] == 1
    # Stage-1 baseline: every instrument acquired exactly once across both processes.
    assert sum(resumed_calls["baseline"].values()) == 0
    assert all(world.calls["baseline"][UUID(int=n)] == 1 for n in range(1, 101))
    assert sum(resumed_calls["deep"].values()) == 60
    # #41 crashed before its evidence commit, so it (and 42..100) acquire once each.
    assert sum(resumed_calls["provider"].values()) == 60
    completed = _job(world.store, cycle_id)
    assert completed["resumed_candidates"] == {PHASE_BASELINE: 100, PHASE_DEEP: 40}
    assert completed["executed_candidates"] == {PHASE_BASELINE: 0, PHASE_DEEP: 60}


# ------------------------------------------------ Progress truthfulness ----
@pytest.mark.asyncio
async def test_resume_progress_never_misleadingly_regresses_below_durable_high_water_mark(world_factory, monkeypatch, caplog):
    """Root-cause regression test: before this fix, the per-process
    stage2_deep_progress log counter restarted at 0 on every resume (it
    counted raw loop iterations in THIS process, not durable completed
    outcomes), so an operator watching logs saw completed=40/100 before a
    restart and completed=10/100 shortly after -- even though recovery
    itself never re-executes the 40 already-durable candidates. This test
    proves the FIX: a resumed cycle (a) emits an explicit
    stage2_deep_resume_state log stating the durable K/N it is resuming
    from, and (b) every subsequent stage2_deep_progress log's completed
    count is monotonically >= that durable K -- it must never again report
    a number below the pre-restart durable high-water mark."""
    import logging
    world = world_factory(100)
    world.crash = ("deep_before_evidence", 41)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]
    # 40 candidates are durably COMPLETED before the crash (see the test above).
    durable_before_restart = 40

    caplog.set_level(logging.INFO, logger="app.global_opportunity_orchestration")
    caplog.clear()
    await _restart_and_finish(world, monkeypatch)
    assert _job(world.store, cycle_id)["status"] == "COMPLETED"

    resume_state = [r for r in caplog.records if "stage2_deep_resume_state" in r.getMessage()]
    assert len(resume_state) == 1, "expected exactly one resume-state announcement"
    resume_msg = resume_state[0].getMessage()
    assert f"cycleId={cycle_id}" in resume_msg
    match = re.search(r"durableCompleted=(\d+)/(\d+)", resume_msg)
    assert match is not None, resume_msg
    assert int(match.group(1)) == durable_before_restart
    assert int(match.group(2)) == 100

    progress_lines = [r.getMessage() for r in caplog.records if "stage2_deep_progress:" in r.getMessage()]
    assert progress_lines, "expected at least one progress log line after resume"
    for line in progress_lines:
        completed, total = (int(x) for x in re.search(r"completed=(\d+)/(\d+)", line).groups())
        assert total == 100
        # The crux of the fix: never below the durable high-water mark
        # established BEFORE this restart, at any point during resume.
        assert completed >= durable_before_restart, line
    # And progress must still reach full completion by the end.
    assert int(re.search(r"completed=(\d+)/", progress_lines[-1]).group(1)) == 100
    _assert_no_duplicates(world.store, cycle_id, 100)


@pytest.mark.asyncio
async def test_crash_after_facts_committed_reuses_facts(world_factory, monkeypatch):
    world = world_factory(20)
    world.crash = ("deep_after_evidence_commit", 7)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]
    order = [UUID(i) for i in world.store.cycle_run(cycle_id)["selection"]["deep_ids"]]
    target = order[6]
    assert world.store.load_financial_facts({target})  # committed before the crash
    await _restart_and_finish(world, monkeypatch)
    assert world.calls["provider"][target] == 1  # reused, not reacquired
    assert world.calls["deep"][target] == 2      # investigation resumed once
    _assert_no_duplicates(world.store, cycle_id, 20)


@pytest.mark.asyncio
async def test_crash_after_publication_completes_suggestions_exactly_once(world_factory, monkeypatch):
    world = world_factory(8)
    original = SqliteResearchPersistence.persist_global_suggestion_lifecycle
    state = {"crash": True, "calls": 0}

    def lifecycle(self, scan, cards):
        state["calls"] += 1
        if state["crash"]:
            state["crash"] = False
            raise SimulatedCrash("after publication")
        return original(self, scan, cards)
    monkeypatch.setattr(SqliteResearchPersistence, "persist_global_suggestion_lifecycle", lifecycle)
    job, _ = await _run_until_crash(world, monkeypatch)
    cycle_id = job["cycle_id"]
    assert world.store.cycle_run(cycle_id)["status"] == "PUBLISHED"
    evaluated_before = sum(world.calls["evaluated"].values())
    await _restart_and_finish(world, monkeypatch)
    assert world.store.cycle_run(cycle_id)["status"] == "COMPLETED"
    assert sum(world.calls["evaluated"].values()) == evaluated_before  # no re-evaluation after publication
    assert sum(world.calls["deep"].values()) == 8
    assert state["calls"] == 2
    scans = world.store._connection.execute("SELECT COUNT(*) FROM global_market_scan WHERE scan_id = ?", (cycle_id,)).fetchone()[0]
    assert scans == 1
    _assert_no_duplicates(world.store, cycle_id, 8)


@pytest.mark.asyncio
async def test_live_owner_lease_blocks_second_worker_and_submissions_coalesce(world_factory, monkeypatch):
    world = world_factory(6)
    repo, _, runner = build_process(world, monkeypatch)
    # Construct synchronous fixtures before starting a subsecond heartbeat;
    # repository/client initialization must not block the live owner's loop.
    repo2, _, runner2 = build_process(world, monkeypatch)
    gate = asyncio.Event()
    release = asyncio.Event()

    async def slow_runner(**kwargs):
        gate.set()
        await release.wait()
        return await runner(**kwargs)
    first = OpportunityCycleWorker(repo, slow_runner, lease_seconds=0.3, poll_seconds=0.05)
    job = first.submit({"top_n": 4, "shortlist_limit": 100})
    await asyncio.wait_for(gate.wait(), 5)
    second = OpportunityCycleWorker(repo2, runner2, lease_seconds=0.3, poll_seconds=0.05)
    second.start()
    coalesced = second.submit({"top_n": 4, "shortlist_limit": 100})
    assert coalesced["coalesced"] and coalesced["cycle_id"] == job["cycle_id"]
    await asyncio.sleep(0.8)  # > lease: the first owner's heartbeat keeps it alive
    assert world.store.cycle_run(job["cycle_id"])["owner_id"] == first.owner_id
    assert sum(world.calls["deep"].values()) == 0
    release.set()
    await asyncio.wait_for(first.queue.join(), 60)
    await first.close()
    await second.close()
    assert world.store.cycle_run(job["cycle_id"])["status"] == "COMPLETED"
    _assert_no_duplicates(world.store, job["cycle_id"], 6)


@pytest.mark.asyncio
async def test_graceful_shutdown_keeps_cycle_resumable(world_factory, monkeypatch):
    world = world_factory(6)
    repo, _, runner = build_process(world, monkeypatch)
    entered = asyncio.Event()

    async def blocked_runner(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    worker = OpportunityCycleWorker(repo, blocked_runner, lease_seconds=5, poll_seconds=0.05)
    job = worker.submit({"top_n": 4, "shortlist_limit": 100})
    await asyncio.wait_for(entered.wait(), 5)
    await worker.close()
    run = world.store.cycle_run(job["cycle_id"])
    assert run["status"] == "RUNNING" and run["owner_id"] is None  # released, resumable
    assert _job(world.store, job["cycle_id"])["error_code"] == "WORKER_STOPPED_RESUMABLE"
    await _restart_and_finish(world, monkeypatch, lease_seconds=0.1)
    assert world.store.cycle_run(job["cycle_id"])["status"] == "COMPLETED"
    assert _job(world.store, job["cycle_id"])["result_cycle_id"] == job["cycle_id"]
