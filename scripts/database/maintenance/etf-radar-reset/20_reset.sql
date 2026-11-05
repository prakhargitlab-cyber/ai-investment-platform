-- ETF Radar data reset.  DRY-RUN BY DEFAULT (rolls back).
--   psql -v expected_schema=research -v confirm=RESET_ETF_RADAR [-v commit=yes] -f 20_reset.sql
-- Operator procedure only - NOT a Flyway migration. Schema, constraints and flyway_schema_history are untouched.
\set ON_ERROR_STOP on
\if :{?commit}
\else
  \set commit no
\endif
\if :{?expected_schema}
\else
  \echo 'expected_schema is required' \quit
\endif
BEGIN;
LOCK TABLE research.global_opportunity_cycle_run, research.global_opportunity_cycle_active IN SHARE ROW EXCLUSIVE MODE;
\i _scope.sql
SELECT set_config('etf.confirm', :'confirm', true) AS _c, set_config('etf.schema', :'expected_schema', true) AS _s \gset
DO $guards$
DECLARE n bigint; blocked bigint;
BEGIN
  IF current_setting('etf.confirm') <> 'RESET_ETF_RADAR' THEN RAISE EXCEPTION 'GUARD: confirm must equal RESET_ETF_RADAR'; END IF;
  IF current_setting('etf.schema') <> 'research' OR NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='research') THEN
    RAISE EXCEPTION 'GUARD: expected_schema must be research'; END IF;
  SELECT count(*) INTO blocked FROM research.global_opportunity_cycle_run
    WHERE market='ETF' AND status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED');
  IF blocked > 0 THEN RAISE EXCEPTION 'GUARD: % ETF cycle(s) are non-terminal; quiesce/cancel them first', blocked; END IF;
  SELECT count(*) INTO n FROM research.global_opportunity_cycle_active a
    WHERE a.market <> 'ETF' AND a.cycle_id IN (SELECT cycle_id FROM etf_cycle_scope);
  IF n > 0 THEN RAISE EXCEPTION 'GUARD: non-ETF active pointer references an ETF cycle'; END IF;
  SELECT
    (SELECT count(*) FROM research.global_financial_facts WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_shareholding_snapshots WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_rule_engine_results WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.research_events WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_opportunity_snapshot WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_suggestion_history WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.stock_recommendation_history WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.recommendation_current_state WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope))
    INTO n;
  IF n > 0 THEN RAISE EXCEPTION 'GUARD: % equity-owned rows reference ETF ids; ids are not ETF-exclusive', n; END IF;
  IF EXISTS (SELECT 1 FROM research.etf_holdings_positions p LEFT JOIN research.etf_holdings_snapshots s ON s.evidence_id=p.snapshot_id WHERE s.evidence_id IS NULL) THEN
    RAISE EXCEPTION 'GUARD: orphan holdings positions'; END IF;
END
$guards$;
-- Fingerprint of PRESERVED data to prove it is untouched (checked at the end, inside the transaction).
CREATE TEMP TABLE preserved_before AS SELECT
  (SELECT count(*) FROM research.market_trading_schedules) cal_s,
  (SELECT count(*) FROM research.market_trading_calendar_exceptions) cal_e,
  (SELECT count(*) FROM research.flyway_schema_history) flyway,
  (SELECT count(*) FROM research.global_daily_market_bars WHERE global_instrument_id::text NOT IN (SELECT instrument_id FROM etf_scope)) other_bars,
  (SELECT count(*) FROM research.global_opportunity_cycle_run WHERE market <> 'ETF') other_runs,
  (SELECT count(*) FROM research.global_opportunity_cycle_progress WHERE cycle_id NOT IN (SELECT cycle_id FROM etf_cycle_scope)) other_progress,
  (SELECT count(*) FROM research.global_opportunity_cycle_active WHERE market <> 'ETF') other_active;
-- Delete in FK order.
DELETE FROM research.etf_holdings_positions;
DELETE FROM research.etf_holdings_snapshots;
DELETE FROM research.etf_fact_observations;
DELETE FROM research.etf_nav_observations;
DELETE FROM research.etf_listing_observations;
DELETE FROM research.etf_acquisition_attempts;
DELETE FROM research.etf_radar_cycles;
DELETE FROM research.global_opportunity_cycle_active WHERE market='ETF' OR cycle_id IN (SELECT cycle_id FROM etf_cycle_scope);
DELETE FROM research.global_opportunity_cycle_progress WHERE cycle_id IN (SELECT cycle_id FROM etf_cycle_scope);
DELETE FROM research.global_opportunity_cycle_run WHERE cycle_id IN (SELECT cycle_id FROM etf_cycle_scope);
DELETE FROM research.global_daily_market_bars WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope);
-- Post-conditions (abort + rollback if violated).
DO $post$
DECLARE b record;
BEGIN
  SELECT * INTO b FROM preserved_before;
  IF (SELECT count(*) FROM research.market_trading_schedules) <> b.cal_s
   OR (SELECT count(*) FROM research.market_trading_calendar_exceptions) <> b.cal_e
   OR (SELECT count(*) FROM research.flyway_schema_history) <> b.flyway
   OR (SELECT count(*) FROM research.global_daily_market_bars) <> b.other_bars
   OR (SELECT count(*) FROM research.global_opportunity_cycle_run) <> b.other_runs
   OR (SELECT count(*) FROM research.global_opportunity_cycle_progress) <> b.other_progress
   OR (SELECT count(*) FROM research.global_opportunity_cycle_active) <> b.other_active
  THEN RAISE EXCEPTION 'POSTCONDITION: preserved data counts changed'; END IF;
  IF (SELECT count(*) FROM research.etf_fact_observations)+(SELECT count(*) FROM research.etf_nav_observations)
    +(SELECT count(*) FROM research.etf_holdings_snapshots)+(SELECT count(*) FROM research.etf_holdings_positions)
    +(SELECT count(*) FROM research.etf_listing_observations)+(SELECT count(*) FROM research.etf_acquisition_attempts)
    +(SELECT count(*) FROM research.etf_radar_cycles) <> 0 THEN RAISE EXCEPTION 'POSTCONDITION: ETF tables not empty'; END IF;
END
$post$;
\echo 'Guards and post-conditions passed.'
SELECT :'commit' = 'yes' AS do_commit \gset
\if :do_commit
  COMMIT;
  \echo 'COMMITTED'
\else
  ROLLBACK;
  \echo 'DRY RUN: rolled back (pass -v commit=yes to apply)'
\endif
