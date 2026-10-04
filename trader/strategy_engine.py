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


def opening_range(bars: list[Bar], minutes: int, timezone: str = "America/New_York") -> tuple[float, float] | None:
    if not bars:
        return None
    local_date = bars[-1].timestamp.astimezone(ZoneInfo(timezone)).date()
    today = [b for b in bars if b.timestamp.astimezone(ZoneInfo(timezone)).date() == local_date]
    today.sort(key=lambda b: b.timestamp)
    if not today:
        return None
    opening = time(9, 30)
    start = next((i for i, b in enumerate(today) if b.timestamp.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None) >= opening), None)
    if start is None:
        return None
    base = today[start].timestamp
    first = [b for b in today[start:] if (b.timestamp - base).total_seconds() < minutes * 60]
    if len(first) < minutes:
        return None
    return max(b.high for b in first), min(b.low for b in first)


def evaluate_underlying(bars: list[Bar], strategy: Strategy) -> Signal | None:
    rules = strategy.rules
    if len(bars) < max(int(rules["trend_ema_slow"]), int(rules["atr_period"])) + 2:
        return None
    ordered = sorted(bars, key=lambda b: b.timestamp)
    values = [b.close for b in ordered]
    fast = ema(values, int(rules["trend_ema_fast"]))
    slow = ema(values, int(rules["trend_ema_slow"]))
    volatility = atr(ordered, int(rules["atr_period"]))
    price = ordered[-1].close
    if price <= 0 or volatility / price > float(rules["max_atr_fraction"]):
        return None
    op = opening_range(ordered, strategy.opening_range_minutes)
    if not op:
        return None
    high, low = op
    volume_window = ordered[-20:]
    average_volume = sum(b.volume for b in volume_window[:-1]) / max(1, len(volume_window) - 1)
    relative_volume = ordered[-1].volume / average_volume if average_volume > 0 else 0
    buffer = volatility * float(rules["breakout_buffer_atr"])
    current = ordered[-1]
    previous = ordered[-2]
    # Require a close beyond the range and a preceding close inside it; this avoids repeatedly entering late.
    bullish = fast > slow and current.close > high + buffer and previous.close <= high + buffer
    bearish = fast < slow and current.close < low - buffer and previous.close >= low - buffer
    vwap_ok = current.vwap is None or (bullish and current.close > current.vwap) or (bearish and current.close < current.vwap)
    metrics = {"ema_fast": fast, "ema_slow": slow, "atr": volatility, "relative_volume": relative_volume,
               "opening_range_high": high, "opening_range_low": low, "vwap": current.vwap}
    if relative_volume < float(rules["relative_volume_min"]) or not vwap_ok:
        return None
    if bullish:
        return Signal("CALL", "opening_range_breakout_up_trend_volume_vwap", price - volatility * float(rules["stop_underlying_atr"]),
                      price + volatility * float(rules["stop_underlying_atr"]) * float(rules["take_profit_r"]), metrics)
    if bearish:
        return Signal("PUT", "opening_range_breakout_down_trend_volume_vwap", price + volatility * float(rules["stop_underlying_atr"]),
                      price - volatility * float(rules["stop_underlying_atr"]) * float(rules["take_profit_r"]), metrics)
    return None
