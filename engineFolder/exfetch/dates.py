"""Date tokens and the trading calendar."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Which NSE holiday list governs each segment. BSE follows the CM list;
# MCX is approximated by NSE's COM list (MCX can differ on evening sessions).
SEGMENT_HOLIDAY_KEY = {"NFO": "FO", "BFO": "FO", "CDS": "CD", "MCX": "COM", "EQ": "CM"}


def today_ist() -> date:
    return datetime.now(IST).date()


def tokens(d: date, **extra) -> dict[str, str]:
    t = {
        "YYYYMMDD": d.strftime("%Y%m%d"),
        "DDMMYYYY": d.strftime("%d%m%Y"),
        "DDMMYY": d.strftime("%d%m%y"),
        "YYYY": d.strftime("%Y"),
        "YY": d.strftime("%y"),
        "MM": d.strftime("%m"),
        "DD": d.strftime("%d"),
        "MON": d.strftime("%b").upper(),
        "Mon": d.strftime("%b"),
        "DD-Mon-YYYY": d.strftime("%d-%b-%Y"),
        "DD-MM-YYYY": d.strftime("%d-%m-%Y"),
        "DD/MM/YYYY": d.strftime("%d/%m/%Y"),
    }
    t.update({k: str(v) for k, v in extra.items() if v is not None})
    return t


_TOKEN = re.compile(r"\{([A-Za-z/\-]+)\}")


def render(template: str, d: date, **extra) -> str:
    """Fill {TOKENS}. Regex quantifiers like {8} are left alone."""
    t = tokens(d, **extra)
    return _TOKEN.sub(lambda m: t.get(m.group(1), m.group(0)), template)


class Calendar:
    """Weekends + NSE published holidays, cached on disk per year."""

    def __init__(self, cache_dir: Path, fetch_json=None):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fetch_json = fetch_json          # callable(url) -> dict, or None (offline)
        self._holidays: dict[str, set[date]] | None = None

    def _load(self) -> dict[str, set[date]]:
        if self._holidays is not None:
            return self._holidays
        year = today_ist().year
        cache = self.cache_dir / f"holidays_{year}.json"
        raw = None
        if cache.exists():
            raw = json.loads(cache.read_text())
        elif self.fetch_json:
            try:
                raw = self.fetch_json("https://www.nseindia.com/api/holiday-master?type=trading")
                cache.write_text(json.dumps(raw))
            except Exception as e:  # calendar is best-effort; never block downloads
                print(f"[calendar] could not fetch NSE holidays ({e}); using weekends only")
        out: dict[str, set[date]] = {}
        for seg, rows in (raw or {}).items():
            if not isinstance(rows, list):
                continue
            days = set()
            for r in rows:
                s = r.get("tradingDate") if isinstance(r, dict) else None
                try:
                    days.add(datetime.strptime(s, "%d-%b-%Y").date())
                except (TypeError, ValueError):
                    pass
            out[seg] = days
        self._holidays = out
        return out

    def is_trading_day(self, d: date, segment: str = "EQ") -> bool:
        if d.weekday() >= 5:
            return False
        key = SEGMENT_HOLIDAY_KEY.get(segment, "CM")
        return d not in self._load().get(key, set())

    def previous_trading_day(self, d: date, segment: str = "EQ") -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d, segment):
            d -= timedelta(days=1)
        return d

    def trading_days(self, start: date, end: date, segment: str = "EQ"):
        d = start
        while d <= end:
            if self.is_trading_day(d, segment):
                yield d
            d += timedelta(days=1)
