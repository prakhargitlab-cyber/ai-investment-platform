-- Dedicated durable GLOBAL auto-suggestion lifecycle, separate from user-owned
-- portfolio/watchlist recommendations. The market is scanned once and all users
-- read the same processed result. No user_id ownership column exists on any table.

CREATE TABLE global_market_scan (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED', 'IN_PROGRESS')),
    market TEXT NOT NULL,
    exchange TEXT,
    universe_count INTEGER NOT NULL,
    shortlist_count INTEGER NOT NULL,
    evaluated_count INTEGER NOT NULL,
    rank_eligible_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    controlled INTEGER NOT NULL DEFAULT 0,
    failure_reason_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_global_market_scan_created ON global_market_scan (created_at DESC);

CREATE TABLE global_stock_suggestion (
    suggestion_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES global_market_scan(scan_id),
    global_instrument_id TEXT NOT NULL,
    symbol TEXT,
    company_name TEXT,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM', 'LONG_TERM')),
    initial_action TEXT NOT NULL CHECK (initial_action IN ('STRONG_BUY', 'BUY')),
    suggested_at TEXT NOT NULL,
    suggested_price NUMERIC,
    rank INTEGER,
    opportunity_score NUMERIC,
    confidence NUMERIC,
    coverage NUMERIC,
    data_state TEXT,
    entry_range_low NUMERIC,
    entry_range_high NUMERIC,
    fair_value NUMERIC,
    target_1 NUMERIC,
    target_2 NUMERIC,
    invalidation_price NUMERIC,
    reasons JSON,
    risks JSON,
    evidence_snapshot JSON,
    engine_version TEXT NOT NULL,
    recommendation_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_global_suggestion_instrument ON global_stock_suggestion (global_instrument_id, horizon);

CREATE TABLE global_stock_suggestion_history (
    event_id TEXT PRIMARY KEY,
    suggestion_id TEXT NOT NULL REFERENCES global_stock_suggestion(suggestion_id),
    scan_id TEXT NOT NULL REFERENCES global_market_scan(scan_id),
    global_instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM', 'LONG_TERM')),
    action TEXT NOT NULL CHECK (action IN ('STRONG_BUY', 'BUY', 'PARTIAL_EXIT', 'SELL')),
    action_at TEXT NOT NULL,
    action_price NUMERIC,
    rank INTEGER,
    opportunity_score NUMERIC,
    confidence NUMERIC,
    reasons JSON,
    risks JSON,
    evidence_snapshot JSON,
    engine_version TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL
);
CREATE INDEX idx_global_suggestion_history_instrument ON global_stock_suggestion_history (global_instrument_id, horizon, action_at);
CREATE INDEX idx_global_suggestion_history_fingerprint ON global_stock_suggestion_history (suggestion_id, event_fingerprint);

CREATE TABLE global_stock_suggestion_current (
    global_instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM', 'LONG_TERM')),
    suggestion_id TEXT NOT NULL REFERENCES global_stock_suggestion(suggestion_id),
    latest_action TEXT NOT NULL CHECK (latest_action IN ('STRONG_BUY', 'BUY', 'PARTIAL_EXIT', 'SELL')),
    latest_action_at TEXT NOT NULL,
    latest_price NUMERIC,
    original_suggested_at TEXT NOT NULL,
    original_suggested_price NUMERIC,
    current_rank INTEGER,
    opportunity_score NUMERIC,
    confidence NUMERIC,
    coverage NUMERIC,
    entry_range_low NUMERIC,
    entry_range_high NUMERIC,
    fair_value NUMERIC,
    target_1 NUMERIC,
    target_2 NUMERIC,
    invalidation_price NUMERIC,
    reasons JSON,
    risks JSON,
    evidence_snapshot JSON,
    engine_version TEXT NOT NULL,
    last_scan_id TEXT NOT NULL REFERENCES global_market_scan(scan_id),
    PRIMARY KEY (global_instrument_id, horizon)
);
CREATE INDEX idx_global_suggestion_current_action ON global_stock_suggestion_current (latest_action, horizon);

CREATE FUNCTION reject_suggestion_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'SUGGESTION_HISTORY_IS_IMMUTABLE';
END;
$$;
CREATE TRIGGER immutable_global_market_scan BEFORE UPDATE OR DELETE ON global_market_scan
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_mutation();
CREATE TRIGGER immutable_global_suggestion BEFORE UPDATE OR DELETE ON global_stock_suggestion
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_mutation();
CREATE TRIGGER immutable_global_suggestion_history BEFORE UPDATE OR DELETE ON global_stock_suggestion_history
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_mutation();
