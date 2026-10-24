"""Correlation and exposure grouping.

Data: daily close-to-close log returns from tastytrade DXLink daily candles, the last `lookback_sessions` completed
sessions (today's partial session is never used). Two positions are the same bet when
    corr(a, b) x direction(a) x direction(b) >= threshold        (direction: CALL +1, PUT -1)
so QQQ calls and SQQQ puts group together, and a QQQ call against a QQQ put does not. Pairs with fewer than
`min_observations` overlapping returns fall back to conservative grouping: same sector ETF, a symbol and its own
sector ETF, broad indexes together, a leveraged/inverse ETF with the index it tracks (sign-adjusted).
"""
from __future__ import annotations

import math
from typing import Any

from .sectors import DIRECTION, SectorMap


def log_returns(closes: list[tuple[str, float]]) -> dict[str, float]:
    """{session_date: log return vs the previous listed session} from (date, close) rows in date order."""
    out = {}
    for (_, previous), (day, close) in zip(closes, closes[1:]):
        if previous > 0 and close > 0:
            out[day] = math.log(close / previous)
    return out


def pearson(a: dict[str, float], b: dict[str, float]) -> tuple[float | None, int]:
    days = sorted(set(a) & set(b))
    n = len(days)
    if n < 3:
        return None, n
    xs, ys = [a[d] for d in days], [b[d] for d in days]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None, n
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy), n


class CorrelationModel:
    def __init__(self, cfg: dict[str, Any], sector_map: SectorMap, daily_closes: dict[str, list[tuple[str, float]]]):
        self.cfg = cfg
        self.sector_map = sector_map
        lookback = int(cfg["lookback_sessions"])
        self.returns = {symbol: dict(list(log_returns(rows).items())[-lookback:]) for symbol, rows in daily_closes.items()}
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def pair(self, a: str, b: str) -> dict[str, Any]:
        """{'corr': value or None, 'n': observations, 'method': 'measured' | 'fallback' | 'same_symbol'}."""
        if a == b:
            return {"corr": 1.0, "n": None, "method": "same_symbol"}
        key = (a, b) if a < b else (b, a)
        if key not in self._cache:
            corr, n = pearson(self.returns.get(a, {}), self.returns.get(b, {}))
            if corr is not None and n >= int(self.cfg["min_observations"]):
                self._cache[key] = {"corr": corr, "n": n, "method": "measured"}
            else:
                self._cache[key] = {"corr": self._fallback(a, b), "n": n, "method": "fallback"}
        return self._cache[key]

    def _fallback(self, a: str, b: str) -> float:
        """Conservative stand-in: 1.0 / -1.0 for pairs assumed to move together / inversely, else 0."""
        ea, eb = self.sector_map.entry(a) or {}, self.sector_map.entry(b) or {}
        def tracked(entry: dict, symbol: str) -> tuple[str, int]:
            return entry.get("tracks", symbol), int(entry.get("direction", 1))
        ta, sa = tracked(ea, a)
        tb, sb = tracked(eb, b)
        if ta == tb:
            return float(sa * sb)
        if ea.get("group") == "broad_index" and eb.get("group") == "broad_index":
            return float(sa * sb)
        etf_a, etf_b = ea.get("sector_etf"), eb.get("sector_etf")
        if etf_a and (etf_a == etf_b or etf_a == b or etf_b == a):
            return 1.0
        if ea.get("group") == eb.get("group") == "china_adr":
            return 1.0
        return 0.0

    def same_bet(self, a: str, side_a: str, b: str, side_b: str) -> dict[str, Any]:
        info = self.pair(a, b)
        effective = (info["corr"] or 0.0) * DIRECTION[side_a] * DIRECTION[side_b]
        return {**info, "effective": effective, "same_bet": effective >= float(self.cfg["threshold"])}

    def related_positions(self, symbol: str, side: str, positions: list[dict]) -> list[dict[str, Any]]:
        out = []
        for position in positions:
            other_side = "CALL" if str(position["right"]).upper() in ("C", "CALL") else "PUT"
            info = self.same_bet(symbol, side, str(position["underlying"]), other_side)
            if info["same_bet"]:
                out.append({"trade_id": position["trade_id"], "underlying": position["underlying"], "side": other_side,
                            "max_loss": max_loss(position), **{k: info[k] for k in ("corr", "n", "method", "effective")}})
        return out

    def clusters(self, positions: list[dict]) -> list[list[str]]:
        """Display grouping of open positions: connected components of the same-bet relation."""
        items = [(str(p["trade_id"]), str(p["underlying"]), "CALL" if str(p["right"]).upper() in ("C", "CALL") else "PUT")
                 for p in positions]
        parent = {item[0]: item[0] for item in items}
        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, (ida, sa, da) in enumerate(items):
            for idb, sb, db in items[i + 1:]:
                if self.same_bet(sa, da, sb, db)["same_bet"]:
                    parent[find(ida)] = find(idb)
        groups: dict[str, list[str]] = {}
        for trade_id, underlying, side in items:
            groups.setdefault(find(trade_id), []).append(f"{underlying} {side}")
        return list(groups.values())


def max_loss(position: dict) -> float:
    """Maximum potential loss of an open long option: the recorded modeled risk (premium x 100 x qty + modeled
    round-trip costs), else premium plus entry fees."""
    modeled = position.get("modeled_risk_dollars")
    if modeled is not None:
        return float(modeled)
    return float(position["entry_price"]) * 100 * int(position["quantity"]) + float(position.get("entry_fees", 0))
