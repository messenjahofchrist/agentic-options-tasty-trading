import os, numpy as np, pandas as pd
from feat import S
pd.set_option("display.width", 250)
t = pd.read_pickle(os.path.join(S, "sim_trades.pkl"))
t = t[t.rvol_or.notna()]
FAV = ["SPY","QQQ","IWM","NVDA","AAPL","AMZN","GOOGL","PLTR","NFLX","INTC","UBER"]
SECT = [s for s in t.sym.unique() if s.startswith("XL")]
def pf(v):
    g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else np.inf
def split(name, x, key):
    rows = []
    for k, grp in x.groupby(key, observed=True):
        row = {"bucket": k}
        for p, gp in grp.groupby("period"):
            row[p] = f"{len(gp):4d} {gp.r.mean():+.3f} {pf(gp.r):4.2f}"
        row["ALL"] = f"{len(grp):4d} sum{grp.r.sum():+7.1f} avg{grp.r.mean():+.3f} PF{pf(grp.r):4.2f} win{(grp.r>0).mean():.0%}"
        rows.append(row)
    print(f"\n== {name}  (n, avgR, PF)"); print(pd.DataFrame(rows).fillna("").to_string(index=False))
bins = [0, .6, .8, 1.0, 1.25, 1.6, 2.2, 99]
for label, x in (("ALL 52 symbols", t), ("11 favorites", t[t.sym.isin(FAV)]), ("41 others", t[~t.sym.isin(FAV)]),
                 ("others excl. sector ETFs", t[~t.sym.isin(FAV) & ~t.sym.isin(SECT)])):
    split(label + ": total", x, lambda i: "all")
    split(label + ": by opening-range relative volume", x, pd.cut(x.rvol_or, bins))
split("ALL: OR direction aligned", t, "or_dir_al")
split("ALL: entry time", t, pd.cut(t.minute, [0, 30, 60, 120, 210, 300, 390]))
split("ALL: OR width/daily ATR", t, pd.cut(t.or_w_atr, [0, .2, .3, .4, .55, 9]))
split("ALL: breakout-bar rel volume", t, pd.cut(t.rv_bar, [0, 1.2, 2, 3, 5, 999]))
split("ALL: side", t, "side")
# cross-sectional: each day rank symbols by rvol_or (known at 9:45)
t["rank"] = t.groupby("day").rvol_or.rank(ascending=False, method="first")
dsym = t.drop_duplicates(["sym","day"]).copy()
split("ALL: daily rank by rvol_or (among traded symbols that day)", t, pd.cut(t["rank"], [0, 3, 6, 12, 24, 99]))
f = t[t.sym.isin(FAV)].copy(); f["rank"] = f.groupby("day").rvol_or.rank(ascending=False, method="first")
split("FAV: daily rank by rvol_or", f, pd.cut(f["rank"], [0, 2, 4, 7, 99]))
