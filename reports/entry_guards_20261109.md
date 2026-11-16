# Entry guards after the INTC loss (2026-11-09)

Owner question: INTC was bought at 9:55 ET after a 3.6% run in 24 minutes, 2.2 one-minute ATRs past the breakout level,
with expensive options (IV rank 0.67, rising). It pulled back through the stop and then ran to $60. Should the strategy
block entries like that?

Method: 2.5.0 replay with optional, replay-only entry guards (`entry_guard` in `trader/v2/replay.py`, not a live
config key). Results are in **stock R** (no option prices). Driver: `C:\Users\messe\AppData\Local\Temp\claude\C--development-optiobot\da318926-28df-4e0e-a8f1-60cfc6634e82\scratchpad\p2\replay_guards.py`.

## Results: profit factor (trades)

| Variant | Jun–Aug (tuning) | Sep–Oct 9 (validation) | Oct 14–Nov 6 |
|---|---|---|---|
| 2.5.0 baseline | 1.30 (293) | 0.87 (131) | 1.32 (84) |
| Skip if > 1.0 ATR past the level | 1.08 (234) | 0.75 (95) | 1.12 (62) |
| Skip if > 1.5 ATR past the level | 1.03 (255) | 0.87 (105) | 0.98 (76) |
| Skip if > 2.0 ATR past the level | 1.23 (273) | 0.89 (115) | 1.05 (80) |
| Skip if already ran > 0.4 daily range | 1.09 (124) | 0.80 (36) | 1.52 (43) |
| Skip if already ran > 0.5 daily range | 1.06 (184) | 0.72 (78) | 1.42 (58) |
| Skip if already ran > 0.6 daily range | 1.10 (246) | 0.74 (105) | 1.42 (71) |
| No entries before 10:00 (first entry 10:30 with today's pause) | 1.27 (268) | 0.90 (125) | 1.48 (71) |
| No re-entry in a symbol after a loss that day | 1.29 (285) | 0.77 (125) | 1.23 (80) |
| Stop 1 ATR below the breakout level (wider) | 1.25 (292) | 0.92 (126) | 1.18 (86) |

**Baseline trades split by how stretched the entry was:**
- **More than 3 ATR past the level:** PF 1.78, 1.30 and 1.27 in the three periods. These were profitable everywhere.
- **First 30 minutes:** PF 1.19, 1.35 and 1.32. Also profitable in all three periods.

## Conclusion

The data does not support blocking INTC-style entries:
- Stretched and early breakouts were, on average, among the *better* trades. INTC was an ordinary loss from a pattern
  that pays more often than not.
- Every guard either hurt the tuning period badly, or helped one period and hurt another.
- None lifts the validation period above PF 1.25.
- "No entries before 10:00" is the least inconsistent (slightly worse, slightly better, better), but it still fails
  validation (0.90).

No rule is recommended, and nothing was changed in the live strategy.

The expensive-option part (IV rank) can't be replayed, because there is no IV history. The live decision log already
records IV rank, IV trend, the breakout level and the ATR on every entry. The weekly check can measure this going
forward with no code change.
