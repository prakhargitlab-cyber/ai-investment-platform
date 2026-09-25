"""Durable per-cycle / per-instrument progress for resumable production cycles.

Slices 2, 3 and 8. A production opportunity cycle (the full NSE universe) is
long-running; a worker restart previously discarded all traversal progress
(ACCEPTED/RUNNING -> FAILED(WORKER_RESTARTED)) and the next submission minted a
new cycle_id that re-traversed every instrument.

This module persists, keyed by the stable (cycle_id, phase, instrument):

* ``global_opportunity_cycle_run``      -- one row per resumable production
  cycle: status, parameters, original as_of, the deep-candidate selection once
  made, and a DB lease (owner_id + lease_expires_at) so exactly one worker in
  any replica owns the cycle.
* ``global_opportunity_cycle_active``   -- market PRIMARY KEY slot naming the
  single active production cycle, so scheduler ticks / API submissions
  coalesce even across pods (RollingUpdate maxSurge=1 overlaps two pods).
  Portable (PostgreSQL, SQLite, H2) unlike a partial unique index.
* ``global_opportunity_cycle_progress`` -- one row per (cycle, phase,
  instrument) with a state from ``CandidateState`` and the compact outcome
  needed to restore the candidate without re-acquisition or re-evaluation.

Every progress/selection/publication write is fenced on the owner: a worker
that lost its lease cannot write (``CycleOwnershipLost``).

Transaction boundaries (per candidate):
  1. IN_PROGRESS upsert (attempts += 1)             -- own commit
  2. evidence acquisition                           -- evidence commits itself
  3. rule-engine result persistence                 -- own commit (fingerprint cache)
  4. final-state upsert with restorable payload     -- own commit
Publication commits the snapshots/recommendations/selection and flips the run
to PUBLISHED in ONE transaction; suggestion lifecycle then completes the run
(idempotent: INSERT OR IGNORE scan + fingerprint-deduplicated history).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class CandidateState(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"                        # analysis completed (rule engine evaluated / baseline acquired)
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"        # unresolved technical failure -- eligible for bounded repair
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"  # genuine evidence unavailable -- not retried
    TERMINAL_OUTCOME = "TERMINAL_OUTCOME"          # terminal business/rule outcome (identity, ineligible, ...)


FINAL_STATES = frozenset({CandidateState.COMPLETED, CandidateState.EVIDENCE_UNAVAILABLE,
                          CandidateState.TERMINAL_OUTCOME})

PHASE_BASELINE = "BASELINE"
PHASE_DEEP = "DEEP"
ACTIVE_RUN_STATUSES = ("ACCEPTED", "RUNNING", "PUBLISHED")

# Existing orchestrator dispositions (global_opportunity_orchestration.py) ->
# checkpoint state. Technical failures are never business rejections.
DISPOSITION_STATE: dict[str, CandidateState] = {
    "ANALYZED": CandidateState.COMPLETED,
    "RANK_FILTERED": CandidateState.COMPLETED,
    "DEEP_READINESS_NOT_MET": CandidateState.EVIDENCE_UNAVAILABLE,
    "DEEP_ACQUISITION_TIMEOUT": CandidateState.RETRYABLE_FAILURE,
    "DEEP_SOURCE_UNAVAILABLE": CandidateState.RETRYABLE_FAILURE,
    "DEEP_TECHNICAL_FAILURE": CandidateState.RETRYABLE_FAILURE,
    "RULE_ENGINE_EXCEPTION": CandidateState.RETRYABLE_FAILURE,
    "STAGE2_INTERNAL_ERROR": CandidateState.RETRYABLE_FAILURE,
    "PROFILE_HYDRATION_FAILED": CandidateState.TERMINAL_OUTCOME,
    "PROFILE_IDENTITY_MISMATCH": CandidateState.TERMINAL_OUTCOME,
    "CANONICAL_INELIGIBLE": CandidateState.TERMINAL_OUTCOME,
    "BASELINE_ACQUISITION_FAILED": CandidateState.RETRYABLE_FAILURE,
}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300


class CycleOwnershipLost(RuntimeError):
    """This worker no longer owns the cycle lease; it must stop writing."""


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_opportunity_cycle_run (
    cycle_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACCEPTED','RUNNING','PUBLISHED','COMPLETED','FAILED')),
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    # Fixed width so lexicographic comparison of stored leases is chronological.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row) if not isinstance(row, dict) else dict(row)
    for key in ("parameters", "selection", "payload"):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value


class CycleRunPersistenceMixin:
    """SQL for the two tables; SQLite dialect (the Postgres adapter maps ? -> %s)."""

    def _cycle_rowcount(self, cursor) -> int:
        return int(getattr(cursor, "rowcount", 0) or 0)

    # -- run -----------------------------------------------------------------
    def active_cycle_run(self, market: str = "NSE") -> dict[str, Any] | None:
        with self._connection:
            row = self._connection.execute(
                "SELECT r.* FROM global_opportunity_cycle_active a "
                "JOIN global_opportunity_cycle_run r ON r.cycle_id = a.cycle_id WHERE a.market = ?", (market,)).fetchone()
        return _row(row)

    def cycle_run(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM global_opportunity_cycle_run WHERE cycle_id = ?", (str(cycle_id),)).fetchone()
        return _row(row)

    def create_cycle_run(self, cycle_id: str, parameters: dict, *, market: str = "NSE",
                         now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        """Create the resumable run, or return the already-active one.

        Returns (run, created). The partial UNIQUE index makes this safe across
        replicas: a concurrent creator loses and reads the winner's row."""
        now = now or _now()
        existing = self.active_cycle_run(market)
        if existing is not None:
            return existing, False
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO global_opportunity_cycle_run (cycle_id, market, status, parameters, created_at, updated_at) "
                    "VALUES (?, ?, 'ACCEPTED', ?, ?, ?)",
                    (str(cycle_id), market, json.dumps(parameters, sort_keys=True, default=str), _iso(now), _iso(now)))
                # PRIMARY KEY(market): a concurrent creator in another replica
                # fails here and its whole transaction (run row included) rolls back.
                self._connection.execute(
                    "INSERT INTO global_opportunity_cycle_active (market, cycle_id) VALUES (?, ?)", (market, str(cycle_id)))
        except Exception:
            existing = self.active_cycle_run(market)
            if existing is not None:
                return existing, False
            raise
        return self.cycle_run(cycle_id), True

    def claim_cycle_run(self, cycle_id: str, owner_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                        now: datetime | None = None) -> bool:
        """Atomically take (or keep) the lease when free, ours, or expired."""
        now = now or _now()
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET "
                " resume_count = resume_count + CASE WHEN status <> 'ACCEPTED' AND (owner_id IS NULL OR owner_id <> ?) THEN 1 ELSE 0 END, "
                " status = CASE WHEN status = 'ACCEPTED' THEN 'RUNNING' ELSE status END, "
                " owner_id = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE cycle_id = ? AND status IN ('ACCEPTED','RUNNING','PUBLISHED') "
                " AND (owner_id IS NULL OR owner_id = ? OR lease_expires_at IS NULL OR lease_expires_at < ?)",
                (owner_id, owner_id, _iso(now + timedelta(seconds=lease_seconds)), _iso(now), str(cycle_id),
                 owner_id, _iso(now)))
            return self._cycle_rowcount(cursor) == 1

    def renew_cycle_lease(self, cycle_id: str, owner_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                          now: datetime | None = None) -> bool:
        now = now or _now()
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET lease_expires_at = ?, updated_at = ? "
                "WHERE cycle_id = ? AND owner_id = ? AND status IN ('ACCEPTED','RUNNING','PUBLISHED')",
                (_iso(now + timedelta(seconds=lease_seconds)), _iso(now), str(cycle_id), owner_id))
            return self._cycle_rowcount(cursor) == 1

    def release_cycle_run(self, cycle_id: str, owner_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET owner_id = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE cycle_id = ? AND owner_id = ?", (_iso(_now()), str(cycle_id), owner_id))

    def update_cycle_run(self, cycle_id: str, owner_id: str, **fields) -> None:
        """Fenced update of status / as_of / selection / error_code."""
        allowed = {"status", "as_of", "selection", "error_code"}
        assert set(fields) <= allowed, set(fields) - allowed
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(json.dumps(value, sort_keys=True, default=str) if key == "selection" else value)
        sets.append("updated_at = ?")
        params.append(_iso(_now()))
        with self._connection:
            cursor = self._connection.execute(
                f"UPDATE global_opportunity_cycle_run SET {', '.join(sets)} WHERE cycle_id = ? AND owner_id = ?",
                (*params, str(cycle_id), owner_id))
            if self._cycle_rowcount(cursor) != 1:
                raise CycleOwnershipLost(str(cycle_id))
            if fields.get("status") in ("COMPLETED", "FAILED"):
                self._connection.execute(
                    "UPDATE global_opportunity_cycle_run SET owner_id = NULL, lease_expires_at = NULL WHERE cycle_id = ?",
                    (str(cycle_id),))
                self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))

    def fail_unowned_cycle_run(self, cycle_id: str, error_code: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET status = 'FAILED', error_code = ?, owner_id = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE cycle_id = ? AND status IN ('ACCEPTED','RUNNING','PUBLISHED')",
                (error_code, _iso(_now()), str(cycle_id)))
            self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))

    def _fence_cycle_publication(self, cycle_id: str, owner_id: str) -> None:
        """Called INSIDE publish_opportunity_cycle's transaction."""
        cursor = self._connection.execute(
            "UPDATE global_opportunity_cycle_run SET status = 'PUBLISHED', updated_at = ? "
            "WHERE cycle_id = ? AND owner_id = ? AND status = 'RUNNING'", (_iso(_now()), str(cycle_id), owner_id))
        if self._cycle_rowcount(cursor) != 1:
            raise CycleOwnershipLost(str(cycle_id))

    def cycle_published_selection(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connection:
            row = self._connection.execute(
                "SELECT payload FROM global_opportunity_top_selection WHERE cycle_id = ?", (str(cycle_id),)).fetchone()
        if row is None:
            return None
        payload = row["payload"] if not isinstance(row, (tuple, list)) else row[0]
        return json.loads(payload) if isinstance(payload, str) else payload

    # -- progress ------------------------------------------------------------
    def record_cycle_progress(self, cycle_id: str, owner_id: str, phase: str, instrument_id, state: CandidateState,
                              *, disposition: str | None = None, failure_reason: str | None = None,
                              payload: dict | None = None, start_attempt: bool = False) -> None:
        now = _iso(_now())
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO global_opportunity_cycle_progress "
                "(cycle_id, phase, global_instrument_id, state, disposition, failure_reason, attempts, payload, updated_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? FROM global_opportunity_cycle_run WHERE cycle_id = ? AND owner_id = ? "
                "ON CONFLICT (cycle_id, phase, global_instrument_id) DO UPDATE SET "
                " state = excluded.state, disposition = excluded.disposition, failure_reason = excluded.failure_reason, "
                " attempts = global_opportunity_cycle_progress.attempts + excluded.attempts, "
                " payload = excluded.payload, updated_at = excluded.updated_at",
                (str(cycle_id), phase, str(instrument_id), str(state), disposition, failure_reason,
                 1 if start_attempt else 0,
                 json.dumps(payload, sort_keys=True, default=str) if payload is not None else None, now,
                 str(cycle_id), owner_id))
            if self._cycle_rowcount(cursor) < 1:
                raise CycleOwnershipLost(str(cycle_id))

    def cycle_progress(self, cycle_id: str, phase: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        sql = "SELECT * FROM global_opportunity_cycle_progress WHERE cycle_id = ?"
        params: tuple = (str(cycle_id),)
        if phase is not None:
            sql += " AND phase = ?"
            params += (phase,)
        with self._connection:
            rows = self._connection.execute(sql, params).fetchall()
        return {(r["phase"], r["global_instrument_id"]): _row(r) for r in rows}


class CycleCheckpoint:
    """Orchestrator-facing handle for one owned, resumable cycle.

    All persistence goes through ``run_blocking`` (the repository's bounded
    persistence boundary) so the event loop is never blocked on Postgres."""

    def __init__(self, persistence, cycle_id: str, owner_id: str, *, run_blocking=None,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.persistence = persistence
        self.cycle_id = str(cycle_id)
        self.owner_id = owner_id
        self.max_attempts = max_attempts
        self._run_blocking = run_blocking
        self._progress: dict[tuple[str, str], dict[str, Any]] = {}
        self.restored: dict[str, int] = {PHASE_BASELINE: 0, PHASE_DEEP: 0}
        self.executed: dict[str, int] = {PHASE_BASELINE: 0, PHASE_DEEP: 0}

    async def _call(self, operation, *args, **kwargs):
        if self._run_blocking is not None:
            return await self._run_blocking(operation, *args, **kwargs)
        return operation(*args, **kwargs)

    async def load(self) -> "CycleCheckpoint":
        self._progress = await self._call(self.persistence.cycle_progress, self.cycle_id)
        return self

    async def run(self) -> dict[str, Any] | None:
        return await self._call(self.persistence.cycle_run, self.cycle_id)

    # restore decisions -------------------------------------------------------
    def row(self, phase: str, instrument_id) -> dict[str, Any] | None:
        return self._progress.get((phase, str(instrument_id)))

    def restorable(self, phase: str, instrument_id) -> dict[str, Any] | None:
        """A final outcome, or a retryable failure whose repair budget is spent."""
        row = self.row(phase, instrument_id)
        if row is None:
            return None
        state = CandidateState(row["state"])
        if state in FINAL_STATES:
            return row
        if state == CandidateState.RETRYABLE_FAILURE and int(row["attempts"]) >= self.max_attempts:
            return row
        return None

    def note_restored(self, phase: str) -> None:
        self.restored[phase] += 1

    # writes ------------------------------------------------------------------
    async def start(self, phase: str, instrument_id) -> None:
        self.executed[phase] += 1
        await self._call(self.persistence.record_cycle_progress, self.cycle_id, self.owner_id, phase, instrument_id,
                         CandidateState.IN_PROGRESS, start_attempt=True)
        row = self._progress.setdefault((phase, str(instrument_id)), {"attempts": 0})
        row.update(state=str(CandidateState.IN_PROGRESS), attempts=int(row.get("attempts") or 0) + 1)

    async def finish(self, phase: str, instrument_id, state: CandidateState, *, disposition: str | None = None,
                     failure_reason: str | None = None, payload: dict | None = None) -> None:
        await self._call(self.persistence.record_cycle_progress, self.cycle_id, self.owner_id, phase, instrument_id,
                         state, disposition=disposition, failure_reason=failure_reason, payload=payload)
        row = self._progress.setdefault((phase, str(instrument_id)), {"attempts": 0})
        row.update(state=str(state), disposition=disposition, failure_reason=failure_reason, payload=payload)

    async def save_selection(self, selection: dict) -> None:
        await self._call(self.persistence.update_cycle_run, self.cycle_id, self.owner_id, selection=selection)

    async def selection(self) -> dict | None:
        run = await self.run()
        return (run or {}).get("selection")


def state_for_disposition(disposition: str | None) -> CandidateState:
    return DISPOSITION_STATE.get(disposition or "", CandidateState.RETRYABLE_FAILURE)


def new_owner_id() -> str:
    return f"research-engine:{uuid4()}"
