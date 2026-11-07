ALTER TABLE uploads ADD COLUMN source_ref TEXT;
CREATE INDEX IF NOT EXISTS ix_uploads_source_ref ON uploads (trade_date, source_ref);
CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, requested_by TEXT NOT NULL, segments TEXT NOT NULL,
    trade_date TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    github_run_id TEXT, message TEXT, summary TEXT);
CREATE INDEX IF NOT EXISTS ix_fetch_runs_status ON fetch_runs (status, id);
CREATE TABLE IF NOT EXISTS fetch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES fetch_runs(id), source_ref TEXT NOT NULL,
    segment TEXT NOT NULL, file_type TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, upload_id INTEGER,
    updated_at TEXT NOT NULL, UNIQUE (run_id, source_ref));
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('005_fetcher');
