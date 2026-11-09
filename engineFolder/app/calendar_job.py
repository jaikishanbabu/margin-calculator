"""Holiday-calendar import - runs on GitHub (the exchanges' sites block Render).

Fetches each exchange's holiday page, reads the dates and sends them to the engine as a proposal;
the SuperAdmin reviews and approves it on the Holiday calendar page before anything changes.
"""
from app import holidays

SESSION = {"NSE": "nse", "NSE_CD": "nse", "BSE": "bse", "MCX": "mcx"}


def import_all(eng, run_id, http, exchanges=None, today=None, log=print):
    today = today or _today()
    srcs = eng.call("GET", "/api/fetcher/calendar").json()["sources"]
    years = [today.year, today.year + 1]
    counts = {}
    nse_raw = None
    for ex in exchanges or list(holidays.EXCHANGES):
        src = srcs[ex]
        found, used, errors = [], None, []
        for url in src["urls"]:
            try:
                if src["method"] == "nse_api":
                    nse_raw = nse_raw or http.get_json(url, SESSION[ex])
                    found = holidays.parse_nse_api(nse_raw, ex)
                else:
                    r = http.get(url, SESSION[ex], expect_file=False, accept="text/html,application/xhtml+xml")
                    found = holidays.parse_html_table(r.text, ex)
                if found:
                    used = url
                    break
                errors.append(f"{url}: no dates found on the page")
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}: {str(e)[:160]}")
        for y in years:
            ys = [e for e in found if e["day"].startswith(str(y))]
            if not ys and y != today.year:
                continue
            err = None if ys else ("; ".join(errors) or f"no {y} dates found")
            eng.call("POST", f"/api/fetcher/runs/{run_id}/calendar-proposal",
                     json={"exchange": ex, "year": y, "found": ys, "source": used or src["urls"][0], "error": err})
            st = "archived" if ys else "failed"
            counts[st] = counts.get(st, 0) + 1
            eng.item(run_id, source_ref=f"calendar:{ex}:{y}", segment=ex[:10], file_type="calendar", status=st,
                     detail=(f"{len(ys)} days found on {used} - review on the Holiday calendar page" if ys else err)[:480])
            log(f"  {st:9} calendar:{ex}:{y} {len(ys)} days {err or ''}")
    return counts


def _today():
    from exfetch.dates import today_ist
    return today_ist()
