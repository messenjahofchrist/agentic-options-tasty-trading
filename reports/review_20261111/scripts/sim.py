"""Simplified ORB simulator (NOT the production pipeline): 15-min opening range, enter on the first 1-min close beyond it
with EMA9/21 agreeing, stop = max(0.5 x range, 1 x ATR14) on closes, at 2R switch to runner (lock 1R, trail 1.5R), flat at close.
No regime/sector/confirmation stages, no option prices. Result in stock R."""
import os, glob, sys
import numpy as np, pandas as pd
from feat import load, S
pd.set_option("display.width", 250)
d = pd.read_pickle(os.path.join(S, "dayfeat.pkl")).set_index(["sym", "day"])
def ema(x, n):
    a = 2 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)): out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out
rows = []
for path in sorted(glob.glob(os.path.join(S, "*.npz"))):
    sym = os.path.basename(path)[:-4]; df = load(sym)
    for day, b in df.groupby("day"):
        if len(b) < 380: continue
        m = b.minute.values; c = b.c.values; h = b.h.values; l = b.l.values; v = b.v.values
        orb = m < 15
        if orb.sum() < 15: continue
        hi, lo = h[orb].max(), l[orb].min(); rng = hi - lo
        if rng <= 0: continue
        e9, e21 = ema(c, 9), ema(c, 21)
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1)))); tr[0] = h[0] - l[0]
        atr = pd.Series(tr).rolling(14).mean().values
        done = set(); i = 22; n = len(c)
        while i < n:
            if m[i] >= 360: break
            side = 0
            if c[i] > hi and c[i - 1] <= hi and e9[i] > e21[i]: side = 1
            elif c[i] < lo and c[i - 1] >= lo and e9[i] < e21[i]: side = -1
            if side == 0 or side in done: i += 1; continue
            done.add(side)
            avgv = v[i - 19:i].mean(); rv_bar = v[i] / avgv if avgv > 0 else 0
            entry = c[i]; R = max(0.5 * rng, atr[i]); stop = entry - side * R; best = entry; runner = False; j = i + 1
            reason = "close"
            while j < n:
                px = c[j]; best = max(best, px) if side == 1 else min(best, px)
                fav = (best - entry) * side / R
                if fav >= 2.0:
                    runner = True
                    lock = entry + side * R; tr_ = best - side * 1.5 * R
                    ns = max(lock, tr_) if side == 1 else min(lock, tr_)
                    stop = max(stop, ns) if side == 1 else min(stop, ns)
                if (px - stop) * side <= 0: reason = "runner" if runner else "stop"; break
                j += 1
            j = min(j, n - 1)
            rows.append(dict(sym=sym, day=day, minute=m[i], side=side, r=(c[j] - entry) * side / R, reason=reason,
                             rv_bar=rv_bar, R_pct=R / entry, hold=j - i))
            i = j + 1
t = pd.DataFrame(rows).merge(d.reset_index(), on=["sym", "day"], how="left")
t["or_dir_al"] = t.side * t.or_dir
t["period"] = np.where(t.day < 20260716, "A Jun1-Jul15", np.where(t.day < 20260901, "B Jul16-Aug", "C Sep-Oct9"))
t.to_pickle(os.path.join(S, "sim_trades.pkl"))
print(len(t), "trades")
