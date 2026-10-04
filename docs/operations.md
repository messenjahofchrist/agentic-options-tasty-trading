# Operations and readiness gates

## Current strategy candidate

Version 0.1.0 uses a 15-minute regular-session opening range. A signal requires a close crossing the range with 9/21 EMA trend agreement, at least 1.5 relative volume and price on the matching side of VWAP when supplied. The candidate buys a liquid 7–21 DTE option with 0.45–0.65 absolute delta and spread no wider than 10% of midpoint. Initial invalidation is 1 ATR; nominal target is 2R; time exit is three hours. Entries stop after 14:30 ET. Position risk is 1% of equity, hard cap 2%; daily stop 2%, weekly stop 5%, three consecutive losses, three trades/day and two open positions.

These thresholds are test hypotheses only. Opening-range breakout, trend, volume and VWAP filters can all overfit. Compare each component with ablations and out-of-sample/walk-forward validation, include spreads, fees and slippage, and segment by ticker, time and volatility regime. Do not interpret the included proxy replay as options expectancy. The official tastytrade backtester supports historical option strategy simulation but is not yet wired into this application.

## Research notes (not a strategy validation)

- A peer-reviewed ORB study reports results for U.S. crude-oil futures. Its instrument and implementation differ from SPY/QQQ options, so it motivates a test and does not establish this candidate's edge. [Marshall, Young & Rose, *Assessing the profitability of intraday opening range breakout strategies* (2013)](https://doi.org/10.1016/j.frl.2012.09.001).
- An S&P 500 ETF study documents a relation between the first half-hour return and the final half-hour return in 1993–2013, stronger on volatile/high-volume days. That is not evidence that a 15-minute ORB or a same-minute entry rule earns net returns. [Gao, Han, Li & Zhou, *Market intraday momentum* (2018)](https://doi.org/10.1016/j.jfineco.2018.05.009).
- Option execution costs matter: a recent Review of Financial Studies paper documents quoted options spreads and analyzes routing/execution cost differences. This supports bid/ask-aware replay, not a universal spread cutoff. [*Option Auctions*, Review of Financial Studies](https://academic.oup.com/rfs/article/39/3/783/8193725).
- The 7–21 DTE range, delta range, VWAP, EMA, relative-volume threshold and ATR controls are engineering hypotheses; the sources above do not establish their profitability. Implied-volatility/term-structure and liquidity data are available via tastytrade's market-metrics snapshot endpoint, but historical values must be sourced and tested before conditioning on them.

## API facts to verify again before connecting

OAuth refresh-token grants mint 15-minute access tokens; sandbox and production grants/base URLs are separate. Quote REST is for one-off snapshots, DXLink for streaming. Option chains and account positions/balances are REST endpoints. Orders must be dry-run first. The API does not make retries idempotent, even with an external identifier; query orders before retrying ambiguous submissions. Fees in the REST dry-run should be inspected; local fees are only estimates. Sandbox fills are synthetic and cannot validate profitability.

Official sources: [OAuth](https://developer.tastytrade.com/oauth/), [streaming](https://developer.tastytrade.com/docs/concepts/streaming/), [market data](https://developer.tastytrade.com/docs/concepts/market-data/), [orders](https://developer.tastytrade.com/docs/concepts/orders-and-order-types/), [sandbox](https://developer.tastytrade.com/docs/sandbox/), [backtesting](https://developer.tastytrade.com/docs/guides/backtesting/).

## Required before autonomous PAPER operation

1. Supply production OAuth credentials in local secret storage; verify account-market-data entitlement, quote freshness, DXLink event layouts, full option-chain/Greek normalization, reconnection and API rate behavior.
2. Complete and verify the deterministic cycle: calendar, data-age gate, per-symbol and per-contract logs, economic gate after spread/slippage/fees, paper buy/sell lifecycle, realistic partial/cancel simulation if used, protective exit behavior and daily/weekly loss accounting.
3. Reconcile the local account and simulated fills after restarts and test stale feed, authentication failure, broker uncertainty, duplicate IDs, and process termination.
4. Obtain locally sourced historical options data; compare candidate variants, OOS and walk-forward results, all transaction costs and useful sample sizes. Review result independently before considering PAPER deployment.

Until this list passes, PAPER must be supervised and no unattended startup command is provided.

## Required before LIVE

LIVE is unavailable as an operational mode until independent end-to-end validation verifies production OAuth, market feed, order schema, dry-run warnings/fees/buying power, external identifiers, ambiguous retry reconciliation, account state, protective order behavior, kill switch, restart recovery, and risk blocks. Then update only local ignored configuration, obtain the owner's explicit LIVE authorization, and type the startup phrase configured by `AppConfig`. No such validation or authorization has happened. Do not use sandbox P/L as performance evidence.
