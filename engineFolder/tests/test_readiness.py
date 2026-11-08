"""Stage 2C: snapshots (parsed data), the readiness page, the basis toggle and the build job."""
import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app import build_job, config, fetcher, readiness, storage
from conftest import app_exec, login, make_admin, span_xml, upload, zip_bytes

FT = "test-fetcher-token-0123456789abcdef0123456789"
BHAV = b"TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,ClsPric\n2026-11-06,2026-11-06,CM,NSE,STK,1,INE1,ABC,EQ,10.5\n"


@pytest.fixture()
def rx(client, monkeypatch):
    monkeypatch.setattr(config, "FETCHER_TOKEN", FT)
    monkeypatch.setattr(fetcher, "dispatch_github", lambda rid: None)
    return {"c": client, "sa": login(client), "job": {"X-Fetcher-Token": FT}}


def running_run(rx, seg="NFO"):
    r = rx["c"].post("/api/admin/readiness/rebuild", json={"segment": seg}, headers=rx["sa"])
    assert r.status_code == 202, r.text
    rid = r.json()["run"]["id"]
    assert rx["c"].post(f"/api/fetcher/runs/{rid}/start", json={}, headers=rx["job"]).status_code == 200
    return rid


def post_snapshot(rx, rid, seg="NFO", basis="bod", trade_date="2026-11-06", blob=b"sqlite-bytes", **meta):
    m = {"fingerprint": meta.pop("fingerprint", "fp-" + basis), "trade_date": trade_date,
         "info": dict({"span_cycle": "i1", "span_business_date": trade_date, "trade_date": trade_date,
                       "span_created": "2026-11-05 21:30"}, **meta.pop("info", {})),
         "inputs": [{"upload_id": 1, "filename": "x", "used": True, "trade_date": "20261106", "sha256": "0" * 64}],
         "rules": {"static_rates.yaml": "a" * 64}, "code_version": "abc"}
    m.update(meta)
    files = {"file": ("NFO.sqlite.gz", gzip.compress(blob), "application/gzip")} if blob is not None else None
    return rx["c"].post(f"/api/fetcher/runs/{rid}/snapshot", data={"segment": seg, "basis": basis, "meta": json.dumps(m)},
                        files=files, headers=rx["job"])


def seg_row(rx, seg="NFO"):
    return next(r for r in rx["c"].get("/api/admin/readiness", headers=rx["sa"]).json()["segments"] if r["segment"] == seg)


# ---------------------------------------------------------------- engine side
def test_rebuild_needs_a_parser_and_starts_a_rebuild_run(rx):
    c = rx["c"]
    assert c.post("/api/admin/readiness/rebuild", json={"segment": "CDS"}, headers=rx["sa"]).status_code == 400
    r = c.post("/api/admin/readiness/rebuild", json={"segment": "NFO"}, headers=rx["sa"])
    assert r.status_code == 202 and r.json()["run"]["trigger"] == "rebuild"
    rows = c.get("/api/admin/readiness", headers=rx["sa"]).json()["segments"]
    assert {r["segment"]: r["status"] for r in rows} == {
        "NFO": "not_built", "BFO": "not_built", "CDS": "no_parser", "MCX": "not_built", "NSECM": "not_built", "BSECM": "no_parser"}


def test_build_inputs_pick_the_loader_files_and_originals_verify(rx):
    c, sa, job = rx["c"], rx["sa"], rx["job"]
    span = zip_bytes({"nsccl.20261106.i1.spn": span_xml(date="20261106")})
    assert upload(c, sa, "nsccl.20261106.i1.zip", span, trade_date="20261106", cycle="i1").status_code == 201
    assert upload(c, sa, "BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv", BHAV, segment="NSECM", file_type="bhavcopy",
                  trade_date="20261106").status_code == 201
    var = b"01,Margin for 06Nov2026,1,100\n10,ABC,EQ,INE1,5,0,5,3.5,8.5,10\n"
    upload(c, sa, "C_VAR1_06112026_1.DAT", var, segment="NSECM", file_type="var", trade_date="20261106")
    p = c.get("/api/fetcher/build/inputs?segment=NFO", headers=job).json()
    names = sorted(f["filename"] for f in p["files"])
    assert names == ["BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv", "nsccl.20261106.i1.zip"]   # no VaR for NFO
    assert p["bases"] == ["bod", "latest"] and p["live"] == {"bod": None, "latest": None}
    f = next(f for f in p["files"] if f["filename"].endswith(".zip"))
    r = c.get(f"/api/fetcher/uploads/{f['id']}/original", headers=job)
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == f["sha256"]
    assert c.get(f"/api/fetcher/uploads/{f['id']}/original", headers=sa).status_code == 403     # job token only
    assert c.get("/api/fetcher/build/inputs?segment=CDS", headers=job).status_code == 400


