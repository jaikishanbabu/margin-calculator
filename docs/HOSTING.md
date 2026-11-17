# Hosting: Render + Railway, pages on GitHub Pages

## Why (18 Nov 2026)
Render's free (Hobby) workspace includes **5 GB of outbound bandwidth a month**. When it runs out, Render
suspends every service in the workspace until the next billing month. Ours ran out because the engine was
sending exchange files out over and over:

* every file the fetcher downloaded went GitHub Actions → engine → Supabase Storage (engine → Supabase is outbound);
* every snapshot rebuild downloaded **all** input files back Supabase → engine → GitHub Actions, with no cache;
* the admin and calculator pages were served by the engine too.

## What changed
| Before | Now |
|---|---|
| Files pass through the engine | The job moves files to/from Supabase Storage over short-lived signed links. The engine still checks every file (same pipeline, same audit), reading the staged copy *into* Render, which is free, and copying inside storage |
| Every rebuild re-downloads every input | GitHub Actions keeps a cache of originals (by SHA-256, re-checked on read) |
| Uncompressed API responses | gzip |
| Pages served by the engine | GitHub Pages: `https://jaikishanbabu.github.io/margin-calculator/admin/` and `/calculator/` |
| One host | Render **and** Railway, same code and database. SuperAdmin picks the preferred host in **Admin → Hosting**; pages fall back to the other host automatically |

If a signed link ever fails, the job falls back to the old route through the engine for the rest of that run,
so fetching never stops because of this change.

## One-time setup (phone browser, ~15 min)

### 1. GitHub Pages
Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**. Then **Actions → "Pages (admin +
calculator)" → Run workflow** (branch `develop`).

### 2. Railway (engine, second host)
1. railway.com → sign in with GitHub → **New project → Deploy from GitHub repo → margin-calculator**.
2. Service → **Settings → Source → Branch: `develop`**. Railway reads `railway.json` (Dockerfile build, health check, sleep when idle).
3. Service → **Variables** → add these. Copy each value from Render → margin-engine-staging → Environment
   (dashboard to dashboard; never paste them in chat):

   | Name | Value |
   |---|---|
   | `APP_ENV` | `staging` |
   | `STORAGE_BACKEND` | `supabase` |
   | `SUPABASE_URL` | `https://nlubvmkdvrjgsvvxfvxm.supabase.co` |
   | `SUPABASE_BUCKET` | `margin-archive` |
   | `SUPERADMIN_USERNAME` | `superadmin` |
   | `GITHUB_REPO` | `jaikishanbabu/margin-calculator` |
   | `GITHUB_REF` | `develop` |
   | `CORS_ORIGINS` | `https://jaikishanbabu.github.io` |
   | `SERVE_STATIC` | `1` |
   | `DATABASE_URL` | copy from Render |
   | `SUPABASE_SERVICE_KEY` | copy from Render |
   | `SUPERADMIN_PASSWORD` | copy from Render |
   | `JWT_SECRET` | copy from Render (same value = one login works on both hosts) |
   | `OPS_TOKEN` | copy from Render (must match GitHub secret `STAGING_OPS_TOKEN` and the Supabase Vault secret) |
   | `FETCHER_TOKEN` | copy from Render (must match GitHub secret `STAGING_FETCHER_TOKEN`) |
   | `GITHUB_DISPATCH_TOKEN` | copy from Render |

   If Render won't show the values while suspended: generate new `JWT_SECRET`, `OPS_TOKEN`, `FETCHER_TOKEN` with
   your password manager (40+ letters/digits), and put the same `OPS_TOKEN` / `FETCHER_TOKEN` into the GitHub secrets
   `STAGING_OPS_TOKEN` / `STAGING_FETCHER_TOKEN` and, later, into Render.
4. Service → **Settings → Networking → Generate domain**. Copy the `https://….up.railway.app` address.

### 3. Tell GitHub about Railway
Repo → **Settings → Secrets and variables → Actions → Variables → New**: `STAGING_URL_ALT` = the Railway address
(no trailing slash). Re-run the **Pages** workflow so the pages know both hosts.

### 4. Prefer Railway until Render resets
Open the GitHub Pages admin → **Hosting** → **Make preferred** on Railway. Logged in the audit log.

### 5. Scheduler
The Supabase cron job now calls the preferred host (each host records its address at startup). Re-run
`engineFolder/ops/supabase_scheduler.sql` step 2 in the Supabase SQL editor (the Vault secret stays as it is).

## Railway free plan: the catch
A 30-day trial with $5 credit, then $1/month. Enough to bridge until Render's bandwidth resets; with the changes
above, Render's 5 GB should be plenty afterwards. Switch back in **Admin → Hosting** once Render is up.
