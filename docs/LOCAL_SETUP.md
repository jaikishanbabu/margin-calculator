# Running the Margin Calculator Locally (no Render, no Supabase)

The engine runs entirely on one computer:
- **Database:** a SQLite file instead of Supabase Postgres
- **Archive:** a local folder instead of Supabase Storage
- **Pages:** admin and calculator, served by the engine itself
- **Jobs:** fetch, snapshot build and validation, run as Python commands on the same machine instead of GitHub Actions

```
            ┌────────────────────── your computer ──────────────────────┐
 browser ──►│ engine (uvicorn, port 8000)                               │
            │   /admin/  /calculator/  /api/...                         │
            │   SQLite: engineFolder/data/margin.db                     │
            │   archive: engineFolder/data/archive/   (originals,       │
            │            snapshots, backups)                            │
            │                       ▲                                   │
            │ fetch job (python) ───┘ downloads from NSE/BSE/MCX,       │
            │                         uploads to the engine, builds     │
            │                         snapshots                         │
            └───────────────────────────────────────────────────────────┘
```

---

## Part A: One-time setup

> **Tested 19 Nov 2026 on an office Mac:** the NSE Cash fetch and snapshot build work locally.
> Gotchas found then are in Part E: Python 3.12 via `uv`, port 8000 already taken, the SuperAdmin
> password only set on first start, and the first build needing the previous day's bhavcopy.

### A1. Install the tools
| | Windows | macOS | Linux |
|---|---|---|---|
| Python **3.12** | python.org installer (tick **"Add python.exe to PATH"**) | python.org installer, or without admin rights: `curl -LsSf https://astral.sh/uv/install.sh \| sh` then `uv python install 3.12` | `sudo apt install python3.12 python3.12-venv` |
| Git | git-scm.com (Git for Windows) | `xcode-select --install` | `sudo apt install git` |

Check:
```bash
python --version      # Windows (should say 3.12.x)
python3 --version     # macOS / Linux
git --version
```

### A2. Get the code
```bash
git clone https://github.com/jaikishanbabu/margin-calculator
cd margin-calculator/engineFolder
git checkout develop
```

### A3. Create a virtual environment and install packages
**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```
If PowerShell refuses to run the activate script, run this once, then activate again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**macOS / Linux:**
```bash
python3.12 -m venv .venv          # or, with uv: uv venv --python 3.12 .venv
source .venv/bin/activate
python --version                  # must say 3.12 - macOS's built-in python3 is 3.9 and won't work
pip install -r requirements-dev.txt   # or: uv pip install -r requirements-dev.txt
```

### A4. Check everything works
```bash
python -m pytest -q
```
Every test should pass (around 170 tests, a few minutes). This uses its own temporary data, not yours.

### A5. Choose two local secrets
Make up these two values and keep them in your password manager:
- **SUPERADMIN_PASSWORD:** the first login password (at least 8 characters, with letters and digits). You'll be asked to change it at first login.
- **FETCHER_TOKEN:** a random string of **40+ letters and digits**. The fetch job uses it to talk to the engine.

These stay on your machine. Don't commit them or paste them into chat.

---

## Part B: Daily flow

Each terminal window you open needs to:
1. be in the `engineFolder` folder
2. have the virtual environment activated (A3)
3. have the environment variables below set

**Windows (PowerShell):**
```powershell
$env:SUPERADMIN_PASSWORD = "<your first-login password>"
$env:FETCHER_TOKEN       = "<your 40+ char token>"
$env:SERVE_STATIC        = "1"
```
**macOS / Linux:**
```bash
export SUPERADMIN_PASSWORD='<your first-login password>'
export FETCHER_TOKEN='<your 40+ char token>'
export SERVE_STATIC=1
```

### B1. Start the engine (terminal 1, leave it running)
```bash
uvicorn app.main:app --port 8000
```
Open:
- Admin: **http://localhost:8000/admin/**
- Calculator: **http://localhost:8000/calculator/**
- Health: http://localhost:8000/api/health

On first start, the engine creates `data/margin.db` and `data/archive/` in `engineFolder`.

### B2. First login
1. In the admin, log in as **superadmin** with `SUPERADMIN_PASSWORD`.
2. You'll be forced to set a new password. From then on, `SUPERADMIN_PASSWORD` is only used if the database is new.
3. Optional: under **Users**, create admin accounts.

### B3. Get exchange files (terminal 2)
On a server, **Fetch now** starts a job on GitHub Actions. Locally you run the job yourself, in two steps.

**Step 1: create a fetch run** (prints the run number). Choose which files: `ALL`, or one or more of `NFO`, `NSECM`, `BFO`, `MCX`, `BSECM`.

Windows:
```powershell
python -c "from app import fetcher; print(fetcher.create_run('manual','superadmin',['NFO','NSECM'],dispatch_fn=lambda r: None)['id'])"
```
macOS / Linux: the same command, with `python3` if `python` isn't found.

**Step 2: run the job for that run number** (replace `5` with the number printed).

Windows:
```powershell
$env:ENGINE_URL = "http://localhost:8000"
$env:ORIGINALS_CACHE = "$HOME\.cache\margin-originals"
python -m app.fetch_job --run-id 5
```
macOS / Linux:
```bash
ENGINE_URL=http://localhost:8000 ORIGINALS_CACHE=~/.cache/margin-originals python -m app.fetch_job --run-id 5
```

The job:
1. checks which files the engine already has
2. downloads the rest from the exchanges
3. uploads each one to the engine, which checks and archives it
4. builds the per-segment **snapshots** the calculator uses

You can watch progress in **Admin → Exchange File Fetcher**.

Trading-date rules are the same as on the server:
- After 21:00 IST it fetches the **next** trading day's files.
- On weekends and holidays, manual runs target the next trading day.

### B4. Check the data
Open **Admin → Data readiness**. Each segment should show a snapshot **in use**, with its trade date and SPAN cycle.
The basis toggle (start of day / latest) works as on staging.

### B5. Use the calculator
Open **http://localhost:8000/calculator/**:
- **F&O tab:** NSE F&O
- **Cash tab:** NSE Cash, labelled "not validated"

Every result shows which data it used (trade date, SPAN cycle, file time).

### B6. Uploading files by hand (optional)
**Admin → Uploads**: pick segment → file type → attach the file. The engine runs the same checks as for fetched files.
Then run a fetch run (B3) so the snapshot is rebuilt. Files the engine already has aren't downloaded again.

### B7. Broker validation (optional)
Create a run in the same way as B3 step 1, then run the validation job for that number:
```bash
python -m app.validate_job --run-id <id>
```
The result appears in **Admin → Validation**.

### B8. Backups
**Admin → Backups → Run backup**, and **Restore test**. Backups are written to `data/archive/backups/`.
To keep a copy, copy the whole `engineFolder/data/` folder somewhere safe.

### B9. Stop
Press **Ctrl+C** in terminal 1. Your data stays in `engineFolder/data/` for next time.

---

## Part C: Auto-fetch locally (optional)
On the server, Supabase's timer starts a run every 15 minutes. Locally, a small loop does the same while the
engine is running. Use terminal 2, set up as in Part B.

**Windows (PowerShell),** every 30 minutes:
```powershell
$env:ENGINE_URL = "http://localhost:8000"; $env:ORIGINALS_CACHE = "$HOME\.cache\margin-originals"
while ($true) {
  $id = python -c "from app import fetcher; print(fetcher.create_run('auto','auto-fetcher',['ALL'],dispatch_fn=lambda r: None)['id'])"
  if ($LASTEXITCODE -eq 0) { python -m app.fetch_job --run-id $id }
  Start-Sleep -Seconds 1800
}
```
**macOS / Linux:**
```bash
export ENGINE_URL=http://localhost:8000 ORIGINALS_CACHE=~/.cache/margin-originals
while true; do
  id=$(python -c "from app import fetcher; print(fetcher.create_run('auto','auto-fetcher',['ALL'],dispatch_fn=lambda r: None)['id'])") \
    && python -m app.fetch_job --run-id "$id"
  sleep 1800
