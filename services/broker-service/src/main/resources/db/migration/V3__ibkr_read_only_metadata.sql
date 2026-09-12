ALTER TABLE broker_connections ADD COLUMN account_currency VARCHAR(3);
ALTER TABLE broker_connections ADD COLUMN provider_status VARCHAR(80);
ALTER TABLE broker_connections ADD COLUMN data_freshness VARCHAR(40);
ALTER TABLE broker_connections ADD COLUMN session_reference VARCHAR(240);
ALTER TABLE broker_connections ADD COLUMN capabilities VARCHAR(500);

CREATE INDEX idx_broker_connections_provider_status ON broker_connections (provider_status);
