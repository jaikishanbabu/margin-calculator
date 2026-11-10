"""NFO validation: the owner's engine tests (validation/tests/test_engine_nfo.py, unchanged) run
one by one, and every broker basket in tests/fixtures/nfo_baskets.yaml matched on its own, so the
report says which baskets pass, which fail and why. Runs on GitHub (validate.yml) with the
22-28 Oct 2026 exchange files in validation/data/.

    PYTHONPATH=engineFolder python validation/run_nfo.py --out report.json
"""
import argparse
import contextlib
import io
import json
import math
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))           # engineFolder: engine/, loader/, exfetch/
sys.path.insert(0, str(HERE / "tests"))


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def data_inventory(data):
    span = Path(data) / "NFO" / "span"
    out = {}
    for day in sorted(p for p in span.glob("20*") if p.is_dir()) if span.is_dir() else []:
        out[day.name] = sorted(f.name.split(".")[2] for f in day.glob("nsccl.*.spn"))
    return out


def run_tests(t, skip=("test_broker_baskets",)):
    rows = []
    for name in sorted(n for n in dir(t) if n.startswith("test_") and callable(getattr(t, n))):
        if name in skip:
            continue
        buf, t0 = io.StringIO(), time.time()
        try:
            with contextlib.redirect_stdout(buf):
                getattr(t, name)()
            out = buf.getvalue()
            status = "skipped" if "skip" in out.lower() and "not on disk" in out.lower() else "passed"
            rows.append({"name": name, "status": status, "detail": out.strip()[-600:], "secs": round(time.time() - t0, 1)})
        except Exception as e:
            rows.append({"name": name, "status": "failed", "secs": round(time.time() - t0, 1),
                         "detail": (f"{type(e).__name__}: {e}\n" + buf.getvalue())[-1200:]})
        print(f"  {rows[-1]['status']:8} {name}")
    return rows


def run_baskets(t):
    import yaml
    fx = yaml.safe_load(t.FIXTURES.read_text())
    rows = []
    for b in fx["baskets"]:
        row = {"id": b["id"], "broker": b.get("broker"), "captured_at": b.get("captured_at"),
               "legs": [{k: lg.get(k) for k in ("symbol", "expiry", "type", "strike", "side", "qty")} for lg in b["legs"]]}
        if not t._filled(b):
            rows.append(dict(row, status="pending", detail="no broker figures yet"))
            continue
        want = t.want_of(b)
        try:
            m = t.match_basket(fx, b)
        except Exception as e:
            rows.append(dict(row, status="error", detail=f"{type(e).__name__}: {e}"[:600], want=want))
            print(f"  error    {b['id']}: {e}")
            continue
        tried = [{"cycle": str(x.cycle), "bad": x.bad, "error": f"{type(x.error).__name__}: {x.error}" if x.error else None}
                 for x in m.tried]
        best = m.best()
        ours = {k: _num(best.got.get(k)) for k in want} if best and best.got else {}
        if m.hit:
            status = "lagged" if m.lagged else "passed"
            detail = f"matches SPAN {m.hit}" + (f" (live was {m.live})" if m.lagged else "")
        else:
            status = "failed"
            detail = f"no cycle matches; closest {best.cycle if best else '-'}"
        rows.append(dict(row, status=status, detail=detail, hit=str(m.hit) if m.hit else None,
                         live=str(m.live) if m.live else None, want=want, ours=ours,
                         diff={k: (round(ours[k] - w, 2) if ours.get(k) is not None else None) for k, w in want.items()},
                         tried=tried))
        print(f"  {status:8} {b['id']}: {detail}")
    return rows


RULES = {
    "bod": "Start-of-day file only",
    "live": "Latest file only",
    "portfolio_max": "Higher of start-of-day and latest - whole portfolio",
    "underlying_max": "Higher of start-of-day and latest - per underlying",
}


