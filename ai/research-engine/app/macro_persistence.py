"""Persistence for global (non-instrument-scoped) macro observations.

Deliberately its own table (`macro_observations`), its own mixin, and its
own module -- never `global_financial_facts` (per-instrument) and never
written or read through the financial-fact loaders. SQLite dialect; the
Postgres adapter maps ``?`` -> ``%s`` the same way it does for every other
mixin here.
"""
from __future__ import annotations

from typing import Any

from app.macro_observation import MacroObservation

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_observations (
    indicator TEXT NOT NULL,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    actual_value REAL NOT NULL,
    unit TEXT NOT NULL,
    effective_at TEXT,
    release_at TEXT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    provider TEXT NOT NULL,
    provenance TEXT NOT NULL,
    previous_value REAL,
    expected_value REAL,
    surprise REAL,
    PRIMARY KEY (indicator, region, period)
);
CREATE INDEX IF NOT EXISTS idx_macro_observations_indicator_region
    ON macro_observations (indicator, region, observed_at);
"""


class MacroPersistenceMixin:
    def upsert_macro_observation(self, observation: MacroObservation) -> None:
        """Write only on a successful fetch. There is no failure-path call
        site for this method anywhere in this codebase: a failed provider
        attempt must never overwrite, blank, or otherwise touch a
        previously persisted observation."""
        payload = observation.model_dump(mode="json", by_alias=False)
        with self._connection:
            self._connection.execute(
                """INSERT INTO macro_observations
                (indicator, region, period, actual_value, unit, effective_at, release_at, observed_at,
                 source, source_url, provider, provenance, previous_value, expected_value, surprise)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (indicator, region, period) DO UPDATE SET
                actual_value=excluded.actual_value, unit=excluded.unit, effective_at=excluded.effective_at,
                release_at=excluded.release_at, observed_at=excluded.observed_at, source=excluded.source,
                source_url=excluded.source_url, provider=excluded.provider, provenance=excluded.provenance,
                previous_value=excluded.previous_value, expected_value=excluded.expected_value,
                surprise=excluded.surprise""",
                (
                    payload["indicator"], payload["region"], payload["period"], payload["actual_value"],
                    payload["unit"], payload.get("effective_at"), payload.get("release_at"), payload["observed_at"],
                    payload["source"], payload["source_url"], payload["provider"], payload["provenance"],
                    payload.get("previous_value"), payload.get("expected_value"), payload.get("surprise"),
                ),
            )

    def load_macro_observation(self, indicator: str, region: str = "IN") -> MacroObservation | None:
        """Latest (by observed_at) persisted observation for one indicator/region."""
        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM macro_observations WHERE indicator=? AND region=? ORDER BY observed_at DESC LIMIT 1",
                (indicator, region),
            ).fetchone()
        return _macro_observation_from_row(row)

    def load_macro_observations(self, indicator: str | None = None, region: str = "IN") -> list[MacroObservation]:
        with self._connection:
            if indicator is None:
                rows = self._connection.execute(
                    "SELECT * FROM macro_observations WHERE region=? ORDER BY indicator, observed_at DESC", (region,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM macro_observations WHERE indicator=? AND region=? ORDER BY observed_at DESC",
                    (indicator, region),
                ).fetchall()
        return [obs for row in rows if (obs := _macro_observation_from_row(row)) is not None]


def _macro_observation_from_row(row: Any) -> MacroObservation | None:
    if row is None:
        return None
    data = dict(row)
    return MacroObservation(
        indicator=data["indicator"], region=data["region"], period=data["period"],
        actual_value=data["actual_value"], unit=data["unit"], effective_at=data.get("effective_at"),
        release_at=data.get("release_at"), observed_at=data["observed_at"], source=data["source"],
        source_url=data["source_url"], provider=data["provider"], provenance=data["provenance"],
        previous_value=data.get("previous_value"), expected_value=data.get("expected_value"),
        surprise=data.get("surprise"),
    )
