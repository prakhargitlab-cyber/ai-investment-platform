"""Offline-only tests for the bounded SBI TER workbook adapter.

No network call is made anywhere in this file. The real, unchanged SBI
"Current Year TER for All Schemes" workbook the user supplied is read
directly from `.tmp/etf-validation/current-year-ter.xlsx` -- guarded by
pytest.skip() exactly like the existing genuine-document tests, since
`.tmp/` is gitignored and may not exist in every checkout. All negative
tests build small synthetic workbooks in-memory with openpyxl so they do
not depend on that fixture being present.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import openpyxl
import pytest

from app.etf_evidence import EtfMetric
from app.persistence import SqliteResearchPersistence
from app.sbi_ter_workbook_adapter import (
    EXPECTED_HEADERS,
    AdapterOutcome,
    BoundedSbiTerAdapter,
    SchemeClassification,
    WorkbookSchemaError,
    build_staged_observations,
    classify_scheme_name,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
REAL_WORKBOOK = Path(__file__).resolve().parents[1] / ".tmp" / "etf-validation" / "current-year-ter.xlsx"


def _write_workbook(path, rows, *, headers=EXPECTED_HEADERS):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)


def _row(code, name, ter_date, regular_total, direct_total):
    return (code, name, ter_date, 0, 0, 0, 0, regular_total, 0, 0, 0, 0, direct_total)


# ---------------------------------------------------------------------------
# Classification (Task 1 / "distinguish ETF rows from ETF fund-of-funds")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("SBI Nifty 50 ETF", SchemeClassification.ETF),
    ("SBI Nifty 50 ETF ", SchemeClassification.ETF),  # trailing-space variant seen in the real workbook
    ("SBI BSE Sensex ETF", SchemeClassification.ETF),
    ("SBI Nifty200 Value 30 ETF FOF", SchemeClassification.ETF_FUND_OF_FUND),
    ("SBI Silver ETF Fund of Fund", SchemeClassification.ETF_FUND_OF_FUND),
    ("SBI Equity Hybrid Fund", SchemeClassification.OTHER),
    ("SBI Nifty Index Fund", SchemeClassification.OTHER),
])
def test_classify_scheme_name(name, expected):
    assert classify_scheme_name(name) == expected


# ---------------------------------------------------------------------------
# Input-contract / schema enforcement
# ---------------------------------------------------------------------------

def test_wrong_sheet_name_is_rejected(tmp_path):
    path = tmp_path / "bad.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Sheet0"
    workbook.save(path)
    with pytest.raises(WorkbookSchemaError, match="UNSUPPORTED_SHEET_NAME"):
        build_staged_observations(path)


def test_wrong_header_row_is_rejected(tmp_path):
    path = tmp_path / "bad.xlsx"
    _write_workbook(path, [], headers=("Scheme Code", "Name"))
    with pytest.raises(WorkbookSchemaError, match="UNSUPPORTED_HEADER_ROW"):
        build_staged_observations(path)


def test_non_date_ter_date_cell_is_rejected(tmp_path):
    path = tmp_path / "bad.xlsx"
    _write_workbook(path, [_row("SBIM/X/0001", "SBI Nifty 50 ETF", "not-a-date", 0.05, 0)])
    with pytest.raises(WorkbookSchemaError, match="UNSUPPORTED_TER_DATE_CELL"):
        build_staged_observations(path)


# ---------------------------------------------------------------------------
# ETF-vs-FOF exclusion and latest-by-date selection (not by row position)
# ---------------------------------------------------------------------------

def test_fund_of_fund_row_is_excluded_from_staged_etf_observations(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.05, 0),
        _row("SBIM/O/O/FOD/1", "SBI Nifty200 Value 30 ETF FOF", datetime(2026, 4, 1), 0.49, 0.08),
    ])
    staged = build_staged_observations(path)
    codes = {o.scheme_code for o in staged}
    assert codes == {"SBIM/O/O/EET/1"}


def test_latest_observation_selected_by_date_not_row_position(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        # Deliberately out of chronological order in the sheet.
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 6, 1), 0.06, 0),
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.04, 0),
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 10, 27), 0.052, 0),
    ])
    staged = build_staged_observations(path)
    assert len(staged) == 1
    assert staged[0].ter_date == date(2026, 10, 27)
    assert staged[0].regular_total_ter_percent == Decimal("0.052")


def test_scheme_code_whitespace_variants_are_normalized_together(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.04, 0),
        _row("SBIM/O/O/EET/1 ", "SBI Nifty 50 ETF", datetime(2026, 10, 27), 0.052, 0),
    ])
    staged = build_staged_observations(path)
    assert len(staged) == 1
    assert staged[0].ter_date == date(2026, 10, 27)


def test_conflicting_same_date_observations_fail_closed(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.04, 0),
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.09, 0),
    ])
    with pytest.raises(ValueError, match="CONFLICTING_TER_OBSERVATION_FOR_SAME_SCHEME_AND_DATE"):
        build_staged_observations(path)


def test_invalid_ter_percent_is_rejected(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 150, 0)])
    with pytest.raises(ValueError, match="INVALID_REGULAR_TOTAL_TER_PERCENT"):
        build_staged_observations(path)


# ---------------------------------------------------------------------------
# Zero Direct TER must never be read as a verified zero expense ratio
# ---------------------------------------------------------------------------

def test_zero_direct_ter_on_etf_row_is_flagged_not_applicable(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.052, 0)])
    staged = build_staged_observations(path)
    assert staged[0].direct_plan_applicable is False
    assert staged[0].direct_total_ter_percent == Decimal("0")
    # The authoritative TER figure always comes from the Regular Plan column.
    assert staged[0].regular_total_ter_percent == Decimal("0.052")


def test_nonzero_direct_ter_on_etf_row_is_preserved_not_discarded(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.052, 0.01)])
    staged = build_staged_observations(path)
    assert staged[0].direct_plan_applicable is True
    assert staged[0].direct_total_ter_percent == Decimal("0.01")


# ---------------------------------------------------------------------------
# Identity gate: no verified SBI mapping exists -> staged only, by default
# ---------------------------------------------------------------------------

def test_default_resolver_stages_every_row_without_persisting(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.052, 0),
        _row("SBIM/O/O/GET/1", "SBI Gold ETF", datetime(2026, 4, 1), 0.65, 0),
    ])
    store = SqliteResearchPersistence(":memory:")
    adapter = BoundedSbiTerAdapter()  # default resolver: always returns None
    results = adapter.acquire(store, path, source_identity="test-source", retrieved_at=NOW)
    assert len(results) == 2
    assert all(r.outcome == AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY for r in results)
    assert all(r.instrument_id is None and r.fact is None for r in results)


# ---------------------------------------------------------------------------
# Mechanical pipe test: IF a verified mapping existed, the full
# workbook -> scheme code -> identity -> fact -> persistence -> re-read
# chain works correctly, is idempotent, and detects a genuine conflict.
# The resolver below is an explicitly synthetic test double -- it is NOT a
# claim that this scheme code's real-world identity has been verified.
# ---------------------------------------------------------------------------

def test_synthetic_resolved_identity_persists_fact_idempotently_and_is_readable(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 10, 27), 0.052, 0)])
    instrument_id = uuid4()
    store = SqliteResearchPersistence(":memory:")
    adapter = BoundedSbiTerAdapter(identity_resolver=lambda code: instrument_id if code == "SBIM/O/O/EET/1" else None)

    first = adapter.acquire(store, path, source_identity="synthetic-test-source", retrieved_at=NOW)
    assert len(first) == 1
    assert first[0].outcome == AdapterOutcome.INGESTED
    assert first[0].instrument_id == instrument_id
    facts = store.etf_facts(instrument_id, EtfMetric.EXPENSE_RATIO)
    assert len(facts) == 1
    assert facts[0].value == Decimal("0.052")
    assert facts[0].unit == "percent"
    assert facts[0].as_of_date == date(2026, 10, 27)

    # Re-running against the same workbook must not duplicate the fact.
    second = adapter.acquire(store, path, source_identity="synthetic-test-source", retrieved_at=NOW)
    assert second[0].outcome == AdapterOutcome.ALREADY_INGESTED
    assert len(store.etf_facts(instrument_id, EtfMetric.EXPENSE_RATIO)) == 1


def test_synthetic_resolved_identity_conflicting_value_for_same_period_fails_closed(tmp_path):
    instrument_id = uuid4()
    store = SqliteResearchPersistence(":memory:")
    resolver = lambda code: instrument_id

    path_a = tmp_path / "wb_a.xlsx"
    _write_workbook(path_a, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 10, 27), 0.052, 0)])
    BoundedSbiTerAdapter(identity_resolver=resolver).acquire(
        store, path_a, source_identity="source-a", retrieved_at=NOW)

    path_b = tmp_path / "wb_b.xlsx"
    _write_workbook(path_b, [_row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 10, 27), 0.099, 0)])
    results = BoundedSbiTerAdapter(identity_resolver=resolver).acquire(
        store, path_b, source_identity="source-b", retrieved_at=NOW)
    assert results[0].outcome == AdapterOutcome.CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD
    # The original, first-seen value must not have been overwritten.
    facts = store.etf_facts(instrument_id, EtfMetric.EXPENSE_RATIO)
    assert len(facts) == 1 and facts[0].value == Decimal("0.052")


def test_bounded_rollout_instrument_cap_is_enforced(tmp_path):
    path = tmp_path / "wb.xlsx"
    _write_workbook(path, [
        _row("SBIM/O/O/EET/1", "SBI Nifty 50 ETF", datetime(2026, 4, 1), 0.05, 0),
        _row("SBIM/O/O/EET/2", "SBI Nifty Bank ETF", datetime(2026, 4, 1), 0.05, 0),
        _row("SBIM/O/O/EET/3", "SBI BSE Sensex ETF", datetime(2026, 4, 1), 0.05, 0),
        _row("SBIM/O/O/EET/4", "SBI Nifty Next 50 ETF", datetime(2026, 4, 1), 0.05, 0),
    ])
    store = SqliteResearchPersistence(":memory:")
    adapter = BoundedSbiTerAdapter(identity_resolver=lambda code: uuid4(), max_instruments=3)
    with pytest.raises(ValueError, match="BOUNDED_ROLLOUT_INSTRUMENT_LIMIT_EXCEEDED"):
        adapter.acquire(store, path, source_identity="test-source", retrieved_at=NOW)


# ---------------------------------------------------------------------------
# Real, unchanged workbook (user-supplied) -- parsing/classification only.
# No identity is resolved for any real SBI scheme code in this repo, so
# this test asserts staging-only behavior against the genuine file.
# ---------------------------------------------------------------------------

def test_real_sbi_workbook_stages_etf_rows_and_excludes_fund_of_funds():
    if not REAL_WORKBOOK.exists():
        pytest.skip("Real SBI TER workbook not present in this checkout (.tmp/ is gitignored)")
    staged = build_staged_observations(REAL_WORKBOOK)
    names = {o.scheme_name.strip() for o in staged}
    assert any("Nifty 50 ETF" in n for n in names)
    assert not any("fof" in n.lower() or "fund of fund" in n.lower() for n in names)
    # Every observation must be the latest calendar date seen for its scheme.
    assert all(o.ter_date <= date(2026, 10, 27) for o in staged)

    store = SqliteResearchPersistence(":memory:")
    adapter = BoundedSbiTerAdapter()  # no verified SBI identity mapping exists
    results = adapter.acquire(store, REAL_WORKBOOK, source_identity="current-year-ter.xlsx", retrieved_at=NOW)
    assert len(results) == len(staged)
    assert all(r.outcome == AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY for r in results)
    # Nothing was ever persisted against a guessed identity.
    assert store.etf_facts(UUID(int=0), EtfMetric.EXPENSE_RATIO) == []
