"""Market-regime classifier. Documented, ordered rules over measurable inputs (docs/v2/professional_strategy_v2.md):

R1 UNCLEAR           a broad index (SPY, QQQ, IWM) is missing, stale, or its bias inputs are incomplete, or SPY ATR
                     is not yet computable (SPY efficiency missing only rules out the STRONG regimes)
R2 HIGH_VOLATILITY   VIX >= vix_disorderly_level, or VIX up >= vix_disorderly_change_fraction vs its prior close,
                     or SPY 1-minute ATR / price >= spy_disorderly_atr_fraction. With high_volatility_lean enabled, a
                     direction is still allowed when most of the market leans that way: at least min_indexes of
                     SPY/QQQ/IWM with bias <= -min_index_bias (PUT) or >= +min_index_bias (CALL), at least min_sectors
                     sector ETFs leaning the same way, and (if required) SPY on that side of the prior close
R3 STRONG_BULLISH    every index bias >= strong_min_index_bias, SPY efficiency >= strong_min_spy_efficiency and
                     the share of symbols above VWAP >= strong_min_breadth
R4 STRONG_BEARISH    mirror image of R3
R5 WEAK_BULLISH /    summed index bias >= weak_min_market_score (or <= -it) and SPY bias >= weak_min_spy_bias
   WEAK_BEARISH      (or <= -it)
R6 RANGE_BOUND       anything else

There is no sentiment score. The regime gates which directions the core signal may trade (allowed_directions);
it never creates a trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RegimeResult:
    regime: str
    rule: str
    allowed: tuple[str, ...]
    inputs: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"regime": self.regime, "rule": self.rule, "allowed_directions": list(self.allowed),
                "inputs": self.inputs, "notes": list(self.notes)}


def classify(snapshot: dict[str, Any], cfg: dict[str, Any]) -> RegimeResult:
    allowed = cfg["allowed_directions"]
    broad = snapshot.get("broad", {})
    notes: list[str] = []
    inputs: dict[str, Any] = {name: {"bias": state.get("bias"), "fresh": state.get("fresh"),
                                     "missing": state.get("missing"), "efficiency": state.get("efficiency"),
                                     "atr_fraction": state.get("atr_fraction")} for name, state in broad.items()}
    vix = snapshot.get("vix")
    breadth = snapshot.get("breadth", {})
    inputs["vix"] = vix
    inputs["breadth"] = breadth

    def result(regime: str, rule: str) -> RegimeResult:
        return RegimeResult(regime, rule, tuple(allowed[regime]), inputs, tuple(notes))

    def disorderly(note: str) -> RegimeResult:
        notes.append(note)
        lean = cfg.get("high_volatility_lean") or {}
        if not lean.get("enabled"):
            return result("HIGH_VOLATILITY", "R2")
        sides = tuple(side for side, sign in (("PUT", -1), ("CALL", 1)) if _market_leans(snapshot, sign, lean, notes))
        return RegimeResult("HIGH_VOLATILITY", "R2", tuple(allowed["HIGH_VOLATILITY"]) + sides, inputs, tuple(notes))

    for symbol in ("SPY", "QQQ", "IWM"):
        state = broad.get(symbol)
        if not state or not state.get("fresh") or state.get("missing"):
            notes.append(f"{symbol} missing, stale or incomplete")
            return result("UNCLEAR", "R1")
    spy = broad["SPY"]
    if spy.get("atr_fraction") is None:
        notes.append("SPY ATR not yet computable")
        return result("UNCLEAR", "R1")
    if spy.get("efficiency") is None:
        # Efficiency needs efficiency_window_bars + 1 bars; until then the regime can be weak or range, never strong.
        notes.append("SPY efficiency not yet computable: STRONG regimes unavailable")

    if vix is None or not vix.get("fresh"):
        notes.append("VIX unavailable or stale: volatility check uses SPY ATR only")
    else:
        if vix["value"] >= cfg["vix_disorderly_level"]:
            return disorderly(f"VIX {vix['value']:.2f} >= {cfg['vix_disorderly_level']}")
        if vix.get("change_fraction") is not None and vix["change_fraction"] >= cfg["vix_disorderly_change_fraction"]:
            return disorderly(f"VIX up {vix['change_fraction']:.1%} vs prior close")
    if spy["atr_fraction"] >= cfg["spy_disorderly_atr_fraction"]:
        return disorderly(f"SPY ATR/price {spy['atr_fraction']:.4%} >= {cfg['spy_disorderly_atr_fraction']:.4%}")

    biases = [broad[s]["bias"] for s in ("SPY", "QQQ", "IWM")]
    score = sum(biases)
    inputs["market_score"] = score
    above, below = breadth.get("above_fraction"), breadth.get("below_fraction")
    strong_bias = int(cfg["strong_min_index_bias"])
    efficient = spy.get("efficiency") is not None and spy["efficiency"] >= cfg["strong_min_spy_efficiency"]
    if all(b >= strong_bias for b in biases) and efficient and above is not None and above >= cfg["strong_min_breadth"]:
        return result("STRONG_BULLISH", "R3")
    if all(b <= -strong_bias for b in biases) and efficient and below is not None and below >= cfg["strong_min_breadth"]:
        return result("STRONG_BEARISH", "R4")
    if score >= cfg["weak_min_market_score"] and spy["bias"] >= cfg["weak_min_spy_bias"]:
        return result("WEAK_BULLISH", "R5")
    if score <= -cfg["weak_min_market_score"] and spy["bias"] <= -cfg["weak_min_spy_bias"]:
        return result("WEAK_BEARISH", "R5")
    return result("RANGE_BOUND", "R6")


def _market_leans(snapshot: dict[str, Any], sign: int, lean: dict[str, Any], notes: list[str]) -> bool:
    """High-volatility exception: does most of the market lean one way (sign -1 down for PUT, +1 up for CALL)?"""
    broad, sectors = snapshot.get("broad", {}), snapshot.get("sectors", {})
    side = "PUT" if sign < 0 else "CALL"
    indexes = [s for s in ("SPY", "QQQ", "IWM") if sign * ((broad.get(s) or {}).get("bias") or 0) >= lean["min_index_bias"]]
    leaning = sum(1 for state in sectors.values() if sign * (state.get("bias") or 0) > 0)
    change = (broad.get("SPY") or {}).get("change_from_prior_close")
    prior_ok = not lean["require_spy_vs_prior_close"] or (change is not None and sign * change > 0)
    ok = len(indexes) >= lean["min_indexes"] and leaning >= lean["min_sectors"] and prior_ok
    if ok:
        notes.append(f"{side} allowed: market leans {'down' if sign < 0 else 'up'} ({', '.join(indexes)}; "
                     f"{leaning}/{len(sectors)} sectors; SPY vs prior close {change if change is None else f'{change:+.2%}'})")
    return ok
