from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .calendar import MarketCalendar
from .config import AppConfig, Mode
from .engine import decision_cycle
from .fees import FeeSchedule
from .market_data import CachedQuote, ProductionMarketData, fetch_option_quotes, live_minute_bars
from .models import Bar, OptionQuote, Position
from .notifications import record_critical_alert
from .paper_execution import PaperExecutor, realized_option_pnl
from .portfolio import PaperAccount, load_or_initialize, save_account
from .position_manager import exit_reason
from .risk_engine import RiskState
from .strategy_loader import Strategy, verify_manifest
from .tastytrade_client import TastytradeClient


def _write_heartbeat(path: Path, *, status: str, symbol: str, mode: str = "PAPER",
                     last_bar: datetime | None = None, open_positions: int = 0,
                     error: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "mode": mode, "symbol": symbol, "pid": os.getpid(),
               "updated_at_utc": datetime.now(timezone.utc).isoformat(),
               "last_bar_at_utc": last_bar.isoformat() if last_bar else None,
               "open_positions": open_positions, "error": error}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_paper_session(*, root: Path, cfg: AppConfig, strategy: Strategy, symbol: str) -> None:
    if cfg.mode.value != "PAPER":
        raise RuntimeError("paper-session is only available when TRADING_MODE=PAPER")
    if symbol not in strategy.universe:
        raise ValueError(f"{symbol} is not in the active strategy universe")
    cfg.require_market_data_credentials()
    verify_manifest(root / "strategy.txt", root / "active_strategy.json")
    heartbeat = cfg.data_dir / "engine_heartbeat.json"
    _write_heartbeat(heartbeat, status="starting", symbol=symbol)
    try:
        asyncio.run(_run(root=root, cfg=cfg, strategy=replace(strategy, universe=(symbol,)), symbol=symbol))
    except Exception as exc:
        _write_heartbeat(heartbeat, status="failed", symbol=symbol, error=type(exc).__name__)
        record_critical_alert("paper_engine_runtime_failure", mode="PAPER",
                              details={"symbol": symbol, "error_type": type(exc).__name__}, data_dir=cfg.data_dir)
        raise
    else:
        _write_heartbeat(heartbeat, status="stopped", symbol=symbol)


def run_paper_test_session(*, root: Path, cfg: AppConfig, strategy: Strategy, symbol: str,
                           test_capital: float = 100_000) -> None:
    """Run an isolated paper-only afternoon test with a rolling opening range."""
    if cfg.mode is Mode.LIVE:
        raise RuntimeError("paper-test-session is forbidden in LIVE mode")
    if cfg.mode.value != "PAPER":
        raise RuntimeError("paper-test-session requires TRADING_MODE=PAPER")
    if symbol not in strategy.universe:
        raise ValueError(f"{symbol} is not in the active strategy universe")
    if test_capital <= 0:
        raise ValueError("test capital must be positive")
    cfg.require_market_data_credentials()
    verify_manifest(root / "strategy.txt", root / "active_strategy.json")
    asyncio.run(_run(root=root, cfg=cfg, strategy=replace(strategy, universe=(symbol,)), symbol=symbol,
                     test_mode=True, test_capital=test_capital))


