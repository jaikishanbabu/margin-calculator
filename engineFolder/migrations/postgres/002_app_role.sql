-- Restricted application role used by the running engine (DATABASE_URL).
-- It owns nothing, has no DDL rights, and can only append to / read the audit log.
-- Its password is NOT set here: set it once from the Supabase SQL editor with
--     ALTER ROLE margin_app WITH LOGIN PASSWORD '<generated>';
-- and store the resulting connection string only in Render's environment settings.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'margin_app') THEN
        CREATE ROLE margin_app NOLOGIN NOINHERIT NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

ALTER ROLE margin_app SET search_path = margin;
ALTER ROLE margin_app SET statement_timeout = '120s';

GRANT USAGE ON SCHEMA margin TO margin_app;

GRANT SELECT, INSERT, UPDATE         ON margin.users        TO margin_app;
GRANT SELECT, INSERT                 ON margin.audit_log    TO margin_app;   -- no UPDATE / DELETE / TRUNCATE
GRANT SELECT, INSERT, UPDATE         ON margin.uploads      TO margin_app;   -- no DELETE / TRUNCATE
GRANT SELECT, INSERT, UPDATE         ON margin.backup_runs  TO margin_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON margin.settings     TO margin_app;
GRANT SELECT                         ON margin.schema_migrations TO margin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA margin TO margin_app;

-- Supabase API roles get nothing in this schema.
REVOKE ALL ON ALL TABLES IN SCHEMA margin FROM anon, authenticated, service_role;
REVOKE ALL ON SCHEMA margin FROM anon, authenticated, service_role;

INSERT INTO margin.schema_migrations (version) VALUES ('002_app_role') ON CONFLICT DO NOTHING;
