CREATE TABLE portfolios (
    portfolio_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    name VARCHAR(160) NOT NULL,
    base_currency VARCHAR(3) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE broker_accounts (
    broker_account_id VARCHAR(80) PRIMARY KEY,
    user_id UUID NOT NULL,
    broker_type VARCHAR(40) NOT NULL,
    external_account_reference VARCHAR(160),
    display_name VARCHAR(160) NOT NULL,
    base_currency VARCHAR(3) NOT NULL,
    status VARCHAR(40) NOT NULL
);

CREATE TABLE instruments (
    instrument_id UUID PRIMARY KEY,
    isin VARCHAR(20),
    ticker VARCHAR(40) NOT NULL,
    exchange VARCHAR(40) NOT NULL,
    mic VARCHAR(12),
    company_name VARCHAR(240) NOT NULL,
    asset_type VARCHAR(40) NOT NULL,
    country VARCHAR(80),
    trading_currency VARCHAR(3) NOT NULL,
    sector VARCHAR(120),
    industry VARCHAR(160)
);

CREATE TABLE portfolio_positions (
    position_id UUID PRIMARY KEY,
    portfolio_id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    quantity DECIMAL(28, 8) NOT NULL,
    average_cost_amount DECIMAL(28, 8) NOT NULL,
    average_cost_currency VARCHAR(3) NOT NULL,
    current_price_amount DECIMAL(28, 8) NOT NULL,
    current_price_currency VARCHAR(3) NOT NULL,
    broker_account_id VARCHAR(80) NOT NULL,
    last_updated TIMESTAMP NOT NULL,
    CONSTRAINT fk_positions_portfolio FOREIGN KEY (portfolio_id) REFERENCES portfolios (portfolio_id),
    CONSTRAINT fk_positions_instrument FOREIGN KEY (instrument_id) REFERENCES instruments (instrument_id),
    CONSTRAINT fk_positions_broker_account FOREIGN KEY (broker_account_id) REFERENCES broker_accounts (broker_account_id)
);

CREATE INDEX idx_portfolios_user_id ON portfolios (user_id);
CREATE INDEX idx_broker_accounts_user_id ON broker_accounts (user_id);
CREATE INDEX idx_positions_portfolio_id ON portfolio_positions (portfolio_id);
CREATE INDEX idx_positions_instrument_id ON portfolio_positions (instrument_id);
CREATE INDEX idx_positions_broker_account_id ON portfolio_positions (broker_account_id);
CREATE INDEX idx_instruments_ticker ON instruments (ticker);
CREATE INDEX idx_instruments_isin ON instruments (isin);
CREATE INDEX idx_instruments_exchange ON instruments (exchange);
