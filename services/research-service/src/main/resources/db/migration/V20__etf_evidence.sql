-- ETF-only append-only evidence. No stock tables are altered.
-- Payloads preserve exact decimal strings, optional values and full provenance.

CREATE TABLE etf_fact_observations (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, metric TEXT NOT NULL,
    as_of_date TEXT, provider TEXT NOT NULL, source_identity TEXT NOT NULL,
    authority TEXT NOT NULL, published_at TEXT, retrieved_at TEXT NOT NULL,
    source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX idx_etf_facts_lookup ON etf_fact_observations (instrument_id, metric, as_of_date);
CREATE TABLE etf_nav_observations (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, nav TEXT NOT NULL,
    currency TEXT NOT NULL, nav_date TEXT NOT NULL, provider TEXT NOT NULL,
    source_identity TEXT NOT NULL, authority TEXT NOT NULL, published_at TEXT,
    retrieved_at TEXT NOT NULL, source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX idx_etf_nav_lookup ON etf_nav_observations (instrument_id, nav_date);
CREATE TABLE etf_holdings_snapshots (
    evidence_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, as_of_date TEXT NOT NULL,
    provider TEXT NOT NULL, source_identity TEXT NOT NULL, authority TEXT NOT NULL,
    published_at TEXT, retrieved_at TEXT NOT NULL, source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX idx_etf_holdings_lookup ON etf_holdings_snapshots (instrument_id, as_of_date);
CREATE TABLE etf_holdings_positions (
    snapshot_id TEXT NOT NULL REFERENCES etf_holdings_snapshots(evidence_id),
    position_number INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, position_number)
);
CREATE TABLE etf_listing_observations (
    evidence_id TEXT PRIMARY KEY, exchange TEXT NOT NULL CHECK(exchange = 'NSE'),
    isin TEXT NOT NULL, symbol TEXT NOT NULL, instrument_id TEXT,
    asset_type TEXT NOT NULL CHECK(asset_type = 'ETF'), retrieved_at TEXT NOT NULL,
    source_mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX idx_etf_listing_identity ON etf_listing_observations (exchange, isin);
CREATE TABLE etf_acquisition_attempts (
    attempt_id TEXT PRIMARY KEY, instrument_id TEXT, metric TEXT NOT NULL,
    provider TEXT NOT NULL, attempted_at TEXT NOT NULL, outcome TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX idx_etf_attempt_lookup ON etf_acquisition_attempts (instrument_id, metric, attempted_at);
