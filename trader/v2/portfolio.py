"""Portfolio exposure engine: evaluates a proposed trade against the whole ledger, not just its own contract.

Checks (each logged with its inputs):
  aggregate   open max loss (+ working entries) + this trade <= aggregate cap (owner-authorized fraction of equity)
  cluster     max loss of open positions that are the same bet (correlation.py) + this trade <= one trade budget
  buying power  the entry's cash cost <= ledger cash
Directional exposure (delta dollars of open positions) is reported, not limited: no such limit is authorized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .correlation import CorrelationModel, max_loss


@dataclass(frozen=True)
class Budgets:
    """What one trade, one cluster and the whole book may lose, in dollars, at this moment."""
    per_trade: float
    aggregate_cap: float
    aggregate_open: float
    cluster_cap: float

    @property
    def aggregate_remaining(self) -> float:
        return max(0.0, self.aggregate_cap - self.aggregate_open)


def open_max_loss(positions: list[dict], pending: list[dict] | None = None) -> float:
    return sum(max_loss(p) for p in positions) + sum(float(p.get("max_loss", 0)) for p in (pending or []))


def budgets_for(*, equity: float, per_trade_budget: float, aggregate_fraction: float, positions: list[dict],
                pending: list[dict] | None = None) -> Budgets:
    return Budgets(per_trade=max(0.0, per_trade_budget), aggregate_cap=max(0.0, equity) * aggregate_fraction,
                   aggregate_open=open_max_loss(positions, pending), cluster_cap=max(0.0, per_trade_budget))


def cluster_exposure(model: CorrelationModel, symbol: str, side: str, positions: list[dict]) -> tuple[float, list[dict]]:
    related = model.related_positions(symbol, side, positions)
    return sum(item["max_loss"] for item in related), related


def directional_exposure(positions: list[dict], spot: dict[str, float]) -> dict[str, Any]:
    """Approximate delta dollars by underlying, from the delta recorded at entry (refreshed deltas are not streamed)."""
    by_symbol, total = {}, 0.0
    for p in positions:
        delta = p.get("entry_delta")
        price = spot.get(str(p["underlying"]))
        if delta is None or price is None:
            continue
        dollars = float(delta) * 100 * int(p["quantity"]) * price
        by_symbol[str(p["underlying"])] = by_symbol.get(str(p["underlying"]), 0.0) + dollars
        total += dollars
    return {"delta_dollars_total": round(total, 2), "by_underlying": {k: round(v, 2) for k, v in by_symbol.items()},
            "basis": "entry delta x 100 x quantity x current underlying price"}


@dataclass
class PortfolioCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def evaluate(*, model: CorrelationModel, symbol: str, side: str, candidate_max_loss: float, entry_cash: float,
             cash: float, positions: list[dict], budgets: Budgets) -> PortfolioCheck:
    cluster_open, related = cluster_exposure(model, symbol, side, positions)
    aggregate_after = budgets.aggregate_open + candidate_max_loss
    cluster_after = cluster_open + candidate_max_loss
    reasons = []
    if aggregate_after > budgets.aggregate_cap + 1e-9:
        reasons.append(f"aggregate_max_loss_cap: ${budgets.aggregate_open:,.2f} open + ${candidate_max_loss:,.2f} "
                       f"> ${budgets.aggregate_cap:,.2f}")
    if cluster_after > budgets.cluster_cap + 1e-9:
        names = ",".join(f"{r['underlying']} {r['side']}" for r in related)
        reasons.append(f"correlated_cluster_cap: ${cluster_open:,.2f} in [{names}] + ${candidate_max_loss:,.2f} "
                       f"> ${budgets.cluster_cap:,.2f}")
    if entry_cash > cash + 1e-9:
        reasons.append(f"buying_power: entry cost ${entry_cash:,.2f} > cash ${cash:,.2f}")
    return PortfolioCheck(not reasons, reasons, {
        "aggregate_open_max_loss": round(budgets.aggregate_open, 2), "aggregate_after": round(aggregate_after, 2),
        "aggregate_cap": round(budgets.aggregate_cap, 2), "cluster_open_max_loss": round(cluster_open, 2),
        "cluster_after": round(cluster_after, 2), "cluster_cap": round(budgets.cluster_cap, 2),
        "correlated_positions": related, "candidate_max_loss": round(candidate_max_loss, 2),
        "entry_cash": round(entry_cash, 2), "cash": round(cash, 2)})
