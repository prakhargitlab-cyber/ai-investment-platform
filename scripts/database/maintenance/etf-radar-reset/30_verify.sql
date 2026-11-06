-- Post-reset verification (read-only). Expect all ETF counts 0 and preserved counts unchanged.
\set ON_ERROR_STOP on
\i _scope.sql
\i _flyway.sql
\i _preserved.sql
SELECT 'etf_tables_total_rows' k,
 (SELECT count(*) FROM etf_fact_observations)+(SELECT count(*) FROM etf_nav_observations)+(SELECT count(*) FROM etf_holdings_snapshots)
 +(SELECT count(*) FROM etf_holdings_positions)+(SELECT count(*) FROM etf_listing_observations)+(SELECT count(*) FROM etf_acquisition_attempts)
 +(SELECT count(*) FROM etf_radar_cycles) v
UNION ALL SELECT 'etf_cycle_runs', count(*) FROM global_opportunity_cycle_run WHERE market='ETF'
UNION ALL SELECT 'etf_active_pointer', count(*) FROM global_opportunity_cycle_active WHERE market='ETF'
UNION ALL SELECT 'etf_scoped_historical_bars', count(*) FROM global_daily_market_bars b
  WHERE EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text)))
UNION ALL SELECT 'orphan_progress', count(*) FROM global_opportunity_cycle_progress p LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL
UNION ALL SELECT 'orphan_active', count(*) FROM global_opportunity_cycle_active a LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL
UNION ALL SELECT 'trading_schedules', count(*) FROM market_trading_schedules
UNION ALL SELECT 'calendar_exceptions', count(*) FROM market_trading_calendar_exceptions;
\echo == Flyway history (absence is explicit; no metadata is created)
SELECT table_name, CASE WHEN present THEN 'PRESENT_READ_ONLY' ELSE 'ABSENT_NOT_CREATED' END AS state, row_count
FROM etf_flyway_state ORDER BY table_name;
\echo == Preserved counts (compare with 00_inventory.sql; no persistent baseline is created)
SELECT t, n FROM etf_preserved_counts ORDER BY t;
DO $verify$
BEGIN
  IF (SELECT count(*) FROM etf_fact_observations)+(SELECT count(*) FROM etf_nav_observations)
    +(SELECT count(*) FROM etf_holdings_snapshots)+(SELECT count(*) FROM etf_holdings_positions)
    +(SELECT count(*) FROM etf_listing_observations)+(SELECT count(*) FROM etf_acquisition_attempts)
    +(SELECT count(*) FROM etf_radar_cycles) <> 0
    OR EXISTS (SELECT 1 FROM global_opportunity_cycle_run WHERE market='ETF')
    OR EXISTS (SELECT 1 FROM global_opportunity_cycle_active WHERE market='ETF')
    OR EXISTS (SELECT 1 FROM global_daily_market_bars b
               WHERE EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text))))
    OR EXISTS (SELECT 1 FROM global_opportunity_cycle_progress p LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL)
    OR EXISTS (SELECT 1 FROM global_opportunity_cycle_active a LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL)
  THEN RAISE EXCEPTION 'VERIFY: ETF-owned rows or orphan lifecycle rows remain'; END IF;
END
$verify$;
