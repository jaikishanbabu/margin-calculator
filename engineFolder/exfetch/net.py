"""HTTP per exchange: browser-like headers, cookie warm-up, retries, pacing,
and a headless-Chrome fallback for sites that refuse plain scripts.

A session with `impersonate: chrome` uses curl_cffi (Chrome's TLS fingerprint and
headers) instead of requests: MCX's bot check answers plain requests with an HTML
page. Its JSON calls must come back as JSON, else they count as refused."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def _transport_errors():
    """Exceptions meaning the connection failed, for requests and curl_cffi alike."""
    try:
        from curl_cffi.requests.exceptions import RequestException as CurlError
        return requests.RequestException, CurlError
    except ImportError:
        return (requests.RequestException,)


class NotPublished(Exception):
    """404/empty/HTML-instead-of-file: the file isn't there (yet)."""


class Blocked(Exception):
    """401/403/429, a bot page or a dropped connection: the site refused us."""


def _check(r, url: str, expect_file: bool):
    """Shared verdict on a response, whether it came from requests or the browser."""
    if r.status_code == 404:
        raise NotPublished(f"404 {url}")
    if r.status_code in (401, 403, 429):
        raise Blocked(f"{r.status_code} {url}")
    if r.status_code >= 400:
        raise Exception(f"{r.status_code} {url}")
    if expect_file:
        ctype = r.headers.get("Content-Type", "")
        head = r.content[:200].lstrip().lower()
        if not r.content:
            raise NotPublished(f"empty body {url}")
        # exchanges answer missing files with a 200 HTML error page
        if "text/html" in ctype and (head.startswith(b"<!doctype") or head.startswith(b"<html")):
            raise NotPublished(f"HTML page instead of file {url}")
    return r


def _json(r, url: str):
    try:
        return r.json()
    except ValueError:
        raise Blocked(f"non-JSON reply from {url} (probably a bot-check page)")


