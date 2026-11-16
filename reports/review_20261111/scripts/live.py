import json, os, numpy as np, pandas as pd
S = os.path.dirname(os.path.abspath(__file__))
pd.set_option("display.width", 250)
o = pd.read_pickle(os.path.join(S, "oos_spy.pkl"))
def trades(path):
    L = json.load(open(path))["ledger"]; open_ = {}; out = []; last_sell = None
    for x in L:
        if x["type"] == "fill" and x["side"] == "BUY":
            open_.setdefault(x["underlying"], []).append(x)
        elif x["type"] == "fill": last_sell = x
        elif x["type"] == "paper_close":
            b = open_[x["underlying"]].pop(0); q = b["quote"]; sq = last_sell["quote"]
            out.append(dict(sym=x["underlying"], entry=b["timestamp"], exit=x["timestamp"], pnl=x["realized_pnl"], reason=x["reason"],
                            prem=b["fill_price"] * 100, right=b["symbol"].strip()[-9], ver=b["strategy_version"],
                            spread_in=(q["ask"] - q["bid"]) * 100, spread_out=(sq["ask"] - sq["bid"]) * 100,
                            fees=b["fees"] + last_sell["fees"], mfr=(x.get("forecast") or {}).get("max_favourable_r"),
                            regime=(x.get("forecast") or {}).get("regime")))
    t = pd.DataFrame(out); et = pd.to_datetime(t.entry, utc=True).dt.tz_convert("America/New_York")
    t["day"] = et.dt.strftime("%Y-%m-%d"); t["m"] = et.dt.hour * 60 + et.dt.minute - 570
    t["hold"] = (pd.to_datetime(t.exit, utc=True) - pd.to_datetime(t.entry, utc=True)).dt.total_seconds() / 60
    return t
def pf(v):
    v = np.array(v); g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else float("inf")
def line(name, g): return f"  {name:26s} n={len(g):3d} sum={g.pnl.sum():+9.2f} avg={g.pnl.mean():+7.2f} PF={pf(g.pnl):.2f} win={(g.pnl>0).mean():.0%}"
for name, path in (("$10K live", "data/paper_account.json"), ("$1K 11/09-", "data/profiles/small_account_paper/paper_account.json"),
                   ("$1K 10/26-11/06", "data/profiles/small_account_paper_run_20261109_open/paper_account.json")):
    t = trades(path); t = t[t.day >= "2026-10-26"]
    print(f"\n##### {name}"); print(line("all", t))
    for w, g in t.groupby(t.day.map(o.wide)): print(line(f"SPY wide open={w} ({g.day.nunique()}d)", g))
    for k, g in t.groupby("sym"): print(line(k, g))
    for k, g in t.groupby("right"): print(line(k, g))
    for k, g in t.groupby("reason"): print(line(k, g))
    for k, g in t.groupby(pd.cut(t.m, [0, 30, 60, 120, 210, 300, 390]), observed=True): print(line(f"entry min {k}", g))
    for k, g in t.groupby(pd.cut(t.prem, [0, 150, 300, 500, 800, 2000]), observed=True): print(line(f"premium {k}", g))
    half = (t.spread_in + t.spread_out) / 2 + 4 + t.fees   # half-spread each side + 2x$0.02 slippage(x100) + fees
    print(f"  cost of crossing spreads+slippage+fees: ${half.sum():.0f} total, ${half.mean():.2f}/trade, {half.sum()/t.prem.sum():.1%} of premium; gross before costs ${t.pnl.sum()+half.sum():.0f}")
    print(f"  median hold {t.hold.median():.0f} min; winners {t[t.pnl>0].hold.median():.0f}, losers {t[t.pnl<=0].hold.median():.0f}")
    print("  top 4:", [(r.sym, r.day, round(r.pnl)) for r in t.nlargest(4, "pnl").itertuples()], " without them:", round(t.pnl.sum() - t.nlargest(4, "pnl").pnl.sum()))
    print("  by day:", t.groupby("day").pnl.sum().round(0).to_dict())
    if t.mfr.notna().any():
        m = t[t.mfr.notna()]; print(f"  max favourable R (n={len(m)}): median {m.mfr.median():.2f}; reached>=1R {(m.mfr>=1).mean():.0%}; >=2R {(m.mfr>=2).mean():.0%}")
