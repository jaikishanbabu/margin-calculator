# Vendored: exfetch

Copied unchanged from the owner's `exchange-fetcher` project (Oct 2026), together with its
`sources.yaml` (here: `engineFolder/exfetch_sources.yaml`). It holds the working exchange URLs and
the workarounds found while building it (NSE cookie warm-up, BSE/MCX Chrome TLS impersonation via
curl_cffi, BSE notices -> bsedata rewrite, BSE ELM date token, MCX JSON endpoints).

Stage 2A uses only `Agent.probe()` (tries every strategy, saves nothing) via `app/probe.py`.
Stage 2B will adapt the fetch path to hand files to the upload pipeline instead of the local disk store.

## Local changes (holiday calendars)

- `dates.Calendar` reads every cached `holidays_<year>.json` (not only the current year), honours a
  `_special` key (sessions on normally closed days, e.g. Muhurat Sunday), and knows `BSE` and `MCX`
  lists (falling back to NSE's `FO` / `COM` when absent). The engine's Holiday calendar page writes
  these files for the loader on GitHub.
