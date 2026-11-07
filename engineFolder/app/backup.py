"""Database backup export and restore testing, through the storage interface only.

backup_export():
  - dumps every table to gzip JSON-lines `backups/<stamp>/db.jsonl.gz`
  - writes `backups/<stamp>/manifest.json`: row counts, per-table sha256, sha256 of the gz,
    audit-chain head (last seq + hash), schema/app versions
  - verifies both objects by reading them back
  - prunes backups beyond the retention count (never the newest restore-tested one)

restore_test():
  - downloads the newest backup, checks the gz sha256 against its manifest
  - loads it into a throw-away in-memory SQLite database built from the same migrations
  - checks row counts, verifies the restored audit hash chain, and checks the live audit
    log still contains the restored head hash at the same seq (detects later tampering
    or truncation of the live log)
  - re-downloads a sample of archived original files and verifies their checksums
"""
import gzip
import hashlib
import json
import random
import sqlite3
from datetime import datetime, timezone

from . import audit, config, db
from .storage import NotFound, get_store

BACKUP_FORMAT = "margin-backup/1"
DEFAULT_BACKUP_RETENTION = 12        # keep this many backups
ORIGINALS_SAMPLE = 5                  # archived originals re-verified per restore test


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().strftime("%Y%m%dT%H%M%SZ")


def _order_col(table):
    return "seq" if table == "audit_log" else ("key" if table == "settings" else "id")


def _start_run(kind, who):
    with db.tx() as c:
        return c.insert("INSERT INTO backup_runs (kind, started_at, status, backend, triggered_by) VALUES (?, ?, 'running', ?, ?)",
                        (kind, _now().isoformat(timespec="seconds"), get_store().name, who))


def _finish_run(run_id, status, location=None, sha=None, details=None):
    with db.tx() as c:
        c.execute("UPDATE backup_runs SET finished_at = ?, status = ?, location = ?, sha256 = ?, details = ? WHERE id = ?",
                  (_now().isoformat(timespec="seconds"), status, location, sha, json.dumps(details, default=str), run_id))


def backup_export(who="system"):
    run_id = _start_run("backup", who)
    store = get_store()
    try:
        chain = audit.verify()
        lines, counts, table_sha = [], {}, {}
        for t in db.table_names():
            rows = db.query(f"SELECT * FROM {t} ORDER BY {_order_col(t)}")
            if t == "backup_runs":
                rows = [r for r in rows if r["id"] != run_id]
            h = hashlib.sha256()
            for r in rows:
                line = json.dumps({"t": t, "r": r}, sort_keys=True, default=str, separators=(",", ":"))
                h.update(line.encode())
                lines.append(line)
            counts[t] = len(rows)
            table_sha[t] = h.hexdigest()
        raw = ("\n".join(lines) + "\n").encode()
        gz = gzip.compress(raw, mtime=0)
        stamp = _stamp()
        key = f"backups/{stamp}/db.jsonl.gz"
        store.put_verified(key, gz, "application/gzip")
        gz_sha = hashlib.sha256(gz).hexdigest()
        manifest = {
            "format": BACKUP_FORMAT, "created_at": _now().isoformat(timespec="seconds"), "created_by": who,
            "object": key, "sha256": gz_sha, "size_bytes": len(gz), "row_counts": counts, "table_sha256": table_sha,
            "audit_head": {"last_seq": chain.get("last_seq"), "head_hash": chain.get("head_hash"), "chain_ok": chain["ok"]},
            "app_version": config.APP_VERSION, "backend": store.name, "db": "postgres" if db.is_pg() else "sqlite",
        }
        mkey = f"backups/{stamp}/manifest.json"
        store.put_verified(mkey, json.dumps(manifest, indent=2, sort_keys=True).encode(), "application/json")
        pruned = prune_backups()
        details = {"manifest": mkey, "row_counts": counts, "audit_chain_ok": chain["ok"], "pruned": pruned}
        _finish_run(run_id, "ok", key, gz_sha, details)
        audit.log(who, "backup_created", {"run_id": run_id, "object": key, "sha256": gz_sha, "rows": counts})
        return {"ok": True, "run_id": run_id, "object": key, "manifest": mkey, "sha256": gz_sha, **details}
    except Exception as e:
        _finish_run(run_id, "failed", details={"error": str(e)})
        audit.log(who, "backup_failed", {"run_id": run_id, "error": str(e)})
        return {"ok": False, "run_id": run_id, "error": str(e)}


def list_backups():
    keys = get_store().list("backups/")
    return sorted({k.split("/")[1] for k in keys if k.endswith("manifest.json")})


def prune_backups():
    keep = int(db.get_setting("backup_retention_count", DEFAULT_BACKUP_RETENTION))
    stamps = list_backups()
    protected = None
    last_ok = db.query_one("SELECT location FROM backup_runs WHERE kind = 'restore_test' AND status = 'ok' ORDER BY id DESC LIMIT 1")
    if last_ok and last_ok["location"]:
        protected = last_ok["location"].split("/")[1] if "/" in last_ok["location"] else None
    victims = [s for s in stamps[:-keep] if s != protected] if len(stamps) > keep else []
    store = get_store()
    for s in victims:
        for k in store.list(f"backups/{s}/"):
            store.delete(k)
    return victims


