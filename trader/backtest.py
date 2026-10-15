from __future__ import annotations

import csv
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .metrics import calculate_performance
from .models import Bar
from .strategy_engine import evaluate_underlying
from .strategy_loader import Strategy


def load_bars(path: str | Path) -> list[Bar]:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"Historical market-data CSV not found: {file.resolve()}. "
            "Supply a real 1-minute OHLCV file for SPY or QQQ; required columns are "
            "timestamp,open,high,low,close,volume (optional: vwap). "
            "Timestamps must include a UTC offset. No sample prices are generated."
        )
    with file.open(newline="", encoding="utf-8") as stream:
        rows = csv.DictReader(stream)
        needed = {"timestamp", "open", "high", "low", "close", "volume"}
        if not rows.fieldnames or not needed.issubset(rows.fieldnames):
            raise ValueError("CSV requires timestamp,open,high,low,close,volume columns; optional vwap")
        bars = []
        for row in rows:
            timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("CSV timestamps must include a timezone/UTC offset")
            bars.append(Bar(timestamp, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                            float(row["volume"]), float(row["vwap"]) if row.get("vwap") else None))
        return bars


def replay(bars: list[Bar], strategy: Strategy, *, in_sample_end: datetime, slippage_per_share: float = .01,
           fee_per_trade: float = 1.40) -> dict:
    """Underlying-only replay of the strategy's entries and exits; not an options-performance backtest.

    Like the live engine, each bar sees only its own ET session's bars up to that point (no future
    bars, no prior days). Each trade is then followed bar by bar to the strategy's underlying stop or
    target (stop assumed first if one bar touches both), or to the session close (the live bot carries
    overnight; the replay does not). No new entry is taken in a symbol while a trade is open, and
    entries per session are capped at risk.max_trades_per_day. P/L is per 100 shares of the underlying
    with a per-share slippage and per-trade fee haircut. Previously each trade was marked one minute
    after entry, which judged every trade on noise and could not reflect the strategy's exits.
    """
    train, test = [], []
    signals = []
    eastern = ZoneInfo("America/New_York")
    # Providers such as Massive include extended-hours bars; the live engine starts at the 09:30 open.
    bars = [bar for bar in sorted(bars, key=lambda bar: bar.timestamp)
            if time(9, 30) <= bar.timestamp.astimezone(eastern).time() < time(16, 0)]
    days = [bar.timestamp.astimezone(eastern).date() for bar in bars]
    max_per_day = int(strategy.risk["max_trades_per_day"])
    session_start, session_day, taken, flat_after = 0, None, 0, -1
    for index in range(len(bars)):
        day = days[index]
        if day != session_day:
            session_start, session_day, taken = index, day, 0
        if index <= flat_after or taken >= max_per_day:
            continue
        history = bars[session_start:index + 1]  # this session only; no future bars
        signal = evaluate_underlying(history, strategy)
        if not signal:
            continue
        taken += 1
        current = bars[index]
        side = 1 if signal.side == "CALL" else -1
        exit_index, exit_price, exit_reason = index, current.close, "session_close"
        for forward in range(index + 1, len(bars)):
            if days[forward] != day:
                break
            bar = bars[forward]
            exit_index, exit_price = forward, bar.close
            stop_hit = bar.low <= signal.stop_price if side > 0 else bar.high >= signal.stop_price
            target_hit = bar.high >= signal.target_price if side > 0 else bar.low <= signal.target_price
            if stop_hit:
                exit_price, exit_reason = signal.stop_price, "stop"
                break
            if target_hit:
                exit_price, exit_reason = signal.target_price, "target"
                break
        flat_after = exit_index
        gross = (exit_price - current.close) * side * 100
        net = gross - slippage_per_share * 200 - fee_per_trade
        row = {"timestamp": current.timestamp.isoformat(), "side": signal.side, "entry": current.close,
               "exit": exit_price, "exit_reason": exit_reason, "minutes_held": exit_index - index,
               "gross_pnl": gross, "net_pnl": net,
               "r_multiple": net / max(abs(current.close - signal.stop_price) * 100, .01), "fees": fee_per_trade,
               "slippage": slippage_per_share * 200}
        signals.append(row)
        (train if current.timestamp <= in_sample_end else test).append({**row, "realized_pnl": net})
    return {"model": "underlying proxy with strategy stop/target exits; excludes option selection/greeks/IV/option bid-ask",
            "trade_count": len(signals),
            "in_sample": calculate_performance(train).__dict__, "out_of_sample": calculate_performance(test).__dict__,
            "signals": signals}
