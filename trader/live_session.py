"""LIVE session: readiness checks, owner authorization and the supervised run of the shared strategy loop."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .calendar import MarketCalendar
from .config import AppConfig, Mode
from .history_providers import create_history_provider
from .live_execution import (LIVE_AUTHORIZATION_FILE, LIVE_AUTHORIZATION_STATEMENT, LONG_OPTION_LEVELS, LiveTrader,
                             account_fingerprint, mask_account, verify_live_authorization)
from .logging_utils import atomic_replace
from .market_data import ProductionMarketData, dxlink_events, fetch_option_quotes
from .notifications import record_critical_alert
from .option_selector import evaluate_options
from .fees import FeeSchedule
from .paper_runner import (MAX_CLOCK_OFFSET_SECONDS, MAX_ENGINE_RESTARTS, _close_session_log_file,
                           _fresh_underlying_quote, _is_connection_failure, _run, _session_logger, _session_symbols,
                           _set_engine_expected, _validated_session_history, _write_heartbeat)
from .strategy_loader import Strategy, parse_strategy, verify_manifest
from .tastytrade_client import TastytradeClient

LIVE_DIR_NAME = "live"


def live_data_dir(cfg: AppConfig) -> Path:
    """Everything LIVE writes (ledger, orders, decisions, heartbeat, reports) lives apart from PAPER files."""
    return cfg.data_dir / LIVE_DIR_NAME


def authorize_live(*, root: Path, cfg: AppConfig, strategy_hash: str, account_suffix: str, statement: str,
                   note: str = "") -> Path:
    """Owner action: bind LIVE execution to this exact strategy hash and configured account."""
    strategy = parse_strategy(root / "strategy.txt")
    verify_manifest(root / "strategy.txt", root / "active_strategy.json")
    if strategy_hash != strategy.sha256:
        raise RuntimeError(f"--strategy-hash does not match the active strategy hash {strategy.sha256}")
    if not cfg.account_number or not cfg.account_number.endswith(account_suffix) or len(account_suffix) < 4:
        raise RuntimeError("--account-suffix must be the last 4+ characters of TASTYTRADE_ACCOUNT_NUMBER")
    if statement != LIVE_AUTHORIZATION_STATEMENT:
        raise RuntimeError(f'--statement must be exactly "{LIVE_AUTHORIZATION_STATEMENT}"')
    record = {"strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
              "strategy_sha256": strategy.sha256, "account_fingerprint": account_fingerprint(cfg.account_number),
              "account_masked": mask_account(cfg.account_number), "statement": statement, "note": note,
              "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "LIVE execution of the unchanged strategy; strategy.txt and active_strategy.json are not modified"}
    path = root / LIVE_AUTHORIZATION_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    atomic_replace(temporary, path)
    return path


def _check(report: dict, name: str, ok: bool | None, detail: Any = None, *, blocker: bool = True) -> bool:
    """Record a readiness check. ok=None means not verifiable now (e.g. market closed)."""
    status = "PASS" if ok else "NOT_VERIFIED" if ok is None else "FAIL"
    report["checks"].append({"check": name, "status": status, "blocking": blocker, "detail": detail})
    if blocker and ok is not True:
        report["blockers"].append(f"{name}: {detail}")
    elif ok is not True:
        report["warnings"].append(f"{name}: {detail}")
    return bool(ok)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def live_readiness(*, root: Path, cfg: AppConfig, symbols: tuple[str, ...] | None = None,
                   market_data: bool = True, client: TastytradeClient | None = None) -> dict:
    """Read-only LIVE readiness check. Never submits, dry-runs or cancels an order."""
    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {"checked_at_utc": now.isoformat(), "checks": [], "blockers": [], "warnings": [],
                              "account": mask_account(cfg.account_number), "orders_submitted": 0}
    strategy = parse_strategy(root / "strategy.txt")
    chosen = _session_symbols(strategy, symbols)
    report["strategy"] = {"id": strategy.strategy_id, "version": strategy.version, "sha256": strategy.sha256}
    try:
        verify_manifest(root / "strategy.txt", root / "active_strategy.json")
        _check(report, "strategy_manifest_verified", True, "manifest matches strategy.txt (PAPER designation unchanged)")
    except Exception as exc:
        _check(report, "strategy_manifest_verified", False, str(exc))
    _check(report, "trading_mode_live", cfg.mode is Mode.LIVE, f"TRADING_MODE={cfg.mode.value}")
    _check(report, "enable_live_trading", cfg.live_enabled, f"ENABLE_LIVE_TRADING={'true' if cfg.live_enabled else 'false'}")
    _check(report, "live_start_confirmation_phrase", cfg.live_confirmation == "I UNDERSTAND LIVE TRADING USES REAL MONEY",
           "LIVE_START_CONFIRMATION set" if cfg.live_confirmation else "LIVE_START_CONFIRMATION empty")
    try:
        record = verify_live_authorization(root, strategy, cfg.account_number)
        _check(report, "owner_live_authorization", True, f"authorized {record.get('authorized_at_utc')}")
    except Exception as exc:
        _check(report, "owner_live_authorization", False, str(exc))
    _check(report, "kill_switch_clear", not (root / "KILL_SWITCH").exists(),
           "KILL_SWITCH present: entries blocked" if (root / "KILL_SWITCH").exists() else "absent")
    missing = [n for n, v in (("TASTYTRADE_CLIENT_ID", cfg.client_id), ("TASTYTRADE_CLIENT_SECRET", cfg.client_secret),
                              ("TASTYTRADE_REFRESH_TOKEN", cfg.refresh_token),
                              ("TASTYTRADE_ACCOUNT_NUMBER", cfg.account_number)) if not v]
    if not _check(report, "credentials_present", not missing, "missing " + ", ".join(missing) if missing else "present"):
        return report
    client = client or TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    try:
        client._access_token()
        _check(report, "oauth_access_token", True, "refresh-token grant succeeded (15-minute tokens renew automatically)")
    except Exception as exc:
        _check(report, "oauth_access_token", False, f"{type(exc).__name__}: {str(exc)[:300]}")
        return report

    # Account identity, permissions and status.
    items = client.accounts().get("data", {}).get("items", [])
    match = next((i for i in items if (i.get("account") or {}).get("account-number") == cfg.account_number), None)
    _check(report, "configured_account_accessible", match is not None,
           f"{len(items)} account(s) on this login; configured {mask_account(cfg.account_number)} "
           f"{'found' if match else 'NOT found (no fallback to another account)'}")
    if match is None:
        return report
    info = match.get("account") or {}
    authority = str(match.get("authority-level"))
    _check(report, "trading_authority", authority.lower() in {"owner", "trade-only", "trade"}, authority)
    status = client.trading_status(cfg.account_number).get("data", {})
    flags = [k for k in ("is-closed", "is-frozen", "is-closing-only", "is-risk-reducing-only", "is-in-margin-call",
                         "is-in-day-trade-equity-maintenance-call") if status.get(k)]
    _check(report, "account_status_open", not flags and not info.get("is-closed"), ", ".join(flags) or "open, not frozen")
    _check(report, "options_permission_long_options", status.get("options-level") in LONG_OPTION_LEVELS,
           f"options-level={status.get('options-level')}")
    report["account_profile"] = {"type": info.get("account-type-name"), "margin_or_cash": info.get("margin-or-cash"),
                                 "options_level": status.get("options-level"),
                                 "pattern_day_trader": status.get("is-pattern-day-trader")}
    balances = client.balances(cfg.account_number).get("data", {})
    net_liq = float(balances.get("net-liquidating-value") or 0)
    dbp = float(balances.get("derivative-buying-power") or 0)
    report["balances"] = {"net_liquidating_value": net_liq, "derivative_buying_power": dbp,
                          "cash_balance": float(balances.get("cash-balance") or 0),
                          "settled_cash": _optional_float(balances.get("cash-settle-balance")),
                          "balances_updated_at": balances.get("updated-at")}
    _check(report, "balances_retrieved", True, f"net liq ${net_liq:,.2f}, derivative buying power ${dbp:,.2f}")
    risk_fraction = min(float(strategy.risk["max_risk_per_trade_fraction"]), float(strategy.risk["hard_max_risk_per_trade_fraction"]))
    budget = net_liq * risk_fraction
    report["strategy_risk_budget_per_trade"] = budget
    if str(info.get("margin-or-cash")).lower() != "margin":
        settled = report["balances"]["settled_cash"]
        _check(report, "cash_account_settlement_rule", settled is not None,
               "cash account: option sale proceeds settle T+1. Settlement rule ENFORCED: each LIVE purchase must fit in "
               "settled funds (derivative buying power less the bot's unsettled sale proceeds, capped by settled cash "
               + (f"${settled:,.2f}); same-day proceeds are not reused." if settled is not None
                  else "— missing from the broker response, so entries are blocked)."))
    elif net_liq < 25_000:
        _check(report, "pattern_day_trader_rule", None,
               "margin account under $25,000: the strategy's intraday round trips count as day trades", blocker=False)

    positions = client.positions(cfg.account_number).get("data", {}).get("items", [])
    orders = client.live_orders(cfg.account_number).get("data", {}).get("items", [])
    ledger_path = live_data_dir(cfg) / "live_account.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {"positions": []}
    ledger_symbols = {p["symbol"]: int(p["quantity"]) for p in ledger.get("positions", [])}
    universe = set(strategy.universe)
    broker_managed = {p["symbol"]: int(float(p["quantity"])) * (-1 if p.get("quantity-direction") == "Short" else 1)
                      for p in positions if p.get("instrument-type") == "Equity Option" and p.get("underlying-symbol") in universe}
    working = [o for o in orders if o.get("status") not in {"Filled", "Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"}]
    report["broker_positions"] = {"total": len(positions), "strategy_underlying_options": broker_managed}
    report["broker_working_orders"] = len(working)
    _check(report, "position_reconciliation", broker_managed == ledger_symbols,
           f"broker {broker_managed or 'none'} vs LIVE ledger {ledger_symbols or 'none'}")
    _check(report, "no_foreign_working_orders", not [o for o in working if o.get("underlying-symbol") in universe],
           f"{len(working)} working order(s) at broker")

    offset = client.clock_offset_seconds()
    _check(report, "local_clock_offset", abs(offset) <= MAX_CLOCK_OFFSET_SECONDS, f"{offset:+.3f}s (limit {MAX_CLOCK_OFFSET_SECONDS}s)")
    token = client.quote_token().get("data", {})
    calendar = MarketCalendar()
    market_tz = ZoneInfo("America/New_York")
    today = now.astimezone(market_tz).date()
    bounds = calendar.session_bounds(today)
    expires = token.get("expires-at")
    expires_ok = bool(token.get("token") and token.get("dxlink-url") and expires and
                      datetime.fromisoformat(expires.replace("Z", "+00:00")) > (bounds[1] if bounds else now))
    _check(report, "quote_token", expires_ok, f"level={token.get('level')} expires-at={expires}")
    if not market_data or not expires_ok:
        return report

    # Market data: the same feeds the PAPER loop consumes.
    market_open = calendar.is_open(now)
    report["market_open_at_check"] = market_open
    history_day = today
    while not calendar.session_bounds(history_day) or (history_day == today and not market_open and
                                                        now < calendar.session_bounds(today)[1]):
        history_day -= timedelta(days=1)
    open_et, close_et = calendar.session_bounds(history_day)
    start = open_et.astimezone(timezone.utc)
    last = min(close_et.astimezone(timezone.utc), now.replace(second=0, microsecond=0)) - timedelta(minutes=1)
    provider = create_history_provider(cfg.historical_data_provider, dxlink_url=token["dxlink-url"],
                                       quote_token=token["token"], massive_api_key=cfg.massive_api_key)
    history: dict[str, Any] = {}
    spots: dict[str, float] = {}

    async def fetch_history() -> None:
        for symbol in chosen:
            audit: dict = {}
            try:
                batch = await provider.fetch(symbol=symbol, start=start, end=last + timedelta(minutes=1))
                audit.update(batch.audit)
                bars = _validated_session_history(list(batch.candles), session_open=start, last_completed_bar=last, audit=audit)
                history[symbol] = {"ok": True, "bars": len(bars), "expected": audit.get("expected_candle_count"),
                                   "missing": audit.get("missing_candle_count"), "duplicates": audit.get("duplicate_candle_count")}
                spots[symbol] = bars[-1].close
            except Exception as exc:
                history[symbol] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                   "received": audit.get("received_candle_count")}

    asyncio.run(fetch_history())
    report["history"] = {"provider": provider.name, "session": history_day.isoformat(), "symbols": history}
    failed = [s for s, row in history.items() if not row["ok"]]
    _check(report, "historical_candles_complete", not failed,
           f"{len(chosen) - len(failed)}/{len(chosen)} symbols complete for {history_day}" + (f"; failed {failed}" if failed else ""))

    async def stream_probe(subscriptions: list[dict]) -> dict[str, int]:
        seen: dict[str, int] = {}
        try:
            async with asyncio.timeout(20):
                async for message in dxlink_events(token["dxlink-url"], token["token"], subscriptions):
                    for item in message.get("data", []):
                        if isinstance(item, list) and item:
                            seen[str(item[0])] = seen.get(str(item[0]), 0) + 1
                    if {"Quote", "Trade", "Greeks"} <= set(seen) and sum(seen.values()) >= 6:
                        break
        except TimeoutError:
            pass
        return seen

    quotes = ProductionMarketData(client)
    underlying = quotes.snapshots(list(chosen)[:100])
    fresh = {q.symbol: _fresh_underlying_quote(q, datetime.now(timezone.utc)) for q in underlying}
    report["underlying_quotes"] = {q.symbol: {"bid": q.bid, "ask": q.ask, "updated_at": q.timestamp.isoformat(),
                                              "fresh": fresh[q.symbol]} for q in underlying}
    _check(report, "underlying_quotes_fresh", all(fresh.values()) if market_open else None,
           f"{sum(fresh.values())}/{len(fresh)} fresh" + ("" if market_open else " (market closed: freshness not verifiable)"))

    options: dict[str, Any] = {}
    in_band_streamer = None
    fees = FeeSchedule(float(strategy.execution["commission_per_contract"]), float(strategy.execution["exchange_fee_per_contract"]))
    costs = 2 * float(strategy.execution["slippage_per_contract"]) + fees.estimate(1, legs=2)
    for symbol in chosen:
        spot = next(((q.bid + q.ask) / 2 for q in underlying if q.symbol == symbol and q.bid > 0), spots.get(symbol))
        try:
            chain = fetch_option_quotes(client, underlying=symbol, spot=spot, now=now,
                                        min_dte=int(strategy.rules["min_dte"]), max_dte=int(strategy.rules["max_dte"]))
        except Exception as exc:
            options[symbol] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            continue
        greeks = [o for o in chain if None not in (o.delta, o.gamma, o.theta)]
        eligible = {}
        for side in ("CALL", "PUT"):
            unconstrained, _ = evaluate_options(chain, side, now, strategy)
            affordable, _ = evaluate_options(chain, side, now, strategy, max_risk_dollars_per_contract=budget,
                                             costs_per_contract=costs)
            eligible[side] = {"liquid_contract": unconstrained.quote.symbol if unconstrained else None,
                              "liquid_contract_modeled_risk": round(unconstrained.quote.ask * 100 + costs, 2) if unconstrained else None,
                              "affordable_within_strategy_budget": affordable.quote.symbol if affordable else None}
        ages = [(datetime.now(timezone.utc) - o.quote_timestamp).total_seconds() for o in chain if o.quote_timestamp]
        options[symbol] = {"ok": bool(chain), "contracts_quoted": len(chain), "with_greeks": len(greeks),
                           "max_quote_age_seconds": round(max(ages), 1) if ages else None, "eligible": eligible}
        if in_band_streamer is None:
            liquid = eligible["CALL"]["liquid_contract"] or eligible["PUT"]["liquid_contract"]
            if liquid:
                match_item = next((i for i in client.option_chain(symbol).get("data", {}).get("items", [])
                                   if i.get("symbol") == liquid), None)
                in_band_streamer = (match_item or {}).get("streamer-symbol") or in_band_streamer
    report["options"] = options
    bad = [s for s, row in options.items() if not row.get("ok")]
    _check(report, "option_chain_and_quotes", not bad, f"chains+quotes for {len(chosen) - len(bad)}/{len(chosen)}"
           + (f"; failed {bad}" if bad else ""))
    _check(report, "option_greeks_available", all(row.get("with_greeks") for row in options.values() if row.get("ok")),
           "delta/gamma/theta present on quoted contracts")
    affordable_any = any(side["affordable_within_strategy_budget"] for row in options.values() if row.get("ok")
                         for side in row["eligible"].values())
    _check(report, "strategy_can_afford_a_contract", affordable_any,
           f"strategy per-trade budget = {risk_fraction:.0%} x net liq ${net_liq:,.2f} = ${budget:,.2f}; "
           + ("at least one eligible contract fits" if affordable_any else
              "no eligible contract fits, so LIVE entries will always be rejected by the strategy's own risk gate"))
    subscriptions = [{"type": "Trade", "symbol": chosen[0]}, {"type": "Quote", "symbol": chosen[0]}]
    if in_band_streamer:
        subscriptions += [{"type": "Quote", "symbol": in_band_streamer}, {"type": "Greeks", "symbol": in_band_streamer}]
    seen = asyncio.run(stream_probe(subscriptions))
    report["dxlink_events_seen"] = seen
    _check(report, "dxlink_quote_and_greeks_events", {"Quote", "Greeks"} <= set(seen), seen)
    _check(report, "dxlink_live_trade_events", ("Trade" in seen) if market_open else None,
           seen.get("Trade", 0) if market_open else "market closed: live Trade stream verifiable only during RTH")
    return report


def write_readiness_report(cfg: AppConfig, report: dict) -> Path:
    directory = live_data_dir(cfg) / "readiness"
    directory.mkdir(parents=True, exist_ok=True)
    report["overall"] = "READY" if not report["blockers"] else "BLOCKED"
    path = directory / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-live-readiness.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def confirm_live_start(cfg: AppConfig, *, prompt: Callable[[str], str] = input, interactive: bool | None = None) -> None:
    """A deliberate, per-start human confirmation; .env settings alone never start LIVE."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise RuntimeError("LIVE blocked: live-session needs an interactive terminal for the startup confirmation")
    expected = f"LIVE {cfg.account_number[-4:]}"
    answer = prompt(f"REAL-MONEY LIVE session on tastytrade account {mask_account(cfg.account_number)}.\n"
                    f"Type '{expected}' to start, anything else to abort: ")
    if answer.strip() != expected:
        raise RuntimeError("LIVE start aborted: confirmation did not match")


