"""Exchange holiday calendars: parsing imports, trading-day rules, the admin flow, the loader cache."""
import json
from datetime import date

import pytest

from app import config, fetcher, holidays
from conftest import login, make_admin

FT = "test-fetcher-token-0123456789abcdef0123456789"

NSE_API = {"CM": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"},
                  {"tradingDate": "25-Dec-2026", "description": "Christmas"}],
           "FO": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"},
                  {"tradingDate": "25-Dec-2026", "description": "Christmas"}],
           "CD": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"},
                  {"tradingDate": "27-Mar-2026", "description": "Annual bank closing"}]}

MCX_HTML = """<table><tr><th>Sr</th><th>Date</th><th>Day</th><th>Description</th><th>Morning Session</th><th>Evening Session</th></tr>
<tr><td>1</td><td>26-Jan-2026</td><td>Monday</td><td>Republic Day</td><td>Closed</td><td>Closed</td></tr>
<tr><td>2</td><td>10-Nov-2026</td><td>Tuesday</td><td>Diwali-Balipratipada</td><td>Closed</td><td>Open</td></tr>
<tr><td>3</td><td>25-Dec-2026</td><td>Friday</td><td>Christmas</td><td>Open</td><td>Closed</td></tr></table>"""

BSE_HTML = """<table><tr><td>Republic Day</td><td>January 26, 2026</td><td>Monday</td></tr>
<tr><td>Diwali Laxmi Pujan* (Muhurat Trading)</td><td>November 08, 2026</td><td>Sunday</td></tr>
<tr><td>Christmas</td><td>25/12/2026</td><td>Friday</td></tr></table>"""


def test_parse_nse_api_per_segment():
    nse = holidays.parse_nse_api(NSE_API, "NSE")
    assert [e["day"] for e in nse] == ["2026-11-10", "2026-12-25"] and nse[0]["description"] == "Diwali-Balipratipada"
    cd = holidays.parse_nse_api(NSE_API, "NSE_CD")
    assert [e["day"] for e in cd] == ["2026-03-27", "2026-11-10"]


def test_parse_mcx_sessions_and_bse_formats():
    mcx = {e["day"]: e["kind"] for e in holidays.parse_html_table(MCX_HTML, "MCX")}
    assert mcx == {"2026-01-26": "closed", "2026-11-10": "morning_closed", "2026-12-25": "evening_closed"}
    bse = {e["day"]: e for e in holidays.parse_html_table(BSE_HTML, "BSE")}
    assert set(bse) == {"2026-01-26", "2026-11-08", "2026-12-25"}
    assert bse["2026-11-08"]["kind"] == "special_session" and bse["2026-01-26"]["kind"] == "closed"
    assert holidays.parse_html_table("<p>nothing here</p>", "BSE") == []


def test_trading_day_rules():
    cal = holidays.Cal([{"exchange": "NSE", "day": "2026-11-10", "kind": "closed"},
                        {"exchange": "NSE", "day": "2026-11-08", "kind": "special_session"},
                        {"exchange": "MCX", "day": "2026-11-10", "kind": "morning_closed"}])
    assert not cal.is_trading_day("NSE", date(2026, 11, 10))
    assert cal.is_trading_day("MCX", date(2026, 11, 10))           # evening session: MCX publishes files
    assert cal.is_trading_day("NSE", date(2026, 11, 8))            # Muhurat Sunday
    assert not cal.is_trading_day("NSE", date(2026, 11, 7))        # ordinary Saturday
    assert cal.next_trading_day("NSE", date(2026, 11, 10)) == date(2026, 11, 11)
    assert cal.has_year("NSE", 2026) and not cal.has_year("BSE", 2026)


