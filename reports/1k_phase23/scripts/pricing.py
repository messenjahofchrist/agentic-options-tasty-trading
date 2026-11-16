# Phase 2 pricing harness: price a signal's option structure at its entry and exit minutes.
#   RealQuotes  - recorded tastytrade 1-minute bid/ask candles (data/option_history), label "real option quotes"
#   ProxyQuotes - Black-Scholes with IV = HV20 x calibrated ratio and spreads calibrated from the real data, label "proxy"
from __future__ import annotations

import bisect, gzip, json, math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RATE = 0.04
FEES_OPEN = 1.12          # tastytrade: $1.00 commission + $0.10 clearing + $0.02 ORF per contract (cap $10/leg ignored: 1 lot)
FEES_CLOSE = 0.13         # $0.12 + SEC fee (rounded)
SLIP = 0.02               # strategy.txt slippage_per_contract, per leg per side


def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(spot, strike, t, vol, right):
    if t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot), (1.0 if spot > strike else 0.0) * (1 if right == "C" else -1)
    d1 = (math.log(spot / strike) + (RATE + vol * vol / 2) * t) / (vol * math.sqrt(t)); d2 = d1 - vol * math.sqrt(t)
    if right == "C":
        return spot * ncdf(d1) - strike * math.exp(-RATE * t) * ncdf(d2), ncdf(d1)
    return strike * math.exp(-RATE * t) * ncdf(-d2) - spot * ncdf(-d1), ncdf(d1) - 1


def implied_vol(price, spot, strike, t, right):
    lo, hi = 0.01, 5.0
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if price <= intrinsic + 1e-4 or t <= 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(spot, strike, t, mid, right)[0] > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to_expiry(when_utc: datetime, expiration: date) -> float:
    exp = datetime.combine(expiration, time(16, 0), ET)
    return max(0.0, (exp - when_utc.astimezone(ET)).total_seconds()) / (365 * 86400)


@dataclass
class Quote:
    occ: str
    expiration: date
    strike: float
    right: str
    bid: float
    ask: float
    delta: float | None
    iv: float | None

    @property
    def mid(self): return (self.bid + self.ask) / 2


