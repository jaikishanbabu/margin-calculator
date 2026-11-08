"""Parse downloaded exchange files into one SQLite database per segment.

    from loader import load_segment
    load_segment("NFO")                         # from data/NFO/*/latest/
    load_segment("NFO", on_date=date(2026, 10, 23))   # from that day's history folders
    load_segment("NFO", on_date=date(2026, 10, 26), span_cycle="i2")   # a specific SPAN cycle
    load_segment("NSE", on_date=date(2026, 10, 26), span_cycle="3")    # NSE cash: VaR batch 3

Writes db/<SEG>.sqlite.tmp, checks it, then swaps it in; on any error the
previous database is left untouched.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from . import bfo, db, files, mcx, nfo, nse

# db/<key>.sqlite <- module; a module's DATA_SEGMENT is its data/ folder if not the key
SEGMENTS = {"NFO": nfo, "NSE": nse, "BFO": bfo, "MCX": mcx}
RATES = Path(__file__).with_name("static_rates.yaml")

__all__ = ["SEGMENTS", "OlderThanLive", "load_segment"]


class OlderThanLive(RuntimeError):
    """The load would replace a database built from newer data (a newer SPAN cycle / VaR batch)."""


def data_segment(segment: str) -> str:
    """data/<folder> a loader reads: NSE cash files live in data/EQ/."""
    return getattr(SEGMENTS[segment], "DATA_SEGMENT", segment)


def _freshness(mod, info: dict) -> tuple:
    if hasattr(mod, "freshness"):
        return mod.freshness(info)
    return (info.get("span_business_date") or "", info.get("span_created") or "")


def _describe(mod, info: dict) -> str:
    if hasattr(mod, "describe"):
        return mod.describe(info)
    return f"SPAN {info.get('span_cycle')} of {info.get('span_created')}"


def load_segment(segment: str = "NFO", data_dir: str | Path = "data", db_dir: str | Path = "db",
                 on_date: date | None = None, rates: str | Path = RATES, log=print,
                 span_cycle: str | None = None, force: bool = False, basis: str | None = None,
                 today: date | None = None) -> dict:
    """force: allow replacing a database built from newer data (e.g. a deliberate
    rebuild of an earlier day into the live db/). span_cycle: the SPAN cycle (NFO)
    or VaR batch (NSE) to load, with on_date.
    NFO basis: "bod" = the trade date's start-of-day file (nfo.bod_choice, as of
    `today`), into db/NFO.sqlite, what client margins use; "latest" = the newest cycle
    in latest/, into db/NFO.latest.sqlite (information only). None = as before: the
    newest cycle (of on_date), into db/NFO.sqlite."""
    mod = SEGMENTS.get(segment)
    if mod is None:
        raise ValueError(f"no loader for {segment} yet (have: {', '.join(SEGMENTS)})")
    t0 = time.time()
    if span_cycle and not on_date:
        raise ValueError("span_cycle needs on_date (latest/ holds only the newest cycle)")
    bod = None
    if basis == "bod":
        from exfetch.dates import today_ist
        bod = mod.bod_choice(Path(data_dir), today or today_ist(), trade_date=on_date)
        wants = dict(mod.wants(bod["cycle"]))
        span = files.find(Path(data_dir), data_segment(segment), {"span": wants.pop("span")},
                          date.fromisoformat(bod["day"]))["span"]
        inputs = files.find(Path(data_dir), data_segment(segment), wants, date.fromisoformat(bod["trade_date"]))
        inputs["span"] = span
        if bod["stale"]:
            log(f"  STALE: {bod['note']}")
    else:
        inputs = files.find(Path(data_dir), data_segment(segment), mod.wants(span_cycle), on_date)
    final = Path(db_dir) / (f"{segment}.latest.sqlite" if basis == "latest" else f"{segment}.sqlite")
    with db.building(final, getattr(mod, "SCHEMA", db.SCHEMA)) as conn:
        info = mod.load(conn, inputs, Path(rates), log, Path(data_dir), **({"bod": bod} if bod else {}))
        # checked under the lock, so a concurrent load by the agent can't slip in between
        if final.exists() and not force:
            live = db.info(final)
            same_kind = live.get("basis", "cycle") == info.get("basis", "cycle")
            if same_kind and _freshness(mod, info) < _freshness(mod, live):
                raise OlderThanLive(f"{final} holds {_describe(mod, live)}; this load is {_describe(mod, info)}. "
                                    f"Use force (--force) to replace it, or load into another --db-dir.")
    info["seconds"] = round(time.time() - t0, 1)
    log(f"  {segment}: loaded into {final} in {info['seconds']}s")
    return info
