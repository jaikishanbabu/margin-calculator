"""BSE F&O: ICCL's SPAN file + ICCL's per-contract ELM file + BSE's bhavcopy -> one SQLite database.

ICCL's SPAN file (BSERISK<date>-NN.XML, SPAN XML 4.00, same parser as NSE's) covers BSE's own
contracts and, under interoperability, NSE's too (with ICCL's own risk numbers). Its products
use BSE codes (BSX = SENSEX). Which contracts are BSE's, and their tickers, come from ICCL's
ELM file (ICCL_EDXELM<DDMMYY>): token < 2,000,000 = a BSE contract (SENSEX, BANKEX, BSE stock
F&O), >= 2,000,000 = NSE's. That file also gives each contract's final ELM % (deep OTM,
long-dated and the expiry-day +2% included), which the engine uses as given.

Cycles: -00 (made ~01:00-02:30) = start of day, -01..-04 intraday, FINAL (~18:00) end of day.
Lots and the previous close come from the previous trading day's BFO bhavcopy (traded
contracts only: lot per symbol + expiry, else per symbol).

Contracts on a BSE expiry that ICCL's file doesn't list (a strike added since) stay listed
and use the static rule (static_rates.yaml BFO), flagged; static rules are also cross-checked
against ICCL's rates and disagreements logged (ICCL's rate is used).
"""
from __future__ import annotations

import collections
import csv
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import nfo
from . import span as spanmod
from .files import Input, MissingFile, Want, find_before

SEGMENT = "BFO"
BSE_TOKEN_MAX = 2_000_000              # ICCL tokens from here up are NSE contracts (interoperability)
MIN_CONTRACTS = 10_000
MAX_BSE_WITHOUT_ELM = 0.02             # share of BSE contracts on BSE expiries with no ICCL row
LoadCheckFailed = nfo.LoadCheckFailed


def _rank(groups):
    return (groups[0], 99 if groups[1] == "FINAL" else int(groups[1]))


WANTS = {
    "span": Want("span", "bfo.span_xml", r"^BSERISK(\d{8})-(\d{2}|FINAL)\.XML$", rank=_rank, exact=True),
    "elm": Want("exposure", "bfo.elm", r"^ICCL_EDXELM(\d{6})\.csv$"),
}
BHAVCOPY = Want("bhavcopy", "bfo.bhavcopy", r"^BhavCopy_BSE_FO_0_0_0_(\d{8})_F_0000\.CSV$")
START_OF_DAY, END_OF_DAY = "00", "FINAL"


def wants(span_cycle: str | None = None) -> dict[str, Want]:
    if not span_cycle:
        return WANTS
    out = dict(WANTS)
    out["span"] = Want("span", "bfo.span_xml", rf"^BSERISK(\d{{8}})-({re.escape(span_cycle)})\.XML$", exact=True)
    return out


def span_cycles(data_dir: Path, day: str) -> list[tuple[str, str, datetime]]:
    """[(day, cycle "00".."04"/"FINAL", created)] of data/BFO/span/<day>/, oldest first."""
    folder = Path(data_dir) / SEGMENT / "span" / day
    rx = re.compile(WANTS["span"].pattern)
    out = []
    for p in folder.glob("BSERISK*.XML") if folder.is_dir() else []:
        m = rx.search(p.name)
        if not m:
            continue
        with open(p, "rb") as f:
            c = re.search(rb"<created>(\d{12})</created>", f.read(4096))
        if c:
            out.append((day, m.group(2), datetime.strptime(c.group(1).decode(), "%Y%m%d%H%M")))
    return sorted(out, key=lambda x: x[2])


