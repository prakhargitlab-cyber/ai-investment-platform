"""CURRENT_NEWS is optional contextual research (narrow correction).

Prior behavior exempted only a CURRENT_NEWS *technical/provider search
failure* (PARTIAL / FAILED) from the fundamental eligibility gates, while
MISSING (never-attempted) CURRENT_NEWS still blocked full_analysis_allowed /
the Rule Engine / rank_eligible. That contradicted the intended full-research
contract, under which CURRENT_NEWS must never by itself block eligibility in
ANY truthful state.

This file proves the corrected, blanket, state-independent CURRENT_NEWS
exemption (see app.stock_rule_engine._current_news_is_optional_and_non_blocking)
end to end: eligibility policy -> Rule Engine evaluation -> orchestration
completeness gate -> ranker rank_eligible -- while confirming mandatory
fundamental requirements are completely unaffected (test F) and CURRENT_NEWS
diagnostics remain visible (test G).

Tests H-K extend this to app.deep_investigation.investigate() itself: proving
CURRENT_NEWS's *acquisition* -- not just its already-proven eligibility
exemption -- does not sit on the mandatory wall-clock critical path when a
caller opts in via investigate()'s `background_tasks` parameter, using the
same lifecycle-safe drain (asyncio.gather(*pending, return_exceptions=True))
that GlobalOpportunityOrchestrator._acquire_deep's finally: block performs on
its own `pending_news_tasks`.
"""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.deep_investigation import investigate
from app.research_readiness_runtime import TargetedEnsureResult

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.news_intelligence import ProviderOutcome, aggregate_search
from app.research_readiness import ResearchRequirementStatus
from app.stock_rule_engine import AreaScoreStatus, DecisionSignal, StockRuleEngineEligibilityPolicy, StockRuleEngineV1

from test_stock_rule_engine import NOW, INSTRUMENT_ID, _inputs, _readiness


class _FakeInvestigateRuntime:
    """A controllable runtime for investigate(): each requirement's ensure()
    call can be given an artificial delay and/or made to raise, so a test can
    prove exactly which calls investigate() waited on and which it did not.
    Mirrors the SimpleNamespace(read=..., ensure=..., repository=...) pattern
    already used by tests/test_di20h_discovery_investigation.py, just made
    parameterizable for timing/failure control."""

    def __init__(self, readiness, *, delays=None, fail=None):
        self.readiness = readiness
        self.delays = dict(delays or {})
        self.fail = dict(fail or {})
        self.fail_full_read_from_now = False
        self.observations = []
        self.repository = SimpleNamespace(record_acquisition_observation=self._record_observation)

    async def _record_observation(self, *args, **kwargs):
        self.observations.append((args, kwargs))

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        # fail_full_read_from_now is only ever flipped on by a test *after*
        # investigate() has already returned, so it can only affect reads
        # issued by a still-running background task -- never investigate()'s
        # own synchronous-path reads.
        if self.fail_full_read_from_now and not evidence_only:
            raise RuntimeError("SIMULATED_POST_RETURN_READ_FAILURE")
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids, identity_headers=None,
                     correlation_id=None, wait_for_completion=True):
        requirement_id = requirement_ids[0]
        delay = self.delays.get(requirement_id, 0)
        if delay:
            await asyncio.sleep(delay)
        if requirement_id in self.fail:
            raise self.fail[requirement_id]
        self.readiness = replace(self.readiness, requirements=tuple(
            replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
            if row.requirement_id == requirement_id else row for row in self.readiness.requirements))
        return TargetedEnsureResult(self.readiness, (requirement_id,), (f"NSE:{requirement_id}",))


async def _drain(background_tasks, **gather_kwargs):
    if not background_tasks:
        return []
    outcomes = await asyncio.gather(*background_tasks, **gather_kwargs)
    background_tasks.clear()
    return outcomes


