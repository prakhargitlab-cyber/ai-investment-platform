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

CREATE INDEX idx_app_users_email ON app_users (email);