def rule_check(t):
    """Which blocking rule reproduces the brokers' figures? For every basket with figures, work
    out the portfolio on the start-of-day file and on the file live at capture time, then apply
    each rule and compare SPAN / exposure / total with the broker (same tolerance)."""
    import yaml
    from engine import match as mm
    fx = yaml.safe_load(t.FIXTURES.read_text())
    pct, abs_ = fx.get("tolerance_pct", 0.05), fx.get("tolerance_abs", 5)
    dbs = t.cycle_dbs()
    rows = []
    for b in fx["baskets"]:
        if not t._filled(b):
            continue
        at = t._captured(b)
        want = {k: float(b["expected"][k]) for k in ("span", "exposure", "total") if k in b["expected"]}
        row = {"id": b["id"], "broker": b.get("broker"), "captured_at": b.get("captured_at"), "want": want}
        try:
            pos = [t._position(lg) for lg in b["legs"]]
            cands = mm.candidate_cycles(t.DATA, at, None)
            live = cands[0]
            bod = mm.bod_cycle(cands, at)
            as_of = at.date().isoformat()
            m = {"bod": dbs.engine(bod).margin(pos, as_of=as_of, alone=False),
                 "live": dbs.engine(live).margin(pos, as_of=as_of, alone=False)}
        except Exception as e:
            rows.append(dict(row, error=f"{type(e).__name__}: {e}"[:300]))
            continue

        def figs(x):
            return {"span": x["span"], "exposure": x["exposure"], "total": x["total"]}
        got = {"bod": figs(m["bod"]), "live": figs(m["live"])}
        got["portfolio_max"] = got["bod"] if got["bod"]["total"] >= got["live"]["total"] else got["live"]
        und = {"span": 0.0, "exposure": 0.0}
        for sym in set(m["bod"]["underlyings"]) | set(m["live"]["underlyings"]):
            a, c = m["bod"]["underlyings"].get(sym), m["live"]["underlyings"].get(sym)
            pick = max((x for x in (a, c) if x), key=lambda x: x["span"] + x["exposure"])
            und["span"] += pick["span"]
            und["exposure"] += pick["exposure"]
        extra = max(m["bod"].get("other", 0), m["live"].get("other", 0))
        und["total"] = und["span"] + und["exposure"] + extra
        got["underlying_max"] = und
        ok = {r: not mm.off(want, got[r], pct, abs_) for r in RULES}
        differs = abs(got["bod"]["total"] - got["live"]["total"]) > mm.tolerance(want.get("total", 0), pct, abs_)
        rows.append(dict(row, bod=f"{bod.day} {bod.cycle}", live=f"{live.day} {live.cycle}", same_file=bod == live,
                         files_differ=differs, matches=ok,
                         got={r: {k: _num(v) for k, v in g.items()} for r, g in got.items()}))
    summary = {}
    for r in RULES:
        summary[r] = {"all": sum(1 for x in rows if x.get("matches", {}).get(r)),
                      "deciding": sum(1 for x in rows if x.get("files_differ") and x["matches"].get(r))}
    summary["baskets"] = sum(1 for x in rows if "matches" in x)
    summary["deciding_baskets"] = sum(1 for x in rows if x.get("files_differ"))
    summary["errors"] = sum(1 for x in rows if "error" in x)
    return {"names": RULES, "summary": summary, "baskets": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import test_engine_nfo as t                      # the owner's file, unchanged
    t0 = time.time()
    report = {"data": data_inventory(t.DATA), "tests": [], "baskets": []}
    print("data on disk:", report["data"])
    try:
        report["tests"] = run_tests(t)
        report["baskets"] = run_baskets(t)
    except Exception:
        report["crash"] = traceback.format_exc()[-2000:]
    try:                                   # information only: never changes pass / fail
        print("rule check ...")
        report["rules"] = rule_check(t)
        print("rules:", report["rules"]["summary"])
    except Exception:
        report["rules_error"] = traceback.format_exc()[-2000:]
    s = {}
    for r in report["tests"]:
        s[f"tests_{r['status']}"] = s.get(f"tests_{r['status']}", 0) + 1
    for r in report["baskets"]:
        s[f"baskets_{r['status']}"] = s.get(f"baskets_{r['status']}", 0) + 1
    s["secs"] = round(time.time() - t0)
    report["summary"] = s
    Path(a.out).write_text(json.dumps(report, default=str))
    print("summary:", s)


if __name__ == "__main__":
    main()
