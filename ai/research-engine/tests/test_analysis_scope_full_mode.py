"""FULL vs BOUNDED analysis_scope: the explicit, persisted request-level
control for whether the pre-deep shortlist cap applies (see
app/global_opportunity_cycle.py run_global_opportunity_cycle).

Layer 1 (fast, isolated via _CapturingOrchestrator): proves analysis_scope
resolves to the correct effective_shortlist_limit for every required case,
without exercising the real scanner/readiness chain.

Layer 2 (realistic, real GlobalOpportunityOrchestrator + a fixture with a
genuine eligible/ineligible split): proves that resolved shortlist_limit
value actually controls how many candidates are deep-admitted, using the
exact universe=120 / baseline-eligible=87 / shortlist_limit=25 shape
requested for this feature.

Layer 3: durable-parameters round-trip, proving analysis_scope survives
checkpoint/resume (it is read fresh from persisted cycle_run.parameters on
every resume, so there is no separate "mode" state to lose).
"""
from threading import RLock
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.global_opportunity_cycle import (
    ANALYSIS_SCOPE_BOUNDED,
    ANALYSIS_SCOPE_FULL,
    run_global_opportunity_cycle,
)
from app.global_scanner import GlobalPreScore
from app.persistence import SqliteResearchPersistence
from test_global_opportunity_baseline import setup_acquisition, _baseline_ready_count
from test_global_opportunity_empty_universe import _EmptyRanking, _FakeSource, _Clock
from test_global_scanner import NOW
from test_production_unbounded_deep_pool import _CapturingOrchestrator


# --- Layer 1: analysis_scope -> effective_shortlist_limit resolution ------

@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(7, 7), (25, 25), (100, 100), (None, 25)])
async def test_bounded_mode_matches_existing_default_behavior(monkeypatch, limit, expected):
    """Explicit analysis_scope=BOUNDED is byte-for-byte identical to omitting
    it (requirement: existing request without the new mode remains default
    bounded behavior; explicit BOUNDED must match it exactly)."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=None, shortlist_limit=limit, top_n=2,
        analysis_scope=ANALYSIS_SCOPE_BOUNDED)

    assert _CapturingOrchestrator.last_shortlist_limit == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [7, 25, 100, None])
async def test_full_mode_forces_unbounded_regardless_of_shortlist_limit(monkeypatch, limit):
    """analysis_scope=FULL must bypass the pre-deep shortlist cap entirely --
    whatever shortlist_limit value is supplied is superseded with None, for a
    production (candidate_ids=None) cycle."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=None, shortlist_limit=limit, top_n=2,
        analysis_scope=ANALYSIS_SCOPE_FULL)

    assert _CapturingOrchestrator.last_shortlist_limit is None


@pytest.mark.asyncio
async def test_full_mode_also_bypasses_cap_for_controlled_candidate_ids(monkeypatch):
    """FULL must behave the same way for an explicit candidate_ids cycle too
    -- analysis_scope, not candidate_ids, decides whether the cap applies."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    await cycle.run_global_opportunity_cycle(
        repo, _FakeSource(), candidate_ids=[UUID(int=1)], shortlist_limit=10, top_n=2,
        analysis_scope=ANALYSIS_SCOPE_FULL)

    assert _CapturingOrchestrator.last_shortlist_limit is None


@pytest.mark.asyncio
async def test_invalid_analysis_scope_is_rejected(monkeypatch):
    """API validation must clearly distinguish FULL vs BOUNDED: anything else
    is rejected, not silently coerced to either mode."""
    from app import global_opportunity_cycle as cycle
    monkeypatch.setattr(cycle, "datetime", _Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", _CapturingOrchestrator)
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock())

    for bad in ("full", "bounded", "UNBOUNDED", "", None, 1):
        with pytest.raises(ValueError):
            await cycle.run_global_opportunity_cycle(
                repo, _FakeSource(), candidate_ids=None, shortlist_limit=25, top_n=2,
                analysis_scope=bad)


# --- Layer 2: realistic fixture -- universe=120, baseline eligible=87 -----

def _make_partially_eligible_fixture(monkeypatch, *, count=120, ineligible_count=33):
    """count candidates total; the first ineligible_count are made genuinely
    ineligible for both acquisition and deep analysis (a real baseline/
    admission exclusion, never a ranking-position exclusion), leaving exactly
    count - ineligible_count legitimately baseline-eligible candidates."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=count)
    ineligible_ids = {UUID(int=n) for n in range(1, ineligible_count + 1)}
    original_score = GlobalPreScore.score

    def score(self, row, *args, **kwargs):
        candidate = original_score(self, row, *args, **kwargs)
        if candidate.global_instrument_id in ineligible_ids:
            return candidate.model_copy(update={
                'eligible_for_deep_analysis': False,
                'eligible_for_acquisition': False,
                'pre_score': 0,
            })
        return candidate

    monkeypatch.setattr(GlobalPreScore, 'score', score)
    return service, rows, pairs, store, tracker


