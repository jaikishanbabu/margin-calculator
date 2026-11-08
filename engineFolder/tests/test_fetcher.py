import io
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import config, db, fetcher, storage, uploads
from conftest import login, make_admin, span_xml, zip_bytes

IST = fetcher.IST
FT = "test-fetcher-token-0123456789abcdef0123456789"


@pytest.fixture()
def fx(client, monkeypatch):
    monkeypatch.setattr(config, "FETCHER_TOKEN", FT)
    calls = []
    monkeypatch.setattr(fetcher, "dispatch_github", lambda rid: calls.append(rid))
    return {"client": client, "sa": login(client), "dispatched": calls,
            "job": {"X-Fetcher-Token": FT}}


def at(h, m=0, day=6):        # 6 Nov 2026 = Friday
    return datetime(2026, 11, day, h, m, tzinfo=IST)


# ---------------------------------------------------------------- settings + permissions
def test_admin_can_fetch_but_not_change_settings(fx):
    c = fx["client"]
    ad = make_admin(c, fx["sa"])
    assert c.put("/api/admin/fetcher/settings", json={"fetch_auto_enabled": True}, headers=ad).status_code == 403
    r = c.post("/api/admin/fetcher/run", json={"segment": "NFO"}, headers=ad)
    assert r.status_code == 202 and r.json()["run"]["segments"] == ["NFO"] and fx["dispatched"]
    assert c.get("/api/admin/fetcher", headers=ad).json()["can_edit"] is False


def test_settings_validation_and_audit(fx):
    c = fx["client"]
    assert c.put("/api/admin/fetcher/settings", json={"fetch_frequency_min": 7}, headers=fx["sa"]).status_code == 400
    assert c.put("/api/admin/fetcher/settings", json={"nonsense": 1}, headers=fx["sa"]).status_code == 400
    r = c.put("/api/admin/fetcher/settings", json={"fetch_auto_enabled": True, "fetch_frequency_min": 60}, headers=fx["sa"])
    assert r.json()["settings"]["fetch_frequency_min"] == 60
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=fx["sa"]).json()["entries"]]
    assert "fetcher_settings_changed" in acts


def test_tokens_are_separate(fx):
    c = fx["client"]
    # fetcher token can't use admin or ops endpoints; admin/ops can't use fetcher endpoints
    assert c.get("/api/admin/fetcher", headers=fx["job"]).status_code == 401
    assert c.post("/api/ops/fetch-tick", headers=fx["job"]).status_code == 403
    assert c.get("/api/fetcher/have?trade_date=20261106", headers=fx["sa"]).status_code == 403
    assert c.get("/api/fetcher/have?trade_date=20261106", headers={"X-Ops-Token": config.OPS_TOKEN}).status_code == 403
    assert c.get("/api/fetcher/have?trade_date=20261106", headers=fx["job"]).status_code == 200


def test_one_run_at_a_time_and_bad_segment(fx):
    c = fx["client"]
    assert c.post("/api/admin/fetcher/run", json={"segment": "XYZ"}, headers=fx["sa"]).status_code == 400
    assert c.post("/api/admin/fetcher/run", json={"segment": "ALL"}, headers=fx["sa"]).status_code == 202
    r = c.post("/api/admin/fetcher/run", json={"segment": "MCX"}, headers=fx["sa"])
    assert r.status_code == 400 and "already" in r.json()["error"]


def test_dispatch_failure_marks_run_failed(client, monkeypatch):
    def boom(rid):
        raise fetcher.FetchError("GitHub answered HTTP 401")
    monkeypatch.setattr(fetcher, "dispatch_github", boom)
    r = client.post("/api/admin/fetcher/run", json={"segment": "NFO"}, headers=login(client))
    assert r.status_code == 400 and "401" in r.json()["error"]
    assert fetcher.list_runs()[0]["status"] == "failed"


# ---------------------------------------------------------------- scheduler tick
def test_tick_rules(fx):
    noop = lambda rid: None  # noqa: E731
    assert fetcher.tick(at(10), noop)["reason"] == "auto fetch is off"
    db.set_setting("fetch_auto_enabled", True)
    db.set_setting("fetch_frequency_min", 30)
    assert fetcher.tick(at(10, day=7), noop)["reason"] == "weekend"
    assert fetcher.tick(at(5), noop)["reason"] == "outside fetch window"
    first = fetcher.tick(at(10), noop)
    assert first["action"] == "started"
    fetcher.job_started(first["run_id"])
    fetcher.job_finished(first["run_id"])
    assert "not due" in fetcher.tick(at(10, 10), noop)["reason"]
    assert fetcher.tick(at(10, 29), noop)["action"] == "started"      # 2 min scheduler slack


