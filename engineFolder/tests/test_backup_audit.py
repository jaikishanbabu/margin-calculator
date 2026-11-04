import gzip
import json

import pytest

from app import audit, backup, config, db, storage
from app.storage.supabase import SupabaseStore
from conftest import app_exec, login, make_admin, owner_tamper as _tamper, span_xml, upload, zip_bytes
from fake_supabase import KEY, FakeStorage


def _seed(client):
    sa = login(client)
    ad = make_admin(client, sa)
    upload(client, ad, "a.zip", zip_bytes({"a.spn": span_xml()}), cycle="i1")
    upload(client, ad, "ael.csv", b"Symbol,ELM\nNIFTY,2\n", file_type="elm", trade_date="20261104")
    return sa, ad


# ---------------------------------------------------------------- audit chain
def test_audit_chain_valid(client):
    _seed(client)
    v = audit.verify()
    assert v["ok"] and v["checked"] >= 5


def test_app_cannot_edit_or_delete_audit_rows(client):
    _seed(client)
    for sql in ("UPDATE audit_log SET detail = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(Exception):
            app_exec(sql)
    assert audit.verify()["ok"]


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE audit_log SET username = 'mallory' WHERE seq = 3;", "changed"),
    ("DELETE FROM audit_log WHERE seq = 3;", "gap"),
    ("UPDATE audit_log SET ts = '2020-01-01T00:00:00+00:00' WHERE seq = 2;", "changed"),
])
def test_tampering_is_detected(client, sql, reason):
    _seed(client)
    _tamper(sql)
    v = audit.verify()
    assert not v["ok"] and reason in v["reason"]


def test_audit_verify_endpoint(client):
    sa, _ = _seed(client)
    assert client.get("/api/admin/audit/verify", headers=sa).json()["ok"] is True


# ---------------------------------------------------------------- backup + restore
def test_backup_and_restore_test_pass(client):
    sa, _ = _seed(client)
    b = client.post("/api/admin/backups/run", headers=sa).json()
    assert b["ok"], b
    r = client.post("/api/admin/backups/restore-test", headers=sa).json()
    assert r["ok"], r
    names = {c["check"] for c in r["checks"]}
    assert {"backup checksum", "restored audit chain", "live audit log still contains backup head", "live audit chain"} <= names
    assert r["originals_checked"] == 2 and all(c["ok"] for c in r["checks"])


def test_restore_detects_corrupted_backup(client):
    sa, _ = _seed(client)
    b = backup.backup_export("t")
    store = storage.get_store()
    path = store._path(b["object"])
    path.write_bytes(path.read_bytes()[:-5] + b"XXXXX")
    r = backup.restore_test("t")
    assert not r["ok"]
    assert any(c["check"] == "backup checksum" and not c["ok"] for c in r["checks"])


def test_restore_detects_missing_or_changed_original(client):
    sa, _ = _seed(client)
    backup.backup_export("t")
    store = storage.get_store()
    for k in store.list("originals/"):
        store._path(k).write_bytes(b"tampered")
    r = backup.restore_test("t")
    assert not r["ok"] and any(c["check"].startswith("original #") and not c["ok"] for c in r["checks"])


def test_restore_detects_live_log_truncated_after_backup(client):
    sa, _ = _seed(client)
    backup.backup_export("t")
    _tamper("DELETE FROM audit_log WHERE seq >= 4;")
    r = backup.restore_test("t")
    assert not r["ok"]
    assert any(c["check"] == "live audit log still contains backup head" and not c["ok"] for c in r["checks"])


def test_restore_without_backup_fails_clearly(client):
    r = backup.restore_test("t")
    assert not r["ok"] and "No backups" in r["error"]


def test_backup_retention(client):
    sa, _ = _seed(client)
    db.set_setting("backup_retention_count", 2)
    import time
    for _ in range(4):
        assert backup.backup_export("t")["ok"]
        time.sleep(1.05)  # distinct timestamps
    assert len(backup.list_backups()) == 2


def test_backup_restore_through_supabase_backend(client):
    """Same flow, but the archive is the Supabase Storage API (fake server)."""
    fake = FakeStorage()
    try:
        storage.set_store(SupabaseStore(fake.url, KEY, "margin-archive"))
        sa, _ = _seed(client)
        assert backup.backup_export("t")["ok"]
        r = backup.restore_test("t")
        assert r["ok"], r
    finally:
        fake.close()


def test_backup_manifest_contents(client):
    _seed(client)
    b = backup.backup_export("t")
    man = json.loads(storage.get_store().get(b["manifest"]))
    assert man["format"] == "margin-backup/1" and man["row_counts"]["uploads"] == 2
    assert man["audit_head"]["chain_ok"] and len(man["sha256"]) == 64
    rows = gzip.decompress(storage.get_store().get(b["object"])).decode().splitlines()
    assert sum(1 for r in rows if json.loads(r)["t"] == "uploads") == 2


# ---------------------------------------------------------------- ops endpoints
def test_ops_endpoints_require_token(client):
    assert client.post("/api/ops/backup").status_code == 403
    assert client.post("/api/ops/backup", headers={"X-Ops-Token": "wrong"}).status_code == 403
    r = client.post("/api/ops/backup", headers={"X-Ops-Token": config.OPS_TOKEN})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.post("/api/ops/restore-test", headers={"X-Ops-Token": config.OPS_TOKEN})
    assert r.status_code == 200 and r.json()["ok"]


def test_health(client):
    r = client.get("/api/health?deep=1").json()
    assert r["ok"] and r["db"] == "ok" and r["storage"] == "ok" and r["backup"]["backup_overdue"] is True