class Http:
    def __init__(self, sessions_cfg: dict, timeout: int = 60, retries: int = 3,
                 min_interval: float = 0.6, browser_headless: bool = True):
        self.cfg = sessions_cfg or {}
        self.timeout, self.retries, self.min_interval = timeout, retries, min_interval
        self._sessions: dict[str, requests.Session] = {}
        self._last_hit: dict[str, float] = {}
        self._browser = None
        self._browser_headless = browser_headless
        self._browser_hosts: set[str] = set()   # hosts that refused us this run: go straight to browser
        self._blocked_until: dict[str, float] = {}   # host -> time.time(): back-off after a block

    # -- sessions ----------------------------------------------------------
    def session(self, name: str | None) -> requests.Session:
        name = name or "default"
        if name in self._sessions:
            return self._sessions[name]
        conf = self.cfg.get(name, {})
        if conf.get("impersonate"):
            from curl_cffi import requests as curl     # optional: only sessions that ask for it
            s = curl.Session(impersonate=conf["impersonate"])   # sets Chrome's own UA and headers
        else:
            s = requests.Session()
            s.headers.update(BASE_HEADERS)
        s.headers.update(conf.get("headers", {}))
        self._sessions[name] = s
        if conf.get("warmup"):
            try:
                self._pace(conf["warmup"])
                s.get(conf["warmup"], timeout=self.timeout,
                      headers={"Accept": "text/html,application/xhtml+xml"})
            except _transport_errors() as e:
                print(f"[net] warm-up for {name} failed: {e}")
        return s

    def reset(self, name: str | None):
        self._sessions.pop(name or "default", None)

    def _pace(self, url: str):
        host = urlparse(url).netloc
        wait = self._last_hit.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # -- browser fallback ----------------------------------------------------
    def _browser_ok(self, session: str | None) -> bool:
        return bool(self.cfg.get(session or "", {}).get("browser_fallback"))

    def _via_browser(self, url, session, method="GET", body=None, as_file=False, remember=True):
        if self._browser is None:
            from .browser import Browser
            self._browser = Browser(headless=self._browser_headless)
        if remember:
            self._browser_hosts.add(urlparse(url).netloc)
        self._pace(url)
        warmup = self.cfg.get(session or "", {}).get("warmup")
        if as_file and method == "GET":
            return self._browser.download(url, warmup)    # open the link like a click
        return self._browser.fetch(url, method, body, warmup)

    def close(self):
        """End of a run: close Chrome and forget which hosts refused us, so one refusal
        doesn't send a host to the browser for the rest of the agent's life. Back-offs
        (backoff_after_block_min) are kept: they run on the clock."""
        if self._browser:
            self._browser.close()
            self._browser = None
        self._browser_hosts.clear()

    # -- back-off after a block (sessions with backoff_after_block_min) ------------
    def _check_backoff(self, url: str):
        host = urlparse(url).netloc
        until = self._blocked_until.get(host)
        if until and time.time() < until:
            raise Blocked(f"backing off {host} until {time.strftime('%H:%M', time.localtime(until))} "
                          f"(blocked earlier); no request sent")

    def _block(self, url: str, session: str | None, why: str) -> Blocked:
        minutes = self.cfg.get(session or "", {}).get("backoff_after_block_min")
        if minutes:
            host = urlparse(url).netloc
            self._blocked_until[host] = time.time() + 60 * float(minutes)
            why += f"; backing off {host} for {float(minutes):g} min"
        return Blocked(why)

    # -- one request, direct first, browser if refused ---------------------------
    def _request(self, url, session, method="GET", body=None, accept=None, as_file=False, want_json=False):
        conf = self.cfg.get(session or "", {})
        use_browser = self._browser_ok(session)
        self._check_backoff(url)
        # impersonated sessions: JSON calls sent like the page's own XHR, and an HTML reply is a refusal
        xhr = {}
        if conf.get("impersonate") and want_json:
            page = urlparse(conf.get("warmup") or url)
            xhr = {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
                   "Referer": conf.get("warmup") or url, "Origin": f"{page.scheme}://{page.netloc}"}
        if use_browser and urlparse(url).netloc in self._browser_hosts:
            return self._via_browser(url, session, method, body, as_file)   # known to block scripts
        # paths a site only serves to browsers (it answers scripts with a fake 404)
        paths = self.cfg.get(session or "", {}).get("browser_paths") or []
        if any(urlparse(url).path.lower().startswith(p.lower()) for p in paths):
            return self._via_browser(url, session, method, body, as_file, remember=False)
        last_err: Exception | None = None
        for attempt in range(1, conf.get("retries", self.retries) + 1):
            s = self.session(session)
            self._pace(url)
            try:
                if method == "POST":
                    r = s.post(url, json=body, timeout=self.timeout,
                               headers={"Accept": accept or "application/json, text/javascript, */*",
                                        "Content-Type": "application/json; charset=utf-8",
                                        "X-Requested-With": "XMLHttpRequest", **xhr})
                else:
                    h = {**({"Accept": accept} if accept else {}), **xhr}
                    r = s.get(url, timeout=self.timeout, headers=h or None)
            except _transport_errors() as e:   # dropped connection / TLS refusal
                last_err = Blocked(f"connection refused by {urlparse(url).netloc}: {type(e).__name__}")
                if use_browser:
                    break
                time.sleep(2 * attempt)
                continue
            if r.status_code in (401, 403, 429):
                last_err = Blocked(f"{r.status_code} {url}")
                if conf.get("backoff_after_block_min"):      # don't retry: wait it out
                    raise self._block(url, session, f"{r.status_code} {url}")
                if use_browser:
                    break
                self.reset(session)           # NSE drops cookies regularly: re-warm, retry
                time.sleep(3 * attempt)
                continue
            if r.status_code >= 500:
                last_err = Exception(f"{r.status_code} {url}")
                time.sleep(2 * attempt)
                continue
            if xhr and r.status_code == 200 and "json" not in r.headers.get("Content-Type", "").lower():
                head = " ".join(r.text[:200].split())
                last_err = Blocked(f"non-JSON reply (HTTP {r.status_code}, {r.headers.get('Content-Type') or 'no type'}) "
                                   f"from {url}: {head}")
                if conf.get("backoff_after_block_min"):
                    raise self._block(url, session, str(last_err))
                if use_browser:
                    break
                time.sleep(3)
                continue
            return r
        if use_browser:
            return self._via_browser(url, session, method, body, as_file)
        raise last_err or Exception(f"failed {url}")

    # -- public API ------------------------------------------------------------
    def get(self, url: str, session: str | None = None, expect_file: bool = True,
            accept: str | None = None):
        return _check(self._request(url, session, "GET", None, accept, as_file=expect_file),
                      url, expect_file)

    def get_json(self, url: str, session: str | None = None):
        r = self._request(url, session, "GET", None, "application/json, text/plain, */*", want_json=True)
        return _json(_check(r, url, False), url)

    def post_json(self, url: str, body, session: str | None = None):
        r = self._request(url, session, "POST", body, want_json=True)
        return _json(_check(r, url, False), url)
