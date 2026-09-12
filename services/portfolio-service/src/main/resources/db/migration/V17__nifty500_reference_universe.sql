CREATE TABLE nifty500_universe (instrument_id UUID PRIMARY KEY, symbol VARCHAR(64) NOT NULL, isin VARCHAR(16) NOT NULL, company_name VARCHAR(500) NOT NULL, industry VARCHAR(500) NOT NULL, canonical_sector VARCHAR(120), source VARCHAR(120) NOT NULL, retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL);
CREATE UNIQUE INDEX uq_nifty500_universe_isin ON nifty500_universe(isin);
CREATE INDEX idx_nifty500_universe_order ON nifty500_universe(symbol, instrument_id);