def test_loader_cache_drives_exfetch_calendar(tmp_path):
    from exfetch.dates import Calendar
    cal = holidays.Cal([{"exchange": "NSE", "day": "2026-11-10", "kind": "closed"},
                        {"exchange": "NSE", "day": "2026-11-08", "kind": "special_session"},
                        {"exchange": "MCX", "day": "2026-11-10", "kind": "morning_closed"},
                        {"exchange": "MCX", "day": "2026-12-25", "kind": "closed"},
                        {"exchange": "NSE", "day": "2025-12-25", "kind": "closed"}])
    (tmp_path / "holidays_2026.json").write_text(json.dumps(cal.loader_cache(2026)))
    (tmp_path / "holidays_2025.json").write_text(json.dumps(cal.loader_cache(2025)))
    c = Calendar(tmp_path)
    assert not c.is_trading_day(date(2026, 11, 10), "NFO")
    assert c.is_trading_day(date(2026, 11, 10), "MCX")              # MCX's own list
    assert not c.is_trading_day(date(2026, 12, 25), "MCX")
    assert not c.is_trading_day(date(2026, 11, 10), "BFO")         # no BSE list -> NSE's
    assert c.is_trading_day(date(2026, 11, 8), "NFO")              # special session
    assert c.previous_trading_day(date(2026, 11, 9), "NFO") == date(2026, 11, 8)
    assert c.previous_trading_day(date(2026, 1, 1), "NFO") == date(2025, 12, 31)
    assert not c.is_trading_day(date(2025, 12, 25), "NFO")          # earlier year's file read too


@pytest.fixture()
def hx(client, monkeypatch):
    monkeypatch.setattr(config, "FETCHER_TOKEN", FT)
    monkeypatch.setattr(fetcher, "dispatch_github", lambda rid: None)
    return {"c": client, "sa": login(client), "job": {"X-Fetcher-Token": FT}}


def test_manual_days_superadmin_only_and_audited(hx):
    c, sa = hx["c"], hx["sa"]
    ad = make_admin(c, sa)
    body = {"exchange": "MCX", "day": "2026-11-10", "kind": "morning_closed", "description": "Diwali-Balipratipada"}
    assert c.put("/api/admin/calendar/day", json=body, headers=ad).status_code == 403
    assert c.put("/api/admin/calendar/day", json=dict(body, kind="nope"), headers=sa).status_code == 400
    assert c.put("/api/admin/calendar/day", json=dict(body, day="10-11-2026"), headers=sa).status_code == 400
    assert c.put("/api/admin/calendar/day", json=body, headers=sa).status_code == 200
    ov = c.get("/api/admin/calendar?year=2026", headers=ad).json()       # admins can read
    assert [d["day"] for d in ov["exchanges"]["MCX"]["days"]] == ["2026-11-10"] and ov["can_edit"] is False
    assert any("NSE - equity and F&O: no 2026 calendar" in w["text"] for w in ov["warnings"])
    assert c.request("DELETE", "/api/admin/calendar/day", json={"exchange": "MCX", "day": "2026-11-10"},
                     headers=sa).status_code == 200
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=sa).json()["entries"]]
    assert "calendar_day_set" in acts and "calendar_day_removed" in acts


