CREATE TABLE global_stock_rule_engine_results (
    global_instrument_id UUID NOT NULL,
    rule_engine_version VARCHAR(80) NOT NULL,
    input_fingerprint CHAR(64) NOT NULL,
    calculated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    input_as_of TIMESTAMP WITH TIME ZONE,
    overall_score NUMERIC(6, 2),
    quality_score NUMERIC(6, 2),
    opportunity_score NUMERIC(6, 2),
    risk_score NUMERIC(6, 2),
    confidence_score NUMERIC(6, 2) NOT NULL,
    decision_signal VARCHAR(40) NOT NULL,
    partial BOOLEAN NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (global_instrument_id, rule_engine_version, input_fingerprint),
    CHECK (overall_score IS NULL OR overall_score BETWEEN 0 AND 100),
    CHECK (quality_score IS NULL OR quality_score BETWEEN 0 AND 100),
    CHECK (opportunity_score IS NULL OR opportunity_score BETWEEN 0 AND 100),
    CHECK (risk_score IS NULL OR risk_score BETWEEN 0 AND 100),
    CHECK (confidence_score BETWEEN 0 AND 100)
);

CREATE INDEX idx_stock_rule_engine_latest
    ON global_stock_rule_engine_results (global_instrument_id, rule_engine_version, calculated_at DESC);

COMMENT ON TABLE global_stock_rule_engine_results IS
    'Versioned deterministic global public-company scores; never stores portfolio quantity, cost, P&L, or allocation.';
