"""NFO margin engine.

    python tests/test_engine_nfo.py      or      python -m pytest tests -q

1. SPAN core on made-up legs (tiers, spreads, option value).
2. Rules on the real 23 Oct 2026 files; expected values are worked out here
   from the raw risk arrays and rates, not by the engine.
3. Broker reference baskets (tests/fixtures/nfo_baskets.yaml, Zerodha and Angel
   One): each must match the SPAN cycle live at its capture time or an earlier one
   (brokers lag), loaded with that day's files.
"""
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import Engine, NoRiskData, Position, UnknownContract  # noqa: E402
from engine import match as match_mod  # noqa: E402
from engine import span as core  # noqa: E402
from loader import load_segment  # noqa: E402

DATA = ROOT / "data"
D = date(2026, 10, 23)
HAVE_FILES = (DATA / "NFO/span/2026-10-23/nsccl.20261023.s.spn").exists()
FIXTURES = ROOT / "tests/fixtures/nfo_baskets.yaml"
_TMP = Path(tempfile.mkdtemp(prefix="engine_test_"))
_eng = {}


def quiet(*a):
    pass


def eng() -> Engine:
    if "e" not in _eng:
        load_segment("NFO", DATA, _TMP / "db", D, log=quiet)
        _eng["e"] = Engine(_TMP / "db" / "NFO.sqlite")
    e = _eng["e"]
    e.rules = dict(e.conn.execute("SELECT name, value FROM rule").fetchall())   # undo any test tweak
    return e


def ra(e, **k):
    """Raw risk array, price and multiplier of one contract, straight from the tables."""
    strike = k.get("strike")
    r = e.conn.execute("""SELECT c.span_price, c.multiplier, c.lot_size, r.* FROM contract c
                          JOIN risk_array r ON r.contract_id = c.id
                          WHERE symbol = ? AND expiry = ? AND IFNULL(strike, 0) = ? AND IFNULL(option_type, '') = ?""",
                       (k["symbol"], k["expiry"], strike or 0, k.get("option_type") or "")).fetchone()
    return [r[f"s{i}"] for i in range(1, 17)], r["span_price"], r["multiplier"], r["lot_size"]


def close(a, b, tol=0.02):
    assert abs(a - b) <= tol, (a, b)


# ---- 1. SPAN core ---------------------------------------------------------------------
def _leg(expiry, units, risk, delta=1.0, instrument="FUT", price=0.0):
    return SimpleNamespace(expiry=expiry, units=units, multiplier=1.0, risk=risk, composite_delta=delta,
                           instrument=instrument, option_type=None, price=price)


def test_core_scan_tiers_do_not_offset():
    up = [-10.0] * 8 + [10.0] * 8          # long gains in scenarios 1-8, loses in 9-16
    near, far = _leg("2026-10-27", 1, up), _leg("2026-11-23", -1, up)
    one_tier, _ = core.scan_risk([near, far], [(1, "2026-10-27", "2031-01-01")])
    assert one_tier == 0.0                                       # perfect offset
    two, detail = core.scan_risk([near, far], [(1, "2026-10-27", "2026-10-27"), (2, "2026-11-03", "2031-01-01")])
    assert two == 20.0 and [d[1] for d in detail] == [10.0, 10.0]  # expiry-day tiers: no offset


def test_core_spread_charge():
    legs = [_leg("2026-10-27", 100, [0] * 16), _leg("2026-11-23", -60, [0] * 16)]
    spreads = [(1, 5.0, [("2026-10-27", "A", 1.0), ("2026-11-03", "B", 1.0)]),     # no delta in Nov 3: skipped
               (2, 7.0, [("2026-10-27", "A", 1.0), ("2026-11-23", "B", 1.0)])]
    charge, detail = core.spread_charge(legs, spreads)
    assert charge == 60 * 7.0 and detail == [(2, 60.0, 420.0)]
    same_side = [_leg("2026-10-27", 100, [0] * 16), _leg("2026-11-23", 60, [0] * 16)]
    assert core.spread_charge(same_side, spreads)[0] == 0.0


def test_core_net_option_value():
    short = _leg("2026-11-03", -10, [-5.0] * 16, instrument="OPT", price=3.0)    # short loses 50
    assert core.cc_risk([short], [], [], 0.0, True).risk == 50.0 + 30.0              # risk + short value
    assert core.cc_risk([short], [], [], 0.0, False).risk == 50.0
    long = _leg("2026-11-03", 10, [2.0] * 16, instrument="OPT", price=3.0)
    assert core.cc_risk([long], [], [], 0.0, True).risk == 0.0                          # long only: 0


