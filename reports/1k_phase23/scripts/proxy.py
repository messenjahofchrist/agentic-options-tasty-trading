# PROXY option prices for days without recorded quotes (Jun 1 - Oct 10): Black-Scholes on real 1-minute stock bars,
# IV = HV20 x a per-symbol ratio and bid/ask spreads, both calibrated from the recorded real quotes. Constant IV within
# a day, so IV crush/expansion is NOT modeled. Results from this module are labeled "proxy" and never prove anything.
from __future__ import annotations

import bisect, gzip, json, math, statistics as st
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pricing import Quote, bs, implied_vol, years_to_expiry

ET = ZoneInfo("America/New_York")
DAILY = ("SPY", "QQQ", "IWM")
BUCKETS = ((0.05, 0.2), (0.2, 0.35), (0.35, 0.45), (0.45, 0.65), (0.65, 0.95))


def bucket(delta):
    if delta is None:
        return None
    return next(((lo, hi) for lo, hi in BUCKETS if lo <= abs(delta) < hi), None)


def calibrate(root: Path, symbols, days) -> dict:
    """Per symbol: median IV/HV20 ratio (0.45-0.65 |delta|, 7-21 DTE) and the median real bid/ask spread in each
    |delta| bucket (spreads do not scale with price: a $10 SPY option trades a few cents wide)."""
    from pricing import RealQuotes
    rq = RealQuotes(root)
    out = {}
    for s in symbols:
        ratios, fracs, abss = [], [], []
        buckets = {b: [] for b in BUCKETS}
        for d in days:
            if not rq.has(d, s):
                continue
            data = rq._day(d, s)
            for minute in (60, 150, 270):
                when = (data["open"] + timedelta(minutes=minute)).astimezone(timezone.utc)
                for q in rq.chain(d, s, when):
                    dte = (q.expiration - d).days
                    if q.delta is None or not 5 <= dte <= 25:
                        continue
                    b = bucket(q.delta)
                    if b is not None:
                        buckets[b].append(q.ask - q.bid)
                    if 0.2 <= abs(q.delta) <= 0.7:
                        abss.append(q.ask - q.bid)
                        if q.mid >= 0.5:
                            fracs.append((q.ask - q.bid) / q.mid)
                    if 0.45 <= abs(q.delta) <= 0.65 and q.iv:
                        ratios.append((d, q.iv))
        out[s] = {"iv_samples": ratios, "spread_frac": st.median(fracs) if fracs else None,
                  "spread_floor": sorted(abss)[len(abss) // 10] if abss else None, "n": len(abss),
                  "spread_by_delta": {f"{lo}-{hi}": round(st.median(v), 3) for (lo, hi), v in buckets.items() if len(v) >= 5}}
    return out


class ProxyQuotes:
    label = "proxy"

    def __init__(self, bars: dict, daily_closes: dict, calib: dict, strike_step: dict):
        self.bars = {s: sorted(b, key=lambda x: x.timestamp) for s, b in bars.items()}
        self.stamps = {s: [x.timestamp for x in b] for s, b in self.bars.items()}
        self.closes = daily_closes          # {symbol: [(date, close)] sorted}
        self.calib = calib; self.step = strike_step

    def has(self, day, symbol):
        return symbol in self.bars and symbol in self.calib and self.hv20(symbol, day) is not None

    def hv20(self, symbol, day):
        c = [x for d, x in self.closes.get(symbol, []) if d < day][-21:]
        if len(c) < 21:
            return None
        r = [math.log(b / a) for a, b in zip(c, c[1:])]
        return st.pstdev(r) * math.sqrt(252)

    def spot(self, day, symbol, when_utc):
        i = bisect.bisect_right(self.stamps[symbol], when_utc) - 1
        return self.bars[symbol][i].close if i >= 0 else None

    def _quote(self, symbol, day, when_utc, expiration, strike, right):
        spot = self.spot(day, symbol, when_utc)
        vol = self.hv20(symbol, day) * self.calib[symbol]["iv_ratio"]
        t = years_to_expiry(when_utc, expiration)
        price, delta = bs(spot, strike, t, vol, right)
        by = self.calib[symbol].get("spread_by_delta") or {}
        b = bucket(delta)
        spread = by.get(f"{b[0]}-{b[1]}") if b else None
        if spread is None:
            spread = max(self.calib[symbol]["spread_floor"], self.calib[symbol]["spread_frac"] * price)
        bid = max(0.0, round(price - spread / 2, 2)); ask = round(price + spread / 2, 2)
        occ = f"{symbol:<6}{expiration:%y%m%d}{right}{int(round(strike * 1000)):08d}"
        return Quote(occ, expiration, strike, right, bid, ask, delta, vol)

    def expirations(self, symbol, day):
        out, d = [], day
        while d <= day + timedelta(days=35):
            if d.weekday() < 5 and (symbol in DAILY or d.weekday() == 4):
                out.append(d)
            d += timedelta(days=1)
        return out

    def chain(self, day, symbol, when_utc):
        spot = self.spot(day, symbol, when_utc); step = self.step[symbol]
        base = round(spot / step) * step
        strikes = [base + k * step for k in range(-20, 21)]
        exps = [e for e in self.expirations(symbol, day) if 5 <= (e - day).days <= 25]      # selection uses 7-21 DTE only
        return [self._quote(symbol, day, when_utc, e, k, r) for e in exps for k in strikes for r in ("C", "P")]

    def quote(self, day, symbol, when_utc, occ):
        exp = datetime.strptime(occ[6:12], "%y%m%d").date(); right = occ[12]; strike = int(occ[13:]) / 1000
        return self._quote(symbol, day, when_utc, exp, strike, right)
