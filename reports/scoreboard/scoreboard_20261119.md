# OptioBot scoreboard, 2026-11-19 17:45 ET

Recorded option quotes: 19 sessions, 2026-10-26 to 2026-11-19. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 79 closed trades, +$1,059 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 27 | +$307 | +$454 | -$213 | -$59 | 56% |
| All recorded sessions | 67 | +$1,153 | +$1,519 | +$104 | +$402 | 66% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share not met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.18R over 79 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 45 | +$928 | 34 | +$131 |
| 0.30R | 58 | +$1,228 | 21 | -$169 |

## $1K account: 18 closed trades, +$175 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 11 | +$227 | +$258 | +$61 | +$88 | 73% |
| All recorded sessions | 16 | +$207 | +$268 | +$9 | +$67 | 75% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.
Shorter-dated contract fits this account's $350 limit on 94% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 18 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 10 | +$115 | 8 | +$60 |
| 0.30R | 16 | +$113 | 2 | +$61 |

## Agent trade ideas (never traded)

28 logged, 28 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 28 | +8.00 | +5.80 | -$366 | 50% |

## Agent vetoes

10 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

397 names logged, 137 scored, 260 outside the recorded symbols.
Listed names moved more than the bot's other symbols 75% of the time.

## Wide SPY opening range (logged lead, $10K trades)

9 of 14 classified sessions were wide. Trades on wide days: 38 for +$24; other days: 22 for +$541.

