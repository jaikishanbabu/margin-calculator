-- Bootstrap / temporary passwords must be changed at first login.
ALTER TABLE margin.users ADD COLUMN IF NOT EXISTS must_change_password INTEGER NOT NULL DEFAULT 0;
INSERT INTO margin.schema_migrations (version) VALUES ('003_must_change_password') ON CONFLICT DO NOTHING;
