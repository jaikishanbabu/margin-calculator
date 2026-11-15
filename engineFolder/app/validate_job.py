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
# Per segment: the days the broker baskets and tests need, what to download, and the runner.
PLANS = {
    "NFO": {"from": "2026-10-22", "to": "2026-10-28", "runner": "run_nfo.py",
            "need": ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"],  # 20 Oct Dussehra
            # not fo_mktlots: only today's exists, and it would be wrong for October
            "sources": ["nfo.span", "nfo.contract_master", "nfo.elm", "nfo.bhavcopy", "nfo.settlement_prices",
                        "nfo.ban_list", "nse.bhavcopy"]},
    "NSECM": {"from": "2026-10-23", "to": "2026-10-27", "runner": "run_nse.py",
              "need": ["2026-10-23", "2026-10-26", "2026-10-27"],
              # ASM / GSM / security lists only exist for today: October's can't be downloaded now
              "sources": ["nse.var", "nse.security", "nse.bhavcopy", "nse.regulatory_indicator"]},
}
NAMES = {"NFO": "NSE F&O", "NSECM": "NSE Cash"}


def inventory(segment):
    sys.path.insert(0, str(ROOT / "validation"))
    if segment == "NSECM":
        from run_nse import data_inventory
    else:
        from run_nfo import data_inventory
    return data_inventory(DATA)


def load_surveillance(eng, item):
    """NSE publishes only its current ASM / GSM lists, so October's can't be downloaded. The newest copies
    the engine has archived are put with the October files (the report says so)."""
    import shutil
    r = eng.call("GET", "/api/fetcher/build/inputs?segment=NSECM")
    files = r.json().get("files", []) if r.status_code == 200 else []
    folder = DATA / "EQ" / "surveillance" / PLANS["NSECM"]["from"]
    got = []
    for name in ("nse_asm.json", "nse_gsm.json"):
        f = max((x for x in files if x["filename"] == name), key=lambda x: (x["trade_date"], x["id"]), default=None)
        if not f:
            continue
        d = eng.call("GET", f"/api/fetcher/uploads/{f['id']}/original", timeout=120)
        if d.status_code == 200:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(d.content)
            got.append(f"{name.split('_')[1][:3].upper()} of {f['trade_date'][6:8]}-{f['trade_date'][4:6]}")
    note = ("ASM / GSM: " + ", ".join(got) + " lists (NSE publishes only current lists)") if got else \
        "ASM / GSM lists: none archived yet"
    item("validate:surveillance", "archived" if got else "failed", note)
    return note


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
    segs = r.json().get("segments") or ["NFO"]
    seg = segs[0] if segs and segs[0] in PLANS else "NFO"
    plan = PLANS[seg]

    def item(ref, status, detail=""):
        eng.item(rid, source_ref=ref, segment=seg, file_type="validation", status=status, detail=detail[:480])
        print(f"  {status:9} {ref} {detail[:200]}")

    item("validate:data", "running", f"checking the {plan['from'][8:]}-{plan['to'][8:]} Oct files")
    inv = inventory(seg)
    missing = [d for d in plan["need"] if not inv.get(d)]
    if missing:
        item("validate:data", "running", f"downloading {', '.join(missing)} from NSE's archives")
        p = subprocess.run([sys.executable, "-m", "exfetch", "--config", str(ROOT / "exfetch_sources.yaml"),
                            "--data-dir", str(DATA), "backfill", "--from", plan["from"], "--to", plan["to"],
                            "--only", *plan["sources"]],
                           cwd=ROOT, capture_output=True, text=True, timeout=2400)
        print(p.stdout[-6000:], p.stderr[-3000:])
        inv = inventory(seg)
    have = "; ".join(f"{d[8:10]}-{d[5:7]}: {', '.join(c) or 'none'}" for d, c in sorted(inv.items()))
    still = [d for d in plan["need"] if not inv.get(d)]
    item("validate:data", "failed" if still else "archived",
         (f"missing {', '.join(still)} - " if still else "") +
         f"{'VaR batches' if seg == 'NSECM' else 'SPAN cycles'} on disk: {have}")

    surv_note = ""
    if seg == "NSECM":
        surv_note = load_surveillance(eng, item)

    item("validate:run", "running", "running the engine tests and the broker baskets")
    out = ROOT / "validation" / "report.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, str(ROOT / "validation" / plan["runner"]), "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=3600, env=env)
    print(p.stdout[-20000:], p.stderr[-5000:])
    if not out.exists():
        item("validate:run", "failed", f"the validation crashed: {p.stderr[-400:]}")
        eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "failed", "message": "validation crashed"})
        return
    report = json.loads(out.read_text())
    report["code_version"] = os.getenv("GITHUB_SHA", "local")[:40]
    if surv_note:
        report["surveillance_note"] = surv_note
    s = report["summary"]
    r = eng.call("POST", f"/api/fetcher/runs/{rid}/validation", json={"segment": seg, "report": report}, timeout=120)
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
