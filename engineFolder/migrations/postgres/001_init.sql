-- Stage 1 schema (Postgres / Supabase).
-- Applied by the schema owner (postgres) via the Supabase SQL editor, the Supabase MCP,
-- or the manual "db-migrate" GitHub Action. The running app NEVER has DDL rights.
--
-- Everything lives in schema "margin", which is NOT exposed through Supabase's REST API
-- (PostgREST only exposes "public" and "graphql_public" by default), so the Supabase
-- anon / authenticated / service keys cannot read or change these tables.

CREATE SCHEMA IF NOT EXISTS margin;
REVOKE ALL ON SCHEMA margin FROM PUBLIC;

CREATE TABLE IF NOT EXISTS margin.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS margin.users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    full_name       TEXT,
    pw_hash         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('superadmin', 'admin')),
    active          INTEGER NOT NULL DEFAULT 1,
    token_version   INTEGER NOT NULL DEFAULT 1,
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    last_login_at   TEXT
);

-- Append-only, hash-chained audit trail.
CREATE TABLE IF NOT EXISTS margin.audit_log (
    id          BIGSERIAL PRIMARY KEY,
    seq         BIGINT UNIQUE NOT NULL,
    ts          TEXT NOT NULL,
    username    TEXT,
    ip          TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    prev_hash   TEXT NOT NULL,
    row_hash    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS margin.uploads (
    id                  BIGSERIAL PRIMARY KEY,
    segment             TEXT NOT NULL,
    file_type           TEXT NOT NULL,
    trade_date          TEXT NOT NULL,      -- effective trading date, YYYYMMDD
    trade_date_source   TEXT NOT NULL,      -- file | admin
    cycle               TEXT,               -- SPAN cycle label exactly as recorded
    cycle_source        TEXT,               -- file | admin
    source_timestamp    TEXT,               -- creation timestamp read from inside the file
    source_meta         TEXT,               -- JSON: every provenance field read from the file
    filename            TEXT NOT NULL,
    content_kind        TEXT NOT NULL,      -- zip | gzip | xml | text | xlsx | json
    size_bytes          BIGINT NOT NULL,
    sha256              TEXT NOT NULL,
    inner_manifest      TEXT,               -- JSON: members of an archive with their sha256
    archive_backend     TEXT NOT NULL,
    archive_key         TEXT NOT NULL,
    archive_verified_at TEXT,
    manifest_key        TEXT,
    parser_versions     TEXT NOT NULL,      -- JSON
    app_version         TEXT NOT NULL,
    uploaded_by         TEXT NOT NULL,
    uploaded_at         TEXT NOT NULL,
    uploader_ip         TEXT,
    status              TEXT NOT NULL,      -- archived | failed | superseded | deleted
    message             TEXT
);
CREATE INDEX IF NOT EXISTS ix_uploads_lookup ON margin.uploads (segment, file_type, trade_date, status);
CREATE INDEX IF NOT EXISTS ix_uploads_sha ON margin.uploads (sha256);

CREATE TABLE IF NOT EXISTS margin.backup_runs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,          -- backup | restore_test
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,          -- running | ok | failed
    backend         TEXT,
    location        TEXT,
    sha256          TEXT,
    details         TEXT,                   -- JSON
    triggered_by    TEXT
);

CREATE TABLE IF NOT EXISTS margin.settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- ---------- tamper resistance ----------
CREATE OR REPLACE FUNCTION margin.block_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'margin.%: % is not allowed (append-only table)', TG_TABLE_NAME, TG_OP;
END $$;

DROP TRIGGER IF EXISTS audit_no_update ON margin.audit_log;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON margin.audit_log
    FOR EACH ROW EXECUTE FUNCTION margin.block_change();
DROP TRIGGER IF EXISTS audit_no_truncate ON margin.audit_log;
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON margin.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION margin.block_change();

-- Upload records: status/message may change, provenance may not; rows are never deleted.
CREATE OR REPLACE FUNCTION margin.guard_upload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'margin.uploads: DELETE is not allowed (mark status=deleted instead)';
    END IF;
    IF NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by
       OR NEW.uploaded_at IS DISTINCT FROM OLD.uploaded_at
       OR NEW.filename IS DISTINCT FROM OLD.filename
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
       OR NEW.trade_date IS DISTINCT FROM OLD.trade_date
       OR NEW.cycle IS DISTINCT FROM OLD.cycle
       OR NEW.source_meta IS DISTINCT FROM OLD.source_meta
       OR NEW.archive_key IS DISTINCT FROM OLD.archive_key THEN
        RAISE EXCEPTION 'margin.uploads: provenance columns are immutable';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS uploads_guard ON margin.uploads;
CREATE TRIGGER uploads_guard BEFORE UPDATE OR DELETE ON margin.uploads
    FOR EACH ROW EXECUTE FUNCTION margin.guard_upload();
DROP TRIGGER IF EXISTS uploads_no_truncate ON margin.uploads;
CREATE TRIGGER uploads_no_truncate BEFORE TRUNCATE ON margin.uploads
    FOR EACH STATEMENT EXECUTE FUNCTION margin.block_change();

INSERT INTO margin.schema_migrations (version) VALUES ('001_init') ON CONFLICT DO NOTHING;
