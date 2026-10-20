# Small-account PAPER profile

An isolated $1,000 PAPER ledger runs the **unchanged** strategy (`opening_range_breakout_options` 0.1.6, config hash `c597cfe8…`). The original ledger in `data/` keeps running exactly as before. The owner authorized this on 2026-10-16.

Only three things differ from the original account:

| | `SMALL_ACCOUNT_PAPER` | `ORIGINAL_ACCOUNT_PAPER` (and the original ledger) |
|---|---|---|
| Ledger | `data/profiles/small_account_paper/`, starts at $1,000 | original: `data/`, $10,000 |
| Allocation | Combined maximum loss of open positions ≤ **40% of equity** ($400 at $1,000). One trade may use what is left of that pool (owner's choice). | strategy's own: ≤ 10% of equity per trade, no extra cap |
| Universe | 47 symbols: the original 14 plus 33 audited additions (`profiles/small_account_universe.json`) | the original 14, from `strategy.txt` |
| Daily loss limit | **20% of equity** ($200 at $1,000). Owner-authorized 2026-10-16 as `risk_overrides` in `profiles/small_account.json` | strategy's own 2% |
| Weekly loss limit | **40% of equity** (~$400 at $1,000). Owner-authorized 2026-10-19 as `risk_overrides` in `profiles/small_account.json` | strategy's own 5% |
| Per-trade cap | **$150** of modeled maximum loss (premium x 100 plus modeled round-trip costs). Owner-authorized 2026-10-19 as `max_per_trade_dollars` in `profiles/small_account.json`; the budget is the remaining 40% pool or $150, whichever is smaller | none (strategy's own sizing) |

Everything else is the strategy's own and identical in both profiles: candles, indicators, opening range, signals, filters, contract selection, exits, the 1-contract limit, 4 open positions, 6 trades a day, and the weekly and consecutive-loss limits.

## Run it

```bash
python -m trader.cli paper-session --account-profile small   # PAPER only; refuses TRADING_MODE=LIVE
python -m trader.cli profile-status                          # read-only JSON summary
python -m trader.cli dashboard                               # "PROFILE: …" panel above the original account
python -m trader.cli profile-report                          # writes data/profiles/small_account_paper/report.html
```

`report.html` is self-contained: open it from disk; it reloads itself every 60 s. The scheduled task `\OptioBot\SmallAccountReport` regenerates it every 2 minutes (hidden, read-only: it reads the ledger files and writes only `report.html` and `report.json`).

It is a separate process from the scheduled original engine. Both share the same `KILL_SWITCH`. The watchdog, scheduled task and original heartbeat (`data/engine_heartbeat.json`) are untouched. The profile writes its own heartbeat, logs, decisions and alerts under `data/profiles/small_account_paper/`, and **the watchdog does not monitor it**.

## How the 40% cap works

- **Exposure** is each open position's maximum potential loss: the full premium × 100 × quantity plus modeled round-trip fees and slippage (the strategy's `modeled_risk_dollars`). For example, a $2.00 option counts as $201.44, not $2. The strategy only buys single long options, so debit spreads do not occur.
- **Each decision cycle:**
  - The strategy object is handed a per-trade sizing budget equal to `40% × equity − open exposure`.
  - Only `risk.max_risk_per_trade_fraction` and `hard_max_risk_per_trade_fraction` are swapped at runtime. A test asserts that no other field differs.
  - The strategy's existing option selector then skips any contract that doesn't fit. Cheaper contracts are never forced, and a trade is never forced to use the capital.
- **At the fill:** an extra synchronous check (no `await` before the fill) rejects an entry that would push combined exposure past the cap. This is what stops two same-minute signals from both filling.
- **As equity grows,** the fraction stays 40%. The dollar cap follows equity: 40% of the current equity.

## $10,000 restore (one way)

- **When it's checked:** only at session boundaries, before the session starts and after it ends.
- **Equity basis:** ledger cash, with any open position valued at zero. Only booked fills move it, so a quote spike or a bad mark cannot trigger a switch. Flatten-at-close normally leaves no open positions at the boundary, so cash equals equity there.
- **When cash ≥ $10,000:**
  1. `profile_state.json` switches to `ORIGINAL_ACCOUNT_PAPER`.
  2. The transition is recorded in `profile_audit.jsonl`.
  3. A notification goes to `notifications.jsonl`, by email if SMTP is configured, and to the console.
  4. Open positions and history are left as they are.
- **Restored settings:** the next session runs `run_paper_session(..., profile=None)` with the unmodified strategy object. That is the original universe and original risk settings, verified against `docs/profiles/baseline_20261016/original_account_settings.json`. If the running strategy's hash, universe or risk settings differ from that record, the profile refuses to run rather than guess.
- It never switches back.

## Expanded universe audit (2026-10-16, 14:50 UTC, read-only, market open)

102 candidates were checked, with these requirements:
- a valid, fresh quote
- complete DXLink 1-minute history for the session (80/80 bars)
- an option chain with quotes and Greeks on every contract
- usable bid/ask
- at least one contract passing the **existing** filters at ≤ $400 modeled risk

**Added (33), cheapest qualifying contract:**
NIO $18, RIVN $42, JD $46, PFE $59, F $60, UNG $60, XLF $62, XLU $64, T $67, LQD $70, AAL $71, MARA $75, RIOT $75, CMCSA $78, VZ $87, CCL $94, SOFI $95, SLV $100, SQQQ $102, EEM $105, NKE $105, DKNG $109, XLE $119, PYPL $131, PDD $143, IBIT $149, SMCI $170, WMT $171, UPST $185, TQQQ $204, KO $207, BABA $255, HOOD $284.

**Excluded:**
- 11 for incomplete or out-of-order history: XYZ, RBLX, SNAP, ROKU, C, FXI, PARA, ORCL, AVGO, JPM, WBD. The strategy would block these anyway.
- 44 because no contract met the strategy's filters within $400. Examples: AMD, TSLA, META, MSFT, COIN, GLD, EWZ.

Per-symbol evidence is in the universe file.

**Original 14 at $400:** TLT, UBER, BAC, IWM, NFLX, INTC and SPY had a qualifying contract. QQQ, DIA, NVDA, AAPL, AMZN, GOOGL and PLTR did not at audit time; they stay in the list.

**Capacity.**
- *Problem:* 47 quote requests at once drew HTTP 429 from tastytrade on about a third of the symbols.
- *Fix:* the profile process paces its REST requests to 10/s and retries a refused read-only GET up to 3 times. POST/PUT/DELETE are never retried.
- *Measured:* 47/47 quotes per minute for 3 consecutive minutes, the slowest after 8.0 s. The quote freshness limit is 20 s.
- *Scope:* pacing is off in every other process, so the original engine's request timing is unchanged.
- *Side effect:* the last symbols in a minute are decided up to ~5–8 s after the first. The same-minute ranking window is 3 s, so a late symbol can be ranked in a second round. Each symbol's own decision is unchanged.

**What the earlier backtest showed for some additions.** This is information only; it was not a selection criterion. Out of sample (Sep 1 – Oct 10, 2026), NIO, RIVN, PFE, F, XLF, T, AAL, MARA, CCL, VZ, SOFI and SLV had profit factors of 0.04–0.63, all net losing. The other additions were not backtested.

## Strategy-integrity audit

Baseline recorded before any change: `docs/profiles/baseline_20261016/`. It contains `strategy_sha256.txt`, `git_status.txt` and `original_account_settings.json`. The original paper ledger snapshot at that point had 12 closed trades and +$858.72 realized.

| File | Result |
|---|---|
| strategy.txt, active_strategy.json, strategy_engine.py, option_selector.py, risk_engine.py, engine.py, position_manager.py, fees.py, strategy_loader.py, backtest.py, metrics.py, calendar.py, paper_execution.py, portfolio.py, market_data.py, history_providers.py, strategy_plain_english.md | **Unchanged** (sha256 OK) |
| trader/paper_runner.py | **Changed: profile hook only.** Full diff in `paper_runner_profile_hook.diff`. Removing exactly these edits reproduces the recorded baseline hash. |

Strategy configuration hash: `c597cfe86d7971a7e96b08ed71bc58eff6dca6942fcb0288b24498dedf6e902b`, before and after.

**Every file this work added or changed:**

| File | Why |
|---|---|
| `trader/account_profiles.py` (new) | Profile config, isolated ledger, 40% cap, one-way transition, status, session wrapper |
| `trader/paper_runner.py` | The shared loop needs the per-cycle sizing budget and the fill-time cap check. `profile=None`, the default, is the original code path. |
| `trader/tastytrade_client.py` | Opt-in request pacing and GET-only 429 retry, needed for the 47-symbol load; off unless the profile turns it on |
| `trader/cli.py` | `paper-session --account-profile small`, `profile-status` |
| `trader/dashboard.py` | Profile panel |
| `profiles/small_account.json`, `profiles/small_account_universe.json` (new) | Profile settings and expanded universe, kept outside the strategy |
| `docs/profiles/*` (new) | Baseline, diff and this document |
| `tests/test_profiles.py` (new) | Acceptance tests |
| `README.md`, `docs/operations.md` | Pointers to this document |

## Acceptance criteria

All of these are verified by `tests/test_profiles.py` or a live read-only check:

| # | Criterion | Evidence |
|---|---|---|
| 1 | Ledger starts at exactly $1,000 | `test_new_ledger_initializes_at_exactly_1000_in_its_own_directory`; real ledger initialized with cash $1,000 |
| 2 | Original ledger untouched | `test_original_ledger_is_untouched_by_a_profile_session` (byte-identical); real `data/paper_account.json` hash unchanged by initialization |
| 3 | Cap $400 initially | `test_initial_allocation_is_400_and_cap_counts_the_contract_multiplier` |
| 4 | Simultaneous positions share $400 | `test_two_simultaneous_signals_respect_the_combined_400_cap` (second fill blocked by the cap) |
| 5 | ×100 multiplier | same test: $2.00 option = $201.44 exposure |
| 6 | Unaffordable skipped | `test_unaffordable_contract_is_skipped_not_forced` ($406.44 > $400) |
| 7 | Expanded universe loads | `test_expanded_universe_loads_from_its_own_file_and_strategy_is_untouched`, `test_repository_profile_matches_the_recorded_original_and_expands_the_universe` |
| 8–10 | Same analysis and signals, no weakened criteria | `test_same_market_data_gives_identical_analysis_and_only_the_budget_differs`, `test_only_sizing_keys_differ_from_the_strategy`, `StrategyIntegrityTests` |
| 11–12 | Transition at $10,000, exact original restored | `test_transition_at_10000_restores_original_exactly_and_never_reverts` (the restored run uses the unmodified strategy object), `test_quote_spike_cannot_trigger_transition`, `test_original_cannot_be_restored_from_a_different_strategy` |
| 13 | Positions and history survive transitions | same transition test |
| 14 | Restart keeps profile and balances | `test_restart_resumes_the_profile_ledger_and_its_positions`, and the reload check in the transition test |

**Not yet demonstrated:** a full real-market session of the profile. Nothing has run it against live data beyond the read-only audits, and it should be watched on its first day.

## Things to know

- **Daily limit:** 20% ($200 at $1,000) by owner decision on 2026-10-16. It feeds the strategy's daily-loss entry block and its daily-loss exit, just as the strategy's own 2% would. 
- **Weekly limit:** 40% (~$400 at $1,000) by owner decision on 2026-10-19, after the strategy's 5% (~$48) blocked entries following a single stopped trade. It counts realized P/L since Monday plus open positions' marks. Only `max_daily_loss_fraction` and `max_weekly_loss_fraction` can be overridden; the loader rejects anything else.
- **Weekly limit:** still the strategy's 5% ($50 at $1,000), along with 3 consecutive losses. The weekly limit will usually bind before the daily one.
- **No LIVE:** this profile is PAPER-only. Nothing here sends broker orders or changes LIVE behavior.

## End-of-day summary

`python -m trader.cli profile-eod --account-profile small` (scheduled task `\OptioBot\SmallAccountEndOfDay`, weekdays 3:20 PM CT) lists every contract bought that day with its exit and P/L, plus the day's opening cash (reconstructed from the day's fills), closing cash, day/week/total P/L and fees. It writes `eod/eod-YYYY-MM-DD.html` and `.json` in the profile data directory, emails the text version and pushes a one-line result to the phone. A position still open at that time breaks the no-overnight rule (`flatten_at_session_close` closes everything ~1 minute before the close), so the push is urgent and says `N OPEN OVERNIGHT`. `--no-notify` writes the files only.