def _restore_into_sqlite(lines):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for f in sorted((db.MIGRATIONS_DIR / "sqlite").glob("*.sql")):
        conn.executescript(f.read_text())
    conn.execute("DELETE FROM schema_migrations")
    for line in lines:
        if not line.strip():
            continue
        obj = json.loads(line)
        t, r = obj["t"], obj["r"]
        cols = list(r.keys())
        conn.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])
    conn.commit()
    return conn


def restore_test(who="system", stamp=None):
    run_id = _start_run("restore_test", who)
    store = get_store()
    checks = []

    def check(name, ok, info=""):
        checks.append({"check": name, "ok": bool(ok), "info": info})
        return ok

    try:
        stamps = list_backups()
        if not stamps:
            raise RuntimeError("No backups exist yet - run a backup first")
        stamp = stamp or stamps[-1]
        manifest = json.loads(store.get(f"backups/{stamp}/manifest.json"))
        check("manifest format", manifest.get("format") == BACKUP_FORMAT, manifest.get("format"))
        gz = store.get(manifest["object"])
        check("backup checksum", hashlib.sha256(gz).hexdigest() == manifest["sha256"], manifest["sha256"][:16])
        raw = gzip.decompress(gz).decode()
        lines = raw.splitlines()
        # per-table checksums
        per = {}
        for line in lines:
            if line.strip():
                per.setdefault(json.loads(line)["t"], hashlib.sha256()).update(line.encode())
        for t, expect in manifest["table_sha256"].items():
            got = per.get(t, hashlib.sha256()).hexdigest()
            check(f"table checksum {t}", got == expect)
        conn = _restore_into_sqlite(lines)
        for t, n in manifest["row_counts"].items():
            got = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            check(f"row count {t}", got == n, f"{got}/{n}")
        restored_audit = [dict(r) for r in conn.execute(
            "SELECT seq, ts, username, ip, action, detail, prev_hash, row_hash FROM audit_log ORDER BY seq")]
        rv = audit.verify_rows(restored_audit)
        check("restored audit chain", rv["ok"], rv.get("reason") or f"{rv['checked']} rows")
        head = manifest["audit_head"]
        if head.get("last_seq"):
            live = db.query_one("SELECT row_hash FROM audit_log WHERE seq = ?", (head["last_seq"],))
            check("live audit log still contains backup head", live and live["row_hash"] == head["head_hash"],
                  f"seq {head['last_seq']}")
        live_chain = audit.verify()
        check("live audit chain", live_chain["ok"], live_chain.get("reason") or f"{live_chain['checked']} rows")
        # archived originals
        ups = [dict(r) for r in conn.execute("SELECT id, archive_key, sha256, archive_encoding FROM uploads WHERE status != 'failed'")]
        sample = random.sample(ups, min(ORIGINALS_SAMPLE, len(ups)))
        for u in sample:
            try:
                data = store.get(u["archive_key"])
                if (u.get("archive_encoding") or "identity") == "gzip":
                    data = gzip.decompress(data)
                check(f"original #{u['id']} checksum", hashlib.sha256(data).hexdigest() == u["sha256"])
            except NotFound:
                check(f"original #{u['id']} present", False, "missing from archive")
        if not ups:
            check("archived originals", True, "none uploaded yet")
        ok = all(c["ok"] for c in checks)
        details = {"backup": stamp, "checks": checks, "originals_checked": len(sample)}
        _finish_run(run_id, "ok" if ok else "failed", manifest["object"], manifest["sha256"], details)
        audit.log(who, "restore_test_" + ("passed" if ok else "failed"),
                  {"run_id": run_id, "backup": stamp, "failed": [c["check"] for c in checks if not c["ok"]]})
        return {"ok": ok, "run_id": run_id, **details}
    except Exception as e:
        _finish_run(run_id, "failed", details={"error": str(e), "checks": checks})
        audit.log(who, "restore_test_failed", {"run_id": run_id, "error": str(e)})
        return {"ok": False, "run_id": run_id, "error": str(e), "checks": checks}


def status():
    last_b = db.query_one("SELECT * FROM backup_runs WHERE kind = 'backup' AND status = 'ok' ORDER BY id DESC LIMIT 1")
    last_r = db.query_one("SELECT * FROM backup_runs WHERE kind = 'restore_test' ORDER BY id DESC LIMIT 1")

    def age_days(r):
        if not r or not r.get("finished_at"):
            return None
        return round((_now() - datetime.fromisoformat(r["finished_at"])).total_seconds() / 86400, 1)

    return {"last_backup_at": last_b and last_b["finished_at"], "last_backup_age_days": age_days(last_b),
            "last_restore_test_at": last_r and last_r["finished_at"],
            "last_restore_test_ok": bool(last_r and last_r["status"] == "ok"),
            "backup_overdue": (age_days(last_b) is None) or age_days(last_b) > 7}