# ---- 2. rules on real files ----------------------------------------------------------
NIFTY_OCT = {"symbol": "NIFTY", "expiry": "2026-10-27"}
NIFTY_NOV = {"symbol": "NIFTY", "expiry": "2026-11-23"}


def test_as_of_is_the_loaded_cycle():
    m = eng().margin([Position("NIFTY26OCTFUT", -65)])
    assert m["as_of"]["span_cycle"] == "s" and m["as_of"]["span_created"] == "2026-10-23 16:54"
    assert m["as_of"]["margin_date"] == "2026-10-23"


def test_short_future():
    e = eng()
    risk, price, _, lot = ra(e, **NIFTY_OCT)
    m = e.margin([Position(NIFTY_OCT, -lot)])
    close(m["span"], lot * max(-x for x in risk))                  # 65 x 2094.29
    close(m["exposure"], lot * price * 0.02)                       # index futures 2%
    close(m["total"], m["span"] + m["exposure"])


def test_calendar_spread_futures():
    e = eng()
    r1, _, _, lot = ra(e, **NIFTY_OCT)
    r2, p2, _, _ = ra(e, **NIFTY_NOV)
    rate = e.conn.execute("""SELECT s.rate FROM spread s
        JOIN spread_leg a ON a.symbol = s.symbol AND a.spread_no = s.spread_no AND a.expiry = '2026-10-27'
        JOIN spread_leg b ON b.symbol = s.symbol AND b.spread_no = s.spread_no AND b.expiry = '2026-11-23'
        WHERE s.symbol = 'NIFTY' ORDER BY s.spread_no LIMIT 1""").fetchone()[0]
    m = e.margin([Position(NIFTY_OCT, lot), Position(NIFTY_NOV, -lot)])
    u = m["underlyings"]["NIFTY"]
    close(u["scan_risk"], lot * max(a - b for a, b in zip(r1, r2)))
    close(u["spread_charge"], lot * rate)                          # 65 x 399
    close(m["exposure"], lot * p2 * 0.02 / 3, 0.1)                 # 1/3 of far leg only
    assert m["benefit"] > 0


def test_short_and_long_option():
    e = eng()
    k = {"symbol": "NIFTY", "expiry": "2026-11-03", "strike": 22600, "option_type": "C"}
    risk, price, _, lot = ra(e, **k)
    prev = e.conn.execute("SELECT prev_close FROM underlying WHERE symbol = 'NIFTY'").fetchone()[0]
    assert e.rules["span_less_net_option_value"] == 1             # default: standard SPAN
    m = e.margin([Position(k, -lot)])
    close(m["span"], lot * max(-x for x in risk) + lot * price)    # short: risk + its premium
    close(m["exposure"], lot * prev * 0.02)                        # on the previous close, not strike
    close(m["net_premium"], -lot * price)
    m = e.margin([Position(k, lot)])                               # long: option value covers the risk
    assert m["span"] == 0 and m["exposure"] == 0 and m["net_premium"] > 0
    e.rules["span_less_net_option_value"] = 0                      # the switch: raw scan risk
    close(e.margin([Position(k, -lot)])["span"], lot * max(-x for x in risk))
    m = e.margin([Position(k, lot)])                               # long: SPAN = its worst loss
    close(m["span"], lot * max(risk))
    assert m["span"] <= lot * price + 0.01
    e.rules["span_less_net_option_value"] = 1


def test_short_option_minimum():
    """SPAN = max(scan + spread - net option value, SOM). NSE's file has SOM rate 0, so
    a far-OTM short gets its (tiny) scan risk plus its premium; with a SOM rate the
    minimum takes over."""
    e = eng()
    k = {"symbol": "NIFTY", "expiry": "2026-11-03", "strike": 26000, "option_type": "C"}
    risk, price, _, lot = ra(e, **k)
    u = e._underlying("NIFTY")
    assert u["som_rate"] == 0.0 and u["som_method"] == "GROSS"
    m = e.margin([Position(k, -lot)])
    scan = lot * max(max(-x for x in risk), 0) + lot * price
    close(m["span"], scan)
    assert m["underlyings"]["NIFTY"]["short_option_minimum"] == 0
    u["som_rate"] = 50.0                                           # pretend the file set 50 per unit
    try:
        m = e.margin([Position(k, -lot)])
        assert m["underlyings"]["NIFTY"]["short_option_minimum"] == 50.0 * lot
        close(m["span"], max(scan, 50.0 * lot))
        hedge = dict(k, strike=26500)                              # GROSS: longs don't reduce it
        assert e.margin([Position(k, -lot), Position(hedge, lot)])["underlyings"]["NIFTY"][
            "short_option_minimum"] == 50.0 * lot
        u["som_method"] = "NET"
        assert e.margin([Position(k, -lot), Position(hedge, lot)])["underlyings"]["NIFTY"][
            "short_option_minimum"] == 0
    finally:
        e._cc.clear()


