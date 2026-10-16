# LIVE trading on tastytrade

Status at 2026-10-16 01:15 UTC (after the 2026-10-15 close): **the LIVE pipeline is built and tested against fakes. It is BLOCKED from trading** (see [Remaining blockers](#11-remaining-blockers)). No order, dry-run order, or cancel has ever been sent to the broker.

LIVE runs the **same strategy loop** as PAPER (`trader/paper_runner._run`). The strategy files are unchanged. Only the execution step differs: PAPER calls `PaperExecutor.fill`, and LIVE calls `LiveTrader` (`trader/live_execution.py`), which sends real orders.

## 1. What was built

| Piece | File | Purpose |
|---|---|---|
| `LiveTrader` | `trader/live_execution.py` | Verifies the account, keeps a LIVE ledger and order journal, runs the order lifecycle, reconciles with the broker, applies execution safety gates and recovers after a restart |
| LIVE session | `trader/live_session.py` | Owner authorization, read-only readiness check (`live-check`), per-start confirmation, supervised run with restart after connection failures |
| Loop seam | `trader/paper_runner.py` | `_run(..., live=None)`. When `live` is set, entries and exits go to `LiveTrader` instead of the paper fill model. Every other line runs identically |
| REST client | `trader/tastytrade_client.py` | Added `trading_status`, `live_orders`, `order`, `cancel_order`. The external-identifier lookup now also searches `/orders/live` |
| CLI | `trader/cli.py` | `live-session`, `live-check`, `authorize-live` |
| Tests | `tests/test_live.py` | 29 LIVE tests. The broker is an in-memory fake, so no test can reach tastytrade |

## 2. Tastytrade connectivity report (live, read-only, 2026-10-16 01:12 UTC)

| Check | Result |
|---|---|
| OAuth refresh-token grant | PASS. 15-minute access tokens renew automatically (tested) |
| Accounts visible on this login | 2 |
| Configured account `****6633` | Found. Owner authority. Individual **Cash** account |
| Other account `****1443` (margin) | Present, never used. No fallback exists |
| Trading status | Open. Not frozen, not closing-only, no margin call, not PDT |
| Options level | `Covered And Cash Secured`. Permits buying calls and puts to open |
| Net liquidating value | **$50.00** |
| Derivative buying power | $50.00 |
| Positions / working orders | 0 / 0 |
| Clock offset vs tastytrade | +0.84 s (limit 2.0 s) |
| Quote token | level `api`, valid 24 h |

## 3. DXLink historical and live-data report

- **History:** for the 2026-10-15 session, all 14 universe symbols returned **390 of 390** one-minute candles each, with no gaps, duplicates or out-of-order bars. Each took about 0.85 s.
- **The "2 of 173 candles" issue (2026-10-12):**
  - *Cause:* the code then opened the DXLink channel with `contract: AUTO`. That delivery mode does not replay a time series, so only a couple of events arrived and the snapshot never completed.
  - *Fix:* commit `8a65e53` changed the request to `contract: HISTORY` and accepted both `{=1m,…}` and `{=m,…}` symbol forms.
  - *Evidence:* the 2026-10-13 diagnostic run got 390/390. Production startups returned 260/260 on 2026-10-14 and 16/16 on 2026-10-15, and tonight returned 14 × 390.
  - *Safety:* incomplete history still blocks the symbol. Nothing is forward-filled or substituted.
- **Residual history risk:** on 2026-10-14 one PLTR startup failed with "DXLink candle snapshot is out of order". A live update for the current minute arrived inside the snapshot. The code failed closed and disabled that symbol for the day. That is the correct, safe behavior, but the symbol was lost for the day.
- **Live events:** DXLink authenticated and delivered `Quote` (SPY and an option), `Greeks` (option) and `Trade` (SPY last-trade snapshot) events. The market was closed, so a **continuous live Trade stream during regular hours has not been verified for LIVE**. PAPER sessions on 2026-10-14 and 2026-10-15 did run on it.
- **Shared pipeline features (unchanged from PAPER):** Trade→1-minute bars, the history-to-live join check, gap recovery from history, duplicate-bar detection, reconnect (up to 8 times, then the session restarts), background keepalive, and quote freshness (underlying ≤ 20 s, options ≤ 30 s).

## 4. Options market-data report

- Option chains and REST quotes were retrieved for **14/14** symbols, up to 100 near-the-money contracts each.
- Every quoted contract carried delta, gamma and theta.
- Example (SPY, 7–21 DTE, delta 0.45–0.65): `SPY 261022P00772000` bid 4.36 / ask 4.39, delta −0.56, OI 571.
- Cheapest strategy-eligible contracts by modeled risk (premium + slippage + fees), at the after-hours quotes:

| Underlying | Call | Put |
|---|---|---|
| TLT | $98.44 | $90.44 |
| UBER | — | $128.44 |
| NFLX | — | $294.44 |
| SPY | $726.44 | $480.44 |

- **No eligible contract fits the strategy's $5.00 budget** (10% × $50).

## 5. Account reconciliation report

- Both the broker and the LIVE ledger hold no options on strategy underlyings: **reconciled**.
- No working orders.

The reconciliation rules in LIVE:

- **Each bar:** broker balances, positions and working orders are refreshed (at most every 30 s) and compared with the LIVE ledger.
- **Mismatch:** any mismatch on a strategy underlying blocks new entries and raises a critical alert.
- **Outside orders:** a working order on a strategy underlying that this bot did not place also blocks entries.
- **Other positions:** positions outside the strategy universe are reported but don't block.
- **Stale broker state:** broker state older than 90 s blocks entries.

## 6. Order execution and test report

**How orders work in LIVE:**

- **Entry:** a Day limit order to buy to open at the ask the strategy evaluated, for the strategy's quantity.
- **Exit:** a Day limit order to sell to close at the current bid.
- **Every order:**
  - It is written to `data/live/live_open_intents.json` and `live_orders.jsonl`, with a unique `external-identifier`, before any network call.
  - It is dry-run first; any error or warning blocks it.
  - It is polled until it reaches a terminal status.
  - Fills are taken only from the broker's order record.
- **Unfilled entry after 20 s:** cancel, then poll until the broker reports `Cancelled`.
- **Unfilled exit after 10 s:** cancel, confirm, then re-price at the new bid (up to 6 attempts). If it still hasn't filled, the position stays tracked, entries are blocked and an alert is raised.
- **Timeout or 5xx on submit:** look up the external-identifier and **never resubmit**. If the order isn't found, entries stay blocked until a re-check a minute later confirms it is absent.
- **Partial fill:** book the filled quantity, cancel and confirm the remainder.
- **Restart:** every unresolved intent is resolved against the broker. Fills that happened while the bot was down are booked, and still-working orders are cancelled and confirmed before any entry.

Results: `pytest` **98 passed** (69 existing unchanged + 29 new). Tests by scenario:

| Scenario | Test(s) |
|---|---|
| Successful authentication / expired token renewal | `test_expired_access_token_is_renewed_with_the_refresh_token` |
| Invalid credentials | `test_readiness_reports_invalid_credentials_without_raising` |
| Wrong/missing account, no fallback | `test_startup_uses_only_the_configured_account_and_never_falls_back` |
| Frozen account / no options permission | `test_startup_blocks_frozen_account_and_missing_options_permission` |
| DXLink disconnection & reconnect | existing `test_dxlink_keepalive_continues_while_consumer_is_busy`, `test_live_bars_drop_the_minute_in_progress_at_connect`, `test_only_connection_failures_restart_the_paper_engine` |
| Historical-data failure / missing candles | existing `test_dxlink_history_timeout_is_explicit_and_audited`, `test_late_paper_start_requires_complete_contiguous_history`; new `test_live_with_missing_history_candles_submits_nothing` |
| Stale option quote, invalid contract, wide spread, not-ready indicators, quantity limit, insufficient buying power, kill switch | `test_execution_safety_rejects_stale_invalid_wide_unaffordable_and_killed` |
| Cash-account settlement: settled cash short, same-day proceeds not reused until T+1, missing settled balance blocks, margin exempt | `test_cash_account_entry_needs_settled_cash_not_just_buying_power`, `test_same_day_sale_proceeds_cannot_fund_a_new_entry_until_settled`, `test_missing_settled_cash_blocks_cash_account_and_margin_account_is_exempt` |
| Dry-run warning | `test_dry_run_warning_blocks_submission` |
| Rejected order | `test_rejected_order_books_nothing` |
| Partial fill | `test_partial_fill_books_filled_quantity_and_cancels_the_rest` |
| Unfilled / cancel confirmed / cancel unconfirmed | `test_unfilled_entry_is_cancelled_and_confirmed_before_returning`, `test_unconfirmed_cancellation_blocks_new_entries` |
| Submission timeout (accepted / lost) | `test_timeout_after_broker_accepted_is_resolved_by_lookup_not_resubmission`, `test_timeout_with_order_not_found_is_not_resubmitted_and_blocks_entries` |
| Duplicate-order prevention | `test_unconfirmed_cancellation_blocks_new_entries`, `test_exit_never_stacks_on_an_unresolved_exit_order`, plus the shared-loop test (one order per strategy entry) |
| Exits / re-price after confirmed cancel | `test_exit_sells_at_bid_and_realizes_actual_fill_pnl`, `test_unfilled_exit_reprices_only_after_confirmed_cancel` |
| Restart recovery | `test_restart_books_fills_of_orders_left_open_by_a_crash`, `test_restart_cancels_and_confirms_a_working_order_left_by_a_crash` |
| Position reconciliation | `test_reconciliation_mismatch_or_foreign_order_blocks_entries` |
| Kill switch in the strategy loop | `test_live_kill_switch_blocks_strategy_entries` |
| LIVE gates (mode, flag, authorization, confirmation) | `test_live_session_refuses_paper_mode_missing_authorization_and_bad_confirmation`, `test_confirmation_requires_account_suffix`, `test_authorization_binds_hash_and_account_without_touching_strategy_files` |
| **Same decisions to PAPER and LIVE; PAPER/LIVE separation** | `test_identical_market_data_produces_identical_decisions_and_live_orders_match_paper_entries`: the same bars and quotes run through the loop twice. Decisions match field for field, and LIVE submits exactly the contracts, quantity and price PAPER filled. PAPER never touches a broker, and each mode writes only its own files |

## 7. PAPER versus LIVE architecture

```
                     ONE strategy loop: paper_runner._run (unchanged decision path)
 DXLink Trade ─► 1-min bars ─► history join/validation ─► evaluate_underlying ─► decision_cycle
 REST quotes/chains/Greeks ─────────────────────────────────────────────────┘   (signal, option
                                                                                 selection, sizing,
                                                                                 economic gate,
                                                                                 exit_reason, ranking)
                                         │ entry/exit decision (same object in both modes)
                    ┌────────────────────┴─────────────────────┐
              live is None                                live = LiveTrader
           PaperExecutor.fill                     execution safety gates (independent)
   data/paper_account.json, decisions.jsonl       dry-run ─► submit ─► poll ─► cancel-confirm
   (no broker orders)                             fills from broker order record
                                                  data/live/live_account.json, live_orders.jsonl,
                                                  live_open_intents.json, decisions.jsonl,
                                                  engine_heartbeat.json, recovery_reports/
                                                  broker reconciliation every bar
```

Risk inputs in LIVE:

- **Equity** is the broker's net liquidating value.
- **Daily and weekly P/L, loss streak, and trade count** come from the LIVE ledger of actual fills. They use the same helper functions as PAPER.
- **The kill switch** (`KILL_SWITCH` in the repository root) is shared and blocks entries in both modes.

## 8. CLI usage

```powershell
# Read-only readiness report (no orders, no dry-runs). Safe in PAPER mode. Exit code 0 = READY, 3 = BLOCKED.
.\.venv\Scripts\python.exe -m trader.cli live-check            # add --symbol SPY to narrow, --skip-market-data for account only

# One-time owner authorization for THIS exact strategy hash and account (written to live_authorization.json, git-ignored)
.\.venv\Scripts\python.exe -m trader.cli validate-strategy     # copy configuration_hash
.\.venv\Scripts\python.exe -m trader.cli authorize-live --strategy-hash <configuration_hash> --account-suffix <last 4> `
    --statement "I AUTHORIZE LIVE EXECUTION OF THIS EXACT STRATEGY HASH" --note "why"

# REAL MONEY. Asks you to type "LIVE <last 4>" in the terminal at every start.
.\.venv\Scripts\python.exe -m trader.cli live-session          # add --symbol X (repeatable) to narrow
```

`paper-session` refuses to run when `TRADING_MODE=LIVE`, and `live-session` refuses unless it is. Changing `strategy.txt` (through `activate-strategy`) voids the LIVE authorization until you authorize the new hash.

## 9. Operations

- **Start:**
  - Start before 09:30 ET, as with PAPER.
  - Startup verifies the account, permissions and balances, resolves leftover orders, and reconciles.
  - It then recovers session history and joins the live feed.
  - Entries per symbol are allowed only once the recovery report says `READY` and the history-to-live join is `VERIFIED`.
- **Stop:** Ctrl+C. **Positions stay open at the broker.** The strategy flattens at 15:59 ET (`flatten_at_session_close`). If you stop before then, close positions manually or restart `live-session`, which resumes managing them.
- **Kill switch:** `New-Item KILL_SWITCH` blocks new entries immediately. Exits continue. Remove it manually.
- **Files:** everything is under `data/live/`:
  - ledger: `live_account.json`
  - order lifecycle: `live_orders.jsonl`
  - unresolved orders: `live_open_intents.json`
  - decisions: `decisions.jsonl`
  - readiness and recovery reports: `readiness/`, `recovery_reports/`
  - log: `paper_session.log` (the shared logger's file name)
  - alerts: `critical_alerts.jsonl`
- **Entries blocked mid-session:** read the `entries_blocked` events in `live_orders.jsonl` and `critical_alerts.jsonl`. Resolve the broker-side cause (for example cancel a stray order, or close a position you opened manually), then restart.
- **Execution timing settings** live in `ExecutionSettings` in `trader/live_execution.py`. They are not strategy parameters:
  - entry fill wait: 20 s
  - exit fill wait: 10 s, up to 6 attempts
  - cancel confirmation: up to 20 s
  - broker refresh: every 30 s; state is stale after 90 s

## 10. Exact `.env` settings to activate LIVE

```ini
TRADING_MODE=LIVE
ENABLE_LIVE_TRADING=true
LIVE_START_CONFIRMATION=I UNDERSTAND LIVE TRADING USES REAL MONEY
# already present and verified:
TASTYTRADE_CLIENT_ID=...
TASTYTRADE_CLIENT_SECRET=...
TASTYTRADE_REFRESH_TOKEN=...
TASTYTRADE_ACCOUNT_NUMBER=...   # the ****6633 cash account
```

You also need the `authorize-live` record (section 8) and the typed confirmation at each start. Set `TRADING_MODE=PAPER` to return to paper; LIVE files stay separate.

## 11. Remaining blockers

1. **The account is underfunded for the strategy.**
   - Net liq is $50.00, and the strategy risks at most 10% of equity per trade: $5.00.
   - The cheapest eligible contract tonight needed about $90 (TLT put), which implies roughly $905+ net liq; SPY needs about $4,800+.
   - LIVE would run and log decisions, but the strategy's own risk gate would reject every entry.
   - Sizing was **not** changed.
   - **Small-contract check (2026-10-16, read-only, after-hours quotes).** The cheapest contract each symbol offers that passes the strategy's own filters (7–21 DTE, 0.45–0.65 delta, spread ≤ $0.10 and ≤ 10%, OI ≥ 100, volume ≥ 10). Modeled risk is ask × 100 + $1.44 round-trip costs; the net liq needed is 10× that.

     | In the current universe | Modeled risk | Net liq needed |
     |---|---|---|
     | TLT put | $62.44 | ~$624 |
     | UBER call | $171.44 | ~$1,714 |
     | IWM call | $179.44 | ~$1,794 |
     | NFLX put | $246.44 | ~$2,464 |
     | SPY call | $374.44 | ~$3,744 |

     At $200 net liq ($20 per trade) nothing in the universe fits. Outside it, only PLUG (~$13) fit; F (~$34, ~$344 needed) and SOFI (~$47, ~$474) were next. Adding symbols changes the strategy's universe and hash, which needs explicit owner authorization, a new version, a new backtest and a new `authorize-live`.
2. **Cash account settlement.**
   - Option sale proceeds settle T+1.
   - The strategy flattens every day and can trade up to 6 times a day. Re-buying with same-day unsettled proceeds can cause good-faith violations.
   - **Settlement rule: ENFORCED (owner-authorized 2026-10-16).** It's an account rule in `LiveTrader`, not a strategy parameter.
     - Each LIVE purchase (ask × 100 × qty + commission + exchange fee) must fit in *settled funds*.
     - Settled funds = the broker's derivative buying power, minus this bot's own sale proceeds that haven't reached their T+1 settlement date (next trading session, ET), capped by the broker's `cash-settle-balance`.
     - Otherwise the entry is rejected as `insufficient_settled_funds_cash_account` before any dry-run or order.
     - The rule applies to every account the broker doesn't report as `Margin`. A missing `cash-settle-balance` blocks entries.
     - Proceeds are computed from the LIVE ledger, so the rule survives restarts.
     - `live-check` reports it as `cash_account_settlement_rule`, and the `live-session` startup line prints the settled funds available.
3. **No separate LIVE account-level limit exists.** The strategy's own limits apply to the broker net liq:
   - 10% per trade
   - 2% daily loss
   - 5% weekly loss
   - 3 consecutive losses
   - 6 trades a day
   - 4 open positions
   - 1 contract per trade

   If you want a LIVE capital cap (for example "treat at most $X as equity"), authorize the value. I did not invent one.
4. **Not verifiable until market hours:** the continuous DXLink Trade stream, fresh underlying quotes, and fresh option quotes for LIVE. Run `live-check` between 09:30 and 16:00 ET; it must report READY apart from the items above.
5. **Broker-side order validation has never been exercised.** The order JSON follows tastytrade's documented schema, but no dry-run has been sent. **I need your explicit authorization** to send one `POST /orders/dry-run` (validation only; it places nothing).
6. **No broker-resident protective order.** The strategy's stop and target are underlying-price triggers monitored by this process, and tastytrade has no underlying-triggered option stop. A protective order would be a different exit rule, so none was added. If the process dies mid-session, positions are unmanaged until restart; restart resumes management.
7. **The watchdog does not cover LIVE yet.** `trader.supervisor watchdog` reads `data/engine_heartbeat.json` (PAPER). The LIVE heartbeat is `data/live/engine_heartbeat.json`.
8. **Labeling.** `decision_cycle` (frozen `engine.py`) labels its entry decision `PAPER_ENTRY` with `diagnostics.mode = "PAPER"`, also in `data/live/decisions.jsonl`. LIVE orders and fills are recorded with `mode: LIVE` in the LIVE ledger and journal. The label was left alone because changing it would mean editing a strategy file.
9. ~~`trader/paper_runner.py` changed (needs your sign-off).~~ **Owner approved keeping these changes, 2026-10-16.** It contains the loop shared by both modes. The exact diff is in section 12.

## 12. Strategy-integrity hash comparison

The baseline was recorded before any change in `docs/live/strategy_baseline_sha256.txt` (git `8a65e53`, clean tree). Strategy configuration hash: `c597cfe86d7971a7e96b08ed71bc58eff6dca6942fcb0288b24498dedf6e902b`, before and after.

| File | Result |
|---|---|
| strategy.txt, active_strategy.json, strategy_engine.py, option_selector.py, risk_engine.py, engine.py, position_manager.py, fees.py, strategy_loader.py, backtest.py, metrics.py, calendar.py, paper_execution.py, portfolio.py, market_data.py, history_providers.py, strategy_plain_english.md | **Unchanged** (sha256 OK) |
| trader/paper_runner.py | **Changed: LIVE execution seam only. Owner approved 2026-10-16** |

The `paper_runner.py` changes, all behind `live is not None` unless noted:

- `_run(..., live=None)`. A LIVE run uses the LIVE ledger instead of `paper_account.json`.
- The entry fill and the exit/flatten fills call `LiveTrader` instead of `PaperExecutor`.
- `RiskState.equity` and the daily-loss equity use broker net liq.
- `decision_cycle` receives `reconciled` / `protection_verified` / `account_certain` from broker reconciliation. PAPER passes the same `True` values as before.
- Pending LIVE entries count against open slots. This is 0 in PAPER.
- Mode labels in log, report and heartbeat text. These are identical strings in PAPER.
- `_closed()` also counts `live_close` ledger rows. A paper ledger never contains them.

The 69 pre-existing tests, including the end-to-end multi-symbol PAPER session, pass unchanged.
