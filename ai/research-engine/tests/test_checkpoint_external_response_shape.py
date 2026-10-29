"""Regression tests for the resumed_candidates/executed_candidates external
response contract (app.opportunity_worker.OpportunityCycleWorker._run_one).

PHASE_ETF_ONBOARD is a third, fully-registered checkpoint phase (strict
internal support: CycleCheckpoint.restored/executed always carry all three
phases, and an unregistered phase still fails loudly -- see
app/cycle_checkpoint.py). But the external resumed_candidates/
executed_candidates fields on a completed cycle's job record are a
long-standing response contract, established and asserted only by Equity
Radar (test_cycle_checkpoint_recovery.py), with exactly the two keys
{PHASE_BASELINE, PHASE_DEEP}. Naively reporting dict(checkpoint.restored) /
dict(checkpoint.executed) wholesale -- as the code did before this fix --
leaked a third {"ETF_ONBOARD": 0} key into EVERY completed cycle's response,
Equity included, breaking that contract.

The fix (EXTERNAL_RESPONSE_PHASES in app/cycle_checkpoint.py) filters the
reported dict to exactly the original two keys for every market, while
leaving the internal registry, strict KeyError protection, and the SQL
CHECK constraints untouched. These tests prove both halves of that: the
external shape never changes, and the internal phase support never weakens.
"""
from __future__ import annotations

import asyncio
from threading import RLock
from types import SimpleNamespace

import pytest

from app.cycle_checkpoint import (
    CandidateState,
    EXTERNAL_RESPONSE_PHASES,
    PHASE_BASELINE,
    PHASE_DEEP,
    PHASE_ETF_ONBOARD,
)
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence


def _pod(store, runner, *, market=None):
    settings = SimpleNamespace(research_opportunity_recovery_paused=False)
    repo = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), settings=settings)
    return OpportunityCycleWorker(repo, runner, lease_seconds=60, poll_seconds=0.05, market=market)


def _completed_job(store, cycle_id):
    return next(j for j in store.opportunity_jobs() if j["cycle_id"] == cycle_id)


def test_external_response_phases_is_exactly_the_original_two_phases():
    """Documents the contract directly: whatever phases get registered for
    internal strictness in the future, EXTERNAL_RESPONSE_PHASES must not grow
    silently -- only a deliberate change to a consumer's contract should move
    this tuple."""
    assert EXTERNAL_RESPONSE_PHASES == (PHASE_BASELINE, PHASE_DEEP)
    assert PHASE_ETF_ONBOARD not in EXTERNAL_RESPONSE_PHASES


@pytest.mark.asyncio
async def test_equity_style_cycle_response_excludes_etf_onboard_even_when_internally_registered():
    """market=None (Equity's historical default, 'NSE') cycle: the completed
    job's resumed_candidates/executed_candidates must be exactly the
    two-key legacy shape, with no 'ETF_ONBOARD' key -- the exact defect
    this round's Windows run reported."""
    store = SqliteResearchPersistence()

    async def runner(*, cycle_id, checkpoint, **_params):
        await checkpoint.start(PHASE_BASELINE, "INE000000001")
        await checkpoint.finish(PHASE_BASELINE, "INE000000001", CandidateState.COMPLETED, disposition="ANALYZED")
        await checkpoint.start(PHASE_DEEP, "INE000000001")
        await checkpoint.finish(PHASE_DEEP, "INE000000001", CandidateState.COMPLETED, disposition="ANALYZED")
        return {"cycle_id": cycle_id, "universe_count": 1}

    worker = _pod(store, runner)
    try:
        job = await worker.submit_async({"top_n": 10})
        await asyncio.wait_for(worker.queue.join(), 5)
        completed = _completed_job(store, job["cycle_id"])
        assert completed["status"] == "COMPLETED"
        assert completed["resumed_candidates"] == {PHASE_BASELINE: 0, PHASE_DEEP: 0}
        assert completed["executed_candidates"] == {PHASE_BASELINE: 1, PHASE_DEEP: 1}
        assert "ETF_ONBOARD" not in completed["resumed_candidates"]
        assert "ETF_ONBOARD" not in completed["executed_candidates"]
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_etf_cycle_response_excludes_etf_onboard_even_though_onboarding_actually_ran():
    """market='ETF' cycle where PHASE_ETF_ONBOARD is genuinely exercised
    (onboarding provisioned a real ISIN, incrementing checkpoint.executed
    internally): the external resumed_candidates/executed_candidates must
    STILL be exactly the two-key legacy shape -- onboarding's own progress
    is surfaced separately via coverage.stages.onboarding
    (app.main._etf_onboarding_stage_progress), not duplicated here."""
    store = SqliteResearchPersistence()

    async def runner(*, cycle_id, checkpoint, **_params):
        await checkpoint.start(PHASE_BASELINE, "INE000000001")
        await checkpoint.finish(PHASE_BASELINE, "INE000000001", CandidateState.COMPLETED, disposition="ANALYZED")
        await checkpoint.start(PHASE_ETF_ONBOARD, "INE999999999")
        await checkpoint.finish(PHASE_ETF_ONBOARD, "INE999999999", CandidateState.TERMINAL_OUTCOME,
                                disposition="OFFICIAL_ETF_IDENTITY_CREATED")
        return {"cycle_id": cycle_id, "universe_count": 1}

    worker = _pod(store, runner, market="ETF")
    try:
        job = await worker.submit_async({"top_n": None})
        await asyncio.wait_for(worker.queue.join(), 5)
        completed = _completed_job(store, job["cycle_id"])
        assert completed["status"] == "COMPLETED"

        # Internal registry proof: the phase genuinely ran and was counted --
        # this fix must never make PHASE_ETF_ONBOARD silently uncounted or
        # unsupported, only unreported through this specific external field.
        checkpoint_cycle_id = job["cycle_id"]
        progress = store.cycle_progress(checkpoint_cycle_id)
        assert progress[(PHASE_ETF_ONBOARD, "INE999999999")]["state"] == str(CandidateState.TERMINAL_OUTCOME)

        # External contract proof: still exactly the legacy two-key shape.
        assert completed["resumed_candidates"] == {PHASE_BASELINE: 0, PHASE_DEEP: 0}
        assert completed["executed_candidates"] == {PHASE_BASELINE: 1, PHASE_DEEP: 0}
        assert "ETF_ONBOARD" not in completed["resumed_candidates"]
        assert "ETF_ONBOARD" not in completed["executed_candidates"]
    finally:
        await worker.close()
