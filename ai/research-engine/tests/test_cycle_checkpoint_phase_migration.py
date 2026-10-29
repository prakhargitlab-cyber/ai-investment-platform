"""Regression tests for the ETF_ONBOARD checkpoint-phase persistence contract.

The in-memory CycleCheckpoint.restored/executed dicts (app/cycle_checkpoint.py)
were the FIRST of two independent places a phase must be registered; the second
is the durable, SQL-level CHECK (phase IN (...)) constraint on
global_opportunity_cycle_progress. A fix that registers a new phase in the
in-memory dicts alone (as happened in an earlier round) looks complete -- the
original KeyError disappears -- but still fails the moment a real
record_cycle_progress() write for that phase reaches the database, now as
sqlite3.IntegrityError instead of KeyError.

These tests exercise the durable layer directly, independent of the in-memory
dicts, covering the four things Task A of this round's instructions required:
new database initialization, existing (pre-ETF_ONBOARD) database upgrade,
durable save/reload, and lease fencing -- all against the REAL
SqliteResearchPersistence/CycleCheckpoint code path, not a hand-rolled stand-in.

research_database_backend == "sqlite" (app/settings.py) is a genuine, if
non-default, on-disk deployment option, not merely a test fixture path, so
"existing database" here means a real file on disk created under the old
schema, not just an in-memory connection.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.cycle_checkpoint import (
    CandidateState,
    CycleOwnershipLost,
    PHASE_BASELINE,
    PHASE_ETF_ONBOARD,
)
from app.persistence import SqliteResearchPersistence

# A frozen snapshot of the schema as it shipped BEFORE ETF_ONBOARD existed --
# deliberately NOT derived from the current SQLITE_SCHEMA, since the whole
# point is to reproduce what an already-deployed database file actually looks
# like, independent of any later source change.
# The exact, compact (no-space) JSON text the legacy fixture stores for its
# BASELINE row's payload column -- a raw SQL string literal, never passed
# through json.dumps, so there is only one place its exact bytes can be
# defined. Both _seed_legacy_database's INSERT and the preservation
# assertion below reference THIS constant, so they cannot independently
# drift out of sync with each other the way a hand-typed literal in each
# place once did.
_LEGACY_PAYLOAD_TEXT = '{"k":1}'

_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_opportunity_cycle_run (
    cycle_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED')),
    parameters TEXT NOT NULL,
    as_of TEXT,
    selection TEXT,
    owner_id TEXT,
    lease_expires_at TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS global_opportunity_cycle_active (
    market TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES global_opportunity_cycle_run(cycle_id)
);
CREATE TABLE IF NOT EXISTS global_opportunity_cycle_progress (
    cycle_id TEXT NOT NULL REFERENCES global_opportunity_cycle_run(cycle_id),
    phase TEXT NOT NULL CHECK (phase IN ('BASELINE','DEEP')),
    global_instrument_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING','IN_PROGRESS','COMPLETED','RETRYABLE_FAILURE','EVIDENCE_UNAVAILABLE','TERMINAL_OUTCOME')),
    disposition TEXT,
    failure_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    payload TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, phase, global_instrument_id)
);
CREATE INDEX IF NOT EXISTS ix_opportunity_cycle_progress_state
    ON global_opportunity_cycle_progress (cycle_id, phase, state);
"""


def _seed_legacy_database(path: Path) -> None:
    """Build a pre-ETF_ONBOARD on-disk database file, as a real deployment would have it."""
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO global_opportunity_cycle_run "
        "(cycle_id, market, status, parameters, owner_id, lease_expires_at, created_at, updated_at) "
        "VALUES ('legacy-cycle','NSE','RUNNING','{}','owner-A','2999-01-01T00:00:00.000000+00:00','t','t')"
    )
    con.execute(
        "INSERT INTO global_opportunity_cycle_progress "
        "(cycle_id, phase, global_instrument_id, state, disposition, failure_reason, attempts, payload, updated_at) "
        "VALUES ('legacy-cycle','BASELINE','INE002A01018','COMPLETED','ANALYZED',NULL,2,?,"
        "'2026-01-01T00:00:00.000000+00:00')",
        (_LEGACY_PAYLOAD_TEXT,),
    )
    con.commit()
    con.close()


def test_fresh_sqlite_database_accepts_etf_onboard_phase():
    """A brand-new database (the common test/CI path) must support ETF_ONBOARD
    from first use -- this is the exact write that previously raised
    sqlite3.IntegrityError once the earlier, in-memory-only fix let execution
    reach the database."""
    store = SqliteResearchPersistence()  # ":memory:"
    store.create_cycle_run("fresh-cycle", {}, market="ETF")
    assert store.claim_cycle_run("fresh-cycle", "owner-A")
    store.record_cycle_progress("fresh-cycle", "owner-A", PHASE_ETF_ONBOARD, "INE123A01016",
                                 CandidateState.IN_PROGRESS, start_attempt=True)
    row = store._connection.execute(
        "SELECT state, attempts FROM global_opportunity_cycle_progress WHERE phase = ?", (PHASE_ETF_ONBOARD,)
    ).fetchone()
    assert tuple(row) == ("IN_PROGRESS", 1)


