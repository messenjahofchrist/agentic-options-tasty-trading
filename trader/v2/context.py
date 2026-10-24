"""Market-context engine: whole-market and sector state from shared data, with provenance and staleness.

The engine's single DXLink feed supplies 1-minute bars for the trading universe and the context symbols (SPY,
QQQ, IWM, sector ETFs); this class only references those bar lists, never fetches them. Prior-session levels and
daily closes come from the shared daily-history cache, premarket ranges from the shared premarket cache, VIX
from the shared REST quote cache (see datahub). Missing or stale inputs are reported, never filled in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Bar
from . import indicators


@dataclass
class DailyLevels:
    """Most recent completed regular session before today."""
    session_date: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class VixReading:
    value: float
    previous_close: float | None
    timestamp: datetime
    source: str


@dataclass
class MarketContext:
    broad_symbols: tuple[str, ...]
    sector_etfs: tuple[str, ...]
    ema_fast: int
    ema_slow: int
    atr_period: int
    efficiency_window: int
    max_bar_age_seconds: float
    vix_max_age_seconds: float
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    prior: dict[str, DailyLevels] = field(default_factory=dict)
    premarket: dict[str, dict[str, Any]] = field(default_factory=dict)
    vix: VixReading | None = None
    unavailable: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, symbol: str, bars: list[Bar]) -> None:
        """Share the engine's own bar list for a symbol (no copy, no second subscription)."""
        self.bars[symbol] = bars

    def mark_unavailable(self, symbol: str, reason: str) -> None:
        self.unavailable[symbol] = reason

    def fresh(self, symbol: str, now: datetime) -> bool:
        bars = self.bars.get(symbol)
        if not bars or symbol in self.unavailable:
            return False
        # A 1-minute bar stamped T completes at T+1m.
        age = (now - (bars[-1].timestamp + timedelta(minutes=1))).total_seconds()
        return age <= self.max_bar_age_seconds

    def vix_fresh(self, now: datetime) -> bool:
        # `now` is the bar time; a VIX quote fetched seconds after the bar closed is newer than it, not stale.
        return self.vix is not None and -90 <= (now - self.vix.timestamp).total_seconds() <= self.vix_max_age_seconds

    def symbol_state(self, symbol: str, now: datetime) -> dict[str, Any]:
        bars = self.bars.get(symbol) or []
        state: dict[str, Any] = {"symbol": symbol, "fresh": self.fresh(symbol, now), "bars": len(bars),
                                 "last_bar_utc": bars[-1].timestamp.isoformat() if bars else None,
                                 "unavailable": self.unavailable.get(symbol)}
        if not bars:
            return state
        bias = indicators.directional_bias(bars, fast=self.ema_fast, slow=self.ema_slow)
        state.update(bias)
        closes = [bar.close for bar in bars]
        state["efficiency"] = indicators.trend_efficiency(closes, self.efficiency_window)
        state["atr_fraction"] = indicators.atr_fraction(bars, self.atr_period)
        state["momentum_15"] = indicators.change(closes[-1], closes[-16]) if len(closes) >= 16 else None
        prior = self.prior.get(symbol)
        if prior:
            close = closes[-1]
            state.update({"prior_session": prior.session_date, "prior_high": prior.high, "prior_low": prior.low,
                          "prior_close": prior.close,
                          "change_from_prior_close": indicators.change(close, prior.close),
                          "overnight_gap": indicators.change(bars[0].open, prior.close),
                          "vs_prior_range": ("above_prior_high" if close > prior.high else
                                             "below_prior_low" if close < prior.low else "inside_prior_range")})
        pre = self.premarket.get(symbol)
        if pre and pre.get("high") is not None:
            close = closes[-1]
            state.update({"premarket_high": pre["high"], "premarket_low": pre["low"],
                          "vs_premarket_range": ("above_premarket_high" if close > pre["high"] else
                                                 "below_premarket_low" if close < pre["low"] else "inside_premarket_range")})
        return state

    def breadth(self, symbols: list[str], now: datetime) -> dict[str, Any]:
        """Share of fresh symbols trading above / below their own session VWAP."""
        above = below = counted = 0
        for symbol in symbols:
            bars = self.bars.get(symbol)
            if not bars or not self.fresh(symbol, now):
                continue
            vwap = indicators.session_vwap(bars)
            if vwap is None:
                continue
            counted += 1
            above += bars[-1].close > vwap
            below += bars[-1].close < vwap
        return {"counted": counted, "above_vwap": above, "below_vwap": below,
                "above_fraction": above / counted if counted else None,
                "below_fraction": below / counted if counted else None}

    def snapshot(self, now: datetime, breadth_symbols: list[str]) -> dict[str, Any]:
        broad = {symbol: self.symbol_state(symbol, now) for symbol in self.broad_symbols}
        sectors = {symbol: self.symbol_state(symbol, now) for symbol in self.sector_etfs}
        spy_change = broad.get("SPY", {}).get("change_from_open")
        for state in sectors.values():
            change_from_open = state.get("change_from_open")
            state["relative_strength_vs_spy"] = (None if change_from_open is None or spy_change is None
                                                 else change_from_open - spy_change)
        vix = None
        if self.vix is not None:
            vix = {"value": self.vix.value, "previous_close": self.vix.previous_close,
                   "change_fraction": indicators.change(self.vix.value, self.vix.previous_close),
                   "timestamp_utc": self.vix.timestamp.isoformat(), "fresh": self.vix_fresh(now), "source": self.vix.source}
        return {"generated_at_utc": now.astimezone(timezone.utc).isoformat(), "broad": broad, "sectors": sectors,
                "vix": vix, "breadth": self.breadth(breadth_symbols, now), "unavailable": dict(self.unavailable),
                "provenance": dict(self.provenance)}
