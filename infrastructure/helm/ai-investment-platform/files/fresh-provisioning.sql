-- Executed by the official postgres entrypoint ONLY for an empty PGDATA.
-- Kept in postgres, outside the application database and its logical backups.
\connect postgres
CREATE SCHEMA platform_bootstrap;
CREATE TABLE platform_bootstrap.provisioning (
    database_name text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    consumed_at timestamptz
);
\getenv application_database POSTGRES_DB
INSERT INTO platform_bootstrap.provisioning(database_name) VALUES (:'application_database');
REVOKE ALL ON SCHEMA platform_bootstrap FROM PUBLIC;
REVOKE ALL ON platform_bootstrap.provisioning FROM PUBLIC;
