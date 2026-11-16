"""Intraday momentum 'noise band' test (after Zarattini, Aziz & Barbon 2024, SPY): go with the move only when price is outside
the band of its normal move-from-open for that time of day (14-day average), checked every 30 min; exit when price comes back
through the band edge or VWAP (whichever is tighter), or at the close. Stock-price returns in basis points; no option prices."""
import os, numpy as np, pandas as pd
from feat import load, S
pd.set_option("display.width", 250)
def run(sym, step=30, cost_bps=0.0):
    df = load(sym)
    px = df.pivot_table(index="day", columns="minute", values="c").ffill(axis=1)
    hi = df.pivot_table(index="day", columns="minute", values="h"); lo = df.pivot_table(index="day", columns="minute", values="l")
    vol = df.pivot_table(index="day", columns="minute", values="v").fillna(0)
    op = df.groupby("day").o.first(); cl = df.groupby("day").c.last(); pc = cl.shift(1)
    move = px.div(op, axis=0).sub(1).abs()
    sig = move.rolling(14).mean().shift(1)
    tp = (hi + lo + px) / 3; vwap = (tp * vol).cumsum(axis=1) / vol.cumsum(axis=1)
    out = []
    for day in px.index[15:]:
        if np.isnan(sig.loc[day, 30]): continue
        up = max(op[day], pc[day]) * (1 + sig.loc[day]); dn = min(op[day], pc[day]) * (1 - sig.loc[day])
        p = px.loc[day]; vw = vwap.loc[day]; pos = 0; entry = None
        for m in range(29, 390, step):          # bar closes at 10:00, 10:30, ...
            if m not in p.index: continue
            c = p[m]
            if pos == 1 and c < max(up[m], vw[m]): out.append((day, em, m, 1, (c / entry - 1) * 1e4)); pos = 0
            elif pos == -1 and c > min(dn[m], vw[m]): out.append((day, em, m, -1, (entry / c - 1) * 1e4)); pos = 0
            if pos == 0 and m < 360:
                if c > up[m]: pos, entry, em = 1, c, m
                elif c < dn[m]: pos, entry, em = -1, c, m
        if pos: c = p.iloc[-1]; out.append((day, em, 389, pos, ((c / entry - 1) if pos == 1 else (entry / c - 1)) * 1e4))
    t = pd.DataFrame(out, columns=["day", "em", "xm", "side", "bps"]); t["bps"] -= cost_bps; t["sym"] = sym
    return t
def pf(v):
    g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else np.inf
allt = []
for sym in ["SPY", "QQQ", "IWM", "DIA", "NVDA", "AAPL", "AMZN", "GOOGL", "PLTR", "NFLX", "INTC", "UBER"]:
    t = run(sym); allt.append(t)
    t["period"] = np.where(t.day < 20260716, "A", np.where(t.day < 20260901, "B", "C"))
    per = "  ".join(f"{p}: n{len(g):3d} {g.bps.sum():+6.0f}bps PF{pf(g.bps):4.2f}" for p, g in t.groupby("period"))
    print(f"{sym:5s} trades {len(t):3d} days {t.day.nunique():2d} win {(t.bps>0).mean():.0%} avg {t.bps.mean():+5.1f}bps sum {t.bps.sum():+6.0f}bps PF {pf(t.bps):4.2f} | {per}")
a = pd.concat(allt); a.to_pickle(os.path.join(S, "nb_trades.pkl"))
# production replay in the same units for comparison
p = pd.read_pickle(os.path.join(S, "trades_feat.pkl"))
p["bps"] = p.dir * (p.exit / p.entry - 1) * 1e4
print("\nProduction 2.5.0 replay, same window, in bps of the stock price:")
print(p.groupby("symbol").bps.agg(n="count", avg="mean", total="sum").round(1).T.to_string())
print("all:", len(p), round(p.bps.mean(), 2), round(p.bps.sum()), "PF", round(pf(p.bps), 2))
for sym in ["SPY", "QQQ"]:
    for step in (15, 30, 60):
        t = run(sym, step); print(sym, "step", step, "n", len(t), "avg", round(t.bps.mean(), 1), "sum", round(t.bps.sum()), "PF", round(pf(t.bps), 2))
