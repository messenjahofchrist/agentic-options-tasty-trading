"""V2's additional calculations, kept apart from the core strategy engine. EMA and ATR are the core's own
functions (trader.strategy_engine.ema / atr), called with the strategy's own periods; nothing here changes them.
Every function uses only the completed bars it is given (no look-ahead)."""
from __future__ import annotations

from datetime import datetime

from ..models import Bar
from ..strategy_engine import atr as core_atr, ema as core_ema


def session_bars(bars: list[Bar], session_open: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.timestamp >= session_open]


def session_vwap(bars: list[Bar]) -> float | None:
    """Cumulative session VWAP from typical price x volume. None until there is volume."""
    volume = sum(bar.volume for bar in bars)
    if volume <= 0:
        return None
    return sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in bars) / volume


def trend_efficiency(closes: list[float], window: int) -> float | None:
    """Net move / path length over the last `window` bar-to-bar moves (1 = straight line, ~0 = chop)."""
    if len(closes) < window + 1:
        return None
    sample = closes[-(window + 1):]
    path = sum(abs(b - a) for a, b in zip(sample, sample[1:]))
    return abs(sample[-1] - sample[0]) / path if path > 0 else 0.0


def ema_pair(closes: list[float], fast: int, slow: int) -> tuple[float, float] | None:
    if len(closes) < slow:
        return None
    return core_ema(closes, fast), core_ema(closes, slow)


def atr_fraction(bars: list[Bar], period: int) -> float | None:
    if len(bars) < period + 1 or bars[-1].close <= 0:
        return None
    return core_atr(bars, period) / bars[-1].close


def directional_bias(bars: list[Bar], *, fast: int, slow: int) -> dict:
    """Bias in [-3, 3] from three measurable votes: close vs session VWAP, EMA fast vs slow, close vs session open.
    A vote that cannot be computed is 0 and is listed in `missing`."""
    out = {"bias": 0, "votes": {}, "missing": []}
    if not bars:
        out["missing"] = ["bars"]
        return out
    close = bars[-1].close
    vwap = session_vwap(bars)
    pair = ema_pair([bar.close for bar in bars], fast, slow)
    session_open = bars[0].open
    votes = {
        "vwap": None if vwap is None else (1 if close > vwap else -1 if close < vwap else 0),
        "ema": None if pair is None else (1 if pair[0] > pair[1] else -1 if pair[0] < pair[1] else 0),
        "open": 1 if close > session_open else -1 if close < session_open else 0,
    }
    out["votes"] = votes
    out["missing"] = [name for name, vote in votes.items() if vote is None]
    out["bias"] = sum(vote for vote in votes.values() if vote is not None)
    out.update({"close": close, "vwap": vwap, "ema_fast": pair[0] if pair else None, "ema_slow": pair[1] if pair else None,
                "session_open": session_open,
                "change_from_open": (close - session_open) / session_open if session_open > 0 else None})
    return out


def change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old
