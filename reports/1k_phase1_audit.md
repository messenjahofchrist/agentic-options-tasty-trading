# $1K breakout ledger: Phase 1 audit

Date: 2026-11-08 (market closed). Scope: `SMALL_ACCOUNT_PAPER`, every run since 2026-10-16, compared with the $10K ledger.
PAPER only. LIVE is untouched and still disabled. No strategy, setting or ledger was changed for this audit. There is one logging
fix (section 1.7) and three doc corrections.

Appendices, generated from the ledgers:
- `reports/1k_phase1_tables.md`: every breakdown requested in 1b, for both ledgers, current run and all runs.
- `reports/1k_phase1_trades.csv`: all 97 trades, one row each, with costs, excursions, the forecast and the loss cause.

Labels used below:
- **real option quotes**: computed from recorded paper fills, plus tastytrade's historical 1-minute option bid and ask (DXLink candles with `price=bid` and `price=ask`, available for the contracts traded from Oct 26 on).
- **stock bars**: computed from tastytrade 1-minute regular-hours stock bars.

Nothing in this report is a backtest. Every number describes trades that actually happened on paper.

---

## The short version

1. **The $1K breakout bot had no edge before costs, and costs made it much worse.** Across all 40 trades:
   - Gross result, measured at the mid price (mid-to-mid): **−$90.50**.
   - Spread, slippage and fees: **$392.90** more.
   - Net: **−$483.40**.
   
   Costs were 7.3% of premium per trade at the median, against 1.8% on the $10K. *(real option quotes)*
2. **The $1K's trades almost never worked.** Its profit target (2R) was reached in **2 of 40** trades; the $10K reached it in 8 of 33. The furthest the stock moved in the trade's favor (the favorable excursion, measured in R, the stop distance) was:

   | | Median | 75th percentile |
   |---|---|---|
   | $1K | 0.61R | 1.13R |
   | $10K | 0.88R | 1.96R |

   *(stock bars)*
3. **Most losing dollars come from entries that never moved.** In 56% of the $1K's lost dollars, the stock never got 0.5R in the trade's favor. Another 26% reached 0.5–1R, then reversed. Exits that gave back a 1R+ move explain 12%. Costs explain 4% on that per-trade view. Costs dominate the net in dollars because they hit every trade, winners included (see 1b).
4. **The "≥ 1R expected net" entry gate is not a forecast of expected profit.** It computes the payoff *if the target is reached*, per unit of loss at the stop. It never weighs the chance of reaching the target. When the target was reached, the payoff estimate was accurate: the option's bid at the target averaged 2.2% above the forecast (8 cases). But the $1K reached its target 5% of the time, and a 1–1.8 reward-to-risk ratio needs about 35–50% to break even. *(real option quotes)*
5. **The simulator is honest.**
   - Paper fills match real quotes. Over 21 checked $1K fills, recorded prices were within $2 in total of the real bid/ask range for that minute.
   - Slippage is $0.02 per contract per side, as the setting's name and the sizing docs say. The fill already pays the full bid/ask spread, so the extra slippage is a small cushion, not a hidden subsidy.
   - No entry broke the rules of the version active at the time.
6. **The brief's main hypothesis is supported.** The $1K's budget pushed it into cheap, slow underlyings: median premium $93, half of trades under $100. The edge the $10K found sits in SPY, QQQ, IWM and NVDA. At Friday's close, a 0.55-delta 7–21 DTE option on those names cost **$680–$1,580**. Under the Phase 4 limit of 10% of equity (about $80 today), no single option on any of the 11 symbols fits, so Phase 2 has to test debit spreads or sitting out.

---

## 1a. Plumbing

### 1.1 Slippage: per contract, as intended (not a bug)

