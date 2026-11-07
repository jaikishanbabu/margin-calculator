"""The agent loop: decide what's due, fetch it, record it."""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from .dates import IST, Calendar, render, today_ist
from json import dumps as json_str          # AppleScript string literal (quoted, escaped)
from .net import Blocked, Http, NotPublished
from .resolvers import _basename, resolve
from .store import Store, looks_like_archive_ok


@dataclass
class Result:
    source: str
    variant: str
    status: str            # OK | HAVE | PENDING | BLOCKED | ERROR
    detail: str = ""


class Agent:
    def __init__(self, config_path: str | Path = "sources.yaml", data_dir: str | None = None):
        self.config_path = Path(config_path)
        self.cfg = yaml.safe_load(self.config_path.read_text())
        dflt = self.cfg.get("defaults", {})
        root = Path(data_dir or dflt.get("data_dir", "data"))
        if not root.is_absolute():
            root = self.config_path.parent / root
        self.store = Store(root)
        db_dir = Path(dflt.get("db_dir", "db"))
        self.db_dir = db_dir if db_dir.is_absolute() else self.config_path.parent / db_dir
        self.extract = dflt.get("extract", True)
        self.http = Http(self.cfg.get("sessions", {}), dflt.get("timeout", 60),
                         dflt.get("retries", 3), dflt.get("min_interval_sec", 0.6),
                         browser_headless=dflt.get("browser_headless", True))
        self.cal = Calendar(root / "_cache", lambda u: self.http.get_json(u, "nse"))
        self._failed: set[str] = set()          # segments whose last load failed: retried each run
        self._notified: dict[str, float] = {}

    # ---- what a source expands into ----------------------------------------
    @staticmethod
    def variants(src: dict) -> list[tuple[str, dict, str]]:
        """-> [(variant_name, loop_tokens, phase)]"""
        if src.get("cycles"):
            return [(f"cycle={c}", {"cycle": c}, ph) for c, ph in src["cycles"].items()]
        if src.get("batches"):
            return [(f"batch={b}", {"batch": b}, ph) for b, ph in src["batches"].items()]
        return [("-", {}, src.get("phase", "eod"))]

    def sources(self, only: list[str] | None = None) -> list[dict]:
        out = [s for s in self.cfg["sources"] if s.get("enabled", True)]
        if only:
            out = [s for s in out if any(fnmatch.fnmatch(s["id"], p) or s["segment"].lower() == p.lower()
                                         for p in only)]
        return out

    # ---- one source / variant -------------------------------------------------
    def fetch_one(self, src: dict, d: date, variant: str, loop: dict, force=False,
                  rank: int = 0) -> Result:
        """
        Three kinds of source:
          plain      one file per date/variant; skip once we have it
          versions   a listing API (json_list) that grows during the day; keep every
                     new file it lists, skip the ones already saved
          refresh    same name all day but contents change (BSE VaR, MCX margins);
                     re-download each run, save only if it changed
        With fallback_days: N (template sources named for their date), a day whose file
        isn't out yet takes the newest of the N trading days before, saying so; HAVE
        only once the day's own file is saved, never anything older than N days.
        """
        sid = src["id"]
        versions, refresh = src.get("versions", False), src.get("refresh", False)
        fallback = src.get("fallback_days", 0)
        if not (force or versions or refresh) and self.store.have(sid, d, variant) and \
                (not fallback or any(self.store.have_name(sid, d, n) for _, n in self._dated(src, d, loop))):
            return Result(sid, variant, "HAVE")
        errors, saw_block, saw_error = [], False, False
        for strategy in src["try"]:
            try:
                cands = resolve(strategy, d, loop, self.http, src.get("session"))
                saved, unchanged = [], 0
                for c in cands:
                    if versions and not force and self.store.have_name(sid, d, c.name):
                        unchanged += 1
                        continue
                    data = c.data
                    if data is None:
                        data = self.http.get(c.url, src.get("session")).content
                    if not looks_like_archive_ok(c.name, data):
                        raise NotPublished(f"corrupt/partial archive {c.url}")
                    if refresh and not force and self.store.same_as_saved(sid, d, c.name, data):
                        unchanged += 1
                        continue
                    p = self.store.save(sid, src["segment"], src["kind"], d, variant,
                                        c.url, c.name, data, src.get("extract", self.extract),
                                        rank=c.rank if versions else rank)
                    saved.append(f"{p.name} ({len(data):,} B)")
                if not saved:
                    return Result(sid, variant, "HAVE", f"{unchanged} already current")
                self.store.update_latest(sid, src["segment"], src["kind"])
                if src.get("retain_days"):        # this source only: keep the newest N days
                    gone = self.store.prune(sid, int(src["retain_days"]))
                    if gone:
                        saved.append(f"retention: removed {len(gone)} older file(s)")
                self.store.log(sid, d, variant, "OK", "; ".join(saved))
                return Result(sid, variant, "OK", "; ".join(saved))
            except NotPublished as e:
                errors.append(str(e))
            except Blocked as e:
                saw_block = True
                errors.append(str(e))
            except Exception as e:  # keep the agent alive whatever one source does
                saw_error = True
                errors.append(f"{type(e).__name__}: {e}")
        if fallback and not (saw_block or saw_error):
            r = self._fall_back(src, d, variant, loop, fallback)
            if r:
                return r
            errors.append(f"no file for {d} or the {fallback} trading days before "
                          f"(back to {self._back(src, d, fallback)[-1]}); not taking anything older")
        # PENDING = simply not on the site yet; the next scheduled run retries it
        status = "BLOCKED" if saw_block else ("ERROR" if saw_error else "PENDING")
        self.store.log(sid, d, variant, status, " | ".join(errors))
        return Result(sid, variant, status, errors[-1] if errors else "")

    @staticmethod
    def _dated(src: dict, d: date, loop: dict) -> list[tuple[str, str]]:
        """(url, file name) of each template strategy for day d."""
        out = []
        for st in src["try"]:
            if "template" in st:
                url = render(st["template"], d, **loop)
                out.append((url, _basename(url)))
        return out

    def _back(self, src: dict, d: date, n: int) -> list[date]:
        days = []
        for _ in range(n):
            d = self.cal.previous_trading_day(d, src["segment"])
            days.append(d)
        return days

    def _fall_back(self, src: dict, d: date, variant: str, loop: dict, n: int) -> Result | None:
        """d's own file isn't out: take the newest earlier one within n trading days,
        saved under d (so d's loads use it) and reported "from <date>"."""
        sid = src["id"]
        for back in self._back(src, d, n):
            for url, name in self._dated(src, back, loop):
                note = f"(from {back}; {d}'s file isn't out yet)"
                if self.store.have_name(sid, d, name):
                    return Result(sid, variant, "HAVE", f"{name} {note}")
                try:
                    data = self.http.get(url, src.get("session")).content
                except NotPublished:
                    continue
                except Exception as e:      # refused / failed: report it, don't guess further back
                    status = "BLOCKED" if isinstance(e, Blocked) else "ERROR"
                    self.store.log(sid, d, variant, status, f"fallback {back}: {e}")
                    return Result(sid, variant, status, f"fallback {back}: {e}")
                if not looks_like_archive_ok(name, data):
                    continue
                p = self.store.save(sid, src["segment"], src["kind"], d, variant, url, name, data,
                                    src.get("extract", self.extract))
                self.store.update_latest(sid, src["segment"], src["kind"])
                detail = f"{p.name} ({len(data):,} B) {note}"
                self.store.log(sid, d, variant, "OK", detail)
                return Result(sid, variant, "OK", detail)
        return None

    # ---- a whole run ------------------------------------------------------------
    def run(self, d: date | None = None, phases: list[str] | None = None,
            only: list[str] | None = None, force=False, quiet=False, load=True) -> list[Result]:
        d = d or today_ist()
        results, new_in = [], set()
        for src in self.sources(only):
            if src.get("dated", True) is False and d != today_ist():
                continue                         # "latest" files can't be back-filled
            if not self.cal.is_trading_day(d, src["segment"]):
                continue
            for idx, (variant, loop, phase) in enumerate(self.variants(src)):
                # Running a phase also retries anything from earlier phases still missing
                if phases and not _phase_due(phase, phases):
                    continue
                r = self.fetch_one(src, d, variant, loop, force, rank=idx)
                results.append(r)
                if r.status == "OK":
                    new_in.add(src["segment"])
                if not quiet and r.status != "HAVE":
                    print(f"  {r.status:8} {r.source:28} {r.variant:10} {r.detail}")
        self.http.close()        # don't leave Chrome running between scheduled runs
        if load:
            self.reload(new_in, retry=set(self._failed))
        return results

    # ---- database ---------------------------------------------------------------
    def load(self, segment: str, d: date | None = None, cycle: str | None = None, force=False,
             db_dir: str | None = None) -> dict:
        """NFO from latest/: two databases, the start-of-day one client margins use
        (db/NFO.sqlite, basis bod) and the newest cycle (db/NFO.latest.sqlite). Returns
        the BOD load's info. With a date or cycle: that one file into db/NFO.sqlite."""
        from loader import load_segment
        where = db_dir or self.db_dir
        if segment in ("NFO", "BFO") and not d and not cycle:
            latest = load_segment(segment, self.store.root, where, basis="latest", force=force)
            info = load_segment(segment, self.store.root, where, basis="bod", force=force)
            info["latest"] = latest
            return info
        return load_segment(segment, self.store.root, where, d, span_cycle=cycle, force=force)

    # Files a segment's load needs, by day (relative to the trade date). Before each
    # reload any that are missing are fetched (29 Oct 2026: the 28 Oct master was never
    # fetched while the Mac slept, and every NFO load failed until it was fetched by hand).
    LOAD_INPUTS = {"NFO": [("nfo.contract_master", "prev"), ("nfo.elm", "trade"),
                           ("nfo.bhavcopy", "prev"), ("nfo.settlement_prices", "prev")],
                   "NSE": [("nse.bhavcopy", "prev"), ("nse.security", "prev")],
                   "BFO": [("bfo.elm", "trade"), ("bfo.bhavcopy", "prev")]}
    MARKET_HOURS = ((9, 15), (15, 30))     # NFO / NSE cash: a failed load then is notified
    NOTIFY_EVERY_SEC = 3600

    def _trade_date(self, seg: str) -> date:
        today = today_ist()
        if seg in ("NFO", "BFO"):
            from loader import SEGMENTS
            bod_choice = SEGMENTS[seg].bod_choice
            try:
                return date.fromisoformat(bod_choice(self.store.root, today)["trade_date"])
            except Exception:
                pass
        d = today
        while not self.cal.is_trading_day(d, "NFO"):
            d -= timedelta(days=1)
        return d

    def ensure_inputs(self, seg: str) -> list[str]:
        """Fetch any of the segment's load inputs (LOAD_INPUTS) not on disk yet; -> what
        was fetched. A missing source that still isn't published is just tried again
        at the next reload."""
        trade = self._trade_date(seg)
        prev = self.cal.previous_trading_day(trade)
        known = {s["id"] for s in self.sources()}
        got = []
        for sid, which in self.LOAD_INPUTS.get(seg, []):
            d = prev if which == "prev" else trade
            if sid not in known or self.store.have(sid, d, "-"):
                continue
            print(f"  [load {seg}] {sid} for {d} is missing: fetching it")
            for r in self.run(d, ["eod"], [sid], load=False):
                if r.status == "OK":
                    got.append(f"{sid} {d}")
        return got

    def _serving(self, seg: str) -> str:
        from loader import db as dbmod
        path = Path(self.db_dir) / f"{seg}.sqlite"
        if not path.exists():
            return "nothing (no database yet)"
        i = dbmod.info(path)
        if seg in ("NFO", "BFO"):
            return f"{i.get('trade_date')} {i.get('span_cycle')}"
        return f"VaR {i.get('var_date')} batch {i.get('var_batch')}"

    def notify(self, title: str, message: str):
        """A macOS notification (the agent runs in the user's session). Never raises."""
        import subprocess
        try:
            subprocess.run(["osascript", "-e", f"display notification {json_str(message)} with title {json_str(title)}"],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    def _report_failure(self, seg: str, e: Exception):
        why = f"{type(e).__name__}: {e}"
        line = f"ERROR load {seg} {self._trade_date(seg)}: {why}; still serving {self._serving(seg)}"
        print(f"  LOAD-FAILED {seg}: {why}")
        print(f"[{datetime.now(IST):%H:%M}] {line}")
        self.store.log(f"load.{seg}", today_ist(), "-", "ERROR", why)
        now = datetime.now(IST)
        (h1, m1), (h2, m2) = self.MARKET_HOURS
        open_ = self.cal.is_trading_day(now.date(), "NFO") and (h1, m1) <= (now.hour, now.minute) <= (h2, m2)
        last = self._notified.get(seg, 0.0)
        if open_ and time.time() - last >= self.NOTIFY_EVERY_SEC:
            self._notified[seg] = time.time()
            self.notify(f"Margin data: {seg} load failed", f"{why[:150]}. Still serving {self._serving(seg)}.")

    def reload(self, segments, retry: set | None = None) -> None:
        """Rebuild the database of each segment that got a new file (and of each in
        `retry`: its last load failed). Once per run, not per file. Missing inputs are
        fetched first; a failure caused by one that then arrives is retried once."""
        from loader import SEGMENTS, data_segment
        todo = {s for s in SEGMENTS if data_segment(s) in set(segments)} | set(retry or ())
        for seg in sorted(todo):
            self.ensure_inputs(seg)
            try:
                self.load(seg)
            except Exception as e:  # a bad load keeps the old database; never stop the agent
                fetched = self.ensure_inputs(seg)
                if fetched:
                    print(f"  [load {seg}] failed ({type(e).__name__}); fetched {', '.join(fetched)}; retrying once")
                    try:
                        self.load(seg)
                    except Exception as e2:
                        self._report_failure(seg, e2)
                        self._failed.add(seg)
                        continue
                else:
                    self._report_failure(seg, e)
                    self._failed.add(seg)
                    continue
            if seg in self._failed:
                print(f"  [load {seg}] loaded again after an earlier failure")
            self._failed.discard(seg)

    def backfill(self, start: date, end: date, only=None):
        d = start
        while d <= end:
            if d.weekday() < 5:
                print(f"== {d}")
                self.run(d, None, only, load=False)   # old dates don't change latest/
            d += timedelta(days=1)

    # ---- probe: try every strategy separately, save nothing -------------------
    def probe(self, d: date, only=None):
        rows = []
        for src in self.sources(only):
            for variant, loop, _ in self.variants(src):
                for i, strategy in enumerate(src["try"]):
                    kind = next(iter(k for k in strategy if k != "save_as"))
                    try:
                        cands = resolve(strategy, d, loop, self.http, src.get("session"))
                        c = cands[-1]                       # newest version
                        if len(cands) > 1:
                            kind += f" ({len(cands)} files)"
                        size = len(c.data) if c.data is not None else \
                            len(self.http.get(c.url, src.get("session")).content)
                        rows.append((src["id"], variant, f"{i+1}:{kind}", "OK", f"{size:,} B  {c.url}"))
                    except Exception as e:
                        rows.append((src["id"], variant, f"{i+1}:{kind}", type(e).__name__, str(e)[:140]))
                    print("  %-28s %-10s %-15s %-12s %s" % rows[-1])
        self.http.close()
        return rows

    # ---- scheduler -------------------------------------------------------------
    def slots(self, day: date) -> list[tuple[datetime, str]]:
        s = self.cfg.get("schedule", {})
        out = []
        for ph in ("bod", "eod"):
            for hm in s.get(ph, []):
                h, m = map(int, hm.split(":"))
                out.append((datetime(day.year, day.month, day.day, h, m, tzinfo=IST), ph))
        for hm in (s.get("early") or {}).get("at", []):     # a few sources before dawn
            h, m = map(int, hm.split(":"))
            out.append((datetime(day.year, day.month, day.day, h, m, tzinfo=IST), "early"))
        iv = s.get("intraday")
        if iv:
            h, m = map(int, iv["from"].split(":"))
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
            h2, m2 = map(int, iv["to"].split(":"))
            end = datetime(day.year, day.month, day.day, h2, m2, tzinfo=IST)
            while t <= end:
                out.append((t, "intraday"))
                t += timedelta(minutes=iv.get("every_min", 30))
        return sorted(out)

    # The next trading day's start-of-day SPAN file (i1) comes out ~21:30 the evening
    # before; client margins switch to it then. From this time, each run also asks for it.
    NEXT_DAY_FROM = (21, 0)
    NEXT_DAY_SOURCES = ["nfo.span"]          # only its i1 has phase bod
    LATE_SECONDS = 300                       # a slot run this late: the Mac was asleep, catch up

    def serve(self):
        """Run the schedule. Sleeps in short steps against the wall clock, so after the
        Mac sleeps the missed runs happen on wake (time.sleep doesn't count sleep).
        On start, and after waking late, catch up: earlier days' missing files and
        every phase of today."""
        print("Agent running. Ctrl+C to stop. Times are IST.")
        self.wake_up(datetime.now(IST), "start")
        while True:
            now = datetime.now(IST)
            upcoming = [x for x in self.slots(now.date()) if x[0] > now] or \
                       [x for x in self.slots(now.date() + timedelta(days=1))]
            when, phase = upcoming[0]
            print(f"[{now:%H:%M}] next run: {phase} at {when:%a %H:%M}")
            while True:
                left = (when - datetime.now(IST)).total_seconds()
                if left <= 0:
                    break
                time.sleep(min(60.0, left))
            now = datetime.now(IST)
            if (now - when).total_seconds() > self.LATE_SECONDS:
                self.wake_up(now, f"woke {(now - when).total_seconds() / 60:.0f} min after the {when:%H:%M} run")
                continue
            self.slot(when.date(), phase, now)

    def slot(self, day: date, phase: str, now: datetime):
        """One scheduled run, plus the next day's i1 in the evening."""
        if self.cal.is_trading_day(day, "EQ"):
            print(f"[{now:%H:%M}] running {phase} for {day}")
            if phase == "early":
                self.run(day, ["bod"], self.early_sources())
                return
            if phase == "bod":
                self.catch_up(day)
            self.run(day, [phase])
        self.next_day_i1(now)

    def early_sources(self) -> list[str]:
        """schedule.early.only: sources fetched before dawn (BSE's start-of-day SPAN is
        made ~01:00-02:30), so they're on disk before 09:15 without waking NSE/MCX."""
        return list((self.cfg.get("schedule", {}).get("early") or {}).get("only", []))

    def wake_up(self, now: datetime, why: str):
        """Catch up after a start or a sleep: earlier days' missing files, every phase
        of today that is due, and (evening) the next day's i1."""
        today = now.date()
        print(f"[{now:%H:%M}] catching up ({why})")
        if self.cal.is_trading_day(today, "EQ"):
            self.catch_up(today)
            due = {ph for t, ph in self.slots(today) if t <= now}
            main = sorted(due - {"early"})
            if main:                      # any later phase also retries the early sources
                self.run(today, main)
            elif "early" in due:
                self.run(today, ["bod"], self.early_sources())
        self.next_day_i1(now)

    def next_day_i1(self, now: datetime):
        if (now.hour, now.minute) >= self.NEXT_DAY_FROM:
            nxt = now.date() + timedelta(days=1)
            while not self.cal.is_trading_day(nxt, "NFO"):
                nxt += timedelta(days=1)
            self.run(nxt, ["bod"], self.NEXT_DAY_SOURCES)

    def catch_up(self, day: date):
        """At BOD: retry files still missing from earlier trading days. Every source
        gets the previous trading day; one with catch_up_days: N gets N days back
        (for files an exchange publishes late, or an agent that was stopped)."""
        d = self.cal.previous_trading_day(day)
        self.run(d, ["eod"])
        deepest = max((s.get("catch_up_days", 1) for s in self.sources()), default=1)
        for k in range(2, deepest + 1):
            d = self.cal.previous_trading_day(d)
            ids = [s["id"] for s in self.sources() if s.get("catch_up_days", 1) >= k]
            self.run(d, ["eod"], ids)


_ORDER = {"bod": 0, "intraday": 1, "eod": 2}


def _phase_due(item_phase: str, requested: list[str]) -> bool:
    top = max(_ORDER.get(p, 2) for p in requested)
    return _ORDER.get(item_phase, 2) <= top
