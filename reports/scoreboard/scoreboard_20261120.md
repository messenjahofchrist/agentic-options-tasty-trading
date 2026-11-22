# OptioBot scoreboard, 2026-11-20 17:46 ET

Recorded option quotes: 20 sessions, 2026-10-26 to 2026-11-20. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 83 closed trades, +$1,053 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 30 | +$333 | +$503 | -$235 | -$69 | 60% |
| All recorded sessions | 70 | +$1,179 | +$1,568 | +$82 | +$392 | 67% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.18R over 83 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 47 | +$987 | 36 | +$66 |
| 0.30R | 62 | +$1,223 | 21 | -$169 |

## $1K account: 20 closed trades, +$158 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 11 | +$227 | +$258 | +$61 | +$88 | 73% |
| All recorded sessions | 16 | +$207 | +$268 | +$9 | +$67 | 75% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.
Shorter-dated contract fits this account's $350 limit on 85% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 20 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 11 | +$140 | 9 | +$18 |
| 0.30R | 18 | +$97 | 2 | +$61 |

## Agent trade ideas (never traded)

30 logged, 30 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 30 | +10.99 | +8.75 | -$366 | 53% |

## Agent vetoes

10 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

462 names logged, 158 scored, 304 outside the recorded symbols.
Listed names moved more than the bot's other symbols 77% of the time.

## Wide SPY opening range (logged lead, $10K trades)

9 of 15 classified sessions were wide. Trades on wide days: 38 for +$24; other days: 26 for +$536.

