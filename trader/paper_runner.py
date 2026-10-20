from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .calendar import MarketCalendar
from .config import AppConfig, Mode
from .engine import decision_cycle
from .broker_fees import TastytradeOptionFees
from .history_providers import create_history_provider
from .logging_utils import atomic_replace
from .market_data import CachedQuote, ProductionMarketData, fetch_option_quotes, live_minute_bars
from .models import Bar, OptionQuote, Position
from .notifications import record_critical_alert
from .paper_execution import PaperExecutor, realized_option_pnl
from .portfolio import PaperAccount, load_or_initialize, save_account
from .position_manager import exit_reason
from .risk_engine import RiskState
from .strategy_engine import atr as calculate_atr, ema as calculate_ema, evaluate_underlying, opening_range
from .strategy_loader import Strategy, verify_manifest
from .tastytrade_client import TastytradeClient

MAX_ENGINE_RESTARTS = 20
# How long the first symbol to qualify in a minute waits for other symbols' bars before ranking.
ENTRY_ARBITRATION_SECONDS = 3.0
MAX_CLOCK_OFFSET_SECONDS = 2.0
# A one-minute live-feed gap is backfilled from history; the provider can lag a few seconds behind.
GAP_RECOVERY_ATTEMPTS = 3
GAP_RECOVERY_RETRY_SECONDS = 5.0
MAX_QUOTE_FUTURE_SECONDS = 2.5


