"""Stage 2C: parse archived exchange files into per-segment snapshots - runs on GitHub Actions.

Called by app.fetch_job after a fetch (and for "rebuild" runs, which fetch nothing):

1. Asks the engine which archived files a segment's loader needs (newest trade dates only).
2. Downloads them and lays them out as the data/<SEG>/<kind>/<YYYY-MM-DD>/ folders the owner's
   exfetch kept (unpacked the same way), so the vendored `loader` runs unchanged.
3. Builds each basis - bod (the trade date's start-of-day SPAN) and latest (newest cycle) -
   unless the live snapshot was already built from exactly these inputs, rules and code.
4. Uploads the gzipped database with what it was built from; a failed load is reported with
   the reason and the previous snapshot stays in use.
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import traceback
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LOADER_DIR = ROOT / "loader"
RULE_FILES = ("static_rates.yaml", "mcx_contracts.yaml")
LOADER_VERSION = "loader-2026-10-31+mc2c.1"
SOURCES = ROOT / "exfetch_sources.yaml"
BASIS_NAMES = {"bod": "start-of-day", "latest": "latest cycle"}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def rules_digest():
    return {f: _sha((LOADER_DIR / f).read_bytes()) for f in RULE_FILES if (LOADER_DIR / f).exists()}


def folders():
    """exfetch source id -> (data/ segment folder, kind), from the owner's sources.yaml."""
    cat = yaml.safe_load(SOURCES.read_text())
    return {s["id"]: (s["segment"], s["kind"]) for s in cat["sources"]}


def folder_for(f, src_folders, source_map):
    sid = (f.get("source_ref") or "").split(":")[0]
    if sid not in src_folders:     # a manual upload: first catalogue source of that segment + file type
        sid = next((k for k, v in source_map.items() if tuple(v) == (f["segment"], f["file_type"])), None)
    return src_folders.get(sid) if sid else None


def iso_day(yyyymmdd):
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


# Originals already downloaded by an earlier run (GitHub Actions cache, see fetcher.yml). Keyed by the
# SHA-256 the engine recorded, and re-checked on read, so a stale or damaged cache can't feed a build.
CACHE_DIR = os.environ.get("ORIGINALS_CACHE", "")


def cache_get(sha):
    if not CACHE_DIR or not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return None
    p = Path(CACHE_DIR) / sha
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if _sha(data) != sha:
        p.unlink(missing_ok=True)
        return None
    os.utime(p)          # keep recently used files when the cache is pruned
    return data


def cache_put(sha, data):
    if not CACHE_DIR or not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return
    try:
        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
        tmp = Path(CACHE_DIR) / (sha + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, Path(CACHE_DIR) / sha)
    except OSError:
        pass


def materialize(eng, files, data_dir, source_map, log=print):
    """Download + unpack into data/<SEG>/<kind>/<YYYY-MM-DD>/. Returns {upload_id: [names on disk]}."""
    from exfetch.store import unpack
    src_folders = folders()
    names = {}
    for f in files:
        where = folder_for(f, src_folders, source_map)
        if not where:
            log(f"    skip #{f['id']} {f['filename']}: no folder for {f['segment']}/{f['file_type']}")
            continue
        folder = Path(data_dir) / where[0] / where[1] / iso_day(f["trade_date"])
        folder.mkdir(parents=True, exist_ok=True)
        content = cache_get(f["sha256"])
        if content is None:
            try:
                if hasattr(eng, "original"):
                    content = eng.original(f["id"])
                else:
                    r = eng.call("GET", f"/api/fetcher/uploads/{f['id']}/original", timeout=300)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    content = r.content
            except Exception as e:
                raise RuntimeError(f"could not download #{f['id']} {f['filename']}: {e}")
            if _sha(content) != f["sha256"]:
                raise RuntimeError(f"#{f['id']} {f['filename']}: checksum mismatch after download")
            cache_put(f["sha256"], content)
        before = set(os.listdir(folder))
        path = folder / f["filename"]
        path.write_bytes(content)
        unpack(path, folder)
        names[f["id"]] = sorted({f["filename"]} | (set(os.listdir(folder)) - before))
    return names


