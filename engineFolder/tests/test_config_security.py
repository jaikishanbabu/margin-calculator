import re
import subprocess
from pathlib import Path

import pytest

from app import config

REPO = Path(__file__).resolve().parents[2]


def _set(monkeypatch, **kv):
    base = dict(APP_ENV="staging", IS_DEV=False, JWT_SECRET="j" * 40, SUPERADMIN_PASSWORD="S" * 16, OPS_TOKEN="o" * 40,
                DATABASE_URL="postgresql://x", STORAGE_BACKEND="supabase", SUPABASE_URL="https://x.supabase.co",
                SUPABASE_SERVICE_KEY="k", CORS_ORIGINS=[])
    base.update(kv)
    for k, v in base.items():
        monkeypatch.setattr(config, k, v)


def test_valid_staging_config_passes(monkeypatch):
    _set(monkeypatch)
    config.validate()


@pytest.mark.parametrize("override,msg", [
    ({"JWT_SECRET": "short"}, "JWT_SECRET"),
    ({"SUPERADMIN_PASSWORD": "weak"}, "SUPERADMIN_PASSWORD"),
    ({"OPS_TOKEN": ""}, "OPS_TOKEN"),
    ({"DATABASE_URL": ""}, "DATABASE_URL"),
    ({"STORAGE_BACKEND": "local"}, "ephemeral"),
    ({"SUPABASE_SERVICE_KEY": ""}, "SUPABASE"),
    ({"CORS_ORIGINS": ["*"]}, "CORS"),
])
def test_unsafe_staging_config_refuses_to_start(monkeypatch, override, msg):
    _set(monkeypatch, **override)
    with pytest.raises(config.ConfigError, match=msg):
        config.validate()


SECRET_PATTERNS = [
    r"sb_secret_[A-Za-z0-9_-]{10,}",                          # Supabase secret keys
    r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.eyJpc3MiOiJzdXBhYmFzZ",  # legacy Supabase service JWT
    r"postgres(ql)?://[^\s:@/]+:[^\s@/]{6,}@",                # DB URL with a password
    r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",
    r"ghp_[A-Za-z0-9]{30,}", r"rnd_[A-Za-z0-9]{20,}",          # GitHub / Render tokens
    r"GOCSPX-[A-Za-z0-9_-]{20,}",                             # Google OAuth client secret
]


TEST_ONLY_VALUES = ("ci-owner-pass", "ci-app-pass")  # disposable CI container passwords


def test_no_secrets_committed():
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    hits = []
    for f in files:
        p = REPO / f
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        text = p.read_text(errors="ignore")
        for v in TEST_ONLY_VALUES:
            text = text.replace(v, "")
        for pat in SECRET_PATTERNS:
            if re.search(pat, text):
                hits.append(f"{f}: {pat}")
    assert not hits, hits


def test_frontends_contain_no_credentials():
    for folder in ("adminFolder", "marginCalc"):
        for p in (REPO / folder).rglob("*"):
            if p.is_file():
                t = p.read_text(errors="ignore").lower()
                for word in ("superadmin_password", "service_key", "jwt_secret", "ops_token", "database_url"):
                    assert word not in t, f"{p}: mentions {word}"


def test_env_files_are_gitignored():
    gi = (REPO / ".gitignore").read_text()
    assert ".env" in gi and "data/" in gi


def test_security_headers(client):
    r = client.get("/api/health")
    for h in ("content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy"):
        assert h in r.headers
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_errors_do_not_leak_internals(client, monkeypatch):
    from app import uploads
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /etc/x")
    monkeypatch.setattr(uploads, "listing", boom)
    from conftest import login
    r = client.get("/api/admin/uploads", headers=login(client))
    assert r.status_code == 500 and "secret" not in r.text


def test_no_cors_by_default(client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_health_exposes_nothing_sensitive(client, monkeypatch):
    from app import db as _db
    r = client.get("/api/health?deep=1")
    body = r.text
    assert set(r.json()) <= {"ok", "env", "version", "api", "db", "storage", "backup"}
    for secret in (config.SUPERADMIN_PASSWORD, config.OPS_TOKEN, "pbkdf2", "postgres", "Traceback"):
        assert secret not in body
    # even when the database is down, only a status word comes back
    monkeypatch.setattr(_db, "query_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("password=hunter2 host=db")))
    r = client.get("/api/health")
    assert r.status_code == 503 and "hunter2" not in r.text and r.json()["db"] == "unreachable"


def test_secrets_never_printed():
    import re as _re
    for p in (REPO / "engineFolder" / "app").rglob("*.py"):
        for line in p.read_text().splitlines():
            if "print(" in line:
                assert not _re.search(r"PASSWORD\)|TOKEN\)|SECRET\)|SERVICE_KEY\)|DATABASE_URL\)", line), f"{p}: {line}"


def test_probe_endpoint_requires_ops_token(client):
    assert client.post("/api/ops/probe-exchanges?date=2026-11-06").status_code == 403
    assert client.get("/api/ops/probe-exchanges").status_code == 403
    r = client.post("/api/ops/probe-exchanges?date=bad", headers={"X-Ops-Token": config.OPS_TOKEN})
    assert r.status_code == 400


def test_probe_only_contacts_configured_exchange_hosts():
    import yaml, re
    cfg = yaml.safe_load((REPO / "engineFolder" / "exfetch_sources.yaml").read_text())
    hosts = set()
    for s in cfg["sources"]:
        for st in s.get("try", []):
            for v in st.values():
                url = v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None)
                if url and url.startswith("http"):
                    hosts.add(re.match(r"https?://([^/]+)", url).group(1))
    allowed = {"nsearchives.nseindia.com", "www.nseindia.com", "api.bseindia.com", "www.bseindia.com",
               "www.mcxccl.com", "www.mcxindia.com", "images.dhan.co"}
    assert hosts <= allowed, hosts - allowed
