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


def select_option(candidates: list[OptionQuote], direction: str, now: datetime, strategy: Strategy) -> OptionSelection | None:
    rules = strategy.rules
    right = "C" if direction == "CALL" else "P"
    eligible: list[tuple[float, OptionQuote]] = []
    for option in candidates:
        dte = (option.expiration.date() - now.date()).days
        delta = option.delta
        if option.right.upper() not in (right, "CALL" if right == "C" else "PUT"):
            continue
        if not int(rules["min_dte"]) <= dte <= int(rules["max_dte"]):
            continue
        if delta is None or not float(rules["min_abs_delta"]) <= abs(delta) <= float(rules["max_abs_delta"]):
            continue
        if option.bid < 0 or option.ask <= 0 or option.ask < option.bid or option.spread_fraction > float(rules["max_spread_fraction"]):
            continue
        if option.open_interest < int(rules["min_open_interest"]) or option.volume < int(rules["min_option_volume"]):
            continue
        # Favor target delta and narrow spreads; never rank on percentage upside.
        target_delta = (float(rules["min_abs_delta"]) + float(rules["max_abs_delta"])) / 2
        score = abs(abs(delta) - target_delta) + option.spread_fraction + 1 / max(option.open_interest, 1) + 1 / max(option.volume, 1)
        eligible.append((score, option))
    if not eligible:
        return None
    score, quote = min(eligible, key=lambda item: (item[0], item[1].expiration, item[1].strike))
    return OptionSelection(quote, score)
