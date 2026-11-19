# CLAUDE.md: Margin Calculator

Read this first in every session. Full background: `docs/HANDOVER.md`. Running locally: `docs/LOCAL_SETUP.md`.
Deployment: `docs/DEPLOY_STAGING.md`, `docs/HOSTING.md`.

## What this is
A SPAN and exposure margin calculator for Indian exchanges, modelled on Zerodha's SPAN calculator.
It will be a public website. The margin logic is also being handed to a developer for the owner's OMS.

Segments:
- **NSE F&O (NFO):** live
- **NSE Cash (NSECM):** built, not validated
- **BSE Cash (BSECM):** next to build
- **BFO, MCX:** later
- **CDS:** on hold

## Repo layout
```
engineFolder/   Python 3.12 backend (Starlette + uvicorn)
  app/          API, auth, audit log, uploads, archive, fetcher control, backups, margin service
  engine/       margin engines: nfo.py (F&O SPAN + ELM), nse.py (cash VaR/ELM/surveillance)
  loader/       exchange-file parsers -> per-segment SQLite snapshots; static_rates.yaml = versioned rules
  exfetch/      exchange download logic; exfetch_sources.yaml = sources + schedule
  validation/   broker-comparison runners + the owner's tests (see VALIDATION_CHANGES.md)
  tests/        pytest (SQLite by default; CI also runs Postgres)
adminFolder/    admin website (static HTML/JS)
marginCalc/     public calculator (static HTML/JS)
.github/workflows/  CI, fetcher, validation, Pages, health, backup
```

## Commands (run from engineFolder, venv active, Python 3.12)
```bash
python -m pytest -q                                   # all tests must pass before any commit
SUPERADMIN_PASSWORD=... FETCHER_TOKEN=... SERVE_STATIC=1 uvicorn app.main:app --port 8001
# local fetch: create a run, then run the job (the admin Fetch now button needs GitHub, not usable locally)
python -c "from app import fetcher; print(fetcher.create_run('manual','superadmin',['NSECM'],dispatch_fn=lambda r: None)['id'])"
ENGINE_URL=http://localhost:8001 ORIGINALS_CACHE=~/.cache/margin-originals python -m app.fetch_job --run-id <id>
```
- **Ports:** use 8001 on the owner's Mac, because another program holds 8000.
- **Python:** macOS's built-in python3 is 3.9 and won't work; use 3.12 (via `uv`).

## Owner's rules (must follow)
1. **Never put secrets in code, docs, commits or chat.** That covers DB URLs/passwords, the Supabase key,
   JWT_SECRET, OPS_TOKEN, FETCHER_TOKEN, GITHUB_DISPATCH_TOKEN and admin passwords. They're entered in the
   Render, Supabase and GitHub dashboards by the owner. List the variable *names* only.
2. **Branch, then open a PR into `develop`.** CI (SQLite + Postgres + gitleaks) must pass, and the owner approves
   before merge. `develop` auto-deploys staging; `main` = production, only via an approved PR.
3. **Show the plan before building** anything non-trivial.
4. **Don't change the owner's test files** (`validation/tests/`, fixtures) without approval; log approved changes in
   `engineFolder/validation/VALIDATION_CHANGES.md`.
5. **No accuracy claims** for a segment until it passes broker validation. Production serves only validated
   segments; staging and dev show unvalidated ones, labelled.
6. **The engine never estimates:** missing risk data raises an error (e.g. `NoRiskData`).
7. **The owner works from an iPhone or this Mac:** give dashboard steps that work in a browser, and keep replies short.

## Margin decisions
- **F&O:** the start-of-day SPAN (i1) is used for the whole day. Intraday recalculation (higher of start-of-day
  and latest) is deferred. Admin basis toggle: bod / latest.
- **Cash (NSE + BSE):** one common CM PRD covering CNC, MIS and MTF. **Always use the latest VaR file**: each new
  batch replaces earlier rates, with no max with start-of-day and no toggle. In the OMS the VaR file is mandatory
  for order validation, and CM margin has no front end.
- **Rules:** versioned and exchange-specific, in `loader/static_rates.yaml`.

## Architecture (staging)
- **Engine:** Render (pay-as-you-go bandwidth).
- **Database:** Supabase Postgres, schema `margin`, app role `margin_app`.
- **Files:** Supabase Storage bucket `margin-archive`.
- **Pages:** GitHub Pages.
- **Jobs:** fetch, build and validation run on GitHub Actions, dispatched by the engine. Files move
  job ↔ storage over signed links, never through the engine (Render bandwidth).
- **Timer:** Supabase pg_cron calls `/api/ops/fetch-tick` every 15 min.
- **Locally:** SQLite + `engineFolder/data/archive/`, no Render or Supabase needed.

## Security model (keep it)
- PBKDF2 passwords, lockout and throttling.
- Append-only, hash-chained audit log; the `margin_app` role can't alter it.
- Upload checks: type from content, bomb and traversal checks, provenance from inside the file; write-once,
  checksum-verified archive.
- The SuperAdmin bootstrap password is one-time; `SUPERADMIN_RESET=1` is the audited break-glass.
