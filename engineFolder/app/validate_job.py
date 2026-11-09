"""Stage 3: NFO validation on GitHub (validate.yml), started from the Data readiness page.

1. Makes sure the exchange files of 22-28 Oct 2026 (the days the broker baskets were captured,
   plus the days before them) are in validation/data/ - GitHub's cache keeps them between runs;
   missing days are downloaded from NSE's archives with exfetch.
2. Runs validation/run_nfo.py: the owner's engine tests and every broker basket.
3. Sends the report to the engine, which marks NFO validated only if everything that has broker
   figures passes.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from app.fetch_job import Engine

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "validation" / "data"
FROM, TO = "2026-10-22", "2026-10-28"
NEED = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]   # 20 Oct Dussehra, weekends
SOURCES = ["nfo.span", "nfo.contract_master", "nfo.elm", "nfo.bhavcopy", "nfo.settlement_prices", "nfo.ban_list",
           "nse.bhavcopy"]          # not fo_mktlots: only today's exists, and it would be wrong for October


def inventory():
    sys.path.insert(0, str(ROOT / "validation"))
    from run_nfo import data_inventory
    return data_inventory(DATA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    a = ap.parse_args()
    eng = Engine(os.environ["ENGINE_URL"], os.environ["FETCHER_TOKEN"])
    eng.wake()
    r = eng.call("POST", f"/api/fetcher/runs/{a.run_id}/start", json={"github_run_id": os.getenv("GITHUB_RUN_ID")})
    if r.status_code != 200:
        raise SystemExit(f"cannot start run: HTTP {r.status_code} {r.text[:200]}")
    rid = r.json()["run_id"]

    def item(ref, status, detail=""):
        eng.item(rid, source_ref=ref, segment="NFO", file_type="validation", status=status, detail=detail[:480])
        print(f"  {status:9} {ref} {detail[:200]}")

    item("validate:data", "running", "checking the 22-28 Oct files")
    inv = inventory()
    missing = [d for d in NEED if not inv.get(d)]
    if missing:
        item("validate:data", "running", f"downloading {', '.join(missing)} from NSE's archives")
        p = subprocess.run([sys.executable, "-m", "exfetch", "--config", str(ROOT / "exfetch_sources.yaml"),
                            "--data-dir", str(DATA), "backfill", "--from", FROM, "--to", TO, "--only", *SOURCES],
                           cwd=ROOT, capture_output=True, text=True, timeout=2400)
        print(p.stdout[-6000:], p.stderr[-3000:])
        inv = inventory()
    have = "; ".join(f"{d[8:10]}-{d[5:7]}: {', '.join(c) or 'none'}" for d, c in sorted(inv.items()))
    still = [d for d in NEED if not inv.get(d)]
    item("validate:data", "failed" if still else "archived",
         (f"missing {', '.join(still)} - " if still else "") + f"SPAN cycles on disk: {have}")

    item("validate:run", "running", "running the engine tests and the broker baskets")
    out = ROOT / "validation" / "report.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, str(ROOT / "validation" / "run_nfo.py"), "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=3600, env=env)
    print(p.stdout[-20000:], p.stderr[-5000:])
    if not out.exists():
        item("validate:run", "failed", f"the validation crashed: {p.stderr[-400:]}")
        eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "failed", "message": "validation crashed"})
        return
    report = json.loads(out.read_text())
    report["code_version"] = os.getenv("GITHUB_SHA", "local")[:40]
    s = report["summary"]
    r = eng.call("POST", f"/api/fetcher/runs/{rid}/validation", json={"segment": "NFO", "report": report}, timeout=120)
    if r.status_code != 201:
        item("validate:run", "failed", f"engine said {r.status_code}: {r.text[:300]}")
    else:
        v = r.json()["validation"]
        item("validate:run", "built" if v["status"] == "passed" else "failed",
             f"{v['status'].upper()}: baskets {s.get('baskets_passed', 0)} passed, {s.get('baskets_lagged', 0)} passed on an "
             f"older file, {s.get('baskets_failed', 0)} failed, {s.get('baskets_error', 0)} could not run, "
             f"{s.get('baskets_pending', 0)} without figures; tests {s.get('tests_passed', 0)} passed, "
             f"{s.get('tests_failed', 0)} failed")
    eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "done", "message": f"validation: {s}"[:300]})


if __name__ == "__main__":
    main()
