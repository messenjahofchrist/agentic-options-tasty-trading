"""Dynamic trade management layered on the original exits.

The original exit logic (trader.position_manager.exit_reason: underlying stop and target, daily-loss emergency
exit, flatten at the close) always runs and is the baseline. These rules exit EARLIER or protect more and never widen
a stop. The one exception is let_winners_run (off unless enabled): at the target the position is held instead of
sold, with a stop that has locked in part of the gain and rises behind the best price (hold_past_target).

R = the position's initial underlying stop distance |entry_underlying - stop_underlying| (the strategy's own).
  breakeven     once the underlying has moved trigger_r x R in favour, exit if the option bid falls to the price that
                returns the premium plus entry fees, estimated exit fees and slippage (net ~0). With
                require_option_above_floor it arms only once the bid has actually reached that price, so it can never
                fire on the bar it arms at a loss (a wide option spread can lag a small underlying move).
  let_winners_run  once the underlying reaches the original target, keep the position: its stop becomes the tighter
                of entry + lock_r x R and best price - trail_r x R (only ever rising), and the position is sold when the
                underlying closes back through it (or at the session close).
  trailing      once the underlying has moved trigger_r x R in favour, trail the underlying stop at the best price
                since entry minus k x ATR(14) of the latest completed bar, k = atr_multiple (a number, or the
                strategy's stop_underlying_atr when it is "rules.stop_underlying_atr"). It
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
        if not be.get("require_option_above_floor") or option_bid > floor:
            state.update(breakeven_armed=True, breakeven_option_floor=round(floor, 4), breakeven_armed_at=now.isoformat())
            events.append({"event": "breakeven_armed", "option_floor": round(floor, 4), "favourable_r": round(favourable_r, 3)})

    runner = cfg.get("let_winners_run") or {}
    target_r = abs(float(position["target_underlying"]) - entry) / r
    if runner.get("enabled") and favourable_r >= target_r:
        lock = entry + direction * float(runner["lock_r"]) * r
        trail_behind = best - direction * float(runner["trail_r"]) * r
        level = max(lock, trail_behind) if direction == 1 else min(lock, trail_behind)
        previous = state.get("runner_stop_underlying")
        if previous is not None:
            level = max(level, previous) if direction == 1 else min(level, previous)
        if not state.get("runner"):
            state["runner"] = True
            events.append({"event": "target_reached_letting_it_run", "favourable_r": round(favourable_r, 3),
                           "runner_stop": round(level, 4)})
        elif level != previous:
            events.append({"event": "runner_stop_moved", "from": previous, "to": round(level, 4), "best_underlying": best})
        state["runner_stop_underlying"] = round(level, 4)
    result["hold_past_target"] = bool(state.get("runner"))

    trail = cfg["trailing"]
    if trail["enabled"] and favourable_r >= trail["trigger_r"] and atr is not None and atr > 0:
        k = atr_multiple if trail["atr_multiple"] == "rules.stop_underlying_atr" else float(trail["atr_multiple"])
        candidate = best - direction * k * atr
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
    runner_stop = state.get("runner_stop_underlying")
    if runner_stop is not None and (underlying - runner_stop) * direction <= 0:
        result["exit_reason"] = "v2_runner_stop"
    elif trail_stop is not None and (underlying - trail_stop) * direction <= 0:
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
