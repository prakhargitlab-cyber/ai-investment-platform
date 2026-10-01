"""Regression test for PostgresResearchPersistence._assert_schema_available.

Post-commit review of 8eaed27 ("feat: complete ETF research and paginated
discovery") found that V20__etf_evidence.sql creates six ETF tables but
_assert_schema_available() only existence-checked five of them --
etf_holdings_positions was missing (likely a copy/paste gap next to the very
similarly-named etf_holdings_snapshots). A missing check here does not fail
loudly: a researcher-engine pod could boot successfully against a
not-fully-migrated schema and only discover the gap later, as an opaque SQL
error the first time ETF holdings-position data is actually written.

This test proves every table the migration creates is checked, using a fake
connection (no real Postgres) that simply records every SQL statement
executed, so it also guards against a similar omission being reintroduced for
any future ETF (or other) table.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.postgres_persistence import PostgresResearchPersistence

MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "services/research-service/src/main/resources/db/migration/V20__etf_evidence.sql"
)


class _RecordingConnection:
    """Minimal stand-in for _PostgresConnectionAdapter: just records SQL."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.connection = self

    def execute(self, sql: str, params=None):
        self.statements.append(sql)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _etf_table_names_from_migration() -> list[str]:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    return re.findall(r"CREATE TABLE (etf_\w+)", sql)


def test_v20_migration_creates_exactly_six_etf_tables():
    """Guards the premise of this test: if V20 is ever edited to add/remove
    an ETF table, this fails loudly instead of silently under-checking."""
    tables = _etf_table_names_from_migration()
    assert sorted(tables) == sorted([
        "etf_fact_observations",
        "etf_nav_observations",
        "etf_holdings_snapshots",
        "etf_holdings_positions",
        "etf_listing_observations",
        "etf_acquisition_attempts",
    ])


def test_assert_schema_available_checks_every_etf_table_from_v20_migration():
    expected_etf_tables = _etf_table_names_from_migration()
    assert expected_etf_tables, "fixture migration path drifted; found zero ETF tables"

    persistence = object.__new__(PostgresResearchPersistence)
    fake_connection = _RecordingConnection()
    persistence._connection = fake_connection

    persistence._assert_schema_available()

    checked = " ".join(fake_connection.statements)
    missing = [table for table in expected_etf_tables if table not in checked]
    assert missing == [], f"_assert_schema_available() does not check: {missing}"


def test_assert_schema_available_specifically_checks_etf_holdings_positions():
    """Direct regression test for the exact omission found in 8eaed27:
    etf_holdings_positions existed in the migration but not in the
    existence-check list."""
    persistence = object.__new__(PostgresResearchPersistence)
    fake_connection = _RecordingConnection()
    persistence._connection = fake_connection

    persistence._assert_schema_available()

    assert any(
        "etf_holdings_positions" in statement for statement in fake_connection.statements
    )
