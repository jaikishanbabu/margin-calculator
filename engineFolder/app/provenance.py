"""Provenance read from INSIDE uploaded files (never from the filename).

For SPAN XML we read the header fields (spanFile/*, pointInTime/*, clearingOrg/*) before
the first <exchange>, and record every one of them verbatim in `source_meta`.

Rules for the stored values
- Effective trading date: pointInTime/date from the file. If the admin also typed a date,
  the two must agree, otherwise the upload is rejected.
- Source timestamp: spanFile/created (or pointInTime/time) from the file, verbatim.
- SPAN cycle: taken from the file ONLY if the file contains an explicit cycle field.
  Otherwise the admin MUST enter it, and it is stored with cycle_source = "admin".
  The filename is never used. If the file says it is a settlement (EOD) file
  (pointInTime/isSetl = 1) and the admin enters an intraday label, or vice versa,
  the upload is rejected.
"""
import io
import re
from xml.etree.ElementTree import iterparse, ParseError

PROVENANCE_VERSION = "1.0.0"

CYCLE_FIELDS = ("cycle", "cycleId", "cycleCode", "cycleNo", "runId", "runNo")
CYCLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
EOD_LABELS = {"s", "eod", "setl", "settlement", "final"}


class ProvenanceError(Exception):
    pass


def span_header(xml: bytes, max_elements=5000):
    """Return dict of header fields: {'spanFile/created': '...', 'pointInTime/date': '...', ...}."""
    fields = {}
    stack = []
    try:
        for n, (event, el) in enumerate(iterparse(io.BytesIO(xml), events=("start", "end"))):
            tag = el.tag.split("}")[-1]
            if event == "start":
                if tag == "exchange" or tag == "ccDef":
                    break
                stack.append(tag)
            else:
                stack.pop()
                parent = stack[-1] if stack else None
                if parent in ("spanFile", "pointInTime", "clearingOrg") and len(el) == 0:
                    key = f"{parent}/{tag}"
                    if key not in fields:
                        fields[key] = (el.text or "").strip()
                el.clear()
            if n > max_elements:
                break
    except ParseError as e:
        raise ProvenanceError(f"SPAN file is not valid XML: {e}")
    if not stack or stack[0] != "spanFile":
        if not fields:
            raise ProvenanceError("This does not look like a SPAN XML file (no <spanFile> root)")
    return fields


def resolve_span(xml: bytes, admin_trade_date=None, admin_cycle=None, cycle_source="admin", check_setl_label=True):
    """cycle_source: "admin" (typed/confirmed by a person) or "exchange" (the cycle the exchange published the
    file under: the cycle in its official download URL / listing, recorded by the fetcher)."""
    fields = span_header(xml)
    file_date = fields.get("pointInTime/date") or None
    if file_date and not re.fullmatch(r"\d{8}", file_date):
        raise ProvenanceError(f"pointInTime/date in file is not YYYYMMDD: {file_date!r}")
    out = {"source_meta": fields}

    # effective trading date
    if file_date:
        if admin_trade_date and admin_trade_date != file_date:
            raise ProvenanceError(f"Trading date you entered ({admin_trade_date}) does not match the file ({file_date})")
        out["trade_date"], out["trade_date_source"] = file_date, "file"
    elif admin_trade_date:
        out["trade_date"], out["trade_date_source"] = admin_trade_date, "admin"
    else:
        raise ProvenanceError("The file has no business date; enter the trading date")

    # source timestamp (verbatim)
    out["source_timestamp"] = fields.get("spanFile/created") or fields.get("pointInTime/time") or None

    # cycle
    file_cycle = next((fields[f"pointInTime/{k}"] for k in CYCLE_FIELDS if fields.get(f"pointInTime/{k}")), None) \
        or next((fields[f"spanFile/{k}"] for k in CYCLE_FIELDS if fields.get(f"spanFile/{k}")), None)
    if file_cycle:
        if admin_cycle and admin_cycle != file_cycle:
            raise ProvenanceError(f"Cycle you entered ({admin_cycle}) does not match the file ({file_cycle})")
        out["cycle"], out["cycle_source"] = file_cycle, "file"
    else:
        if not admin_cycle:
            raise ProvenanceError("This SPAN file does not state its cycle. Enter the cycle exactly as published "
                                  "(e.g. i1, i2 … or s for end-of-day).")
        if not CYCLE_RE.match(admin_cycle):
            raise ProvenanceError("Cycle may only contain letters, digits, - and _ (max 12)")
        is_setl = fields.get("pointInTime/isSetl")
        if check_setl_label and is_setl in ("0", "1"):
            says_eod = admin_cycle.lower() in EOD_LABELS
            if is_setl == "1" and not says_eod:
                raise ProvenanceError(f"The file is a settlement (end-of-day) file but you entered cycle {admin_cycle!r}")
            if is_setl == "0" and says_eod:
                raise ProvenanceError(f"The file is NOT a settlement file but you entered end-of-day cycle {admin_cycle!r}")
        out["cycle"], out["cycle_source"] = admin_cycle, cycle_source
    return out
