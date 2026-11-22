# OptioBot scoreboard, 2026-11-17 17:44 ET

Recorded option quotes: 17 sessions, 2026-10-26 to 2026-11-17. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 69 closed trades, +$1,365 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 18 | +$612 | +$726 | +$228 | +$340 | 56% |
| All recorded sessions | 58 | +$1,457 | +$1,790 | +$544 | +$800 | 67% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share not met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.18R over 69 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 39 | +$1,165 | 30 | +$199 |
| 0.30R | 51 | +$1,494 | 18 | -$129 |

## $1K account: 15 closed trades, +$220 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 9 | +$258 | +$282 | +$120 | +$139 | 78% |
| All recorded sessions | 14 | +$239 | +$293 | +$69 | +$119 | 79% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.
Shorter-dated contract fits this account's $350 limit on 93% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 15 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 9 | +$127 | 6 | +$92 |
| 0.30R | 13 | +$158 | 2 | +$61 |

## Agent trade ideas (never traded)

21 logged, 21 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 21 | +2.40 | +0.81 | -$363 | 43% |

## Agent vetoes

10 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

273 names logged, 90 scored, 183 outside the recorded symbols.
Listed names moved more than the bot's other symbols 69% of the time.

## Wide SPY opening range (logged lead, $10K trades)

7 of 12 classified sessions were wide. Trades on wide days: 28 for +$330; other days: 22 for +$541.

