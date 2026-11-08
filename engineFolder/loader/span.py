"""Streaming parser for SPAN 4.00 XML risk parameter files (NSE F&O, NSE currency, BSE).

An NSE file is ~50 MB with ~150k option contracts, so contracts are handed to a
callback as they are read instead of building the whole tree. Everything else
(products, combined commodities, scenarios) is small and returned at the end.

Layout of the file (only the parts we use):
  spanFile/created                          YYYYMMDDHHMM the file was made
  spanFile/pointInTime/date|isSetl|setlQualifier
  .../clearingOrg/pointDef/scanPointDef     the 16 risk scenarios
  .../clearingOrg/ccDef                     one "combined commodity" per underlying:
                                            pfLinks, tiers, short-option minimum, dSpreads
  .../clearingOrg/interSpreads              offsets between underlyings
  .../clearingOrg/exchange/phyPf/phy        spot
  .../clearingOrg/exchange/futPf/fut        futures, each with a risk array (ra)
  .../clearingOrg/exchange/oopPf/series/opt options, each with a risk array (ra)
Risk array values are per unit of the underlying; positive = loss to a long.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

N_SCENARIOS = 16
HEADER_TAGS = {"fileFormat", "created", "date", "isSetl", "setlQualifier"}


class SpanError(ValueError):
    pass


@dataclass
class Span:
    header: dict = field(default_factory=dict)      # fileFormat, created, date, isSetl, setlQualifier
    points: list = field(default_factory=list)      # dicts: kind, point, price_mult, vol_mult, weight, paired_point
    products: dict = field(default_factory=dict)    # pf_id -> dict(pf_code, pf_type, name, currency, multiplier, ...)
    ccdefs: list = field(default_factory=list)      # dicts, see _ccdef
    inter_spreads: list = field(default_factory=list)
    n_contracts: int = 0


def _num(el, path, default=None):
    t = el.findtext(path)
    if t is None or not t.strip():
        return default
    return float(t)


def _int(el, path, default=None):
    t = el.findtext(path)
    return int(t) if t and t.strip() else default


def iso(pe: str | None) -> str | None:
    """'20261027' -> '2026-10-27'; SPAN uses '00000000' for 'no date'."""
    if not pe or not pe.strip() or pe.strip("0") == "":
        return None
    pe = pe.strip()
    if len(pe) == 6:                     # BSE's intra-month tiers: a month, YYYYMM
        return f"{pe[:4]}-{pe[4:6]}"
    return f"{pe[:4]}-{pe[4:6]}-{pe[6:8]}"


def _risk(holder) -> tuple[list[float], float | None]:
    ra = holder.find("ra")
    if ra is None:
        raise SpanError(f"no risk array under {holder.tag}")
    vals = [float(a.text) for a in ra.findall("a")]
    if len(vals) != N_SCENARIOS:
        raise SpanError(f"risk array with {len(vals)} values, expected {N_SCENARIOS}")
    return vals, _num(ra, "d")


def _contract(pf, holder, el, instrument: str) -> dict:
    """holder carries expiry/scan data: the fut itself, or the option's series."""
    risk, cdelta = _risk(el)
    return {
        "pf_id": int(pf.findtext("pfId")),
        "product_code": pf.findtext("pfCode"),
        "instrument": instrument,
        "expiry": iso(holder.findtext("pe")),
        "strike": _num(el, "k") if instrument == "OPT" else None,
        "option_type": el.findtext("o") if instrument == "OPT" else None,
        "multiplier": _num(holder, "cvf", _num(pf, "cvf", 1.0)),
        "span_price": _num(el, "p"),
        "delta": _num(el, "d"),
        "vol": _num(el, "v"),
        "time_to_expiry": _num(holder, "t"),
        "price_scan": _num(holder, "scanRate/priceScan"),
        "vol_scan": _num(holder, "scanRate/volScan"),
        "risk": risk,
        "composite_delta": cdelta,
        # MCX options on futures (oofPf): the series' underlying future (its pfId + cId)
        "span_cid": _int(el, "cId"),
        "und_pf_id": _int(holder, "undC/pfId") if instrument == "OPT" else None,
        "und_cid": _int(holder, "undC/cId") if instrument == "OPT" else None,
    }


def _product(pf, pf_type: str) -> dict:
    p = {
        "pf_id": int(pf.findtext("pfId")), "pf_code": pf.findtext("pfCode"), "pf_type": pf_type,
        "name": (pf.findtext("name") or "").strip(), "currency": pf.findtext("currency"),
        "multiplier": _num(pf, "cvf", 1.0), "value_method": pf.findtext("valueMeth"),
        "price_method": pf.findtext("priceMeth"), "settle_method": pf.findtext("setlMeth"),
        "exercise": pf.findtext("exercise"), "price_model": pf.findtext("priceModel"),
    }
    if pf_type == "PHY":
        phy = pf.find("phy")
        if phy is not None:
            p.update(spot_price=_num(phy, "p"), price_scan=_num(phy, "scanRate/priceScan"),
                     vol_scan=_num(phy, "scanRate/volScan"))
    return p