def test_hedged_spread_cheaper_than_naked():
    e = eng()
    sell = {"symbol": "NIFTY", "expiry": "2026-11-03", "strike": 22600, "option_type": "C"}
    buy = dict(sell, strike=22800)
    naked = e.margin([Position(sell, -65)])
    hedged = e.margin([Position(sell, -65), Position(buy, 65)])
    assert hedged["total"] < naked["total"] and hedged["benefit"] > 0
    assert hedged["exposure"] == naked["exposure"]                 # only the short leg pays ELM
    assert hedged["span"] <= 200 * 65                              # never more than the max loss
    assert hedged["benefit"] == round(naked["total"] + e.margin([Position(buy, 65)])["total"] - hedged["total"], 2)


def test_deep_otm_stock_short():
    e = eng()
    base = {"symbol": "RELIANCE", "expiry": "2026-11-23", "option_type": "C"}
    _, _, _, lot = ra(e, **base, strike=1300)
    near = e.margin([Position({**base, "strike": 1300}, -lot)])["legs"][0]
    deep = e.margin([Position({**base, "strike": 1600}, -lot)])     # > 1180.43 x 1.30
    assert near["elm_pct"] == 3.5 and deep["legs"][0]["elm_pct"] == 5.25
    assert "deep OTM" in deep["legs"][0]["elm_basis"]
    # 22 Oct's close: the 22 Oct F&O bhavcopy if on disk, else 23 Oct's i1 spot (the same 1,180.43)
    ref = deep["underlyings"]["RELIANCE"]["reference"]
    assert (ref["price"], ref["date"], ref["stale"]) == (1180.43, "2026-10-22", False), ref
    assert ref["source"] in ("fo_bhavcopy", "span_i1"), ref
    assert not deep["warnings"]


class fixed_reference:
    """Pin an underlying's previous close (and staleness) in the engine's cache."""
    def __init__(self, e, symbol, price, stale=False, spot=None):
        self.u, self.new = e._underlying(symbol), {"prev_close": price, "prev_close_source": "fo_bhavcopy",
                                                   "prev_close_date": "2026-10-22", "prev_close_stale": int(stale)}
        if spot is not None:
            self.new["spot_price"] = spot          # the cycle's spot, moved away from the close

    def __enter__(self):
        self.old = {k: self.u.get(k) for k in self.new}
        self.u.update(self.new)

    def __exit__(self, *a):
        self.u.update(self.old)


def test_deep_otm_thresholds():
    """Deep OTM is strictly beyond the cutoff: a strike exactly at close x 1.3 (call)
    or x 0.7 (put) is a normal short option; the next strike out is deep OTM. Index:
    x 1.1 / x 0.9 (25000 x 1.1 is 27500.000000000004 in floating point: still 27500)."""
    e = eng()
    cases = [("RELIANCE", "2026-11-23", 1000, [("C", 1300, False), ("C", 1320, True),
                                                ("P", 700, False), ("P", 680, True)]),
             ("NIFTY", "2026-11-03", 25000, [("C", 27500, False), ("C", 27550, True),
                                             ("P", 22500, False), ("P", 22450, True)])]
    for sym, expiry, close_, legs in cases:
        oth, otm = (e._underlying(sym)["elm"][k] for k in ("OTH", "OTM"))
        with fixed_reference(e, sym, close_):
            for ot, strike, deep in legs:
                k = {"symbol": sym, "expiry": expiry, "option_type": ot, "strike": strike}
                lot = ra(e, **k)[3]
                lg = e.margin([Position(k, -lot)])["legs"][0]
                assert lg["elm_pct"] == (otm if deep else oth), (sym, ot, strike, lg["elm_pct"], lg["elm_basis"])
                assert ("deep OTM" in lg["elm_basis"]) == deep, (sym, ot, strike, lg["elm_basis"])
                long_ = e.margin([Position(k, lot)])["legs"][0]
                assert long_["exposure"] == 0                       # longs never pay ELM


