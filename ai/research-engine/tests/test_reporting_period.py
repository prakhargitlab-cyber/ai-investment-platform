"""Tests for the reusable app.reporting_period utility -- independent of
any evidence type, so a future balance-sheet/cash-flow/P&L interpreter
can rely on the same semantics as the shareholding table interpreter.
"""
from __future__ import annotations

from datetime import date

from app.reporting_period import find_period_tokens, month_end_date, parse_reporting_period


class TestParseReportingPeriod:
    def test_month_year_abbreviated(self):
        p = parse_reporting_period("Jun 2026")
        assert p.precision == "MONTH"
        assert p.year == 2026 and p.month == 6 and p.day is None
        assert p.normalized_date == date(2026, 6, 1)

    def test_month_year_full_name(self):
        p = parse_reporting_period("June 2026")
        assert p.precision == "MONTH"
        assert p.normalized_date == date(2026, 6, 1)

    def test_mar_2026(self):
        p = parse_reporting_period("Mar 2026")
        assert (p.year, p.month, p.precision) == (2026, 3, "MONTH")
        assert p.normalized_date == date(2026, 3, 1)

    def test_dec_2025(self):
        p = parse_reporting_period("Dec 2025")
        assert (p.year, p.month, p.precision) == (2025, 12, "MONTH")
        assert p.normalized_date == date(2025, 12, 1)

    def test_sep_2025(self):
        p = parse_reporting_period("Sep 2025")
        assert (p.year, p.month, p.precision) == (2025, 9, "MONTH")
        assert p.normalized_date == date(2025, 9, 1)

    def test_month_dash_two_digit_year(self):
        p = parse_reporting_period("Jun-26")
        assert (p.year, p.month, p.precision) == (2026, 6, "MONTH")
        assert p.normalized_date == date(2026, 6, 1)

    def test_explicit_day_month_year_is_day_precision(self):
        p = parse_reporting_period("30 Jun 2026")
        assert p.precision == "DAY"
        assert (p.year, p.month, p.day) == (2026, 6, 30)
        assert p.normalized_date == date(2026, 6, 30)

    def test_year_only_is_year_precision_and_never_invents_month_day(self):
        p = parse_reporting_period("2026")
        assert p.precision == "YEAR"
        assert p.month is None and p.day is None
        assert p.normalized_date is None

    def test_invalid_calendar_date_returns_none(self):
        assert parse_reporting_period("31 Feb 2026") is None

    def test_empty_text_returns_none(self):
        assert parse_reporting_period("") is None
        assert parse_reporting_period("   ") is None

    def test_unrecognizable_text_returns_none(self):
        assert parse_reporting_period("not a period at all") is None


class TestFindPeriodTokens:
    def test_header_row_in_order(self):
        periods = find_period_tokens("Sep 2025   Dec 2025   Mar 2026   Jun 2026")
        assert [p.original_text for p in periods] == ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]
        assert [(p.year, p.month) for p in periods] == [(2025, 9), (2025, 12), (2026, 3), (2026, 6)]

    def test_irregular_whitespace_still_finds_all_tokens(self):
        periods = find_period_tokens("Sep 2025    Dec 2025\tMar 2026     Jun 2026")
        assert len(periods) == 4

    def test_no_period_tokens_returns_empty_list(self):
        assert find_period_tokens("Promoters 74.24 1.87 0.11 23.78") == []

    def test_bare_years_are_not_treated_as_period_tokens(self):
        """A header made only of bare 4-digit numbers (no month name) is
        far more likely to be data than a period column list -- this
        function intentionally does not fall back to bare-year matching."""
        assert find_period_tokens("2023 2024 2025 2026") == []


class TestPeriodOrdering:
    def test_sort_key_orders_chronologically_across_year_boundary(self):
        periods = find_period_tokens("Sep 2025   Dec 2025   Mar 2026   Jun 2026")
        ordered = sorted(periods, key=lambda p: p.sort_key)
        assert [p.original_text for p in ordered] == ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]
        assert max(periods, key=lambda p: p.sort_key).original_text == "Jun 2026"

    def test_shuffled_periods_still_select_the_correct_latest(self):
        periods = find_period_tokens("Jun 2026   Sep 2025   Mar 2026   Dec 2025")
        assert max(periods, key=lambda p: p.sort_key).original_text == "Jun 2026"

    def test_synthetic_first_of_month_day_has_no_effect_on_ordering(self):
        """Every MONTH-precision period normalizes its day to 1 -- proving
        two different months in the same year still order correctly by
        (year, month), never accidentally comparing equal because the
        synthetic days match."""
        mar = parse_reporting_period("Mar 2026")
        jun = parse_reporting_period("Jun 2026")
        assert mar.normalized_date.day == jun.normalized_date.day == 1
        assert mar < jun
        assert jun > mar


class TestMonthEndDate:
    def test_june_30_days(self):
        assert month_end_date(2026, 6) == date(2026, 6, 30)

    def test_march_31_days(self):
        assert month_end_date(2026, 3) == date(2026, 3, 31)

    def test_february_leap_year(self):
        assert month_end_date(2028, 2) == date(2028, 2, 29)

    def test_february_non_leap_year(self):
        assert month_end_date(2026, 2) == date(2026, 2, 28)


class TestFinancialPeriodReuseBeyondShareholding:
    """Proves the SAME reporting-period helper is usable outside
    Shareholding -- e.g. a future Balance Sheet or Quarterly Financials
    table column -- without implementing a whole new parser."""

    def test_balance_sheet_style_column_header(self):
        periods = find_period_tokens("Mar 2024   Mar 2025   Mar 2026")
        assert [p.month for p in periods] == [3, 3, 3]
        assert [p.year for p in periods] == [2024, 2025, 2026]
        latest = max(periods, key=lambda p: p.sort_key)
        assert (latest.year, latest.month) == (2026, 3)
        assert latest.precision == "MONTH"
        assert latest.normalized_date == date(2026, 3, 1)

    def test_quarterly_financials_style_column_header(self):
        periods = find_period_tokens("Jun 2025   Sep 2025   Dec 2025   Mar 2026")
        latest = max(periods, key=lambda p: p.sort_key)
        assert (latest.year, latest.month) == (2026, 3)
