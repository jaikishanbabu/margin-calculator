"""NSE cash: loader + engine, on the real 26 Oct 2026 files (VaR batches 1, 3, 6).

    python tests/test_engine_nse.py      or      python -m pytest tests -q

Expected values are read straight from the raw VaR / master / bhavcopy rows here,
not from the loader or engine.
"""
import csv
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import CashEngine, CashError, CashOrder, InvalidQuantity, NoPrice, UnknownSecurity  # noqa: E402
from loader import OlderThanLive, load_segment  # noqa: E402

DATA = ROOT / "data"
D = date(2026, 10, 26)
EQ = DATA / "EQ"
HAVE_FILES = (EQ / "var/2026-10-26/C_VAR1_26102026_3.DAT").exists() and \
    (EQ / "contract_master/2026-10-26/NSE_CM_security_26102026.csv").exists()
_TMP = Path(tempfile.mkdtemp(prefix="nse_test_"))
_eng = {}


def quiet(*a):
    pass


def eng(batch="3") -> CashEngine:
    if batch not in _eng:
        info = load_segment("NSE", DATA, _TMP / f"b{batch}", D, log=quiet, span_cycle=batch)
        _eng[batch] = CashEngine(_TMP / f"b{batch}" / "NSE.sqlite")
        _eng[batch].load_info = info
    return _eng[batch]


def raw_var(batch=3) -> dict:
    return {r[3]: r for r in csv.reader(open(EQ / f"var/2026-10-26/C_VAR1_26102026_{batch}.DAT")) if r and r[0] == "20"}


def raw_master() -> list:
    return list(csv.DictReader(open(EQ / "contract_master/2026-10-26/NSE_CM_security_26102026.csv", errors="replace")))


def raw_bhav() -> dict:
    return {r["FinInstrmId"]: r for r in csv.DictReader(open(EQ / "bhavcopy/2026-10-26/BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv"))}


def close(a, b, tol=0.011):
    assert abs(a - b) <= tol, (a, b)


# ---- loader ------------------------------------------------------------------------
def test_load_info_and_batches():
    info = eng("3").load_info
    assert (info["var_date"], info["var_batch"]) == ("2026-10-26", 3)
    # var_fetched_at is when the file was downloaded - for archive downloads that's long after 26 Oct
    # (approved change, see validation/VALIDATION_CHANGES.md)
    assert info["var_amr_mismatches"] == 0 and re.match(r"\d{4}-\d{2}-\d{2}T", info["var_fetched_at"])
    assert info["var_dropped_six_digit"] > 8000               # BSE scrip codes in the interoperable file
    # batch 1's header has no batch field: taken from the file name
    assert eng("1").load_info["var_batch"] == 1 and eng("6").load_info["var_batch"] == 6
    # a load never replaces newer data
    try:
        load_segment("NSE", DATA, _TMP / "b6", D, log=quiet, span_cycle="3")
        raise AssertionError("batch 3 replaced batch 6")
    except OlderThanLive:
        pass


def test_joins_by_isin_and_token():
    e = eng()
    master = [r for r in raw_master() if r["TckrSymb"] == "RELIANCE" and r["DelFlg"] == "N"]
    assert len(master) > 1                                    # AF, BE, BL, EQ ... rows share one ISIN
    rows = e.conn.execute("SELECT * FROM security WHERE symbol = 'RELIANCE'").fetchall()
    assert len(rows) == 1 and rows[0]["series"] == "EQ" and rows[0]["is_primary"]
    eqrow = next(r for r in master if r["SctySrs"] == "EQ")
    v = raw_var()[eqrow["ISIN"]]
    b = raw_bhav()[eqrow["FinInstrmId"]]
    r = rows[0]
    assert r["token"] == int(eqrow["FinInstrmId"]) and r["isin"] == eqrow["ISIN"]
    assert (r["var_pct"], r["elm_pct"], r["adhoc_pct"], r["amr"]) == tuple(float(x) for x in v[6:10])
    assert r["close"] == float(b["ClsPric"]) and r["prev_close"] == float(b["PrvsClsgPric"])
    assert r["tick"] == float(eqrow["BidIntrvl"]) / 100 and r["lot"] == 1
    assert (r["band_low"], r["band_high"]) == tuple(float(x) for x in eqrow["PricRg"].split("-"))


def test_close_not_settlement_price():
    e, bhav = eng(), raw_bhav()
    diff = [(t, r) for t, r in bhav.items() if r["Sgmt"] == "CM" and r["SctySrs"] == "EQ"
            and r["ClsPric"] != r["SttlmPric"] and r["SttlmPric"]]
    assert diff
    for t, r in diff[:20]:
        row = e.conn.execute("SELECT close FROM security WHERE token = ?", (int(t),)).fetchone()
        if row:
            assert row["close"] == float(r["ClsPric"]), (t, row["close"], r["ClsPric"], r["SttlmPric"])


