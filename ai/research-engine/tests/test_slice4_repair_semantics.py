"""Slice 4: technical failures are never business rejections; they are
retried within a bounded, durable repair budget. Genuinely unavailable
evidence is terminal and not retried. DI-20H.4 reason strings are preserved.
"""
from __future__ import annotations

from collections import Counter
from uuid import UUID

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_DEEP
from app.failure_taxonomy import (EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason,
                                  classify_requirement_failures)
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from test_global_opportunity_acquisition import acquisition_service
from test_global_scanner import NOW, instrument, persisted
from test_stock_rule_engine import _readiness

TECHNICAL = ["NETWORK_TIMEOUT", "PDF_EXTRACTION_TIMEOUT", "PDF_EXTRACTION_QUEUE_TIMEOUT", "PARSER_FAILED",
             "DOCUMENT_PERSIST_FAILED", "HTTP_FETCH_FAILED", "EXTERNAL_PROVIDER_UNAVAILABLE",
             "SEARCH_PROVIDER_RATE_LIMITED", "DOCUMENT_BUDGET_EXHAUSTED", "RuntimeError", "TimeoutError"]
PERMANENT = ["ROBOTS_OR_ACCESS_BLOCKED", "DOMAIN_BLOCKED", "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES",
             "DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED", "EVIDENCE_INSUFFICIENT_WITHIN_PLAN",
             "DOCUMENT_SIZE_LIMIT_EXCEEDED", "COMPANY_RELEVANCE_FAILED",
             "PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS",
             "PARSER_FAILED:NSE_SHAREHOLDING_XBRL_NO_SUPPORTED_VALUES"]


def test_taxonomy_is_explicit_and_conservative():
    for reason in TECHNICAL:
        assert classify_reason(reason) == TECHNICAL_RETRYABLE, reason
    for reason in PERMANENT:
        assert classify_reason(reason) == EVIDENCE_UNAVAILABLE, reason
    # Composite DI-20H.4 reasons: any technical component keeps it retryable.
    assert classify_reason("ROBOTS_OR_ACCESS_BLOCKED|NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE
    assert classify_reason("PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS|NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE
    assert classify_reason("PARSER_FAILED:NSE_SHAREHOLDING_XBRL_NO_SUPPORTED_VALUES|TimeoutError") == TECHNICAL_RETRYABLE
    assert classify_reason("PARSER_FAILED:UNRECOGNIZED_FAILURE") == TECHNICAL_RETRYABLE
    assert classify_requirement_failures({"A": "DISCOVERY_NO_CANDIDATES", "B": "NETWORK_TIMEOUT"}, ["A"]) == EVIDENCE_UNAVAILABLE
    assert classify_requirement_failures({"A": "DISCOVERY_NO_CANDIDATES", "B": "NETWORK_TIMEOUT"}, ["B"]) == TECHNICAL_RETRYABLE
    assert classify_requirement_failures({}, ["A"]) is None


def _service(monkeypatch, schedule):
    """schedule: key -> list of failure reasons per deep attempt (None = ready)."""
    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)
    calls = Counter()

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get("requirement_ids") is not None:
            return TargetedEnsureResult(None, (), ())
        attempt = calls[key]
        calls[key] += 1
        plan = schedule.get(key.int, [None])
        reason = plan[min(attempt, len(plan) - 1)]
        if reason is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(not_ready, (), (), failures={"VALUATION_INPUTS": reason})
    runtime.ensure.side_effect = ensure
    return service, store, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", TECHNICAL)
async def test_transient_technical_failure_is_repaired_not_rejected(monkeypatch, reason):
    service, _, calls = _service(monkeypatch, {1: [reason, None]})
    result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1)
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "ANALYZED", diagnostic
    assert calls[UUID(int=1)] == 2
    assert result.deep_repair_attempted_count == 1 and result.deep_repair_recovered_count == 1
    assert result.rule_analyzed_count == 1 and result.deep_readiness_failed_count == 0
    assert len(result.diagnostics) == 1  # exact accounting after repair


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", PERMANENT)
async def test_genuinely_unavailable_evidence_is_terminal_and_not_retried(monkeypatch, reason):
    service, _, calls = _service(monkeypatch, {1: [reason]})
    result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1)
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.failure_class == EVIDENCE_UNAVAILABLE
    assert diagnostic.acquisition_failures == {"VALUATION_INPUTS": reason}  # DI-20H.4 reason preserved
    assert calls[UUID(int=1)] == 1
    assert result.deep_repair_attempted_count == 0 and result.deep_technical_failure_count == 0


