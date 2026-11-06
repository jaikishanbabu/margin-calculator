"""Which files each segment needs, and how to recognise them from the file name."""
import re
from datetime import datetime

SEGMENTS = {
    "NFO": {"name": "NSE F&O", "kind": "deriv", "exchange": "NSE"},
    "BFO": {"name": "BSE F&O", "kind": "deriv", "exchange": "BSE"},
    "CDS": {"name": "NSE Currency", "kind": "deriv", "exchange": "NSE"},
    "MCX": {"name": "MCX Commodity", "kind": "deriv", "exchange": "MCX"},
    "NSECM": {"name": "NSE Equity (cash)", "kind": "cash", "exchange": "NSE"},
    "BSECM": {"name": "BSE Equity (cash)", "kind": "cash", "exchange": "BSE"},
}

# file_type -> label, required?, accepted extensions
FILE_TYPES = {
    "span": {"label": "SPAN risk parameter file", "ext": ".zip .spn .xml .gz"},
    "contract_master": {"label": "Contract master", "ext": ".csv .gz .zip .txt"},
    "lot_sizes": {"label": "Market lots (fo_mktlots)", "ext": ".csv"},
    "elm": {"label": "Exposure / ELM margin file", "ext": ".csv .txt .zip .xls .xlsx"},
    "bhavcopy": {"label": "Bhavcopy (prices)", "ext": ".csv .zip .gz"},
    "settlement": {"label": "Settlement prices", "ext": ".csv .zip"},
    "ban_list": {"label": "F&O ban list", "ext": ".csv .txt"},
    "var": {"label": "VaR margin file", "ext": ".dat .csv .txt .zip"},
    "security_master": {"label": "Security / scrip master", "ext": ".csv .txt .zip"},
    "surveillance": {"label": "Surveillance list (ASM/GSM/T2T)", "ext": ".csv .json .txt .xlsx"},
}

SEGMENT_FILES = {
    "NFO": {"required": ["span", "elm"], "optional": ["contract_master", "lot_sizes", "bhavcopy", "settlement", "ban_list"]},
    "BFO": {"required": ["span", "elm"], "optional": ["contract_master", "bhavcopy"]},
    "CDS": {"required": ["span"], "optional": ["contract_master", "elm", "bhavcopy"]},
    "MCX": {"required": ["span", "contract_master"], "optional": ["elm", "bhavcopy"]},
    "NSECM": {"required": ["var", "bhavcopy"], "optional": ["security_master", "surveillance"]},
    "BSECM": {"required": ["var", "bhavcopy"], "optional": ["security_master", "surveillance"]},
}

# What each file looks like, per segment (shown in the admin upload form).
FILE_HINTS = {
    ("NFO", "span"): "nsccl.YYYYMMDD.i1.zip … i5, s (end of day) — NSE Clearing",
    ("NFO", "contract_master"): "NSE_FO_contract_DDMMYYYY.csv.gz",
    ("NFO", "lot_sizes"): "fo_mktlots.csv",
    ("NFO", "elm"): "ael_DDMMYYYY.csv",
    ("NFO", "bhavcopy"): "BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip",
    ("NFO", "settlement"): "FOSett_prce_DDMMYYYY.csv",
    ("NFO", "ban_list"): "fo_secban_DDMMYYYY.csv",
    ("CDS", "span"): "nsccl_x.YYYYMMDD.i1.zip … s — NSE Clearing currency",
    ("CDS", "contract_master"): "NSE_CD_contract_DDMMYYYY.csv.gz",
    ("CDS", "elm"): "Currency exposure (ELM) file",
    ("CDS", "bhavcopy"): "BhavCopy_NSE_CD_…_YYYYMMDD….csv.zip",
    ("BFO", "span"): "BSE/ICCL risk parameter file — XML or .spn (BOD / intraday / EOD)",
    ("BFO", "contract_master"): "BSE derivatives contract master (CSV)",
    ("BFO", "elm"): "BSE derivatives exposure (ELM) file",
    ("BFO", "bhavcopy"): "BhavCopy_BSE_FO_0_0_0_YYYYMMDD_F_0000.CSV",
    ("MCX", "span"): "MCXCCL SPAN risk parameter file (.spn / XML, zip)",
    ("MCX", "contract_master"): "MCX contract master — lot size, price unit, multiplier",
    ("MCX", "elm"): "MCX exposure + additional / tender / delivery margins",
    ("MCX", "bhavcopy"): "MCX bhavcopy / settlement prices",
    ("NSECM", "var"): "C_VAR1_DDMMYYYY_N.DAT (N = batch 1–6)",
    ("NSECM", "bhavcopy"): "BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip",
    ("NSECM", "security_master"): "EQUITY_L.csv",
    ("NSECM", "surveillance"): "ASM / GSM / trade-to-trade lists (CSV or JSON)",
    ("BSECM", "var"): "BSE VaR margin file",
    ("BSECM", "bhavcopy"): "BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV",
    ("BSECM", "security_master"): "BSE scrip master",
    ("BSECM", "surveillance"): "ASM / GSM / trade-to-trade lists (CSV or JSON)",
}

