# PROFESSIONAL_STRATEGY_V2 — build and validation report (2026-10-23)

PAPER only. No real orders were placed or possible. The kill switch and Watchdog were left as found. The original
strategy files (`strategy.txt`, `active_strategy.json`) are byte-identical to the 2026-10-16 baseline.

## 1. Architecture

```
               one DXLink connection per engine (trading universe + SPY/QQQ/IWM + 11 sector ETFs)
                                         │ completed 1-minute bars
             ┌───────────────────────────┼──────────────────────────────────┐
   market context (context.py)     core strategy (UNCHANGED)          shadow benchmark (shadow.py)
   regime (regime.py)              strategy_engine.evaluate_…           original decision_cycle on its own
   sectors (sectors.py)                      │                          simulated ledger, same bars/quotes
             │                     breakout tracker (confirmation.py)
             └───────────► candidate report, 12 stages (pipeline.py) ◄── events.py, correlation.py, portfolio.py,
                                         │                               options.py, sizing.py, datahub.py
                          APPROVED → same-minute ranking → re-checks → PAPER fill (paper_runner)
                          open positions → management.py (earlier exits only) → original exits
```

Decision interface vs execution: the pipeline produces a deterministic report with an `entry` (contract, quantity,
underlying stop/target, modeled max loss). The PAPER adapter is the existing `PaperExecutor` path in
`paper_runner._run`; the LIVE adapter would be the existing `LiveTrader.open_position/close_position`, which take the
same position template. The shared loop refuses V2 with LIVE (or test mode) outright, so the LIVE path is neither
enabled nor exercised.

## 2. New modules and files

| File | Purpose |
|---|---|
| `strategies/professional_v2.json` | V2 configuration (every stage enabled/configured here; fail-closed schema) |
| `strategies/v2_sector_map.json` | maintained GICS symbol → SPDR sector ETF map (review due 2027-01-23) |
| `strategies/v2_economic_calendar.json` | FOMC, CPI, PPI, Employment, GDP, PCE from the agencies' pages (valid to 2026-12-23) |
| `strategies/professional_v2.manifest.json` | activation record (config + map + calendar hash) |
| `trader/v2/config.py` | load/validate config, pin the base strategy hash, write/verify the manifest |
| `trader/v2/indicators.py` | session VWAP, bias votes, efficiency (uses the core EMA/ATR functions unchanged) |
| `trader/v2/context.py` | market-context engine: freshness, prior-day/premarket levels, gap, breadth, VIX, provenance |
| `trader/v2/regime.py` | regime classifier, rules R1–R6 |
| `trader/v2/sectors.py` | sector mapping and confirmation |
| `trader/v2/confirmation.py` | breakout confirmation / pullback-retest state machine |
| `trader/v2/correlation.py` | daily-return correlation, sign-aware same-bet test, fallback grouping, clusters |
| `trader/v2/portfolio.py` | aggregate, cluster and buying-power checks; directional exposure report |
| `trader/v2/options.py` | cached chain listing + identical near-money selection; pricing/IV/liquidity checks |
| `trader/v2/events.py` | earnings and economic-event engine (reject / delay / review, verification status) |
| `trader/v2/sizing.py` | risk-based sizing (1-contract cap this week; risk quantity logged) |
| `trader/v2/management.py` | breakeven, trailing, time stop, partials (state in the position record) |
| `trader/v2/pipeline.py` | 12-stage candidate report; original risk engine and economic gate reused |
| `trader/v2/shadow.py` | original-strategy benchmark ledger (no orders) |
| `trader/v2/datahub.py` | shared disk cache, batched DXLink history, VIX, metrics, chains, request log |
| `trader/v2/runtime.py` | V2Session: the engine-facing object |
| `trader/v2/report.py` | Professional Strategy dashboard (`data/v2_dashboard.html`) |
| `trader/v2/compare.py` | original shadow vs V2 from the ledgers (`v2-compare`) |
| `trader/v2/replay.py` | historical replay harness (`v2-replay`) |
| `trader/v2/commands.py` | `v2-validate`, `v2-activate`, `v2-readiness`, `v2-report`, `v2-replay`, `v2-compare` |
| `tests/test_v2.py`, `tests/test_v2_engine.py` | 67 mocked tests incl. an end-to-end engine run |
| `docs/v2/*` | plain-English description, Monday checklist, this report, baseline, diffs |

