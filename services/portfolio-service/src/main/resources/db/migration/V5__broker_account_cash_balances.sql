CREATE TABLE broker_account_cash_balances (
    broker_account_id VARCHAR(80) PRIMARY KEY,
    user_id UUID NOT NULL,
    cash_amount NUMERIC(28, 8) NOT NULL,
    cash_currency VARCHAR(3) NOT NULL,
    settled_cash_amount NUMERIC(28, 8),
    settled_cash_currency VARCHAR(3),
    net_liquidation_value_amount NUMERIC(28, 8),
    net_liquidation_value_currency VARCHAR(3),
    stock_market_value_amount NUMERIC(28, 8),
    stock_market_value_currency VARCHAR(3),
    unrealized_pnl_amount NUMERIC(28, 8),
    unrealized_pnl_currency VARCHAR(3),
    realized_pnl_amount NUMERIC(28, 8),
    realized_pnl_currency VARCHAR(3),
    source VARCHAR(40) NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_cash_broker_account FOREIGN KEY (broker_account_id) REFERENCES broker_accounts (broker_account_id)
);

CREATE INDEX idx_cash_user_id ON broker_account_cash_balances (user_id);
CREATE INDEX idx_cash_source ON broker_account_cash_balances (source);
