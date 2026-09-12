CREATE TABLE broker_connector_instances (
    connector_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    broker_type VARCHAR(40) NOT NULL,
    runtime_mode VARCHAR(40) NOT NULL,
    runtime_status VARCHAR(40) NOT NULL,
    auth_status VARCHAR(40) NOT NULL,
    login_url VARCHAR(500),
    last_heartbeat_at TIMESTAMP,
    last_authenticated_at TIMESTAMP,
    idle_timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    session_timeout_seconds INTEGER NOT NULL DEFAULT 86400,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT fk_broker_connector_instances_app_user FOREIGN KEY (user_id) REFERENCES app_users (id)
);

CREATE INDEX idx_broker_connector_instances_user_id ON broker_connector_instances (user_id);
CREATE INDEX idx_broker_connector_instances_broker_type ON broker_connector_instances (broker_type);
CREATE INDEX idx_broker_connector_instances_runtime_status ON broker_connector_instances (runtime_status);

ALTER TABLE broker_connections ADD COLUMN connector_id UUID;
ALTER TABLE broker_connections
    ADD CONSTRAINT fk_broker_connections_connector_instance
    FOREIGN KEY (connector_id) REFERENCES broker_connector_instances (connector_id);

CREATE INDEX idx_broker_connections_connector_id ON broker_connections (connector_id);
