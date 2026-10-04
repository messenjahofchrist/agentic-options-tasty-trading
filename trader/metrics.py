from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class Performance:
    trade_count: int
    win_rate: float
    expectancy: float
    average_r: float
    median_r: float
    profit_factor: float | None
    max_drawdown: float
    total_pnl: float
    fees: float
    slippage: float


def calculate_performance(trades: list[dict]) -> Performance:
    pnl = [float(t.get("realized_pnl", 0)) for t in trades]
    r_values = [float(t.get("r_multiple", 0)) for t in trades]
    wins = [v for v in pnl if v > 0]
    losses = [v for v in pnl if v < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for value in pnl:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    return Performance(len(trades), len(wins) / len(trades) if trades else 0,
                       sum(pnl) / len(pnl) if pnl else 0, sum(r_values) / len(r_values) if r_values else 0,
                       median(r_values) if r_values else 0, gross_win / gross_loss if gross_loss else (None if gross_win else 0),
                       drawdown, sum(pnl), sum(float(t.get("fees", 0)) for t in trades), sum(float(t.get("slippage", 0)) for t in trades))
