from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .logging_utils import append_jsonl
from .models import Bar, Decision
from .option_selector import select_option
from .risk_engine import RiskState, assess_entry
from .strategy_engine import evaluate_underlying
from .strategy_loader import Strategy, verify_manifest
from .watchdog import KillSwitch


def decision_cycle(*, strategy: Strategy, strategy_path: str | Path, manifest_path: str | Path,
                   bars_by_symbol: dict[str, list[Bar]], option_chain_by_symbol: dict[str, list],
                   risk_state: RiskState, now: datetime, data_fresh: bool, account_certain: bool,
                   reconciled: bool, protection_verified: bool, market_open: bool, within_entry_window: bool,
                   decision_log: str | Path, kill_switch_path: str | Path = "KILL_SWITCH") -> list[Decision]:
    verify_manifest(strategy_path, manifest_path)
    killed = KillSwitch(kill_switch_path).entries_blocked()
    decisions = []
    for symbol in strategy.universe:
        bars = bars_by_symbol.get(symbol, [])
        price = bars[-1].close if bars else None
        signal = evaluate_underlying(bars, strategy)
        diag = {"bars": len(bars), "underlying_signal": signal.reason if signal else None,
                "trend_state": None, "timeframe_state": f"{strategy.bar_interval_minutes}m", "breakout_state": None,
                "relative_volume": None, "volatility_state": None, "option_candidate": None,
                "bid": None, "ask": None, "mid": None, "spread": None, "greeks": None,
                "iv": None, "iv_rank": None, "dte": None, "expected_entry": None, "expected_exit": None,
                "option_candidates": len(option_chain_by_symbol.get(symbol, [])), "gross_opportunity": None, "net_opportunity": None}
        if killed:
            decision, reason = "NO_TRADE", "kill_switch_active"
        elif not market_open or not within_entry_window:
            decision, reason = "NO_TRADE", "market_closed_or_outside_entry_window"
        elif not data_fresh or not account_certain or not reconciled or not protection_verified:
            decision, reason = "NO_TRADE", "safety_gate_blocked"
        elif not signal:
            decision, reason = "NO_TRADE", "underlying_rules_not_satisfied"
        else:
            diag.update({"trend_state": "bullish" if signal.side == "CALL" else "bearish",
                         "breakout_state": signal.reason, "relative_volume": signal.metrics.get("relative_volume"),
                         "volatility_state": {"atr": signal.metrics.get("atr")}, "signal_metrics": signal.metrics})
            selected = select_option(option_chain_by_symbol.get(symbol, []), signal.side, now, strategy)
            if not selected:
                decision, reason = "NO_TRADE", "no_liquid_option_meets_filters"
            else:
                option = selected.quote
                diag.update({"option_candidate": option.symbol, "option": option.symbol, "bid": option.bid, "ask": option.ask,
                             "mid": option.mid, "spread": option.spread, "spread_fraction": option.spread_fraction,
                             "greeks": {"delta": option.delta, "gamma": option.gamma, "theta": option.theta, "vega": option.vega},
                             "iv": option.iv, "iv_rank": option.iv_rank,
                             "dte": (option.expiration.date() - now.date()).days,
                             "expected_entry": option.ask, "expected_exit": None,
                             "volume": option.volume, "open_interest": option.open_interest,
                             "quote_timestamp": option.quote_timestamp.isoformat() if option.quote_timestamp else None})
                risk = assess_entry(state=risk_state, contract_price=selected.quote.ask, strategy_risk=strategy.risk,
                                    max_contracts=int(strategy.rules["max_contracts"]), data_fresh=data_fresh,
                                    account_certain=account_certain, reconciled=reconciled, protection_verified=protection_verified)
                decision, reason = ("CANDIDATE", "risk_limits_passed") if risk.allowed else ("NO_TRADE", risk.reason)
                diag.update({"risk_dollars": risk.risk_dollars, "quantity": risk.quantity})
                # Log candidate-level diagnostics, but block until an empirically grounded expected exit model exists.
                if risk.allowed:
                    decision, reason = "NO_TRADE", "economic_gate_and_order_pipeline_not_fully_populated"
        record = Decision(now, symbol, price, strategy.version, strategy.sha256, decision, reason, diag)
        append_jsonl(decision_log, record.to_dict()); decisions.append(record)
    return decisions
