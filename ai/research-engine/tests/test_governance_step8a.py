"""STEP 8A -- GOVERNANCE_HISTORY root-cause fix regression tests.

Covers the three defects fixed together:

1. Rule Engine input mapping (app/stock_rule_engine.py::_governance): the
   MANAGEMENT_GOVERNANCE area was scored from `value.events` only, ignoring
   readiness's own determination that a persisted governance DOCUMENT already
   satisfies GOVERNANCE_EVIDENCE (research_readiness_runtime._append_documents).
   This is exactly the Step-7 condition: GOVERNANCE_HISTORY READY_FRESH from a
   document while events stay empty -> RULE_AREA_UNSCORABLE.

2. Missing VERIFIED_AUTHORITATIVE_CLEAN_CHECK evidence
   (app/research_readiness_runtime.py): an authoritative NSE governance-
   category check that completed with zero relevant events was durably
   recorded (SUCCESS_EMPTY, app/repository.py) but never converted into
   scorable evidence. Fixed by mirroring the existing ORDER_BOOK_CAPEX_GUIDANCE
   catalyst-check / CURRENT_NEWS clean-search precedent
   (_completed_authoritative_check), which already refuses anything but a
   completed NSE-provider check.

3. Partial-coverage guard (app/repository.py): a shared acquisition budget
   exhausted mid-check must not let a zero-result governance check be
   recorded as SUCCESS_EMPTY (that would wrongly become "verified clean").
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.models import EventImpact, ResearchEventType
from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.stock_rule_engine import AreaScoreStatus, StockRuleEngineV1

from test_governance_acquisition import _profile as _gov_profile, _repo as _gov_repo
from test_news_intelligence_v2 import NOW as NEWS_NOW, outcome as news_outcome, run as news_run
from test_research_readiness_runtime import DurableRepositoryFixture, _profile as _readiness_profile
from test_stock_rule_engine import NOW as SRE_NOW, _event, _inputs, _readiness, _shareholding

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _governance_observation(outcome, *, provider="NSE", failure_reason=None, observed_at=None):
    return {
        "requirement_id": "GOVERNANCE_HISTORY",
        "provider": provider,
        "outcome": outcome,
        "observed_at": (observed_at or NOW).isoformat(),
        "failure_reason": failure_reason,
        "evidence_count": 0,
    }


# 1 -- persisted valid governance document makes the area scorable ------------
def test_01_persisted_governance_document_makes_area_scorable():
    # _readiness() defaults GOVERNANCE_HISTORY to READY_FRESH -- the exact
    # readiness state a persisted governance document (not an event) produces
    # via _append_documents. No governance event, no shareholding pledge: the
    # OLD _governance() would return RULE_AREA_UNSCORABLE here.
    result = StockRuleEngineV1().evaluate(
        _inputs(events=(), shareholding=(), readiness=_readiness()), allow_partial=True)
    area = next(item for item in result.area_scores if str(item.area) == "MANAGEMENT_GOVERNANCE")
    assert area.status != AreaScoreStatus.UNSCORABLE
    assert area.missing_inputs == []


# 12 -- direct regression reproducing the Step-7 condition --------------------
def test_12_step7_regression_ready_fresh_document_no_events_is_no_longer_unscorable():
    result = StockRuleEngineV1()._governance(_inputs(events=(), shareholding=(), readiness=_readiness()))
    assert result.status != AreaScoreStatus.UNSCORABLE, (
        "GOVERNANCE_HISTORY READY_FRESH from a persisted document with empty "
        "events must not stay RULE_AREA_UNSCORABLE (Step-7 blocker)")
    assert result.missing_inputs == []


# 2 -- authoritative zero-event governance check is scorable verified-clean ---
def test_02_authoritative_zero_event_check_is_verified_clean():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [_governance_observation("SUCCESS_EMPTY")]
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    governance = result.for_requirement("GOVERNANCE_HISTORY")
    assert governance.status == ResearchRequirementStatus.READY_FRESH
    assert "GOVERNANCE_EVIDENCE" not in governance.missing_input_ids


# 3 -- generic search SUCCESS_EMPTY does NOT become verified clean ------------
def test_03_generic_search_success_empty_is_not_verified_clean():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [
        _governance_observation("SUCCESS_EMPTY", provider="GLOBAL_NEWS_SEARCH")]
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    governance = result.for_requirement("GOVERNANCE_HISTORY")
    assert governance.status not in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}
    assert "GOVERNANCE_EVIDENCE" in governance.missing_input_ids


# 4 -- provider timeout stays unscorable/unknown -------------------------------
def test_04_timeout_stays_unscorable():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [
        _governance_observation("FAILED", failure_reason="NETWORK_TIMEOUT")]
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    governance = result.for_requirement("GOVERNANCE_HISTORY")
    assert governance.status not in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}
    area = StockRuleEngineV1()._governance(_inputs(events=(), shareholding=(), readiness=result))
    assert area.status == AreaScoreStatus.UNSCORABLE


# 5 -- CAPTCHA / 403 / 429 stay unscorable/unknown -----------------------------
@pytest.mark.parametrize("reason", ["CAPTCHA_CHALLENGE", "HTTP_403", "HTTP_429"])
def test_05_captcha_and_rate_limit_stay_unscorable(reason):
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [_governance_observation("FAILED", failure_reason=reason)]
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    governance = result.for_requirement("GOVERNANCE_HISTORY")
    assert governance.status not in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}


# 6 -- partial authoritative coverage (shared budget exhausted) is NOT clean --
def test_06_partial_budget_exhausted_coverage_is_not_recorded_clean():
    repo = _gov_repo()
    profile = _gov_profile()

    async def empty_discover(*_a, **_k):
        return []
    repo._official_filing_discovery.discover = empty_discover

    async def sufficient():
        return False
    budget = RequirementAcquisitionBudget(
        profile.instrument_id, "ORDER_BOOK_CAPEX_GUIDANCE+SHAREHOLDING+GOVERNANCE_HISTORY",
        NOW, sufficient, max_documents=1, documents_attempted=1)  # already exhausted

    async def run():
        token = _scope.set(budget)
        try:
            await repo._refresh_targeted(
                profile, set(), now=NOW, force=True,
                requested_categories={"REGULATORY", "MANAGEMENT", "RISKS"})
        finally:
            _scope.reset(token)
    asyncio.run(run())
    rows = [row for row in repo.acquisition_observations_for(profile.instrument_id)
            if row.get("requirement_id") == "GOVERNANCE_HISTORY" and row.get("provider") == "NSE"]
    assert rows, "an observation should still be recorded"
    assert rows[-1]["outcome"] != "SUCCESS_EMPTY"
    assert rows[-1]["outcome"] == "FAILED"
    assert rows[-1]["failure_reason"] == "DOCUMENT_BUDGET_EXHAUSTED_PARTIAL_COVERAGE"


# 7 -- fresh persisted clean-check reused without a new provider call ---------
def test_07_persisted_clean_check_reused_without_provider_call():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [_governance_observation("SUCCESS_EMPTY")]
    adapter = RepositoryResearchReadinessAdapter(repo)
    first = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    second = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    assert first.for_requirement("GOVERNANCE_HISTORY").status == ResearchRequirementStatus.READY_FRESH
    assert second.for_requirement("GOVERNANCE_HISTORY").status == ResearchRequirementStatus.READY_FRESH
    assert repo.provider_calls == 0  # nothing here ever calls a provider


# 8 -- an actual negative governance event still affects governance normally --
def test_08_negative_governance_event_still_scores_normally():
    event = _event(event_type=ResearchEventType.REGULATORY_EVENT, impact=EventImpact.STRONG_NEGATIVE,
                    title="SEBI show cause notice", summary="Regulator alleges disclosure lapses.")
    result = StockRuleEngineV1()._governance(_inputs(events=(event,), shareholding=(), readiness=_readiness()))
    assert result.status != AreaScoreStatus.UNSCORABLE
    assert any(metric.metric == "GOVERNANCE_EVIDENCE" for metric in result.metrics)
    assert any(metric.score < 50 for metric in result.metrics if metric.metric == "GOVERNANCE_EVIDENCE")


# 9 -- promoter-pledge governance behavior is unchanged ------------------------
def test_09_promoter_pledge_governance_unchanged():
    result = StockRuleEngineV1()._governance(
        _inputs(events=(), shareholding=_shareholding(), readiness=_readiness()))
    assert result.status != AreaScoreStatus.UNSCORABLE
    assert any(metric.metric == "PROMOTER_PLEDGE_GOVERNANCE" for metric in result.metrics)


# 10 -- CURRENT_NEWS semantics are unchanged -----------------------------------
def test_10_current_news_clean_search_semantics_unchanged():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    search = news_run([news_outcome(state="SUCCESS_EMPTY")]).model_copy(update={"instrument_id": profile.instrument_id})
    repo.news_records_for = lambda *a, **k: [search]
    adapter = RepositoryResearchReadinessAdapter(repo)
    adapter._evaluation_times[profile.instrument_id] = NEWS_NOW
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NEWS_NOW)
    assert result.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH


# 11 -- other mandatory readiness semantics are unchanged ---------------------
def test_11_unrelated_mandatory_requirement_missing_stays_missing():
    profile = _readiness_profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda *a, **k: [_governance_observation("SUCCESS_EMPTY")]
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    # GOVERNANCE_HISTORY becomes verified-clean, but an unrelated mandatory
    # requirement with genuinely zero durable evidence must remain MISSING.
    assert result.for_requirement("VALUATION_INPUTS").status == ResearchRequirementStatus.MISSING
