"""Headless Chrome, used only where a site refuses plain scripts (MCX, BSE notices).

The request is made with fetch() from inside a page on the same site, so it
carries real browser TLS, headers and cookies - exactly like clicking on the page.

Setup once:  pip install playwright && playwright install chromium
"""
from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

from .net import UA

_LINKS_JS = """() => Array.from(document.querySelectorAll('a[href]')).map(a => [a.href, (a.innerText||'').trim()])"""

_FETCH_JS = """async ({url, method, body}) => {
  const opts = {method, credentials: 'include', headers: {}};
  if (body !== null) {
    opts.body = body;
    opts.headers['Content-Type'] = 'application/json; charset=utf-8';
    opts.headers['X-Requested-With'] = 'XMLHttpRequest';
  }
  const r = await fetch(url, opts);
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return {status: r.status, ctype: r.headers.get('content-type') || '', b64: btoa(bin)};
}"""


def _need_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError("This site needs a headless browser: "
                           "pip install playwright && playwright install chromium") from e


class BrowserResponse:
    """Just enough of requests.Response for the rest of the agent."""

    def __init__(self, url: str, status: int, ctype: str, content: bytes):
        self.url, self.status_code, self.content = url, status, content
        self.headers = {"Content-Type": ctype}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class Browser:
    """One headless Chrome for the whole run; one tab per website origin."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = self._browser = self._ctx = None
        self._pages: dict[str, object] = {}

    def _start(self):
        if self._ctx:
            return
        _need_playwright()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:   # prefer the real installed Chrome; fall back to Playwright's Chromium
            self._browser = self._pw.chromium.launch(channel="chrome", headless=self.headless)
        except Exception:
            self._browser = self._pw.chromium.launch(headless=self.headless)
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-IN",
                                              timezone_id="Asia/Kolkata", accept_downloads=True)

    def _page_for(self, url: str, warmup: str | None):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        page = self._pages.get(origin)
        if page is None or page.is_closed():
            self._start()
            page = self._ctx.new_page()
            # land on the same origin first so fetch() is same-site and gets the cookies;
            # a 404 landing page is fine, we only need the origin
            start = warmup if warmup and warmup.startswith(origin) else origin + "/"
            try:
                page.goto(start, wait_until="load", timeout=60000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            self._pages[origin] = page
        return page

    def fetch(self, url: str, method: str = "GET", body=None, warmup: str | None = None) -> BrowserResponse:
        """API calls (JSON): fetch() from a page on the same site."""
        payload = None if body is None else json.dumps(body)
        for attempt in (1, 2):
            page = self._page_for(url, warmup)
            try:
                r = page.evaluate(_FETCH_JS, {"url": url, "method": method, "body": payload})
                return BrowserResponse(url, r["status"], r["ctype"], base64.b64decode(r["b64"]))
            except Exception as e:
                # landing page still redirecting: let it settle, then retry once
                if attempt == 2 or "context was destroyed" not in str(e):
                    raise
                try:
                    page.wait_for_load_state("load", timeout=30000)
                except Exception:
                    pass

    def download(self, url: str, warmup: str | None = None) -> BrowserResponse:
        """Files: open the link in a tab, as a person clicking it would, and keep
        what the browser downloads. Works across sites and with plain http links."""
        self._start()
        page = self._ctx.new_page()
        got = []
        page.on("download", lambda d: got.append(d))
        try:
            if warmup:                       # pick up the site's cookies first
                try:
                    page.goto(warmup, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            resp = None
            try:
                resp = page.goto(url, timeout=90000, referer=warmup or None)
            except Exception as e:           # Chrome aborts navigation when a download starts
                if "Download is starting" not in str(e) and "net::ERR_ABORTED" not in str(e):
                    raise
            if not got:
                page.wait_for_timeout(1500)  # download event can land just after goto returns
            if got:
                dl = got[0]
                if dl.failure():
                    raise Exception(f"browser download failed: {dl.failure()}")
                with open(dl.path(), "rb") as f:
                    return BrowserResponse(url, 200, "application/octet-stream", f.read())
            if resp is None:
                raise Exception(f"browser got no response for {url}")
            return BrowserResponse(url, resp.status, resp.headers.get("content-type", ""), resp.body())
        finally:
            page.close()

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._ctx = None
            self._pages.clear()


def rendered_links(page: str, headers: dict | None = None, wait_ms: int = 4000) -> list[tuple[str, str]]:
    _need_playwright()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, extra_http_headers=headers or {})
        pg = ctx.new_page()
        pg.goto(page, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(wait_ms)
        links = [tuple(x) for x in pg.evaluate(_LINKS_JS)]
        b.close()
    return [(h, t) for h, t in links if h and not h.startswith(("javascript:", "mailto:"))]
