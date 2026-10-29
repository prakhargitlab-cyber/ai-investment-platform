-- Durable ETF catalog onboarding checkpointing (research-engine app/cycle_checkpoint.py,
-- app/etf_full_universe.py _bounded_onboard_missing_etfs). Bounded single-ISIN ETF catalog
-- onboarding reuses the exact same generic checkpoint machinery as the existing BASELINE/DEEP
-- phases, under a third phase value: 'ETF_ONBOARD'.
--
-- Adds 'ETF_ONBOARD' to the global_opportunity_cycle_progress.phase CHECK constraint using the
-- same pattern as V21__cycle_cancellation_status.sql (PostgreSQL does not support ALTERing a
-- CHECK constraint in place; it must be dropped by its auto-generated name and re-added with
-- the expanded value set). Additive only: no row is rewritten, no backfill is needed, and
-- every existing BASELINE/DEEP row is unaffected.
ALTER TABLE global_opportunity_cycle_progress
    DROP CONSTRAINT IF EXISTS global_opportunity_cycle_progress_phase_check,
    ADD CONSTRAINT global_opportunity_cycle_progress_phase_check
    CHECK (phase IN ('BASELINE','DEEP','ETF_ONBOARD'));
