import jwt

from app import auth, config, db
from conftest import SUPER_PW, login, make_admin


def test_superadmin_login_and_me(client):
    h = login(client)
    r = client.get("/api/auth/me", headers=h)
    assert r.status_code == 200 and r.json()["user"]["role"] == "superadmin"


def test_wrong_password_and_unknown_user_same_message(client):
    a = client.post("/api/auth/login", json={"username": "superadmin", "password": "nope"})
    b = client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_lockout_after_repeated_failures(client):
    for _ in range(config.LOCKOUT_THRESHOLD):
        auth.reset_throttle()
        client.post("/api/auth/login", json={"username": "superadmin", "password": "wrong"})
    auth.reset_throttle()
    r = client.post("/api/auth/login", json={"username": "superadmin", "password": SUPER_PW})
    assert r.status_code == 423  # correct password is refused while locked


def test_ip_throttle(client):
    codes = [client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code
             for _ in range(config.IP_LOGIN_LIMIT_PER_MIN + 2)]
    assert 429 in codes


def test_requires_login(client):
    for path in ["/api/admin/uploads", "/api/admin/audit", "/api/admin/users", "/api/admin/backups"]:
        assert client.get(path).status_code == 401


def test_admin_cannot_manage_users_or_run_backups(client):
    sa = login(client)
    ad = make_admin(client, sa)
    assert client.get("/api/admin/users", headers=ad).status_code == 403
    assert client.post("/api/admin/users", json={"username": "bob", "password": "Bob-Pass-2026!x"}, headers=ad).status_code == 403
    assert client.post("/api/admin/backups/run", headers=ad).status_code == 403
    assert client.put("/api/admin/settings", json={"backup_retention_count": 3}, headers=ad).status_code == 403
    assert client.get("/api/admin/uploads", headers=ad).status_code == 200


def test_eight_character_password_accepted(client):
    sa = login(client)
    r = client.post("/api/admin/users", json={"username": "carol", "password": "abcd1234"}, headers=sa)
    assert r.status_code == 201


def test_password_rules(client):
    sa = login(client)
    for bad in ["short1A", "", "bob-Contains-Name-1", "x" * 129]:
        r = client.post("/api/admin/users", json={"username": "bob", "password": bad}, headers=sa)
        assert r.status_code == 400


def test_disable_and_reset_invalidate_tokens(client):
    sa = login(client)
    ad = make_admin(client, sa)
    uid = [u for u in client.get("/api/admin/users", headers=sa).json()["users"] if u["username"] == "alice"][0]["id"]
    assert client.patch(f"/api/admin/users/{uid}", json={"password": "New-Desk-Pass-9!"}, headers=sa).status_code == 200
    assert client.get("/api/auth/me", headers=ad).status_code == 401      # old token dead
    ad2 = login(client, "alice", "New-Desk-Pass-9!")
    client.patch(f"/api/admin/users/{uid}", json={"active": False}, headers=sa)
    assert client.get("/api/auth/me", headers=ad2).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "New-Desk-Pass-9!"}).status_code == 401


def test_main_superadmin_cannot_be_disabled(client):
    sa = login(client)
    uid = client.get("/api/auth/me", headers=sa).json()["user"]["id"]
    assert client.patch(f"/api/admin/users/{uid}", json={"active": False}, headers=sa).status_code == 400


def test_forged_and_expired_tokens_rejected(client):
    forged = jwt.encode({"sub": "superadmin", "role": "superadmin", "tv": 1, "iat": 0, "exp": 9999999999, "iss": "margin-engine"},
                        "not-the-secret", algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + forged}).status_code == 401
    none_alg = jwt.encode({"sub": "superadmin", "tv": 1}, None, algorithm="none") if hasattr(jwt, "encode") else ""
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + none_alg}).status_code == 401


def test_passwords_are_hashed(env):
    row = db.query_one("SELECT pw_hash FROM users WHERE username = 'superadmin'")
    assert SUPER_PW not in row["pw_hash"] and row["pw_hash"].startswith("pbkdf2_sha256$")


def test_user_actions_are_audited(client):
    sa = login(client)
    make_admin(client, sa)
    actions = [e["action"] for e in client.get("/api/admin/audit", headers=sa).json()["entries"]]
    assert "user_created" in actions and "login" in actions
