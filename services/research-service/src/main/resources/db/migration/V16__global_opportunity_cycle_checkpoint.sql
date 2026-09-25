-- Durable per-cycle / per-instrument progress for resumable production
-- opportunity cycles (research-engine app/cycle_checkpoint.py; mirrors its
-- SQLite schema). Additive only; no backfill.
--
-- global_opportunity_cycle_run: one row per resumable production cycle. The
-- partial unique index allows at most ONE active (ACCEPTED/RUNNING/PUBLISHED)
-- production cycle per market, so scheduler ticks and API submissions
-- coalesce across replicas (RollingUpdate maxSurge=1 overlaps two pods).
-- owner_id + lease_expires_at form a DB lease: exactly one worker owns and
-- writes a cycle; every progress/publication write is fenced on owner_id.
CREATE TABLE global_opportunity_cycle_run (
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

-- At most ONE active (ACCEPTED/RUNNING/PUBLISHED) production cycle per market.
-- A PRIMARY KEY slot rather than a partial unique index keeps this migration
-- portable to the H2 (MODE=PostgreSQL) test profile.
CREATE TABLE global_opportunity_cycle_active (
    market TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES global_opportunity_cycle_run (cycle_id)
);

-- One row per (cycle, phase, instrument). state distinguishes pending, in
-- progress, completed analysis, retryable technical failure, genuinely
-- unavailable evidence and terminal business/rule outcome. payload holds the
-- compact outcome needed to restore the candidate without re-acquisition.
CREATE TABLE global_opportunity_cycle_progress (
    cycle_id TEXT NOT NULL REFERENCES global_opportunity_cycle_run (cycle_id),
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
CREATE INDEX ix_opportunity_cycle_progress_state
    ON global_opportunity_cycle_progress (cycle_id, phase, state);