def test_tick_endpoint_needs_ops_token(fx):
    c = fx["client"]
    assert c.post("/api/ops/fetch-tick").status_code == 403
    assert c.post("/api/ops/fetch-tick", headers={"X-Ops-Token": config.OPS_TOKEN}).status_code == 200


def test_stale_runs_expire(fx, monkeypatch):
    run = fetcher.create_run("manual", "superadmin", ["NFO"], dispatch_fn=lambda r: None)
    monkeypatch.setattr(fetcher, "STALE_RUN_MINUTES", -1)
    assert fetcher.active_run() is None
    assert fetcher.get_run(run["id"])["status"] == "failed"


# ---------------------------------------------------------------- job flow
def _run(fx, seg="ALL"):
    c = fx["client"]
    rid = c.post("/api/admin/fetcher/run", json={"segment": seg}, headers=fx["sa"]).json()["run"]["id"]
    r = c.post(f"/api/fetcher/runs/{rid}/start", json={"github_run_id": "123"}, headers=fx["job"])
    assert r.status_code == 200 and r.json()["status"] == "running"
    return rid


def _up(fx, rid, sid, name, data, **form):
    return fx["client"].post(f"/api/fetcher/runs/{rid}/upload", headers=fx["job"],
                             data={"source_id": sid, "url": f"https://example/{name}", **form},
                             files={"file": (name, data, "application/octet-stream")})


def test_job_upload_records_exchange_cycle_and_progress(fx, monkeypatch):
    monkeypatch.setattr(fetcher, "trading_date_for", lambda now=None: "20261106")
    rid = _run(fx)
    data = zip_bytes({"nsccl.20261106.i1.spn": span_xml(date="20261106")})
    r = _up(fx, rid, "nfo.span", "nsccl.20261106.i1.zip", data, cycle="i1", source_ref="nfo.span:cycle=i1")
    assert r.status_code == 201, r.text
    u = r.json()["upload"]
    assert u["cycle"] == "i1" and u["cycle_source"] == "exchange" and u["trade_date_source"] == "file"
    assert u["uploaded_by"].startswith("fetcher") and u["source_ref"] == "nfo.span:cycle=i1"
    assert u["source_meta"]["fetched_from"].endswith("nsccl.20261106.i1.zip")
    c = fx["client"]
    c.post(f"/api/fetcher/runs/{rid}/items", headers=fx["job"], json={"items": [
        {"source_ref": "nfo.span:cycle=i1", "segment": "NFO", "file_type": "span", "status": "archived", "upload_id": u["id"]}]})
    have = c.get("/api/fetcher/have?trade_date=20261106", headers=fx["job"]).json()["have"]
    assert {"nfo.span:cycle=i1"} == {h["source_ref"] for h in have}
    fin = c.post(f"/api/fetcher/runs/{rid}/finish", headers=fx["job"], json={"status": "done", "message": "archived 1"})
    assert fin.json()["run"]["summary"] == {"archived": 1}
    run = c.get(f"/api/admin/fetcher/runs/{rid}", headers=fx["sa"]).json()["run"]
    assert run["items"][0]["status"] == "archived"
    # finished runs refuse further reports / uploads
    assert c.post(f"/api/fetcher/runs/{rid}/items", headers=fx["job"], json={"items": []}).status_code == 400
    assert _up(fx, rid, "nfo.elm", "ael.csv", b"a,b\n1,2\n").status_code == 409


