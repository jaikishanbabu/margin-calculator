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

from . import audit, auth, backup, config, db, segments, uploads
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


def endpoint(*roles, ops=False, allow_pending_password=False):
    """Wrap a handler: auth/role check + uniform error handling."""
    def deco(fn):
        async def wrapper(request: Request):
            try:
                if ops:
                    tok = request.headers.get("x-ops-token", "")
                    if not config.OPS_TOKEN or not hmac.compare_digest(tok, config.OPS_TOKEN):
                        return err("Forbidden", 403)
                    user = {"username": "ops-scheduler", "role": "ops"}
                else:
                    user = await run_in_threadpool(current_user, request) if roles else None
                    if roles:
                        auth.require(user, *[r for r in roles if r != "any"])
                        if user["must_change_password"] and not allow_pending_password:
                            return err("Change your password before continuing (Account tab)", 403)
                return await fn(request, user)
            except auth.AuthError as e:
                return err(str(e), e.status)
            except (ValueError, uploads.UploadRejected) as e:
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