def test_drops_and_kept_rows():
    e = eng()
    n = lambda q, *a: e.conn.execute(q, a).fetchone()[0]
    assert n("SELECT COUNT(*) FROM security WHERE symbol LIKE '%NSETEST%' OR isin LIKE 'DUMMY%'") == 0
    assert n("SELECT COUNT(*) FROM security WHERE symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'") == 0
    assert n("SELECT COUNT(*) FROM security WHERE symbol = 'AASHRIT'") == 0          # BSE-exclusive
    assert n("SELECT COUNT(*) FROM security WHERE series NOT IN ('EQ','BE','BZ','SM','ST','SZ')") == 0
    assert n("SELECT COUNT(*) FROM (SELECT symbol FROM security WHERE is_primary GROUP BY symbol HAVING COUNT(*) > 1)") == 0
    # SME lot from the master; KHAZANCHI (moving SM -> EQ) kept without a VaR row on batch 3
    assert n("SELECT lot FROM security WHERE symbol = 'AAKAAR'") == 1600
    assert n("SELECT COUNT(*) FROM security WHERE symbol = 'KHAZANCHI' AND amr IS NULL AND series = 'EQ'") == 1


# ---- engine ------------------------------------------------------------------------
def test_large_cap_intraday_and_delivery():
    e = eng()
    r = e.security("RELIANCE")
    m = e.margin([CashOrder("RELIANCE", "buy", 100)])
    leg = m["legs"][0]
    assert leg["price_source"] == "close" and leg["price"] == r["close"]
    close(leg["nse_margin"], 100 * r["close"] * r["amr"] / 100)                # NSE's 12.5%
    close(leg["intraday_margin"], 100 * r["close"] * max(20, r["amr"]) / 100)   # broker minimum 20%
    close(leg["leverage"], 100 / max(20, r["amr"]), 0.006)
    assert leg["delivery_value"] == leg["value"] == round(100 * r["close"], 2) and not leg["warnings"]
    assert m["as_of"]["var_batch"] == 3 and m["as_of"]["close_date"] == "2026-10-26"
    s = e.margin([CashOrder("RELIANCE", "sell", 100, price=1200)])["legs"][0]
    assert s["delivery_value"] == 0 and "holdings" in s["delivery_note"] and s["intraday_margin"] == 24000.0


def test_t2t_asm_gsm_and_no_var():
    e = eng()
    be = e.margin([CashOrder("AARTISURF", "buy", 100)])["legs"][0]
    assert be["effective_pct"] == 100 and be["leverage"] == 1 and any("trade-for-trade" in w for w in be["warnings"])
    # NSE publishes only its current ASM / GSM lists, so October's can't be downloaded: the rules are checked
    # on stocks that are on the lists loaded (approved change, see validation/VALIDATION_CHANGES.md;
    # was ADVANCE for short-term ASM and AQYLON for GSM)
    def listed(col, extra=""):
        r = e.conn.execute(f"SELECT symbol FROM security WHERE {col} IS NOT NULL {extra} AND amr < 100 AND close > 0 "
                           "AND series = 'EQ' AND is_primary AND gsm IS NULL AND series NOT IN ('BE', 'BZ') ORDER BY symbol").fetchone()
        return r[0] if r else None
    st = listed("asm_short", "AND asm_long IS NULL")
    if st:
        adv = e.security(st)                                    # short-term ASM
        rule = max(1.5 * (adv["var_pct"] + adv["elm_pct"]), 40)     # of VaR + ELM: AMR's ad hoc is NSE's ASM add-on
        leg = e.margin([CashOrder(st, "buy", adv["lot"] or 1)])["legs"][0]
        assert leg["effective_pct"] == round(min(100, max(adv["amr"], rule)), 2) and any("short-term ASM" in w for w in leg["warnings"])
    lt = listed("asm_long")
    if lt:
        assert e.margin([CashOrder(lt, "buy", e.security(lt)["lot"] or 1)])["legs"][0]["effective_pct"] == 100
    gs = e.conn.execute("SELECT symbol FROM security WHERE gsm IS NOT NULL AND amr IS NOT NULL AND close > 0 "
                       "AND is_primary ORDER BY symbol").fetchone()
    if gs:
        g = e.margin([CashOrder(gs[0], "buy", e.security(gs[0])["lot"] or 1)])["legs"][0]
        assert any("GSM" in w for w in g["warnings"]) and g["effective_pct"] == e.security(gs[0])["amr"]
    assert st and lt and gs, "ASM / GSM lists not loaded"
    k = e.margin([CashOrder("KHAZANCHI", "buy", 100, price=700)])["legs"][0]
    assert k["effective_pct"] == 100 and k["amr_pct"] is None and any("no VaR rate" in w for w in k["warnings"])


