# PROFESSIONAL_STRATEGY_V2 (PAPER)

V2 is a set of decision stages added on top of the **unchanged** original strategy (`strategy.txt` 0.1.6,
hash `c597cfe8…`). The opening-range breakout signal, its indicators (EMA 9/21, ATR 14, relative volume, opening
range, candle interval), contract selection rules, stop/target logic and loss limits are the original's. V2 never
edits them. It decides *whether* and *when* a qualified original signal may trade, *how much* the whole book may
risk, and *how* an open trade is protected.

* Config: `strategies/professional_v2.json` (+ `v2_sector_map.json`, `v2_economic_calendar.json`). Unknown or
  missing keys, a different base-strategy hash, or `mode` other than PAPER all refuse to load.
* Activation: `python -m trader.cli v2-activate --approve --note "…"` writes `strategies/professional_v2.manifest.json`
  (hash of config + sector map + calendar). An engine started with `--strategy v2` refuses to run if the manifest
  does not match.
* Code: `trader/v2/` (one module per stage). Hooks in `trader/paper_runner.py` run only when `--strategy v2` is given;
  without it the engine is the original, byte-for-byte in its decisions.

## Decision flow (every completed 1-minute bar)

```
DXLink trades ─► 1-minute bars (trading universe + SPY/QQQ/IWM + 11 sector ETFs, one connection)
                     │
                     ├─► market context (context.py): VWAP, EMA, vs open, gap, prior-day H/L, premarket H/L,
                     │      breadth, VIX (REST, shared 55 s cache)        ─► regime (regime.py, rules R1–R6)
                     │
                     └─► core signal (strategy_engine, unchanged) ─► breakout tracker (confirmation.py)
                                                                          │
     candidate report (pipeline.py), stages in order, each logged with inputs and reason:
       1 technical   core signal qualified at the breakout bar; trend/ATR/spike still hold on this bar
       2 confirmation CONFIRMED or PULLBACK_CONFIRMED (else WAIT / REJECT)
       3 regime      direction allowed in the regime (RANGE_BOUND, HIGH_VOLATILITY, UNCLEAR allow nothing)
       4 sector      the stock's sector ETF does not contradict the trade
       5 volume/VWAP breakout bar relative volume ≥ 1.2 (strategy value); price on the right side of session VWAP
       6 events      earnings (reject AMC today / BTO next session), FOMC window 13:30–15:00 ET (WAIT)
       7 portfolio   aggregate and correlated-cluster budget left
       ── only now is the option chain requested ──
       8 option      ORIGINAL contract selection within V2's budget
       9 volatility  IV rank ≤ 0.85, IV/HV30 ≤ 2.5, contract IV ≤ 1.3× IV index, quote ≤ 30 s old
      10 liquidity   original OI/volume/spread rules + displayed size ≥ 1
      11 sizing      original risk engine + original expected-net-R gate + full portfolio check + sizing
      12 final       APPROVED / WAIT_FOR_CONFIRMATION / REJECTED / INSUFFICIENT_DATA
                                                                          │
     APPROVED ─► same-minute ranking (expected net R, trend efficiency) ─► synchronous re-checks ─► PAPER fill
```

Open positions, every bar: flatten at the close and the daily-loss emergency exit (original) first; then V2
management (trailing stop, breakeven, time stop, partials); then the original underlying stop/target.

## Owner-authorized values (2026-10-23)

| Item | Value |
|---|---|
| Correlated cluster max loss | one per-trade budget ($10K ledger: 10% of equity; $1K ledger: min(remaining 40%, $150)) |
| Aggregate open max loss | $10K ledger 30% of equity (new); $1K ledger 40% (existing profile) |
| Contracts per trade | 1 (risk-based quantity computed and logged only) |
| Partial profit-taking | off (needs ≥ 2 contracts) |

All other risk limits are the original strategy's (and, for the $1K ledger, the profile's owner-authorized 20%
daily / 40% weekly loss limits and $150 per-trade cap).

## Regime rules

Per index (SPY, QQQ, IWM) bias ∈ [−3, 3]: ±1 close vs session VWAP, ±1 EMA 9 vs 21, ±1 close vs session open.

| Rule | Regime | Condition |
|---|---|---|
| R1 | UNCLEAR | any index missing, stale (> 150 s) or without all three votes |
| R2 | HIGH_VOLATILITY | VIX ≥ 30, or VIX ≥ +15% vs prior close, or SPY 1-min ATR/price ≥ 0.25% |
| R3/R4 | STRONG_BULLISH / BEARISH | every index bias ≥ 2 (≤ −2), SPY efficiency ≥ 0.30, ≥ 60% of the universe above (below) VWAP |
| R5 | WEAK_BULLISH / BEARISH | summed bias ≥ 3 (≤ −3) and SPY bias ≥ 1 (≤ −1) |
| R6 | RANGE_BOUND | otherwise |

