# Changes to the owner's tests

`tests/test_engine_nfo.py` and `tests/fixtures/*.yaml` are the owner's files, copied unchanged
except for the following. Each change keeps what the test checks and only removes a dependency on
the exact copy of an exchange file the owner happened to download.

1. `test_no_risk_data_and_errors` (10 Nov 2026, approved by the owner): the example of "a listed
   contract with no SPAN risk array" was RVNL 27-Oct-2026 90 PE. NSE adds strikes during the day,
   and the contract master in NSE's archive (used on GitHub) doesn't list that strike, so the engine
   rightly called it unknown instead. The test now takes any listed NSE contract without a risk
   array from the day's data (RVNL 90 PE first, if present) and skips, saying so, if there is none.