def test_rejections():
    e = eng()
    for order, err in ((CashOrder("AAKAAR", "buy", 1601), InvalidQuantity), (CashOrder("RELIANCE", "buy", 0), InvalidQuantity),
                       (CashOrder("NOSUCHSTOCK", "buy", 1), UnknownSecurity), (CashOrder("RELIANCE", "hold", 1), CashError),
                       (CashOrder("KHAZANCHI", "buy", 100), NoPrice)):
        try:
            e.margin([order])
            raise AssertionError(f"accepted {order}")
        except err:
            pass
    assert e.margin([CashOrder("AAKAAR", "buy", 3200)])["legs"][0]["qty"] == 3200


def test_basket_adds_up_and_broker_minimum():
    e = eng()
    orders = [CashOrder("RELIANCE", "buy", 100), CashOrder("TCS", "buy", 50), CashOrder("INFY", "sell", 20)]
    m = e.margin(orders)
    alone = [e.margin([o])["legs"][0] for o in orders]
    close(m["total"]["intraday_margin"], sum(l["intraday_margin"] for l in alone))
    close(m["total"]["delivery_value"], alone[0]["value"] + alone[1]["value"])
    assert e.rules["broker_min_intraday_pct"] == 20                         # on, verified (Zerodha, Dhan)
    leg = e.margin([CashOrder("RELIANCE", "buy", 100)])["legs"][0]
    assert leg["intraday_pct"] == 20 and leg["leverage"] == 5 and leg["effective_pct"] == 12.5
    e.rules["broker_min_intraday_pct"] = 0                                  # the switch: NSE's rate only
    try:
        leg = e.margin([CashOrder("RELIANCE", "buy", 100)])["legs"][0]
        assert leg["intraday_pct"] == 12.5 and leg["leverage"] == 8 and leg["nse_margin"] == leg["intraday_margin"]
    finally:
        e.rules["broker_min_intraday_pct"] = 20


def test_corporate_action_warning():
    e = eng()
    row = e.conn.execute("""SELECT symbol, lot FROM security WHERE close > 0 AND prev_close > 0 AND is_primary
                            AND ABS(close / prev_close - 1) > 0.2 LIMIT 1""").fetchone()
    if row:
        w = e.margin([CashOrder(row[0], "buy", row[1])])["legs"][0]["warnings"]
        assert any("corporate action" in x for x in w), w


def test_refresh_after_reload():
    folder = _TMP / "swap"
    load_segment("NSE", DATA, folder, D, log=quiet, span_cycle="1")
    e = CashEngine(folder / "NSE.sqlite")
    assert e.refresh() is False and e.as_of["var_batch"] == 1
    load_segment("NSE", DATA, folder, D, log=quiet, span_cycle="6")
    assert e.refresh() is True and e.as_of["var_batch"] == 6


def test_broker_baskets():
    """Dhan baskets brokercheck recorded (tests/fixtures/nse_baskets.yaml): each must
    match a VaR batch live at its capture time or earlier, at the prices sent to Dhan
    (intraday with the 20% broker minimum)."""
    import yaml
    from datetime import datetime
    from engine.match import CycleDatabases, match
    path = ROOT / "tests/fixtures/nse_baskets.yaml"
    if not path.exists():
        return
    dbs = CycleDatabases(DATA, _TMP / "cycles", segment="NSE")
    failures = []
    for b in yaml.safe_load(path.read_text())["baskets"]:
        orders = [CashOrder(o["symbol"], o["side"], o["qty"], o["price"]) for o in b["orders"]]
        want = dict(b["expected"])
        for i, o in enumerate(b["orders"], 1):
            want.update({f"leg{i}.{k}": v for k, v in o["expected"].items()})
        m = match(dbs, datetime.strptime(str(b["captured_at"]), "%Y-%m-%d %H:%M"), orders, want)
        if m.hit:
            print(f"    {b['id']:34} {b['broker']} matches VaR batch {m.hit}")
        else:
            failures.append(f"{b['id']}: no VaR batch matches ({len(m.tried)} tried)")
    assert not failures, "\n".join(failures)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        if not HAVE_FILES:
            print(f"  skip {t.__name__} (no 26 Oct NSE cash files)")
            continue
        t()
        print(f"  ok  {t.__name__}")
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"nse cash tests passed ({len(TESTS)})")
