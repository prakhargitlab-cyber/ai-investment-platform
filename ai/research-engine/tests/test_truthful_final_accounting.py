"""Defect 3 -- lock down truthful final research accounting (Slice DI-20H.5).

Diagnosis (Guardian Review, diagnosis-only pass) found that the existing
persisted contract -- ``disposition`` + ``failure_class`` (computed once, at
the point a candidate's outcome is decided) + the checkpoint ``state`` column
they are folded into by ``_checkpoint_deep_outcome`` before persistence -- is
ALREADY sufficient to reconstruct, for every applicable instrument and after
a restart/recovery, exactly one of the three truthful terminal meanings:

  A. FULLY_ANALYZED                    -- CandidateState.COMPLETED
  B. DATA_GENUINELY_UNAVAILABLE        -- CandidateState.EVIDENCE_UNAVAILABLE
  C. TECHNICAL_FAILURE / RETRY_REQUIRED -- CandidateState.RETRYABLE_FAILURE

No state-machine redesign was needed or made. This file proves that
contract end to end (readiness/failures in -> disposition/failure_class out
-> checkpoint state persisted -> app.cycle_checkpoint.terminal_meaning()
reconstructs the correct meaning), for the two previously-diagnosed
legitimate patterns (stale-but-valid durable evidence, and READY-but-
unscorable) plus the technical-vs-genuine failure distinction, the
CURRENT_NEWS non-blocking exemption, and completeness (no silently omitted
applicable instrument).

Two existing gates are deliberately NOT touched or redesigned here:
  - StockRuleEngineEligibilityPolicy.evaluate() (readiness -> eligibility)
  - GlobalOpportunityOrchestrator._incomplete_analysis() (Rule Engine result
    -> fully-analyzed or not)
This file only proves what their outputs already, correctly, persist as.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.cycle_checkpoint import (CandidateState, CycleCheckpoint, PHASE_DEEP,
                                   state_for_disposition, terminal_meaning)
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult

import test_stock_rule_engine as sre
from test_full_research_state_contract import _unscorable_rule
from test_global_opportunity_acquisition import acquisition_service
from test_global_opportunity_baseline import setup_acquisition, _baseline_ready_count
from test_global_scanner import NOW, instrument, persisted
from test_global_opportunity_ranker import inputs as ranker_inputs

MANDATORY_REQ = "VALUATION_INPUTS"  # a real mandatory, non-CURRENT_NEWS requirement


def _readiness(overrides=None):
    return sre._readiness(overrides=overrides or {})


async def _run_single(monkeypatch, *, readiness, failures=None, rule=None):
    """Run one candidate through the real Stage-2 deep-evaluation path
    (readiness -> eligibility -> Rule Engine -> disposition/failure_class),
    exactly as production does, and return its single CandidateDiagnostic
    plus the checkpoint state that outcome would be persisted as."""
    service, runtime, store = acquisition_service(monkeypatch)
    if rule is not None:
        service.rule_engine.analyze.side_effect = None
        service.rule_engine.analyze.return_value = rule

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get("requirement_ids") is None:
            return TargetedEnsureResult(readiness, (), (), failures=failures or {})
        return TargetedEnsureResult(None, (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)
    assert len(result.diagnostics) == 1, "exactly one applicable instrument must produce exactly one diagnostic"
    diagnostic = result.diagnostics[0]
    state = state_for_disposition(diagnostic.disposition)
    if diagnostic.disposition == "DEEP_READINESS_NOT_MET" and diagnostic.failure_class == TECHNICAL_RETRYABLE:
        state = CandidateState.RETRYABLE_FAILURE  # the same override _checkpoint_deep_outcome applies
    return diagnostic, state


# --- 1: stale-but-valid durable evidence + failed freshness acquisition -> FULLY_ANALYZED ---

@pytest.mark.asyncio
async def test_stale_but_valid_mandatory_evidence_can_legitimately_reach_fully_analyzed(monkeypatch):
    """READY_STALE durable evidence satisfies eligibility on its own (the
    policy's FULL_STATUSES includes READY_STALE) even when this cycle's
    attempt to refresh it recorded a technical failure -- that failure
    describes the refresh attempt, not the durable evidence's validity, and
    must not by itself keep an otherwise-complete candidate out of
    FULLY_ANALYZED."""
    readiness = _readiness({MANDATORY_REQ: ResearchRequirementStatus.READY_STALE})
    diagnostic, state = await _run_single(
        monkeypatch, readiness=readiness, failures={MANDATORY_REQ: "NETWORK_TIMEOUT"})

    assert diagnostic.disposition in {"ANALYZED", "RANK_FILTERED"}, diagnostic.disposition
    assert state == CandidateState.COMPLETED
    assert terminal_meaning(state) == "FULLY_ANALYZED"


# --- 2: mandatory requirement READY but zero usable/scorable Rule Engine metrics -> NOT FULLY_ANALYZED ---

@pytest.mark.asyncio
async def test_ready_requirement_with_unscorable_rule_engine_area_never_becomes_fully_analyzed(monkeypatch):
    """Readiness reports the mandatory requirement fully READY (eligibility
    permits the Rule Engine to run), but the Rule Engine itself could not
    score an applicable required area from that evidence. This must never be
    represented as FULLY_ANALYZED (ANALYZED/RANK_FILTERED) -- it is a
    distinct, narrower gate than requirement-level readiness."""
    readiness = _readiness()  # every requirement READY_FRESH
    rule = _unscorable_rule(engine_flagged=True)
    diagnostic, state = await _run_single(monkeypatch, readiness=readiness, rule=rule)

    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.disposition not in {"ANALYZED", "RANK_FILTERED"}
    assert state != CandidateState.COMPLETED
    assert terminal_meaning(state) != "FULLY_ANALYZED"


# --- 3: technical mandatory acquisition failure -> retry-required, never DATA_GENUINELY_UNAVAILABLE ---

@pytest.mark.asyncio
async def test_technical_mandatory_acquisition_failure_is_retry_required_not_genuinely_unavailable(monkeypatch):
    readiness = _readiness({MANDATORY_REQ: ResearchRequirementStatus.MISSING})
    diagnostic, state = await _run_single(
        monkeypatch, readiness=readiness, failures={MANDATORY_REQ: "NETWORK_TIMEOUT"})

    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.failure_class == TECHNICAL_RETRYABLE
    assert state == CandidateState.RETRYABLE_FAILURE
    assert terminal_meaning(state) == "TECHNICAL_FAILURE_RETRY_REQUIRED"
    assert terminal_meaning(state) != "DATA_GENUINELY_UNAVAILABLE"


# --- 4: authoritative genuine absence -> DATA_GENUINELY_UNAVAILABLE, never retry-required ---

@pytest.mark.asyncio
async def test_authoritative_genuine_absence_is_data_genuinely_unavailable_not_retry_required(monkeypatch):
    readiness = _readiness({MANDATORY_REQ: ResearchRequirementStatus.MISSING})
    diagnostic, state = await _run_single(
        monkeypatch, readiness=readiness, failures={MANDATORY_REQ: "SOURCE_QUALITY_REJECTED"})

    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.failure_class == EVIDENCE_UNAVAILABLE
    assert state == CandidateState.EVIDENCE_UNAVAILABLE
    assert terminal_meaning(state) == "DATA_GENUINELY_UNAVAILABLE"
    assert terminal_meaning(state) != "TECHNICAL_FAILURE_RETRY_REQUIRED"


# --- 5: CURRENT_NEWS technical failure remains non-blocking (final accounting, not just eligibility) ---

@pytest.mark.asyncio
async def test_current_news_technical_failure_still_reaches_fully_analyzed(monkeypatch):
    """Narrow confirmation, at the persisted-disposition level, that a
    CURRENT_NEWS acquisition failure alone -- with every other mandatory
    requirement satisfied -- never prevents FULLY_ANALYZED. This is an
    end-to-end check of the already-established optional/non-blocking
    semantics (app.stock_rule_engine._current_news_is_optional_and_non_blocking);
    those semantics are verified, not modified, here."""
    readiness = _readiness({"CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    diagnostic, state = await _run_single(
        monkeypatch, readiness=readiness, failures={"CURRENT_NEWS": "SEARCH_PROVIDER_UNAVAILABLE"})

    assert diagnostic.disposition in {"ANALYZED", "RANK_FILTERED"}
    assert state == CandidateState.COMPLETED
    assert terminal_meaning(state) == "FULLY_ANALYZED"


# --- 6: completed production accounting cannot silently omit an applicable instrument ---

@pytest.mark.asyncio
async def test_completed_accounting_never_silently_omits_an_applicable_instrument(monkeypatch):
    """A mixed pool (successful / technical-failure / genuinely-unavailable
    outcomes) must produce exactly one diagnostic per applicable instrument,
    each mapping to a non-None terminal meaning, with durable checkpoint rows
    for every one of them -- no instrument silently missing from the final
    accounting."""
    COUNT = 6
    service, rows, pairs, store, _tracker = setup_acquisition(monkeypatch, count=COUNT)

    async def ensure(key, *, requirement_ids=None, **kwargs):
        if requirement_ids is not None:
            return TargetedEnsureResult(_readiness(), (), ())  # baseline: cache-hit
        bucket = key.int % 3
        if bucket == 0:
            return TargetedEnsureResult(_readiness(), (), ())  # fully ready -> FULLY_ANALYZED
        if bucket == 1:
            return TargetedEnsureResult(
                _readiness({MANDATORY_REQ: ResearchRequirementStatus.MISSING}), (), (),
                failures={MANDATORY_REQ: "NETWORK_TIMEOUT"})  # technical
        return TargetedEnsureResult(
            _readiness({MANDATORY_REQ: ResearchRequirementStatus.MISSING}), (), (),
            failures={MANDATORY_REQ: "SOURCE_QUALITY_REJECTED"})  # genuine absence

    service.readiness.ensure = ensure

    store.create_cycle_run("test-cycle-accounting", {})
    assert store.claim_cycle_run("test-cycle-accounting", "test-owner")
    checkpoint = CycleCheckpoint(store, "test-cycle-accounting", "test-owner", run_blocking=None)
    await checkpoint.load()

    result = await service.run(rows, as_of=NOW, shortlist_limit=None, top_n=4, checkpoint=checkpoint)

    expected_keys = {UUID(int=n) for n in range(1, COUNT + 1)}
    diagnosed_keys = {d.global_instrument_id for d in result.diagnostics}
    assert diagnosed_keys == expected_keys, (
        f"missing from final diagnostics: {expected_keys - diagnosed_keys}")

    progress = store.cycle_progress("test-cycle-accounting")
    deep_rows = {k[1]: v for k, v in progress.items() if k[0] == PHASE_DEEP}
    assert {UUID(r) for r in deep_rows} == expected_keys, (
        "every applicable instrument must have a durable PHASE_DEEP checkpoint row")

    for key_str, row in deep_rows.items():
        state = CandidateState(row["state"])
        meaning = terminal_meaning(state)
        assert meaning is not None, f"instrument {key_str} persisted with no reconstructable terminal meaning"
        assert meaning in {"FULLY_ANALYZED", "DATA_GENUINELY_UNAVAILABLE", "TECHNICAL_FAILURE_RETRY_REQUIRED"}

    # And the bucket split landed where expected -- proving the mixed pool
    # actually exercised all three outcomes, not just one repeated.
    meanings = {UUID(k): terminal_meaning(CandidateState(v["state"])) for k, v in deep_rows.items()}
    for n in range(1, COUNT + 1):
        key = UUID(int=n)
        bucket = n % 3
        expected = {0: "FULLY_ANALYZED", 1: "TECHNICAL_FAILURE_RETRY_REQUIRED",
                    2: "DATA_GENUINELY_UNAVAILABLE"}[bucket]
        assert meanings[key] == expected, (key, bucket, meanings[key])


# --- terminal_meaning() itself: closed, exhaustive over final states ---

def test_terminal_meaning_covers_every_final_candidate_state_with_no_silent_gap():
    from app.cycle_checkpoint import FINAL_STATES
    for state in FINAL_STATES:
        assert terminal_meaning(state) is not None, f"{state} has no terminal meaning mapped"
    assert terminal_meaning(CandidateState.PENDING) is None
    assert terminal_meaning(CandidateState.IN_PROGRESS) is None
