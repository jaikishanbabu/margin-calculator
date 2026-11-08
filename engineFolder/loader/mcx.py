"""MCX: MCXCCL's SPAN file + daily margins file + contract units table -> one SQLite database.

SPAN (MCXRPF-<date>-<HHMM>-<NN>-I|E.spn, SPAN XML 4.00): ten a day; -01 (made ~01:00-06:00)
is the start of day, the -E file (23:45) the end of day. Options are on futures (oofPf): each
option is tied to its future (contract.und_contract_id), whose price is the short option's
exposure base (static_rates MCX rule; start-of-day future price, no premium, verified: false).

Margins (mcx_daily_margin_<date>_<version>.csv, versions 1..N a day; _1 = start of day): per
futures contract, % of contract value: ELM (elmLong / elmShort), additional, special, tender,
delivery. Options take their future's ELM.

Units (loader/mcx_contracts.yaml): SPAN's cvf is 1 and its risk arrays are per quotation unit
(per 10 g for GOLD), so a lot = `multiplier` units; lot_size here is that number. Commodities
without a multiplier (the brokers disagree, or not in the table) are left unpriced
(in_master = 0); unverified rows are priced and marked (lot_source mcx_table_unverified).
"""
from __future__ import annotations

import collections
import csv
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import yaml

from . import nfo
from . import span as spanmod
from .files import Input, MissingFile, Want, find_before

SEGMENT = "MCX"
UNITS = Path(__file__).with_name("mcx_contracts.yaml")
START_OF_DAY, END_OF_DAY = "01", "EOD"
LoadCheckFailed = nfo.LoadCheckFailed


def _rank(groups):
    return (groups[0], 99 if groups[3].upper() == "E" else int(groups[2]))


WANTS = {
    "span": Want("span", "mcx.span", r"^MCXRPF-(\d{8})-(\d{4})-(\d{2})-([IEie])\.spn$", rank=_rank, exact=True),
}
MARGINS = Want("exposure", "mcx.margins", r"^mcx_daily_margin_(\d{8})_(\d+)\.csv$",
               rank=lambda g: (g[0], int(g[1])))


def cycle_of(groups) -> str:
    return END_OF_DAY if groups[3].upper() == "E" else groups[2]


def wants(span_cycle: str | None = None) -> dict[str, Want]:
    if not span_cycle:
        return WANTS
    if span_cycle == END_OF_DAY:
        pat = r"^MCXRPF-(\d{8})-(\d{4})-(\d{2})-([Ee])\.spn$"
    else:
        pat = rf"^MCXRPF-(\d{{8}})-(\d{{4}})-({re.escape(span_cycle)})-([Ii])\.spn$"
    return {"span": Want("span", "mcx.span", pat, exact=True)}


def span_cycles(data_dir: Path, day: str) -> list[tuple[str, str, datetime]]:
    folder = Path(data_dir) / SEGMENT / "span" / day
    rx = re.compile(WANTS["span"].pattern)
    out = []
    for p in folder.glob("MCXRPF-*.spn") if folder.is_dir() else []:
        m = rx.search(p.name)
        if not m:
            continue
        with open(p, "rb") as f:
            c = re.search(rb"<created>(\d{12})</created>", f.read(4096))
        if c:
            out.append((day, cycle_of(m.groups()), datetime.strptime(c.group(1).decode(), "%Y%m%d%H%M")))
    return sorted(out, key=lambda x: x[2])


def bod_choice(data_dir: Path, today: date, trade_date: date | None = None) -> dict:
    """The trade date's -01; else the previous trading day's end-of-day file, marked stale."""
    base = Path(data_dir) / SEGMENT / "span"
    days = sorted(p.name for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)) if base.is_dir() else []
    cycles = {d: {c: t for _, c, t in span_cycles(data_dir, d)} for d in days}
    sod = [d for d in days if START_OF_DAY in cycles[d]]
    trade = trade_date.isoformat() if trade_date else max([nfo.last_trading_day(data_dir, today).isoformat()] + sod[-1:])

    def pick(day, cycle, stale, note):
        return {"trade_date": trade, "day": day, "cycle": cycle, "stale": stale, "note": note,
                "created": f"{cycles[day][cycle]:%Y-%m-%d %H:%M}"}
    if START_OF_DAY in cycles.get(trade, {}):
        return pick(trade, START_OF_DAY, False, "")
    prev = nfo.previous_trading_day(data_dir, trade)
    if END_OF_DAY in cycles.get(prev, {}):
        return pick(prev, END_OF_DAY, True, f"{trade}'s start-of-day (-01) file isn't out; using {prev}'s end-of-day file")
    earlier = [d for d in sod if d < trade]
    if earlier:
        return pick(earlier[-1], START_OF_DAY, True, f"{trade}'s start-of-day (-01) file isn't out; using {earlier[-1]}'s")
    raise MissingFile(f"no start-of-day MCX SPAN file for {trade} or before in {base}")


