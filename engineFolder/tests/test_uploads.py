import gzip
import hashlib
import io
import json
import sqlite3
import stat
import zipfile

import pytest

from app import config, db, storage
from conftest import app_exec, login, make_admin, span_xml, upload, zip_bytes


@pytest.fixture()
def admin(client):
    return make_admin(client, login(client))


# ---------------------------------------------------------------- provenance
def test_span_provenance_comes_from_file_not_filename(client, admin):
    # filename claims 1 Jan and cycle i3; the file content says 4 Nov 2026
    data = zip_bytes({"nsccl.20260101.i3.spn": span_xml(date="20261104", created="20261104083012")})
    r = upload(client, admin, "nsccl.20260101.i3.zip", data, cycle="i1")
    assert r.status_code == 201, r.text
    u = r.json()["upload"]
    assert u["trade_date"] == "20261104" and u["trade_date_source"] == "file"
    assert u["cycle"] == "i1" and u["cycle_source"] == "admin"          # admin entered; filename ignored
    assert u["source_timestamp"] == "20261104083012"
    assert u["source_meta"]["pointInTime/isSetl"] == "0"
    assert u["sha256"] == hashlib.sha256(data).hexdigest()
    assert u["parser_versions"]["provenance"] and u["app_version"]
    assert u["uploaded_by"] == "alice" and u["uploaded_at"]


def test_span_without_cycle_is_rejected(client, admin):
    r = upload(client, admin, "nsccl.20261104.i1.zip", zip_bytes({"a.spn": span_xml()}))
    assert r.status_code == 400 and "cycle" in r.json()["error"].lower()


def test_span_cycle_from_file_when_present(client, admin):
    data = zip_bytes({"a.spn": span_xml(extra_pit="<cycle>I2</cycle>")})
    r = upload(client, admin, "x.zip", data)
    assert r.status_code == 201
    assert r.json()["upload"]["cycle"] == "I2" and r.json()["upload"]["cycle_source"] == "file"
    r2 = upload(client, admin, "y.zip", zip_bytes({"b.spn": span_xml(extra_pit="<cycle>I3</cycle>", created="1")}), cycle="I9")
    assert r2.status_code == 400 and "does not match" in r2.json()["error"]


def test_trade_date_mismatch_rejected(client, admin):
    r = upload(client, admin, "a.zip", zip_bytes({"a.spn": span_xml(date="20261104")}), trade_date="20261103", cycle="i1")
    assert r.status_code == 400 and "does not match" in r.json()["error"]


def test_settlement_flag_conflicts_rejected(client, admin):
    eod = zip_bytes({"a.spn": span_xml(is_setl="1")})
    assert upload(client, admin, "a.zip", eod, cycle="i1").status_code == 400
    assert upload(client, admin, "a.zip", eod, cycle="s").status_code == 201
    intraday = zip_bytes({"b.spn": span_xml(is_setl="0", created="2")})
    assert upload(client, admin, "b.zip", intraday, cycle="s").status_code == 400


def test_non_span_requires_confirmed_date_and_no_cycle(client, admin):
    csv = b"Symbol,Instrument Type,ELM%\nNIFTY,OTH,2\n"
    assert upload(client, admin, "ael_04112026.csv", csv, file_type="elm").status_code == 400
    r = upload(client, admin, "ael_04112026.csv", csv, file_type="elm", trade_date="20261104")
    assert r.status_code == 201 and r.json()["upload"]["trade_date_source"] == "admin"
    assert upload(client, admin, "ael2.csv", csv + b"X,OTH,3\n", file_type="elm", trade_date="20261104", cycle="i1").status_code == 400


# ---------------------------------------------------------------- safety
def test_zip_bomb_rejected(client, admin):
    bomb = zip_bytes({"a.spn": b"<spanFile>" + b" " * (30 * 1024 * 1024) + b"</spanFile>"})
    r = upload(client, admin, "bomb.zip", bomb, cycle="i1")
    assert r.status_code == 400 and "zip bomb" in r.json()["error"]


def test_path_traversal_rejected(client, admin):
    for name in ["../../etc/passwd.spn", "/abs.spn", "C:/win.spn", "a/../../b.spn"]:
        r = upload(client, admin, "t.zip", zip_bytes({name: span_xml()}), cycle="i1")
        assert r.status_code == 400 and "unsafe path" in r.json()["error"], name


def test_symlink_rejected(client, admin):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link.spn")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    r = upload(client, admin, "l.zip", buf.getvalue(), cycle="i1")
    assert r.status_code == 400 and "symbolic link" in r.json()["error"]


def test_nested_archive_and_too_many_members_rejected(client, admin):
    nested = zip_bytes({"inner.zip": zip_bytes({"a.spn": span_xml()})})
    assert "Nested" in upload(client, admin, "n.zip", nested, cycle="i1").json()["error"]
    many = zip_bytes({f"f{i}.txt": b"x" for i in range(config.ZIP_MAX_MEMBERS + 1)})
    assert "limit" in upload(client, admin, "m.zip", many, file_type="bhavcopy", trade_date="20261104").json()["error"]


