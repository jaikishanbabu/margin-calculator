# Vendored: loader

Copied unchanged from the owner's `exchange-fetcher` project (Oct 2026): parses the exchange files of
one segment into one checked SQLite database (NFO, NSE cash, BFO, MCX). A load that fails its checks
raises and leaves nothing behind.

Stage 2C runs it on GitHub Actions (`app/build_job.py`), right after a fetch: the job rebuilds the
`data/<SEG>/<kind>/<YYYY-MM-DD>/` folders exfetch used to keep, from the engine's archive, calls
`load_segment`, and uploads the finished database ("snapshot") to the engine.

Rules that no daily exchange file carries live in `static_rates.yaml` and `mcx_contracts.yaml`
(versioned in Git; every snapshot records their SHA-256 and the Git commit it was built from).
Edit them through a pull request, then press Rebuild on the Data readiness page.
