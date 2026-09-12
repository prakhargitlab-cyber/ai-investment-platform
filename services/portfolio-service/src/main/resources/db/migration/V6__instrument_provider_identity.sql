ALTER TABLE instruments ADD COLUMN provider VARCHAR(40);
ALTER TABLE instruments ADD COLUMN provider_instrument_id VARCHAR(80);

CREATE INDEX idx_instruments_provider_identity ON instruments (provider, provider_instrument_id);
