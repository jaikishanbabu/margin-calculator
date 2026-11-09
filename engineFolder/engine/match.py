"""Which SPAN cycle a broker's margin figures match.

Brokers lag the exchange: Angel One kept Monday's i1 file until the evening and
Monday's final file past midnight. So for a basket and the figures a broker
showed at `captured_at`, try the SPAN cycle live then, then every earlier cycle
of that day and of the previous day on disk, newest first; the first cycle whose
figures are all within tolerance is the one the broker used.

    dbs = CycleDatabases(Path("data"))
    m = match(dbs, captured_at, positions, {"span": ..., "exposure": ..., "total": ...})
    m.hit, m.live, m.tried
    dbs.close()

Each cycle is loaded (loader.load_segment) into its own scratch database under a
temporary folder, never db/. Figure keys: span, exposure, total, net_premium,
benefit (sum of the legs margined alone - basket total) and leg<N>.<span|exposure|
total> (leg N, from 1, margined alone).

NSE cash works the same way with VaR batches instead of SPAN cycles
(candidate_batches, CycleDatabases(segment="NSE"), cash_figures). A VaR file has
no creation time, so a batch counts as live from when exfetch fetched it
(data/manifest.sqlite), else from the file's modification time.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import BseEngine, CashEngine, CashError, CashOrder, Engine, McxEngine, NoRiskData, Position, UnknownContract

SEGMENT = "NFO"
TOLERANCE_PCT = 0.05      # % of the broker's figure ...
TOLERANCE_ABS = 5.0       # ... or this many rupees, whichever is larger


@dataclass(frozen=True)
class Cycle:
    day: str              # SPAN business date, YYYY-MM-DD
    cycle: str            # i1..i5 or s
    created: str          # file creation time, "YYYY-MM-DD HH:MM"

    def __str__(self):
        return f"{self.day} {self.cycle} ({self.created[11:]})"


def candidate_cycles(data_dir: Path, at: datetime, span_cycle: Optional[str] = None,
                     segment: str = SEGMENT) -> list[Cycle]:
    """Cycles to check, in order: the one live at `at`, then every earlier one of
    that day and of the previous day on disk, newest first. Before a day's first
    file is on disk, the previous day's last file is the live one. With span_cycle,
    only that cycle of at's day."""
    from loader import SEGMENTS
    span_cycles = SEGMENTS[segment].span_cycles      # NFO: i1..i5/s; BFO: 00..04/FINAL

    day = at.date().isoformat()
    if span_cycle:
        hit = [c for c in span_cycles(data_dir, day) if c[1] == span_cycle]
        return [Cycle(day, hit[0][1], f"{hit[0][2]:%Y-%m-%d %H:%M}")] if hit else []
    base = Path(data_dir) / segment / "span"
    folders = sorted(p.name for p in base.iterdir() if p.name[:2] == "20" and p.name <= day) if base.is_dir() else []
    seq = [c for d in folders[-2:] for c in span_cycles(data_dir, d) if c[2] <= at]
    return [Cycle(d, c, f"{t:%Y-%m-%d %H:%M}") for d, c, t in reversed(seq)]


# Older files a broker may legitimately still be on (start of day / end of day); never an
# intraday cycle. NSE: i1 / s; BSE (when BFO is built): -00 / FINAL.
LAGGED_CYCLES = ("i1", "s", "00", "FINAL")
START_OF_DAY, END_OF_DAY = ("i1", "00"), ("s", "FINAL")


def bod_cycle(cands: list[Cycle], at: datetime) -> Optional[Cycle]:
    """The start-of-day file client margins use at `at` (SEBI: fixed BOD parameters all
    day): the newest i1 created by then; if that is older than the last trading day
    up to `at` (that day's i1 never arrived), the newest final (s) file after it,
    which NSE builds the next i1 from. cands: candidate_cycles(), newest first."""
    from datetime import timedelta
    i1 = next((c for c in cands if c.cycle in START_OF_DAY), None)
    day = at.date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    if i1 is not None and i1.day >= day.isoformat():
        return i1
    s = next((c for c in cands if c.cycle in END_OF_DAY and (i1 is None or c.day >= i1.day)), None)
    return s or i1


