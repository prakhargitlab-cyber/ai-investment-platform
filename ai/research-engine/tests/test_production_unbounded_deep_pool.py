"""Production preserves the requested deep cap; explicit diagnostic None stays supported."""
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_BASELINE, PHASE_DEEP
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.persistence import SqliteResearchPersistence
from test_global_opportunity_baseline import setup_acquisition, _baseline_ready_count
from test_global_opportunity_empty_universe import _EmptyRanking, _FakeSource, _Clock
from test_global_scanner import NOW


# --- 1: candidate_ids=None does not truncate a large candidate pool (orchestrator level) ---

@pytest.mark.asyncio
async def test_shortlist_limit_none_does_not_truncate_a_pool_larger_than_the_old_cap(monkeypatch):
    """With shortlist_limit=None (an explicit diagnostic value), a pool larger than the
    old fixed 100-candidate cap must be processed in full: every eligible
    candidate reaches baseline AND deep evaluation, with no silent truncation
    anywhere in the pipeline."""
    COUNT = 130  # > the old shortlist_limit=100 default/cap
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=COUNT)

    result = await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4)

    assert _baseline_ready_count(tracker) == COUNT
    assert result.baseline_ready_count == COUNT
    assert result.deep_pool_eligible_count == COUNT
    # The critical assertion: NO truncation anywhere in the deep pipeline.
    assert result.shortlist_count == COUNT
    assert result.deep_candidate_count == COUNT


@pytest.mark.asyncio
async def test_shortlist_limit_none_rejected_only_for_bad_types_not_for_none_itself(monkeypatch):
    """None is now a legitimate value (unbounded); the validator must still
    reject genuinely invalid values (0, negative, >100, non-int, non-None)."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=1)
    # None is accepted (no ValueError).
    await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4)
    for bad in (0, -1, 101, "25", 3.5):
        with pytest.raises(ValueError):
            await service.run(rows, as_of=NOW, shortlist_limit=bad, top_n=4)


# --- 2: a pool larger than 100 is fully iterated AND durably checkpointed ---

@pytest.mark.asyncio
async def test_full_pool_larger_than_100_is_durably_checkpointed(monkeypatch):
    """Every candidate in a >100-sized production pool must receive a durable
    Phase-1 AND Phase-2 checkpoint row (global_opportunity_cycle_progress) --
    not just the first 100 -- proving the existing checkpoint/resume machinery
    (already O(1)-per-candidate, not O(shortlist_limit)) is exercised for the
    complete unbounded pool."""
    COUNT = 130
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=COUNT)
    # record_cycle_progress requires a matching, owned global_opportunity_cycle_run
    # row (INSERT ... SELECT ... FROM global_opportunity_cycle_run WHERE cycle_id=?
    # AND owner_id=?) -- create and claim it exactly as the real production worker
    # does before ever calling checkpoint.start()/finish().
    store.create_cycle_run("test-cycle-unbounded", {})
    assert store.claim_cycle_run("test-cycle-unbounded", "test-owner")
    checkpoint = CycleCheckpoint(store, "test-cycle-unbounded", "test-owner", run_blocking=None)
    await checkpoint.load()

    result = await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4, checkpoint=checkpoint)

    assert result.baseline_ready_count == COUNT
    assert result.deep_candidate_count == COUNT

    progress = store.cycle_progress("test-cycle-unbounded")
    baseline_rows = {k[1] for k in progress if k[0] == PHASE_BASELINE}
    deep_rows = {k[1] for k in progress if k[0] == PHASE_DEEP}
    assert len(baseline_rows) == COUNT, (
        f"expected {COUNT} durable Phase-1 checkpoint rows, got {len(baseline_rows)}")
    assert len(deep_rows) == COUNT, (
        f"expected {COUNT} durable Phase-2 checkpoint rows, got {len(deep_rows)} "
        "-- the deep pool must not be silently capped below the full eligible universe")


# --- 3: candidate_ids=None forces unbounded regardless of caller value; controlled cycles keep their bound ---

class _CapturingOrchestrator:
    """Stand-in for GlobalOpportunityOrchestrator that records the exact
    shortlist_limit it receives, without exercising the real scanner/readiness
    chain (mirrors _FakeOrchestrator in test_global_opportunity_empty_universe)."""
    last_shortlist_limit = "UNSET"

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, *args, **kwargs):
        _CapturingOrchestrator.last_shortlist_limit = kwargs.get("shortlist_limit")
        return _EmptyRanking()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(7, 7), (25, 25), (100, 100), (None, 25)])
async def test_production_preserves_requested_bound_and_defaults_legacy_none(monkeypatch, limit, expected):
    """The production boundary must not nullify the requested deep budget."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=None, shortlist_limit=limit, top_n=2)

    assert _CapturingOrchestrator.last_shortlist_limit == expected


@pytest.mark.asyncio
async def test_controlled_candidate_ids_preserves_the_requested_bound(monkeypatch):
    """An explicit candidate_ids list (controlled/manual validation) must keep
    enforcing its requested shortlist_limit exactly as before -- the production
    override applies only when candidate_ids is None."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=[UUID(int=1)], shortlist_limit=10, top_n=2)

    assert _CapturingOrchestrator.last_shortlist_limit == 10


# --- 5: bounded worker concurrency is unchanged and holds even for a large unbounded pool ---

def test_baseline_concurrency_constant_unchanged():
    """The Phase-1 bounded worker-pool size is untouched by the shortlist-cap
    removal -- concurrency and candidate-count are orthogonal."""
    assert GlobalOpportunityOrchestrator._BASELINE_CONCURRENCY == 8


@pytest.mark.asyncio
async def test_peak_baseline_concurrency_stays_bounded_for_a_large_unbounded_pool(monkeypatch):
    """A >100-candidate production pool (shortlist_limit=None) must still never
    exceed _BASELINE_CONCURRENCY in-flight baseline acquisitions at once --
    unbounded candidate COUNT must not become unbounded concurrency."""
    import asyncio
    COUNT = 130
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=COUNT)

    active = 0
    peak = 0
    real_ensure = service.readiness.ensure

    async def tracking_ensure(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)  # yield, so overlap is actually observable
            return await real_ensure(*args, **kwargs)
        finally:
            active -= 1

    service.readiness.ensure = tracking_ensure

    result = await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4)

    assert result.baseline_ready_count == COUNT
    assert peak <= GlobalOpportunityOrchestrator._BASELINE_CONCURRENCY, (
        f"peak concurrent baseline acquisitions was {peak}, expected <= "
        f"{GlobalOpportunityOrchestrator._BASELINE_CONCURRENCY}")
