-- How the original is stored in the archive: identity (as uploaded) or gzip (lossless, to fit storage limits).
-- The sha256 column always refers to the ORIGINAL uploaded bytes.
ALTER TABLE margin.uploads ADD COLUMN IF NOT EXISTS archive_encoding TEXT NOT NULL DEFAULT 'identity';
INSERT INTO margin.schema_migrations (version) VALUES ('004_archive_encoding') ON CONFLICT DO NOTHING;
