ALTER TABLE portfolio_positions
    ADD COLUMN data_freshness VARCHAR(40) NOT NULL DEFAULT 'DEMO';
