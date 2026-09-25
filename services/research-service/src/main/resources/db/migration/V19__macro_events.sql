-- Global (non-instrument-scoped) macro event calendar: FOMC and RBI MPC
-- meeting dates. Deliberately separate from macro_observations (V18) --
-- an event has no actual_value; it is a calendar row, not a released
-- observation.
CREATE TABLE macro_events (
    id VARCHAR(160) NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    indicator VARCHAR(64) NOT NULL,
    region VARCHAR(16) NOT NULL,
    -- Calendar date only -- no official source publishes a clock time for
    -- "the meeting", and none is fabricated here.
    scheduled_at DATE NOT NULL,
    period VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED',
    source VARCHAR(240) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    provider VARCHAR(80) NOT NULL,
    provenance VARCHAR(40) NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_macro_events PRIMARY KEY (id),
    CONSTRAINT ck_macro_events_status CHECK (status IN ('SCHEDULED', 'COMPLETED', 'CANCELLED'))
);

CREATE INDEX idx_macro_events_region_scheduled ON macro_events (region, scheduled_at);
CREATE INDEX idx_macro_events_indicator_scheduled ON macro_events (indicator, scheduled_at);
