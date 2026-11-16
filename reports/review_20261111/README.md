# OptioBot review: strategy, code and tested recommendations

Date: 2026-11-11, written before the open. PAPER only. **Nothing in the bot was changed**: no strategy file, setting,
ledger or scheduled task. The only new files are this report and the test scripts in `scripts/` beside it.

## Short answer

1. **The bot is not passing over good trades.** The 328 signals it rejected live since Oct 26 would have lost money
   as a group (−33.4R, 40% winners). Loosening the filters would add losing trades.
2. **The entry signal's edge is about a third the size of what it costs to trade it with options.** In the replay the
   average trade makes +0.08R on the stock move. The $10K's measured round-trip option cost is 0.26R per trade
   (median). No selection rule fixes that gap; cost has to come down or the edge per trade has to go up.
3. **The $10K is ahead because of 4 trades and lower costs, not because it picks better.** Its top 4 trades made
   +$1,199; the other 41 lost $513. Its costs are 1.9% of premium; the old $1K run paid 6.2%.
4. **One change tested well on real option quotes: buy shorter-dated contracts** (1–6 days out, not 7–21). On the
   $10K's own entries and exits it made more money with less than half the capital and half the cost per trade.
5. Four other ideas I tested failed and should not be built (section 4).

R means the planned stop distance: +1R is a gain equal to the planned loss.

---

## 1. What the accounts actually did (Oct 26 – Nov 10)

| | $10K | $1K, Oct 26 – Nov 6 | $1K, since Nov 9 |
|---|---|---|---|
| Trades | 45 | 19 | 5 |
| Result | **+$684.63** | **−$200** | −$8 |
| Winners | 33% | 16% | 40% |
| Costs (spreads, slippage, fees) | $447 | $143 | $30 |
| Costs as a share of premium | 1.9% | 6.2% | 2.3% |
| Result before costs | +$1,132 | −$57 | +$22 |

About the $10K:

- **Four trades carry it.** QQQ +$452 and IWM +$244 on Oct 30, NVDA +$299 and INTC +$204 on Nov 2. Without them it is
  −$513.
- **Puts made +$1,065 (17 trades); calls lost $380 (28 trades).** That reflects a falling market in this window, and the
  replay shows the call/put result flips from period to period.
- **Costs took 40% of the gross profit.**
- **Expensive contracts won; cheap ones lost.** Premium above $500: 24 trades, +$780. Premium of $500 or less: 21 trades,
  −$95. The cheap contracts are on low-priced stocks (BAC, NFLX, INTC, UBER), where costs are largest relative to the
  move being traded.

## 2. Is the bot rejecting good trades? No.

The live log (`data/v2_signal_outcomes.jsonl`) records what every rejected signal would have done on the stock.

| Why it was rejected | Signals | Would have made | 
|---|---|---|
| **All rejected signals** | **328** | **−33.4R** (profit factor 0.80) |
| Breakout never confirmed | 113 | −13.7R |
| No contract passed the option rules | 52 | −19.6R |
| Daily 3-losses-in-a-row or trade-count limit | 11 | −9.0R |
| Range-bound market, call blocked | 15 | −10.2R |
| 10:00–10:30 pause | 31 | **+6.0R** |
| High-volatility or range-bound market, put blocked | 45 | +5.0R |

The filters are doing their job. The two rows with a positive number are small samples from one falling stretch; the
10:00–10:30 pause was chosen because it lost in all three replay periods, and it has made money since. That is a
warning about how rules are being chosen (section 6), not a reason to remove it.

## 3. The core problem: edge versus cost

**The edge.** Replay of the active 2.5.0 rules, Jun 1 – Nov 10, stock moves only:

- 509 trades, +41.2R, +0.08R per trade. With this much trade-to-trade variation that is not statistically
  distinguishable from zero (t = 1.2).
- The top 10 days made +82R, twice the total. 55% of days lose.
- SPY made +40.5R of the +41.2R and was positive in all four periods. The other 10 symbols together made +0.7R on 437
  trades.
- All the profit comes from trades held to the close (+130R) or stopped out as runners (+145R). Ordinary stop-outs cost
  −220R.

**The cost.** On each live $10K trade I measured the round-trip cost (half the bid/ask spread each way, slippage, fees)
against what 1R of stock movement is worth on that option:

| Symbol | Avg premium | Cost per trade | Cost in R |
|---|---|---|---|
| NVDA | $811 | $11.58 | 0.14 |
| QQQ | $946 | $9.56 | 0.14 |
| SPY | $850 | $7.73 | 0.16 |
| UBER | $186 | $6.79 | 0.20 |
| IWM | $555 | $6.80 | 0.26 |
| INTC | $311 | $10.69 | 0.31 |
| NFLX | $189 | $13.96 | 0.38 |
| BAC | $127 | $7.29 | 0.55 |