@pytest.mark.asyncio
async def test_bounded_mode_admits_no_more_than_the_legitimate_bounded_population(monkeypatch):
    """universe=120, baseline eligible=87, shortlist_limit=25 -> BOUNDED deep
    admission is capped at <= 25, never at the full 87-strong eligible pool."""
    service, rows, pairs, store, tracker = _make_partially_eligible_fixture(monkeypatch)

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    assert result.deep_pool_eligible_count <= 87
    assert result.shortlist_count <= 25
    assert result.deep_candidate_count <= 25


@pytest.mark.asyncio
async def test_full_mode_does_not_apply_shortlist_limit_to_eligible_candidates(monkeypatch):
    """Same fixture, FULL semantics (shortlist_limit=None reaching the
    orchestrator, exactly as analysis_scope=FULL resolves it): deep admission
    must equal the full 87-candidate legitimately-eligible population --
    neither more (no ineligible candidate sneaks in) nor fewer (no ranking-
    position/shortlist truncation)."""
    service, rows, pairs, store, tracker = _make_partially_eligible_fixture(monkeypatch)

    result = await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4)

    assert result.deep_candidate_count == 87, (
        f"expected all 87 legitimately eligible candidates admitted, got {result.deep_candidate_count}")
    assert result.shortlist_count == 87
    # top_n still caps the FINAL published set, independent of the deep pool size.
    assert len(result.top_n) <= 4


@pytest.mark.asyncio
async def test_full_mode_still_excludes_genuinely_ineligible_candidates(monkeypatch):
    """FULL must not weaken eligibility: the 33 deliberately-ineligible
    candidates must never reach deep investigation, even though no shortlist
    cap is applied."""
    service, rows, pairs, store, tracker = _make_partially_eligible_fixture(monkeypatch)
    ineligible_ids = {UUID(int=n) for n in range(1, 34)}

    await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4)

    deep_ids = {c[0] for c in tracker.deep_calls}
    assert not (deep_ids & ineligible_ids), "a deterministically ineligible candidate reached deep investigation"


# Ranking-after-completion-order correctness for the shortlist_limit=None
# path (exactly what analysis_scope=FULL resolves to) is already proven by
# tests/test_di20h1a_full_universe.py::
# test_late_unready_nominee_reaches_rules_and_wins_despite_earlier_failure --
# a candidate that finishes LAST (deliberately, via an injected RuntimeError
# on an earlier candidate) still wins top_n, and
# result.investigation_matrix[...]['rule_evaluated'] proves it reached rule
# analysis. Re-run here only as a regression citation, not duplicated.


# --- Layer 3: durable parameters round-trip (checkpoint/resume) -----------

def test_analysis_scope_full_survives_a_persisted_cycle_run_round_trip():
    """FULL mode must never fall back to the bounded default after a worker
    restart/resume: analysis_scope is read fresh from the durable
    cycle_run.parameters on every resume (there is no separate mode state to
    lose), so persisting it and reading it back must be exact."""
    store = SqliteResearchPersistence()
    parameters = {"top_n": 4, "shortlist_limit": 25, "candidate_ids": None, "analysis_scope": ANALYSIS_SCOPE_FULL}
    run, created = store.create_cycle_run("full-mode-cycle", parameters)
    assert created
    assert run["parameters"]["analysis_scope"] == ANALYSIS_SCOPE_FULL

    reloaded = store.cycle_run("full-mode-cycle")
    assert reloaded["parameters"]["analysis_scope"] == ANALYSIS_SCOPE_FULL
    assert reloaded["parameters"]["shortlist_limit"] == 25, (
        "the REQUEST shortlist_limit is persisted verbatim for audit; it is "
        "analysis_scope, read separately, that decides whether it is applied")
