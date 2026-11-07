"""Command line:  python -m exfetch <command> [options]

  run       fetch everything due today (or --date), optionally one --phase
  probe     try every URL/strategy for a date, save nothing, report what works
  backfill  fetch a date range
  status    list what's on disk for a date
  latest    show the newest file per source (mirrored in .../latest/<source>/)
  inspect   dump the links on a page (to fix an `html:` regex quickly)
  serve     stay running and fetch on the IST schedule in sources.yaml
  load      rebuild a segment's database (db/<SEG>.sqlite) from files already on disk
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime

from .dates import today_ist
from .runner import Agent


def _date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d%m%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"bad date {s!r}, use YYYY-MM-DD")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="exfetch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="sources.yaml")
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_date=True):
        if need_date:
            p.add_argument("--date", type=_date)
        p.add_argument("--only", nargs="*", help="source ids or globs (nfo.*, mcx.span) or segments (NFO)")

    p = sub.add_parser("run"); common(p)
    p.add_argument("--phase", choices=["bod", "intraday", "eod"])
    p.add_argument("--force", action="store_true", help="re-download even if already saved")
    p = sub.add_parser("probe"); common(p)
    p = sub.add_parser("backfill"); common(p, False)
    p.add_argument("--from", dest="start", type=_date, required=True)
    p.add_argument("--to", dest="end", type=_date, default=None)
    p = sub.add_parser("status"); p.add_argument("--date", type=_date)
    p = sub.add_parser("latest"); p.add_argument("--only", nargs="*")
    p = sub.add_parser("inspect")
    p.add_argument("url"); p.add_argument("--render", action="store_true")
    p.add_argument("--grep", default="", help="regex to filter links")
    p.add_argument("--session", default=None, help="nse | bse | mcx")
    sub.add_parser("serve")
    p = sub.add_parser("load")
    p.add_argument("--segment", nargs="*", help="default: every segment with a loader (NFO)")
    p.add_argument("--date", type=_date, help="load that day's history folders instead of latest/")
    p.add_argument("--cycle", help="with --date: NFO SPAN cycle i1..i5 or s, NSE VaR batch 1..6 (default: newest that day)")
    p.add_argument("--force", action="store_true", help="replace a database built from a newer SPAN cycle")
    p.add_argument("--db-dir", help="write here instead of the live db/ (e.g. to inspect an old day)")

    a = ap.parse_args(argv)
    agent = Agent(a.config, a.data_dir)

    if a.cmd == "run":
        d = a.date or today_ist()
        print(f"== {d} {a.phase or 'all phases'}")
        res = agent.run(d, [a.phase] if a.phase else None, a.only, a.force)
        summary = {}
        for r in res:
            summary[r.status] = summary.get(r.status, 0) + 1
        print("summary:", summary or "nothing due (holiday/weekend?)")
        return 1 if summary.get("ERROR") or summary.get("BLOCKED") else 0

    if a.cmd == "probe":
        d = a.date or agent.cal.previous_trading_day(today_ist())
        print(f"== probing for {d} (nothing is saved)")
        rows = agent.probe(d, a.only)
        ok = {r[0] for r in rows if r[3] == "OK"}
        bad = sorted({r[0] for r in rows} - ok)
        print(f"\n{len(ok)} sources reachable; {len(bad)} need fixing: {', '.join(bad) or '-'}")
        return 0

    if a.cmd == "backfill":
        agent.backfill(a.start, a.end or today_ist(), a.only)
        return 0

    if a.cmd == "status":
        d = a.date or today_ist()
        rows = agent.store.status(d)
        for sid, var, size, path in rows:
            print(f"  {sid:28} {var:10} {size:>12,}  {path}")
        print(f"{len(rows)} files for {d}")
        return 0

    if a.cmd == "latest":
        for src in agent.sources(a.only):
            row = agent.store.newest(src["id"])
            where = f"data/{src['segment']}/{src['kind']}/latest/{src['id']}/"
            print(f"  {src['id']:28} {row[1] + '  ' + row[0].rsplit('/', 1)[-1] if row else '-':55} {where if row else ''}")
        return 0

    if a.cmd == "inspect":
        from .resolvers import page_links
        pat = re.compile(a.grep, re.I) if a.grep else None
        for h, t in page_links(a.url, agent.http, a.session, a.render):
            if not pat or pat.search(h) or pat.search(t):
                print(f"{t[:50]:50}  {h}")
        return 0

    if a.cmd == "load":
        from loader import SEGMENTS
        failed = 0
        for seg in a.segment or list(SEGMENTS):
            print(f"== loading {seg} from {a.date or 'latest/'}")
            try:
                info = agent.load(seg, a.date, a.cycle, a.force, a.db_dir)
            except Exception as e:
                print(f"  FAILED {type(e).__name__}: {e}  (previous database left in place)")
                failed += 1
                continue
            if "span_cycle" in info:
                if info.get("basis") == "bod":
                    print(f"  start of day for {info['trade_date']}: SPAN {info['span_business_date']} "
                          f"{info['span_cycle']} created {info['span_created']}"
                          + (f"  STALE: {info['bod_note']}" if info.get("bod_stale") else ""))
                    lt = info.get("latest") or {}
                    if lt:
                        print(f"  latest (information only): SPAN {lt['span_business_date']} {lt['span_cycle']} "
                              f"created {lt['span_created']}")
                else:
                    print(f"  SPAN {info['span_cycle']} created {info['span_created']}; {info['contracts']:,} contracts "
                          f"({info['master_not_yet_in_span']:,} listed but no risk data yet)")
            else:
                print(f"  VaR batch {info['var_batch']} of {info['var_date']} (fetched {info['var_fetched_at'] or '?'}); "
                      f"{info['securities']:,} securities, {info['securities_without_var']} without a VaR row")
        return 1 if failed else 0

    if a.cmd == "serve":
        try:
            agent.serve()
        except KeyboardInterrupt:
            print("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
