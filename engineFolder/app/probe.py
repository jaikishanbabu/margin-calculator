"""Stage 2A: can this machine download each exchange file?

Runs the owner's exfetch probe (every source, every strategy, nothing saved) for one date and
returns a table. Used two ways:

  python -m app.probe --date 2026-11-06           (GitHub Actions runner: prints a table)
  POST /api/ops/probe-exchanges?date=2026-11-06   (Render: same probe from the engine's server)

Only URLs from exfetch_sources.yaml are contacted (no user-supplied URLs).
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # engineFolder/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCES = ROOT / "exfetch_sources.yaml"
PROBE_TIMEOUT = 25        # seconds per request (exfetch default is 60)
PROBE_RETRIES = 1


def where_am_i():
    info = {"host": socket.gethostname(), "runner": "github-actions" if os.getenv("GITHUB_ACTIONS") else
            ("render" if os.getenv("RENDER") else "other")}
    try:
        import requests
        r = requests.get("https://ipinfo.io/json", timeout=10)
        j = r.json()
        info.update({"ip": j.get("ip"), "country": j.get("country"), "region": j.get("region"),
                     "city": j.get("city"), "org": j.get("org")})
    except Exception as e:  # informational only
        info["ip_lookup"] = f"failed: {type(e).__name__}"
    return info


def run_probe(d: date, only=None):
    import yaml
    from exfetch.runner import Agent
    cfg = yaml.safe_load(SOURCES.read_text())
    cfg.setdefault("defaults", {}).update({"timeout": PROBE_TIMEOUT, "retries": PROBE_RETRIES})
    tmp = Path(tempfile.mkdtemp(prefix="probe_"))
    (tmp / "sources.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    started = time.time()
    agent = Agent(tmp / "sources.yaml", data_dir=str(tmp / "data"))
    rows = agent.probe(d, only=only)
    seg_of = {s["id"]: s["segment"] for s in cfg["sources"]}
    out = []
    for sid, variant, strategy, status, detail in rows:
        out.append({"source": sid, "segment": seg_of.get(sid, "?"), "variant": variant,
                    "strategy": strategy, "status": status, "detail": detail})
    return {"date": d.isoformat(), "seconds": round(time.time() - started, 1), "where": where_am_i(),
            "rows": out, "summary": summarize(out)}


def summarize(rows):
    """Per source: OK if any strategy of any variant worked."""
    by = {}
    for r in rows:
        s = by.setdefault(r["source"], {"segment": r["segment"], "ok": 0, "tried": 0, "errors": set(),
                                        "ok_example": None, "fail_example": None})
        s["tried"] += 1
        if r["status"] == "OK":
            s["ok"] += 1
            s["ok_example"] = s["ok_example"] or r["detail"][:160]
        else:
            s["errors"].add(r["status"])
            s["fail_example"] = s["fail_example"] or f"{r['variant']} {r['strategy']}: {r['detail'][:160]}"
    return {k: {"segment": v["segment"], "result": "OK" if v["ok"] else "FAILED", "ok_attempts": v["ok"],
                "attempts": v["tried"], "failure_kinds": sorted(v["errors"]),
                "ok_example": v["ok_example"], "fail_example": v["fail_example"]} for k, v in by.items()}


# ---- background runner for the Render endpoint -------------------------------------
_state = {"running": False, "result": None, "started_at": None}
_lock = threading.Lock()


def start_background(d: date):
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, result=None, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def work():
        try:
            res = run_probe(d)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        with _lock:
            _state.update(running=False, result=res)
    threading.Thread(target=work, daemon=True).start()
    return True


def status():
    with _lock:
        return dict(_state)


def markdown(res):
    lines = [f"### Exchange download probe — {res['where'].get('runner')} "
             f"({res['where'].get('city')}, {res['where'].get('country')}, {res['where'].get('org')})",
             f"Date probed: {res['date']} · took {res['seconds']} s", "",
             "| Segment | Source | Result | Working attempts | Example |", "|---|---|---|---|---|"]
    for sid, s in sorted(res["summary"].items(), key=lambda kv: (kv[1]["segment"], kv[0])):
        ex = (s["ok_example"] if s["result"] == "OK" else s["fail_example"]) or ""
        lines.append(f"| {s['segment']} | {sid} | {'✅' if s['result'] == 'OK' else '❌'} {s['result']} | "
                     f"{s['ok_attempts']}/{s['attempts']} | {ex.replace('|', '/')} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--json", help="write full results here")
    a = ap.parse_args()
    res = run_probe(date.fromisoformat(a.date))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))
    md = markdown(res)
    print(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n\n")


if __name__ == "__main__":
    main()