def run_live_session(*, root: Path, cfg: AppConfig, symbols: tuple[str, ...] | None = None,
                     max_restarts: int = MAX_ENGINE_RESTARTS, prompt: Callable[[str], str] = input,
                     interactive: bool | None = None) -> None:
    """LIVE: verify gates, confirm with the operator, then run the shared strategy loop with broker execution."""
    if cfg.mode is not Mode.LIVE:
        raise RuntimeError("live-session requires TRADING_MODE=LIVE (and ENABLE_LIVE_TRADING=true plus the confirmation phrase)")
    cfg.validate_startup()
    strategy = parse_strategy(root / "strategy.txt")
    verify_manifest(root / "strategy.txt", root / "active_strategy.json")
    verify_live_authorization(root, strategy, cfg.account_number)
    symbols = _session_symbols(strategy, symbols)
    confirm_live_start(cfg, prompt=prompt, interactive=interactive)
    live_cfg = replace(cfg, data_dir=live_data_dir(cfg))
    live_cfg.data_dir.mkdir(parents=True, exist_ok=True)
    restarts = 0
    label = ",".join(symbols)
    while True:
        try:
            _run_live_once(root=root, cfg=live_cfg, strategy=strategy, symbols=symbols)
            return
        except Exception as exc:
            now = datetime.now(timezone.utc)
            if not _is_connection_failure(exc) or restarts >= max_restarts or not MarketCalendar().is_open(now):
                raise
            restarts += 1
            delay = min(10 * restarts, 60)
            print(f"RESTARTING LIVE session in {delay}s after {type(exc).__name__} ({restarts}/{max_restarts}); "
                  "startup re-verifies the account and reconciles before any entry", flush=True)
            _set_engine_expected(live_cfg.data_dir, expected=True, state="RECOVERING", symbol=label)
            _write_heartbeat(live_cfg.data_dir / "engine_heartbeat.json", status="recovering", symbol=label, mode="LIVE",
                             startup_deadline=now + timedelta(seconds=delay, minutes=5))
            time.sleep(delay)