# H -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_h_slow_current_news_does_not_delay_mandatory_candidate_completion():
    # A deliberately slow CURRENT_NEWS acquisition (far past what a mandatory
    # requirement should ever take) must not add to investigate()'s own
    # wall-clock time once a caller opts in via background_tasks.
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING,
                            "CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    slow_news_seconds = 0.35
    runtime = _FakeInvestigateRuntime(readiness, delays={"CURRENT_NEWS": slow_news_seconds})
    background_tasks = set()
    started = time.monotonic()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    returned_after = time.monotonic() - started
    assert returned_after < slow_news_seconds / 2  # measured timing: see report section 7

    # The mandatory requirement was fully awaited and genuinely completed.
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert "QUARTERLY_FINANCIALS" not in result.failures
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    assert eligibility.blocking_requirements == []

    # CURRENT_NEWS is genuinely still in flight the moment investigate()
    # returned -- not a case that merely happened to finish fast.
    assert len(background_tasks) == 1
    assert matrix["CURRENT_NEWS"]["state"] == "MISSING"

    drain_started = time.monotonic()
    await _drain(background_tasks, return_exceptions=True)
    drained_after = time.monotonic() - drain_started
    assert drained_after >= slow_news_seconds * 0.8  # the background task genuinely ran the full delay

    # Once drained, CURRENT_NEWS's true, truthful outcome is reflected --
    # acquisition was preserved, never skipped or faked.
    assert runtime.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH
    assert matrix["CURRENT_NEWS"]["state"] == "READY_FRESH"
    assert matrix["CURRENT_NEWS"]["failure"] is None


# I -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_i_current_news_technical_failure_stays_visible_and_non_blocking():
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING,
                            "CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    runtime = _FakeInvestigateRuntime(readiness, fail={"CURRENT_NEWS": RuntimeError("PROVIDER_TIMEOUT")})
    background_tasks = set()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    # Mandatory completion and eligibility are already fully unaffected the
    # instant investigate() returns -- before CURRENT_NEWS's own failure is
    # even observed.
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    assert eligibility.blocking_requirements == []

    outcomes = await _drain(background_tasks, return_exceptions=True)
    # The background wrapper itself completes normally: _acquire() catches
    # CURRENT_NEWS's technical failure internally, exactly as the pre-existing
    # inline path always has -- this is not a new/different failure path.
    assert outcomes == [None]

    # The technical failure remains visible/retryable, never silently dropped.
    assert matrix["CURRENT_NEWS"]["failure"] is not None
    assert runtime.observations, "record_acquisition_observation was never called for the failure"
    obs_args = runtime.observations[-1][0]
    assert obs_args[1] == "CURRENT_NEWS" and obs_args[3] == "FAILED"

    # It does not add DEEP_READINESS_NOT_MET by itself, does not make full
    # analysis ineligible, and does not suppress Rule Engine/ranking.
    rule_result = StockRuleEngineV1().evaluate(_inputs(readiness=result.readiness), allow_partial=False)
    incomplete = GlobalOpportunityOrchestrator._incomplete_analysis(rule_result, result.readiness)
    assert incomplete is None
    assert rule_result.eligibility.full_analysis_allowed
    assert not rule_result.partial
    assert "CURRENT_NEWS" not in rule_result.eligibility.blocking_requirements
    assert _ranked(rule_result).rank_eligible


# J -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_j_mandatory_requirement_delay_and_failure_still_fully_block():
    # A slow mandatory requirement is still fully awaited: investigate() does
    # not return until it resolves, background_tasks notwithstanding.
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING})
    slow_mandatory_seconds = 0.2
    runtime = _FakeInvestigateRuntime(readiness, delays={"QUARTERLY_FINANCIALS": slow_mandatory_seconds})
    background_tasks = set()
    started = time.monotonic()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    assert time.monotonic() - started >= slow_mandatory_seconds * 0.8
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    await _drain(background_tasks, return_exceptions=True)

    # A failing mandatory requirement still blocks eligibility outright --
    # background_tasks changes nothing about mandatory semantics.
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING})
    runtime = _FakeInvestigateRuntime(readiness, fail={"QUARTERLY_FINANCIALS": RuntimeError("PROVIDER_DOWN")})
    background_tasks = set()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    assert result.failures.get("QUARTERLY_FINANCIALS") == "RuntimeError"
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert not eligibility.full_analysis_allowed
    assert "QUARTERLY_FINANCIALS" in eligibility.blocking_requirements
    await _drain(background_tasks, return_exceptions=True)


