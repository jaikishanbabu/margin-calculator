"""Exchange file fetcher: settings, runs, scheduling, progress and archive retention (engine side).

Where things run
- The downloads run on GitHub Actions (workflow fetcher.yml): Stage 2A showed NSE and MCX
  refuse Render's address but accept GitHub's. The job reports progress and uploads every file
  back to this engine with the restricted FETCHER_TOKEN.
- This module starts runs: manually from the admin page, or from the Supabase pg_cron "tick"
  (every 15 min) when auto fetch is on and a run is due.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import audit, config, db
from .fetcher_map import SEGMENT_CHOICES, SOURCE_MAP
from .storage import get_store

IST = timezone(timedelta(hours=5, minutes=30))
FREQUENCIES = [15, 30, 60, 120, 180]
DEFAULTS = {"fetch_auto_enabled": False, "fetch_frequency_min": 30,
            "fetch_window_start": "07:00", "fetch_window_end": "23:30",
            "retention_trading_days": 7, "retention_intraday_days": 2}
STALE_RUN_MINUTES = 45          # a run with no news for this long is marked failed
FINAL_STATUSES = ("done", "failed", "skipped")
ITEM_STATUSES = ("queued", "running", "archived", "have", "pending", "blocked", "failed", "not_due", "built")
TRIGGERS = ("auto", "manual", "rebuild", "calendar")   # rebuild: parse only; calendar: import holiday pages only
# SPAN cycles kept for the full retention window; any other SPAN cycle is "intraday"
KEEP_CYCLES = {"i1", "i01", "s", "bod", "eod", "final", "00", "-00"}


def is_intraday_span(segment, cycle):
    """Intraday SPAN versions get the shorter retention; start-of-day and end-of-day are kept longer.
    MCX names its files HHMM-seq[-e]: seq 01 is the day's first file, '-e' the end-of-day one."""
    c = (cycle or "").lower()
    if not c:
        return False
    if segment == "MCX":
        return not (c.endswith("-e") or re.search(r"-0?1$", c))
    return c not in KEEP_CYCLES


