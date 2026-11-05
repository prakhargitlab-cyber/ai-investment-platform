-- Rollback restore from the scoped export (10_export_rollback.sql). Single transaction; fails (and rolls back)
-- on any primary-key conflict, so it cannot overwrite data created by a later run: reset those first.
--   psql -v ON_ERROR_STOP=1 -1 -v exportdir=/abs/dir -f 40_restore.sql
SET search_path = research;
\cd :exportdir
\copy global_opportunity_cycle_run FROM 'global_opportunity_cycle_run.csv' WITH (FORMAT csv, HEADER true)
\copy global_opportunity_cycle_progress FROM 'global_opportunity_cycle_progress.csv' WITH (FORMAT csv, HEADER true)
\copy global_opportunity_cycle_active FROM 'global_opportunity_cycle_active.csv' WITH (FORMAT csv, HEADER true)
\copy etf_holdings_snapshots FROM 'etf_holdings_snapshots.csv' WITH (FORMAT csv, HEADER true)
\copy etf_holdings_positions FROM 'etf_holdings_positions.csv' WITH (FORMAT csv, HEADER true)
\copy etf_fact_observations FROM 'etf_fact_observations.csv' WITH (FORMAT csv, HEADER true)
\copy etf_nav_observations FROM 'etf_nav_observations.csv' WITH (FORMAT csv, HEADER true)
\copy etf_listing_observations FROM 'etf_listing_observations.csv' WITH (FORMAT csv, HEADER true)
\copy etf_acquisition_attempts FROM 'etf_acquisition_attempts.csv' WITH (FORMAT csv, HEADER true)
\copy etf_radar_cycles FROM 'etf_radar_cycles.csv' WITH (FORMAT csv, HEADER true)
\copy global_daily_market_bars FROM 'global_daily_market_bars.csv' WITH (FORMAT csv, HEADER true)
