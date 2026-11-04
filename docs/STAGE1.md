# Stage 1 — Foundation: review notes

## Scope delivered
| Requirement | Where |
|---|---|
| SuperAdmin from server secrets; SuperAdmin creates/disables/unlocks/resets admins | `app/auth.py`, Admin → *Admins* |
| Hardened login: PBKDF2, lockout, IP throttle, token invalidation, role checks on every endpoint | `app/auth.py`, `app/main.py` |
| Upload log: who uploaded what, when, from where, with checksum | `uploads` table, Admin → *Upload log* |
| Tamper-evident audit log (hash chain + DB permissions + triggers + separate backups) | `app/audit.py`, `migrations/postgres/*` |
| Safe uploads: content sniffing, zip/gzip-bomb, traversal, symlink, encryption, nesting, XML-entity, size limits | `app/filesafety.py` |
| SPAN provenance from file contents; cycle never inferred from filenames | `app/provenance.py` |
| Durable archive of originals, write-once, read-back checksum verification, per-upload manifest | `app/uploads.py` |
| Storage interface (local, Supabase now; Google Drive slot for Stage 2) | `app/storage/` |
| Weekly DB backup export with manifest; restore test (checksums, row counts, audit chain, originals) | `app/backup.py`, `.github/workflows/backup.yml` |
| Backup retention control | Setting `backup_retention_count` (default 12) |
| Daily health check and alerting (monitoring only, not a pause guarantee) | `.github/workflows/health.yml` |
| Secrets only in Render/GitHub stores; startup refuses weak/missing secrets; repo secret scanning | `app/config.py`, CI |
| Separate staging deployment from `develop` | `render.yaml` |

Not in Stage 1, on purpose: parsing files into calculation tables, the 7-trading-day active-data window, the
readiness dashboard and Google Drive (Stage 2); any margin calculation (Stage 3+). The public calculator
page only states that nothing is validated yet.

## Test results

### Automated suite — 85 passed (SQLite mode, local run)
| File | Tests | Covers |
|---|---|---|
| `test_auth.py` | 12 | login, identical failure messages, lockout, IP throttle, 401/403 matrix, password rules, token invalidation on reset/disable, forged/`alg=none` tokens, hashing, auditing |
| `test_uploads.py` | 22 | provenance from file vs filename, missing/mismatched cycle and date, settlement-flag conflicts, zip bomb, gzip bomb, traversal (4 variants), symlink, nested archive, too many members, encrypted zip, executable/HTML/CSV-as-SPAN, XML entities, size limit, byte-exact archive + manifest, duplicates, replace/supersede, withdraw keeps record + original, provenance immutability, rejected uploads audited |
| `test_storage.py` | 21 | same contract on local + Supabase backends: round-trip, write-once, not-found, recursive list, delete, unsafe keys, corruption detection, wrong key |
| `test_backup_audit.py` | 16 | chain valid, app can't edit/delete audit rows, 3 tamper types detected, backup + restore passes, corrupted backup detected, altered originals detected, live log truncated after backup detected, retention pruning, Supabase-backend backup/restore, manifest contents, ops-token protection, health |
| `test_config_security.py` | 14 | staging refuses weak/missing secrets (7 cases), no secrets in tracked files, no credentials in frontends, `.env` ignored, security headers, no internal error leakage, no CORS by default |

CI repeats the whole suite on **Postgres 16 logged in as the restricted `margin_app` role**, plus the privilege
script and direct `psql` attempts as `margin_app`, plus gitleaks. (Run results: see the Actions tab.)

### Database checks on the real staging Supabase project
`tests/pg/privilege_check.sql`, run 2026-11-04 on `margin-calculator-staging`:

```
REFUSED  | UPDATE margin.audit_log SET detail = 'tampered'          -> permission denied for table audit_log
REFUSED  | DELETE FROM margin.audit_log                             -> permission denied for table audit_log
REFUSED  | TRUNCATE margin.audit_log                                -> permission denied for table audit_log
REFUSED  | ALTER TABLE margin.audit_log DISABLE TRIGGER ...         -> must be owner of table audit_log
REFUSED  | DROP TRIGGER audit_no_update ON margin.audit_log         -> must be owner of relation audit_log
REFUSED  | DROP TABLE margin.audit_log                              -> must be owner of table audit_log
REFUSED  | CREATE TABLE margin.evil (x int)                         -> permission denied for schema margin
REFUSED  | DELETE FROM margin.uploads                               -> permission denied for table uploads
REFUSED  | TRUNCATE margin.uploads                                  -> permission denied for table uploads
privileges: app_update_audit=false app_delete_audit=false app_truncate_audit=false app_delete_uploads=false
            app_in_postgres=false service_key_schema=false anon_schema=false
```
Owner-level triggers (checked on local Postgres 16): UPDATE/DELETE/TRUNCATE of `audit_log` and changes to
upload provenance columns are refused even for the owner unless the trigger is explicitly disabled;
status updates on uploads are allowed.

### Still required before Stage 1 is declared complete
- [ ] CI green on `develop` (SQLite, Postgres, secret scan)
- [ ] Staging deployed on Render and reachable
- [ ] On staging: real upload → **Run backup** → **Run restore test** passes (real Supabase Storage)
- [ ] *Staging backup + restore test* workflow green; *Staging health check* workflow green

## Known limits / decisions for you
1. Holder of the Supabase **owner** password can still alter data (by disabling triggers). The hash chain
   and off-database backups make that detectable, not impossible. Keep that password out of Render.
2. The per-IP throttle relies on the proxy's forwarded address; the per-account lockout does not.
3. The SPAN header fields NSE/BSE/MCX actually publish will be confirmed with your first real files; if a
   file carries an explicit cycle field under a name not yet recognised, it is still recorded verbatim in
   `source_meta`, and the admin-entered cycle is required.
4. Backups include password *hashes* (needed to restore accounts) — the bucket is private.

## Incident: credentials shown in chat (2026-11-04)
Staging credentials were printed in the build chat. Response: the `margin_app` login was disabled and its
password wiped the same day; the printed SuperAdmin password and ops token were never configured anywhere and
must not be used. Deployment now avoids the pattern: Render generates `JWT_SECRET` and `OPS_TOKEN`, the owner
sets the DB and SuperAdmin passwords in the dashboards, and only the public staging URL is shared.
