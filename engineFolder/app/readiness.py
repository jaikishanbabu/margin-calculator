"""Stage 2C: parsed data ("snapshots") and the readiness of each segment.

A snapshot is one segment's exchange files parsed by the vendored `loader` into a checked SQLite
database. The GitHub job (app/build_job.py) builds them right after a fetch, from the files in this
engine's archive, and uploads them here; a load that fails its checks is recorded as failed and the
previous snapshot stays in use.

Two bases per SPAN segment:
  bod     the trade date's start-of-day SPAN file (i1): what client margins use all day
          (SEBI: fixed beginning-of-day parameters) and what the calculator was validated on
  latest  the newest SPAN cycle of the day: information, or used when a SuperAdmin switches
          the segment to it (audited)
"""
import json
from datetime import datetime, timezone

from . import audit, db
from .storage import get_store, sha256

# Our segment -> the loader's key, the upload segments (and file types) it reads, the bases it builds.
PARSERS = {
    "NFO": {"loader": "NFO", "inputs": [("NFO", None), ("NSECM", "bhavcopy")], "bases": ["bod", "latest"]},
    "BFO": {"loader": "BFO", "inputs": [("BFO", None)], "bases": ["bod", "latest"]},
    "MCX": {"loader": "MCX", "inputs": [("MCX", None)], "bases": ["bod", "latest"]},
    "NSECM": {"loader": "NSE", "inputs": [("NSECM", None)], "bases": ["latest"]},
}
ALL_SEGMENTS = ["NFO", "BFO", "CDS", "MCX", "NSECM", "BSECM"]
BASES = ("bod", "latest")
LOOKBACK_TRADE_DATES = 4        # inputs the job downloads: this many newest trade dates per segment
KEEP_READY_SNAPSHOTS = 3        # per segment and basis; older snapshot files are deleted (record kept)
MAX_SNAPSHOT_MB = 48            # Supabase free plan: 50 MB per object


def iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(v):
    try:
        return json.loads(v) if v else None
    except Exception:
        return None


def _snap(r, full=False):
    if not r:
        return None
    r = dict(r)
    for k in ("inputs", "info", "rules"):
        r[k] = _j(r.get(k))
    if not full:
        info = r.get("info") or {}
        r["info"] = {k: info.get(k) for k in ("bod_stale", "bod_note", "span_settlement", "span_contracts",
                                                 "contracts", "master_stale", "var_batch", "describe") if k in info}
    return r


# ---------------------------------------------------------------- basis setting (SuperAdmin)
def basis_settings():
    s = db.get_setting("margin_basis", {}) or {}
    return {seg: (s.get(seg) if s.get(seg) in p["bases"] else p["bases"][0]) for seg, p in PARSERS.items()}


def set_basis(actor, segment, basis, ip=None):
    if segment not in PARSERS:
        raise ValueError("This segment has no parser yet")
    if basis not in PARSERS[segment]["bases"]:
        raise ValueError(f"{segment} supports: {', '.join(PARSERS[segment]['bases'])}")
    cur = basis_settings()
    if cur[segment] == basis:
        return cur
    s = dict(db.get_setting("margin_basis", {}) or {})
    s[segment] = basis
    db.set_setting("margin_basis", s)
    audit.log(actor["username"], "margin_basis_changed", {"segment": segment, "from": cur[segment], "to": basis}, ip)
    return basis_settings()


# ---------------------------------------------------------------- snapshots
def live(segment, basis):
    return _snap(db.query_one("SELECT * FROM snapshots WHERE segment = ? AND basis = ? AND status = 'ready' "
                              "ORDER BY id DESC LIMIT 1", (segment, basis)))


def last_attempt(segment, basis):
    return _snap(db.query_one("SELECT * FROM snapshots WHERE segment = ? AND basis = ? AND status IN ('ready','failed') "
                              "ORDER BY id DESC LIMIT 1", (segment, basis)))


def get_snapshot(sid):
    return _snap(db.query_one("SELECT * FROM snapshots WHERE id = ?", (sid,)), full=True)


def in_use(segment):
    """The snapshot margins use for a segment: the chosen basis, else start-of-day as the fallback."""
    if segment not in PARSERS:
        return None, None
    basis = basis_settings()[segment]
    snap = live(segment, basis)
    if snap or basis == PARSERS[segment]["bases"][0]:
        return snap, None
    fb = live(segment, PARSERS[segment]["bases"][0])
    return fb, (f"No usable {basis} snapshot; using start-of-day" if fb else None)