done
```
Stop it with Ctrl+C.

---

## Part D: What doesn't work locally (and why)
| Feature | Locally | Reason |
|---|---|---|
| Admin **Fetch now**, **Rebuild**, **Run validation** buttons | Show an error | They start jobs on GitHub Actions, and GitHub can't reach `localhost`. Use the commands in B3 / B7 instead |
| Auto-fetch toggle in the admin | Has no effect | The timer lives in Supabase. Use Part C |
| Others opening your pages | No | Only you, on this machine |
| Data shared with staging | No | Separate database. Staging data stays in Supabase |

---

## Part E: Troubleshooting
| Problem | Fix |
|---|---|
| `No matching distribution found for starlette==1.7.0` | The venv uses Python 3.9 (macOS built-in). Delete `.venv` and recreate it with Python 3.12 (A1, A3) |
| Login says "Wrong username or password" | `SUPERADMIN_PASSWORD` only creates the account on the **first** start. If the account already exists, restart once with `SUPERADMIN_RESET=1 uvicorn app.main:app --port 8001`, log in, change the password, then restart without the reset. Values go without `< >` |
| First NSECM build: `missing inputs: bhavcopy` | A new local database has no previous-day closing prices yet. Run one fetch with `trade_date='<previous trading day YYYYMMDD>'` in `create_run(...)`, then a normal run |
| Port 8000 taken by a program that comes back after `kill` | Something on the Mac restarts it. Use port 8001 (`--port 8001`, `ENGINE_URL=http://localhost:8001`) |
| `Fetch run #N is already queued/running` | An earlier run never finished. Run `python -m app.fetch_job --run-id N` for that number |
| Fetch job: `401` / `403` from the engine | `FETCHER_TOKEN` differs between terminal 1 and terminal 2. Set the same value in both and restart the engine |
| Downloads fail with 403 / timeouts | The exchange or your network is blocking the requests. Office networks often need a proxy: set `HTTPS_PROXY` (ask IT for the value) and run again. Some exchange sites only allow Indian IP addresses |
| `Address already in use` | Something else is using port 8000. Use `--port 8001` and `ENGINE_URL=http://localhost:8001` |
| Calculator says no data | Run B3, then check B4 |
| Start completely fresh | Stop the engine and delete `engineFolder/data/`. You'll do the first login again |
| `python` not found (Windows) | Reinstall Python with "Add to PATH" ticked, or use `py -3.12` instead of `python` |

---

## Part F: Moving between local and staging
- **Code:** the code is identical. Local mode is chosen automatically when `DATABASE_URL` and `STORAGE_BACKEND`
  aren't set (SQLite + `data/archive`).
- **Data:** isn't copied either way. Staging keeps using Render + Supabase.
- **Staying up to date:** run `git pull` in `margin-calculator`, then `pip install -r requirements-dev.txt` again.
