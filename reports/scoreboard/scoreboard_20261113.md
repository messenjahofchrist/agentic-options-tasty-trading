# OptioBot scoreboard, 2026-11-13 17:44 ET

Recorded option quotes: 15 sessions, 2026-10-26 to 2026-11-13. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 61 closed trades, +$1,297 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 10 | +$537 | +$647 | +$303 | +$393 | 70% |
| All recorded sessions | 50 | +$1,382 | +$1,712 | +$620 | +$854 | 72% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.17R over 61 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 36 | +$1,156 | 25 | +$141 |
| 0.30R | 46 | +$1,363 | 15 | -$66 |

## $1K account: 11 closed trades, +$163 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 5 | +$200 | +$215 | +$139 | +$134 | 80% |
| All recorded sessions | 10 | +$180 | +$225 | +$87 | +$113 | 80% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals not met so far.
Shorter-dated contract fits this account's $350 limit on 91% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 11 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 8 | +$98 | 3 | +$65 |
| 0.30R | 10 | +$100 | 1 | +$63 |

## Agent trade ideas (never traded)

14 logged, 14 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 14 | -2.13 | -3.02 | -$154 | 29% |

## Agent vetoes

3 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

168 names logged, 48 scored, 120 outside the recorded symbols.
Listed names moved more than the bot's other symbols 69% of the time.

## Wide SPY opening range (logged lead, $10K trades)

6 of 10 classified sessions were wide. Trades on wide days: 24 for +$291; other days: 18 for +$513.

