-- READ-ONLY pre-reset inventory.  psql -f 00_inventory.sql
\i _scope.sql
\i _flyway.sql
\i _preserved.sql
\echo == ETF scope size
SELECT (SELECT count(*) FROM etf_scope_csv) AS canonical_ids_from_csv,
       (SELECT count(*) FROM etf_scope)      AS total_scope_ids,
       (SELECT count(*) FROM etf_cycle_scope) AS etf_cycles_in_run_table;
\echo == ETF-owned tables (will be emptied)
SELECT 'etf_holdings_positions' t, count(*) FROM etf_holdings_positions
UNION ALL SELECT 'etf_holdings_snapshots', count(*) FROM etf_holdings_snapshots
UNION ALL SELECT 'etf_fact_observations', count(*) FROM etf_fact_observations
UNION ALL SELECT 'etf_nav_observations', count(*) FROM etf_nav_observations
UNION ALL SELECT 'etf_listing_observations', count(*) FROM etf_listing_observations
UNION ALL SELECT 'etf_acquisition_attempts', count(*) FROM etf_acquisition_attempts
UNION ALL SELECT 'etf_radar_cycles', count(*) FROM etf_radar_cycles ORDER BY 1;
\echo == Shared tables: ETF-scoped rows that WILL be deleted
SELECT 'global_opportunity_cycle_progress (ETF cycles)' t, count(*) FROM global_opportunity_cycle_progress WHERE cycle_id IN (SELECT cycle_id FROM etf_cycle_scope)
UNION ALL SELECT 'global_opportunity_cycle_active (ETF)', count(*) FROM global_opportunity_cycle_active WHERE market='ETF' OR cycle_id IN (SELECT cycle_id FROM etf_cycle_scope)
UNION ALL SELECT 'global_opportunity_cycle_run (market=ETF)', count(*) FROM etf_cycle_scope
UNION ALL SELECT 'global_daily_market_bars (ETF ids)', count(*) FROM global_daily_market_bars WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope);
\echo == ETF cycle status mix (must contain no ACCEPTED/RUNNING/PUBLISHED/CANCEL_REQUESTED to reset)
SELECT status, count(*) FROM global_opportunity_cycle_run WHERE market='ETF' GROUP BY 1 ORDER BY 1;
\echo == Shared tables: ETF-id rows that are PRESERVED (not deleted) - review
SELECT 'research_documents' t, count(*) FROM research_documents WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_manual_evidence', count(*) FROM global_manual_evidence WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_market_price_observations', count(*) FROM global_market_price_observations WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_structured_market_snapshots', count(*) FROM global_structured_market_snapshots WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'research_acquisition_observations', count(*) FROM research_acquisition_observations WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope);
\echo == Equity-owned tables containing an ETF id (MUST be 0 for the reset to proceed)
SELECT t, n FROM (
 SELECT 'global_financial_facts' t, count(*) n FROM global_financial_facts WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_shareholding_snapshots', count(*) FROM global_shareholding_snapshots WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_stock_rule_engine_results', count(*) FROM global_stock_rule_engine_results WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'research_events', count(*) FROM research_events WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'research_refresh_runs', count(*) FROM research_refresh_runs WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'research_news_search_runs', count(*) FROM research_news_search_runs WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'research_event_impact_features', count(*) FROM research_event_impact_features WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'company_business_exposure_profiles', count(*) FROM company_business_exposure_profiles WHERE lower(btrim(instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_opportunity_snapshot', count(*) FROM global_opportunity_snapshot WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_stock_suggestion', count(*) FROM global_stock_suggestion WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_stock_suggestion_current', count(*) FROM global_stock_suggestion_current WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'global_stock_suggestion_history', count(*) FROM global_stock_suggestion_history WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'stock_recommendation_history', count(*) FROM stock_recommendation_history WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
 UNION ALL SELECT 'recommendation_current_state', count(*) FROM recommendation_current_state WHERE lower(btrim(global_instrument_id::text)) IN (SELECT instrument_id FROM etf_scope)
) x ORDER BY t;
\echo == Orphan check: holdings positions without snapshot (must be 0)
SELECT count(*) FROM etf_holdings_positions p LEFT JOIN etf_holdings_snapshots s ON s.evidence_id=p.snapshot_id WHERE s.evidence_id IS NULL;
\echo == PRESERVED reference rows (counts only)
SELECT 'market_trading_schedules' t, count(*) FROM market_trading_schedules
UNION ALL SELECT 'market_trading_calendar_exceptions', count(*) FROM market_trading_calendar_exceptions;
\echo == Flyway history (absence is explicit; no metadata is created)
SELECT table_name, CASE WHEN present THEN 'PRESENT_READ_ONLY' ELSE 'ABSENT_NOT_CREATED' END AS state, row_count
FROM etf_flyway_state ORDER BY table_name;
\echo == Preservation baseline (compare with 30_verify.sql after an approved reset)
SELECT t, n FROM etf_preserved_counts ORDER BY t;