## 3. Existing files modified (diffs in `docs/v2/`)

| File | Change | Effect on the original strategy |
|---|---|---|
| `trader/paper_runner.py` (frozen list; owner-authorized V2 mission) | V2 hooks: context symbols on the feed, V2 entry/management/shadow calls, partial-exit booking, context-symbol tolerant history, startup history retry | none: every hook runs only with `--strategy v2`; the retry and the tolerant history are V2-only |
| `trader/account_profiles.py` | pass `v2` through `run_profile_paper_session` | none |
| `trader/cli.py` | `paper-session --strategy {original,v2}`; V2 commands; `profile-report` also writes the V2 dashboard | none (default `original`) |
| `trader/tastytrade_client.py` | optional request observer for API logging | none (off unless V2 sets it; logging can never fail a request) |
| `trader/supervisor.py` | the hourly review snapshot includes V2 evidence and the shadow benchmark when a ledger runs V2 | none for original ledgers |

Reverted during the build: a 5-line change to `trader/market_data.py` (frozen) — see §11, item 1.

## 4. Original strategy integrity

Verified at the end of the build (`docs/v2/baseline_20261023/` holds the start-of-day record): `strategy.txt`
`ee75a068…`, `active_strategy.json` `aedc1a97…` (manifest verified, hash `c597cfe8…`, 0.1.6), and every frozen file
except `paper_runner.py` (V2 hooks, recorded) and `paper_execution.py` (the owner's 2026-10-20 fee change, already
committed) match the 2026-10-16 baseline. The strategy-integrity test passes.

## 5. Decision flow

See `docs/v2/professional_strategy_v2.md` (stages 1–12, regime rules, confirmation states, correlation rule).

## 6–7. Tastytrade data and API load

| Data | Endpoint | Frequency |
|---|---|---|
| 1-minute bars | DXLink Trade stream (one connection) | streaming: 14 + 13 context symbols ($10K), 47 + 11 ($1K) |
| History at startup/gaps | DXLink Candle HISTORY | per symbol at startup; gaps |
| Daily candles (60 sessions) | DXLink `{=1d}`, 40 series per connection | once per day, shared by both engines |
| Premarket range | DXLink `{=1m,tho=false}` | once per day after 09:30, shared |
| VIX | REST index quote | ≤ 1/55 s, shared |
| IV rank, HV30, earnings | REST `/market-metrics` | ≤ 1/15 min, shared |
| Chain listing | REST `/option-chains/{symbol}` | once per symbol per day, shared (was every signal) |
| Option quotes + Greeks | REST `/market-data/by-type` | only when stages 1–7 pass; memoized per symbol-bar |

Measured in the live sandbox ($10K configuration): ~17 REST requests per minute, all HTTP 200, slowest 0.52 s.
Both engines together: about 1–2 requests per second, paced at ≤ 10/s with GET-only 429 backoff.

## 8–9. Historical replay (underlying proxy; same harness for both strategies)

Harness (`trader/v2/replay.py`): the production signal, confirmation, context, regime, sector, VWAP, FOMC, correlation
and aggregate-cap code on completed bars; exits on later completed closes; 100-share underlying proxy with $0.01/share
slippage each way and $1.40 fees; 4 open / 6 per session. Option-level stages, earnings and IV cannot be replayed.

Expectancy per trade in R (trade count in brackets):

| Variant | Jun 1 – Aug 15 (in-sample, Massive cache) | Aug 16 – Oct 9 (out-of-sample, Massive cache) | Sep 28 – Oct 22 (tastytrade, full context incl. intraday VIX) |
|---|---|---|---|
| Original strategy | **−0.003** (311) | −0.114 (227) | −0.386 (112) |
| Original + V2 trade management | −0.058 (316) | −0.144 (228) | −0.342 (112) |
| V2 filters, original exits | −0.180 (278) | −0.124 (211) | −0.083 (106) |
| **V2 as built (deployed)** | −0.158 (304) | **−0.088** (228) | **−0.017** (111) |
| V2 without the confirmation wait, original exits | −0.132 (282) | −0.121 (210) | −0.155 (102) |
| V2 without the confirmation wait, with management | −0.096 (305) | −0.066 (222) | −0.032 (112) |