def _session_logger(data_dir: Path, *, test_mode: bool = False) -> logging.Logger:
    """Log session lifecycle and failures to console and a rotating local file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    name = "optio_bot.paper_test" if test_mode else "optio_bot.paper"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = (data_dir / "paper_test_session.log" if test_mode
                else data_dir / "paper_session.log")
    normalized_log_path = os.path.normcase(str(log_path.resolve()))
    if not any(os.path.normcase(str(getattr(handler, "baseFilename", ""))) == normalized_log_path
               for handler in logger.handlers):
        formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s")
        formatter.converter = time.gmtime
        file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5,
                                           encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    if not any(getattr(handler, "_optio_console", False) for handler in logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler._optio_console = True  # type: ignore[attr-defined]
        console_formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
        console_formatter.converter = time.gmtime
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    return logger


def _close_session_log_file(logger: logging.Logger) -> None:
    """Release the session log before Windows test/workspace cleanup or a later relaunch."""
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.flush()
            handler.close()


def _write_heartbeat(path: Path, *, status: str, symbol: str, mode: str = "PAPER",
                     last_bar: datetime | None = None, open_positions: int = 0,
                     error: str | None = None,
                     startup_deadline: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_state = status.strip().upper()
    payload = {"status": status.lower(), "lifecycle_state": normalized_state,
               "expected_running": normalized_state not in {"NOT_STARTED", "STOPPED"},
               "mode": mode, "symbol": symbol, "pid": os.getpid(),
               "updated_at_utc": datetime.now(timezone.utc).isoformat(),
               "last_bar_at_utc": last_bar.isoformat() if last_bar else None,
               "open_positions": open_positions, "error": error}
    if normalized_state in {"STARTING", "RECOVERING"}:
        deadline = startup_deadline or (datetime.now(timezone.utc) + timedelta(minutes=5))
        payload["startup_deadline_utc"] = deadline.astimezone(timezone.utc).isoformat()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    atomic_replace(temporary, path)


def _ledger_position_count(data_dir: Path) -> int:
    """Open positions in the paper ledger, for heartbeats written outside the session loop (a failed or
    stopped session must not report 0 while positions are still open)."""
    try:
        return len(json.loads((data_dir / "paper_account.json").read_text(encoding="utf-8")).get("positions", []))
    except (OSError, ValueError, AttributeError):
        return 0


def _write_recovery_checkpoint(data_dir: Path, *, symbol: str, trading_date: str,
                               provider: str, timestamp: datetime) -> Path:
    """Persist validated progress, while still revalidating the full session on restart."""
    path = data_dir / f"history_recovery_state-{symbol}.json"
    payload = {"symbol": symbol, "trading_date_et": trading_date, "provider": provider,
               "last_validated_timestamp_utc": timestamp.astimezone(timezone.utc).isoformat(),
               "validated_at_utc": datetime.now(timezone.utc).isoformat()}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    atomic_replace(temporary, path)
    return path


def _set_engine_expected(data_dir: Path, *, expected: bool, state: str, symbol: str) -> None:
    path = data_dir / "engine_expectation.json"
    payload = {"expected_running": expected, "state": state, "symbol": symbol,
               "updated_at_utc": datetime.now(timezone.utc).isoformat()}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    atomic_replace(temporary, path)


def _write_recovery_report(data_dir: Path, report: dict) -> Path:
    directory = data_dir / "recovery_reports"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}-{report['symbol']}.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    atomic_replace(temporary, path)
    return path


def _validated_session_history(rows: list[dict], *, session_open: datetime,
                               last_completed_bar: datetime, audit: dict | None = None) -> list[Bar]:
    """Convert and validate a complete, contiguous session history for a late PAPER start."""
    if session_open.tzinfo is None or last_completed_bar.tzinfo is None:
        raise ValueError("Historical session bounds must be timezone-aware")
    expected: list[datetime] = []
    stamp = session_open.astimezone(timezone.utc)
    end = last_completed_bar.astimezone(timezone.utc)
    while stamp <= end:
        expected.append(stamp)
        stamp += timedelta(minutes=1)
    if not expected:
        return []

    parsed: dict[datetime, Bar] = {}
    duplicates = 0
    out_of_order = 0
    previous: datetime | None = None
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
            timestamp = timestamp.astimezone(timezone.utc)
            bar = Bar(timestamp, float(row["open"]), float(row["high"]), float(row["low"]),
                      float(row["close"]), float(row["volume"]),
                      float(row["vwap"]) if row.get("vwap") is not None else None)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError("Historical candle bootstrap returned an invalid candle") from None
        if timestamp < expected[0] or timestamp > expected[-1]:
            continue
        if previous is not None and timestamp < previous:
            out_of_order += 1
        previous = timestamp
        if (bar.open <= 0 or bar.close <= 0 or bar.volume < 0 or
                bar.high < max(bar.open, bar.close, bar.low) or
                bar.low > min(bar.open, bar.close, bar.high)):
            raise RuntimeError(f"Historical candle bootstrap returned invalid OHLCV at {timestamp.isoformat()}")
        if timestamp in parsed:
            duplicates += 1
            if parsed[timestamp] != bar:
                if audit is not None:
                    audit.update({"duplicate_candle_count": duplicates, "out_of_order_count": out_of_order})
                raise RuntimeError(f"Historical candle bootstrap returned conflicting duplicate minute {timestamp.isoformat()}")
            continue
        parsed[timestamp] = bar

    missing = [stamp for stamp in expected if stamp not in parsed]
    if audit is not None:
        audit.update({"expected_candle_count": len(expected), "received_candle_count": len(parsed),
                      "missing_candle_count": len(missing),
                      "first_missing_timestamp_utc": missing[0].isoformat() if missing else None,
                      "duplicate_candle_count": int(audit.get("duplicate_candle_count", 0)) + duplicates,
                      "out_of_order_count": int(audit.get("out_of_order_count", 0)) + out_of_order,
                      "missing_timestamps_utc": [stamp.isoformat() for stamp in missing]})
    if missing:
        raise RuntimeError(
            "Historical candle bootstrap incomplete; blocking entries. "
            f"Expected {len(expected)} one-minute bars from {expected[0].isoformat()} "
            f"through {expected[-1].isoformat()}, received {len(parsed)}; "
            f"first missing minute {missing[0].isoformat()}"
        )
    if out_of_order:
        raise RuntimeError(f"Historical candle bootstrap returned {out_of_order} out-of-order bars")
    return [parsed[stamp] for stamp in expected]


def _startup_history_metrics(bars: list[Bar], strategy: Strategy, *, session_open: datetime) -> dict:
    opening = opening_range(bars, strategy.opening_range_minutes, start_at=session_open)
    required = max(int(strategy.rules["trend_ema_slow"]), int(strategy.rules["atr_period"])) + 2
    metrics = {
        "indicator_bars_required": required,
        "indicator_bars_received": len(bars),
        "indicator_warmup_status": "VERIFIED" if len(bars) >= required else "INCOMPLETE",
        "opening_range_status": "VERIFIED" if opening else "INCOMPLETE",
        "opening_range_start": session_open.isoformat(),
        "opening_range_end": (session_open + timedelta(minutes=strategy.opening_range_minutes)).isoformat(),
        "opening_range_bar_count": min(len(bars), strategy.opening_range_minutes),
        "opening_range_duration_minutes": min(len(bars), strategy.opening_range_minutes),
        "opening_range_high": opening[0] if opening else None,
        "opening_range_low": opening[1] if opening else None,
    }
    if opening and len(bars) >= required:
        values = [bar.close for bar in bars]
        metrics["indicator_reference"] = {
            "ema_fast": calculate_ema(values, int(strategy.rules["trend_ema_fast"])),
            "ema_slow": calculate_ema(values, int(strategy.rules["trend_ema_slow"])),
            "atr": calculate_atr(bars, int(strategy.rules["atr_period"])),
        }
    return metrics


def _classify_live_transition(previous: datetime, current: datetime) -> str:
    delta = current - previous
    if delta == timedelta(minutes=1):
        return "CONTIGUOUS"
    if delta > timedelta(minutes=1):
        return "GAP"
    return "DUPLICATE_OR_OUT_OF_ORDER"


def _same_market_bar(left: Bar, right: Bar) -> bool:
    return (left.timestamp == right.timestamp and left.open == right.open and left.high == right.high and
            left.low == right.low and left.close == right.close and left.volume == right.volume)


def _is_connection_failure(exc: BaseException) -> bool:
    """True when the failure (or anything in its cause chain) is a network/feed drop worth retrying."""
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:  # streaming extra not installed
        WebSocketException = ConnectionError  # type: ignore[misc,assignment]
    from .tastytrade_client import TransportError
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (ConnectionError, OSError, TransportError, WebSocketException)):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _session_symbols(strategy: Strategy, symbols: tuple[str, ...] | None) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(symbols or strategy.universe))
    outside = [symbol for symbol in chosen if symbol not in strategy.universe]
    if outside:
        raise ValueError(f"{', '.join(outside)} not in the active strategy universe")
    return chosen


def run_paper_session(*, root: Path, cfg: AppConfig, strategy: Strategy, symbols: tuple[str, ...] | None = None,
                      max_restarts: int = MAX_ENGINE_RESTARTS, profile=None) -> None:
    """Run the session, restarting it after feed/network failures while the market is open.

    A restart re-runs the full startup path: manifest check, clock check, full-session history
    recovery and carried-position resume. Any other failure (configuration, data conflicts,
    strategy errors) still stops the engine for review.
    """
    symbols = _session_symbols(strategy, symbols)
    label = ",".join(symbols)
    restarts = 0
    while True:
        try:
            _run_paper_session_once(root=root, cfg=cfg, strategy=strategy, symbols=symbols, profile=profile)
            return
        except Exception as exc:
            now = datetime.now(timezone.utc)
            if not _is_connection_failure(exc) or restarts >= max_restarts or not MarketCalendar().is_open(now):
                raise
            restarts += 1
            delay = min(10 * restarts, 60)
            logger = _session_logger(cfg.data_dir)
            logger.warning("Restarting paper session after connection failure %d/%d in %ds symbols=%s error=%s: %s",
                           restarts, max_restarts, delay, label, type(exc).__name__, exc)
            print(f"RESTARTING paper session in {delay}s after {type(exc).__name__} ({restarts}/{max_restarts})", flush=True)
            # Report "recovering" so the watchdog treats the gap as a startup, not a dead engine.
            _set_engine_expected(cfg.data_dir, expected=True, state="RECOVERING", symbol=label)
            _write_heartbeat(cfg.data_dir / "engine_heartbeat.json", status="recovering", symbol=label,
                             startup_deadline=now + timedelta(seconds=delay, minutes=5))
            _close_session_log_file(logger)
            time.sleep(delay)


def _run_paper_session_once(*, root: Path, cfg: AppConfig, strategy: Strategy, symbols: tuple[str, ...],
                            profile=None) -> None:
    logger = _session_logger(cfg.data_dir)
    heartbeat = cfg.data_dir / "engine_heartbeat.json"
    label = ",".join(symbols)
    logger.info("Paper session launch requested symbols=%s strategy=%s version=%s hash=%s",
                label, strategy.strategy_id, strategy.version, strategy.sha256)
    try:
        if cfg.mode.value != "PAPER":
            raise RuntimeError("paper-session is only available when TRADING_MODE=PAPER")
        cfg.require_market_data_credentials()
        verify_manifest(root / "strategy.txt", root / "active_strategy.json")
        _set_engine_expected(cfg.data_dir, expected=True, state="STARTING", symbol=label)
        _write_heartbeat(heartbeat, status="starting", symbol=label)
        asyncio.run(_run(root=root, cfg=cfg, strategy=strategy, symbols=symbols, profile=profile))
    except KeyboardInterrupt:
        logger.info("Paper session stopped by operator symbols=%s", label)
        _set_engine_expected(cfg.data_dir, expected=False, state="STOPPED", symbol=label)
        _write_heartbeat(heartbeat, status="stopped", symbol=label, open_positions=_ledger_position_count(cfg.data_dir))
        raise
    except Exception as exc:
        logger.exception("Paper session failed symbols=%s error_type=%s", label, type(exc).__name__)
        try:
            _set_engine_expected(cfg.data_dir, expected=True, state="FAILED", symbol=label)
            _write_heartbeat(heartbeat, status="failed", symbol=label, error=type(exc).__name__,
                             open_positions=_ledger_position_count(cfg.data_dir))
            record_critical_alert("paper_engine_runtime_failure", mode="PAPER",
                                  details={"symbols": list(symbols), "error_type": type(exc).__name__,
                                           "error": str(exc)}, data_dir=cfg.data_dir)
        except Exception:
            logger.exception("Could not persist failed-session state symbols=%s", label)
        raise
    else:
        logger.info("Paper session ended normally symbols=%s", label)
        _set_engine_expected(cfg.data_dir, expected=False, state="STOPPED", symbol=label)
        _write_heartbeat(heartbeat, status="stopped", symbol=label, open_positions=_ledger_position_count(cfg.data_dir))
    finally:
        _close_session_log_file(logger)


def run_paper_test_session(*, root: Path, cfg: AppConfig, strategy: Strategy, symbols: tuple[str, ...] | None = None,
                           test_capital: float = 100_000) -> None:
    """Run an isolated paper-only afternoon test with a rolling opening range."""
    logger = _session_logger(cfg.data_dir, test_mode=True)
    try:
        if cfg.mode is Mode.LIVE:
            raise RuntimeError("paper-test-session is forbidden in LIVE mode")
        if cfg.mode.value != "PAPER":
            raise RuntimeError("paper-test-session requires TRADING_MODE=PAPER")
        symbols = _session_symbols(strategy, symbols)
        if test_capital <= 0:
            raise ValueError("test capital must be positive")
        cfg.require_market_data_credentials()
        verify_manifest(root / "strategy.txt", root / "active_strategy.json")
        logger.info("Paper test launch requested symbols=%s strategy=%s version=%s hash=%s",
                    ",".join(symbols), strategy.strategy_id, strategy.version, strategy.sha256)
        asyncio.run(_run(root=root, cfg=cfg, strategy=strategy, symbols=symbols,
                         test_mode=True, test_capital=test_capital))
    except KeyboardInterrupt:
        logger.info("Paper test stopped by operator")
        raise
    except Exception as exc:
        logger.exception("Paper test failed error_type=%s", type(exc).__name__)
        raise
    finally:
        _close_session_log_file(logger)


@dataclass
class _SymbolState:
    """Per-underlying session state. All symbols share one account, risk limits and live feed."""
    symbol: str
    report: dict
    bars: list[Bar] = field(default_factory=list)
    session_started: bool = False
    data_valid: bool = True
    history_end: datetime | None = None
    report_path: Path | None = None

    def persist(self, data_dir: Path) -> Path:
        if self.report_path is None:
            self.report_path = _write_recovery_report(data_dir, self.report)
        else:
            temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self.report, indent=2, default=str) + "\n", encoding="utf-8")
            atomic_replace(temporary, self.report_path)
        return self.report_path


class _EntryArbiter:
    """Rank same-minute entry candidates across symbols so the best ones get the limited slots.

    The first symbol to qualify in a minute waits briefly for the others' bars for that minute, then
    the candidates are ranked by (expected net R, trend efficiency) and the top ones are approved.
    Callers must still re-check the live slot count synchronously right before filling.
    """

    def __init__(self, available_slots, wait_seconds: float):
        self._available = available_slots
        self._wait = wait_seconds
        self._minutes: dict[datetime, dict] = {}

    async def approve(self, minute: datetime, symbol: str, score: tuple[float, float]) -> bool:
        slot = self._minutes.get(minute)
        if slot is not None and slot["decided"]:
            return self._available() > 0  # arrived after the ranked batch; take any slot still free
        if slot is not None:
            slot["candidates"][symbol] = score
            await slot["done"].wait()
            return symbol in slot["winners"]
        slot = self._minutes[minute] = {"candidates": {symbol: score}, "decided": False,
                                        "winners": set(), "done": asyncio.Event()}
        for old in [stamp for stamp in self._minutes if stamp < minute - timedelta(minutes=5)]:
            del self._minutes[old]
        await asyncio.sleep(self._wait)
        ranked = sorted(slot["candidates"], key=lambda name: slot["candidates"][name], reverse=True)
        slot["winners"] = set(ranked[:max(0, self._available())])
        slot["decided"] = True
        slot["done"].set()
        return symbol in slot["winners"]


async def _run(*, root: Path, cfg: AppConfig, strategy: Strategy, symbols: tuple[str, ...],
               test_mode: bool = False, test_capital: float = 100_000, live=None, profile=None) -> None:
    """The one strategy loop for PAPER and LIVE.

    `live` is None for PAPER (simulated fills, unchanged). For LIVE it is a `live_execution.LiveTrader`
    that executes the same entry/exit decisions on the broker at the points PAPER calls PaperExecutor.
    `profile` is None for the original account. For an account profile (`account_profiles.AccountProfile`,
    PAPER only) it sets the per-trade sizing budget to the remaining aggregate allocation and re-checks the
    aggregate cap at the fill; the strategy's analysis and decisions are otherwise identical.
    """
    if live is not None and test_mode:
        raise RuntimeError("paper-test-session cannot run with LIVE execution")
    if profile is not None and (live is not None or test_mode):
        raise RuntimeError("account profiles run only in a normal PAPER session")
    mode_label = "LIVE" if live is not None else "PAPER"
    logger = _session_logger(cfg.data_dir, test_mode=test_mode)
    label = ",".join(symbols)
    logger.info("Session initialization started mode=%s test_mode=%s symbols=%s provider=%s",
                mode_label, test_mode, label, cfg.historical_data_provider)
    now = datetime.now(timezone.utc)
    calendar = MarketCalendar()
    market_tz = ZoneInfo("America/New_York")
    bounds = calendar.session_bounds(now.astimezone(market_tz).date())
    if not bounds:
        raise RuntimeError("There is no equity session today")
    session_open, session_close = bounds
    heartbeat_path = cfg.data_dir / "engine_heartbeat.json"
    startup_deadline = max(session_open.astimezone(timezone.utc) + timedelta(minutes=5),
                           datetime.now(timezone.utc) + timedelta(minutes=5 + len(symbols)))
    if not test_mode:
        _set_engine_expected(cfg.data_dir, expected=True, state="RECOVERING", symbol=label)
        _write_heartbeat(heartbeat_path, status="recovering", symbol=label, startup_deadline=startup_deadline)
    if now >= session_close.astimezone(timezone.utc):
        raise RuntimeError("Today's equity session has already closed")

    # Also the opening-range anchor: the calendar's actual open (or the test's rolling start), rather
    # than the signal engine's 09:30 ET fallback, so history recovery and live signals agree.
    test_start = now.replace(second=0, microsecond=0) if test_mode else session_open.astimezone(timezone.utc)
    active_strategy = strategy
    if test_mode:
        entry_start = test_start.astimezone(market_tz) + timedelta(minutes=strategy.opening_range_minutes)
        entry_end = session_close.astimezone(market_tz)
        if entry_start >= entry_end:
            raise RuntimeError("Not enough time remains to form a rolling range and test an entry")
        active_strategy = replace(strategy, entry_window_et={"start": entry_start.strftime("%H:%M"),
                                                             "end": entry_end.strftime("%H:%M")})

    if live is not None:
        account_path, account = live.account_path, live.account  # LIVE ledger of broker fills, data/live/
    else:
        account_path = cfg.data_dir / ("paper_test_account.json" if test_mode else "paper_account.json")
        account = load_or_initialize(account_path, test_capital if test_mode else cfg.starting_cash)
    if test_mode and account.starting_capital != test_capital:
        raise RuntimeError(f"Existing test ledger uses ${account.starting_capital:.2f}; pass the same --test-capital or archive {account_path.name}")
    flatten_at_close = bool(strategy.execution["flatten_at_session_close"])
    # Open positions at startup are resumed and managed, whether or not flatten-at-close is on. (The old
    # behaviour refused to start when flatten was on, which bricked a mid-session restart and left the
    # positions unmanaged — strictly worse. With flatten on, any still open at the bell is closed then.)
    carried = sorted({str(position["underlying"]) for position in account.positions} - set(symbols))
    if carried:
        # A carried position must stay managed even if its underlying is outside this run's symbol list.
        symbols = tuple(symbols) + tuple(carried)
        label = ",".join(symbols)
        logger.warning("Adding %s to the session to manage carried positions", ",".join(carried))
    if account.positions:
        logger.warning("Resuming %d carried %s position(s) from an earlier session", len(account.positions), mode_label)
        print(f"RESUMING {len(account.positions)} carried {mode_label.lower()} position(s); stops/targets are from the entry session", flush=True)

    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    clock_offset = client.clock_offset_seconds()
    if abs(clock_offset) > MAX_CLOCK_OFFSET_SECONDS:
        raise RuntimeError(
            f"This computer's clock is {abs(clock_offset):.1f}s {'behind' if clock_offset > 0 else 'ahead of'} tastytrade's. "
            "Session times and quote-freshness checks depend on it. In an administrator PowerShell run: "
            "Set-Service w32time -StartupType Automatic; Start-Service w32time; w32tm /resync /force")
    logger.info("Local clock offset vs tastytrade=%.3fs", clock_offset)
    token_data = client.quote_token().get("data", {})
    if not token_data.get("token") or not token_data.get("dxlink-url"):
        raise RuntimeError("tastytrade quote-token response lacked token or dxlink-url")
    logger.info("Authenticated market data session established symbols=%s dxlink_endpoint_present=%s",
                label, bool(token_data.get("dxlink-url")))
    history_provider = create_history_provider(
        cfg.historical_data_provider, dxlink_url=token_data["dxlink-url"], quote_token=token_data["token"],
        massive_api_key=cfg.massive_api_key)

    def new_report(symbol: str) -> dict:
        return {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": mode_label, "symbol": symbol, "session_symbols": list(symbols),
            "trading_date_et": now.astimezone(market_tz).date().isoformat(),
            "market_session_start_et": session_open.isoformat(), "market_session_close_et": session_close.isoformat(),
            "current_time_et": datetime.now(timezone.utc).astimezone(market_tz).isoformat(),
            "provider": history_provider.name, "retrieval_method": "configured historical candle provider",
            "strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
            "strategy_hash": strategy.sha256, "risk_controls_status": "LOADED_FROM_VERIFIED_STRATEGY",
            "broker_order_submission": "ENABLED_LIVE" if live is not None else "DISABLED_IN_PAPER",
            "timezone": "America/New_York",
            "expected_full_session_candle_count": int((session_close - session_open).total_seconds() // 60),
            "indicator_bars_required": max(int(strategy.rules["trend_ema_slow"]), int(strategy.rules["atr_period"])) + 2,
            "indicator_bars_received": 0, "expected_candle_count": 0, "received_candle_count": 0,
            "missing_candle_count": 0, "duplicate_candle_count": 0, "out_of_order_count": 0,
            "indicator_warmup_status": "NOT_CHECKED", "opening_range_status": "NOT_CHECKED",
            "historical_to_live_transition_status": "PENDING", "final_readiness": "PENDING",
            "kill_switch_status": "ACTIVE" if (root / "KILL_SWITCH").exists() else "CLEAR",
            "execution_mode_status": ("LIVE_BROKER_EXECUTION" if live is not None
                                      else "PAPER_ONLY" if cfg.mode.value == "PAPER" and not cfg.live_enabled else "BLOCKED"),
            "local_clock_offset_seconds": round(clock_offset, 3),
            "paper_ledger_position_count": len(account.positions),
            "paper_ledger_state": "CARRIED_POSITIONS_RESUMED" if account.positions else "CLEAR",
        }

    states = {symbol: _SymbolState(symbol, new_report(symbol)) for symbol in symbols}
    for state in states.values():
        state.persist(cfg.data_dir)
    disabled: set[str] = set()

    def has_position(symbol: str) -> bool:
        if live is not None and symbol in live.pending_entries:
            return True
        return any(str(position["underlying"]) == symbol for position in account.positions)

    def disable(state: _SymbolState, exc: Exception) -> None:
        """Stop trading one symbol after a data problem that is not a shared-feed failure."""
        if has_position(state.symbol) or _is_connection_failure(exc):
            raise exc
        disabled.add(state.symbol)
        state.report.update({"final_readiness": "SYMBOL_DISABLED", "error_type": type(exc).__name__, "error": str(exc)})
        state.persist(cfg.data_dir)
        logger.error("Disabling %s for the rest of the session: %s: %s", state.symbol, type(exc).__name__, exc)
        print(f"SYMBOL DISABLED {state.symbol}: {exc}", flush=True)
        record_critical_alert("paper_symbol_disabled", mode="PAPER",
                              details={"symbol": state.symbol, "error_type": type(exc).__name__, "error": str(exc)},
                              data_dir=cfg.data_dir)
        if len(disabled) == len(states):
            raise RuntimeError("Every symbol in the session was disabled; see recovery reports") from exc

    first_live_bar = asyncio.Event()

    async def startup_heartbeat() -> None:
        while not first_live_bar.is_set():
            _write_heartbeat(heartbeat_path, status="recovering", symbol=label, startup_deadline=startup_deadline)
            try:
                await asyncio.wait_for(first_live_bar.wait(), timeout=30)
            except TimeoutError:
                pass

    async def bootstrap(state: _SymbolState) -> None:
        symbol, report = state.symbol, state.report
        last_completed_bar = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
        history_audit: dict = {}
        try:
            history_batch = await history_provider.fetch(
                symbol=symbol, start=session_open.astimezone(timezone.utc), end=last_completed_bar)
            history_audit.update(history_batch.audit)
            report.update({"provider": history_batch.provider,
                           "retrieval_method": history_batch.retrieval_method,
                           "history_retrieved_at_utc": history_batch.retrieved_at_utc.isoformat(),
                           "history_requested_start_utc": history_batch.requested_start_utc.isoformat(),
                           "history_requested_end_utc": history_batch.requested_end_utc.isoformat()})
            state.bars = _validated_session_history(list(history_batch.candles), session_open=session_open,
                                                    last_completed_bar=last_completed_bar, audit=history_audit)
            if state.bars:
                _write_recovery_checkpoint(cfg.data_dir, symbol=symbol,
                    trading_date=now.astimezone(market_tz).date().isoformat(), provider=history_batch.provider,
                    timestamp=state.bars[-1].timestamp)
            report.update(history_audit)
            report["last_recovered_candle_et"] = state.bars[-1].timestamp.astimezone(market_tz).isoformat() if state.bars else None
            metrics = _startup_history_metrics(state.bars, strategy, session_open=session_open.astimezone(timezone.utc))
            report.update(metrics)
            opening = metrics["opening_range_status"] == "VERIFIED"
            required_bars = int(metrics["indicator_bars_required"])
            # Indicator warm-up is not required here: the signal engine reports
            # "indicator_history_warming_up" until enough live bars arrive. Requiring it blocked any
            # start between the end of the opening range and the first full indicator window.
            if now.astimezone(market_tz) >= session_open + timedelta(minutes=strategy.opening_range_minutes) and not opening:
                raise RuntimeError("Recovered session history does not contain a complete opening range")
            report["final_readiness"] = "READY_FOR_LIVE_HANDOFF" if opening and len(state.bars) >= required_bars else "WARMING_UP"
            state.persist(cfg.data_dir)
        except Exception as exc:
            report.update(history_audit)
            report.update({"final_readiness": "BLOCKED", "error_type": type(exc).__name__,
                           "error": str(exc), "historical_to_live_transition_status": "NOT_REACHED"})
            path = state.persist(cfg.data_dir)
            raise RuntimeError(f"Paper startup blocked by historical recovery for {symbol}; report: {path}; {exc}") from exc
        state.session_started = bool(state.bars)
        state.history_end = state.bars[-1].timestamp if state.bars else None
        print(f"{mode_label} {symbol} history bootstrap verified {len(state.bars)} complete one-minute bars through "
              f"{state.bars[-1].timestamp.isoformat() if state.bars else 'session open'}", flush=True)

    decision_log = cfg.data_dir / ("paper_test_decisions.jsonl" if test_mode else "decisions.jsonl")
    quotes = ProductionMarketData(client)
    # Booked fills pay tastytrade's published option fees; the strategy's own fee values keep driving its
    # sizing and economic gate (owner request 2026-10-20).
    fee_schedule = TastytradeOptionFees()
    executor = PaperExecutor(account, buy_price=strategy.execution["paper_buy_price"],
                             sell_price=strategy.execution["paper_sell_price"],
                             slippage_per_contract=float(strategy.execution["slippage_per_contract"]),
                             fees=fee_schedule)
    position_alerted: set[str] = set()

    def available_slots(at: datetime | None = None) -> int:
        moment = at or datetime.now(timezone.utc)
        pending = len(live.pending_entries) if live is not None else 0  # LIVE entries awaiting broker fills
        return min(int(strategy.risk["max_open_positions"]) - len(account.positions) - pending,
                   int(strategy.risk["max_trades_per_day"]) - _trades_today(account, moment) - pending)

    def book_paper_exit(position_data: dict, quote: OptionQuote, why: str, when: datetime) -> float:
        """Sell a simulated position at the quote's bid and book the paper_close record."""
        position = _position_from_record(position_data)
        fill = executor.fill(quote, "SELL", position.quantity, why, when)
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
                               "underlying": position_data["underlying"], "reason": why, "timestamp": when.isoformat()})
        account.positions.remove(position_data)
        account.unrealized_pnl = _unrealized_pnl(account, executor)
        print(f"PAPER EXIT {position_data['underlying']} {position.symbol} {why}; net=${realized:.2f}", flush=True)
        return realized

    async def protective_exit(symbol: str, exc: Exception) -> None:
        """PAPER: a symbol whose data failed can no longer be managed by the strategy's exit rules. Rather than
        stop the whole session (leaving every position unmanaged), sell that symbol's positions at a fresh bid.
        A position without a fresh quote stays open, and disable() then fails the session as before."""
        for position_data in [p for p in account.positions if p["underlying"] == symbol]:
            try:
                quote = (await asyncio.to_thread(_option_marks, client, [position_data]))[0]
            except Exception:
                logger.exception("Protective exit quote failed symbol=%s option=%s", symbol, position_data["symbol"])
                continue
            if position_data not in account.positions or not _fresh_quote(quote, datetime.now(timezone.utc)):
                continue
            realized = book_paper_exit(position_data, quote, "data_failure_protective_exit", datetime.now(timezone.utc))
            save_account(account_path, account)
            record_critical_alert("paper_position_protective_exit", mode="PAPER",
                details={"trade_id": str(position_data["trade_id"]), "option_symbol": position_data["symbol"],
                         "underlying": symbol, "realized_pnl": round(realized, 2),
                         "error_type": type(exc).__name__, "error": str(exc)}, data_dir=cfg.data_dir)

    arbiter = _EntryArbiter(available_slots, ENTRY_ARBITRATION_SECONDS)
    queues: dict[str, asyncio.Queue] = {symbol: asyncio.Queue() for symbol in symbols}

    async def feed() -> None:
        """One DXLink connection for every symbol, fanned out to per-symbol queues."""
        try:
            async for event_symbol, bar in live_minute_bars(token_data["dxlink-url"], token_data["token"], list(symbols)):
                if event_symbol in queues and event_symbol not in disabled:
                    queues[event_symbol].put_nowait(bar)
        except Exception as exc:
            for queue in queues.values():
                queue.put_nowait(exc)
            raise

    async def process_bar(state: _SymbolState, bar: Bar) -> None:
        symbol, bars, report = state.symbol, state.bars, state.report
        if bar.timestamp < test_start:
            return
        if state.history_end is not None and bar.timestamp <= state.history_end:
            return  # minute already covered by verified history
        if bars and bar.timestamp <= bars[-1].timestamp:
            matching = next((existing for existing in reversed(bars) if existing.timestamp == bar.timestamp), None)
            if matching is not None and _same_market_bar(matching, bar):
                report["duplicate_candle_count"] = int(report.get("duplicate_candle_count", 0)) + 1
                return
            state.data_valid = False
            report.update({"historical_to_live_transition_status": "FAILED", "final_readiness": "BLOCKED",
                           "transition_error": "live candle is out of order or conflicts with recovered history",
                           "transition_conflict_timestamp_utc": bar.timestamp.isoformat()})
            state.persist(cfg.data_dir)
            raise RuntimeError(f"{symbol}: live candle is out of order or conflicts with recovered history; entries blocked")
        open_utc = session_open.astimezone(timezone.utc)
        if bars:
            transition = _classify_live_transition(bars[-1].timestamp, bar.timestamp)
        elif not test_mode and bar.timestamp > open_utc:
            # Started just after the open, or the 09:30 minute was dropped as partial at connect:
            # recover the opening minutes from history rather than inferring an opening range.
            transition = "GAP"
        else:
            transition = "FIRST_BAR"
        state.session_started = True
        if transition == "GAP":
            gap_start = bars[-1].timestamp + timedelta(minutes=1) if bars else open_utc
            gap_end = bar.timestamp - timedelta(minutes=1)
            gap_audit: dict = {}
            try:
                # Fetch up to the live bar (exclusive end would equal gap_start for a single-minute
                # gap, which the provider rejects as start==end); _validated_session_history trims the
                # result back to [gap_start, gap_end], so the extra trailing minute is dropped.
                # The provider often publishes a just-missed minute a few seconds later, so retry briefly.
                for attempt in range(1, GAP_RECOVERY_ATTEMPTS + 1):
                    try:
                        gap_audit = {"gap_recovery_attempts": attempt}
                        recovered_batch = await history_provider.fetch(symbol=symbol, start=gap_start, end=bar.timestamp)
                        gap_audit.update(recovered_batch.audit)
                        recovered = _validated_session_history(list(recovered_batch.candles), session_open=gap_start,
                                                               last_completed_bar=gap_end, audit=gap_audit)
                        break
                    except Exception:
                        if attempt == GAP_RECOVERY_ATTEMPTS:
                            raise
                        logger.warning("%s: live-feed gap recovery attempt %d/%d failed; retrying",
                                       symbol, attempt, GAP_RECOVERY_ATTEMPTS)
                        await asyncio.sleep(GAP_RECOVERY_RETRY_SECONDS * attempt)
                bars.extend(recovered)
                if recovered:
                    _write_recovery_checkpoint(cfg.data_dir, symbol=symbol,
                        trading_date=now.astimezone(market_tz).date().isoformat(), provider=recovered_batch.provider,
                        timestamp=recovered[-1].timestamp)
            except Exception as exc:
                state.data_valid = False
                report.update(gap_audit)
                report.update({"historical_to_live_transition_status": "FAILED",
                               "final_readiness": "BLOCKED", "transition_error": str(exc)})
                state.persist(cfg.data_dir)
                raise RuntimeError(f"{symbol}: live-feed gap could not be recovered at {gap_start.isoformat()}: {exc}") from exc
            if not bars or _classify_live_transition(bars[-1].timestamp, bar.timestamp) != "CONTIGUOUS":
                state.data_valid = False
                report.update({"historical_to_live_transition_status": "FAILED",
                               "final_readiness": "BLOCKED", "transition_error": "recovered history did not join the live bar"})
                state.persist(cfg.data_dir)
                raise RuntimeError(f"{symbol}: recovered candles do not join the next live minute; entries blocked")
        if not test_mode and report.get("historical_to_live_transition_status") == "PENDING":
            report.update({"historical_to_live_transition_status": "VERIFIED" if bars else "NOT_REQUIRED_LIVE_CAPTURE",
                           "live_bar_timestamp_et": bar.timestamp.astimezone(market_tz).isoformat()})
        if not test_mode:
            first_live_bar.set()
        bars.append(bar)
        if not test_mode:
            metrics = _startup_history_metrics(bars, strategy, session_open=session_open.astimezone(timezone.utc))
            report.update(metrics)
            report["observed_session_bar_count"] = len(bars)
            report["live_underlying_data_status"] = "FRESH_BAR_PROCESSED"
            report["kill_switch_status"] = "ACTIVE" if (root / "KILL_SWITCH").exists() else "CLEAR"
            if metrics["opening_range_status"] == "VERIFIED" and metrics["indicator_warmup_status"] == "VERIFIED":
                report["final_readiness"] = ("READY_ENTRIES_BLOCKED_KILL_SWITCH"
                                              if report["kill_switch_status"] == "ACTIVE" else "READY")
            elif metrics["opening_range_status"] != "VERIFIED":
                report["final_readiness"] = "COLLECTING_OPENING_RANGE"
            else:
                report["final_readiness"] = "WARMING_UP_INDICATORS"
            state.persist(cfg.data_dir)
            lifecycle = "kill_switch_active" if (root / "KILL_SWITCH").exists() else "running"
            _write_heartbeat(heartbeat_path, status=lifecycle, symbol=label, mode=mode_label,
                             last_bar=bar.timestamp, open_positions=len(account.positions))
            _write_recovery_checkpoint(cfg.data_dir, symbol=symbol,
                trading_date=bar.timestamp.astimezone(market_tz).date().isoformat(), provider=history_provider.name,
                timestamp=bar.timestamp)
        bar_now = bar.timestamp + timedelta(minutes=1)
        if live is not None:
            await live.refresh()  # broker balances/positions/orders, rate-limited; reconciles before any entry
        # Refresh option marks for this symbol's simulated positions before evaluating a new entry.
        positions_mark_fresh = True
        for position_data in list(account.positions):
            if position_data["underlying"] != symbol:
                continue
            try:
                quote = (await asyncio.to_thread(_option_marks, client, [position_data]))[0]
            except Exception as exc:
                logger.exception("Option mark request failed; retaining position and blocking new entries "
                                 "symbol=%s option=%s trade_id=%s",
                                 symbol, position_data["symbol"], position_data["trade_id"])
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
                logger.warning("Option mark is stale or invalid; retaining position and blocking new entries "
                               "symbol=%s option=%s quote_timestamp=%s bid=%s",
                               symbol, position_data["symbol"],
                               quote.quote_timestamp.isoformat() if quote.quote_timestamp else None, quote.bid)
                print(f"EXIT HOLD {position_data['symbol']}: option quote stale; review required", flush=True)
                positions_mark_fresh = False
                key = str(position_data["trade_id"])
                if key not in position_alerted:
                    record_critical_alert("paper_position_management_failed", mode="PAPER",
                        details={"trade_id": key, "option_symbol": position_data["symbol"],
                                 "underlying": symbol, "reason": "stale_or_invalid_option_quote"}, data_dir=cfg.data_dir)
                    position_alerted.add(key)
                continue
            if position_data not in account.positions:
                continue  # closed by another step while this quote was in flight
            position_alerted.discard(str(position_data["trade_id"]))
            position_data["last_bid"] = quote.bid
            account.unrealized_pnl = _unrealized_pnl(account, executor)
            position = _position_from_record(position_data)
            daily_pnl = _daily_realized(account, bar_now) + account.unrealized_pnl
            current_equity = (live.equity() if live is not None else
                              max(0.0, account.starting_capital + account.deposits -
                                  account.withdrawals + account.realized_pnl + account.unrealized_pnl))
            why = ("end_of_session_close" if flatten_at_close and bar_now >= session_close.astimezone(timezone.utc) - timedelta(minutes=1)
                   else exit_reason(position, underlying=bar.close, option_bid=quote.bid, now=bar_now,
                                    daily_loss_hit=daily_pnl <= -current_equity * float(strategy.risk["max_daily_loss_fraction"]),
                                    enable_time_exit=bool(active_strategy.execution["enable_time_exit"])))
            if why and live is not None:
                realized = await live.close_position(record=position_data, reason=why, now=bar_now,
                                                     requote=lambda record: _option_marks(client, [record])[0],
                                                     quote_fresh=_fresh_quote)
                if realized is None:
                    positions_mark_fresh = False  # exit incomplete: keep managing, block new entries
                account.unrealized_pnl = _unrealized_pnl(account, executor)
            elif why:
                book_paper_exit(position_data, quote, why, bar_now)
            else:
                position_data["mfe"] = position.mfe
                position_data["mae"] = position.mae
                account.unrealized_pnl = _unrealized_pnl(account, executor)
        save_account(account_path, account)

        data_issues: list[str] = []
        try:
            latest = await asyncio.to_thread(quotes.snapshots, [symbol])
        except Exception as exc:
            latest = []
            data_issues.append(f"underlying_quote_request_failed:{type(exc).__name__}")
            logger.exception("Underlying quote request failed symbol=%s bar_timestamp=%s", symbol, bar.timestamp.isoformat())
        quote_check_now = datetime.now(timezone.utc)
        quote_fresh = len(latest) == 1 and _fresh_underlying_quote(latest[0], quote_check_now)
        if not quote_fresh:
            age = ((quote_check_now - latest[0].timestamp).total_seconds() if latest else None)
            quote_reason = (_underlying_quote_rejection(latest[0], quote_check_now) if latest
                            else "quote response contained no underlying quote")
            age_text = f" age={age:.3f}s" if age is not None else ""
            logger.warning("NO TRADE %s: %s%s; count=%d expected=1 bar=%s quote_timestamp=%s bid=%s ask=%s",
                           symbol, quote_reason, age_text, len(latest), bar.timestamp.isoformat(),
                           latest[0].timestamp.isoformat() if latest else None,
                           latest[0].bid if latest else None, latest[0].ask if latest else None)
            if not any(issue.startswith("underlying_quote_request_failed") for issue in data_issues):
                data_issues.append(f"underlying_quote_rejected:{quote_reason}")
        option_chain: dict[str, list[OptionQuote]] = {symbol: []}
        signal = evaluate_underlying(bars, active_strategy, opening_range_start=test_start)
        entry_window = calendar.entry_window_ok(bar_now, active_strategy.entry_window_et["start"], active_strategy.entry_window_et["end"])
        if signal and entry_window and state.data_valid and quote_fresh:
            try:
                # Option chains can be large (SPY: 13k+ contracts); keep the request off the event loop.
                option_chain[symbol] = await asyncio.to_thread(
                    fetch_option_quotes, client, underlying=symbol, spot=bar.close, now=bar_now,
                    min_dte=int(active_strategy.rules["min_dte"]), max_dte=int(active_strategy.rules["max_dte"]))
            except Exception as exc:
                data_issues.append(f"option_chain_request_failed:{type(exc).__name__}")
                logger.exception("Option chain request failed symbol=%s bar_timestamp=%s", symbol, bar.timestamp.isoformat())
        cycle_equity = (live.equity() if live is not None else
                        max(0.0, account.starting_capital + account.deposits - account.withdrawals +
                            account.realized_pnl + account.unrealized_pnl))
        cycle_strategy = active_strategy if profile is None else profile.cycle_strategy(active_strategy, cycle_equity, account)
        risk_state = RiskState(
            equity=cycle_equity,
            daily_pnl=_daily_realized(account, bar_now) + account.unrealized_pnl,
            weekly_pnl=_weekly_realized(account, bar_now) + account.unrealized_pnl,
            consecutive_losses=_consecutive_losses(account), trades_today=_trades_today(account, bar_now),
            open_positions=len(account.positions),
            open_underlyings=tuple(str(position["underlying"]) for position in account.positions))
        records = decision_cycle(strategy=replace(cycle_strategy, universe=(symbol,)), strategy_path=root / "strategy.txt",
            manifest_path=root / "active_strategy.json", bars_by_symbol={symbol: list(bars)},
            option_chain_by_symbol=option_chain, risk_state=risk_state, now=bar_now,
            data_fresh=state.data_valid and quote_fresh,
            account_certain=positions_mark_fresh and (live is None or live.account_certain),
            reconciled=live is None or live.reconciled,
            protection_verified=live is None or live.protection_verified, market_open=calendar.is_open(bar_now),
            within_entry_window=entry_window, decision_log=decision_log, kill_switch_path=root / "KILL_SWITCH",
            paper_execution=True, paper_test_mode=test_mode,
            opening_range_start=test_start, data_issues=data_issues)
        for decision in records:
            if decision.decision != "PAPER_ENTRY":
                if signal or decision.reason in {"kill_switch_active", "safety_gate_blocked"}:
                    print(f"NO TRADE {symbol}: {decision.reason}", flush=True)
                continue
            score = (float(decision.diagnostics.get("expected_net_r") or 0.0),
                     float((signal.metrics if signal else {}).get("trend_efficiency") or 0.0))
            if not await arbiter.approve(bar.timestamp, symbol, score):
                print(f"NO TRADE {symbol}: outranked by other symbols this minute (net R {score[0]:.2f})", flush=True)
                continue
            # Re-check synchronously: no await between this check and the fill below.
            if available_slots(bar_now) < 1 or has_position(symbol) or (root / "KILL_SWITCH").exists():
                print(f"NO TRADE {symbol}: position/trade limit reached while ranking", flush=True)
                continue
            if profile is not None:
                within_cap, cap_reason = profile.entry_allowed(account, cycle_equity, decision.diagnostics["modeled_risk_dollars"])
                if not within_cap:
                    logger.info("NO TRADE %s: %s profile=%s", symbol, cap_reason, profile.name)
                    print(f"NO TRADE {symbol}: {cap_reason}", flush=True)
                    continue
            selected = next((q for q in option_chain[symbol] if q.symbol == decision.diagnostics.get("option_candidate")), None)
            if selected is None:
                print(f"NO TRADE {symbol}: selected contract quote disappeared", flush=True)
                continue
            signal_now = evaluate_underlying(bars, active_strategy, opening_range_start=test_start)
            if signal_now is None:
                raise RuntimeError(f"{symbol}: signal disappeared during paper entry; stop and review")
            quantity = int(decision.diagnostics["quantity"])
            if live is not None:
                # Same position record PAPER books; LIVE fills it with the broker's actual fill.
                await live.open_position(template={
                    "trade_id": str(uuid.uuid4()), "symbol": selected.symbol, "underlying": symbol,
                    "right": selected.right, "entry_underlying": bar.close,
                    "stop_underlying": signal_now.stop_price, "target_underlying": signal_now.target_price,
                    "entry_time": bar_now.isoformat(), "max_holding_minutes": int(strategy.rules["max_holding_minutes"]),
                    "initial_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                    "planned_risk_dollars": decision.diagnostics["planned_risk_dollars"],
                    "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                    "premium_exposure_dollars": decision.diagnostics["premium_exposure_dollars"],
                    "expected_net_r": decision.diagnostics.get("expected_net_r"),
                    "strategy_id": strategy.strategy_id,
                    "strategy_version": strategy.version, "strategy_hash": strategy.sha256,
                    "mfe": 0.0, "mae": 0.0, "last_bid": selected.bid,
                }, quote=selected, quantity=quantity, decision_option=decision.diagnostics.get("option_candidate"),
                    readiness=report, now=datetime.now(timezone.utc), quote_fresh=_fresh_quote)
                account.unrealized_pnl = _unrealized_pnl(account, executor)
                continue
            fill = executor.fill(selected, "BUY", quantity, "strategy_entry", bar_now)
            account.ledger[-1].update({"strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
                                       "strategy_hash": strategy.sha256, "underlying": symbol,
                                       "planned_risk_dollars": decision.diagnostics["planned_risk_dollars"],
                                       "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                                       "premium_exposure_dollars": decision.diagnostics["premium_exposure_dollars"]})
            account.positions.append({
                "trade_id": fill.trade_id, "symbol": selected.symbol, "underlying": symbol,
                "right": selected.right, "quantity": quantity, "entry_price": fill.fill_price,
                "entry_fees": fill.fees, "entry_underlying": bar.close,
                "stop_underlying": signal_now.stop_price, "target_underlying": signal_now.target_price,
                "entry_time": bar_now.isoformat(), "max_holding_minutes": int(strategy.rules["max_holding_minutes"]),
                "initial_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                "planned_risk_dollars": decision.diagnostics["planned_risk_dollars"],
                "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                "premium_exposure_dollars": decision.diagnostics["premium_exposure_dollars"],
                "expected_net_r": decision.diagnostics.get("expected_net_r"),
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version, "strategy_hash": strategy.sha256,
                "mfe": 0.0, "mae": 0.0, "last_bid": selected.bid,
            })
            account.unrealized_pnl = _unrealized_pnl(account, executor)
            save_account(account_path, account)
            print(f"PAPER ENTRY {symbol} {selected.symbol} x{quantity} @ ask+slippage ${fill.fill_price:.2f} "
                  f"(net R {score[0]:.2f})", flush=True)

    async def run_symbol(state: _SymbolState) -> None:
        queue = queues[state.symbol]
        while True:
            item = await queue.get()
            if isinstance(item, BaseException):
                raise item
            if state.symbol in disabled:
                return
            try:
                await process_bar(state, item)
            except Exception as exc:
                if live is None and has_position(state.symbol) and not _is_connection_failure(exc):
                    await protective_exit(state.symbol, exc)
                disable(state, exc)  # re-raises if the symbol still holds a position or the feed failed
                return

    if test_mode:
        print(f"PAPER TEST started for {label}; fake test capital ${account.starting_capital:,.2f}; "
              f"rolling {strategy.opening_range_minutes}-minute range from {test_start.isoformat()}; "
              f"entries {active_strategy.entry_window_et['start']}-{active_strategy.entry_window_et['end']} ET; "
              "local simulated orders only, no broker orders. Press Ctrl+C to stop.", flush=True)
    elif live is not None:
        print(f"LIVE session started for {label}; REAL broker orders on account {live.startup_report.get('account')}. "
              "Press Ctrl+C to stop (open positions stay at the broker).", flush=True)
    else:
        print(f"PAPER session started for {label}; local simulated orders only. Press Ctrl+C to stop.", flush=True)

    tasks: list[asyncio.Task] = []
    heartbeat_task = asyncio.create_task(startup_heartbeat()) if not test_mode else None
    try:
        remaining = max(1.0, (session_close.astimezone(timezone.utc) - now).total_seconds())
        async with asyncio.timeout(remaining):
            if not test_mode and now >= session_open.astimezone(timezone.utc) + timedelta(minutes=1):
                for state in states.values():
                    try:
                        await bootstrap(state)
                    except Exception as exc:
                        disable(state, exc)
            tasks = [asyncio.create_task(feed())] + [asyncio.create_task(run_symbol(state))
                                                       for symbol, state in states.items() if symbol not in disabled]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
    except TimeoutError:
        print(f"{mode_label} session ended at the equity close.", flush=True)
        if account.positions and not flatten_at_close:
            for position_data in account.positions:
                print(f"CARRYING {position_data['underlying']} {position_data['symbol']} overnight "
                      "(flatten_at_session_close=false); it is unmonitored until the next session starts", flush=True)
            save_account(account_path, account)
        elif account.positions and live is not None:
            for position_data in list(account.positions):
                realized = await live.close_position(record=position_data, reason="end_of_session_close",
                                                     now=datetime.now(timezone.utc),
                                                     requote=lambda record: _option_marks(client, [record])[0],
                                                     quote_fresh=_fresh_quote)
                if realized is None:
                    print(f"REVIEW REQUIRED {position_data['symbol']}: LIVE end-of-session close incomplete; "
                          "the position may still be open at the broker", flush=True)
                    record_critical_alert("live_position_unresolved_at_session_close", mode="LIVE",
                        details={"trade_id": str(position_data["trade_id"]), "option_symbol": position_data["symbol"],
                                 "underlying": position_data["underlying"]}, data_dir=cfg.data_dir)
            account.unrealized_pnl = _unrealized_pnl(account, executor)
            save_account(account_path, account)
        elif account.positions:
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
                                       "underlying": position_data["underlying"],
                                       "reason": "end_of_session_close", "timestamp": close_now.isoformat()})
                account.positions.remove(position_data)
                print(f"PAPER EXIT {position_data['underlying']} {position_data['symbol']} end_of_session_close; net=${realized:.2f}", flush=True)
            account.unrealized_pnl = _unrealized_pnl(account, executor)
            save_account(account_path, account)
    finally:
        first_live_bar.set()
        for task in tasks + ([heartbeat_task] if heartbeat_task else []):
            task.cancel()
        await asyncio.gather(*tasks, *([heartbeat_task] if heartbeat_task else []), return_exceptions=True)


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
    return (quote.quote_timestamp is not None and quote.bid > 0
            and -MAX_QUOTE_FUTURE_SECONDS <= (now - quote.quote_timestamp).total_seconds() <= 30)


