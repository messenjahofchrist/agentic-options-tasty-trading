from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    commission_per_contract: float = 0.65
    exchange_fee_per_contract: float = 0.05
    regulatory_fee_per_contract: float = 0.0

    def estimate(self, contracts: int, legs: int = 1) -> float:
        if contracts < 0 or legs < 1:
            raise ValueError("contracts must be nonnegative and legs positive")
        return contracts * legs * (self.commission_per_contract + self.exchange_fee_per_contract + self.regulatory_fee_per_contract)


def economic_gate(*, entry: float, exit_price: float, contracts: int, spread_cost: float, slippage: float,
                  fees: float, risk_dollars: float, min_net_r: float) -> dict[str, float | bool]:
    if contracts <= 0 or risk_dollars <= 0:
        raise ValueError("contracts and risk_dollars must be positive")
    gross = (exit_price - entry) * 100 * contracts
    net = gross - spread_cost - slippage - fees
    expected_r = net / risk_dollars
    return {"gross_opportunity": gross, "spread_cost": spread_cost, "slippage": slippage, "fees": fees,
            "net_opportunity": net, "expected_r": expected_r, "accepted": expected_r >= min_net_r}