## Breakout confirmation states

NO_SETUP → AWAITING_CONFIRMATION (core signal: a completed close beyond the level) → CONFIRMED (2 consecutive closes
beyond, latest ≥ 0.25 ATR beyond, within 5 bars) or PULLBACK_HOLDING (low/high back within 0.25 ATR of the level,
range not lost) → PULLBACK_CONFIRMED (close beyond the retest bar). REJECTED_BREAKOUT (first bar back inside the
range), FAILED_BREAKOUT (later close back inside), EXPIRED (no confirmation in 15 bars). Completed bars only.

## Correlation

Pearson correlation of daily close-to-close log returns, last 60 completed sessions (tastytrade DXLink `{=1d}`
candles, cached once per day for both engines). Two positions are one bet when corr × direction × direction ≥ 0.60.
Fewer than 40 overlapping returns → conservative grouping (same sector ETF, broad indexes together, leveraged or
inverse ETF with its index, China ADRs together).

## Data and API use (tastytrade only)

| Data | Source | Frequency | Shared by both engines |
|---|---|---|---|
| 1-minute bars, trading + context symbols | DXLink Trade events, one connection per engine | streaming | — (one connection each) |
| Session history at startup / gap backfill | DXLink Candle HISTORY | startup, gaps | — |
| Daily candles (prior-day levels, correlation) | DXLink `{=1d}` HISTORY, batched 40 per connection | once per day | yes (`data/v2_shared_cache`) |
| Premarket range SPY/QQQ/IWM | DXLink `{=1m,tho=false}` | once after 09:30 | yes |
| VIX | REST `/market-data/by-type?index=VIX` | ≤ 1 per 55 s | yes |
| IV rank, HV30, earnings | REST `/market-metrics` (≤ 50 symbols per call) | ≤ 1 per 15 min | yes |
| Option-chain listing | REST `/option-chains/{symbol}` | once per symbol per day | yes |
| Option quotes + Greeks | REST `/market-data/by-type` (≤ 100 contracts) | only when stages 1–7 pass | memoized per symbol-bar (V2 + benchmark) |
| Underlying quote freshness | REST `/market-data/by-type` | per symbol-bar (unchanged) | — |

Every REST request is logged to `<ledger>/v2_api_requests.jsonl` (account numbers masked). V2 engines are paced at
10 requests/second with GET-only 429 backoff. Estimated load per engine-minute: the original engine's one quote
snapshot per symbol-bar (14 or 47), plus ≈ 1 VIX request, ≈ 0.07 metrics requests, and one option-quote request per
candidate that passes stages 1–7 (typically 0–3 a minute). The chain listing that the original engine re-downloads
on every signal (≈ 12,800 SPY contracts) is fetched once a day.

## Original-strategy benchmark

Each V2 engine also runs the original strategy as a non-executing shadow on the same bars and quotes
(`<ledger>/v2_shadow_original/`): the original decision cycle, risk engine, ranking, exits and fill model on its own
simulated ledger. Signals V2 did not take get a hypothetical underlying outcome at the close
(`v2_signal_outcomes.jsonl`). `python -m trader.cli v2-compare` reports both side by side.

## Known limitations

* Not proven profitable or robustly better. In replays (underlying proxy) V2 beat the original from mid-August on but
  lost to it in June to mid-August; no variant was positive (docs/v2/FINAL_REPORT_20261023.md, section 8-9).
* IV rank/HV/earnings and option-level pricing cannot be replayed historically (no history); those stages are
  validated by unit tests and the live readiness check only.
* Economic calendar: FOMC, CPI, PPI, Employment Situation, GDP and PCE from the agencies' pages, valid through
  2026-12-23. Other releases (ISM, JOLTS, consumer confidence, Treasury auctions) are not modeled. FOMC statement time
  is customary (14:00 ET), not stated on the Fed page.
* Live 1-minute bars carry no VWAP, so the original strategy's own VWAP check has never run live (it passes when VWAP
  is missing). V2's stage 5 supplies a real session VWAP; the original check is left unchanged.
* The original strategy's 3-consecutive-loss block carries across sessions (`_consecutive_losses` reads the whole
  ledger); it is unchanged in V2.
* One DXLink connection per engine: the two engines each stream their own symbols (the $10K ledger's 14 are a subset
  of the $1K ledger's 47). Slow-changing data is shared through the disk cache.
* LIVE: V2 is refused by the shared loop with LIVE execution. A separate production-readiness audit is required.
