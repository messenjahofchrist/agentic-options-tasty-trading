"""Risk-based position sizing for long options (full premium plus modeled round-trip costs at risk).

quantity = min(hard contract cap,
               floor(min(per-trade budget, remaining aggregate budget, remaining cluster budget, cash) / max loss per contract),
               floor(displayed size x max_fraction_of_displayed_size),
               floor(open interest x max_fraction_of_open_interest))
The setup-quality score is recorded but has no effect on size (quality_score_affects_size must stay false until a
calibration against history exists). The uncapped risk-based quantity is logged so its effect can be studied.
"""
from __future__ import annotations

import math
from typing import Any


def size_long_option(*, max_loss_per_contract: float, per_trade_budget: float, aggregate_remaining: float,
                     cluster_remaining: float, cash: float, cash_per_contract: float, bid_size: int, ask_size: int,
                     open_interest: int, cfg: dict[str, Any], hard_max_contracts: int) -> dict[str, Any]:
    if cfg["quality_score_affects_size"]:
        raise ValueError("quality_score_affects_size requires a documented calibration; refusing to size with it")
    if max_loss_per_contract <= 0 or cash_per_contract <= 0:
        return {"quantity": 0, "risk_based_quantity": 0, "limiting_factor": "invalid_contract_cost"}
    budget = min(per_trade_budget, aggregate_remaining, cluster_remaining)
    by_risk = int(math.floor((budget + 1e-9) / max_loss_per_contract))
    by_cash = int(math.floor((cash + 1e-9) / cash_per_contract))
    # The fraction keeps larger orders inside visible liquidity; one contract against a displayed size of at least 1
    # is fillable at the touch, so the cap never rounds a 1-lot down to zero (2026-10-23: SPY showed 1 x 93).
    shown = min(bid_size, ask_size)
    by_size = max(1, int(math.floor(shown * cfg["max_fraction_of_displayed_size"]))) if shown >= 1 else 0
    by_oi = int(math.floor(open_interest * cfg["max_fraction_of_open_interest"]))
    cap = min(int(cfg["max_contracts"]), int(hard_max_contracts))
    limits = {"risk_budget": by_risk, "cash": by_cash, "displayed_size": by_size, "open_interest": by_oi, "contract_cap": cap}
    quantity = max(0, min(limits.values()))
    limiting = min(limits, key=lambda name: (limits[name], name != "contract_cap"))
    return {"quantity": quantity, "risk_based_quantity": max(0, min(by_risk, by_cash, by_size, by_oi)),
            "limits": limits, "limiting_factor": limiting, "budget_dollars": round(budget, 2),
            "max_loss_per_contract": round(max_loss_per_contract, 2)}
