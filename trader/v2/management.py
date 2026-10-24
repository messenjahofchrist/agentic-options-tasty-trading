"""Dynamic trade management layered on the original exits.

The original exit logic (trader.position_manager.exit_reason: underlying stop and target, daily-loss emergency
exit, flatten at the close) always runs and is the baseline. These rules can only exit EARLIER or protect more;
none of them widens a stop or moves a target away.

R = the position's initial underlying stop distance |entry_underlying - stop_underlying| (the strategy's own).
  breakeven     once the underlying has moved trigger_r x R in favour, exit if the option bid falls to the price that
                returns the premium plus entry fees, estimated exit fees and slippage (net ~0).
  trailing      once the underlying has moved trigger_r x R in favour, trail the underlying stop at the best price
                since entry minus k x ATR(14) of the latest completed bar, k = the strategy's stop_underlying_atr. It
                only tightens, and never sits behind the original stop.
  time_stop     after after_minutes, exit if the best favourable move so far is below min_progress_r x R.
  partial       once trigger_r x R is reached, sell floor(quantity x fraction) contracts (needs >= 2 contracts).
State lives in the position record under "v2_management", so it survives a restart with the ledger.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any


def _side(position: dict) -> int:
    return 1 if str(position["right"]).upper() in ("C", "CALL") else -1


def initial_r(position: dict) -> float:
    state = position.get("v2_management") or {}
    if state.get("initial_stop_distance"):
        return float(state["initial_stop_distance"])
    return abs(float(position["entry_underlying"]) - float(position["stop_underlying"]))


def breakeven_option_price(position: dict, *, exit_fees_estimate: float, slippage_per_contract: float) -> float:
    quantity = int(position["quantity"])
    return (float(position["entry_price"]) + slippage_per_contract / 100
            + (float(position.get("entry_fees", 0)) + exit_fees_estimate) / (100 * quantity))


def manage(position: dict, *, underlying: float, atr: float | None, option_bid: float, now: datetime,
           cfg: dict[str, Any], atr_multiple: float, exit_fees_estimate: float, slippage_per_contract: float) -> dict[str, Any]:
    """Update the position's V2 management state for one completed bar and return
    {'exit_reason': str | None, 'partial_quantity': int, 'events': [...]}."""
    state = position.setdefault("v2_management", {})
    direction = _side(position)
    entry = float(position["entry_underlying"])
    r = initial_r(position)
    state.setdefault("initial_stop_distance", r)
    best = state.get("best_underlying")
    best = underlying if best is None else (max(best, underlying) if direction == 1 else min(best, underlying))
    state["best_underlying"] = best
    favourable_r = ((best - entry) * direction) / r if r > 0 else 0.0
    state["max_favourable_r"] = round(favourable_r, 4)
    events: list[dict[str, Any]] = []
    result: dict[str, Any] = {"exit_reason": None, "partial_quantity": 0, "events": events}
    if r <= 0:
        return result

    be = cfg["breakeven"]
    if be["enabled"] and not state.get("breakeven_armed") and favourable_r >= be["trigger_r"]:
        floor = breakeven_option_price(position, exit_fees_estimate=exit_fees_estimate,
                                       slippage_per_contract=slippage_per_contract)
        state.update(breakeven_armed=True, breakeven_option_floor=round(floor, 4), breakeven_armed_at=now.isoformat())
        events.append({"event": "breakeven_armed", "option_floor": round(floor, 4), "favourable_r": round(favourable_r, 3)})

    trail = cfg["trailing"]
    if trail["enabled"] and favourable_r >= trail["trigger_r"] and atr is not None and atr > 0:
        candidate = best - direction * atr_multiple * atr
        original = float(position["stop_underlying"])
        previous = state.get("trail_stop_underlying")
        tightest = candidate if previous is None else (max(previous, candidate) if direction == 1 else min(previous, candidate))
        tightest = max(tightest, original) if direction == 1 else min(tightest, original)
        if previous is None or tightest != previous:
            state["trail_stop_underlying"] = round(tightest, 4)
            events.append({"event": "trail_stop_moved" if previous is not None else "trail_stop_armed",
                           "from": previous, "to": round(tightest, 4), "best_underlying": best, "atr": atr})

    partial = cfg["partial_profit"]
    quantity = int(position["quantity"])
    if partial["enabled"] and not state.get("partial_taken") and quantity >= 2 and favourable_r >= partial["trigger_r"]:
        sell = min(quantity - 1, max(1, int(math.floor(quantity * partial["fraction"]))))
        result["partial_quantity"] = sell
        events.append({"event": "partial_profit_due", "quantity": sell, "of": quantity})

    trail_stop = state.get("trail_stop_underlying")
    if trail_stop is not None and (underlying - trail_stop) * direction <= 0:
        result["exit_reason"] = "v2_trailing_stop"
    elif state.get("breakeven_armed") and option_bid <= float(state["breakeven_option_floor"]):
        result["exit_reason"] = "v2_breakeven_stop"
    else:
        ts = cfg["time_stop"]
        held = (now - datetime.fromisoformat(position["entry_time"])).total_seconds() / 60
        state["stalled"] = bool(held >= ts["after_minutes"] and favourable_r < ts["min_progress_r"])
        if ts["enabled"] and state["stalled"]:
            result["exit_reason"] = "v2_time_stop_no_progress"
    if result["exit_reason"]:
        events.append({"event": "exit_signal", "reason": result["exit_reason"]})
    if events:
        state.setdefault("log", []).extend({"at": now.isoformat(), **event} for event in events)
        state["log"] = state["log"][-40:]
    return result
