"""Exchange fetcher job - runs on GitHub Actions (workflow fetcher.yml), not on Render.

  python -m app.fetch_job --run-id 42
  env: ENGINE_URL (staging URL), FETCHER_TOKEN

1. Tells the engine the run started and reads which segments / trade date to fetch.
2. Asks the engine what is already archived for that date -> those are skipped without
   contacting the exchange (so only the first run of the day downloads everything).
3. For every source (owner's exfetch catalogue) tries its strategies in order, and uploads
   each new file to the engine's upload pipeline (same checks, archive, checksum, audit).
4. Reports each file's status as it goes, so the admin page shows live progress.
"""
import argparse
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from exfetch.dates import Calendar          # noqa: E402
from exfetch.net import Blocked, Http, NotPublished   # noqa: E402
from exfetch.resolvers import resolve       # noqa: E402
from exfetch.runner import Agent            # noqa: E402
from exfetch.store import looks_like_archive_ok   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SOURCES = ROOT / "exfetch_sources.yaml"
EOD_NOT_BEFORE = "17:30"     # auto runs don't ask for end-of-day files before this (IST, on the trade date)


class Engine:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"X-Fetcher-Token": token, "User-Agent": "margin-fetcher"})

    def wake(self):
        for _ in range(8):
            try:
                if self.s.get(self.base + "/api/health", timeout=90).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(20)
        raise SystemExit("engine unreachable")

    def call(self, method, path, **kw):
        for attempt in range(3):
            try:
                r = self.s.request(method, self.base + path, timeout=kw.pop("timeout", 120), **kw)
                return r
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5)

    def item(self, run_id, **it):
        self.call("POST", f"/api/fetcher/runs/{run_id}/items", json={"items": [it]})


def span_cycle(sid, loop, name):
    """The cycle the exchange published this file under (from its official URL / listing)."""
    if loop.get("cycle"):
        return str(loop["cycle"])
    m = re.search(r"BSERISK\d{8}-(\w+)\.", name, re.I)              # BSE: -00 .. -04, -FINAL
    if m:
        return m.group(1).upper() if m.group(1).isalpha() else m.group(1)
    m = re.search(r"mcxrpf-\d{8}-(\d{4}-\d+(?:-[a-z])?)", name, re.I)   # MCX: HHMM-seq[-e]
    if m:
        return m.group(1).lower()
    return None


