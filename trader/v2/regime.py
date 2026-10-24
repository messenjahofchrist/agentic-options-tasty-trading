"""Market-regime classifier. Documented, ordered rules over measurable inputs (docs/v2/professional_strategy_v2.md):

R1 UNCLEAR           a broad index (SPY, QQQ, IWM) is missing, stale, or its bias inputs are incomplete, or SPY ATR
                     is not yet computable (SPY efficiency missing only rules out the STRONG regimes)
R2 HIGH_VOLATILITY   VIX >= vix_disorderly_level, or VIX up >= vix_disorderly_change_fraction vs its prior close,
                     or SPY 1-minute ATR / price >= spy_disorderly_atr_fraction
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
            notes.append(f"VIX {vix['value']:.2f} >= {cfg['vix_disorderly_level']}")
            return result("HIGH_VOLATILITY", "R2")
        if vix.get("change_fraction") is not None and vix["change_fraction"] >= cfg["vix_disorderly_change_fraction"]:
            notes.append(f"VIX up {vix['change_fraction']:.1%} vs prior close")
            return result("HIGH_VOLATILITY", "R2")
    if spy["atr_fraction"] >= cfg["spy_disorderly_atr_fraction"]:
        notes.append(f"SPY ATR/price {spy['atr_fraction']:.4%} >= {cfg['spy_disorderly_atr_fraction']:.4%}")
        return result("HIGH_VOLATILITY", "R2")

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