# Angel One, 27 Oct 2026 (matched i1): short calls, exposure = 3.5% x previous close x qty,
# whether the stock was up or down on the day. (symbol, strike, qty, previous close, spot, Angel One)
ANGEL_EXPOSURE_27OCT = [("RELIANCE", 1200, 500, 1201.80, 1192.80, 21031.50),   # down
                        ("MARUTI", 11300, 50, 11219.80, 11363.00, 19634.65),    # up
                        ("CDSL", 1220, 475, 1196.00, 1215.10, 19883.50),        # up
                        ("HDFCBANK", 620, 650, 688.05, 680.75, 15653.14)]       # down


def test_option_exposure_on_previous_close():
    """Short option exposure = rate x previous close x units, never the cycle's spot
    (rule short_option_elm_on_previous_close); futures stay on the futures price."""
    e = eng()
    for sym, strike, qty, close_, spot, angel in ANGEL_EXPOSURE_27OCT:
        k = {"symbol": sym, "expiry": "2026-11-23", "option_type": "C", "strike": strike}
        with fixed_reference(e, sym, close_, spot=spot):
            m = e.margin([Position(k, -qty)])
            lg = m["legs"][0]
            assert m["exposure"] == angel, (sym, m["exposure"], angel, lg["elm_basis"])    # to the paisa
            assert lg["elm_pct"] == 3.5 and lg["elm_notional"] == round(qty * close_, 2)
            e.rules["short_option_elm_on_previous_close"] = 0                                # old rule: the spot
            assert e.margin([Position(k, -qty)])["exposure"] == round(0.035 * spot * qty, 2)
            e.rules["short_option_elm_on_previous_close"] = 1
    fut = {"symbol": "RELIANCE", "expiry": "2026-11-23"}
    price = ra(e, **fut)[1]
    with fixed_reference(e, "RELIANCE", 1000.0, spot=1500.0):                              # neither is used
        m = e.margin([Position(fut, -500)])
        assert m["exposure"] == round(0.035 * price * 500, 2), (m["exposure"], price)


# The same four, end to end on the real 27 Oct start-of-day file (i1, made 26 Oct 21:33) with
# 26 Oct's F&O closes: SPAN, exposure and premium as Angel One / Zerodha showed them all day.
# (symbol, strike, qty, SPAN, exposure, premium received)
BOD_27OCT = [("RELIANCE", 1200, 500, 96310.00, 21031.50, 24400.00),
             ("MARUTI", 11300, 50, 83974.00, 19634.65, 19662.50),
             ("CDSL", 1220, 475, 97251.50, 19883.50, 18335.00),
             ("HDFCBANK", 620, 650, 108979.00, 15653.14, 46358.00)]
D27 = date(2026, 10, 27)
HAVE_27 = (DATA / "NFO/span/2026-10-27/nsccl.20261027.i01.spn").exists() and \
    (DATA / "NFO/bhavcopy/2026-10-26/BhavCopy_NSE_FO_0_0_0_20261026_F_0000.csv").exists()


def eng27() -> Engine:
    if "e27" not in _eng:
        load_segment("NFO", DATA, _TMP / "db27", D27, log=quiet, basis="bod")
        _eng["e27"] = Engine(_TMP / "db27" / "NFO.sqlite")
    return _eng["e27"]


def test_bod_27oct_four_cases():
    """Start-of-day basis: the four 27 Oct cases to the paisa; exposure stays on the
    previous close when the intraday spot is above (MARUTI, CDSL) or below (RELIANCE,
    HDFCBANK) it; a future's exposure stays on the futures price."""
    if not HAVE_27:
        print("    skip: 27 Oct i1 / 26 Oct F&O bhavcopy not on disk")
        return
    e = eng27()
    assert (e.info["basis"], e.info["trade_date"], e.info["span_cycle"], e.info["bod_stale"]) == \
        ("bod", "2026-10-27", "i1", "0")
    intraday = {"RELIANCE": 1192.80, "MARUTI": 11363.00, "CDSL": 1215.10, "HDFCBANK": 680.75}
    for sym, strike, qty, span, exposure, premium in BOD_27OCT:
        k = {"symbol": sym, "expiry": "2026-11-23", "option_type": "C", "strike": strike}
        m = e.margin([Position(k, -qty)])
        got = (m["span"], m["exposure"], -m["net_premium"])
        assert got == (span, exposure, premium), (sym, got, (span, exposure, premium))
        assert m["underlyings"][sym]["reference"]["source"] == "fo_bhavcopy"
        u = e._underlying(sym)
        old, u["spot_price"] = u["spot_price"], intraday[sym]          # the stock moved during the day
        try:
            assert e.margin([Position(k, -qty)])["exposure"] == exposure, sym
        finally:
            u["spot_price"] = old
    fut = {"symbol": "RELIANCE", "expiry": "2026-11-23"}
    price = ra(e, **fut)[1]
    m = e.margin([Position(fut, -500)])
    assert m["exposure"] == round(0.035 * price * 500, 2), (m["exposure"], price)