def _fresh_underlying_quote(quote: CachedQuote, now: datetime) -> bool:
    age = (now - quote.timestamp).total_seconds()
    # Provider timestamps can lead the local clock by clock skew and transport jitter. A quote that
    # appears slightly future-dated is not stale; the old 250ms bound rejected 72 fresh quotes on
    # 2026-10-13 because the PC clock lagged ~0.3-0.5s. Startup separately refuses a clock offset
    # beyond MAX_CLOCK_OFFSET_SECONDS, so this bound only needs to cover skew within that limit.
    return -MAX_QUOTE_FUTURE_SECONDS <= age <= 20 and quote.bid > 0 and quote.ask >= quote.bid


def _underlying_quote_rejection(quote: CachedQuote, now: datetime) -> str:
    age = (now - quote.timestamp).total_seconds()
    if age < -MAX_QUOTE_FUTURE_SECONDS:
        return f"underlying quote timestamp is more than {MAX_QUOTE_FUTURE_SECONDS:g}s ahead of local time"
    if age > 20:
        return "underlying quote is stale (older than 20 seconds)"
    if quote.bid <= 0:
        return "underlying quote bid is missing or non-positive"
    if quote.ask < quote.bid:
        return "underlying quote ask is below bid"
    return "underlying quote failed validation"


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
    # A LIVE ledger books "live_close" records from broker fills; a paper ledger never contains them.
    return [item for item in account.ledger if item.get("type") in ("paper_close", "live_close")]


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
