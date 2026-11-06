import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

os.environ["APP_ENV"] = "test"
os.environ.setdefault("SUPERADMIN_PASSWORD", "Test-Super-Pass-123!")
os.environ.setdefault("OPS_TOKEN", "test-ops-token-0123456789abcdef0123456789")
# Postgres mode (CI): TEST_PG_OWNER_URL = schema owner, TEST_PG_APP_URL = restricted margin_app login
PG_OWNER = os.environ.get("TEST_PG_OWNER_URL", "")
PG_APP = os.environ.get("TEST_PG_APP_URL", "")
os.environ["DATABASE_URL"] = PG_APP if (PG_OWNER and PG_APP) else ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, config, db, storage  # noqa: E402
from app.storage.local import LocalStore  # noqa: E402

SUPER_PW = os.environ["SUPERADMIN_PASSWORD"]


MIGR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"


def _pg_reset():
    import psycopg2
    conn = psycopg2.connect(PG_OWNER)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS margin CASCADE")
    for f in sorted(MIGR.glob("*.sql")):
        cur.execute(f.read_text())
    conn.close()
    if db._pg_pool is not None:
        db._pg_pool.closeall()
        db._pg_pool = None


def owner_tamper(sql):
    """Simulate someone with OWNER rights editing the audit log behind the app's back
    (they must first switch the protection off)."""
    if db.is_pg():
        import psycopg2
        conn = psycopg2.connect(PG_OWNER)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path TO margin")
        cur.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_update")
        cur.execute(sql)
        cur.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_update")
        conn.close()
    else:
        import sqlite3
        conn = sqlite3.connect(config.SQLITE_PATH)
        conn.executescript("DROP TRIGGER IF EXISTS audit_no_update; DROP TRIGGER IF EXISTS audit_no_delete;" + sql)
        conn.commit()
        conn.close()


def app_exec(sql):
    """Run SQL with the APP's own credentials (what a compromised app could do)."""
    with db.tx() as c:
        c.execute(sql)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    if db.is_pg():
        _pg_reset()
    monkeypatch.setattr(config, "SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "LOCAL_STORE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(config, "STORAGE_BACKEND", "local")
    storage.set_store(LocalStore(tmp_path / "archive"))
    auth.reset_throttle()
    CHANGED.clear()
    db.init()
    auth.ensure_superadmin()
    yield tmp_path
    storage.set_store(None)


@pytest.fixture()
def client(env):
    from starlette.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


CHANGED = {}  # username -> password after the forced first-login change (per test)


def changed_password(old):
    return "Rotated-" + old[::-1] + "-9xZ"


def login(client, username="superadmin", password=SUPER_PW, complete_first_login=True):
    """Log in; if a password change is required, do it (like a real first login) and log in again."""
    password = CHANGED.get((username, password), password)
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    h = {"Authorization": "Bearer " + r.json()["token"]}
    if complete_first_login and r.json()["user"]["must_change_password"]:
        new = changed_password(password)
        rc = client.post("/api/auth/password", json={"current_password": password, "new_password": new}, headers=h)
        assert rc.status_code == 200, rc.text
        CHANGED[(username, password)] = new
        r = client.post("/api/auth/login", json={"username": username, "password": new})
        h = {"Authorization": "Bearer " + r.json()["token"]}
    return h


def make_admin(client, sa_headers, username="alice", password="Upload-Desk-2026!"):
    r = client.post("/api/admin/users", json={"username": username, "password": password, "full_name": "Alice"}, headers=sa_headers)
    assert r.status_code == 201, r.text
    return login(client, username, password)


# ---------------------------------------------------------------- sample files
def span_xml(date="20261104", is_setl="0", created="20261104083012", extra_pit="", n_opts=3):
    opts = "".join(
        f"<opt><cId>{100+i}</cId><o>C</o><k>{24000+50*i}</k><p>{120-i}.5</p><d>0.5</d>"
        f"<ra>{''.join(f'<a>{(-1)**j * (10+j+i)}.25</a>' for j in range(16))}<d>0.5</d></ra></opt>"
        for i in range(n_opts))
    return (
        f"<?xml version=\"1.0\"?><spanFile><fileFormat>4.00</fileFormat><created>{created}</created>"
        f"<pointInTime><date>{date}</date><isSetl>{is_setl}</isSetl>{extra_pit}"
        f"<clearingOrg><ec>NSCCL</ec><exchange><exch>NSE</exch>"
        f"<oopPf><pfId>1</pfId><pfCode>NIFTY</pfCode><series><pe>20261125</pe><cvf>1</cvf>{opts}</series></oopPf>"
        f"</exchange></clearingOrg></pointInTime></spanFile>"
    ).encode()


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def upload(client, headers, filename, data, segment="NFO", file_type="span", trade_date="", cycle="", replace=False):
    return client.post("/api/admin/uploads", headers=headers,
                       data={"segment": segment, "file_type": file_type, "trade_date": trade_date, "cycle": cycle,
                             "replace": "1" if replace else ""},
                       files={"file": (filename, data, "application/octet-stream")})
