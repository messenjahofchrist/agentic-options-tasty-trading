"""Sector mapping and sector confirmation for a proposed trade."""
from __future__ import annotations

from typing import Any

DIRECTION = {"CALL": 1, "PUT": -1}


class SectorMap:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.symbols: dict[str, dict[str, Any]] = data["symbols"]

    def entry(self, symbol: str) -> dict[str, Any] | None:
        return self.symbols.get(symbol)

    def sector_etf(self, symbol: str) -> str | None:
        entry = self.entry(symbol)
        return entry.get("sector_etf") if entry else None

    def group(self, symbol: str) -> str | None:
        entry = self.entry(symbol)
        return entry.get("group") if entry else None

    def is_single_stock(self, symbol: str) -> bool:
        return self.group(symbol) in {"us_equity", "china_adr"}

    def unmapped(self, symbols: list[str] | tuple[str, ...]) -> list[str]:
        return [symbol for symbol in symbols if symbol not in self.symbols]


def sector_confirmation(symbol: str, side: str, sector_map: SectorMap, sectors_snapshot: dict[str, Any],
                        cfg: dict[str, Any], symbol_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """PASS / REJECT / NOT_APPLICABLE / INSUFFICIENT_DATA with the inputs that decided it."""
    entry = sector_map.entry(symbol)
    if entry is None:
        return {"status": "INSUFFICIENT_DATA", "reason": "symbol_not_in_sector_map", "symbol": symbol}
    etf = entry.get("sector_etf")
    if etf is None or etf == symbol:
        return {"status": "NOT_APPLICABLE", "reason": f"group_{entry['group']}_has_no_separate_sector_etf",
                "group": entry["group"]}
    state = sectors_snapshot.get(etf)
    if not state or not state.get("fresh") or state.get("missing"):
        return {"status": "INSUFFICIENT_DATA", "reason": f"sector_etf_{etf}_missing_or_stale", "sector_etf": etf}
    direction = DIRECTION[side]
    leaning = state["bias"] * direction
    out = {"sector_etf": etf, "sector_bias": state["bias"], "sector_bias_in_trade_direction": leaning,
           "sector_change_from_open": state.get("change_from_open"),
           "sector_relative_strength_vs_spy": state.get("relative_strength_vs_spy"),
           "sector_momentum_15": state.get("momentum_15"), "min_sector_bias": cfg["min_sector_bias"]}
    if symbol_state and symbol_state.get("change_from_open") is not None and state.get("change_from_open") is not None:
        out["symbol_vs_sector_change"] = symbol_state["change_from_open"] - state["change_from_open"]
    if leaning < cfg["min_sector_bias"]:
        out.update(status="REJECT", reason=f"sector_{etf}_bias_{state['bias']:+d}_contradicts_or_not_confirming_{side}")
    else:
        out.update(status="PASS", reason=f"sector_{etf}_confirms_{side}")
    return out
