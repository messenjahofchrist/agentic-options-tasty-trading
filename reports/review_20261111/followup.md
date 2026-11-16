# Build log: review recommendations into PAPER production (2026-11-11)

Owner instruction, 2026-11-11 about 09:20 CT: build every recommendation in `README.md` plus the structure and AI
items, start now, in production today on paper, and keep the AI agent independent of any one vendor (set in a config
file). Later the same day: the owner decides when (or whether) to freeze versions, and wants fewer emails.

## Live

| Piece | What it does | Where |
|---|---|---|
| **V2 2.8.0** (active since 14:59 CT, both engines) | 2.5.0 plus: option cost in R logged on every entry (0.20R limit, **not enforced**); advisor veto **enforced** (block-only, one symbol, stated reason, ends at a stated time; ignored once the veto file is 90 minutes old). | `strategies/professional_v2.json`, `trader/v2/pipeline.py`, `trader/v2/vetoes.py` |
| Vendor-neutral agents | Each AI job is a role in `agents.json`; an agent is any command that takes a prompt. Roles: `advisor` (Claude, then Codex), `reviewer_1` (Claude, no tools), `reviewer_2` (Codex, no tools). | `agents.json`, `trader/agents.py` |
| News advisor | Weekdays 08:00 CT and hourly to 14:00. Market view, in-play watchlist, vetoes, trade ideas (never traded). | `trader/advisor.py`, task `\OptioBot\Advisor` |
| Hourly two-agent review | Unchanged behaviour, but the two reviewers now come from `agents.json`. | `trader/supervisor.py` |
| Nightly scoreboard | Weekdays 16:40 CT. Shorter-dated contract trial, cost gate, agent ideas / vetoes / watchlist, wide-SPY-open lead, all on recorded real quotes. Report file only (no email). | `trader/scoreboard.py`, task `\OptioBot\Scoreboard`, `reports/scoreboard/latest.md` |
| Option cost in R | Per-symbol table rebuilt nightly; every replay summary shows results net of option cost. | `trader/cost_model.py`, `data/option_cost_table.json` |
| Fewer emails | `notifications.json`: routine emails off; sent only for failures and agent decisions (veto, exit/tighten advice, trade idea) and high/critical or failed reviews. End-of-day account emails left on. | `notifications.json`, `trader/notifications.py` |

## Built, off until switched on

- **Contract choice** (`contract_policy` in the V2 config: `min_dte`, `max_dte`, `nearest_expiry_first`). Absent in
  2.8.0, so the bot still buys 7-21 day contracts. Moving to 1-6 day contracts is a config-only new version.
- **Cost gate enforcement**: `cost_gate.enforce` per account.
- **Version freeze guard**: `scripts/set_v2_version.ps1` refuses a switch only while `strategies/FREEZE.json` exists.
  **No freeze is in force.** The owner decides if and when to create one.

## Decisions the owner can overrule

1. Cost gate logs but does not block (see scoreboard: at 0.20R the $10K would have dropped 23 trades that made +$174).
2. Shorter-dated contracts are a shadow price for 10 sessions first; pass mark fixed in advance (better on 60% of the
   same trades and a higher total under both fill models).
3. Agent trade ideas earn a small paper allocation only after 50 scored ideas over 4+ weeks with a positive total
   after option cost.
4. End-of-day account emails stay on; trade ideas count as an agent decision for emailing.

## First scoreboard (2026-11-11, 13 recorded sessions)

- Shorter-dated contracts on the $10K's own trades: +$1,180 vs +$898 (44 trades), better on 73%; still ahead with
  pessimistic fills (+$505 vs +$306). Trial day one: 4 trades, better on 3. On the $1K, a 1-6 day contract fit the
  $350 limit on every trade.
- Agent ideas so far: 3 (Claude), +0.14R after option cost. Far too few to judge.
- Wide SPY opening range: $10K trades on wide days -$219 (13), other days +$513 (18). Still a logged lead only.

## Problems found tonight

- **Scoreboard crashed at 16:40** on the recorder's new VIX file (no session times). Fixed; each section now fails on
  its own without losing the report. Re-run by hand at 18:37: OK.