## Fees on paper fills

Since 2026-10-20 (owner request), booked PAPER fills pay tastytrade's published stock/ETF option fees
(`trader/broker_fees.py`; "Commissions & Fees", last updated 2026-07-30) instead of the strategy's flat
$0.70 per contract per side:

| Fill | Fees per contract |
|---|---|
| Buy to open | $1.00 commission (capped at $10 per leg) + $0.10 clearing + $0.02 Options Regulatory Fee = **$1.12** |
| Sell to close | $0 commission + $0.10 clearing + $0.02 ORF + SEC fee ($20.60 per $1M of proceeds, rounded up to the cent) + FINRA TAF ($0.00329, not assessed 2026-10-01 through 2026-12-31) = **about $0.13** |

A one-contract round trip costs about $1.25 (was $1.40). The strategy's own fee values in `strategy.txt` still
drive its sizing and economic gate, so the strategy and its hash are unchanged; the change is limited to the
ledger booking (`trader/paper_execution.py`, `trader/paper_runner.py`; diff in `docs/profiles/broker_fees.diff`).
Fills booked before 2026-10-20 keep the fees they were booked with.

## Supervisor review (Claude and Codex)

Since 2026-10-20 the hourly review (`HourlyReview`, `python -m trader.supervisor review`) and the 5:15 PM daily
report review the ledger whose engine is scheduled to run: every `\OptioBot\` task running `paper-session` that
is not disabled. `paper-session --account-profile small` maps to `small_account_paper`; a plain `paper-session`
(`PaperEngine`) maps to the original ledger in `data/`. If both engines are enabled, both ledgers are reviewed
separately. If neither is, nothing is reviewed. If the schedule can't be read, the review fails and sends the
failure email instead of picking a ledger. `--ledger small` or `--ledger original` names one explicitly.

Each review writes to its own ledger's `reviews/` folder and `supervisor_reviews.jsonl`, and the email subject
names the ledger. The snapshot includes the profile's rules (allocation, per-trade cap, loss limits) so
reviewers judge the small ledger against them. Advisory only: reviewers have no tools, credentials, or
authority to change anything.
