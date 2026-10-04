# Deterministic tastytrade options bot

This is an early, deterministic research and paper-trading baseline. There is no AI dependency in its decision logic. The current strategy is an opening-range breakout candidate with EMA trend, relative-volume and optional VWAP confirmation. It buys defined-risk calls or puts with 7–21 DTE, target absolute delta 0.45–0.65 and liquidity/spread screens. Its rules and risk bounds live in [`strategy.txt`](strategy.txt).

**The strategy has not demonstrated profitability.** No market data, account credentials, or historical options data were supplied. The local replay is an underlying-price proxy and must not be treated as options performance or evidence to enable PAPER trading unattended. The integrated decision cycle currently logs candidates but deliberately does not submit entries; complete economic gating, account integration and end-to-end paper position lifecycle remain outstanding.

## Modes and safety

- Default mode is `PAPER`. It has local account storage and conservative bid/ask fills, but the complete autonomous PAPER data-to-position loop is not operational yet.
- `BACKTEST` accepts local timestamped OHLCV CSV data. It is a no-lookahead, underlying-only signal replay; it does not model option Greeks, option quotes, assignment, or expiry.
- `LIVE` is blocked unless `TRADING_MODE=LIVE`, `ENABLE_LIVE_TRADING=true`, credentials/account are present, and `LIVE_START_CONFIRMATION="I UNDERSTAND LIVE TRADING USES REAL MONEY"`. The live REST primitives dry-run orders first, require explicit dry-run result and external identifier, and raise an uncertain-submission error. There is no complete production LIVE orchestration and no real order has been submitted.

The kill switch file `KILL_SWITCH` blocks new entries at the integration boundary. Removing it is a manual local action. It never flattens positions. Run without dashboard to keep local command tools available.

## Install and use

Python 3.12+ is required.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dashboard,streaming,test]'
Copy-Item .env.example .env
python -m trader.cli validate-strategy
python -m trader.cli init-paper
python -m trader.cli backtest .\data\bars.csv --in-sample-end 2024-12-31T23:59:59Z
python -m trader.cli dashboard
```

The dashboard binds to localhost by default. Backtest CSV columns are `timestamp,open,high,low,close,volume` and optional `vwap`; timestamps must include a timezone. No example market prices are fabricated. `init-paper` creates `data/paper_account.json` using `PAPER_STARTING_CASH` (default USD 10,000); committed `paper_account.json` is a clean template only.

There is currently no PAPER start command because the complete market-data-to-execution loop has not been verified. Do not enable LIVE. For LIVE, do not proceed until all integration and operational gates listed in `docs/operations.md` have actually passed; the explicit confirmation is a last local gate, not a substitute for validation.

## Tastytrade API research

Checked against the current official documentation on 2026-10-04:

- OAuth is mandatory; access tokens last 15 minutes. All REST calls need `User-Agent: product/version`. Production REST is `https://api.tastyworks.com`; sandbox credentials and data are separate. Legacy sessions are decommissioned. [OAuth2](https://developer.tastytrade.com/oauth/), [Quickstart](https://developer.tastytrade.com/docs/get-started/)
- Production market quotes are real-time and account eligibility gated. REST supports up to 100 combined symbols; DXLink is preferred for continuous quotes. Option chains are under `/option-chains/{symbol}`. [Market Data](https://developer.tastytrade.com/docs/concepts/market-data/), [Instruments reference](https://developer.tastytrade.com/reference/instruments/)
- DXLink requires quote-token acquisition then SETUP/AUTH/channel/feed setup/subscription and regular keepalive; use instrument `streamer-symbol`. This repository has a minimal COMPACT feed client, but it has not been authenticated or field-validated against an account. [Streaming](https://developer.tastytrade.com/docs/concepts/streaming/)
- Orders use REST. Dry-run returns warnings, buying-power effect and fee estimate. Use a unique `external-identifier`. The server does not deduplicate retries; on timeout or 5xx reconcile order state before retry. [Orders](https://developer.tastytrade.com/docs/concepts/orders-and-order-types/), [Idempotency and retries](https://developer.tastytrade.com/docs/guides/idempotency-and-retries/)
- The sandbox does not serve `/market-data` or `/market-metrics` (HTTP 502). Market orders are synthetically filled at $1 and limits below $3 fill immediately while limits at/above $3 remain live. It is for API/order-flow testing only. [Sandbox](https://developer.tastytrade.com/docs/sandbox/)
- The official market-metrics snapshot endpoint includes IV rank/percentile, liquidity rating and per-expiration IV; the client wrapper exposes this endpoint but does not yet normalize it into a full candidate score. [Market Metrics reference](https://developer.tastytrade.com/reference/market-metrics/getMarketMetricsIndex/)
- Official backtesting is a separate service at `https://backtester.vast.tastyworks.com`, with no separate cert environment. The remote service has not been integrated or called. [Backtesting guide](https://developer.tastytrade.com/docs/guides/backtesting/)

## Strategy governance

`active_strategy.json` carries the exact SHA-256 of `strategy.txt`; validation fails on mismatch. Only an explicit owner instruction `Implement new strategy <filename>` can authorize deployment. AI agents may create proposal files but cannot alter or activate the active strategy. Claude and Codex must independently review proposals; disagreement or missing evidence blocks owner approval. Lease helpers are in `trader/watchdog.py`; reviewers must acquire the lease before touching shared mutable review files.

## Implementation limits

The repository was an empty initial commit. This implementation is a safety-focused starting point, not a production-ready trading system. In particular, there is no end-to-end broker-stream-to-portfolio reconciliation cycle, automated order-protection verification, remote backtest integration, historical option replay, MFE/MAE ledger lifecycle, breadth feed, data-freshness monitor wiring, pending proposal review UI, or completed failure-injection validation. API endpoint behavior has been researched in official docs but not verified with credentials. No profitability claim is made.
