# Margin Calculator: Project Handover

*Status as of 19 Nov 2026. Repository: github.com/jaikishanbabu/margin-calculator (public). Running locally: see [LOCAL_SETUP.md](LOCAL_SETUP.md).*

## 1. What it is

A SPAN and exposure margin calculator for Indian exchanges, modelled on Zerodha's SPAN calculator
(zerodha.com/margin-calculator/SPAN/). Zerodha's calculator is the reference for layout and features.
The UI style follows the PPF calculator on laabh.com.

- It will be a **public website**, not an internal tool.
- The margin calculation logic is being shared with a developer who will build it into the owner's **OMS**
  (order management system).
- Segments: **NSE F&O (NFO), BSE F&O (BFO), NSE Currency (CDS), MCX Commodity, NSE Cash, BSE Cash.**

## 2. Current status

| Area | State |
|---|---|
| Stage 1: foundation (auth, audit log, safe uploads, archive, backups) | Done, accepted |
| Stage 2: Exchange File Fetcher, parsers, Data readiness page, holiday calendar | Done |
| Stage 3: NFO margin engine + validation against broker test cases | Done |
| Stage 4: Admin + calculator UIs on staging | Done for NSE F&O and NSE Cash |
| NSE Cash | Engine, validation and Cash tab built; shown on staging labelled "not validated" |
| BSE Cash | **Next segment to build** (chosen 16 Nov 2026) |
| BFO, MCX | Not built; no accuracy claims until validated against broker test cases |
| CDS (NSE Currency) | **On hold** (16 Nov 2026) |
| Google Drive permanent archive | Planned (switchable account) |
| Production (`main`) | Not deployed; staging only |

## 3. Decisions made

### Margin rules
- **F&O SPAN basis:** use the **start-of-day SPAN file (cycle i1) for the whole day** for all margins
  (decided 13 Nov 2026). Recalculating with intraday SPAN files (higher of start-of-day and latest) is
  deferred. An admin toggle to switch to the latest SPAN cycle exists on Data readiness (basis bod / latest).
- **Cash (CM) VaR:** always use the **latest VaR file**. Each new batch replaces earlier rates; there is no
  max with start-of-day and no admin toggle (18 Nov 2026).
- **The cash-market margin PRD is common to NSE and BSE** and covers **CNC, MIS and MTF**. In the OMS, CM margin
  has no front end, and the **VaR file is mandatory** for order validation. PRDs should be kept
  straightforward for the developer.
- Margin rules are **versioned and exchange-specific** (`engineFolder/loader/static_rates.yaml`).
- **No accuracy claims for BSE, CDS or MCX** until they're validated against broker test cases.
- The engine **never estimates**: contracts with no SPAN risk array raise `NoRiskData`.

### Process
- Build in stages on the **`develop` branch → staging only**. Nothing reaches `main` (production) without the
  owner approving the PR and required tests passing.
- The owner wants to **see the plan before building** a new piece.
- Changes to the owner's own test files need the owner's approval and are logged in
  `engineFolder/validation/VALIDATION_CHANGES.md`.

### Security
- **Secrets never go in chat or source code.** DB passwords, the Supabase secret key, OPS/FETCHER tokens,
  admin passwords and the GitHub dispatch token are entered only in the Render, Supabase and GitHub
  dashboards. Only public URLs are shared.
- The bootstrap SuperAdmin password is one-time: a password change is forced at first login.

## 4. Margin logic (summary)

### NSE F&O (`engineFolder/engine/nfo.py`)
**Total = SPAN + exposure margin (ELM)**, the two figures Zerodha shows.
- **SPAN** is calculated per underlying from the loaded SPAN cycle, less net option value (standard SPAN).
- **ELM** is charged per leg as a % of notional:
  - **Futures:** units × futures price × OTH rate. Calendar spreads pay on one third of the far leg
    (not on an expiring future on its expiry day).
  - **Short options:** units × previous close × rate. The rate is OTH, or OTM if deep out of the money
    (call strike > close × 1.3, put strike < close × 0.7; 10% for indices). For indices it's 5% beyond
    9 months, plus 2% on expiry day.
  - **Long options:** no ELM.
- Contracts are identified by exchange + symbol + expiry + strike + option type. The NSE SPAN file also
  holds BSE contracts, so only NSE-listed contracts resolve (from the contract master or fo_mktlots).

### NSE Cash (`engineFolder/engine/nse.py`)
For each stock, the **effective rate % = min(100, max of)**:
- **AMR:** NSE's applicable margin rate (VaR + ELM + ad hoc) from the VaR batch.
- **Surveillance:**
  - short-term ASM stage I: max(1.5 × (VaR + ELM), 40)
  - stage II: max(2.5 × (VaR + ELM), 80)
  - long-term ASM: 100%
