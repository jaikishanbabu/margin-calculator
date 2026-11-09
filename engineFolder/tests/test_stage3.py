"""Stage 3: the owner's SPAN core tests, validation results, and the margin endpoint."""
import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app import config, fetcher, margin, readiness, validation
from conftest import login, make_admin
from test_readiness import FT, post_snapshot, running_run

ROOT = Path(__file__).resolve().parent.parent


def _owner_tests():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("owner_engine_tests", ROOT / "validation/tests/test_engine_nfo.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("name", ["test_core_scan_tiers_do_not_offset", "test_core_spread_charge",
                                  "test_core_net_option_value", "test_fixture_file_shape"])
def test_owner_engine_core(name):
    """The owner's engine tests that need no exchange files (the rest run on GitHub)."""
    getattr(_owner_tests(), name)()


@pytest.fixture()
def vx(client, monkeypatch):
    monkeypatch.setattr(config, "FETCHER_TOKEN", FT)
    monkeypatch.setattr(fetcher, "dispatch_github", lambda rid: None)
    return {"c": client, "sa": login(client), "job": {"X-Fetcher-Token": FT}}


def _report(**s):
    summary = {"baskets_passed": 200, "baskets_lagged": 2, "tests_passed": 20}
    summary.update(s)
    return {"summary": summary, "tests": [], "baskets": [], "data": {"2026-10-27": ["i1", "s"]}, "code_version": "abc"}


def _validation_run(vx):
    r = vx["c"].post("/api/admin/validation/run", json={"segment": "NFO"}, headers=vx["sa"])
    assert r.status_code == 202 and r.json()["run"]["trigger"] == "validate"
    rid = r.json()["run"]["id"]
    vx["c"].post(f"/api/fetcher/runs/{rid}/start", json={}, headers=vx["job"])
    return rid


def test_validation_flow(vx, monkeypatch):
    c, sa, job = vx["c"], vx["sa"], vx["job"]
    assert c.post("/api/admin/validation/run", json={"segment": "MCX"}, headers=sa).status_code == 400
    assert validation.status("NFO")["validated"] is False
    rid = _validation_run(vx)
    r = c.post(f"/api/fetcher/runs/{rid}/validation", json={"segment": "NFO", "report": _report(baskets_failed=3)}, headers=job)
    assert r.status_code == 201 and r.json()["validation"]["status"] == "failed"
    assert validation.status("NFO") == {**validation.status("NFO"), "validated": False, "reason": "The latest validation failed"}
    c.post(f"/api/fetcher/runs/{rid}/finish", json={"status": "done"}, headers=job)

    rid = _validation_run(vx)
    assert c.post(f"/api/fetcher/runs/{rid}/validation", json={"segment": "NFO", "report": _report()},
                  headers=sa).status_code == 403
    r = c.post(f"/api/fetcher/runs/{rid}/validation", json={"segment": "NFO", "report": _report()}, headers=job)
    assert r.json()["validation"]["status"] == "passed" and validation.status("NFO")["validated"] is True
    ov = c.get("/api/admin/validation?segment=NFO", headers=make_admin(c, sa)).json()
    assert ov["status"]["validated"] and ov["report"]["report"]["data"] == {"2026-10-27": ["i1", "s"]}
    assert len(ov["history"]) == 2
    rows = c.get("/api/admin/readiness", headers=sa).json()["validation"]
    assert rows["NFO"]["validated"] is True
    # a change to the rules files invalidates it
    monkeypatch.setattr(validation, "current_rules", lambda: {"static_rates.yaml": "changed"})
    st = validation.status("NFO")
    assert not st["validated"] and "Rules changed" in st["reason"]
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=sa).json()["entries"]]
    assert "validation_recorded" in acts


def test_validation_needs_a_validate_run(vx):
    rid = running_run(vx | {"c": vx["c"]})                 # a rebuild run, not a validation
    r = vx["c"].post(f"/api/fetcher/runs/{rid}/validation", json={"segment": "NFO", "report": _report()}, headers=vx["job"])
    assert r.status_code == 409


