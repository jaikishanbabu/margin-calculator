"""Streaming parser for SPAN risk parameter files in the standard SPAN XML layout
(used by NSE Clearing `.spn`, BSE/ICCL XML and MCXCCL).

Structure handled (simplified):

  spanFile/pointInTime/date
  spanFile/pointInTime/clearingOrg/exchange/{exch, phyPf, futPf, oopPf, ooePf, oofPf}
     phyPf/phy             underlying (cash) with price and risk array
     futPf/fut             futures: pe (expiry), p (price), ra (risk array)
     oopPf|oofPf/series    options: pe, cvf, undC; series/opt: o (C/P), k (strike), p, ra
  spanFile/pointInTime/clearingOrg/ccDef       combined commodity: pfLink, intrTiers,
                                               somTiers, dSpread (intra-commodity spreads)
  spanFile/pointInTime/clearingOrg/interSpreads/dSpread   inter-commodity spread credits

Risk arrays: 16 scenario values (`a`), loss per contract (positive = loss for a long position).
Composite delta (`d`) inside `ra` is used for spreads.
"""
import json
from xml.etree.ElementTree import iterparse
import io

from .util import ParseError, to_float, unpack

PF_TAGS = {"phyPf": "PHY", "futPf": "FUT", "oopPf": "OPT", "ooePf": "OPT", "oofPf": "OPT"}


def _t(el, tag, default=None):
    c = el.find(tag)
    return c.text.strip() if c is not None and c.text is not None else default


def _ra(el):
    """Read the risk array of an instrument -> (list of 16 floats, composite delta)."""
    ras = el.findall("ra")
    if not ras:
        return None, None
    values, deltas = [], []
    if len(ras) == 1:
        vals = [to_float(a.text) or 0.0 for a in ras[0].findall("a")]
        values = vals
        ds = [to_float(d.text) for d in ras[0].findall("d")]
        deltas = [d for d in ds if d is not None]
    else:
        items = []
        for i, ra in enumerate(ras):
            r = to_float(_t(ra, "r")) or (i + 1)
            items.append((r, to_float(_t(ra, "a")) or 0.0, to_float(_t(ra, "d"))))
        items.sort()
        values = [v for _, v, _ in items]
        deltas = [d for _, _, d in items if d is not None]
    comp_delta = deltas[0] if deltas else None
    return [round(v, 6) for v in values], comp_delta


def _norm_pe(pe):
    pe = (pe or "").strip()
    return pe if pe and pe != "00000000" else None


def _parse_pf(el, kind, exch):
    pf_id = _t(el, "pfId")
    pf_code = _t(el, "pfCode") or _t(el, "name")
    pf_cvf = to_float(_t(el, "cvf")) or 1.0
    rows = []
    if kind == "PHY":
        for phy in el.findall("phy"):
            ra, cd = _ra(phy)
            rows.append(dict(exch=exch, pf_id=pf_id, pf_code=pf_code, pf_type="PHY", c_id=_t(phy, "cId"), kind="PHY",
                             expiry=None, strike=None, opt_type=None, price=to_float(_t(phy, "p")),
                             delta=cd if cd is not None else (to_float(_t(phy, "d")) or 1.0),
                             cvf=to_float(_t(phy, "cvf")) or pf_cvf, und_pf_id=None, und_c_id=None, ra=ra))
    elif kind == "FUT":
        for fut in el.findall("fut"):
            ra, cd = _ra(fut)
            und = fut.find("undC")
            rows.append(dict(exch=exch, pf_id=pf_id, pf_code=pf_code, pf_type="FUT", c_id=_t(fut, "cId"), kind="FUT",
                             expiry=_norm_pe(_t(fut, "pe")), strike=None, opt_type=None, price=to_float(_t(fut, "p")),
                             delta=cd if cd is not None else (to_float(_t(fut, "d")) or 1.0),
                             cvf=to_float(_t(fut, "cvf")) or pf_cvf,
                             und_pf_id=_t(und, "pfId") if und is not None else None,
                             und_c_id=_t(und, "cId") if und is not None else None, ra=ra))
    else:
        und_pf = el.find("undPf")
        und_pf_id = _t(und_pf, "pfId") if und_pf is not None else None
        for series in el.findall("series"):
            pe = _norm_pe(_t(series, "pe"))
            s_cvf = to_float(_t(series, "cvf")) or pf_cvf
            und = series.find("undC")
            s_und_pf = (_t(und, "pfId") if und is not None else None) or und_pf_id
            s_und_c = _t(und, "cId") if und is not None else None
            for opt in series.findall("opt"):
                ra, cd = _ra(opt)
                o = (_t(opt, "o") or "").upper()
                rows.append(dict(exch=exch, pf_id=pf_id, pf_code=pf_code, pf_type="OPT", c_id=_t(opt, "cId"), kind="OPT",
                                 expiry=pe, strike=to_float(_t(opt, "k")), opt_type="CE" if o.startswith("C") else "PE",
                                 price=to_float(_t(opt, "p")),
                                 delta=cd if cd is not None else to_float(_t(opt, "d")),
                                 cvf=s_cvf, und_pf_id=s_und_pf, und_c_id=s_und_c, ra=ra))
    return rows


