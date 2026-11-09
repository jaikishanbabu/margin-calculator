"""HTTP API (Starlette - the framework FastAPI is built on).

Run locally:  cd engineFolder && uvicorn app.main:app --reload
"""
import hmac
import json
import traceback
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import audit, auth, backup, config, db, fetcher, holidays, margin as margin_mod, readiness, segments, uploads, validation
from .fetcher_map import SEGMENT_CHOICES, SOURCE_MAP
from .storage import get_store

API_VERSION = "stage1"


# ---------------------------------------------------------------- helpers
def client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else None)


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


def err(msg, status=400):
    return JSONResponse({"error": msg}, status_code=status)


async def body_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def current_user(request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return auth.user_from_token(h[7:].strip())


def endpoint(*roles, ops=False, allow_pending_password=False, fetcher_job=False):
    """Wrap a handler: auth/role check + uniform error handling."""
    def deco(fn):
        async def wrapper(request: Request):
            try:
                if ops:
                    tok = request.headers.get("x-ops-token", "")
                    if not config.OPS_TOKEN or not hmac.compare_digest(tok, config.OPS_TOKEN):
                        return err("Forbidden", 403)
                    user = {"username": "ops-scheduler", "role": "ops"}
                elif fetcher_job:
                    tok = request.headers.get("x-fetcher-token", "")
                    if not config.FETCHER_TOKEN or not hmac.compare_digest(tok, config.FETCHER_TOKEN):
                        return err("Forbidden", 403)
                    user = {"username": "auto-fetcher", "role": "fetcher"}
                else:
                    user = await run_in_threadpool(current_user, request) if roles else None
                    if roles:
                        auth.require(user, *[r for r in roles if r != "any"])
                        if user["must_change_password"] and not allow_pending_password:
                            return err("Change your password before continuing (Account tab)", 403)
                return await fn(request, user)
            except auth.AuthError as e:
                return err(str(e), e.status)
            except (ValueError, uploads.UploadRejected, fetcher.FetchError) as e:
                return err(str(e), 400)
            except LookupError as e:
                return err(str(e), 404)
            except Exception as e:  # never leak internals
                traceback.print_exc()
                return err("Internal error", 500)
        return wrapper
    return deco


# ---------------------------------------------------------------- public
async def health(request):
    out = {"ok": True, "env": config.APP_ENV, "version": config.APP_VERSION, "api": API_VERSION}
    try:
        await run_in_threadpool(db.query_one, "SELECT 1 AS one")
        out["db"] = "ok"
    except Exception:
        out["ok"], out["db"] = False, "unreachable"
    if request.query_params.get("deep") == "1":
        try:
            await run_in_threadpool(get_store().list, "manifests/__health__")
            out["storage"] = "ok"
        except Exception:
            out["ok"], out["storage"] = False, "unreachable"
        try:
            out["backup"] = await run_in_threadpool(backup.status)
        except Exception:
            out["backup"] = None
    return ok(out, 200 if out["ok"] else 503)


async def public_status(request):
    return ok({"stage": 1, "calculator_published": False,
               "message": "The margin calculator is not published yet: no segment has passed validation.",
               "segments": [{"code": k, "name": v["name"], "validated": False} for k, v in segments.SEGMENTS.items()]})


# ---------------------------------------------------------------- auth
@endpoint()
async def login(request, _):
    b = await body_json(request)
    user = await run_in_threadpool(auth.login, b.get("username"), b.get("password"), client_ip(request))
    return ok({"token": auth.make_token(user), "user": auth.public_user(user), "expires_in_hours": config.TOKEN_HOURS})


@endpoint("any", allow_pending_password=True)
async def me(request, user):
    return ok({"user": auth.public_user(user)})


@endpoint("any", allow_pending_password=True)
async def change_password(request, user):
    b = await body_json(request)
    await run_in_threadpool(auth.change_own_password, user, b.get("current_password"), b.get("new_password"), client_ip(request))
    return ok({"ok": True, "message": "Password changed - please log in again"})


# ---------------------------------------------------------------- users (superadmin)
@endpoint("superadmin")
async def users_list(request, user):
    rows = await run_in_threadpool(db.query, "SELECT * FROM users ORDER BY id")
    return ok({"users": [auth.public_user(r) for r in rows]})


@endpoint("superadmin")
async def users_create(request, user):
    b = await body_json(request)
    u = await run_in_threadpool(auth.create_user, user, b.get("username"), b.get("password"), b.get("full_name", ""),
                                b.get("role", "admin"), client_ip(request))
    return ok({"user": u}, 201)


@endpoint("superadmin")
async def users_update(request, user):
    b = await body_json(request)
    uid = int(request.path_params["uid"])
    active = b.get("active")
    u = await run_in_threadpool(auth.update_user, user, uid, None if active is None else bool(active), b.get("password"),
                                b.get("full_name"), b.get("role"), bool(b.get("unlock")), client_ip(request))
    return ok({"user": u})


# ---------------------------------------------------------------- uploads (admin + superadmin)
@endpoint("admin", "superadmin")
async def meta(request, user):
    hints = {f"{seg}:{ft}": h for (seg, ft), h in segments.FILE_HINTS.items()}
    return ok({"segments": segments.SEGMENTS, "file_types": segments.FILE_TYPES, "segment_files": segments.SEGMENT_FILES,
               "file_hints": hints, "max_upload_mb": config.MAX_UPLOAD_MB, "min_password_length": auth.MIN_PASSWORD_LENGTH})


@endpoint("admin", "superadmin")
async def suggest(request, user):
    b = await body_json(request)
    return ok(uploads.suggest(str(b.get("filename", ""))[:300]))


@endpoint("admin", "superadmin")
async def upload_create(request, user):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > (config.MAX_UPLOAD_MB + 1) * 1024 * 1024:
        return err(f"File is larger than {config.MAX_UPLOAD_MB} MB", 413)
    form = await request.form(max_files=1, max_fields=10)
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        return err("No file attached")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks, size = [], 0
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            return err(f"File is larger than {config.MAX_UPLOAD_MB} MB", 413)
        chunks.append(chunk)
    data = b"".join(chunks)
    result = await run_in_threadpool(
        uploads.process, user, f.filename, data, str(form.get("segment", "")), str(form.get("file_type", "")),
        str(form.get("trade_date", "")) or None, str(form.get("cycle", "")) or None, client_ip(request),
        str(form.get("replace", "")).lower() in ("1", "true", "yes", "on"))
    return ok({"upload": result}, 201)


@endpoint("admin", "superadmin")
async def upload_list(request, user):
    seg = request.query_params.get("segment") or None
    rows = await run_in_threadpool(uploads.listing, seg, request.query_params.get("limit", 200))
    return ok({"uploads": rows})


@endpoint("admin", "superadmin")
async def upload_get(request, user):
    u = await run_in_threadpool(uploads.get, int(request.path_params["uid"]))
    if not u:
        return err("Upload not found", 404)
    return ok({"upload": u})


@endpoint("admin", "superadmin")
async def upload_withdraw(request, user):
    b = await body_json(request)
    u = await run_in_threadpool(uploads.mark_deleted, user, int(request.path_params["uid"]), b.get("reason", ""), client_ip(request))
    return ok({"upload": u})


@endpoint("admin", "superadmin")
async def upload_verify(request, user):
    r = await run_in_threadpool(uploads.verify_archive, int(request.path_params["uid"]))
    await run_in_threadpool(audit.log, user["username"], "archive_verified", r, client_ip(request))
    return ok(r)


# ---------------------------------------------------------------- audit
@endpoint("admin", "superadmin")
async def audit_list(request, user):
    limit = max(1, min(int(request.query_params.get("limit", 200)), 1000))
    before = request.query_params.get("before")
    if before and before.isdigit():
        rows = await run_in_threadpool(db.query, "SELECT * FROM audit_log WHERE seq < ? ORDER BY seq DESC LIMIT ?", (int(before), limit))
    else:
        rows = await run_in_threadpool(db.query, "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,))
    return ok({"entries": rows})


@endpoint("admin", "superadmin")
async def audit_verify(request, user):
    r = await run_in_threadpool(audit.verify)
    await run_in_threadpool(audit.log, user["username"], "audit_verified", {"ok": r["ok"], "checked": r["checked"]}, client_ip(request))
    return ok(r)


# ---------------------------------------------------------------- backups
@endpoint("admin", "superadmin")
async def backups_list(request, user):
    rows = await run_in_threadpool(db.query, "SELECT * FROM backup_runs ORDER BY id DESC LIMIT 50")
    for r in rows:
        try:
            r["details"] = json.loads(r["details"]) if r["details"] else None
        except Exception:
            pass
    return ok({"runs": rows, "status": await run_in_threadpool(backup.status)})


@endpoint("superadmin")
async def backups_run(request, user):
    return ok(await run_in_threadpool(backup.backup_export, user["username"]))


@endpoint("superadmin")
async def backups_restore_test(request, user):
    return ok(await run_in_threadpool(backup.restore_test, user["username"]))


@endpoint("superadmin")
async def settings_update(request, user):
    b = await body_json(request)
    allowed = {"backup_retention_count": (int, 2, 520)}
    changed = {}
    for k, v in b.items():
        if k not in allowed:
            raise ValueError(f"Unknown setting {k}")
        typ, lo, hi = allowed[k]
        v = typ(v)
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        await run_in_threadpool(db.set_setting, k, v)
        changed[k] = v
    await run_in_threadpool(audit.log, user["username"], "settings_changed", changed, client_ip(request))
    return ok({"settings": changed})


@endpoint("admin", "superadmin")
async def settings_get(request, user):
    return ok({"settings": {"backup_retention_count": await run_in_threadpool(db.get_setting, "backup_retention_count", backup.DEFAULT_BACKUP_RETENTION)}})


# ---------------------------------------------------------------- ops (scheduled GitHub jobs)
@endpoint(ops=True)
async def ops_backup(request, user):
    r = await run_in_threadpool(backup.backup_export, user["username"])
    return ok(r, 200 if r["ok"] else 500)


@endpoint(ops=True)
async def ops_restore_test(request, user):
    r = await run_in_threadpool(backup.restore_test, user["username"])
    return ok(r, 200 if r["ok"] else 500)


@endpoint(ops=True)
async def ops_probe_start(request, user):
    from datetime import date as _date
    from . import probe
    d = request.query_params.get("date", "")
    try:
        day = _date.fromisoformat(d)
    except ValueError:
        return err("date=YYYY-MM-DD required")
    started = probe.start_background(day)
    await run_in_threadpool(audit.log, user["username"], "exchange_probe_started", {"date": d, "started": started})
    return ok({"started": started, "status": probe.status()["running"]}, 202)


@endpoint(ops=True)
async def ops_probe_status(request, user):
    from . import probe
    return ok(probe.status())


# ---------------------------------------------------------------- exchange file fetcher (admin page)
@endpoint("admin", "superadmin")
async def fetcher_overview(request, user):
    def work():
        s = fetcher.settings()
        nd = fetcher.next_due(s)
        return {"settings": s, "frequencies": fetcher.FREQUENCIES, "segments": SEGMENT_CHOICES,
                "segment_names": {k: segments.SEGMENTS[k]["name"] for k in SEGMENT_CHOICES},
                "active": fetcher.active_run(), "runs": fetcher.list_runs(15),
                "next_due": nd.isoformat(timespec="minutes") if nd else None,
                "configured": bool(config.GITHUB_DISPATCH_TOKEN and config.FETCHER_TOKEN),
                "can_edit": user["role"] == "superadmin"}
    return ok(await run_in_threadpool(work))


@endpoint("superadmin")
async def fetcher_settings_update(request, user):
    b = await body_json(request)
    return ok({"settings": await run_in_threadpool(fetcher.update_settings, user, b, client_ip(request))})


@endpoint("admin", "superadmin")
async def fetcher_run_manual(request, user):
    b = await body_json(request)
    seg = str(b.get("segment") or "ALL").upper()
    run = await run_in_threadpool(fetcher.create_run, "manual", user["username"], [seg], None, client_ip(request))
    return ok({"run": run}, 202)


@endpoint("admin", "superadmin")
async def fetcher_run_get(request, user):
    r = await run_in_threadpool(fetcher.get_run, int(request.path_params["rid"]))
    if not r:
        return err("Run not found", 404)
    return ok({"run": r})


# ---------------------------------------------------------------- data readiness (Stage 2C)
@endpoint("admin", "superadmin")
async def readiness_overview(request, user):
    def work():
        return {"segments": readiness.readiness(), "basis": readiness.basis_settings(),
                "segment_names": {k: segments.SEGMENTS[k]["name"] for k in readiness.ALL_SEGMENTS},
                "active": fetcher.active_run(), "can_edit": user["role"] == "superadmin",
                "validation": {seg: {k: v for k, v in validation.status(seg).items() if k != "latest"} |
                               {"latest": validation.status(seg)["latest"]} for seg in validation.SEGMENTS}}
    return ok(await run_in_threadpool(work))


@endpoint("superadmin")
async def readiness_basis(request, user):
    b = await body_json(request)
    return ok({"basis": await run_in_threadpool(readiness.set_basis, user, str(b.get("segment", "")).upper(),
                                                str(b.get("basis", "")), client_ip(request))})


@endpoint("admin", "superadmin")
async def readiness_rebuild(request, user):
    b = await body_json(request)
    seg = str(b.get("segment") or "ALL").upper()
    if seg != "ALL" and seg not in readiness.PARSERS:
        return err("This segment has no parser yet")
    run = await run_in_threadpool(fetcher.create_run, "rebuild", user["username"], [seg], None, client_ip(request))
    return ok({"run": run}, 202)


@endpoint("admin", "superadmin")
async def snapshot_get(request, user):
    s = await run_in_threadpool(readiness.get_snapshot, int(request.path_params["sid"]))
    if not s:
        return err("Snapshot not found", 404)
    return ok({"snapshot": s})


# ---------------------------------------------------------------- margins (Stage 3, public)
async def margin_api(request):
    ip = client_ip(request)
    if not margin_mod.limiter.allow(ip):
        return err("Too many requests - wait a minute", 429)
    b = await body_json(request)
    try:
        res = await run_in_threadpool(margin_mod.margin, str(b.get("segment") or "NFO").upper(), b.get("positions"))
    except margin_mod.MarginError as e:
        return err(str(e))
    except Exception as e:                       # never leak internals on the public endpoint
        print(f"margin error: {type(e).__name__}: {e}")
        return err("Couldn't work out the margin for this basket", 500)
    return ok(res)


# ---------------------------------------------------------------- validation against brokers (Stage 3)
@endpoint("admin", "superadmin")
async def validation_overview(request, user):
    seg = request.query_params.get("segment", "NFO").upper()
    def work():
        st = validation.status(seg)
        vid = request.query_params.get("id")
        report = validation.get(int(vid)) if vid else (validation.latest(seg, with_report=True) if st["latest"] else None)
        return {"segment": seg, "status": st, "history": validation.history(seg), "report": report,
                "active": fetcher.active_run()}
    return ok(await run_in_threadpool(work))


@endpoint("admin", "superadmin")
async def validation_run(request, user):
    b = await body_json(request)
    seg = str(b.get("segment") or "NFO").upper()
    if seg not in validation.SEGMENTS:
        return err("No broker baskets for this segment yet")
    run = await run_in_threadpool(fetcher.create_run, "validate", user["username"], [seg], None, client_ip(request))
    return ok({"run": run}, 202)


# ---------------------------------------------------------------- holiday calendar
@endpoint("admin", "superadmin")
async def calendar_overview(request, user):
    try:
        year = int(request.query_params.get("year") or fetcher.now_ist().year)
    except ValueError:
        return err("Bad year")
    data = await run_in_threadpool(holidays.overview, year)
    data["can_edit"] = user["role"] == "superadmin"
    data["active"] = await run_in_threadpool(fetcher.active_run)
    return ok(data)


@endpoint("superadmin")
async def calendar_day(request, user):
    b = await body_json(request)
    ex, day = str(b.get("exchange", "")), str(b.get("day", ""))
    if request.method == "DELETE":
        await run_in_threadpool(holidays.remove_day, user, ex, day, client_ip(request))
    else:
        await run_in_threadpool(holidays.set_day, user, ex, day, str(b.get("kind", "")),
                                str(b.get("description", "")), client_ip(request))
    return ok({"saved": True})


@endpoint("admin", "superadmin")
async def calendar_import(request, user):
    run = await run_in_threadpool(fetcher.create_run, "calendar", user["username"], ["ALL"], None, client_ip(request))
    return ok({"run": run}, 202)


@endpoint("superadmin")
async def calendar_decide(request, user):
    b = await body_json(request)
    ex, year = str(b.get("exchange", "")), int(b.get("year") or 0)
    if request.path_params["action"] not in ("approve", "discard"):
        return err("Not found", 404)
    if request.path_params["action"] == "approve":
        return ok(await run_in_threadpool(holidays.approve, user, ex, year, client_ip(request)))
    await run_in_threadpool(holidays.discard, user, ex, year, client_ip(request))
    return ok({"discarded": True})


# ---------------------------------------------------------------- scheduler tick (Supabase pg_cron, OPS token)
@endpoint(ops=True)
async def ops_fetch_tick(request, user):
    return ok(await run_in_threadpool(fetcher.tick))


# ---------------------------------------------------------------- fetcher job (GitHub Actions, FETCHER token)
@endpoint(fetcher_job=True)
async def job_start(request, user):
    b = await body_json(request)
    return ok(await run_in_threadpool(fetcher.job_started, int(request.path_params["rid"]), b.get("github_run_id")))


@endpoint(fetcher_job=True)
async def job_have(request, user):
    td = request.query_params.get("trade_date", "")
    if not (len(td) == 8 and td.isdigit()):
        return err("trade_date=YYYYMMDD required")
    return ok({"have": await run_in_threadpool(fetcher.have, td)})


@endpoint(fetcher_job=True)
async def job_items(request, user):
    b = await body_json(request)
    await run_in_threadpool(fetcher.report_items, int(request.path_params["rid"]), b.get("items") or [])
    return ok({"ok": True})


@endpoint(fetcher_job=True)
async def job_finish(request, user):
    b = await body_json(request)
    return ok({"run": await run_in_threadpool(fetcher.job_finished, int(request.path_params["rid"]),
                                              b.get("status", "done"), b.get("message", ""))})


@endpoint(fetcher_job=True)
async def job_upload(request, user):
    form = await request.form(max_files=1, max_fields=12)
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        return err("No file attached")
    rid = int(request.path_params["rid"])
    run = await run_in_threadpool(fetcher.get_run, rid, False)
    if not run or run["status"] != "running":
        return err("Run is not running", 409)
    sid = str(form.get("source_id", ""))
    if sid not in SOURCE_MAP:
        return err("Unknown source")
    segment, file_type = SOURCE_MAP[sid]
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks, size = [], 0
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            return err(f"File is larger than {config.MAX_UPLOAD_MB} MB", 413)
        chunks.append(chunk)
    actor = {"username": "auto-fetcher" if run["trigger"] == "auto" else f"fetcher ({run['requested_by']})"}
    result = await run_in_threadpool(
        uploads.process, actor, f.filename, b"".join(chunks), segment, file_type,
        fetcher.upload_trade_date(run, str(form.get("trade_date", ""))), str(form.get("cycle", "")) or None, client_ip(request),
        str(form.get("replace", "")) == "1", str(form.get("source_ref", ""))[:120] or None,
        {"url": str(form.get("url", ""))[:400], "run_id": rid})
    return ok({"upload": result}, 201)


@endpoint(fetcher_job=True)
async def job_trade_date(request, user):
    b = await body_json(request)
    return ok(await run_in_threadpool(fetcher.job_move_trade_date, int(request.path_params["rid"]), b.get("trade_date", "")))


@endpoint(fetcher_job=True)
async def job_calendar(request, user):
    return ok(await run_in_threadpool(lambda: {"entries": holidays.dump(), "sources": holidays.sources()}))


@endpoint(fetcher_job=True)
async def job_calendar_proposal(request, user):
    rid = int(request.path_params["rid"])
    run = await run_in_threadpool(fetcher.get_run, rid, False)
    if not run or run["status"] != "running" or run["trigger"] != "calendar":
        return err("Not a running calendar import", 409)
    b = await body_json(request)
    found = b.get("found") if isinstance(b.get("found"), list) else []
    p = await run_in_threadpool(holidays.save_proposal, rid, str(b.get("exchange", "")), int(b.get("year") or 0),
                                found[:400], str(b.get("source", ""))[:300], (str(b["error"])[:500] if b.get("error") else None))
    return ok({"proposal": p})


@endpoint(fetcher_job=True)
async def job_validation(request, user):
    rid = int(request.path_params["rid"])
    run = await run_in_threadpool(fetcher.get_run, rid, False)
    if not run or run["status"] != "running" or run["trigger"] != "validate":
        return err("Not a running validation", 409)
    b = await body_json(request)
    v = await run_in_threadpool(validation.record, rid, str(b.get("segment", "")).upper(), b.get("report"))
    return ok({"validation": v}, 201)


@endpoint(fetcher_job=True)
async def job_build_inputs(request, user):
    return ok(await run_in_threadpool(readiness.build_inputs, request.query_params.get("segment", "").upper()))


@endpoint(fetcher_job=True)
async def job_original(request, user):
    name, data = await run_in_threadpool(readiness.original, int(request.path_params["uid"]))
    return Response(data, media_type="application/octet-stream",
                    headers={"x-filename": uploads.safe_filename(name)})


@endpoint(fetcher_job=True)
async def job_snapshot(request, user):
    rid = int(request.path_params["rid"])
    run = await run_in_threadpool(fetcher.get_run, rid, False)
    if not run or run["status"] != "running":
        return err("Run is not running", 409)
    form = await request.form(max_files=1, max_fields=8)
    try:
        meta = json.loads(str(form.get("meta", "{}")))
        assert isinstance(meta, dict)
    except Exception:
        return err("meta must be a JSON object")
    if str(form.get("unchanged", "")) == "1":
        await run_in_threadpool(readiness.mark_unchanged, str(form.get("segment", "")).upper(),
                                str(form.get("basis", "")), meta.get("fingerprint", ""))
        return ok({"unchanged": True})
    blob = None
    f = form.get("file")
    if f is not None and hasattr(f, "read"):
        limit = readiness.MAX_SNAPSHOT_MB * 1024 * 1024
        chunks, size = [], 0
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                return err(f"Snapshot is larger than {readiness.MAX_SNAPSHOT_MB} MB", 413)
            chunks.append(chunk)
        blob = b"".join(chunks)
    snap = await run_in_threadpool(readiness.record, rid, str(form.get("segment", "")).upper(),
                                   str(form.get("basis", "")), meta, blob)
    return ok({"snapshot": snap}, 201)


# ---------------------------------------------------------------- app
class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                for k, v in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                                                 b"font-src https://fonts.gstatic.com; img-src 'self' data:; "
                                                 b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                    (b"cache-control", b"no-store"),
                ):
                    h.append((k, v))
                if not config.IS_DEV:
                    h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
            await send(message)

        await self.app(scope, receive, send_wrapped)


@asynccontextmanager
async def lifespan(app):
    config.validate()
    db.init()
    auth.ensure_superadmin()
    yield


routes = [
    Route("/api/health", health),
    Route("/api/public/status", public_status),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/me", me),
    Route("/api/auth/password", change_password, methods=["POST"]),
    Route("/api/admin/users", users_list, methods=["GET"]),
    Route("/api/admin/users", users_create, methods=["POST"]),
    Route("/api/admin/users/{uid:int}", users_update, methods=["PATCH"]),
    Route("/api/admin/meta", meta),
    Route("/api/admin/suggest", suggest, methods=["POST"]),
    Route("/api/admin/uploads", upload_list, methods=["GET"]),
    Route("/api/admin/uploads", upload_create, methods=["POST"]),
    Route("/api/admin/uploads/{uid:int}", upload_get),
    Route("/api/admin/uploads/{uid:int}/withdraw", upload_withdraw, methods=["POST"]),
    Route("/api/admin/uploads/{uid:int}/verify", upload_verify, methods=["POST"]),
    Route("/api/admin/audit", audit_list),
    Route("/api/admin/audit/verify", audit_verify),
    Route("/api/admin/backups", backups_list),
    Route("/api/admin/backups/run", backups_run, methods=["POST"]),
    Route("/api/admin/backups/restore-test", backups_restore_test, methods=["POST"]),
    Route("/api/admin/settings", settings_get, methods=["GET"]),
    Route("/api/admin/settings", settings_update, methods=["PUT"]),
    Route("/api/ops/backup", ops_backup, methods=["POST"]),
    Route("/api/ops/restore-test", ops_restore_test, methods=["POST"]),
    Route("/api/ops/probe-exchanges", ops_probe_start, methods=["POST"]),
    Route("/api/ops/fetch-tick", ops_fetch_tick, methods=["POST"]),
    Route("/api/admin/fetcher", fetcher_overview),
    Route("/api/admin/fetcher/settings", fetcher_settings_update, methods=["PUT"]),
    Route("/api/admin/fetcher/run", fetcher_run_manual, methods=["POST"]),
    Route("/api/admin/fetcher/runs/{rid:int}", fetcher_run_get),
    Route("/api/fetcher/runs/{rid:int}/start", job_start, methods=["POST"]),
    Route("/api/fetcher/have", job_have),
    Route("/api/fetcher/runs/{rid:int}/items", job_items, methods=["POST"]),
    Route("/api/fetcher/runs/{rid:int}/upload", job_upload, methods=["POST"]),
    Route("/api/fetcher/build/inputs", job_build_inputs),
    Route("/api/fetcher/calendar", job_calendar),
    Route("/api/fetcher/runs/{rid:int}/validation", job_validation, methods=["POST"]),
    Route("/api/admin/validation", validation_overview),
    Route("/api/margin", margin_api, methods=["POST"]),
    Route("/api/admin/validation/run", validation_run, methods=["POST"]),
    Route("/api/fetcher/runs/{rid:int}/calendar-proposal", job_calendar_proposal, methods=["POST"]),
    Route("/api/admin/calendar", calendar_overview),
    Route("/api/admin/calendar/day", calendar_day, methods=["PUT", "DELETE"]),
    Route("/api/admin/calendar/import", calendar_import, methods=["POST"]),
    Route("/api/admin/calendar/{action:str}", calendar_decide, methods=["POST"]),
    Route("/api/fetcher/runs/{rid:int}/trade-date", job_trade_date, methods=["POST"]),
    Route("/api/fetcher/uploads/{uid:int}/original", job_original),
    Route("/api/fetcher/runs/{rid:int}/snapshot", job_snapshot, methods=["POST"]),
    Route("/api/admin/readiness", readiness_overview),
    Route("/api/admin/readiness/basis", readiness_basis, methods=["PUT"]),
    Route("/api/admin/readiness/rebuild", readiness_rebuild, methods=["POST"]),
    Route("/api/admin/snapshots/{sid:int}", snapshot_get),
    Route("/api/fetcher/runs/{rid:int}/finish", job_finish, methods=["POST"]),
    Route("/api/ops/probe-exchanges", ops_probe_status, methods=["GET"]),
]

if config.SERVE_STATIC:
    routes += [
        Route("/", lambda r: RedirectResponse("/admin/")),
        Mount("/admin", StaticFiles(directory=str(config.REPO_DIR / "adminFolder"), html=True), name="admin"),
        Mount("/calculator", StaticFiles(directory=str(config.REPO_DIR / "marginCalc"), html=True), name="calc"),
    ]

middleware = [Middleware(SecurityHeaders)]
if config.CORS_ORIGINS:
    middleware.append(Middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST", "PATCH", "PUT"],
                                 allow_headers=["Authorization", "Content-Type"], max_age=600))

app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
