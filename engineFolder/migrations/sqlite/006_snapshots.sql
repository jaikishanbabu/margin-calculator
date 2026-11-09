-- Stage 2C: parsed per-segment databases ("snapshots") built on GitHub from the archived files.
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment TEXT NOT NULL,
    basis TEXT NOT NULL,                 -- bod (start-of-day SPAN) | latest (newest cycle)
    status TEXT NOT NULL,                -- ready | failed | expired
    error_kind TEXT,                     -- missing_inputs | check_failed | error
    trade_date TEXT,                     -- YYYYMMDD the data is for
    span_day TEXT, span_cycle TEXT, span_created TEXT,
    fingerprint TEXT NOT NULL,           -- inputs + rules + loader version + day
    inputs TEXT NOT NULL,                -- JSON [{upload_id, filename, sha256, trade_date, file_type}]
    info TEXT,                           -- JSON load_info from the loader
    rules TEXT,                          -- JSON {file: sha256}
    code_version TEXT,                   -- Git commit the job ran
    archive_key TEXT, sha256 TEXT, size_bytes INTEGER, raw_bytes INTEGER,
    run_id INTEGER, built_by TEXT NOT NULL, built_at TEXT NOT NULL, message TEXT
);
CREATE INDEX IF NOT EXISTS ix_snapshots_seg ON snapshots (segment, basis, status, id);
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots: DELETE is not allowed'); END;
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('006_snapshots');