def test_ready_snapshot_then_failed_rebuild_keeps_the_old_one(rx):
    c = rx["c"]
    rid = running_run(rx)
    r = post_snapshot(rx, rid)
    assert r.status_code == 201 and r.json()["snapshot"]["status"] == "ready"
    snap = r.json()["snapshot"]
    blob = storage.get_store().get(snap["archive_key"])
    assert gzip.decompress(blob) == b"sqlite-bytes" and hashlib.sha256(blob).hexdigest() == snap["sha256"]
    row = seg_row(rx)
    assert row["status"] == "ready" and row["in_use"]["id"] == snap["id"] and row["basis"] == "bod"

    r = post_snapshot(rx, rid, blob=None, error_kind="check_failed", message="too few risk arrays", fingerprint="fp2")
    assert r.status_code == 201 and r.json()["snapshot"]["status"] == "failed"
    row = seg_row(rx)
    assert row["in_use"]["id"] == snap["id"] and row["status"] == "ready"
    assert "too few risk arrays" in row["warning"]
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=rx["sa"]).json()["entries"]]
    assert "snapshot_built" in acts and "snapshot_failed" in acts
    # the job only uploads to a running run
    c.post(f"/api/fetcher/runs/{rid}/finish", json={"status": "done"}, headers=rx["job"])
    assert post_snapshot(rx, rid).status_code == 409


def test_failed_first_build_shows_the_reason(rx):
    rid = running_run(rx)
    post_snapshot(rx, rid, blob=None, error_kind="missing_inputs", message="contract_master: no file matching")
    row = seg_row(rx)
    assert row["status"] == "missing_inputs" and "contract_master" in row["message"] and row["in_use"] is None


def test_stale_when_newer_files_are_not_built(rx):
    c, sa = rx["c"], rx["sa"]
    rid = running_run(rx)
    post_snapshot(rx, rid, trade_date="2026-11-06")
    span = zip_bytes({"nsccl.20261109.i1.spn": span_xml(date="20261109", created="20261106213000")})
    upload(c, sa, "nsccl.20261109.i1.zip", span, trade_date="20261109", cycle="i1")
    row = seg_row(rx)
    assert row["status"] == "stale" and "20261109" in row["message"]
    rid2 = rid
    post_snapshot(rx, rid2, trade_date="2026-11-09", fingerprint="fp9", info={"bod_stale": "1", "bod_note": "i1 isn't out"})
    row = seg_row(rx)
    assert row["status"] == "stale" and "i1 isn't out" in row["message"]


def test_basis_toggle_superadmin_only_audited_with_fallback(rx):
    c, sa = rx["c"], rx["sa"]
    ad = make_admin(c, sa)
    assert c.put("/api/admin/readiness/basis", json={"segment": "NFO", "basis": "latest"}, headers=ad).status_code == 403
    assert c.put("/api/admin/readiness/basis", json={"segment": "NSECM", "basis": "bod"}, headers=sa).status_code == 400
    assert c.put("/api/admin/readiness/basis", json={"segment": "CDS", "basis": "bod"}, headers=sa).status_code == 400
    rid = running_run(rx)
    bod = post_snapshot(rx, rid).json()["snapshot"]
    r = c.put("/api/admin/readiness/basis", json={"segment": "NFO", "basis": "latest"}, headers=sa)
    assert r.status_code == 200 and r.json()["basis"]["NFO"] == "latest" and r.json()["basis"]["BFO"] == "bod"
    row = seg_row(rx)            # no latest snapshot yet -> falls back to start-of-day, and says so
    assert row["basis"] == "latest" and row["status"] == "fallback" and row["in_use"]["id"] == bod["id"]
    lat = post_snapshot(rx, rid, basis="latest", info={"span_cycle": "i3"}).json()["snapshot"]
    row = seg_row(rx)
    assert row["status"] == "ready" and row["in_use"]["id"] == lat["id"] and row["in_use"]["span_cycle"] == "i3"
    audit = c.get("/api/admin/audit", headers=sa).json()["entries"]
    ch = [e for e in audit if e["action"] == "margin_basis_changed"]
    assert ch and json.loads(ch[0]["detail"]) == {"segment": "NFO", "from": "bod", "to": "latest"} if isinstance(ch[0]["detail"], str) \
        else ch[0]["detail"] == {"segment": "NFO", "from": "bod", "to": "latest"}


