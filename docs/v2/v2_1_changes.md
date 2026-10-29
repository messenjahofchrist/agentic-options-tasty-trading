# PROFESSIONAL_STRATEGY_V2 2.1.0 (PAPER), 2026-10-27

Owner request (2026-10-27): let winners run, improve trade selection, test today, keep it easy to reverse.

## What changed (every change is a switch in `strategies/professional_v2.json`)

| Switch | 2.0.0 | 2.1.0 | What it does |
|---|---|---|---|
| `management.let_winners_run` | off | **on** | At the original target the trade is held, not sold. Its stop locks in +1R and trails 1R behind the best price; sold when the price closes back through it (or at the close). |
| `management.breakeven.require_option_above_floor` | off | **on** | Breakeven arms only once the option bid has covered cost + fees, so it can no longer fire at a loss on the bar it arms (the XLE trade on 2026-10-27). |
| `entry_filters` | off | **on** | No V2 entries in TLT, and none 10:00-10:30 ET (09:00-09:30 CT). |
| `daily_trend` (20-day average) | off | off | Built and tested; inconsistent across periods (hurt Aug-Oct by 0.13R/trade), so not enabled. |
| `trade_limits` (3 per day) | off | off | Built and tested; worse in the most recent period, so not enabled. |

The early trailing stop (1 ATR behind the best price after +1R) stays on: turning it off was worse in two of three periods.

## Replay evidence (underlying proxy, 14-symbol $10K universe; expectancy in R per trade)

| Variant | Jun 1-Aug 15 | Aug 16-Oct 9 | Oct 1-26 |
|---|---|---|---|
| 2.0.0 (V2 as of 2026-10-26) | -0.158 (304) | -0.088 (228) | -0.231 (105) |
| 2.0.0 + entry filters | +0.099 (294) | -0.085 (216) | -0.008 (100) |
| **2.1.0** (filters + let winners run) | **+0.125** (292) | -0.092 (216) | **+0.010** (100) |

