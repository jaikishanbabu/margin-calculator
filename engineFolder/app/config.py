"""Runtime configuration. Every secret comes from environment variables
(Render / GitHub secret stores). Nothing secret is ever committed to the repo.

  APP_ENV                 dev | test | staging | production   (default dev)
  DATABASE_URL            Postgres URL of the *restricted* app role (margin_app). Empty = local SQLite.
  SQLITE_PATH             SQLite file for dev/test (default engineFolder/data/margin.db)
  JWT_SECRET              >= 32 random chars, signs admin login tokens
  SUPERADMIN_USERNAME     default "superadmin"
  SUPERADMIN_PASSWORD     >= 14 chars outside dev/test. BOOTSTRAP ONLY: creates the SuperAdmin once,
                          who must change it at first login; afterwards it no longer works.
  SUPERADMIN_RESET        set to 1 (temporarily) to re-apply SUPERADMIN_PASSWORD after a lock-out;
                          audited, forces another password change; remove it again afterwards
  OPS_TOKEN               >= 32 chars; lets the scheduled GitHub jobs trigger backups / restore tests
  STORAGE_BACKEND         local | supabase            (gdrive arrives in Stage 2)
  LOCAL_STORE_DIR         folder for the local backend (dev/test only)
  SUPABASE_URL            https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY    Supabase secret/service key (storage only; DB tables are not exposed to it)
  SUPABASE_BUCKET         private bucket name (default margin-archive)
  CORS_ORIGINS            comma-separated allowed origins (default: none = same-origin only)
  MAX_UPLOAD_MB           default 60
  SERVE_STATIC            1 = also serve adminFolder/ and marginCalc/ (used on staging)
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # engineFolder/
REPO_DIR = BASE_DIR.parent


def _env(name, default=""):
    return os.getenv(name, default).strip()


APP_ENV = _env("APP_ENV", "dev").lower()
APP_VERSION = _env("RENDER_GIT_COMMIT") or _env("APP_VERSION") or "dev"

DATABASE_URL = _env("DATABASE_URL")
SQLITE_PATH = _env("SQLITE_PATH") or str(BASE_DIR / "data" / "margin.db")

JWT_SECRET = _env("JWT_SECRET")
SUPERADMIN_USERNAME = (_env("SUPERADMIN_USERNAME") or "superadmin").lower()
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "").strip()   # bootstrap only (stray spaces/newlines from copy-paste removed): creates the account once
SUPERADMIN_RESET = _env("SUPERADMIN_RESET") == "1"           # break-glass: re-apply SUPERADMIN_PASSWORD on start
OPS_TOKEN = _env("OPS_TOKEN")

STORAGE_BACKEND = (_env("STORAGE_BACKEND") or "local").lower()
LOCAL_STORE_DIR = _env("LOCAL_STORE_DIR") or str(BASE_DIR / "data" / "archive")
SUPABASE_URL = _env("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = _env("SUPABASE_BUCKET") or "margin-archive"

CORS_ORIGINS = [o.strip() for o in _env("CORS_ORIGINS").split(",") if o.strip()]
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB") or 60)
SERVE_STATIC = _env("SERVE_STATIC") == "1"

TOKEN_HOURS = int(_env("TOKEN_HOURS") or 8)
LOCKOUT_THRESHOLD = 5          # failed logins before lockout
LOCKOUT_MINUTES = 15
IP_LOGIN_LIMIT_PER_MIN = 10

# Archive extraction limits (zip bombs, nested archives, traversal)
ZIP_MAX_MEMBERS = 20
ZIP_MAX_TOTAL_UNCOMPRESSED_MB = 1024
ZIP_MAX_RATIO = 250

IS_DEV = APP_ENV in ("dev", "test")


class ConfigError(RuntimeError):
    pass


def validate():
    """Refuse to start staging/production with weak or missing secrets."""
    if IS_DEV:
        return
    problems = []
    if len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET must be at least 32 characters")
    if len(SUPERADMIN_PASSWORD) < 14:
        problems.append("SUPERADMIN_PASSWORD must be at least 14 characters")
    if len(OPS_TOKEN) < 32:
        problems.append("OPS_TOKEN must be at least 32 characters")
    if not DATABASE_URL:
        problems.append("DATABASE_URL is required outside dev (Render's disk is wiped on every restart)")
    if STORAGE_BACKEND == "local":
        problems.append("STORAGE_BACKEND=local is not allowed outside dev (Render's disk is ephemeral)")
    if STORAGE_BACKEND == "supabase" and not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        problems.append("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for STORAGE_BACKEND=supabase")
    if "*" in CORS_ORIGINS:
        problems.append("CORS_ORIGINS=* is not allowed outside dev")
    if problems:
        raise ConfigError("; ".join(problems))