- **How it works:** `strategy.txt` sets `slippage_per_contract: 0.02`. `trader/paper_execution.py` adds `0.02 / 100` = $0.0002 per share to the ask on a buy, and subtracts it from the bid on a sale. That's $0.02 per contract per side.
- **This was the intent:** the setting's name says per contract. `docs/profiles/small_account_profile.md` gives the example "a $2.00 option counts as $201.44" (premium $200, plus 2 × $0.02, plus $1.40 fees). The sizing gate and the economic gate use the same per-contract value.
- **Is paper too optimistic?** No. The fills cross the whole quoted spread: buys pay the ask, sales get the bid. Against real 1-minute quotes:

  | Ledger | Fills checked | Inside the real bid/ask range that minute | Net difference vs. nearest real quote |
  |---|---|---|---|
  | $1K | 21 (15 buys, 6 sales) | 16; the other 5 were 1–2¢ off, in both directions | **$2 total** |
  | $10K | 60 (32 buys, 28 sales) | 49; the other 11 were 1–5¢ off, in both directions | **$13 total** |
- **One real gap: latency.** On the $10K, the ask one minute after entry averaged $2.23 higher than the recorded fill. On the $1K it was $0.20. A live order that took a minute would cost about that much more. Phase 2 will model it as an option.
- **If slippage were $0.02 per share instead:** it would cost $3.96 more per round trip, about $158 more over the $1K's 40 trades. No evidence supports that number, because the recorded fills already match real quotes.

### 1.2 Breakeven stops that lost money: one old bug (already fixed) and one built-in limit

| Trade | Version | P&L | Cause |
|---|---|---|---|
| XLE, Oct 27 | 2.0.0 | −$5.29 | **Bug, fixed in 2.1.0.** 2.0.0 had `require_option_above_floor: false`, so the stop armed when the stock reached 1R even though the option bid ($1.16) was still below the breakeven price ($1.2128). It fired on the same bar. 2.1.0 arms only once the bid has actually covered cost plus fees. |
| F, Oct 29 | 2.4.0 | −$2.29 | **Not a bug.** It armed correctly with the bid above $0.4428 at 12:39 ET. At 14:02 the bid dropped below the floor, and the next fill was the then-current bid, $0.42. On a $0.43 option, one cent is 2.3%, and the stop is checked once per completed 1-minute bar. |

The $10K had the same pattern on Nov 6: SPY −$19.30 and NVDA −$11.30.

A stop can only promise to *trigger* at breakeven, not to *fill* there. The only way to never sell below breakeven is a resting limit order, which may not fill at all, and then the trade can become a full loss. So "$0 or better after costs" cannot be guaranteed. Phase 3 will measure how much the breakeven stop costs compared with no breakeven stop.

### 1.3 Rule compliance: every current-run entry obeyed its version

I checked all 52 current-run entries (both ledgers) against the `entry_filters` and `late_entry_cutoff` of the saved version that was active at each entry (`strategies/v2_versions/`). **No violations.**

- **PFE bought at 15:58 ET, Oct 27, on 2.1.0:** allowed. 2.1.0 had `late_entry_cutoff.enabled: false`; the 30-minute cutoff arrived in 2.2.0.
- **Entries at 10:30–10:42 ET on 2.4.0:** allowed. The skip window is 10:00 *up to* 10:30, and these were decided at 10:30:05–10:42:06. The breakouts behind them may have formed inside the window and confirmed just after it. That's a design question, which Phase 3 can test as an entry variant, not a rule breach.

### 1.4 The Nov 3 XLU "market data failed" exit: a data gap, fixed on Nov 4

At 14:30 ET tastytrade's live feed published no 1-minute bar for XLU. When the engine re-requested that minute from tastytrade's history, it still got none; **that minute is still missing today.** Massive's data shows 6,076 XLU shares traded in it, so tastytrade's own bar data simply skips some minutes. The engine treated the gap as a data failure, disabled XLU, and sold the open call at the bid as a safety step: −$10.29.

The fix went in on 2026-11-04 (`MAX_FILLED_GAP_MINUTES = 3` in `trader/paper_runner.py`): after 3 retries, a run of up to 3 missing minutes is filled with flat zero-volume bars. Before the fix, the $1K disabled a symbol 95 times, 36 of them for a minute that came back empty. Since Nov 5, neither engine has disabled a symbol.