- **Claude's 5-hour usage limit** was hit around 14:30 CT; the 14:30 and 15:30 reviews ran with Codex only and the
  advisor fell back to Codex. The bot's Claude jobs share the owner's own Claude account limits. Options: put
  `codex` first for the advisor, run the advisor every two hours, or give the bot its own account.
- **Gemini** is wired but its account returned "402 Payment Required / quota exhausted".

## Two-year history test

Massive's plan covers two years of one-minute bars (2024-11-12 returned data; 2024-06-04 was refused). Month files are
downloading to `data/backtest_cache/chunks/` (`python -m trader.v2.history_cache status`). The windowed replay
(`trader.v2.replay.run_long_replay`) loads one quarter at a time and reproduced the earlier replay exactly on a test
week (31 trades, identical results).

### Results (run 19:30-20:44 CT, 2.8.0 rules untouched, 2024-12-02 .. 2026-11-10, 487 sessions)

Files: `long_replay/long-20261112T024338Z-summary.json` and `-trades.json`; log `long_replay.log`.

| | Trades | Gross R | Gross R/trade | Net of option cost R | Net R/trade |
|---|---:|---:|---:|---:|---:|
| All two years | 2,088 | -7.9 | -0.004 (t -0.12) | -431.3 | -0.21 |
| Dec 2024 - May 2026 (before the rules were tuned) | 1,569 | -41.0 | -0.026 | -368.4 | -0.23 |
| Jun - Nov 2026 (the period the rules were tuned on) | 519 | +33.1 | +0.064 | -62.9 | -0.12 |

The Jun-Nov 2026 slice reproduces the earlier replay (509 trades, +0.08R), so the long run is consistent with it. The
18 months before that show **no gross edge**. Net of option cost, every quarter lost:

| Quarter | Trades | Gross R | Net R |
|---|---:|---:|---:|
| 2024-Q4 (Dec) | 92 | +2.3 | -18.0 |
| 2025-Q1 | 260 | -1.0 | -51.7 |
| 2025-Q2 | 185 | -2.0 | -47.2 |
| 2025-Q3 | 288 | -29.1 | -96.0 |
| 2025-Q4 | 287 | +2.7 | -62.9 |
| 2026-Q1 | 259 | -42.9 | -86.5 |
| 2026-Q2 | 296 | +43.7 | -6.1 |
| 2026-Q3 | 293 | -3.8 | -56.6 |
| 2026-Q4 (to 11/10) | 128 | +22.2 | -6.5 |

By symbol (gross R / net R): AAPL +16.6 / +2.9, NVDA +22.1 / -8.5, PLTR +1.8 / -18.5, GOOGL -3.9 / -19.2,
IWM +23.5 / -26.1, AMZN -17.6 / -36.1, QQQ -15.7 / -39.2, SPY +3.9 / -41.6, NFLX -1.5 / -65.6, INTC -21.3 / -86.3,
UBER -15.8 / -93.1. Only AAPL is positive after cost.

Other cuts: calls +15.9 gross / -195.8 net, puts -23.8 / -235.5. First hour (09:xx ET entries) +36.7 gross, 10:xx
-35.8. Cost gate at 0.20R keeps 1,299 trades: +21.2 gross, -128.1 net; at 0.30R keeps 1,686: +24.9 / -219.5.
Median option cost 0.16R per trade.

Caveats: option cost uses the per-symbol table from recorded Oct-Nov 2026 quotes for 7-21 day contracts (older
spreads unknown); 2025 FOMC dates in the replay were entered from memory, not checked against a source; the replay
cannot see news. Even halving the option cost (shorter-dated contracts) leaves the two-year result negative, because
the gross edge is about zero.

Nothing was changed because of these results.

## Version 2.9.0 side test (built 2026-11-12, runs from 2026-11-13)

Owner, 11/12: build steps 1-3 as a separate 2.9, keep 2.8 as it is (winning streak), run both all day and compare,
with a daily page that crowns a winner.

- **2.8.0 stays the active version**; `strategies/professional_v2.json` and its manifest were not touched. A one-week
  replay of 2.8.0 with only the old data file still gives exactly 31 trades, -2.31R, -7.37R net (as on 11/11).
