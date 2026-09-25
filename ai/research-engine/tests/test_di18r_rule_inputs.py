"""Offline DI-18R rule-input regressions; no summary/UI dependency."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.fact_precedence import FactSourceTier
from app.stock_rule_engine import StockRuleEngineInputAdapter, StockRuleEngineV1
from app.persistence import SqliteResearchPersistence
from test_stock_rule_engine import _fact, _inputs, _structured, _readiness, _profile, NOW


def fact(metric, number, period="2026-03-31", kind="ANNUAL", basis="CONSOLIDATED", unit="INR", official=True):
    f = _fact(metric, Decimal(str(number)), datetime.fromisoformat(period).replace(tzinfo=timezone.utc), kind)
    return replace(f, key=replace(f.key, reporting_basis=basis),
        value=f.value.model_copy(update={"unit": unit}),
        source_tier=FactSourceTier.OFFICIAL_NSE if official else FactSourceTier.YAHOO,
        source_provider="NSE" if official else "YAHOO_FINANCE")


def metrics(method, facts, structured=()):
    return {m.metric: m for m in getattr(StockRuleEngineV1(), method)(_inputs(facts=facts, structured=structured)).metrics}


def test_debt_and_ocf_survive_persistence_and_actual_input_adapter(tmp_path):
    db = SqliteResearchPersistence(str(tmp_path / "facts.db"))
    rows = [fact("total_debt", 80), fact("equity", 200), fact("cash_and_cash_equivalents", 20),
            fact("operating_cash_flow", 30), fact("pat", 20)]
    for row in rows:
        db.upsert_financial_fact(row)
    db.upsert_financial_fact(fact("total_debt", 999, official=False))
    profile = _profile()
    repo = SimpleNamespace(
        financial_facts_for_instruments=AsyncMock(return_value={profile.instrument_id: db.load_financial_facts({profile.instrument_id})}),
        structured_market_snapshots_for_instruments=AsyncMock(return_value={profile.instrument_id: [_structured()]}),
        market_price_observations_for_instruments=AsyncMock(return_value={}),
        events_for=lambda *a, **k: [], shareholding_for=lambda *a, **k: [])
    adapter = StockRuleEngineInputAdapter(repo, SimpleNamespace(canonical_metadata_for=lambda _: {"canonicalSector": "Industrials"}))
    value = asyncio.run(adapter.load(profile, _readiness(), now=NOW))
    balance = {m.metric: m for m in StockRuleEngineV1()._balance_sheet(value).metrics}
    quality = {m.metric: m for m in StockRuleEngineV1()._quality(value).metrics}
    assert balance["DEBT_TO_EQUITY"].value == 40
    assert balance["NET_DEBT_TO_EQUITY"].value == 30
    assert quality["CASH_CONVERSION"].value == 1.5
    assert balance["DEBT_TO_EQUITY"].source == "NSE"
    assert len(value.financial_facts) == len(rows)


@pytest.mark.parametrize("kwargs", [{"basis": "STANDALONE"}, {"basis": "UNKNOWN"}, {"basis": None},
    {"kind": "QUARTERLY"}, {"unit": "USD"}, {"period": "2025-03-31"}])
def test_incompatible_debt_or_ocf_never_derive(kwargs):
    assert "DEBT_TO_EQUITY" not in metrics("_balance_sheet", [fact("total_debt", 80, **kwargs), fact("equity", 200)])
    assert "CASH_CONVERSION" not in metrics("_quality", [fact("operating_cash_flow", 30, **kwargs), fact("pat", 20)])
    assert "FCF_QUALITY" not in metrics("_quality", [fact("free_cash_flow", 30, **kwargs), fact("pat", 20)])


def test_missing_stays_missing_and_provider_ratio_remains_fallback():
    assert not metrics("_balance_sheet", [])
    assert not metrics("_quality", [])
    assert metrics("_balance_sheet", [], [_structured()])["DEBT_TO_EQUITY"].value == 35
    assert "NET_DEBT_TO_EQUITY" not in metrics("_balance_sheet", [fact("equity", 200)], [_structured()])


@pytest.mark.parametrize("metric,name,summary", [("roe", "ROE", "roe"), ("roce", "ROCE", "roce"),
    ("operating_margin", "OPERATING_MARGIN", "operatingMargin"), ("net_margin", "NET_MARGIN", "profitMargin")])
def test_explicit_official_ratios_outrank_summary(metric, name, summary):
    result = metrics("_quality", [fact(metric, 18, unit="PERCENT"), fact(metric, 99, unit="PERCENT", official=False)], [_structured(**{summary: Decimal("0.99")})])
    assert result[name].value == 18
    assert result[name].source == "NSE"


def test_official_annual_fcf_not_masked_by_summary_or_investing_cashflow():
    result = metrics("_quality", [fact("free_cash_flow", 10), fact("pat", 20)], [_structured()])
    assert result["FCF_QUALITY"].value == .5
    assert "FCF_QUALITY" not in metrics("_quality", [fact("investing_cash_flow", 10), fact("pat", 20)])
    assert "FCF_QUALITY" not in metrics("_quality", [fact("free_cash_flow", 10, kind="QUARTERLY"), fact("pat", 20)])


@pytest.mark.parametrize("metric,name", [("revenue", "REVENUE_YOY"), ("pat", "PAT_YOY"), ("eps", "EPS_YOY")])
def test_same_quarter_yoy_already_derived_and_official_preferred(metric, name):
    rows = [fact(metric, 100, "2025-06-30", "QUARTERLY"), fact(metric, 120, "2026-06-30", "QUARTERLY"),
            fact(metric, 900, "2026-06-30", "QUARTERLY", official=False)]
    assert metrics("_quarterly", rows)[name].value == 20
    assert name not in metrics("_quarterly", [rows[-2], fact(metric, 100, "2026-03-31", "QUARTERLY")])
    # Within the old 300-day tolerance, but in a different prior-year quarter.
    assert name not in metrics("_quarterly", [rows[-2], fact(metric, 100, "2025-08-31", "QUARTERLY")])


@pytest.mark.parametrize("kwargs", [{"basis": "STANDALONE"}, {"basis": "UNKNOWN"}, {"kind": "ANNUAL"}, {"unit": "USD"}])
def test_yoy_rejects_incompatible_prior(kwargs):
    args = dict(period="2025-06-30", kind="QUARTERLY")
    args.update(kwargs)
    rows = [fact("revenue", 120, "2026-06-30", "QUARTERLY"), fact("revenue", 100, **args)]
    assert "REVENUE_YOY" not in metrics("_quarterly", rows)


def test_no_unsafe_roe_roa_ebitda_or_operating_profit_derivation():
    rows = [fact("pat", 20), fact("equity", 200), fact("assets", 300), fact("ebit", 40), fact("finance_cost", 5)]
    assert "ROE" not in metrics("_quality", rows)
    assert "ROA" not in metrics("_quality", rows)
    assert "INTEREST_COVERAGE" not in metrics("_balance_sheet", rows)
    assert "OPERATING_MARGIN" not in metrics("_quality", [fact("ebitda_margin", 20)])


def test_missing_concepts_not_hidden_by_fresh_umbrella():
    value = _inputs()
    readiness = replace(value.readiness, requirements=tuple(replace(r, concept_evidence_states={
        "ORDER_BOOK": "READY_FRESH", "CAPEX": "MISSING", "GUIDANCE": "MISSING"})
        if r.requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE" else r for r in value.readiness.requirements))
    area = StockRuleEngineV1()._catalysts(replace(value, readiness=readiness))
    assert area.status == "PARTIAL"
    assert "CAPACITY_OR_CAPEX_OR_COMMISSIONING" in area.missing_inputs
    assert "MANAGEMENT_GUIDANCE" in area.missing_inputs


def test_derived_authority_does_not_upgrade_secondary_operand():
    result = metrics("_balance_sheet", [fact("total_debt", 80), fact("equity", 200, official=False)])
    assert result["DEBT_TO_EQUITY"].value == 40
    from app.stock_rule_engine import _debt_equity_datum
    datum = _debt_equity_datum(_inputs(facts=[fact("total_debt", 80), fact("equity", 200, official=False)]), {})
    assert datum.authority == 2


def test_compatible_explicit_scale_and_zero_debt_are_not_missing():
    result = metrics("_balance_sheet", [fact("total_debt", 2, unit="INR crore"), fact("equity", 100000000)])
    assert result["DEBT_TO_EQUITY"].value == 20
    assert metrics("_balance_sheet", [fact("total_debt", 0), fact("equity", 200)])["DEBT_TO_EQUITY"].value == 0


def test_yoy_zero_prior_is_missing_and_existing_negative_prior_semantics_retained():
    latest = fact("pat", 20, "2026-06-30", "QUARTERLY")
    assert "PAT_YOY" not in metrics("_quarterly", [fact("pat", 0, "2025-06-30", "QUARTERLY"), latest])
    assert metrics("_quarterly", [fact("pat", -10, "2025-06-30", "QUARTERLY"), latest])["PAT_YOY"].value == 100


def test_na_concepts_do_not_mark_catalyst_area_partial():
    value = _inputs()
    readiness = replace(value.readiness, requirements=tuple(replace(r, concept_evidence_states={
        "ORDER_BOOK": "READY_FRESH", "CAPEX": "NOT_APPLICABLE", "GUIDANCE": "NOT_APPLICABLE"})
        if r.requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE" else r for r in value.readiness.requirements))
    assert StockRuleEngineV1()._catalysts(replace(value, readiness=readiness)).status == "READY_FRESH"


def test_explicit_ttm_fcf_remains_usable_for_yield_only():
    rows = [fact("free_cash_flow", 100, kind="TTM"), fact("pat", 20)]
    assert metrics("_valuation", rows, [_structured()])["FCF_YIELD"].value == 1
    assert "FCF_QUALITY" not in metrics("_quality", rows)


def test_quarterly_operating_margin_does_not_substitute_ebitda():
    revenues = [fact("revenue", 100, "2026-03-31", "QUARTERLY"), fact("revenue", 120, "2026-06-30", "QUARTERLY")]
    ebitda = [fact("ebitda", 20, "2026-03-31", "QUARTERLY"), fact("ebitda", 30, "2026-06-30", "QUARTERLY")]
    assert "OPERATING_MARGIN_TREND" not in metrics("_quarterly", revenues + ebitda)
    operating = [replace(f, key=replace(f.key, metric="operating_profit")) for f in ebitda]
    assert metrics("_quarterly", revenues + operating)["OPERATING_MARGIN_TREND"].value == 5