The Nov 6 $10K engine stop was different: I ran CPU-heavy replays during market hours. The replays aren't run during market hours any more.

### 1.5 Paper fills vs. real prices

See 1.1. Real bid/ask history was available for 52 of the 97 trades: every contract traded from Oct 26 on. Contracts from the Oct 14–23 runs fall outside tastytrade's history window. *(real option quotes)*

### 1.6 Forecast vs. result

| Ledger | Trades | Mean forecast "expected net R" | Mean realized R (same basis) | Target reached |
|---|---|---|---|---|
| $1K | 40 | 1.28 | **−0.59** | 2 |
| $10K, current run | 33 | 1.48 | +0.28 | 8 |

The realized R uses the same denominator as the forecast: the gate's modeled loss at the stop.

- **The forecast's price math is fine.** In the 8 trades that reached the target, the real option bid at that minute averaged **+2.2%** versus the forecast, ranging from −0.8% to +6.4%.
- **What's wrong is what it measures.** It's a reward-to-risk ratio, not an expectation, because it never estimates how often the target is reached. That's why trades that all "passed ≥ 1R" lost 11% of premium on average on the $1K. Phase 2 needs a probability-weighted forecast, built from the replay's measured hit rates, and must check it out-of-sample.

### 1.7 The fix made in this phase (with a test)

Every `paper_close` record now carries a `forecast` block beside the result:
- `expected_net_r`
- entry, stop and target levels
- regime and setup type
- entry delta
- the furthest the stock moved in the trade's favor, in R

That makes the forecast-vs-result check automatic from now on.

- **Changed:** `trader/paper_runner.py`, in all three close paths (normal, partial, end of day).
- **Test:** `tests/test_v2_engine.py::test_let_winners_run_holds_past_the_target_in_the_engine`.
- **Behavior:** unchanged; this is logging only.
- **Full suite:** 268 passed.

### 1.8 Doc corrections (no behavior change)

- `docs/strategy_plain_english.md` said the end-of-day close was off. `strategy.txt` has `flatten_at_session_close: true`, and all 38 end-of-day exits happened at 15:59–16:00 ET. The doc now says so.
- **2.6.0 (favorites first, volatile-day option) was built on Nov 6 but never switched on.** Only 2.5.0 has ever traded. Your brief makes 2.5.0 the baseline and allows one new version, so 2.6.0 stays parked: its file is saved in `strategies/v2_versions/`, and the active files are 2.5.0. The docs now say this. Its code paths are optional settings that 2.5.0 does not have, so they do nothing. Tests were adjusted so they don't depend on the active files.

---

## 1b. Performance breakdown

### By ledger and run

| Ledger, run | Trades | Win % | Net | Avg win | Avg loss | Profit factor | $ / trade | Max drawdown | Median hold | Gross (mid) | Costs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **$1K current, Oct 27–Nov 6** | 19 | 21% | **−$199.51** | $9.96 | −$15.96 | 0.17 | −$10.50 | $201.93 (20.1%) | 178 min | −$57.00 | $142.51 |
| $1K Oct 16 | 4 | 50% | −$25.76 | $8.06 | −$20.94 | 0.38 | −$6.44 | 3.9% | 148 | +$83.50 | $109.26 |
| $1K Oct 19–20 | 8 | 12% | −$155.52 | $6.56 | −$23.15 | 0.04 | −$19.44 | 15.6% | 174 | −$90.00 | $65.52 |
| $1K Oct 21 | 3 | 0% | −$83.87 | – | −$27.96 | 0.00 | −$27.96 | 8.4% | 29 | −$68.50 | $15.37 |
| $1K Oct 23 | 6 | 33% | −$18.74 | $18.21 | −$13.79 | 0.66 | −$3.12 | 4.4% | 256 | +$41.50 | $60.24 |
| **$1K all runs** | 40 | 22% | **−$483.40** | $10.99 | −$18.79 | ≈0.17 | −$12.09 | | 171 | **−$90.50** | **$392.90** |
| **$10K current, Oct 26–Nov 6** | 33 | 36% | **+$638.18** | $127.45 | −$42.44 | 1.72 | +$19.34 | 3.7% | 113 | +$955.00 | $316.82 |
| $10K, 3 earlier runs | 24 | 50% | +$424.18 | | | | | ≤2.3% | | +$664 | $239.82 |