def test_same_type_same_day_coexist_and_refresh_replaces(fx, monkeypatch):
    monkeypatch.setattr(fetcher, "trading_date_for", lambda now=None: "20261106")
    rid = _run(fx)
    b1 = b"10,header\n20,RELIANCE,EQ,INE002A01018,5.1,4,9.1,3.5,0,12.6\n"
    b2 = b"10,header\n20,RELIANCE,EQ,INE002A01018,5.2,4,9.2,3.5,0,12.7\n"
    assert _up(fx, rid, "nse.var", "C_VAR1_06112026_1.DAT", b1, source_ref="nse.var:batch=1").status_code == 201
    assert _up(fx, rid, "nse.var", "C_VAR1_06112026_2.DAT", b2, source_ref="nse.var:batch=2").status_code == 201
    # identical content again -> already uploaded
    r = _up(fx, rid, "nse.var", "C_VAR1_06112026_1.DAT", b1, source_ref="nse.var:batch=1")
    assert r.status_code == 400 and "already uploaded" in r.json()["error"]
    # refresh source: same name, new content, replace=1 supersedes
    v1 = zip_bytes({"VAR061126.csv": "a,b\n1,2\n"})
    v2 = zip_bytes({"VAR061126.csv": "a,b\n1,3\n"})
    first = _up(fx, rid, "bse.var", "VAR061126.zip", v1, source_ref="bse.var", replace="1").json()["upload"]
    second = _up(fx, rid, "bse.var", "VAR061126.zip", v2, source_ref="bse.var", replace="1").json()["upload"]
    assert uploads.get(first["id"])["status"] == "superseded" and second["status"] == "archived"


def test_unknown_source_rejected(fx):
    rid = _run(fx)
    assert _up(fx, rid, "evil.source", "x.csv", b"a\n").status_code == 400


# ---------------------------------------------------------------- retention
def _seed_days(fx, days):
    ad = fx["sa"]
    c = fx["client"]
    ids = {}
    for d in days:
        for cyc in ("i1", "i3", "s"):
            data = zip_bytes({"a.spn": span_xml(date=d, created=f"{d}{cyc}", is_setl="1" if cyc == "s" else "0")})
            r = c.post("/api/admin/uploads", headers=ad, data={"segment": "NFO", "file_type": "span", "cycle": cyc},
                       files={"file": (f"nsccl.{d}.{cyc}.zip", data, "application/octet-stream")})
            assert r.status_code == 201, r.text
            ids[(d, cyc)] = r.json()["upload"]
    return ids


def test_retention_keeps_window_and_records(fx):
    days = ["20261102", "20261103", "20261104", "20261105", "20261106"]
    ids = _seed_days(fx, days)
    db.set_setting("retention_trading_days", 3)
    db.set_setting("retention_intraday_days", 2)
    res = fetcher.prune_archive("t")
    store = storage.get_store()
    for (d, cyc), u in ids.items():
        row = uploads.get(u["id"])
        old = d < "20261104"
        intraday_old = cyc == "i3" and d < "20261105"
        if old or intraday_old:
            assert row["status"] == "expired" and not store.exists(u["archive_key"]), (d, cyc)
            assert row["sha256"] == u["sha256"]          # record + checksum kept
        else:
            assert row["status"] == "archived" and store.exists(u["archive_key"]), (d, cyc)
    assert res["removed"] == 7 and res["errors"] == 0
    # restore test ignores expired originals and still passes
    from app import backup
    assert backup.backup_export("t")["ok"]
    rt = backup.restore_test("t")
    assert rt["ok"], rt