async def _run(*, root: Path, cfg: AppConfig, strategy: Strategy, symbol: str,
               test_mode: bool = False, test_capital: float = 100_000) -> None:
    now = datetime.now(timezone.utc)
    calendar = MarketCalendar()
    bounds = calendar.session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    if not bounds:
        raise RuntimeError("There is no equity session today")
    session_open, session_close = bounds
    if now >= session_close.astimezone(timezone.utc):
        raise RuntimeError("Today's equity session has already closed")
    test_start = now.replace(second=0, microsecond=0) if test_mode else session_open.astimezone(timezone.utc)
    active_start = test_start
    active_strategy = strategy
    if test_mode:
        entry_start = test_start.astimezone(ZoneInfo("America/New_York")) + timedelta(minutes=strategy.opening_range_minutes)
        entry_end = session_close.astimezone(ZoneInfo("America/New_York")) - timedelta(minutes=15)
        if entry_start >= entry_end:
            raise RuntimeError("Not enough time remains to form a rolling range and test an entry")
        active_strategy = replace(strategy, entry_window_et={"start": entry_start.strftime("%H:%M"),
                                                             "end": entry_end.strftime("%H:%M")})
    elif now >= session_open.astimezone(timezone.utc) + timedelta(minutes=2):
        raise RuntimeError("Paper session must start before the open to build today's opening range from live trades; no partial-session entries are allowed")

    account_path = cfg.data_dir / ("paper_test_account.json" if test_mode else "paper_account.json")
    account = load_or_initialize(account_path, test_capital if test_mode else cfg.starting_cash)
    if test_mode and account.starting_capital != test_capital:
        raise RuntimeError(f"Existing test ledger uses ${account.starting_capital:.2f}; pass the same --test-capital or archive {account_path.name}")
    if account.positions:
        raise RuntimeError("Paper account has open positions from an earlier run; reconcile or close them before starting a new session")
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    token_data = client.quote_token().get("data", {})
    if not token_data.get("token") or not token_data.get("dxlink-url"):
        raise RuntimeError("tastytrade quote-token response lacked token or dxlink-url")

    bars: list[Bar] = []
    session_started = False
    session_data_valid = True
    decision_log = cfg.data_dir / ("paper_test_decisions.jsonl" if test_mode else "decisions.jsonl")
    quotes = ProductionMarketData(client)
    fee_schedule = FeeSchedule(float(strategy.execution["commission_per_contract"]),
                               float(strategy.execution["exchange_fee_per_contract"]))
    executor = PaperExecutor(account, buy_price=strategy.execution["paper_buy_price"],
                             sell_price=strategy.execution["paper_sell_price"],
                             slippage_per_contract=float(strategy.execution["slippage_per_contract"]),
                             fees=fee_schedule)
    position_alerted: set[str] = set()
    if test_mode:
        print(f"PAPER TEST started for {symbol}; fake test capital ${account.starting_capital:,.2f}; "
              f"rolling {strategy.opening_range_minutes}-minute range from {test_start.isoformat()}; "
              f"entries {active_strategy.entry_window_et['start']}-{active_strategy.entry_window_et['end']} ET; "
              "local simulated orders only, no broker orders. Press Ctrl+C to stop.", flush=True)
    else:
        print(f"PAPER session started for {symbol}; local simulated orders only. Press Ctrl+C to stop.", flush=True)

    async def consume() -> None:
        nonlocal session_started, session_data_valid
        async for event_symbol, bar in live_minute_bars(token_data["dxlink-url"], token_data["token"], [symbol]):
            if event_symbol != symbol or bar.timestamp < active_start:
                continue
            if not session_started:
                if not test_mode and bar.timestamp > session_open.astimezone(timezone.utc) + timedelta(minutes=1):
                    raise RuntimeError("The live feed missed the opening minutes; blocking entries instead of inferring an opening range")
                session_started = True
            if bars and bar.timestamp <= bars[-1].timestamp:
                continue
            if bars and bar.timestamp - bars[-1].timestamp > timedelta(minutes=1):
                session_data_valid = False
                print(f"NO NEW ENTRIES {symbol}: live feed gap invalidated minute-bar history", flush=True)
            bars.append(bar)
            if not test_mode:
                _write_heartbeat(cfg.data_dir / "engine_heartbeat.json", status="running", symbol=symbol,
                                 last_bar=bar.timestamp, open_positions=len(account.positions))
            now = bar.timestamp + timedelta(minutes=1)
            # Refresh option marks for existing simulated positions before evaluating a new entry.
            positions_mark_fresh = True
            for position_data in list(account.positions):
                if position_data["underlying"] != symbol:
                    continue
                try:
                    quote = _option_marks(client, [position_data])[0]
                except Exception as exc:
                    print(f"EXIT HOLD {position_data['symbol']}: option quote request failed ({type(exc).__name__}); review required", flush=True)
                    positions_mark_fresh = False
                    key = str(position_data["trade_id"])
                    if key not in position_alerted:
                        record_critical_alert("paper_position_management_failed", mode="PAPER",
                            details={"trade_id": key, "option_symbol": position_data["symbol"],
                                     "underlying": symbol, "error_type": type(exc).__name__}, data_dir=cfg.data_dir)
                        position_alerted.add(key)
                    continue
                if not _fresh_quote(quote, datetime.now(timezone.utc)):
                    print(f"EXIT HOLD {position_data['symbol']}: option quote stale; review required", flush=True)
                    positions_mark_fresh = False
                    key = str(position_data["trade_id"])
                    if key not in position_alerted:
                        record_critical_alert("paper_position_management_failed", mode="PAPER",
                            details={"trade_id": key, "option_symbol": position_data["symbol"],
                                     "underlying": symbol, "reason": "stale_or_invalid_option_quote"}, data_dir=cfg.data_dir)
                        position_alerted.add(key)
                    continue
                position_alerted.discard(str(position_data["trade_id"]))
                position_data["last_bid"] = quote.bid
                account.unrealized_pnl = _unrealized_pnl(account, executor)
                position = _position_from_record(position_data)
                daily_pnl = _daily_realized(account, now) + account.unrealized_pnl
                current_equity = max(0.0, account.starting_capital + account.deposits -
                                    account.withdrawals + account.realized_pnl + account.unrealized_pnl)
                why = ("end_of_session_close" if now >= session_close.astimezone(timezone.utc) - timedelta(minutes=1)
                       else exit_reason(position, underlying=bar.close, option_bid=quote.bid, now=now,
                                        daily_loss_hit=daily_pnl <= -current_equity * float(strategy.risk["max_daily_loss_fraction"])))
                if why:
                    fill = executor.fill(quote, "SELL", position.quantity, why, now)
                    account.ledger[-1].update({"strategy_id": position_data.get("strategy_id"),
                                               "strategy_version": position_data["strategy_version"],
                                               "strategy_hash": position_data["strategy_hash"]})
                    realized = realized_option_pnl(entry_fill_price=position.entry_price, exit_fill_price=fill.fill_price,
                        quantity=position.quantity, entry_fees=float(position_data["entry_fees"]), exit_fees=fill.fees)
                    account.realized_pnl += realized
                    account.ledger.append({"type": "paper_close", "trade_id": position.trade_id,
                                           "realized_pnl": realized, "actual_realized_pnl": realized,
                                           "planned_risk_dollars": position_data.get("planned_risk_dollars"),
                                           "modeled_risk_dollars": position_data.get("modeled_risk_dollars"),
                                           "premium_exposure_dollars": position_data.get("premium_exposure_dollars"),
                                           "actual_r_multiple": realized / max(float(position_data.get("modeled_risk_dollars", 0)), 1e-9),
                                           "strategy_id": position_data.get("strategy_id"),
                                           "strategy_version": position_data["strategy_version"],
                                           "strategy_hash": position_data["strategy_hash"],
                                           "reason": why, "timestamp": now.isoformat()})
                    account.positions.remove(position_data)
                    account.unrealized_pnl = _unrealized_pnl(account, executor)
                    print(f"PAPER EXIT {symbol} {position.symbol} {why}; net=${realized:.2f}", flush=True)
                else:
                    position_data["mfe"] = position.mfe
                    position_data["mae"] = position.mae
                    account.unrealized_pnl = _unrealized_pnl(account, executor)
            save_account(account_path, account)

            data_issues: list[str] = []
            try:
                latest = quotes.snapshots([symbol])
            except Exception as exc:
                latest = []
                data_issues.append(f"underlying_quote_request_failed:{type(exc).__name__}")
            quote_check_now = datetime.now(timezone.utc)
            quote_fresh = len(latest) == 1 and _fresh_underlying_quote(latest[0], quote_check_now)
            if not quote_fresh:
                age = ((quote_check_now - latest[0].timestamp).total_seconds() if latest else None)
                age_text = f" age={age:.2f}s" if age is not None else ""
                print(f"NO TRADE {symbol}: underlying quote is missing, stale, or timestamped ahead of local time.{age_text}", flush=True)
                if not any(issue.startswith("underlying_quote_request_failed") for issue in data_issues):
                    data_issues.append("underlying_quote_missing_or_stale")
            option_chain: dict[str, list[OptionQuote]] = {symbol: []}
            from .strategy_engine import evaluate_underlying
            signal = evaluate_underlying(bars, active_strategy,
                opening_range_start=test_start if test_mode else None)
            entry_window = calendar.entry_window_ok(now, active_strategy.entry_window_et["start"], active_strategy.entry_window_et["end"])
            if signal and entry_window and session_data_valid and quote_fresh:
                try:
                    option_chain[symbol] = fetch_option_quotes(client, underlying=symbol, spot=bar.close, now=now,
                        min_dte=int(active_strategy.rules["min_dte"]), max_dte=int(active_strategy.rules["max_dte"]))
                except Exception as exc:
                    data_issues.append(f"option_chain_request_failed:{type(exc).__name__}")
            state = RiskState(
                equity=max(0.0, account.starting_capital + account.deposits - account.withdrawals +
                           account.realized_pnl + account.unrealized_pnl),
                daily_pnl=_daily_realized(account, now) + account.unrealized_pnl,
                weekly_pnl=_weekly_realized(account, now) + account.unrealized_pnl,
                consecutive_losses=_consecutive_losses(account), trades_today=_trades_today(account, now),
                open_positions=len(account.positions),
                open_underlyings=tuple(str(position["underlying"]) for position in account.positions))
            records = decision_cycle(strategy=active_strategy, strategy_path=root / "strategy.txt",
                manifest_path=root / "active_strategy.json", bars_by_symbol={symbol: list(bars)},
                option_chain_by_symbol=option_chain, risk_state=state, now=now,
                data_fresh=session_data_valid and quote_fresh, account_certain=positions_mark_fresh,
                reconciled=True, protection_verified=True,
                market_open=calendar.is_open(now),
                within_entry_window=entry_window,
                decision_log=decision_log, kill_switch_path=root / "KILL_SWITCH", paper_execution=True,
                paper_test_mode=test_mode, opening_range_start=test_start if test_mode else None,
                data_issues=data_issues)
            for decision in records:
                if decision.decision != "PAPER_ENTRY":
                    if decision.underlying == symbol and (signal or decision.reason in {"kill_switch_active", "safety_gate_blocked"}):
                        print(f"NO TRADE {symbol}: {decision.reason}", flush=True)
                    continue
                selected = next((q for q in option_chain[symbol] if q.symbol == decision.diagnostics.get("option_candidate")), None)
                if selected is None:
                    print(f"NO TRADE {symbol}: selected contract quote disappeared", flush=True)
                    continue
                quantity = int(decision.diagnostics["quantity"])
                fill = executor.fill(selected, "BUY", quantity, "strategy_entry", now)
                account.ledger[-1].update({"strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
                                           "strategy_hash": strategy.sha256,
                                           "planned_risk_dollars": decision.diagnostics["planned_risk_dollars"],
                                           "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                                           "premium_exposure_dollars": decision.diagnostics["premium_exposure_dollars"]})
                signal_now = evaluate_underlying(bars, active_strategy,
                    opening_range_start=test_start if test_mode else None)
                if signal_now is None:
                    raise RuntimeError("Signal disappeared during paper entry; stop and review")
                account.positions.append({
                    "trade_id": fill.trade_id, "symbol": selected.symbol, "underlying": symbol,
                    "right": selected.right, "quantity": quantity, "entry_price": fill.fill_price,
                    "entry_fees": fill.fees, "entry_underlying": bar.close,
                    "stop_underlying": signal_now.stop_price, "target_underlying": signal_now.target_price,
                    "entry_time": now.isoformat(), "max_holding_minutes": int(strategy.rules["max_holding_minutes"]),
                    "initial_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                    "planned_risk_dollars": decision.diagnostics["planned_risk_dollars"],
                    "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                    "premium_exposure_dollars": decision.diagnostics["premium_exposure_dollars"],
                    "strategy_id": strategy.strategy_id,
                    "strategy_version": strategy.version, "strategy_hash": strategy.sha256,
                    "mfe": 0.0, "mae": 0.0, "last_bid": selected.bid,
                })
                account.unrealized_pnl = _unrealized_pnl(account, executor)
                save_account(account_path, account)
                print(f"PAPER ENTRY {symbol} {selected.symbol} x{quantity} @ ask+slippage ${fill.fill_price:.2f}", flush=True)

    try:
        remaining = max(1.0, (session_close.astimezone(timezone.utc) - now).total_seconds())
        async with asyncio.timeout(remaining):
            await consume()
    except TimeoutError:
        print("PAPER session ended at the equity close.", flush=True)
        if account.positions:
            marks = _option_marks(client, list(account.positions))
            mark_by_symbol = {quote.symbol: quote for quote in marks}
            close_now = datetime.now(timezone.utc)
            for position_data in list(account.positions):
                quote = mark_by_symbol.get(position_data["symbol"])
                if not quote or not _fresh_quote(quote, close_now):
                    print(f"REVIEW REQUIRED {position_data['symbol']}: no fresh closing quote; simulated position remains open", flush=True)
                    record_critical_alert("paper_position_unresolved_at_session_close", mode="PAPER",
                        details={"trade_id": str(position_data["trade_id"]), "option_symbol": position_data["symbol"],
                                 "underlying": position_data["underlying"]}, data_dir=cfg.data_dir)
                    continue
                fill = executor.fill(quote, "SELL", int(position_data["quantity"]), "end_of_session_close", close_now)
                account.ledger[-1].update({"strategy_id": position_data.get("strategy_id"),
                                           "strategy_version": position_data["strategy_version"],
                                           "strategy_hash": position_data["strategy_hash"]})
                realized = realized_option_pnl(entry_fill_price=float(position_data["entry_price"]),
                    exit_fill_price=fill.fill_price, quantity=int(position_data["quantity"]),
                    entry_fees=float(position_data["entry_fees"]), exit_fees=fill.fees)
                account.realized_pnl += realized
                account.ledger.append({"type": "paper_close", "trade_id": position_data["trade_id"],
                                       "realized_pnl": realized, "actual_realized_pnl": realized,
                                       "planned_risk_dollars": position_data.get("planned_risk_dollars"),
                                       "modeled_risk_dollars": position_data.get("modeled_risk_dollars"),
                                       "premium_exposure_dollars": position_data.get("premium_exposure_dollars"),
                                       "actual_r_multiple": realized / max(float(position_data.get("modeled_risk_dollars", 0)), 1e-9),
                                       "strategy_id": position_data.get("strategy_id"),
                                       "strategy_version": position_data["strategy_version"],
                                       "strategy_hash": position_data["strategy_hash"],
                                       "reason": "end_of_session_close",
                                       "timestamp": close_now.isoformat()})
                account.positions.remove(position_data)
                print(f"PAPER EXIT {position_data['underlying']} {position_data['symbol']} end_of_session_close; net=${realized:.2f}", flush=True)
            account.unrealized_pnl = _unrealized_pnl(account, executor)
            save_account(account_path, account)


def _option_marks(client: TastytradeClient, positions: list[dict]) -> list[OptionQuote]:
    symbols = [position["symbol"] for position in positions]
    query = urlencode({"equity-option[]": ",".join(symbols)})
    items = client.quotes(query).get("data", {}).get("items", [])
    indexed = {item.get("symbol"): item for item in items}
    out = []
    for position in positions:
        item = indexed.get(position["symbol"])
        if not item:
            out.append(OptionQuote(position["symbol"], position["underlying"], datetime.fromisoformat(position["entry_time"]),
                0.0, position["right"], 0.0, 0.0))
            continue
        stamp = datetime.fromisoformat(item["updated-at"].replace("Z", "+00:00"))
        out.append(OptionQuote(position["symbol"], position["underlying"], datetime.fromisoformat(position["entry_time"]),
            0.0, position["right"], float(item.get("bid") or 0), float(item.get("ask") or 0),
            quote_timestamp=stamp))
    return out


def _fresh_quote(quote: OptionQuote, now: datetime) -> bool:
    return quote.quote_timestamp is not None and 0 <= (now - quote.quote_timestamp).total_seconds() <= 30 and quote.bid > 0


def _fresh_underlying_quote(quote: CachedQuote, now: datetime) -> bool:
    age = (now - quote.timestamp).total_seconds()
    return 0 <= age <= 20 and quote.bid > 0 and quote.ask >= quote.bid


def _unrealized_pnl(account: PaperAccount, executor: PaperExecutor) -> float:
    total = 0.0
    for position in account.positions:
        quantity = int(position["quantity"])
        exit_price = max(0.0, float(position.get("last_bid", position["entry_price"])) -
                         executor.slippage_per_contract / 100)
        total += ((exit_price - float(position["entry_price"])) * quantity * 100 -
                  float(position.get("entry_fees", 0)) - executor.fees.estimate(quantity))
    return total


def _position_from_record(record: dict) -> Position:
    return Position(record["trade_id"], record["symbol"], record["underlying"], record["right"],
        int(record["quantity"]), float(record["entry_price"]), float(record["entry_underlying"]),
        float(record["stop_underlying"]), float(record["target_underlying"]),
        datetime.fromisoformat(record["entry_time"]), int(record["max_holding_minutes"]),
        float(record["initial_risk_dollars"]), record["strategy_version"], record["strategy_hash"],
        fees=float(record.get("entry_fees", 0)), mfe=float(record.get("mfe", 0)), mae=float(record.get("mae", 0)))


def _closed(account: PaperAccount) -> list[dict]:
    return [item for item in account.ledger if item.get("type") == "paper_close"]


def _daily_realized(account: PaperAccount, now: datetime) -> float:
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    return sum(float(item["realized_pnl"]) for item in _closed(account)
               if datetime.fromisoformat(item["timestamp"]).astimezone(ZoneInfo("America/New_York")).date() == today)


def _weekly_realized(account: PaperAccount, now: datetime) -> float:
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    week_start = today - timedelta(days=today.weekday())
    return sum(float(item["realized_pnl"]) for item in _closed(account)
               if week_start <= datetime.fromisoformat(item["timestamp"]).astimezone(ZoneInfo("America/New_York")).date() <= today)


def _consecutive_losses(account: PaperAccount) -> int:
    count = 0
    for item in reversed(_closed(account)):
        if float(item["realized_pnl"]) >= 0:
            break
        count += 1
    return count


def _trades_today(account: PaperAccount, now: datetime) -> int:
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    return sum(1 for item in account.ledger if item.get("type") == "fill" and item.get("side") == "BUY" and
               datetime.fromisoformat(item["timestamp"]).astimezone(ZoneInfo("America/New_York")).date() == today)