def prime_calendar(data_dir, fetch_json, calendar_entries=None, today=None):
    """The loader reads holiday lists from data/_cache (no network of its own): the admin's Holiday
    calendar for every year it covers; NSE's own list for the current year if NSE's isn't entered."""
    from exfetch.dates import Calendar, today_ist
    from app.holidays import Cal
    cache = Path(data_dir) / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    today = today or today_ist()
    cal = Cal(calendar_entries or [])
    years = set().union(*cal.years.values()) if cal.years else set()
    for y in sorted(years):
        if cal.has_year("NSE", y):
            (cache / f"holidays_{y}.json").write_text(json.dumps(cal.loader_cache(y)))
    if not cal.has_year("NSE", today.year):
        try:
            Calendar(cache, fetch_json)._load()
        except Exception as e:
            print(f"    holiday list unavailable ({e}); weekends only")


def newest_day(data_dir, folder_seg, kind):
    base = Path(data_dir) / folder_seg / kind
    days = sorted(p.name for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)) if base.is_dir() else []
    return date.fromisoformat(days[-1]) if days else None


def fingerprint(files, rules, basis, today):
    parts = {"inputs": sorted([f["id"], f["sha256"]] for f in files), "rules": rules, "loader": LOADER_VERSION,
             "basis": basis, "day": today.isoformat() if basis == "bod" else None}
    return _sha(json.dumps(parts, sort_keys=True).encode())[:40]


def classify(e):
    from loader.files import MissingFile
    from loader.nfo import LoadCheckFailed
    if isinstance(e, MissingFile):
        return "missing_inputs"
    if isinstance(e, LoadCheckFailed):
        return "check_failed"
    return "error"


FALLBACK_DAYS = 2                     # a reference file may be at most this many trading days older than expected
NO_FALLBACK = ("span", "var")         # risk files: always from the trade date (the loader asks for them exactly)


def _is_price(key):
    return "bhavcopy" in key or "settlement" in key


def _ddmm(iso):
    return f"{iso[8:10]}-{iso[5:7]}"


def check_reference_age(loader_key, info, data_dir):
    """Reference files (contract/security master, lots, ELM/exposure, ban list, surveillance lists) may come
    from up to FALLBACK_DAYS trading days before the day they are expected for, when the exchange has not
    published that day's copy. SPAN, VaR and prices never fall back. Sets info["fallback_inputs"];
    raises MissingFile when a required reference file is older than that."""
    import loader as L
    from loader.files import MissingFile
    from loader.nfo import previous_trading_day
    mod = L.SEGMENTS[loader_key]
    cal_seg = getattr(mod, "SEGMENT", loader_key)
    anchor = next((info.get(f"{k}_trade_date") for k in NO_FALLBACK if info.get(f"{k}_trade_date")), None)
    if not anchor:
        return
    wants = getattr(mod, "WANTS", {})
    notes, too_old = [], []
    for k in sorted(info):
        if not k.endswith("_trade_date") or k == "trade_date":
            continue
        key = k[:-len("_trade_date")]
        day = info[k]
        if key in NO_FALLBACK or _is_price(key) or not day:
            continue
        expected = info.get("master_expected_date") if key == "contract_master" and info.get("master_expected_date") \
            else anchor
        if day >= expected:
            continue
        allowed, d = [], expected
        for _ in range(FALLBACK_DAYS):
            d = previous_trading_day(data_dir, d, cal_seg)
            allowed.append(d)
        label = key.replace("_", " ")
        if day >= allowed[-1]:
            notes.append(f"{label} from {_ddmm(day)} ({_ddmm(expected)} not published)")
        else:
            w = wants.get(key)
            (too_old if (w is None or w.required) else notes).append(
                f"{label} from {_ddmm(day)} - {_ddmm(expected)} and the {FALLBACK_DAYS} trading days before it "
                f"are missing" + ("" if (w is None or w.required) else " (optional, used anyway)"))
    if too_old:
        raise MissingFile("; ".join(too_old))
    if notes:
        info["fallback_inputs"] = "; ".join(notes)


