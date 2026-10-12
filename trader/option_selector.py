from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import OptionQuote
from .strategy_loader import Strategy


@dataclass(frozen=True)
class OptionSelection:
    quote: OptionQuote
    score: float
    rejected: tuple[str, ...] = ()


def evaluate_options(candidates: list[OptionQuote], direction: str, now: datetime, strategy: Strategy
                     ) -> tuple[OptionSelection | None, list[dict]]:
    rules = strategy.rules
    right = "C" if direction == "CALL" else "P"
    eligible: list[tuple[float, OptionQuote]] = []
    audit: list[dict] = []
    for option in candidates:
        dte = (option.expiration.date() - now.date()).days
        delta = option.delta
        reasons: list[str] = []
        if option.right.upper() not in (right, "CALL" if right == "C" else "PUT"):
            reasons.append("option_right_mismatch")
        if not int(rules["min_dte"]) <= dte <= int(rules["max_dte"]):
            reasons.append("dte_outside_range")
        if delta is None or not float(rules["min_abs_delta"]) <= abs(delta) <= float(rules["max_abs_delta"]):
            reasons.append("delta_outside_range_or_missing")
        valid_market = option.bid >= 0 and option.ask > 0 and option.ask >= option.bid
        if not valid_market:
            reasons.append("invalid_bid_ask")
        spread_dollars = option.spread if valid_market else None
        midpoint = option.mid if valid_market else None
        spread_fraction = option.spread_fraction if valid_market else None
        if valid_market and spread_dollars > float(rules["max_spread_dollars"]):
            reasons.append("spread_dollars_exceeded")
        if valid_market and spread_fraction > float(rules["max_spread_fraction"]):
            reasons.append("spread_percentage_exceeded")
        if option.open_interest < int(rules["min_open_interest"]) or option.volume < int(rules["min_option_volume"]):
            if option.open_interest < int(rules["min_open_interest"]): reasons.append("open_interest_below_minimum")
            if option.volume < int(rules["min_option_volume"]): reasons.append("option_volume_below_minimum")
        audit.append({"contract": option.symbol, "expiration": option.expiration.isoformat(), "dte": dte,
                      "bid": option.bid, "ask": option.ask, "mid": midpoint, "spread_dollars": spread_dollars,
                      "spread_fraction": spread_fraction,
                      "spread_percent": spread_fraction * 100 if spread_fraction is not None else None,
                      "max_spread_dollars": float(rules["max_spread_dollars"]),
                      "max_spread_fraction": float(rules["max_spread_fraction"]),
                      "max_spread_percent": float(rules["max_spread_fraction"]) * 100, "delta": delta,
                      "open_interest": option.open_interest, "volume": option.volume,
                      "eligible": not reasons, "rejection_reasons": reasons})
        if not reasons:
            # Prefer the configured target delta and narrower absolute and percentage spreads.
            target_delta = float(rules["target_abs_delta"])
            score = (abs(abs(delta) - target_delta) + option.spread_fraction +
                     1 / max(option.open_interest, 1) + 1 / max(option.volume, 1))
            eligible.append((score, option))
    if not eligible:
        return None, audit
    score, quote = min(eligible, key=lambda item: (item[0], item[1].expiration, item[1].strike))
    return OptionSelection(quote, score), audit


def select_option(candidates: list[OptionQuote], direction: str, now: datetime, strategy: Strategy) -> OptionSelection | None:
    return evaluate_options(candidates, direction, now, strategy)[0]
