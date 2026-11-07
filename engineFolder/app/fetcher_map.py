"""Which exfetch source feeds which admin segment / file type.

Admin segments: NFO BFO CDS MCX NSECM BSECM. exfetch puts NSE and BSE cash under one
segment "EQ"; here they are split by exchange.
"""

SOURCE_MAP = {
    # NSE F&O
    "nfo.span": ("NFO", "span"),
    "nfo.contract_master": ("NFO", "contract_master"),
    "nfo.market_lots": ("NFO", "lot_sizes"),
    "nfo.elm": ("NFO", "elm"),
    "nfo.bhavcopy": ("NFO", "bhavcopy"),
    "nfo.settlement_prices": ("NFO", "settlement"),
    "nfo.ban_list": ("NFO", "ban_list"),
    # BSE F&O
    "bfo.span_xml": ("BFO", "span"),
    "bfo.span_spn": ("BFO", "span"),
    "bfo.elm": ("BFO", "elm"),
    "bfo.bhavcopy": ("BFO", "bhavcopy"),
    # MCX
    "mcx.span": ("MCX", "span"),
    "mcx.margins": ("MCX", "elm"),
    "dhan.scrip_master": ("MCX", "contract_master"),
    "mcx.bhavcopy": ("MCX", "bhavcopy"),
    # NSE currency
    "cds.span": ("CDS", "span"),
    "cds.contract_master": ("CDS", "contract_master"),
    "cds.elm": ("CDS", "elm"),
    "cds.bhavcopy": ("CDS", "bhavcopy"),
    # NSE cash
    "nse.var": ("NSECM", "var"),
    "nse.bhavcopy": ("NSECM", "bhavcopy"),
    "nse.regulatory_indicator": ("NSECM", "surveillance"),
    "nse.security": ("NSECM", "security_master"),
    "nse.asm": ("NSECM", "surveillance"),
    "nse.gsm": ("NSECM", "surveillance"),
    "nse.security_list": ("NSECM", "surveillance"),
    # BSE cash
    "bse.var": ("BSECM", "var"),
    "bse.bhavcopy": ("BSECM", "bhavcopy"),
}

SEGMENT_CHOICES = ["NFO", "BFO", "CDS", "MCX", "NSECM", "BSECM"]


def sources_for(segments):
    """['ALL'] or a list of admin segments -> exfetch source ids."""
    if not segments or "ALL" in segments:
        return list(SOURCE_MAP)
    return [sid for sid, (seg, _) in SOURCE_MAP.items() if seg in segments]