Median: **0.26R per trade**, against an edge of 0.08R. The old $1K run's median was 0.50R.

The reason is that the stops are small. A typical 1R is worth $32 on the option, and the round trip costs about $10.

**Replay net of these costs:** +46.8R before costs becomes **−58.4R** after, on the 420 trades in symbols with measured
costs. Only SPY stays positive (+25.9R). This is an estimate (it treats the option as moving in a straight line with
the stock), but the real-quote test agrees in direction: the 2.5.0 replay signals priced on recorded quotes for Oct 26
– Nov 10 lost $393 on 46 trades, and $1,183 with pessimistic fills.

The replay scoreboard that every rule decision so far has used leaves these costs out. It has been overstating results
by roughly 0.2R per trade.

## 4. What I tested

Data: 1-minute bars for 52 symbols, Jun 1 – Oct 9; the production replay's trades for Jun 1 – Nov 10; recorded option
quotes for Oct 26 – Nov 10; the live ledgers and decision logs. "Naive" means my own simplified breakout simulation
(first close beyond the 15-minute range, same stop and runner rules, no regime, sector or confirmation filters).

| # | Theory | Result | Verdict |
|---|---|---|---|
| 1 | Shorter-dated contracts cost less for the same move | See 4.1 | **Supported on real quotes. Recommend a paper trial.** |
| 2 | A cost limit in R selects the trades that can pay for themselves | Limit of 0.20R keeps 229 of 509 replay trades: +12.7R net versus −58.4R for all. Positive in 3 of 4 periods (−10.1R in the latest) | Better, still thin. Worth having as a gate; not an edge by itself |
| 3 | The raw breakout has an edge on a wider list of stocks | Naive, 52 symbols: 4,736 trades, −222R, profit factor 0.92 | **No.** The V2 filters are what lift it to roughly break-even |
| 4 | Stocks with unusual opening volume ("in play") break out better | Naive: opening volume 1.6× normal or more made +40.8R on 407 trades, positive in all 3 periods, mostly from stocks outside the 11. Production replay: opening volume under 0.7× lost in all 3 periods (−15.9R, 110 trades), but −13.9R of that is one period | A lead, not proven (t about 1.4). Research item |
| 5 | Breakouts fail on days SPY's first 15 minutes are already wide (over 30% of its daily range) | Negative in all 3 periods in the production replay (−21.1R, 84 trades) and the naive test (−184.6R, 1,082 trades). On new data: live $10K −$183 on those days versus +$868 on the others, but the replay for the same days shows no difference (+3.5R versus +4.1R) | **Not confirmed out of sample.** Log it, don't build it |
| 6 | Trade index momentum outside the normal move-from-open band (a published SPY method) | SPY 75 trades, −390 basis points, profit factor 0.56. DIA 0.42, IWM 0.75, QQQ 1.09 | **Rejected** |
| 7 | Stop for the day after 2 losses | +27.3R versus +30.8R without it (Jun 1 – Oct 9) | **Rejected.** The existing 3-in-a-row limit is fine |
| 8 | Deeper in-the-money contracts (0.75 delta) | Real quotes: worse on both signal sets; fewer contracts pass the spread rule | **Rejected** |
| 9 | The bot's "expected net R" score ranks trades | Live $10K by score: 1.25 or less +$54, 1.25–1.5 −$42, 1.5–1.75 +$745, above 1.75 −$72 | **No relationship** |

I looked at about 30 trade features in total. With that many, some will look good by chance, which is why only the
ones checked on separate data are listed, and why rows 4 and 5 are not recommendations.

### 4.1 Shorter-dated contracts, on recorded real quotes

Same entry minute, same exit minute, same delta target (0.55) and spread rules; only the expiration changes. Fees and
slippage included, every fill crosses the spread.

| Trades priced both ways | Fill model | Current (7–21 days) | 1–6 days | Shorter was better in |
|---|---|---|---|---|
| $10K's actual entries and exits, 40 trades | start of minute | +$845 | **+$1,064** | 75% of trades |
| same | worst price in the minute | +$316 | **+$460** | 68% |
| 2.5.0 replay signals, 43 trades | start of minute | −$390 | −$288 | 63% |
| same | worst price in the minute | −$1,125 | −$1,003 | 60% |

| On the $10K's entries | Current | 1–6 days |
|---|---|---|
| Average premium | $627 | $275 |
| Average cost per trade | $11.65 | $5.37 |
| Worst single trade | −$139 | −$151 |
| Contracts costing $350 or less | 36% | 60% |

The harness reproduces the live ledger closely (current contracts: +$715 on 44 trades priced, against the actual +$685
on 45), so the comparison is on solid ground.

