"""V2Session: everything Professional Strategy V2 needs at runtime, owned by one PAPER engine process.

The engine loop (trader.paper_runner._run) keeps its feed, history recovery, marks, fills and heartbeats; with
V2 enabled it calls this object at a few fixed points:
  startup()                 once: daily history, premarket range, market metrics, correlation, VIX
  on_history(symbol, bars)  after each symbol's verified history bootstrap
  on_bar(symbol, bars, ..)  every completed bar of every symbol (context-only symbols stop here)
  manage(...)               each open position, each bar, after the fresh option mark
  evaluate(...)             entry decision for a trading symbol; returns the candidate report
  record_entry(...)         after a V2 PAPER fill
  shadow_bar(...)           the original-strategy benchmark for the same bar
  session_end(...)          hypothetical outcomes of signals V2 did not take; shadow flatten
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..logging_utils import append_jsonl, atomic_replace
from ..models import Bar, OptionQuote
from ..strategy_engine import evaluate_underlying_diagnostics
from ..strategy_loader import Strategy
from . import management as management_mod
from . import pipeline
from .config import V2Config, V2ConfigError
from .confirmation import ACTIONABLE, TERMINAL, BreakoutTracker
from .context import DailyLevels, MarketContext, VixReading
from .correlation import CorrelationModel
from .datahub import DataHub, RequestLog
from .events import EventEngine
from .options import fetch_option_quotes_cached
from .regime import RegimeResult, classify
from .sectors import SectorMap

ET = ZoneInfo("America/New_York")
TRACKER_REPLAY_BARS = 60


class V2Session:
    def __init__(self, *, root: Path, data_dir: Path, config: V2Config, strategy: Strategy, profile_name: str,
                 client, calendar, session_open: datetime, session_close: datetime, logger, starting_cash: float,
                 profile_policy=None, arbiter_factory: Callable | None = None, helpers: dict[str, Callable] | None = None,
                 shared_dir: Path | None = None, trade_symbols: tuple[str, ...] | None = None):
        self.root, self.data_dir, self.config, self.strategy = root, data_dir, config, strategy
        self.profile_name, self.client, self.calendar, self.logger = profile_name, client, calendar, logger
        self.session_open, self.session_close = session_open.astimezone(timezone.utc), session_close.astimezone(timezone.utc)
        self.cfg = config.data
        # The symbols this session actually trades (the engine's list, incl. --symbol limits and carried positions).
        self.trade_symbols = tuple(trade_symbols) if trade_symbols is not None else tuple(strategy.universe)
        context_cfg = self.cfg["context"]
        self.context_only = tuple(s for s in config.context_symbols() if s not in self.trade_symbols)
        self.sector_map = SectorMap(config.sector_map)
        unmapped = self.sector_map.unmapped(self.trade_symbols)
        if unmapped:
            raise V2ConfigError("V2 sector map has no entry for: " + ", ".join(unmapped))
        self.request_log = RequestLog(data_dir / "v2_api_requests.jsonl")
        self.hub = DataHub(client=client, shared_dir=shared_dir or (root / "data" / "v2_shared_cache"),
                           request_log=self.request_log)
        rules = strategy.rules
        self.context = MarketContext(tuple(context_cfg["broad_symbols"]), tuple(context_cfg["sector_etfs"]),
                                     int(rules["trend_ema_fast"]), int(rules["trend_ema_slow"]), int(rules["atr_period"]),
                                     int(self.cfg["regime"]["efficiency_window_bars"]), float(context_cfg["max_bar_age_seconds"]),
                                     float(context_cfg["vix_max_age_seconds"]))
        self.trackers = {s: BreakoutTracker(self.cfg["confirmation"], int(rules["breakout_lookback_bars"]))
                         for s in self.trade_symbols}
        self.events = EventEngine(config.calendar, self.cfg["events"], next_session=self._next_session)
        self.correlation = CorrelationModel(self.cfg["correlation"], self.sector_map, {})
        self.metrics: dict[str, dict] = {}
        self.metrics_error: str | None = None
        self.last_metrics_refresh: datetime | None = None
        self.last_vix_refresh: datetime | None = None
        self.premarket_loaded = False
        self.token_data: dict[str, Any] = {}
        self.last_core: dict[str, tuple] = {}
        self.last_tracker: dict[str, dict] = {}
        self.latest_reports: dict[str, dict] = {}
        self.untaken: list[dict] = []
        self._snapshot: tuple[datetime, dict] | None = None
        self._snapshot_built = datetime.now(timezone.utc)
        self._chains: dict[tuple[str, datetime], list[OptionQuote]] = {}
        self.decisions_log = data_dir / "v2_decisions.jsonl"
        self.snapshot_path = data_dir / "v2_context_snapshot.json"
        self.status_path = data_dir / "v2_status.json"
        self.shadow = None
        if self.cfg["shadow_benchmark"]["enabled"] and helpers is not None:
            from .shadow import ShadowOriginal
            self.shadow = ShadowOriginal(root=root, data_dir=data_dir, strategy=strategy, starting_cash=starting_cash,
                                         profile=profile_policy, arbiter_factory=arbiter_factory, helpers=helpers)

    # ------------------------------------------------------------------------------------------------
    def is_context_only(self, symbol: str) -> bool:
        return symbol in self.context_only

    def _next_session(self, day: date) -> date | None:
        for offset in range(1, 8):
            candidate = day + timedelta(days=offset)
            if self.calendar.session_bounds(candidate):
                return candidate
        return None

    def _log(self, record: dict[str, Any]) -> None:
        append_jsonl(self.decisions_log, {"strategy_id": self.config.strategy_id, "strategy_version": self.config.version,
                                          "strategy_hash": self.config.sha256, "ledger_profile": self.profile_name, **record})

    def _write_json(self, path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        atomic_replace(temporary, path)

    # --- startup ---------------------------------------------------------------------------------------
    async def startup(self, token_data: dict[str, Any], now: datetime) -> dict[str, Any]:
        self.token_data = token_data
        today = now.astimezone(ET).date()
        status: dict[str, Any] = {"started_at_utc": now.isoformat(), "ledger_profile": self.profile_name,
                                  "strategy_hash": self.config.sha256, "base_strategy_hash": self.config.base.sha256,
                                  "trade_symbols": list(self.trade_symbols), "context_only_symbols": list(self.context_only)}
        vix_symbol = self.cfg["context"]["volatility_index"]
        symbols = list(dict.fromkeys([*self.trade_symbols, *self.config.context_symbols(), vix_symbol]))
        daily, failures = await self.hub.daily_history(symbols, dxlink_url=token_data["dxlink-url"],
                                                       quote_token=token_data["token"],
                                                       sessions=int(self.cfg["context"]["daily_history_sessions"]),
                                                       today=today)
        for symbol, rows in daily.items():
            if rows:
                last = rows[-1]
                self.context.prior[symbol] = DailyLevels(last["session_date"], last["open"], last["high"], last["low"], last["close"])
        self.correlation = CorrelationModel(self.cfg["correlation"], self.sector_map,
                                            {s: [(r["session_date"], r["close"]) for r in rows] for s, rows in daily.items()})
        self.context.provenance["daily_history"] = {"source": "tastytrade DXLink {=1d} candles (shared cache)",
                                                    "symbols": len(daily), "failures": failures, "today": today.isoformat()}
        status["daily_history"] = {"symbols": len(daily), "failures": failures}
        await self.refresh_metrics(now, force=True)
        status["market_metrics"] = {"symbols": len(self.metrics), "error": self.metrics_error}
        await self.refresh_vix(now, force=True)
        status["vix"] = None if self.context.vix is None else {"value": self.context.vix.value,
                                                                "previous_close": self.context.vix.previous_close}
        if now.astimezone(ET).time() >= datetime.strptime("09:30", "%H:%M").time():
            await self.load_premarket(now)
        status["premarket_loaded"] = self.premarket_loaded
        self._write_json(self.status_path, status)
        self.logger.info("V2 startup %s", json.dumps(status, default=str)[:2000])
        return status

    async def refresh_metrics(self, now: datetime, *, force: bool = False) -> None:
        max_age = float(self.cfg["option_quality"]["max_metrics_age_minutes"]) * 60
        if not force and self.last_metrics_refresh and (now - self.last_metrics_refresh).total_seconds() < max_age / 2:
            return
        self.last_metrics_refresh = now
        try:
            self.metrics = await asyncio.to_thread(self.hub.market_metrics, list(self.trade_symbols), max_age / 2)
            self.metrics_error = None
        except Exception as exc:  # stale metrics are rejected by the stage policies; never invented
            self.metrics_error = f"{type(exc).__name__}: {exc}"[:300]
            self.logger.warning("V2 market metrics refresh failed: %s", self.metrics_error)

    async def refresh_vix(self, now: datetime, *, force: bool = False) -> None:
        if not force and self.last_vix_refresh and (now - self.last_vix_refresh).total_seconds() < 50:
            return
        self.last_vix_refresh = now
        symbol = self.cfg["context"]["volatility_index"]
        try:
            reading = await asyncio.to_thread(self.hub.vix, symbol)
        except Exception as exc:
            self.context.provenance["vix_error"] = f"{type(exc).__name__}: {exc}"[:300]
            return
        if not reading:
            return
        prior = self.context.prior.get(symbol)
        stamp = reading.get("quote_updated_at")
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else now
        self.context.vix = VixReading(reading["value"], prior.close if prior else reading.get("prev_close"), timestamp,
                                      reading["source"])

    async def load_premarket(self, now: datetime) -> None:
        if self.premarket_loaded:
            return
        broad = list(self.cfg["context"]["broad_symbols"])
        try:
            ranges, failures = await self.hub.premarket(broad, dxlink_url=self.token_data["dxlink-url"],
                                                        quote_token=self.token_data["token"], day=now.astimezone(ET).date(),
                                                        start_et=self.cfg["context"]["premarket_start_et"])
        except Exception as exc:
            self.context.provenance["premarket_error"] = f"{type(exc).__name__}: {exc}"[:300]
            self.premarket_loaded = True
            return
        self.context.premarket.update(ranges)
        self.context.provenance["premarket"] = {"source": "tastytrade DXLink 1m extended-hours candles", "failures": failures}
        self.premarket_loaded = True

    # --- bars ------------------------------------------------------------------------------------------
    def on_history(self, symbol: str, bars: list[Bar]) -> None:
        self.context.register(symbol, bars)
        if symbol not in self.trackers or not bars:
            return
        tracker = self.trackers[symbol]
        for index in range(max(0, len(bars) - TRACKER_REPLAY_BARS), len(bars)):
            signal, metrics, _ = evaluate_underlying_diagnostics(bars[:index + 1], self.strategy,
                                                                 opening_range_start=self.session_open)
            self.last_tracker[symbol] = tracker.update(bars[index], signal, metrics)
            self.last_core[symbol] = (signal, metrics)

    async def on_bar(self, symbol: str, bars: list[Bar], bar_now: datetime) -> None:
        self.context.register(symbol, bars)
        if not self.premarket_loaded and bar_now.astimezone(ET).time() >= datetime.strptime("09:31", "%H:%M").time():
            await self.load_premarket(bar_now)
        await self.refresh_vix(bar_now)
        await self.refresh_metrics(bar_now)
        if symbol in self.trackers:
            signal, metrics, reason = evaluate_underlying_diagnostics(bars, self.strategy, opening_range_start=self.session_open)
            self.last_core[symbol] = (signal, metrics)
            self.last_tracker[symbol] = self.trackers[symbol].update(bars[-1], signal, metrics)
        self.snapshot(bar_now)   # memoized 5 s within a minute; keeps the dashboard's context file current every minute

    def mark_unavailable(self, symbol: str, reason: str) -> None:
        self.context.mark_unavailable(symbol, reason)

    def snapshot(self, now: datetime) -> dict[str, Any]:
        if self._snapshot and self._snapshot[0] == now and (datetime.now(timezone.utc) - self._snapshot_built).total_seconds() < 5:
            return self._snapshot[1]
        snap = self.context.snapshot(now, list(self.trade_symbols))
        snap["regime"] = classify(snap, self.cfg["regime"]).to_dict()
        snap["events_upcoming"] = self.events.upcoming(now, 7)
        snap["ledger_profile"] = self.profile_name
        self._snapshot = (now, snap)
        self._snapshot_built = datetime.now(timezone.utc)
        try:
            self._write_json(self.snapshot_path, snap)
        except OSError:
            pass
        return snap

    # --- position management ------------------------------------------------------------------------------
    def manage(self, position: dict, *, bar: Bar, option_bid: float, now: datetime, exit_fees_estimate: float) -> dict[str, Any]:
        symbol = position["underlying"]
        atr = (self.last_core.get(symbol, (None, {}))[1] or {}).get("atr")
        result = management_mod.manage(position, underlying=bar.close, atr=atr, option_bid=option_bid, now=now,
                                       cfg=self.cfg["management"], atr_multiple=float(self.strategy.rules["stop_underlying_atr"]),
                                       exit_fees_estimate=exit_fees_estimate,
                                       slippage_per_contract=float(self.strategy.execution["slippage_per_contract"]))
        review = self.events.open_position_review(now)
        if review:
            flags = position.setdefault("v2_management", {}).setdefault("review", [])
            for flag in review:
                if flag not in flags:
                    flags.append(flag)
                    self._log({"type": "position_review", "trade_id": position["trade_id"], "underlying": symbol,
                               "flag": flag, "timestamp": now.isoformat()})
        if result["events"]:
            self._log({"type": "management", "trade_id": position["trade_id"], "underlying": symbol,
                       "events": result["events"], "timestamp": now.isoformat()})
        return result

    # --- entries ------------------------------------------------------------------------------------------
    def chain_sync(self, symbol: str, bar_time: datetime, spot: float, now: datetime) -> list[OptionQuote]:
        key = (symbol, bar_time)
        if key not in self._chains:
            for old in [k for k in self._chains if k[1] < bar_time - timedelta(minutes=2)]:
                del self._chains[old]
            self._chains[key] = fetch_option_quotes_cached(self.client, self.hub, underlying=symbol, spot=spot, now=now,
                                                           min_dte=int(self.strategy.rules["min_dte"]),
                                                           max_dte=int(self.strategy.rules["max_dte"]))
        return self._chains[key]

    async def evaluate(self, *, symbol: str, bars: list[Bar], now: datetime, account, equity: float, strategy: Strategy,
                       risk_state, data_fresh: bool, entries_blocked: str | None, pending: list[dict]) -> pipeline.CandidateReport | None:
        tracker = self.last_tracker.get(symbol)
        if not tracker or tracker["state"] == "NO_SETUP":
            return None
        setup = tracker.get("setup") or {}
        transitions = setup.get("transitions") or []
        just_ended = tracker["state"] in TERMINAL and transitions and transitions[-1]["at"] == bars[-1].timestamp.isoformat()
        if tracker["state"] in TERMINAL and not just_ended:
            return None
        snap = self.snapshot(now)
        regime = classify(snap, self.cfg["regime"])
        core_signal, core_metrics = self.last_core.get(symbol, (None, {}))
        spot = bars[-1].close
        inputs = pipeline.CandidateInputs(
            symbol=symbol, now=now, bars=list(bars), core_metrics=dict(core_metrics or {}), tracker=copy.deepcopy(tracker),
            snapshot=snap, regime=regime, strategy=strategy, v2=self.cfg, sector_map=self.sector_map, events=self.events,
            metrics=self.metrics.get(symbol) if not self.metrics_error or symbol in self.metrics else None,
            correlation=self.correlation, risk_state=risk_state, positions=copy.deepcopy(account.positions),
            pending=list(pending), cash=float(account.cash), equity=equity, profile_name=self.profile_name,
            fetch_chain=lambda: self.chain_sync(symbol, bars[-1].timestamp, spot, now), data_fresh=data_fresh,
            entries_blocked=entries_blocked, core_signal_now=core_signal is not None)
        report = await asyncio.to_thread(pipeline.evaluate_candidate, inputs)
        record = pipeline.report_record(report)
        self.latest_reports[symbol] = record
        self._log({"type": "candidate", "timestamp": now.isoformat(), **record})
        if report.outcome != "APPROVED" and (just_ended or report.outcome == "REJECTED"):
            self._remember_untaken(symbol, setup, report, now, spot)
        return report

    def _remember_untaken(self, symbol: str, setup: dict, report: pipeline.CandidateReport, now: datetime, spot: float) -> None:
        trigger = setup.get("trigger_time")
        if not trigger or any(u["symbol"] == symbol and u["trigger_time"] == trigger for u in self.untaken):
            return
        direction = 1 if setup["side"] == "CALL" else -1
        self.untaken.append({"symbol": symbol, "side": setup["side"], "trigger_time": trigger, "decided_at": now.isoformat(),
                             "entry_underlying": spot, "stop": spot - direction * float(setup["stop_distance"]),
                             "target": spot + direction * float(setup["target_distance"]), "outcome": report.outcome,
                             "reasons": report.reasons[:4], "regime": report.stages.get("market_regime", {}).get("regime")})

    def record_entry(self, report: pipeline.CandidateReport, record: dict) -> None:
        self._log({"type": "entry", "timestamp": record["entry_time"], "trade_id": record["trade_id"], "underlying": record["underlying"],
                   "option": record["symbol"], "quantity": record["quantity"], "entry_price": record["entry_price"],
                   "regime": record["v2_entry"]["regime"], "setup_type": record["v2_entry"]["setup_type"],
                   "confirmation_delay_minutes": record["v2_entry"].get("confirmation_delay_minutes")})

    def entry_record_fields(self, report: pipeline.CandidateReport, now: datetime) -> dict[str, Any]:
        entry = report.entry or {}
        setup = report.stages.get("breakout_confirmation", {}).get("setup") or {}
        trigger = setup.get("trigger_time")
        delay = ((now - datetime.fromisoformat(trigger)).total_seconds() / 60 - 1) if trigger else None
        return {"strategy_id": self.config.strategy_id, "strategy_version": self.config.version,
                "strategy_hash": self.config.sha256, "base_strategy_hash": self.config.base.sha256,
                "entry_delta": entry["option"].delta if entry.get("option") else None,
                "v2_entry": {"regime": entry.get("regime"), "setup_type": entry.get("setup_type"),
                             "quality": entry.get("quality"), "breakout_bar_utc": trigger,
                             "confirmation_delay_minutes": round(delay, 1) if delay is not None else None,
                             "ledger_profile": self.profile_name},
                "v2_management": {"initial_stop_distance": abs(float(entry["entry_underlying"]) - float(entry["stop_underlying"]))}}

    def portfolio_recheck(self, report: pipeline.CandidateReport, account, equity: float, strategy: Strategy,
                          pending: list[dict]) -> tuple[bool, str]:
        """Synchronous re-check right before the fill (positions may have changed while ranking)."""
        from . import portfolio as portfolio_mod
        entry = report.entry or {}
        side = report.side
        max_fraction = min(float(strategy.risk["max_risk_per_trade_fraction"]), float(strategy.risk["hard_max_risk_per_trade_fraction"]))
        budgets = portfolio_mod.budgets_for(equity=equity, per_trade_budget=equity * max_fraction,
                                            aggregate_fraction=self.cfg["portfolio"]["aggregate_max_loss_fraction"][self.profile_name],
                                            positions=account.positions, pending=pending)
        option = entry["option"]
        cash = (option.ask * 100 + float(strategy.execution["slippage_per_contract"])) * int(entry["quantity"])
        check = portfolio_mod.evaluate(model=self.correlation, symbol=report.symbol, side=side,
                                       candidate_max_loss=float(entry["modeled_risk_dollars"]), entry_cash=cash,
                                       cash=float(account.cash), positions=account.positions, budgets=budgets)
        return check.ok, "; ".join(check.reasons)

    def log_outcome(self, symbol: str, now: datetime, outcome: str, detail: str) -> None:
        self._log({"type": "entry_not_filled", "symbol": symbol, "timestamp": now.isoformat(), "outcome": outcome,
                   "detail": detail})

    # --- benchmark & session end ---------------------------------------------------------------------------
    async def shadow_bar(self, **kwargs) -> None:
        if self.shadow is None:
            return
        symbol, bars = kwargs["symbol"], kwargs["bars"]
        spot = bars[-1].close
        bar_time = bars[-1].timestamp
        now = kwargs["bar_now"]

        async def chain() -> list[OptionQuote]:
            return await asyncio.to_thread(self.chain_sync, symbol, bar_time, spot, now)
        try:
            await self.shadow.on_bar(chain=chain, opening_range_start=self.session_open, **kwargs)
        except Exception as exc:  # the benchmark must never stop the engine
            self.logger.exception("V2 shadow benchmark failed for %s", symbol)
            self._log({"type": "shadow_error", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:300]})

    def session_end(self, bars_by_symbol: dict[str, list[Bar]], when: datetime) -> list[dict]:
        outcomes = []
        for item in self.untaken:
            bars = [b for b in bars_by_symbol.get(item["symbol"], []) if b.timestamp + timedelta(minutes=1) > datetime.fromisoformat(item["decided_at"])]
            direction = 1 if item["side"] == "CALL" else -1
            risk = abs(item["entry_underlying"] - item["stop"])
            result, exit_price, exit_time = "session_close", bars[-1].close if bars else item["entry_underlying"], None
            for bar in bars:
                if (bar.close - item["stop"]) * direction <= 0:
                    result, exit_price, exit_time = "stop", bar.close, bar.timestamp
                    break
                if (bar.close - item["target"]) * direction >= 0:
                    result, exit_price, exit_time = "target", bar.close, bar.timestamp
                    break
            r = (exit_price - item["entry_underlying"]) * direction / risk if risk > 0 else 0.0
            outcome = {**item, "hypothetical_result": result, "hypothetical_exit_underlying": exit_price,
                       "hypothetical_exit_time": exit_time.isoformat() if exit_time else None,
                       "hypothetical_underlying_r": round(r, 3), "model": "underlying proxy, completed bars after the decision"}
            outcomes.append(outcome)
            append_jsonl(self.data_dir / "v2_signal_outcomes.jsonl", outcome)
        if self.shadow is not None:
            try:
                self.shadow.flatten(self.client, when)
            except Exception:
                self.logger.exception("V2 shadow flatten failed")
        return outcomes
