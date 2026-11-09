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
