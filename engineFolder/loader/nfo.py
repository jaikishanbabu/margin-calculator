"""NSE F&O: SPAN + contract master + ELM + prices + ban list -> one SQLite database.

Contracts come from two sources that don't fully overlap:
  SPAN only    BSE contracts NSE Clearing also clears (SENSEX, BANKEX, ...) and
               far strikes the master doesn't list
  master only  strikes listed after the SPAN file was made (e.g. Monday's new
               strikes against Friday's SPAN); they have no risk array and the
               engine must report "no risk data", not estimate
Both are kept, flagged in_span / in_master.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import span as spanmod
from .files import Input, MissingFile, Want, _hits, find_before

SEGMENT = "NFO"
NSE_EPOCH = datetime(1980, 1, 1)       # contract master dates are seconds since this
BATCH = 5000
# Sanity limits: a load that fails them is rejected and the old database stays.
MIN_CONTRACTS = 10_000
MIN_UNDERLYINGS = 100
MAX_MASTER_WITHOUT_SPAN = 0.005        # share of master contracts already listed on the SPAN date
MAX_MASTER_BAD_EXPIRY = 0.005          # share of live (DelFlg N) master rows with no usable expiry


def _span_rank(groups):
    cycle = groups[1]
    return (groups[0], 99 if cycle == "s" else int(cycle[1:]))


WANTS = {
    "span": Want("span", "nfo.span", r"^nsccl\.(\d{8})\.(s|i\d+)\.spn$", rank=_span_rank, exact=True),
    "contract_master": Want("contract_master", "nfo.contract_master", r"^NSE_FO_contract_(\d{8})\.csv$"),
    "elm": Want("exposure", "nfo.elm", r"^ael_(\d{8})\.csv$"),
    "bhavcopy": Want("bhavcopy", "nfo.bhavcopy", r"^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv$", required=False),
    "settlement": Want("bhavcopy", "nfo.settlement_prices", r"^FOSett_prce_(\d{8})\.csv$", required=False),
    "ban_list": Want("ban_list", "nfo.ban_list", r"^fo_secban_(\d{8})\.csv$", required=False),
    # current lot size per symbol and expiry month; cross-checks a master from an earlier day
    "market_lots": Want("contract_master", "nfo.market_lots", r"^fo_mktlots\.csv$", required=False),
}



def wants(span_cycle: str | None = None) -> dict[str, Want]:
    """WANTS, optionally pinned to one SPAN cycle ('i1'..'i5', 's') for date-mode loads."""
    if not span_cycle:
        return WANTS
    c = "s" if span_cycle == "s" else f"i0?{int(span_cycle[1:])}"
    out = dict(WANTS)
    out["span"] = Want("span", "nfo.span", rf"^nsccl\.(\d{{8}})\.({c})\.spn$", exact=True)
    return out


def span_cycles(data_dir: Path, day: str) -> list[tuple[str, str, datetime]]:
    """[(day, cycle, created)] of the SPAN files in data/NFO/span/<day>/, oldest first,
    from each file's <created> header."""
    folder = Path(data_dir) / SEGMENT / "span" / day
    rx = re.compile(WANTS["span"].pattern)
    out = []
    for p in folder.glob("nsccl.*.spn") if folder.is_dir() else []:
        m = rx.search(p.name)
        if not m:
            continue
        with open(p, "rb") as f:
            c = re.search(rb"<created>(\d{12})</created>", f.read(4096))
        if c:
            out.append((day, cycle_name(m.group(2)), datetime.strptime(c.group(1).decode(), "%Y%m%d%H%M")))
    return sorted(out, key=lambda x: x[2])


def cycle_live_at(data_dir: Path, at: datetime) -> tuple[str, str] | None:
    """(cycle, created) of the newest SPAN cycle for at's date that was created by `at`."""
    live = [c for c in span_cycles(data_dir, at.date().isoformat()) if c[2] <= at]
    return (live[-1][1], f"{live[-1][2]:%Y-%m-%d %H:%M}") if live else None


def cycle_before(data_dir: Path, at: datetime) -> tuple[str, str, str] | None:
    """(day, cycle, created) of the cycle before the one live at `at`: an earlier cycle
    the same day, else the last cycle of the previous day on disk."""
    day = at.date().isoformat()
    live = [c for c in span_cycles(data_dir, day) if c[2] <= at]
    if len(live) >= 2:
        d, c, t = live[-2]
        return d, c, f"{t:%Y-%m-%d %H:%M}"
    base = Path(data_dir) / SEGMENT / "span"
    for prev_day in sorted((p.name for p in base.iterdir() if p.name < day and p.name[:2] == "20"), reverse=True):
        cycles = span_cycles(data_dir, prev_day)
        if cycles:
            d, c, t = cycles[-1]
            return d, c, f"{t:%Y-%m-%d %H:%M}"
    return None


RISK_COLS = ", ".join(f"s{i}" for i in range(1, 17))
# NSE's dummy underlyings (011NSETEST ... 181NSETEST): in SPAN, master and settlement
# files, never traded, no ELM rate. Dropped at load.
TEST_SYMBOL = re.compile(r"^\d+NSETEST$")


class LoadCheckFailed(RuntimeError):
    pass