def build_one(loader_key, basis, data_dir, out_dir, today, log):
    """Run the vendored loader for one basis. Returns (db_path, info)."""
    import loader as L
    mod = L.SEGMENTS[loader_key]
    folder_seg = L.data_segment(loader_key)
    db_dir = Path(out_dir) / basis
    if basis == "bod":
        info = L.load_segment(loader_key, data_dir, db_dir, basis="bod", today=today, log=log, force=True)
    else:
        kind = "var" if loader_key == "NSE" else "span"
        day = newest_day(data_dir, folder_seg, kind)
        if not day:
            from loader.files import MissingFile
            raise MissingFile(f"no {kind} files for {loader_key}")
        info = L.load_segment(loader_key, data_dir, db_dir, on_date=day, log=log, force=True)
    check_reference_age(loader_key, info, data_dir)
    if hasattr(mod, "describe"):
        info["describe"] = mod.describe(info)
    return db_dir / f"{loader_key}.sqlite", info


def used_inputs(files, names, info):
    used_names = {str(v) for k, v in info.items() if k.endswith("_file")}
    out = []
    for f in files:
        on_disk = names.get(f["id"], [])
        out.append({"upload_id": f["id"], "filename": f["filename"], "segment": f["segment"],
                    "file_type": f["file_type"], "trade_date": f["trade_date"], "sha256": f["sha256"],
                    "used": bool(used_names & set(on_disk))})
    return out