# K -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_k_no_unhandled_task_exception_or_leaked_task_after_drain():
    # Even when CURRENT_NEWS's background wrapper itself fails (not merely the
    # ensure() call inside it, which _acquire() already catches), draining
    # background_tasks the way GlobalOpportunityOrchestrator._acquire_deep's
    # finally: block does -- asyncio.gather(*pending_news_tasks,
    # return_exceptions=True) -- retrieves the exception, so Python's default
    # "Task exception was never retrieved" handler is never invoked and no
    # task is left dangling for the garbage collector to warn about.
    readiness = _readiness({"CURRENT_NEWS": ResearchRequirementStatus.MISSING})
    runtime = _FakeInvestigateRuntime(readiness)
    background_tasks = set()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    assert len(background_tasks) == 1
    task = next(iter(background_tasks))
    # Only now -- after investigate() itself has already returned, so every
    # read() call on investigate()'s own synchronous path is long done --
    # make the background wrapper's own post-acquisition read() raise.
    runtime.fail_full_read_from_now = True

    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, ctx: unhandled.append(ctx))
    try:
        outcomes = await asyncio.gather(*background_tasks, return_exceptions=True)
        background_tasks.clear()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], RuntimeError)
        assert str(outcomes[0]) == "SIMULATED_POST_RETURN_READ_FAILURE"
        assert task.done() and isinstance(task.exception(), RuntimeError)
        del outcomes
        import gc
        gc.collect()
        await asyncio.sleep(0)  # let any pending "exception never retrieved" callback fire, if one exists
    finally:
        loop.set_exception_handler(previous_handler)
    assert unhandled == []
from test_global_opportunity_ranker import inputs as ranker_inputs

POLICY = StockRuleEngineEligibilityPolicy()
ENGINE = StockRuleEngineV1()


def _ranked(result):
    candidate, rule = ranker_inputs()
    return GlobalOpportunityRanker().score(
        candidate, result.model_copy(update={"global_instrument_id": rule.global_instrument_id}))


def _search_run(state: str):
    outcome = ProviderOutcome(provider="web", outcome=state, candidate_count=0, queries_planned=1,
        queries_completed=0 if state in {"FAILED", "DEGRADED"} else 1,
        failure_code="SEARCH_PROVIDER_UNAVAILABLE" if state == "FAILED" else None)
    from datetime import timedelta
    return aggregate_search(INSTRUMENT_ID, [outcome], started_at=NOW - timedelta(minutes=5),
        completed_at=NOW - timedelta(minutes=1), qualifying_events=0)


def _assert_fully_unblocked(status):
    readiness = _readiness({"CURRENT_NEWS": status})

    # Eligibility policy gate.
    eligibility = POLICY.evaluate(readiness)
    assert eligibility.full_analysis_allowed
    assert "CURRENT_NEWS" not in eligibility.blocking_requirements

    # Rule Engine evaluation. (Default fixture events/facts -- not overridden
    # to an empty events tuple -- so only CURRENT_NEWS varies; an empty events
    # tuple would separately make ORDER_BOOK_CAPACITY_CATALYSTS UNSCORABLE,
    # which is an unrelated, still-blocking area and not what this proves.)
    result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=False)
    assert result.eligibility.full_analysis_allowed
    assert not result.partial
    assert result.overall_score is not None
    assert result.decision_signal != DecisionSignal.INSUFFICIENT_DATA
    assert "CURRENT_NEWS" not in result.eligibility.blocking_requirements

    # Orchestration completeness gate -- no DEEP_READINESS_NOT_MET solely for news.
    incomplete = GlobalOpportunityOrchestrator._incomplete_analysis(result, readiness)
    assert incomplete is None

    # Ranker eligibility.
    scored = _ranked(result)
    assert scored.rank_eligible
    assert "CURRENT_NEWS" not in scored.eligibility_reasons
    return result, eligibility, scored


# A -----------------------------------------------------------------------------
def test_a_current_news_missing_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.MISSING)


# B -----------------------------------------------------------------------------
def test_b_current_news_partial_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.PARTIAL)


# C -----------------------------------------------------------------------------
def test_c_current_news_failed_alone_never_blocks():
    _assert_fully_unblocked(ResearchRequirementStatus.FAILED)