def due(src_phase, trade_date, now, trigger):
    if trigger != "auto" or src_phase != "eod":
        return True
    if trade_date > now.date():          # evening run for the next day: its end-of-day files don't exist yet
        return False
    if trade_date < now.date():
        return True
    return now.strftime("%H:%M") >= EOD_NOT_BEFORE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    a = ap.parse_args()
    eng = Engine(os.environ["ENGINE_URL"], os.environ["FETCHER_TOKEN"])
    eng.wake()
    r = eng.call("POST", f"/api/fetcher/runs/{a.run_id}/start", json={"github_run_id": os.getenv("GITHUB_RUN_ID")})
    if r.status_code != 200:
        raise SystemExit(f"cannot start run: HTTP {r.status_code} {r.text[:200]}")
    cfg_run = r.json()
    rid, td, trigger = cfg_run["run_id"], cfg_run["trade_date"], cfg_run["trigger"]
    smap = cfg_run["source_map"]
    want = set(cfg_run["segments"])
    d = date(int(td[:4]), int(td[4:6]), int(td[6:]))
    now = datetime.now(IST)
    print(f"run #{rid}: {trigger}, trade date {d}, segments {sorted(want)}")

    cat = yaml.safe_load(SOURCES.read_text())
    dflt = cat.get("defaults", {})
    http = Http(cat.get("sessions", {}), min(int(dflt.get("timeout", 60)), 45), min(int(dflt.get("retries", 3)), 2),
                dflt.get("min_interval_sec", 0.6), browser_headless=True)
    cal = Calendar(Path(tempfile.mkdtemp()), lambda u: http.get_json(u, "nse"))
    rebuild = trigger == "rebuild"
    today = datetime.now(IST).date()
    if not rebuild and d > today:            # an evening run for the next trading day: skip holidays
        try:
            nd = d
            for _ in range(10):
                if cal.is_trading_day(nd, "EQ"):
                    break
                nd += timedelta(days=1)
            if nd != d:
                r = eng.call("POST", f"/api/fetcher/runs/{rid}/trade-date", json={"trade_date": nd.strftime("%Y%m%d")})
                if r.status_code == 200:
                    print(f"{d} is an exchange holiday - fetching for {nd}")
                    d, td = nd, nd.strftime("%Y%m%d")
        except Exception as e:
            print(f"holiday check failed ({e}); keeping {d}")
    if trigger == "auto":
        try:
            if not cal.is_trading_day(d, "EQ"):
                eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "skipped", "message": f"{d} is not a trading day"})
                print("not a trading day - nothing to do")
                return
        except Exception as e:
            print(f"holiday check failed ({e}); continuing")

    have_rows = eng.call("GET", f"/api/fetcher/have?trade_date={td}").json()["have"]
    have_refs = {h["source_ref"] for h in have_rows}

    sources = [s for s in cat["sources"] if s.get("enabled", True) and s["id"] in smap
               and ("ALL" in want or smap[s["id"]][0] in want)] if not rebuild else []
    plan = []
    for src in sources:
        for variant, loop, phase in Agent.variants(src):
            ref = src["id"] + ("" if variant == "-" else f":{variant}")
            plan.append((src, variant, loop, phase, ref))
    for src, variant, loop, phase, ref in plan:     # show the whole list as queued first
        seg, ft = smap[src["id"]]
        eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status="queued", detail="")

    counts = {}
    for src, variant, loop, phase, ref in plan:
        seg, ft = smap[src["id"]]
        sid = src["id"]
        versions, refresh = src.get("versions", False), src.get("refresh", False)

        def report(status, detail="", upload_id=None):
            counts[status] = counts.get(status, 0) + 1
            eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status=status, detail=detail[:480], upload_id=upload_id)
            print(f"  {status:9} {ref:38} {detail[:120]}")

        if not due(phase, d, now, trigger):
            report("not_due", f"end-of-day file - not checked before {EOD_NOT_BEFORE} IST")
            continue
        if not (versions or refresh) and ref in have_refs:
            report("have", "already archived today")
            continue
        eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status="running", detail="")
        errors, kind = [], "pending"
        done = False
        for strategy in src["try"]:
            try:
                cands = resolve(strategy, d, loop, http, src.get("session"))
                if not versions:
                    cands = cands[-1:]
                new, skipped, failed = [], 0, []
                for c in cands:
                    vref = f"{ref}:{c.name}" if versions else ref
                    if versions and vref in have_refs:
                        skipped += 1
                        continue
                    data = c.data if c.data is not None else http.get(c.url, src.get("session")).content
                    if not looks_like_archive_ok(c.name, data):
                        raise NotPublished(f"corrupt/partial archive {c.url}")
                    form = {"source_id": sid, "source_ref": vref, "url": c.url, "replace": "1" if refresh else ""}
                    if ft == "span":
                        cyc = span_cycle(sid, loop, c.name)
                        if cyc:
                            form["cycle"] = cyc
                    up = eng.call("POST", f"/api/fetcher/runs/{rid}/upload", data=form,
                                  files={"file": (c.name, data, "application/octet-stream")}, timeout=300)
                    if up.status_code == 201:
                        u = up.json()["upload"]
                        new.append(f"#{u['id']} {c.name} ({len(data):,} B)")
                        have_refs.add(vref)
                    elif up.status_code == 400 and "already uploaded" in up.text:
                        skipped += 1
                        have_refs.add(vref)
                    else:
                        failed.append(f"{c.name}: engine said {up.status_code} {up.json().get('error', '')[:200]}")
                if failed:
                    report("failed", "; ".join(failed + new))
                elif new:
                    report("archived", "; ".join(new) + (f" (+{skipped} already had)" if skipped else ""))
                else:
                    report("have", f"{skipped} already archived" if skipped else "nothing new")
                done = True
                break
            except NotPublished as e:
                errors.append(str(e))
            except Blocked as e:
                kind = "blocked"
                errors.append(str(e))
            except Exception as e:
                kind = "failed" if kind != "blocked" else kind
                errors.append(f"{type(e).__name__}: {e}")
        if not done:
            msg = errors[-1] if errors else ""
            report(kind, ("not published yet - " if kind == "pending" else "") + msg)
    # Stage 2C: parse what is archived into per-segment snapshots (skipped when nothing changed)
    from app.build_job import build_segments
    from app.readiness import PARSERS
    build_for = [s for s in PARSERS if "ALL" in want or s in want]
    try:
        bc = build_segments(eng, rid, build_for, lambda u: http.get_json(u, "nse"), force=rebuild)
        for k, v in bc.items():
            counts[f"snapshot {k}"] = counts.get(f"snapshot {k}", 0) + v
    except Exception as e:
        print(f"build step failed: {type(e).__name__}: {e}")
        counts["snapshot error"] = 1
    http.close()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "done", "message": summary})
    print("finished:", summary)


if __name__ == "__main__":
    main()
