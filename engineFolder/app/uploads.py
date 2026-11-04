"""Upload pipeline (Stage 1): validate -> provenance -> archive original -> verify -> manifest -> record.

Parsing files into calculation tables is Stage 2. In Stage 1 every accepted upload is
archived byte-for-byte with its checksum, and its provenance is recorded immutably.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from . import audit, config, db, filesafety, provenance, segments
from .storage import AlreadyExists, StorageError, get_store

PARSER_VERSIONS = {
    "file_safety": filesafety.VALIDATOR_VERSION,
    "provenance": provenance.PROVENANCE_VERSION,
}

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
DATE_RE = re.compile(r"^\d{8}$")


class UploadRejected(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(name):
    base = (name or "upload").replace("\\", "/").split("/")[-1]
    base = SAFE_NAME_RE.sub("_", base).strip("._") or "upload"
    return base[:120]


def _valid_date(s):
    if not s:
        return None
    s = s.strip().replace("-", "")
    if not DATE_RE.match(s):
        raise UploadRejected("Trading date must be YYYYMMDD")
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        raise UploadRejected("Trading date is not a real date")
    return s


def suggest(filename):
    """Hints for the admin form ONLY. Nothing from here is stored as provenance."""
    d = segments.detect(filename)
    return {"segment": d["segment"], "file_type": d["file_type"],
            "trade_date_hint": d["trade_date"], "cycle_hint": d["cycle"],
            "note": "Suggested from the filename - confirm before uploading. Stored values come from the file or from what you confirm."}


def process(user, filename, data: bytes, segment, file_type, trade_date=None, cycle=None, ip=None, replace=False):
    if segment not in segments.SEGMENTS:
        raise UploadRejected("Choose a valid segment")
    allowed_types = segments.SEGMENT_FILES[segment]["required"] + segments.SEGMENT_FILES[segment]["optional"]
    if file_type not in allowed_types:
        raise UploadRejected(f"{file_type} is not a file type for {segment}")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadRejected(f"File is larger than {config.MAX_UPLOAD_MB} MB")
    fname = safe_filename(filename)
    admin_date = _valid_date(trade_date)
    admin_cycle = (cycle or "").strip() or None

    # 1. content checks (type by magic bytes, archive limits)
    try:
        info = filesafety.inspect(data, file_type)
    except filesafety.UnsafeFile as e:
        audit.log(user["username"], "upload_rejected", {"filename": fname, "reason": str(e)}, ip)
        raise UploadRejected(str(e))

    # 2. provenance from inside the file
    if file_type == "span":
        try:
            prov = provenance.resolve_span(info["primary"], admin_date, admin_cycle)
        except provenance.ProvenanceError as e:
            audit.log(user["username"], "upload_rejected", {"filename": fname, "reason": str(e)}, ip)
            raise UploadRejected(str(e))
    else:
        if not admin_date:
            raise UploadRejected("Enter the trading date this file is effective for")
        if admin_cycle:
            raise UploadRejected("Cycle only applies to SPAN files")
        prov = {"trade_date": admin_date, "trade_date_source": "admin", "cycle": None, "cycle_source": None,
                "source_timestamp": None, "source_meta": {}}

    sha = hashlib.sha256(data).hexdigest()
    with db.tx() as c:
        dup = c.execute("SELECT id, uploaded_by, uploaded_at FROM uploads WHERE sha256 = ? AND segment = ? AND file_type = ? "
                        "AND status = 'archived'", (sha, segment, file_type)).fetchone()
    if dup:
        raise UploadRejected(f"This exact file was already uploaded (#{dup['id']} by {dup['uploaded_by']} at {dup['uploaded_at']})")

    # same slot (segment, type, date, cycle) already filled?
    with db.tx() as c:
        existing = c.execute("SELECT id FROM uploads WHERE segment = ? AND file_type = ? AND trade_date = ? "
                             "AND COALESCE(cycle, '') = ? AND status = 'archived'",
                             (segment, file_type, prov["trade_date"], prov["cycle"] or "")).fetchall()
    if existing and not replace:
        raise UploadRejected(f"A {file_type} file for {segment} {prov['trade_date']}"
                             + (f" cycle {prov['cycle']}" if prov["cycle"] else "")
                             + f" already exists (#{existing[0]['id']}). Tick 'replace' to supersede it.")

    # 3. archive original (write-once) and verify by reading it back
    store = get_store()
    key = f"originals/{segment}/{prov['trade_date']}/{file_type}/{sha[:16]}_{fname}"
    try:
        obj = store.put_verified(key, data)
    except AlreadyExists:
        back = store.get(key)
        if hashlib.sha256(back).hexdigest() != sha:
            raise UploadRejected("Archive already holds a different file under this name - contact SuperAdmin")
    except StorageError as e:
        audit.log(user["username"], "upload_failed", {"filename": fname, "reason": f"archive: {e}"}, ip)
        raise UploadRejected(f"Could not archive the file: {e}")
    verified_at = now_iso()

    # 4. record (+ supersede older slot holders) and audit, in one transaction
    uploaded_at = now_iso()
    with db.tx() as c:
        uid = c.insert(
            "INSERT INTO uploads (segment, file_type, trade_date, trade_date_source, cycle, cycle_source, source_timestamp, "
            "source_meta, filename, content_kind, size_bytes, sha256, inner_manifest, archive_backend, archive_key, "
            "archive_verified_at, parser_versions, app_version, uploaded_by, uploaded_at, uploader_ip, status, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived', ?)",
            (segment, file_type, prov["trade_date"], prov["trade_date_source"], prov["cycle"], prov["cycle_source"],
             prov["source_timestamp"], json.dumps(prov["source_meta"], sort_keys=True), fname, info["kind"], len(data), sha,
             json.dumps(info["members"]), store.name, key, verified_at, json.dumps(PARSER_VERSIONS, sort_keys=True),
             config.APP_VERSION, user["username"], uploaded_at, ip, "Archived and checksum-verified"))
        for e in existing:
            c.execute("UPDATE uploads SET status = 'superseded', message = ? WHERE id = ?", (f"Superseded by #{uid}", e["id"]))
        audit.append(c, user["username"], "upload_archived",
                     {"upload_id": uid, "segment": segment, "file_type": file_type, "trade_date": prov["trade_date"],
                      "cycle": prov["cycle"], "cycle_source": prov["cycle_source"], "sha256": sha,
                      "superseded": [e["id"] for e in existing]}, ip)

    # 5. manifest next to the original (write-once)
    manifest = {
        "upload_id": uid, "segment": segment, "file_type": file_type, "filename": fname,
        "trade_date": prov["trade_date"], "trade_date_source": prov["trade_date_source"],
        "cycle": prov["cycle"], "cycle_source": prov["cycle_source"], "source_timestamp": prov["source_timestamp"],
        "source_meta": prov["source_meta"], "size_bytes": len(data), "sha256": sha, "content_kind": info["kind"],
        "inner_members": info["members"], "archive_backend": store.name, "archive_key": key,
        "archive_verified_at": verified_at, "parser_versions": PARSER_VERSIONS, "app_version": config.APP_VERSION,
        "uploaded_by": user["username"], "uploaded_at": uploaded_at,
    }
    mkey = f"manifests/{segment}/{prov['trade_date']}/{uid}.json"
    try:
        store.put_verified(mkey, json.dumps(manifest, indent=2, sort_keys=True).encode(), "application/json")
        with db.tx() as c:
            c.execute("UPDATE uploads SET manifest_key = ? WHERE id = ?", (mkey, uid))
    except StorageError as e:
        audit.log("system", "manifest_failed", {"upload_id": uid, "reason": str(e)})
    return get(uid)


def _row(r):
    if not r:
        return None
    out = dict(r)
    for k in ("source_meta", "inner_manifest", "parser_versions"):
        try:
            out[k] = json.loads(out[k]) if out.get(k) else None
        except Exception:
            pass
    return out


def get(upload_id):
    return _row(db.query_one("SELECT * FROM uploads WHERE id = ?", (upload_id,)))


def listing(segment=None, limit=200):
    limit = max(1, min(int(limit or 200), 1000))
    if segment:
        rows = db.query("SELECT * FROM uploads WHERE segment = ? ORDER BY id DESC LIMIT ?", (segment, limit))
    else:
        rows = db.query("SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows]


def mark_deleted(user, upload_id, reason, ip=None):
    """Uploads are never removed from the log; they are withdrawn from use. The archived original stays."""
    if not (reason or "").strip():
        raise UploadRejected("Give a reason")
    with db.tx() as c:
        r = c.execute("SELECT id, status FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if not r:
            raise LookupError("Upload not found")
        if r["status"] == "deleted":
            raise UploadRejected("Already withdrawn")
        c.execute("UPDATE uploads SET status = 'deleted', message = ? WHERE id = ?",
                  (f"Withdrawn by {user['username']}: {reason.strip()[:200]}", upload_id))
        audit.append(c, user["username"], "upload_withdrawn", {"upload_id": upload_id, "reason": reason.strip()[:200]}, ip)
    return get(upload_id)


def verify_archive(upload_id):
    """Re-download an archived original and compare its checksum with the database."""
    u = get(upload_id)
    if not u:
        raise LookupError("Upload not found")
    data = get_store().get(u["archive_key"])
    actual = hashlib.sha256(data).hexdigest()
    return {"upload_id": upload_id, "ok": actual == u["sha256"], "expected": u["sha256"], "actual": actual, "size": len(data)}
