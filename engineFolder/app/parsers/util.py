"""Helpers shared by the parsers: unzip/gunzip, CSV sniffing, column matching, dates, numbers."""
import csv
import gzip
import io
import re
import zipfile
from datetime import datetime, timedelta

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


class ParseError(Exception):
    pass


def unpack(data: bytes, prefer_ext=()):
    """Return (inner_filename, bytes) after removing zip / gzip wrappers."""
    name = None
    for _ in range(3):
        if data[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(data))
            members = [i for i in zf.infolist() if not i.is_dir()]
            if any(m.filename == "[Content_Types].xml" for m in members):
                break  # it's an .xlsx workbook, not an archive
            if not members:
                raise ParseError("Zip file is empty")
            pick = None
            for ext in prefer_ext:
                pick = next((m for m in members if m.filename.lower().endswith(ext)), None)
                if pick:
                    break
            pick = pick or max(members, key=lambda m: m.file_size)
            name, data = pick.filename, zf.read(pick)
        elif data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        else:
            break
    return name, data


def norm(h):
    return re.sub(r"[^a-z0-9%]", "", str(h or "").lower())


def to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "NA", "N/A", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def decode(data: bytes):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def read_rows(data: bytes, filename=""):
    """Read CSV/TSV/pipe text or an Excel sheet into a list of raw rows (lists of strings)."""
    fn = (filename or "").lower()
    if fn.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            return [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
        except Exception as e:  # noqa
            raise ParseError(f"Could not read Excel file: {e}")
    if fn.endswith(".xls") or data[:4] == b"\xd0\xcf\x11\xe0":
        try:
            import xlrd  # optional
            book = xlrd.open_workbook(file_contents=data)
            sh = book.sheet_by_index(0)
            return [[str(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
        except ImportError:
            raise ParseError("Old .xls files are not supported - save it as .csv or .xlsx")
    text = decode(data)
    if text.lstrip().startswith("<"):
        # HTML tables saved as .xls are common on exchange sites
        return _html_table_rows(text)
    sample = text[:20000]
    delim = max([",", "|", "\t", ";"], key=lambda d: sample.count(d))
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]


def _html_table_rows(text):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)
        rows.append([re.sub(r"<[^>]+>", "", c).strip() for c in cells])
    return rows


def table(data: bytes, filename="", must_have=()):
    """Return (headers_normalized, list_of_dicts). Finds the header row automatically
    (the first row containing all `must_have` alias groups, else the first wide row)."""
    rows = read_rows(data, filename)
    rows = [[c.strip() for c in r] for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise ParseError("File has no rows")
    hdr_idx = 0
    if must_have:
        for i, r in enumerate(rows[:30]):
            normed = {norm(c) for c in r}
            if all(any(a in normed for a in group) for group in must_have):
                hdr_idx = i
                break
        else:
            raise ParseError("Could not find the header row (expected columns like: "
                             + ", ".join(g[0] for g in must_have) + ")")
    headers = [norm(h) for h in rows[hdr_idx]]
    out = []
    for r in rows[hdr_idx + 1:]:
        if len(r) < len(headers) // 2:
            continue
        out.append({headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))})
    return headers, out


def pick(headers, *aliases):
    """First header that matches one of the aliases (exact, then prefix)."""
    for a in aliases:
        if a in headers:
            return a
    for a in aliases:
        for h in headers:
            if h.startswith(a):
                return h
    return None


def norm_date(v):
    """Many exchange date styles -> YYYYMMDD (or None)."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s or s in ("-", "NA"):
        return None
    # NSE legacy contract files: seconds since 1980-01-01
    if re.fullmatch(r"\d{9,10}(\.0+)?", s):
        return (datetime(1980, 1, 1) + timedelta(seconds=int(float(s)))).strftime("%Y%m%d")
    s = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d %b %Y", "%Y/%m/%d", "%d.%m.%Y", "%d%m%Y"):
        try:
            return datetime.strptime(s.title() if "%b" in fmt else s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def norm_opt(v):
    s = str(v or "").strip().upper()
    if s in ("CE", "C", "CALL"):
        return "CE"
    if s in ("PE", "P", "PUT"):
        return "PE"
    return None