- **100% floor:** trade-for-trade series (BE/BZ/ST/SZ), or no VaR row.

Then by product:
- **Intraday (MIS):** value × max(rate, broker minimum 20%); leverage = 1 / that.
- **Delivery (CNC):** a buy needs 100% of its value; a sell needs the shares in holdings.
- **No offsets:** stocks are margined one by one.

## 5. Exchange files per segment

| Segment | Required | Optional |
|---|---|---|
| NFO | SPAN, ELM | contract master, lot sizes, bhavcopy, settlement, ban list |
| BFO | SPAN, ELM | contract master, bhavcopy |
| CDS | SPAN | contract master, ELM, bhavcopy |
| MCX | SPAN, contract master | ELM, bhavcopy |
| NSE / BSE Cash | VaR, bhavcopy | security master, surveillance (ASM/GSM/T2T) |

Example file names:
- `nsccl.YYYYMMDD.i1.zip` (NFO SPAN, i1…i5, s)
- `ael_DDMMYYYY.csv` (ELM)
- `NSE_FO_contract_DDMMYYYY.csv.gz`
- `fo_mktlots.csv`
- `C_VAR1_DDMMYYYY_N.DAT` (VaR, batches 1–6)
- `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`

Sources and schedule are in `engineFolder/exfetch_sources.yaml`.

**Fetcher rules:**
- From 21:00 IST, it fetches the next trading day's files.
- Later SPAN cycles and VaR batches are skipped until the first one is archived.
- Reference files may come from up to 2 previous trading days, with a note. SPAN, VaR and prices never do.
- Manual runs on a weekend or holiday target the next trading day.

## 6. Architecture

```
engineFolder/   Python (Starlette/uvicorn) backend: API, upload pipeline, archive, audit log, backups,
                fetcher control, margin engine (engine/), file loaders (loader/), validation runners
adminFolder/    Admin website (static HTML/JS)
marginCalc/     Public calculator (static HTML/JS): F&O tab and Cash tab with an exchange dropdown
render.yaml     Render Blueprint (staging, branch develop)
railway.json    Optional second engine host (Railway, Dockerfile build)
.github/        CI, fetcher, validation, Pages deploy, health check, weekly backup, exchange probe
docs/           DEPLOY_STAGING.md, HOSTING.md, STAGE1.md
```

| Piece | Where it runs |
|---|---|
| Engine API | **Render** (paid bandwidth, pay-as-you-go $0.15/GB over 5 GB; free instance, sleeps after 15 min idle) |
| Database | **Supabase** Postgres, schema `margin`, app role `margin_app` (no DDL, append-only audit log) |
| File archive | **Supabase Storage** bucket `margin-archive` (originals, manifests, snapshots, backups) |
| Admin + calculator pages | **GitHub Pages**: `jaikishanbabu.github.io/margin-calculator/admin/` and `/calculator/`. The engine also serves `/admin/` and `/calculator/` |
| Fetching, parsing, snapshot builds, validation | **GitHub Actions** (engine dispatches workflows) |
| Auto-fetch timer | Supabase pg_cron calls `/api/ops/fetch-tick` every 15 min |
| Staging URL | `https://margin-engine-staging.onrender.com` |

### Data flow
1. A Supabase cron tick or the admin's **Fetch now** makes the engine dispatch `fetcher.yml` on GitHub Actions.
2. The job downloads exchange files and uploads them **directly to Supabase Storage via signed links**.
   The engine checks each file (type from content, zip bomb and path traversal checks, provenance read from
   inside the file, duplicates, checksum), archives it write-once and logs it.
3. The job builds **per-segment SQLite snapshots** (gzipped) from the archived files, with originals cached
   in GitHub Actions, and stores them. Data readiness marks which snapshot is in use.
4. `POST /api/margin` loads the in-use snapshot (checked by SHA-256, cached on disk) and returns SPAN,
   exposure and total. Every answer says which data it used (trade date, SPAN cycle, file time, basis)
   and whether the segment is validated.

### Admin features
- **Roles:** SuperAdmin creates and disables admins; there's a log of who uploaded each file and when.
- **Uploads:** segment → file type → attach a file.
- **Logs:** upload log, plus a hash-chained, append-only audit log with a **Verify chain** check.
- **Exchange File Fetcher:**
  - auto-fetch toggle with a frequency of 15 min to 3 h
  - Fetch now button
  - progress per segment and per file
- **Data readiness:** snapshot in use, basis toggle (bod / latest), rebuild.
- **Validation report:** broker comparison, pass/fail per test case.
- **Holiday calendar:** per exchange, with imports and approvals.
- **Backups:** weekly, retention setting, and a restore test.
- **Hosting tab (SuperAdmin):** choose the preferred engine host (Render or Railway). Pages fall back automatically.
- **Look:** light/dark theme, sidebar navigation (drawer on phones).

