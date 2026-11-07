ALTER TABLE uploads ADD COLUMN archive_encoding TEXT NOT NULL DEFAULT 'identity';
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('004_archive_encoding');