def test_encrypted_zip_rejected(client, admin):
    data = bytearray(zip_bytes({"a.spn": span_xml()}))
    # set the "encrypted" general-purpose flag on the local header and the central directory entry
    data[6] |= 0x1
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 8] |= 0x1
    r = upload(client, admin, "e.zip", bytes(data), cycle="i1")
    assert r.status_code == 400


def test_type_decided_by_content(client, admin):
    exe = b"MZ\x90\x00" + b"\x00" * 100
    assert upload(client, admin, "nsccl.20261104.i1.zip", exe, cycle="i1").status_code == 400
    assert upload(client, admin, "fake.zip", b"Symbol,ELM\nA,1\n", cycle="i1").json()["error"].startswith("A span file must be")
    html = b"<html><body>Access denied</body></html>"
    assert upload(client, admin, "x.xml", html, cycle="i1").status_code == 400


def test_xml_entities_rejected(client, admin):
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><spanFile><pointInTime><date>20261104</date></pointInTime></spanFile>'
    r = upload(client, admin, "x.xml", evil, cycle="i1")
    assert r.status_code == 400 and "DOCTYPE" in r.json()["error"]


def test_gzip_bomb_rejected(client, admin):
    big = gzip.compress(b"a" * (60 * 1024 * 1024))
    r = upload(client, admin, "c.csv.gz", big, file_type="contract_master", trade_date="20261104")
    assert r.status_code == 400


def test_size_limit(client, admin, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    r = upload(client, admin, "big.csv", b"a,b\n" * 400_000, file_type="bhavcopy", trade_date="20261104")
    assert r.status_code == 413


def test_wrong_segment_type_combination(client, admin):
    r = upload(client, admin, "v.dat", b"20,ABC,EQ\n", segment="NFO", file_type="var", trade_date="20261104")
    assert r.status_code == 400


# ---------------------------------------------------------------- integrity
def test_archive_is_byte_exact_with_manifest(client, admin):
    data = zip_bytes({"a.spn": span_xml()})
    u = upload(client, admin, "a.zip", data, cycle="i1").json()["upload"]
    store = storage.get_store()
    assert store.get(u["archive_key"]) == data
    man = json.loads(store.get(u["manifest_key"]))
    assert man["sha256"] == u["sha256"] and man["cycle_source"] == "admin" and man["uploaded_by"] == "alice"
    assert client.post(f"/api/admin/uploads/{u['id']}/verify", headers=admin).json()["ok"] is True


def test_duplicate_and_replace(client, admin):
    data = zip_bytes({"a.spn": span_xml()})
    first = upload(client, admin, "a.zip", data, cycle="i1").json()["upload"]
    assert "already uploaded" in upload(client, admin, "a.zip", data, cycle="i1").json()["error"]
    other = zip_bytes({"a.spn": span_xml(created="20261104090000")})
    assert "already exists" in upload(client, admin, "b.zip", other, cycle="i1").json()["error"]
    second = upload(client, admin, "b.zip", other, cycle="i1", replace=True).json()["upload"]
    old = client.get(f"/api/admin/uploads/{first['id']}", headers=admin).json()["upload"]
    assert old["status"] == "superseded" and str(second["id"]) in old["message"]


def test_withdraw_keeps_record_and_original(client, admin):
    u = upload(client, admin, "a.zip", zip_bytes({"a.spn": span_xml()}), cycle="i1").json()["upload"]
    assert client.post(f"/api/admin/uploads/{u['id']}/withdraw", json={"reason": ""}, headers=admin).status_code == 400
    r = client.post(f"/api/admin/uploads/{u['id']}/withdraw", json={"reason": "wrong file"}, headers=admin)
    assert r.json()["upload"]["status"] == "deleted"
    assert storage.get_store().exists(u["archive_key"])


def test_provenance_immutable_and_rows_undeletable(client, admin):
    upload(client, admin, "a.zip", zip_bytes({"a.spn": span_xml()}), cycle="i1")
    for sql in ["UPDATE uploads SET sha256 = 'x'", "UPDATE uploads SET uploaded_by = 'mallory'",
                "UPDATE uploads SET cycle = 'i9'", "DELETE FROM uploads"]:
        with pytest.raises(Exception):
            app_exec(sql)
    app_exec("UPDATE uploads SET status = 'deleted'")  # status may change


def test_failed_and_rejected_uploads_are_audited(client, admin):
    upload(client, admin, "bad.zip", b"MZ" + b"\x00" * 10, cycle="i1")
    sa = login(client)
    entries = client.get("/api/admin/audit", headers=sa).json()["entries"]
    assert any(e["action"] == "upload_rejected" and e["username"] == "alice" for e in entries)


def test_suggest_is_hint_only(client, admin):
    r = client.post("/api/admin/suggest", json={"filename": "nsccl.20261104.i1.zip"}, headers=admin).json()
    assert r["segment"] == "NFO" and r["file_type"] == "span" and r["cycle_hint"] == "i1"
    assert "confirm" in r["note"]