def test_snapshot_files_kept_three_per_basis_records_forever(rx):
    rid = running_run(rx)
    snaps = [post_snapshot(rx, rid, blob=f"db{i}".encode(), fingerprint=f"fp{i}").json()["snapshot"] for i in range(5)]
    store = storage.get_store()
    assert [store.exists(s["archive_key"]) for s in snaps] == [False, False, True, True, True]
    st = {s["id"]: readiness.get_snapshot(s["id"])["status"] for s in snaps}
    assert list(st.values()) == ["expired", "expired", "ready", "ready", "ready"]


def test_snapshot_records_cannot_be_deleted(rx):
    rid = running_run(rx)
    post_snapshot(rx, rid)
    with pytest.raises(Exception):
        app_exec("DELETE FROM snapshots")


def test_unchanged_rebuild_updates_fingerprint_only(rx):
    c = rx["c"]
    rid = running_run(rx)
    s = post_snapshot(rx, rid).json()["snapshot"]
    r = c.post(f"/api/fetcher/runs/{rid}/snapshot", data={"segment": "NFO", "basis": "bod", "unchanged": "1",
                                                          "meta": json.dumps({"fingerprint": "fp-new"})}, headers=rx["job"])
    assert r.status_code == 200
    assert readiness.live("NFO", "bod")["id"] == s["id"] and readiness.live("NFO", "bod")["fingerprint"] == "fp-new"


# ---------------------------------------------------------------- build job (runs on GitHub)
class FakeResp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code, self._p, self.content = status, payload, content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._p


class FakeEngine:
    """Stands in for the engine's HTTP API inside the build job."""
    def __init__(self, files, blobs, live=None, last_failed=None, live_content=None):
        self.files, self.blobs = files, blobs
        self.live, self.last_failed, self.live_content = live or {}, last_failed or {}, live_content or {}
        self.items, self.snapshots = [], []

    def item(self, run_id, **it):
        self.items.append(it)

    def call(self, method, path, **kw):
        if path.startswith("/api/fetcher/build/inputs"):
            from app.fetcher_map import SOURCE_MAP
            return FakeResp(200, {"segment": "NFO", "loader": "NFO", "bases": ["bod", "latest"], "files": self.files,
                                  "source_map": SOURCE_MAP, "live": self.live, "last_failed": self.last_failed,
                                  "live_content": self.live_content})
        if path.startswith("/api/fetcher/uploads/"):
            uid = int(path.split("/")[4])
            return FakeResp(200, content=self.blobs[uid])
        if path.endswith("/snapshot"):
            d = kw["data"]
            self.snapshots.append({"basis": d["basis"], "meta": json.loads(d["meta"]), "unchanged": d.get("unchanged"),
                                   "file": kw.get("files", {}).get("file")})
            return FakeResp(201, {"snapshot": {"id": len(self.snapshots)}})
        raise AssertionError(path)


def _f(uid, name, data, seg="NFO", ftype="span", td="20261106", ref=None):
    return {"id": uid, "filename": name, "segment": seg, "file_type": ftype, "trade_date": td, "cycle": None,
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "source_ref": ref}


