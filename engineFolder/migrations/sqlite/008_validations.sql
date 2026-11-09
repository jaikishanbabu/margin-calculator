-- Stage 3: broker-basket validation results per segment.
CREATE TABLE IF NOT EXISTS validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment TEXT NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,                -- passed | failed
    summary TEXT NOT NULL,               -- JSON counts
    report TEXT NOT NULL,                -- JSON: tests, baskets, data on disk
    rules TEXT,                          -- JSON {file: sha256} the run was checked against
    code_version TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_validations_seg ON validations (segment, id);
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('008_validations');
