# $1K breakout ledger: diagnosis, tests and recommendation

Date: 2026-11-09, written before the open. PAPER only: live trading is untouched and still disabled. No strategy, setting,
`strategy.txt`, fingerprinted file or ledger was changed by this work. The $1K keeps running V2 2.5.0, as the owner decided.
The previous run ended at $800.49. On 2026-11-09, before the open, the owner reset it to a fresh
$1,000; the old run is archived in `data/profiles/small_account_paper_run_20261109_open`.

Companion files:
- `reports/1k_phase1_audit.md`: the Phase 1 audit of the recorded trades.
- `reports/1k_phase23/scripts/`: the test harness (pricing, proxy, account simulator, drivers).
- `reports/1k_phase23/results/`: raw results, logs and the replay signals used.

Labels on every number:
- **real option quotes**: tastytrade 1-minute bid/ask candles, recorded for Oct 26–Nov 6 (10 sessions, 14 symbols,
  roughly 9,000 contracts a day).
- **proxy**: Black-Scholes on real 1-minute stock bars, with implied volatility = 20-day historical volatility × a
  per-symbol ratio, and bid/ask spreads by delta range, both fitted to the real quotes. IV is held constant within a day
  and there is no skew. A proxy result is a rough guide, never proof.
- **stock R**: the replay's stock-price result in units of the stop distance. It contains no option prices at all.

---

## Answer

**Not ready, and no new version.** No structure, contract choice or exit tested here passes the gates.

The cause is not the option structure. The 2.5.0 breakout signal itself loses on stock moves alone in the validation
period (profit factor 0.87, stock R), and everything layered on top of it, options included, can only subtract from that.
Every option structure tested lost money in validation, under both fill models, with real quotes and with the proxy.
"Do nothing" ($0) beat all of them.

---

## 1. Why the $1K lost (Phase 1, recap)

- All $1K runs: gross −$90.50 at mid prices, costs $392.90, net **−$483.40** (real option quotes).
- The profit target was reached in 2 of 40 trades. In 56% of the lost dollars, the stock never moved 0.5R in the trade's
  favor.
- Fills were honest and no rule was broken.
- The $1K budget pushed it into cheap, slow names. A 0.55-delta option on SPY, QQQ, IWM or NVDA costs $680–$1,580, so
  none fits a 10%-of-equity limit (about $80).

## 2. Is the signal any good? (stock R, 2.5.0 replay, all 11 symbols)

| Period | Role | Signals | Win rate | Total R | Profit factor |
|---|---|---|---|---|---|
| Jun 1 – Aug 31 | tuning | 293 | 39% | +40.3 | 1.30 |
| **Sep 1 – Oct 9** | **validation** | **131** | **34%** | **−9.5** | **0.87** |
| Oct 14 – Nov 6 | later check | 84 | 39% | +14.3 | 1.32 |

The edge is not stable from one period to the next.

| Split | Jun–Aug | Sep–Oct 9 | Oct 14–Nov 6 |
|---|---|---|---|
| Calls (R) | +27.2 | +7.1 | −9.6 |
| Puts (R) | +13.2 | −16.5 | +23.9 |
| Weak-bullish regime (R) | +26.5 | +7.3 | −6.8 |
| Weak-bearish regime (R) | +14.7 | −16.1 | +24.4 |

What works flips between periods, so neither a regime filter nor a calls-only or puts-only rule is supported.

**One consistent pattern:** SPY was positive in all three periods (+29.0R, +6.1R and +8.4R; 75 signals in all). That is
one symbol picked after the fact, and a 0.55-delta SPY option costs about $700, so the $1K cannot use it anyway. This
is evidence only. Nothing was built, and changing the 11-symbol list is the owner's decision.

## 3. Exit variants (alternative 3; stock R)

| Variant | Jun–Aug PF | Sep–Oct 9 PF (validation) | Oct 14–Nov 6 PF |
|---|---|---|---|
| 2.5.0 baseline: let winners run (lock 1R, trail 1.5R) | 1.30 | 0.87 | 1.32 |
| + no-progress time stop, 45 min | 1.28 | 0.89 | 1.31 |
| + no-progress time stop, 60 min | 1.17 | 0.87 | 1.32 |
| + no-progress time stop, 90 min | 1.28 | 0.90 | 1.30 |
| Fixed 2R target instead of letting winners run | 1.23 | 0.93 | 1.00 |

Time stops change almost nothing. The fixed target removes the edge in the later period. No exit variant reaches 1.25 in
validation.

