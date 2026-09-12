CREATE TABLE global_market_price_observations (
    instrument_id UUID NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    price NUMERIC(28, 8) NOT NULL,
    currency VARCHAR(16),
    provider VARCHAR(120) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    retrieved_at TIMESTAMP NOT NULL,
    PRIMARY KEY (instrument_id, provider, observed_at)
);

CREATE INDEX idx_market_price_observations_lookup
    ON global_market_price_observations (instrument_id, observed_at);
