import json, glob, os, sys
import numpy as np, pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
S = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open("data/v2_shared_cache/daily_v2_2026-11-10.json"))["payload"]["rows"]["SPY"]
print(rows[-1])
d = pd.DataFrame(rows); d["day"] = d.timestamp.str[:10]
d = d.drop_duplicates("day").set_index("day").sort_index()
pc = d.close.shift(1)
tr = np.maximum(d.high - d.low, np.maximum((d.high - pc).abs(), (d.low - pc).abs()))
d["atr14"] = tr.rolling(14).mean().shift(1)
# today's (11/10) ATR needs 11/9 row: present if file stored 11/10 holds completed sessions through 11/9
ors = {}
for f in sorted(glob.glob("data/v2_shadow_original/decisions.jsonl*")):
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            if '"underlying": "SPY"' not in line or '"opening_range_high": null' in line: continue
            day = line[line.index('"timestamp_et": "') + 17:][:10]
            if day in ors: continue
            r = json.loads(line)["diagnostics"]
            ors[day] = (r["opening_range_high"], r["opening_range_low"])
o = pd.DataFrame(ors, index=["hi", "lo"]).T.sort_index()
last = d.iloc[-1]
o["atr14"] = [d.atr14.get(k, np.nan) for k in o.index]
# for the last session not in the daily file, compute ATR from the trailing 14 rows
for k in o.index[o.atr14.isna()]:
    prior = tr[tr.index < k].tail(14); o.loc[k, "atr14"] = prior.mean() if len(prior) == 14 else np.nan
o["or_w_atr"] = (o.hi - o.lo) / o.atr14
o["wide"] = o.or_w_atr > 0.30
print(o.round(3).to_string())
o.to_pickle(os.path.join(S, "oos_spy.pkl"))
def pf(v):
    v = np.array(v); g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else float("inf")
def report(name, t, col):
    t = t[t.day.isin(o.index)]
    for w, g in t.groupby(t.day.map(o.wide)):
        print(f"  {name:28s} SPY-wide-open={w!s:5s} days={g.day.nunique():2d} n={len(g):3d} sum={g[col].sum():+9.2f} avg={g[col].mean():+7.2f} PF={pf(g[col]):.2f} win={(g[col]>0).mean():.0%}")
def ledger(path):
    a = json.load(open(path)); tr_ = a.get("closed_trades") or a.get("trades") or []
    return a, tr_
for name, path in (("$10K live", "data/paper_account.json"), ("$1K live (since 11/09)", "data/profiles/small_account_paper/paper_account.json"),
                   ("$1K live (10/26-11/06)", "data/profiles/small_account_paper_run_20261109_open/paper_account.json")):
    try:
        a, t = ledger(path)
    except Exception as e:
        print(name, "ERR", e); continue
    if not t: print(name, list(a.keys())); continue
    t = pd.DataFrame(t); 
    tc = "entry_time" if "entry_time" in t else [c for c in t.columns if "entry" in c and "time" in c][0]
    t["day"] = pd.to_datetime(t[tc], utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    pc_ = "realized_pnl" if "realized_pnl" in t else ("pnl" if "pnl" in t else None)
    if pc_ is None: print(name, list(t.columns)); continue
    t = t[t.day >= "2026-10-26"]
    print(name, "total", len(t), round(t[pc_].sum(), 2)); report(name, t, pc_)
    if name.startswith("$10K"):
        sym = "underlying" if "underlying" in t else "symbol"
        print(t.groupby(sym)[pc_].agg(["count", "sum"]).round(1).sort_values("sum").T.to_string())
        t["et"] = pd.to_datetime(t[tc], utc=True).dt.tz_convert("America/New_York"); t["m"] = t.et.dt.hour * 60 + t.et.dt.minute - 570
        print(t.groupby(pd.cut(t.m, [0, 30, 60, 120, 210, 300, 390]), observed=True)[pc_].agg(["count", "sum"]).round(1).T.to_string())
        print("top 5:", t.nlargest(5, pc_)[[sym, "day", pc_]].values.tolist())
p = "reports/new_rules_20261110/tastytrade-20261111T053500Z-base_250+reentry30+reentry60+reentry120+ttt075+ttt100+ttt150+trail100+re_ttt+all3-trades.json"
t = pd.DataFrame(json.load(open(p))["base_250"])
t["day"] = pd.to_datetime(t.entry_time, utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
print("replay Oct16-Nov10 total", len(t), round(t.r.sum(), 1), "| days with OR data:", t.day.isin(o.index).sum())
report("replay 2.5.0 (R)", t, "r")
print(t.groupby("symbol").r.agg(["count", "sum"]).round(1).T.to_string())