class RealQuotes:
    label = "real option quotes"

    def __init__(self, root: Path, fill: str = "open"):
        """fill="open": the quote at the start of the minute; "adverse": the worst bid/ask inside that minute (ask high,
        bid low), a conservative bound for an order sent a few seconds into the minute."""
        self.root = root / "data" / "option_history"
        self.cache = {}
        self.fill = fill

    def has(self, day: date, symbol: str) -> bool:
        return (self.root / day.isoformat() / f"{symbol}.json.gz").exists()

    def _day(self, day: date, symbol: str):
        key = (day, symbol)
        if key not in self.cache:
            if len(self.cache) > 6:
                self.cache.pop(next(iter(self.cache)))
            d = json.loads(gzip.decompress((self.root / day.isoformat() / f"{symbol}.json.gz").read_bytes()))
            d["open"] = datetime.fromisoformat(d["session_open_et"])
            for c in d["contracts"].values():
                for side in ("bid", "ask"):
                    rows = c.get(side) or []
                    c[side + "_m"] = [r[0] for r in rows]
            d["und_m"] = [r[0] for r in d["underlying"]]
            self.cache[key] = d
        return self.cache[key]

    @staticmethod
    def _at(rows, minutes, m, pick=1):
        """Quote in force at the start of minute m: that minute's open (pick=1; 2 = high, 3 = low) if it has a candle,
        else the last close before."""
        if not rows:
            return None
        i = bisect.bisect_right(minutes, m) - 1
        if i < 0:
            return None
        return rows[i][pick] if minutes[i] == m else rows[i][4]

    def spot(self, day, symbol, when_utc):
        d = self._day(day, symbol); m = int((when_utc.astimezone(ET) - d["open"]).total_seconds() // 60)
        return self._at(d["underlying"], d["und_m"], m)

    def chain(self, day: date, symbol: str, when_utc: datetime) -> list[Quote]:
        d = self._day(day, symbol)
        m = int((when_utc.astimezone(ET) - d["open"]).total_seconds() // 60)
        spot = self._at(d["underlying"], d["und_m"], m)
        out = []
        for occ, c in d["contracts"].items():
            if not 5 <= (date.fromisoformat(c["expiration"]) - day).days <= 25:     # selection uses 7-21 DTE only
                continue
            bid = self._at(c.get("bid"), c.get("bid_m"), m); ask = self._at(c.get("ask"), c.get("ask_m"), m)
            if bid is None or ask is None or ask <= 0 or ask < bid:
                continue
            exp = date.fromisoformat(c["expiration"]); t = years_to_expiry(when_utc, exp)
            iv = implied_vol((bid + ask) / 2, spot, c["strike"], t, c["right"]) if spot else None
            delta = bs(spot, c["strike"], t, iv, c["right"])[1] if iv else None
            out.append(Quote(occ, exp, c["strike"], c["right"], bid, ask, delta, iv))
        return out

    def quote(self, day, symbol, when_utc, occ) -> Quote | None:
        d = self._day(day, symbol); c = d["contracts"].get(occ)
        if not c:
            return None
        m = int((when_utc.astimezone(ET) - d["open"]).total_seconds() // 60)
        adverse = self.fill == "adverse"
        bid = self._at(c.get("bid"), c.get("bid_m"), m, 3 if adverse else 1)
        ask = self._at(c.get("ask"), c.get("ask_m"), m, 2 if adverse else 1)
        if bid is None or ask is None:
            return None
        return Quote(occ, date.fromisoformat(c["expiration"]), c["strike"], c["right"], bid, ask, None, None)


def select_single(chain: list[Quote], side: str, day: date, *, min_dte=7, max_dte=21, target=0.55, lo=0.45, hi=0.65,
                  max_spread=0.10, max_spread_frac=0.10) -> Quote | None:
    """The strategy's own contract rule (strategy.txt): 7-21 DTE, |delta| 0.45-0.65 nearest 0.55, spread <= $0.10 and
    <= 10% of mid. Open interest / volume filters cannot be applied historically."""
    right = "C" if side == "CALL" else "P"
    ok = [q for q in chain if q.right == right and q.delta is not None and min_dte <= (q.expiration - day).days <= max_dte
          and lo <= abs(q.delta) <= hi and (q.ask - q.bid) <= max_spread + 1e-9 and q.mid > 0 and (q.ask - q.bid) / q.mid <= max_spread_frac + 1e-9]
    if not ok:
        return None
    return min(ok, key=lambda q: (abs(abs(q.delta) - target), (q.expiration - day).days))


def select_spread(chain: list[Quote], long: Quote, budget: float, *, max_width_steps=10, min_width_steps=1) -> tuple[Quote, float] | None:
    """Short leg: same expiration, further out of the money. The widest one whose worst-case loss (net debit at the
    ask/bid plus round-trip fees and slippage for both legs) fits the budget."""
    same = sorted({q.strike for q in chain if q.expiration == long.expiration and q.right == long.right})
    i = same.index(long.strike) if long.strike in same else None
    if i is None:
        return None
    step = 1 if long.right == "C" else -1
    best = None
    for k in range(min_width_steps, max_width_steps + 1):
        j = i + step * k
        if not 0 <= j < len(same):
            break
        short = next((q for q in chain if q.expiration == long.expiration and q.right == long.right and q.strike == same[j]), None)
        if short is None or short.bid <= 0:
            continue
        risk = (long.ask - short.bid) * 100 + 2 * (FEES_OPEN + FEES_CLOSE) + 4 * SLIP
        if risk <= budget:
            best = (short, risk)
    return best
