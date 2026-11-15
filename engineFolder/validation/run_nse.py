"""NSE cash validation: the owner's cash engine tests (validation/tests/test_engine_nse.py, unchanged)
run one by one, and every Dhan basket in tests/fixtures/nse_baskets.yaml matched on its own against
the VaR batches live at its capture time (or earlier). Runs on GitHub (validate.yml) with the
23-27 Oct 2026 NSE cash files in validation/data/.

    PYTHONPATH=engineFolder python validation/run_nse.py --out report.json
"""
import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "tests"))
from run_nfo import _num, run_tests  # noqa: E402


def data_inventory(data):
    var = Path(data) / "EQ" / "var"
    out = {}
    for day in sorted(p for p in var.glob("20*") if p.is_dir()) if var.is_dir() else []:
        out[day.name] = sorted(f.name.split("_")[-1].split(".")[0] for f in day.glob("C_VAR1_*.DAT"))
    return out


def schedule_batches(data_dir, at):
    """VaR batches a broker could have had at `at`, by NSE's schedule (exfetch_sources: batch 1 start of day,
    2-4 intraday, 5-6 end of day) - for files downloaded later, whose fetch times say nothing about when
    NSE published them. Order: the capture day's (batch 1; 2-4 only after the close), then every batch
    of the previous day on disk, newest first."""
    import re
    from engine.match import Cycle
    base = Path(data_dir) / "EQ" / "var"
    day = at.date().isoformat()
    folders = sorted(p.name for p in base.iterdir() if p.name[:2] == "20" and p.name <= day) if base.is_dir() else []
    out = []
    for d in reversed(folders[-2:]):
        nums = sorted({int(m[1]) for p in (base / d).glob("C_VAR1_*_*.DAT") if (m := re.search(r"_(\d)\.DAT$", p.name))},
                      reverse=True)
        if d == day:
            nums = [n for n in nums if n == 1 or (n <= 4 and at.strftime("%H:%M") >= "15:30")]
        out += [Cycle(d, str(n), "") for n in nums]
    return out


def run_baskets(t):
    import yaml
    from engine import CashOrder
    from engine.match import CycleDatabases, candidate_batches, match
    path = t.ROOT / "tests/fixtures/nse_baskets.yaml"
    dbs = CycleDatabases(t.DATA, t._TMP / "cycles", segment="NSE")
    rows = []
    for b in yaml.safe_load(path.read_text())["baskets"]:
        legs = [{"symbol": o["symbol"], "type": "cash", "side": o["side"], "qty": o["qty"], "price": o["price"]}
                for o in b["orders"]]
        row = {"id": b["id"], "broker": b.get("broker"), "captured_at": str(b.get("captured_at")), "legs": legs,
               "what": b.get("what")}
        want = {k: float(v) for k, v in b["expected"].items()}
        for i, o in enumerate(b["orders"], 1):
            want.update({f"leg{i}.{k}": float(v) for k, v in o["expected"].items()})
        try:
            orders = [t.CashOrder(o["symbol"], o["side"], o["qty"], o["price"]) for o in b["orders"]]
            at = datetime.strptime(str(b["captured_at"]), "%Y-%m-%d %H:%M")
            cands = candidate_batches(t.DATA, at)
            if not cands:                      # downloaded after the quote: go by NSE's batch schedule
                cands = schedule_batches(t.DATA, at)
                row["batches_by"] = "NSE's batch schedule (files downloaded later)"
            m = match(dbs, at, orders, want, candidates=cands)
        except Exception as e:
            rows.append(dict(row, status="error", detail=f"{type(e).__name__}: {e}"[:600], want=want))
            continue
        best = m.best()
        ours = {k: _num(best.got.get(k)) for k in want} if best and best.got else {}
        tried = [{"cycle": str(x.cycle), "bad": x.bad, "error": f"{type(x.error).__name__}: {x.error}" if x.error else None}
                 for x in m.tried]
        if m.hit:
            status = "lagged" if m.lagged else "passed"
            detail = f"matches VaR batch {m.hit}" + (f" (live was {m.live})" if m.lagged else "")
        else:
            status, detail = "failed", f"no VaR batch matches; closest {best.cycle if best else '-'}"
        rows.append(dict(row, status=status, detail=detail, hit=str(m.hit) if m.hit else None, want=want, ours=ours,
                         diff={k: (round(ours[k] - w, 2) if ours.get(k) is not None else None) for k, w in want.items()},
                         tried=tried))
        print(f"  {status:8} {b['id']}: {detail}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import test_engine_nse as t                      # the owner's file, unchanged
    t0 = time.time()
    report = {"data": data_inventory(t.DATA), "tests": [], "baskets": [], "segment": "NSECM"}
    print("data on disk:", report["data"])
    try:
        report["tests"] = run_tests(t)
        report["baskets"] = run_baskets(t)
    except Exception:
        report["crash"] = traceback.format_exc()[-2000:]
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
