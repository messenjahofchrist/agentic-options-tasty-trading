# Phase 2/3 driver: baseline + alternatives on 2.5.0 replay signals, priced with real quotes where recorded and the
# calibrated proxy elsewhere; tuning (Jun 1 - Aug 31) vs validation (Sep 1 - Nov 6); acceptance gates.
# usage: run_phase3.py <signals_massive.json> <signals_tasty.json> <out_dir> [fill]
import json, statistics as st, sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import evaluate as ev
from pricing import RealQuotes
from proxy import ProxyQuotes, calibrate
from trader.models import Bar
import trader.v2.replay as r

ET = ZoneInfo("America/New_York")
ROOT = Path(r"C:\development\optiobot\agentic-options-tasty-trading")
HERE = Path(__file__).parent
SYMS = ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "AMZN", "GOOGL", "PLTR", "NFLX", "INTC", "UBER"]
sig_m = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); sig_t = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
OUT = Path(sys.argv[3]); OUT.mkdir(parents=True, exist_ok=True)
FILL = sys.argv[4] if len(sys.argv) > 4 else "adverse"
d = lambda s: datetime.fromisoformat(s["entry_time"]).astimezone(ET).date()
last_m = max(d(s) for s in sig_m)
signals = [s for s in sig_m] + [s for s in sig_t if d(s) > last_m]

# --- quote sources ---------------------------------------------------------------------------------------------
real = RealQuotes(ROOT, fill=FILL)
real_days = sorted({date.fromisoformat(p.name) for p in (ROOT / "data/option_history").iterdir() if p.is_dir()})
daily = {s: [(date.fromisoformat(a), b) for a, b in v] for s, v in json.loads((HERE / "daily_closes.json").read_text()).items()}
cal = calibrate(ROOT, SYMS, real_days)
for s in SYMS:
    hv = []
    for day, iv in cal[s]["iv_samples"]:
        c = [x for dd, x in daily[s] if dd < day][-21:]
        import math
        rets = [math.log(b / a) for a, b in zip(c, c[1:])]
        h = st.pstdev(rets) * math.sqrt(252) if len(rets) >= 20 else None
        if h:
            hv.append(iv / h)
    cal[s]["iv_ratio"] = st.median(hv) if hv else 1.0
    cal[s].pop("iv_samples")
print("calibration", json.dumps(cal), flush=True)
steps = {}
for s in SYMS:
    data = real._day(real_days[-1], s)
    ks = sorted({c["strike"] for c in data["contracts"].values()})
    near = [b - a for a, b in zip(ks, ks[1:]) if abs(a - data["spot_close"]) < data["spot_close"] * 0.05]
    steps[s] = min(near) if near else 1.0
bars = r.load_massive_cache(ROOT, SYMS)
for s, rows in json.loads((HERE / "minute_bars_oct.json").read_text()).items():
    cut = bars[s][-1].timestamp if bars.get(s) else datetime(2000, 1, 1, tzinfo=timezone.utc)
    bars.setdefault(s, []).extend(Bar(datetime.fromisoformat(x["timestamp"]).astimezone(timezone.utc), x["open"], x["high"], x["low"], x["close"], x["volume"] or 0)
                                  for x in rows if datetime.fromisoformat(x["timestamp"]) > cut)
proxy = ProxyQuotes(bars, daily, cal, steps)


class Mixed:
    """Real quotes on recorded days, proxy elsewhere; remembers which one priced each day."""
    def has(self, day, symbol):
        return real.has(day, symbol) or proxy.has(day, symbol)
    def _src(self, day, symbol):
        return real if real.has(day, symbol) else proxy
    def chain(self, day, symbol, when):
        return self._src(day, symbol).chain(day, symbol, when)
    def quote(self, day, symbol, when, occ):
        return self._src(day, symbol).quote(day, symbol, when, occ)


ev.rq = Mixed()
for s in signals:
    s["source"] = "real" if real.has(d(s), s["symbol"]) else "proxy"
STRUCTURES = ("single", "single_350", "single_budget", "single_otm_budget", "debit_spread", "debit_spread_costcap")
res = ev.run_all(signals, 1000.0, STRUCTURES, quiet=True)


def split_metrics(trades):
    tune = [t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() <= date(2026, 8, 31)]
    val = [t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() > date(2026, 8, 31)]
    real_only = [t for t in trades if t.get("source") == "real"]
    return {"tuning (Jun-Aug, proxy)": ev.metrics(tune, 1000), "validation (Sep-Nov 6)": ev.metrics(val, 1000),
            "real quotes only (Oct 26-Nov 6)": ev.metrics(real_only, 1000)}


