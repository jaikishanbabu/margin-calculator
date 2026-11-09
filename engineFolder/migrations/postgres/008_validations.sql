-- Stage 3: broker-basket validation results per segment.
CREATE TABLE IF NOT EXISTS margin.validations (
    id           BIGSERIAL PRIMARY KEY,
    segment      TEXT NOT NULL,
    run_id       BIGINT,
    status       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    report       TEXT NOT NULL,
    rules        TEXT,
    code_version TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_validations_seg ON margin.validations (segment, id);
GRANT SELECT, INSERT ON margin.validations TO margin_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA margin TO margin_app;
REVOKE ALL ON margin.validations FROM anon, authenticated, service_role;
INSERT INTO margin.schema_migrations (version) VALUES ('008_validations') ON CONFLICT DO NOTHING;
