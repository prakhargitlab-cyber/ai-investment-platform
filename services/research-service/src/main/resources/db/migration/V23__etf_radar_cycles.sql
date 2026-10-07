-- Durable ETF Radar cycle persistence.
-- Moved out of V20 because applied Flyway migrations are immutable.

CREATE TABLE etf_radar_cycles (
    cycle_id TEXT PRIMARY KEY,
    radar_version TEXT NOT NULL,
    correlation_id TEXT,
    as_of TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_etf_radar_cycles_as_of
    ON etf_radar_cycles (as_of);
