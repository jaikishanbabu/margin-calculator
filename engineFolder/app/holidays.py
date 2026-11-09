"""Exchange holiday calendars: one list per exchange, kept in the engine, edited on the admin's
Holiday calendar page and imported from the exchanges' own pages (reviewed before they apply).

Every date decision uses them: which trading date the fetcher downloads for each segment, and the
loader's "previous trading day" / start-of-day choice (the build job writes them to the loader's
calendar cache).

Kinds:
  closed            no trading, no files
  morning_closed    MCX: morning session closed, evening session open (a trading day)
  evening_closed    MCX: evening session closed (a trading day)
  special_session   trading on a day that is normally closed (Muhurat Sunday, a Saturday session)
"""
import re
from datetime import date, datetime, timedelta, timezone

EXCHANGES = {
    "NSE": {"name": "NSE - equity and F&O", "segments": ["NFO", "NSECM"]},
    "NSE_CD": {"name": "NSE - currency", "segments": ["CDS"]},
    "BSE": {"name": "BSE - equity and derivatives", "segments": ["BFO", "BSECM"]},
    "MCX": {"name": "MCX - commodities", "segments": ["MCX"]},
}
EXCHANGE_OF = {seg: ex for ex, v in EXCHANGES.items() for seg in v["segments"]}
KINDS = ("closed", "morning_closed", "evening_closed", "special_session")
KIND_NAMES = {"closed": "Closed", "morning_closed": "Morning closed, evening open",
              "evening_closed": "Evening closed", "special_session": "Special session (open)"}

# Where imports come from. NSE's own holiday API is structured and per segment; the pages the
# owner gave are kept as the human reference. BSE: the exchange's page first, the broker page second.
DEFAULT_SOURCES = {
    "NSE": {"method": "nse_api", "urls": ["https://www.nseindia.com/api/holiday-master?type=trading"],
            "reference": "https://www.niftyindices.com/resources/holiday-calendar"},
    "NSE_CD": {"method": "nse_api", "urls": ["https://www.nseindia.com/api/holiday-master?type=trading"],
               "reference": "https://www.nseindia.com/resources/exchange-communication-holidays"},
    "BSE": {"method": "html_table", "urls": ["https://www.bseindia.com/static/markets/marketinfo/listholi.aspx",
                                             "https://www.angelone.in/bse-holidays"],
            "reference": "https://www.bseindia.com/static/markets/marketinfo/listholi.aspx"},
    "MCX": {"method": "html_table", "urls": ["https://www.mcxindia.com/market-operations/trading-surveillance/trading-holidays"],
            "reference": "https://www.mcxindia.com/market-operations/trading-surveillance/trading-holidays"},
}
NSE_API_KEYS = {"NSE": ["FO", "CM"], "NSE_CD": ["CD"]}


def iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- pure calendar logic (engine + job)
class Cal:
    """entries: [{exchange, day 'YYYY-MM-DD', kind}] -> trading-day questions per exchange."""

    def __init__(self, entries):
        self.by = {}
        self.years = {}
        for e in entries:
            self.by.setdefault(e["exchange"], {})[e["day"]] = e["kind"]
            self.years.setdefault(e["exchange"], set()).add(int(e["day"][:4]))

    def has_year(self, ex, year):
        return year in self.years.get(ex, set())

    def kind(self, ex, d):
        return self.by.get(ex, {}).get(d.isoformat())

    def is_trading_day(self, ex, d):
        k = self.kind(ex, d)
        if k == "special_session":
            return True
        if d.weekday() >= 5:
            return False
        return k != "closed"

    def next_trading_day(self, ex, d, include=True, limit=15):
        d = d if include else d + timedelta(days=1)
        for _ in range(limit):
            if self.is_trading_day(ex, d):
                return d
            d += timedelta(days=1)
        return None

    def loader_cache(self, year):
        """The loader's calendar cache file (NSE holiday-master format + our extra keys)."""
        def rows(ex):
            return [{"tradingDate": datetime.strptime(day, "%Y-%m-%d").strftime("%d-%b-%Y"), "description": ""}
                    for day, k in sorted(self.by.get(ex, {}).items()) if k == "closed" and day.startswith(str(year))]

        def special(ex):
            return [datetime.strptime(day, "%Y-%m-%d").strftime("%d-%b-%Y")
                    for day, k in sorted(self.by.get(ex, {}).items()) if k == "special_session" and day.startswith(str(year))]
        def own(ex):        # an exchange without a list for the year falls back to NSE's
            return rows(ex) if self.has_year(ex, year) else rows("NSE")
        out = {"CM": rows("NSE"), "FO": rows("NSE"), "CD": own("NSE_CD"), "BSE": own("BSE"), "MCX": own("MCX"),
               "_special": {"CM": special("NSE"), "FO": special("NSE"), "CD": special("NSE_CD"),
                            "BSE": special("BSE"), "MCX": special("MCX")},
               "_source": "margin-calculator holiday calendar"}
        return out


