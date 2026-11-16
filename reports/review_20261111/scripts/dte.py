"""Real recorded option quotes (Oct 26 - Nov 10): price the SAME entries and exits with different contract choices."""
import json, sys, glob, os
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(r"C:\development\optiobot\agentic-options-tasty-trading")
sys.path.insert(0, str(ROOT / "reports/1k_phase23/scripts"))
import numpy as np, pandas as pd
from pricing import RealQuotes, Quote, implied_vol, bs, years_to_expiry, FEES_OPEN, FEES_CLOSE, SLIP
ET = ZoneInfo("America/New_York")
pd.set_option("display.width", 250)
class RQ(RealQuotes):
    def chain(self, day, symbol, when_utc):
        d = self._day(day, symbol); m = int((when_utc.astimezone(ET) - d["open"]).total_seconds() // 60)
        spot = self._at(d["underlying"], d["und_m"], m); out = []
        for occ, c in d["contracts"].items():
            exp = date.fromisoformat(c["expiration"])
            if not 1 <= (exp - day).days <= 25: continue
            if spot and abs(c["strike"] / spot - 1) > 0.06: continue
            bid = self._at(c.get("bid"), c.get("bid_m"), m); ask = self._at(c.get("ask"), c.get("ask_m"), m)
            if bid is None or ask is None or ask <= 0 or ask < bid: continue
            t = years_to_expiry(when_utc, exp)
            iv = implied_vol((bid + ask) / 2, spot, c["strike"], t, c["right"]) if spot else None
            delta = bs(spot, c["strike"], t, iv, c["right"])[1] if iv else None
            out.append(Quote(occ, exp, c["strike"], c["right"], bid, ask, delta, iv))
        return out
VARIANTS = {  # name: (min_dte, max_dte, target delta, lo, hi, prefer_short)
    "A current 7-21d 0.55": (7, 21, .55, .45, .65, False),
    "B 1-6d 0.55": (1, 6, .55, .45, .65, True),
    "C 1-6d 0.70": (1, 6, .70, .60, .80, True),
    "D 7-21d 0.75": (7, 21, .75, .65, .85, False),
    "E 3-10d 0.55": (3, 10, .55, .45, .65, True),
}
def pick(chain, side, day, v):
    lo_d, hi_d, tgt, lo, hi, short = v; right = "C" if side == "CALL" else "P"
    ok = [q for q in chain if q.right == right and q.delta is not None and lo_d <= (q.expiration - day).days <= hi_d
          and lo <= abs(q.delta) <= hi and (q.ask - q.bid) <= 0.10 + 1e-9 and q.mid > 0 and (q.ask - q.bid) / q.mid <= 0.10 + 1e-9]
    if not ok: return None
    if short:
        first = min((q.expiration - day).days for q in ok); ok = [q for q in ok if (q.expiration - day).days == first]
    return min(ok, key=lambda q: (abs(abs(q.delta) - tgt), (q.expiration - day).days))
def run(signals, fill):
    rq = RQ(ROOT, fill=fill); rows = []
    for s in sorted(signals, key=lambda s: (s["entry_time"][:10], s["symbol"])):
        t_in = datetime.fromisoformat(s["entry_time"]).astimezone(timezone.utc); t_out = datetime.fromisoformat(s["exit_time"]).astimezone(timezone.utc)
        day = t_in.astimezone(ET).date()
        if not rq.has(day, s["symbol"]): continue
        chain = rq.chain(day, s["symbol"], t_in)
        for name, v in VARIANTS.items():
            q = pick(chain, s["side"], day, v)
            if q is None: rows.append(dict(variant=name, symbol=s["symbol"], entry=s["entry_time"], skip=True)); continue
            a = rq.quote(day, s["symbol"], t_in, q.occ); b = rq.quote(day, s["symbol"], t_out, q.occ)
            if a is None or b is None: rows.append(dict(variant=name, symbol=s["symbol"], entry=s["entry_time"], skip=True)); continue
            pnl = (b.bid - a.ask) * 100 - FEES_OPEN - FEES_CLOSE - 2 * SLIP
            cost = (a.ask - a.bid) * 50 + (b.ask - b.bid) * 50 + FEES_OPEN + FEES_CLOSE + 2 * SLIP
            rows.append(dict(variant=name, symbol=s["symbol"], entry=s["entry_time"], skip=False, pnl=pnl, prem=a.ask * 100, cost=cost,
                             dte=(q.expiration - day).days, delta=abs(q.delta)))
    return pd.DataFrame(rows)
def pf(v):
    v = np.array(v); g = v[v > 0].sum(); l = -v[v < 0].sum(); return g / l if l else float("inf")
def summarize(title, t):
    print(f"\n=== {title}")
    # same-trade comparison: only signals priced under every variant
    ok = t[~t.skip].groupby(["symbol", "entry"]).variant.nunique(); common = ok[ok == len(VARIANTS)].index
    for label, x in (("all priced", t[~t.skip]), ("signals priced under every variant", t[~t.skip].set_index(["symbol", "entry"]).loc[common].reset_index())):
        print(f" -- {label}")
        for name, g in x.groupby("variant"):
            print(f"   {name:22s} n={len(g):3d} P&L={g.pnl.sum():+8.0f} avg={g.pnl.mean():+7.1f} PF={pf(g.pnl):4.2f} win={(g.pnl>0).mean():.0%} avg premium=${g.prem.mean():5.0f} "
                  f"cost/trade=${g.cost.mean():5.2f} return on premium={g.pnl.sum()/g.prem.sum():+.1%} worst={g.pnl.min():+.0f} best={g.pnl.max():+.0f} dte={g.dte.mean():.1f} fits $350: {(g.prem<=350).mean():.0%}")
replay = json.load(open(glob.glob(str(ROOT / "reports/new_rules_20261110/tastytrade-*-trades.json"))[0]))["base_250"]
L = json.load(open(ROOT / "data/paper_account.json"))["ledger"]; open_ = {}; live = []
for x in L:
    if x["type"] == "fill" and x["side"] == "BUY": open_.setdefault(x["underlying"], []).append(x)
    elif x["type"] == "paper_close":
        b = open_[x["underlying"]].pop(0)
        live.append(dict(symbol=x["underlying"], side="CALL" if b["symbol"].strip()[-9] == "C" else "PUT", entry_time=b["timestamp"], exit_time=x["timestamp"], actual=x["realized_pnl"]))
out = {}
for fill in ("open", "adverse"):
    for title, sig in (("2.5.0 replay signals", replay), ("actual $10K live entries/exits", live)):
        t = run(sig, fill); out[(fill, title)] = t
        summarize(f"{title}, fill={fill}", t)
pd.to_pickle(out, os.path.join(os.path.dirname(os.path.abspath(__file__)), "dte_results.pkl"))
print("\nlive actual total", round(sum(x["actual"] for x in live), 2), "n", len(live))
