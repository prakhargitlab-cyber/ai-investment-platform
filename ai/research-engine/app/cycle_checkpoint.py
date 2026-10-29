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
PHASE_ETF_ONBOARD = "ETF_ONBOARD"
ACTIVE_RUN_STATUSES = ("ACCEPTED", "RUNNING", "PUBLISHED", "CANCEL_REQUESTED")

# The external resumed_candidates / executed_candidates fields on a completed
# cycle's job record are a long-standing response contract, established and
# asserted only by Equity Radar, with exactly the two keys {PHASE_BASELINE,
# PHASE_DEEP}. PHASE_ETF_ONBOARD is registered for internal strictness (the
# checkpoint dict always carries all three phases, the SQL CHECK constraint
# accepts it, and strict KeyError protection applies), but it is intentionally
# NOT leaked through this external field -- ETF onboarding's own progress is
# surfaced separately via coverage.stages.onboarding
# (app.main._etf_onboarding_stage_progress), never duplicated here.
EXTERNAL_RESPONSE_PHASES: tuple[str, ...] = (PHASE_BASELINE, PHASE_DEEP)

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
    "PROFILE_MISSING_AFTER_HYDRATION": CandidateState.RETRYABLE_FAILURE,
}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300


class CycleOwnershipLost(RuntimeError):
    """This worker no longer owns the cycle lease; it must stop writing."""


