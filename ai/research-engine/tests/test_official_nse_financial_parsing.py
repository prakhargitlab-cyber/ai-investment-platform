from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from uuid import UUID

import asyncio
import pytest

import app.structured_research as structured_research
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import CompanyResearchProfile, DocumentStatus, DocumentType, ProvenancedValue, ReliabilityLevel, ResearchDocument, SourceClassification, SourceMode, SourceType
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository, _InstrumentRefreshGate
from app.settings import Settings
from app.source_discovery import DiscoveryResult
from app.source_registry import RegisteredResearchSource
from app.structured_research import _STATEMENT_HEADING, _classify_financial_row, _financial_columns, _financial_number, _header_dates, _nse_statement_candidates, _parse_nse_income_statement, _parse_statement_candidate, _row_semantic_tokens, _statement_quality, latest_quarterly_result, parsed_nse_balance_sheet_periods, parsed_nse_cash_flow_periods, parsed_nse_income_statement_periods


def _document(text: str) -> ResearchDocument:
    return ResearchDocument(
        canonical_url="https://nsearchives.nseindia.com/corporate/result.pdf", original_url="https://nsearchives.nseindia.com/corporate/result.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", content_type="application/pdf", document_type=DocumentType.PDF_REFERENCE,
        normalized_text=text, content_hash="fixture", status=DocumentStatus.PROCESSED, reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL, instrument_id=UUID("99999999-9999-9999-9999-999999999999"),
        company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"), published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


JUNE = """Amounts in Rs. Crore Extract of Statement of Unaudited Financial Results for the quaiter ended 30\" Jiine 2026 Particulars Quarter Ended Year Ended 30th Ju ne 31st March 30th June 31st March Z026 2026 2025 2026 Revenue From Operations 8,261.11 7,335.75 6,915.38 27,284.15 Net Profit for the period after Tax 1,927.21 1,684.31 1,745.69 6,427.00 Earning Per Share (of Rs 10 each) Basic (Rs.) 1.47 1.29 1.34 5.36 Diluted (Rs.) 1.47 1.29 1.34 5.36 Borrowings 142851.50 163186.05"""


# Sanitized structural excerpts from the persisted June-2026 layout families.
# Values are fixture-local; assertions below verify only aligned source cells.
FEDERAL_BANK_JUNE_2026 = """STANDALONE UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026 (Rs. in Lakhs) Particulars Quarter ended Year ended 30.06.2026 31.03.2026 30.06.2025 31.03.2026 1. Interest earned 210000.00 190000.00 180000.00 760000.00 2. Other income 40000.00 35000.00 30000.00 120000.00 3. TOTAL INCOME 250000.00 225000.00 210000.00 880000.00 12. Net Profit from Ordinary Activities after tax 30000.00 28000.00 26000.00 105000.00 Basic EPS 1.63 1.52 1.42 5.71 Diluted EPS 1.63 1.52 1.42 5.71"""

SHRIRAM_JUNE_2026 = """STATEMENT OF UNAUDITED STANDALONE FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026 (Rs. in Crore) Particulars Quarter ended Year ended 30.06.2026 31.03.2026 30.06.2025 31.03.2026 Revenue from operations 5000.00 4700.00 4300.00 18500.00 Total revenue from operations 5050.00 4750.00 4350.00 18700.00 Other income 100.00 90.00 80.00 350.00 Total income 5150.00 4840.00 4430.00 19050.00 Profit for the period/year 900.00 850.00 790.00 3300.00 Basic EPS 12.50 11.80 10.90 45.80 Diluted EPS 12.50 11.80 10.90 45.80"""

UJJIVAN_JUNE_2026 = """Statement of Unaudited Financial Results for the quarter ended June 30, 2026 (Rs. in Lakh) Particulars Quarter ended Year ended June 30 2026 March 31 2026 June 30 2025 March 31 2026 Interest earned 190000 175000 160000 690000 Other income 38093 35000 32000 113897 Total income 228093 210000 192000 803897 Net profit from ordinary activities after tax 31654 30000 28000 69263 Net profit for the period/year 31654 30000 28000 69263 Basic EPS 1.63 1.54 1.42 3.57 Diluted EPS 1.63 1.54 1.42 3.57"""

PRE_PARTICULARS_GROUP_LAYOUT = """Statement of Unaudited Financial Results (Rs. in Lakh) Quarter ended Year ended SI Particulars June 30, March 31, June 30, March 31, No. 2026 2026 2025 2026 Total income (1)+(2) 2,28,093 2,18,506 1,86,783 8,03,897 Profit after tax (10)-(11) 31,654 28,197 10,322 69,263 Basic Earnings Per Share 1.63 1.45 0.53 3.57"""

PRE_PARTICULARS_AUDITED_LAYOUT = """Statement of Audited Financial Results (Rs. in Lakh) Quarter ended Year ended SI Particulars March 31, December 31, March 31, 2025 March 31, March 31, 2025 No. 2026 2025 2026 Total income (1)+(2) 2,18,506 2,04,740 1,84,305 8,03,897 7,20,059 Net profit after tax (10)-(11) 28,197 18,572 8,339 69,263 72,610 Basic Earnings Per Share 1.45 0.96 0.43 3.57 3.75"""

INVALID_CALENDAR_HEADER = """Quarter ended Half year ended Year ended SI September 30, June 30, 2023 September 30, September September March 31, No. 2023 2022 30, 2023 30, 2022 2023"""

ZODIAC_JUNE_2026 = """Statement Of Unaudited Standalone Financial Results For the Quarter ended On 30th June, 2026 (Rs. in Lakhs) Particulars Quarter Ended Year Ended 30th June 2026 31st March 2026 30th June 2025 31st March 2026 Revenue From Operations 125.50 118.00 104.25 450.00 Other Income 2.50 2.00 1.75 8.00 Total Income 128.00 120.00 106.00 458.00 Net Profit/(Loss) After Tax 9.75 8.50 7.25 31.00 Basic Earnings Per Share 0.98 0.85 0.73 3.10 Diluted Earnings Per Share 0.98 0.85 0.73 3.10"""


# Structural excerpts from the persisted Deepak Nitrite filing.  The (5)/(6)
# cells are table row references, not profit values.
DEEPAK_DECEMBER_2025 = """CONSOLIDATED Statement of Unaudited Financial Results for the quarter and nine months ended 31 December 2025 (Rs. in Crore) Particulars Quarter Ended Nine Months Ended 31 December 2025 30 September 2025 31 December 2024 31 December 2025 31 December 2024 Revenue From Operations 1974.97 1901.89 1903.40 5766.74 6102.24 Net Profit for the period/year (5 • 6) 99.82 118.75 98.13 330.83 494.85 STANDALONE Statement of Unaudited Financial Results for the quarter and nine months ended 31 December 2025 (Rs. in Crore) Particulars Quarter Ended Nine Months Ended 31 December 2025 30 September 2025 31 December 2024 31 December 2025 31 December 2024 Revenue From Operations 693.59 615.92 551 .59 1921.79 1872.00 Net Profit for the period/year (5 - 6) 8.14 111.65 17.25 150.09 222.57"""


# This Electronics Mart-shaped header retains the report-level quarter/nine-
# month semantics while OCR loses the table's nine-month group label.
ELECTRONICS_MART_DECEMBER_2025 = """CONSOLIDATED Statement of Unaudited Financial Results for the third quarter and nine months ended 31 December 2025 (Rs. in Lakhs) Particulars Quarter Ended Year Ended 31 December 2025 30 September 2025 31 December 2024 31 December 2025 31 December 2024 Revenue From Operations 1000.00 900.00 800.00 2800.00 2300.00 Net Profit for the period/year 335.04 296.45 161.42 900.00 674.04"""


def _multi_period_statement(
    current: str,
    prior: str,
    comparable: str,
    annual: str,
    *,
    revenue: str = "100.00",
    pat: str = "10.00",
) -> str:
    return (
        "Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended "
        f"{current} {prior} {comparable} {annual} "
        "Revenue From Operations "
        f"{revenue} 90.00 80.00 350.00 "
        f"Profit after Tax {pat} 9.00 8.00 30.00"
    )


def _rolling_financial_documents() -> list[ResearchDocument]:
    """Two durable filings exposing the latest four explicitly reported quarters."""
    return [
        _document(_multi_period_statement(
            "30 June 2026", "31 March 2026", "31 December 2025", "31 March 2026",
            revenue="160.00", pat="16.00",
        )).model_copy(update={"published_at": datetime(2026, 8, 1, tzinfo=timezone.utc)}),
        _document(_multi_period_statement(
            "30 September 2025", "30 June 2025", "30 September 2024", "31 March 2025",
            revenue="150.00", pat="15.00",
        )).model_copy(update={"published_at": datetime(2025, 11, 1, tzinfo=timezone.utc)}),
    ]


def _balance_sheet(*, current: str = "31 March 2026", prior: str = "31 March 2025", basis: str = "") -> str:
    return (
        f"{basis} Balance Sheet Particulars As at {current} {prior} "
        "Total Assets 500.00 450.00 Total Equity 120.00 110.00 "
        "Total Liabilities 380.00 340.00 Total Current Assets 200.00 180.00 "
        "Total Current Liabilities 150.00 140.00 Cash and Cash Equivalents 25.00 20.00 "
        "Total Borrowings 300.00 275.00"
    )


def _cash_flow(*, current: str = "31 March 2026", prior: str = "31 March 2025", basis: str = "") -> str:
    return (
        f"{basis} Statement of Cash Flows Particulars Year Ended {current} {prior} "
        "Net cash from operating activities 100.00 90.00 "
        "Net cash used in investing activities 40.00 35.00 "
        "Net cash from financing activities 20.00 15.00 "
        "Net increase in cash and cash equivalents 40.00 40.00 "
        "Operating profit 200.00 180.00 Opening cash and cash equivalents 10.00 8.00"
    )


def test_noisy_nse_june_table_uses_current_column_and_preserves_inr_crore() -> None:
    result = latest_quarterly_result([_document(JUNE)])
    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("8261.11")
    assert result.pat.value == Decimal("1927.21")
    assert result.eps.value == Decimal("1.47")
    assert result.eps.value != Decimal("10")
    assert result.eps.unit == "INR per share"
    assert result.revenue.unit == "INR crore"
    assert result.debt_or_borrowings is None


def test_deepak_shaped_pat_rows_skip_note_references_and_keep_reporting_bases_separate() -> None:
    periods = parsed_nse_income_statement_periods([_document(DEEPAK_DECEMBER_2025)])
    values = {
        (period.period_end, period.period_type, period.reporting_basis): {
            metric: value.value for metric, value in period.metrics
        }
        for period in periods
    }

    assert values[("2025-12-31", "QUARTERLY", "CONSOLIDATED")] == {
        "revenue": Decimal("1974.97"), "pat": Decimal("99.82"),
    }
    assert values[("2024-12-31", "QUARTERLY", "CONSOLIDATED")] == {
        "revenue": Decimal("1903.40"), "pat": Decimal("98.13"),
    }
    assert values[("2025-12-31", "QUARTERLY", "STANDALONE")] == {
        "revenue": Decimal("693.59"), "pat": Decimal("8.14"),
    }
    assert values[("2024-12-31", "QUARTERLY", "STANDALONE")] == {
        "revenue": Decimal("551.59"), "pat": Decimal("17.25"),
    }


def test_electronics_mart_shaped_quarter_nine_month_report_does_not_persist_cumulative_columns_as_annual() -> None:
    periods = parsed_nse_income_statement_periods([_document(ELECTRONICS_MART_DECEMBER_2025)])
    values = {
        (period.period_end, period.period_type): {metric: value.value for metric, value in period.metrics}
        for period in periods
    }

    assert values[("2025-12-31", "QUARTERLY")]["pat"] == Decimal("335.04")
    assert values[("2024-12-31", "QUARTERLY")]["pat"] == Decimal("161.42")
    assert not any(period.period_type == "ANNUAL" for period in periods)


def _income_metrics(text: str) -> dict[str, Decimal]:
    periods = parsed_nse_income_statement_periods([_document(text)])
    period = next(value for value in periods if value.period_end == "2026-06-30" and value.period_type == "QUARTERLY")
    return {metric: value.value for metric, value in period.metrics}


def test_federal_bank_style_june_2026_financial_layout_extracts_aligned_bank_result_rows() -> None:
    period = next(value for value in parsed_nse_income_statement_periods([_document(FEDERAL_BANK_JUNE_2026)])
                  if value.period_end == "2026-06-30" and value.period_type == "QUARTERLY")

    assert period.reporting_basis == "STANDALONE"
    assert {metric: value.value for metric, value in period.metrics} == {
        "revenue": Decimal("250000.00"), "pat": Decimal("30000.00"), "eps": Decimal("1.63"),
    }


def test_shriram_style_june_2026_financial_layout_keeps_quarter_and_year_columns_aligned() -> None:
    assert _income_metrics(SHRIRAM_JUNE_2026) == {
        "revenue": Decimal("5050.00"), "pat": Decimal("900.00"), "eps": Decimal("12.50"),
    }


def test_ujjivan_style_june_2026_financial_layout_supports_month_first_dates_and_bank_rows() -> None:
    assert _income_metrics(UJJIVAN_JUNE_2026) == {
        "revenue": Decimal("228093"), "pat": Decimal("31654"), "eps": Decimal("1.63"),
    }


def test_pre_particulars_period_groups_associate_with_post_particulars_dates() -> None:
    periods = parsed_nse_income_statement_periods([_document(PRE_PARTICULARS_GROUP_LAYOUT)])
    quarterly = {
        period.period_end: {metric: value.value for metric, value in period.metrics}
        for period in periods if period.period_type == "QUARTERLY"
    }
    annual = {
        period.period_end: {metric: value.value for metric, value in period.metrics}
        for period in periods if period.period_type == "ANNUAL"
    }

    assert quarterly["2026-06-30"] == {
        "revenue": Decimal("228093"), "pat": Decimal("31654"), "eps": Decimal("1.63"),
    }
    assert quarterly["2026-03-31"] == {
        "revenue": Decimal("218506"), "pat": Decimal("28197"), "eps": Decimal("1.45"),
    }
    assert quarterly["2025-06-30"] == {
        "revenue": Decimal("186783"), "pat": Decimal("10322"), "eps": Decimal("0.53"),
    }
    assert annual["2026-03-31"] == {
        "revenue": Decimal("803897"), "pat": Decimal("69263"), "eps": Decimal("3.57"),
    }


def test_pre_particulars_groups_do_not_leak_from_a_previous_statement_region() -> None:
    text = (
        "Auditor discussion: Quarter ended Year ended. "
        "Statement of Unaudited Financial Results Particulars "
        "June 30 2026 March 31 2026 June 30 2025 March 31 2026 "
        "Total income 100 90 80 350 Profit after tax 10 9 8 30"
    )

    assert _nse_statement_candidates(text, datetime(2026, 8, 1, tzinfo=timezone.utc)) == []


def test_auditor_prose_before_pre_particulars_financial_statement_keeps_alignment() -> None:
    text = (
        "Auditor report on Financial Results. The Financial Results were reviewed. "
        + PRE_PARTICULARS_GROUP_LAYOUT
    )

    assert _income_metrics(text) == {
        "revenue": Decimal("228093"), "pat": Decimal("31654"), "eps": Decimal("1.63"),
    }


def test_pre_particulars_groups_align_mixed_explicit_and_deferred_year_cells() -> None:
    periods = parsed_nse_income_statement_periods([_document(PRE_PARTICULARS_AUDITED_LAYOUT)])
    values = {
        (period.period_end, period.period_type): {metric: value.value for metric, value in period.metrics}
        for period in periods
    }

    assert values[("2026-03-31", "QUARTERLY")] == {
        "revenue": Decimal("218506"), "pat": Decimal("28197"), "eps": Decimal("1.45"),
    }
    assert values[("2025-12-31", "QUARTERLY")] == {
        "revenue": Decimal("204740"), "pat": Decimal("18572"), "eps": Decimal("0.96"),
    }
    assert values[("2025-03-31", "QUARTERLY")] == {
        "revenue": Decimal("184305"), "pat": Decimal("8339"), "eps": Decimal("0.43"),
    }
    assert values[("2026-03-31", "ANNUAL")] == {
        "revenue": Decimal("803897"), "pat": Decimal("69263"), "eps": Decimal("3.57"),
    }
    assert values[("2025-03-31", "ANNUAL")] == {
        "revenue": Decimal("720059"), "pat": Decimal("72610"), "eps": Decimal("3.75"),
    }


def test_invalid_calendar_header_is_rejected_without_date_correction() -> None:
    groups, dates = structured_research._tokenize_financial_header(
        INVALID_CALENDAR_HEADER,
        datetime(2023, 11, 1, tzinfo=timezone.utc),
    )

    assert groups == []
    assert dates == []
    malformed = (
        "Statement of Unaudited Financial Results Particulars " + INVALID_CALENDAR_HEADER
        + " Total income 100 90 80 270 250 350 Profit after tax 10 9 8 27 25 35"
    )
    assert parsed_nse_income_statement_periods([_document(malformed)]) == []


def test_invalid_calendar_candidate_does_not_block_later_valid_statement() -> None:
    malformed = (
        "Statement of Unaudited Financial Results Particulars " + INVALID_CALENDAR_HEADER
        + " Total income 100 90 80 270 250 350 Profit after tax 10 9 8 27 25 35 "
    )
    periods = parsed_nse_income_statement_periods([_document(malformed + JUNE)])
    values = {
        metric: value.value
        for period in periods
        if period.period_end == "2026-06-30" and period.period_type == "QUARTERLY"
        for metric, value in period.metrics
    }

    assert values == {
        "revenue": Decimal("8261.11"), "pat": Decimal("1927.21"), "eps": Decimal("1.47"),
    }


def test_zodiac_style_june_2026_financial_layout_supports_basic_earnings_per_share_row() -> None:
    period = next(value for value in parsed_nse_income_statement_periods([_document(ZODIAC_JUNE_2026)])
                  if value.period_end == "2026-06-30" and value.period_type == "QUARTERLY")

    assert period.reporting_basis == "STANDALONE"
    assert {metric: value.value for metric, value in period.metrics} == {
        "revenue": Decimal("125.50"), "pat": Decimal("9.75"), "eps": Decimal("0.98"),
    }


def test_latest_quarterly_result_discovers_nse_statement_candidates_once_per_document(monkeypatch) -> None:
    original = structured_research._nse_statement_candidates
    calls = 0

    def recording_candidates(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(structured_research, "_nse_statement_candidates", recording_candidates)

    result = latest_quarterly_result([_document(JUNE), _document(JUNE)])

    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("8261.11")
    assert result.pat.value == Decimal("1927.21")
    assert result.eps.value == Decimal("1.47")
    assert calls == 2


def test_statement_boundary_index_preserves_heading_particulars_and_statement_end_offsets() -> None:
    text = (
        "Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended "
        "30 June 2026 31 March 2026 30 June 2025 31 March 2026 "
        "Revenue From Operations 100.00 90.00 80.00 350.00 "
        "Profit after Tax 10.00 9.00 8.00 30.00 Basic EPS 1.00 0.90 0.80 3.50 "
        "Statement of Assets and Liabilities Particulars As at 31 March 2026 31 March 2025"
    )
    start = _STATEMENT_HEADING.search(text)
    assert start is not None

    direct = _parse_statement_candidate(text, datetime(2026, 8, 1, tzinfo=timezone.utc), start.start(), True)
    indexed = _parse_statement_candidate(
        text,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        start.start(),
        True,
        boundary_index=structured_research._StatementBoundaryIndex.from_text(text),
    )

    assert indexed == direct
    assert indexed is not None
    assert indexed.current_column.period_end == "2026-06-30"
    assert "Statement of Assets" not in indexed.region


def test_statement_candidate_parse_reuses_identical_header_tokenization_within_document(monkeypatch) -> None:
    text = (
        "Particulars Quarter Ended Year Ended 30 June 2026 31 March 2026 30 June 2025 31 March 2026 "
        "Revenue From Operations 100.00 90.00 80.00 350.00 Profit after Tax 10.00 9.00 8.00 30.00 "
        "Particulars Notes to the financial results"
    )
    original = structured_research._tokenize_financial_header
    calls = 0

    def recording_tokenizer(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(structured_research, "_tokenize_financial_header", recording_tokenizer)

    candidates = structured_research._nse_statement_candidates(
        text,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert candidates
    assert candidates[0].current_column.period_end == "2026-06-30"
    assert calls == 1


def test_statement_candidate_parse_tokenizes_distinct_headers_independently(monkeypatch) -> None:
    text = (
        "Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended "
        "30 June 2026 31 March 2026 30 June 2025 31 March 2026 Revenue From Operations 100.00 90.00 80.00 350.00 "
        "Profit after Tax 10.00 9.00 8.00 30.00 "
        "Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended "
        "30 September 2026 30 June 2026 30 September 2025 31 March 2026 Revenue From Operations 110.00 100.00 90.00 360.00 "
        "Profit after Tax 11.00 10.00 9.00 31.00"
    )
    original = structured_research._tokenize_financial_header
    calls = 0

    def recording_tokenizer(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(structured_research, "_tokenize_financial_header", recording_tokenizer)
    boundary_index = structured_research._StatementBoundaryIndex.from_text(text)
    context = structured_research._StatementCandidateParseContext(boundary_index)
    starts = [start for start, _ in boundary_index.statement_headings]

    first = _parse_statement_candidate(
        text, datetime(2026, 8, 1, tzinfo=timezone.utc), starts[0], True, parse_context=context,
    )
    second = _parse_statement_candidate(
        text, datetime(2026, 8, 1, tzinfo=timezone.utc), starts[1], True, parse_context=context,
    )

    assert first is not None
    assert second is not None
    assert first.current_column.period_end == "2026-06-30"
    assert second.current_column.period_end == "2026-09-30"
    assert calls == 2


def test_statement_candidate_tokenization_cache_does_not_leak_between_parser_invocations(monkeypatch) -> None:
    original = structured_research._tokenize_financial_header
    calls = 0

    def recording_tokenizer(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(structured_research, "_tokenize_financial_header", recording_tokenizer)

    assert latest_quarterly_result([_document(JUNE)]) is not None
    assert latest_quarterly_result([_document(JUNE)]) is not None

    assert calls == 2


def test_march_quarter_uses_first_column_not_annual_column() -> None:
    result = latest_quarterly_result([_document("Amounts in Rs. Crore Extract of Statement Of Audited Financial Results for the quarter and Year ended 31st March 2026 Particulars Quarter Ended Year Ended 31st March 31st December 31st March 31st March 31st March 2026 2025 2025 2026 2025 33 Revenue From Operations 7,335.75 6,661.13 6,722.83 27,284.15 27,152.14 3 Net Profit after Tax 1,684.31 1,500.00 1,300.00 6,427.00 6,100.00 Earning Per Share (of Rs 10 each) Basic 1.29 1.15 1.00 5.36 5.00")])
    assert result is not None
    assert result.period == "2026-03-31"
    assert result.revenue.value == Decimal("7335.75")
    assert result.pat.value == Decimal("1684.31")
    assert result.eps.value == Decimal("1.29")
    assert result.revenue.value != Decimal("33")
    assert result.pat.value != Decimal("3")


def test_december_table_ignores_nine_month_and_year_columns_and_missing_metrics_stay_missing() -> None:
    result = latest_quarterly_result([_document("Amounts in Rs. Crore Statement of Unaudited Financial Results Particulars Quarter Ended Nine Month Ended Year Ended 31 December 2025 30 September 2025 31 December 2024 31 December 2025 31 December 2024 31 March 2025 Total Revenue From operations 6,661.13 6,400.00 5,900.00 19,500.00 18,000.00 25,000.00 Total Income 6,919.23 6,700.00 6,200.00 20,200.00 19,000.00 26,000.00 Finance costs 4,812.02 4,600.00 4,100.00 14,100.00 12,300.00 18,900.00 Profit for the period 1,802.19 1,500.00 1,300.00 5,100.00 4,000.00 6,000.00")])
    assert result is not None
    assert result.period == "2025-12-31"
    assert result.revenue.value == Decimal("6661.13")
    assert result.pat.value == Decimal("1802.19")
    assert result.revenue.value != Decimal("6919.23")
    assert result.eps is None
    assert result.ebitda is None


def test_multi_period_parser_emits_explicit_quarterly_and_annual_columns() -> None:
    periods = parsed_nse_income_statement_periods([_document(JUNE)])
    values = {
        (period.period_end, period.period_type): {metric: value.value for metric, value in period.metrics}
        for period in periods
    }
    assert values[("2026-06-30", "QUARTERLY")]["revenue"] == Decimal("8261.11")
    assert values[("2026-06-30", "QUARTERLY")]["pat"] == Decimal("1927.21")
    assert values[("2026-06-30", "QUARTERLY")]["eps"] == Decimal("1.47")
    assert values[("2026-03-31", "QUARTERLY")]["revenue"] == Decimal("7335.75")
    assert values[("2026-03-31", "ANNUAL")]["revenue"] == Decimal("27284.15")
    # A March quarter remains a quarter because the table header says so.
    assert ("2026-03-31", "QUARTERLY") in values


def test_multi_period_parser_keeps_consolidated_and_standalone_periods_distinct() -> None:
    consolidated = _document(JUNE.replace("Extract of Statement", "Consolidated Statement"))
    standalone = _document(JUNE.replace("Extract of Statement", "Standalone Statement").replace("8,261.11", "7,000.00"))
    periods = parsed_nse_income_statement_periods([consolidated, standalone])
    values = {
        period.reporting_basis: dict(period.metrics)["revenue"].value
        for period in periods
        if period.period_end == "2026-06-30" and period.period_type == "QUARTERLY"
    }
    assert values == {"CONSOLIDATED": Decimal("8261.11"), "STANDALONE": Decimal("7000.00")}


def test_multi_period_parser_emits_annual_only_when_explicitly_present() -> None:
    text = (
        "Statement of Audited Financial Results Particulars Year Ended "
        "31 March 2026 31 March 2025 Revenue From Operations 350.00 300.00 "
        "Profit after Tax 30.00 25.00"
    )
    periods = parsed_nse_income_statement_periods([_document(text)])
    assert {(period.period_end, period.period_type) for period in periods} == {
        ("2026-03-31", "ANNUAL"), ("2025-03-31", "ANNUAL"),
    }


def test_balance_sheet_parser_maps_explicit_as_at_columns_without_derivation() -> None:
    periods = parsed_nse_balance_sheet_periods([_document(_balance_sheet())])
    values = {period.period_end: dict(period.metrics) for period in periods}
    assert set(values) == {"2026-03-31", "2025-03-31"}
    assert all(period.period_type == "AS_AT" for period in periods)
    assert values["2026-03-31"]["total_assets"].value == Decimal("500")
    assert values["2026-03-31"]["total_equity"].value == Decimal("120")
    assert values["2026-03-31"]["total_liabilities"].value == Decimal("380")
    assert values["2026-03-31"]["current_assets"].value == Decimal("200")
    assert values["2026-03-31"]["current_liabilities"].value == Decimal("150")
    assert values["2026-03-31"]["cash_and_cash_equivalents"].value == Decimal("25")
    assert values["2026-03-31"]["total_debt"].value == Decimal("300")
    assert "total_debt" not in dict(parsed_nse_balance_sheet_periods([_document(
        _balance_sheet().replace("Total Borrowings 300.00 275.00", "Long Term Borrowings 200.00 180.00")
    )])[0].metrics)


def test_balance_sheet_row_boundaries_skip_ocr_separator_without_rejecting_genuine_one() -> None:
    text = (
        "Balance Sheet Particulars As at 31 March 2026 31 March 2025 "
        "Total Non-Financial Assets 11.691.51 11.844.60 11 "
        "Total Assets I 480.742.87 I 481.218.19 "
        "Total Non-Financial Liabilities 312.75 172.19 11 "
        "Total Liabilities I 426.318.91 I 430.445.98 "
        "Cash and Cash Equivalents 1 2"
    )
    values = dict(parsed_nse_balance_sheet_periods([_document(text)])[0].metrics)
    assert values["total_assets"].value == Decimal("480742.87")
    assert values["total_liabilities"].value == Decimal("426318.91")
    assert values["cash_and_cash_equivalents"].value == Decimal("1")


def test_balance_sheet_parser_keeps_reporting_bases_distinct() -> None:
    periods = parsed_nse_balance_sheet_periods([
        _document(_balance_sheet(basis="Consolidated")),
        _document(_balance_sheet(basis="Standalone").replace("500.00", "400.00", 1)),
    ])
    values = {
        period.reporting_basis: dict(period.metrics)["total_assets"].value
        for period in periods if period.period_end == "2026-03-31"
    }
    assert values == {"CONSOLIDATED": Decimal("500"), "STANDALONE": Decimal("400")}


def test_cash_flow_parser_emits_only_explicit_annual_flow_rows() -> None:
    periods = parsed_nse_cash_flow_periods([_document(_cash_flow())])
    values = {period.period_end: dict(period.metrics) for period in periods}
    assert set(values) == {"2026-03-31", "2025-03-31"}
    assert all(period.period_type == "ANNUAL" for period in periods)
    assert values["2026-03-31"]["cash_flow_from_operating_activities"].value == Decimal("100")
    assert values["2026-03-31"]["cash_flow_from_investing_activities"].value == Decimal("40")
    assert values["2026-03-31"]["cash_flow_from_financing_activities"].value == Decimal("20")
    assert values["2026-03-31"]["net_change_in_cash"].value == Decimal("40")
    assert parsed_nse_cash_flow_periods([_document(_cash_flow().replace("Year Ended", "As at"))]) == []


def test_cash_flow_parser_keeps_reporting_bases_distinct() -> None:
    periods = parsed_nse_cash_flow_periods([
        _document(_cash_flow(basis="Consolidated")),
        _document(_cash_flow(basis="Standalone").replace("100.00", "70.00", 1)),
    ])
    values = {period.reporting_basis: dict(period.metrics)["cash_flow_from_operating_activities"].value
              for period in periods if period.period_end == "2026-03-31"}
    assert values == {"CONSOLIDATED": Decimal("100"), "STANDALONE": Decimal("70")}


def test_cash_flow_two_quarters_and_explicit_year_ended_emits_only_annual_column() -> None:
    text = (
        "Statement for the Cash Fl OWS Particulars Quarter Ended Quarter Ended Year Ended "
        "30 June 2025 30 June 2024 31 March 2025 "
        "Net Cash Flow/(Used) in Operating Activities (A) 1.467.53 4,641.47 8.229.57 "
        "Net Cash Flow/(used) in Investing Activities (B) '0.241 (0.11) '0.lot "
        "Net Cash Generated By/(Used ln) Financing Activities (C) '6.975.lot (4.640.381 12.571.951 "
        "Net Increase in Cash and Cash Equivalents (A+B+C) (5,507.81) 0.98 5,657.52"
    )
    periods = parsed_nse_cash_flow_periods([_document(text)])
    assert len(periods) == 1
    assert periods[0].period_end == "2025-03-31"
    assert periods[0].period_type == "ANNUAL"
    values = dict(periods[0].metrics)
    assert values["net_change_in_cash"].value == Decimal("5657.52")
    assert values["cash_flow_from_operating_activities"].value == Decimal("8229.57")
    assert "cash_flow_from_investing_activities" not in values
    assert "cash_flow_from_financing_activities" not in values


def test_generic_reordered_statement_layout_maps_quarter_columns_not_annual_columns() -> None:
    text = "Amounts in Rs. Crore Statement of Audited Financial Results Particulars Year Ended Quarter Ended 31 March 2026 31 March 2025 31 March 2026 31 December 2025 31 March 2025 Revenue From Operations 27,284.15 27,152.14 7,335.75 6,661.13 6,722.83 Profit after Tax 6,427.00 6,100.00 1,684.31 1,500.00 1,300.00"
    result = latest_quarterly_result([_document(text)])
    assert result is not None
    assert result.period == "2026-03-31"
    assert result.revenue.value == Decimal("7335.75")
    assert result.pat.value == Decimal("1684.31")


def test_standalone_and_consolidated_statements_are_not_combined() -> None:
    text = "Amounts in Rs. Crore Standalone Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended 30 June 2026 31 March 2026 30 June 2025 31 March 2026 Revenue From Operations 100.00 90.00 80.00 350.00 Profit after Tax 10.00 9.00 8.00 30.00 Consolidated Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended 30 June 2026 31 March 2026 30 June 2025 31 March 2026 Revenue From Operations 200.00 190.00 180.00 700.00 Profit after Tax 20.00 19.00 18.00 60.00"
    result = latest_quarterly_result([_document(text)])
    assert result is not None
    assert result.reporting_basis == "STANDALONE"
    assert result.revenue.value == Decimal("100")


def test_ambiguous_or_unaligned_quarter_table_is_rejected_instead_of_fabricating_values() -> None:
    result = latest_quarterly_result([_document("Quarter Ended Particulars Revenue From Operations 8,261.11 Profit after Tax 1,927.21 Earning Per Share (of Rs 10 each) Basic 1.47")])
    assert result is None


def test_absurd_ocr_header_year_and_no_valid_metric_cells_produce_parse_no_result() -> None:
    document = _document("Statement of Unaudited Financial Results Particulars Quarter Ended Year Ended 30 September 2005 30 June 2005 30 September 2004 31 March 2005 Revenue From Operations Note 3")
    assert latest_quarterly_result([document]) is None
    repository = ResearchRepository()
    repository._persistence = _FactRecorder()
    assert repository._persist_official_financial_facts(document) == (False, 0)


def test_strict_ocr_numeric_cells_accept_whole_token_repairs_and_reject_partial_digits() -> None:
    assert _financial_number("7,335.7S") == Decimal("7335.75")
    assert _financial_number("Z7,284.1S") == Decimal("27284.15")
    assert _financial_number("6.661.13") == Decimal("6661.13")
    assert _financial_number("|.33S.rs") is None
    assert _financial_number("\\.9Z|.21") is None


def test_structural_row_classifier_distinguishes_pat_from_pbt_tax_and_comprehensive_income() -> None:
    assert _classify_financial_row("Profit for the period from continuing operations") == "PAT"
    assert _classify_financial_row("Profit before tax") == "PBT"
    assert _classify_financial_row("Proflt for the perlod fr0m continuinq operati0ns") == "PAT"
    assert _classify_financial_row("Tax expense") is None
    assert _classify_financial_row("Total comprehensive income for the period") is None
    assert "PROFIT" in _row_semantic_tokens("Proflt for the perlod")


def test_later_clean_statement_beats_earlier_corrupted_compact_statement() -> None:
    text = """Extract of Statement of Audited Financial Results Particulars Quarter Ended Year Ended 31St March 3lst December 31st M8rcli 31st March 31st March 2026 20ZS Z02S ZOZ6 Z025 Revenue F.om oDeraoons |.33S.rs 6,661.13 6.722.83 Z7,284.1S Z7,1SZ.14 Net ProfJt 1,684.31 1,cO2.19 I.681.87 7,009.17 6,502.00 Statement of Audited Financial Results for the quarter and year ended 31st March 2026 Particulars Quarter Ended Year Ended 31st March 31st December 31st March 31st March 31st March 2026 2025 2025 2026 2025 Total Revenue From operations 7,335.75 6,661.13 6,722.83 27,284.15 27,152.14 Profit for the period From continuing operations 1,684.31 1,802.19 1,681.87 7,009.17 6,502.00 Earnings per equity share Basic 1.29 1.38 1.29 5.36 4.98"""
    result = latest_quarterly_result([_document(text)])
    assert result is not None
    assert result.period == "2026-03-31"
    assert result.revenue.value == Decimal("7335.75")
    assert result.pat.value == Decimal("1684.31")
    assert result.eps.value == Decimal("1.29")


def test_later_clean_june_statement_beats_compact_extract_without_repairing_bad_pat() -> None:
    text = """Extract of Statement of Unaudited Financial Results for the quarter ended 30 June 2026 Particulars Quarter Ended Year Ended 30th June 31st March 30th June 31st March Z026 2026 2025 2026 Revenue From Operations 8,261.11 7,335.7S 6,915.38 27,284.1S Net Pro{it for the period after Tax \\.9Z|.21 1,684.31 1,745.69 i.009.\1 Earning Per Share (of Rs 10 each) Basic (Rs.) 1.47 1.29 1.34 5.36 Statement of Unaud.ited Financial Results for the quarter ended 30th June ZOZ6 (Amounts in Rs. Crore, unless stated otherwise) Particulars Quarter Ended Year Ended 30tli June 31st March 30th June 31st March Z026 20Z6 2025 2026 (I) Total Revenue From operations 8,261.11 7,335.75 6,915.38 27,Z84.15 (X) Profit for the period From continuing operations (VIII-IX) 1,927.21 1,684.31 1,745.69 7,009.17 Earnings per equity share (Face Value of Rs.10/- per share) -Basic (Rs.) 1.47 1.29 1.34 5.36"""
    result = latest_quarterly_result([_document(text)])
    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("8261.11")
    assert result.pat.value == Decimal("1927.21")
    assert result.eps.value == Decimal("1.47")
    assert result.revenue.value != Decimal("27284.15")
    assert result.eps.value != Decimal("10")


def test_production_shaped_june_text_selects_later_full_statement_over_compact_extract() -> None:
    text = """PDF_PAGE 1 Extract of Statement of Unaudited Financial Results for the quaiter ended 30\" Jiine 2026 (Amounts in Rs. Crore, unless stated otherwise) Particulars Quarter Ended Year Ended 30th Ju ne 31st March 30th June 31st March Z026 2026 2025 2026 Unaudited Audited Unaud-ited Audited (I) Revenue From Operations 8,261.11 7,335.7S 6,915.38 27,284.1S (VIII) Profit Before Exceptional Items and Tax 1,927.21 1,684.31 1,745.69 7,009.17 Net Profit for the period after Tax \\.9Z|.21 1,684.31 1,745.69 i.009.\1 Basic EPS 1.47 1.29 1.34 5.36 PDF_PAGE 3 Statement of Unaud.ited Financial Results for the quarter ended 30th June ZOZ6 (Amounts in Rs. Crore, unless stated otherwise) Particulars Quarter Ended Year Ended 30tli June 31st March 30th June 31st March Z026 20Z6 2025 2026 Unaudited Audited Unaudited Audited (I) Total Revenue From operations 8,261.11 7,335.75 6,915.38 27,284.15 (VIII) Profit Before Exceptional Items and Tax 1,927.21 1,684.31 1,745.69 7,009.17 (X) Profit for the period From contjp▒▒i?gop,engtions (VIII-lx) 1,927.21 1,684.31 1,745.69 7,009.17 Other Comprehensive Income (B) (I) Items that will be reclassified to profit or loss -Effective portion of gains and loss on cash flow hedging instruments Earnings per equity share (Face Value of Rs.10/- per share) Basic EPS 1.47 1.29 1.34 5.36"""
    document = _document(text)
    compact_start = text.index("Extract of Statement")
    full_start = text.index("Statement of Unaud.ited")
    compact = _parse_statement_candidate(text, document.published_at, compact_start, True)
    full = _parse_statement_candidate(text, document.published_at, full_start, True)
    assert compact is not None
    assert full is not None
    assert "Basic EPS" in full.region
    assert _statement_quality(compact) == (2, 0, 4, 1)
    assert _statement_quality(full) > _statement_quality(compact)
    statement = _parse_nse_income_statement(text, document.published_at)
    assert statement is not None
    assert statement.region.lstrip().startswith("Statement of Unaud.ited")
    result = latest_quarterly_result([document])
    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("8261.11")
    assert result.pat.value == Decimal("1927.21")
    assert result.eps.value == Decimal("1.47")
    assert _financial_number("\\.9Z|.21") is None


def test_production_shaped_march_text_accepts_clean_audlted_heading_after_corrupt_extract() -> None:
    text = """PDF_PAGE 1 Extract af Statement Of Andlted FJnanclal Results for the quarter and Year ended 31st Ma.ch 2026 Partlculars Quarte. Ended Year Ended 31St March 3lst De(ember 31st M8rcli 31st March 31st March 2026 20ZS Z02S ZOZ6 Z025 Revenue F.om oDeraoons |.33S.rs 6,661.13 6.722.83 Z7,284.1S Z7,1SZ.14 PDF_PAGE 3 Statement of Audlted Fl nclal Results for the quarter and year ended 315t March 2026 Particulars Quarter Ended Year Ended Year Ended 31st March 3lst December 3Lst March 31st March 31st March 2026 2025 2025 2026 2025 Total Revenue From operations 7,335.75 6,661.13 6,722.83 27,284.15 27,152.14 Profit for the period 1,684.31 1,802.19 1,681.87 7,009.17 6,502.00 Earnings per equity share Basic 1.29 1.38 1.29 5.36 4.98"""
    document = _document(text)
    full_start = text.index("Statement of Audlted")
    assert _STATEMENT_HEADING.search(text, full_start).start() == full_start
    header = "Quarter Ended Year Ended Year Ended 31st March 3lst December 3Lst March 31st March 31st March 2026 2025 2025 2026 2025"
    dates = _header_dates(header, document.published_at, ["quarter", "year", "year"])
    assert dates == ["2026-03-31", "2025-12-31", "2025-03-31", "2026-03-31", "2025-03-31"]
    assert [(column.period_end, column.period_type) for column in _financial_columns(["quarter", "year"], dates)] == [
        ("2026-03-31", "QUARTERLY"), ("2025-12-31", "QUARTERLY"), ("2025-03-31", "QUARTERLY"),
        ("2026-03-31", "ANNUAL"), ("2025-03-31", "ANNUAL"),
    ]
    full = _parse_statement_candidate(text, document.published_at, full_start, True)
    assert full is not None
    assert [(column.index, column.period_end, column.period_type) for column in full.columns] == [
        (0, "2026-03-31", "QUARTERLY"), (1, "2025-12-31", "QUARTERLY"),
        (2, "2025-03-31", "QUARTERLY"), (3, "2026-03-31", "ANNUAL"),
        (4, "2025-03-31", "ANNUAL"),
    ]
    assert full.current_column.index == 0
    statement = _parse_nse_income_statement(text, document.published_at)
    assert statement is not None
    assert statement.region.lstrip().startswith("Statement of Audlted")
    result = latest_quarterly_result([document])
    assert result is not None
    assert result.period == "2026-03-31"
    assert result.revenue.value == Decimal("7335.75")
    assert result.pat.value == Decimal("1684.31")
    assert result.eps.value == Decimal("1.29")
    assert _financial_number("|.33S.rs") is None


def test_production_shaped_december_text_accepts_ocr_heading_and_quarter_nine_month_columns() -> None:
    text = """PDF_PAGE 2 Statement Of umaudtoed Flnanclal Results for the quarter and nine month ended 31 De[embei 2025 P8mculars Quar`er Ended Nlne Momh Ended Year Ended 31 December 30 September 31 December 31 December 31 December 31 March 2025 2025 2024 2025 2024 2025 Revenue From operations 6.661.13 6,371.89 6,763.43 19,948.40 20,428.40 27,152.14 Profit for the period From continuing operations 1,802.19 1,776.98 1,630.66 5,324.86 4,820.13 6,502.00 PDF_PAGE 4 Statemeot of Unaudlted Financial Results for the quarter and nine month ended 31 Deceli`I]er 2025 parriculars Quarter Ended Nine Month Ended year Ended 31 December 30 Se|)tember 31 December 31 D▒cembe] 31 December Slat March 2025 2025 20Z4 20Z5 2024 2025 Total Revenue From operations 6,661.1304 6,371.89 6,763.43 19,948.40024 20,428.40032 27,152.14072 Profit for the period From continuing operations 1,802.19 1,776.98 1,630.66 5,324.86 4,820.13 6,502.00 PDF_PAGE 8 Statement of unaudited financial results auditor report Press release PAT Rs 1,802.19 crore"""
    document = _document(text)
    full_start = text.index("Statemeot")
    assert _STATEMENT_HEADING.search(text, full_start).start() == full_start
    header = "Quarter Ended Nine Month Ended year Ended 31 December 30 Se|)tember 31 December 31 D▒cembe] 31 December Slat March 2025 2025 20Z4 20Z5 2024 2025"
    dates = _header_dates(header, document.published_at, ["quarter", "nine month", "year"])
    assert dates == ["2025-12-31", "2025-09-30", "2024-12-31", "2025-12-31", "2024-12-31", "2025-03-31"]
    assert [column.period_type for column in _financial_columns(["quarter", "nine month", "year"], dates)] == ["QUARTERLY", "QUARTERLY", "QUARTERLY", "NINE_MONTH", "NINE_MONTH", "ANNUAL"]
    full = _parse_statement_candidate(text, document.published_at, full_start, True)
    assert full is not None
    assert full.current_column.period_end == "2025-12-31"
    assert full.current_column.period_type == "QUARTERLY"
    statement = _parse_nse_income_statement(text, document.published_at)
    assert statement is not None
    assert statement.region.lstrip().startswith("Statement Of umaudtoed")
    assert "Press release" not in statement.region
    result = latest_quarterly_result([document])
    assert result is not None
    assert result.period == "2025-12-31"
    assert result.revenue.value == Decimal("6661.13")
    assert result.pat.value == Decimal("1802.19")
    assert result.revenue.value != Decimal("19500")


def test_exact_irfc_normalized_text_fixtures_extract_official_quarterly_facts() -> None:
    fixtures = {
        "irfc_2026_06_30_normalized.txt": ("2026-06-30", "8261.11", "1927.21", "1.47"),
        "irfc_2026_03_31_normalized.txt": ("2026-03-31", "7335.75", "1684.31", "1.29"),
        "irfc_2025_12_31_normalized.txt": ("2025-12-31", "6661.13", "1802.19", None),
    }
    root = Path(__file__).parent / "fixtures" / "nse" / "irfc"
    for filename, (period, revenue, pat, eps) in fixtures.items():
        result = latest_quarterly_result([_document((root / filename).read_text(encoding="utf-8"))])
        assert result is not None, filename
        assert result.period == period
        assert result.revenue.value == Decimal(revenue)
        assert result.pat.value == Decimal(pat)
        if eps is not None:
            assert result.eps.value == Decimal(eps)


def test_direct_aligned_eps_outranks_fallback_regardless_of_candidate_order(monkeypatch) -> None:
    text = (Path(__file__).parent / "fixtures" / "nse" / "irfc" / "irfc_2026_06_30_normalized.txt").read_text(encoding="utf-8")
    document = _document(text)
    original = structured_research._nse_statement_candidates
    candidates = original(text, document.published_at, require_quarterly=False)
    assert len(candidates) >= 2
    monkeypatch.setattr(
        structured_research,
        "_nse_statement_candidates",
        lambda *_args, **_kwargs: list(reversed(candidates)),
    )

    periods = parsed_nse_income_statement_periods([document])
    values = {
        (period.period_end, period.period_type): dict(period.metrics)
        for period in periods
    }
    assert values[("2026-03-31", "ANNUAL")]["eps"].value == Decimal("5.36")
    assert values[("2026-06-30", "QUARTERLY")]["eps"].value == Decimal("1.47")
    assert values[("2026-03-31", "QUARTERLY")]["eps"].value == Decimal("1.29")


def test_eps_fallback_remains_available_without_a_direct_aligned_basic_row() -> None:
    text = _multi_period_statement("30 June 2026", "31 March 2026", "30 June 2025", "31 March 2026")
    text += " 1.47 1.29 1.34 5.36 Earnings per equity share (Face Value of Rs.10/- per share) -Basic (Rs.)"

    periods = parsed_nse_income_statement_periods([_document(text)])
    values = {
        (period.period_end, period.period_type): dict(period.metrics)
        for period in periods
    }
    assert values[("2026-03-31", "ANNUAL")]["eps"].value == Decimal("5.36")


def test_direct_aligned_diluted_eps_outranks_malformed_fallback_when_basic_is_unaligned() -> None:
    text = (Path(__file__).parent / "fixtures" / "nse" / "irfc" / "irfc_2026_03_31_normalized.txt").read_text(encoding="utf-8")

    periods = parsed_nse_income_statement_periods([_document(text)])
    values = {
        (period.period_end, period.period_type): dict(period.metrics)
        for period in periods
    }
    assert values[("2026-03-31", "ANNUAL")]["eps"].value == Decimal("5.36")
    assert values[("2025-03-31", "ANNUAL")]["eps"].value == Decimal("4.98")
    assert values[("2025-12-31", "QUARTERLY")]["eps"].value == Decimal("1.38")


class _FactRecorder:
    def __init__(self): self.facts = []
    def load_financial_facts(self): return self.facts
    def upsert_financial_fact(self, fact, **_kwargs): self.facts.append(fact); return True


def test_repository_persists_official_nse_fact_with_canonical_period_end() -> None:
    repository = ResearchRepository()
    recorder = _FactRecorder()
    repository._persistence = recorder
    repository._persist_official_financial_facts(_document(JUNE))
    revenue = next(fact for fact in recorder.facts if fact.key.metric == "revenue")
    eps = next(fact for fact in recorder.facts if fact.key.metric == "eps")
    assert revenue.key.period_end == "2026-06-30"
    assert revenue.source_tier == FactSourceTier.OFFICIAL_NSE
    assert revenue.source_provider == "NSE"
    assert revenue.value.source_url == "https://nsearchives.nseindia.com/corporate/result.pdf"
    assert eps.key.period_end == "2026-06-30"
    assert eps.value.value == Decimal("1.47")
    assert eps.value.unit == "INR per share"
    assert all(fact.key.metric != "debt_or_borrowings" for fact in recorder.facts)


def _document_fact(document: ResearchDocument, metric: str, period: str, period_type: str, basis: str | None, value: str) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(document.instrument_id, metric, period, period_type, basis),
        ProvenancedValue(
            value=Decimal(value), unit="INR crore", source_url=document.canonical_url,
            source_name=document.source_name, source_type=str(document.source_type), retrieved_at=document.retrieved_at,
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", str(document.document_id), SourceMode.REAL,
    )


def test_document_scoped_reconciliation_replaces_deepak_values_without_touching_other_document_facts() -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(DEEPAK_DECEMBER_2025).model_copy(update={
        "document_id": UUID("2ebb7d9a-64ce-4431-93ca-2585d16d6252"),
    })
    # The persisted source text proves the standalone comparative revenue is
    # 551.59; seed the observed stale 551.00 alongside the PAT row references.
    for basis in ("CONSOLIDATED", "STANDALONE"):
        repository._persistence.upsert_financial_fact(_document_fact(document, "pat", "2025-12-31", "QUARTERLY", basis, "5"))
        repository._persistence.upsert_financial_fact(_document_fact(document, "pat", "2024-12-31", "QUARTERLY", basis, "6"))
    repository._persistence.upsert_financial_fact(_document_fact(document, "revenue", "2024-12-31", "QUARTERLY", "STANDALONE", "551.00"))
    unrelated = FinancialFact(
        FinancialFactKey(document.instrument_id, "revenue", "2023-12-31", "QUARTERLY", "STANDALONE"),
        ProvenancedValue(value=Decimal("77"), unit="INR crore", source_url="https://other.example", source_name="Other", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", "other-official-document", SourceMode.REAL,
    )
    repository._persistence.upsert_financial_fact(unrelated)

    parsed, _written = repository._reconcile_persisted_official_financial_document(document)
    assert parsed
    facts = {fact.key: fact for fact in repository.financial_facts_for(document.instrument_id)}
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2025-12-31", "QUARTERLY", "CONSOLIDATED")].value.value == Decimal("99.82")
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2024-12-31", "QUARTERLY", "CONSOLIDATED")].value.value == Decimal("98.13")
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2025-12-31", "QUARTERLY", "STANDALONE")].value.value == Decimal("8.14")
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2024-12-31", "QUARTERLY", "STANDALONE")].value.value == Decimal("17.25")
    assert facts[FinancialFactKey(document.instrument_id, "revenue", "2024-12-31", "QUARTERLY", "STANDALONE")].value.value == Decimal("551.59")
    assert facts[unrelated.key].source_identity == "other-official-document"
    assert all(fact.source_identity == str(document.document_id) for key, fact in facts.items() if key != unrelated.key)
    before = list(repository.financial_facts_for(document.instrument_id))
    assert repository._reconcile_persisted_official_financial_document(document)[0]
    assert repository.financial_facts_for(document.instrument_id) == before


def test_document_scoped_reconciliation_deletes_electronics_mart_obsolete_annual_keys_only_for_that_source() -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(ELECTRONICS_MART_DECEMBER_2025).model_copy(update={
        "document_id": UUID("df9d629e-a0ee-4290-ada9-bbb9da6d06df"),
    })
    for period, value in (("2025-12-31", "335.04"), ("2024-12-31", "674.04")):
        repository._persistence.upsert_financial_fact(_document_fact(document, "pat", period, "ANNUAL", "CONSOLIDATED", value))
    other = FinancialFact(
        FinancialFactKey(document.instrument_id, "pat", "2023-12-31", "ANNUAL", "CONSOLIDATED"),
        ProvenancedValue(value=Decimal("99"), unit="INR crore", source_url="https://other.example", source_name="Other", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", "other-official-document", SourceMode.REAL,
    )
    repository._persistence.upsert_financial_fact(other)

    assert repository._reconcile_persisted_official_financial_document(document)[0]
    facts = {fact.key: fact for fact in repository.financial_facts_for(document.instrument_id)}
    assert FinancialFactKey(document.instrument_id, "pat", "2025-12-31", "ANNUAL", "CONSOLIDATED") not in facts
    assert FinancialFactKey(document.instrument_id, "pat", "2024-12-31", "ANNUAL", "CONSOLIDATED") not in facts
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2025-12-31", "QUARTERLY", "CONSOLIDATED")].value.value == Decimal("335.04")
    assert facts[FinancialFactKey(document.instrument_id, "pat", "2024-12-31", "QUARTERLY", "CONSOLIDATED")].value.value == Decimal("161.42")
    assert facts[other.key].source_identity == "other-official-document"


def test_document_scoped_reconciliation_does_not_delete_when_parser_emits_no_valid_facts() -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document("NSE announcement without a supported financial table")
    stale = _document_fact(document, "pat", "2025-12-31", "QUARTERLY", None, "5")
    repository._persistence.upsert_financial_fact(stale)

    assert repository._reconcile_persisted_official_financial_document(document) == (False, 0)
    assert repository.financial_facts_for(document.instrument_id) == [stale]


def test_document_scoped_reconciliation_preserves_another_document_owning_the_same_semantic_key() -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(JUNE)
    existing = FinancialFact(
        FinancialFactKey(document.instrument_id, "revenue", "2026-06-30", "QUARTERLY", None),
        ProvenancedValue(value=Decimal("8261.11"), unit="INR crore", source_url="https://other.example", source_name="Other", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", "other-official-document", SourceMode.REAL,
    )
    repository._persistence.upsert_financial_fact(existing)

    assert repository._reconcile_persisted_official_financial_document(document)[0]
    fact = next(item for item in repository.financial_facts_for(document.instrument_id) if item.key == existing.key)
    assert fact.source_identity == "other-official-document"
    assert fact.value.value == Decimal("8261.11")


def test_document_scoped_reconciliation_rolls_back_on_persistence_failure(monkeypatch) -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(DEEPAK_DECEMBER_2025)
    stale = _document_fact(document, "pat", "2025-12-31", "ANNUAL", "CONSOLIDATED", "5")
    repository._persistence.upsert_financial_fact(stale)
    original = repository._persistence._write_financial_fact
    calls = 0

    def fail_second_write(fact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated persistence failure")
        original(fact)

    monkeypatch.setattr(repository._persistence, "_write_financial_fact", fail_second_write)
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        repository._reconcile_persisted_official_financial_document(document)
    assert repository.financial_facts_for(document.instrument_id) == [stale]


def test_repository_retains_financial_history_while_newest_windows_remain_available_with_document_provenance() -> None:
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    periods_by_year = [
        (2026, "30 June 2026", "31 March 2026", "30 June 2025"),
        (2025, "30 June 2025", "31 March 2025", "30 June 2024"),
        (2024, "30 June 2024", "31 March 2024", "30 June 2023"),
        (2023, "30 June 2023", "31 March 2023", "30 June 2022"),
        (2022, "30 June 2022", "31 March 2022", "30 June 2021"),
    ]
    documents = [
        _document(_multi_period_statement(current, prior, comparable, f"31 March {year}", revenue=f"{year - 1920}.00")).model_copy(
            update={"published_at": datetime(year, 8, 1, tzinfo=timezone.utc)}
        )
        for year, current, prior, comparable in periods_by_year
    ]
    for document in documents:
        repository._persist_official_financial_facts(document)

    facts = [fact for fact in repository.financial_facts_for(documents[0].instrument_id) if fact.key.metric == "revenue"]
    quarterly = {fact.key.period_end for fact in facts if fact.key.period_type == "QUARTERLY"}
    annual = {fact.key.period_end for fact in facts if fact.key.period_type == "ANNUAL"}
    assert {"2026-06-30", "2026-03-31", "2025-06-30", "2022-03-31"} <= quarterly
    assert annual == {"2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31"}
    assert len(quarterly) > 4
    assert all(fact.source_tier == FactSourceTier.OFFICIAL_NSE and fact.source_provider == "NSE" for fact in facts)
    assert all(fact.source_identity in {str(document.document_id) for document in documents} for fact in facts)
    latest_revenue = next(fact for fact in facts if fact.key.period_end == "2026-06-30" and fact.key.period_type == "QUARTERLY")
    assert latest_revenue.source_identity == str(documents[0].document_id)
    assert latest_revenue.value.source_url == documents[0].canonical_url
    total_before = len(repository.financial_facts_for(documents[0].instrument_id))
    before = len(facts)
    repository._persist_official_financial_facts(documents[0])
    assert len(repository.financial_facts_for(documents[0].instrument_id)) == total_before
    assert len([fact for fact in repository.financial_facts_for(documents[0].instrument_id) if fact.key.metric == "revenue"]) == before


def test_duplicate_official_period_does_not_overwrite_existing_document_provenance() -> None:
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    first = _document(_multi_period_statement("30 June 2026", "31 March 2026", "30 June 2025", "31 March 2026", revenue="100.00"))
    second = _document(_multi_period_statement("30 June 2026", "31 March 2026", "30 June 2025", "31 March 2026", revenue="999.00"))
    repository._persist_official_financial_facts(first)
    repository._persist_official_financial_facts(second)
    facts = [
        fact for fact in repository.financial_facts_for(first.instrument_id)
        if fact.key.metric == "revenue" and fact.key.period_end == "2026-06-30" and fact.key.period_type == "QUARTERLY"
    ]
    assert len(facts) == 1
    assert facts[0].value.value == Decimal("100.00")
    assert facts[0].source_identity == str(first.document_id)


def test_balance_sheet_persistence_retains_history_idempotently_and_keeps_document_provenance() -> None:
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    documents = [
        _document(_balance_sheet(current=f"31 March {year}", prior=f"31 March {year - 1}")).model_copy(
            update={"published_at": datetime(year, 8, 1, tzinfo=timezone.utc)}
        )
        for year in (2026, 2025, 2024, 2023, 2022)
    ]
    for document in documents:
        repository._persist_official_financial_facts(document)
    facts = [fact for fact in repository.financial_facts_for(documents[0].instrument_id) if fact.key.metric == "total_assets"]
    assert {fact.key.period_end for fact in facts} == {"2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31", "2021-03-31"}
    assert all(fact.key.period_type == "AS_AT" and fact.source_identity in {str(item.document_id) for item in documents} for fact in facts)
    before = len(repository.financial_facts_for(documents[0].instrument_id))
    repository._persist_official_financial_facts(documents[0])
    assert len(repository.financial_facts_for(documents[0].instrument_id)) == before


def test_duplicate_balance_sheet_period_does_not_overwrite_other_official_document() -> None:
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    first = _document(_balance_sheet())
    second = _document(_balance_sheet().replace("Total Assets 500.00", "Total Assets 999.00"))
    repository._persist_official_financial_facts(first)
    repository._persist_official_financial_facts(second)
    fact = next(
        item for item in repository.financial_facts_for(first.instrument_id)
        if item.key.metric == "total_assets" and item.key.period_end == "2026-03-31"
    )
    assert fact.value.value == Decimal("500")
    assert fact.source_identity == str(first.document_id)


def test_cash_flow_persistence_retains_history_and_preserves_official_provenance() -> None:
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    documents = [
        _document(_cash_flow(current=f"31 March {year}", prior=f"31 March {year - 1}")).model_copy(
            update={"published_at": datetime(year, 8, 1, tzinfo=timezone.utc)}
        )
        for year in (2026, 2025, 2024, 2023, 2022)
    ]
    for document in documents:
        repository._persist_official_financial_facts(document)
    facts = [fact for fact in repository.financial_facts_for(documents[0].instrument_id)
             if fact.key.metric == "cash_flow_from_operating_activities"]
    assert {fact.key.period_end for fact in facts} == {"2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31", "2021-03-31"}
    assert all(fact.key.period_type == "ANNUAL" and fact.source_identity in {str(item.document_id) for item in documents} for fact in facts)
    first = _document(_cash_flow())
    second = _document(_cash_flow().replace("100.00", "999.00", 1))
    repository._persist_official_financial_facts(first)
    repository._persist_official_financial_facts(second)
    fact = next(item for item in repository.financial_facts_for(first.instrument_id)
                if item.key.metric == "cash_flow_from_operating_activities" and item.key.period_end == "2026-03-31")
    assert fact.value.value == Decimal("100")


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "EXAMPLE"})


def _trusted_source(profile: CompanyResearchProfile) -> RegisteredResearchSource:
    return RegisteredResearchSource(source_id="nse-result", instrument_id=profile.instrument_id,
        url="https://nsearchives.nseindia.com/corporate/result.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE corporate announcements", publisher="NSE",
        reliability_level=ReliabilityLevel.LEVEL_A, domain="nsearchives.nseindia.com", company_id=profile.company_id,
        discovery_method="NSE_OFFICIAL_API", official_nse_profile_symbol="EXAMPLE", categories=("FINANCIAL_RESULTS",))


def test_reused_trusted_nse_document_reconciles_same_document_facts_without_fetch_or_events() -> None:
    profile = _profile()
    source = _trusted_source(profile)
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    document = _document(JUNE).model_copy(update={"canonical_url": source.url, "original_url": source.url})
    repository.documents[document.document_id] = document
    bad = FinancialFact(FinancialFactKey(profile.instrument_id, "eps", "2026-06-30", "QUARTERLY", None),
        ProvenancedValue(value=Decimal("10"), unit="INR crore", source_url=source.url, source_name="NSE", source_type="EXCHANGE", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", str(document.document_id), SourceMode.REAL)
    repository._persistence.upsert_financial_fact(bad)
    events_before = len(repository.events)
    documents_before = len(repository.documents)

    asyncio.run(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))
    facts = repository.financial_facts_for(profile.instrument_id)
    eps = next(fact for fact in facts if fact.key.metric == "eps")
    assert eps.value.value == Decimal("1.47")
    assert eps.value.unit == "INR per share"
    assert next(fact for fact in facts if fact.key.metric == "revenue").value.value == Decimal("8261.11")
    assert next(fact for fact in facts if fact.key.metric == "pat").value.value == Decimal("1927.21")
    assert len(repository.events) == events_before
    assert len(repository.documents) == documents_before

    asyncio.run(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))
    assert len(repository.financial_facts_for(profile.instrument_id)) == len(facts)
    yahoo = FinancialFact(eps.key, ProvenancedValue(value=Decimal("9"), source_url="https://yahoo.test", source_name="Yahoo", source_type="YAHOO", retrieved_at=document.retrieved_at), FactSourceTier.YAHOO, "YAHOO_FINANCE", "yahoo", SourceMode.REAL)
    assert not repository._persistence.upsert_financial_fact(yahoo)
    assert next(fact for fact in repository.financial_facts_for(profile.instrument_id) if fact.key.metric == "eps").value.value == Decimal("1.47")


def test_already_persisted_official_document_recovers_zero_facts_without_redownload() -> None:
    profile = _profile()
    source = _trusted_source(profile)
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(JUNE).model_copy(update={"canonical_url": source.url, "original_url": source.url})
    repository.documents[document.document_id] = document
    documents_before = len(repository.documents)
    parser_calls = 0
    network_calls = 0
    original_persist = repository._persist_official_financial_facts

    def record_parser(reused_document):
        nonlocal parser_calls
        parser_calls += 1
        return original_persist(reused_document)

    async def no_network(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("an already persisted official document must not be downloaded again")

    repository._persist_official_financial_facts = record_parser  # type: ignore[method-assign]
    repository._single_flight_official_filing = no_network  # type: ignore[method-assign]

    asyncio.run(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))

    facts_after_recovery = repository.financial_facts_for(profile.instrument_id)
    assert parser_calls == 1
    assert network_calls == 0
    assert len(repository.documents) == documents_before
    assert {fact.key.metric for fact in facts_after_recovery} >= {"revenue", "pat", "eps"}

    asyncio.run(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))
    assert network_calls == 0
    assert len(repository.documents) == documents_before
    assert len(repository.financial_facts_for(profile.instrument_id)) == len(facts_after_recovery)


def test_reused_official_document_repairs_missing_facts_without_degrading_existing_official_revenue() -> None:
    profile = _profile()
    source = _trusted_source(profile)
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document(JUNE).model_copy(update={"canonical_url": source.url, "original_url": source.url})
    repository.documents[document.document_id] = document
    revenue = next(
        value for period in parsed_nse_income_statement_periods([document])
        if period.period_end == "2026-06-30" and period.period_type == "QUARTERLY"
        for metric, value in period.metrics if metric == "revenue"
    )
    existing_revenue = FinancialFact(
        FinancialFactKey(profile.instrument_id, "revenue", "2026-06-30", "QUARTERLY", None),
        revenue,
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        "older-official-document",
        SourceMode.REAL,
    )
    repository._persistence.upsert_financial_fact(existing_revenue)
    network_calls = 0

    async def no_network(*_args, **_kwargs):
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("persisted evidence must repair missing facts without a provider request")

    repository._single_flight_official_filing = no_network  # type: ignore[method-assign]
    asyncio.run(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))

    facts = {fact.key.metric: fact for fact in repository.financial_facts_for(profile.instrument_id)
             if fact.key.period_end == "2026-06-30" and fact.key.period_type == "QUARTERLY"}
    assert network_calls == 0
    assert facts["revenue"].source_identity == "older-official-document"
    assert facts["revenue"].value.value == Decimal("8261.11")
    assert facts["pat"].value.value == Decimal("1927.21")
    assert facts["eps"].value.value == Decimal("1.47")


def test_persisted_official_document_with_no_parseable_financial_result_creates_no_facts() -> None:
    profile = _profile()
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    document = _document("NSE exchange announcement: board meeting outcome and management commentary only.")
    repository.documents[document.document_id] = document
    parser_calls = 0
    original_persist = repository._persist_official_financial_facts

    def record_parser(reused_document):
        nonlocal parser_calls
        parser_calls += 1
        return original_persist(reused_document)

    repository._persist_official_financial_facts = record_parser  # type: ignore[method-assign]

    assert asyncio.run(repository._reconcile_incomplete_persisted_official_financial_facts(profile)) is False
    assert parser_calls == 0
    assert repository.financial_facts_for(profile.instrument_id) == []


def test_reused_official_reconciliation_does_not_block_the_event_loop() -> None:
    profile = _profile()
    source = _trusted_source(profile)
    repository = ResearchRepository()
    document = _document(JUNE).model_copy(update={"canonical_url": source.url, "original_url": source.url})
    repository.documents[document.document_id] = document
    started = threading.Event()

    def slow_reconcile(*_args) -> None:
        started.set()
        time.sleep(0.08)

    repository._reconcile_reused_official_financial_facts = slow_reconcile

    async def run() -> int:
        task = asyncio.create_task(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()))
        while not started.is_set():
            await asyncio.sleep(0)
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.005)
            ticks += 1
        await task
        return ticks

    assert asyncio.run(run()) > 0


def test_async_yahoo_fact_persistence_preserves_fact_precedence_payload() -> None:
    repository = ResearchRepository()
    recorder = _FactRecorder()
    repository._persistence = recorder
    snapshot = SimpleNamespace(
        resolution=SimpleNamespace(provider_ticker="EXAMPLE.NS"),
        statement_facts=[{
            "metric": "revenue",
            "value": Decimal("100"),
            "periodEnd": "2026-06-30",
            "periodType": "QUARTERLY",
            "unit": "INR crore",
            "sourceUrl": "https://finance.yahoo.test/EXAMPLE.NS",
            "sourceName": "Yahoo Finance",
            "sourceType": "YAHOO_FINANCE",
            "retrievedAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "confidence": Decimal("0.8"),
        }],
    )

    asyncio.run(repository.persist_yahoo_statement_facts_async(_profile().instrument_id, snapshot))

    assert len(recorder.facts) == 1
    fact = recorder.facts[0]
    assert fact.source_tier == FactSourceTier.YAHOO
    assert fact.source_identity == "EXAMPLE.NS:revenue:2026-06-30:QUARTERLY"
    assert fact.key.period_end == "2026-06-30"


def test_reused_untrusted_or_textless_document_cannot_create_facts() -> None:
    profile = _profile()
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    source = _trusted_source(profile)
    untrusted = source.__class__(**{**source.__dict__, "official_nse_profile_symbol": None})
    repository._reconcile_reused_official_financial_facts(profile, untrusted, _document(JUNE).model_copy(update={"canonical_url": source.url}))
    repository._reconcile_reused_official_financial_facts(profile, source, _document(JUNE).model_copy(update={"normalized_text": ""}))
    assert repository.financial_facts_for(profile.instrument_id) == []


def test_persisted_trusted_financial_document_repairs_zero_normalized_facts_idempotently() -> None:
    profile = _profile()
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    document = _document(JUNE)
    repository.documents[document.document_id] = document
    first = asyncio.run(repository._incomplete_persisted_official_financial_documents(profile))
    assert first == [document]
    asyncio.run(repository._run_blocking_persistence(repository._persist_official_financial_facts, document))
    assert {fact.key.metric for fact in repository.financial_facts_for(profile.instrument_id)} >= {"revenue", "pat"}
    assert asyncio.run(repository._incomplete_persisted_official_financial_documents(profile)) == []


def test_no_due_refresh_repairs_persisted_official_facts_without_discovery() -> None:
    profile = _profile()
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repository.profiles = [profile]
    document = _document(JUNE)
    repository.documents[document.document_id] = document
    fresh_gate = _InstrumentRefreshGate(set(), False, False, "FRESH_AND_COMPLETE")
    repository._instrument_refresh_gate = lambda *args, **kwargs: fresh_gate  # type: ignore[method-assign]
    calls = {"official": 0, "search": 0, "registered": 0}

    async def official_discover(*args, **kwargs):
        calls["official"] += 1
        return []

    async def search_discover(*args, **kwargs):
        calls["search"] += 1
        return []

    async def fetch_registered(*args, **kwargs):
        calls["registered"] += 1
        raise AssertionError("fresh repair must not fetch a source")

    repository._official_filing_discovery.discover = official_discover  # type: ignore[method-assign]
    repository._discovery.discover = search_discover  # type: ignore[method-assign]
    repository._fetch_registered_source = fetch_registered  # type: ignore[method-assign]

    asyncio.run(repository.refresh(profile.instrument_id, allow_demo=False))

    assert {fact.key.metric for fact in repository.financial_facts_for(profile.instrument_id)} >= {"revenue", "pat"}
    assert calls == {"official": 0, "search": 0, "registered": 0}
    writes = 0
    original_persist = repository._persist_official_financial_facts

    def count_persist(reused_document):
        nonlocal writes
        writes += 1
        return original_persist(reused_document)

    repository._persist_official_financial_facts = count_persist  # type: ignore[method-assign]
    asyncio.run(repository.refresh(profile.instrument_id, allow_demo=False))
    assert writes == 0


def test_long_absence_keeps_existing_financial_provider_path_eligible_after_local_reconcile() -> None:
    profile = _profile()
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False), persistence=SqliteResearchPersistence())
    repository.profiles = [profile]
    old = _document(_multi_period_statement("30 June 2025", "31 March 2025", "31 December 2024", "31 March 2025"))
    repository.documents[old.document_id] = old
    repository._persist_official_financial_facts(old)
    repository._instrument_refresh_gate = lambda *args, **kwargs: _InstrumentRefreshGate({"FINANCIAL_RESULTS"}, False, False, "STALE")  # type: ignore[method-assign]
    calls = {"discover": 0, "provider": 0}
    source = _trusted_source(profile)
    async def discover(*_args, **_kwargs):
        calls["discover"] += 1
        return [DiscoveryResult("FINANCIAL_RESULTS", source)]
    async def provider(*_args, **_kwargs):
        calls["provider"] += 1
        return True
    repository._official_filing_discovery.discover = discover  # type: ignore[method-assign]
    repository._fetch_official_filings = provider  # type: ignore[method-assign]
    repository._discovery.discover = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

    asyncio.run(repository._refresh_live(profile.instrument_id, set()))
    assert calls == {"discover": 1, "provider": 1}


def test_persisted_trusted_financial_document_with_revenue_and_pat_is_complete_without_eps() -> None:
    profile = _profile()
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    document = _document(JUNE)
    repository.documents[document.document_id] = document
    repository._persist_official_financial_facts(document)
    for fact in list(repository.financial_facts_for(profile.instrument_id)):
        if fact.key.metric == "eps":
            repository._persistence._connection.execute("DELETE FROM global_financial_facts WHERE metric = 'eps'")
            repository._persistence._connection.commit()
    assert asyncio.run(repository._incomplete_persisted_official_financial_documents(profile)) == []


def test_latest_trusted_period_repairs_even_when_older_period_is_complete() -> None:
    profile = _profile()
    repository = ResearchRepository()
    repository._persistence = SqliteResearchPersistence()
    root = Path(__file__).parent / "fixtures" / "nse" / "irfc"
    older = _document((root / "irfc_2026_03_31_normalized.txt").read_text(encoding="utf-8"))
    latest = _document(JUNE)
    repository.documents[older.document_id] = older
    repository.documents[latest.document_id] = latest
    repository._persist_official_financial_facts(older)

    assert asyncio.run(repository._incomplete_persisted_official_financial_documents(profile)) == [latest]
    repository._persist_official_financial_facts(latest)
    assert asyncio.run(repository._incomplete_persisted_official_financial_documents(profile)) == []


def test_non_nse_profile_does_not_activate_fresh_financial_repair() -> None:
    profile = _profile().model_copy(update={"exchange": "XAMS", "country": "NL", "provider_instrument_ids": {}})
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repository.profiles = [profile]
    document = _document(JUNE)
    repository.documents[document.document_id] = document
    repository._instrument_refresh_gate = lambda *args, **kwargs: _InstrumentRefreshGate(set(), False, False, "FRESH_AND_COMPLETE")  # type: ignore[method-assign]
    writes = 0
    original_persist = repository._persist_official_financial_facts

    def count_persist(reused_document):
        nonlocal writes
        writes += 1
        return original_persist(reused_document)

    repository._persist_official_financial_facts = count_persist  # type: ignore[method-assign]
    asyncio.run(repository.refresh(profile.instrument_id, allow_demo=False))
    assert writes == 0
    assert repository.financial_facts_for(profile.instrument_id) == []


def test_rolling_window_recovers_newer_quarters_after_one_year_absence_and_retains_history() -> None:
    profile = _profile()
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    old_documents = [
        _document(_multi_period_statement("30 June 2025", "31 March 2025", "31 December 2024", "31 March 2025")),
        _document(_multi_period_statement("30 September 2024", "30 June 2024", "31 March 2024", "31 March 2024")),
    ]
    for document in old_documents:
        repository.documents[document.document_id] = document
        repository._persist_official_financial_facts(document)
    current_documents = _rolling_financial_documents()
    for document in current_documents:
        repository.documents[document.document_id] = document

    assert asyncio.run(repository._incomplete_persisted_official_financial_documents(profile)) == current_documents
    assert asyncio.run(repository._reconcile_incomplete_persisted_official_financial_facts(profile)) is True
    quarterly = {
        fact.key.period_end for fact in repository.financial_facts_for(profile.instrument_id)
        if fact.key.metric == "revenue" and fact.key.period_type == "QUARTERLY"
    }
    assert {"2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"} <= quarterly
    assert {"2025-06-30", "2024-09-30"} <= quarterly
    before = len(repository.financial_facts_for(profile.instrument_id))
    assert asyncio.run(repository._reconcile_incomplete_persisted_official_financial_facts(profile)) is False
    assert len(repository.financial_facts_for(profile.instrument_id)) == before


def test_rolling_window_repairs_historical_gap_and_fresh_path_never_calls_provider() -> None:
    profile = _profile()
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False), persistence=SqliteResearchPersistence())
    repository.profiles = [profile]
    documents = _rolling_financial_documents()
    for document in documents:
        repository.documents[document.document_id] = document
        repository._persist_official_financial_facts(document)
    repository._persistence._connection.execute("DELETE FROM global_financial_facts WHERE period_end = '2026-03-31'")
    repository._persistence._connection.commit()
    repository._instrument_refresh_gate = lambda *args, **kwargs: _InstrumentRefreshGate(set(), False, False, "FRESH_AND_COMPLETE")  # type: ignore[method-assign]
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("persisted evidence repair must not call a provider")
    repository._fetch_registered_source = forbidden  # type: ignore[method-assign]
    repository._official_filing_discovery.discover = forbidden  # type: ignore[method-assign]
    repository._discovery.discover = forbidden  # type: ignore[method-assign]

    asyncio.run(repository._refresh_live(profile.instrument_id, set()))
    recovered = {(fact.key.period_end, fact.key.metric) for fact in repository.financial_facts_for(profile.instrument_id)}
    assert {("2026-03-31", "revenue"), ("2026-03-31", "pat")} <= recovered


def test_rolling_window_is_basis_isolated_and_malformed_document_does_not_block_valid_recovery() -> None:
    profile = _profile()
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    malformed = _document(INVALID_CALENDAR_HEADER)
    standalone = _document("STANDALONE " + _multi_period_statement("30 June 2026", "31 March 2026", "31 December 2025", "31 March 2026"))
    consolidated = _document("CONSOLIDATED " + _multi_period_statement("30 September 2025", "30 June 2025", "31 March 2025", "31 March 2025"))
    for document in (malformed, standalone, consolidated):
        repository.documents[document.document_id] = document

    selected = asyncio.run(repository._incomplete_persisted_official_financial_documents(profile))
    assert selected == [standalone, consolidated]
    asyncio.run(repository._reconcile_incomplete_persisted_official_financial_facts(profile))
    facts = repository.financial_facts_for(profile.instrument_id)
    assert {fact.key.reporting_basis for fact in facts if fact.key.period_type == "QUARTERLY"} == {"STANDALONE", "CONSOLIDATED"}
