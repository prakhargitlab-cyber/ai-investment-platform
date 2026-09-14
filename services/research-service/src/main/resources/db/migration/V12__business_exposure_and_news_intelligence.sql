-- Additive evidence history only. No acquisition or backfill occurs here.
CREATE TABLE company_business_exposure_profiles (
    profile_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    profile_version VARCHAR(80) NOT NULL,
    evidence_fingerprint VARCHAR(64) NOT NULL,
    public_available_at TIMESTAMPTZ NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    confidence NUMERIC NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    payload JSONB NOT NULL,
    UNIQUE (instrument_id, profile_version, evidence_fingerprint),
    UNIQUE (profile_id, instrument_id),
    CHECK (computed_at >= public_available_at AND computed_at >= retrieved_at)
);
CREATE INDEX ix_exposure_profile_available ON company_business_exposure_profiles (instrument_id, public_available_at, computed_at);

CREATE TABLE research_news_search_runs (
    run_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    query_plan_version VARCHAR(80) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL CHECK (completed_at >= started_at),
    outcome VARCHAR(40) NOT NULL CHECK (outcome IN ('SEARCH_COMPLETE_WITH_EVENTS','SEARCH_COMPLETE_NO_EVENTS','SEARCH_PARTIAL','SEARCH_FAILED')),
    coverage NUMERIC NOT NULL CHECK (coverage BETWEEN 0 AND 1),
    qualifying_events INTEGER NOT NULL CHECK (qualifying_events >= 0),
    payload JSONB NOT NULL
);
CREATE INDEX ix_news_search_completed ON research_news_search_runs (instrument_id, completed_at);

CREATE TABLE research_event_impact_features (
    feature_id UUID PRIMARY KEY,
    instrument_id UUID NOT NULL,
    event_key VARCHAR(64) NOT NULL,
    feature_version VARCHAR(80) NOT NULL,
    evidence_fingerprint VARCHAR(64) NOT NULL,
    profile_id UUID NOT NULL,
    source_document_id UUID NOT NULL REFERENCES research_documents(document_id),
    source_event_id UUID REFERENCES research_events(event_id),
    event_type VARCHAR(80) NOT NULL,
    exposure_key VARCHAR(80),
    direction SMALLINT NOT NULL CHECK (direction IN (-1,0,1)),
    magnitude NUMERIC NOT NULL CHECK (magnitude BETWEEN 0 AND 1),
    impact_score NUMERIC NOT NULL CHECK (impact_score BETWEEN -100 AND 100),
    relevance NUMERIC NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    source_confidence NUMERIC NOT NULL CHECK (source_confidence BETWEEN 0 AND 1),
    event_confidence NUMERIC NOT NULL CHECK (event_confidence BETWEEN 0 AND 1),
    source_tier VARCHAR(40) NOT NULL,
    relevance_type VARCHAR(40) NOT NULL CHECK (relevance_type IN ('DIRECT_COMPANY','SECTOR_EXPOSURE')),
    publication_time TIMESTAMPTZ,
    public_available_at TIMESTAMPTZ NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    short_term BOOLEAN NOT NULL,
    medium_term BOOLEAN NOT NULL,
    long_term BOOLEAN NOT NULL,
    payload JSONB NOT NULL,
    FOREIGN KEY (profile_id, instrument_id) REFERENCES company_business_exposure_profiles(profile_id, instrument_id),
    UNIQUE (instrument_id, event_key, feature_version, evidence_fingerprint),
    CHECK (valid_until IS NULL OR valid_until >= public_available_at),
    CHECK (computed_at >= public_available_at AND computed_at >= discovered_at),
    CHECK (publication_time IS NULL OR publication_time <= public_available_at)
);
CREATE INDEX ix_news_features_available ON research_event_impact_features (instrument_id, public_available_at, computed_at);
CREATE INDEX ix_news_features_exposure ON research_event_impact_features (exposure_key, event_type, public_available_at);

CREATE FUNCTION reject_news_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'NEWS_HISTORY_IS_IMMUTABLE';
END;
$$;
CREATE TRIGGER immutable_exposure_profiles BEFORE UPDATE OR DELETE ON company_business_exposure_profiles
    FOR EACH ROW EXECUTE FUNCTION reject_news_history_mutation();
CREATE TRIGGER immutable_news_search_runs BEFORE UPDATE OR DELETE ON research_news_search_runs
    FOR EACH ROW EXECUTE FUNCTION reject_news_history_mutation();
CREATE TRIGGER immutable_event_impact_features BEFORE UPDATE OR DELETE ON research_event_impact_features
    FOR EACH ROW EXECUTE FUNCTION reject_news_history_mutation();