# Stocks with "Additional ELM% for Trade Date" in the 27 Oct ael file: exposure must use the
# total rate (a parser that dropped that column would give 3.5% and fail here).
# (symbol, strike, qty, previous close, normal %, additional %, total %, exposure)
ADDITIONAL_ELM_27OCT = [("SAIL", 175, 4700, 169.81, 3.5, 15.0, 18.5, 147649.80),         # F&O ban, +15%
                        ("APLAPOLLO", 2100, 350, 2077.90, 3.5, 5.25, 8.75, 63635.69)]    # +5.25%


def test_additional_elm_in_exposure():
    if not HAVE_27:
        print("    skip: 27 Oct i1 / 26 Oct F&O bhavcopy not on disk")
        return
    e = eng27()
    for sym, strike, qty, close_, normal, extra, total, exposure in ADDITIONAL_ELM_27OCT:
        row = e.conn.execute("SELECT normal_pct, additional_pct, total_pct FROM elm_rate WHERE symbol = ? "
                             "AND applies_to = 'OTH'", (sym,)).fetchone()
        assert tuple(row) == (normal, extra, total), (sym, tuple(row))
        k = {"symbol": sym, "expiry": "2026-11-23", "option_type": "C", "strike": strike}
        m = e.margin([Position(k, -qty)])
        lg = m["legs"][0]
        assert e.reference(sym)["price"] == close_
        assert (lg["elm_pct"], m["exposure"]) == (total, exposure), (sym, lg["elm_pct"], m["exposure"], exposure)
    assert e._underlying("SAIL")["is_banned"] == 1                  # on 27 Oct's ban list


def test_stale_reference_warns():
    """COM-005: no close for the previous trading day -> still calculated, with a
    warning naming the close used and its date; only when a short option needs it."""
    e = eng()
    k = {"symbol": "RELIANCE", "expiry": "2026-11-23", "option_type": "C", "strike": 1600}
    lot = ra(e, **k)[3]
    with fixed_reference(e, "RELIANCE", 1150.0, stale=True):
        m = e.margin([Position(k, -lot)])
        assert m["total"] > 0 and m["underlyings"]["RELIANCE"]["reference"]["stale"]
        w = [x for x in m["warnings"] if "previous trading day" in x]
        assert len(w) == 1 and "1150" in w[0] and "2026-10-22" in w[0], m["warnings"]
        assert not e.margin([Position(k, lot)])["warnings"]                       # a long doesn't use it
        fut = {"symbol": "RELIANCE", "expiry": "2026-11-23"}
        assert not e.margin([Position(fut, -lot)])["warnings"]


def test_expiry_day_rules():
    e = eng()
    k = {**NIFTY_OCT, "strike": 22600, "option_type": "C"}
    assert e.margin([Position(k, -65)], as_of="2026-10-26")["legs"][0]["elm_pct"] == 2.0
    assert e.margin([Position(k, -65)], as_of="2026-10-27")["legs"][0]["elm_pct"] == 4.0
    # calendar with the near leg expiring today: no 1/3 treatment, both legs pay in full
    m = e.margin([Position(NIFTY_OCT, 65), Position(NIFTY_NOV, -65)], as_of="2026-10-27")
    _, p1, _, _ = ra(e, **NIFTY_OCT)
    _, p2, _, _ = ra(e, **NIFTY_NOV)
    close(m["exposure"], 65 * (p1 + p2) * 0.02, 0.1)


