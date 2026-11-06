ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('003_must_change_password');