def test_crash_or_nothing_checked_is_not_a_pass(vx):
    rid = _validation_run(vx)
    v = validation.record(rid, "NFO", dict(_report(), crash="Traceback"))
    assert v["status"] == "failed"
    v = validation.record(rid, "NFO", _report(baskets_passed=0, baskets_lagged=0))
    assert v["status"] == "failed"


# ---------------------------------------------------------------- margin endpoint
class FakeEngine:
    opened = []

    def __init__(self, path):
        FakeEngine.opened.append(Path(path).read_bytes())
        self.conn = type("C", (), {"close": lambda self: None})()

    def leg(self, p):
        return type("L", (), {"lot_size": 65})()

    def margin(self, pos, alone=True):
        return {"span": 1000.0, "exposure": 200.0, "other": 0.0, "total": 1200.0, "net_premium": -50.0, "benefit": 0.0,
                "warnings": [], "legs": [{"label": "NIFTY", "exposure": 200.0}], "underlyings": {},
                "as_of": {"span_cycle": "i1", "margin_date": "2026-11-11"}}


@pytest.fixture()
def mx(vx, monkeypatch, tmp_path):
    import engine as engine_pkg
    monkeypatch.setattr(engine_pkg, "Engine", FakeEngine)
    monkeypatch.setattr(margin, "CACHE_DIR", tmp_path / "cache")
    margin._engines.clear()
    FakeEngine.opened = []
    return vx


def test_margin_endpoint_uses_the_in_use_snapshot(mx):
    c = mx["c"]
    r = c.post("/api/margin", json={"segment": "NFO", "positions": [{"symbol": "NIFTY", "expiry": "2026-11-17",
                                                                      "strike": 23000, "option_type": "CE", "qty": -65}]})
    assert r.status_code == 400 and "No NFO data" in r.json()["error"]
    rid = running_run(mx)
    post_snapshot(mx, rid, blob=b"nfo-database", info={"span_cycle": "i1", "bod_stale": "0"})
    body = {"segment": "NFO", "positions": [{"symbol": "NIFTY", "expiry": "2026-11-17", "strike": 23000,
                                             "option_type": "CE", "qty": -65}]}
    r = c.post("/api/margin", json=body)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["total"] == 1200.0 and m["data"]["span_cycle"] == "i1" and m["data"]["basis"] == "bod"
    assert m["validated"] is False and "Not validated" in m["validation_note"]
    assert FakeEngine.opened == [b"nfo-database"]                     # unpacked from the archive
    c.post("/api/margin", json=body)
    assert len(FakeEngine.opened) == 1                                 # engine kept open
    assert c.post("/api/margin", json=dict(body, positions=[dict(body["positions"][0], qty=-10)])).status_code == 400
    assert c.post("/api/margin", json=dict(body, segment="MCX")).status_code == 400
    assert c.post("/api/margin", json=dict(body, positions=[])).status_code == 400
    assert c.post("/api/margin", json=dict(body, positions=[{"qty": 65}])).status_code == 400


def test_margin_refuses_a_corrupted_snapshot(mx):
    from app import storage
    rid = running_run(mx)
    s = post_snapshot(mx, rid, blob=b"nfo-database").json()["snapshot"]
    storage.get_store().delete(s["archive_key"])
    storage.get_store().put(s["archive_key"], gzip.compress(b"tampered"), "application/gzip")
    r = mx["c"].post("/api/margin", json={"segment": "NFO", "positions": [{"tradingsymbol": "NIFTY26NOVFUT", "qty": 65}]})
    assert r.status_code == 400 and "checksum" in r.json()["error"]


def test_margin_rate_limit():
    lim = margin.RateLimit(n=3, per=60)
    assert [lim.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    assert lim.allow("5.6.7.8")