def test_expiry_day_27oct_with_previous_master():
    """27 Oct 2026, a NIFTY expiry, loaded with 26 Oct's master (made for 27 Oct) even
    though 27 Oct's (made for 28 Oct, the day's expiries deleted) is on disk. The live
    run of NFO-019 / NFO-020 on 3 Nov tests the same two things against brokers."""
    if not HAVE_27:
        print("    skip: 27 Oct i1 / 26 Oct F&O bhavcopy not on disk")
        return
    e = eng27()
    assert (e.info["contract_master_file"], e.info["master_pairing"], e.info["master_stale"]) == \
        ("NSE_FO_contract_26102026.csv", "previous", "0"), e.info
    close_ = e.reference("NIFTY")["price"]
    # NFO-019: a short NIFTY 27 Oct option is an NSE contract, ELM 2% + 2% on its expiry day
    k = {"symbol": "NIFTY", "expiry": "2026-10-27", "option_type": "C", "strike": 22750}
    m = e.margin([Position(k, -65)])
    lg = m["legs"][0]
    assert (lg["elm_pct"], lg["elm_basis"]) == (4.0, "short option, expiry day"), lg
    assert m["exposure"] == round(0.04 * close_ * 65, 2), (m["exposure"], close_)
    # NFO-020: 27 Oct / 23 Nov futures calendar, no calendar benefit on the near leg's expiry day
    near, far = {"symbol": "NIFTY", "expiry": "2026-10-27"}, {"symbol": "NIFTY", "expiry": "2026-11-23"}
    m = e.margin([Position(near, 65), Position(far, -65)])
    alone = [e.margin([p]) for p in (Position(near, 65), Position(far, -65))]
    assert m["benefit"] == 0, m["benefit"]
    for f in ("span", "exposure", "total"):
        close(m[f], sum(a[f] for a in alone), 0.01)
    _, p1, _, _ = ra(e, **near)
    _, p2, _, _ = ra(e, **far)
    close(m["exposure"], 65 * (p1 + p2) * 0.02, 0.1)                # full ELM on both legs


def test_final_file_27oct_keeps_expiring_contracts():
    """27 Oct's s file (made after the close) leaves out the day's 34,017 expired
    contracts: the load passes, and they stay listed (no risk data, not 'BSE')."""
    if not (DATA / "NFO/span/2026-10-27/nsccl.20261027.s.spn").exists():
        print("    skip: 27 Oct s not on disk")
        return
    info = load_segment("NFO", DATA, _TMP / "db27s", D27, log=quiet, span_cycle="s")
    assert info["contract_master_file"] == "NSE_FO_contract_26102026.csv", info["contract_master_file"]
    assert info["master_without_span"] == 0 and info["expiring_without_span"] == 34017, info
    e = Engine(_TMP / "db27s" / "NFO.sqlite")
    try:
        e.margin([Position({"symbol": "NIFTY", "expiry": "2026-10-27", "option_type": "C", "strike": 22750}, -65)])
        raise AssertionError("an expired contract with no risk array was priced")
    except NoRiskData:
        pass
    assert e.margin([Position({"symbol": "NIFTY", "expiry": "2026-11-23"}, 65)])["total"] > 0


def test_no_risk_data_and_errors():
    e = eng()
    try:
        e.margin([Position({"symbol": "RVNL", "expiry": "2026-10-27", "strike": 90, "option_type": "P"}, -1925)])
        raise AssertionError("estimated a contract with no risk array")
    except NoRiskData as x:
        assert x.contracts == ["RVNL26OCT90PE"] and x.as_of["span_cycle"] == "s"
    try:
        e.margin([Position("NOSUCH26OCTFUT", 1)])
        raise AssertionError("unknown contract accepted")
    except UnknownContract:
        pass
    m = e.margin([Position("NIFTY26OCTFUT", 65), Position("NIFTY26OCTFUT", -65)])
    assert m["total"] == 0 and m["legs"] == []                     # identical contracts net out


def test_nse_only_lookup():
    """NSE's SPAN file carries BSE stock F&O under the same symbol (RELIANCE 29 Oct is
    BSE's expiry). Contracts are keyed by exchange too, and NFO lookups only resolve
    listed NSE contracts."""
    e = eng()
    ex = lambda exp: [r[0] for r in e.conn.execute(
        "SELECT DISTINCT exchange FROM contract WHERE symbol = 'RELIANCE' AND expiry = ?", (exp,))]
    assert ex("2026-10-27") == ["NSE"] and ex("2026-10-29") == ["BSE"]
    assert e.conn.execute("""SELECT COUNT(*) FROM contract WHERE symbol = 'RELIANCE' AND strike = 1200
                             AND option_type = 'C' AND expiry IN ('2026-10-27', '2026-10-29')""").fetchone()[0] == 2
    assert e.conn.execute("SELECT COUNT(DISTINCT exchange) FROM contract WHERE symbol = 'SENSEX'").fetchone()[0] == 1
    assert e.conn.execute("SELECT exchange FROM contract WHERE symbol = 'SENSEX' LIMIT 1").fetchone()[0] == "BSE"

    # one basket asks for both: the NSE leg resolves, the BSE one is refused
    nse = {"symbol": "RELIANCE", "expiry": "2026-10-27", "strike": 1200, "option_type": "CE"}
    bse = dict(nse, expiry="2026-10-29")
    m = e.margin([Position(nse, -500)])
    leg = m["legs"][0]
    assert (leg["exchange"], leg["contract"], leg["lots"]) == ("NSE", "RELIANCE26OCT1200CE", -1.0)   # sold: lots are signed
    for bad, why in ((bse, "BSE contract"), ({**nse, "exchange": "BSE"}, "can't margin a BSE"),
                     ({"symbol": "RELIANCE", "expiry": "2026-10-29"}, "BSE contract"),       # BSE future
                     ({**nse, "strike": 650}, "not listed for trading")):                    # SPAN-only strike
        try:
            e.margin([Position(nse, -500), Position(bad, -500)])
            raise AssertionError(f"resolved {bad}")
        except UnknownContract as x:
            assert why in str(x), (bad, x)
    assert e.margin([Position({"symbol": "RELIANCE", "expiry": "2026-10-27", "option_type": "FUT"}, 500)])[
        "legs"][0]["contract"] == "RELIANCE26OCTFUT"                       # FUT spelled out = future


