"""Breakout confirmation engine. A per-symbol state machine fed one COMPLETED 1-minute bar at a time together with
the core strategy's own diagnostics for that bar (trader.strategy_engine.evaluate_underlying_diagnostics, unchanged).
It never looks at a bar after the one being processed.

States
  NO_SETUP                no qualified core signal being tracked
  AWAITING_CONFIRMATION   the core signal qualified (a completed close beyond the breakout level)
  CONFIRMED               confirm_closes consecutive completed closes beyond the level, the latest at least
                          min_distance_atr x ATR beyond it                                   -> actionable
  PULLBACK_HOLDING        price came back to within retest_tolerance_atr x ATR of the level without closing back
                          inside the opening range
  PULLBACK_CONFIRMED      after PULLBACK_HOLDING, a completed close beyond the level and beyond the retest bar's
                          high (calls) / low (puts)                                           -> actionable
  REJECTED_BREAKOUT       the first bar after the breakout closed back inside the opening range (terminal)
  FAILED_BREAKOUT         a later bar closed back inside the opening range (terminal)
  EXPIRED                 no actionable state within the waiting windows (terminal)

An actionable state stays actionable only while price remains beyond the level by min_distance_atr x ATR and no
more than the strategy's breakout_lookback_bars have passed since the breakout (the core's own anti-chasing bound).

2.8.2 (owner, 2026-11-19; NFLX 11/18 was bought on a 12-cent poke that a 3-cent lunchtime ATR called "3.4 ATR"):
  min_distance_adr      every distance-beyond-the-level test also needs min_distance_adr x the average daily range
  retest_tolerance_adr  a retest counts within max(retest_tolerance_atr x ATR, retest_tolerance_adr x daily range)
  pullback_only         CONFIRMED is a waiting state, not a buy: from it, a retest moves to PULLBACK_HOLDING and only
                        PULLBACK_CONFIRMED is actionable
The daily range comes from set_daily_range(); while it is unknown, a check that needs it is not met (fail closed).

Every REJECTED_BREAKOUT / FAILED_BREAKOUT is also kept in `failures` (side, level, time) for the rest of the session,
so a later setup can see that the same breakout already failed (V2 failed_level_reentry, 2.7.0).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models import Bar

TERMINAL = {"REJECTED_BREAKOUT", "FAILED_BREAKOUT", "EXPIRED"}
ACTIONABLE = {"CONFIRMED", "PULLBACK_CONFIRMED"}


@dataclass
class Setup:
    side: str
    level: float
    range_high: float
    range_low: float
    atr: float
    trigger_time: datetime
    trigger_close: float
    trigger_relative_volume: float | None
    stop_distance: float
    target_distance: float
    state: str = "AWAITING_CONFIRMATION"
    bars_since: int = 0
    closes_beyond: int = 1
    retest_extreme: float | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"side": self.side, "state": self.state, "level": self.level, "range_high": self.range_high,
                "range_low": self.range_low, "atr_at_trigger": self.atr, "trigger_time": self.trigger_time.isoformat(),
                "trigger_close": self.trigger_close, "trigger_relative_volume": self.trigger_relative_volume,
                "bars_since_trigger": self.bars_since, "closes_beyond": self.closes_beyond,
                "retest_extreme": self.retest_extreme, "stop_distance": self.stop_distance,
                "target_distance": self.target_distance, "transitions": self.transitions[-8:]}


class BreakoutTracker:
    def __init__(self, cfg: dict[str, Any], breakout_lookback_bars: int):
        self.cfg = cfg
        self.lookback = int(breakout_lookback_bars)
        self.setup: Setup | None = None
        self.last_bar_time: datetime | None = None
        self.failures: list[dict[str, Any]] = []   # this session's failed breakouts, oldest first
        self.daily_range: float | None = None      # average daily range (2.8.2 ADR rulers)

    def set_daily_range(self, value: float | None) -> None:
        self.daily_range = float(value) if value is not None and value > 0 else None

    @property
    def state(self) -> str:
        return self.setup.state if self.setup else "NO_SETUP"

    @property
    def actionable_states(self) -> set[str]:
        return {"PULLBACK_CONFIRMED"} if self.cfg.get("pullback_only") else ACTIONABLE

    def _min_beyond(self, atr: float) -> float | None:
        """How far past the level price must be (None = unknown: the ADR ruler is on but the daily range is not known)."""
        need = self.cfg["min_distance_atr"] * atr
        adr_fraction = self.cfg.get("min_distance_adr") or 0
        if adr_fraction:
            if self.daily_range is None:
                return None
            need = max(need, adr_fraction * self.daily_range)
        return need

    def _retest_tolerance(self, atr: float) -> float:
        pull = self.cfg["pullback"]
        tolerance = pull["retest_tolerance_atr"] * atr
        if pull.get("retest_tolerance_adr") and self.daily_range is not None:
            tolerance = max(tolerance, pull["retest_tolerance_adr"] * self.daily_range)
        return tolerance

    def _move(self, state: str, bar: Bar, why: str) -> None:
        assert self.setup is not None
        self.setup.transitions.append({"at": bar.timestamp.isoformat(), "from": self.setup.state, "to": state, "why": why})
        self.setup.state = state

    def update(self, bar: Bar, signal, metrics: dict[str, Any]) -> dict[str, Any]:
        """Process one completed bar. `signal` is the core Signal for this bar or None; `metrics` its diagnostics."""
        if self.last_bar_time is not None and bar.timestamp <= self.last_bar_time:
            raise ValueError(f"confirmation tracker received bar {bar.timestamp.isoformat()} out of order")
        self.last_bar_time = bar.timestamp
        cfg, pull = self.cfg, self.cfg["pullback"]
        setup = self.setup
        if signal is not None and (setup is None or setup.state in TERMINAL or setup.side != signal.side):
            level = metrics.get("breakout_level_call" if signal.side == "CALL" else "breakout_level_put")
            if level is None or metrics.get("atr") is None:
                self.setup = None
                return self.result(bar, note="signal_without_levels")
            self.setup = Setup(signal.side, float(level), float(metrics["opening_range_high"]),
                               float(metrics["opening_range_low"]), float(metrics["atr"]), bar.timestamp, bar.close,
                               metrics.get("relative_volume"), float(metrics["stop_distance"]),
                               float(metrics["target_distance"]))
            self.setup.transitions.append({"at": bar.timestamp.isoformat(), "from": "NO_SETUP",
                                           "to": "AWAITING_CONFIRMATION", "why": "core_signal_qualified"})
            setup = self.setup
            self._check_confirmed(bar, self.setup.atr)
            return self.result(bar)
        if setup is None or setup.state in TERMINAL:
            return self.result(bar)

        setup.bars_since += 1
        direction = 1 if setup.side == "CALL" else -1
        close = bar.close
        atr = float(metrics.get("atr") or setup.atr)
        beyond = (close - setup.level) * direction
        back_inside = close < setup.range_high if direction == 1 else close > setup.range_low
        if back_inside:
            failed_state = "REJECTED_BREAKOUT" if setup.bars_since == 1 else "FAILED_BREAKOUT"
            self._move(failed_state, bar, "completed close back inside the opening range")
            self.failures.append({"side": setup.side, "level": setup.level, "at": bar.timestamp.isoformat(),
                                  "state": failed_state, "trigger_time": setup.trigger_time.isoformat()})
            self.failures = self.failures[-20:]
            return self.result(bar)
        setup.closes_beyond = setup.closes_beyond + 1 if beyond > 0 else 0
        touched = ((bar.low - setup.level) * direction if direction == 1 else (setup.level - bar.high)) <= self._retest_tolerance(atr)
        need = self._min_beyond(atr)

        if setup.state == "CONFIRMED" and cfg.get("pullback_only"):
            # 2.8.2: the breakout is proven but not bought; wait for a retest of the level.
            if touched and setup.bars_since <= pull["max_wait_bars"]:
                setup.retest_extreme = bar.high if direction == 1 else bar.low
                self._move("PULLBACK_HOLDING", bar, "price back at the level after a confirmed breakout; range not lost")
            elif setup.bars_since >= pull["max_wait_bars"]:
                self._move("EXPIRED", bar, "no retest of the level within pullback.max_wait_bars (pullback_only)")
            return self.result(bar)

        if setup.state in ACTIONABLE:
            if setup.bars_since > self.lookback or need is None or beyond < need:
                if pull["enabled"] and touched and setup.bars_since <= pull["max_wait_bars"]:
                    setup.retest_extreme = bar.high if direction == 1 else bar.low
                    self._move("PULLBACK_HOLDING", bar, "price back at the level; range not lost")
                elif setup.bars_since > self.lookback:
                    self._move("EXPIRED", bar, "more than breakout_lookback_bars since the breakout")
                else:
                    self._move("AWAITING_CONFIRMATION", bar, "no longer min_distance_atr beyond the level")
            return self.result(bar)

        if setup.state == "PULLBACK_HOLDING":
            if (setup.retest_extreme is not None and beyond > 0 and (close - setup.retest_extreme) * direction > 0
                    and need is not None and beyond >= need):
                self._move("PULLBACK_CONFIRMED", bar, "completed close beyond the level and the retest bar")
            elif touched:
                setup.retest_extreme = bar.high if direction == 1 else bar.low  # latest retest bar sets the trigger
            if setup.state != "PULLBACK_CONFIRMED" and setup.bars_since >= pull["max_wait_bars"]:
                self._move("EXPIRED", bar, "pullback not confirmed within pullback.max_wait_bars")
            return self.result(bar)

        # AWAITING_CONFIRMATION
        if self._check_confirmed(bar, atr):
            return self.result(bar)
        if pull["enabled"] and touched:
            setup.retest_extreme = bar.high if direction == 1 else bar.low
            self._move("PULLBACK_HOLDING", bar, "price back at the level; range not lost")
        elif setup.bars_since >= cfg["max_wait_bars"] and not pull["enabled"]:
            self._move("EXPIRED", bar, "not confirmed within max_wait_bars")
        elif setup.bars_since >= max(cfg["max_wait_bars"], pull["max_wait_bars"] if pull["enabled"] else 0):
            self._move("EXPIRED", bar, "not confirmed within the waiting windows")
        return self.result(bar)

    def _check_confirmed(self, bar: Bar, atr: float) -> bool:
        setup = self.setup
        assert setup is not None
        direction = 1 if setup.side == "CALL" else -1
        beyond = (bar.close - setup.level) * direction
        need = self._min_beyond(atr)
        if (setup.bars_since <= self.cfg["max_wait_bars"] and setup.closes_beyond >= self.cfg["confirm_closes"]
                and need is not None and beyond >= need):
            adr = f", {beyond / self.daily_range:.2f} daily ranges" if self.daily_range else ""
            self._move("CONFIRMED", bar, f"{setup.closes_beyond} completed closes beyond the level, "
                                         f"{beyond / atr:.2f} ATR beyond{adr}")
            return True
        return False

    def result(self, bar: Bar, note: str | None = None) -> dict[str, Any]:
        out = {"state": self.state, "actionable": self.state in self.actionable_states, "bar_utc": bar.timestamp.isoformat()}
        if self.daily_range is not None:
            out["daily_range"] = self.daily_range
        if self.setup:
            out["setup"] = self.setup.to_dict()
        if self.failures:
            out["failures"] = list(self.failures)
        if note:
            out["note"] = note
        return out
