import json, sys, os
import numpy as np, pandas as pd
from feat import load, S
pd.set_option("display.width", 250)
T = json.load(open(sys.argv[1]))["base_250"]
t = pd.DataFrame(T)
diag = pd.DataFrame(list(t.diagnostics)); t = pd.concat([t.drop(columns="diagnostics"), diag], axis=1)
et = pd.to_datetime(t.entry_time).dt.tz_convert("America/New_York")
t["day"] = et.dt.year * 10000 + et.dt.month * 100 + et.dt.day
t["minute"] = et.dt.hour * 60 + et.dt.minute - 570
t["dir"] = np.where(t.side == "CALL", 1, -1)
d = pd.read_pickle(os.path.join(S, "dayfeat.pkl")).rename(columns={"sym": "symbol"})
t = t.merge(d, on=["symbol", "day"], how="left")
spy = d[d.symbol == "SPY"].set_index("day")
t["spy_gap"] = t.day.map(spy.gap); t["spy_rvol_or"] = t.day.map(spy.rvol_or)
# intraday features at entry: cumulative rvol vs prior 14 days same minute, rel strength vs SPY since open
bars = {s: load(s) for s in set(t.symbol) | {"SPY"}}
cum = {}
for s, b in bars.items():
    b["cv"] = b.groupby("day").v.cumsum()
    cum[s] = b.pivot_table(index="day", columns="minute", values="cv").ffill(axis=1)
    bars[s] = b.set_index(["day", "minute"])
def rv_at(r):
    c = cum[r.symbol]; 
    if r.minute not in c.columns: return np.nan
    col = c[r.minute]; prior = col[col.index < r.day].tail(14)
    return col.get(r.day, np.nan) / prior.mean() if len(prior) >= 8 else np.nan
t["rvol_tod"] = t.apply(rv_at, axis=1)
def rs(r):
    try:
        spx = bars["SPY"].loc[(r.day, r.minute), "c"] / spy.open[r.day] - 1
    except KeyError: return np.nan
    return r.dir * ((r.entry / r.open - 1) - spx)
t["rel_str"] = t.apply(rs, axis=1)
t["gap_aligned"] = t.dir * t.gap
t["spy_gap_aligned"] = t.dir * t.spy_gap
t["beyond_prev"] = np.where(t.dir == 1, t.entry > t.prev_high, t.entry < t.prev_low)
t["sma_aligned"] = t.dir * (t.entry - t.sma20) > 0
t["or_dir_aligned"] = t.dir * t.or_dir
t["nth_sym_day"] = t.groupby(["symbol", "day"]).cumcount() + 1
t["nth_day"] = t.sort_values("entry_time").groupby("day").cumcount() + 1
t["period"] = np.where(t.day < 20260716, "A Jun1-Jul15", np.where(t.day < 20260901, "B Jul16-Aug", "C Sep-Oct9"))
t.to_pickle(os.path.join(S, "trades_feat.pkl"))
def pf(v):
    g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else np.inf
def split(name, key):
    rows = []
    for k, grp in t.groupby(key, observed=True):
        row = {"bucket": k}
        for p, gp in grp.groupby("period"):
            row[p] = f"{len(gp):3d} {gp.r.sum():+6.1f} {pf(gp.r):4.2f}"
        row["ALL"] = f"{len(grp):3d} {grp.r.sum():+6.1f} avg{grp.r.mean():+.2f} PF{pf(grp.r):4.2f}"
        rows.append(row)
    print(f"\n== {name}  (n, sumR, PF)"); print(pd.DataFrame(rows).fillna("").to_string(index=False))
print("TOTAL"); split("all", lambda i: "all")
def q(col, bins): return pd.cut(t[col], bins)
split("rvol_or (opening-range volume vs 14d avg)", q("rvol_or", [0, .7, 1.0, 1.3, 1.8, 99]))
split("rvol_tod at entry", q("rvol_tod", [0, .7, 1.0, 1.3, 1.8, 99]))
split("OR width / daily ATR", q("or_w_atr", [0, .2, .3, .4, .55, 9]))
split("gap aligned (stock)", q("gap_aligned", [-1, -.005, -.001, .001, .005, 1]))
split("SPY gap aligned", q("spy_gap_aligned", [-1, -.005, -.001, .001, .005, 1]))
split("rel strength vs SPY at entry", q("rel_str", [-1, 0, .003, .008, .015, 1]))
split("beyond prior-day high/low", "beyond_prev")
split("20d SMA aligned", "sma_aligned")
split("OR direction aligned", "or_dir_aligned")
split("nth trade in symbol that day", t.nth_sym_day.clip(upper=3))
split("nth trade of day", t.nth_day.clip(upper=6))
split("entry time", pd.cut(t.minute, [0, 30, 60, 120, 210, 300, 390]))
split("setup", "setup_type"); split("regime", "regime"); split("side", "side"); split("symbol", "symbol")
split("ext_atr", q("ext_atr", [-9, .5, 1, 2, 3, 99])); split("run_pct", q("run_pct", [-9, .3, .5, .7, .9, 99]))
split("exit_reason", "exit_reason")
