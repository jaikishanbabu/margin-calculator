"""SuperAdmin bootstrap password: usable once, must be changed, never re-applied (except break-glass)."""
from app import auth, config, db
from conftest import SUPER_PW, changed_password, login


def _raw_login(client, pw, user="superadmin"):
    return client.post("/api/auth/login", json={"username": user, "password": pw})


def test_bootstrap_login_requires_password_change(client):
    r = _raw_login(client, SUPER_PW)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    h = {"Authorization": "Bearer " + r.json()["token"]}
    for path in ["/api/admin/users", "/api/admin/uploads", "/api/admin/audit", "/api/admin/backups"]:
        assert client.get(path, headers=h).status_code == 403
    assert client.get("/api/auth/me", headers=h).status_code == 200


def test_bootstrap_password_dies_after_change(client):
    login(client)                                  # performs the first-login change
    assert _raw_login(client, SUPER_PW).status_code == 401
    assert _raw_login(client, changed_password(SUPER_PW)).status_code == 200


def test_restart_does_not_reapply_bootstrap_password(client):
    login(client)
    auth.ensure_superadmin()                       # simulates a redeploy/restart
    auth.reset_throttle()
    assert _raw_login(client, SUPER_PW).status_code == 401


def test_new_password_must_differ_and_be_strong(client):
    h = {"Authorization": "Bearer " + _raw_login(client, SUPER_PW).json()["token"]}
    for bad in [SUPER_PW, "Short-1a!", "only-lower-case-long-enough"]:
        r = client.post("/api/auth/password", json={"current_password": SUPER_PW, "new_password": bad}, headers=h)
        assert r.status_code == 400, bad


def test_break_glass_reset(client, monkeypatch):
    login(client)
    monkeypatch.setattr(config, "SUPERADMIN_RESET", True)
    auth.ensure_superadmin()
    auth.reset_throttle()
    r = _raw_login(client, SUPER_PW)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    actions = [e["action"] for e in db.query("SELECT action FROM audit_log")]
    assert "superadmin_break_glass_reset" in actions and "superadmin_bootstrapped" in actions


def test_old_sessions_end_after_password_change(client):
    r = _raw_login(client, SUPER_PW)
    old = {"Authorization": "Bearer " + r.json()["token"]}
    client.post("/api/auth/password", json={"current_password": SUPER_PW, "new_password": "A-Brand-New-Pass-2026"}, headers=old)
    assert client.get("/api/auth/me", headers=old).status_code == 401


def test_admin_temporary_password_must_be_changed(client):
    sa = login(client)
    client.post("/api/admin/users", json={"username": "desk1", "password": "Temp-Pass-2026-ok"}, headers=sa)
    r = _raw_login(client, "Temp-Pass-2026-ok", "desk1")
    assert r.json()["user"]["must_change_password"] is True
    h = {"Authorization": "Bearer " + r.json()["token"]}
    assert client.get("/api/admin/uploads", headers=h).status_code == 403


def test_bootstrap_password_never_logged(client):
    login(client)
    blob = " ".join(str(v) for r in db.query("SELECT * FROM audit_log") for v in r.values())
    assert SUPER_PW not in blob
    blob = " ".join(str(v) for r in db.query("SELECT * FROM users") for v in r.values())
    assert SUPER_PW not in blob
