"""Focused regression tests for the GROWTH_FACTS quarterly-trend coverage fix.

Root cause (see app/research_readiness_runtime.py's
RepositoryResearchReadinessAdapter._append_financial_evidence): the quarterly
revenue<->earnings intersection used for GROWTH_FACTS.QUARTERLY_YOY_QOQ_TRENDS
(and QUARTERLY_FINANCIALS.COMPARABLE_QUARTERS, which shares the same computed
intersection) was keyed by (reporting_basis, normalized physical unit).
Revenue and earnings are legitimately different physical measures -- e.g. an
NSE issuer's revenue in "INR" versus its EPS in "INR per share" -- so keying
by unit made the intersection permanently empty for any issuer whose revenue
and earnings units differ, even when both had ample real, overlapping
quarters. A second, compounding defect: EPS was not classified into the same
"earnings" metric family as PAT/net_income/net_profit for this specific
intersection (even though EARNINGS_HISTORY elsewhere already treats EPS as
earnings), so an issuer reporting EPS but no separate PAT/net-income line
(KPIGREEN's NSE quarterly filings) could never produce ANY qualifying
earnings-family periods to intersect against revenue.

The fix tracks each (reporting_basis, metric-family) quarterly period set
PER UNIT, and uses each family's own largest single-unit bucket (its
internally unit-consistent series) for the cross-family intersection. This:
  - lets revenue (INR) and earnings/EPS (INR per share) intersect on shared
    calendar quarters despite differing units,
  - still refuses to call two quarters of the SAME metric family comparable
    when they were reported in genuinely different, non-normalizing units
    (e.g. a USD quarter vs an INR quarter) -- see
    test_nse_data_quality_foundation.py::test_quarterly_readiness_respects_normalized_currency,
    which continues to pass unmodified,
  - never mixes CONSOLIDATED and STANDALONE reporting bases (basis stays the
    outer grouping key),
  - never mixes QUARTERLY and ANNUAL period types (they were already, and
    remain, entirely separate dicts),
  - never lets a single metric family (revenue-only, or earnings-only)
    manufacture cross-metric trend coverage on its own (the intersection of
    a real set with an empty set is always empty).

Provider-free: no real NSE/Yahoo/network calls, no opportunity cycle, no DB
reset, no deploy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import ProvenancedValue, SourceMode
from app.research_readiness import (
    ResearchReadinessService,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
)
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from test_di12a_quarterly_financial_readiness_integrity import NOW, _Repo, _adapter, _profile


def _fact(
    profile, metric, value, period_end, period_type, *,
    reporting_basis="CONSOLIDATED", unit="INR", provider="NSE",
    tier=FactSourceTier.OFFICIAL_NSE,
) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(profile.instrument_id, metric, period_end, period_type, reporting_basis),
        ProvenancedValue(
            value=Decimal(value),
            unit=unit,
            as_of_date=datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc),
            source_url=(
                "https://nsearchives.nseindia.com/corporate/quarterly-result.pdf" if provider == "NSE"
                else "https://finance.yahoo.com/quote/EXAMPLE.NS"
            ),
            source_name=provider,
            source_type="EXCHANGE_ANNOUNCEMENT" if provider == "NSE" else "STRUCTURED_MARKET_PROVIDER",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(hours=1),
            confidence=0.95,
        ),
        tier, provider, f"{provider}-{metric}-{reporting_basis}-{period_end}-{period_type}", SourceMode.REAL,
    )


def _growth_facts_coverage(facts) -> set[str]:
    values = {r.requirement_id: [] for r in ResearchRequirementRegistry.default().requirements}
    RepositoryResearchReadinessAdapter._append_financial_evidence(values, facts)
    return {input_id for row in values["GROWTH_FACTS"] for input_id in row.covered_input_ids}


def _quarterly_financials_coverage(facts) -> set[str]:
    values = {r.requirement_id: [] for r in ResearchRequirementRegistry.default().requirements}
    RepositoryResearchReadinessAdapter._append_financial_evidence(values, facts)
    return {input_id for row in values["QUARTERLY_FINANCIALS"] for input_id in row.covered_input_ids}


# 1. NSE CONSOLIDATED revenue (INR) + EPS (INR per share) over >=2 matching
#    quarters satisfies GROWTH_FACTS quarterly coverage -----------------------
def test_consolidated_revenue_inr_and_eps_inr_per_share_satisfy_quarterly_trend_coverage():
    profile = _profile()
    facts = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", unit="INR"),
        _fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY", unit="INR"),
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", unit="INR per share"),
        _fact(profile, "eps", "4.5", "2026-03-31", "QUARTERLY", unit="INR per share"),
    ]
    covered = _growth_facts_coverage(facts)
    assert "REVENUE_HISTORY" in covered
    assert "EARNINGS_HISTORY" in covered
    assert "QUARTERLY_YOY_QOQ_TRENDS" in covered
    # The same underlying intersection also feeds QUARTERLY_FINANCIALS.
    assert "COMPARABLE_QUARTERS" in _quarterly_financials_coverage(facts)


# 2. STANDALONE and CONSOLIDATED periods must not be mixed to manufacture
#    coverage -----------------------------------------------------------------
def test_standalone_and_consolidated_quarters_are_not_combined():
    profile = _profile()
    facts = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", reporting_basis="CONSOLIDATED", unit="INR"),
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", reporting_basis="CONSOLIDATED", unit="INR per share"),
        # Only STANDALONE has a second, EARLIER matching quarter -- a basis
        # mix-up would let it complete CONSOLIDATED's lone quarter to 2.
        _fact(profile, "revenue", "80", "2026-03-31", "QUARTERLY", reporting_basis="STANDALONE", unit="INR"),
        _fact(profile, "eps", "4", "2026-03-31", "QUARTERLY", reporting_basis="STANDALONE", unit="INR per share"),
    ]
    assert "QUARTERLY_YOY_QOQ_TRENDS" not in _growth_facts_coverage(facts)


# 3. Revenue-only history does not manufacture earnings/trend completeness --
def test_revenue_only_history_does_not_manufacture_earnings_or_trend_completeness():
    profile = _profile()
    facts = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", unit="INR"),
        _fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY", unit="INR"),
    ]
    covered = _growth_facts_coverage(facts)
    assert "REVENUE_HISTORY" in covered
    assert "EARNINGS_HISTORY" not in covered
    assert "QUARTERLY_YOY_QOQ_TRENDS" not in covered


# 4. Earnings-only history does not manufacture revenue/trend completeness --
def test_earnings_only_history_does_not_manufacture_revenue_or_trend_completeness():
    profile = _profile()
    facts = [
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", unit="INR per share"),
        _fact(profile, "eps", "4.5", "2026-03-31", "QUARTERLY", unit="INR per share"),
    ]
    covered = _growth_facts_coverage(facts)
    assert "EARNINGS_HISTORY" in covered
    assert "REVENUE_HISTORY" not in covered
    assert "QUARTERLY_YOY_QOQ_TRENDS" not in covered


# 5. Different period types (QUARTERLY vs ANNUAL) do not get mixed ----------
def test_quarterly_and_annual_period_types_are_never_mixed():
    profile = _profile()
    facts = [
        # Only ONE quarterly period and ONE annual period per metric -- if
        # period types were ever conflated, that would look like 2 periods.
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", unit="INR"),
        _fact(profile, "revenue", "360", "2025-03-31", "ANNUAL", unit="INR"),
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", unit="INR per share"),
        _fact(profile, "eps", "18", "2025-03-31", "ANNUAL", unit="INR per share"),
    ]
    covered = _growth_facts_coverage(facts)
    assert "QUARTERLY_YOY_QOQ_TRENDS" not in covered
    assert "ANNUAL_CAGR_INPUTS" not in covered


# 6. Existing same-unit Yahoo revenue/PAT behavior remains valid (INFY-style:
#    reporting_basis UNKNOWN, revenue and PAT share the same currency unit) -
def test_existing_same_unit_yahoo_revenue_and_pat_quarterly_trend_still_works():
    profile = _profile()
    facts = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", reporting_basis="UNKNOWN",
              unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO),
        _fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY", reporting_basis="UNKNOWN",
              unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO),
        _fact(profile, "pat", "12", "2026-06-30", "QUARTERLY", reporting_basis="UNKNOWN",
              unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO),
        _fact(profile, "pat", "10", "2026-03-31", "QUARTERLY", reporting_basis="UNKNOWN",
              unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO),
    ]
    covered = _growth_facts_coverage(facts)
    assert {"REVENUE_HISTORY", "EARNINGS_HISTORY", "QUARTERLY_YOY_QOQ_TRENDS"} <= covered


# 7. Full GROWTH_FACTS becomes ready only when all four required inputs are
#    genuinely covered (adds annual coverage on top of test 1's quarters) ---
def test_growth_facts_ready_only_when_all_four_inputs_genuinely_covered():
    profile = _profile()
    quarterly_only = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", unit="INR"),
        _fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY", unit="INR"),
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", unit="INR per share"),
        _fact(profile, "eps", "4.5", "2026-03-31", "QUARTERLY", unit="INR per share"),
    ]
    partial_repo = _Repo(profile, facts=quarterly_only)
    partial_result = ResearchReadinessService(_adapter(partial_repo)).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    # REVENUE_HISTORY, EARNINGS_HISTORY and QUARTERLY_YOY_QOQ_TRENDS are
    # covered, but ANNUAL_CAGR_INPUTS has no annual evidence at all yet.
    assert partial_result.for_requirement("GROWTH_FACTS").status != ResearchRequirementStatus.READY_FRESH

    # Annual periods dated within the GROWTH_FACTS freshness window (its
    # freshness_policy_id is "QUARTERLY_FINANCIALS", maximum_age=120 days,
    # and NOW is 2026-09-10) so this test isolates coverage completeness
    # from freshness staleness -- a separate, already-correct axis.
    full_facts = quarterly_only + [
        _fact(profile, "revenue", "360", "2026-08-31", "ANNUAL", unit="INR"),
        _fact(profile, "revenue", "300", "2026-05-20", "ANNUAL", unit="INR"),
        _fact(profile, "eps", "18", "2026-08-31", "ANNUAL", unit="INR per share"),
        _fact(profile, "eps", "15", "2026-05-20", "ANNUAL", unit="INR per share"),
    ]
    full_repo = _Repo(profile, facts=full_facts)
    full_result = ResearchReadinessService(_adapter(full_repo)).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    assert full_result.for_requirement("GROWTH_FACTS").status == ResearchRequirementStatus.READY_FRESH


# 8. INFY-shaped fixture (Yahoo-only, reporting_basis UNKNOWN, 4 annual +
#    5+ quarterly revenue/PAT periods, same unit) produces all four inputs --
def test_infy_shaped_yahoo_fixture_produces_all_four_growth_facts_inputs():
    profile = _profile()
    # Period markers are spaced as periods (quarterly/annual counts matter,
    # not literal calendar spacing) but kept inside GROWTH_FACTS's freshness
    # window (freshness_policy_id "QUARTERLY_FINANCIALS", maximum_age=120
    # days, NOW=2026-09-10) so this test isolates coverage completeness from
    # the orthogonal, already-correct freshness axis.
    quarters = ["2026-08-31", "2026-08-01", "2026-07-01", "2026-06-01", "2026-05-15"]
    years = ["2026-08-31", "2026-08-01", "2026-07-01", "2026-05-20"]
    facts = []
    for i, q in enumerate(quarters):
        facts.append(_fact(profile, "revenue", str(1000 + i * 10), q, "QUARTERLY", reporting_basis="UNKNOWN",
                            unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO))
        facts.append(_fact(profile, "pat", str(100 + i), q, "QUARTERLY", reporting_basis="UNKNOWN",
                            unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO))
    for i, y in enumerate(years):
        facts.append(_fact(profile, "revenue", str(4000 + i * 100), y, "ANNUAL", reporting_basis="UNKNOWN",
                            unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO))
        facts.append(_fact(profile, "pat", str(400 + i * 10), y, "ANNUAL", reporting_basis="UNKNOWN",
                            unit="USD", provider="YAHOO_FINANCE", tier=FactSourceTier.YAHOO))
    covered = _growth_facts_coverage(facts)
    assert {"REVENUE_HISTORY", "EARNINGS_HISTORY", "QUARTERLY_YOY_QOQ_TRENDS", "ANNUAL_CAGR_INPUTS"} <= covered
    repo = _Repo(profile, facts=facts)
    result = ResearchReadinessService(_adapter(repo)).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    assert result.for_requirement("GROWTH_FACTS").status == ResearchRequirementStatus.READY_FRESH


# 9. KPIGREEN-shaped fixture (NSE CONSOLIDATED + STANDALONE, revenue in INR,
#    EPS in INR per share, no separate PAT line) produces all four inputs
#    for EACH basis independently, without mixing them ------------------------
def test_kpigreen_shaped_nse_fixture_produces_all_four_growth_facts_inputs_per_basis():
    profile = _profile()
    # As above: period markers stay inside GROWTH_FACTS's 120-day freshness
    # window so this test isolates coverage completeness from freshness.
    quarters = ["2026-08-31", "2026-07-15", "2026-06-01"]
    years = ["2026-08-31", "2026-05-20"]
    facts = []
    for basis in ("CONSOLIDATED", "STANDALONE"):
        for i, q in enumerate(quarters):
            facts.append(_fact(profile, "revenue", str(500 + i * 5), q, "QUARTERLY",
                                reporting_basis=basis, unit="INR"))
            facts.append(_fact(profile, "eps", str(10 + i), q, "QUARTERLY",
                                reporting_basis=basis, unit="INR per share"))
        for i, y in enumerate(years):
            facts.append(_fact(profile, "revenue", str(2000 + i * 50), y, "ANNUAL",
                                reporting_basis=basis, unit="INR"))
            facts.append(_fact(profile, "eps", str(40 + i), y, "ANNUAL",
                                reporting_basis=basis, unit="INR per share"))
    covered = _growth_facts_coverage(facts)
    assert {"REVENUE_HISTORY", "EARNINGS_HISTORY", "QUARTERLY_YOY_QOQ_TRENDS", "ANNUAL_CAGR_INPUTS"} <= covered
    repo = _Repo(profile, facts=facts)
    result = ResearchReadinessService(_adapter(repo)).assess(profile.instrument_id, jurisdiction="INDIA", now=NOW)
    assert result.for_requirement("GROWTH_FACTS").status == ResearchRequirementStatus.READY_FRESH


# 10. The currency-consistency invariant this fix must NOT weaken: two
#     quarters of the SAME metric family in genuinely different,
#     non-normalizing units are still not "comparable" ----------------------
def test_same_metric_family_across_incompatible_units_still_not_comparable():
    profile = _profile()
    facts = [
        _fact(profile, "revenue", "100", "2026-06-30", "QUARTERLY", unit="INR"),
        _fact(profile, "revenue", "90", "2026-03-31", "QUARTERLY", unit="USD"),
        _fact(profile, "eps", "5", "2026-06-30", "QUARTERLY", unit="INR per share"),
        _fact(profile, "eps", "4.5", "2026-03-31", "QUARTERLY", unit="USD per share"),
    ]
    assert "QUARTERLY_YOY_QOQ_TRENDS" not in _growth_facts_coverage(facts)
