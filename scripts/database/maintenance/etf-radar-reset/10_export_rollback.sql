-- Scoped rollback export: writes exactly the rows 20_reset.sql would delete.
-- psql -v exportdir=/abs/dir -f 10_export_rollback.sql (cwd = this directory, with etf_ids.csv)   (read-only)
\i _scope.sql
\cd :exportdir
\copy (SELECT * FROM etf_holdings_snapshots ORDER BY evidence_id) TO 'etf_holdings_snapshots.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_holdings_positions ORDER BY snapshot_id, position_number) TO 'etf_holdings_positions.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_fact_observations ORDER BY evidence_id) TO 'etf_fact_observations.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_nav_observations ORDER BY evidence_id) TO 'etf_nav_observations.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_listing_observations ORDER BY evidence_id) TO 'etf_listing_observations.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_acquisition_attempts ORDER BY attempt_id) TO 'etf_acquisition_attempts.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM etf_radar_cycles ORDER BY cycle_id) TO 'etf_radar_cycles.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM global_opportunity_cycle_run WHERE cycle_id IN (SELECT cycle_id FROM etf_cycle_scope) ORDER BY cycle_id) TO 'global_opportunity_cycle_run.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM global_opportunity_cycle_progress WHERE cycle_id IN (SELECT cycle_id FROM etf_cycle_scope) ORDER BY cycle_id, phase, global_instrument_id) TO 'global_opportunity_cycle_progress.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM global_opportunity_cycle_active WHERE market='ETF' OR cycle_id IN (SELECT cycle_id FROM etf_cycle_scope)) TO 'global_opportunity_cycle_active.csv' WITH (FORMAT csv, HEADER true)
\copy (SELECT * FROM global_daily_market_bars WHERE global_instrument_id::text IN (SELECT instrument_id FROM etf_scope) ORDER BY global_instrument_id, trading_date, provider) TO 'global_daily_market_bars.csv' WITH (FORMAT csv, HEADER true)
