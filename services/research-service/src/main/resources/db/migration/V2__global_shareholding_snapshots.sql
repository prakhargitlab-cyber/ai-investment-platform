CREATE TABLE global_shareholding_snapshots (
    id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    period_end DATE NOT NULL,
    filing_basis VARCHAR(40),
    source_provider VARCHAR(80) NOT NULL,
    source_type VARCHAR(80) NOT NULL,
    source_identity_key VARCHAR(1000) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    research_document_id UUID,
    published_at TIMESTAMP,
    retrieved_at TIMESTAMP NOT NULL,
    confidence DECIMAL(5,4) NOT NULL,
    reliability_level VARCHAR(20) NOT NULL,
    source_mode VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_global_shareholding_snapshot_source UNIQUE (instrument_id, source_provider, source_identity_key),
    CONSTRAINT fk_global_shareholding_snapshot_document
        FOREIGN KEY (research_document_id) REFERENCES research_documents (document_id)
);

CREATE INDEX idx_global_shareholding_snapshots_instrument_period
    ON global_shareholding_snapshots (instrument_id, period_end);
CREATE INDEX idx_global_shareholding_snapshots_instrument_period_provider
    ON global_shareholding_snapshots (instrument_id, period_end, source_provider);

CREATE TABLE global_shareholding_snapshot_values (
    id UUID PRIMARY KEY,
    snapshot_id UUID NOT NULL,
    category VARCHAR(40) NOT NULL,
    percentage NUMERIC(7,4) NOT NULL,
    metric_basis VARCHAR(80),
    raw_source_label VARCHAR(240),
    source_locator VARCHAR(600),
    evidence_text VARCHAR(1200),
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_global_shareholding_snapshot_value UNIQUE (snapshot_id, category),
    CONSTRAINT ck_global_shareholding_pledge_basis
        CHECK (category <> 'PROMOTER_PLEDGE' OR metric_basis IS NOT NULL),
    CONSTRAINT fk_global_shareholding_snapshot_value_snapshot
        FOREIGN KEY (snapshot_id) REFERENCES global_shareholding_snapshots (id)
);
