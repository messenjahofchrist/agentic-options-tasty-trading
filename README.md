# Deterministic tastytrade options bot

This is a deterministic research and supervised local paper-trading prototype; the strategy decision logic does not use AI. The current candidate is an opening-range breakout with EMA trend, relative-volume and optional VWAP confirmation. It selects defined-risk long calls or puts with 7–21 DTE, target absolute delta 0.45–0.65 and liquidity/spread screens. Its rules and risk bounds live in [`strategy.txt`](strategy.txt); a plain-English copy is in [`docs/strategy_plain_english.md`](docs/strategy_plain_english.md).

**The strategy has not demonstrated profitability.** Local paper commands read Tastytrade market data and simulate fills; they do not submit broker orders. Backtests use underlying candles and do not establish options performance. A supervised paper run is available, but a full-session run, restart/reconnect recovery, and strategy validation remain outstanding. Paper results are not evidence to enable LIVE trading.

## Modes and safety

- Default mode is `PAPER`. `paper-session` runs a local minute-bar decision and simulated-position loop after being started before the market open. `paper-test-session` provides a separate supervised intraday test with a temporary rolling range and fake test capital. Neither submits broker orders.
- `BACKTEST` accepts local timestamped OHLCV CSV data. It is a no-lookahead, underlying-only signal replay; it does not model option Greeks, option quotes, assignment, or expiry.
- `LIVE` is blocked unless `TRADING_MODE=LIVE`, `ENABLE_LIVE_TRADING=true`, credentials/account are present, and `LIVE_START_CONFIRMATION="I UNDERSTAND LIVE TRADING USES REAL MONEY"`. The live REST primitives dry-run orders first, require explicit dry-run result and external identifier, and raise an uncertain-submission error. There is no complete production LIVE orchestration and no real order has been submitted.

The kill switch file `KILL_SWITCH` blocks new entries at the integration boundary. Removing it is a manual local action. It never flattens positions. Run without dashboard to keep local command tools available.

## Supervisory reviews and Windows tasks

`python -m trader.supervisor review` captures a PAPER-only snapshot and invokes Claude Code and Codex independently. Claude is run without tools/MCP in a temporary working directory; Codex runs with a read-only sandbox in a temporary directory. Broker, account-number, SMTP and API credential environment variables are removed before either CLI starts. The coordinator locks overlapping runs, validates structured output, retries each failed reviewer once, stores timestamped snapshots/reports under `data/reviews/`, records disagreements and explicitly marks partial reviews incomplete. Reviewers cannot place orders or change the application.

`python -m trader.supervisor daily-report` creates a daily local performance/risk report. `python -m trader.supervisor test-email` sends a fixed harmless test message to `REPORT_TO` after SMTP is configured. `python -m trader.supervisor watchdog` checks the PAPER engine heartbeat during market hours; stale/missing health writes `KILL_SWITCH` to block new entries and records an alert. It does not restart the engine or attempt to manage/close an open simulated position. Status is available with `python -m trader.supervisor status`.

Windows task definitions are in [`scripts/supervisor-tasks.ps1`](scripts/supervisor-tasks.ps1). Run `& .\scripts\supervisor-tasks.ps1 -Action Install` to register hourly reviews, weekday minute watchdog checks and a 17:15 local daily report. These tasks use the current Windows user with an interactive logon token: they persist in Task Scheduler, but do not run while that user is logged out. Review summaries and critical email alerts need STARTTLS SMTP values in local `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `REPORT_FROM`, `REPORT_TO`). They are absent until configured; the software will record that absence and will not claim an email was sent.

JSONL operational logs rotate at 10 MiB to timestamped `.archive` files; change `OPTIOBOT_LOG_MAX_BYTES` locally if needed. Existing records are retained.

The PAPER engine is deliberately not registered as an unattended task yet. It rejects mid-session starts and refuses to start when it finds existing simulated positions; reconnect/restart position management is not implemented or validated. A watchdog blocks new entries and alerts when possible, but cannot manage a simulated position after an engine crash. Running it unattended before that recovery path is verified would not meet the required safety behavior. No full-session PAPER run has been verified.

## Install and use

Python 3.12+ is required.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dashboard,streaming,test]'
Copy-Item .env.example .env
python -m trader.cli validate-strategy
python -m trader.cli init-paper
# Run this before the equity open; it stays active through the close.
python -m trader.cli paper-session --symbol SPY
python -m trader.cli backtest --symbol SPY --start 2025-01-01T00:00:00Z --end 2025-12-31T23:59:59Z --in-sample-end 2025-09-30T23:59:59Z
python -m trader.cli dashboard
```

The dashboard binds to localhost by default. For a backtest, the command retrieves one-minute underlying candles from Massive by default; no manual CSV download is needed. Add `MASSIVE_API_KEY` to the ignored local `.env` file. `--source tastytrade` selects the DXLink candle history path; that requires the existing Tastytrade OAuth credentials and market-data entitlement. A local OHLCV CSV remains an optional input. Massive Stocks Basic is free and includes minute aggregates, but its data is end-of-day, with at most 2 years of history and 5 API calls per minute; it supports backtests only, not live entry signals. The CLI honors Massive pagination limits. Never paste secrets into chat. `init-paper` creates `data/paper_account.json` using `PAPER_STARTING_CASH` (default USD 10,000); committed `paper_account.json` is a clean template only.

`paper-session` is a local simulator only: it never submits broker orders. Start it before the equity open so it can build the opening range from live trades; starting mid-session is blocked. It simulates entries at ask plus slippage, monitors exits at live bids, and persists the paper ledger. The end-to-end live session has not yet passed a full-session validation, and the current $10,000 paper account may be too small to size one SPY/QQQ contract under the active 1% risk cap. Do not enable LIVE. For LIVE, do not proceed until all integration and operational gates listed in `docs/operations.md` have actually passed; the explicit confirmation is a last local gate, not a substitute for validation.

For a supervised same-day connectivity check only, `python -m trader.cli paper-test-session --symbol SPY` uses a separate fake-$100,000 ledger, starts a new rolling 15-minute range, and extends the test entry window to 15 minutes before the close. It retains all strategy and economic gates and does not submit broker orders. See `docs/operations.md` for its limits.

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

`active_strategy.json` carries the canonical configuration hash and complete `research_test` snapshot from `strategy.txt`; validation fails on any mismatch. The current owner-directed hardening remains PAPER-only and does not authorize LIVE. Future strategy changes still require the established governance review and explicit owner approval. AI agents may not independently activate a strategy. The advisory coordinator is not a strategy-governance approval system.

## Implementation limits

The repository was an empty initial commit. This implementation is a safety-focused starting point, not a production-ready trading system. In particular, there is no end-to-end broker-stream-to-portfolio reconciliation cycle, automated order-protection verification, remote backtest integration, historical option replay, MFE/MAE ledger lifecycle, breadth feed, data-freshness monitor wiring, pending proposal review UI, or completed failure-injection validation. API endpoint behavior has been researched in official docs but not verified with credentials. No profitability claim is made.