## 4. Option structures (alternatives 1 and 2)

Each replay signal was priced at its real entry and exit minutes: real option quotes on Oct 26–Nov 6, proxy elsewhere.
Costs are fees ($1.12 to open, $0.13 to close, per contract) plus $0.02 slippage per contract per side, and every fill
crosses the spread.

There are two fill models:
- **open**: the quote at the start of the minute.
- **adverse**: the worst bid and ask inside that minute.

Checked against the recorded $1K fills, the two bracket reality: open was $2.17 per trade too kind, adverse $4.66 per
trade too harsh.

### 4a. Every signal, no account limits: is there any edge after option costs?

| Structure | Validation, open fills | Validation, adverse fills | Real quotes only (Oct 26–Nov 6), open / adverse |
|---|---|---|---|
| Single 0.55Δ option, any size (the $10K's trade) | 98 trades, PF 0.85, −$418 | PF 0.69, −$1,019 | 39 trades: PF 0.75 / 0.50 |
| Single 0.20–0.45Δ option within $100 (**alternative 2**) | 50, PF 0.34, −$412 | 49, PF 0.34, −$406 | 9 / 8 trades: PF 0.21 / 0.18 |
| Debit spread within $100 (**alternative 1**) | 84, PF 0.00, −$1,494 | 80, PF 0.00, −$2,004 | 25 / 21 trades: 0 winners |
| Debit spread, round-trip cost ≤ 20% of risk | 43, PF 0.00, −$669 | 33, PF 0.00, −$532 | 10 / 0 trades: 0 winners |

For reference, the same full-size single options in the tuning period (proxy) made +$1,014 with PF 1.48 under adverse
fills. The edge existed in the summer and disappeared in validation.

**Why debit spreads cannot work here:** $100 of risk buys a spread only $1 wide on SPY, or $1.50 on IWM. Held about an
hour, such a spread moves only $1–$20, even when the stock goes +2R to +3R. Its round trip (four fills plus fees) costs
$13–$21. The structure has no room to win. A wider spread doesn't fit the budget, and longer holds are a different
strategy.

**Why cheap out-of-the-money options fail:** they decay and cost more in spread relative to their price, and the stock
needs a bigger move than the signal delivers in validation.

### 4b. The $1K account under the Phase 4 limits

Limits: 10% of equity at risk per trade, 1 open position, 3 entries a day, stop for the day after 2 losses in a row,
10% daily and 20% weekly loss limits.

The account starts at $1,000 on Jun 1. The current setup ($350 cap and 4% friction gate) is shown for comparison.

| Structure | Validation trades (open / adverse) | Validation P&L (open / adverse) | PF | End equity (open / adverse) | Max drawdown |
|---|---|---|---|---|---|
| Current $1K setup ($350 cap) | 12 / 10 | −$199 / −$173 | 0.18 / 0.20 | $874 / $901 | 20% / 17% |
| Single option within 10% | 0 (nothing fits) | $0 | – | $1,000 | 0% |
| 0.20–0.45Δ within 10% | 25 / 25 | −$253 / −$256 | 0.17 / 0.17 | $788 / $785 | 25% |
| Debit spread within 10% | 1 / 1 | −$17 | 0 | **$321** | **68%** |
| Debit spread with cost cap | 11 / 4 | −$148 / −$62 | 0 | $659 / $745 | 34% / 26% |
| **Do nothing** | 0 | **$0** | – | **$1,000** | 0% |

The debit-spread account lost most of its money in Jun–Aug: 37 trades and 0 winners. After that, equity was too small
for any spread to fit, which is why validation shows only 1 trade.

### 4c. Gates (validation period)

| Gate | Current | 0.20–0.45Δ | Debit spread | Debit + cost cap |
|---|---|---|---|---|
| 1. ≥ 40 out-of-sample trades | no (12) | no (25) | no (1) | no (11) |
| 2. Profit factor > 1.25 | no | no | no | no |
| 3. Positive in more than one market type | no | no | no | no |
| 4. Drawdown within limit (20%) | yes | no | yes* | yes* |
| 5. Positive without the best 2 trades | no | no | no | no |
| 6. Robust to ±20% settings | no: every variant loses | no | no | no |
| Real-quote expectancy ≥ 0 | no | no | no | no |

\* Within the limit only because the account had already lost most of its money before the validation period began.

**Robustness (gate 6):** budget 8% and 12%, target delta 0.50 and 0.60, cost cap 16% and 24%. Every variation lost
money, and none had a winning trade among the debit spreads. Nothing is close to a passing edge.

## 5. Credit spreads (evaluated only, not changed)

| Ledger | Closed | Realized | Open now |
|---|---|---|---|
| CREDIT_SPREADS_10K_PAPER | 4: all take-profit, +$39 to +$58 each | **+$191.80** | 2 call spreads, $5 wide (QQQ 710/715, SPY), Dec 11 |
| CREDIT_SPREADS_1K_PAPER | 2: both take-profit | **+$26.40** | 2 call spreads, $2 wide (SPY 784/786, QQQ), Dec 11 |

It's too early to judge. The test's own bar, set in advance, is 20 closed spreads or 6 weeks, whichever comes later;
there are 4 closed on the $10K so far. All the closes came in one falling stretch, when call spreads were on the right
side.

They can't be back-tested yet: they hold for weeks, and the proxy has no skew. The nightly recorder now saves their
strikes (21–49 days out, up to 12% out of the money, SPY and QQQ), so a real-quote test becomes possible as data
accumulates.

No change is recommended.

## 6. What the data does and doesn't support

| Idea | Verdict |
|---|---|
| Debit spreads for the $1K | **Rejected.** Structurally unable to beat the costs at this budget and hold time. |
| Cheaper (lower-delta) options | **Rejected.** Worse than the current setup. |
| Time stops | No effect. |
| Fixed target | Worse. |
| Regime or direction filter | Not supported: the sign flips between periods. |
| SPY-focused signal | Consistent in all 3 periods, but unaffordable for the $1K as a single option, and a symbol-list change needs the owner. Evidence only. |
| Declined rules (gap warning, 1 PM cutoff, $150 limit) | Not tested further; nothing here argues for reviving them. |

## 7. Recommendation and proposal (owner's decision)

1. **No new $1K version.** Nothing passed, so shipping one would break the brief's own rule.
2. **Proposal: pause new breakout trades on the $1K** until a signal passes the gates. The switch is to disable the
   `\OptioBot\SmallAccountEngine` scheduled task and enable it again to resume; the ledger is never reset. The owner
   declined pausing for now, so this stays a proposal and nothing was switched.
   Expected cost of keeping it on, based on the validation period: about −$15 to −$20 per trade, or roughly $150–$250
   over 4 weeks.
3. **Core issue (a proposal, not built):** after the summer, the breakout entry has no stable edge across the 11
   symbols. That is the $1K's problem and the $10K's alike, since both trade the same list. Any fix belongs to the
   signal: stricter breakout confirmation, or an SPY/index focus. It needs its own pre-registered test on real quotes,
   which should wait for about 4 more weeks of recorded quotes so the result doesn't rest on the proxy.
4. **Keep the credit-spread test running** unchanged until its pre-set bar.

## 8. Rollback

Nothing in the trading path changed, so there is nothing to roll back for trading. The only additions:
- `trader/option_recorder.py`, the `record-options` CLI command and the `\OptioBot\OptionRecorder` task (weekdays 16:45
  ET). To remove: `powershell -ExecutionPolicy Bypass -File scripts\install_option_recorder.ps1 -Remove`.
- A logging-only `forecast` block in `paper_close` records (`trader/paper_runner.py`, `_forecast_fields`), from Phase 1.
  It has no effect on decisions.

## 9. Four-week forward test (Nov 9 – Dec 4)

The $1K runs 2.5.0 unchanged, as the owner decided. This is a measurement period, not a rescue attempt.

| Item | Rule |
|---|---|
| Data | The nightly recorder saves real quotes every session, which adds about 20 days to the real-quote set. |
| Weekly check (Fri after close) | $1K and $10K P&L, gross at mid vs costs, target-hit rate, and how far the stock moved in the trade's favor (median favorable excursion), reported against Phase 1. |
| Stop early if | The fresh $1K loses 15% (equity below $850), or 2 weeks in a row at −10% or worse. The owner decides; nothing switches automatically. |
| End of test | Re-run this harness on the then roughly 30 days of real quotes, for the 2.5.0 signal and any pre-registered signal proposal. The same gates apply. |
| Success | Nothing new ships unless a candidate passes every gate on real quotes. |

## 10. Limits of this work

- Real option quotes cover only 10 sessions (Oct 26–Nov 6). Everything before that is proxy (constant IV, no skew).
- The signals come from the replay, which exits on stock-price levels. Live exits check option prices, and Phase 1 found
  that difference small.
- No signals for Oct 12–13: the gap between the two bar sources.
- Open interest and volume filters can't be applied historically.
