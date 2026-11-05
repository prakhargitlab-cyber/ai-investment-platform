-- Post-reset verification (read-only). Expect all ETF counts 0 and preserved counts unchanged.
SET search_path = research;
SELECT 'etf_tables_total_rows' k,
 (SELECT count(*) FROM etf_fact_observations)+(SELECT count(*) FROM etf_nav_observations)+(SELECT count(*) FROM etf_holdings_snapshots)
 +(SELECT count(*) FROM etf_holdings_positions)+(SELECT count(*) FROM etf_listing_observations)+(SELECT count(*) FROM etf_acquisition_attempts)
 +(SELECT count(*) FROM etf_radar_cycles) v
UNION ALL SELECT 'etf_cycle_runs', count(*) FROM global_opportunity_cycle_run WHERE market='ETF'
UNION ALL SELECT 'etf_active_pointer', count(*) FROM global_opportunity_cycle_active WHERE market='ETF'
UNION ALL SELECT 'orphan_progress', count(*) FROM global_opportunity_cycle_progress p LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL
UNION ALL SELECT 'orphan_active', count(*) FROM global_opportunity_cycle_active a LEFT JOIN global_opportunity_cycle_run r USING (cycle_id) WHERE r.cycle_id IS NULL
UNION ALL SELECT 'trading_schedules', count(*) FROM market_trading_schedules
UNION ALL SELECT 'calendar_exceptions', count(*) FROM market_trading_calendar_exceptions
UNION ALL SELECT 'flyway_rows', count(*) FROM flyway_schema_history;