@pytest.mark.asyncio
async def test_persistent_technical_failure_is_bounded_and_explicitly_unresolved(monkeypatch):
    service, _, calls = _service(monkeypatch, {1: ["NETWORK_TIMEOUT"]})
    result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1)
    diagnostic = result.diagnostics[0]
    assert diagnostic.disposition == "DEEP_READINESS_NOT_MET"
    assert diagnostic.failure_class == TECHNICAL_RETRYABLE
    assert diagnostic.acquisition_failures == {"VALUATION_INPUTS": "NETWORK_TIMEOUT"}
    assert calls[UUID(int=1)] == 2  # main pass + one bounded repair
    assert result.deep_technical_failure_count == 1 and result.deep_readiness_failed_count == 1
    assert result.deep_repair_attempted_count == 1 and result.deep_repair_recovered_count == 0
    assert len(result.diagnostics) == 1


@pytest.mark.asyncio
async def test_mixed_universe_accounting_is_exact_after_repair(monkeypatch):
    schedule = {1: ["NETWORK_TIMEOUT", None], 2: ["ROBOTS_OR_ACCESS_BLOCKED"], 3: ["PARSER_FAILED"],
                4: [None], 5: ["ACQUISITION_TIMEOUT", None]}
    service, _, calls = _service(monkeypatch, schedule)
    result = await service.run([instrument(n) for n in range(1, 6)], as_of=NOW, shortlist_limit=5)
    by_id = {d.global_instrument_id.int: d for d in result.diagnostics}
    assert len(result.diagnostics) == 5
    assert by_id[1].disposition == by_id[4].disposition == by_id[5].disposition == "ANALYZED"
    assert by_id[2].failure_class == EVIDENCE_UNAVAILABLE and by_id[3].failure_class == TECHNICAL_RETRYABLE
    assert dict(calls) == {UUID(int=1): 2, UUID(int=2): 1, UUID(int=3): 2, UUID(int=4): 1, UUID(int=5): 2}
    assert result.deep_attempted_count == 5
    assert result.rule_analyzed_count == 3 and result.deep_readiness_failed_count == 2
    assert result.deep_acquisition_timeout_count == 0 and result.deep_technical_failure_count == 1
    assert result.deep_repair_attempted_count == 3 and result.deep_repair_recovered_count == 2


@pytest.mark.asyncio
async def test_repair_state_is_durable_and_bounded_across_restarts(monkeypatch):
    service, store, calls = _service(monkeypatch, {1: ["NETWORK_TIMEOUT"]})
    store.create_cycle_run("cycle-r", {})
    store.claim_cycle_run("cycle-r", "owner-a")
    checkpoint = await CycleCheckpoint(store, "cycle-r", "owner-a").load()
    await service.run([instrument(1)], as_of=NOW, shortlist_limit=1, checkpoint=checkpoint)
    row = store.cycle_progress("cycle-r")[(PHASE_DEEP, str(UUID(int=1)))]
    assert row["state"] == CandidateState.RETRYABLE_FAILURE and row["attempts"] == 2
    assert row["payload"]["diagnostic"]["acquisition_failures"] == {"VALUATION_INPUTS": "NETWORK_TIMEOUT"}
    # Restart 1: one more attempt is allowed (max 3 per cycle), then the budget is spent.
    store._connection.execute("UPDATE global_opportunity_cycle_run SET owner_id='owner-b' WHERE cycle_id='cycle-r'")
    resumed = await CycleCheckpoint(store, "cycle-r", "owner-b").load()
    await service.run([instrument(1)], as_of=NOW, shortlist_limit=1, checkpoint=resumed)
    assert calls[UUID(int=1)] == 3
    assert store.cycle_progress("cycle-r")[(PHASE_DEEP, str(UUID(int=1)))]["attempts"] == 3
    # Restart 2: repair budget exhausted -> restored, never retried forever.
    store._connection.execute("UPDATE global_opportunity_cycle_run SET owner_id='owner-c' WHERE cycle_id='cycle-r'")
    again = await CycleCheckpoint(store, "cycle-r", "owner-c").load()
    result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1, checkpoint=again)
    assert calls[UUID(int=1)] == 3
    assert result.diagnostics[0].failure_class == TECHNICAL_RETRYABLE
    assert result.deep_technical_failure_count == 1


@pytest.mark.asyncio
async def test_evidence_unavailable_is_checkpointed_terminal(monkeypatch):
    service, store, calls = _service(monkeypatch, {1: ["ROBOTS_OR_ACCESS_BLOCKED"]})
    store.create_cycle_run("cycle-p", {})
    store.claim_cycle_run("cycle-p", "owner-a")
    checkpoint = await CycleCheckpoint(store, "cycle-p", "owner-a").load()
    await service.run([instrument(1)], as_of=NOW, shortlist_limit=1, checkpoint=checkpoint)
    row = store.cycle_progress("cycle-p")[(PHASE_DEEP, str(UUID(int=1)))]
    assert row["state"] == CandidateState.EVIDENCE_UNAVAILABLE and row["attempts"] == 1
