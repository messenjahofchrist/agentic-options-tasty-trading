from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .logging_utils import append_jsonl
from .fees import FeeSchedule, economic_gate
from .models import Bar, Decision
from .option_selector import evaluate_options
from .risk_engine import RiskState, assess_entry
from .strategy_engine import evaluate_underlying_diagnostics
from .strategy_loader import Strategy, verify_manifest
from .watchdog import KillSwitch


def decision_cycle(*, strategy: Strategy, strategy_path: str | Path, manifest_path: str | Path,
                   bars_by_symbol: dict[str, list[Bar]], option_chain_by_symbol: dict[str, list],
                   risk_state: RiskState, now: datetime, data_fresh: bool, account_certain: bool,
                   reconciled: bool, protection_verified: bool, market_open: bool, within_entry_window: bool,
                   decision_log: str | Path, kill_switch_path: str | Path = "KILL_SWITCH",
                   paper_execution: bool = False, paper_test_mode: bool = False,
                   opening_range_start: datetime | None = None,
                   data_issues: list[str] | None = None) -> list[Decision]:
    if paper_test_mode and not paper_execution:
        raise ValueError("paper_test_mode requires paper_execution")
    verify_manifest(strategy_path, manifest_path)
    killed = KillSwitch(kill_switch_path).entries_blocked()
    decisions = []
    for symbol in strategy.universe:
        bars = bars_by_symbol.get(symbol, [])
        price = bars[-1].close if bars else None
        signal, signal_metrics, signal_reason = evaluate_underlying_diagnostics(
            bars, strategy, opening_range_start=opening_range_start)
        diag = {"mode": "PAPER_TEST" if paper_test_mode else "PAPER",
                "timestamp_et": now.astimezone(ZoneInfo("America/New_York")).isoformat(),
                "strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
                "strategy_hash": strategy.sha256, "bars": len(bars),
                "entry_window_et": dict(strategy.entry_window_et),
                "opening_range_start": opening_range_start.isoformat() if opening_range_start else None,
                "paper_test_mode": paper_test_mode,
                "underlying_signal": signal.reason if signal else None,
                "underlying_filter_reason": signal_reason, **signal_metrics,
                "data_issues": list(data_issues or []),
                "trend_state": "bullish" if signal and signal.side == "CALL" else "bearish" if signal else None,
                "timeframe_state": f"{strategy.bar_interval_minutes}m", "breakout_state": signal.reason if signal else None,
                "relative_volume": signal_metrics.get("relative_volume"),
                "volatility_state": {"atr": signal_metrics.get("atr")}, "option_candidate": None,
                "bid": None, "ask": None, "mid": None, "spread": None, "greeks": None,
                "iv": None, "iv_rank": None, "dte": None, "expected_entry": None, "expected_exit": None,
                "option_candidates": len(option_chain_by_symbol.get(symbol, [])), "gross_opportunity": None, "net_opportunity": None}
        if killed:
            decision, reason = "NO_TRADE", "kill_switch_active"
        elif not market_open or not within_entry_window:
            decision, reason = "NO_TRADE", "market_closed_or_outside_entry_window"
        elif not data_fresh or not account_certain or not reconciled or not protection_verified:
            failures = [name for name, passed in (("data_freshness", data_fresh), ("account_state", account_certain),
                        ("reconciliation", reconciled), ("protection", protection_verified)) if not passed]
            decision, reason = "NO_TRADE", "safety_gate_blocked:" + ",".join(failures)
        elif not signal:
            decision, reason = "NO_TRADE", signal_reason
        else:
            diag.update({"trend_state": "bullish" if signal.side == "CALL" else "bearish",
                         "breakout_state": signal.reason, "relative_volume": signal.metrics.get("relative_volume"),
                         "volatility_state": {"atr": signal.metrics.get("atr")}, "signal_metrics": signal.metrics,
                         "underlying_stop_trigger": signal.stop_price,
                         "underlying_target_trigger": signal.target_price,
                         "atr_stop_is_dollar_loss_cap": False})
            selected, option_audit = evaluate_options(option_chain_by_symbol.get(symbol, []), signal.side, now, strategy)
            entry_slippage_estimate = float(strategy.execution["slippage_per_contract"])
            round_trip_fees_per_contract = FeeSchedule(
                commission_per_contract=float(strategy.execution["commission_per_contract"]),
                exchange_fee_per_contract=float(strategy.execution["exchange_fee_per_contract"]),
            ).estimate(1, legs=2)
            max_risk_fraction = min(float(strategy.risk["max_risk_per_trade_fraction"]),
                                    float(strategy.risk["hard_max_risk_per_trade_fraction"]))
            risk_budget = max(0.0, risk_state.equity * max_risk_fraction)
            for row in option_audit:
                ask = row["ask"]
                row.update({"quote_timestamp": next((q.quote_timestamp.isoformat() for q in option_chain_by_symbol.get(symbol, [])
                                                       if q.symbol == row["contract"] and q.quote_timestamp), None),
                            "entry_slippage_estimate": entry_slippage_estimate,
                            "exit_slippage_estimate": entry_slippage_estimate,
                            "estimated_round_trip_fees": round_trip_fees_per_contract,
                            "premium_exposure_dollars": ask * 100 if ask is not None else None,
                            "modeled_risk_dollars": ask * 100 + 2 * entry_slippage_estimate + round_trip_fees_per_contract
                                if ask is not None else None,
                            "risk_budget_dollars": risk_budget,
                            "economic_gate_result": "not_evaluated_until_option_selected"})
            diag["option_evaluations"] = option_audit
            if not selected:
                rejections = sorted({reason for row in option_audit for reason in row["rejection_reasons"]})
                diag["rejection_reasons"] = rejections or ["no_option_contracts_returned"]
                decision, reason = "NO_TRADE", "option_filters_rejected_candidates:" + ",".join(diag["rejection_reasons"])
            else:
                option = selected.quote
                execution_slippage = float(strategy.execution["slippage_per_contract"])
                diag.update({"option_candidate": option.symbol, "option": option.symbol, "bid": option.bid, "ask": option.ask,
                             "expiration": option.expiration.isoformat(), "mid": option.mid,
                             "spread": option.spread, "spread_dollars": option.spread,
                             "spread_fraction": option.spread_fraction, "spread_percent": option.spread_fraction * 100,
                             "max_spread_dollars": float(strategy.rules["max_spread_dollars"]),
                             "max_spread_percent": float(strategy.rules["max_spread_fraction"]) * 100,
                             "greeks": {"delta": option.delta, "gamma": option.gamma, "theta": option.theta, "vega": option.vega},
                             "iv": option.iv, "iv_rank": option.iv_rank,
                             "dte": (option.expiration.date() - now.date()).days,
                             "expected_entry": option.ask, "expected_exit": None,
                             "volume": option.volume, "open_interest": option.open_interest,
                             "quote_timestamp": option.quote_timestamp.isoformat() if option.quote_timestamp else None})
                risk = assess_entry(state=risk_state, contract_price=selected.quote.ask, strategy_risk=strategy.risk,
                                    max_contracts=int(strategy.rules["max_contracts"]), data_fresh=data_fresh,
                                    account_certain=account_certain, reconciled=reconciled, protection_verified=protection_verified,
                                    estimated_entry_slippage_per_contract=execution_slippage,
                                    estimated_exit_slippage_per_contract=execution_slippage,
                                    estimated_round_trip_fees_per_contract=round_trip_fees_per_contract,
                                    underlying=symbol)
                decision, reason = ("CANDIDATE", "risk_limits_passed") if risk.allowed else ("NO_TRADE", risk.reason)
                diag.update({"risk_dollars": risk.risk_dollars, "modeled_risk_dollars": risk.risk_dollars,
                             "premium_exposure_dollars": risk.premium_exposure_dollars,
                             "modeled_costs_dollars": risk.modeled_costs_dollars,
                             "risk_budget_dollars": risk.risk_budget_dollars,
                             "planned_risk_dollars": risk.planned_risk_dollars,
                             "entry_slippage_estimate_per_contract": execution_slippage,
                             "exit_slippage_estimate_per_contract": execution_slippage,
                             "round_trip_fees_estimate_per_contract": round_trip_fees_per_contract,
                             "quantity": risk.quantity})
                if not risk.allowed:
                    diag["rejection_reasons"] = ["premium_exposure_plus_costs_exceeds_risk_budget"]
                    diag["economic_gate_result"] = "not_evaluated_risk_gate_rejected"
                if risk.allowed:
                    if not paper_execution:
                        decision, reason = "NO_TRADE", "paper_execution_not_enabled"
                    elif option.delta is None or option.gamma is None or option.theta is None:
                        decision, reason = "NO_TRADE", "option_greeks_missing_for_paper_economic_gate"
                        diag["economic_gate_result"] = "not_evaluated_missing_greeks"
                        diag["rejection_reasons"] = ["option_greeks_missing"]
                    else:
                        # This is a paper-only scenario estimate, not an empirical profitability claim.
                        # It projects the option to the strategy target using current Greeks, charges one
                        # full spread across entry/exit, and includes round-trip fees and slippage.
                        underlying_move = signal.target_price - price
                        holding_days = int(strategy.rules["max_holding_minutes"]) / 1440
                        projected_change = (option.delta * underlying_move +
                                            0.5 * option.gamma * underlying_move * underlying_move +
                                            option.theta * holding_days)
                        expected_exit = max(0.0, option.ask + projected_change - option.spread)
                        fees = round_trip_fees_per_contract * risk.quantity
                        slippage = execution_slippage * risk.quantity * 2
                        gate = economic_gate(
                            entry=option.ask, exit_price=expected_exit, contracts=risk.quantity,
                            spread_cost=0.0, slippage=slippage, fees=fees,
                            risk_dollars=risk.risk_dollars,
                            min_net_r=float(strategy.execution["min_expected_net_r"]),
                        )
                        diag.update({"expected_exit": expected_exit, "expected_exit_model": "current_greeks_at_underlying_target",
                                     "projected_change": projected_change, "gross_opportunity": gate["gross_opportunity"],
                                     "net_opportunity": gate["net_opportunity"], "expected_net_r": gate["expected_r"],
                                     "economic_gate_result": "accepted" if gate["accepted"] else "rejected",
                                     "economic_gate_assumption": "target reached within max holding time; actual paper exits use live bid"})
                        if gate["accepted"]:
                            decision, reason = "PAPER_ENTRY", ("paper_test_risk_and_greek_economic_gate_passed" if paper_test_mode
                                                                 else "paper_risk_and_greek_economic_gate_passed")
                        else:
                            decision, reason = "NO_TRADE", "paper_expected_net_r_below_minimum"
                            diag["rejection_reasons"] = ["expected_net_reward_below_minimum_r"]
        record = Decision(now, symbol, price, strategy.version, strategy.sha256, strategy.strategy_id, decision, reason, diag)
        append_jsonl(decision_log, record.to_dict()); decisions.append(record)
    return decisions