def newest_span_date(segment):
    r = db.query_one("SELECT MAX(trade_date) AS d FROM uploads WHERE segment = ? AND file_type IN ('span','var') "
                     "AND status = 'archived'", (segment,))
    return r["d"] if r else None


def readiness():
    """One row per segment for the Data readiness page."""
    bases = basis_settings()
    rows = []
    for seg in ALL_SEGMENTS:
        p = PARSERS.get(seg)
        row = {"segment": seg, "has_parser": bool(p)}
        if not p:
            row.update(status="no_parser", message="No parser yet (Stage 5)")
            rows.append(row)
            continue
        row["bases"] = p["bases"]
        row["basis"] = bases[seg]
        row["snapshots"] = {b: {"live": live(seg, b), "last": last_attempt(seg, b)} for b in p["bases"]}
        use, fallback = in_use(seg)
        row["in_use"] = use
        row["fallback"] = fallback
        newest = newest_span_date(seg)
        row["newest_input_date"] = newest
        chosen_last = row["snapshots"][bases[seg]]["last"]
        if not use:
            if chosen_last and chosen_last["status"] == "failed":
                row["status"] = chosen_last.get("error_kind") or "check_failed"
                row["message"] = chosen_last.get("message")
            else:
                row["status"] = "not_built"
                row["message"] = "No snapshot built yet - run a fetch or press Rebuild"
        else:
            stale = []
            if (use.get("info") or {}).get("bod_stale") in ("1", 1, True):
                stale.append(use["info"].get("bod_note") or "start-of-day file not out yet")
            if newest and use.get("trade_date") and use["trade_date"] < newest:
                stale.append(f"newer files ({newest}) not built yet")
            row["status"] = "stale" if stale else ("fallback" if fallback else "ready")
            row["message"] = "; ".join(stale) or fallback or ""
            if chosen_last and chosen_last["status"] == "failed" and chosen_last["id"] > (use or {}).get("id", 0):
                row["warning"] = f"Last rebuild failed ({chosen_last.get('error_kind')}): {chosen_last.get('message')}"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- job side (FETCHER_TOKEN)
def build_inputs(segment):
    """Archived files a segment's loader needs (newest trade dates only) + the live fingerprints."""
    p = PARSERS.get(segment)
    if not p:
        raise ValueError("No parser for this segment")
    files = []
    for useg, ftype in p["inputs"]:
        days = [r["trade_date"] for r in db.query(
            "SELECT DISTINCT trade_date FROM uploads WHERE segment = ? AND status = 'archived' "
            "ORDER BY trade_date DESC LIMIT ?", (useg, LOOKBACK_TRADE_DATES))]
        if not days:
            continue
        sql = ("SELECT id, segment, file_type, trade_date, cycle, filename, sha256, size_bytes, source_ref "
               "FROM uploads WHERE segment = ? AND status = 'archived' AND trade_date >= ?")
        args = [useg, days[-1]]
        if ftype:
            sql += " AND file_type = ?"
            args.append(ftype)
        files += db.query(sql + " ORDER BY id", tuple(args))
    from .fetcher_map import SOURCE_MAP
    last = {b: last_attempt(segment, b) or {} for b in p["bases"]}
    return {"segment": segment, "loader": p["loader"], "bases": p["bases"], "files": files, "source_map": SOURCE_MAP,
            "live": {b: (live(segment, b) or {}).get("fingerprint") for b in p["bases"]},
            "live_content": {b: ((get_snapshot((live(segment, b) or {}).get("id") or 0) or {}).get("info") or {})
                             .get("content_key") for b in p["bases"]},
            "last_failed": {b: {"fingerprint": last[b].get("fingerprint"), "id": last[b].get("id"),
                                "message": last[b].get("message")} if last[b].get("status") == "failed" else None
                            for b in p["bases"]}}