def build_segments(eng, run_id, segments, fetch_json=None, today=None, log=print, force=False, calendar_entries=None):
    """Build snapshots for each segment that has a parser. Returns counts {built, have, failed}."""
    from exfetch.dates import today_ist
    today = today or today_ist()
    rules = rules_digest()
    if calendar_entries:          # holidays change the loader's previous-day / start-of-day choices
        rules["holiday_calendar"] = _sha(json.dumps(sorted(calendar_entries, key=lambda e: (e["exchange"], e["day"])),
                                                    sort_keys=True).encode())
    counts = {}

    def item(seg, basis, status, detail=""):
        if status in ("built", "have", "failed"):
            counts[status] = counts.get(status, 0) + 1
        eng.item(run_id, source_ref=f"build:{seg}:{basis}", segment=seg, file_type="snapshot", status=status,
                 detail=detail[:480])
        log(f"  {status:9} build:{seg}:{basis:7} {detail[:140]}")

    for seg in segments:
        r = eng.call("GET", f"/api/fetcher/build/inputs?segment={seg}")
        if r.status_code != 200:
            log(f"  no build for {seg}: HTTP {r.status_code} {r.text[:120]}")
            continue
        plan = r.json()
        files, bases = plan["files"], plan["bases"]
        prints = {b: fingerprint(files, rules, b, today) for b in bases}
        todo, why = [], {}
        for b in bases:
            failed = (plan.get("last_failed") or {}).get(b)
            if not force and prints[b] == plan["live"].get(b):
                why[b] = "up to date (same files, rules and code)"
            elif not force and failed and failed.get("fingerprint") == prints[b]:
                why[b] = f"inputs unchanged since failed build #{failed['id']}: {failed.get('message') or ''}"[:300]
            else:
                todo.append(b)
        for b in bases:
            if b in todo:
                item(seg, b, "queued", "")
            elif why[b].startswith("up to date"):
                item(seg, b, "have", why[b])
            else:
                item(seg, b, "failed", why[b])
        if not todo:
            continue
        work = Path(tempfile.mkdtemp(prefix=f"build_{seg}_"))
        try:
            data_dir = work / "data"
            data_dir.mkdir()
            prime_calendar(data_dir, fetch_json, calendar_entries, today)
            for b in todo:
                eng.item(run_id, source_ref=f"build:{seg}:{b}", segment=seg, file_type="snapshot", status="running",
                         detail="downloading inputs" if b == todo[0] else "")
            try:
                names = materialize(eng, files, data_dir, plan.get("source_map") or {}, log)
            except Exception as e:
                for b in todo:
                    _report_failure(eng, run_id, seg, b, prints[b], files, {}, rules, "error",
                                    f"could not prepare inputs: {e}")
                    item(seg, b, "failed", f"could not prepare inputs: {e}")
                continue
            for b in todo:
                eng.item(run_id, source_ref=f"build:{seg}:{b}", segment=seg, file_type="snapshot", status="running",
                         detail="parsing")
                try:
                    path, info = build_one(plan["loader"], b, data_dir, work / "db", today, lambda *a: log(*a))
                    uin = used_inputs(files, names, info)
                    ckey = content_key(uin, rules, b, info)
                    if ckey == (plan.get("live_content") or {}).get(b):
                        r2 = eng.call("POST", f"/api/fetcher/runs/{run_id}/snapshot",
                                      data={"segment": seg, "basis": b, "unchanged": "1",
                                            "meta": json.dumps({"fingerprint": prints[b]})})
                        item(seg, b, "have", f"{BASIS_NAMES[b]}: rebuilt from the same files - no change ({describe(info)})")
                        continue
                    info["content_key"] = ckey
                    raw = path.read_bytes()
                    blob = gzip.compress(raw, 6)
                    info = {k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v)) for k, v in info.items()}
                    meta = {"fingerprint": prints[b], "info": info, "inputs": uin,
                            "rules": rules, "code_version": os.getenv("GITHUB_SHA", "local")[:40],
                            "trade_date": info.get("trade_date"), "raw_bytes": len(raw), "loader_version": LOADER_VERSION}
                    sform = {"segment": seg, "basis": b, "meta": json.dumps(meta)}
                    if hasattr(eng, "post_file"):
                        up = eng.post_file(run_id, f"/api/fetcher/runs/{run_id}/snapshot", sform, path.name + ".gz", blob,
                                           "application/gzip", allow_gzip=False)
                    else:
                        up = eng.call("POST", f"/api/fetcher/runs/{run_id}/snapshot", data=sform,
                                      files={"file": (path.name + ".gz", blob, "application/gzip")}, timeout=300)
                    if up.status_code != 201:
                        raise RuntimeError(f"engine said {up.status_code} {up.text[:200]}")
                    s = up.json()["snapshot"]
                    item(seg, b, "built", f"{BASIS_NAMES[b]}: {describe(info)} - #{s['id']} "
                                          f"({len(raw)/1e6:.1f} MB, {len(blob)/1e6:.1f} MB gz)")
                except Exception as e:
                    kind = classify(e)
                    msg = f"{type(e).__name__}: {e}" if kind == "error" else str(e)
                    if kind == "error":
                        traceback.print_exc()
                    _report_failure(eng, run_id, seg, b, prints[b], files, names, rules, kind, msg)
                    item(seg, b, "failed", f"{kind.replace('_', ' ')}: {msg}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return counts


def content_key(uin, rules, basis, info):
    """What a snapshot was really built from: the files the loader used, rules, code, basis, day."""
    used = [u for u in uin if u["used"]] or uin          # nothing recognised: every input counts
    parts = {"used": sorted([u["upload_id"], u["sha256"]] for u in used), "rules": rules,
             "loader": LOADER_VERSION, "basis": basis, "trade_date": info.get("trade_date"),
             "stale": str(info.get("bod_stale")), "span": info.get("span_file") or info.get("var_file")}
    return _sha(json.dumps(parts, sort_keys=True, default=str).encode())[:40]


def describe(info):
    extra = f" - {info['fallback_inputs']}" if info.get("fallback_inputs") else ""
    if info.get("describe"):
        return str(info["describe"]) + extra
    cyc = info.get("span_cycle")
    return f"SPAN {cyc} of {info.get('span_business_date') or info.get('trade_date')}" + \
        (f" (created {info['span_created']})" if info.get("span_created") else "") + \
        (" - STALE" if str(info.get("bod_stale")) == "1" else "") + extra


def _report_failure(eng, run_id, seg, basis, fp, files, names, rules, kind, msg):
    meta = {"fingerprint": fp, "error_kind": kind, "message": msg[:1000], "rules": rules,
            "inputs": used_inputs(files, names, {}), "code_version": os.getenv("GITHUB_SHA", "local")[:40]}
    try:
        eng.call("POST", f"/api/fetcher/runs/{run_id}/snapshot", data={"segment": seg, "basis": basis,
                                                                       "meta": json.dumps(meta)})
    except Exception:
        pass


def snapshot_tables(path):
    """Row counts of a built database (used by tests and the job log)."""
    c = sqlite3.connect(path)
    try:
        return {r[0]: c.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        c.close()