def units_table(path: Path = UNITS) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _margins_file(data_dir: Path, day: str, start_of_day: bool) -> Input | None:
    """The day's margins file: version 1 (start of day) or the newest one."""
    folder = Path(data_dir) / SEGMENT / "exposure" / day
    rx = re.compile(MARGINS.pattern)
    hits = sorted(((int(m.group(2)), p) for p in folder.glob("mcx_daily_margin_*.csv") if (m := rx.search(p.name))),
                  key=lambda x: x[0]) if folder.is_dir() else []
    if hits:
        v, p = hits[0] if start_of_day else hits[-1]
        return Input("margins", p, day, (day, str(v)))
    return None


def load(conn: sqlite3.Connection, inputs: dict[str, Input], rates_path: Path, log=print,
         data_dir: Path | None = None, bod: dict | None = None) -> dict:
    rates = (yaml.safe_load(Path(rates_path).read_text()) or {}).get(SEGMENT, {})
    info = {"segment": SEGMENT}
    s = inputs["span"]
    sp = nfo.load_span(conn, s.path)
    created = sp.header.get("created", "")
    info.update(span_file=s.path.name, span_trade_date=s.trade_date, span_cycle=cycle_of(s.groups),
                span_created=f"{created[:4]}-{created[4:6]}-{created[6:8]} {created[8:10]}:{created[10:12]}",
                span_business_date=spanmod.iso(sp.header.get("date")),
                span_contracts=conn.execute("SELECT COUNT(*) FROM contract").fetchone()[0],
                basis="bod" if bod else "cycle",
                trade_date=bod["trade_date"] if bod else spanmod.iso(sp.header.get("date")),
                bod_stale=int(bool(bod and bod["stale"])), bod_note=(bod or {}).get("note", ""))
    log(f"  SPAN {s.path.name}: {info['span_contracts']:,} contracts, {len(sp.ccdefs)} commodities")

    # options -> their future (same commodity, the series' undC cId)
    conn.execute("""UPDATE contract SET und_cid = (SELECT f.id FROM contract f WHERE f.symbol = contract.symbol
                        AND f.instrument = 'FUT' AND f.span_cid = contract.und_cid)
                    WHERE instrument = 'OPT' AND und_cid IS NOT NULL""")
    info["options_without_future"] = conn.execute(
        "SELECT COUNT(*) FROM contract WHERE instrument = 'OPT' AND und_cid IS NULL AND symbol <> 'MCXBULLDEX'").fetchone()[0]

    # units
    table = units_table()
    priced, unverified, unpriced = [], [], []
    for sym, row in table.items():
        m = row.get("multiplier")
        if not m:
            unpriced.append(sym)
            continue
        src = "mcx_table" if row.get("verified") else "mcx_table_unverified"
        conn.execute("""UPDATE contract SET exchange = 'MCX', lot_size = ?, lot_source = ?, in_master = 1
                        WHERE symbol = ?""", (int(m), src, sym))
        (priced if row.get("verified") else unverified).append(sym)
    conn.execute("UPDATE contract SET exchange = 'MCX' WHERE exchange IS NULL")
    missing = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM contract WHERE lot_size IS NULL") if r[0] not in table]
    info.update(units_verified=",".join(sorted(priced)), units_unverified=",".join(sorted(unverified)),
                units_unpriced=",".join(sorted(unpriced)), units_not_in_table=",".join(sorted(missing)))
    log(f"  units: {len(priced)} verified, {len(unverified)} unverified (priced, marked), "
        f"{len(unpriced)} unpriced ({', '.join(unpriced)}), {len(missing)} not in the table")

    # margins: start-of-day version for the BOD database, the newest for the cycle one
    mf = _margins_file(data_dir, info["trade_date"], start_of_day=bool(bod)) if data_dir else None
    if mf is None and data_dir:
        prev = find_before(Path(data_dir), SEGMENT, MARGINS, info["trade_date"])
        mf = prev
    if mf is None:
        raise MissingFile(f"no MCX daily margins file for {info['trade_date']}")
    info.update(margins_file=mf.path.name, margins_trade_date=mf.trade_date, margins_version=mf.groups[1] if mf.groups else "")
    _apply_margins(conn, mf.path, info, log)

    # underlying: ELM row (engine needs one), spot = SPAN spot; previous close not used on MCX
    conn.execute("UPDATE underlying SET kind = CASE WHEN symbol LIKE 'MCX%DEX' THEN 'INDEX' ELSE 'COMMODITY' END, "
                 "in_master = 1, prev_close = spot_price, prev_close_source = 'span_spot', prev_close_date = ?",
                 (info["span_business_date"],))
    conn.executemany("INSERT INTO elm_rate VALUES (?,?,?,?,?,?,?)",
                     [(sym, "OTH", e, 0.0, e, "mcx_margins_file", None) for sym, e in conn.execute(
                         "SELECT symbol, MAX(elm_pct) FROM contract WHERE instrument = 'FUT' AND elm_pct IS NOT NULL "
                         "GROUP BY symbol")])
    for name, r in rates.get("rules", {}).items():
        conn.execute("INSERT INTO rule VALUES (?,?,?,?,?)",
                     (name, r["value"], r.get("source"), int(bool(r.get("verified"))), r.get("note")))
    check(conn, info)
    info.update(loaded_at=datetime.now().isoformat(timespec="seconds"))
    conn.executemany("INSERT INTO load_info VALUES (?,?)", [(k, str(v)) for k, v in info.items()])
    return info


MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                      "NOV", "DEC"), 1)}


def _apply_margins(conn, path: Path, info: dict, log):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            e = r.get("expiryDate", "").strip()        # 19NOV2026
            m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{4})", e)
            if not m:
                continue
            exp = f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{m.group(1)}"
            g = lambda k: float(r.get(k) or 0)          # noqa: E731
            rows.append((max(g("elmLong"), g("elmShort")), g("additionalLongMargin") + g("specialLongMargin"),
                         g("additionalShortMargin") + g("specialShortMargin"), g("tenderMargin"), g("deliveryMargin"),
                         r["symbol"].strip(), exp))
    conn.executemany("""UPDATE contract SET elm_pct = ?, extra_long_pct = ?, extra_short_pct = ?, tender_pct = ?,
                        delivery_pct = ? WHERE instrument = 'FUT' AND symbol = ? AND expiry = ?""", rows)
    # options: their future's ELM
    conn.execute("""UPDATE contract SET elm_pct = (SELECT f.elm_pct FROM contract f WHERE f.id = contract.und_cid)
                    WHERE instrument = 'OPT' AND und_cid IS NOT NULL""")
    n = conn.execute("SELECT COUNT(*) FROM contract WHERE instrument = 'FUT' AND elm_pct IS NULL AND in_master").fetchone()[0]
    info.update(margins_rows=len(rows), futures_without_margins=n,
                contracts_with_extra=conn.execute("SELECT COUNT(*) FROM contract WHERE instrument = 'FUT' AND "
                                                  "(extra_long_pct > 0 OR extra_short_pct > 0 OR tender_pct > 0)").fetchone()[0])
    log(f"  margins {path.name}: {len(rows)} futures rows; {n} priced futures without a row; "
        f"{info['contracts_with_extra']} with additional / special / tender margin")


def check(conn: sqlite3.Connection, info: dict):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    problems = []
    if q("SELECT COUNT(*) FROM risk_array") < 500:
        problems.append("too few risk arrays")
    if q("SELECT COUNT(*) FROM contract WHERE in_master") < 100:
        problems.append("too few priced contracts (units table matched?)")
    if info.get("options_without_future", 0) > 50:
        problems.append(f"{info['options_without_future']} options not tied to a future")
    if problems:
        raise LoadCheckFailed("; ".join(problems))


def freshness(info: dict) -> tuple:
    return (info.get("span_business_date") or "", info.get("span_created") or "")
