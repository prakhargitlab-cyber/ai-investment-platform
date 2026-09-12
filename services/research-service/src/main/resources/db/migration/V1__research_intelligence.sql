CREATE TABLE research_documents (
    document_id UUID PRIMARY KEY,
    company_id UUID,
    instrument_id UUID,
    source_type VARCHAR(60) NOT NULL,
    source_classification VARCHAR(60) NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    canonical_url VARCHAR(1000) NOT NULL,
    original_url VARCHAR(1000) NOT NULL,
    document_type VARCHAR(40) NOT NULL,
    title VARCHAR(500),
    published_at TIMESTAMP WITH TIME ZONE,
    retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    source_mode VARCHAR(20) NOT NULL,
    freshness VARCHAR(40) NOT NULL,
    reliability_level VARCHAR(20) NOT NULL,
    status VARCHAR(40) NOT NULL,
    entity_resolution_confidence DECIMAL(5, 4) NOT NULL,
    discovered_at TIMESTAMP WITH TIME ZONE,
    discovery_provider VARCHAR(120),
    source_independence_key CHAR(64),
    duplicate_of_document_id UUID,
    normalized_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT fk_research_documents_duplicate FOREIGN KEY (duplicate_of_document_id) REFERENCES research_documents (document_id)
);

CREATE UNIQUE INDEX ux_research_documents_canonical_url ON research_documents (canonical_url);
CREATE UNIQUE INDEX ux_research_documents_content_hash ON research_documents (content_hash);
CREATE INDEX idx_research_documents_instrument_id ON research_documents (instrument_id);
CREATE INDEX idx_research_documents_company_id ON research_documents (company_id);
CREATE INDEX idx_research_documents_source_mode ON research_documents (source_mode);

CREATE TABLE research_events (
    event_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    company_id UUID NOT NULL,
    event_fingerprint VARCHAR(1600) NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    event_date TIMESTAMP WITH TIME ZONE,
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    title VARCHAR(300) NOT NULL,
    summary VARCHAR(1200) NOT NULL,
    impact VARCHAR(40) NOT NULL,
    time_horizon VARCHAR(40) NOT NULL,
    confidence DECIMAL(5, 4) NOT NULL,
    status VARCHAR(40) NOT NULL,
    source_document_id UUID NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    source_type VARCHAR(60) NOT NULL,
    source_classification VARCHAR(60) NOT NULL,
    reliability VARCHAR(20) NOT NULL,
    source_mode VARCHAR(20) NOT NULL,
    currency VARCHAR(3),
    monetary_value DECIMAL(28, 4),
    monetary_original VARCHAR(80),
    percentage_value DECIMAL(12, 4),
    percentage_original VARCHAR(80),
    customer VARCHAR(240),
    counterparty VARCHAR(240),
    location VARCHAR(240),
    capacity_value DECIMAL(28, 4),
    capacity_unit VARCHAR(40),
    raw_evidence_reference VARCHAR(600) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    retrieved_at TIMESTAMP WITH TIME ZONE,
    independence_key CHAR(64),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT fk_research_events_document FOREIGN KEY (source_document_id) REFERENCES research_documents (document_id)
);

CREATE UNIQUE INDEX ux_research_events_fingerprint ON research_events (event_fingerprint);
CREATE INDEX idx_research_events_instrument_id ON research_events (instrument_id);
CREATE INDEX idx_research_events_company_id ON research_events (company_id);
CREATE INDEX idx_research_events_type ON research_events (event_type);

CREATE TABLE research_event_sources (
    event_source_id UUID PRIMARY KEY,
    event_id UUID NOT NULL,
    document_id UUID NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    canonical_url VARCHAR(1000) NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    publisher VARCHAR(240),
    source_classification VARCHAR(60) NOT NULL,
    evidence_excerpt VARCHAR(600) NOT NULL,
    reliability VARCHAR(20) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_mode VARCHAR(20) NOT NULL,
    independent BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT fk_research_event_sources_event FOREIGN KEY (event_id) REFERENCES research_events (event_id),
    CONSTRAINT fk_research_event_sources_document FOREIGN KEY (document_id) REFERENCES research_documents (document_id),
    CONSTRAINT ux_research_event_sources_event_document_excerpt UNIQUE (event_id, document_id, evidence_excerpt)
);

CREATE TABLE research_refresh_runs (
    refresh_run_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    company_id UUID NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(40) NOT NULL,
    mode VARCHAR(40) NOT NULL,
    documents_discovered INTEGER NOT NULL DEFAULT 0,
    documents_accepted INTEGER NOT NULL DEFAULT 0,
    events_extracted INTEGER NOT NULL DEFAULT 0,
    events_created INTEGER NOT NULL DEFAULT 0,
    events_updated INTEGER NOT NULL DEFAULT 0,
    deduplicated_count INTEGER NOT NULL DEFAULT 0,
    correlation_id VARCHAR(120),
    safe_error_code VARCHAR(120),
    safe_error_message VARCHAR(500),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX idx_research_refresh_runs_instrument_id ON research_refresh_runs (instrument_id);
CREATE INDEX idx_research_refresh_runs_status ON research_refresh_runs (status);
