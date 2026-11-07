-- Stage 2B: exchange file fetcher runs and per-file progress.
ALTER TABLE margin.uploads ADD COLUMN IF NOT EXISTS source_ref TEXT;   -- fetcher source/variant, e.g. nse.var:batch=3
CREATE INDEX IF NOT EXISTS ix_uploads_source_ref ON margin.uploads (trade_date, source_ref);

CREATE TABLE IF NOT EXISTS margin.fetch_runs (
    id            BIGSERIAL PRIMARY KEY,
    trigger       TEXT NOT NULL,              -- auto | manual
    requested_by  TEXT NOT NULL,
    segments      TEXT NOT NULL,              -- JSON list, e.g. ["NFO"] or ["ALL"]
    trade_date    TEXT NOT NULL,              -- YYYYMMDD
    status        TEXT NOT NULL,              -- queued | running | done | failed | skipped
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    github_run_id TEXT,
    message       TEXT,
    summary       TEXT                        -- JSON counts per status
);
CREATE INDEX IF NOT EXISTS ix_fetch_runs_status ON margin.fetch_runs (status, id);

CREATE TABLE IF NOT EXISTS margin.fetch_items (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES margin.fetch_runs(id),
    source_ref  TEXT NOT NULL,
    segment     TEXT NOT NULL,
    file_type   TEXT NOT NULL,
    status      TEXT NOT NULL,                -- queued | running | archived | have | pending | blocked | failed | not_due
    detail      TEXT,
    upload_id   BIGINT,
    updated_at  TEXT NOT NULL,
    UNIQUE (run_id, source_ref)
);

GRANT SELECT, INSERT, UPDATE ON margin.fetch_runs, margin.fetch_items TO margin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA margin TO margin_app;
REVOKE ALL ON margin.fetch_runs, margin.fetch_items FROM anon, authenticated, service_role;

INSERT INTO margin.schema_migrations (version) VALUES ('005_fetcher') ON CONFLICT DO NOTHING;
