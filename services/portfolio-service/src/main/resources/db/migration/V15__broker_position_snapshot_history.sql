CREATE TABLE broker_position_snapshots (
    snapshot_position_id UUID PRIMARY KEY,
    sync_generation_id UUID NOT NULL,
    user_id UUID NOT NULL,
    portfolio_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    broker_account_id VARCHAR(255) NOT NULL,
    instrument_provider VARCHAR(64) NOT NULL,
    provider_instrument_id VARCHAR(255) NOT NULL,
    isin VARCHAR(32),
    ticker VARCHAR(64),
    quantity NUMERIC(38, 18) NOT NULL,
    average_cost_amount NUMERIC(38, 18),
    average_cost_currency VARCHAR(3),
    current_price_amount NUMERIC(38, 18),
    current_price_currency VARCHAR(3),
    market_value_amount NUMERIC(38, 18),
    market_value_currency VARCHAR(3),
    unrealized_pnl_amount NUMERIC(38, 18),
    unrealized_pnl_currency VARCHAR(3),
    effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source VARCHAR(64) NOT NULL,
    CONSTRAINT fk_broker_position_snapshot_portfolio
        FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
);

CREATE INDEX idx_broker_position_snapshot_history
    ON broker_position_snapshots(user_id, portfolio_id, effective_at);

CREATE UNIQUE INDEX uq_broker_position_snapshot_identity
    ON broker_position_snapshots(sync_generation_id, broker_account_id, instrument_provider, provider_instrument_id);
