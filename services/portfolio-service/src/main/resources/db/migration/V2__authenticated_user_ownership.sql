CREATE TABLE app_users (
    id UUID PRIMARY KEY,
    issuer VARCHAR(240) NOT NULL,
    external_subject VARCHAR(240) NOT NULL,
    email VARCHAR(320),
    display_name VARCHAR(240),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT ux_app_users_issuer_subject UNIQUE (issuer, external_subject)
);

INSERT INTO app_users (id, issuer, external_subject, email, display_name, created_at, updated_at)
SELECT DISTINCT user_id, 'legacy-dev', user_id::VARCHAR, NULL, 'Legacy DEV User', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
FROM portfolios
ON CONFLICT DO NOTHING;

INSERT INTO app_users (id, issuer, external_subject, email, display_name, created_at, updated_at)
SELECT DISTINCT user_id, 'legacy-dev', user_id::VARCHAR, NULL, 'Legacy DEV User', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
FROM broker_accounts
ON CONFLICT DO NOTHING;

ALTER TABLE portfolios
    ADD CONSTRAINT fk_portfolios_app_user FOREIGN KEY (user_id) REFERENCES app_users (id);

ALTER TABLE broker_accounts
    ADD CONSTRAINT fk_broker_accounts_app_user FOREIGN KEY (user_id) REFERENCES app_users (id);