- **Gross (mid):** the option's mid price at exit minus its mid price at entry.
- **Costs:** half the bid/ask spread at each end, plus slippage and fees. Net = gross − costs.

### Corrections to the brief's numbers

Everything else in "What we already know" checks out against the raw ledgers, to the cent:

- **$1K current-run average win and loss are $9.96 and −$15.96.** The $11 and $19 figures are the all-runs averages, $10.99 and −$18.79.
- **The biggest current-run win was $27.71.** The $34.71 biggest win was XLE in an earlier run.
- **The $1K had 9 wins in 40 trades across all runs.**
- **The 16 end-of-day exits lost $102.54.** They gained **+$136.50** mid-to-mid but paid $239.04 in costs. They didn't so much "barely move" as move slightly the right way and lose to costs.
- **The 0.55-delta premiums on SPY, QQQ and NVDA are higher than the brief said** (see the short version, point 6).

### Where the $1K's losses come from

Two complementary views, all runs.

**Dollars: costs vs. trading**

| | $ |
|---|---|
| Gross trading result (mid-to-mid) | −90.50 |
| Bid/ask spread paid (half at entry, half at exit) | −339.50 |
| Fees | −51.80 |
| Slippage ($0.02 per contract per side) | −1.60 |
| **Net** | **−483.40** |

Removing every cost would still leave −$90.50: there was no edge to protect. Costs explain about **81%** of the net loss.

**Losing trades by primary cause** (31 losers, $582.34; *stock bars plus real option quotes*)

| Cause | Trades | $ lost | Share |
|---|---|---|---|
| Wrong idea: the stock never moved 0.5R in the trade's favor | 15 | 325.25 | 56% |
| Weak follow-through: reached 0.5–1R, then reversed | 6 | 152.89 | 26% |
| Exit gave back a move of 1R or more | 3 | 69.17 | 12% |
| Costs: the mid price gained, the net lost | 6 | 24.74 | 4% |
| Forced exit (the XLU data gap) | 1 | 10.29 | 2% |
| Contract: the stock moved the trade's way and the option still lost | 0 | 0 | 0% |

In the brief's categories:

- **Entries: about 80% of losing dollars.** False or weak breakouts. Of the 40, 2 reached the target, and only one loser (XLE, Oct 27, sold by the 2.0.0 breakeven bug) would have reached it later that day.
- **Contract choice: indirect.** It caused no losses through theta or IV: no trade lost while the stock went its way. It shows up as costs (7.3% of premium vs. 1.8%) and as the slow names the budget allowed.
- **Exits: about 12%.** Too few trades to judge the let-winners-run rule on the $1K. It never got far enough to apply.
- **Market conditions:** current run only (V2 regime recorded at entry). WEAK_BULLISH: 10 trades, 1 win, −$166.90. WEAK_BEARISH: 7 trades, 3 wins, −$10.03. Calls won 11% of the time across all runs, puts 32%. These samples are too small to act on.
- **Execution costs:** the largest single dollar item, mostly the bid/ask spread ($339.50 of $392.90). A 3.6% median spread on a $93 option cost more than these breakouts moved it.

### Other cuts (full tables in the appendix)

- **Underlying:** XLE was the only real winner, +$31.69 over 4 trades. Everything else was flat or negative. NFLX −$69.87, INTC −$64.29 and XLF −$49.88 were the worst.
- **Premium:** under $100 = 21 trades, −$240.44, profit factor 0.08; $100–200 = 14 trades, −$117.21; $200–350 = 4 trades, −$79.31. Nothing worked, but the cheapest contracts were worst.
- **Spread at entry:** losses at every level (under 3%, 3–6%, 6% or more). Wider spreads cost more per trade.
- **Entry hour:** 10:00–11:00 ET was worst, 12 trades, −$141.93. Entries from 13:00 on: 6 trades, 0 wins, −$121.04.
   - This matters because you declined the 1 PM cutoff. **6 trades is far too few to say anything**, so this is not a case for reopening it; I'm reporting it because the brief asks for the evidence.
