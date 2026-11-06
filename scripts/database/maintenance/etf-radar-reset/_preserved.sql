-- Exact complement of the shared-row deletion predicates, plus tables that are
-- wholly preserved. This view is re-evaluated after deletion against a frozen
-- etf_scope / etf_cycle_scope; NULL identifiers remain outside ETF ownership.
CREATE TEMP VIEW etf_preserved_counts AS
SELECT 'market_trading_schedules' t, count(*) n FROM research.market_trading_schedules
UNION ALL SELECT 'market_trading_calendar_exceptions', count(*) FROM research.market_trading_calendar_exceptions
UNION ALL SELECT 'global_daily_market_bars', count(*) FROM research.global_daily_market_bars b
  WHERE NOT EXISTS (SELECT 1 FROM etf_scope s WHERE s.instrument_id=lower(btrim(b.global_instrument_id::text)))
UNION ALL SELECT 'global_opportunity_cycle_run', count(*) FROM research.global_opportunity_cycle_run
  WHERE market IS DISTINCT FROM 'ETF'
UNION ALL SELECT 'global_opportunity_cycle_progress', count(*) FROM research.global_opportunity_cycle_progress p
  WHERE NOT EXISTS (SELECT 1 FROM etf_cycle_scope s WHERE s.cycle_id=p.cycle_id)
UNION ALL SELECT 'global_opportunity_cycle_active', count(*) FROM research.global_opportunity_cycle_active a
  WHERE a.market IS DISTINCT FROM 'ETF'
    AND NOT EXISTS (SELECT 1 FROM etf_cycle_scope s WHERE s.cycle_id=a.cycle_id)
UNION ALL SELECT 'global_financial_facts', count(*) FROM research.global_financial_facts
UNION ALL SELECT 'global_shareholding_snapshots', count(*) FROM research.global_shareholding_snapshots
UNION ALL SELECT 'global_stock_rule_engine_results', count(*) FROM research.global_stock_rule_engine_results
UNION ALL SELECT 'research_events', count(*) FROM research.research_events
UNION ALL SELECT 'research_refresh_runs', count(*) FROM research.research_refresh_runs
UNION ALL SELECT 'research_news_search_runs', count(*) FROM research.research_news_search_runs
UNION ALL SELECT 'research_event_impact_features', count(*) FROM research.research_event_impact_features
UNION ALL SELECT 'company_business_exposure_profiles', count(*) FROM research.company_business_exposure_profiles
UNION ALL SELECT 'global_opportunity_snapshot', count(*) FROM research.global_opportunity_snapshot
UNION ALL SELECT 'global_stock_suggestion', count(*) FROM research.global_stock_suggestion
UNION ALL SELECT 'global_stock_suggestion_current', count(*) FROM research.global_stock_suggestion_current
UNION ALL SELECT 'global_stock_suggestion_history', count(*) FROM research.global_stock_suggestion_history
UNION ALL SELECT 'stock_recommendation_history', count(*) FROM research.stock_recommendation_history
UNION ALL SELECT 'recommendation_current_state', count(*) FROM research.recommendation_current_state
UNION ALL SELECT 'research_documents', count(*) FROM research.research_documents
UNION ALL SELECT 'global_manual_evidence', count(*) FROM research.global_manual_evidence
UNION ALL SELECT 'global_market_price_observations', count(*) FROM research.global_market_price_observations
UNION ALL SELECT 'global_structured_market_snapshots', count(*) FROM research.global_structured_market_snapshots
UNION ALL SELECT 'research_acquisition_observations', count(*) FROM research.research_acquisition_observations;
