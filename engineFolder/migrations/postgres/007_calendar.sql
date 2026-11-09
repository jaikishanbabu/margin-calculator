-- Exchange holiday calendars (admin-maintained, imported from the exchanges) + per-segment fetch dates.
CREATE TABLE IF NOT EXISTS margin.calendar_days (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT NOT NULL,           -- NSE | NSE_CD | BSE | MCX
    day         TEXT NOT NULL,           -- YYYY-MM-DD
    kind        TEXT NOT NULL,           -- closed | morning_closed | evening_closed | special_session
    description TEXT,
    source      TEXT,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (exchange, day)
);
ALTER TABLE margin.fetch_items ADD COLUMN IF NOT EXISTS trade_date TEXT;

GRANT SELECT, INSERT, UPDATE, DELETE ON margin.calendar_days TO margin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA margin TO margin_app;
REVOKE ALL ON margin.calendar_days FROM anon, authenticated, service_role;

INSERT INTO margin.schema_migrations (version) VALUES ('007_calendar') ON CONFLICT DO NOTHING;
