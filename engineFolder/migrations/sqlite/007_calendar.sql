-- Exchange holiday calendars (admin-maintained, imported from the exchanges) + per-segment fetch dates.
CREATE TABLE IF NOT EXISTS calendar_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,              -- NSE | NSE_CD | BSE | MCX
    day TEXT NOT NULL,                   -- YYYY-MM-DD
    kind TEXT NOT NULL,                  -- closed | morning_closed | evening_closed | special_session
    description TEXT,
    source TEXT,                         -- import:<url> | manual
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (exchange, day)
);
ALTER TABLE fetch_items ADD COLUMN trade_date TEXT;
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('007_calendar');