def candidate_batches(data_dir: Path, at: datetime) -> list[Cycle]:
    """NSE cash: VaR batches to check, in order: the one live at `at` (the newest
    fetched by then), then every earlier one of that day and the previous day on
    disk, newest first. Cycle.cycle is the batch number; created = fetch time."""
    import re
    import sqlite3

    base = Path(data_dir) / "EQ" / "var"
    day = at.date().isoformat()
    folders = sorted(p.name for p in base.iterdir() if p.name[:2] == "20" and p.name <= day) if base.is_dir() else []
    fetched = {}
    manifest = Path(data_dir) / "manifest.sqlite"
    if manifest.exists():
        conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        try:
            for detail, t in conn.execute("""SELECT detail, MIN(at) FROM attempts WHERE source = 'nse.var'
                                             AND status = 'OK' GROUP BY detail"""):
                fetched[str(detail).split(" ")[0]] = datetime.fromisoformat(t)
        finally:
            conn.close()
    seq = []
    for d in folders[-2:]:
        for p in (base / d).glob("C_VAR1_*_*.DAT"):
            m = re.search(r"_(\d)\.DAT$", p.name)
            t = fetched.get(p.name) or datetime.fromtimestamp(p.stat().st_mtime)
            if m and t <= at:
                seq.append((d, m[1], t))
    seq.sort(key=lambda x: (x[0], int(x[1])))
    return [Cycle(d, b, f"{t:%Y-%m-%d %H:%M}") for d, b, t in reversed(seq)]


class CycleDatabases:
    """One scratch database per SPAN cycle (NFO) or VaR batch (NSE), built on first
    use and kept for the caller's lifetime (a cycle takes a few seconds to load)."""

    def __init__(self, data_dir: Path, root: Optional[Path] = None, log=None, segment: str = SEGMENT):
        self.data_dir = Path(data_dir)
        self.segment = segment
        self._own = root is None
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="cycles_"))
        self.log = log or (lambda *a: None)
        self._engines: dict = {}

    def engine(self, c: Cycle) -> Engine:
        from loader import load_segment

        key = (c.day, c.cycle)
        if key not in self._engines:
            folder = self.root / f"{self.segment}_{c.day}_{c.cycle}"
            if not (folder / f"{self.segment}.sqlite").exists():
                load_segment(self.segment, self.data_dir, folder, date.fromisoformat(c.day), log=self.log,
                             span_cycle=c.cycle)
            cls = {"NSE": CashEngine, "BFO": BseEngine, "MCX": McxEngine}.get(self.segment, Engine)
            self._engines[key] = cls(folder / f"{self.segment}.sqlite")
        return self._engines[key]

    def close(self):
        self._engines.clear()
        if self._own:
            shutil.rmtree(self.root, ignore_errors=True)


def figures(eng: Engine, positions: list[Position], as_of: date) -> dict:
    """Our figures for a basket: the basket, its net premium, the benefit, and each
    leg margined alone. Raises NoRiskData / UnknownContract like Engine.margin."""
    m = eng.margin(positions, as_of=as_of)
    alone = [eng.margin([p], as_of=as_of) for p in positions]
    out = {k: m[k] for k in ("span", "exposure", "total", "net_premium")}
    out["benefit"] = sum(a["total"] for a in alone) - m["total"]
    for i, a in enumerate(alone, 1):
        for k in ("span", "exposure", "total"):
            out[f"leg{i}.{k}"] = a[k]
    return out


def cash_figures(eng: CashEngine, orders: list[CashOrder]) -> dict:
    """NSE cash: intraday margin (with the broker minimum), delivery value of the buys,
    and both per stock. Raises CashError like CashEngine.margin."""
    m = eng.margin(orders)
    out = {"intraday": m["total"]["intraday_margin"], "delivery": m["total"]["delivery_value"],
           "nse": m["total"]["nse_margin"]}         # at NSE's rates alone (never compared)
    for i, lg in enumerate(m["legs"], 1):
        out[f"leg{i}.intraday"] = lg["intraday_margin"]
        out[f"leg{i}.delivery"] = lg["delivery_value"]
    return out


