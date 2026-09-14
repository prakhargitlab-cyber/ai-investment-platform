-- Additive recommendation lifecycle; payloads retain complete versioned evidence. No backfill.

CREATE TABLE global_opportunity_snapshot (
 snapshot_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, global_instrument_id TEXT NOT NULL,
 market TEXT NOT NULL, generated_at TEXT NOT NULL, scanner_version TEXT, rule_engine_version TEXT, technical_version TEXT, sector_version TEXT, ranker_version TEXT, price_as_of TEXT, opportunity_score NUMERIC, opportunity_confidence NUMERIC, score_coverage NUMERIC, rule_engine_score NUMERIC, rank_position NUMERIC, technical_score NUMERIC, sector_score NUMERIC, valuation_score NUMERIC, quality_score NUMERIC, growth_score NUMERIC, balance_sheet_score NUMERIC, quarterly_score NUMERIC, catalyst_score NUMERIC, news_score NUMERIC, shareholding_score NUMERIC, governance_score NUMERIC, current_price NUMERIC, rank_eligible INTEGER, suppression_reasons TEXT, top_positive_reasons TEXT, top_negative_reasons TEXT, evidence_state TEXT, payload TEXT NOT NULL,
 UNIQUE(cycle_id, global_instrument_id)
);
CREATE INDEX opportunity_snapshot_instrument ON global_opportunity_snapshot(global_instrument_id, generated_at);
CREATE TABLE stock_recommendation_history (
 recommendation_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES global_opportunity_snapshot(snapshot_id),
 global_instrument_id TEXT NOT NULL, generated_at TEXT NOT NULL,
 recommendation_engine_version TEXT NOT NULL, fingerprint TEXT NOT NULL, rule_engine_version TEXT, ranker_version TEXT, new_investor_action TEXT, existing_holder_action TEXT, short_term_action TEXT, long_term_action TEXT, price_at_recommendation NUMERIC, opportunity_score NUMERIC, confidence NUMERIC, coverage NUMERIC, short_entry_low NUMERIC, short_entry_high NUMERIC, short_target_1 NUMERIC, short_target_2 NUMERIC, short_invalidation NUMERIC, long_entry_low NUMERIC, long_entry_high NUMERIC, long_fair_value NUMERIC, long_target NUMERIC, long_invalidation NUMERIC, top_positive_reasons TEXT, top_negative_reasons TEXT, evidence_snapshot TEXT, payload TEXT NOT NULL
);
CREATE INDEX recommendation_history_instrument ON stock_recommendation_history(global_instrument_id, generated_at);
CREATE TABLE recommendation_current_state (
 global_instrument_id TEXT PRIMARY KEY,
 latest_recommendation_id TEXT NOT NULL REFERENCES stock_recommendation_history(recommendation_id),
 updated_at TEXT NOT NULL, short_term_state TEXT, long_term_state TEXT, current_short_action TEXT, current_long_action TEXT, lifecycle_status TEXT, price_at_recommendation NUMERIC, current_price NUMERIC, short_target_distance_pct NUMERIC, long_target_distance_pct NUMERIC, payload TEXT NOT NULL
);
CREATE TABLE global_opportunity_top_selection (
 cycle_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, market TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE recommendation_backtest_run (
 backtest_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
);

CREATE FUNCTION reject_recommendation_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'RECOMMENDATION_HISTORY_IS_IMMUTABLE';
END;
$$;
CREATE TRIGGER immutable_opportunity_snapshots BEFORE UPDATE OR DELETE ON global_opportunity_snapshot
    FOR EACH ROW EXECUTE FUNCTION reject_recommendation_history_mutation();
CREATE TRIGGER immutable_recommendation_history BEFORE UPDATE OR DELETE ON stock_recommendation_history
    FOR EACH ROW EXECUTE FUNCTION reject_recommendation_history_mutation();
CREATE TRIGGER immutable_top_selections BEFORE UPDATE OR DELETE ON global_opportunity_top_selection
    FOR EACH ROW EXECUTE FUNCTION reject_recommendation_history_mutation();
CREATE TRIGGER immutable_backtest_runs BEFORE UPDATE OR DELETE ON recommendation_backtest_run
    FOR EACH ROW EXECUTE FUNCTION reject_recommendation_history_mutation();
