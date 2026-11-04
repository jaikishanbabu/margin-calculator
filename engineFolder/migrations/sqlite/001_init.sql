-- Stage 1 schema for local dev/test (SQLite). Mirrors migrations/postgres/001_init.sql.
-- SQLite has no roles, so only the triggers part of the tamper protection applies here.

CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('superadmin', 'admin')),
    active INTEGER NOT NULL DEFAULT 1,
    token_version INTEGER NOT NULL DEFAULT 1,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER UNIQUE NOT NULL,
    ts TEXT NOT NULL,
    username TEXT,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment TEXT NOT NULL,
    file_type TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    trade_date_source TEXT NOT NULL,
    cycle TEXT,
    cycle_source TEXT,
    source_timestamp TEXT,
    source_meta TEXT,
    filename TEXT NOT NULL,
    content_kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    inner_manifest TEXT,
    archive_backend TEXT NOT NULL,
    archive_key TEXT NOT NULL,
    archive_verified_at TEXT,
    manifest_key TEXT,
    parser_versions TEXT NOT NULL,
    app_version TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploader_ip TEXT,
    status TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_uploads_lookup ON uploads (segment, file_type, trade_date, status);
CREATE INDEX IF NOT EXISTS ix_uploads_sha ON uploads (sha256);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    backend TEXT,
    location TEXT,
    sha256 TEXT,
    details TEXT,
    triggered_by TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log: UPDATE is not allowed (append-only table)'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log: DELETE is not allowed (append-only table)'); END;

CREATE TRIGGER IF NOT EXISTS uploads_no_delete BEFORE DELETE ON uploads
BEGIN SELECT RAISE(ABORT, 'uploads: DELETE is not allowed (mark status=deleted instead)'); END;
CREATE TRIGGER IF NOT EXISTS uploads_guard BEFORE UPDATE ON uploads
WHEN NEW.sha256 IS NOT OLD.sha256 OR NEW.uploaded_by IS NOT OLD.uploaded_by OR NEW.uploaded_at IS NOT OLD.uploaded_at
  OR NEW.filename IS NOT OLD.filename OR NEW.size_bytes IS NOT OLD.size_bytes OR NEW.trade_date IS NOT OLD.trade_date
  OR NEW.cycle IS NOT OLD.cycle OR NEW.source_meta IS NOT OLD.source_meta OR NEW.archive_key IS NOT OLD.archive_key
BEGIN SELECT RAISE(ABORT, 'uploads: provenance columns are immutable'); END;

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('001_init');
