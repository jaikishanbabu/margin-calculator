"""NSE cash (equity) margin for a basket of stock orders, from db/NSE.sqlite.

    eng = CashEngine("db/NSE.sqlite")
    m = eng.margin([CashOrder("RELIANCE", "buy", 100), CashOrder("INFY", "sell", 20, price=1170)])
    m["total"]["intraday_margin"], m["legs"][0]["leverage"], m["as_of"]

Per stock, effective rate % = min(100, max of):
  AMR             NSE's applicable margin rate (VaR + ELM + ad hoc) from the VaR batch;
                  it already carries NSE's surveillance add-on for most ASM stocks
  surveillance    short-term ASM stage I: max(1.5 x (VaR + ELM), 40); stage II: max(2.5 x
                  (VaR + ELM), 80); long-term ASM: 100 (rules in static_rates.yaml, verified:
                  false). The multiple is of VaR + ELM, not AMR: AMR's ad hoc part is
                  already NSE's surveillance add-on (ADVANCE: VaR + ELM 22.38, AMR 50)
  100% floor      trade-for-trade series (BE / BZ / ST / SZ), or no VaR row
Intraday (MIS): value x max(rate, broker minimum), leverage = 1 / that. The broker
minimum (rule broker_min_intraday_pct, 20%: Zerodha and Dhan agree) is 0 = off;
nse_margin is the same at NSE's rate alone.
Delivery (CNC): a buy needs 100% of its value; a sell needs the shares in holdings.
Stocks are margined one by one: cash positions never offset each other.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

T2T_SERIES = ("BE", "BZ", "ST", "SZ")


class CashError(ValueError):
    code = "invalid_input"


class UnknownSecurity(CashError):
    code = "unknown_security"


class InvalidQuantity(CashError):
    code = "invalid_qty"


class NoPrice(CashError):
    code = "no_price"


@dataclass
class CashOrder:
    symbol: str                   # NSE symbol (resolves to its primary series) or a token
    side: str                     # buy | sell
    qty: int                      # shares
    price: Optional[float] = None  # default: the last close in the bhavcopy


def _stage(text) -> int:
    """'Stage II' / 'STASM - II (12)' -> 2; unknown -> 1."""
    m = re.search(r"\b(IV|III|II|I)\b", str(text or ""))
    return {"I": 1, "II": 2, "III": 3, "IV": 4}[m[1]] if m else 1


class CashEngine:
    def __init__(self, path: str | Path = "db/NSE.sqlite"):
        self.path = Path(path)
        self.conn = None
        self._ino = None
        self.refresh(force=True)

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
        return True

    @property
    def as_of(self) -> dict:
        i = self.info
        return {"var_date": i.get("var_date"), "var_batch": int(i["var_batch"]) if i.get("var_batch") else None,
                "var_fetched_at": i.get("var_fetched_at") or None, "close_date": i.get("close_date") or None,
                "master_date": i.get("master_trade_date")}

    def rule(self, name: str, default: float) -> float:
        v = self.rules.get(name)
        return default if v is None else float(v)

    # ---- lookup -------------------------------------------------------------------
    def security(self, symbol) -> sqlite3.Row:
        s = str(symbol).strip().upper()
        if s.isdigit():
            row = self.conn.execute("SELECT * FROM security WHERE token = ?", (int(s),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM security WHERE symbol = ? AND is_primary", (s,)).fetchone()
        if row is None:
            raise UnknownSecurity(f"{symbol}: not an NSE-listed stock in the security master")
        return row

    # ---- rates --------------------------------------------------------------------
    def rate(self, sec: sqlite3.Row) -> tuple[float, list, list]:
        """(effective rate %, [reasons it is what it is], [warnings]) for one security."""
        reasons, warnings = [], []
        amr = sec["amr"]
        rates = []
        if amr is None:
            rates.append(100.0)
            reasons.append("no VaR row: 100%")
            warnings.append(f"{sec['symbol']}: no VaR rate in NSE's batch for series {sec['series']}; "
                            f"charged 100%")
        else:
            rates.append(amr)
            reasons.append(f"NSE AMR {amr:g}% (VaR {sec['var_pct']:g} + ELM {sec['elm_pct']:g} + ad hoc "
                           f"{sec['adhoc_pct']:g})")
        base = sec["var_pct"] + sec["elm_pct"] if amr is not None else 100.0
        if sec["asm_short"]:
            if _stage(sec["asm_short"]) >= 2:
                r = max(self.rule("asm_short_stage2_mult", 2.5) * base, self.rule("asm_short_stage2_floor", 80))
            else:
                r = max(self.rule("asm_short_stage1_mult", 1.5) * base, self.rule("asm_short_stage1_floor", 40))
            rates.append(r)
            reasons.append(f"short-term ASM {sec['asm_short']}: {min(r, 100):g}%")
            warnings.append(f"{sec['symbol']}: under short-term ASM ({sec['asm_short']})")
        if sec["asm_long"]:
            r = self.rule("asm_long_pct", 100)
            rates.append(r)
            reasons.append(f"long-term ASM {sec['asm_long']}: {r:g}%")
            warnings.append(f"{sec['symbol']}: under long-term ASM ({sec['asm_long']})")
        if sec["gsm"]:
            warnings.append(f"{sec['symbol']}: under GSM ({sec['gsm']}); NSE's rate applies")
        if sec["series"] in T2T_SERIES:
            r = self.rule("t2t_pct", 100)
            rates.append(r)
            reasons.append(f"trade-for-trade series {sec['series']}: {r:g}%")
            warnings.append(f"{sec['symbol']}: trade-for-trade ({sec['series']} series): delivery only, "
                            f"no intraday leverage")
        tradable = self.rule("tradable_status", 6)
        if sec["status"] is not None and sec["status"] != tradable:
            warnings.append(f"{sec['symbol']}: NSE trading status {sec['status']} (normal is "
                            f"{tradable:g}); it may not be tradable today")
        return min(100.0, max(rates)), reasons, warnings

    # ---- margin -------------------------------------------------------------------
    def margin(self, orders: list[CashOrder]) -> dict:
        legs, warnings = [], []
        move = self.rule("corporate_action_move_pct", 20)
        broker_min = self.rule("broker_min_intraday_pct", 0)
        for o in orders:
            side = str(o.side).lower()
            if side not in ("buy", "sell"):
                raise CashError(f"{o.symbol}: side must be buy or sell")
            sec = self.security(o.symbol)
            qty = int(o.qty)
            if qty <= 0 or qty != o.qty:
                raise InvalidQuantity(f"{sec['symbol']}: quantity must be a whole number above 0")
            if qty % sec["lot"]:
                raise InvalidQuantity(f"{sec['symbol']}: quantity {qty:,} is not a multiple of the lot size "
                                      f"{sec['lot']:,}")
            if o.price is not None:
                if not o.price > 0:
                    raise CashError(f"{sec['symbol']}: price must be above 0")
                price, price_source = float(o.price), "given"
            elif sec["close"]:
                price, price_source = float(sec["close"]), "close"
            else:
                raise NoPrice(f"{sec['symbol']}: no closing price in the bhavcopy; enter a price")
            rate, reasons, warn = self.rate(sec)
            intraday = max(rate, broker_min) if broker_min > 0 else rate
            if broker_min > 0 and broker_min > rate:
                reasons.append(f"broker minimum {broker_min:g}%")
            if sec["close"] and sec["prev_close"] and abs(sec["close"] / sec["prev_close"] - 1) * 100 > move:
                warn.append(f"{sec['symbol']}: close {sec['close']:g} vs previous {sec['prev_close']:g} "
                            f"({(sec['close'] / sec['prev_close'] - 1) * 100:+.1f}%): possible corporate action")
            if price_source == "given" and sec["band_low"] and not sec["band_low"] <= price <= sec["band_high"]:
                warn.append(f"{sec['symbol']}: price {price:g} is outside today's band "
                            f"{sec['band_low']:g}-{sec['band_high']:g}")
            value = round(qty * price, 2)
            legs.append({
                "symbol": sec["symbol"], "series": sec["series"], "token": sec["token"], "isin": sec["isin"],
                "name": sec["name"], "side": side, "qty": qty, "lot": sec["lot"], "price": price,
                "price_source": price_source, "value": value,
                "amr_pct": sec["amr"], "var_pct": sec["var_pct"], "elm_pct": sec["elm_pct"],
                "adhoc_pct": sec["adhoc_pct"], "effective_pct": round(rate, 2), "intraday_pct": round(intraday, 2),
                "rate_reasons": reasons,
                "intraday_margin": round(value * intraday / 100, 2), "leverage": round(100 / intraday, 2),
                "nse_margin": round(value * rate / 100, 2),        # at NSE's rate alone, no broker minimum
                "delivery_value": value if side == "buy" else 0.0,
                "delivery_note": None if side == "buy" else "sell: needs the shares in your holdings (no margin)",
                "warnings": warn,
            })
            warnings.extend(warn)
        total = {k: round(sum(l[k] for l in legs), 2) for k in ("value", "intraday_margin", "delivery_value", "nse_margin")}
        total["leverage"] = round(total["value"] / total["intraday_margin"], 2) if total["intraday_margin"] else None
        return {"as_of": self.as_of, "legs": legs, "total": total, "warnings": warnings}
