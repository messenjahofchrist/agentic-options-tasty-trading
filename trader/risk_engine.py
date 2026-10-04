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


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    quantity: int
    risk_dollars: float
    reason: str


def assess_entry(*, state: RiskState, contract_price: float, strategy_risk: dict, max_contracts: int,
                 data_fresh: bool, account_certain: bool, reconciled: bool, protection_verified: bool,
                 execution_errors: int = 0) -> RiskDecision:
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
    if contract_price <= 0: blocks.append("invalid_contract_price")
    if blocks:
        return RiskDecision(False, 0, 0.0, ",".join(blocks))
    risk = min(state.equity * float(strategy_risk["max_risk_per_trade_fraction"]),
               state.equity * float(strategy_risk["hard_max_risk_per_trade_fraction"]))
    per_contract = contract_price * 100
    quantity = min(int(risk // per_contract), int(max_contracts))
    if quantity < 1:
        return RiskDecision(False, 0, 0.0, "contract_cost_exceeds_risk_budget")
    return RiskDecision(True, quantity, quantity * per_contract, "risk_limits_passed")
