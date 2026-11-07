"""Turn one `try:` strategy into concrete download URLs (or raw bytes).

Every resolver returns a list of Candidate(url, name, data). `data` is set only
when the resolver already holds the bytes (json_api, browser downloads).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, unquote

from .dates import render
from .net import Http, NotPublished


@dataclass
class Candidate:
    url: str
    name: str
    data: bytes | None = None
    rank: int = 0          # higher = newer version of the same day's file


def _basename(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download"


# --------------------------------------------------------------------------
# template: URL straight from tokens
# --------------------------------------------------------------------------
def resolve_template(spec: str, d: date, loop: dict, http: Http, session: str | None):
    url = render(spec, d, **loop)
    return [Candidate(url, _basename(url))]


# --------------------------------------------------------------------------
# nse_reports: NSE's All-Reports JSON listing
#   GET https://www.nseindia.com/api/daily-reports?key=FO|CM|CD|COM
#   -> {"CurrentDay":[...], "PreviousDay":[...], "FutureDay":[...]}
#   each row has displayName / fileActlName / filePath / tradingDate
# For past dates it falls back to the archive endpoint the same page uses.
# --------------------------------------------------------------------------
_NSE_SECTIONS = {"FO": ("equity", "derivatives"), "CM": ("equities", "capital-market"),
                 "CD": ("currency", "derivatives"), "COM": ("commodity", "derivatives")}


def _nse_rows(payload) -> list[dict]:
    rows = []
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                rows.extend(r for r in v if isinstance(r, dict))
    elif isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
    return rows


def _nse_row_url(row: dict) -> str | None:
    path = row.get("filePath") or row.get("link") or ""
    name = row.get("fileActlName") or ""
    if not path:
        return None
    if name and not path.rstrip("/").endswith(name):
        path = path.rstrip("/") + "/" + name
    return urljoin("https://nsearchives.nseindia.com/", path)


def _nse_row_date(row: dict) -> date | None:
    from datetime import datetime
    for k in ("tradingDate", "fileDate", "date"):
        s = row.get(k)
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(s)[:11], fmt).date()
            except (TypeError, ValueError):
                continue
    return None


def resolve_nse_reports(spec: dict, d: date, loop: dict, http: Http, session: str | None):
    pattern = re.compile(render(spec["match"], d, **loop))
    key = spec.get("key", "FO")
    rows = _nse_rows(http.get_json(f"https://www.nseindia.com/api/daily-reports?key={key}", session))
    hits = []
    for row in rows:
        text = " ".join(str(row.get(k, "")) for k in ("displayName", "fileActlName", "filePath"))
        if not pattern.search(text):
            continue
        rd = _nse_row_date(row)
        if rd and rd != d:
            continue          # listing covers yesterday/today/tomorrow; keep ours
        url = _nse_row_url(row)
        if url:
            hits.append(Candidate(url, row.get("fileActlName") or _basename(url)))
    if not hits:
        raise NotPublished(f"nse_reports key={key}: nothing matches {pattern.pattern} for {d}")
    return hits if spec.get("all") else hits[:1]


# --------------------------------------------------------------------------
# Shared JSON helpers
# --------------------------------------------------------------------------
_LIST_KEYS = ("Data", "data", "d", "Table", "Table1", "items", "rows")


def _fill(obj, d: date, loop: dict):
    if isinstance(obj, str):
        return render(obj, d, **loop)
    if isinstance(obj, dict):
        return {k: _fill(v, d, loop) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, d, loop) for v in obj]
    return obj


def _call(spec: dict, d: date, loop: dict, http: Http, session: str | None):
    url = render(spec["url"], d, **loop)
    if spec.get("method", "GET").upper() == "POST":
        return url, http.post_json(url, _fill(spec.get("body", {}), d, loop), session)
    return url, http.get_json(url, session)


def _items(payload, path: str | None):
    """Find the list of records in an API reply; refuse 'success: false' replies."""
    if isinstance(payload, dict):
        for flag in ("success", "IsSuccess"):
            if payload.get(flag) is False:
                raise NotPublished(f"API says: {payload.get('message') or payload.get('Message') or 'no data'}")
    node = payload
    if path:
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
    elif isinstance(payload, dict):
        node = next((payload[k] for k in _LIST_KEYS if isinstance(payload.get(k), list)), None)
    if not isinstance(node, list) or not node:
        raise NotPublished("API returned no records")
    return node


def _to_csv(rows: list[dict]) -> bytes:
    import csv
    import io
    cols = list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
    return buf.getvalue().encode()


# --------------------------------------------------------------------------
# json_api: save what an API returns. Either a plain URL string (saved as JSON)
# or a dict: {url, method, body, items, to_csv, keep_max, sort_rows, save_as}
#   keep_max: field  -> keep only rows with the highest value (latest version)
#   split_by: field  -> one file per value of that field (each version of the day's file:
#                       MCX margins fileID 1..N), named with {version} in save_as, ranked by it
#   sort_rows: true  -> sort rows (the API returns them in a different order each call,
#                       which would make an unchanged file look new on every refresh)
# --------------------------------------------------------------------------
def resolve_json_api(spec, d: date, loop: dict, http: Http, session: str | None,
                     save_as: str | None = None):
    if isinstance(spec, str):
        url = render(spec, d, **loop)
        data = http.get_json(url, session)
        return [Candidate(url, save_as or (_basename(url) + ".json"),
                          json.dumps(data, indent=1).encode())]
    url, payload = _call(spec, d, loop, http, session)
    rows = _items(payload, spec.get("items"))
    if spec.get("keep_max"):
        f = spec["keep_max"]
        top = max(r.get(f) or 0 for r in rows)
        rows = [r for r in rows if (r.get(f) or 0) == top]
    if spec.get("sort_rows"):
        cols = list(dict.fromkeys(k for r in rows for k in r))
        rows = sorted(rows, key=lambda r: [str(r.get(k)) for k in cols])
    template = spec.get("save_as") or save_as or (_basename(url) + ".json")
    encode = (lambda rs: _to_csv(rs)) if spec.get("to_csv") else (lambda rs: json.dumps(rs, indent=1).encode())
    if spec.get("split_by"):
        f = spec["split_by"]
        groups: dict = {}
        for r in rows:
            groups.setdefault(r.get(f) or 0, []).append(r)
        return [Candidate(url, render(template, d, **loop, version=v), encode(rs), rank=int(v))
                for v, rs in sorted(groups.items(), key=lambda kv: int(kv[0]))]
    return [Candidate(url, render(template, d, **loop), encode(rows))]


# --------------------------------------------------------------------------
# json_list: an API that lists the day's files (BSE LoadData, MCX SPAN list).
#   {url, method, body, items, file: <field with the link>, base: <prefix for
#    relative links>, sort: <field to order by, oldest first>}
# Returns every listed file, oldest first; the runner keeps all versions and
# copies the newest into latest/.
# --------------------------------------------------------------------------
def resolve_json_list(spec: dict, d: date, loop: dict, http: Http, session: str | None):
    url, payload = _call(spec, d, loop, http, session)
    rows = _items(payload, spec.get("items"))
    if spec.get("sort"):
        rows = sorted(rows, key=lambda r: str(r.get(spec["sort"]) or ""))
    out = []
    for i, r in enumerate(rows):
        link = (r.get(spec["file"]) or "").strip()
        if not link:
            continue
        for old, new in (spec.get("rewrite") or {}).items():   # same fix-up the site's page applies
            link = link.replace(old, new)
        full = urljoin(spec.get("base", url), link)
        out.append(Candidate(full, _basename(full), rank=i))
    if not out:
        raise NotPublished(f"json_list {url}: no file links in field {spec['file']!r}")
    return out


# --------------------------------------------------------------------------
# html: scan a page's links. render=true uses Playwright (JS / ASPX pages)
# --------------------------------------------------------------------------
class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def page_links(page: str, http: Http, session: str | None, render_js: bool) -> list[tuple[str, str]]:
    if render_js:
        from .browser import rendered_links
        return rendered_links(page, http.cfg.get(session or "", {}).get("headers", {}))
    r = http.get(page, session, expect_file=False, accept="text/html")
    p = _Links()
    p.feed(r.text)
    return [(urljoin(page, h), t) for h, t in p.links if h and not h.startswith(("javascript:", "#", "mailto:"))]


def resolve_html(spec: dict, d: date, loop: dict, http: Http, session: str | None):
    page = render(spec["page"], d, **loop)
    pattern = re.compile(render(spec["match"], d, **loop))
    links = page_links(page, http, session, spec.get("render", False))
    hits = [(h, t) for h, t in links if pattern.search(h) or pattern.search(t)]
    if spec.get("prefer"):
        pref = re.compile(render(spec["prefer"], d, **loop))
        dated = [x for x in hits if pref.search(x[0]) or pref.search(x[1])]
        hits = dated or hits
    if not hits:
        raise NotPublished(f"html {page}: no link matches {pattern.pattern}")
    seen, out = set(), []
    for h, _ in hits:
        if h not in seen:
            seen.add(h)
            out.append(Candidate(h, _basename(h)))
    return out if spec.get("all") else out[:1]


def resolve(strategy: dict, d: date, loop: dict, http: Http, session: str | None):
    if "template" in strategy:
        return resolve_template(strategy["template"], d, loop, http, session)
    if "nse_reports" in strategy:
        return resolve_nse_reports(strategy["nse_reports"], d, loop, http, session)
    if "json_api" in strategy:
        return resolve_json_api(strategy["json_api"], d, loop, http, session, strategy.get("save_as"))
    if "json_list" in strategy:
        return resolve_json_list(strategy["json_list"], d, loop, http, session)
    if "html" in strategy:
        return resolve_html(strategy["html"], d, loop, http, session)
    raise ValueError(f"unknown strategy {list(strategy)}")