- **Exit reason:** stock-price stops: 18 trades, −$418.97 (average adverse excursion 1.15R, so they hit full stops). End of day: 16 trades, −$102.54. Targets: 2 trades, +$48.27.
- **Favorable and adverse excursion in the option:** a $1K option's best bid during the trade was 6.3% above its fill on average (current run), and its worst bid 16.2% below. On the $10K the figures were +13.0% and −9.7%.
- **$10K concentration:** its six best trades, +$1,411.19, were four puts in bearish regimes on Oct 30 and Nov 2, plus SPY and IWM calls. Two "let winners run" exits made +$695.39. The other 27 trades lost −$773.01. SPY, QQQ, IWM and NVDA made +$624.97 over 20 trades; all other names +$13.21 over 13. Trades with premium of $350 or less made +$5.81 over 11. **The $10K's profit is a few large index and NVDA moves on 3–4 days, and it can't be called proven yet either.**

---

## 1c. Data inventory

| Data | What we have or can get | How far back | Cost |
|---|---|---|---|
| 1-minute stock bars | Local cache `data/backtest_cache/massive-*` (52 symbols, including all 11 and the sector ETFs) | Jun 1–Oct 10, 2026 | in hand |
| | Massive API, current key | about 2 years (a 2024 request was refused as outside the plan) | current plan; about 5 requests per minute |
| | tastytrade DXLink | about 26 days | free with the account |
| **Historical option bid/ask** | tastytrade DXLink 1-minute candles, `price=bid` and `price=ask` (confirmed working today) | **about 10–15 trading days**, because a request holds at most about 8,000 candles | free |
| | Massive API, current key | **not included** (option quotes and snapshots: "not entitled") | upgrade needed |
| Historical option trade bars | Massive API, current key: 1-minute trade OHLC per contract | about 2 years | in hand, but rate-limited |
| Greeks | Live only (tastytrade quotes and DXLink) | none historical | – |
| Implied volatility history | tastytrade market metrics: current IV, 52-week IV rank, HV30 | none historical | – |

**We do not have real historical option bid/ask quotes for Jun 1–Oct 23.** Until we buy them, every options result for that period will be labeled **proxy simulation**: option prices modeled from stock bars and Greeks, or from option trade prints plus a modeled spread. Real-quote results are possible only for:
- the trades already taken from Oct 26 on (done in this audit), and
- whatever we record from now on.

Sources for real historical option quotes. Prices are from public pages or third-party listings; **I could not confirm any of them. Check before buying.**