def tolerance(want: float, pct: float = TOLERANCE_PCT, abs_: float = TOLERANCE_ABS) -> float:
    return max(abs_, abs(want) * pct / 100)


def off(want: dict, got: dict, pct: float = TOLERANCE_PCT, abs_: float = TOLERANCE_ABS) -> list[str]:
    """Keys of `want` whose figure in `got` is outside tolerance."""
    return [k for k, w in want.items() if abs(got[k] - float(w)) > tolerance(float(w), pct, abs_)]


@dataclass
class Tried:
    cycle: Cycle
    got: Optional[dict]       # our figures on this cycle (None if the engine raised)
    bad: list                 # keys out of tolerance
    error: Optional[Exception] = None
    miss: float = 0.0         # how far out: sum over bad keys of |ours - want| / tolerance


@dataclass
class Match:
    hit: Optional[Cycle]      # first cycle within tolerance, or None
    live: Optional[Cycle]     # the cycle live at captured_at (newest by then)
    tried: list = field(default_factory=list)
    bod: Optional[Cycle] = None   # NFO: the start-of-day file brokers should be on (tried first)

    @property
    def lagged(self) -> bool:
        return self.hit is not None and self.hit != self.live

    def closest(self) -> Optional[Tried]:
        """The cycle that ran with the fewest figures out of tolerance (then the smallest
        miss, then the newest): on a Fail, the cycle the broker most likely used."""
        runs = [t for t in self.tried if t.got is not None]
        return min(runs, key=lambda t: (len(t.bad), t.miss)) if runs else None

    def live_run(self) -> Optional[Tried]:
        return next((t for t in self.tried if t.cycle == self.live), None)

    def best(self) -> Optional[Tried]:
        """The matching cycle's figures, else the closest cycle's."""
        hit = [t for t in self.tried if t.got is not None and t.cycle == self.hit]
        return hit[0] if hit else self.closest()


def match(dbs: CycleDatabases, at: datetime, positions: list, want: dict,
          pct: float = TOLERANCE_PCT, abs_: float = TOLERANCE_ABS, span_cycle: Optional[str] = None,
          as_of: Optional[date] = None, candidates: Optional[list] = None, bod_first: bool = False) -> Match:
    """Try each candidate cycle (candidate_cycles, or candidate_batches for NSE cash)
    until one is within tolerance on every key in `want`. A cycle where the engine
    raises (NoRiskData / UnknownContract, CashError) is recorded with the error and
    skipped. positions: Positions (NFO) or CashOrders (NSE). bod_first (NFO): try the
    start-of-day file (bod_cycle) first; Match.bod says which it was."""
    cash = dbs.segment == "NSE"
    if candidates is None:
        candidates = candidate_batches(dbs.data_dir, at) if cash else \
            candidate_cycles(dbs.data_dir, at, span_cycle, dbs.segment)
    cands = candidates
    result = Match(None, cands[0] if cands else None)
    if bod_first and cands:
        result.bod = b = bod_cycle(cands, at)
        # besides the start-of-day file: later files (a broker ahead of it is a Fail, so
        # they're tried to say so), and older ones only if they are a start-of-day or
        # end-of-day file. An older intraday cycle matching is a coincidence, not a lag
        # (29 Oct: Dhan NFO-014 "matched" 28 Oct i4); it must not pass a row.
        cands = [b] + [c for c in cands if c != b and (c.created > b.created or c.cycle in LAGGED_CYCLES)]
    as_of = as_of or at.date()
    for c in cands:
        try:
            eng = dbs.engine(c)
            got = cash_figures(eng, positions) if cash else figures(eng, positions, as_of)
        except (NoRiskData, UnknownContract, CashError) as e:
            result.tried.append(Tried(c, None, list(want), e))
            continue
        bad = off(want, got, pct, abs_)
        miss = sum(abs(got[k] - float(want[k])) / tolerance(float(want[k]), pct, abs_) for k in bad)
        result.tried.append(Tried(c, got, bad, miss=miss))
        if not bad:
            result.hit = c
            break
    return result
