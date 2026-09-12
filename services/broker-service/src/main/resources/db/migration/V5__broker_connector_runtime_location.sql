ALTER TABLE broker_connector_instances ADD COLUMN runtime_endpoint VARCHAR(500);
ALTER TABLE broker_connector_instances ADD COLUMN runtime_identity VARCHAR(200);
ALTER TABLE broker_connector_instances ADD COLUMN runtime_provider VARCHAR(40);
ALTER TABLE broker_connector_instances ADD COLUMN runtime_created_at TIMESTAMP;
ALTER TABLE broker_connector_instances ADD COLUMN runtime_stopped_at TIMESTAMP;
