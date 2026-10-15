from __future__ import annotations

from datetime import datetime

from .models import Position


def exit_reason(position: Position, *, underlying: float, option_bid: float, now: datetime, daily_loss_hit: bool = False,
                enable_time_exit: bool = True) -> str | None:
    if daily_loss_hit:
        return "emergency_daily_risk_exit"
    if position.right.upper() in ("C", "CALL"):
        if underlying <= position.stop_underlying: return "underlying_invalidation_stop"
        if underlying >= position.target_underlying: return "underlying_profit_target"
    else:
        if underlying >= position.stop_underlying: return "underlying_invalidation_stop"
        if underlying <= position.target_underlying: return "underlying_profit_target"
    held = (now - position.entry_time).total_seconds() / 60
    if enable_time_exit and held >= position.max_holding_minutes: return "time_exit"
    if option_bid <= 0: return "market_quote_invalid_emergency_review"
    position.mfe = max(position.mfe, (option_bid - position.entry_price) * position.quantity * 100)
    position.mae = min(position.mae, (option_bid - position.entry_price) * position.quantity * 100)
    return None
