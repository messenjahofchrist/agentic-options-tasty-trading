# Strategy 0.1.0 in plain English

This is a test idea, not a proven money-making system. It watches SPY or QQQ and tries to buy one call when price breaks upward, or one put when price breaks downward. It does not sell options short.

## When it may enter

1. From 9:30 to 9:45 a.m. Eastern Time, it records the day's first 15 one-minute bars. The highest high and lowest low make the opening range.
2. It may look for an entry from 9:45 a.m. through 2:30 p.m. Eastern Time.
3. For an upward trade, a one-minute bar must close above the opening-range high plus a small buffer. The previous bar must not already have closed above that level. The 9-bar EMA must be above the 21-bar EMA.
4. For a downward trade, the reverse must be true: a bar closes below the opening-range low minus the buffer, the previous bar was not already below it, and the 9-bar EMA is below the 21-bar EMA.
5. The current bar's volume must be at least 1.5 times the average volume of the preceding bars in the recent 20-bar window. If VWAP is available, price must also be above VWAP for a call or below VWAP for a put.
6. It ignores a setup if the 14-bar ATR is more than 2.5% of the underlying price.

## Which option it may buy

For an upward signal, it looks for a call; for a downward signal, a put. The contract must expire in 7 to 21 days, have an absolute delta from 0.45 to 0.65, a bid/ask spread no wider than both $0.10 and 10% of its midpoint, open interest of at least 100, and today's volume of at least 10. Among eligible contracts it prefers delta near 0.55 and a tighter spread. It can buy at most one contract per entry.

## Risk and exits

- The maximum planned loss budget is 1% of account equity per trade; a separate 2% hard ceiling is configured. The 1% limit is the tighter limit. Sizing rejects a contract if its ask times 100, plus estimated entry and exit slippage and round-trip fees, exceeds the budget. It never adds contracts to use up the budget.
- The underlying price is an exit trigger, not a dollar-risk guarantee: the stop is one ATR against the trade, and the target is two ATR in favor of the trade. It also exits after 180 minutes, at the daily loss limit, or near the session close. Exits are simulated using the option's bid minus modeled slippage. Actual realized P/L is calculated from the option fill prices and fees and recorded beside planned and modeled risk.
- It stops taking entries after three trades in a day, two simultaneous open positions, three consecutive losing trades, a 2% daily loss, or a 5% weekly loss. The kill-switch file blocks new entries.
- It will not open another position in an underlying that already has an open position, so it does not average into that symbol.
- Normal paper fills assume buying at the option ask plus estimated slippage and selling at the bid minus estimated slippage. Commissions and exchange fees are recorded as estimates.
- In the normal paper session, a rough Greek-based estimate must also forecast at least 1R of net reward after spread, slippage, and fees. This estimate is only a gate; it is not evidence the trade will make that return.

## Today's temporary test

The separate `paper-test-session` changes the timing only in memory: it starts a new 15-minute range when the test is launched and allows entries until 15 minutes before the close. It retains the signal, option liquidity checks, 1% sizing limit, Greek-based expected-return gate, kill switch, and data/quote freshness checks. It uses a separate fake-$100,000 ledger and never sends an order to tastytrade. These temporary test rules do not replace this strategy or authorize live trading.
