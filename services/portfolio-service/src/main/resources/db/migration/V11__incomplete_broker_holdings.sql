CREATE TABLE broker_holdings (
    holding_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    portfolio_id UUID NOT NULL REFERENCES portfolios(portfolio_id),
    connection_id UUID NOT NULL,
    broker_account_id VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    provider_instrument_id VARCHAR(255) NOT NULL,
    isin VARCHAR(32),
    symbol VARCHAR(255),
    quantity NUMERIC(38, 18) NOT NULL,
    average_cost_amount NUMERIC(38, 18),
    current_price_amount NUMERIC(38, 18),
    market_value_amount NUMERIC(38, 18),
    currency VARCHAR(3),
    exchange VARCHAR(32),
    valuation_completeness VARCHAR(32) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    observed_at TIMESTAMPTZ NOT NULL,
    sync_generation_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_broker_holding_source UNIQUE
        (user_id, connection_id, broker_account_id, provider, provider_instrument_id)
);
CREATE INDEX idx_broker_holdings_portfolio_active ON broker_holdings(portfolio_id, active);
CREATE INDEX idx_broker_holdings_source ON broker_holdings(user_id, connection_id, broker_account_id);