def _ccdef(el) -> dict:
    tiers = []
    for ttype in ("scan", "intra", "inter", "som"):
        for t in el.findall(f"{ttype}Tiers/tier"):
            tiers.append({"tier_type": ttype, "tier_no": _int(t, "tn"),
                          "start_expiry": iso(t.findtext("sPe")), "end_expiry": iso(t.findtext("ePe")),
                          "rate": _num(t, "rate/val")})
    spreads = []
    for ds in el.findall("dSpread"):
        legs = [{"cc": lg.findtext("cc"), "expiry": iso(lg.findtext("pe")), "tier": None, "side": lg.findtext("rs"),
                 "ratio": _num(lg, "i")} for lg in ds.findall("pLeg")]
        legs += [{"cc": lg.findtext("cc"), "expiry": None, "tier": _int(lg, "tn"), "side": lg.findtext("rs"),
                  "ratio": _num(lg, "i")} for lg in ds.findall("tLeg")]        # MCX: by intra tier
        spreads.append({"spread_no": _int(ds, "spread"), "charge_method": ds.findtext("chargeMeth"),
                        "rate": _num(ds, "rate/val"), "legs": legs})
    som = [t for t in tiers if t["tier_type"] == "som"]
    return {
        "cc": el.findtext("cc"), "name": (el.findtext("name") or "").strip(),
        "currency": el.findtext("currency"), "som_method": el.findtext("somMeth"),
        "som_rate": som[0]["rate"] if som else None,
        "links": [{"pf_id": _int(p, "pfId"), "pf_type": p.findtext("pfType"), "pf_code": p.findtext("pfCode")}
                  for p in el.findall("pfLink")],
        "tiers": tiers, "spreads": spreads,
    }


def _inter_spreads(el) -> list[dict]:
    out = []
    for ds in el.findall("dSpread"):
        no = _int(ds, "spread")
        if no is None:                     # NSE ships one empty placeholder
            continue
        legs = [{"cc": lg.findtext("cc"), "side": lg.findtext("rs"), "ratio": _num(lg, "i"),
                 "tier": _int(lg, "tn"), "expiry": iso(lg.findtext("pe"))}
                for lg in list(ds.findall("pLeg")) + list(ds.findall("tLeg"))]
        out.append({"spread_no": no, "method": ds.findtext("chargeMeth"),
                    "credit_rate": _num(ds, "rate/val"), "legs": legs})
    return out


def _point(el, kind: str) -> dict:
    return {"kind": kind, "point": _int(el, "point"), "price_mult": _num(el, "priceScanDef/mult"),
            "vol_mult": _num(el, "volScanDef/mult"), "weight": _num(el, "weight"),
            "paired_point": _int(el, "pairedPoint")}


def parse(source, on_contract: Callable[[dict], None]) -> Span:
    """Parse a SPAN XML file (path or binary file object). on_contract(dict) is
    called once per future/option; the rest is returned."""
    span = Span()
    pf = None
    try:
        for ev, el in ET.iterparse(source, events=("start", "end")):
            tag = el.tag
            if ev == "start":
                if tag in ("phyPf", "futPf", "oopPf", "oofPf"):       # oofPf: options on futures (MCX)
                    pf = el
                continue
            if tag == "fut" and pf is not None:
                on_contract(_contract(pf, el, el, "FUT"))
                span.n_contracts += 1
                el.clear()
            elif tag == "series" and pf is not None:
                for o in el.findall("opt"):
                    on_contract(_contract(pf, el, o, "OPT"))
                    span.n_contracts += 1
                el.clear()
            elif tag in ("phyPf", "futPf", "oopPf", "oofPf"):
                p = _product(el, {"phyPf": "PHY", "futPf": "FUT", "oopPf": "OOP", "oofPf": "OOF"}[tag])
                span.products[p["pf_id"]] = p
                pf = None
                el.clear()
            elif tag == "ccDef":
                span.ccdefs.append(_ccdef(el))
                el.clear()
            elif tag == "pointDef":
                span.points = [_point(p, "scan") for p in el.findall("scanPointDef")] + \
                              [_point(p, "delta") for p in el.findall("deltaPointDef")]
                el.clear()
            elif tag == "interSpreads":
                span.inter_spreads = _inter_spreads(el)
                el.clear()
            elif tag in HEADER_TAGS and tag not in span.header:
                span.header[tag] = (el.text or "").strip()
    except ET.ParseError as e:
        raise SpanError(f"bad SPAN XML: {e}") from e
    if not span.ccdefs or not span.n_contracts:
        raise SpanError("SPAN file has no contracts or no combined commodities")
    if len([p for p in span.points if p["kind"] == "scan"]) != N_SCENARIOS:
        raise SpanError("SPAN file does not define 16 scan points")
    return span
