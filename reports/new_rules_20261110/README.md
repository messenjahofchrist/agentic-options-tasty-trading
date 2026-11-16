# 2026-11-10 rule ideas: replay check (read-only)

Asked by the owner after the $1K bought the same failed IWM breakout twice and NVDA gave back about half its gain past
the target. Code is built and saved as V2 2.7.0 (`strategies/v2_versions/professional_v2_2.7.0.json`); **not
activated** pending the owner's decision on these results.

Method: `trader/v2/replay.py` (production pipeline + management on 1-minute history, underlying proxy, no option
prices), the active 2.5.0 config plus one change per variant. Periods: Jun 1–Aug 31 and Sep 1–Oct 9 (massive cache),
Oct 16–Nov 10 (tastytrade). Profit factor / total R.

| Variant | Jun–Aug | Sep–Oct 9 | Oct 16–Nov 10 | Total R |
|---|---|---|---|---|
| 2.5.0 (active) | 1.30 / +40.3 | 0.87 / −9.5 | 1.23 / +10.3 | **+41.2** |
| 1. No re-entry 60 min after a failed breakout | 1.26 / +31.2 | 0.92 / −5.3 | 0.98 / −0.7 | +25.2 |
| 1. … 30 min | 1.18 / +24.2 | 0.92 / −5.7 | 1.04 / +1.6 | +20.1 |
| 1. … 120 min | 1.12 / +14.5 | 1.07 / +4.0 | 0.95 / −1.9 | +16.5 |
| 2. Target reachable (reach 1.0) | 1.29 / +36.4 | 0.86 / −9.9 | 1.31 / +13.2 | +39.7 |
| 2. … reach 0.75 (stricter) | 1.44 / +50.6 | 0.86 / −9.6 | 1.20 / +8.2 | +49.2 |
| 2. … reach 1.5 (looser) | 1.31 / +40.2 | 0.87 / −9.4 | 1.25 / +10.9 | +41.7 |
| 3. Runner trail 1.0R (from 1.5R) | 1.23 / +31.6 | 0.88 / −9.3 | 1.17 / +7.7 | +30.0 |
| 1 + 2 | 1.26 / +28.9 | 1.02 / +1.1 | 1.07 / +2.6 | +32.7 |
| 1 + 2 + 3 (2.7.0 as built) | 1.26 / +29.5 | 0.99 / −0.9 | 0.93 / −2.5 | +26.1 |

Reading:
* **Idea 1 costs money in 2 of 3 periods** at every window tried. It helps only in the losing Sep–Oct period. Same
  finding as the 11/09 "no re-entry after a loss" test: second attempts at a breakout win often enough to pay for the
  IWM-style failures.
* **Idea 2 is close to neutral** at reach 1.0 (−4.0R, −0.4R, +2.9R). It does no clear harm, and the evidence for it
  is weak. Reach 0.75 looks better in total, but only because of Jun–Aug; it is worse in the most recent period.
* **Idea 3 costs money in 2 of 3 periods** (−8.7R, +0.2R, −2.6R). The 10/29 replay found a 0.5R trail worse too. On
  average the 1.5R trail's bigger winners pay for days like NVDA on 11/10.
* All three together: −15R against 2.5.0.

Limits: stock-move proxy, not option prices; one contract; the $10K universe and ranking. Treat as direction, not size.

Files: `massive.log`, `tasty.log`, `*-summary.json`, `*-trades.json` here. Script:
`replay_new_rules.py` (run from the repo root with PYTHONPATH=.).