# ---------------------------------------------------------------- import parsers (run on GitHub)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def parse_day(text, year_hint=None):
    """Dates as exchanges write them: 26-Jan-2026, 26/01/2026, January 26, 2026, 26 January 2026."""
    t = text.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
                "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y", "%A, %B %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,]+(\d{4})", t)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS.index(m.group(2)[:3].lower()) + 1, int(m.group(1)))
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", t)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2)))
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", t)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def parse_nse_api(raw, exchange):
    """NSE holiday-master JSON -> entries (closed days) for NSE or NSE_CD."""
    out = {}
    for key in NSE_API_KEYS[exchange]:
        for r in (raw or {}).get(key, []) or []:
            d = parse_day(str(r.get("tradingDate", "")))
            if d:
                out.setdefault(d.isoformat(), {"day": d.isoformat(), "kind": "closed",
                                               "description": str(r.get("description") or "").strip()[:120]})
    return sorted(out.values(), key=lambda e: e["day"])


def _cells(row_html):
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.S | re.I)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&amp;", "&").replace("&nbsp;", " ").strip()
            for c in cells]


def parse_html_table(html, exchange):
    """Any page that lists holidays as table rows with a date in one cell. MCX tables also have
    Morning / Evening session columns; those become morning_closed / evening_closed."""
    out = {}
    m_col = e_col = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", flags=re.S | re.I):
        cells = _cells(row)
        if len(cells) < 2:
            continue
        lows = [c.lower() for c in cells]
        d_idx, d = None, None
        for i, c in enumerate(cells):
            d = parse_day(c)
            if d:
                d_idx = i
                break
        if not d:                                   # a header row: remember the session columns
            m_col = next((i for i, c in enumerate(lows) if "morning" in c), m_col)
            e_col = next((i for i, c in enumerate(lows) if "evening" in c), e_col)
            continue
        rest = [c for i, c in enumerate(cells) if i != d_idx and i not in (m_col, e_col)]
        text = " | ".join(x for x in rest if x and not re.fullmatch(r"\d+|[A-Za-z]+day", x))
        low = " ".join(lows)
        kind = "closed"

        def is_open(i):
            return i is not None and i < len(lows) and ("open" in lows[i] or "trading" in lows[i]) and "clos" not in lows[i]
        if exchange == "MCX":
            if m_col is not None or e_col is not None:
                mo, eo = is_open(m_col), is_open(e_col)
            else:                                   # sessions written in the description
                mo = bool(re.search(r"morning[^|]*open", low))
                eo = bool(re.search(r"evening[^|]*open", low))
            kind = "morning_closed" if eo and not mo else "evening_closed" if mo and not eo else "closed"
            if mo and eo:
                continue                            # both sessions open: not a holiday
        if ("muhurat" in low or "special" in low) and d.weekday() >= 5:
            kind = "special_session"
        out[d.isoformat()] = {"day": d.isoformat(), "kind": kind, "description": text[:120]}
    return sorted(out.values(), key=lambda e: e["day"])


# ---------------------------------------------------------------- engine side
def _db():
    from . import db
    return db


def entries(year=None, exchange=None):
    db = _db()
    sql, args = "SELECT exchange, day, kind, description, source, updated_by, updated_at FROM calendar_days WHERE 1=1", []
    if year:
        sql += " AND day LIKE ?"
        args.append(f"{int(year)}-%")
    if exchange:
        sql += " AND exchange = ?"
        args.append(exchange)
    return db.query(sql + " ORDER BY exchange, day", tuple(args))


def calendar():
    return Cal(entries())


def sources():
    s = dict(DEFAULT_SOURCES)
    s.update(_db().get_setting("calendar_sources", {}) or {})
    return s


def proposals():
    return _db().get_setting("calendar_proposals", {}) or {}


def _check(exchange, day, kind):
    if exchange not in EXCHANGES:
        raise ValueError("Unknown exchange")
    try:
        d = datetime.strptime(str(day), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Date must be YYYY-MM-DD")
    if kind not in KINDS:
        raise ValueError("Unknown kind")
    if kind == "special_session" and d.weekday() < 5:
        pass   # a weekday can also be a special session (e.g. a session on a declared holiday)
    return d


def set_day(actor, exchange, day, kind, description="", ip=None, source="manual"):
    from . import audit
    d = _check(exchange, day, kind)
    db = _db()
    before = db.query_one("SELECT kind, description FROM calendar_days WHERE exchange = ? AND day = ?", (exchange, d.isoformat()))
    with db.tx() as c:
        if before:
            c.execute("UPDATE calendar_days SET kind = ?, description = ?, source = ?, updated_by = ?, updated_at = ? "
                      "WHERE exchange = ? AND day = ?", (kind, str(description)[:120], source, actor["username"], iso(),
                                                         exchange, d.isoformat()))
        else:
            c.insert("INSERT INTO calendar_days (exchange, day, kind, description, source, updated_by, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (exchange, d.isoformat(), kind, str(description)[:120], source,
                                                      actor["username"], iso()))
        audit.append(c, actor["username"], "calendar_day_set",
                     {"exchange": exchange, "day": d.isoformat(), "kind": kind, "description": str(description)[:120],
                      "before": dict(before) if before else None, "source": source}, ip)