| Source | What it offers | Approximate price |
|---|---|---|
| [ThetaData](https://thetadata.net/pricing) | 1-minute NBBO option quote history API ([docs](https://http-docs.thetadata.us/Articles/Getting-Started/Subscriptions.html)) | Options Value about $40/month, Standard about $80/month. Which tier includes quote history is unconfirmed. |
| [Databento OPRA](https://databento.com/pricing) | `cbbo-1m` (consolidated best bid/offer, 1-minute) from 2013 ([unit prices](https://databento.com/docs/api-historical/metadata/metadata-list-unit-prices)) | Pay per GB, or a [Standard OPRA plan](https://databento.com/blog/introducing-new-opra-pricing-plans) reported at $199/month. Jun–Nov for 11 near-the-money chains is likely a small one-time purchase, but get a quote. |
| [Massive options](https://massive.com/business-options) | Business tier with 2.5 years of quote history | $1,999/month; a $199 "Advanced" retail tier is reported by third parties but unconfirmed |

**Free alternative:** record our own. Each evening, save the day's 1-minute bid and ask candles for the near-the-money chains of the 11 symbols from DXLink. In 4 weeks that gives about 20 days of real quotes at no cost. The catch is that it only builds history forward.

---

## Proposed research plan (Phases 2–3)

### Acceptance gates, written before any results

Measured on the out-of-sample period, for the $1K profile, after realistic costs (ask buys, bid sales, fees, $0.02/contract slippage, plus a 1-minute latency variant):

1. **Enough trades:** at least 40 out-of-sample trades; below that the result is "not enough data", not a pass.
2. **Profit factor:** above 1.25.
3. **Market types:** positive net expectancy in at least two regime types.
4. **Drawdown:** max drawdown within the Phase 4 limits: 10% daily, 20% weekly, never more than 2 losses in a row before stopping for the day.
5. **No outlier dependence:** still positive with the best 2 trades removed, and not dependent on one date range. It must be positive in each half of the out-of-sample period.
6. **Robust settings:** still passes with each key parameter moved ±10–20%.
7. **(added) Real quotes:** any setup that passes on proxy prices must also show non-negative expectancy on whatever real-quote data exists (the Oct 26–Nov 6 contracts plus anything recorded) before I recommend it.

### Steps

1. **Data decision (yours):** buy one month of real option quote history (ThetaData or Databento, about $40–200), or run on proxy prices only and label everything "proxy". I recommend buying. Without real quotes, the debit-spread test, which is the most important one, is a guess about spreads. I'll also start the free nightly recorder either way, if you approve.
2. **Make the replay reproduce reality first:** the replay must reproduce the actual 2.5.0 $1K ledger before it tests anything. I'll replay Oct 27–Nov 6 with the real quotes for the contracts actually traded, and fix the replay until entries, exits and P&L match the 19 recorded trades closely. Then I'll extend it to Jun 1–Nov 5 with:
   - ask buys and bid sales,
   - the Phase 4 limits,
   - no look-ahead,
   - tuning on Jun–Aug and validation on Sep–Nov 5, with walk-forward windows if there are enough trades.
3. **Experiments: the baseline plus at most 3 alternatives, each tested on its own:**
   - **A. Debit spreads on SPY, QQQ, IWM and NVDA** (the core change, built behind an off switch, $1K only). Buy the 0.55-delta option, sell a further-out strike so the net debit is at most 10% of equity, and reuse the same entry signal. This tests the main hypothesis directly.
   - **B. Contract selection with a cost ceiling.** Same single options, but reject any contract whose round-trip cost exceeds a set share of its expected move, and test higher delta and shorter DTE.
   - **C. Exit package.** A hybrid stop (the stock decides when the idea is wrong; the option price caps the dollars at risk) plus a 60-minute no-progress time stop. I'll also report 45 and 90 minutes, as a sensitivity check only.
   - **Also evaluated, not changed:**
     - **Credit spreads:** replay the credit-spread rules from Jun to Nov, including the stretches when price was above the 50-day average, when the bot would sell put spreads.
     - **"Do nothing":** the comparison every alternative must beat.
   - **Later:** entry variants (5- vs. 15-minute range, confirmation timing, VWAP reclaim) only after A–C are understood, and only if a slot is left within the 3-alternative limit.
4. **One report, then one version at most:** `reports/1k_diagnosis.md` with the results table (tuning and validation side by side, each labeled real or proxy). If something passes, it ships as one new $1K-only version, off by default, with a changelog entry and a one-step rollback to 2.5.0. If nothing passes, the recommendation will be to pause the $1K breakout bot.

### What I need from you before Phase 2

1. **Do you approve buying real option quote history?** About $40–200, one-time or for one month. I'll get an exact quote first.
2. **Can I add the nightly option-quote recorder?** It's read-only, runs after the close, and doesn't touch the engines.
3. **Should the $1K keep trading 2.5.0 while I research?** The Phase 1 evidence says it has no edge in its current form. Its run so far: −$199.51 and a 20.1% drawdown in 8 trading days. Pausing it is a settings change, so it's your call. My recommendation is to pause it and keep the engine running read-only for data, but I won't do either without your OK.
