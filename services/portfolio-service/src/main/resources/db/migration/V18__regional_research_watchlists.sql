CREATE TABLE watchlists (
    watchlist_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    name VARCHAR(80) NOT NULL,
    region VARCHAR(16) NOT NULL,
    system_default BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_watchlists_app_user FOREIGN KEY (user_id) REFERENCES app_users (id),
    CONSTRAINT ck_watchlists_region CHECK (region IN ('INDIA', 'EUROPE', 'USA')),
    CONSTRAINT ux_watchlists_user_region_default UNIQUE (user_id, region, system_default)
);

CREATE TABLE watchlist_memberships (
    membership_id UUID PRIMARY KEY,
    watchlist_id UUID NOT NULL,
    global_instrument_id UUID NOT NULL,
    source_period VARCHAR(16),
    source_performance_pct DECIMAL(18, 8),
    added_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_watchlist_membership_watchlist FOREIGN KEY (watchlist_id) REFERENCES watchlists (watchlist_id) ON DELETE CASCADE,
    CONSTRAINT fk_watchlist_membership_instrument FOREIGN KEY (global_instrument_id) REFERENCES instrument_master (instrument_id),
    CONSTRAINT ck_watchlist_membership_period CHECK (source_period IS NULL OR source_period IN ('DAY', 'WEEK', 'MONTH', 'YEAR')),
    CONSTRAINT ux_watchlist_membership_instrument UNIQUE (watchlist_id, global_instrument_id)
);

CREATE INDEX idx_watchlists_user_id ON watchlists (user_id);
CREATE INDEX idx_watchlist_memberships_watchlist_id ON watchlist_memberships (watchlist_id);
CREATE INDEX idx_watchlist_memberships_global_instrument_id ON watchlist_memberships (global_instrument_id);
