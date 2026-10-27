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

## Switching versions

```
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.0.0   # revert
powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.1.0   # 2.1
```

The script copies the saved version from `strategies\v2_versions\`, re-activates the manifest and restarts the running
engines. Ledgers and open trades are kept; the restarted engine manages open trades under the chosen version. Git tag
`v2.0-before-v2.1` marks the code before this change.
