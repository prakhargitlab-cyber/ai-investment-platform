CREATE TABLE research_sources (
    source_id VARCHAR(120) PRIMARY KEY,
    source_type VARCHAR(60) NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    reliability_level VARCHAR(20) NOT NULL,
    fetch_strategy VARCHAR(40) NOT NULL,
    automatic_access VARCHAR(40) NOT NULL,
    javascript_required BOOLEAN NOT NULL DEFAULT FALSE,
    requests_per_minute INTEGER NOT NULL,
    min_delay_seconds DECIMAL(12, 3) NOT NULL
);

CREATE TABLE company_research_profiles (
    company_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL UNIQUE,
    company_name VARCHAR(240) NOT NULL,
    isin VARCHAR(20),
    ticker VARCHAR(40) NOT NULL,
    exchange VARCHAR(40) NOT NULL,
    mic VARCHAR(12),
    country VARCHAR(2) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    official_website VARCHAR(600),
    investor_relations_url VARCHAR(600),
    press_release_url VARCHAR(600),
    annual_reports_url VARCHAR(600),
    exchange_announcements_url VARCHAR(600),
    regulatory_filings_url VARCHAR(600)
);

CREATE TABLE research_documents (
    document_id UUID PRIMARY KEY,
    canonical_url VARCHAR(1000) NOT NULL,
    original_url VARCHAR(1000) NOT NULL,
    title VARCHAR(500),
    source_type VARCHAR(60) NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    publisher VARCHAR(240),
    published_at TIMESTAMP,
    retrieved_at TIMESTAMP NOT NULL,
    language VARCHAR(16),
    content_type VARCHAR(120) NOT NULL,
    document_type VARCHAR(40) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    instrument_id UUID,
    company_id UUID,
    country VARCHAR(2),
    exchange VARCHAR(40),
    status VARCHAR(40) NOT NULL,
    reliability_level VARCHAR(20) NOT NULL,
    entity_resolution_confidence DECIMAL(5, 4) NOT NULL
);

CREATE TABLE research_document_sources (
    document_source_id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    source_id VARCHAR(120),
    source_url VARCHAR(1000) NOT NULL,
    discovered_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_research_document_sources_document FOREIGN KEY (document_id) REFERENCES research_documents (document_id)
);

CREATE TABLE research_events (
    event_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    company_id UUID NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    event_date TIMESTAMP,
    detected_at TIMESTAMP NOT NULL,
    title VARCHAR(300) NOT NULL,
    summary VARCHAR(1200) NOT NULL,
    source_document_id UUID NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    source_type VARCHAR(60) NOT NULL,
    reliability VARCHAR(20) NOT NULL,
    confidence DECIMAL(5, 4) NOT NULL,
    impact VARCHAR(40) NOT NULL,
    time_horizon VARCHAR(40) NOT NULL,
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
    status VARCHAR(40) NOT NULL,
    CONSTRAINT fk_research_events_document FOREIGN KEY (source_document_id) REFERENCES research_documents (document_id)
);

CREATE TABLE research_event_evidence (
    evidence_id UUID PRIMARY KEY,
    event_id UUID NOT NULL,
    source_document_id UUID NOT NULL,
    evidence_reference VARCHAR(600) NOT NULL,
    CONSTRAINT fk_research_event_evidence_event FOREIGN KEY (event_id) REFERENCES research_events (event_id)
);

CREATE TABLE research_fetch_history (
    fetch_id UUID PRIMARY KEY,
    source_id VARCHAR(120),
    url VARCHAR(1000) NOT NULL,
    attempted_at TIMESTAMP NOT NULL,
    status VARCHAR(40) NOT NULL,
    http_status INTEGER,
    failure_code VARCHAR(120),
    bytes_read BIGINT,
    latency_ms BIGINT
);

CREATE INDEX idx_research_documents_instrument_id ON research_documents (instrument_id);
CREATE INDEX idx_research_documents_company_id ON research_documents (company_id);
CREATE INDEX idx_research_documents_published_at ON research_documents (published_at);
CREATE UNIQUE INDEX idx_research_documents_canonical_url ON research_documents (canonical_url);
CREATE INDEX idx_research_documents_content_hash ON research_documents (content_hash);
CREATE INDEX idx_research_documents_source_type ON research_documents (source_type);
CREATE INDEX idx_research_documents_status ON research_documents (status);
CREATE INDEX idx_research_events_instrument_id ON research_events (instrument_id);
CREATE INDEX idx_research_events_company_id ON research_events (company_id);
CREATE INDEX idx_research_events_event_type ON research_events (event_type);
CREATE INDEX idx_research_events_event_date ON research_events (event_date);
CREATE INDEX idx_research_events_impact ON research_events (impact);