def test_refresh_after_reload():
    e = eng()
    assert e.refresh() is False
    load_segment("NFO", DATA, _TMP / "db", D, log=quiet)
    assert e.refresh() is True and e.margin([Position("NIFTY26OCTFUT", 65)])["total"] > 0


# ---- 3. Broker reference baskets -----------------------------------------------------
LEG_FIELDS = ("symbol", "expiry", "type", "strike", "side", "qty")
FIGURES = ("span", "exposure", "total")
OPTIONAL = {"net_premium", "benefit"}
BROKERS = ("zerodha", "angelone", "dhan")
BASKETS = ["naked_short_index_call", "hedged_put_spread", "nifty_nov_dec_calendar",
           "deep_otm_reliance_short_call", "expiry_day_call_and_future", "long_index_call",
           "angelone_banknifty_short_future", "angelone_banknifty_future_and_call",
           "angelone_nifty_short_straddle"]


def _leg_filled(lg) -> bool:
    return all(lg.get(k) is not None for k in LEG_FIELDS if not (k == "strike" and lg.get("type") == "FUT"))


def _filled(b) -> bool:
    x = b.get("expected") or {}
    return bool(b.get("captured_at")) and all(x.get(k) is not None for k in FIGURES) \
        and all(_leg_filled(lg) for lg in b["legs"])


def _checked(b) -> tuple:
    """The basket figures compared: SPAN, exposure, total, plus net premium / benefit if given."""
    return FIGURES + tuple(k for k in sorted(OPTIONAL) if b["expected"].get(k) is not None)


def _position(lg) -> Position:
    """A fixture leg -> NSE Position (signed units)."""
    contract = {"exchange": "NSE", "symbol": lg["symbol"], "expiry": str(lg["expiry"])}
    if lg["type"] != "FUT":
        contract.update(strike=lg["strike"], option_type=lg["type"])
    return Position(contract, int(lg["qty"]) * (1 if lg["side"] == "buy" else -1))


def _captured(b) -> datetime:
    return datetime.strptime(str(b["captured_at"]), "%Y-%m-%d %H:%M")


def want_of(b) -> dict:
    """The broker's figures for engine.match: basket keys plus leg<N>.<key> per leg `expected`."""
    want = {k: float(b["expected"][k]) for k in _checked(b)}
    for i, lg in enumerate(b["legs"], 1):
        for k in FIGURES if lg.get("expected") else ():
            want[f"leg{i}.{k}"] = float(lg["expected"][k])
    return want


_cycle_dbs = None


def cycle_dbs() -> match_mod.CycleDatabases:
    global _cycle_dbs
    if _cycle_dbs is None:
        _cycle_dbs = match_mod.CycleDatabases(DATA, _TMP / "cycles", log=quiet)
    return _cycle_dbs


def match_basket(fx, b) -> match_mod.Match:
    """engine.match for a fixture basket, at the fixture's tolerance."""
    positions = [_position(lg) for lg in b["legs"]]
    dbs = cycle_dbs()
    cands = match_mod.candidate_cycles(DATA, _captured(b), b.get("span_cycle"))
    assert cands, f"{b['id']}: no SPAN file created by {b['captured_at']} in data/NFO/span/"
    for lg, pos in zip(b["legs"], positions):
        lot = dbs.engine(cands[0]).leg(pos).lot_size
        assert lot and int(lg["qty"]) % lot == 0, f"{b['id']}: qty {lg['qty']} is not whole lots of {lot}: {lg}"
    return match_mod.match(dbs, _captured(b), positions, want_of(b), fx.get("tolerance_pct", 0.05),
                           fx.get("tolerance_abs", 5), b.get("span_cycle"))


