# OptioBot scoreboard, 2026-11-18 17:45 ET

Recorded option quotes: 18 sessions, 2026-10-26 to 2026-11-18. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 75 closed trades, +$1,125 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 24 | +$357 | +$489 | -$122 | +$25 | 54% |
| All recorded sessions | 64 | +$1,202 | +$1,553 | +$194 | +$485 | 66% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share not met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.18R over 75 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 43 | +$982 | 32 | +$143 |
| 0.30R | 56 | +$1,282 | 19 | -$158 |

## $1K account: 16 closed trades, +$191 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 10 | +$226 | +$258 | +$88 | +$113 | 80% |
| All recorded sessions | 15 | +$207 | +$269 | +$37 | +$93 | 80% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.
Shorter-dated contract fits this account's $350 limit on 94% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 16 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 9 | +$127 | 7 | +$64 |
| 0.30R | 14 | +$130 | 2 | +$61 |

## Agent trade ideas (never traded)

25 logged, 25 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 25 | +4.01 | +2.02 | -$366 | 44% |

## Agent vetoes

10 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

339 names logged, 112 scored, 227 outside the recorded symbols.
Listed names moved more than the bot's other symbols 72% of the time.

## Wide SPY opening range (logged lead, $10K trades)

8 of 13 classified sessions were wide. Trades on wide days: 34 for +$90; other days: 22 for +$541.