def _run_live_once(*, root: Path, cfg: AppConfig, strategy: Strategy, symbols: tuple[str, ...]) -> None:
    logger = _session_logger(cfg.data_dir)
    label = ",".join(symbols)
    heartbeat = cfg.data_dir / "engine_heartbeat.json"
    try:
        client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
        trader = LiveTrader(cfg=cfg, client=client, strategy=strategy, data_dir=cfg.data_dir,
                            kill_switch_path=root / "KILL_SWITCH")
        report = trader.startup()
        logger.info("LIVE account verified %s", json.dumps(report, default=str))
        print(f"LIVE account {report['account']} verified: {report.get('margin_or_cash')} account, options level "
              f"{report.get('options_level')}, net liq ${report.get('net_liquidating_value'):,.2f}, "
              f"derivative buying power ${report.get('derivative_buying_power'):,.2f}, "
              f"settled funds available ${report.get('settled_funds_available') or 0:,.2f}, "
              f"{report.get('ledger_positions')} ledger position(s)", flush=True)
        if report.get("entry_block_reasons"):
            print("LIVE entries blocked until resolved: " + json.dumps(report["entry_block_reasons"]), flush=True)
        _set_engine_expected(cfg.data_dir, expected=True, state="STARTING", symbol=label)
        _write_heartbeat(heartbeat, status="starting", symbol=label, mode="LIVE")
        asyncio.run(_run(root=root, cfg=cfg, strategy=strategy, symbols=symbols, live=trader))
    except KeyboardInterrupt:
        logger.info("LIVE session stopped by operator symbols=%s", label)
        _set_engine_expected(cfg.data_dir, expected=False, state="STOPPED", symbol=label)
        _write_heartbeat(heartbeat, status="stopped", symbol=label, mode="LIVE")
        raise
    except Exception as exc:
        logger.exception("LIVE session failed symbols=%s error_type=%s", label, type(exc).__name__)
        _set_engine_expected(cfg.data_dir, expected=True, state="FAILED", symbol=label)
        _write_heartbeat(heartbeat, status="failed", symbol=label, mode="LIVE", error=type(exc).__name__)
        record_critical_alert("live_engine_runtime_failure", mode="LIVE",
                              details={"symbols": list(symbols), "error_type": type(exc).__name__, "error": str(exc)[:500]},
                              data_dir=cfg.data_dir)
        raise
    else:
        _set_engine_expected(cfg.data_dir, expected=False, state="STOPPED", symbol=label)
        _write_heartbeat(heartbeat, status="stopped", symbol=label, mode="LIVE")
    finally:
        _close_session_log_file(logger)