Limits: 12 sessions in one market stretch; open-interest and volume rules can't be applied to history; shorter-dated
options swing harder, and a position that the bot can't exit (feed outage) decays faster. It does not turn a losing
signal into a winning one: the replay signals still lose.

## 5. Code review

The engineering is careful: every stage fails closed, decisions are logged with their inputs, the shadow log of
rejected signals is exactly the right tool, and the replay runs the production code path. I found no bug that
explains the results. Four things to know:

1. **"Expected net R" is not an expectation** (`trader/v2/pipeline.py:53`, `greek_projection`). It is the payoff if the
   target is reached divided by the loss if the stop is hit. The target is reached in a minority of trades. This number
   ranks same-minute candidates and sets the 1.5 bar for non-favorites, and live it has no relationship to results
   (row 9).
2. **The 10:00–10:30 pause delays entries; it doesn't skip them.** The rejection is per bar
   (`trader/v2/pipeline.py:163`), while the confirmed setup stays alive for 15 bars (`trader/v2/confirmation.py`). In
   the replay, 56 of 424 entries (13%) happen at exactly 10:30. Those trades happened to do well (+17.3R), so I am not
   proposing a change, but the behavior differs from the description in the config comment.
3. **Stops act on completed 1-minute closes**, so the average stop-out is −1.15R, not −1R (191 trades, −220R). I did
   not test an in-bar stop; it would cut the overshoot and also get knocked out more often.
4. **The replay ignores option costs** (`trader/v2/replay.py`, "underlying proxy"), as covered in section 3.

## 6. Recommendations, in order

Nothing here has been built or switched on.

1. **Score every future rule net of option costs.** Add a per-symbol cost in R (from the recorded quotes) to the replay
   report. Smallest change, and it corrects every later decision.
2. **Paper-trial shorter-dated contracts (1–6 days) on the $10K.** First as a shadow price logged beside each real trade
   for two weeks, with no change to what is bought; then switch if it holds. Pass mark, set now: better on at least 60%
   of the same trades and a higher total under both fill models.
3. **Add a cost gate in R** (round-trip cost no more than about 0.20R of the trade's own stop) in place of the
   "expected net R ≥ 1.0" gate, which rejected only 2 signals live. This follows your earlier direction: no symbol is
   banned; a trade is skipped only when its own cost is too large for its own stop.
4. **The $1K accounts.** At $1K the breakout trade cannot cover its costs with the current contracts, and no rule I or
   the earlier work tested changes that. If recommendation 2 holds up, short-dated IWM (about $176), SPY (about $353)
   and NVDA (about $430) contracts come within reach of the $350 cap; that is the first realistic route to the $1K
   trading the same names as the $10K. Until then, expect it to keep losing slowly. Pausing it remains your call.
5. **Get more history before changing the signal.** Five months and one market stretch is too little: SPY aside, nothing
   repeats across periods. Extending the 1-minute cache back two years or more from the same provider and running 2.5.0
   on it untouched would be a true out-of-sample test. This is the most valuable test available, and it is an
   after-hours job.
6. **Hold the rules still through the Dec 4 forward test.** There were eight versions between Oct 23 and Nov 10, all
   tuned on the same five months. Row 5 shows how a rule can lose in every period of that history and still not hold
   on new data. Log candidate ideas (wide SPY open, opening volume) in the shadow log without acting on them.
7. **Do not loosen filters to get more trades** (section 2).
8. **Research, not a build: an "in play" scanner.** If more good trades exist, row 4 suggests they are in whichever
   stocks have unusual opening volume that day, not in a fixed list. It needs a much wider stock list and the longer
   history from recommendation 5 to test properly.

## 7. Limits of this review

- Stock-price tests ignore option prices; the real-quote tests cover only 12 sessions.
- Bars for Oct 12 – Nov 10 are not cached locally, so the new features (opening volume, gap, range width) were
  computed for Jun 1 – Oct 9 only, except row 5.
- The naive simulation is not the production pipeline. It is a second opinion on a larger sample.
- The cost-in-R estimate is linear in the stock move and leaves out time decay, which makes true costs a little higher.
- No heavy replay was run: the market was about to open.

## 8. Files

- `scripts/prep.py`, `feat.py`: compact the bar cache; daily features.
- `scripts/join.py`, `an2.py`: production replay trades split by feature; loss breaker.
- `scripts/sim.py`, `an.py`: naive 52-symbol simulation and its splits.
- `scripts/oos.py`, `live.py`: wide-open check on new data; live ledger breakdown.
- `scripts/cost.py`: cost in R on live fills; replay net of cost; cost gate.
- `scripts/dte.py`: contract choices on recorded real quotes.
- `scripts/nb.py`: index momentum band test.

The scripts read the bar arrays from the folder they sit in; run `prep.py` and `feat.py` first, with
`.venv\Scripts\python.exe` from the repo root.
