ALTER TABLE portfolios ADD COLUMN broker_connection_id UUID;
ALTER TABLE portfolios ADD COLUMN broker_account_id VARCHAR(120);
ALTER TABLE portfolios ADD COLUMN broker_provider VARCHAR(40);
ALTER TABLE portfolios ADD COLUMN last_successful_broker_sync_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE portfolios ADD COLUMN last_broker_sync_attempt_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE portfolios ADD COLUMN last_broker_sync_error_code VARCHAR(80);

CREATE UNIQUE INDEX ux_portfolios_broker_source
    ON portfolios (user_id, broker_connection_id, broker_account_id);

CREATE INDEX idx_portfolios_broker_connection
    ON portfolios (user_id, broker_connection_id);
