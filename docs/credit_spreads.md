# CREDIT_SPREADS (PAPER), from 2026-10-30

Owner request (2026-10-30), after a week of losses on the opening-range bots: sell options instead of buying them.
Runs next to the opening-range bots with its own paper money ($10K and $1K ledgers in `data/credit_spreads/`). It
never reads or changes `strategy.txt`, the V2 config or the other ledgers. No broker order is ever sent.

## The idea

Buying a short-dated option pays the bid/ask gap, fees and time decay, and options are usually priced for more
movement than happens (across the 55 V2 symbols on 2026-10-23, implied/realized volatility had a median of 1.49).
A credit spread takes the other side: sell one option, buy a further-out one as protection, keep the credit if the
price stays on the right side. The protection caps the worst loss at (width - credit) x 100 + fees.

## Rules (1.0.0, `strategies/credit_spreads.json`)

| Step | Rule |
|---|---|
| Underlyings | SPY, QQQ |
| When | A pass every 15 minutes, 08:45-15:15 CT (task `\OptioBot\CreditSpreads`). New spreads only 10:00-15:00 ET; open spreads are managed all session. |
| Direction | Price above its 50-day average: sell a put spread. Below: sell a call spread. |
| Expiration | Closest to 35 days out, inside 28-45 days |
| Option sold | Delta closest to 0.20 (inside 0.15-0.25); open interest at least 100; bid/ask gap at most 15% of its price |
| Protection | $5 wide on the $10K ledger, $2 on the $1K (the widest whose worst loss fits the per-trade limit) |
| Price | Sell at the bid, buy the protection at the ask (no mid-price fills). Fees $0.70 per contract per leg, each way. |
| Minimum credit | 15% of the width |
| Limits | One new spread per underlying per day; no second spread in the same expiration; no new spreads with VIX at or above 30. $10K: worst loss per trade $500, at most 4 open, all open worst losses at most 40% of equity. $1K: $225, 2 open, 45%. |
| Take profit | Buy back once it costs half the credit or less |
| Stop loss | Buy back once the loss reaches twice the credit |
| Time exit | Close with 21 days left |

Exit decisions use the mid price of the two legs; the fill is the worse side (buy back the sold option at the ask,
sell the protection at the bid). Stale or missing quotes: the spread is held and the next pass tries again.

## Evidence

None from past data: the replay data are stock prices only, with no option prices, so this cannot be back-tested
here. This is a forward paper test. Pass/fail bar, set before it starts: after 20 closed spreads or 6 weeks
(whichever is later), the $10K ledger must be up after fees and its worst drawdown must stay under 15%.

## Switches

```
powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 off       # no new spreads; open ones still managed
powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 on
powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 remove    # stop the bot completely
powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 install
powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 1.0.0     # a saved version
.venv\Scripts\python.exe -m trader.cli spreads-status                             # ledger summary
```

Git tag `before-credit-spreads` marks the code before this change. Every pass is logged to
`data/credit_spreads/spreads.log` and every decision to `data/credit_spreads/decisions.jsonl`.
