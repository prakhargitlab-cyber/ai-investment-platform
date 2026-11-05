-- Shared by 00/10/20/30. Builds temp tables describing exactly what is ETF Radar-owned.
-- Requires ./etf_ids.csv in the CURRENT directory (psql \copy does not expand variables); one column,
-- header `instrument_id`, exported from portfolio-service canonical ETF instruments.
\set ON_ERROR_STOP on
SET search_path = research;
CREATE TEMP TABLE etf_scope_csv (instrument_id text) ON COMMIT PRESERVE ROWS;
\copy etf_scope_csv FROM 'etf_ids.csv' WITH (FORMAT csv, HEADER true)
-- ETF instrument set = canonical ETF ids (CSV) UNION ids already referenced by ETF-owned tables.
CREATE TEMP TABLE etf_scope AS
  SELECT lower(btrim(instrument_id)) AS instrument_id FROM etf_scope_csv WHERE instrument_id <> ''
  UNION SELECT instrument_id FROM etf_fact_observations
  UNION SELECT instrument_id FROM etf_nav_observations
  UNION SELECT instrument_id FROM etf_holdings_snapshots
  UNION SELECT instrument_id FROM etf_listing_observations
  UNION SELECT instrument_id FROM etf_acquisition_attempts;
CREATE TEMP TABLE etf_cycle_scope AS
  SELECT cycle_id FROM global_opportunity_cycle_run WHERE market = 'ETF';