# D -----------------------------------------------------------------------------
def test_d_usable_ready_news_behavior_is_unchanged():
    # READY_FRESH / READY_STALE CURRENT_NEWS is untouched by this change: it
    # was never blocking before and remains fully scorable/usable now, and a
    # real qualifying event still contributes to the NEWS area score exactly
    # as before.
    for status in (ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE):
        readiness = _readiness({"CURRENT_NEWS": status})
        eligibility = POLICY.evaluate(readiness)
        assert eligibility.full_analysis_allowed
        assert "CURRENT_NEWS" not in eligibility.blocking_requirements
    result = ENGINE.evaluate(_inputs(readiness=_readiness()), allow_partial=False)
    news_area = next(item for item in result.area_scores if item.area == "NEWS_GEOPOLITICAL_EVENTS")
    assert news_area.raw_score is not None  # the default fixture's qualifying event still scores.
    assert result.eligibility.full_analysis_allowed and not result.partial


# E -----------------------------------------------------------------------------
def test_e_success_empty_news_remains_truthful_and_non_blocking():
    # A completed zero-result news check (SUCCESS_EMPTY) stays an explicit,
    # truthful READY area with no fabricated event/metric -- this was already
    # non-blocking, and this test pins that SUCCESS_EMPTY's *meaning* is
    # unchanged by the CURRENT_NEWS optional-eligibility correction.
    from datetime import timedelta
    from test_stock_rule_engine import _event
    old_catalyst = _event(at=NOW - timedelta(days=90))  # outside the 30-day news window
    value = replace(_inputs(events=(old_catalyst,)), news_search_run=_search_run("SUCCESS_EMPTY"))
    news = ENGINE._news(value)
    assert news.status == AreaScoreStatus.READY_FRESH
    assert news.raw_score is None and news.metrics == []  # zero-result stays zero-result, never fabricated
    assert not any(str(old_catalyst.event_id) in ref for ref in news.evidence_references)
    result = ENGINE.evaluate(value, allow_partial=False)
    assert result.eligibility.full_analysis_allowed and not result.partial and result.overall_score is not None


