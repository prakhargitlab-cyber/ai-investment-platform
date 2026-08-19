CREATE TABLE broker_connections (
    connection_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    broker_type VARCHAR(40) NOT NULL,
    external_account_reference VARCHAR(160),
    display_name VARCHAR(160) NOT NULL,
    status VARCHAR(40) NOT NULL,
    connected_at TIMESTAMP,
    last_successful_sync_at TIMESTAMP,
    last_sync_attempt_at TIMESTAMP,
    last_error_code VARCHAR(80),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_broker_connections_user_id ON broker_connections (user_id);
CREATE INDEX idx_broker_connections_broker_type ON broker_connections (broker_type);
CREATE INDEX idx_broker_connections_status ON broker_connections (status);
