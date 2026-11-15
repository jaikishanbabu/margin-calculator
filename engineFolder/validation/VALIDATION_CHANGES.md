# Changes to the owner's tests

`tests/test_engine_nfo.py` and `tests/fixtures/*.yaml` are the owner's files, copied unchanged
except for the following. Each change keeps what the test checks and only removes a dependency on
the exact copy of an exchange file the owner happened to download.

1. `test_no_risk_data_and_errors` (10 Nov 2026, approved by the owner): the example of "a listed
   contract with no SPAN risk array" was RVNL 27-Oct-2026 90 PE. NSE adds strikes during the day,
   and the contract master in NSE's archive (used on GitHub) doesn't list that strike, so the engine
   rightly called it unknown instead. The test now takes any listed NSE contract without a risk
   array from the day's data (RVNL 90 PE first, if present) and skips, saying so, if there is none.

## `tests/test_engine_nse.py` (NSE cash)

2. `test_load_info_and_batches` (15 Nov 2026, approved by the owner): checked that the 26-Oct VaR
   file was fetched on 26 Oct (`var_fetched_at`). Files downloaded later from NSE's archive carry
   the download time, so the test now only checks that a fetch time is recorded.
3. `test_t2t_asm_gsm_and_no_var` (15 Nov 2026, approved by the owner): used ADVANCE (short-term ASM)
   and AQYLON (GSM) as of October. NSE publishes only its current ASM / GSM lists, so October's
   can't be downloaded. The validation job loads the newest lists the engine has archived (the
   report says which) and the test checks the same rules on stocks that are on them: a short-term
   ASM stock gets max(1.5 x (VaR + ELM), 40) and the warning, a long-term ASM stock 100%, a GSM
   stock NSE's rate and the warning. The trade-for-trade and no-VaR checks are unchanged.

The basket runner (`run_nse.py`, not the owner's file) also changed: VaR batches downloaded after a
broker quote have fetch times later than the quote, so none counted as "live" and no batch was tried.
For such files it now picks batches by NSE's schedule (batch 1 start of day, 2-4 intraday, 5-6 end
of day): the quote day's batch 1 (2-4 only after the close), then the previous day's batches.
