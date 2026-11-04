-- Repeatable, NON-DESTRUCTIVE check that a compromised app credential (role margin_app)
-- cannot modify or delete audit entries or upload records.
-- Run as the schema owner (Supabase SQL editor / MCP / db-migrate workflow).
-- Every line of the result must say REFUSED, and every privilege column must be false.
-- (Statements are attempted against all rows; permission checks happen before any row is
--  touched, and the whole block is rolled back at the end, so nothing changes.)

GRANT margin_app TO postgres;

CREATE TEMP TABLE privcheck_out (line text);

DO $$
DECLARE
  attempts text[] := ARRAY[
    'UPDATE margin.audit_log SET detail = ''tampered''',
    'DELETE FROM margin.audit_log',
    'TRUNCATE margin.audit_log',
    'ALTER TABLE margin.audit_log DISABLE TRIGGER audit_no_update',
    'DROP TRIGGER audit_no_update ON margin.audit_log',
    'DROP TABLE margin.audit_log',
    'CREATE TABLE margin.evil (x int)',
    'DELETE FROM margin.uploads',
    'TRUNCATE margin.uploads'
  ];
  q text;
  lines text[] := '{}';
BEGIN
  SET LOCAL ROLE margin_app;
  FOREACH q IN ARRAY attempts LOOP
    BEGIN
      EXECUTE q;
      lines := lines || ('ALLOWED  | ' || q);
    EXCEPTION WHEN OTHERS THEN
      lines := lines || ('REFUSED  | ' || q || '  -> ' || SQLERRM);
    END;
  END LOOP;
  RESET ROLE;
  INSERT INTO privcheck_out SELECT unnest(lines);
END $$;

REVOKE margin_app FROM postgres;

SELECT line FROM privcheck_out
UNION ALL
SELECT 'privileges: ' || json_build_object(
  'app_update_audit',   has_table_privilege('margin_app','margin.audit_log','UPDATE'),
  'app_delete_audit',   has_table_privilege('margin_app','margin.audit_log','DELETE'),
  'app_truncate_audit', has_table_privilege('margin_app','margin.audit_log','TRUNCATE'),
  'app_delete_uploads', has_table_privilege('margin_app','margin.uploads','DELETE'),
  'app_in_postgres',    pg_has_role('margin_app','postgres','MEMBER'),
  'service_key_schema', has_schema_privilege('service_role','margin','USAGE'),
  'anon_schema',        has_schema_privilege('anon','margin','USAGE'))::text;
-- Immutability of upload provenance columns (sha256, uploader, time...) is enforced by the
-- uploads_guard trigger and covered by tests/test_uploads.py::test_provenance_immutable.