class FetchError(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def now_ist():
    return datetime.now(IST)


def iso(dt=None):
    return (dt or now_utc()).isoformat(timespec="seconds")


# ---------------------------------------------------------------- settings
def settings():
    return {k: db.get_setting(k, v) for k, v in DEFAULTS.items()}


def update_settings(actor, changes, ip=None):
    cur = settings()
    clean = {}
    if "fetch_auto_enabled" in changes:
        clean["fetch_auto_enabled"] = bool(changes["fetch_auto_enabled"])
    if "fetch_frequency_min" in changes:
        f = int(changes["fetch_frequency_min"])
        if f not in FREQUENCIES:
            raise ValueError(f"Frequency must be one of {FREQUENCIES} minutes")
        clean["fetch_frequency_min"] = f
    for k in ("fetch_window_start", "fetch_window_end"):
        if k in changes:
            v = str(changes[k])
            datetime.strptime(v, "%H:%M")
            clean[k] = v
    for k, lo, hi in (("retention_trading_days", 2, 60), ("retention_intraday_days", 1, 30)):
        if k in changes:
            v = int(changes[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            clean[k] = v
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown setting(s): {sorted(unknown)}")
    for k, v in clean.items():
        db.set_setting(k, v)
    audit.log(actor["username"], "fetcher_settings_changed",
              {k: {"from": cur.get(k), "to": v} for k, v in clean.items()}, ip)
    return settings()


# ---------------------------------------------------------------- runs
def _row(r):
    if not r:
        return None
    r = dict(r)
    for k in ("segments", "summary"):
        try:
            r[k] = json.loads(r[k]) if r.get(k) else None
        except Exception:
            pass
    return r


def active_run():
    expire_stale()
    return _row(db.query_one("SELECT * FROM fetch_runs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1"))


def get_run(run_id, with_items=True):
    r = _row(db.query_one("SELECT * FROM fetch_runs WHERE id = ?", (run_id,)))
    if r and with_items:
        r["items"] = db.query("SELECT source_ref, segment, file_type, status, detail, upload_id, updated_at "
                              "FROM fetch_items WHERE run_id = ? ORDER BY segment, source_ref", (run_id,))
    return r


def list_runs(limit=20):
    rows = db.query("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 100)),))
    return [_row(r) for r in rows]


def expire_stale():
    cutoff = iso(now_utc() - timedelta(minutes=STALE_RUN_MINUTES))
    with db.tx() as c:
        rows = c.execute("SELECT id FROM fetch_runs WHERE status IN ('queued','running') AND "
                         "COALESCE((SELECT MAX(updated_at) FROM fetch_items i WHERE i.run_id = fetch_runs.id), "
                         "started_at, created_at) < ?", (cutoff,)).fetchall()
        for r in rows:
            c.execute("UPDATE fetch_runs SET status = 'failed', finished_at = ?, message = ? WHERE id = ?",
                      (iso(), f"No progress for {STALE_RUN_MINUTES} min (GitHub job did not start or stopped)", r["id"]))
            audit.append(c, "system", "fetch_run_expired", {"run_id": r["id"]})


NEXT_DAY_FROM = "21:00"     # IST: NSE publishes the next trading day's start-of-day SPAN (i1) ~21:30


def trading_date_for(now=None):
    """The trading date a run fetches for. Until 21:00 IST: today. From 21:00: the next weekday,
    because the next day's start-of-day SPAN (and contract master) come out that evening; the
    job moves it on past exchange holidays (it has NSE's holiday list)."""
    t = (now or now_ist())
    if t.strftime("%H:%M") >= NEXT_DAY_FROM:
        t = t + timedelta(days=1)
        while t.weekday() >= 5:
            t += timedelta(days=1)
    return t.strftime("%Y%m%d")


def create_run(trigger, requested_by, segments, trade_date=None, ip=None, dispatch_fn=None, at=None):
    if trigger not in TRIGGERS:
        raise ValueError(f"bad trigger {trigger!r}")
    segs = ["ALL"] if not segments or "ALL" in segments else segments
    bad = [s for s in segs if s != "ALL" and s not in SEGMENT_CHOICES]
    if bad:
        raise ValueError(f"Unknown segment(s): {bad}")
    act = active_run()
    if act:
        raise FetchError(f"Fetch run #{act['id']} is already {act['status']} - wait for it to finish")
    td = trade_date or trading_date_for()
    with db.tx() as c:
        rid = c.insert("INSERT INTO fetch_runs (trigger, requested_by, segments, trade_date, status, created_at) "
                       "VALUES (?, ?, ?, ?, 'queued', ?)", (trigger, requested_by, json.dumps(segs), td,
                                                            iso(at.astimezone(timezone.utc) if at else None)))
        audit.append(c, requested_by, "fetch_run_requested", {"run_id": rid, "trigger": trigger, "segments": segs,
                                                               "trade_date": td}, ip)
    try:
        (dispatch_fn or dispatch_github)(rid)
    except Exception as e:
        with db.tx() as c:
            c.execute("UPDATE fetch_runs SET status = 'failed', finished_at = ?, message = ? WHERE id = ?",
                      (iso(), f"Could not start the GitHub fetcher job: {e}", rid))
            audit.append(c, "system", "fetch_run_dispatch_failed", {"run_id": rid, "error": str(e)[:300]})
        raise FetchError(f"Could not start the fetcher job: {e}")
    return get_run(rid)


def dispatch_github(run_id):
    """Start the fetcher workflow on GitHub Actions (workflow_dispatch)."""
    if not (config.GITHUB_DISPATCH_TOKEN and config.GITHUB_REPO):
        raise FetchError("GITHUB_DISPATCH_TOKEN / GITHUB_REPO are not configured on the server")
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/actions/workflows/fetcher.yml/dispatches"
    body = json.dumps({"ref": config.GITHUB_REF, "inputs": {"run_id": str(run_id)}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {config.GITHUB_DISPATCH_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "margin-engine"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status not in (200, 204):
                raise FetchError(f"GitHub answered HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise FetchError(f"GitHub answered HTTP {e.code}: {e.read().decode(errors='replace')[:160]}")


# ---------------------------------------------------------------- scheduler tick
def _in_window(t, s):
    return s["fetch_window_start"] <= t.strftime("%H:%M") <= s["fetch_window_end"]


def next_due(s=None, now=None):
    s = s or settings()
    if not s["fetch_auto_enabled"]:
        return None
    last = db.query_one("SELECT created_at FROM fetch_runs WHERE trigger = 'auto' ORDER BY id DESC LIMIT 1")
    t = now or now_ist()
    due = t if not last else datetime.fromisoformat(last["created_at"]).astimezone(IST) + timedelta(minutes=s["fetch_frequency_min"])
    return max(due, t) if _in_window(max(due, t), s) else None


def tick(now=None, dispatch_fn=None):
    """Called every ~15 min by Supabase pg_cron. Starts an auto run when one is due."""
    t = now or now_ist()
    s = settings()
    out = {"at": t.isoformat(timespec="minutes"), "auto_enabled": s["fetch_auto_enabled"]}
    expire_stale()
    if t.strftime("%H:%M") >= "23:40" and db.get_setting("retention_last_day") != t.strftime("%Y%m%d"):
        out["retention"] = prune_archive()
        from .readiness import prune_snapshots
        out["snapshot_retention"] = prune_snapshots()
        db.set_setting("retention_last_day", t.strftime("%Y%m%d"))
    if not s["fetch_auto_enabled"]:
        return dict(out, action="none", reason="auto fetch is off")
    if t.weekday() >= 5 and not db.get_setting("fetch_weekend_override", False):
        return dict(out, action="none", reason="weekend")
    if not _in_window(t, s):
        return dict(out, action="none", reason="outside fetch window")
    last = db.query_one("SELECT created_at FROM fetch_runs WHERE trigger = 'auto' ORDER BY id DESC LIMIT 1")
    if last:
        age_min = (t - datetime.fromisoformat(last["created_at"]).astimezone(IST)).total_seconds() / 60
        if age_min < s["fetch_frequency_min"] - 2:       # 2 min slack for scheduler jitter
            return dict(out, action="none", reason=f"not due ({age_min:.0f} of {s['fetch_frequency_min']} min)")
    if active_run():
        return dict(out, action="none", reason="a run is already in progress")
    run = create_run("auto", "auto-fetcher", ["ALL"], trade_date=trading_date_for(t), dispatch_fn=dispatch_fn, at=t)
    return dict(out, action="started", run_id=run["id"])


# ---------------------------------------------------------------- fetcher-job side (FETCHER_TOKEN)
def job_config(run_id):
    r = get_run(run_id, with_items=False)
    if not r:
        raise LookupError("Fetch run not found")
    return {"run_id": r["id"], "trade_date": r["trade_date"], "segments": r["segments"], "trigger": r["trigger"],
            "status": r["status"], "source_map": SOURCE_MAP}


def job_started(run_id, github_run_id=None):
    with db.tx() as c:
        r = c.execute("SELECT status FROM fetch_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise LookupError("Fetch run not found")
        if r["status"] != "queued":
            raise FetchError(f"Run is {r['status']}, not queued")
        c.execute("UPDATE fetch_runs SET status = 'running', started_at = ?, github_run_id = ? WHERE id = ?",
                  (iso(), str(github_run_id or "")[:40], run_id))
    return job_config(run_id)


def upload_trade_date(run, requested):
    """A segment's trading date inside a run (its own exchange's calendar): within 10 days of the run's."""
    if not requested:
        return run["trade_date"]
    try:
        a, b = datetime.strptime(run["trade_date"], "%Y%m%d"), datetime.strptime(str(requested), "%Y%m%d")
    except ValueError:
        raise ValueError("trade_date must be YYYYMMDD")
    if abs((b - a).days) > 10:
        raise ValueError("trade_date is too far from the run's date")
    return b.strftime("%Y%m%d")


def job_move_trade_date(run_id, new_date):
    """The job found the run's date is an exchange holiday: move it forward to the next trading day.
    Only forward, at most 10 days, only while running, and only before anything was uploaded."""
    r = get_run(run_id, with_items=False)
    if not r:
        raise LookupError("Fetch run not found")
    if r["status"] != "running":
        raise FetchError(f"Run is {r['status']}")
    new = str(new_date)
    try:
        old_d, new_d = datetime.strptime(r["trade_date"], "%Y%m%d"), datetime.strptime(new, "%Y%m%d")
    except ValueError:
        raise ValueError("trade_date must be YYYYMMDD")
    if not 0 < (new_d - old_d).days <= 10:
        raise ValueError("trade date can only move forward, by at most 10 days")
    if db.query_one("SELECT id FROM fetch_items WHERE run_id = ? AND upload_id IS NOT NULL", (run_id,)):
        raise FetchError("files were already uploaded for this run")
    with db.tx() as c:
        c.execute("UPDATE fetch_runs SET trade_date = ? WHERE id = ?", (new, run_id))
        audit.append(c, "auto-fetcher", "fetch_run_trade_date_moved", {"run_id": run_id, "from": r["trade_date"], "to": new})
    return job_config(run_id)


def have(trade_date):
    """What is already archived for a trade date (so the job skips it without contacting the exchange)."""
    rows = db.query("SELECT source_ref, filename, sha256 FROM uploads WHERE trade_date = ? AND status = 'archived' "
                    "AND source_ref IS NOT NULL", (trade_date,))
    return rows


def report_items(run_id, items):
    """items: [{source_ref, segment, file_type, status, detail, upload_id}]"""
    r = db.query_one("SELECT status FROM fetch_runs WHERE id = ?", (run_id,))
    if not r:
        raise LookupError("Fetch run not found")
    if r["status"] in FINAL_STATUSES:
        raise FetchError("Run already finished")
    with db.tx() as c:
        for it in items[:200]:
            st = it.get("status")
            if st not in ITEM_STATUSES:
                raise ValueError(f"bad item status {st!r}")
            ref = str(it.get("source_ref", ""))[:120]
            tdt = str(it.get("trade_date") or "")[:8] or None
            row = (it.get("segment", "")[:10], it.get("file_type", "")[:30], st, str(it.get("detail") or "")[:500],
                   it.get("upload_id"), tdt, iso())
            if c.execute("SELECT id FROM fetch_items WHERE run_id = ? AND source_ref = ?", (run_id, ref)).fetchone():
                c.execute("UPDATE fetch_items SET segment = ?, file_type = ?, status = ?, detail = ?, upload_id = ?, "
                          "trade_date = COALESCE(?, trade_date), updated_at = ? WHERE run_id = ? AND source_ref = ?",
                          row + (run_id, ref))
            else:
                c.insert("INSERT INTO fetch_items (segment, file_type, status, detail, upload_id, trade_date, updated_at, "
                         "run_id, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row + (run_id, ref))


def job_finished(run_id, status="done", message=""):
    if status not in ("done", "failed", "skipped"):
        raise ValueError("bad final status")
    counts = {}
    for r in db.query("SELECT status, COUNT(*) AS n FROM fetch_items WHERE run_id = ? GROUP BY status", (run_id,)):
        counts[r["status"]] = r["n"]
    with db.tx() as c:
        c.execute("UPDATE fetch_runs SET status = ?, finished_at = ?, message = ?, summary = ? WHERE id = ?",
                  (status, iso(), str(message)[:500], json.dumps(counts), run_id))
        audit.append(c, "auto-fetcher", "fetch_run_finished", {"run_id": run_id, "status": status, "counts": counts})
    return get_run(run_id, with_items=False)


# ---------------------------------------------------------------- archive retention (Supabase free plan)
def prune_archive(actor="system"):
    """Delete archived ORIGINALS outside the retention window; keep every record, checksum and manifest.
    Window is counted in trading days present in the upload log for each segment."""
    s = settings()
    keep_all, keep_intra = s["retention_trading_days"], s["retention_intraday_days"]
    store = get_store()
    removed, errors = [], []
    for seg_row in db.query("SELECT DISTINCT segment FROM uploads"):
        seg = seg_row["segment"]
        days = [r["trade_date"] for r in db.query(
            "SELECT DISTINCT trade_date FROM uploads WHERE segment = ? ORDER BY trade_date DESC", (seg,))]
        all_cut = days[keep_all - 1] if len(days) >= keep_all else None
        intra_cut = days[keep_intra - 1] if len(days) >= keep_intra else None
        for u in db.query("SELECT id, trade_date, file_type, cycle, archive_key FROM uploads WHERE segment = ? "
                          "AND status IN ('archived','superseded','deleted')", (seg,)):
            intraday = u["file_type"] == "span" and is_intraday_span(seg, u["cycle"])
            expire = (all_cut and u["trade_date"] < all_cut) or (intraday and intra_cut and u["trade_date"] < intra_cut)
            if not expire:
                continue
            try:
                store.delete(u["archive_key"])
                with db.tx() as c:
                    c.execute("UPDATE uploads SET status = 'expired', message = ? WHERE id = ?",
                              (f"Original removed by retention on {iso()[:10]} (record and checksum kept)", u["id"]))
                removed.append(u["id"])
            except Exception as e:
                errors.append(f"#{u['id']}: {e}")
    audit.log(actor, "archive_retention", {"removed": removed, "errors": errors[:20],
                                           "keep_trading_days": keep_all, "keep_intraday_days": keep_intra})
    return {"removed": len(removed), "errors": len(errors)}
