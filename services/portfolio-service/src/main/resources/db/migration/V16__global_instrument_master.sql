CREATE TABLE instrument_master (
    instrument_id UUID PRIMARY KEY,
    isin VARCHAR(20),
    normalized_isin VARCHAR(20),
    canonical_name VARCHAR(240) NOT NULL,
    asset_type VARCHAR(40) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    country VARCHAR(80),
    primary_exchange VARCHAR(40),
    primary_symbol VARCHAR(80),
    status VARCHAR(40) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE UNIQUE INDEX ux_instrument_master_normalized_isin ON instrument_master (normalized_isin);
CREATE INDEX idx_instrument_master_exchange_symbol ON instrument_master (primary_exchange, primary_symbol);

CREATE TABLE instrument_provider_mappings (
    mapping_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    provider VARCHAR(60) NOT NULL,
    provider_symbol VARCHAR(120),
    provider_instrument_id VARCHAR(160),
    exchange VARCHAR(80),
    currency VARCHAR(3),
    status VARCHAR(40) NOT NULL,
    resolution_source VARCHAR(80) NOT NULL,
    confidence DECIMAL(5,4) NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE NOT NULL,
    verified_at TIMESTAMP WITH TIME ZONE,
    last_validation_at TIMESTAMP WITH TIME ZONE,
    failure_reason VARCHAR(240),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_instrument_provider_mapping_master FOREIGN KEY (instrument_id) REFERENCES instrument_master (instrument_id),
    CONSTRAINT ux_instrument_provider_id UNIQUE (provider, provider_instrument_id),
    CONSTRAINT ux_instrument_provider_symbol UNIQUE (provider, exchange, provider_symbol)
);

CREATE INDEX idx_instrument_provider_mapping_instrument ON instrument_provider_mappings (instrument_id);
CREATE INDEX idx_instrument_provider_mapping_status ON instrument_provider_mappings (provider, status);

ALTER TABLE instruments ADD COLUMN master_instrument_id UUID;
ALTER TABLE instruments ADD CONSTRAINT fk_legacy_instrument_master FOREIGN KEY (master_instrument_id) REFERENCES instrument_master (instrument_id);
CREATE INDEX idx_instruments_master_instrument_id ON instruments (master_instrument_id);
