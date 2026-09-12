CREATE TABLE canonical_identity_bootstrap (
    region VARCHAR(16) PRIMARY KEY,
    lease_owner VARCHAR(64),
    lease_until TIMESTAMP WITH TIME ZONE NOT NULL,
    universe_loaded_at TIMESTAMP WITH TIME ZONE,
    reference_loaded_at TIMESTAMP WITH TIME ZONE
);
INSERT INTO canonical_identity_bootstrap(region, lease_until) VALUES ('INDIA', TIMESTAMP '1970-01-01 00:00:00');
CREATE TABLE canonical_identity_mapping_jobs (
    instrument_id UUID PRIMARY KEY REFERENCES instrument_master(instrument_id),
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(200),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX idx_identity_mapping_due ON canonical_identity_mapping_jobs(status, next_attempt_at);
