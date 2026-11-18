"""Hosting: backend switch (Render <-> Railway), health info, gzip."""
from conftest import login, make_admin


def test_superadmin_switches_backend_and_it_is_audited(client):
    sa = login(client)
    h = client.get("/api/health").json()
    assert h["active_backend"] == "render" and h["host"] in ("render", "railway", "local")
    r = client.put("/api/admin/settings", json={"active_backend": "railway"}, headers=sa)
    assert r.status_code == 200 and r.json()["settings"] == {"active_backend": "railway"}
    assert client.get("/api/health").json()["active_backend"] == "railway"
    assert client.get("/api/admin/settings", headers=sa).json()["settings"]["active_backend"] == "railway"
    log = client.get("/api/admin/audit", headers=sa).json()
    entries = log.get("entries") or log.get("audit") or log.get("items") or []
    sw = [e for e in entries if e.get("action") == "backend_switched"]
    assert sw and '"to":"railway"' in str(sw[0].get("detail")).replace("'", '"').replace(" ", "")
    assert client.put("/api/admin/settings", json={"active_backend": "heroku"}, headers=sa).status_code == 400


def test_admin_cannot_switch_backend(client):
    sa = login(client)
    ad = make_admin(client, sa)
    assert client.put("/api/admin/settings", json={"active_backend": "railway"}, headers=ad).status_code in (401, 403)
    assert client.get("/api/health").json()["active_backend"] == "render"


def test_api_responses_are_gzipped(client):
    r = client.get("/api/margin/segments", headers={"Accept-Encoding": "gzip"})
    if len(r.content) >= 1024:
        assert r.headers.get("content-encoding") == "gzip"
    small = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert small.headers.get("content-encoding") is None      # tiny responses aren't worth compressing
