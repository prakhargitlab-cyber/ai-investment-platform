ALTER TABLE portfolios ADD COLUMN acquisition_source VARCHAR(40);
ALTER TABLE portfolios ADD COLUMN source_account_reference VARCHAR(160);
ALTER TABLE portfolios ADD COLUMN last_imported_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE portfolios ADD COLUMN last_imported_filename VARCHAR(255);

CREATE UNIQUE INDEX ux_portfolios_manual_source
    ON portfolios (user_id, broker_provider, acquisition_source, source_account_reference);

CREATE TABLE portfolio_import_history (
    import_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    portfolio_id UUID NOT NULL REFERENCES portfolios(portfolio_id),
    broker_provider VARCHAR(40) NOT NULL,
    source_account_reference VARCHAR(160) NOT NULL,
    uploaded_filename VARCHAR(255) NOT NULL,
    imported_at TIMESTAMP WITH TIME ZONE NOT NULL,
    row_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    parser_type VARCHAR(80) NOT NULL,
    file_sha256 VARCHAR(64)
);

CREATE INDEX idx_portfolio_import_history_owner
    ON portfolio_import_history (user_id, portfolio_id, imported_at);

CREATE TABLE manual_import_holding_snapshots (
    snapshot_holding_id UUID PRIMARY KEY,
    import_id UUID NOT NULL REFERENCES portfolio_import_history(import_id),
    user_id UUID NOT NULL,
    portfolio_id UUID NOT NULL REFERENCES portfolios(portfolio_id),
    security_key VARCHAR(64) NOT NULL,
    isin VARCHAR(32) NOT NULL,
    symbol VARCHAR(80),
    company_name VARCHAR(240) NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    average_cost NUMERIC(38, 18),
    imported_current_price NUMERIC(38, 18),
    value_at_cost NUMERIC(38, 18),
    imported_market_value NUMERIC(38, 18),
    realized_pnl NUMERIC(38, 18),
    unrealized_pnl NUMERIC(38, 18),
    unrealized_pnl_percent NUMERIC(38, 18),
    currency VARCHAR(3) NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX idx_manual_import_snapshots_portfolio
    ON manual_import_holding_snapshots (user_id, portfolio_id, import_id);
