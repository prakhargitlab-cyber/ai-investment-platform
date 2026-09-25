"""Persistence for the macro event calendar (macro_events).

Its own table, its own mixin, its own module -- never macro_observations
(released values) and never global_financial_facts. SQLite dialect; the
Postgres adapter maps ``?`` -> ``%s`` the same way it does for every other
mixin here.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.macro_event import MacroEvent

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    indicator TEXT NOT NULL,
    region TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SCHEDULED',
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    provider TEXT NOT NULL,
    provenance TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_events_region_scheduled ON macro_events (region, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_macro_events_indicator_scheduled ON macro_events (indicator, scheduled_at);
"""


class MacroEventPersistenceMixin:
    def upsert_macro_event(self, event: MacroEvent) -> None:
        """Idempotent by construction: `event.id` is deterministic from
        (event_type, indicator, region, scheduled_at), so re-acquiring the
        same meeting upserts the same row. created_at is deliberately left
        out of the UPDATE SET clause so it is set once, on first insert, and
        never touched again."""
        created_at = (event.created_at or event.observed_at).isoformat()
        updated_at = (event.updated_at or event.observed_at).isoformat()
        with self._connection:
            self._connection.execute(
                """INSERT INTO macro_events
                (id, event_type, indicator, region, scheduled_at, period, status, source, source_url,
                 provider, provenance, observed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                scheduled_at=excluded.scheduled_at, period=excluded.period, status=excluded.status,
                source=excluded.source, source_url=excluded.source_url, provider=excluded.provider,
                provenance=excluded.provenance, observed_at=excluded.observed_at, updated_at=excluded.updated_at""",
                (
                    event.id, event.event_type, event.indicator, event.region, event.scheduled_at.isoformat(),
                    event.period, event.status, event.source, event.source_url, event.provider,
                    event.provenance, event.observed_at.isoformat(), created_at, updated_at,
                ),
            )

    def load_macro_event(self, event_id: str) -> MacroEvent | None:
        with self._connection:
            row = self._connection.execute("SELECT * FROM macro_events WHERE id=?", (event_id,)).fetchone()
        return _macro_event_from_row(row)

    def next_macro_event(self, indicator: str, region: str, now: datetime) -> MacroEvent | None:
        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM macro_events WHERE indicator=? AND region=? AND status='SCHEDULED' "
                "AND scheduled_at >= ? ORDER BY scheduled_at ASC LIMIT 1",
                (indicator, region, now.date().isoformat()),
            ).fetchone()
        return _macro_event_from_row(row)

    def macro_events_between(self, start: date, end: date, *, indicator: str | None = None,
                              region: str | None = None) -> list[MacroEvent]:
        clauses = ["scheduled_at >= ?", "scheduled_at <= ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if indicator is not None:
            clauses.append("indicator = ?")
            params.append(indicator)
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        with self._connection:
            rows = self._connection.execute(
                f"SELECT * FROM macro_events WHERE {' AND '.join(clauses)} ORDER BY scheduled_at ASC", params,
            ).fetchall()
        return [e for row in rows if (e := _macro_event_from_row(row)) is not None]

    def upcoming_macro_events(self, *, region: str | None = None, indicator: str | None = None,
                               now: datetime | None = None) -> list[MacroEvent]:
        now = now or datetime.now()
        clauses = ["status='SCHEDULED'", "scheduled_at >= ?"]
        params: list[Any] = [now.date().isoformat()]
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        if indicator is not None:
            clauses.append("indicator = ?")
            params.append(indicator)
        with self._connection:
            rows = self._connection.execute(
                f"SELECT * FROM macro_events WHERE {' AND '.join(clauses)} ORDER BY scheduled_at ASC", params,
            ).fetchall()
        return [e for row in rows if (e := _macro_event_from_row(row)) is not None]

    def macro_events_last_refreshed(self, event_type: str, indicator: str, region: str) -> datetime | None:
        with self._connection:
            row = self._connection.execute(
                "SELECT MAX(updated_at) AS latest FROM macro_events WHERE event_type=? AND indicator=? AND region=?",
                (event_type, indicator, region),
            ).fetchone()
        value = row["latest"] if row else None
        return datetime.fromisoformat(value) if value else None


def _macro_event_from_row(row: Any) -> MacroEvent | None:
    if row is None:
        return None
    data = dict(row)
    return MacroEvent(
        id=data["id"], event_type=data["event_type"], indicator=data["indicator"], region=data["region"],
        scheduled_at=data["scheduled_at"], period=data["period"], status=data["status"], source=data["source"],
        source_url=data["source_url"], provider=data["provider"], provenance=data["provenance"],
        observed_at=data["observed_at"], created_at=data["created_at"], updated_at=data["updated_at"],
    )
