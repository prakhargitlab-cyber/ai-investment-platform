-- Global (non-instrument-scoped) macro observations: RBI policy repo rate,
-- India CPI inflation. Deliberately its own table, separate from
-- global_financial_facts (per-instrument) -- one row per indicator/region/
-- period, shared and reused across the whole instrument universe rather than
-- duplicated per instrument.
CREATE TABLE macro_observations (
    indicator VARCHAR(64) NOT NULL,
    region VARCHAR(16) NOT NULL,
    period VARCHAR(64) NOT NULL,
    actual_value NUMERIC(28, 8) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    effective_at TIMESTAMP WITH TIME ZONE,
    release_at TIMESTAMP WITH TIME ZONE,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source VARCHAR(240) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    provider VARCHAR(80) NOT NULL,
    provenance VARCHAR(40) NOT NULL,
    -- Forward-compatible only: remain NULL until a trustworthy provider
    -- actually supplies them. Nothing acquired in this iteration populates them.
    previous_value NUMERIC(28, 8),
    expected_value NUMERIC(28, 8),
    surprise NUMERIC(28, 8),
    CONSTRAINT pk_macro_observations PRIMARY KEY (indicator, region, period)
);

CREATE INDEX idx_macro_observations_indicator_region
    ON macro_observations (indicator, region, observed_at);
