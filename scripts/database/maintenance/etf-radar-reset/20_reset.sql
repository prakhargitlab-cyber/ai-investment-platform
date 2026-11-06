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
  \set expected_schema ''
\endif
\if :{?confirm}
\else
  \set confirm ''
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
    WHERE a.market IS DISTINCT FROM 'ETF' AND a.cycle_id IN (SELECT cycle_id FROM etf_cycle_scope);
  IF n > 0 THEN RAISE EXCEPTION 'GUARD: non-ETF active pointer references an ETF cycle'; END IF;
  SELECT
    (SELECT count(*) FROM research.global_financial_facts WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_shareholding_snapshots WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_rule_engine_results WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.research_events WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.research_refresh_runs WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.research_news_search_runs WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.research_event_impact_features WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.company_business_exposure_profiles WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_opportunity_snapshot WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_suggestion WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_suggestion_current WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.global_stock_suggestion_history WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.stock_recommendation_history WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)) +
    (SELECT count(*) FROM research.recommendation_current_state WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope))
    INTO n;
  IF n > 0 THEN RAISE EXCEPTION 'GUARD: % equity-owned rows reference ETF ids; ids are not ETF-exclusive', n; END IF;
  IF EXISTS (SELECT 1 FROM research.etf_holdings_positions p LEFT JOIN research.etf_holdings_snapshots s ON s.evidence_id=p.snapshot_id WHERE s.evidence_id IS NULL) THEN
    RAISE EXCEPTION 'GUARD: orphan holdings positions'; END IF;
END
$guards$;
-- Count only rows outside the exact deletion predicates; preserve all rows in
-- Equity-owned and shared-evidence tables. Snapshot before the first DELETE.
\i _flyway.sql
\i _preserved.sql
CREATE TEMP TABLE preserved_before AS SELECT * FROM etf_preserved_counts;
CREATE TEMP TABLE etf_bars_before AS SELECT
  count(*) AS total,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text)))) AS owned,
  count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text)))) AS preserved
FROM research.global_daily_market_bars b;
\echo 'Flyway metadata state (missing tables remain absent):'
SELECT table_name, present, row_count FROM etf_flyway_state ORDER BY table_name;
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
DELETE FROM research.global_daily_market_bars b
WHERE EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text)));
-- Post-conditions (abort + rollback if violated).
DO $post$
DECLARE b record; f record; relation regclass; n bigint; h text;
BEGIN
  IF EXISTS (
    (SELECT * FROM preserved_before EXCEPT SELECT * FROM etf_preserved_counts)
    UNION ALL
    (SELECT * FROM etf_preserved_counts EXCEPT SELECT * FROM preserved_before)
  )
  THEN RAISE EXCEPTION 'POSTCONDITION: preserved data counts changed'; END IF;
  SELECT * INTO b FROM etf_bars_before;
  IF b.total <> b.owned + b.preserved
     OR (SELECT p.n FROM preserved_before p WHERE p.t='global_daily_market_bars') <> b.preserved
     OR (SELECT count(*) FROM research.global_daily_market_bars) <> b.preserved
     OR EXISTS (SELECT 1 FROM research.global_daily_market_bars bars
                WHERE EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(bars.global_instrument_id::text))))
  THEN RAISE EXCEPTION 'POSTCONDITION: ETF/non-ETF bar conservation failed'; END IF;
  FOR f IN SELECT * FROM etf_flyway_state LOOP
    relation := to_regclass(format('research.%I', f.table_name));
    IF relation::oid IS DISTINCT FROM f.relation_oid THEN
      RAISE EXCEPTION 'POSTCONDITION: Flyway history presence or identity changed';
    END IF;
    IF f.present THEN
      EXECUTE format(
        'SELECT count(*), md5(coalesce(string_agg(t::text, ''|'' ORDER BY t::text), '''')) FROM %s t',
        relation) INTO n, h;
      IF n IS DISTINCT FROM f.row_count OR h IS DISTINCT FROM f.row_hash THEN
        RAISE EXCEPTION 'POSTCONDITION: Flyway metadata changed';
      END IF;
    END IF;
  END LOOP;
  IF (SELECT count(*) FROM research.etf_fact_observations)+(SELECT count(*) FROM research.etf_nav_observations)
    +(SELECT count(*) FROM research.etf_holdings_snapshots)+(SELECT count(*) FROM research.etf_holdings_positions)
    +(SELECT count(*) FROM research.etf_listing_observations)+(SELECT count(*) FROM research.etf_acquisition_attempts)
    +(SELECT count(*) FROM research.etf_radar_cycles) <> 0 THEN RAISE EXCEPTION 'POSTCONDITION: ETF tables not empty'; END IF;
  IF EXISTS (SELECT 1 FROM research.global_opportunity_cycle_run WHERE market='ETF')
     OR EXISTS (SELECT 1 FROM research.global_opportunity_cycle_progress p WHERE EXISTS (SELECT 1 FROM etf_cycle_scope s WHERE s.cycle_id=p.cycle_id))
     OR EXISTS (SELECT 1 FROM research.global_opportunity_cycle_active a WHERE a.market='ETF' OR EXISTS (SELECT 1 FROM etf_cycle_scope s WHERE s.cycle_id=a.cycle_id))
  THEN RAISE EXCEPTION 'POSTCONDITION: ETF lifecycle rows remain'; END IF;
END
$post$;
\echo 'Historical bars before reset: total / ETF-owned / preserved'
SELECT total, owned, preserved FROM etf_bars_before;
\echo 'Preservation counts before / after (must match)'
SELECT b.t, b.n AS before_count, a.n AS after_count FROM preserved_before b JOIN etf_preserved_counts a USING (t) ORDER BY b.t;
\echo 'Guards and post-conditions passed.'
SELECT :'commit' = 'yes' AS do_commit \gset
\if :do_commit
  COMMIT;
  \echo 'COMMITTED'
\else
  ROLLBACK;
  \echo 'DRY RUN: rolled back (pass -v commit=yes to apply)'
\endif
