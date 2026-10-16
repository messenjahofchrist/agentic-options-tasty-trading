# Operations and readiness gates

## Current strategy candidate

Version 0.1.1 uses a 15-minute regular-session opening range. A signal requires a close crossing the range with 9/21 EMA trend agreement, at least 1.5 relative volume and price on the matching side of VWAP when supplied. The candidate buys a liquid 7–21 DTE option with 0.45–0.65 absolute delta, spread no wider than both $0.10 and 10% of midpoint, open interest of at least 100 and option volume of at least 10. Initial invalidation is a 1-ATR underlying-price trigger; the nominal target is 2 ATR. Neither defines or guarantees option-dollar loss. Maximum holding time is three hours. PAPER entries may be evaluated from 09:45 ET until the actual session close (normally 16:00 ET); the calendar blocks entries at or after close and honors early closes. A late entry may be exited almost immediately under the existing session-close rule. Maximum modeled full-premium risk, including round-trip slippage and fees, is 1% of equity; the separate hard ceiling is 2%. Daily stop 2%, weekly stop 5%, three consecutive losses, three trades/day and two open positions.

These thresholds are test hypotheses only. Opening-range breakout, trend, volume and VWAP filters can all overfit. Compare each component with ablations and out-of-sample/walk-forward validation, include spreads, fees and slippage, and segment by ticker, time and volatility regime. Do not interpret the included proxy replay as options expectancy. The official tastytrade backtester supports historical option strategy simulation but is not yet wired into this application.

## Research notes (not a strategy validation)

