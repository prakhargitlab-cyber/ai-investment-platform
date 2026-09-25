"""DI-20D focused tests: generic semantic extraction layer for NSE financials.

Tests cover:
  - _financial_number accounting-parentheses negation (DI-20C fix preserved)
  - revenue_from_operations ≠ total_income (no conflation)
  - profit_after_tax ≠ profit_before_tax (PAT ≠ PBT)
  - NCC March fixture: valid multi-basis facts recovered
  - NCC March cash-flow: accounting-parentheses negation (-458.51, -246.68)
  - NCC June fixture: structurally rejected → zero accepted facts
  - IRFC fixtures: EPS values preserved with i→1 OCR repair
  - Cell → column ordinal binding integrity (no left-shift on blanks)
  - Revenue lookbehind: "total revenue from operations" excluded
  - Segment revenue row excluded
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.financial_metric_extraction import (
    ExtractionResult,
    METRIC_CASH_FLOW_FINANCING,
    METRIC_CASH_FLOW_OPERATING,
    METRIC_EPS_BASIC,
    METRIC_EPS_DILUTED,
    METRIC_FINANCE_COSTS,
    METRIC_PROFIT_AFTER_TAX,
    METRIC_PROFIT_BEFORE_TAX,
    METRIC_REVENUE_FROM_OPERATIONS,
    METRIC_TAX,
    METRIC_TOTAL_EXPENSES,
    METRIC_TOTAL_INCOME,
    SemanticFact,
    extract_semantic_facts,
)
from app.structured_research import _financial_number


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "nse"
EVIDENCE_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _ncc_fixture(filename: str) -> str:
    return (FIXTURE_ROOT / "ncc" / filename).read_text(encoding="utf-8")


def _irfc_fixture(filename: str) -> str:
    return (FIXTURE_ROOT / "irfc" / filename).read_text(encoding="utf-8")


# Inline fixture mirroring the tempsens-style annual filing (Statement of
# Profit and Loss, two-column, audited).
TEMPSENS = (
    "Tempsens Instruments (India) Limited CIN L74210RJ1980PLC002293 Regd. Office "
    "Udaipur Rajasthan The Board of Directors of the Company at its meeting held "
    "on 29th May 2026 approved the Audited Financial Statements of the Company "
    "for the financial year ended 31st March 2026. "
    "Statement of Profit and Loss for the year ended 31st March 2026 (Rs. in Lakhs) "
    "Particulars Year Ended 31st March 2026 31st March 2025 "
    "Revenue from operations 15,234.56 13,012.44 "
    "Other income 210.33 180.22 "
    "Total income 15,444.89 13,192.66 "
    "Total expenses 12,100.00 11,000.00 "
    "Profit before tax 3,344.89 2,192.66 "
    "Tax expense 850.00 560.00 "
    "Profit for the period/year 2,494.89 1,632.66 "
    "Basic Earnings Per Share 9.87 6.45 "
    "Diluted Earnings Per Share 9.85 6.43 "
    "Notes to the financial statements form an integral part of these results."
)

# NSE-format text with "Total revenue from operations" and "Segment revenue"
# rows added to Zodiac-style structure for lookbehind / exclusion tests.
_ZODIAC_WITH_EXTRAS = (
    "Statement Of Unaudited Standalone Financial Results For the Quarter ended "
    "On 30th June, 2026 (Rs. in Lakhs) Particulars Quarter Ended Year Ended "
    "30th June 2026 31st March 2026 30th June 2025 31st March 2026 "
    "Revenue From Operations 125.50 118.00 104.25 450.00 "
    "Total revenue from operations 126.00 119.00 105.00 455.00 "
    "Segment revenue 50.00 48.00 46.00 190.00 "
    "Total Income 128.00 120.00 106.00 458.00 "
    "Net Profit/(Loss) After Tax 9.75 8.50 7.25 31.00 "
    "Basic Earnings Per Share 0.98 0.85 0.73 3.10 "
    "Diluted Earnings Per Share 0.98 0.85 0.73 3.10"
)


# Module-level cached results (avoids class-scoped fixture deprecation).
_MARCH_RESULT = extract_semantic_facts(
    _ncc_fixture("ncc_2026_03_31_normalized.txt"),
    evidence_at=EVIDENCE_AT,
)
_JUNE_RESULT = extract_semantic_facts(
    _ncc_fixture("ncc_2026_06_30_normalized.txt"),
    evidence_at=EVIDENCE_AT,
)
_ZODIAC_RESULT = extract_semantic_facts(_ZODIAC_WITH_EXTRAS, evidence_at=EVIDENCE_AT)

_IRFC_FACTS: list[SemanticFact] = []
for _fn in (
    "irfc_2025_12_31_normalized.txt",
    "irfc_2026_03_31_normalized.txt",
    "irfc_2026_06_30_normalized.txt",
):
    _IRFC_FACTS.extend(extract_semantic_facts(
        _irfc_fixture(_fn), evidence_at=EVIDENCE_AT,
    ).accepted_facts)


# ---------------------------------------------------------------------------
# 1. _financial_number accounting-parentheses negation
# ---------------------------------------------------------------------------

class TestFinancialNumberParentheses:
    """Verify that accounting parentheses produce negative Decimal."""

    def test_parentheses_positive_number_is_negative(self) -> None:
        assert _financial_number("(458.51)") == Decimal("-458.51")

    def test_parentheses_with_inner_minus_is_negative(self) -> None:
        assert _financial_number("(-458.51)") == Decimal("-458.51")

    def test_plain_positive_number_stays_positive(self) -> None:
        assert _financial_number("458.51") == Decimal("458.51")

    def test_i_to_1_ocr_repair(self) -> None:
        assert _financial_number("i.29") == Decimal("1.29")

    def test_nested_parentheses_value(self) -> None:
        assert _financial_number("(1,494.63)") == Decimal("-1494.63")


# ---------------------------------------------------------------------------
# 2. revenue_from_operations ≠ total_income (no conflation)
# ---------------------------------------------------------------------------

class TestRevenueDistinctFromTotalIncome:

    def test_tempsens_revenue_and_total_income_are_distinct(self) -> None:
        result = extract_semantic_facts(TEMPSENS, evidence_at=EVIDENCE_AT)
        assert result.state == "SUCCESS"
        rev = {f.value for f in result.accepted_facts
               if f.metric == METRIC_REVENUE_FROM_OPERATIONS}
        inc = {f.value for f in result.accepted_facts
               if f.metric == METRIC_TOTAL_INCOME}
        assert Decimal("15234.56") in rev
        assert Decimal("15444.89") in inc
        assert rev != inc  # must not be conflated

    def test_tempsens_pat_distinct_from_pbt(self) -> None:
        result = extract_semantic_facts(TEMPSENS, evidence_at=EVIDENCE_AT)
        assert result.state == "SUCCESS"
        pat = {f.value for f in result.accepted_facts
               if f.metric == METRIC_PROFIT_AFTER_TAX}
        pbt = {f.value for f in result.accepted_facts
               if f.metric == METRIC_PROFIT_BEFORE_TAX}
        assert Decimal("2494.89") in pat
        assert Decimal("3344.89") in pbt
        assert pat != pbt

    def test_tempsens_distinct_metrics_all_present(self) -> None:
        result = extract_semantic_facts(TEMPSENS, evidence_at=EVIDENCE_AT)
        metrics = {f.metric for f in result.accepted_facts}
        expected_metrics = {
            METRIC_REVENUE_FROM_OPERATIONS,
            METRIC_TOTAL_INCOME,
            METRIC_PROFIT_BEFORE_TAX,
            METRIC_PROFIT_AFTER_TAX,
            METRIC_EPS_BASIC,
            METRIC_EPS_DILUTED,
            METRIC_TAX,
            METRIC_TOTAL_EXPENSES,
        }
        assert expected_metrics <= metrics


# ---------------------------------------------------------------------------
# 3. NCC March fixture: valid multi-basis facts
# ---------------------------------------------------------------------------

class TestNccMarchValidFacts:

    def test_state_success(self) -> None:
        assert _MARCH_RESULT.state == "SUCCESS"

    def test_all_metrics_present(self) -> None:
        metrics = {f.metric for f in _MARCH_RESULT.accepted_facts}
        for expected in (
            METRIC_REVENUE_FROM_OPERATIONS,
            METRIC_TOTAL_INCOME,
            METRIC_PROFIT_BEFORE_TAX,
            METRIC_PROFIT_AFTER_TAX,
            METRIC_EPS_BASIC,
            METRIC_EPS_DILUTED,
            METRIC_FINANCE_COSTS,
            METRIC_TOTAL_EXPENSES,
            METRIC_CASH_FLOW_OPERATING,
            METRIC_CASH_FLOW_FINANCING,
        ):
            assert expected in metrics, f"missing {expected}"

    def test_revenue_standalone_current_quarter(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
            and f.period_end == "2026-03-31"
            and f.period_type == "QUARTERLY"
            and f.reporting_basis == "STANDALONE"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("5315.71")

    def test_revenue_standalone_annual(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
            and f.period_end == "2026-03-31"
            and f.period_type == "ANNUAL"
            and f.reporting_basis == "STANDALONE"
            and f.column_ordinal == 3
        )
        assert fact.value == Decimal("17463.49")

    def test_pat_standalone_current_quarter(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_PROFIT_AFTER_TAX
            and f.period_end == "2026-03-31"
            and f.period_type == "QUARTERLY"
            and f.reporting_basis == "STANDALONE"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("202.88")

    def test_pbt_standalone_current_quarter(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_PROFIT_BEFORE_TAX
            and f.period_end == "2026-03-31"
            and f.period_type == "QUARTERLY"
            and f.reporting_basis == "STANDALONE"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("280.08")

    def test_eps_standalone_current_quarter(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_EPS_BASIC
            and f.period_end == "2026-03-31"
            and f.period_type == "QUARTERLY"
            and f.reporting_basis == "STANDALONE"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("3.23")
        assert fact.unit == "INR per share"

    def test_revenue_distinct_from_total_income_march(self) -> None:
        rev = {f.value for f in _MARCH_RESULT.accepted_facts
               if f.metric == METRIC_REVENUE_FROM_OPERATIONS}
        inc = {f.value for f in _MARCH_RESULT.accepted_facts
               if f.metric == METRIC_TOTAL_INCOME}
        assert rev != inc

    def test_pat_distinct_from_pbt_march(self) -> None:
        pat = {f.value for f in _MARCH_RESULT.accepted_facts
               if f.metric == METRIC_PROFIT_AFTER_TAX}
        pbt = {f.value for f in _MARCH_RESULT.accepted_facts
               if f.metric == METRIC_PROFIT_BEFORE_TAX}
        assert pat != pbt


# ---------------------------------------------------------------------------
# 4. NCC March cash-flow: accounting parentheses negation
# ---------------------------------------------------------------------------

class TestNccMarchCashFlowParentheses:

    def test_cash_flow_operating_parentheses_negative(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_CASH_FLOW_OPERATING
            and f.period_end == "2026-03-31"
            and f.period_type == "ANNUAL"
            and f.reporting_basis == "CONSOLIDATED"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("-458.51")

    def test_cash_flow_financing_positive(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_CASH_FLOW_FINANCING
            and f.period_end == "2026-03-31"
            and f.period_type == "ANNUAL"
            and f.reporting_basis == "CONSOLIDATED"
            and f.column_ordinal == 0
        )
        assert fact.value == Decimal("878.18")

    def test_cash_flow_operating_prior_positive(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_CASH_FLOW_OPERATING
            and f.period_end == "2025-03-31"
            and f.period_type == "ANNUAL"
            and f.reporting_basis == "CONSOLIDATED"
            and f.column_ordinal == 1
        )
        assert fact.value == Decimal("741.70")

    def test_cash_flow_financing_prior_parentheses_negative(self) -> None:
        fact = next(
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_CASH_FLOW_FINANCING
            and f.period_end == "2025-03-31"
            and f.period_type == "ANNUAL"
            and f.reporting_basis == "CONSOLIDATED"
            and f.column_ordinal == 1
        )
        assert fact.value == Decimal("-246.68")


# ---------------------------------------------------------------------------
# 5. NCC June fixture: structurally rejected → zero facts
# ---------------------------------------------------------------------------

class TestNccJuneRejected:

    def test_zero_accepted_facts(self) -> None:
        assert len(_JUNE_RESULT.accepted_facts) == 0

    def test_rejected_or_no_statement_state(self) -> None:
        assert _JUNE_RESULT.state in ("REJECTED", "NO_STATEMENT")

    def test_all_rows_rejected(self) -> None:
        for row in _JUNE_RESULT.rejected_rows:
            assert row.resolution_state == "REJECTED"

    def test_no_fabricated_pat_or_pbt_from_header_serials(self) -> None:
        metrics = {f.metric for f in _JUNE_RESULT.accepted_facts}
        assert METRIC_PROFIT_BEFORE_TAX not in metrics
        assert METRIC_PROFIT_AFTER_TAX not in metrics
        assert METRIC_FINANCE_COSTS not in metrics


# ---------------------------------------------------------------------------
# 6. IRFC fixtures: EPS preservation with i→1 OCR repair
# ---------------------------------------------------------------------------

class TestIrfcEpsPreserved:

    def test_eps_1_29_present(self) -> None:
        """The i.29 OCR corruption in irfc_2026_03_31 must be repaired to 1.29."""
        eps_values = {
            f.value for f in _IRFC_FACTS
            if f.metric in (METRIC_EPS_BASIC, METRIC_EPS_DILUTED)
        }
        assert Decimal("1.29") in eps_values

    def test_eps_expected_set_is_subset(self) -> None:
        """All five key EPS values must be preserved across IRFC fixtures."""
        eps_values = {
            f.value for f in _IRFC_FACTS
            if f.metric in (METRIC_EPS_BASIC, METRIC_EPS_DILUTED)
        }
        expected = {
            Decimal("1.29"),
            Decimal("1.38"),
            Decimal("4.98"),
            Decimal("5.36"),
            Decimal("1.47"),
        }
        assert expected <= eps_values

    def test_no_bogus_eps_from_corrupted_row(self) -> None:
        """Consolidated EPS row corruption must not leak into accepted facts."""
        eps_values = {
            f.value for f in _IRFC_FACTS
            if f.metric in (METRIC_EPS_BASIC, METRIC_EPS_DILUTED)
        }
        for bogus in (Decimal("82.90"), Decimal("4.65"), Decimal("86.36"), Decimal("84.65")):
            assert bogus not in eps_values

    def test_irfc_2026_06_30_revenue_preserved(self) -> None:
        rev = {
            f.value for f in _IRFC_FACTS
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
            and f.period_end == "2026-06-30"
            and f.period_type == "QUARTERLY"
        }
        assert Decimal("8261.11") in rev

    def test_irfc_2026_03_31_revenue_preserved(self) -> None:
        rev = {
            f.value for f in _IRFC_FACTS
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
            and f.period_end == "2026-03-31"
            and f.period_type == "QUARTERLY"
        }
        assert Decimal("7335.75") in rev


# ---------------------------------------------------------------------------
# 7. Cell → column ordinal binding integrity
# ---------------------------------------------------------------------------

class TestCellColumnBinding:

    def test_tempsens_cell_ordinal_preserved(self) -> None:
        result = extract_semantic_facts(TEMPSENS, evidence_at=EVIDENCE_AT)
        for metric in (
            METRIC_REVENUE_FROM_OPERATIONS,
            METRIC_PROFIT_AFTER_TAX,
            METRIC_EPS_BASIC,
        ):
            facts = sorted(
                (f for f in result.accepted_facts if f.metric == metric),
                key=lambda f: f.column_ordinal,
            )
            assert [f.column_ordinal for f in facts] == [0, 1]

    def test_ncc_march_cell_count_matches_columns(self) -> None:
        rev_facts = [
            f for f in _MARCH_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
        ]
        ordinals = sorted({f.column_ordinal for f in rev_facts})
        assert ordinals[0] == 0
        assert max(ordinals) >= 3

    def test_tempsens_no_left_shift_on_missing_cell(self) -> None:
        """TEMPSEN has no blank cells; this documents the contiguous-ordinal
        contract: ordinals must be 0..N-1 with no gaps."""
        result = extract_semantic_facts(TEMPSENS, evidence_at=EVIDENCE_AT)
        for metric in (
            METRIC_REVENUE_FROM_OPERATIONS,
            METRIC_TOTAL_INCOME,
            METRIC_PROFIT_BEFORE_TAX,
            METRIC_PROFIT_AFTER_TAX,
            METRIC_EPS_BASIC,
            METRIC_EPS_DILUTED,
        ):
            facts = sorted(
                (f for f in result.accepted_facts if f.metric == metric),
                key=lambda f: f.column_ordinal,
            )
            ordinals = [f.column_ordinal for f in facts]
            assert ordinals == list(range(len(ordinals))), (
                f"{metric}: ordinals {ordinals} are not contiguous"
            )


# ---------------------------------------------------------------------------
# 8. Revenue lookbehind: "total revenue from operations" is excluded
# ---------------------------------------------------------------------------

class TestRevenueLookbehind:

    def test_total_revenue_not_extracted_as_revenue(self) -> None:
        rev_values = {
            f.value for f in _ZODIAC_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
        }
        # "Revenue From Operations" row → 125.50 / 118.00 (accepted)
        assert Decimal("125.50") in rev_values
        assert Decimal("118.00") in rev_values
        # "Total revenue from operations" row → 126.00 must NOT appear
        assert Decimal("126.00") not in rev_values

    def test_total_revenue_values_not_in_revenue_metric(self) -> None:
        rev_values = {
            f.value for f in _ZODIAC_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
        }
        # The 126.00 / 119.00 / 105.00 / 455.00 values from the
        # "Total revenue from operations" row must not leak in.
        assert Decimal("455.00") not in rev_values
        assert Decimal("105.00") not in rev_values

    def test_total_income_extracted_separately(self) -> None:
        inc_values = {
            f.value for f in _ZODIAC_RESULT.accepted_facts
            if f.metric == METRIC_TOTAL_INCOME
        }
        assert Decimal("128.00") in inc_values


# ---------------------------------------------------------------------------
# 9. Segment revenue row excluded
# ---------------------------------------------------------------------------

class TestRowExclusion:

    def test_segment_revenue_excluded(self) -> None:
        rev_values = {
            f.value for f in _ZODIAC_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
        }
        assert Decimal("125.50") in rev_values  # "Revenue From Operations"
        assert Decimal("50.00") not in rev_values  # "Segment revenue" excluded

    def test_only_revenue_values_appear(self) -> None:
        rev_values = {
            f.value for f in _ZODIAC_RESULT.accepted_facts
            if f.metric == METRIC_REVENUE_FROM_OPERATIONS
        }
        # Should be exactly the 4 columns of "Revenue From Operations"
        assert rev_values == {Decimal("125.50"), Decimal("118.00"),
                              Decimal("104.25"), Decimal("450.00")}
