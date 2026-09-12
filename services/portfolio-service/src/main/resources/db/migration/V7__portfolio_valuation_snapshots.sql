CREATE TABLE portfolio_valuation_snapshots (
    id UUID PRIMARY KEY,
    portfolio_id UUID NOT NULL,
    user_id UUID NOT NULL,
    snapshot_timestamp TIMESTAMP NOT NULL,
    base_currency VARCHAR(3) NOT NULL,
    invested_capital_amount DECIMAL(28, 8),
    invested_capital_currency VARCHAR(3),
    invested_capital_status VARCHAR(80) NOT NULL,
    cash_amount DECIMAL(28, 8) NOT NULL,
    cash_currency VARCHAR(3) NOT NULL,
    positions_market_value_amount DECIMAL(28, 8) NOT NULL,
    positions_market_value_currency VARCHAR(3) NOT NULL,
    portfolio_market_value_amount DECIMAL(28, 8) NOT NULL,
    portfolio_market_value_currency VARCHAR(3) NOT NULL,
    unrealized_pnl_amount DECIMAL(28, 8) NOT NULL,
    unrealized_pnl_currency VARCHAR(3) NOT NULL,
    realized_pnl_amount DECIMAL(28, 8),
    realized_pnl_currency VARCHAR(3),
    broker VARCHAR(40) NOT NULL,
    source VARCHAR(80) NOT NULL,
    data_freshness VARCHAR(40) NOT NULL,
    source_sync_id VARCHAR(160) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_portfolio_valuation_snapshots_portfolio FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id),
    CONSTRAINT uk_portfolio_valuation_source_sync UNIQUE (portfolio_id, source_sync_id)
);

CREATE INDEX idx_portfolio_valuation_owner_range ON portfolio_valuation_snapshots (portfolio_id, user_id, snapshot_timestamp);
