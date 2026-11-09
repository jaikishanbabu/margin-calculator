"""Stage 3: results of validating a segment's margins against broker baskets.

A segment counts as validated when its latest validation passed (every basket with broker figures
matched within tolerance, every engine test passed) and was run against the rules the engine uses
now (static_rates.yaml / mcx_contracts.yaml unchanged since). Until then the calculator labels that
segment's margins "not validated".
"""
import json
from datetime import datetime, timezone

from . import audit, db

SEGMENTS = ("NFO",)          # segments with broker baskets; the rest come in Stage 5


def iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_rules():
    from .build_job import rules_digest
    return rules_digest()


def record(run_id, segment, report):
    if segment not in SEGMENTS:
        raise ValueError("No validation for this segment yet")
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    s = report.get("summary") or {}
    checked = s.get("baskets_passed", 0) + s.get("baskets_lagged", 0)
    ok = (not report.get("crash") and checked > 0 and not s.get("baskets_failed") and not s.get("baskets_error")
          and not s.get("tests_failed"))
    status = "passed" if ok else "failed"
    rules = current_rules()
    with db.tx() as c:
        vid = c.insert("INSERT INTO validations (segment, run_id, status, summary, report, rules, code_version, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (segment, run_id, status, json.dumps(s), json.dumps(report)[:5_000_000], json.dumps(rules),
                        str(report.get("code_version") or "")[:40], iso()))
        audit.append(c, "auto-fetcher", "validation_recorded", {"validation_id": vid, "segment": segment, "status": status,
                                                                "summary": s, "run_id": run_id})
    return get(vid, with_report=False)


def get(vid, with_report=True):
    r = db.query_one("SELECT * FROM validations WHERE id = ?", (vid,))
    if not r:
        return None
    r = dict(r)
    r["summary"] = json.loads(r["summary"])
    r["rules"] = json.loads(r["rules"] or "{}")
    if with_report:
        r["report"] = json.loads(r["report"])
    else:
        r.pop("report", None)
    return r


def latest(segment, with_report=False):
    r = db.query_one("SELECT id FROM validations WHERE segment = ? ORDER BY id DESC LIMIT 1", (segment,))
    return get(r["id"], with_report) if r else None


def status(segment):
    """{validated: bool, reason, latest} for the calculator and the Data readiness page."""
    if segment not in SEGMENTS:
        return {"validated": False, "reason": "No broker baskets for this segment yet (Stage 5)", "latest": None}
    v = latest(segment)
    if not v:
        return {"validated": False, "reason": "Not validated yet - run the validation", "latest": None}
    if v["status"] != "passed":
        return {"validated": False, "reason": "The latest validation failed", "latest": v}
    if v["rules"] != current_rules():
        return {"validated": False, "reason": "Rules changed since the last validation - run it again", "latest": v}
    return {"validated": True, "reason": "", "latest": v}


def history(segment, limit=10):
    rows = db.query("SELECT id FROM validations WHERE segment = ? ORDER BY id DESC LIMIT ?", (segment, limit))
    return [get(r["id"], with_report=False) for r in rows]
