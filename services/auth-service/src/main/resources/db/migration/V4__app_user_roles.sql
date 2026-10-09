CREATE TABLE app_user_roles (
    user_id UUID NOT NULL REFERENCES app_users(id),
    role VARCHAR(40) NOT NULL,
    granted_at TIMESTAMP NOT NULL,
    PRIMARY KEY (user_id, role)
);

CREATE INDEX idx_app_user_roles_user ON app_user_roles (user_id);

-- Backward-compatible backfill: every account that exists today defaults to USER.
INSERT INTO app_user_roles (user_id, role, granted_at)
SELECT id, 'USER', created_at FROM app_users;

CREATE TABLE app_user_role_audit (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_users(id),
    target_email VARCHAR(320) NOT NULL,
    role VARCHAR(40) NOT NULL,
    action VARCHAR(20) NOT NULL,
    operator VARCHAR(240) NOT NULL,
    reason VARCHAR(500),
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_app_user_role_audit_user ON app_user_role_audit (user_id);
