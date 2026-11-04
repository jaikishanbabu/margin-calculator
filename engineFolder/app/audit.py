"""Tamper-evident audit log.

Every row stores the SHA-256 of (its own fields + the previous row's hash). Changing,
deleting or reordering any past row breaks the chain, which `verify()` detects.
On Postgres the app role also has no UPDATE/DELETE/TRUNCATE rights and triggers block
those operations, so the log is append-only for the application.
"""
import hashlib
import json
from datetime import datetime, timezone

from . import db

GENESIS = "0" * 64
AUDIT_LOCK_KEY = 815_001  # advisory-lock id for appending to the chain


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_hash(seq, ts, username, ip, action, detail, prev_hash):
    payload = json.dumps([seq, ts, username or "", ip or "", action, detail or "", prev_hash],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def append(c, username, action, detail="", ip=None):
    """Append inside an existing transaction `c`."""
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, separators=(",", ":"), ensure_ascii=False, default=str)
    c.lock(AUDIT_LOCK_KEY)
    last = c.execute("SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (last["seq"] + 1) if last else 1
    prev = last["row_hash"] if last else GENESIS
    ts = now_iso()
    h = row_hash(seq, ts, username, ip, action, detail, prev)
    c.insert("INSERT INTO audit_log (seq, ts, username, ip, action, detail, prev_hash, row_hash) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (seq, ts, username, ip, action, detail, prev, h))
    return seq


def log(username, action, detail="", ip=None):
    with db.tx() as c:
        return append(c, username, action, detail, ip)


def verify_rows(rows):
    """Check a list of audit rows (ordered by seq). Returns dict(ok, checked, first_bad_seq, reason)."""
    prev = GENESIS
    expected_seq = rows[0]["seq"] if rows else 1
    if rows and expected_seq != 1:
        return {"ok": False, "checked": 0, "first_bad_seq": expected_seq, "reason": "log does not start at seq 1"}
    for i, r in enumerate(rows):
        if r["seq"] != expected_seq:
            return {"ok": False, "checked": i, "first_bad_seq": r["seq"], "reason": f"gap or reorder: expected seq {expected_seq}"}
        if r["prev_hash"] != prev:
            return {"ok": False, "checked": i, "first_bad_seq": r["seq"], "reason": "prev_hash does not match previous row"}
        calc = row_hash(r["seq"], r["ts"], r["username"], r["ip"], r["action"], r["detail"], r["prev_hash"])
        if calc != r["row_hash"]:
            return {"ok": False, "checked": i, "first_bad_seq": r["seq"], "reason": "row contents were changed"}
        prev = r["row_hash"]
        expected_seq += 1
    return {"ok": True, "checked": len(rows), "first_bad_seq": None, "reason": None,
            "head_hash": prev, "last_seq": expected_seq - 1}


def verify():
    rows = db.query("SELECT seq, ts, username, ip, action, detail, prev_hash, row_hash FROM audit_log ORDER BY seq")
    return verify_rows(rows)