def bod_choice(data_dir: Path, today: date, trade_date: date | None = None) -> dict:
    """The start-of-day file for the trade date: its -00. The trade date is the later of
    the newest -00 on disk and the last trading day up to today. Without that -00: the
    previous trading day's FINAL, else the newest earlier -00, both marked stale (ICCL
    recomputes volatility overnight, so a FINAL is not the next -00: 28 Oct 2026, 1.8% of
    risk arrays identical)."""
    base = Path(data_dir) / SEGMENT / "span"
    days = sorted(p.name for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)) if base.is_dir() else []
    cycles = {d: {c: t for _, c, t in span_cycles(data_dir, d)} for d in days}
    sod_days = [d for d in days if START_OF_DAY in cycles[d]]
    trade = trade_date.isoformat() if trade_date else max([nfo.last_trading_day(data_dir, today, SEGMENT).isoformat()] + sod_days[-1:])

    def pick(day, cycle, stale, note):
        return {"trade_date": trade, "day": day, "cycle": cycle, "stale": stale, "note": note,
                "created": f"{cycles[day][cycle]:%Y-%m-%d %H:%M}"}
    if START_OF_DAY in cycles.get(trade, {}):
        return pick(trade, START_OF_DAY, False, "")
    prev = nfo.previous_trading_day(data_dir, trade, SEGMENT)
    if END_OF_DAY in cycles.get(prev, {}):
        return pick(prev, END_OF_DAY, True, f"{trade}'s start-of-day (-00) file isn't out; using {prev}'s FINAL "
                                            f"(ICCL recomputes volatility overnight: options can be ~0.5% off)")
    earlier = [d for d in sod_days if d < trade]
    if earlier:
        return pick(earlier[-1], START_OF_DAY, True, f"{trade}'s start-of-day (-00) file isn't out; using {earlier[-1]}'s")
    raise MissingFile(f"no start-of-day BSE SPAN file for {trade} or before in {base}")


# ---- ICCL contract names ---------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                      "NOV", "DEC"), 1)}
WEEK_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
MONTHLY = re.compile(r"^(?P<sym>.+?)(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
                     r"(?P<k>\d+(?:\.\d+)?)?(?P<kind>CE|PE|FUT)$")
WEEKLY = re.compile(r"^(?P<sym>.+?)(?P<yy>\d{2})(?P<m>[1-9OND])(?P<dd>\d{2})(?P<k>\d+(?:\.\d+)?)(?P<kind>CE|PE)$")


def parse_name(name: str) -> dict | None:
    """'SENSEX26OCT77100CE' -> {symbol SENSEX, month 2026-10, strike 77100, kind CE};
    'SENSEX26N0573000CE' -> {..., expiry 2026-11-05, ...}; 'SENSEX26OCTFUT' -> FUT."""
    m = MONTHLY.match(name)
    if m:
        return {"symbol": m["sym"], "month": f"20{m['yy']}-{MONTHS[m['mon']]:02d}", "expiry": None,
                "strike": float(m["k"]) if m["k"] else None, "kind": m["kind"]}
    m = WEEKLY.match(name)
    if m:
        mon = WEEK_MONTH[m["m"]]
        return {"symbol": m["sym"], "month": f"20{m['yy']}-{mon:02d}",
                "expiry": f"20{m['yy']}-{mon:02d}-{m['dd']}", "strike": float(m["k"]), "kind": m["kind"]}
    return None


