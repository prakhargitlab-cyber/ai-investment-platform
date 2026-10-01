"""Consolidated fix pass -- Issue 2: mandatory/applicable completeness
contract for the specific fine-grained missing inputs reported in cycle
c49d6c84-6103-4276-95e7-97e8d03150af (rule.partial=false / ANALYZED /
rank_eligible=true stocks whose Rule Engine areas nonetheless list
FCF_YIELD, HISTORICAL_OR_PEER_VALUATION, LIQUIDITY_CURRENT_RATIO_INPUTS,
PROMOTER_PLEDGE, QUARTERLY_MARGINS, SECTOR_PERFORMANCE, PERSISTED_OHLC,
PERSISTED_VOLUME_HISTORY, and ADX/ATR/volume metrics as missing_inputs).

CLASSIFICATION (read directly from ResearchRequirementRegistry.default(),
the ground truth app.research_readiness._classify() gates against --
test_00 below locks this table in as a structural regression, not a
simulated behavior):

  FCF_YIELD                      SUPPORTING  (VALUATION_INPUTS)
  HISTORICAL_OR_PEER_VALUATION   SUPPORTING  (VALUATION_INPUTS)
  LIQUIDITY_CURRENT_RATIO_INPUTS SUPPORTING  (BALANCE_SHEET_FACTS)
  PROMOTER_PLEDGE                SUPPORTING  (SHAREHOLDING)
  QUARTERLY_MARGINS              IMPORTANT   (QUARTERLY_FINANCIALS)
  SECTOR_PERFORMANCE             IMPORTANT   (SECTOR_MACRO)
  PERSISTED_OHLC                 not a readiness input at all
  PERSISTED_VOLUME_HISTORY       not a readiness input at all
  adx14/atr14/atrPct/            not readiness inputs at all
  volumeAverage20/volumeRatio20

app.research_readiness.ResearchReadinessService._classify (~line 1188-1216)
computes `missing_mandatory_inputs` from ONLY the sub-inputs whose
`importance == ResearchRequirementImportance.MANDATORY`, and downgrades a
requirement to PARTIAL (`elif missing_mandatory_inputs: status = PARTIAL`)
ONLY when that set is non-empty. IMPORTANT/SUPPORTING gaps never enter
`mandatory_inputs` at all, so they can never trigger this branch -- a
requirement stays READY_FRESH/READY_STALE despite them, exactly as long as
its own MANDATORY sub-inputs (e.g. BALANCE_SHEET_FACTS's DEBT/EQUITY/CASH,
VALUATION_INPUTS's LATEST_USABLE_PRICE/EARNINGS_BASIS/PB) are covered. This
is not a leak: it is the documented, intentional distinction between (1) the
mandatory/applicable RESEARCH REQUIREMENT contract (readiness) and (2)
optional, finer-grained Rule Engine SCORING inputs -- exactly the
distinction the task explicitly warned against collapsing ("do NOT
automatically make all of these mandatory").

PERSISTED_OHLC/PERSISTED_VOLUME_HISTORY/adx14/atr14/atrPct/
volumeAverage20/volumeRatio20 are a different case entirely: they are not
readiness-registry inputs under ANY tier. LATEST_PRICE and
HISTORICAL_PRICE_SERIES (the two PRICE_TECHNICAL-backing requirements) only
mandate LATEST_USABLE_PRICE and DURABLE_PRICE_OBSERVATIONS/
FIFTY_OBSERVATION_TECHNICAL_BASIS -- a plain price series and a minimum
observation COUNT, never OHLC or volume specifically. OHLC/volume-derived
indicators are Rule-Engine/technical-feature-layer-internal scoring
enrichment (see app/technical_features.py and the prior pass's Section F
finding: price-only technical scoring is intentional, already covered by
tests/test_ohlcv_technical_features.py), entirely outside the
mandatory/applicable research contract. history_readiness=FULL_HISTORY
truthfully measures price-series depth; it never claimed OHLC/volume
completeness.

No leak was found. No production code in research_readiness.py,
stock_rule_engine.py, or global_opportunity_ranker.py was changed for this
issue.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.research_readiness import (
    ResearchRequirementImportance,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
)
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy

from test_research_readiness import GLOBAL_INSTRUMENT_ID, NOW, complete_snapshot, evidence, assess
from app.research_readiness import ResearchEvidence

REGISTRY = ResearchRequirementRegistry.default()
MANDATORY = ResearchRequirementImportance.MANDATORY
IMPORTANT = ResearchRequirementImportance.IMPORTANT
SUPPORTING = ResearchRequirementImportance.SUPPORTING


def _importance(requirement_id: str, input_id: str) -> ResearchRequirementImportance:
    requirement = next(r for r in REGISTRY.requirements if r.requirement_id == requirement_id)
    return next(item.importance for item in requirement.inputs if item.input_id == input_id)


# 0 -- structural: lock in the exact classification table -------------------
@pytest.mark.parametrize("requirement_id,input_id,expected", [
    ("VALUATION_INPUTS", "FCF_YIELD", SUPPORTING),
    ("VALUATION_INPUTS", "HISTORICAL_OR_PEER_VALUATION", SUPPORTING),
    ("BALANCE_SHEET_FACTS", "LIQUIDITY_CURRENT_RATIO_INPUTS", SUPPORTING),
    ("SHAREHOLDING", "PROMOTER_PLEDGE", SUPPORTING),
    ("QUARTERLY_FINANCIALS", "QUARTERLY_MARGINS", IMPORTANT),
    ("SECTOR_MACRO", "SECTOR_PERFORMANCE", IMPORTANT),
])
def test_00_fine_grained_input_importance_tiers_are_as_documented(requirement_id, input_id, expected):
    assert _importance(requirement_id, input_id) == expected


def test_00b_none_of_these_tiers_is_mandatory():
    # The single fact that makes all of them non-blocking: none is MANDATORY.
    for requirement_id, input_id in [
        ("VALUATION_INPUTS", "FCF_YIELD"), ("VALUATION_INPUTS", "HISTORICAL_OR_PEER_VALUATION"),
        ("BALANCE_SHEET_FACTS", "LIQUIDITY_CURRENT_RATIO_INPUTS"), ("SHAREHOLDING", "PROMOTER_PLEDGE"),
        ("QUARTERLY_FINANCIALS", "QUARTERLY_MARGINS"), ("SECTOR_MACRO", "SECTOR_PERFORMANCE"),
    ]:
        assert _importance(requirement_id, input_id) != MANDATORY


def test_00c_ohlc_and_technical_indicator_names_are_not_readiness_inputs_at_all():
    technical_names = {"PERSISTED_OHLC", "PERSISTED_VOLUME_HISTORY", "ADX14", "ATR14", "ATRPCT",
                        "VOLUMEAVERAGE20", "VOLUMERATIO20", "adx14", "atr14", "atrPct",
                        "volumeAverage20", "volumeRatio20"}
    all_input_ids = {item.input_id for requirement in REGISTRY.requirements for item in requirement.inputs}
    assert not (technical_names & all_input_ids)
    # The only two requirements backing PRICE_TECHNICAL mandate a plain price
    # series and an observation COUNT, never OHLC/volume specifically.
    latest_price = next(r for r in REGISTRY.requirements if r.requirement_id == "LATEST_PRICE")
    series = next(r for r in REGISTRY.requirements if r.requirement_id == "HISTORICAL_PRICE_SERIES")
    assert {item.input_id for item in latest_price.inputs} == {"LATEST_USABLE_PRICE"}
    assert {item.input_id for item in series.inputs if item.importance == MANDATORY} == {
        "DURABLE_PRICE_OBSERVATIONS", "FIFTY_OBSERVATION_TECHNICAL_BASIS"}


# 5 -- a genuinely MANDATORY fine-grained input missing suppresses ----------
def test_05_missing_mandatory_input_partial_status_and_blocks_eligibility():
    # BALANCE_SHEET_FACTS evidence covers only EQUITY/CASH -- DEBT (mandatory)
    # is not covered -- everything else in the snapshot stays complete.
    incomplete = replace(evidence("BALANCE_SHEET_FACTS"), covered_input_ids=("EQUITY", "CASH"))
    _, readiness, _ = assess(complete_snapshot(overrides={"BALANCE_SHEET_FACTS": (incomplete,)}))
    result = readiness.for_requirement("BALANCE_SHEET_FACTS")
    assert result.status == ResearchRequirementStatus.PARTIAL
    assert "DEBT" in result.missing_reason
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert "BALANCE_SHEET_FACTS" in eligibility.blocking_requirements


# 6 -- a SUPPORTING/IMPORTANT fine-grained input missing does not -----------
def test_06_missing_supporting_input_alone_stays_ready_fresh_and_eligible():
    # Same requirement, but now DEBT/EQUITY/CASH (all mandatory) ARE
    # covered -- only LIQUIDITY_CURRENT_RATIO_INPUTS (supporting) is not.
    partial_supporting = replace(evidence("BALANCE_SHEET_FACTS"), covered_input_ids=("DEBT", "EQUITY", "CASH"))
    _, readiness, _ = assess(complete_snapshot(overrides={"BALANCE_SHEET_FACTS": (partial_supporting,)}))
    result = readiness.for_requirement("BALANCE_SHEET_FACTS")
    assert result.status == ResearchRequirementStatus.READY_FRESH
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert eligibility.full_analysis_allowed
    assert "BALANCE_SHEET_FACTS" not in eligibility.blocking_requirements


def test_06b_missing_important_input_alone_stays_ready_fresh_and_eligible():
    covered = replace(evidence("QUARTERLY_FINANCIALS"), covered_input_ids=(
        "LATEST_QUARTERLY_RESULT", "COMPARABLE_QUARTERS", "QUARTERLY_REVENUE", "QUARTERLY_PAT", "QUARTERLY_EPS"))
    _, readiness, _ = assess(complete_snapshot(overrides={"QUARTERLY_FINANCIALS": (covered,)}))
    result = readiness.for_requirement("QUARTERLY_FINANCIALS")
    assert result.status == ResearchRequirementStatus.READY_FRESH  # QUARTERLY_MARGINS (important) absent
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert eligibility.full_analysis_allowed


# 9 -- HDFCBANK/LT-style genuine mandatory-unscorable suppression ----------
def test_09_hdfcbank_style_balance_sheet_and_shareholding_gap_stays_suppressed():
    _, readiness, _ = assess(complete_snapshot(omit={"BALANCE_SHEET_FACTS", "SHAREHOLDING"}))
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert {"BALANCE_SHEET_FACTS", "SHAREHOLDING"} <= set(eligibility.blocking_requirements)


def test_09b_lt_style_shareholding_only_gap_stays_suppressed():
    _, readiness, _ = assess(complete_snapshot(omit={"SHAREHOLDING"}))
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert "SHAREHOLDING" in eligibility.blocking_requirements
    # Everything else (in particular BALANCE_SHEET_FACTS) is unaffected.
    assert "BALANCE_SHEET_FACTS" not in eligibility.blocking_requirements
