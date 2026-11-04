# Margin Calculator

SPAN / exposure margin calculator for Indian exchanges (NSE F&O, BSE F&O, NSE currency, MCX, NSE/BSE cash).

```
engineFolder/   Python backend: API, upload pipeline, archive, audit log, backups (margin engine from Stage 3)
adminFolder/    Admin website: upload files, upload log, audit log, backups, admin users
marginCalc/     Public calculator (not published until a segment passes validation)
render.yaml     Render Blueprint - STAGING only (branch develop)
.github/        CI (SQLite + Postgres + secret scan), daily health check, weekly backup + restore test
```

## Status

| Stage | Scope | State |
|---|---|---|
| 1 | Foundation: auth, audit log, safe uploads, archive, backups | **In review** |
| 2 | Parsers for every segment, data-readiness dashboard, Google Drive archive | Not started |
| 3 | NFO margin engine + validation against broker test cases | Not started |
| 4 | Admin + calculator UIs on staging | Not started |
| 5 | BFO, CDS, MCX, cash — one segment at a time | Not started |
| 6 | Production deploy (after approved PR + passing tests) | Not started |

No segment is validated. The public calculator shows nothing calculable until one is.

## Branches and environments

- `develop` → **staging** (Render free + Supabase free). Every push runs CI and auto-deploys staging.
- `main` → production (Stage 6). Changes reach `main` only through an approved pull request with passing CI.

## Security model (Stage 1)

- **Secrets** live only in Render's environment settings and GitHub Actions secrets. The repo, the admin
  page and the calculator page contain no credentials. CI scans every commit (gitleaks + a repo test).
- **SuperAdmin** comes from `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` on the server and can create,
  disable, unlock and reset admins. Tokens are invalidated on disable, password reset or role change.
- **Login protection**: PBKDF2-SHA256 hashing, account lockout after 5 failures (15 min), per-IP throttle,
  identical errors for unknown users and wrong passwords.
- **Database**: the app connects as `margin_app`, which owns nothing, cannot run DDL, and has no UPDATE,
  DELETE or TRUNCATE on the audit log (nor DELETE on uploads). Tables live in schema `margin`, which is
  not exposed through Supabase's REST API. Triggers additionally block changes for the owner unless they
  deliberately disable them.
- **Audit log** is append-only and hash-chained; `Verify chain` (admin) and every restore test re-check it.
  Weekly backups store the chain head separately, so truncating or rewriting the live log is detected.
  Limit: someone holding the Supabase owner password can still alter data — the chain and the separate
  backups make that *detectable*, not impossible.
- **Uploads**: type decided from file content; zip/gzip bombs, path traversal, symlinks, encryption,
  nested archives and XML entity declarations are rejected; size-limited; every original is archived
  write-once, read back and checksum-verified, with a JSON manifest next to it.
- **SPAN provenance**: effective trading date and timestamp are read from inside the file. The cycle is
  taken from the file only if it states one; otherwise the admin must enter it, and it is stored with
  `cycle_source = admin`. Filenames are never used as provenance.

## Running locally

```bash
cd engineFolder
pip install -r requirements-dev.txt
python -m pytest                       # SQLite mode
SUPERADMIN_PASSWORD='Local-Dev-Pass-1!' SERVE_STATIC=1 uvicorn app.main:app --reload
# open http://localhost:8000/admin/
```

See [docs/DEPLOY_STAGING.md](docs/DEPLOY_STAGING.md) for the staging setup and [docs/STAGE1.md](docs/STAGE1.md)
for the Stage 1 review notes.
