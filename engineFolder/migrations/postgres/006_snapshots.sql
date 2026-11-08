-- Stage 2C: parsed per-segment databases ("snapshots") built on GitHub from the archived files.
CREATE TABLE IF NOT EXISTS margin.snapshots (
    id            BIGSERIAL PRIMARY KEY,
    segment       TEXT NOT NULL,
    basis         TEXT NOT NULL,          -- bod | latest
    status        TEXT NOT NULL,          -- ready | failed | expired
    error_kind    TEXT,
    trade_date    TEXT,
    span_day      TEXT, span_cycle TEXT, span_created TEXT,
    fingerprint   TEXT NOT NULL,
    inputs        TEXT NOT NULL,
    info          TEXT,
    rules         TEXT,
    code_version  TEXT,
    archive_key   TEXT, sha256 TEXT, size_bytes BIGINT, raw_bytes BIGINT,
    run_id        BIGINT,
    built_by      TEXT NOT NULL,
    built_at      TEXT NOT NULL,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS ix_snapshots_seg ON margin.snapshots (segment, basis, status, id);

CREATE OR REPLACE FUNCTION margin.snapshots_no_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'snapshots: DELETE is not allowed'; END $$;
DROP TRIGGER IF EXISTS snapshots_no_delete ON margin.snapshots;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE OR TRUNCATE ON margin.snapshots
    FOR EACH STATEMENT EXECUTE FUNCTION margin.snapshots_no_delete();

GRANT SELECT, INSERT, UPDATE ON margin.snapshots TO margin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA margin TO margin_app;
REVOKE ALL ON margin.snapshots FROM anon, authenticated, service_role;

INSERT INTO margin.schema_migrations (version) VALUES ('006_snapshots') ON CONFLICT DO NOTHING;