def test_fixture_file_shape():
    """The planned baskets, each leg fully specifiable, even before numbers exist, then
    any baskets brokercheck appended (source: brokercheck, unique ids)."""
    fx = yaml.safe_load(FIXTURES.read_text())
    planned = [b for b in fx["baskets"] if b.get("source") != "brokercheck"]
    assert [b["id"] for b in planned] == BASKETS
    ids = [b["id"] for b in fx["baskets"]]
    assert len(ids) == len(set(ids)), "duplicate basket ids"
    for b in fx["baskets"]:
        assert b["broker"] in BROKERS, b["id"]
        assert "captured_at" in b and set(FIGURES) <= set(b["expected"]) <= set(FIGURES) | OPTIONAL, b["id"]
        assert b.get("span_cycle") in (None, "i1", "i2", "i3", "i4", "i5", "s"), b["id"]
        for lg in b["legs"]:
            assert set(LEG_FIELDS) <= set(lg), (b["id"], lg)
            assert lg["type"] in ("CE", "PE", "FUT") and lg["side"] in ("buy", "sell", None), (b["id"], lg)
            assert lg["type"] != "FUT" or lg["strike"] is None, (b["id"], lg)
            assert set(lg.get("expected") or FIGURES) == set(FIGURES), (b["id"], lg)
            date.fromisoformat(str(lg["expiry"]))
    legs = {b["id"]: b["legs"] for b in fx["baskets"]}
    assert [lg["type"] for lg in legs["hedged_put_spread"]] == ["PE", "PE"]
    assert [lg["side"] for lg in legs["hedged_put_spread"]] == ["sell", "buy"]
    assert [str(lg["expiry"])[:7] for lg in legs["nifty_nov_dec_calendar"]] == ["2026-11", "2026-12"]
    assert str(legs["expiry_day_call_and_future"][0]["expiry"]) == "2026-10-27"
    assert legs["long_index_call"][0]["side"] == "buy" and legs["naked_short_index_call"][0]["side"] == "sell"
    if HAVE_FILES:                       # every prefilled expiry is a real NSE expiry for the symbol
        e = eng()
        for b in planned:
            for lg in b["legs"]:
                assert e.conn.execute("""SELECT 1 FROM contract WHERE exchange = 'NSE' AND in_master
                                         AND symbol = ? AND expiry = ? AND instrument = ?""",
                                      (lg["symbol"], str(lg["expiry"]),
                                       "FUT" if lg["type"] == "FUT" else "OPT")).fetchone(), (b["id"], lg)


def test_broker_baskets():
    """Each filled basket must match the cycle live at its capture time or an earlier
    one: brokers lag (Angel One keeps a file for hours). The cycle that matched is
    printed, so a lag is visible; matching none is an engine or rule error."""
    fx = yaml.safe_load(FIXTURES.read_text())
    pending, failures = [], []
    for b in fx["baskets"]:
        if not _filled(b):
            pending.append(b["id"])
            continue
        m = match_basket(fx, b)
        if m.hit:
            lag = f"  (live was {m.live}: {b['broker']} on an older file)" if m.lagged else ""
            print(f"    {b['id']:34} {b['broker']} matches SPAN {m.hit}{lag}")
            continue
        first = m.tried[0]
        print(f"    {b['id']:34} {b['broker']}: no cycle matches; live cycle {first.cycle}:")
        want = want_of(b)
        for k, w in want.items():
            ours = first.got[k] if first.got else float("nan")
            print(f"      {k:16} {b['broker']} {w:>12,.2f}  ours {ours:>12,.2f}  diff {ours - w:>+10,.2f}"
                  f"{'  MISMATCH' if k in first.bad else ''}")
        failures.append(f"{b['id']}: matches none of {len(m.tried)} cycles tried "
                        f"({', '.join(str(t.cycle) for t in m.tried)}): engine or rule error")
    if pending:
        print(f"    baskets pending (no numbers yet): {', '.join(pending)}")
    assert not failures, "\n".join(failures)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        if not HAVE_FILES and t.__name__ not in ("test_core_scan_tiers_do_not_offset", "test_core_spread_charge",
                                                 "test_core_net_option_value"):
            print(f"  skip {t.__name__} (no 23 Oct files)")
            continue
        t()
        print(f"  ok  {t.__name__}")
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"engine tests passed ({len(TESTS)})")
