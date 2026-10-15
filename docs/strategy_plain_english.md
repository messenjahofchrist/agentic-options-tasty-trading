# Strategy 0.1.4 in plain English

This is a test idea, not a proven money-making system. It watches 14 liquid funds and large stocks at once (SPY, QQQ, IWM, DIA, TLT, NVDA, AAPL, AMZN, GOOGL, PLTR, NFLX, INTC, UBER, BAC) and tries to buy one call when a stock's price breaks upward, or one put when it breaks downward. It does not sell options short. `paper-session --symbol X` (repeatable) limits a run to fewer names.

## When it may enter

1. From 9:30 to 9:45 a.m. Eastern Time, it records the day's first 15 one-minute bars. The highest high and lowest low make the opening range.
2. In PAPER mode, the entry window covers the whole regular session (9:30 a.m. to the close). In practice no signal can form until the opening range and indicator warm-up are complete, around 9:53 a.m. On a normal day the last eligible minute is 3:59 p.m. Eastern Time; on an early-close day it stops at that day's earlier close. Entries are not allowed at or after the close because the market is shut.
3. For an upward trade, a one-minute bar must close above the opening-range high plus a small buffer, and price must have crossed that level within the last 15 bars (`breakout_lookback_bars`), so a break that happened hours ago does not count. The 9-bar EMA must be above the 21-bar EMA.
4. For a downward trade, the reverse must be true: a bar closes below the opening-range low minus the buffer after crossing it within the last 15 bars, and the 9-bar EMA is below the 21-bar EMA.
5. The current bar's volume must be at least 1.2 times the average volume of the preceding bars in the recent 20-bar window. If VWAP is available, price must also be above VWAP for a call or below VWAP for a put.
6. It ignores a setup if the 14-bar ATR is more than 2.5% of the underlying price.
7. It skips a stock that has suddenly turned jumpy: if the last 14 minutes' average range (ATR) is more than 2× that stock's own average one-minute range so far today (`max_atr_expansion`). Other stocks keep being watched, so a calmer one can still trade.
8. If several stocks qualify in the same minute and there are not enough open slots (two positions, three trades a day), it takes the ones with the best estimated reward-to-risk, and breaks ties in favour of the steadier trend (net move divided by total path over the last 30 minutes).

## Which option it may buy

For an upward signal, it looks for a call; for a downward signal, a put. The contract must expire in 7 to 21 days, have an absolute delta from 0.45 to 0.65, a bid/ask spread no wider than both $0.10 and 10% of its midpoint, open interest of at least 100, and today's volume of at least 10. Contracts that cost more than the per-trade budget are not eligible, so if the 0.56-delta contract is too expensive it can choose a cheaper one still inside 0.45–0.65. Among eligible contracts it prefers delta near 0.55 and a tighter spread. It can buy at most one contract per entry.

## Risk and exits

- The maximum planned loss budget is 10% of account equity per trade (the code refuses anything above 20%). A long option can lose its whole premium, so the budget must cover the full contract cost: about $1,000 on a $10,000 account, enough for one near-the-money SPY contract (~$789 on 2026-10-13). The 2% daily loss limit below still applies, so a position down about $200 on the day is closed by the emergency daily-risk exit. Sizing rejects a contract if its ask times 100, plus estimated entry and exit slippage and round-trip fees, exceeds the budget. It never adds contracts to use up the budget.
- The underlying price is an exit trigger, not a dollar-risk guarantee: the stop is half the opening range's height against the trade and the target is one full opening-range height in its favour (one and two one-minute ATRs are used instead if they are larger). It also exits at the daily loss limit. The 180-minute time exit and the forced exit at the session close are switched off (`execution.enable_time_exit` and `execution.flatten_at_session_close` are `false`): an open position is carried overnight without monitoring, and the next `paper-session` resumes managing it using the stop and target from the day it was entered. Set either flag back to `true` to restore that exit. Exits are simulated using the option's bid minus modeled slippage. Actual realized P/L is calculated from the option fill prices and fees and recorded beside planned and modeled risk.
- It stops taking entries after three trades in a day, two simultaneous open positions, three consecutive losing trades, a 2% daily loss, or a 5% weekly loss. The kill-switch file blocks new entries.
- It will not open another position in an underlying that already has an open position, so it does not average into that symbol.
- Normal paper fills assume buying at the option ask plus estimated slippage and selling at the bid minus estimated slippage. Commissions and exchange fees are recorded as estimates.
- In the normal paper session, a rough Greek-based estimate must also forecast at least 1R of net reward after spread, slippage, and fees, where R is the estimated loss if the stop is hit (not the whole option price). This estimate is only a gate; it is not evidence the trade will make that return.

## Today's temporary test

The separate `paper-test-session` changes the timing only in memory: it starts a new 15-minute range when the test is launched and allows entries until the close. It retains the signal, option liquidity checks, per-trade sizing limit, Greek-based expected-return gate, kill switch, and data/quote freshness checks. It uses a separate fake-$100,000 ledger and never sends an order to tastytrade. These temporary test rules do not replace this strategy or authorize live trading.
