-- Acquisition audit metadata is deliberately separate from financial/event evidence.
CREATE TABLE research_acquisition_observations (
    instrument_id UUID NOT NULL,
    requirement_id VARCHAR(100) NOT NULL,
    provider VARCHAR(100) NOT NULL,
    outcome VARCHAR(40) NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_url TEXT,
    failure_reason TEXT,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_id, requirement_id, provider)
);
