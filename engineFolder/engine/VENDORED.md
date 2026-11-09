# Vendored: engine

Copied unchanged from the owner's `exchange-fetcher` project (Oct 2026): positions + a parsed
segment database -> SPAN, exposure and total margin (NFO; BFO/MCX/NSE cash classes too).

The engine serves `/api/margin` from the snapshot Data readiness marks as in use (app/margin.py).
Validation: `validation/tests/test_engine_nfo.py` (the owner's tests, unchanged) and the broker
baskets in `validation/tests/fixtures/` run on GitHub (`.github/workflows/validate.yml`).