def _nse_date(secs: str) -> str | None:
    """Seconds since 1980-01-01 -> YYYY-MM-DD; None for blank, 0 or not a number."""
    try:
        n = int((secs or "").strip())
    except ValueError:
        return None
    if n <= 0:
        return None
    return (NSE_EPOCH + timedelta(seconds=n)).date().isoformat()


def _dmy(s: str) -> str:
    return datetime.strptime(s.strip(), "%d-%b-%Y").date().isoformat()


def _fnum(s: str):
    s = (s or "").strip()
    return float(s) if s else None


def _inum(s: str):
    s = (s or "").strip()
    return int(float(s)) if s else None


def cycle_name(raw: str) -> str:
    """'i01' / 'i1' -> 'i1'; 's' stays 's'."""
    return "s" if raw == "s" else f"i{int(raw[1:])}"


# ---- SPAN -----------------------------------------------------------------------
def load_span(conn: sqlite3.Connection, path: Path) -> spanmod.Span:
    contracts, risks = [], []
    next_id = [1]

    def flush():
        conn.executemany("""INSERT INTO contract(id, symbol, product_code, instrument, expiry,
                            strike, option_type, multiplier, span_price, delta, vol, time_to_expiry,
                            price_scan, vol_scan, span_cid, und_pf_id, und_cid, in_span)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                         [c[:3] + c[4:] for c in contracts])
        conn.executemany("INSERT INTO contract_pf VALUES (?,?)", [(c[0], c[3]) for c in contracts])
        conn.executemany(f"INSERT INTO risk_array(contract_id, {RISK_COLS}, composite_delta) "
                         f"VALUES ({','.join('?' * 18)})", risks)
        contracts.clear()
        risks.clear()

    def on_contract(c):
        if TEST_SYMBOL.match(c["product_code"]):
            return
        cid = next_id[0]
        next_id[0] += 1
        contracts.append((cid, c["product_code"], c["product_code"], c["pf_id"], c["instrument"], c["expiry"],
                          c["strike"], c["option_type"], c["multiplier"], c["span_price"], c["delta"],
                          c["vol"], c["time_to_expiry"], c["price_scan"], c["vol_scan"],
                          c.get("span_cid"), c.get("und_pf_id"), c.get("und_cid")))
        risks.append((cid, *c["risk"], c["composite_delta"]))
        if len(contracts) >= BATCH:
            flush()

    # contract -> SPAN product, until we know which combined commodity each product is in
    conn.execute("CREATE TEMP TABLE contract_pf (id INTEGER PRIMARY KEY, pf_id INTEGER)")
    with open(path, "rb") as f:
        sp = spanmod.parse(f, on_contract)
    flush()

    conn.executemany("INSERT INTO scenario VALUES (:kind, :point, :price_mult, :vol_mult, :weight, :paired_point)",
                     sp.points)

    pf_cc = []
    for cc in sp.ccdefs:
        if TEST_SYMBOL.match(cc["cc"]):
            continue
        phy = next((sp.products.get(l["pf_id"]) for l in cc["links"] if l["pf_type"] == "PHY"), None) or {}
        conn.execute("""INSERT INTO underlying(symbol, name, currency, spot_price, price_scan, vol_scan,
                        som_rate, som_method) VALUES (?,?,?,?,?,?,?,?)""",
                     (cc["cc"], cc["name"], cc["currency"], phy.get("spot_price"), phy.get("price_scan"),
                      phy.get("vol_scan"), cc["som_rate"], cc["som_method"]))
        pf_cc += [(l["pf_id"], cc["cc"]) for l in cc["links"]]
        for t in cc["tiers"]:
            conn.execute("INSERT INTO tier VALUES (?,?,?,?,?,?)",
                         (cc["cc"], t["tier_type"], t["tier_no"], t["start_expiry"], t["end_expiry"], t["rate"]))
        for s in cc["spreads"]:
            conn.execute("INSERT INTO spread VALUES (?,?,?,?)",
                         (cc["cc"], s["spread_no"], s["charge_method"], s["rate"]))
            conn.executemany("INSERT INTO spread_leg VALUES (?,?,?,?,?,?,?,?)",
                             [(cc["cc"], s["spread_no"], i + 1, lg["cc"], lg["expiry"], lg["side"], lg["ratio"],
                               lg.get("tier")) for i, lg in enumerate(s["legs"])])
    for s in sp.inter_spreads:
        conn.execute("INSERT INTO inter_spread VALUES (?,?,?)", (s["spread_no"], s["method"], s["credit_rate"]))
        conn.executemany("INSERT INTO inter_spread_leg VALUES (?,?,?,?,?,?,?)",
                         [(s["spread_no"], i + 1, lg["cc"], lg["side"], lg["ratio"], lg["tier"], lg["expiry"])
                          for i, lg in enumerate(s["legs"])])

    # contract.symbol = the combined commodity its product belongs to
    conn.execute("CREATE TEMP TABLE pf_cc (pf_id INTEGER PRIMARY KEY, cc TEXT)")
    conn.executemany("INSERT OR REPLACE INTO pf_cc VALUES (?,?)", pf_cc)
    conn.execute("""UPDATE contract SET symbol = pf_cc.cc FROM contract_pf JOIN pf_cc USING (pf_id)
                    WHERE contract_pf.id = contract.id AND contract.symbol <> pf_cc.cc""")
    orphans = conn.execute("SELECT COUNT(*) FROM contract_pf WHERE pf_id NOT IN (SELECT pf_id FROM pf_cc)"
                           ).fetchone()[0]
    if orphans:
        raise LoadCheckFailed(f"{orphans} SPAN contracts belong to no combined commodity")
    return sp


# ---- contract master -------------------------------------------------------------
def pair_master(data_dir: Path, found: Input, day: str, info: dict, log=print) -> Input:
    """The master for trading on `day`: the newest one dated before it. NSE makes
    NSE_FO_contract_D on D's evening for D+1 (27 Oct's lists 28 Oct's new stocks and
    has 27 Oct's expiries deleted), so a master dated `day` or later is for a later
    day. Only if no earlier master is on disk is a same-day one used (logged)."""
    if found.trade_date and found.trade_date < day:
        info["master_pairing"] = "previous"
        return found
    earlier = find_before(Path(data_dir), SEGMENT, WANTS["contract_master"], day) if data_dir else None
    if earlier:
        info["master_pairing"] = "previous"
        log(f"  contract master: {found.path.name} is for the day after {found.trade_date}; "
            f"using {earlier.path.name} for {day}")
        return Input("contract_master", earlier.path, earlier.trade_date, earlier.groups)
    info["master_pairing"] = "same_day"
    log(f"  contract master: no master dated before {day} on disk; using {found.path.name} "
        f"(made for the next day: {day}'s expiring contracts may be missing)")
    return found


def load_master(conn: sqlite3.Connection, path: Path, info: dict | None = None, log=print) -> int:
    """Rows marked deleted (DelFlg Y) are skipped: from 27 Oct 2026 NSE keeps the
    day's expired contracts in the next master as blanked rows (no symbol, XpryDt 0,
    DelFlg Y). Any other row without a usable expiry is skipped and counted; more
    than MAX_MASTER_BAD_EXPIRY of them fails the load."""
    conn.execute("""CREATE TEMP TABLE master (token INTEGER, symbol TEXT, instrument TEXT, expiry TEXT,
                    strike REAL, option_type TEXT, tradingsymbol TEXT, instrument_type TEXT,
                    lot_size INTEGER, settlement TEXT, listed_from TEXT, permitted INTEGER,
                    underlying_token INTEGER)""")
    rows, deleted, bad = [], 0, []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("DelFlg") or "N").strip() != "N":
                deleted += 1
                continue
            if TEST_SYMBOL.match(r["TckrSymb"]):
                continue
            expiry = _nse_date(r["XpryDt"])
            if not expiry or not r["TckrSymb"].strip():
                bad.append(f"{r['FinInstrmId']} {r['TckrSymb'] or '?'} XpryDt={r['XpryDt']!r}")
                continue
            opt = r["OptnTp"] in ("CE", "PE")
            rows.append((int(r["FinInstrmId"]), r["TckrSymb"],
                         "FUT" if r["FinInstrmNm"].startswith("FUT") else "OPT",
                         expiry,
                         int(r["StrkPric"]) / 100 if opt else None,    # paise -> rupees
                         r["OptnTp"][0] if opt else None,
                         r["StockNm"], r["FinInstrmNm"], _inum(r["NewBrdLotQty"]) or _inum(r["MinLot"]),
                         r["SttlmMtd"], _nse_date(r["StartDt"]), _inum(r["PrtdToTrad"]),
                         _inum(r["UndrlygFinInstrmId"])))
    if deleted:
        log(f"  contract master: skipped {deleted:,} deleted rows (DelFlg Y)")
    if bad:
        share = len(bad) / (len(rows) + len(bad))
        msg = f"{len(bad):,} of {len(rows) + len(bad):,} live master rows ({share:.2%}) have no usable expiry or symbol"
        if share > MAX_MASTER_BAD_EXPIRY:
            raise LoadCheckFailed(f"contract master: {msg}, more than {MAX_MASTER_BAD_EXPIRY:.1%}; e.g. {bad[:3]}")
        log(f"  contract master: skipped {msg}; e.g. {bad[:3]}")
    if info is not None:
        info.update(master_deleted_rows=deleted, master_bad_expiry_rows=len(bad))
    conn.executemany("INSERT INTO master VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # A master already lists the next day's new F&O stocks (27 Oct 2026: COROMANDEL,
    # CUB, ... from 28 Oct), which an earlier day's SPAN lacks: not yet listed, skipped.
    span_date = (info or {}).get("span_business_date")
    later = [s for (s,) in conn.execute("""SELECT symbol FROM master
                                           WHERE symbol NOT IN (SELECT symbol FROM underlying)
                                           GROUP BY symbol HAVING MIN(listed_from) > ?""",
                                        (span_date or "9999",))]
    if later:
        conn.execute(f"DELETE FROM master WHERE symbol IN ({','.join('?' * len(later))})", later)
        log(f"  contract master: skipped {len(later)} underlyings listed after the SPAN date {span_date}: {later}")
    if info is not None:
        info["master_later_underlyings"] = ",".join(later)
    conn.execute("CREATE INDEX temp.master_key ON master (symbol, instrument, expiry, IFNULL(option_type,''), IFNULL(strike,0))")
    conn.execute("""UPDATE contract SET token = m.token, tradingsymbol = m.tradingsymbol,
                        instrument_type = m.instrument_type, lot_size = m.lot_size,
                        settlement = m.settlement, listed_from = m.listed_from,
                        permitted_to_trade = m.permitted, in_master = 1, lot_source = 'master',
                        exchange = 'NSE'
                    FROM master m
                    WHERE m.symbol = contract.symbol AND m.instrument = contract.instrument
                      AND m.expiry = contract.expiry
                      AND IFNULL(m.option_type,'') = IFNULL(contract.option_type,'')
                      AND IFNULL(m.strike,0) = IFNULL(contract.strike,0)""")
    conn.execute("""INSERT INTO contract(symbol, product_code, instrument, expiry, strike, option_type,
                        token, tradingsymbol, instrument_type, lot_size, settlement, listed_from,
                        permitted_to_trade, in_master, lot_source, exchange)
                    SELECT symbol, symbol, instrument, expiry, strike, option_type, token, tradingsymbol,
                        instrument_type, lot_size, settlement, listed_from, permitted, 1, 'master', 'NSE'
                    FROM master WHERE token NOT IN (SELECT token FROM contract WHERE token IS NOT NULL)""")
    conn.execute("""UPDATE underlying SET in_master = 1,
                        kind = (SELECT CASE WHEN MAX(instrument_type) LIKE '%IDX' THEN 'INDEX' ELSE 'STOCK' END
                                FROM master WHERE master.symbol = underlying.symbol),
                        underlying_token = (SELECT MAX(underlying_token) FROM master
                                            WHERE master.symbol = underlying.symbol)
                    WHERE symbol IN (SELECT symbol FROM master)""")
    # SPAN-only contracts: NSE if on one of NSE's expiry dates for the symbol, else BSE
    # (NSE Clearing also clears BSE F&O, in the same SPAN products, e.g. RELIANCE 29 Oct)
    conn.execute("CREATE TEMP TABLE nse_expiry AS SELECT DISTINCT symbol, expiry FROM master")
    conn.execute("CREATE UNIQUE INDEX temp.nse_expiry_key ON nse_expiry (symbol, expiry)")
    conn.execute("""UPDATE contract SET exchange = CASE WHEN EXISTS (SELECT 1 FROM nse_expiry x
                        WHERE x.symbol = contract.symbol AND x.expiry = contract.expiry) THEN 'NSE' ELSE 'BSE' END
                    WHERE exchange IS NULL""")
    missing =conn.execute("SELECT DISTINCT symbol FROM master WHERE symbol NOT IN (SELECT symbol FROM underlying)"
                           ).fetchall()
    if missing:
        raise LoadCheckFailed(f"contract master underlyings missing from SPAN: {[m[0] for m in missing][:10]}")
    return len(rows)


def load_market_lots(conn: sqlite3.Connection, path: Path, master_date: str, lots_date: str,
                     day: str, info: dict, log=print, prev_day: str | None = None):
    """Cross-check lot sizes against fo_mktlots.csv (UNDERLYING, SYMBOL, OCT-26, NOV-26, ...).
    Mismatches are always logged. The master for `day` is normally the previous trading
    day's (pair_master); it is stale only when older than that (`prev_day`). Then, if
    fo_mktlots is newer, fo_mktlots wins. SPAN contracts on the master's (NSE) expiry
    dates that the master lacks (strikes added since it was made) get fo_mktlots' lot
    size whenever fo_mktlots is at least as new as the master; they stay in_master = 0.
    SPAN itself carries no lot sizes (risk arrays are per unit), and its other fields
    (expiry, strike, price, multiplier) are already what the database uses."""
    rows = []
    with open(path, newline="") as f:
        rd = csv.reader(f)
        head = [h.strip() for h in next(rd)]
        months = {}
        for i, h in enumerate(head[2:], 2):
            try:
                months[i] = datetime.strptime(h.title(), "%b-%y").strftime("%Y-%m")
            except ValueError:
                pass
        for r in rd:
            if len(r) < 3 or not r[1].strip() or r[1].strip().upper() == "SYMBOL":
                continue
            for i, ym in months.items():
                v = _inum(r[i]) if i < len(r) else None
                if v:
                    rows.append((r[1].strip(), ym, v))
    if not rows:
        raise LoadCheckFailed(f"no lot sizes read from {path.name}")
    conn.execute("CREATE TEMP TABLE mlot (symbol TEXT, ym TEXT, lot INTEGER, PRIMARY KEY (symbol, ym))")
    conn.executemany("INSERT OR REPLACE INTO mlot VALUES (?,?,?)", rows)

    diff = conn.execute("""SELECT DISTINCT c.symbol, c.expiry, c.lot_size, m.lot FROM contract c
                           JOIN mlot m ON m.symbol = c.symbol AND m.ym = substr(c.expiry, 1, 7)
                           WHERE c.in_master AND c.lot_size <> m.lot ORDER BY 1, 2""").fetchall()
    info["lot_size_mismatches"] = len(diff)
    if diff:
        info["lot_size_mismatch_sample"] = "; ".join(f"{s} {e}: master {a}, market_lots {b}" for s, e, a, b in diff[:10])
        log(f"  lot size: {len(diff)} symbol/expiry pairs differ between master and fo_mktlots.csv")
    stale = master_date < (prev_day or day)
    info["master_stale"] = int(stale)
    if lots_date < master_date:
        return
    if stale and master_date < lots_date:
        log(f"  contract master is from {master_date}, older than {prev_day or day} (the trading day before "
            f"{day}): lot sizes from fo_mktlots.csv ({lots_date})")
        conn.execute("""UPDATE contract SET lot_size = m.lot, lot_source = 'market_lots' FROM mlot m
                        WHERE m.symbol = contract.symbol AND m.ym = substr(contract.expiry, 1, 7)
                          AND contract.in_master AND contract.lot_size <> m.lot""")
    n = conn.execute("""UPDATE contract SET lot_size = m.lot, lot_source = 'market_lots'
                        FROM mlot m, nse_expiry x
                        WHERE m.symbol = contract.symbol AND m.ym = substr(contract.expiry, 1, 7)
                          AND x.symbol = contract.symbol AND x.expiry = contract.expiry
                          AND contract.in_span AND NOT contract.in_master""").rowcount
    info["span_only_lots_from_market_lots"] = n


# ---- ELM -------------------------------------------------------------------------
def load_elm(conn: sqlite3.Connection, path: Path, rates: dict) -> int:
    rows = []
    with open(path, newline="") as f:
        rd = csv.reader(f)
        head = next(rd)
        if len(head) < 6 or "ELM" not in head[3]:
            raise LoadCheckFailed(f"unexpected ELM file header: {head}")
        for r in rd:
            if len(r) < 6 or not r[1].strip():
                continue
            rows.append((r[1].strip(), r[2].strip(), _fnum(r[3]), _fnum(r[4]), _fnum(r[5]), "ael_file", None))
    kinds = {r[1] for r in rows}
    if not kinds <= {"OTH", "OTM"}:
        raise LoadCheckFailed(f"unknown ELM instrument types {kinds - {'OTH', 'OTM'}}")
    conn.executemany("INSERT INTO elm_rate VALUES (?,?,?,?,?,?,?)", rows)

    # indices: not in the file, rates from static_rates.yaml
    idx = rates.get("index_elm", {})
    for sym, in conn.execute("""SELECT symbol FROM underlying WHERE kind = 'INDEX'
                                AND symbol NOT IN (SELECT symbol FROM elm_rate)""").fetchall():
        for applies_to, r in idx.items():
            conn.execute("INSERT INTO elm_rate VALUES (?,?,?,?,?,?,?)",
                         (sym, applies_to, r["pct"], 0.0, r["pct"], r["source"], int(bool(r.get("verified")))))
    for name, r in rates.get("rules", {}).items():
        conn.execute("INSERT INTO rule VALUES (?,?,?,?,?)",
                     (name, r["value"], r.get("source"), int(bool(r.get("verified"))), r.get("note")))
    return len(rows)


# ---- prices ----------------------------------------------------------------------
def load_bhavcopy(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["FinInstrmId"]), _fnum(r["ClsPric"]), _fnum(r["SttlmPric"]),
                         _fnum(r["UndrlygPric"]), _inum(r["OpnIntrst"]), _inum(r["TtlTradgVol"])))
    conn.execute("CREATE TEMP TABLE bhav (token INTEGER PRIMARY KEY, close REAL, settle REAL, "
                 "underlying_price REAL, oi INTEGER, volume INTEGER)")
    conn.executemany("INSERT OR REPLACE INTO bhav VALUES (?,?,?,?,?,?)", rows)
    n = conn.execute("""INSERT INTO price(contract_id, close, settle, underlying_price, open_interest, volume)
                        SELECT c.id, b.close, b.settle, b.underlying_price, b.oi, b.volume
                        FROM bhav b JOIN contract c ON c.token = b.token""").rowcount
    return n, len(rows) - n


# ---- start of day (BOD): the file client margins use all day ----------------------
def last_trading_day(data_dir: Path | None, day: date) -> date:
    from exfetch.dates import Calendar
    cal = Calendar(Path(data_dir) / "_cache") if data_dir else None
    ok = (lambda d: cal.is_trading_day(d, SEGMENT)) if cal else (lambda d: d.weekday() < 5)
    while not ok(day):
        day -= timedelta(days=1)
    return day


def bod_choice(data_dir: Path, today: date, trade_date: date | None = None) -> dict:
    """Which SPAN file client margins use (SEBI/HO/MRD2/DCAP/P/CIR/2022/60, .../2023/016:
    fixed beginning-of-day parameters all day): the trade date's i1. The trade date is
    the later of the newest i1 on disk (the next day's comes out ~21:30 the evening
    before) and the last trading day up to today. Without that i1: the previous
    trading day's final (s) file, which NSE builds the i1 from (26 Oct s = 27 Oct i1),
    else the newest earlier i1; both marked stale.
    trade_date: fix the trade date (a rebuild of a past day) instead of working it out.
    -> {trade_date, day, cycle, created, stale, note}"""
    base = Path(data_dir) / SEGMENT / "span"
    days = sorted(p.name for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)) if base.is_dir() else []
    cycles = {d: {c: t for _, c, t in span_cycles(data_dir, d)} for d in days}
    i1_days = [d for d in days if "i1" in cycles[d]]
    trade = trade_date.isoformat() if trade_date else max([last_trading_day(data_dir, today).isoformat()] + i1_days[-1:])

    def pick(day, cycle, stale, note):
        return {"trade_date": trade, "day": day, "cycle": cycle, "stale": stale, "note": note,
                "created": f"{cycles[day][cycle]:%Y-%m-%d %H:%M}"}
    if "i1" in cycles.get(trade, {}):
        return pick(trade, "i1", False, "")
    prev = previous_trading_day(data_dir, trade)
    if "s" in cycles.get(prev, {}):
        return pick(prev, "s", True, f"{trade}'s start-of-day (i1) file isn't out; using {prev}'s final file")
    earlier = [d for d in i1_days if d < trade]
    if earlier:
        return pick(earlier[-1], "i1", True, f"{trade}'s start-of-day (i1) file isn't out; using {earlier[-1]}'s")
    raise MissingFile(f"no start-of-day SPAN file for {trade} or before in {base}")


# ---- previous close: the deep-OTM ELM reference ------------------------------------
CASH_BHAVCOPY = Want("bhavcopy", "nse.bhavcopy", r"^BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv$")


def _fo_closes(path: Path) -> dict:
    """Underlying close per symbol from an F&O bhavcopy (UndrlygPric)."""
    closes = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            p = _fnum(r["UndrlygPric"])
            if p:
                closes.setdefault(r["TckrSymb"], p)
    return closes


def _cash_closes(path: Path) -> dict:
    """Closing price per symbol from the NSE cash bhavcopy (ClsPric, EQ series)."""
    closes = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            p = _fnum(r["ClsPric"])
            if p and r["SctySrs"].strip() == "EQ":
                closes[r["TckrSymb"]] = p
    return closes


def _spots(sp: spanmod.Span) -> dict:
    """Underlying spot per combined commodity in a parsed SPAN file."""
    out = {}
    for cc in sp.ccdefs:
        phy = next((sp.products.get(l["pf_id"]) for l in cc["links"] if l["pf_type"] == "PHY"), None) or {}
        if phy.get("spot_price"):
            out[cc["cc"]] = phy["spot_price"]
    return out


def _i1_spots(data_dir: Path, span_date: str, sp: spanmod.Span, cycle: str) -> dict:
    """Spots in the day's first SPAN cycle (i1, made the evening before from that
    day's closes: equal to the previous F&O bhavcopy's UndrlygPric for all 219
    underlyings on 27 Oct 2026)."""
    if cycle == "i1":
        return _spots(sp)
    folder = Path(data_dir) / SEGMENT / "span" / span_date
    rx = re.compile(WANTS["span"].pattern)
    for p in sorted(folder.glob("nsccl.*.spn")) if folder.is_dir() else []:
        m = rx.search(p.name)
        if m and cycle_name(m.group(2)) == "i1":
            return _spots(spanmod.parse(str(p), lambda c: None))
    return {}


def previous_trading_day(data_dir: Path | None, day: str) -> str:
    from exfetch.dates import Calendar      # weekends + NSE's cached holiday list
    if data_dir:
        cal = Calendar(Path(data_dir) / "_cache")
        return cal.previous_trading_day(datetime.strptime(day, "%Y-%m-%d").date(), SEGMENT).isoformat()
    d = datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def load_reference(conn: sqlite3.Connection, data_dir: Path | None, sp: spanmod.Span, info: dict, log=print):
    """Each underlying's previous close for the deep-OTM test ("previous day closing
    underlying price", NCL/CMPT/44391; also the short-option ELM price), always the
    previous trading day's, from the first of: F&O bhavcopy UndrlygPric, the day's i1
    SPAN spot, NSE cash bhavcopy close (last: it can differ by paise, e.g. MARUTI 26 Oct
    cash 11,220.00 vs F&O 11,219.80, and Angel One used the F&O figure).
    With none of them: the newest earlier F&O close, else the loaded SPAN spot, marked
    stale (the engine warns). Records source and date per underlying. The day is the
    trade date's (a BOD fallback file can be from the day before)."""
    span_date = info.get("trade_date") or info["span_business_date"]
    want = previous_trading_day(data_dir, span_date)
    need = [r[0] for r in conn.execute("SELECT symbol FROM underlying")]
    listed = {r[0] for r in conn.execute("SELECT symbol FROM underlying WHERE in_master")}
    got: dict = {}                                   # symbol -> (price, source, date, stale)

    def day_file(seg: str, w: Want):
        hits = _hits(Path(data_dir) / seg / w.kind / want, w) if data_dir else []
        return hits[-1][0] if hits else None

    steps = [("fo_bhavcopy", lambda: _fo_closes(f) if (f := day_file(SEGMENT, WANTS["bhavcopy"])) else {}),
             ("span_i1", lambda: _i1_spots(data_dir, span_date, sp, info["span_cycle"]) if data_dir else {}),
             ("cash_bhavcopy", lambda: _cash_closes(f) if (f := day_file("EQ", CASH_BHAVCOPY)) else {})]
    for source, closes in steps:
        if not any(s not in got for s in listed):    # BSE-only underlyings never trigger a fallback
            break
        for sym, p in closes().items():
            if sym in need and sym not in got:
                got[sym] = (p, source, want, 0)

    missing = [s for s in need if s not in got]
    if missing:
        older = find_before(data_dir, SEGMENT, WANTS["bhavcopy"], want) if data_dir else None
        oc = _fo_closes(older.path) if older else {}
        spot = dict(conn.execute("SELECT symbol, spot_price FROM underlying"))
        for sym in missing:
            got[sym] = (oc[sym], "fo_bhavcopy", older.trade_date, 1) if sym in oc else \
                (spot.get(sym), "span_spot", span_date, 1)
    conn.executemany("""UPDATE underlying SET prev_close = ?, prev_close_source = ?, prev_close_date = ?,
                        prev_close_stale = ? WHERE symbol = ?""", [(*v, s) for s, v in got.items()])
    by_source: dict = {}
    for sym, (p, source, d, stale) in got.items():
        key = f"{source}{' (stale)' if stale else ''}{'' if sym in listed else ' (not NSE-listed)'}"
        by_source[key] = by_source.get(key, 0) + 1
    stale_listed = sorted(s for s in listed if got[s][3])
    info.update(prev_close_date=want, prev_close_sources=", ".join(f"{k}: {n}" for k, n in sorted(by_source.items())),
                prev_close_stale=len(stale_listed))
    if stale_listed:
        log(f"  previous close for {want} missing for {len(stale_listed)} listed underlyings "
            f"(e.g. {', '.join(stale_listed[:5])}): deep-OTM test marked stale")


