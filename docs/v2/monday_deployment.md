# V2 PAPER deployment — Monday 2026-10-26

## Startup commands (what the scheduled tasks run)

```
# $1,000 ledger (SMALL_ACCOUNT_PAPER), 47 symbols — task \OptioBot\SmallAccountEngine, 08:15 CT
.venv\Scripts\python.exe -u -m trader.cli paper-session --strategy v2 --account-profile small

# $10,000 ledger (ORIGINAL_ACCOUNT_PAPER), the original 14 symbols — task \OptioBot\PaperEngine, 08:20 CT
.venv\Scripts\python.exe -u -m trader.cli paper-session --strategy v2
```

Rollback to the original strategy: remove `--strategy v2` from both task actions (nothing else changes; the original
strategy files were never modified).

## Before the open (automatic + manual)

| # | Check | How | Pass condition |
|---|---|---|---|
| 1 | Clock | `w32tm` / engine startup | offset ≤ 2 s (engine refuses otherwise) |
| 2 | Config and manifests | `python -m trader.cli v2-validate` | base hash `c597cfe8…`, V2 manifest activated and matching |
| 3 | Live data | `python -m trader.cli v2-readiness` (task `\OptioBot\V2Readiness`, 08:05 CT) | overall READY: auth, quote token, VIX, market metrics (IV rank + earnings), daily history, correlation measured, option quotes with Greeks; stream/intraday checks re-run once the market is open |
| 4 | Ledgers | readiness `two_isolated_paper_ledgers` | $10K at `data/`, $1K at `data/profiles/small_account_paper/`, 0 open positions, fresh balances |
| 5 | Kill switch / Watchdog | unchanged | whatever state they are in is left as is |
| 6 | Tests | `python -m pytest -q` | all pass (mocked; does not prove the live connection) |

## After the open (first 30 minutes)

| Check | Where | Expect |
|---|---|---|
| Both engines RUNNING | `engine_heartbeat.json` in each ledger dir | `lifecycle_state` RUNNING, fresh `last_bar_at_utc` |
| Context populated | `v2_context_snapshot.json` | SPY/QQQ/IWM fresh with bias; regime UNCLEAR until ~09:46 ET, then a classified regime; sectors fresh |
| Decisions explainable | `data/v2_dashboard.html` (refreshed every 2 min) and `v2_decisions.jsonl` | every candidate has 12 stages with a reason |
| Benchmark running | `v2_shadow_original/` in each ledger dir | decisions.jsonl growing; entries when the original would trade |
| No real orders | PAPER mode; V2 refuses LIVE | `TRADING_MODE=PAPER`, `ENABLE_LIVE_TRADING=false` |

## Known scheduled events next week

* Wed 10/28 FOMC statement (14:00 ET customary): V2 delays entries 13:30–15:00 ET and flags open positions.
* Thu 10/29 08:30 ET GDP advance + PCE (before the open; flagged, no block).
* Earnings (tastytrade): VZ 10/26 BTO, KO 10/27 BTO, HOOD 10/27 AMC, SOFI/PYPL 10/27 (time not given: treated as both),
  GOOGL and F 10/28 AMC, INTC and RIVN 10/29 AMC, AMZN 10/29 (time not given), AAPL and PLTR 11/02. V2 rejects entries
  in a stock that reports after today's close or before the next open.