Decision rule set before round two: a change is enabled only if it beats 2.0.0 in all three periods (let winners run was
the owner's request and was at least as good as 2.0.0 in all three). Caveats: the entry filters came from the same
replay data's scorecard (TLT and 10:00-10:30 ET lost in all three periods), so part of the gain may not repeat; option
spreads, decay and IV are not in the replay; the $1K ledger's other 33 symbols were not replayed. Results:
`data/v2_replay/v21` (round one) and `data/v2_replay/v21b` (round two); `v2-replay --variants` reruns any variant.

## 2.2.0 (2026-10-27): no new entries in the last 30 minutes

Owner request after the $1K ledger bought PFE at 15:58 ET and the session-close flatten sold it a minute later
(-$2.29, fees and spread). `late_entry_cutoff` (new switch; off in the saved 2.0.0 and 2.1.0, on in 2.2.0) rejects
V2 entries from 30 minutes before the session's actual close (15:30 ET, 12:30 ET on an early-close day). The original
strategy's entry window (09:30-16:00 ET) is unchanged.

| Variant | Jun 1-Aug 15 | Aug 16-Oct 9 | Oct 1-26 |
|---|---|---|---|
| 2.1.0 | +0.125 (292) | -0.092 (216) | +0.010 (100) |
| 2.2.0 (2.1.0 + cutoff) | +0.130 (288) | -0.093 (212) | +0.010 (99) |

The cutoff removed 9 replay trades (total -0.91R): effectively neutral in the replay. The replay is an underlying
proxy, so it understates what a late entry costs (the option spread is paid in full on a trade that is held for
minutes). Results: `data/v2_replay/v22`.

## 2.3.0 (2026-10-28): the 3-losses-in-a-row block resets each day

The original strategy stops entries after 3 losing trades in a row and only a winning trade resets the count, so a
streak carried into later sessions with no way to end it. On 2026-10-28 both ledgers were locked this way (the $1K
from the 2026-10-27 close, the $10K after its first trade). Owner-authorized: `loss_streak.reset_each_session` (new
switch; off in the saved 2.0.0-2.2.0, on in 2.3.0) makes the V2 entry path count only trades closed today (ET). The
2% daily and 5% weekly loss limits are unchanged. The original-strategy shadow benchmark keeps the original count.
The replay already reset the streak each session, so the replay numbers above are unchanged.

## 2.4.0 (2026-10-29): hold winners longer

Owner request (after a Gemini review proposing a bundle of changes): hold winning trades longer, test before the open.
Each candidate is one change on top of 2.3.0; replays on the 14-symbol $10K universe, expectancy in R per trade
(trades). The tastytrade window is now Oct 5-28.

| Variant | Jun 1-Aug 15 | Aug 16-Oct 9 | Oct 5-28 | Total R | Win rate |
|---|---|---|---|---|---|
| 2.3.0 | +0.130 (288) | -0.093 (212) | -0.006 (101) | +17.2 | 0.56 / 0.46 / 0.47 |
| **2.4.0**: early trail off, runner trails 1.5R | **+0.129** (249) | **-0.067** (183) | **+0.076** (89) | **+26.6** | 0.39 / 0.34 / 0.37 |
| early trail off (runner 1.0R) | +0.107 | -0.111 | +0.128 | +18.1 | |
| trail arms 1.0R, 2 ATR behind | +0.082 | -0.076 | -0.012 | +6.6 | |
| trail arms 1.5R, 2 ATR behind | +0.079 | -0.088 | -0.003 | +4.3 | |
| trail arms 1.8R, 2 ATR behind (Gemini) | +0.055 | -0.102 | -0.007 | -5.9 | |
| Gemini management (breakeven 1.25R, trail 1.8R/2 ATR, runner 1.25/0.75) | +0.032 | -0.150 | -0.040 | -23.8 | |
| no 10:00-10:30 ET skip window | +0.015 | -0.006 | -0.079 | -5.3 | |
| skip window 09:45-10:05 ET (Gemini) | -0.033 | +0.050 | -0.065 | -5.3 | |
| minimum opening range 0.3% funds / 0.6% stocks (Gemini) | +0.097 | -0.098 | -0.102 | -2.7 | |
| all of Gemini's testable changes | -0.042 | -0.096 | -0.044 | -34.0 | |

2.4.0 ties 2.3.0 in Jun-Aug (-0.001R) and is better in the other two periods, with a smaller worst drawdown (30R vs 34R).
It wins less often (about 37% of trades vs about 50%) and makes more per win. Caveats: 17 variants were tried on the same
data, so the best one is partly luck; option decay over the longer holds is not in the replay. `min_opening_range` and a
numeric `trailing.atr_multiple` were added as switches (off). Results: `data/v2_replay/v24`, `data/v2_replay/v24b`.

Not adopted from the review (checked against the code): the economic gate already charges the full spread (entry at
the ask, exit at the bid), so charging it again would count it twice; fees are $1.40 a round trip, not $1.25; the
`context.max_bar_age_seconds` is 150, not 180, and stale bars fell to 9 of 1,382 decisions after the 2026-10-27 candle
feed fix; the proposed $1K keys (daily dollar limit, max open positions, option rule overrides) do not exist.

The $1K ledger's per-trade cap went from $150 to $225 (owner, 2026-10-29): `scripts\set_small_account_bet.ps1 225`;
`... 150` reverts.

## Switching versions

```
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.0.0   # revert
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.1.0   # 2.1
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.2.0   # 2.2
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.3.0   # 2.3
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.4.0   # 2.4 (active from 2026-10-29)
```

The script copies the saved version from `strategies\v2_versions\`, re-activates the manifest and restarts the running
engines. Ledgers and open trades are kept; the restarted engine manages open trades under the chosen version. Git tag
`v2.0-before-v2.1` marks the code before this change.
