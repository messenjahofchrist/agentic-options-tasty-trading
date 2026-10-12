from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskState:
    equity: float
    daily_pnl: float
    weekly_pnl: float
    consecutive_losses: int
    trades_today: int
    open_positions: int
    open_underlyings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    quantity: int
    risk_dollars: float
    reason: str
    risk_budget_dollars: float = 0.0
    premium_exposure_dollars: float = 0.0
    modeled_costs_dollars: float = 0.0
    planned_risk_dollars: float = 0.0

    @property
    def modeled_risk_dollars(self) -> float:
        return self.risk_dollars


def assess_entry(*, state: RiskState, contract_price: float, strategy_risk: dict, max_contracts: int,
                 data_fresh: bool, account_certain: bool, reconciled: bool, protection_verified: bool,
                 estimated_entry_slippage_per_contract: float = 0.0,
                 estimated_exit_slippage_per_contract: float = 0.0,
                 estimated_round_trip_fees_per_contract: float = 0.0,
                 underlying: str | None = None,
                 execution_errors: int = 0) -> RiskDecision:
    risk_budget = max(0.0, min(state.equity * float(strategy_risk["max_risk_per_trade_fraction"]),
                               state.equity * float(strategy_risk["hard_max_risk_per_trade_fraction"])))
    premium_per_contract = max(0.0, contract_price) * 100
    costs_per_contract = sum(max(0.0, value) for value in (estimated_entry_slippage_per_contract,
                              estimated_exit_slippage_per_contract, estimated_round_trip_fees_per_contract))
    modeled_risk_per_contract = premium_per_contract + costs_per_contract
    blocks = []
    if not data_fresh: blocks.append("stale_market_data")
    if not account_certain: blocks.append("uncertain_account_state")
    if not reconciled: blocks.append("reconciliation_failed")
    if not protection_verified: blocks.append("protection_unverified")
    if execution_errors >= 3: blocks.append("repeated_execution_errors")
    if state.daily_pnl <= -state.equity * float(strategy_risk["max_daily_loss_fraction"]): blocks.append("daily_loss_limit")
    if state.weekly_pnl <= -state.equity * float(strategy_risk["max_weekly_loss_fraction"]): blocks.append("weekly_loss_limit")
    if state.consecutive_losses >= int(strategy_risk["max_consecutive_losses"]): blocks.append("consecutive_loss_limit")
    if state.trades_today >= int(strategy_risk["max_trades_per_day"]): blocks.append("trade_count_limit")
    if state.open_positions >= int(strategy_risk["max_open_positions"]): blocks.append("open_position_limit")
    if underlying is not None and underlying in state.open_underlyings:
        blocks.append("underlying_position_already_open_no_averaging")
    if contract_price <= 0: blocks.append("invalid_contract_price")
    if max_contracts < 1: blocks.append("invalid_contract_limit")
    cost_inputs = (estimated_entry_slippage_per_contract, estimated_exit_slippage_per_contract,
                   estimated_round_trip_fees_per_contract)
    if any(value < 0 for value in cost_inputs): blocks.append("invalid_cost_estimate")
    if blocks:
        return RiskDecision(False, 0, modeled_risk_per_contract, ",".join(blocks),
                            risk_budget, premium_per_contract, costs_per_contract, risk_budget)
    # The underlying ATR stop is only an exit trigger. It cannot bound option-dollar loss.
    # Budget against losing the full premium plus modeled round-trip trading costs.
    quantity = min(int((risk_budget + 1e-9) // modeled_risk_per_contract), int(max_contracts))
    if quantity < 1:
        return RiskDecision(False, 0, modeled_risk_per_contract, "premium_risk_budget_exceeded",
                            risk_budget, premium_per_contract, costs_per_contract, risk_budget)
    premium_exposure = quantity * premium_per_contract
    modeled_costs = quantity * costs_per_contract
    modeled_risk = premium_exposure + modeled_costs
    return RiskDecision(True, quantity, modeled_risk, "risk_limits_passed", risk_budget,
                        premium_exposure, modeled_costs, risk_budget)