def test_import_is_a_proposal_until_approved(hx):
    c, sa, job = hx["c"], hx["sa"], hx["job"]
    holidays.set_day({"username": "superadmin"}, "NSE", "2026-01-26", "closed", "Republic Day")
    r = c.post("/api/admin/calendar/import", json={}, headers=sa)
    assert r.status_code == 202 and r.json()["run"]["trigger"] == "calendar"
    rid = r.json()["run"]["id"]
    url = f"/api/fetcher/runs/{rid}/calendar-proposal"
    found = [{"day": "2026-11-10", "kind": "closed", "description": "Diwali-Balipratipada"},
             {"day": "2026-12-25", "kind": "closed", "description": "Christmas"},
             {"day": "2027-01-26", "kind": "closed", "description": "next year - dropped from the 2026 proposal"}]
    assert c.post(url, json={"exchange": "NSE", "year": 2026, "found": found}, headers=job).status_code == 409   # not running
    c.post(f"/api/fetcher/runs/{rid}/start", json={}, headers=job)
    assert c.post(url, json={"exchange": "NSE", "year": 2026, "found": found}, headers=sa).status_code == 403
    p = c.post(url, json={"exchange": "NSE", "year": 2026, "found": found, "source": "https://nse"}, headers=job).json()["proposal"]
    assert [e["day"] for e in p["diff"]["add"]] == ["2026-11-10", "2026-12-25"] and p["diff"]["missing"] == []
    assert len(holidays.entries(2026, "NSE")) == 1                               # nothing applied yet
    ad = make_admin(c, sa)
    assert c.post("/api/admin/calendar/approve", json={"exchange": "NSE", "year": 2026}, headers=ad).status_code == 403
    r = c.post("/api/admin/calendar/approve", json={"exchange": "NSE", "year": 2026}, headers=sa)
    assert r.status_code == 200 and r.json() == {"added": 2, "changed": 0}
    assert [e["day"] for e in holidays.entries(2026, "NSE")] == ["2026-01-26", "2026-11-10", "2026-12-25"]
    assert c.get("/api/admin/calendar?year=2026", headers=sa).json()["proposals"] == []
    assert c.post("/api/admin/calendar/approve", json={"exchange": "NSE", "year": 2026}, headers=sa).status_code == 404
    assert c.post("/api/admin/calendar/bogus", json={}, headers=sa).status_code == 404
    acts = [e["action"] for e in c.get("/api/admin/audit", headers=sa).json()["entries"]]
    assert {"calendar_import_fetched", "calendar_import_approved"} <= set(acts)


def test_nse_bse_mismatch_warns(hx):
    a = {"username": "superadmin"}
    holidays.set_day(a, "NSE", "2026-11-10", "closed")
    holidays.set_day(a, "BSE", "2026-11-10", "closed")
    holidays.set_day(a, "BSE", "2026-11-24", "closed")
    w = holidays.warnings(today=date(2026, 6, 1))
    assert any("differ on 2026-11-24" in x["text"] for x in w)


def test_fetcher_uploads_take_the_segments_own_date(hx):
    run = {"trade_date": "20261110"}
    assert fetcher.upload_trade_date(run, "") == "20261110"
    assert fetcher.upload_trade_date(run, "20261111") == "20261111"
    with pytest.raises(ValueError):
        fetcher.upload_trade_date(run, "20261201")
    with pytest.raises(ValueError):
        fetcher.upload_trade_date(run, "11-11-2026")


def test_each_segment_follows_its_own_exchange():
    from app.fetch_job import segment_dates
    cal = holidays.Cal([{"exchange": "NSE", "day": "2026-11-10", "kind": "closed"},
                        {"exchange": "BSE", "day": "2026-11-10", "kind": "closed"},
                        {"exchange": "MCX", "day": "2026-11-10", "kind": "morning_closed"}])
    segs = {"NFO", "BFO", "MCX", "NSECM", "CDS"}
    tr = cal.is_trading_day
    # Monday 23:30 run for Tuesday (an NSE/BSE holiday; MCX's evening session trades)
    dates, skip = segment_dates(date(2026, 11, 10), date(2026, 11, 9), "auto", segs, tr)
    assert dates["NFO"] == dates["BFO"] == dates["NSECM"] == date(2026, 11, 11)
    assert dates["CDS"] == date(2026, 11, 10)        # no NSE_CD days in this calendar (the job falls back to NSE's list)
    assert dates["MCX"] == date(2026, 11, 10) and not skip
    # Tuesday daytime auto run: NSE/BSE skipped, MCX fetched
    dates, skip = segment_dates(date(2026, 11, 10), date(2026, 11, 10), "auto", segs, tr)
    assert set(skip) == {"NFO", "BFO", "NSECM"} and "MCX" not in skip
    # a manual run on a holiday still fetches what was asked
    _, skip = segment_dates(date(2026, 11, 10), date(2026, 11, 10), "manual", segs, tr)
    assert not skip
