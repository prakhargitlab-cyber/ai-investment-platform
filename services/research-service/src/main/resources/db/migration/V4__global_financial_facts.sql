CREATE TABLE global_financial_facts (
    instrument_id UUID NOT NULL,
    metric VARCHAR(80) NOT NULL,
    period_end VARCHAR(64) NOT NULL,
    period_type VARCHAR(32) NOT NULL,
    reporting_basis VARCHAR(32) NOT NULL DEFAULT '',
    fact_value NUMERIC(28,8) NOT NULL,
    unit VARCHAR(32),
    source_provider VARCHAR(80) NOT NULL,
    source_identity VARCHAR(1000) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    source_type VARCHAR(80),
    published_at TIMESTAMP,
    retrieved_at TIMESTAMP NOT NULL,
    confidence DECIMAL(5,4),
    source_mode VARCHAR(20) NOT NULL,
    source_tier INTEGER NOT NULL,
    CONSTRAINT pk_global_financial_fact PRIMARY KEY (instrument_id, metric, period_end, period_type, reporting_basis)
);

CREATE INDEX idx_global_financial_facts_instrument_period
    ON global_financial_facts (instrument_id, period_end, period_type);