def gates(trades, start=1000.0):
    val = sorted([t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() > date(2026, 8, 31)], key=lambda t: t["entry_time"])
    m = ev.metrics(val, start)
    out = {"1 >=40 OOS trades": m.get("n", 0) >= 40, "2 PF > 1.25": (m.get("PF") or 0) > 1.25}
    by_reg = defaultdict(float)
    for t in val:
        by_reg[t.get("regime")] += t["pnl"]
    out["3 positive in >=2 market types"] = sum(1 for v in by_reg.values() if v > 0) >= 2
    eq = peak = start; dd = 0
    for t in val:
        eq += t["pnl"]; peak = max(peak, eq); dd = max(dd, (peak - eq) / peak)
    out["4 max drawdown <= 20%"] = dd <= 0.20
    out["5a positive without best 2"] = (m.get("net_without_best2") or -1) > 0
    half = len(val) // 2
    out["5b positive in each half"] = bool(val) and sum(t["pnl"] for t in val[:half]) > 0 and sum(t["pnl"] for t in val[half:]) > 0
    real_only = [t for t in trades if t.get("source") == "real"]
    out["7 real-quote expectancy >= 0"] = bool(real_only) and sum(t["pnl"] for t in real_only) >= 0
    out["(6 robustness: checked separately)"] = None
    return out


report = {"fill_model": FILL, "signals": len(signals), "signal_sources": dict(st and {k: sum(1 for s in signals if s["source"] == k) for k in ("real", "proxy")}),
          "calibration": cal, "strike_steps": steps}
for name in STRUCTURES:
    acct = res[name]["trades"]
    for t in acct:
        t["source"] = "real" if real.has(datetime.fromisoformat(t["entry_time"]).astimezone(ET).date(), t["symbol"]) else "proxy"
        t["regime"] = next((s.get("regime") for s in signals if s["entry_time"] == t["entry_time"] and s["symbol"] == t["symbol"]), None)
    raw = res[name]["raw"]
    for t in raw:
        t["source"] = "real" if real.has(datetime.fromisoformat(t["entry_time"]).astimezone(ET).date(), t["symbol"]) else "proxy"
    report[name] = {"account": split_metrics(acct), "every_signal_no_limits": split_metrics(raw), "skips": res[name]["skips"],
                    "end_equity": res[name]["end_equity"], "max_dd%": res[name]["max_dd%"], "gates": gates(acct)}
    print("\n==", name, "end equity", res[name]["end_equity"], "maxDD%", res[name]["max_dd%"])
    for k, v in report[name]["account"].items():
        print("  account ", k, v)
    for k, v in report[name]["every_signal_no_limits"].items():
        print("  signals ", k, {x: v.get(x) for x in ("n", "win%", "net", "PF", "exp%risk", "avg_risk", "gross_mid", "costs")})
    print("  skips", res[name]["skips"])
    print("  gates", report[name]["gates"])
# --- gate 6: robustness, each option-layer setting moved -20% / +20% (validation period, account under the limits) ---
ROBUST = {"budget": (0.08, 0.12), "delta": (0.50, 0.60), "costcap": (0.16, 0.24)}
report["robustness"] = {}
for name in ("single_otm_budget", "debit_spread", "debit_spread_costcap"):
    rows = {}
    for key, values in ROBUST.items():
        if (key == "costcap" and name != "debit_spread_costcap") or (key == "delta" and name == "single_otm_budget"):
            continue
        for v in values:
            saved = dict(ev.PARAMS); ev.PARAMS[key] = v
            trades, skips, equity, maxdd = ev.account(signals, name, 1000.0)
            ev.PARAMS.clear(); ev.PARAMS.update(saved)
            for t in trades:
                t["source"] = "real" if real.has(datetime.fromisoformat(t["entry_time"]).astimezone(ET).date(), t["symbol"]) else "proxy"
                t["regime"] = next((x.get("regime") for x in signals if x["entry_time"] == t["entry_time"] and x["symbol"] == t["symbol"]), None)
            val = [t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() > date(2026, 8, 31)]
            rows[f"{key}={v}"] = {"validation": ev.metrics(val, 1000), "end_equity": round(equity, 2), "gates": gates(trades)}
            print("  robust", name, f"{key}={v}", rows[f"{key}={v}"]["validation"], flush=True)
    report["robustness"][name] = rows
(OUT / f"phase3_{FILL}.json").write_text(json.dumps({**report, "trades": {n: res[n]["trades"] for n in STRUCTURES}}, indent=1, default=str), encoding="utf-8")