# F -----------------------------------------------------------------------------
def test_f_missing_mandatory_fundamental_requirement_still_blocks():
    # Proof that the correction is scoped to CURRENT_NEWS only: any other
    # mandatory/applicable fundamental requirement continues to block exactly
    # as before, even when CURRENT_NEWS itself is fully READY.
    for requirement_id in ("LATEST_PRICE", "SHAREHOLDING", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"):
        readiness = _readiness({requirement_id: ResearchRequirementStatus.MISSING})
        eligibility = POLICY.evaluate(readiness)
        assert not eligibility.full_analysis_allowed
        assert requirement_id in eligibility.blocking_requirements
        result = ENGINE.evaluate(_inputs(readiness=readiness), allow_partial=False)
        assert result.partial and result.overall_score is None
        assert not _ranked(result).rank_eligible


# G -----------------------------------------------------------------------------
def test_g_current_news_diagnostics_remain_visible_despite_non_blocking_eligibility():
    # CURRENT_NEWS absence/failure stays truthfully visible in readiness state
    # and in the NEWS area's own status/missing_inputs -- only its power to
    # gate eligibility/ranking was removed. Uses the NEWS area rule directly
    # (with no qualifying event in scope) so this is isolated from the
    # unrelated ORDER_BOOK_CAPACITY_CATALYSTS area, which also goes UNSCORABLE
    # on an empty events tuple and is not part of what this proves.
    for status in (ResearchRequirementStatus.MISSING, ResearchRequirementStatus.PARTIAL, ResearchRequirementStatus.FAILED):
        readiness = _readiness({"CURRENT_NEWS": status})
        assert readiness.for_requirement("CURRENT_NEWS").status == status  # truthful readiness state preserved
        value = replace(_inputs(events=[]), readiness=readiness)
        news_area = ENGINE._news(value)
        assert news_area.status == AreaScoreStatus.UNSCORABLE  # still visible as unscored, not silently hidden
        assert "RELEVANT_CURRENT_EVENT_WITHIN_30_DAYS" in news_area.missing_inputs  # still surfaced in diagnostics
        # Eligibility/ranking are nonetheless fully unblocked by CURRENT_NEWS alone.
        eligibility = POLICY.evaluate(readiness)
        assert eligibility.full_analysis_allowed
        assert "CURRENT_NEWS" not in eligibility.blocking_requirements


# L -----------------------------------------------------------------------------
# Orchestration-level, end-to-end proof (not just the unit-level gates proven
# above): a Stage2 candidate with every mandatory requirement READY_FRESH but
# CURRENT_NEWS FAILED must come out of GlobalOpportunityOrchestrator.run()
# itself as deep-ready / rank-eligible, WITHOUT triggering a repair pass --
# this is the "REPAIR INTERACTION" contract: CURRENT_NEWS failing alone must
# never cost a repair attempt, since repair can never recover it (see Issue
# B's earlier repair-effectiveness diagnosis: repair is a blind retry of the
# identical acquisition call).

import httpx as _httpx
from unittest.mock import AsyncMock as _AsyncMock
from app.deep_investigation import build_plan as _build_plan
from app.global_scanner import GlobalScanner as _GlobalScanner
from test_global_opportunity_baseline import setup_acquisition as _setup_acquisition
from test_global_opportunity_ranker import inputs as _ranker_inputs_full


@pytest.mark.asyncio
async def test_l_current_news_failure_alone_is_deep_ready_and_never_repaired(monkeypatch):
    service, rows, pairs, store, tracker = _setup_acquisition(monkeypatch, count=1)
    candidate_id = next(iter(pairs))
    pairs[candidate_id] = _ranker_inputs_full(1, core=90, confidence=90)

    readiness = replace(_readiness({"CURRENT_NEWS": ResearchRequirementStatus.FAILED}),
                        global_instrument_id=candidate_id)

    async def investigate_stub(runtime, key, **kwargs):
        assert key == candidate_id
        result = TargetedEnsureResult(readiness, (), (), failures={"CURRENT_NEWS": "PROVIDER_TIMEOUT"})
        return result, _build_plan(readiness), {"CURRENT_NEWS": {"state": "FAILED", "failure": "PROVIDER_TIMEOUT"}}

    monkeypatch.setattr("app.deep_investigation.investigate", investigate_stub)
    # Any direct runtime.ensure() for this key (outside investigate()) would
    # be a sign CURRENT_NEWS's own acquisition reached a code path this test
    # does not intend to exercise.
    forbidden = _AsyncMock(side_effect=AssertionError("Unexpected direct ensure() call"))
    monkeypatch.setattr(_httpx.AsyncClient, "send", forbidden)

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, discovery_v2=True)

    diagnostic = next(d for d in result.diagnostics if d.global_instrument_id == candidate_id)
    # Deep-ready / rank-eligible despite CURRENT_NEWS's own technical failure.
    assert diagnostic.disposition == "ANALYZED"
    assert diagnostic.rank_eligible is True
    assert diagnostic.status == "RANK_ELIGIBLE"
    assert result.deep_ready_count == 1
    assert result.rank_eligible_count == 1
    # Never classified as a readiness/technical failure, and never repaired,
    # because of CURRENT_NEWS alone.
    assert result.deep_readiness_failed_count == 0
    assert result.deep_technical_failure_count == 0
    assert result.deep_repair_attempted_count == 0
    # CURRENT_NEWS's own failure remains visible in the investigation matrix
    # (observability preserved -- never hidden).
    assert result.investigation_matrix[str(candidate_id)]["requirements"]["CURRENT_NEWS"]["failure"] == "PROVIDER_TIMEOUT"


@pytest.mark.asyncio
async def test_m_genuinely_mandatory_failure_still_triggers_repair_unaffected(monkeypatch):
    # Control: a candidate failing a genuinely mandatory requirement (not
    # CURRENT_NEWS) still goes through DEEP_READINESS_NOT_MET / repair exactly
    # as before -- this change must not touch that existing behavior.
    service, rows, pairs, store, tracker = _setup_acquisition(monkeypatch, count=1)
    candidate_id = next(iter(pairs))
    pairs[candidate_id] = _ranker_inputs_full(1, core=90, confidence=90)

    attempts = {"n": 0}
    readiness = replace(_readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING}),
                        global_instrument_id=candidate_id)

    async def investigate_stub(runtime, key, **kwargs):
        attempts["n"] += 1
        result = TargetedEnsureResult(readiness, (), (), failures={"QUARTERLY_FINANCIALS": "TimeoutError"})
        return result, _build_plan(readiness), {}

    monkeypatch.setattr("app.deep_investigation.investigate", investigate_stub)
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, discovery_v2=True)

    assert result.deep_ready_count == 0
    assert result.deep_readiness_failed_count == 1
    assert result.deep_repair_attempted_count == 1  # unchanged existing repair behavior
    assert attempts["n"] == 2  # original attempt + one repair pass