def test_existing_on_disk_database_is_upgraded_in_place_preserving_rows(tmp_path):
    """Opening a real pre-ETF_ONBOARD database file must widen the phase
    constraint in place -- CREATE TABLE IF NOT EXISTS alone is a no-op against
    an already-created table and would leave the old constraint forever."""
    db_path = tmp_path / "legacy.sqlite3"
    _seed_legacy_database(db_path)

    # Before opening via the app, confirm this file genuinely reproduces the
    # reported defect (otherwise this test would be proving nothing).
    raw = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO global_opportunity_cycle_progress (cycle_id, phase, global_instrument_id, state, attempts, updated_at) "
            "VALUES ('legacy-cycle', ?, 'x', 'IN_PROGRESS', 1, 't')", (PHASE_ETF_ONBOARD,)
        )
    raw.close()

    store = SqliteResearchPersistence(db_path)  # migrate() runs on __init__

    preserved = store._connection.execute(
        "SELECT phase, global_instrument_id, state, disposition, attempts, payload "
        "FROM global_opportunity_cycle_progress WHERE cycle_id = 'legacy-cycle' AND phase = ?",
        (PHASE_BASELINE,),
    ).fetchone()
    # Every field checked strictly, including the payload text byte-for-byte --
    # the migration's row copy is a plain SQL INSERT ... SELECT with no JSON
    # parsing, so it must reproduce the stored bytes exactly, not merely
    # equivalent JSON.
    assert tuple(preserved) == ("BASELINE", "INE002A01018", "COMPLETED", "ANALYZED", 2, _LEGACY_PAYLOAD_TEXT)

    assert store.claim_cycle_run("legacy-cycle", "owner-A")
    store.record_cycle_progress("legacy-cycle", "owner-A", PHASE_ETF_ONBOARD, "INE123A01016",
                                 CandidateState.IN_PROGRESS, start_attempt=True)
    row = store._connection.execute(
        "SELECT state FROM global_opportunity_cycle_progress WHERE cycle_id = 'legacy-cycle' AND phase = ?",
        (PHASE_ETF_ONBOARD,),
    ).fetchone()
    assert row[0] == "IN_PROGRESS"


def test_migration_is_idempotent_across_repeated_opens(tmp_path):
    """Re-opening an already-migrated database (every process start calls
    migrate()) must not duplicate rows, re-widen an already-widened
    constraint, or otherwise error."""
    db_path = tmp_path / "legacy.sqlite3"
    _seed_legacy_database(db_path)

    first = SqliteResearchPersistence(db_path)  # first migrate(): widens the constraint
    first._connection.close()
    second = SqliteResearchPersistence(db_path)  # second migrate(): must be a clean no-op
    count = second._connection.execute(
        "SELECT COUNT(*) FROM global_opportunity_cycle_progress"
    ).fetchone()[0]
    assert count == 1  # only the original legacy BASELINE row -- nothing duplicated


def test_durable_save_reload_survives_process_restart(tmp_path):
    """A row written by one process must be readable by a brand-new
    SqliteResearchPersistence instance pointed at the same file afterwards --
    the restart-resilience property the one-click cycle depends on."""
    db_path = tmp_path / "durable.sqlite3"
    first = SqliteResearchPersistence(db_path)
    first.create_cycle_run("durable-cycle", {}, market="ETF")
    assert first.claim_cycle_run("durable-cycle", "owner-A")
    first.record_cycle_progress("durable-cycle", "owner-A", PHASE_ETF_ONBOARD, "INE123A01016",
                                 CandidateState.TERMINAL_OUTCOME, disposition="OFFICIAL_ETF_IDENTITY_CREATED")
    first._connection.close()

    second = SqliteResearchPersistence(db_path)
    row = second._connection.execute(
        "SELECT state, disposition FROM global_opportunity_cycle_progress "
        "WHERE cycle_id = 'durable-cycle' AND phase = ?", (PHASE_ETF_ONBOARD,)
    ).fetchone()
    assert tuple(row) == ("TERMINAL_OUTCOME", "OFFICIAL_ETF_IDENTITY_CREATED")


def test_lease_fencing_intact_after_phase_migration(tmp_path):
    """Widening the phase constraint must not weaken owner/lease fencing: a
    non-owning worker, or an expired lease, must still be rejected after the
    migration runs."""
    db_path = tmp_path / "legacy.sqlite3"
    _seed_legacy_database(db_path)
    store = SqliteResearchPersistence(db_path)

    with pytest.raises(CycleOwnershipLost):
        store.record_cycle_progress("legacy-cycle", "owner-B", PHASE_ETF_ONBOARD, "INE123A01016",
                                     CandidateState.IN_PROGRESS, start_attempt=True)
    row = store._connection.execute(
        "SELECT COUNT(*) FROM global_opportunity_cycle_progress WHERE phase = ?", (PHASE_ETF_ONBOARD,)
    ).fetchone()
    assert row[0] == 0  # the rejected write created nothing

    store._connection.execute(
        "UPDATE global_opportunity_cycle_run SET lease_expires_at = '2000-01-01T00:00:00.000000+00:00' "
        "WHERE cycle_id = 'legacy-cycle'"
    )
    store._connection.commit()
    with pytest.raises(CycleOwnershipLost):
        store.record_cycle_progress("legacy-cycle", "owner-A", PHASE_ETF_ONBOARD, "INE123A01016",
                                     CandidateState.IN_PROGRESS, start_attempt=True)