- **2.9.0** = 2.8.0 plus: (1) stocks in play: at 09:45 ET it scans 105 stocks (`strategies/v29_universe.json`) and
  trades only the up to 8 whose first 15 minutes of volume is at least 2x their own 20-day average; (2) stop at least
  1% of the price away, target at least 2 stops, no breakeven move; (3) 1-6 day contracts, nearest expiry first.
  Config `strategies/v2_versions/professional_v2_2.9.0.json`, its own manifest beside it.
- **Two new PAPER ledgers** with the same account rules as today's: `data/v29/original_paper` ($10K) and
  `data/v29/small_account_paper` ($1K: $350 per trade, one position, 4% friction gate, tight option rules).
- **Tasks**: V29Engine10K (08:40), V29Engine1K (08:47), V29Report (every minute), V29Baseline (17:15),
  ResourceMonitor (every 5 minutes). Installer: `scripts/install_v29_tasks.ps1` (`-Remove` takes them all out).
- **Pages**: `data/v29_dashboard.html` (live 2.9, purple), `data/compare_28_vs_29.html` (face-off and crown), dated
  copies in `reports/side_by_side/`.
- **Limits**: the advisor can veto only 2.8's 11 symbols, so new 2.9 stocks have no AI veto; option cost for the new
  stocks is not measured yet (no recorded quotes), so the two-year replay charges them the median measured cost.
- **Measured**: per unit of risk (R), 1-6 day contracts cost about the same as 7-21 day ones; they are cheaper in
  dollars, not in R. Their advantage on recorded quotes came from moving more with the stock, not from cost.
- Two-year replay of 2.9.0 on the wide list (run 23:50-01:07 CT 11/12-13, 487 sessions, picks on 456 days, 5.0 a day):
  1,004 trades, gross -45.7R (-0.045R/trade, PF 0.89, 44.5% winners), net of 1-6 day option cost -94.7R (926 trades
  charged the median cost). Only 2025-Q2 (+3.3R) and 2026-Q3 (+0.2R) were positive after cost. Fewer trades than 2.8
  (2,088) and a smaller net loss per trade (-0.09R vs -0.21R), but still no gross edge in stock terms.
  Files: `long_replay_v29/long-20261113T070646Z-*`.
- **$1K simulation in option dollars, 2025-11-12 .. 2026-11-11** (owner request 11/12; page
  https://claude.ai/artifact/PmUjfSw9WNyWypZ4mAqGWd; scripts in the session scratchpad: `sim_1k.py`, `build_1k_page.py`).
  Option prices modeled with Black-Scholes (vol = 1.08 x robust 20-day stock vol, fitted to recorded Oct-Nov premiums);
  $1K rules applied in time order. Each day starting at $1,000: **2.8 -$1,801** (311 of 1,127 signals traded),
  **2.9 +$2,747** (133 of 533). One account, no top-ups: 2.8 ends at $470 and stops trading after 12/31/2025 (the 40%
  rule shrinks the budget below $350); 2.9 ends at $2,532. **2.9's result is fragile**: its 10 best trades made +$7,060
  and the other 123 lost -$4,314 (stock-measured -7.1R); with options 30% dearer than modeled it is +$414, at 60%
  dearer -$68. 2.8 loses in every case. 2.9 cannot trade single stocks on Fridays under 1-6 DTE if only Friday
  expirations exist (107 signals). NFLX 10:1 split on 2025-11-17 confirmed from Massive reference data.

- **What separates good 2.9 picks from bad ones** (2026-11-13; year 1 = Dec 2024-Nov 2025 found, year 2 checked):
  win rate 44.5% against a 47.3% breakeven (2.8: 35.5% against 35.7%). None of the stock-choice measures (opening
  volume ratio, scan rank, number of picks, price, how far the stock had already run, stretch from average, call vs
  put) was better in both years. Stocks that made money in year 1 lost -19.9R in year 2; year-1 losers made +14.6R.
  Only timing held: entries before 10:00 ET +0.07R / +0.12R a trade, noon entries -0.12R / -0.21R (same direction
  as 2.8's first-hour result). Pullback setups +0.09R / +0.06R on 35 / 44 trades (too few). Nothing changed.

## Checks

- 354 tests pass. `v2-validate` OK on 2.8.0. `v2-readiness --skip-stream`: READY (the one WARN is the skipped stream).

Backups of every changed file: `data/backups/20261111_pre_review_build/`.