def remove_day(actor, exchange, day, ip=None):
    from . import audit
    db = _db()
    before = db.query_one("SELECT kind, description FROM calendar_days WHERE exchange = ? AND day = ?", (exchange, str(day)))
    if not before:
        raise LookupError("No entry for that day")
    with db.tx() as c:
        c.execute("DELETE FROM calendar_days WHERE exchange = ? AND day = ?", (exchange, str(day)))
        audit.append(c, actor["username"], "calendar_day_removed", {"exchange": exchange, "day": str(day), "before": dict(before)}, ip)


def save_proposal(run_id, exchange, year, found, source_url, error=None):
    """From the GitHub job: what an import found. Nothing applies until a SuperAdmin approves it."""
    from . import audit
    if exchange not in EXCHANGES:
        raise ValueError("Unknown exchange")
    clean = []
    for e in found or []:
        try:
            d = _check(exchange, e.get("day"), e.get("kind", "closed"))
        except ValueError:
            continue
        if d.year == int(year):
            clean.append({"day": d.isoformat(), "kind": e.get("kind", "closed"), "description": str(e.get("description") or "")[:120]})
    db = _db()
    p = proposals()
    current = {e["day"]: e for e in entries(year, exchange)}
    new = {e["day"]: e for e in clean}
    diff = {"add": [e for d, e in new.items() if d not in current],
            "change": [dict(e, was=current[d]["kind"]) for d, e in new.items() if d in current and current[d]["kind"] != e["kind"]],
            "missing": [current[d] for d in current if d not in new and current[d]["source"] != "manual"]}
    p[f"{exchange}:{year}"] = {"exchange": exchange, "year": int(year), "found": clean, "diff": diff, "source": source_url,
                               "error": error, "fetched_at": iso(), "run_id": run_id}
    db.set_setting("calendar_proposals", p)
    audit.log("auto-fetcher", "calendar_import_fetched", {"exchange": exchange, "year": year, "found": len(clean),
                                                          "add": len(diff["add"]), "change": len(diff["change"]),
                                                          "error": error, "source": source_url})
    return p[f"{exchange}:{year}"]


def approve(actor, exchange, year, ip=None):
    """Apply an import: add and change what it found; days only the old import had are left for review."""
    from . import audit
    key = f"{exchange}:{int(year)}"
    p = proposals()
    prop = p.get(key)
    if not prop or prop.get("error") and not prop.get("found"):
        raise LookupError("Nothing to approve for that exchange and year")
    src = f"import:{prop.get('source') or ''}"[:200]
    for e in prop["diff"]["add"] + prop["diff"]["change"]:
        set_day(actor, exchange, e["day"], e["kind"], e.get("description", ""), ip, source=src)
    p.pop(key, None)
    _db().set_setting("calendar_proposals", p)
    audit.log(actor["username"], "calendar_import_approved", {"exchange": exchange, "year": int(year),
                                                              "added": len(prop["diff"]["add"]),
                                                              "changed": len(prop["diff"]["change"])}, ip)
    return {"added": len(prop["diff"]["add"]), "changed": len(prop["diff"]["change"])}


def discard(actor, exchange, year, ip=None):
    from . import audit
    p = proposals()
    if p.pop(f"{exchange}:{int(year)}", None) is None:
        raise LookupError("Nothing to discard")
    _db().set_setting("calendar_proposals", p)
    audit.log(actor["username"], "calendar_import_discarded", {"exchange": exchange, "year": int(year)}, ip)


def warnings(today=None):
    """Missing years, and NSE/BSE equity lists that disagree."""
    today = today or (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    cal = calendar()
    out = []
    years = [today.year] + ([today.year + 1] if today.month >= 11 else [])
    for ex, v in EXCHANGES.items():
        if ex == "NSE_CD":
            continue
        for y in years:
            if not cal.has_year(ex, y):
                out.append({"level": "bad" if y == today.year else "warn", "exchange": ex,
                            "text": f"{v['name']}: no {y} calendar - only weekends count as closed"})
    for y in years:
        nse = {d for d, k in cal.by.get("NSE", {}).items() if d.startswith(str(y)) and k == "closed"}
        bse = {d for d, k in cal.by.get("BSE", {}).items() if d.startswith(str(y)) and k == "closed"}
        if nse and bse and nse != bse:
            only = sorted(nse ^ bse)
            out.append({"level": "warn", "exchange": "BSE",
                        "text": f"NSE and BSE {y} holidays differ on {', '.join(only[:6])}" + (" ..." if len(only) > 6 else "")
                                + " - check both are right"})
    return out


def overview(year):
    rows = entries(year)
    return {"year": int(year), "exchanges": {ex: {"name": v["name"], "segments": v["segments"],
                                                  "days": [r for r in rows if r["exchange"] == ex],
                                                  "source": sources()[ex]} for ex, v in EXCHANGES.items()},
            "proposals": [p for k, p in proposals().items() if p["year"] == int(year)],
            "kinds": KIND_NAMES, "warnings": warnings()}


def dump():
    return [{"exchange": r["exchange"], "day": r["day"], "kind": r["kind"]} for r in entries()]
