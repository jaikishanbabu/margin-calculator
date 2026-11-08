"""NSE cash (equity): VaR batch + security master + bhavcopy + surveillance -> db/NSE.sqlite.

One row per security a user can pick (`security`), with NSE's rates and flags as
published; the engine (engine/nse.py) turns them into an effective margin rate.

Joins (never symbol + series: a renamed company keeps its ISIN and token, e.g.
ICICIPRULI -> ICICILIFE, token 18652):
  master <-> VaR        ISIN + the VaR file's series (the VaR file has one row per
                        ISIN, under the series trading today; it has no token)
  master <-> bhavcopy   token (FinInstrmId)
  surveillance lists    symbol (they carry nothing else), on the kept row

Kept: master rows with DelFlg N, not NSETEST / DUMMY, in an equity series, that
are either the VaR-matched row of their ISIN or tradable (status 6). So RELIANCE
appears once (EQ), not also as its inactive BE / BL rows; a stock whose series
changed after the VaR file was made (e.g. KHAZANCHI: SM -> EQ) is kept with no
VaR row, and the engine charges 100%.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

from .files import Input, Want
from .nfo import LoadCheckFailed

SEGMENT = "NSE"                  # db/NSE.sqlite, static_rates.yaml section
DATA_SEGMENT = "EQ"              # data/EQ/ (exfetch keeps NSE and BSE cash files there)
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "SZ")
T2T_SERIES = ("BE", "BZ", "ST", "SZ")
MIN_VAR_ROWS = 2_000             # equity-series rows in a VaR batch (3,763 on 27 Oct 2026)
MIN_SECURITIES = 2_000
MAX_AMR_MISMATCH = 0.001         # share of VaR rows where AMR != VaR + ELM + ad hoc
TEST_SYMBOL = re.compile(r"NSETEST|DUMMY", re.I)

SCHEMA = """
CREATE TABLE load_info (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE rule (name TEXT PRIMARY KEY, value REAL, source TEXT, verified INTEGER, note TEXT);

CREATE TABLE security (
    token INTEGER PRIMARY KEY,                  -- FinInstrmId
    symbol TEXT NOT NULL, series TEXT NOT NULL, name TEXT, isin TEXT NOT NULL,
    lot INTEGER NOT NULL,                       -- NewBrdLotQty (1, or the SME lot)
    tick REAL,                                  -- BidIntrvl / 100
    band_low REAL, band_high REAL,              -- PricRg "low-high"
    status INTEGER,                             -- SctyStsNrmlMkt (6 on every actively traded row)
    is_primary INTEGER NOT NULL,                -- the row a search for the symbol resolves to
    -- VaR batch (NULL when the ISIN + series has no VaR row)
    var_series TEXT, security_var REAL, index_var REAL, var_pct REAL, elm_pct REAL, adhoc_pct REAL,
    amr REAL,                                   -- applicable margin rate % = VaR + ELM + ad hoc
    -- prices: bhavcopy by token
    close REAL, prev_close REAL, close_date TEXT,
    -- surveillance
    asm_long TEXT, asm_short TEXT, gsm TEXT);   -- stage text (e.g. 'Stage I'), NULL if not listed
CREATE INDEX security_symbol ON security (symbol);
CREATE INDEX security_isin ON security (isin);
"""


def _batch_rank(groups):
    return (groups[0][4:] + groups[0][2:4] + groups[0][:2], int(groups[1]))


WANTS = {
    "var": Want("var", "nse.var", r"^C_VAR1_(\d{8})_(\d)\.DAT$", rank=_batch_rank, exact=True),
    "master": Want("contract_master", "nse.security", r"^NSE_CM_security_(\d{8})\.csv$"),
    "bhavcopy": Want("bhavcopy", "nse.bhavcopy", r"^BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv$"),
    "reg_ind": Want("surveillance", "nse.regulatory_indicator", r"^REG_IND(\d{6})\.csv$", required=False),
    "asm": Want("surveillance", "nse.asm", r"^nse_asm\.json$", required=False),
    "gsm": Want("surveillance", "nse.gsm", r"^nse_gsm\.json$", required=False),
}


def wants(batch: str | None = None) -> dict[str, Want]:
    """WANTS, optionally pinned to one VaR batch ('1'..'6') for date-mode loads."""
    if not batch:
        return WANTS
    out = dict(WANTS)
    out["var"] = Want("var", "nse.var", rf"^C_VAR1_(\d{{8}})_({int(str(batch).lstrip('b'))})\.DAT$", exact=True)
    return out


def freshness(info: dict) -> tuple:
    """Orders loads: a load never replaces a database built from a newer VaR batch."""
    return (info.get("var_date") or "", int(info.get("var_batch") or 0))


def describe(info: dict) -> str:
    return f"VaR batch {info.get('var_batch')} of {info.get('var_date')}"


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _dmy(s: str) -> str:
    return f"{s[4:]}-{s[2:4]}-{s[:2]}"


# ---- files ---------------------------------------------------------------------
def read_var(path: Path) -> tuple[dict, dict]:
    """-> (header {date, batch, rows}, {ISIN: (symbol, series, sec VaR, index VaR, VaR, ELM, ad hoc, AMR)}).
    Equity series only; 6-digit symbols (BSE scrip codes in the interoperable file) dropped.
    Header: 10,DDMMYYYY,0.00,[batch,]rows; the BOD file (batch 1) has no batch field,
    so the batch comes from the file name (C_VAR1_DDMMYYYY_n.DAT)."""
    name = re.search(r"_(\d)\.DAT$", path.name, re.I)
    head, rows, dropped = {}, {}, {"six_digit": 0, "other_series": 0, "test": 0}
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if not r:
                continue
            if r[0] == "10":
                batch = int(r[3]) if len(r) >= 5 else 1
                if name and int(name[1]) != batch:
                    raise LoadCheckFailed(f"{path.name}: header says batch {batch}")
                head = {"date": _dmy(r[1]), "batch": batch, "rows": int(r[-1])}
            elif r[0] == "20" and len(r) >= 10:
                sym, series, isin = r[1].strip(), r[2].strip(), r[3].strip()
                if re.fullmatch(r"\d{6}", sym):
                    dropped["six_digit"] += 1
                elif TEST_SYMBOL.search(sym) or TEST_SYMBOL.search(isin):
                    dropped["test"] += 1
                elif series not in EQUITY_SERIES:
                    dropped["other_series"] += 1
                else:
                    rows[isin] = (sym, series, *(float(x) for x in r[4:10]))
    if not head:
        raise LoadCheckFailed(f"{path.name}: no header record (10,...)")
    head["dropped"] = dropped
    return head, rows


def read_master(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    need = {"FinInstrmId", "TckrSymb", "SctySrs", "ISIN", "NewBrdLotQty", "BidIntrvl", "PricRg", "SctyStsNrmlMkt",
            "DelFlg"}
    if not rows or not need <= set(rows[0]):
        raise LoadCheckFailed(f"{path.name}: unexpected security master header")
    return [r for r in rows if r["DelFlg"].strip() == "N" and r["SctySrs"].strip() in EQUITY_SERIES
            and not TEST_SYMBOL.search(r["TckrSymb"]) and not r["ISIN"].upper().startswith("DUMMY")]


def _band(s: str):
    m = re.fullmatch(r"\s*([\d.]+)\s*-\s*([\d.]+)\s*", s or "")
    return (float(m[1]), float(m[2])) if m else (None, None)


def read_bhavcopy(path: Path) -> dict:
    """{token: (close, previous close, trade date)} from the CM UDiFF bhavcopy (ClsPric, not SttlmPric)."""
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("Sgmt") == "CM" and r.get("FinInstrmId"):
                out[int(r["FinInstrmId"])] = (_f(r["ClsPric"]), _f(r["PrvsClsgPric"]), r["TradDt"])
    return out


def read_surveillance(inputs: dict) -> dict:
    """{symbol: {asm_long, asm_short, gsm}} from NSE's ASM / GSM JSON."""
    out: dict = {}
    if "asm" in inputs:
        j = json.loads(inputs["asm"].path.read_text())
        for part, key in (("longterm", "asm_long"), ("shortterm", "asm_short")):
            for d in (j.get(part) or {}).get("data", []):
                out.setdefault(d["symbol"], {})[key] = d.get("asmSurvIndicator") or d.get("survCode")
    if "gsm" in inputs:
        for d in json.loads(inputs["gsm"].path.read_text()):
            out.setdefault(d["symbol"], {})["gsm"] = d.get("gsmStage") or d.get("survCode")
    return out


def batch_fetched_at(data_dir: Path | None, name: str) -> str | None:
    """When exfetch downloaded this VaR batch (data/manifest.sqlite), else None."""
    db = Path(data_dir or "data") / "manifest.sqlite"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("""SELECT MIN(at) FROM attempts WHERE source = 'nse.var' AND status = 'OK'
                              AND detail LIKE ?""", (f"{name}%",)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row and row[0] else None


# ---- load ----------------------------------------------------------------------
def load(conn: sqlite3.Connection, inputs: dict[str, Input], rates_path: Path, log=print,
         data_dir: Path | None = None) -> dict:
    rates = (yaml.safe_load(Path(rates_path).read_text()) or {}).get(SEGMENT, {})
    info: dict = {"segment": SEGMENT}

    def note(key, inp: Input):
        info[f"{key}_file"] = inp.path.name
        info[f"{key}_trade_date"] = inp.trade_date

    v = inputs["var"]
    head, var = read_var(v.path)
    note("var", v)
    bad = [i for i, r in var.items() if abs(r[4] + r[5] + r[6] - r[7]) > 0.011]
    info.update(var_date=head["date"], var_batch=head["batch"], var_rows_in_file=head["rows"],
                var_equity_rows=len(var), var_amr_mismatches=len(bad),
                var_dropped_six_digit=head["dropped"]["six_digit"], var_dropped_test=head["dropped"]["test"],
                var_fetched_at=batch_fetched_at(data_dir, v.path.name) or "")
    if len(var) < MIN_VAR_ROWS:
        raise LoadCheckFailed(f"{v.path.name}: only {len(var)} equity-series rows (expected >= {MIN_VAR_ROWS})")
    if len(bad) > MAX_AMR_MISMATCH * len(var):
        raise LoadCheckFailed(f"{v.path.name}: AMR != VaR + ELM + ad hoc on {len(bad)} rows, e.g. {bad[:3]}")
    log(f"  VaR {v.path.name}: batch {head['batch']} of {head['date']}, {len(var):,} equity rows")

    m = inputs["master"]
    master = read_master(m.path)
    note("master", m)
    by_isin: dict = {}
    for r in master:
        by_isin.setdefault(r["ISIN"].strip(), []).append(r)

    b = inputs["bhavcopy"]
    prices = read_bhavcopy(b.path)
    note("bhavcopy", b)
    surv = read_surveillance(inputs)
    for k in ("asm", "gsm", "reg_ind"):
        if k in inputs:
            note(k, inputs[k])

    rows, matched = [], set()
    for isin, sec_rows in by_isin.items():
        vr = var.get(isin)
        for r in sec_rows:
            series = r["SctySrs"].strip()
            is_var = vr is not None and vr[1] == series
            status = int(r["SctyStsNrmlMkt"]) if r["SctyStsNrmlMkt"].strip().isdigit() else None
            if not is_var and status != 6:
                continue                         # inactive alternate series of a listed stock
            if is_var:
                matched.add(isin)
            token, sym = int(r["FinInstrmId"]), r["TckrSymb"].strip()
            lo, hi = _band(r["PricRg"])
            close, prev, cdate = prices.get(token, (None, None, None))
            sv = surv.get(sym, {})
            rows.append([token, sym, series, r.get("FinInstrmNm", "").strip(), isin, int(r["NewBrdLotQty"] or 1),
                         _f(r["BidIntrvl"]) / 100 if _f(r["BidIntrvl"]) else None, lo, hi, status, 0,
                         *((vr[1], *vr[2:]) if is_var else (None,) * 7),
                         close, prev, cdate, sv.get("asm_long"), sv.get("asm_short"), sv.get("gsm")])
    # one primary row per symbol: the VaR-matched one, else EQ before the other series
    order = {s: i for i, s in enumerate(EQUITY_SERIES)}
    best: dict = {}
    for i, r in enumerate(rows):
        key = (r[11] is None, order.get(r[2], 9))
        if r[1] not in best or key < best[r[1]][0]:
            best[r[1]] = (key, i)
    for _, i in best.values():
        rows[i][10] = 1
    conn.executemany(f"INSERT INTO security VALUES ({','.join('?' * 24)})", rows)

    info.update(securities=len(rows), symbols=len(best),
                # BSE-exclusive securities NSE Clearing's interoperable file also carries, plus
                # series NSE deleted: not tradable on NSE, so not in the calculator
                var_rows_not_on_nse=len(set(var) - matched),
                securities_without_var=sum(1 for r in rows if r[11] is None),
                securities_without_close=sum(1 for r in rows if r[18] is None),
                close_date=max((r[20] for r in rows if r[20]), default=""))
    for name, r in rates.get("rules", {}).items():
        conn.execute("INSERT INTO rule VALUES (?,?,?,?,?)",
                     (name, r["value"], r.get("source"), int(bool(r.get("verified"))), r.get("note")))
    check(conn, info)
    info.update(loaded_at=datetime.now().isoformat(timespec="seconds"))
    conn.executemany("INSERT INTO load_info VALUES (?,?)", [(k, str(x)) for k, x in info.items()])
    log(f"  NSE: {info['securities']:,} securities ({info['securities_without_var']} without a VaR row, "
        f"{info['var_rows_not_on_nse']} VaR rows not listed on NSE)")
    return info


def check(conn: sqlite3.Connection, info: dict):
    if info["securities"] < MIN_SECURITIES:
        raise LoadCheckFailed(f"only {info['securities']} securities (expected >= {MIN_SECURITIES})")
    for sym in ("RELIANCE", "HDFCBANK", "TCS"):
        r = conn.execute("SELECT amr, close FROM security WHERE symbol = ? AND is_primary", (sym,)).fetchone()
        if not r or r[0] is None or r[1] is None:
            raise LoadCheckFailed(f"{sym}: no VaR rate or close price after the load")
    dup = conn.execute("SELECT symbol FROM security WHERE is_primary GROUP BY symbol HAVING COUNT(*) > 1").fetchone()
    if dup:
        raise LoadCheckFailed(f"two primary rows for {dup[0]}")
