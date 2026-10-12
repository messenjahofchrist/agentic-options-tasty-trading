from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .models import Bar
from .strategy_loader import Strategy


@dataclass(frozen=True)
class Signal:
    side: str
    reason: str
    stop_price: float
    target_price: float
    metrics: dict[str, float | str | None]


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("Not enough data for EMA")
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1 - alpha) * value
    return value


def atr(bars: list[Bar], period: int) -> float:
    if len(bars) < period + 1:
        raise ValueError("Not enough data for ATR")
    ranges = [max(b.high - b.low, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close)) for i, b in enumerate(bars[1:], 1)]
    return sum(ranges[-period:]) / period


def opening_range(bars: list[Bar], minutes: int, timezone: str = "America/New_York",
                  start_at: datetime | None = None) -> tuple[float, float] | None:
    if not bars:
        return None
    local_date = bars[-1].timestamp.astimezone(ZoneInfo(timezone)).date()
    today = [b for b in bars if b.timestamp.astimezone(ZoneInfo(timezone)).date() == local_date]
    today.sort(key=lambda b: b.timestamp)
    if not today:
        return None
    if start_at is None:
        opening = time(9, 30)
        start = next((i for i, b in enumerate(today) if b.timestamp.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None) >= opening), None)
        if start is None:
            return None
        base = today[start].timestamp
    else:
        base = start_at
    first = [b for b in today if 0 <= (b.timestamp - base).total_seconds() < minutes * 60]
    if len(first) < minutes or any((first[i].timestamp - first[i - 1].timestamp).total_seconds() > 60
                                   for i in range(1, len(first))):
        return None
    return max(b.high for b in first), min(b.low for b in first)


def evaluate_underlying(bars: list[Bar], strategy: Strategy, *, opening_range_start: datetime | None = None) -> Signal | None:
    return evaluate_underlying_diagnostics(bars, strategy, opening_range_start=opening_range_start)[0]


def evaluate_underlying_diagnostics(
    bars: list[Bar], strategy: Strategy, *, opening_range_start: datetime | None = None
) -> tuple[Signal | None, dict[str, float | str | None], str]:
    rules = strategy.rules
    metrics: dict[str, float | str | None] = {
        "opening_range_high": None, "opening_range_low": None, "ema_fast": None, "ema_slow": None,
        "atr": None, "vwap": None, "volume": None, "average_volume": None, "relative_volume": None,
        "breakout_level_call": None, "breakout_level_put": None, "signal_direction": None,
    }
    if not bars:
        return None, metrics, "no_minute_bars"
    ordered = sorted(bars, key=lambda b: b.timestamp)
    op = opening_range(ordered, strategy.opening_range_minutes, start_at=opening_range_start)
    if not op:
        return None, metrics, "opening_range_not_established"
    high, low = op
    metrics.update({"opening_range_high": high, "opening_range_low": low})
    if len(ordered) < max(int(rules["trend_ema_slow"]), int(rules["atr_period"])) + 2:
        return None, metrics, "indicator_history_warming_up"
    values = [b.close for b in ordered]
    fast = ema(values, int(rules["trend_ema_fast"]))
    slow = ema(values, int(rules["trend_ema_slow"]))
    volatility = atr(ordered, int(rules["atr_period"]))
    price = ordered[-1].close
    current = ordered[-1]
    previous = ordered[-2]
    volume_window = ordered[-20:]
    average_volume = sum(b.volume for b in volume_window[:-1]) / max(1, len(volume_window) - 1)
    relative_volume = current.volume / average_volume if average_volume > 0 else 0.0
    buffer = volatility * float(rules["breakout_buffer_atr"])
    breakout_up = high + buffer
    breakout_down = low - buffer
    cross_up = current.close > breakout_up and previous.close <= breakout_up
    cross_down = current.close < breakout_down and previous.close >= breakout_down
    metrics.update({"ema_fast": fast, "ema_slow": slow, "ema9": fast, "ema21": slow,
                    "atr": volatility, "atr14": volatility, "vwap": current.vwap,
                    "volume": current.volume, "average_volume": average_volume,
                    "relative_volume": relative_volume, "volume_ratio": relative_volume,
                    "breakout_level_call": breakout_up,
                    "breakout_level_put": breakout_down})
    if price <= 0 or volatility / price > float(rules["max_atr_fraction"]):
        return None, metrics, "atr_filter_failed"
    if not cross_up and not cross_down:
        return None, metrics, "breakout_not_triggered"
    bullish, bearish = cross_up and fast > slow, cross_down and fast < slow
    if not bullish and not bearish:
        return None, metrics, "ema_filter_failed"
    if relative_volume < float(rules["relative_volume_min"]):
        return None, metrics, "volume_filter_failed"
    vwap_ok = current.vwap is None or (bullish and current.close > current.vwap) or (bearish and current.close < current.vwap)
    if not vwap_ok:
        return None, metrics, "vwap_filter_failed"
    metrics["signal_direction"] = "CALL" if bullish else "PUT"
    if bullish:
        return Signal("CALL", "opening_range_breakout_up_trend_volume_vwap", price - volatility * float(rules["stop_underlying_atr"]),
                      price + volatility * float(rules["target_atr_multiple"]), metrics), metrics, "signal_qualified"
    if bearish:
        return Signal("PUT", "opening_range_breakout_down_trend_volume_vwap", price + volatility * float(rules["stop_underlying_atr"]),
                      price - volatility * float(rules["target_atr_multiple"]), metrics), metrics, "signal_qualified"
    return None, metrics, "ema_filter_failed"
