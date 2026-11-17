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
import gzip
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from app import holidays

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


def is_compressed(data):
    return data[:4] == b"PK\x03\x04" or data[:2] == b"\x1f\x8b"


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

    # ---- direct storage transfer (app/transfer.py): big files skip the engine host's bandwidth ----
    direct_ok = True     # flips off for the rest of the run after a direct-transfer failure

    def _plain(self, method, url, **kw):
        """Signed storage links carry their own token: never send the fetcher token there."""
        for attempt in range(3):
            try:
                return requests.request(method, url, timeout=300, **kw)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5)

    def post_file(self, run_id, path, form, filename, data, content_type="application/octet-stream", allow_gzip=True):
        """POST a file to an engine endpoint that accepts `file` or `staged_key`. Tries the direct route first."""
        if self.direct_ok:
            try:
                slot = self.call("POST", f"/api/fetcher/runs/{run_id}/stage", json={"filename": filename})
                slot = slot.json().get("slot") if slot.status_code == 200 else None
                if slot:
                    enc, payload = "identity", data
                    if allow_gzip and len(data) > 256 * 1024 and not is_compressed(data):
                        enc, payload = "gzip", gzip.compress(data, compresslevel=6, mtime=0)
                    put = self._plain("PUT", slot["url"], data=payload,
                                      headers={"Content-Type": "application/gzip" if enc == "gzip" else content_type,
                                               "x-upsert": "false", "cache-control": "no-store"})
                    if put.status_code in (200, 201):
                        f2 = dict(form, staged_key=slot["key"], staged_encoding=enc, filename=filename)
                        r = self.call("POST", path, data=f2, timeout=300)
                        if r.status_code != 400 or "staged" not in r.text:
                            return r
                    print(f"direct upload failed (HTTP {put.status_code}) - sending through the engine instead")
                    self.direct_ok = False
            except requests.RequestException as e:
                print(f"direct upload failed ({e}) - sending through the engine instead")
                self.direct_ok = False
        return self.call("POST", path, data=form, files={"file": (filename, data, content_type)}, timeout=300)

    def original(self, upload_id):
        """Original bytes of an archived upload: signed direct link if offered, else through the engine."""
        if self.direct_ok:
            r = self.call("GET", f"/api/fetcher/uploads/{upload_id}/original?link=1", timeout=300)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                link = r.json()["link"]
                d = self._plain("GET", link["url"])
                if d.status_code == 200:
                    return gzip.decompress(d.content) if link["encoding"] == "gzip" else d.content
                print(f"direct download failed (HTTP {d.status_code}) - using the engine instead")
                self.direct_ok = False
            elif r.status_code == 200:
                return r.content
        r = self.call("GET", f"/api/fetcher/uploads/{upload_id}/original", timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content

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


# Reference files may come from up to FALLBACK_DAYS trading days earlier when the trade date's copy
# isn't published (the build notes it). SPAN, VaR and prices are always the trade date's own.
REFERENCE_TYPES = {"contract_master", "security_master", "lot_sizes", "elm", "ban_list", "surveillance"}
FALLBACK_DAYS = 2


def due(src_phase, trade_date, now, trigger):
    if trigger != "auto" or src_phase != "eod":
        return True
    if trade_date > now.date():          # evening run for the next day: its end-of-day files don't exist yet
        return False
    if trade_date < now.date():
        return True
    return now.strftime("%H:%M") >= EOD_NOT_BEFORE


def segment_dates(d, today, trigger, segments, trading):
    """Per segment: the run's date if its exchange trades then; an evening run for a future date moves
    on past that exchange's holidays; a daytime auto run on a holiday skips the segment."""
    seg_date, seg_skip = {}, {}
    for seg in segments:
        ex = holidays.EXCHANGE_OF.get(seg, "NSE")
        sd = d
        if not trading(ex, d):
            if d > today or trigger != "auto":     # evening run, or a manual run on a weekend/holiday
                for _ in range(10):
                    sd += timedelta(days=1)
                    if trading(ex, sd):
                        break
            elif trigger == "auto":
                seg_skip[seg] = f"{d} is a {ex.replace('_', ' ')} holiday"
        seg_date[seg] = sd
    return seg_date, seg_skip


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
    nse_cal = Calendar(Path(tempfile.mkdtemp()), lambda u: http.get_json(u, "nse"))   # fallback only
    calj = eng.call("GET", "/api/fetcher/calendar").json()
    cal = holidays.Cal(calj["entries"])
    rebuild, calendar_run = trigger == "rebuild", trigger == "calendar"
    today = datetime.now(IST).date()

    if calendar_run:                          # import the exchanges' holiday pages, nothing else
        from app.calendar_job import import_all
        counts = import_all(eng, rid, http)
        http.close()
        summary = ", ".join(f"calendar {k} {v}" for k, v in sorted(counts.items()))
        eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "done", "message": summary or "nothing found"})
        print("finished:", summary)
        return

    def trading(ex, day):
        """The admin's calendar for that exchange; NSE's own list if that year isn't entered yet."""
        if cal.has_year(ex, day.year):
            return cal.is_trading_day(ex, day)
        try:
            return nse_cal.is_trading_day(day, {"NSE": "EQ", "NSE_CD": "CDS", "BSE": "EQ", "MCX": "MCX"}[ex])
        except Exception as e:
            print(f"holiday check failed ({e}); weekends only")
            return day.weekday() < 5

    # each segment's trading date, by its own exchange's calendar
    seg_date, seg_skip = segment_dates(d, today, trigger, {v[0] for v in smap.values()}, trading)
    print("trading dates:", {k: str(v) for k, v in sorted(seg_date.items())}, "skipped:", seg_skip)

    have_refs, have_dates = set(), set()
    for sd in sorted(set(seg_date.values())):
        sdt = sd.strftime("%Y%m%d")
        have_dates.add(sdt)
        for h in eng.call("GET", f"/api/fetcher/have?trade_date={sdt}").json()["have"]:
            have_refs.add((sdt, h["source_ref"]))

    sources = [s for s in cat["sources"] if s.get("enabled", True) and s["id"] in smap
               and ("ALL" in want or smap[s["id"]][0] in want)] if not rebuild else []
    if trigger == "auto" and sources and all(smap[x["id"]][0] in seg_skip for x in sources):
        eng.call("POST", f"/api/fetcher/runs/{rid}/finish", json={"status": "skipped",
                 "message": "; ".join(sorted(set(seg_skip.values())))})
        print("not a trading day for any segment - nothing to do")
        return
    plan = []
    for src in sources:
        for variant, loop, phase in Agent.variants(src):
            ref = src["id"] + ("" if variant == "-" else f":{variant}")
            plan.append((src, variant, loop, phase, ref))
    for src, variant, loop, phase, ref in plan:     # show the whole list as queued first
        seg, ft = smap[src["id"]]
        eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status="queued", detail="",
                 trade_date=seg_date[seg].strftime("%Y%m%d"))

    counts = {}
    # A day's later SPAN cycles / VaR batches never come before its first one (i1, batch 1):
    # until the first is archived, the rest aren't even checked.
    first_variant = {s["id"]: Agent.variants(s)[0][0] for s in sources if s.get("cycles") or s.get("batches")}
    first_ok = {}
    for src, variant, loop, phase, ref in plan:
        seg, ft = smap[src["id"]]
        sid = src["id"]
        versions, refresh = src.get("versions", False), src.get("refresh", False)
        sd = seg_date[seg]
        sdt = sd.strftime("%Y%m%d")
        is_first = first_variant.get(sid) == variant

        def report(status, detail="", upload_id=None):
            if is_first:
                first_ok[sid] = status in ("archived", "have")
            counts[status] = counts.get(status, 0) + 1
            eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status=status, detail=detail[:480], upload_id=upload_id,
                     trade_date=sdt)
            print(f"  {status:9} {ref:38} {sdt} {detail[:120]}")

        if seg in seg_skip:
            report("not_due", seg_skip[seg])
            continue
        if sid in first_variant and not is_first and not first_ok.get(sid, False):
            first = first_variant[sid].split("=")[-1]
            report("not_due", f"waiting for {'batch ' if src.get('batches') else ''}{first} - later files come only after it")
            continue
        if not due(phase, sd, now, trigger):
            report("not_due", f"end-of-day file - not checked before {EOD_NOT_BEFORE} IST")
            continue
        if not (versions or refresh) and (sdt, ref) in have_refs:
            report("have", "already archived today")
            continue
        eng.item(rid, source_ref=ref, segment=seg, file_type=ft, status="running", detail="", trade_date=sdt)
        errors, kind = [], "pending"
        done = False
        days = [sd]
        if ft in REFERENCE_TYPES and src.get("dated", True) and not versions:
            ex = holidays.EXCHANGE_OF.get(seg, "NSE")
            back = sd
            for _ in range(FALLBACK_DAYS):
                for _ in range(10):
                    back -= timedelta(days=1)
                    if trading(ex, back):
                        break
                days.append(back)
        for fd in days:
            fdt = fd.strftime("%Y%m%d")
            note = "" if fd == sd else f" - {fd:%d-%m} copy ({sd:%d-%m} not published)"
            if fd != sd:
                if fdt not in have_dates:
                    have_dates.add(fdt)
                    for h in eng.call("GET", f"/api/fetcher/have?trade_date={fdt}").json()["have"]:
                        have_refs.add((fdt, h["source_ref"]))
                if (fdt, ref) in have_refs:
                    report("have", f"already archived{note}")
                    done = True
                    break
            for strategy in src["try"]:
                if fd != sd and "template" not in strategy:
                    continue                     # exchange listings only show the current day
                try:
                    cands = resolve(strategy, fd, loop, http, src.get("session"))
                    if not versions:
                        cands = cands[-1:]
                    new, skipped, failed = [], 0, []
                    for c in cands:
                        vref = f"{ref}:{c.name}" if versions else ref
                        if versions and (fdt, vref) in have_refs:
                            skipped += 1
                            continue
                        data = c.data if c.data is not None else http.get(c.url, src.get("session")).content
                        if not looks_like_archive_ok(c.name, data):
                            raise NotPublished(f"corrupt/partial archive {c.url}")
                        form = {"source_id": sid, "source_ref": vref, "url": c.url, "replace": "1" if refresh else "",
                                "trade_date": fdt}
                        if ft == "span":
                            cyc = span_cycle(sid, loop, c.name)
                            if cyc:
                                form["cycle"] = cyc
                        up = eng.post_file(rid, f"/api/fetcher/runs/{rid}/upload", form, c.name, data)
                        if up.status_code == 201:
                            u = up.json()["upload"]
                            new.append(f"#{u['id']} {c.name} ({len(data):,} B)")
                            have_refs.add((fdt, vref))
                        elif up.status_code == 400 and "already uploaded" in up.text:
                            skipped += 1
                            have_refs.add((fdt, vref))
                        else:
                            failed.append(f"{c.name}: engine said {up.status_code} {up.json().get('error', '')[:200]}")
                    if failed:
                        report("failed", "; ".join(failed + new) + note)
                    elif new:
                        report("archived", "; ".join(new) + (f" (+{skipped} already had)" if skipped else "") + note)
                    else:
                        report("have", (f"{skipped} already archived" if skipped else "nothing new") + note)
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
            if done or kind == "blocked":
                break
        if not done:
            msg = errors[0] if errors else ""
            if len(days) > 1:
                msg += f" (also not found for {', '.join(f'{x:%d-%m}' for x in days[1:])})"
            report(kind, ("not published yet - " if kind == "pending" else "") + msg)
    # Stage 2C: parse what is archived into per-segment snapshots (skipped when nothing changed)
    from app.build_job import build_segments
    from app.readiness import PARSERS
    build_for = [s for s in PARSERS if "ALL" in want or s in want]
    try:
        bc = build_segments(eng, rid, build_for, lambda u: http.get_json(u, "nse"), force=rebuild,
                            calendar_entries=calj["entries"])
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