### Security model
- **Passwords and login:** PBKDF2-SHA256 hashing, lockout after 5 failures (15 min), per-IP throttle,
  identical errors for unknown users and wrong passwords.
- **Tokens:** invalidated on disable, password reset or role change.
- **Audit log:** hash-chained. Backups store the chain head separately, so tampering can be detected.
- **Ops endpoints:** `X-Ops-Token` for ops, `X-Fetcher-Token` for GitHub jobs.
- **CORS:** allows only the GitHub Pages origin.
- **CI:** gitleaks secret scan plus tests on SQLite and on Postgres (restricted app role).

## 7. Validation against brokers
- **Approach:** a test-case spreadsheet of baskets, with margins quoted by **Angel One and Zerodha**.
  - Runners: `validation/run_nfo.py`, `run_nse.py`
  - Owner's tests: `validation/tests/`
- The **Angel One filler agent** lives in its own `angelone-filler` project. The plan is to add it to the
  exchange-fetcher project so Angel One margins are compared with the engine's, with each test case marked pass/fail.
- **Public rule:** a segment is served publicly only after its own broker validation passes. Staging shows
  unvalidated segments, labelled.

## 8. API (main routes)
- **Public:**
  - `GET /api/health`, `/api/public/status`
  - `POST /api/margin`
  - `GET /api/margin/meta | contracts | segments`
- **Auth:** `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/password`
- **Admin:**
  - `/api/admin/users`, `/uploads`, `/audit` (+ `/verify`), `/backups`, `/settings`
  - `/fetcher`, `/readiness`, `/validation`, `/calendar`, `/snapshots/{id}`
- **GitHub jobs:** `/api/fetcher/runs/{id}/start | items | stage | upload | snapshot | validation | finish`,
  `/api/fetcher/uploads/{id}/original`
- **Ops:** `/api/ops/backup`, `/restore-test`, `/fetch-tick`, `/probe-exchanges`

## 9. Hosting incident (18 Nov 2026)
- **What happened:** Render's free workspace was **suspended** after using up its 5 GB of monthly outbound
  bandwidth.
- **Cause:** every exchange file went through the engine on its way to Supabase, and every snapshot rebuild
  re-downloaded all input files through the engine, with no cache.
- **Fix (PR #1, merged, deployed as `f181c8b`):**
  - signed-link transfers straight between GitHub Actions and Supabase
  - an originals cache in GitHub Actions
  - gzip on API responses
  - pages moved to GitHub Pages
  - optional Railway host with an admin switch and automatic fallback
- **Billing:** the owner added a card on Render (pay-as-you-go, not the $25 Pro plan).
- **Railway:** not set up; optional backup. Steps are in `docs/HOSTING.md`.
- **Still to confirm:** run **Fetch now** and check the run log for no "direct upload failed" fallback, which
  shows that the files bypass Render.

## 9a. Running locally (19 Nov 2026)
- The engine runs on the owner's office Mac with no Render and no Supabase: SQLite plus a local folder,
  on port 8001 because 8000 is taken.
- A local NSE Cash fetch and snapshot build worked.
- The admin **Fetch now** button can't work locally (it dispatches GitHub Actions), so fetches run from
  the terminal. Steps are in [LOCAL_SETUP.md](LOCAL_SETUP.md).
- **PR #2:** a dev copy shows NSE Cash labelled "not validated", like staging. Production is unchanged.

## 10. Open items / next steps
1. Confirm the first fetch after the hosting fix uses direct transfers.
1a. Optional: a "local jobs" mode so the admin's Fetch now / Rebuild / Run validation buttons run the jobs
    on the same machine when the engine runs locally.
2. Build **BSE Cash** (next segment), using the common CM PRD (CNC / MIS / MTF; latest VaR file).
3. Validate NSE Cash against brokers before showing it publicly.
4. Add the Angel One comparison into the fetcher project (pass/fail per test case).
5. Google Drive as the permanent archive (Supabase keeps originals on a rolling window).
6. Later: intraday SPAN recalculation (higher of start-of-day and latest).
7. BFO and MCX segments; CDS stays on hold.
8. When it goes public: a paid Render instance (no sleep), then the production deploy from `main` after an approved PR.

## 11. Conventions for whoever picks this up
- Work on a branch, then open a PR into `develop`. CI must pass, and the owner approves before merge.
- Never put secrets in code, chat or docs. List the variable names the owner should enter in each dashboard instead.
- Don't change the owner's test files without approval; log any change in `VALIDATION_CHANGES.md`.
- The owner works from an **iPhone only**, so give dashboard steps that work in a phone browser.
