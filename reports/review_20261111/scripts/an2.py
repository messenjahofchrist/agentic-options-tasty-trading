import os, numpy as np, pandas as pd
from feat import S
pd.set_option("display.width", 250)
d = pd.read_pickle(os.path.join(S, "dayfeat.pkl"))
spy = d[d.sym == "SPY"].set_index("day")
def pf(v):
    g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else np.inf
def split(name, x, key):
    rows = []
    for k, grp in x.groupby(key, observed=True):
        row = {"bucket": k}
        for p, gp in grp.groupby("period"):
            row[p] = f"{len(gp):4d} {gp.r.sum():+7.1f} {pf(gp.r):4.2f}"
        row["ALL"] = f"{len(grp):4d} sum{grp.r.sum():+7.1f} avg{grp.r.mean():+.3f} PF{pf(grp.r):4.2f} win{(grp.r>0).mean():.0%}"
        rows.append(row)
    print(f"\n== {name}  (n, sumR, PF)"); print(pd.DataFrame(rows).fillna("").to_string(index=False))
p = pd.read_pickle(os.path.join(S, "trades_feat.pkl")); p["sym"] = p.symbol
n = pd.read_pickle(os.path.join(S, "sim_trades.pkl")); n = n[n.rvol_or.notna()]
FAV = ["SPY","QQQ","IWM","NVDA","AAPL","AMZN","GOOGL","PLTR","NFLX","INTC","UBER"]
for label, t in (("PROD replay 2.5.0", p), ("NAIVE all 52", n), ("NAIVE favorites", n[n.sym.isin(FAV)])):
    t = t.copy()
    t["abs_spy_gap"] = t.day.map(spy.gap).abs(); t["spy_rvol"] = t.day.map(spy.rvol_or); t["spy_orw"] = t.day.map(spy.or_w_atr)
    t["abs_gap"] = t.gap.abs()
    split(label + ": |SPY gap|", t, pd.cut(t.abs_spy_gap, [-1, .0015, .003, .005, .008, 1]))
    split(label + ": SPY opening rel volume", t, pd.cut(t.spy_rvol, [0, .7, .85, 1.0, 1.2, 99]))
    split(label + ": |own gap|", t, pd.cut(t.abs_gap, [-1, .002, .005, .01, .02, 1]))
    split(label + ": SPY OR width/ATR", t, pd.cut(t.spy_orw, [0, .15, .22, .3, .4, 9]))
print("\nPROD minute histogram 25-65:", p.minute[(p.minute>=25)&(p.minute<=65)].value_counts().sort_index().to_dict())
split("PROD ex-SPY", p[p.symbol != "SPY"], lambda i: "all")
split("NAIVE per symbol (favorites + index)", n[n.sym.isin(FAV + ["DIA","TLT","BAC"])], "sym")
# day circuit breaker on production trades: skip entries made after k losses (closed before entry) that day
p = p.sort_values("entry_time")
def breaker(k, consecutive):
    keep = []
    for day, g in p.groupby("day"):
        closed = []
        for _, r in g.iterrows():
            done = sorted([(e, rr) for e, rr in closed if e <= r.entry_time])
            if consecutive:
                streak = 0
                for e, rr in done: streak = streak + 1 if rr < 0 else 0
                bad = streak >= k
            else:
                bad = sum(rr < 0 for e, rr in done) >= k
            if not bad: keep.append(r.name); closed.append((r.exit_time, r.r))
    return p.loc[keep]
for k, c in ((2, True), (2, False), (3, False)):
    split(f"PROD breaker: stop after {k} {'consecutive ' if c else ''}losses that day", breaker(k, c), lambda i: "all")
