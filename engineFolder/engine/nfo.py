"""NSE F&O margin for a basket of positions, from db/NFO.sqlite.

    eng = Engine("db/NFO.sqlite")
    m = eng.margin([Position("NIFTY26N0322600CE", -65),
                    Position({"symbol": "NIFTY", "expiry": "2026-11-03", "strike": 23000, "option_type": "C"}, 65)])
    m["span"], m["exposure"], m["total"], m["as_of"]

Total = SPAN + exposure margin (ELM), the two figures Zerodha's calculator shows.

SPAN    per underlying, from the SPAN cycle in the database (see engine/span.py),
        less net option value (rule span_less_net_option_value, default 1 =
        standard SPAN; 0 = raw scan risk).
ELM     per leg, % of notional:
          futures         units x futures price x OTH rate; calendar spreads pay
                          on one third of the far leg (not on an expiring future
                          on its expiry day)
          short options   units x previous close x rate (rule short_option_elm_on_
                          previous_close; 0 = the cycle's spot), where rate is OTH, or
                          OTM if deep out of the money vs the previous close
                          (strike > close x 1.3 for a call, < close x 0.7 for a
                          put; 10% for indices). The close is the previous trading
                          day's (loader: F&O bhavcopy, cash bhavcopy, i1 spot);
                          anything older is used but warned about (stale);
                          index: 5% beyond 9 months, +2% on expiry day
          long options    none
        Futures use the SPAN cycle's futures price; short options the previous close.
Contracts with no risk array in the loaded SPAN cycle raise NoRiskData: the
engine never estimates them.

Contracts are identified by exchange + symbol + expiry + strike + option type.
The NSE SPAN file also holds BSE contracts under the same symbols (NSE Clearing
clears both), so lookups only resolve NSE contracts that are listed: in NSE's
contract master, or given a lot size from fo_mktlots.csv when the master is stale.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import span as spanmod


class UnknownContract(LookupError):
    pass


class NoRiskData(LookupError):
    """Listed contracts the loaded SPAN cycle has no risk array for yet."""

    def __init__(self, contracts: list[str], as_of: dict):
        self.contracts, self.as_of = contracts, as_of
        super().__init__(f"no risk data yet for {', '.join(contracts)} "
                         f"(SPAN {as_of.get('span_cycle')} of {as_of.get('span_created')})")


@dataclass
class Position:
    contract: str | dict     # tradingsymbol, or {symbol, expiry, strike, option_type CE|PE}; no strike = future;
                             # optional "exchange" (must be the engine's, NSE)
    qty: int                 # units (lots x lot size): + buy, - sell


@dataclass
class Leg:
    id: int
    exchange: str
    symbol: str
    instrument: str
    expiry: str
    strike: float | None
    option_type: str | None
    multiplier: float
    lot_size: int | None
    price: float
    risk: tuple
    composite_delta: float
    units: int
    label: str
    elm_pct: float | None = None     # the contract's own final ELM % (BFO: ICCL file); None = the rules
    und_price: float | None = None   # MCX option: its future's price (the exposure base)
    extra_pct: tuple = (0.0, 0.0, 0.0)   # MCX future: additional+special (long, short) and tender %


def _months_after(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            pass


LISTED = "(in_master OR lot_source = 'market_lots')"


class Engine:
    exchange = "NSE"

    def __init__(self, path: str | Path = "db/NFO.sqlite"):
        self.path = Path(path)
        self.conn = None
        self.refresh(force=True)

    # ---- database -----------------------------------------------------------------
    def refresh(self, force=False) -> bool:
        """Reopen if the loader swapped in a new database. Returns True if reopened."""
        ino = os.stat(self.path).st_ino
        if not force and ino == self._ino:
            return False
        if self.conn:
            self.conn.close()
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ino = ino
        self.info = dict(self.conn.execute("SELECT key, value FROM load_info"))
        self.rules = {r["name"]: r["value"] for r in self.conn.execute("SELECT name, value FROM rule")}
        # inter-commodity spreads (MCX): credits not applied yet, so a basket that holds two linked
        # commodities is margined higher than with them; margin() warns (never lower)
        inter: dict = {}
        for no, sym in self.conn.execute("SELECT spread_no, symbol FROM inter_spread_leg"):
            inter.setdefault(no, set()).add(sym)
        self._inter = [s for s in inter.values() if len(s) > 1]
        self._cc = {}
        return True

    @property
    def as_of(self) -> dict:
        return {k: self.info.get(k) for k in ("span_cycle", "span_created", "span_business_date", "span_settlement",
                                               "basis", "trade_date")}

    def _row(self, contract) -> sqlite3.Row:
        c = self.conn
        if isinstance(contract, str):
            where, args = "tradingsymbol = ?", (contract,)
        else:
            k = dict(contract)
            if k.get("exchange", self.exchange) != self.exchange:
                raise UnknownContract(f"{self.exchange} engine can't margin a {k['exchange']} contract: {contract}")
            strike = k.get("strike")
            ot = (k.get("option_type") or "").upper()
            ot = None if ot in ("", "FUT", "XX", "F") else ot[:1]      # CE/PE/C/P; FUT = future
            instrument = "FUT" if ot is None else "OPT"
            if instrument == "OPT" and strike is None:
                raise UnknownContract(f"option without a strike: {contract}")
            where = ("symbol = ? AND expiry = ? AND instrument = ? AND IFNULL(option_type, '') = ? "
                     "AND IFNULL(strike, 0) = ?")
            args = (k["symbol"], str(k["expiry"]), instrument, ot or "",
                    float(strike) if instrument == "OPT" else 0)
        row = c.execute(f"SELECT * FROM contract WHERE exchange = ? AND {LISTED} AND {where}",
                        (self.exchange, *args)).fetchone()
        if row is None:
            other = c.execute(f"SELECT exchange, in_master FROM contract WHERE {where}", args).fetchall()
            if any(r["exchange"] != self.exchange for r in other):
                raise UnknownContract(f"{contract} is a {other[0]['exchange']} contract (cleared by NSE Clearing), "
                                      f"not an {self.exchange} one")
            if other:
                raise UnknownContract(f"{contract} is in the SPAN file but not listed for trading on {self.exchange}")
            raise UnknownContract(f"no such contract: {contract}")
        return row

    def leg(self, pos: Position) -> Leg:
        row = self._row(pos.contract)
        label = row["tradingsymbol"] or (f"{row['symbol']} {row['expiry']} "
                                         f"{'FUT' if row['instrument'] == 'FUT' else '%g%s' % (row['strike'], row['option_type'])}")
        ra = self.conn.execute("SELECT * FROM risk_array WHERE contract_id = ?", (row["id"],)).fetchone()
        if ra is None:
            return Leg(row["id"], row["exchange"], row["symbol"], row["instrument"], row["expiry"], row["strike"],
                       row["option_type"], 1.0, row["lot_size"], 0.0, (), 0.0, pos.qty, label)
        keys = row.keys()
        own = row["elm_pct"] if "elm_pct" in keys else None
        und = None
        if "und_cid" in keys and row["instrument"] == "OPT" and row["exchange"] == "MCX" and row["und_cid"]:
            f = self.conn.execute("SELECT span_price FROM contract WHERE id = ?", (row["und_cid"],)).fetchone()
            und = f[0] if f else None
        extra = tuple(float(row[k] or 0) for k in ("extra_long_pct", "extra_short_pct", "tender_pct")) \
            if "tender_pct" in keys else (0.0, 0.0, 0.0)
        return Leg(row["id"], row["exchange"], row["symbol"], row["instrument"], row["expiry"], row["strike"],
                   row["option_type"], row["multiplier"] or 1.0, row["lot_size"], row["span_price"],
                   tuple(ra[f"s{i}"] for i in range(1, 17)), ra["composite_delta"], pos.qty, label, own, und, extra)

    def _underlying(self, symbol: str) -> dict:
        if symbol not in self._cc:
            c = self.conn
            u = dict(c.execute("SELECT * FROM underlying WHERE symbol = ?", (symbol,)).fetchone())
            u["scan_tiers"] = [tuple(r) for r in c.execute(
                "SELECT tier_no, start_expiry, end_expiry FROM tier WHERE symbol = ? AND tier_type = 'scan' "
                "ORDER BY tier_no", (symbol,))]
            legs: dict = {}
            intra = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT tier_no, start_expiry, end_expiry FROM tier WHERE symbol = ? AND tier_type = 'intra' "
                "AND start_expiry IS NOT NULL", (symbol,))}
            cols = {r[1] for r in c.execute("PRAGMA table_info(spread_leg)")}
            tier_col = "tier" if "tier" in cols else "NULL"
            for r in c.execute(f"SELECT spread_no, expiry, side, ratio, {tier_col} FROM spread_leg WHERE symbol = ? "
                               "ORDER BY spread_no, leg_no", (symbol,)):
                key = intra.get(r[4]) if r[4] is not None else r[1]      # MCX tier leg -> its expiry range
                if key is not None:
                    legs.setdefault(r[0], []).append((key, r[2], r[3]))
            u["spreads"] = [(no, rate, legs.get(no, [])) for no, rate in c.execute(
                "SELECT spread_no, rate FROM spread WHERE symbol = ? ORDER BY spread_no", (symbol,))]
            u["elm"] = {r[0]: r[1] for r in c.execute(
                "SELECT applies_to, total_pct FROM elm_rate WHERE symbol = ?", (symbol,))}
            self._cc[symbol] = u
        return self._cc[symbol]

    # ---- margin ---------------------------------------------------------------------
    def margin(self, positions: list[Position], as_of: date | str | None = None, alone: bool = True) -> dict:
        """as_of: the trading day margins are for (default: the SPAN file's date).
        alone: also margin each leg on its own, to report the hedge benefit."""
        # default: the trade date the database is for (a start-of-day fallback file can be a day older)
        as_of = (as_of if isinstance(as_of, str) else as_of.isoformat()) if as_of else \
            (self.info.get("trade_date") or self.info["span_business_date"])
        # net identical contracts first
        net: dict = {}
        for p in positions:
            lg = self.leg(p)
            if lg.id in net:
                net[lg.id].units += lg.units
            else:
                net[lg.id] = lg
        legs = [lg for lg in net.values() if lg.units]
        missing = [lg.label for lg in legs if not lg.risk]
        if missing:
            raise NoRiskData(missing, self.as_of)
        expired = [lg.label for lg in legs if lg.expiry < as_of]
        if expired:
            raise UnknownContract(f"expired before {as_of}: {', '.join(expired)}")

        out = {"as_of": {**self.as_of, "margin_date": as_of}, "span": 0.0, "exposure": 0.0, "other": 0.0,
               "net_premium": 0.0, "underlyings": {}, "legs": [], "warnings": []}
        by_sym: dict = {}
        for lg in legs:
            by_sym.setdefault(lg.symbol, []).append(lg)
        for sym, group in by_sym.items():
            u = self._underlying(sym)
            r = spanmod.cc_risk(group, u["scan_tiers"], u["spreads"], u["som_rate"],
                                bool(self.rules.get("span_less_net_option_value", 1)), u["som_method"])
            elm_legs = self._exposure(group, u, as_of, out["warnings"])
            exp = sum(e["exposure"] for e in elm_legs)
            out["underlyings"][sym] = {
                "scan_risk": r.scan_risk, "tiers": r.tiers, "spread_charge": r.spread_charge,
                "spreads": r.spreads, "short_option_minimum": r.som, "net_option_value": r.net_option_value,
                "span": r.risk, "exposure": exp, "reference": self.reference(sym)}
            out["span"] += r.risk
            out["exposure"] += exp
            out["legs"] += elm_legs
            # MCX futures: additional / special (by side) and tender margin, % of contract value
            for lg in group:
                xl, xs, tender = lg.extra_pct
                pct = (xl if lg.units > 0 else xs) + tender
                if lg.instrument == "FUT" and pct:
                    out["other"] += abs(lg.units) * lg.multiplier * lg.price * pct / 100
        linked = sorted({" + ".join(sorted(s)) for s in self._inter if s <= by_sym.keys()})
        if linked:
            out["warnings"].append(f"inter-commodity spread credit not applied ({'; '.join(linked)}): "
                                   "the margin shown is higher than the exchange's")
        ids = [lg.id for lg in legs]
        unverified = [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT symbol FROM contract WHERE lot_source = 'mcx_table_unverified' "
            f"AND id IN ({','.join('?' * len(ids))}) ORDER BY symbol", ids)]
        if unverified:
            out["warnings"].append(f"unverified lot size: {', '.join(unverified)} (not yet checked against the "
                                   "exchange's contract specification)")
        out["net_premium"] = sum(lg.units * lg.multiplier * lg.price for lg in legs if lg.instrument == "OPT")
        out["total"] = out["span"] + out["exposure"] + out["other"]
        if alone and len(legs) > 1:
            singles = {lg.id: self.margin([Position(self._key(lg), lg.units)], as_of, alone=False) for lg in legs}
            for e in out["legs"]:
                s = singles[e["contract_id"]]
                e["span_alone"], e["total_alone"] = s["span"], s["total"]
            out["benefit"] = sum(s["total"] for s in singles.values()) - out["total"]
        else:
            out["benefit"] = 0.0
            for e in out["legs"]:
                e["span_alone"], e["total_alone"] = out["span"], out["total"]
        return _round(out)

    def reference(self, symbol: str) -> dict:
        """The deep-OTM test's previous close for an underlying: price, source
        (fo_bhavcopy | cash_bhavcopy | span_i1 | span_spot), the day it is for, and
        stale = it is not the previous trading day's close."""
        u = self._underlying(symbol)
        if u.get("prev_close") is None:          # database from before these columns
            return {"price": u["spot_price"], "source": "span_spot", "date": self.info.get("span_business_date"),
                    "expected_date": self.info.get("prev_close_date"), "stale": True}
        return {"price": u["prev_close"], "source": u.get("prev_close_source"), "date": u.get("prev_close_date"),
                "expected_date": self.info.get("prev_close_date"), "stale": bool(u.get("prev_close_stale"))}

    @staticmethod
    def stale_message(symbol: str, ref: dict) -> str:
        what = {"fo_bhavcopy": "F&O bhavcopy close", "cash_bhavcopy": "cash close",
                "span_i1": "i1 SPAN spot", "span_spot": "SPAN spot"}.get(ref["source"], ref["source"])
        return (f"{symbol}: no close for the previous trading day ({ref['expected_date']}); the deep-OTM "
                f"exposure test uses the {what} {ref['price']:g} from {ref['date']}, so exposure may be off.")

    @staticmethod
    def _key(lg: Leg) -> dict:
        return {"exchange": lg.exchange, "symbol": lg.symbol, "expiry": lg.expiry, "strike": lg.strike,
                "option_type": lg.option_type}

    def _exposure(self, legs: list[Leg], u: dict, as_of: str, warnings: list) -> list[dict]:
        rules = self.rules
        index = u["kind"] == "INDEX"
        oth = u["elm"].get("OTH")
        otm = u["elm"].get("OTM", oth)
        if oth is None:
            raise UnknownContract(f"no ELM rate for {u['symbol']}")
        spot = u["spot_price"]
        ref = self.reference(u["symbol"])
        if ref["stale"] and any(lg.instrument == "OPT" and lg.units < 0 for lg in legs):
            warnings.append(self.stale_message(u["symbol"], ref))
        ref = ref["price"]
        rows = []

        # futures: calendar pairs pay on 1/3 of the far leg
        futs = sorted((lg for lg in legs if lg.instrument == "FUT"), key=lambda lg: lg.expiry)
        no_cal = bool(rules.get("expiry_day_no_calendar_elm", 1))
        frac = rules.get("calendar_spread_far_leg_elm_fraction", 1 / 3)
        open_units = {lg.id: lg.units for lg in futs}
        spread_units = {lg.id: 0 for lg in futs}
        pairable = [lg for lg in futs if not (no_cal and lg.expiry == as_of)]
        for near in pairable:
            for far in pairable:
                if far.expiry <= near.expiry or open_units[near.id] * open_units[far.id] >= 0:
                    continue
                m = min(abs(open_units[near.id]), abs(open_units[far.id]))
                for lg in (near, far):
                    open_units[lg.id] -= m if open_units[lg.id] > 0 else -m
                spread_units[far.id] += m          # near leg of a pair pays nothing
        # off by default; brokercheck turns it on to recognise Angel One's BSE convention
        fut_on_close = bool(rules.get("futures_elm_on_previous_close", 0))
        for lg in futs:
            rate = lg.elm_pct if lg.elm_pct is not None else oth
            v = lg.multiplier * (ref if fut_on_close else lg.price)
            full = abs(open_units[lg.id]) * v * rate / 100
            cal = spread_units[lg.id] * v * rate / 100 * frac
            rows.append(self._elm_row(lg, rate, abs(lg.units) * v, full + cal,
                                      f"calendar: {spread_units[lg.id]} units at {frac:.4g}" if spread_units[lg.id]
                                      else "futures"))

        # options: only shorts pay
        deep_pct = rules.get("index_deep_otm_pct" if index else "stock_deep_otm_pct", 10 if index else 30) / 100
        long_dated = _months_after(date.fromisoformat(as_of), int(rules.get("index_long_dated_months", 9)))
        for lg in legs:
            if lg.instrument != "OPT":
                continue
            if lg.units > 0:
                rows.append(self._elm_row(lg, 0.0, 0.0, 0.0, "long option"))
                continue
            if lg.elm_pct is not None and rules.get("contract_elm_deep_otm_as_normal", 0) and lg.elm_pct > oth and \
                    not (index and lg.expiry == as_of):
                # off by default; brokercheck's model of Angel One (BSE: deep OTM charged at the normal rate)
                pct, why = oth, "short option, normal rate (deep OTM ignored)"
            elif lg.elm_pct is not None:          # the clearing corporation's final rate (BFO: ICCL)
                pct, why = lg.elm_pct, "short option, contract's own rate"
            else:
                # strictly beyond the cutoff; rounded so 25000 x 1.1 is 27500, not 27500.000000000004
                deep = (lg.option_type == "C" and lg.strike > round(ref * (1 + deep_pct), 6)) or \
                       (lg.option_type == "P" and lg.strike < round(ref * (1 - deep_pct), 6))
                pct, why = (otm, "deep OTM") if deep else (oth, "short option")
                if index and lg.expiry > long_dated.isoformat():
                    pct, why = max(pct, rules.get("index_long_dated_elm_pct", 5)), why + ", >9 months"
                if index and lg.expiry == as_of:
                    pct, why = pct + rules.get("expiry_day_short_index_option_extra_pct", 2), why + ", expiry day"
            price = ref if rules.get("short_option_elm_on_previous_close", 1) else spot
            if lg.und_price and rules.get("short_option_elm_on_future_price", 0):
                price = lg.und_price                    # MCX: the option's future (start-of-day price)
            basis = price if rules.get("short_option_elm_on_underlying", 1) else lg.strike
            notional = abs(lg.units) * lg.multiplier * basis
            rows.append(self._elm_row(lg, pct, notional, notional * pct / 100, why))
        return rows

    @staticmethod
    def _elm_row(lg: Leg, pct: float, notional: float, exposure: float, why: str) -> dict:
        return {"contract_id": lg.id, "exchange": lg.exchange, "contract": lg.label, "symbol": lg.symbol,
                "expiry": lg.expiry, "strike": lg.strike, "option_type": lg.option_type,
                "qty": lg.units, "lots": (lg.units / lg.lot_size) if lg.lot_size else None,
                "price": lg.price, "elm_pct": pct, "elm_notional": notional, "exposure": exposure,
                "elm_basis": why}


def _round(x):
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(_round(v) for v in x)
    return x


class BseEngine(Engine):
    """BSE F&O (db/BFO.sqlite): the same SPAN + exposure engine on ICCL's SPAN file,
    with each contract's ELM % taken as given from ICCL's per-contract file."""
    exchange = "BSE"


class McxEngine(Engine):
    """MCX (db/MCX.sqlite): MCXCCL's SPAN file; quantities in quotation units (lots x the
    multiplier from loader/mcx_contracts.yaml); futures add MCX's additional / tender margin."""
    exchange = "MCX"