def original(upload_id):
    """Decoded original bytes of an archived upload (for the build job)."""
    from .uploads import decode
    u = db.query_one("SELECT archive_key, archive_encoding, sha256, filename, status FROM uploads WHERE id = ?", (upload_id,))
    if not u:
        raise LookupError("Upload not found")
    if u["status"] != "archived":
        raise ValueError(f"Upload is {u['status']}")
    data = decode(get_store().get(u["archive_key"]), u.get("archive_encoding") or "identity")
    if sha256(data) != u["sha256"]:
        raise ValueError("Archived file failed its checksum")
    return u["filename"], data


def record(run_id, segment, basis, meta, blob=None):
    """Store a build result. blob: gzip of the SQLite database (None for a failed build)."""
    if segment not in PARSERS or basis not in PARSERS[segment]["bases"]:
        raise ValueError("Unknown segment or basis")
    status = "ready" if blob is not None else "failed"
    kind = None if status == "ready" else (meta.get("error_kind") if meta.get("error_kind") in
                                           ("missing_inputs", "check_failed", "error") else "error")
    info = meta.get("info") or {}
    td = str(meta.get("trade_date") or info.get("trade_date") or "").replace("-", "")[:8] or None
    key = digest = size = None
    if blob is not None:
        if len(blob) > MAX_SNAPSHOT_MB * 1024 * 1024:
            raise ValueError(f"Snapshot is larger than {MAX_SNAPSHOT_MB} MB")
        digest = sha256(blob)
        key = f"snapshots/{segment}/{td or 'unknown'}/{basis}-{digest[:16]}.sqlite.gz"
        store = get_store()
        if not store.exists(key):
            store.put_verified(key, blob, "application/gzip")
        size = len(blob)
    span_day = str(info.get("span_business_date") or info.get("span_trade_date") or "").replace("-", "") or None
    row = (segment, basis, status, kind, td, span_day, str(info.get("span_cycle") or info.get("var_batch") or "") or None,
           str(info.get("span_created") or "") or None, str(meta.get("fingerprint", ""))[:80],
           json.dumps(meta.get("inputs") or [])[:200000], json.dumps(info)[:200000], json.dumps(meta.get("rules") or {}),
           str(meta.get("code_version") or "")[:60], key, digest, size, meta.get("raw_bytes"),
           run_id, "auto-fetcher", iso(), str(meta.get("message") or "")[:1000])
    with db.tx() as c:
        sid = c.insert(
            "INSERT INTO snapshots (segment, basis, status, error_kind, trade_date, span_day, span_cycle, span_created, "
            "fingerprint, inputs, info, rules, code_version, archive_key, sha256, size_bytes, raw_bytes, run_id, built_by, "
            "built_at, message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        audit.append(c, "auto-fetcher", "snapshot_built" if status == "ready" else "snapshot_failed",
                     {"snapshot_id": sid, "segment": segment, "basis": basis, "trade_date": td, "run_id": run_id,
                      "sha256": digest, "error_kind": kind, "message": str(meta.get("message") or "")[:300]})
    if status == "ready":
        prune_snapshots("system", only=(segment, basis))
    return get_snapshot(sid)


def mark_unchanged(segment, basis, fingerprint):
    """A rebuild produced the same content as the live snapshot: remember the new fingerprint only."""
    with db.tx() as c:
        c.execute("UPDATE snapshots SET fingerprint = ? WHERE id = (SELECT MAX(id) FROM snapshots WHERE segment = ? "
                  "AND basis = ? AND status = 'ready')", (str(fingerprint)[:80], segment, basis))


def prune_snapshots(actor="system", only=None):
    """Keep the newest KEEP_READY_SNAPSHOTS files per segment and basis; records stay."""
    store, removed = get_store(), []
    for seg, p in PARSERS.items():
        for b in p["bases"]:
            if only and (seg, b) != only:
                continue
            rows = db.query("SELECT id, archive_key FROM snapshots WHERE segment = ? AND basis = ? AND status = 'ready' "
                            "ORDER BY id DESC", (seg, b))
            for r in rows[KEEP_READY_SNAPSHOTS:]:
                try:
                    if r["archive_key"]:
                        store.delete(r["archive_key"])
                    with db.tx() as c:
                        c.execute("UPDATE snapshots SET status = 'expired' WHERE id = ?", (r["id"],))
                    removed.append(r["id"])
                except Exception:
                    pass
    if removed:
        audit.log(actor, "snapshot_retention", {"expired": removed})
    return {"expired": len(removed)}
