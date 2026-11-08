"""Database layout, and building a fresh copy that replaces the live one in one step.

Readers (engine/API) never see a half-loaded database: the loader writes
<SEG>.sqlite.tmp, checks it, then os.replace()s it over <SEG>.sqlite. A reader
that already has the old file open keeps reading the old data until it reopens.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: no lock, run one loader at a time
    fcntl = None

SCHEMA = """
CREATE TABLE load_info (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE scenario (
    kind TEXT, point INTEGER, price_mult REAL, vol_mult REAL, weight REAL, paired_point INTEGER,
    PRIMARY KEY (kind, point));                 -- kind: scan (16) | delta

CREATE TABLE underlying (
    symbol TEXT PRIMARY KEY,                    -- SPAN combined commodity (= NSE ticker)
    name TEXT, kind TEXT,                       -- kind: INDEX | STOCK
    currency TEXT, spot_price REAL, price_scan REAL, vol_scan REAL,
    prev_close REAL,                            -- previous trading day's close (deep-OTM reference)
    prev_close_source TEXT,                     -- fo_bhavcopy | cash_bhavcopy | span_i1 | span_spot
    prev_close_date TEXT,                       -- the day that close is for
    prev_close_stale INTEGER NOT NULL DEFAULT 0,  -- 1: not the previous trading day's close
    som_rate REAL, som_method TEXT,             -- short-option minimum (0 on NSE)
    underlying_token INTEGER,
    in_master INTEGER NOT NULL DEFAULT 0,       -- 0: e.g. BSE indices NSE Clearing also clears
    is_banned INTEGER NOT NULL DEFAULT 0);

CREATE TABLE contract (
    id INTEGER PRIMARY KEY,
    exchange TEXT,                              -- NSE | BSE. SPAN doesn't say: NSE = in NSE's master or on
                                                -- one of its expiry dates; BSE = the rest (cleared by NSE Clearing)
    symbol TEXT NOT NULL,
    product_code TEXT,
    instrument TEXT NOT NULL,                   -- FUT | OPT
    expiry TEXT NOT NULL,                       -- YYYY-MM-DD
    strike REAL,                                -- NULL for futures
    option_type TEXT,                           -- C | P, NULL for futures
    multiplier REAL,                            -- SPAN contract value factor (cvf)
    -- from SPAN (NULL when in_span = 0)
    span_price REAL, delta REAL, vol REAL, time_to_expiry REAL, price_scan REAL, vol_scan REAL,
    -- from the contract master (NULL when in_master = 0)
    token INTEGER UNIQUE, tradingsymbol TEXT, instrument_type TEXT, lot_size INTEGER,
    settlement TEXT, listed_from TEXT, permitted_to_trade INTEGER,
    lot_source TEXT,                            -- master | market_lots (fo_mktlots.csv, when the master is stale)
    elm_pct REAL,                               -- BFO: the contract's own ELM % (ICCL file, final); NULL = rules
    span_cid INTEGER, und_pf_id INTEGER, und_cid INTEGER,   -- SPAN ids; MCX: und_cid = the option's future's id
    extra_long_pct REAL, extra_short_pct REAL, tender_pct REAL, delivery_pct REAL,   -- MCX margins file (% of value)
    in_span INTEGER NOT NULL DEFAULT 0,
    in_master INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX contract_key ON contract
    (IFNULL(exchange, ''), symbol, instrument, expiry, IFNULL(option_type, ''), IFNULL(strike, 0));

CREATE TABLE risk_array (
    contract_id INTEGER PRIMARY KEY REFERENCES contract(id),
    s1 REAL, s2 REAL, s3 REAL, s4 REAL, s5 REAL, s6 REAL, s7 REAL, s8 REAL,
    s9 REAL, s10 REAL, s11 REAL, s12 REAL, s13 REAL, s14 REAL, s15 REAL, s16 REAL,
    composite_delta REAL);                      -- per unit; positive = loss to a long

CREATE TABLE spread (
    symbol TEXT, spread_no INTEGER, charge_method TEXT, rate REAL,
    PRIMARY KEY (symbol, spread_no));           -- spread_no = priority order
CREATE TABLE spread_leg (
    symbol TEXT, spread_no INTEGER, leg_no INTEGER,
    leg_symbol TEXT, expiry TEXT, side TEXT, ratio REAL,
    tier INTEGER,                               -- MCX: the leg is an intra tier (an expiry range), not an expiry
    PRIMARY KEY (symbol, spread_no, leg_no));

CREATE TABLE inter_spread (spread_no INTEGER PRIMARY KEY, method TEXT, credit_rate REAL);
CREATE TABLE inter_spread_leg (
    spread_no INTEGER, leg_no INTEGER, symbol TEXT, side TEXT, ratio REAL, tier INTEGER, expiry TEXT,
    PRIMARY KEY (spread_no, leg_no));

CREATE TABLE tier (
    symbol TEXT, tier_type TEXT, tier_no INTEGER, start_expiry TEXT, end_expiry TEXT, rate REAL);

CREATE TABLE elm_rate (
    symbol TEXT, applies_to TEXT,               -- OTH: all contracts | OTM: deep out-of-the-money options
    normal_pct REAL, additional_pct REAL, total_pct REAL,
    source TEXT, verified INTEGER,              -- source: ael_file | circular; verified = matched Zerodha (NULL for file rates)
    PRIMARY KEY (symbol, applies_to));
CREATE TABLE rule (name TEXT PRIMARY KEY, value REAL, source TEXT, verified INTEGER, note TEXT);

CREATE TABLE price (
    contract_id INTEGER PRIMARY KEY REFERENCES contract(id),
    close REAL, settle REAL, mtm_settle REAL, underlying_price REAL,
    open_interest INTEGER, volume INTEGER);

CREATE TABLE ban_list (symbol TEXT PRIMARY KEY);
"""


@contextmanager
def building(final: Path, schema: str = SCHEMA):
    """Yield a connection to a new empty database (NFO's schema unless another is
    given); on clean exit swap it in."""
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    with open(final.with_name(final.stem + ".lock"), "w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)          # one loader per segment at a time
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("PRAGMA journal_mode=OFF")   # scratch file: speed over crash safety
            conn.execute("PRAGMA synchronous=OFF")
            conn.executescript(schema)
            yield conn
            conn.commit()
            bad = conn.execute("PRAGMA quick_check").fetchone()[0]
            if bad != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {bad}")
            conn.execute("ANALYZE")
            conn.close()
            os.replace(tmp, final)
        except BaseException:
            conn.close()
            tmp.unlink(missing_ok=True)
            raise


def info(path: Path) -> dict:
    """load_info of an existing database, read-only."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT key, value FROM load_info"))
    finally:
        conn.close()