def _tiers(el, tag):
    out = []
    holder = el.find(tag)
    if holder is None:
        return out
    for t in holder.findall("tier"):
        rate = t.find("rate")
        out.append({
            "tn": int(to_float(_t(t, "tn")) or 0),
            "sPe": _t(t, "sPe") or _t(t, "brkPe") or "",
            "ePe": _t(t, "ePe") or "",
            "rate": to_float(_t(rate, "val")) if rate is not None else to_float(_t(t, "val")),
        })
    return out


def _spread(sp, leg_tag):
    rate = sp.find("rate")
    legs = []
    for lg in sp.findall(leg_tag):
        legs.append({"cc": _t(lg, "cc"), "tn": int(to_float(_t(lg, "tn")) or 0),
                     "rs": (_t(lg, "rs") or "A").upper(), "i": to_float(_t(lg, "i")) or 1.0})
    return {"spread": _t(sp, "spread"), "chargeMeth": _t(sp, "chargeMeth"),
            "rate": to_float(_t(rate, "val")) if rate is not None else to_float(_t(sp, "rate")),
            "legs": legs}


def _parse_cc(el):
    links = []
    for pl in el.findall("pfLink"):
        links.append({"exch": _t(pl, "exch"), "pfId": _t(pl, "pfId"), "pfCode": _t(pl, "pfCode"),
                      "pfType": _t(pl, "pfType"), "sc": to_float(_t(pl, "sc")) or 1.0})
    return {
        "cc": _t(el, "cc"),
        "name": _t(el, "name"),
        "currency": _t(el, "currency"),
        "somMeth": (_t(el, "somMeth") or "MAX").upper(),
        "pfLinks": links,
        "intrTiers": _tiers(el, "intrTiers") or _tiers(el, "intraTiers"),
        "somTiers": _tiers(el, "somTiers"),
        "dSpreads": [_spread(sp, "tLeg") for sp in el.findall("dSpread")],
    }


def parse_span(data: bytes):
    """Yields ('meta', {...}), ('inst', row), ('cc', ccdef), ('inter', spread)."""
    _, xml = unpack(data, prefer_ext=(".spn", ".xml"))
    head = xml[:2000].lstrip()
    if not head.startswith(b"<"):
        raise ParseError("Not a SPAN XML file (this engine reads the XML/.spn SPAN format, not the old fixed-width PA2 text format)")
    stack = []
    exch = None
    meta = {}
    n_inst = n_cc = 0
    try:
        for event, el in iterparse(io.BytesIO(xml), events=("start", "end")):
            tag = el.tag.split("}")[-1]
            if event == "start":
                stack.append(tag)
                continue
            stack.pop()
            parent = stack[-1] if stack else None
            if tag == "exch" and parent == "exchange":
                exch = (el.text or "").strip()
            elif tag == "date" and parent == "pointInTime" and "date" not in meta:
                meta["date"] = (el.text or "").strip()
            elif tag == "isSetl" and parent == "pointInTime":
                meta["isSetl"] = (el.text or "").strip()
            elif tag == "ec" and parent == "clearingOrg":
                meta["clearingOrg"] = (el.text or "").strip()
            elif tag in PF_TAGS and parent == "exchange":
                for row in _parse_pf(el, PF_TAGS[tag], exch):
                    n_inst += 1
                    yield "inst", row
                el.clear()
            elif tag == "ccDef":
                n_cc += 1
                yield "cc", _parse_cc(el)
                el.clear()
            elif tag == "dSpread" and parent == "interSpreads":
                yield "inter", _spread(el, "pLeg")
                el.clear()
    except Exception as e:
        if isinstance(e, ParseError):
            raise
        raise ParseError(f"SPAN XML could not be read: {e}")
    if n_inst == 0:
        raise ParseError("No futures/options found in SPAN file")
    meta["instruments"] = n_inst
    meta["combined_commodities"] = n_cc
    yield "meta", meta


def ra_json(ra):
    return json.dumps(ra, separators=(",", ":")) if ra is not None else None
