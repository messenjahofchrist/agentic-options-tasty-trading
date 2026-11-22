# OptioBot scoreboard, 2026-11-16 17:45 ET

Recorded option quotes: 16 sessions, 2026-10-26 to 2026-11-16. Everything below is PAPER. Nothing on this page changes what the bot trades; it is the evidence for deciding that.

## $10K account: 65 closed trades, +$1,336 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 14 | +$562 | +$679 | +$261 | +$355 | 64% |
| All recorded sessions | 54 | +$1,407 | +$1,743 | +$577 | +$815 | 70% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals met so far.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.17R over 65 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 39 | +$1,165 | 26 | +$170 |
| 0.30R | 50 | +$1,402 | 15 | -$66 |

## $1K account: 12 closed trades, +$193 realized

### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)

Same entry and exit minutes, recorded real quotes. "Pessimistic fills" uses the worst price inside the minute.

| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |
|---|---|---|---|---|---|---|
| Trial (from 2026-11-11) | 6 | +$226 | +$246 | +$163 | +$157 | 83% |
| All recorded sessions | 11 | +$207 | +$257 | +$112 | +$137 | 82% |

Pass mark (short better on at least 60% of the same trades and a higher total under both fill models; judged after 10 sessions): better-share met, totals not met so far.
Shorter-dated contract fits this account's $350 limit on 92% of its trades.

### Cost gate (round-trip option cost as a share of the trade's planned risk)

Median cost 0.19R over 12 trades.

| Limit | Trades kept | Kept made | Trades dropped | Dropped made |
|---|---|---|---|---|
| 0.20R | 9 | +$127 | 3 | +$65 |
| 0.30R | 11 | +$130 | 1 | +$63 |

## Agent trade ideas (never traded)

19 logged, 19 scored, 0 waiting for recorded prices. The bar: an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks with a positive net R total.

| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |
|---|---|---|---|---|---|
| claude | 19 | +1.03 | -0.41 | -$363 | 42% |

## Agent vetoes

3 vetoes issued so far.
- $10K: 0 signals blocked.
- $1K: 0 signals blocked.

## Agent watchlist ("in play")

211 names logged, 70 scored, 141 outside the recorded symbols.
Listed names moved more than the bot's other symbols 63% of the time.

## Wide SPY opening range (logged lead, $10K trades)

7 of 11 classified sessions were wide. Trades on wide days: 28 for +$330; other days: 18 for +$513.

