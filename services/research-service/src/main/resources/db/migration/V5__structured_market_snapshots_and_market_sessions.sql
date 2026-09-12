CREATE TABLE global_structured_market_snapshots (
    instrument_id UUID NOT NULL, provider VARCHAR(80) NOT NULL, provider_instrument_id VARCHAR(160), exchange VARCHAR(40), mic VARCHAR(12), currency VARCHAR(8), quote_type VARCHAR(40),
    source_url VARCHAR(1000), source_name VARCHAR(240), source_type VARCHAR(80), source_identity VARCHAR(300), market_as_of TIMESTAMP, retrieved_at TIMESTAMP NOT NULL, persisted_at TIMESTAMP NOT NULL,
    last_price_at TIMESTAMP, last_valuation_at TIMESTAMP, last_fundamentals_at TIMESTAMP, last_analyst_at TIMESTAMP, last_success_at TIMESTAMP, last_provider_attempt_at TIMESTAMP,
    acquisition_status VARCHAR(80) NOT NULL, last_failure_code VARCHAR(160), last_failure_message VARCHAR(500), facts_json JSONB NOT NULL,
    PRIMARY KEY (instrument_id, provider)
);
CREATE INDEX idx_structured_market_instrument ON global_structured_market_snapshots (instrument_id);

CREATE TABLE market_trading_schedules (
    id BIGSERIAL PRIMARY KEY, market_code VARCHAR(40) NOT NULL, mic VARCHAR(12), country_code VARCHAR(2), timezone VARCHAR(80) NOT NULL, trading_day SMALLINT NOT NULL,
    regular_open_time TIME NOT NULL, regular_close_time TIME NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE, provenance VARCHAR(500), created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (market_code, trading_day, regular_open_time, regular_close_time)
);
CREATE INDEX idx_market_trading_schedules_lookup ON market_trading_schedules (market_code, mic, enabled);

CREATE TABLE market_trading_calendar_exceptions (
    id BIGSERIAL PRIMARY KEY, market_code VARCHAR(40) NOT NULL, trading_date DATE NOT NULL, exception_type VARCHAR(40) NOT NULL, open_time TIME, close_time TIME,
    reason VARCHAR(500), source_reference VARCHAR(1000), created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (market_code, trading_date)
);
CREATE INDEX idx_market_calendar_exceptions_lookup ON market_trading_calendar_exceptions (market_code, trading_date);

INSERT INTO market_trading_schedules (market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time,enabled,provenance)
VALUES
 ('NSE','XNSE','IN','Asia/Kolkata',0,'09:15','15:30',TRUE,'NSE regular session'),
 ('NSE','XNSE','IN','Asia/Kolkata',1,'09:15','15:30',TRUE,'NSE regular session'),
 ('NSE','XNSE','IN','Asia/Kolkata',2,'09:15','15:30',TRUE,'NSE regular session'),
 ('NSE','XNSE','IN','Asia/Kolkata',3,'09:15','15:30',TRUE,'NSE regular session'),
 ('NSE','XNSE','IN','Asia/Kolkata',4,'09:15','15:30',TRUE,'NSE regular session')
ON CONFLICT DO NOTHING;