def read_elm(path: Path) -> list[dict]:
    """ICCL_EDXELM rows: token, contract, IO|IF|SO|SF, ELM, additional, total."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) < 6 or not r[0].strip().isdigit():
                continue
            p = parse_name(r[1].strip())
            rows.append({"token": int(r[0]), "name": r[1].strip(), "type": r[2].strip(), "normal": float(r[3]),
                         "additional": float(r[4]), "total": float(r[5]), "parsed": p})
    return rows


# ---- load ------------------------------------------------------------------------------
def load(conn: sqlite3.Connection, inputs: dict[str, Input], rates_path: Path, log=print,
         data_dir: Path | None = None, bod: dict | None = None) -> dict:
    rates = (yaml.safe_load(Path(rates_path).read_text()) or {}).get(SEGMENT, {})
    info = {"segment": SEGMENT}

    def note(key, inp: Input):
        info[f"{key}_file"] = inp.path.name
        info[f"{key}_trade_date"] = inp.trade_date

    s = inputs["span"]
    sp = nfo.load_span(conn, s.path)
    note("span", s)
    created = sp.header.get("created", "")
    info.update(span_cycle=s.groups[1],
                span_created=f"{created[:4]}-{created[4:6]}-{created[6:8]} {created[8:10]}:{created[10:12]}",
                span_business_date=spanmod.iso(sp.header.get("date")),
                span_contracts=conn.execute("SELECT COUNT(*) FROM contract").fetchone()[0],
                basis="bod" if bod else "cycle",
                trade_date=bod["trade_date"] if bod else spanmod.iso(sp.header.get("date")),
                bod_stale=int(bool(bod and bod["stale"])), bod_note=(bod or {}).get("note", ""))
    log(f"  SPAN {s.path.name}: {info['span_contracts']:,} contracts, {len(sp.ccdefs)} underlyings")

    e = inputs["elm"]
    note("elm", e)
    elm = read_elm(e.path)
    info["elm_rows"] = len(elm)
    info["elm_for_trade_date"] = int(e.trade_date == info["trade_date"])
    bse = [r for r in elm if r["token"] < BSE_TOKEN_MAX and r["parsed"]]
    info["elm_unparsed"] = sum(1 for r in elm if not r["parsed"])
    _map_symbols(conn, bse, info, log)
    _apply_elm(conn, bse, info, log)

    bh = find_before(Path(data_dir), SEGMENT, BHAVCOPY, info["trade_date"]) if data_dir else None
    _lots_and_closes(conn, bh, sp, info, data_dir, log)
    if bh:
        note("bhavcopy", bh)

    _rates(conn, rates, bse, info)
    _cross_check(conn, rates, info, log)
    check(conn, info)
    info.update(loaded_at=datetime.now().isoformat(timespec="seconds"))
    conn.executemany("INSERT INTO load_info VALUES (?,?)", [(k, str(v)) for k, v in info.items()])
    return info


def _map_symbols(conn, bse: list[dict], info: dict, log):
    """SPAN codes (BSX) -> tickers (SENSEX), by the most (month, strike, CE/PE) in common
    with ICCL's BSE rows; renamed in every table."""
    span_keys = collections.defaultdict(set)
    for cc, exp, k, o in conn.execute("SELECT symbol, expiry, strike, option_type FROM contract WHERE instrument = 'OPT'"):
        span_keys[cc].add((exp[:7], k, o + "E"))
    for cc, exp in conn.execute("SELECT symbol, expiry FROM contract WHERE instrument = 'FUT'"):
        span_keys[cc].add((exp[:7], None, "FUT"))
    iccl_keys = collections.defaultdict(set)
    for r in bse:
        p = r["parsed"]
        iccl_keys[p["symbol"]].add((p["month"], p["strike"], p["kind"]))
    mapping, used = {}, set()
    pairs = sorted(((len(span_keys[cc] & keys), cc, sym) for cc in span_keys for sym, keys in iccl_keys.items()),
                   reverse=True)
    for n, cc, sym in pairs:
        if n < 3 or cc in mapping or sym in used:
            continue
        mapping[cc] = sym
        used.add(sym)
    conn.execute("CREATE TEMP TABLE cc_map (cc TEXT PRIMARY KEY, symbol TEXT)")
    conn.executemany("INSERT INTO cc_map VALUES (?,?)", mapping.items())
    tables = (("underlying", ["symbol"]), ("contract", ["symbol", "product_code"]), ("tier", ["symbol"]),
              ("spread", ["symbol"]), ("spread_leg", ["symbol", "leg_symbol"]), ("inter_spread_leg", ["symbol"]))
    # an unmapped code that equals a mapped ticker steps aside first (suffix "#span")
    clash = [c for (c,) in conn.execute("SELECT symbol FROM underlying WHERE symbol NOT IN (SELECT cc FROM cc_map) "
                                        "AND symbol IN (SELECT symbol FROM cc_map)")]
    for table, cols in tables:
        for col in cols:
            conn.executemany(f"UPDATE {table} SET {col} = {col} || '#span' WHERE {col} = ?", [(c,) for c in clash])
    if clash:
        info["bse_symbol_clashes"] = ",".join(clash)
    # two passes (via a prefix): a ticker can be another product's BSE code (renamed later)
    for table, cols in tables:
        for col in cols:
            conn.execute(f"UPDATE {table} SET {col} = '~' || (SELECT symbol FROM cc_map WHERE cc = {table}.{col}) "
                         f"WHERE {col} IN (SELECT cc FROM cc_map)")
    for table, cols in tables:
        for col in cols:
            conn.execute(f"UPDATE {table} SET {col} = substr({col}, 2) WHERE {col} LIKE '~%'")
    info["bse_underlyings"] = len(mapping)
    info["bse_symbol_map_sample"] = ", ".join(f"{a}={b}" for a, b in sorted(mapping.items())[:8])
    log(f"  BSE symbols: {len(mapping)} SPAN codes mapped to tickers ({info['bse_symbol_map_sample']}, ...)")