# ---------------------------------------------------------------- end-to-end: the real job script
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_fetch_job_end_to_end_with_fake_exchange(env, tmp_path, monkeypatch):
    """Runs app.fetch_job against a real engine process; the exchange is faked at the resolver."""
    port = _free_port()
    sqlite = str(tmp_path / "e2e.db")
    store_dir = str(tmp_path / "e2e_archive")
    eng_env = dict(os.environ, APP_ENV="dev", SQLITE_PATH=sqlite, LOCAL_STORE_DIR=store_dir, STORAGE_BACKEND="local",
                   SUPERADMIN_PASSWORD="E2e-Boot-Pass-1", FETCHER_TOKEN=FT, OPS_TOKEN="x" * 40, DATABASE_URL="")
    root = Path(__file__).resolve().parent.parent
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=root, env=eng_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import requests
        for _ in range(50):
            try:
                if requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1).ok:
                    break
            except Exception:
                time.sleep(0.2)
        # create the run in the engine's database
        monkeypatch.setattr(config, "SQLITE_PATH", sqlite)
        monkeypatch.setattr(config, "DATABASE_URL", "")   # in PG CI mode too, use the engine's SQLite file
        run = fetcher.create_run("manual", "superadmin", ["NFO"], trade_date="20261106", dispatch_fn=lambda r: None)
        job = subprocess.run([sys.executable, "-c", f"""
import sys; sys.argv=['x','--run-id','{run['id']}']
sys.path.insert(0, 'tests')
from conftest import span_xml, zip_bytes
import app.fetch_job as j
from exfetch.resolvers import Candidate
from exfetch.net import NotPublished
SPAN = zip_bytes({{'nsccl.20261106.i1.spn': span_xml(date='20261106')}})
def fake(strategy, d, loop, http, session):
    if 'template' in strategy and 'nsccl' in strategy['template']:
        if loop.get('cycle') == 'i1':
            return [Candidate('https://nse/nsccl.20261106.i1.zip', 'nsccl.20261106.i1.zip', SPAN)]
        raise NotPublished('404 not yet')
    if 'template' in strategy and 'ael_' in strategy['template']:
        return [Candidate('https://nse/ael_06112026.csv', 'ael_06112026.csv', b'Symbol,Type,ELM\\nNIFTY,OTH,2\\n')]
    raise NotPublished('404')
j.resolve = fake
j.Calendar = lambda *a, **k: type('C', (), {{'is_trading_day': lambda s, d, seg='EQ': True}})()
j.main()
"""], cwd=root, env=dict(eng_env, ENGINE_URL=f"http://127.0.0.1:{port}"), capture_output=True, text=True, timeout=120)
        assert job.returncode == 0, job.stdout + job.stderr
        r = fetcher.get_run(run["id"])
        st = {i["source_ref"]: i["status"] for i in r["items"]}
        assert r["status"] == "done"
        assert st["nfo.span:cycle=i1"] == "archived" and st["nfo.span:cycle=i2"] == "pending"
        assert st["nfo.elm"] == "archived" and st["nfo.bhavcopy"] == "pending"
        # second run: everything already archived is skipped without contacting the exchange
        run2 = fetcher.create_run("manual", "superadmin", ["NFO"], trade_date="20261106", dispatch_fn=lambda r: None)
        job2 = subprocess.run(job.args[:2] + [job.args[2].replace(f"'--run-id','{run['id']}'", f"'--run-id','{run2['id']}'")],
                              cwd=root, env=dict(eng_env, ENGINE_URL=f"http://127.0.0.1:{port}"), capture_output=True, text=True, timeout=120)
        assert job2.returncode == 0, job2.stdout + job2.stderr
        st2 = {i["source_ref"]: i["status"] for i in fetcher.get_run(run2["id"])["items"]}
        assert st2["nfo.span:cycle=i1"] == "have" and st2["nfo.elm"] == "have"
    finally:
        srv.terminate()
        srv.wait(10)


# ---------------------------------------------------------------- evening runs fetch the next trading day
def test_trading_date_moves_to_next_weekday_from_21_00():
    assert fetcher.trading_date_for(at(20, 59)) == "20261106"          # Friday evening, before 21:00
    assert fetcher.trading_date_for(at(21, 0)) == "20261109"           # Friday 21:00 -> Monday
    assert fetcher.trading_date_for(at(21, 30, day=9)) == "20261110"   # Monday 21:30 -> Tuesday


def test_evening_tick_creates_next_day_run(fx):
    fetcher.update_settings({"username": "superadmin"}, {"fetch_auto_enabled": True, "fetch_frequency_min": 30})
    out = fetcher.tick(now=at(21, 30), dispatch_fn=lambda r: None)
    assert out["action"] == "started" and fetcher.get_run(out["run_id"])["trade_date"] == "20261109"


def test_job_can_move_trade_date_forward_only(fx):
    c, job = fx["client"], fx["job"]
    run = fetcher.create_run("auto", "auto-fetcher", ["ALL"], trade_date="20261110", dispatch_fn=lambda r: None)
    rid = run["id"]
    url = f"/api/fetcher/runs/{rid}/trade-date"
    assert c.post(url, json={"trade_date": "20261111"}, headers=job).status_code == 400      # not running yet
    c.post(f"/api/fetcher/runs/{rid}/start", json={}, headers=job)
    assert c.post(url, json={"trade_date": "20261109"}, headers=job).status_code == 400      # backwards
    assert c.post(url, json={"trade_date": "20261130"}, headers=job).status_code == 400      # too far
    assert c.post(url, json={"trade_date": "20261111"}, headers=fx["sa"]).status_code == 403  # job token only
    r = c.post(url, json={"trade_date": "20261111"}, headers=job)
    assert r.status_code == 200 and fetcher.get_run(rid)["trade_date"] == "20261111"
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=fx["sa"]).json()["entries"]]
    assert "fetch_run_trade_date_moved" in acts
