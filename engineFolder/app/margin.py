"""Stage 3: margins for a basket, from the snapshot Data readiness marks as in use.

The snapshot (a gzipped SQLite database built on GitHub) is downloaded from the archive once,
checked against its SHA-256, unpacked to a local cache (Render's disk is temporary: after a restart
it is fetched again) and opened read-only by the owner's engine (engine/, unchanged).

Every answer says which data it used (trade date, SPAN cycle, file creation time, basis) and whether
the segment is validated against brokers. Only NFO is served until other segments are validated.
"""
import gzip
import hashlib
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from . import readiness, validation
from .storage import get_store

SERVED = ("NFO",)
CACHE_DIR = Path(os.getenv("SNAPSHOT_CACHE_DIR") or (Path(tempfile.gettempdir()) / "margin-snapshots"))
KEEP_FILES = 3
MAX_LEGS = 30
_lock = threading.Lock()
_engines = {}            # sha256 -> engine


class MarginError(ValueError):
    pass


def _local(snap):
    """Path of the snapshot's database on this machine, fetching and unpacking it if needed."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{snap['segment']}-{snap['sha256'][:24]}.sqlite"
    if path.exists():
        return path
    blob = get_store().get(snap["archive_key"])
    if hashlib.sha256(blob).hexdigest() != snap["sha256"]:
        raise MarginError("The stored snapshot failed its checksum - rebuild it on the Data readiness page")
    tmp = path.with_suffix(".part")
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(blob)) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    tmp.replace(path)
    old = sorted(CACHE_DIR.glob("*.sqlite"), key=lambda p: p.stat().st_mtime)[:-KEEP_FILES]
    for p in old:
        p.unlink(missing_ok=True)
    return path


def engine_for(segment):
    if segment not in SERVED:
        raise MarginError(f"{segment} margins aren't available yet - only NSE F&O is validated")
    snap, fallback = readiness.in_use(segment)
    if not snap:
        raise MarginError(f"No {segment} data is ready yet")
    with _lock:
        eng = _engines.get(snap["sha256"])
        if eng is None:
            from engine import Engine
            eng = Engine(_local(snap))
            for k in [k for k in _engines if k != snap["sha256"]]:
                _engines.pop(k).conn.close()
            _engines[snap["sha256"]] = eng
    return eng, snap, fallback


def _position(p):
    from engine import Position
    if not isinstance(p, dict):
        raise MarginError("each position must be an object")
    try:
        qty = int(p.get("qty"))
    except (TypeError, ValueError):
        raise MarginError("qty must be a whole number of units (+ buy, - sell)")
    if qty == 0:
        raise MarginError("qty can't be 0")
    if p.get("tradingsymbol"):
        return Position(str(p["tradingsymbol"])[:60], qty)
    if not p.get("symbol") or not p.get("expiry"):
        raise MarginError("each position needs tradingsymbol, or symbol + expiry (+ strike and option_type for options)")
    c = {"symbol": str(p["symbol"]).upper()[:30], "expiry": str(p["expiry"])[:10]}
    if p.get("option_type") not in (None, "", "FUT"):
        c["option_type"] = str(p["option_type"]).upper()[:2]
        c["strike"] = p.get("strike")
    return Position(c, qty)


def margin(segment, positions, alone=True):
    from engine import NoRiskData, UnknownContract
    if not isinstance(positions, list) or not positions:
        raise MarginError("positions must be a non-empty list")
    if len(positions) > MAX_LEGS:
        raise MarginError(f"at most {MAX_LEGS} positions")
    pos = [_position(p) for p in positions]
    eng, snap, fallback = engine_for(segment)
    try:
        for p in pos:                                  # whole lots only, like the exchange
            lot = eng.leg(p).lot_size
            if lot and p.qty % lot:
                raise MarginError(f"{p.contract}: {abs(p.qty)} is not a whole number of lots of {lot}")
        m = eng.margin(pos, alone=alone)
    except (NoRiskData, UnknownContract) as e:
        raise MarginError(str(e))
    v = validation.status(segment)
    out = {k: m.get(k) for k in ("span", "exposure", "other", "total", "net_premium", "benefit", "warnings",
                                 "legs", "underlyings")}
    out["data"] = {"segment": segment, "basis": snap["basis"], "trade_date": snap.get("trade_date"),
                   "span_day": snap.get("span_day"), "span_cycle": snap.get("span_cycle"),
                   "span_created": snap.get("span_created"), "snapshot_id": snap["id"], "fallback": fallback,
                   "stale": str((snap.get("info") or {}).get("bod_stale")) == "1", "as_of": m.get("as_of")}
    out["validated"] = v["validated"] and snap["basis"] == "bod"
    out["validation_note"] = ("" if out["validated"] else
                              (v["reason"] if not v["validated"] else "Latest-cycle margins aren't validated against brokers"))
    return out


class RateLimit:
    """Per-IP sliding window for the public margin endpoint."""

    def __init__(self, n=60, per=60):
        self.n, self.per, self.hits = n, per, {}

    def allow(self, key):
        now = time.monotonic()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        if len(self.hits) > 10000:
            self.hits.clear()
        return True


limiter = RateLimit()


# ---------------------------------------------------------------- pickers for the calculator page
def _first_day(snap):
    td = snap.get("trade_date") or ""
    return f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else "0000-00-00"


def meta(segment):
    """What the calculator page needs before the first basket: the data in use, the validation
    state and the instruments (index first, then stocks) with their reference price."""
    eng, snap, fallback = engine_for(segment)
    day = _first_day(snap)
    rows = eng.conn.execute(
        """SELECT u.symbol, u.kind, COALESCE(u.prev_close, u.spot_price) AS ref, u.is_banned
           FROM underlying u WHERE u.in_master = 1 AND EXISTS (
             SELECT 1 FROM contract c WHERE c.symbol = u.symbol AND c.exchange = 'NSE' AND c.in_master = 1
             AND c.expiry >= ?)
           ORDER BY (u.kind = 'INDEX') DESC, u.symbol""", (day,)).fetchall()
    v = validation.status(segment)
    return {"segment": segment,
            "data": {"basis": snap["basis"], "trade_date": snap.get("trade_date"), "span_day": snap.get("span_day"),
                     "span_cycle": snap.get("span_cycle"), "span_created": snap.get("span_created"),
                     "stale": str((snap.get("info") or {}).get("bod_stale")) == "1", "fallback": fallback},
            "validated": v["validated"] and snap["basis"] == "bod",
            "validation_note": "" if (v["validated"] and snap["basis"] == "bod") else
            (v["reason"] if not v["validated"] else "Latest-cycle margins aren't validated against brokers"),
            "symbols": [{"symbol": r["symbol"], "kind": r["kind"], "ref": r["ref"], "banned": bool(r["is_banned"])}
                        for r in rows]}


def contracts(segment, symbol):
    """Listed contracts of one underlying: per expiry, the lot size, whether a future trades,
    and the call / put strikes."""
    eng, snap, _ = engine_for(segment)
    sym = str(symbol or "").upper()[:30]
    rows = eng.conn.execute(
        """SELECT expiry, instrument, option_type, strike, lot_size FROM contract
           WHERE symbol = ? AND exchange = 'NSE' AND in_master = 1 AND expiry >= ?
           ORDER BY expiry, instrument, strike""", (sym, _first_day(snap))).fetchall()
    if not rows:
        raise MarginError(f"No listed {segment} contracts for {sym}")
    out = {}
    for r in rows:
        e = out.setdefault(r["expiry"], {"expiry": r["expiry"], "lot_size": r["lot_size"], "future": False,
                                          "CE": [], "PE": []})
        if r["instrument"] == "FUT":
            e["future"] = True
            e["lot_size"] = r["lot_size"] or e["lot_size"]
        elif r["option_type"] in ("C", "P"):
            e["CE" if r["option_type"] == "C" else "PE"].append(r["strike"])
    ref = eng.conn.execute("SELECT COALESCE(prev_close, spot_price) FROM underlying WHERE symbol = ?", (sym,)).fetchone()
    return {"symbol": sym, "ref": ref[0] if ref else None, "expiries": list(out.values())}
