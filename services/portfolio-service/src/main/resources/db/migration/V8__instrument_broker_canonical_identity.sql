ALTER TABLE instruments ADD COLUMN broker_symbol VARCHAR(80);
ALTER TABLE instruments ADD COLUMN broker_description VARCHAR(240);
ALTER TABLE instruments ADD COLUMN broker_exchange VARCHAR(80);
ALTER TABLE instruments ADD COLUMN canonical_symbol VARCHAR(80);
ALTER TABLE instruments ADD COLUMN canonical_name VARCHAR(240);
ALTER TABLE instruments ADD COLUMN canonical_exchange VARCHAR(40);
ALTER TABLE instruments ADD COLUMN canonical_mic VARCHAR(12);
ALTER TABLE instruments ADD COLUMN security_type VARCHAR(40);

CREATE INDEX idx_instruments_canonical_symbol_exchange ON instruments (canonical_symbol, canonical_exchange);
