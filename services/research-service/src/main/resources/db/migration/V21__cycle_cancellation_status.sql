-- Durable cycle cancellation support (research-engine app/cycle_checkpoint.py).
--
-- Adds 'CANCEL_REQUESTED' and 'CANCELLED' to the
-- global_opportunity_cycle_run.status CHECK constraint so that operator-driven
-- cancellation requests (a new DELETE endpoint, ADMIN-gated) can persist a
-- two-phase cancellation contract:
--
--   CANCEL_REQUESTED -> terminal CANCELLED
--
-- Phase 1 (request): request_cycle_cancel() sets CANCEL_REQUESTED while
--   PRESERVING ownership/lease/active slot so the owning worker can drain
--   in-flight work. Phase 2 (terminal): the worker calls cancel_cycle_run()
--   to flip to CANCELLED and release ownership + active slot.
--
-- PostgreSQL does not support ALTERing a CHECK constraint in place; the constraint
-- is dropped (by its auto-generated name) and re-added with the expanded value set.
-- Existing rows are unaffected: no status is rewritten, no backfill is needed.
DROP INDEX IF EXISTS ix_global_opportunity_cycle_active_cycle_id;

ALTER TABLE global_opportunity_cycle_run
    DROP CONSTRAINT IF EXISTS global_opportunity_cycle_run_status_check,
    ADD CONSTRAINT global_opportunity_cycle_run_status_check
    CHECK (status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED'));