92-session totals (Jun 1 – Oct 9): original −26.8R (PF 0.93, max DD 45.1R); V2 −68.1R (PF 0.79, max DD 75.1R).
19-session totals (Sep 28 – Oct 22): original −43.2R (PF 0.55, max DD 45.2R); V2 −1.9R (PF 0.97, max DD 23.9R).

What the evidence says:
* **No variant has a positive expectancy in any period.** The original strategy's edge is at best zero before options.
* **V2 is not robustly better.** It beats the original in the two later periods (by 0.03R and 0.37R per trade) and
  loses to it in the summer period (by 0.16R). Do not read the recent 19 sessions as proof.
* Trade management raises the win rate (40% → 48%) but cuts the average win (1.68R → ~1.0R); it helped only in the most
  recent period. The confirmation wait helped in the recent period and hurt in the summer.
* By regime (92 sessions, original entries): STRONG regimes +0.12R/+0.31R (30 trades), RANGE_BOUND −0.20R (97 trades),
  HIGH_VOLATILITY −1.10R (5 trades). The regime classifier separates conditions; the samples in STRONG are small.
* V2 still trades near the 6-per-session limit (~5.8/session); it changes which trades are taken more than how many.
* Option-level effects (spread, theta, IV) are not in these numbers; the live shadow benchmark measures them from Monday.

## 10. Tests

* Mocked unit/integration: 219 pass (152 original + 67 V2, incl. an end-to-end engine session with breakout →
  confirmation → approval → fill → management → shadow). Mocked tests do not prove the live connection.
* Live tastytrade integration (`v2-readiness`, 2026-10-23 13:19 CT, market open): READY — authentication, clock
  0.45 s, context quotes, VIX, market metrics for 55/55 symbols with IV rank and earnings, daily history 56/56,
  correlation measured for 91/91 pairs, intraday 1-minute history, premarket range, DXLink live stream for all 14
  context symbols, option quotes with Greeks.
* Live sandbox: the full V2 engine ran on live data (sandbox ledger, no orders) from 13:29 CT to the close.
* Live option stages: stages 8–11 run against live chains/metrics for SPY, AAPL, NVDA (approved), F (rejected: expected
  net R −0.10) and SOFI (rejected: contract IV 1.38× its IV index ahead of 10/27 earnings).

## 11. Known limitations and items for the owner

1. **Needs your authorization (not applied):** `trader/market_data.py parse_candle_feed` refuses a whole history
   snapshot when the still-forming minute's candle updates during delivery ("DXLink candle snapshot is out of order").
   Today it refused XLE (this morning's restart), NVDA, QQQ, IWM, AMZN, AAPL and XLY at various starts. The original
   engine disables such a symbol (or fails to start if it holds a position). V2 retries up to 3 times, which recovered
   every case today. Proposed fix (5 lines): ignore candles after the requested window end before the order check.
2. Not proven profitable, and not robustly better than the original: see section 8-9 (worse in Jun-Aug, better Aug-Oct).
3. Option-level checks (IV rank, IV/HV, contract IV, option spreads, expected net R) cannot be replayed: no history.
4. Earnings history is not available, so earnings rules are not replayed.
5. Economic calendar lacks 10:00 ET releases (ISM, JOLTS, confidence) and Treasury auctions; FOMC time is customary.
6. The original strategy's own VWAP check never runs live (live bars carry no VWAP); V2's stage 5 supplies a real VWAP.
7. The original 3-consecutive-loss block carries across sessions (unchanged).
8. V2 still takes close to the 6-a-day maximum in replays; it is not yet "a few setups a day".
9. In V2 mode the ledger's `decisions.jsonl` is replaced by `v2_decisions.jsonl` (+ the shadow's decisions), so the
   existing profile-status "signals today" count reads 0; the supervisor and dashboard read the V2 files.

## 12. Startup commands

```
.venv\Scripts\python.exe -u -m trader.cli paper-session --strategy v2 --account-profile small   # $1K, 08:15 CT
.venv\Scripts\python.exe -u -m trader.cli paper-session --strategy v2                           # $10K, 08:20 CT
```

## 13. Monday checklist

`docs/v2/monday_deployment.md`.

## 14. LIVE

Disabled: `TRADING_MODE=PAPER`, `ENABLE_LIVE_TRADING=false`, the V2 config refuses any mode but PAPER, and
`paper_runner._run` refuses V2 with LIVE execution. A separate production-readiness audit is required before LIVE.