def _apply_elm(conn, bse: list[dict], info: dict, log):
    """ICCL's BSE rows -> contract.token / elm_pct / exchange BSE / in_master. A monthly name
    takes that month's last expiry of the symbol in SPAN."""
    month_last = {(sym, m): e for sym, m, e in conn.execute(
        "SELECT symbol, substr(expiry, 1, 7), MAX(expiry) FROM contract GROUP BY symbol, substr(expiry, 1, 7), instrument")}
    fut_month = {(sym, m): e for sym, m, e in conn.execute(
        "SELECT symbol, substr(expiry, 1, 7), MAX(expiry) FROM contract WHERE instrument = 'FUT' GROUP BY 1, 2")}
    opt_month = {(sym, m): e for sym, m, e in conn.execute(
        "SELECT symbol, substr(expiry, 1, 7), MAX(expiry) FROM contract WHERE instrument = 'OPT' GROUP BY 1, 2")}
    rows, unmatched = [], 0
    for r in bse:
        p = r["parsed"]
        fut = p["kind"] == "FUT"
        exp = p["expiry"] or (fut_month if fut else opt_month).get((p["symbol"], p["month"])) or \
            month_last.get((p["symbol"], p["month"]))
        if not exp:
            unmatched += 1
            continue
        rows.append((r["token"], r["name"], r["type"], r["total"], p["symbol"], "FUT" if fut else "OPT", exp,
                     0 if fut else p["strike"], "" if fut else p["kind"][0]))
    conn.execute("""CREATE TEMP TABLE iccl (token INTEGER, name TEXT, type TEXT, total REAL, symbol TEXT,
                    instrument TEXT, expiry TEXT, strike REAL, option_type TEXT)""")
    conn.executemany("INSERT INTO iccl VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("CREATE INDEX temp.iccl_key ON iccl (symbol, instrument, expiry, option_type, strike)")
    conn.execute("""UPDATE contract SET token = i.token, tradingsymbol = i.name, elm_pct = i.total,
                        instrument_type = i.type, exchange = 'BSE', in_master = 1
                    FROM iccl i WHERE i.symbol = contract.symbol AND i.instrument = contract.instrument
                      AND i.expiry = contract.expiry AND i.option_type = IFNULL(contract.option_type, '')
                      AND i.strike = IFNULL(contract.strike, 0)""")
    # on a BSE expiry of a BSE symbol but not in ICCL's file: BSE, listed, static rule (flagged)
    conn.execute("CREATE TEMP TABLE bse_expiry AS SELECT DISTINCT symbol, expiry FROM iccl")
    n = conn.execute("""UPDATE contract SET exchange = 'BSE', in_master = 1, lot_source = 'no_iccl_row'
                        WHERE exchange IS NULL AND EXISTS (SELECT 1 FROM bse_expiry b
                        WHERE b.symbol = contract.symbol AND b.expiry = contract.expiry)""").rowcount
    conn.execute("UPDATE contract SET exchange = 'NSE' WHERE exchange IS NULL")
    info.update(iccl_bse_rows=len(bse), iccl_rows_unplaced=unmatched,
                bse_contracts=conn.execute("SELECT COUNT(*) FROM contract WHERE exchange = 'BSE'").fetchone()[0],
                bse_with_iccl_rate=conn.execute("SELECT COUNT(*) FROM contract WHERE elm_pct IS NOT NULL").fetchone()[0],
                bse_without_iccl_rate=n)
    log(f"  ICCL ELM: {info['bse_with_iccl_rate']:,} BSE contracts with ICCL's rate, {n:,} on BSE expiries without "
        f"(static rule, flagged); {unmatched} ICCL rows not in SPAN")


def _lots_and_closes(conn, bh: Input | None, sp, info: dict, data_dir, log):
    """Lots from the previous trading day's bhavcopy (per symbol + expiry, else symbol);
    previous close = its UndrlygPric, else the start-of-day file's spot (marked)."""
    prev = nfo.previous_trading_day(data_dir, info["trade_date"], SEGMENT)
    lots, lot_sym, close = {}, collections.Counter(), {}
    if bh:
        with open(bh.path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                sym, exp = r["TckrSymb"].strip(), r["XpryDt"].strip()
                lot = nfo._inum(r.get("NewBrdLotQty"))
                if lot:
                    lots[(sym, exp)] = lot
                    lot_sym[(sym, lot)] += 1
                u = nfo._fnum(r.get("UndrlygPric"))
                if u:
                    close.setdefault(sym, u)
    by_sym = {}
    for (sym, lot), n in lot_sym.most_common():
        by_sym.setdefault(sym, lot)
    conn.executemany("UPDATE contract SET lot_size = ?, lot_source = COALESCE(lot_source, 'bhavcopy') "
                     "WHERE exchange = 'BSE' AND symbol = ? AND expiry = ?", [(l, s, e) for (s, e), l in lots.items()])
    conn.executemany("UPDATE contract SET lot_size = ?, lot_source = COALESCE(lot_source, 'bhavcopy_symbol') "
                     "WHERE exchange = 'BSE' AND symbol = ? AND lot_size IS NULL", [(l, s) for s, l in by_sym.items()])
    # untraded BSE stock F&O (most of them): NSE's lot for the same stock and month
    # (fo_mktlots.csv; SEBI sets one lot size per stock for both exchanges)
    ml = Path(data_dir) / "NFO/contract_master/latest/nfo.market_lots/fo_mktlots.csv" if data_dir else None
    if ml and ml.exists():
        rows = []
        with open(ml, newline="") as f:
            rd = csv.reader(f)
            head = [h.strip() for h in next(rd)]
            months = {}
            for i, h in enumerate(head[2:], 2):
                try:
                    months[i] = datetime.strptime(h.title(), "%b-%y").strftime("%Y-%m")
                except ValueError:
                    pass
            for r in rd:
                if len(r) > 2 and r[1].strip() and r[1].strip().upper() != "SYMBOL":
                    rows += [(nfo._inum(r[i]), r[1].strip(), ym) for i, ym in months.items()
                             if i < len(r) and nfo._inum(r[i])]
        conn.executemany("UPDATE contract SET lot_size = ?, lot_source = COALESCE(lot_source, 'nse_market_lots') "
                         "WHERE exchange = 'BSE' AND lot_size IS NULL AND symbol = ? AND substr(expiry, 1, 7) = ?", rows)
        # a BSE month NSE no longer lists (BSE's October expires on the 29th, after NSE's 27th):
        # the stock's NSE lot for its nearest listed month
        near = {}
        for lot, sym, ym in sorted(rows, key=lambda r: r[2]):
            near.setdefault(sym, lot)
        conn.executemany("UPDATE contract SET lot_size = ?, lot_source = COALESCE(lot_source, 'nse_market_lots_near') "
                         "WHERE exchange = 'BSE' AND lot_size IS NULL AND symbol = ?", [(l, s_) for s_, l in near.items()])
    info["bse_lot_sources"] = ", ".join(f"{k}: {v}" for k, v in conn.execute(
        "SELECT IFNULL(lot_source, 'none'), COUNT(*) FROM contract WHERE exchange = 'BSE' GROUP BY 1"))
    info["bse_without_lot"] = conn.execute("SELECT COUNT(*) FROM contract WHERE exchange = 'BSE' AND lot_size IS NULL"
                                           ).fetchone()[0]
    stale_bh = not bh or bh.trade_date < prev
    src = "bfo_bhavcopy" if not stale_bh else "bfo_bhavcopy (stale)"
    rows = []
    for sym, spot in conn.execute("SELECT symbol, spot_price FROM underlying"):
        if sym in close:
            rows.append((close[sym], src, bh.trade_date, int(stale_bh), sym))
        elif spot:
            kind = "span_i1" if info["span_cycle"] == START_OF_DAY else "span_spot"
            rows.append((spot, kind, info["span_business_date"], 0 if kind == "span_i1" else 1, sym))
    conn.executemany("UPDATE underlying SET prev_close = ?, prev_close_source = ?, prev_close_date = ?, "
                     "prev_close_stale = ? WHERE symbol = ?", rows)
    srcs = collections.Counter(r[1] for r in rows)
    info.update(prev_close_date=prev, prev_close_sources=", ".join(f"{k}: {v}" for k, v in srcs.items()),
                prev_close_stale=sum(r[3] for r in rows), bhavcopy_stale=int(stale_bh))
    log(f"  lots: {len(lots):,} symbol/expiry from {bh.path.name if bh else 'no bhavcopy'}; "
        f"{info['bse_without_lot']} BSE contracts without a lot; closes {info['prev_close_sources']}")


def _rates(conn, rates: dict, bse: list[dict], info: dict):
    """Underlying kind, elm_rate (the fallback for contracts without an ICCL rate) and rules."""
    kinds = {}
    for r in bse:
        kinds[r["parsed"]["symbol"]] = "INDEX" if r["type"] in ("IO", "IF") else "STOCK"
    conn.executemany("UPDATE underlying SET kind = ?, in_master = 1 WHERE symbol = ?",
                     [(k, s) for s, k in kinds.items()])
    conn.execute("UPDATE underlying SET kind = 'STOCK' WHERE kind IS NULL")
    fut = {}
    for r in bse:
        if r["type"] in ("IF", "SF"):
            fut.setdefault(r["parsed"]["symbol"], r)
    idx = rates.get("index_elm", {})
    out = []
    for sym, kind in kinds.items():
        if kind == "INDEX":
            oth, otm = idx.get("OTH", {}).get("pct", 2.0), idx.get("OTM", {}).get("pct", 3.0)
            out += [(sym, "OTH", oth, 0.0, oth, "static_rates BFO.index_elm", 1),
                    (sym, "OTM", otm, 0.0, otm, "static_rates BFO.index_elm", 1)]
        elif sym in fut:
            f = fut[sym]
            out += [(sym, "OTH", f["normal"], f["additional"], f["total"], "iccl_futures_row", None),
                    (sym, "OTM", f["normal"] * 1.5, f["additional"], f["normal"] * 1.5 + f["additional"],
                     "iccl_futures_row x1.5", None)]
    conn.executemany("INSERT INTO elm_rate VALUES (?,?,?,?,?,?,?)", out)
    for name, r in rates.get("rules", {}).items():
        conn.execute("INSERT INTO rule VALUES (?,?,?,?,?)",
                     (name, r["value"], r.get("source"), int(bool(r.get("verified"))), r.get("note")))


def _cross_check(conn, rates: dict, info: dict, log):
    """The static rule's rate for each BSE short option vs ICCL's (which is used)."""
    rules = {k: v["value"] for k, v in rates.get("rules", {}).items()}
    trade = info["trade_date"]
    long_cut = (date.fromisoformat(trade) + timedelta(days=int(rules.get("index_long_dated_months", 9)) * 30.4)).isoformat()
    und = {r[0]: r[1:] for r in conn.execute("SELECT symbol, kind, prev_close FROM underlying")}
    oth = {(s, a): t for s, a, t in conn.execute("SELECT symbol, applies_to, total_pct FROM elm_rate")}
    bad, n, sample = 0, 0, []
    for sym, exp, k, o, pct in conn.execute("""SELECT symbol, expiry, strike, option_type, elm_pct FROM contract
                                              WHERE exchange = 'BSE' AND instrument = 'OPT' AND elm_pct IS NOT NULL"""):
        kind, ref = und.get(sym, (None, None))
        if not ref or (sym, "OTH") not in oth:
            continue
        index = kind == "INDEX"
        cut = rules.get("index_deep_otm_pct" if index else "stock_deep_otm_pct", 10 if index else 30) / 100
        deep = (o == "C" and k > round(ref * (1 + cut), 6)) or (o == "P" and k < round(ref * (1 - cut), 6))
        rule = oth[(sym, "OTM" if deep else "OTH")]
        if index and exp > long_cut:
            rule = max(rule, rules.get("index_long_dated_elm_pct", 5))
        if index and exp == trade:
            rule += rules.get("expiry_day_short_index_option_extra_pct", 2)
        n += 1
        if abs(rule - pct) > 1e-6:
            bad += 1
            if len(sample) < 8:
                sample.append(f"{sym} {exp} {k:g}{o}: rule {rule:g}%, ICCL {pct:g}%")
    info.update(elm_cross_checked=n, elm_rule_disagreements=bad, elm_rule_disagreement_sample="; ".join(sample))
    log(f"  ELM cross-check: static rule vs ICCL on {n:,} BSE options: {bad:,} disagree (ICCL used)"
        + (f", e.g. {sample[:3]}" if sample else ""))


def check(conn: sqlite3.Connection, info: dict):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    problems = []
    if q("SELECT COUNT(*) FROM risk_array") < MIN_CONTRACTS:
        problems.append("too few risk arrays")
    bse = info.get("bse_contracts", 0)
    if bse < 1000:
        problems.append(f"only {bse} BSE contracts (ICCL ELM file matched?)")
    without = info.get("bse_without_iccl_rate", 0)
    if bse and without / bse > MAX_BSE_WITHOUT_ELM:
        problems.append(f"{without} of {bse} BSE contracts have no ICCL ELM row")
    if not info.get("elm_for_trade_date"):
        info["elm_note"] = f"ICCL ELM file is for {info.get('elm_trade_date')}, not the trade date {info['trade_date']}"
    if problems:
        raise LoadCheckFailed("; ".join(problems))


def freshness(info: dict) -> tuple:
    return (info.get("span_business_date") or "", info.get("span_created") or "")