SQLITE_SCHEMA = """
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
    phase TEXT NOT NULL CHECK (phase IN ('BASELINE','DEEP','ETF_ONBOARD')),
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

    def _lease_clock_sql(self) -> str:
        return "strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now')"

    def _lease_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return now
        with self._connection:
            row = self._connection.execute(f"SELECT {self._lease_clock_sql()} AS lease_now").fetchone()
        return datetime.fromisoformat(row['lease_now'])

    def _fence_published_owner(self, cycle_id: str, owner_id: str) -> None:
        # Must remain inside the caller's publication/projection transaction.
        cursor = self._connection.execute(
            "UPDATE global_opportunity_cycle_run SET updated_at = ? "
            "WHERE cycle_id = ? AND owner_id = ? AND status = 'PUBLISHED' "
            f"AND lease_expires_at > {self._lease_clock_sql()}",
            (_iso(_now()), str(cycle_id), owner_id))
        if self._cycle_rowcount(cursor) != 1:
            raise CycleOwnershipLost(str(cycle_id))

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
        """Atomically take (or keep) the lease when free, ours, or expired.

        A replacement may claim an expired CANCEL_REQUESTED lease only to
        drain/finalize cancellation. The worker never invokes acquisition for it.
        """
        now = self._lease_now(now)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET "
                " resume_count = resume_count + CASE WHEN status <> 'ACCEPTED' AND (owner_id IS NULL OR owner_id <> ?) THEN 1 ELSE 0 END, "
                " status = CASE WHEN status = 'ACCEPTED' THEN 'RUNNING' ELSE status END, "
                " owner_id = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE cycle_id = ? AND status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED') "
                " AND (owner_id IS NULL OR owner_id = ? OR lease_expires_at IS NULL OR lease_expires_at < ?)",
                (owner_id, owner_id, _iso(now + timedelta(seconds=lease_seconds)), _iso(now), str(cycle_id),
                 owner_id, _iso(now)))
            return self._cycle_rowcount(cursor) == 1

    def renew_cycle_lease(self, cycle_id: str, owner_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                          now: datetime | None = None) -> bool:
        now = self._lease_now(now)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET lease_expires_at = ?, updated_at = ? "
                "WHERE cycle_id = ? AND owner_id = ? AND status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED') "
                f"AND lease_expires_at > {self._lease_clock_sql()}",
                (_iso(now + timedelta(seconds=lease_seconds)), _iso(now), str(cycle_id), owner_id))
            return self._cycle_rowcount(cursor) == 1

    def release_cycle_run(self, cycle_id: str, owner_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET owner_id = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE cycle_id = ? AND owner_id = ? "
                f"AND lease_expires_at > {self._lease_clock_sql()} "
                "AND status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED')",
                (_iso(_now()), str(cycle_id), owner_id))

    def update_cycle_run(self, cycle_id: str, owner_id: str, **fields) -> None:
        """Fenced update of status / as_of / selection / error_code."""
        allowed = {"status", "as_of", "selection", "error_code"}
        assert set(fields) <= allowed, set(fields) - allowed
        states = "('ACCEPTED','RUNNING')"
        if fields.get('status') == 'COMPLETED':
            states = "('RUNNING','PUBLISHED')"
        elif fields.get('status') == 'CANCELLED':
            states = "('ACCEPTED','RUNNING','CANCEL_REQUESTED')"
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(json.dumps(value, sort_keys=True, default=str) if key == "selection" else value)
        sets.append("updated_at = ?")
        params.append(_iso(_now()))
        with self._connection:
            cursor = self._connection.execute(
                f"UPDATE global_opportunity_cycle_run SET {', '.join(sets)} WHERE cycle_id = ? AND owner_id = ? "
                f"AND lease_expires_at > {self._lease_clock_sql()} AND status IN {states}",
                (*params, str(cycle_id), owner_id))
            if self._cycle_rowcount(cursor) != 1:
                raise CycleOwnershipLost(str(cycle_id))
            if fields.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                self._connection.execute(
                    "UPDATE global_opportunity_cycle_run SET owner_id = NULL, lease_expires_at = NULL WHERE cycle_id = ?",
                    (str(cycle_id),))
                self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))

    def fail_unowned_cycle_run(self, cycle_id: str, error_code: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET status = 'FAILED', error_code = ?, owner_id = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE cycle_id = ? AND status IN ('ACCEPTED','RUNNING') "
                f"AND (owner_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= {self._lease_clock_sql()})",
                (error_code, _iso(_now()), str(cycle_id)))
            if self._cycle_rowcount(cursor) == 1:
                self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))

    def request_cycle_cancel(self, cycle_id: str, error_code: str = 'CYCLE_CANCELLED') -> bool:
        """Durable, idempotent operator cancellation REQUEST (two-phase).

        Phase 1 (request): sets status='CANCEL_REQUESTED' for any still-active
        run (ACCEPTED/RUNNING). The active slot, owner_id, and
        lease_expires_at are PRESERVED so the current lease-holder worker can
        drain in-flight work and maintain lease renewal during draining. A
        CANCEL_REQUESTED run is not resumable by a *different* worker (the
        worker's ``_maybe_take_over`` and ``start`` refuse it), but the owner
        keeps writing progress until it finishes and coerces to terminal.

        Phase 2 (terminal): the owning worker, after draining, calls
        ``cancel_cycle_run`` to flip to terminal 'CANCELLED' and release
        ownership + the active slot.

        Completed/FAILED/CANCEL_REQUESTED/CANCELLED runs are left untouched
        (idempotent: a second request is a no-op). Ownership-agnostic (like
        ``fail_unowned_cycle_run``): an operator may request cancellation
        regardless of which worker currently holds the lease.

        Returns True if a row was transitioned to CANCEL_REQUESTED.
        """
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET status = 'CANCEL_REQUESTED', error_code = ?, "
                "updated_at = ? "
                "WHERE cycle_id = ? AND status IN ('ACCEPTED','RUNNING')",
                (error_code, _iso(_now()), str(cycle_id)))
            rowcount = self._cycle_rowcount(cursor)
            # Do NOT delete the active slot here: the current owner must keep
            # draining. The worker's cancel_cycle_run() removes it on terminal.
        return rowcount == 1

    def cancel_cycle_run(self, cycle_id: str, owner_id: str, *, error_code: str = 'CYCLE_CANCELLED') -> bool:
        """Worker-side terminal transition to CANCELLED, fenced on the current owner.

        Used by the owning worker after draining in-flight work: only the lease
        holder may coerce the run from CANCEL_REQUESTED (or RUNNING if the
        worker initiated the cancel) to terminal CANCELLED. Releases ownership,
        lease, and the active slot. Preserves all progress rows. Idempotent on
        already-CANCELLED runs.

        Returns True if the run was transitioned to CANCELLED.
        """
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET status = 'CANCELLED', error_code = ?, "
                "owner_id = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE cycle_id = ? AND owner_id = ? AND status IN ('CANCEL_REQUESTED','RUNNING','ACCEPTED') "
                f"AND lease_expires_at > {self._lease_clock_sql()}",
                (error_code, _iso(_now()), str(cycle_id), owner_id))
            rowcount = self._cycle_rowcount(cursor)
            if rowcount == 1:
                self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))
        return rowcount == 1

    def cycle_cancellation_requested(self, cycle_id: str) -> bool:
        """Read-only check: has a durable cancellation (REQUESTED or terminal)
        been recorded for this cycle?

        A CANCEL_REQUESTED cycle is not resumable by any worker (different
        owner cannot claim; same owner drains to terminal). A CANCELLED cycle
        is permanently terminal.
        """
        with self._connection:
            row = self._connection.execute(
                "SELECT 1 FROM global_opportunity_cycle_run "
                "WHERE cycle_id = ? AND status IN ('CANCEL_REQUESTED','CANCELLED')",
                (str(cycle_id),)).fetchone()
        return row is not None

    def cycle_cancel_status(self, cycle_id: str) -> str | None:
        """Read-only: return the cancellation status ('CANCEL_REQUESTED' or
        'CANCELLED') if the cycle has been cancelled, else None."""
        with self._connection:
            row = self._connection.execute(
                "SELECT status FROM global_opportunity_cycle_run WHERE cycle_id = ?",
                (str(cycle_id),)).fetchone()
        if row is None:
            return None
        status = dict(row).get("status")
        if status in ("CANCEL_REQUESTED", "CANCELLED"):
            return status
        return None

    def cancel_cycle_run_unowned(self, cycle_id: str, *, reason: str | None = None) -> bool:
        """Operator-fenced terminal transition for an UNOWNED CANCEL_REQUESTED cycle.

        Used by the dedicated ``/cycles/{id}/finalize-cancel`` endpoint (ADMIN-gated)
        while a recovery pause is active or when the original owning worker has
        terminated without finalizing its own drain. This does NOT invoke the
        research runner -- it only performs the Phase 2 (terminal) transition:
        CANCEL_REQUESTED -> CANCELLED, releasing ownership/lease and removing the
        active slot.

        Fenced by the status set (only CANCEL_REQUESTED -> CANCELLED is allowed),
        NOT by owner_id, because the cycle may be unowned (lease expired, owner pod
        gone). The endpoint gating + the status WHERE clause together ensure a
        terminal PUBLISHED/COMPLETED/FAILED cycle can never be retroactively
        cancelled. Idempotent: already-CANCELLED returns False.
        """
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE global_opportunity_cycle_run SET status = 'CANCELLED', "
                "error_code = ?, owner_id = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE cycle_id = ? AND status = 'CANCEL_REQUESTED' "
                f"AND (owner_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= {self._lease_clock_sql()})",
                (reason or 'CYCLE_CANCELLED', _iso(_now()), str(cycle_id)))
            rowcount = self._cycle_rowcount(cursor)
            if rowcount == 1:
                self._connection.execute("DELETE FROM global_opportunity_cycle_active WHERE cycle_id = ?", (str(cycle_id),))
        return rowcount == 1

    def _fence_cycle_publication(self, cycle_id: str, owner_id: str) -> None:
        """Called INSIDE publish_opportunity_cycle's transaction.

        Refuses to flip a cycle to PUBLISHED if it has been cancelled (terminal
        CANCELLED) or if a cancellation has been requested (CANCEL_REQUESTED):
        both are not RUNNING, so the ownership/lease fence on
        ``status = 'RUNNING'`` already prevents publication. The explicit
        CANCEL_REQUESTED/CANCELLED check makes that invariant obvious and
        testable.
        """
        status = self._connection.execute(
            "SELECT status FROM global_opportunity_cycle_run WHERE cycle_id = ?",
            (str(cycle_id),)).fetchone()
        if status is not None:
            current = dict(status).get("status")
            if current in ("CANCEL_REQUESTED", "CANCELLED"):
                raise CycleOwnershipLost(str(cycle_id))
        cursor = self._connection.execute(
            "UPDATE global_opportunity_cycle_run SET status = 'PUBLISHED', updated_at = ? "
            "WHERE cycle_id = ? AND owner_id = ? AND status = 'RUNNING' "
            f"AND lease_expires_at > {self._lease_clock_sql()}", (_iso(_now()), str(cycle_id), owner_id))
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
                f"AND lease_expires_at > {self._lease_clock_sql()} "
                "AND (status = 'RUNNING' OR (status = 'CANCEL_REQUESTED' AND ? = 0 AND EXISTS ("
                "SELECT 1 FROM global_opportunity_cycle_progress p WHERE p.cycle_id = global_opportunity_cycle_run.cycle_id "
                "AND p.phase = ? AND p.global_instrument_id = ?))) "
                "ON CONFLICT (cycle_id, phase, global_instrument_id) DO UPDATE SET "
                " state = excluded.state, disposition = excluded.disposition, failure_reason = excluded.failure_reason, "
                " attempts = global_opportunity_cycle_progress.attempts + excluded.attempts, "
                " payload = excluded.payload, updated_at = excluded.updated_at",
                (str(cycle_id), phase, str(instrument_id), str(state), disposition, failure_reason,
                 1 if start_attempt else 0,
                 json.dumps(payload, sort_keys=True, default=str) if payload is not None else None, now,
                 str(cycle_id), owner_id, int(start_attempt), phase, str(instrument_id)))
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

    def cycle_status_snapshot(self, cycle_id: str) -> dict[str, Any]:
        """Reconstruct business counters from this cycle's durable rows.

        The snapshot is deliberately derived from checkpoint state and payloads,
        never from a worker's lifetime counters.  A new process can therefore
        expose the same progress before it resumes work.
        """
        run = self.cycle_run(cycle_id) or {}
        selection = run.get("selection") or {}
        admitted = list(dict.fromkeys(selection.get("deep_ids") or selection.get("shortlist_ids") or []))
        rows = self.cycle_progress(cycle_id, PHASE_DEEP)
        admitted_set = set(map(str, admitted))
        scoped = [row for (phase, key), row in rows.items() if not admitted_set or key in admitted_set]
        terminal = [row for row in scoped if CandidateState(row["state"]) in FINAL_STATES
                     or CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
                     and int(row.get("attempts") or 0) >= DEFAULT_MAX_ATTEMPTS]
        technical = [row for row in scoped if CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE]
        # A cycle that records a flat selection['universe'] list (the
        # ETF-onboarding convention -- see app/etf_full_universe.py) but never
        # advances past BASELINE still has a genuinely known universe size and
        # a genuinely reconstructible baseline-evaluated count from durable
        # BASELINE-phase rows. This fallback activates ONLY when that key is
        # present -- every other cycle shape keeps baseline_evaluated/
        # universe_count as None exactly as before, never inventing a count
        # when no selection evidence names a universe at all.
        universe_list = selection.get('universe')
        baseline_evaluated = selection.get('baseline_evaluated')
        universe_count = selection.get('universe_count')
        baseline_terminal_count = None
        if universe_list is not None:
            if universe_count is None:
                universe_count = len(universe_list)
            baseline_rows = self.cycle_progress(cycle_id, PHASE_BASELINE)
            baseline_terminal_count = sum(
                1 for row in baseline_rows.values()
                if CandidateState(row["state"]) in FINAL_STATES
                or CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
                and int(row.get("attempts") or 0) >= DEFAULT_MAX_ATTEMPTS)
            if baseline_evaluated is None:
                baseline_evaluated = baseline_terminal_count
        # Root cause (status-reporting correction): baseline_evaluated
        # above is, for a cycle shaped like Equity Radar's (deep_ids/
        # shortlist_ids selection, no flat 'universe' list), a STATIC
        # value written once at admission time (len(scan.candidates) --
        # see global_opportunity_orchestration.py's save_selection calls),
        # not a live count of BASELINE acquisition actually finishing.
        # Reporting the BASELINE stage's progress from that static value
        # (as the stage percentage below used to) shows 100% the instant
        # the full universe is admitted, regardless of how much BASELINE
        # evidence acquisition has genuinely completed. baseline_completed
        # is therefore always reconstructed from the durable, per-
        # instrument PHASE_BASELINE rows actually finished so far --
        # unconditionally, not only for the ETF-onboarding 'universe' list
        # shape above (which already happened to compute a live count by
        # a different route and is left untouched: baseline_terminal_count
        # is reused there instead of being recomputed twice).
        if baseline_terminal_count is not None:
            baseline_completed = baseline_terminal_count
            baseline_rows_for_counts = baseline_rows
        else:
            baseline_rows_for_counts = self.cycle_progress(cycle_id, PHASE_BASELINE)
            baseline_completed = sum(
                1 for row in baseline_rows_for_counts.values()
                if CandidateState(row["state"]) in FINAL_STATES
                or CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
                and int(row.get("attempts") or 0) >= DEFAULT_MAX_ATTEMPTS)
        # Baseline failures: a terminal/attempts-exhausted BASELINE row
        # whose own payload did not record readiness (payload['ready'] is
        # not truthy) or that has no payload at all (e.g. a restorable
        # failure_reason-only row) -- distinct from baseline_completed,
        # which counts every terminal row regardless of outcome.
        baseline_failed = sum(
            1 for row in baseline_rows_for_counts.values()
            if (CandidateState(row["state"]) in FINAL_STATES
                or CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
                and int(row.get("attempts") or 0) >= DEFAULT_MAX_ATTEMPTS)
            and not ((row.get("payload") or {}).get("ready")))
        # BASELINE state breakdown (Equity Radar progress-accounting
        # follow-up, Part 2): baseline_completed/baseline_failed above are
        # both terminal-only counts (CandidateState in FINAL_STATES, or
        # RETRYABLE_FAILURE with attempts exhausted). They say nothing
        # about a row that is RETRYABLE_FAILURE but has NOT yet exhausted
        # its repair budget (the just-finished-first-pass BASELINE_INCOMPLETE
        # case), nor about a row currently IN_PROGRESS, nor about an
        # instrument with no BASELINE row at all (never yet claimed by a
        # worker -- start() is what first writes a row, as IN_PROGRESS).
        # Previously the frontend inferred all three as a single
        # 'in progress' bucket via universe_count - baseline_completed -
        # baseline_failed, which silently folded 'awaiting a bounded
        # repair retry' into 'actively processing right now'. These three
        # explicit counts let the UI partition the universe exactly,
        # mirroring the existing DEEP-phase 'technical' field (ALL current
        # RETRYABLE_FAILURE rows, exhausted or not) alongside
        # deep_technical_failure (exhausted-only) -- same established
        # pattern, now made available for BASELINE too, additive only:
        # baseline_completed/baseline_failed keep their exact prior values
        # and are not recomputed here.
        baseline_in_progress = sum(
            1 for row in baseline_rows_for_counts.values()
            if CandidateState(row["state"]) == CandidateState.IN_PROGRESS)
        baseline_awaiting_retry = sum(
            1 for row in baseline_rows_for_counts.values()
            if CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
            and int(row.get("attempts") or 0) < DEFAULT_MAX_ATTEMPTS)
        baseline_not_started = (None if universe_count is None else
            max(universe_count - len(baseline_rows_for_counts), 0))

        ready = eligible = failed = 0
        deep_ready = deep_readiness_failed = deep_technical_failure = 0
        for row in terminal:
            diagnostic = ((row.get("payload") or {}).get("diagnostic") or {})
            disposition = row.get("disposition") or diagnostic.get("disposition")
            is_ready = disposition in {"ANALYZED", "RANK_FILTERED"}
            is_readiness_failed = CandidateState(row["state"]) == CandidateState.EVIDENCE_UNAVAILABLE
            is_technical = CandidateState(row["state"]) == CandidateState.RETRYABLE_FAILURE
            ready += int(is_ready)
            failed += int(is_readiness_failed)
            eligible += int(bool(diagnostic.get("rank_eligible")))
            deep_ready += int(is_ready)
            deep_readiness_failed += int(is_readiness_failed)
            deep_technical_failure += int(is_technical)
        # The data-processing phase marker (BASELINE/CANDIDATE_SELECTION/DEEP)
        # only ever advances forward while the cycle is actively running --
        # once the durable run itself reaches a terminal status (set
        # independently via update_cycle_run, e.g. a cycle that completes
        # without ever recording deep_ids), that terminal status is the
        # authoritative phase a caller should see, not whatever
        # data-processing marker happened to be last recorded. Otherwise a
        # completed cycle can be reconstructed as permanently "BASELINE".
        phase = 'DEEP' if 'deep_ids' in selection else selection.get('phase', 'BASELINE')
        if run.get('status') in ('COMPLETED', 'FAILED', 'CANCELLED'):
            phase = run['status']

        def _stage_percentage(completed: int, total: int | None) -> int | None:
            # Deterministic and bounded: None (never fabricated) when the
            # stage's scope is unknown or not yet applicable; otherwise an
            # exact evidence ratio clamped to [0, 100]. Never derived from
            # elapsed time or worker heartbeats.
            if total is None:
                return None
            if total <= 0:
                return 100
            return max(0, min(100, round(100 * completed / total)))

        deep_total = len(admitted) if admitted else None
        # Distinct from deep_total/deep_denominator (the TRUE admitted
        # count this cycle actually used): requested_shortlist_limit is
        # only the shortlist_limit parameter the caller submitted (or its
        # request-schema default), which the orchestrator deliberately
        # does not apply once analysis_scope=FULL requests an unbounded,
        # whole-universe deep pass (see global_opportunity_cycle.py's
        # effective_shortlist_limit override). shortlist_limit_applied is
        # a plain empirical comparison of the two -- never a re-derivation
        # of that override rule -- so a caller can tell a genuinely-
        # unbounded admission (2605 of 2605, by design) apart from a
        # shortlist that should have been capped but wasn't. None when
        # there is nothing yet to compare (no admission, or no requested
        # limit on record), never a fabricated True/False.
        parameters = run.get('parameters') or {}
        analysis_scope = parameters.get('analysis_scope')
        requested_shortlist_limit = parameters.get('shortlist_limit')
        shortlist_limit_applied = (None if not admitted or requested_shortlist_limit is None
                                    else len(admitted) <= requested_shortlist_limit)
        stages = [
            {"phase": PHASE_BASELINE, "completed": baseline_completed or 0, "total": universe_count,
             "percentage": _stage_percentage(baseline_completed or 0, universe_count)},
            {"phase": PHASE_DEEP, "completed": len(terminal), "total": deep_total,
             "percentage": _stage_percentage(len(terminal), deep_total)},
        ]
        # Overall progress is a genuine evidence-weighted combination across
        # every stage that has actually started or whose scope is known --
        # the instant ANY applicable stage's scope is still unknown (not
        # merely zero), overall stays None rather than reporting a number
        # that silently ignores a stage nobody has measured yet. A terminal
        # run status alone (COMPLETED/FAILED/CANCELLED) is therefore never
        # sufficient on its own to report 100% -- that reading must still be
        # earned by every stage's own completed/total evidence, and FAILED or
        # CANCELLED never gets reported through this field as if it were a
        # successful completion.
        if run.get('status') in ('FAILED', 'CANCELLED') or any(s["percentage"] is None for s in stages):
            overall_percentage = None
        else:
            known = [s for s in stages if s["total"] is not None and s["total"] > 0]
            total_scope = sum(s["total"] for s in known)
            overall_percentage = 100 if not known or total_scope <= 0 else \
                max(0, min(100, round(100 * sum(s["completed"] for s in known) / total_scope)))

        return {
            "phase": phase,
            "baseline_evaluated": baseline_evaluated,
            # Live, durable-row-derived progress -- distinct from
            # baseline_evaluated (for Equity-shaped cycles, a static
            # admitted/scheduled count set once at admission time; see
            # the comment above). Never equals 100% merely because the
            # full universe was admitted -- it reflects only rows that
            # have actually reached a terminal/attempts-exhausted state.
            "baseline_completed": baseline_completed,
            "baseline_failed": baseline_failed,
            "baseline_in_progress": baseline_in_progress,
            "baseline_awaiting_retry": baseline_awaiting_retry,
            "baseline_not_started": baseline_not_started,
            "universe_count": universe_count,
            "deep_attempted": sum(int(row.get('attempts') or 0) > 0 for row in scoped),
            "deep_completed": len(terminal),
            "deep_denominator": len(admitted),
            "deep_ready": deep_ready,
            "deep_readiness_failed": deep_readiness_failed,
            "deep_technical_failure": deep_technical_failure,
            "ready": ready,
            "failed": failed,
            "technical": len(technical),
            "eligible": eligible,
            "stages": stages,
            "overall_percentage": overall_percentage,
            "analysis_scope": analysis_scope,
            "requested_shortlist_limit": requested_shortlist_limit,
            "shortlist_limit_applied": shortlist_limit_applied,
        }


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
        self.restored: dict[str, int] = {PHASE_BASELINE: 0, PHASE_DEEP: 0, PHASE_ETF_ONBOARD: 0}
        self.executed: dict[str, int] = {PHASE_BASELINE: 0, PHASE_DEEP: 0, PHASE_ETF_ONBOARD: 0}

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


# The three truthful terminal meanings the production research-accounting
# contract must distinguish for every applicable instrument, once a cycle
# reaches completion or is reconstructed after a restart (Slice DI-20H
# Defect 3). ``state`` (persisted per (cycle, phase, instrument) row in
# ``global_opportunity_cycle_progress``) already carries this distinction on
# its own: ``_checkpoint_deep_outcome`` resolves any technical-vs-genuine
# override (disposition + failure_class) *before* calling ``finish()``, so
# the stored ``state`` column alone -- without re-reading failure_class from
# the payload -- is sufficient to reconstruct the correct meaning after
# recovery. This mapping makes that already-true invariant explicit and
# testable; it does not change what gets persisted or how it is computed.
TERMINAL_MEANING: dict[CandidateState, str] = {
    CandidateState.COMPLETED: "FULLY_ANALYZED",
    CandidateState.EVIDENCE_UNAVAILABLE: "DATA_GENUINELY_UNAVAILABLE",
    CandidateState.RETRYABLE_FAILURE: "TECHNICAL_FAILURE_RETRY_REQUIRED",
    # A business/identity disposition (CANONICAL_INELIGIBLE, PROFILE_
    # HYDRATION_FAILED, PROFILE_IDENTITY_MISMATCH, ...) is terminal but is not
    # one of the three research-accounting meanings above -- the instrument
    # was never a candidate for research accounting in the first place.
    CandidateState.TERMINAL_OUTCOME: "TERMINAL_OUTCOME",
}


def terminal_meaning(state: CandidateState) -> str | None:
    """The truthful final-accounting meaning of a persisted checkpoint state.

    Returns None for PENDING/IN_PROGRESS (no terminal meaning yet -- the
    candidate has not finished this phase). Every final state maps to
    exactly one of the meanings above; there is no silent/unmapped state:
    ``CandidateState`` is a closed StrEnum and every one of its members is a
    key in ``TERMINAL_MEANING`` except PENDING/IN_PROGRESS.
    """
    return TERMINAL_MEANING.get(state)


def new_owner_id() -> str:
    return f"research-engine:{uuid4()}"