# SPAN cycles, earliest first. "bod" basis uses the earliest cycle of the day.
CYCLE_ORDER = ["bod", "i1", "i2", "i3", "i4", "i5", "i6", "s", "eod"]

# (regex on lower-case filename, segment, file_type). First match wins.
PATTERNS = [
    (r"^nsccl_x\.\d{8}\.", "CDS", "span"),
    (r"^nsccl_o\.\d{8}\.", "NFO", "span"),
    (r"^nsccl\.\d{8}\.", "NFO", "span"),
    (r"nse_cd_contract", "CDS", "contract_master"),
    (r"nse_fo_contract|^contract\.", "NFO", "contract_master"),
    (r"fo_mktlots", "NFO", "lot_sizes"),
    (r"^ael_", "NFO", "elm"),
    (r"bhavcopy_nse_fo", "NFO", "bhavcopy"),
    (r"bhavcopy_nse_cd", "CDS", "bhavcopy"),
    (r"bhavcopy_nse_cm|^cm\d{2}[a-z]{3}\d{4}bhav|^sec_bhavdata", "NSECM", "bhavcopy"),
    (r"bhavcopy_bse_fo", "BFO", "bhavcopy"),
    (r"bhavcopy_bse_cm|^eq\d{6}", "BSECM", "bhavcopy"),
    (r"fosett_prce|fo_sett", "NFO", "settlement"),
    (r"fo_secban", "NFO", "ban_list"),
    (r"^c_var\d?_", "NSECM", "var"),
    (r"bse.*var|var.*bse|^varmargin", "BSECM", "var"),
    (r"^equity_l|nse.*security", "NSECM", "security_master"),
    (r"scrip|bse.*master", "BSECM", "security_master"),
    (r"asm|gsm|t2t|trade.?for.?trade|surveil", "NSECM", "surveillance"),
    (r"mcx.*(span|risk)|^mcxccl|\.spn$", "MCX", "span"),
    (r"mcx.*(contract|master)", "MCX", "contract_master"),
    (r"mcx.*(margin|elm|exposure)", "MCX", "elm"),
    (r"mcx.*bhav", "MCX", "bhavcopy"),
    (r"bse.*(span|risk|bod)|^bfo.*\.(xml|spn)", "BFO", "span"),
    (r"bse.*(contract|deriv.*master)|^bfo.*master", "BFO", "contract_master"),
    (r"bse.*(elm|exposure)|^bfo.*elm", "BFO", "elm"),
    (r"cd.*elm|currency.*elm|ael_cd", "CDS", "elm"),
]

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _valid(y, m, d):
    try:
        datetime(y, m, d)
        return 2000 <= y <= 2100
    except ValueError:
        return False


def date_from_filename(name: str):
    n = name.lower()
    for m in re.finditer(r"(?<!\d)(\d{8})(?!\d)", n):
        s = m.group(1)
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
        if _valid(y, mo, d):
            return s
        d, mo, y = int(s[:2]), int(s[2:4]), int(s[4:])
        if _valid(y, mo, d):
            return f"{y:04d}{mo:02d}{d:02d}"
    m = re.search(r"(\d{1,2})[-_ ]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(\d{4}|\d{2})", n)
    if m:
        d, mo, y = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
        y = y + 2000 if y < 100 else y
        if _valid(y, mo, d):
            return f"{y:04d}{mo:02d}{d:02d}"
    m = re.search(r"(\d{2})[-_.](\d{2})[-_.](\d{4})", n)
    if m and _valid(int(m.group(3)), int(m.group(2)), int(m.group(1))):
        return f"{m.group(3)}{m.group(2)}{m.group(1)}"
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", n)  # DDMMYY (e.g. BSE EQ041126)
    if m:
        s = m.group(1)
        d, mo, y = int(s[:2]), int(s[2:4]), 2000 + int(s[4:])
        if _valid(y, mo, d):
            return f"{y:04d}{mo:02d}{d:02d}"
    return None


def cycle_from_filename(name: str):
    n = name.lower()
    m = re.search(r"\.\d{8}\.(i\d|s)\b", n)
    if m:
        return m.group(1)
    for tag in ("bod", "eod"):
        if tag in n:
            return tag
    m = re.search(r"[_.-](i\d)[_.-]", n)
    if m:
        return m.group(1)
    return None


def detect(filename: str):
    n = filename.lower()
    seg = ftype = None
    for pat, s, t in PATTERNS:
        if re.search(pat, n):
            seg, ftype = s, t
            break
    if ftype == "surveillance" and "bse" in n:
        seg = "BSECM"
    return {
        "segment": seg,
        "file_type": ftype,
        "trade_date": date_from_filename(filename),
        "cycle": cycle_from_filename(filename) if ftype == "span" else None,
    }