def test_materialize_lays_out_exfetch_folders(tmp_path):
    span = zip_bytes({"nsccl.20261106.i1.spn": span_xml(date="20261106")})
    gz = gzip.compress(b"contract master")
    files = [_f(1, "nsccl.20261106.i1.zip", span, ref="nfo.span:cycle=i1"),
             _f(2, "BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv", BHAV, seg="NSECM", ftype="bhavcopy"),     # manual upload
             _f(3, "NSE_FO_contract_06112026.csv.gz", gz, ftype="contract_master", ref="nfo.contract_master")]
    eng = FakeEngine(files, {1: span, 2: BHAV, 3: gz})
    from app.fetcher_map import SOURCE_MAP
    names = build_job.materialize(eng, files, tmp_path, SOURCE_MAP, log=lambda *a: None)
    assert (tmp_path / "NFO/span/2026-11-06/nsccl.20261106.i1.spn").exists()          # unzipped like exfetch
    assert (tmp_path / "EQ/bhavcopy/2026-11-06/BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv").read_bytes() == BHAV
    assert (tmp_path / "NFO/contract_master/2026-11-06/NSE_FO_contract_06112026.csv").read_bytes() == b"contract master"
    assert "nsccl.20261106.i1.spn" in names[1]
    bad = FakeEngine(files[:1], {1: b"tampered"})
    with pytest.raises(RuntimeError, match="checksum"):
        build_job.materialize(bad, files[:1], tmp_path / "x", SOURCE_MAP, log=lambda *a: None)


def test_build_segments_builds_skips_and_reports_failures(tmp_path, monkeypatch):
    span = zip_bytes({"nsccl.20261106.i1.spn": span_xml(date="20261106")})
    files = [_f(1, "nsccl.20261106.i1.zip", span, ref="nfo.span:cycle=i1")]
    built = []

    def fake_build(loader_key, basis, data_dir, out_dir, today, log):
        assert (Path(data_dir) / "NFO/span/2026-11-06/nsccl.20261106.i1.spn").exists()
        if basis == "latest":
            from loader.nfo import LoadCheckFailed
            raise LoadCheckFailed("too few risk arrays")
        p = Path(out_dir) / basis / "NFO.sqlite"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"db")
        built.append(basis)
        return p, {"span_cycle": "i1", "span_file": "nsccl.20261106.i1.spn", "trade_date": "2026-11-06",
                   "span_business_date": "2026-11-06"}
    monkeypatch.setattr(build_job, "build_one", fake_build)
    monkeypatch.setattr(build_job, "prime_calendar", lambda *a: None)
    today = date(2026, 11, 6)

    eng = FakeEngine(files, {1: span})
    counts = build_job.build_segments(eng, 7, ["NFO"], today=today, log=lambda *a: None)
    assert counts == {"built": 1, "failed": 1}
    ok, bad = eng.snapshots
    assert ok["basis"] == "bod" and gzip.decompress(ok["file"][1]) == b"db"
    assert ok["meta"]["inputs"][0]["used"] is True and ok["meta"]["rules"]["static_rates.yaml"]
    assert bad["basis"] == "latest" and bad["meta"]["error_kind"] == "check_failed" and bad["file"] is None

    # same inputs again: bod is up to date, latest failed with the same inputs -> neither rebuilt
    fp = {b: build_job.fingerprint(files, build_job.rules_digest(), b, today) for b in ("bod", "latest")}
    eng2 = FakeEngine(files, {1: span}, live={"bod": fp["bod"]},
                      last_failed={"latest": {"fingerprint": fp["latest"], "id": 2, "message": "too few risk arrays"}})
    counts = build_job.build_segments(eng2, 8, ["NFO"], today=today, log=lambda *a: None)
    assert counts == {"have": 1, "failed": 1} and not eng2.snapshots and built == ["bod"]

    # a Rebuild forces both; unchanged content -> nothing new stored
    ck = build_job.content_key(build_job.used_inputs(files, {1: ["nsccl.20261106.i1.zip", "nsccl.20261106.i1.spn"]},
                                                     {"span_file": "nsccl.20261106.i1.spn"}),
                               build_job.rules_digest(), "bod", {"trade_date": "2026-11-06", "span_file": "nsccl.20261106.i1.spn"})
    eng3 = FakeEngine(files, {1: span}, live={"bod": "old"}, live_content={"bod": ck})
    build_job.build_segments(eng3, 9, ["NFO"], today=today, log=lambda *a: None, force=True)
    assert eng3.snapshots[0]["unchanged"] == "1"
