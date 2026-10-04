from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .metrics import calculate_performance
from .models import Bar
from .strategy_engine import evaluate_underlying
from .strategy_loader import Strategy


def load_bars(path: str | Path) -> list[Bar]:
    with Path(path).open(newline="", encoding="utf-8") as stream:
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
    """Simple underlying-only signal replay; not an options-performance backtest."""
    train, test = [], []
    signals = []
    for index in range(1, len(bars)):
        history = bars[:index + 1]  # no future bars passed to the signal engine
        signal = evaluate_underlying(history, strategy)
        if not signal:
            continue
        current = bars[index]
        side = 1 if signal.side == "CALL" else -1
        # One-bar forward mark is illustrative only; cost haircut makes it a rough screen, not option P/L.
        if index + 1 >= len(bars):
            continue
        next_bar = bars[index + 1]
        gross = (next_bar.close - current.close) * side * 100
        net = gross - slippage_per_share * 200 - fee_per_trade
        row = {"timestamp": current.timestamp.isoformat(), "side": signal.side, "gross_pnl": gross, "net_pnl": net,
               "r_multiple": net / max(abs(current.close - signal.stop_price) * 100, .01), "fees": fee_per_trade,
               "slippage": slippage_per_share * 200}
        signals.append(row)
        (train if current.timestamp <= in_sample_end else test).append({**row, "realized_pnl": net})
    return {"model": "underlying proxy; excludes option selection/greeks/IV/option bid-ask", "trade_count": len(signals),
            "in_sample": calculate_performance(train).__dict__, "out_of_sample": calculate_performance(test).__dict__,
            "signals": signals}
