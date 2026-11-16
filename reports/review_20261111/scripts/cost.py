import json, os, glob, numpy as np, pandas as pd
S = os.path.dirname(os.path.abspath(__file__))
pd.set_option("display.width", 250)
def trades(path):
    L = json.load(open(path))["ledger"]; open_ = {}; out = []; last_sell = None
    for x in L:
        if x["type"] == "fill" and x["side"] == "BUY": open_.setdefault(x["underlying"], []).append(x)
        elif x["type"] == "fill": last_sell = x
        elif x["type"] == "paper_close":
            b = open_[x["underlying"]].pop(0); q = b["quote"]; sq = last_sell["quote"]
            out.append(dict(sym=x["underlying"], entry=b["timestamp"][:16], pnl=x["realized_pnl"], prem=b["fill_price"] * 100,
                            cost=(q["ask"] - q["bid"]) * 50 + (sq["ask"] - sq["bid"]) * 50 + 0.04 + b["fees"] + last_sell["fees"]))
    return pd.DataFrame(out)
def approved(pattern):
    out = []
    for f in glob.glob(pattern):
        for line in open(f, encoding="utf-8"):
            if '"outcome": "APPROVED"' not in line: continue
            r = json.loads(line); e = r["entry"]
            out.append(dict(sym=r["symbol"], entry=r["timestamp"][:16], delta=abs(e["option"]["delta"]), px=e["entry_underlying"],
                            stop=abs(e["entry_underlying"] - e["stop_underlying"]), enr=e["expected_net_r"]))
    return pd.DataFrame(out).drop_duplicates(["sym", "entry"])
def pf(v):
    v = np.array(v); g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else float("inf")
live = {}
for name, led, dec in (("$10K", "data/paper_account.json", "data/v2_decisions.jsonl*"),
                       ("$1K now", "data/profiles/small_account_paper/paper_account.json", "data/profiles/small_account_paper/v2_decisions.jsonl*"),
                       ("$1K 10/26-11/06", "data/profiles/small_account_paper_run_20261109_open/paper_account.json", "data/profiles/small_account_paper_run_20261109_open/v2_decisions.jsonl*")):
    t = trades(led); t = t[t.entry >= "2026-10-26"]
    try: a = approved(dec)
    except Exception as ex: print(name, "no decisions", ex); a = pd.DataFrame(columns=["sym", "entry", "delta", "px", "stop", "enr"])
    t = t.merge(a, on=["sym", "entry"], how="left")
    t["r_dollars"] = t.delta * t.stop * 100           # what 1R of stock move is worth on the option
    t["cost_r"] = t.cost / t.r_dollars                # round-trip cost measured in R
    t["cost_bps"] = t.cost / (t.delta * t.px * 0.01)  # ... and in basis points of the stock price
    live[name] = t
    print(f"\n##### {name}: {len(t)} trades, matched to decision {t.delta.notna().sum()}, P&L {t.pnl.sum():+.0f}, costs {t.cost.sum():.0f} ({t.cost.sum()/t.prem.sum():.1%} of premium), gross {t.pnl.sum()+t.cost.sum():+.0f}")
    m = t[t.cost_r.notna()]
    print(f"  cost in R: median {m.cost_r.median():.2f}; cost in bps of stock: median {m.cost_bps.median():.1f}; 1R worth median ${m.r_dollars.median():.0f}")
    for k, g in m.groupby(pd.cut(m.cost_r, [0, .10, .20, .35, 9]), observed=True):
        print(f"   cost_r {str(k):12s} n={len(g):3d} P&L={g.pnl.sum():+8.0f} avg={g.pnl.mean():+7.1f} PF={pf(g.pnl):.2f} win={(g.pnl>0).mean():.0%} syms={sorted(set(g.sym))}")
    for k, g in m.groupby(pd.cut(m.enr, [0, 1.25, 1.5, 1.75, 9]), observed=True):
        print(f"   expected_net_r {str(k):12s} n={len(g):3d} P&L={g.pnl.sum():+8.0f} avg={g.pnl.mean():+7.1f} PF={pf(g.pnl):.2f}")
    print(m.groupby("sym").agg(n=("pnl", "count"), pnl=("pnl", "sum"), cost=("cost", "mean"), prem=("prem", "mean"), cost_r=("cost_r", "median"), cost_bps=("cost_bps", "median"), R_usd=("r_dollars", "median")).round(2).sort_values("cost_r").to_string())
allv = pd.concat(live.values()); allv = allv[allv.cost_bps.notna()]
cb = allv.groupby("sym").cost_bps.median()
print("\nmedian live cost in bps of the stock, by symbol:", cb.round(1).to_dict())
# replay trades net of measured option cost (cost bps per symbol from live fills; delta cancels out in R terms)
p1 = pd.read_pickle(os.path.join(S, "trades_feat.pkl"))[["symbol", "entry_time", "entry", "exit", "r", "side", "period"]]
t2 = pd.DataFrame(json.load(open(glob.glob("reports/new_rules_20261110/tastytrade-*-trades.json")[0]))["base_250"])[["symbol", "entry_time", "entry", "exit", "r", "side"]]
t2["period"] = "D Oct16-Nov10"
p = pd.concat([p1, t2]); p["dir"] = np.where(p.side == "CALL", 1, -1)
p["move_bps"] = p.dir * (p.exit / p.entry - 1) * 1e4
p["stop_bps"] = np.where(p.r.abs() > 0.05, p.move_bps / p.r, np.nan)
p["stop_bps"] = p.stop_bps.fillna(p.groupby("symbol").stop_bps.transform("median"))
p["cost_bps"] = p.symbol.map(cb)
p["cost_r"] = p.cost_bps / p.stop_bps
p["net_r"] = p.r - p.cost_r
print("\nReplay 2.5.0, Jun 1-Nov 10, stock R before costs vs after measured option costs")
g = p.groupby("symbol").agg(n=("r", "count"), gross_R=("r", "sum"), cost_R=("cost_r", "sum"), net_R=("net_r", "sum"), cost_per_trade_R=("cost_r", "median"), stop_bps=("stop_bps", "median"), cost_bps=("cost_bps", "first")).round(2)
print(g.sort_values("cost_per_trade_R").to_string()); print("TOTAL", len(p), round(p.r.sum(), 1), round(p.cost_r.sum(), 1), round(p.net_r.sum(), 1))
print(p.groupby("period").agg(n=("r", "count"), gross_R=("r", "sum"), cost_R=("cost_r", "sum"), net_R=("net_r", "sum")).round(1).to_string())
for cap in (0.10, 0.15, 0.20, 0.30):
    k = p[p.cost_r <= cap]
    print(f"cost gate <= {cap:.2f}R: keeps {len(k)}/{len(p)} trades; net R {k.net_r.sum():+.1f} (PF {pf(k.net_r):.2f}) vs all {p.net_r.sum():+.1f} (PF {pf(p.net_r):.2f}); by period:",
          {a: round(b, 1) for a, b in k.groupby("period").net_r.sum().items()}, "syms:", k.symbol.value_counts().to_dict())
p.to_pickle(os.path.join(S, "replay_cost.pkl"))
