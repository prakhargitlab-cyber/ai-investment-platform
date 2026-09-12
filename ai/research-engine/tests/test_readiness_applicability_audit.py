from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import pytest

from app.research_applicability import RequirementApplicability, classify_requirements
from app.research_readiness import ResearchRequirementStatus, ResearchRequirementRegistry
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.market_sessions import MarketTradingSchedule, MarketCalendarException, latest_completed_session, next_session_open
from app.structured_market import _normalize_yfinance_news
from test_research_readiness import complete_snapshot, assess
from test_research_readiness_runtime import _profile, _fact


@pytest.mark.parametrize("industry,expected", [
    ("Banks - Regional", "NOT_APPLICABLE"),
    ("Financial Data & Stock Exchanges", "NOT_APPLICABLE"),
    ("Electrical Equipment & Parts", "APPLICABLE"),
    ("Engineering & Construction", "APPLICABLE"),
    ("Aerospace & Defense", "APPLICABLE"),
    ("Utilities - Regulated Electric", "APPLICABLE"),
    ("Software - Application", "PARTIALLY_APPLICABLE"),
    (None, "UNKNOWN"),
])
def test_business_classification_only_controls_applicability(industry, expected):
    decision = classify_requirements(None, industry, "canonical-reference")["ORDER_BOOK_CAPEX_GUIDANCE"]
    assert decision.state == expected


def test_not_applicable_is_excluded_from_both_denominators_without_fake_coverage():
    snapshot = complete_snapshot(omit={"CURRENT_NEWS", "ORDER_BOOK_CAPEX_GUIDANCE"})
    decisions = {key: RequirementApplicability("NOT_APPLICABLE", "DOMAIN_TEST")
                 for key in ("CURRENT_NEWS", "ORDER_BOOK_CAPEX_GUIDANCE")}
    _, baseline, _ = assess(snapshot)
    _, result, plan = assess(replace(snapshot, applicability_by_requirement=decisions))
    assert baseline.overall_completeness_pct < 100
    assert baseline.critical_completeness_pct < 100
    assert result.overall_completeness_pct == 100
    assert result.critical_completeness_pct == 100
    for key in decisions:
        row = result.for_requirement(key)
        assert row.status == ResearchRequirementStatus.NOT_APPLICABLE
        assert row.evidence_ids == ()
        assert row.covered_input_ids == ()
        assert row.supported_actions == ()
        assert key not in {target.requirement_id for target in plan.targets}


@pytest.mark.parametrize("failure", ["EXTERNAL_CAPABILITY_UNSUPPORTED", "ACQUISITION_TIMEOUT", "NO_DATA"])
def test_provider_failure_cannot_establish_non_applicability(failure):
    snapshot = complete_snapshot(omit={"ORDER_BOOK_CAPEX_GUIDANCE"}, failures={"ORDER_BOOK_CAPEX_GUIDANCE": failure})
    _, result, _ = assess(snapshot)
    assert result.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE").status == ResearchRequirementStatus.FAILED
    assert result.overall_completeness_pct < 100


def test_quarterly_comparison_requires_same_basis_revenue_and_pat_not_duplicate_date_strings():
    profile = _profile()
    values = {item.requirement_id: [] for item in ResearchRequirementRegistry.default().requirements}
    facts = [_fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY"),
             _fact(profile, "pat", "10", "2026-06-30T00:00:00", "QUARTERLY")]
    RepositoryResearchReadinessAdapter._append_financial_evidence(values, facts)
    assert not any("COMPARABLE_QUARTERS" in item.covered_input_ids for item in values["QUARTERLY_FINANCIALS"])
    facts += [_fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY"),
              _fact(profile, "pat", "9", "2026-03-31", "QUARTERLY")]
    RepositoryResearchReadinessAdapter._append_financial_evidence(values, facts)
    assert any("COMPARABLE_QUARTERS" in item.covered_input_ids for item in values["QUARTERLY_FINANCIALS"])
    assert any("PROFITABILITY_HISTORY" in item.covered_input_ids for item in values["BUSINESS_QUALITY_FACTS"])


def test_session_validity_uses_persisted_weekend_and_holiday_calendar():
    schedules = [MarketTradingSchedule("NSE", "XNSE", "IN", "Asia/Kolkata", day, time(9,15), time(15,30)) for day in range(5)]
    friday = datetime(2026,9,11,10,0,tzinfo=timezone.utc)
    assert latest_completed_session("XNSE", schedules, [], friday) == friday
    assert next_session_open("XNSE", schedules, [], friday) == datetime(2026,9,14,3,45,tzinfo=timezone.utc)
    holiday = MarketCalendarException("NSE", datetime(2026,9,14).date(), "CLOSED")
    assert next_session_open("XNSE", schedules, [holiday], friday) == datetime(2026,9,15,3,45,tzinfo=timezone.utc)
    assert next_session_open("UNKNOWN", schedules, [], friday) is None


def test_yahoo_iso_news_timestamp_is_preserved_not_replaced_with_retrieval_time():
    retrieved = datetime(2026,9,12,12,tzinfo=timezone.utc)
    result = _normalize_yfinance_news([{"content": {"title":"Reported event", "canonicalUrl":{"url":"https://example.test/event"}, "pubDate":"2026-09-11T10:00:00Z"}}], retrieved)
    assert result[0]["publishedAt"] == datetime(2026,9,11,10,tzinfo=timezone.utc)
    assert result[0]["publishedAt"] != retrieved

@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "-Infinity"])
def test_absent_or_nonfinite_statement_cells_are_not_financial_facts(value):
    from app.structured_market import _decimal
    assert _decimal(value) is None