def load_settlement(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """MTM settlement prices of futures, keyed by underlying + expiry."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            r = {k.strip(): v for k, v in r.items()}
            if r["INSTRUMENT"].strip().startswith("FUT") and not TEST_SYMBOL.match(r["UNDERLYING"].strip()):
                rows.append((r["UNDERLYING"].strip(), _dmy(r["EXPIRY DATE"]), _fnum(r["MTM SETTLEMENT PRICE"]),
                             "INDEX" if r["INSTRUMENT"].strip() == "FUTIDX" else "STOCK"))
    conn.execute("CREATE TEMP TABLE sett (symbol TEXT, expiry TEXT, price REAL, kind TEXT)")
    conn.executemany("INSERT INTO sett VALUES (?,?,?,?)", rows)
    conn.execute("""INSERT OR IGNORE INTO price(contract_id)
                    SELECT c.id FROM sett s JOIN contract c
                    ON c.symbol = s.symbol AND c.expiry = s.expiry AND c.instrument = 'FUT'""")
    n = conn.execute("""UPDATE price SET mtm_settle = s.price FROM sett s, contract c
                        WHERE c.id = price.contract_id AND c.symbol = s.symbol AND c.expiry = s.expiry
                          AND c.instrument = 'FUT'""").rowcount
    # underlyings the master doesn't cover (the BSE indices): kind from FUTIDX/FUTSTK
    conn.execute("""UPDATE underlying SET kind = (SELECT MAX(kind) FROM sett WHERE sett.symbol = underlying.symbol)
                    WHERE kind IS NULL""")
    return n, len(rows) - n


def load_ban_list(conn: sqlite3.Connection, path: Path) -> tuple[str | None, int]:
    lines = path.read_text().splitlines()
    m = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})", lines[0] if lines else "")
    syms = [ln.split(",", 1)[1].strip() for ln in lines[1:] if "," in ln and ln.split(",", 1)[1].strip()]
    conn.executemany("INSERT OR IGNORE INTO ban_list VALUES (?)", [(s,) for s in syms])
    conn.execute("UPDATE underlying SET is_banned = 1 WHERE symbol IN (SELECT symbol FROM ban_list)")
    return (_dmy(m.group(1)) if m else None), len(syms)


# ---- whole segment ---------------------------------------------------------------
def load(conn: sqlite3.Connection, inputs: dict[str, Input], rates_path: Path, log=print,
         data_dir: Path | None = None, bod: dict | None = None) -> dict:
    """bod: bod_choice() result when building the start-of-day database."""
    rates = (yaml.safe_load(Path(rates_path).read_text()) or {}).get(SEGMENT, {})
    info = {"segment": SEGMENT}

    def note(key, inp: Input):
        info[f"{key}_file"] = inp.path.name
        info[f"{key}_trade_date"] = inp.trade_date

    s = inputs["span"]
    sp = load_span(conn, s.path)
    note("span", s)
    created = sp.header.get("created", "")
    info.update(span_cycle=cycle_name(s.groups[1]),
                span_created=(f"{created[:4]}-{created[4:6]}-{created[6:8]} {created[8:10]}:{created[10:12]}"
                              if len(created) >= 12 else created),
                span_business_date=spanmod.iso(sp.header.get("date")),
                span_settlement=sp.header.get("setlQualifier"),     # early | final
                span_contracts=conn.execute("SELECT COUNT(*) FROM contract").fetchone()[0],
                basis="bod" if bod else "cycle",
                trade_date=bod["trade_date"] if bod else spanmod.iso(sp.header.get("date")),
                bod_stale=int(bool(bod and bod["stale"])), bod_note=(bod or {}).get("note", ""),
                test_underlyings_dropped=sum(1 for c in sp.ccdefs if TEST_SYMBOL.match(c["cc"])))
    log(f"  SPAN {s.path.name}: {info['span_contracts']:,} contracts, {len(sp.ccdefs)} underlyings")

    m = pair_master(data_dir, inputs["contract_master"], info["trade_date"], info, log)
    info["master_expected_date"] = previous_trading_day(data_dir, info["trade_date"])
    info["master_contracts"] = load_master(conn, m.path, info, log)
    note("contract_master", m)

    if "market_lots" in inputs:
        ml = inputs["market_lots"]
        note("market_lots", ml)
        load_market_lots(conn, ml.path, m.trade_date, ml.trade_date, info["trade_date"], info, log,
                         info["master_expected_date"])
    else:
        info["master_stale"] = int(m.trade_date < info["master_expected_date"])

    load_reference(conn, data_dir, sp, info, log)

    if "bhavcopy" in inputs:
        n, unmatched = load_bhavcopy(conn, inputs["bhavcopy"].path)
        note("bhavcopy", inputs["bhavcopy"])
        info.update(bhavcopy_rows=n, bhavcopy_unmatched=unmatched)

    if "settlement" in inputs:      # before ELM: it tells which non-master underlyings are indices
        n, unmatched = load_settlement(conn, inputs["settlement"].path)
        note("settlement", inputs["settlement"])
        info.update(settlement_rows=n, settlement_unmatched=unmatched)

    e = inputs["elm"]
    info["elm_rows"] = load_elm(conn, e.path, rates)
    note("elm", e)

    if "ban_list" in inputs:
        ban_date, n = load_ban_list(conn, inputs["ban_list"].path)
        note("ban_list", inputs["ban_list"])
        info.update(ban_list_for=ban_date, ban_list_count=n)

    check(conn, info)
    info.update(loaded_at=datetime.now().isoformat(timespec="seconds"))
    conn.executemany("INSERT INTO load_info VALUES (?,?)", [(k, str(v)) for k, v in info.items()])
    return info


def check(conn: sqlite3.Connection, info: dict):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    span_date = info["span_business_date"]
    info["contracts"] = q("SELECT COUNT(*) FROM contract")
    info["contracts_in_both"] = q("SELECT COUNT(*) FROM contract WHERE in_span AND in_master")
    info["span_without_master"] = q("SELECT COUNT(*) FROM contract WHERE in_span AND NOT in_master")
    info["master_not_yet_in_span"] = q("SELECT COUNT(*) FROM contract WHERE in_master AND NOT in_span")
    info["contracts_nse"] = q("SELECT COUNT(*) FROM contract WHERE exchange = 'NSE'")
    info["contracts_bse"] = q("SELECT COUNT(*) FROM contract WHERE exchange = 'BSE'")
    # master contracts that should be in this SPAN file: listed by its date, not expired.
    # The final (s) file is made after the close and leaves out the day's expired
    # contracts (27 Oct 2026 s: all 34,017 of them); they stay listed for the day.
    after = ">" if info.get("span_cycle") == "s" else ">="
    eligible = q(f"SELECT COUNT(*) FROM contract WHERE in_master AND listed_from <= ? AND expiry {after} ?",
                 span_date, span_date)
    gap = q(f"""SELECT COUNT(*) FROM contract WHERE in_master AND NOT in_span
                AND listed_from <= ? AND expiry {after} ?""", span_date, span_date)
    info["master_without_span"] = gap
    info["expiring_without_span"] = q("""SELECT COUNT(*) FROM contract WHERE in_master AND NOT in_span
                                         AND expiry = ?""", span_date)
    problems = []
    if q("SELECT COUNT(*) FROM risk_array") < MIN_CONTRACTS:
        problems.append("too few risk arrays")
    if q("SELECT COUNT(*) FROM underlying") < MIN_UNDERLYINGS:
        problems.append("too few underlyings")
    if eligible and gap / eligible > MAX_MASTER_WITHOUT_SPAN:
        problems.append(f"{gap} of {eligible} listed contracts have no SPAN risk array")
    if q("SELECT COUNT(*) FROM contract c JOIN risk_array r ON r.contract_id = c.id WHERE NOT c.in_span"):
        problems.append("risk array on a contract not from SPAN")
    no_elm = [r[0] for r in conn.execute("""SELECT symbol FROM underlying WHERE in_master AND symbol NOT IN
                                            (SELECT symbol FROM elm_rate WHERE applies_to = 'OTH')""")]
    if no_elm:
        problems.append(f"no ELM rate for {no_elm[:10]} (index missing from static_rates.yaml?)")
    if q("SELECT COUNT(*) FROM contract WHERE exchange IS NULL OR (in_master AND exchange <> 'NSE')"):
        problems.append("contracts with no exchange, or master contracts not marked NSE")
    if q("SELECT COUNT(*) FROM contract WHERE in_master AND (lot_size IS NULL OR lot_size <= 0)"):
        problems.append("master contracts without a lot size")
    if problems:
        raise LoadCheckFailed("; ".join(problems))