- A peer-reviewed ORB study reports results for U.S. crude-oil futures. Its instrument and implementation differ from SPY/QQQ options, so it motivates a test and does not establish this candidate's edge. [Marshall, Young & Rose, *Assessing the profitability of intraday opening range breakout strategies* (2013)](https://doi.org/10.1016/j.frl.2012.09.001).
- An S&P 500 ETF study documents a relation between the first half-hour return and the final half-hour return in 1993–2013, stronger on volatile/high-volume days. That is not evidence that a 15-minute ORB or a same-minute entry rule earns net returns. [Gao, Han, Li & Zhou, *Market intraday momentum* (2018)](https://doi.org/10.1016/j.jfineco.2018.05.009).
- Option execution costs matter: a recent Review of Financial Studies paper documents quoted options spreads and analyzes routing/execution cost differences. This supports bid/ask-aware replay, not a universal spread cutoff. [*Option Auctions*, Review of Financial Studies](https://academic.oup.com/rfs/article/39/3/783/8193725).
- The 7–21 DTE range, delta range, VWAP, EMA, relative-volume threshold and ATR controls are engineering hypotheses; the sources above do not establish their profitability. Implied-volatility/term-structure and liquidity data are available via tastytrade's market-metrics snapshot endpoint, but historical values must be sourced and tested before conditioning on them.

## API facts to verify again before connecting

OAuth refresh-token grants mint 15-minute access tokens; sandbox and production grants/base URLs are separate. Quote REST is for one-off snapshots, DXLink for streaming. Option chains and account positions/balances are REST endpoints. Orders must be dry-run first. The API does not make retries idempotent, even with an external identifier; query orders before retrying ambiguous submissions. Fees in the REST dry-run should be inspected; local fees are only estimates. Sandbox fills are synthetic and cannot validate profitability.

Official sources: [OAuth](https://developer.tastytrade.com/oauth/), [streaming](https://developer.tastytrade.com/docs/concepts/streaming/), [market data](https://developer.tastytrade.com/docs/concepts/market-data/), [orders](https://developer.tastytrade.com/docs/concepts/orders-and-order-types/), [sandbox](https://developer.tastytrade.com/docs/sandbox/), [backtesting](https://developer.tastytrade.com/docs/guides/backtesting/).

Historical candles for `paper-session` use the explicitly configured `PAPER_HISTORY_PROVIDER` (`tastytrade_dxlink` by default, or `massive`). Both paths normalize one-minute OHLCV into a common record and fail closed on malformed, duplicate, out-of-order, or incomplete regular-session candles. Provider switching is never automatic. Massive's documented individual plans are Basic (free, end-of-day recency, 2 years, 5 requests/minute), Starter ($29/month, 15-minute delayed, 5 years), Developer ($79/month, 15-minute delayed, 10 years), and Advanced ($199/month, real-time, all history). The existing Massive account returned HTTP 403 `NOT_AUTHORIZED` for same-day intraday data; the plan required for a chosen operating workflow has not been purchased or verified. Tastytrade's public API documents Candle history through DXLink and real-time quotes, but does not document a REST OHLCV-history endpoint. On 2026-10-12 the app received 2 unique SPY candles for a 173-candle request and no snapshot completion marker; the cause is unresolved. Do not mark the Tasty history path healthy based on mock tests.

## Required before autonomous PAPER operation

The `paper-session` command runs local simulated fills from the DXLink equity Trade stream and uses live option quote/Greek snapshots. A delayed start requires a complete, contiguous session recovery, a verified opening range and indicator warm-up, then a gap-free handoff to live data. It does not place broker orders. The configured $10,000 paper capital is likely below the amount needed to open one SPY/QQQ contract while honoring the active 1% maximum-risk rule; the risk gate intentionally returns zero contracts when premium at risk exceeds that budget.

For a supervised same-day connectivity/execution exercise, `paper-test-session --symbol SPY` starts a separate, local-only ledger (`data/paper_test_account.json`) with clearly fake $100,000 test capital. It forms a fresh 15-minute range from the launch time and allows entries through 15 minutes before the close. It retains the Greek-based expected-return gate, kill switch, quote/data freshness, contract liquidity checks, and 1% per-trade premium-risk sizing. Decisions go to `data/paper_test_decisions.jsonl`; it never submits broker orders and does not change `strategy.txt`, `active_strategy.json`, or the regular $10,000 paper ledger. A fill depends on the strategy signal; the test does not force one. This is only a connectivity and simulated-execution check, not evidence of profitability or authorization for live trading.

1. Supply production OAuth credentials in local secret storage; verify account-market-data entitlement, quote freshness, DXLink event layouts, full option-chain/Greek normalization, reconnection and API rate behavior.
2. Complete and verify the deterministic cycle: calendar, data-age gate, per-symbol and per-contract logs, economic gate after spread/slippage/fees, paper buy/sell lifecycle, realistic partial/cancel simulation if used, protective exit behavior and daily/weekly loss accounting.
3. Reconcile the local account and simulated fills after restarts and test stale feed, authentication failure, broker uncertainty, duplicate IDs, and process termination.
4. Obtain locally sourced historical options data; compare candidate variants, OOS and walk-forward results, all transaction costs and useful sample sizes. Review result independently before considering PAPER deployment.

Until this list passes, PAPER must be supervised. The heartbeat watchdog and independent reviewer tasks are advisory controls and do not make the engine safe to run unattended. The PAPER runner does not recover and continue managing positions after a crash/restart, so the installer intentionally does not register an engine task. For a manually started `paper-session`, stop it with Ctrl+C in its PowerShell window. If it fails while a simulated position is open, inspect `data/paper_account.json` and `data/critical_alerts.jsonl`, keep new entries blocked, and reconcile/close that paper position before restarting. There is no safe automatic restart command yet.

## Supervisory workflow (advisory only)

The supported commands are:

```powershell
.\.venv\Scripts\python.exe -m trader.supervisor status
.\.venv\Scripts\python.exe -m trader.supervisor review
.\.venv\Scripts\python.exe -m trader.supervisor daily-report
.\.venv\Scripts\python.exe -m trader.supervisor test-email
.\.venv\Scripts\python.exe -m trader.supervisor watchdog
& .\scripts\supervisor-tasks.ps1 -Action Status
```

The review coordinator sends the same sanitized PAPER snapshot separately to both CLIs. The prompt contains only the strategy manifest, simulated account/positions/ledger, recent decision records and local health evidence. The Claude process has no tools, no configured MCP servers and no session persistence. The Codex process uses a read-only sandbox and ephemeral session in a temporary working directory. Only the user-profile paths needed to locate CLI auth are inherited; environment variables resembling broker/API/mail credentials are removed. Reviewer output is advisory and never applied.

To register hourly reviews, weekday one-minute watchdog checks, and the daily 17:15 local report:

```powershell
& .\scripts\supervisor-tasks.ps1 -Action Install
& .\scripts\supervisor-tasks.ps1 -Action Status
& .\scripts\supervisor-tasks.ps1 -Action Disable
& .\scripts\supervisor-tasks.ps1 -Action Enable
```

The initial review interval is one hour. After seven days, the owner can explicitly run `& .\scripts\supervisor-tasks.ps1 -Action Install -ReviewIntervalHours 2` to request a two-hour interval. The script refuses that change unless the full week is evidenced by complete two-reviewer reviews, delivered email summaries, no recorded supervisor error, and healthy engine heartbeats. The cadence never changes automatically.

Task Scheduler uses an interactive current-user token. Jobs run outside the interactive Codex window while that Windows user is logged in. They do not run while logged out; Task Scheduler credentials/service-account configuration is still required to satisfy that condition. The script does not create an engine task. To permanently remove the supervisory tasks, use `& .\scripts\supervisor-tasks.ps1 -Action Remove`.

Email requires all six local `.env` settings: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `REPORT_FROM`, and `REPORT_TO`. Configure a STARTTLS-enabled SMTP account and a destination you control, then run `python -m trader.supervisor test-email` for the fixed harmless message. The checked-in example intentionally has empty fields. Without them, reports stay on disk and status says email is not configured; delivery has not been tested. Do not put SMTP credentials in source or reviewer snapshots.

The watchdog runs only during the exchange session. The runner records `engine_expectation.json` and lifecycle state (`STARTING`, `RECOVERING`, `RUNNING`, `KILL_SWITCH_ACTIVE`, `FAILED`, or `STOPPED`) separately from the heartbeat timestamp. An intentionally stopped engine (`expected_running=false`) is reported as not expected and is not classified as crashed. A bounded start/recovery phase is allowed; stale health while a run is expected writes `KILL_SWITCH`, records `data/critical_alerts.jsonl`, and makes a best-effort email alert if SMTP is configured. A healthy heartbeat never clears the kill switch. The watchdog does not restart or liquidate anything. The Watchdog Task must remain paused until the owner separately authorizes reactivation.

### Paper history provider and recovery

`PAPER_HISTORY_PROVIDER=tastytrade_dxlink` remains the default and current configured source. `PAPER_HISTORY_PROVIDER=massive` can be selected only after a suitable Massive plan/key is authorized and verified locally. Massive is only selected for history; live equity trades and options quotes continue to come from Tastytrade. This makes provider provenance explicit but does not make delayed data current: if the selected provider cannot cover the requested end time, startup blocks. No provider failover is performed. On each successful validated recovery, `data/history_recovery_state.json` records provider, session, symbol and last validated UTC timestamp; restarting still rebuilds and validates the required session history instead of trusting this checkpoint as proof.

The readiness report is written under `data/recovery_reports/`. It must show complete scheduled session candles, opening-range metrics, EMA/ATR warm-up and a verified first-live-bar transition before the PAPER loop can evaluate entries. An active kill switch remains active across startup; startup and a fresh heartbeat do not authorize or unblock entries. Do not start PAPER next session until the Tasty history failure is resolved or an authorized history source passes the same current-account checks, and an owner-approved kill-switch recovery procedure is completed.

### Manual kill-switch recovery

The bot never clears `KILL_SWITCH`. An owner may consider clearing it only after all of these checks pass: (1) stop PaperEngine and confirm no engine process remains; (2) confirm PAPER mode and `ENABLE_LIVE_TRADING=false`; (3) reconcile the local paper ledger and verify there are no open simulated positions; (4) resolve the exact alert reason and preserve its logs; (5) verify the active strategy manifest/hash; (6) obtain a successful same-session recovery report with complete candles, opening range, indicator warm-up, and historical/live transition; and (7) explicitly approve clearing the switch. Only then, from the repository directory, manually run `Remove-Item -LiteralPath .\KILL_SWITCH`, verify the file is absent, and start the already-configured PAPER task. If any check fails, leave the switch in place. This procedure has not been performed for the currently active switch.

Task state and recent outcomes:

- Review outputs: `data/reviews/`.
- Daily records: `data/reviews/daily-*.json`.
- Watchdog events: `data/watchdog.jsonl` and `data/critical_alerts.jsonl`.
- Current reviewer/scheduler status: `data/supervisor_status.json` and `data/supervisor_schedule.json`.
- JSONL logs rotate to timestamped `.archive` files at 10 MiB by default (`OPTIOBOT_LOG_MAX_BYTES`); older records are preserved.

The supervisor lock prevents overlapping reviews and removes only locks whose recorded process is no longer running. A reviewer gets one bounded retry. A failed reviewer makes the consolidated review incomplete. A failed email is recorded locally; it is not treated as successful delivery.

## Crash alerts (phone)

Added 2026-10-16 after a one-minute PDD feed gap stopped the small-account session at 13:45 CT and left three
paper positions unmanaged through the close.

- **Feed gaps no longer stop the session.**
  - The runner retries a missed minute's backfill 3 times, at 5 s and then 10 s.
  - If it still fails on a symbol that holds a PAPER position, that position is sold at a fresh bid (`data_failure_protective_exit`) and only that symbol is switched off.
  - Without a fresh quote, the position stays open and the session fails closed, as before.
  - LIVE behaviour is unchanged.
- **The watchdog checks every engine.** It covers the original one and each `profiles/*.json` profile.
  - Profile engines never write or clear the shared `KILL_SWITCH`.
  - A failed or stale engine sends one email and one phone push per failure. The push is `urgent` when positions are open.
- **Phone push (ntfy).**
  - Set `NTFY_URL=https://ntfy.sh/<long random topic>` in `.env` and subscribe to that topic in the ntfy app.
  - Pushes go out for engine down, protective exit and positions unresolved at the close. Other alerts stay email-only.
- **Dead-man's switch.**
  - Set `HEALTHCHECK_URL` to an external check, for example healthchecks.io.
  - Each watchdog run pings it, or pings `/fail` when an engine is critical.
  - Give the check a schedule that matches the Watchdog task (weekdays 08:25–15:25 CT, every minute) and a few minutes' grace. The service then alerts you when the pings stop, which is the one failure no local monitor can report: the PC asleep, offline or off.

## Required before LIVE

The LIVE workflow (`live-check`, `authorize-live`, `live-session`), its readiness reports and the open blockers are documented in [`live_trading.md`](live_trading.md).

The isolated $1,000 small-account PAPER profile (`paper-session --account-profile small`, `profile-status`) is documented in [`profiles/small_account_profile.md`](profiles/small_account_profile.md). It runs as a separate process; the watchdog and scheduled task still cover only the original engine.

LIVE is unavailable as an operational mode until independent end-to-end validation verifies production OAuth, market feed, order schema, dry-run warnings/fees/buying power, external identifiers, ambiguous retry reconciliation, account state, protective order behavior, kill switch, restart recovery, and risk blocks. Then update only local ignored configuration, obtain the owner's explicit LIVE authorization, and type the startup phrase configured by `AppConfig`. No such validation or authorization has happened. Do not use sandbox P/L as performance evidence.
