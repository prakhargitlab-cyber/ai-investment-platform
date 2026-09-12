ALTER TABLE broker_accounts ADD COLUMN connection_id UUID;
ALTER TABLE broker_accounts ADD COLUMN source_broker_account_id VARCHAR(120);

UPDATE broker_accounts
SET source_broker_account_id = broker_account_id
WHERE source_broker_account_id IS NULL;

ALTER TABLE portfolio_positions ADD COLUMN source_type VARCHAR(40) NOT NULL DEFAULT 'BROKER';
ALTER TABLE portfolio_positions ADD COLUMN source_connection_id UUID;
ALTER TABLE portfolio_positions ADD COLUMN source_broker_type VARCHAR(40);
ALTER TABLE portfolio_positions ADD COLUMN source_broker_account_id VARCHAR(120);
ALTER TABLE portfolio_positions ADD COLUMN external_instrument_provider VARCHAR(40);
ALTER TABLE portfolio_positions ADD COLUMN external_instrument_id VARCHAR(120);
ALTER TABLE portfolio_positions ADD COLUMN observed_at TIMESTAMP;
ALTER TABLE portfolio_positions ADD COLUMN sync_generation_id UUID;
ALTER TABLE portfolio_positions ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE portfolio_positions
SET source_broker_account_id = broker_account_id,
    observed_at = last_updated;

UPDATE portfolio_positions position
SET source_broker_type = (
    SELECT account.broker_type
    FROM broker_accounts account
    WHERE account.broker_account_id = position.broker_account_id
);

UPDATE portfolio_positions position
SET external_instrument_provider = (
        SELECT instrument.provider
        FROM instruments instrument
        WHERE instrument.instrument_id = position.instrument_id
    ),
    external_instrument_id = (
        SELECT instrument.provider_instrument_id
        FROM instruments instrument
        WHERE instrument.instrument_id = position.instrument_id
    );

CREATE INDEX idx_broker_accounts_owner_connection
    ON broker_accounts (user_id, broker_type, connection_id, source_broker_account_id);

CREATE UNIQUE INDEX ux_broker_positions_source_identity
    ON portfolio_positions (portfolio_id, source_type, source_connection_id, source_broker_account_id, external_instrument_provider, external_instrument_id);

CREATE INDEX idx_positions_source_scope
    ON portfolio_positions (portfolio_id, source_connection_id, source_broker_account_id, source_type, active);

CREATE UNIQUE INDEX ux_instruments_provider_identity
    ON instruments (provider, provider_instrument_id);

CREATE TABLE broker_account_cash_balance_entries (
    cash_balance_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    connection_id UUID,
    broker_account_id VARCHAR(120) NOT NULL,
    cash_currency VARCHAR(3) NOT NULL,
    cash_amount NUMERIC(28, 8) NOT NULL,
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
    CONSTRAINT fk_cash_entries_app_user FOREIGN KEY (user_id) REFERENCES app_users (id),
    CONSTRAINT ux_cash_entries_source_currency UNIQUE (user_id, connection_id, broker_account_id, cash_currency)
);

CREATE INDEX idx_cash_entries_user_id ON broker_account_cash_balance_entries (user_id);
CREATE INDEX idx_cash_entries_source_scope ON broker_account_cash_balance_entries (user_id, connection_id, broker_account_id);
