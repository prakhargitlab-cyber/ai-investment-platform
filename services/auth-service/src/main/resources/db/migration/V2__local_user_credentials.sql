ALTER TABLE app_users
    ADD COLUMN normalized_email VARCHAR(320) NULL;

ALTER TABLE app_users
    ADD COLUMN password_hash VARCHAR(255) NULL;

ALTER TABLE app_users
    ADD COLUMN account_status VARCHAR(40) NULL;

ALTER TABLE app_users
    ADD COLUMN email_verified_at TIMESTAMP NULL;

ALTER TABLE app_users
    ADD COLUMN last_login_at TIMESTAMP NULL;

CREATE UNIQUE INDEX ux_app_users_normalized_email
    ON app_users (normalized_email);

CREATE TABLE email_verification_tokens (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_users(id),
    token_hash VARCHAR(128) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT ux_email_verification_tokens_hash UNIQUE (token_hash)
);

CREATE INDEX idx_email_verification_tokens_user ON email_verification_tokens (user_id);
