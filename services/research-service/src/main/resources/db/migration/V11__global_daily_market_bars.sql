-- Dedicated daily OHLCV evidence. No migration or dual-write from close-only prices.
-- Provider symbols and retrieval times never participate in canonical identity.
CREATE TABLE global_daily_market_bars (
    global_instrument_id UUID NOT NULL,
    trading_date DATE NOT NULL,
    open_price NUMERIC(38, 12),
    high_price NUMERIC(38, 12),
    low_price NUMERIC(38, 12),
    close_price NUMERIC(38, 12),
    previous_close NUMERIC(38, 12),
    volume BIGINT,
    turnover NUMERIC(38, 12),
    currency VARCHAR(16) NOT NULL,
    provider VARCHAR(120) NOT NULL,
    provider_symbol VARCHAR(240),
    source_mode VARCHAR(16) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    retrieved_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_global_daily_market_bars PRIMARY KEY (global_instrument_id, trading_date, provider),
    CONSTRAINT ck_daily_bar_open CHECK (open_price > 0 AND open_price <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_high CHECK (high_price > 0 AND high_price <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_low CHECK (low_price > 0 AND low_price <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_close CHECK (close_price > 0 AND close_price <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_previous_close CHECK (previous_close > 0 AND previous_close <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_range CHECK (high_price >= low_price),
    CONSTRAINT ck_daily_bar_volume CHECK (volume >= 0),
    CONSTRAINT ck_daily_bar_turnover CHECK (turnover >= 0 AND turnover <= 99999999999999999999999999.999999999999),
    CONSTRAINT ck_daily_bar_currency CHECK (LENGTH(TRIM(currency)) > 0),
    CONSTRAINT ck_daily_bar_provider CHECK (LENGTH(TRIM(provider)) > 0),
    CONSTRAINT ck_daily_bar_source_mode CHECK (source_mode IN ('REAL', 'DEMO')),
    CONSTRAINT ck_daily_bar_source_url CHECK (LENGTH(TRIM(source_url)) > 0)
);

-- The primary-key index covers instrument/date ranges and provider/day upserts.
-- Date-first lookup covers recent bars across an instrument batch.
CREATE INDEX idx_daily_market_bars_date_instrument
    ON global_daily_market_bars (trading_date, global_instrument_id);
