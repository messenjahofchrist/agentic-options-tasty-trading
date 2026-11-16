"""Read the recorded 1-minute option bid/ask history (data/option_history, written by trader.option_recorder).

Used after the close only (scoreboard, cost table): pricing a trade's entry and exit minutes on real quotes, with any
contract choice. Moved here from the review harness (reports/1k_phase23/scripts/pricing.py) so scheduled jobs do not
import from a report folder. Greeks are not recorded; delta is derived from the mid price (Black-Scholes).
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RATE = 0.04


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes(spot: float, strike: float, t: float, vol: float, right: str) -> tuple[float, float]:
    """(price, delta) of a European option; right is "C" or "P"."""
    if t <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
        return intrinsic, (1.0 if spot > strike else 0.0) * (1 if right == "C" else -1)
    d1 = (math.log(spot / strike) + (RATE + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if right == "C":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t) * _ncdf(d2), _ncdf(d1)
    return strike * math.exp(-RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1), _ncdf(d1) - 1


def implied_vol(price: float, spot: float, strike: float, t: float, right: str) -> float | None:
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if price <= intrinsic + 1e-4 or t <= 0:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if black_scholes(spot, strike, t, mid, right)[0] > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to_expiry(when: datetime, expiration: date) -> float:
    end = datetime.combine(expiration, time(16, 0), ET)
    return max(0.0, (end - when.astimezone(ET)).total_seconds()) / (365 * 86400)


@dataclass
class Quote:
    occ: str
    expiration: date
    strike: float
    right: str
    bid: float
    ask: float
    delta: float | None = None
    iv: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class QuoteHistory:
    def __init__(self, root: Path, fill: str = "open"):
        """fill="open": the quote at the start of the minute; "adverse": the worst bid/ask inside that minute (ask
        high, bid low), a conservative bound for an order sent a few seconds into the minute."""
        self.dir = root / "data" / "option_history"
        self.fill = fill
        self._cache: dict[tuple[date, str], dict] = {}

    def has(self, day: date, symbol: str) -> bool:
        return (self.dir / day.isoformat() / f"{symbol}.json.gz").exists()

    def days(self) -> list[date]:
        out = []
        for path in self.dir.iterdir() if self.dir.is_dir() else []:
            try:
                out.append(date.fromisoformat(path.name))
            except ValueError:
                continue
        return sorted(out)

    def symbols(self, day: date) -> list[str]:
        return sorted(p.name.removesuffix(".json.gz") for p in (self.dir / day.isoformat()).glob("*.json.gz"))

    def _day(self, day: date, symbol: str) -> dict:
        key = (day, symbol)
        if key not in self._cache:
            if len(self._cache) > 6:
                self._cache.pop(next(iter(self._cache)))
            data = json.loads(gzip.decompress((self.dir / day.isoformat() / f"{symbol}.json.gz").read_bytes()))
            # Context-only files (VIX) carry bars but no session times or contracts.
            data["open"] = (datetime.fromisoformat(data["session_open_et"]) if data.get("session_open_et")
                            else datetime.combine(day, time(9, 30), ET))
            data.setdefault("contracts", {})
            data.setdefault("underlying", [])
            for contract in data["contracts"].values():
                for side in ("bid", "ask"):
                    contract[side + "_m"] = [row[0] for row in contract.get(side) or []]
            data["und_m"] = [row[0] for row in data["underlying"]]
            self._cache[key] = data
        return self._cache[key]

    @staticmethod
    def _at(rows, minutes, m: int, pick: int = 1):
        """Value in force at the start of minute m: that minute's open (pick 1; 2 = high, 3 = low) if it has a candle,
        else the last close before it."""
        if not rows:
            return None
        i = bisect.bisect_right(minutes, m) - 1
        if i < 0:
            return None
        return rows[i][pick] if minutes[i] == m else rows[i][4]

    def _minute(self, data: dict, when: datetime) -> int:
        return int((when.astimezone(ET) - data["open"]).total_seconds() // 60)

    def spot(self, day: date, symbol: str, when: datetime) -> float | None:
        data = self._day(day, symbol)
        return self._at(data["underlying"], data["und_m"], self._minute(data, when))

    def underlying_bars(self, day: date, symbol: str) -> list[tuple[datetime, float, float, float, float]]:
        """(bar start, open, high, low, close) for the session's recorded one-minute bars."""
        from datetime import timedelta
        data = self._day(day, symbol)
        return [(data["open"] + timedelta(minutes=int(r[0])), r[1], r[2], r[3], r[4]) for r in data["underlying"]]

    def chain(self, day: date, symbol: str, when: datetime, *, min_dte: int = 1, max_dte: int = 25,
              max_strike_distance: float = 0.06) -> list[Quote]:
        data = self._day(day, symbol)
        m = self._minute(data, when)
        spot = self._at(data["underlying"], data["und_m"], m)
        out = []
        for occ, contract in data["contracts"].items():
            expiration = date.fromisoformat(contract["expiration"])
            if not min_dte <= (expiration - day).days <= max_dte:
                continue
            if spot and abs(contract["strike"] / spot - 1) > max_strike_distance:
                continue
            bid = self._at(contract.get("bid"), contract.get("bid_m"), m)
            ask = self._at(contract.get("ask"), contract.get("ask_m"), m)
            if bid is None or ask is None or ask <= 0 or ask < bid:
                continue
            t = years_to_expiry(when, expiration)
            iv = implied_vol((bid + ask) / 2, spot, contract["strike"], t, contract["right"]) if spot else None
            delta = black_scholes(spot, contract["strike"], t, iv, contract["right"])[1] if iv else None
            out.append(Quote(occ, expiration, contract["strike"], contract["right"], bid, ask, delta, iv))
        return out

    def quote(self, day: date, symbol: str, when: datetime, occ: str) -> Quote | None:
        data = self._day(day, symbol)
        contract = data["contracts"].get(occ)
        if not contract:
            return None
        m = self._minute(data, when)
        adverse = self.fill == "adverse"
        bid = self._at(contract.get("bid"), contract.get("bid_m"), m, 3 if adverse else 1)
        ask = self._at(contract.get("ask"), contract.get("ask_m"), m, 2 if adverse else 1)
        if bid is None or ask is None:
            return None
        return Quote(occ, date.fromisoformat(contract["expiration"]), contract["strike"], contract["right"], bid, ask)


def select_contract(chain: list[Quote], side: str, day: date, *, min_dte: int = 7, max_dte: int = 21, target: float = 0.55,
                    lo: float = 0.45, hi: float = 0.65, max_spread: float = 0.10, max_spread_fraction: float = 0.10,
                    nearest_expiry_first: bool = False) -> Quote | None:
    """The strategy's contract rule on recorded quotes (delta band nearest the target, spread limits). Open interest
    and volume filters cannot be applied historically. nearest_expiry_first: use only the first eligible expiration."""
    right = "C" if side == "CALL" else "P"
    ok = [q for q in chain if q.right == right and q.delta is not None and min_dte <= (q.expiration - day).days <= max_dte
          and lo <= abs(q.delta) <= hi and q.spread <= max_spread + 1e-9 and q.mid > 0
          and q.spread / q.mid <= max_spread_fraction + 1e-9]
    if not ok:
        return None
    if nearest_expiry_first:
        first = min((q.expiration - day).days for q in ok)
        ok = [q for q in ok if (q.expiration - day).days == first]
    return min(ok, key=lambda q: (abs(abs(q.delta) - target), (q.expiration - day).days))
