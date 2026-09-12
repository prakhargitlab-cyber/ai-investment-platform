from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app.persistence import SqliteResearchPersistence
from app.settings import Settings


class PostgresResearchPersistence(SqliteResearchPersistence):
    """PostgreSQL implementation using the same repository contract as the test SQLite store."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        connection = psycopg.connect(
            host=settings.research_database_host,
            port=settings.research_database_port,
            dbname=settings.research_database_name,
            user=settings.research_database_user,
            password=settings.research_database_password or "",
            sslmode=settings.research_database_ssl_mode,
            connect_timeout=settings.research_database_connect_timeout_seconds,
            options=f"-c statement_timeout={settings.research_database_statement_timeout_seconds * 1000}",
            autocommit=False,
            row_factory=dict_row,
        )
        self._connection = _PostgresConnectionAdapter(connection)
        _validate_identifier(settings.research_database_schema)
        self._connection.execute(f"SET search_path TO {settings.research_database_schema}")
        self._connection.commit()
        self._assert_schema_available()

    def migrate(self) -> None:
        raise RuntimeError("PostgreSQL research schema is owned by research-service Flyway migrations")

    def _assert_schema_available(self) -> None:
        try:
            self._connection.execute("SELECT 1 FROM research_documents LIMIT 0")
            self._connection.execute("SELECT 1 FROM research_events LIMIT 0")
            self._connection.execute("SELECT 1 FROM research_event_sources LIMIT 0")
            self._connection.execute("SELECT 1 FROM research_refresh_runs LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_shareholding_snapshots LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_shareholding_snapshot_values LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_financial_facts LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_structured_market_snapshots LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_market_price_observations LIMIT 0")
            self._connection.execute("SELECT 1 FROM global_stock_rule_engine_results LIMIT 0")
            self._connection.execute("SELECT 1 FROM market_trading_schedules LIMIT 0")
            self._connection.execute("SELECT 1 FROM market_trading_calendar_exceptions LIMIT 0")
            self._connection.commit()
        except Exception as exc:
            self._connection.connection.rollback()
            raise RuntimeError("RESEARCH_SCHEMA_NOT_MIGRATED") from exc


class _PostgresConnectionAdapter:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, sql: str, params=None):
        statement = sql.replace("?", "%s")
        if "INSERT OR IGNORE INTO" in statement:
            statement = statement.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
        return self.connection.execute(statement, params)

    def executescript(self, sql: str) -> None:
        self.connection.execute(sql)

    def commit(self) -> None:
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


def _validate_identifier(value: str) -> None:
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise RuntimeError("Invalid research database schema identifier")
