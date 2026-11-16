# Phase 2/3: price signals as option structures at their real entry/exit minutes and run a $1K account through the
# Phase 4 limits. usage: evaluate.py <signals.json> <out.json> [start_equity]
import copy, json, statistics as st, sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pricing import RealQuotes, select_single, select_spread, FEES_OPEN, FEES_CLOSE, SLIP

ET = ZoneInfo("America/New_York")
ROOT = Path(r"C:\development\optiobot\agentic-options-tasty-trading")
START = 1000.0
CHAINS = {}        # (source, symbol, entry time) -> chain, shared by the structures
rq = None          # the quote source: RealQuotes or proxy.ProxyQuotes (set by main() or the caller)
PARAMS = {"budget": 0.10, "delta": 0.55, "costcap": 0.20}   # moved +-20% by the robustness check


def ts(s): return datetime.fromisoformat(s).astimezone(timezone.utc)


def price(sig, structure, equity):
    """-> dict(risk, pnl, gross_mid, costs, legs) or a skip reason (str)."""
    t_in, t_out = ts(sig["entry_time"]), ts(sig["exit_time"])
    day = t_in.astimezone(ET).date()
    if not rq.has(day, sig["symbol"]):
        return "no quotes for this day"
    key = (id(rq), sig["symbol"], sig["entry_time"])
    if key not in CHAINS:
        CHAINS[key] = rq.chain(day, sig["symbol"], t_in)
    chain = [copy.copy(q) for q in CHAINS[key]]
    d = PARAMS["delta"]
    if structure == "single_otm_budget":
        # contract selection alternative: the highest-|delta| option in 0.20-0.45 whose worst case fits the budget
        right = "C" if sig["side"] == "CALL" else "P"; cap = PARAMS["budget"] * equity
        ok = [q for q in chain if q.right == right and q.delta is not None and 7 <= (q.expiration - day).days <= 21
              and 0.20 <= abs(q.delta) <= 0.45 and q.mid > 0 and (q.ask - q.bid) <= 0.10 + 1e-9
              and (q.ask - q.bid) / q.mid <= 0.10 + 1e-9 and q.ask * 100 + FEES_OPEN + FEES_CLOSE + 2 * SLIP <= cap]
        long = max(ok, key=lambda q: (abs(q.delta), -(q.expiration - day).days)) if ok else None
        if long is None:
            return "no 0.20-0.45 delta option fits the budget"
    else:
        long = select_single(chain, sig["side"], day, target=d, lo=d - 0.10, hi=d + 0.10)
    if long is None:
        return "no contract passes the strategy's filters"
    filled = rq.quote(day, sig["symbol"], t_in, long.occ)          # entry price under the chosen fill model
    long.bid, long.ask = filled.bid, filled.ask
    budget = PARAMS["budget"] * equity
    if structure in ("single", "single_budget", "single_350", "single_otm_budget"):
        risk = long.ask * 100 + FEES_OPEN + FEES_CLOSE + 2 * SLIP
        if structure in ("single_budget", "single_otm_budget") and risk > budget:
            return "single option over budget"
        if structure == "single_350":       # the current $1K settings: $350 per trade, 4% friction gate
            if risk > 350:
                return "single option over $350"
            if ((long.ask - long.bid) * 100 + FEES_OPEN + FEES_CLOSE + 2 * SLIP) / (long.ask * 100) > 0.04:
                return "friction over 4%"
        out = rq.quote(day, sig["symbol"], t_out, long.occ)
        if out is None:
            return "no exit quote"
        pnl = (out.bid - long.ask) * 100 - FEES_OPEN - FEES_CLOSE - 2 * SLIP
        return {"risk": round(risk, 2), "pnl": round(pnl, 2), "gross_mid": round((out.mid - long.mid) * 100, 2),
                "costs": round(((long.ask - long.mid) + (out.mid - out.bid)) * 100 + FEES_OPEN + FEES_CLOSE + 2 * SLIP, 2),
                "legs": [long.occ], "delta": round(long.delta, 2), "premium": round(long.ask * 100, 2)}
    if structure in ("debit_spread", "debit_spread_costcap"):
        pick = select_spread(chain, long, budget)
        if pick is None:
            return "no spread fits the budget"
        short, risk = pick
        filled = rq.quote(day, sig["symbol"], t_in, short.occ)
        short.bid, short.ask = filled.bid, filled.ask
        risk = (long.ask - short.bid) * 100 + 2 * (FEES_OPEN + FEES_CLOSE) + 4 * SLIP
        if risk > budget:
            return "no spread fits the budget"
        if structure == "debit_spread_costcap":
            round_trip = ((long.ask - long.bid) + (short.ask - short.bid)) * 100 + 2 * (FEES_OPEN + FEES_CLOSE) + 4 * SLIP
            if round_trip > PARAMS["costcap"] * risk:
                return "spread costs over 20% of the risk"
        lo, so = rq.quote(day, sig["symbol"], t_out, long.occ), rq.quote(day, sig["symbol"], t_out, short.occ)
        if lo is None or so is None:
            return "no exit quote"
        pnl = ((lo.bid - so.ask) - (long.ask - short.bid)) * 100 - 2 * (FEES_OPEN + FEES_CLOSE) - 4 * SLIP
        mid_in, mid_out = long.mid - short.mid, lo.mid - so.mid
        return {"risk": round(risk, 2), "pnl": round(pnl, 2), "gross_mid": round((mid_out - mid_in) * 100, 2),
                "costs": round(((long.ask - short.bid) - mid_in + mid_out - (lo.bid - so.ask)) * 100 + 2 * (FEES_OPEN + FEES_CLOSE) + 4 * SLIP, 2),
                "legs": [long.occ, short.occ], "width": abs(short.strike - long.strike), "debit": round((long.ask - short.bid) * 100, 2)}
    raise ValueError(structure)


def account(signals, structure, start):
    """$1K account under the Phase 4 limits: 1 open position, <= 3 entries/day, stop for the day after 2 losses in a
    row or a 10% daily loss, stop for the week after a 20% weekly loss, risk <= 10% of equity."""
    equity = start; peak = start; maxdd = 0.0; trades = []; skips = defaultdict(int)
    open_until = None
    day_state = {}
    for sig in sorted(signals, key=lambda s: s["entry_time"]):
        t_in = ts(sig["entry_time"]); d = t_in.astimezone(ET).date(); wk = d.isocalendar()[:2]
        ds = day_state.setdefault(d, {"entries": 0, "streak": 0, "start": equity, "pnl": 0.0})
        ws = day_state.setdefault(wk, {"start": equity, "pnl": 0.0})
        if open_until and t_in < open_until:
            skips["position already open"] += 1; continue
        if ds["entries"] >= 3: skips["3 entries today"] += 1; continue
        if ds["streak"] >= 2: skips["2 losses in a row today"] += 1; continue
        if -ds["pnl"] >= 0.10 * ds["start"]: skips["daily loss limit"] += 1; continue
        if -ws["pnl"] >= 0.20 * ws["start"]: skips["weekly loss limit"] += 1; continue
        res = price(sig, structure, equity)
        if isinstance(res, str):
            skips[res] += 1; continue
        if res["risk"] > (350.0 if structure == "single_350" else PARAMS["budget"] * equity):
            skips["over the per-trade risk limit"] += 1; continue
        ds["entries"] += 1
        equity += res["pnl"]; ds["pnl"] += res["pnl"]; ws["pnl"] += res["pnl"]
        ds["streak"] = ds["streak"] + 1 if res["pnl"] <= 0 else 0
        peak = max(peak, equity); maxdd = max(maxdd, (peak - equity) / peak)
        open_until = ts(sig["exit_time"])
        trades.append({**{k: sig[k] for k in ("symbol", "side", "entry_time", "exit_time", "exit_reason")}, **res,
                       "r_underlying": sig.get("r")})
    return trades, dict(skips), equity, maxdd


def metrics(trades, start):
    if not trades:
        return {"n": 0}
    p = [t["pnl"] for t in trades]; w = [x for x in p if x > 0]; l = [x for x in p if x <= 0]
    top2 = sorted(p, reverse=True)[2:]
    return {"n": len(p), "win%": round(100 * len(w) / len(p)), "net": round(sum(p), 2),
            "PF": round(sum(w) / -sum(l), 2) if l and sum(l) else None, "exp$": round(sum(p) / len(p), 2),
            "exp%risk": round(100 * st.mean(t["pnl"] / t["risk"] for t in trades), 1),
            "avg_win": round(st.mean(w), 2) if w else 0, "avg_loss": round(st.mean(l), 2) if l else 0,
            "net_without_best2": round(sum(top2), 2), "gross_mid": round(sum(t["gross_mid"] for t in trades), 2),
            "costs": round(sum(t["costs"] for t in trades), 2), "avg_risk": round(st.mean(t["risk"] for t in trades), 2)}


def run_all(signals, start, structures=("single", "single_budget", "debit_spread"), quiet=False):
    results = {}
    for structure in structures:
        raw = []
        for sig in signals:
            r = price(sig, structure if structure != "single_budget" else "single", 10_000 if structure == "single" else start)
            if isinstance(r, dict):
                raw.append({**sig, **r})
        trades, skips, equity, maxdd = account(signals, structure, start)
        results[structure] = {"raw_signals": metrics(raw, start), "account": metrics(trades, start), "end_equity": round(equity, 2),
                              "max_dd%": round(100 * maxdd, 1), "skips": skips, "trades": trades, "raw": raw}
        if not quiet:
            print(structure, "| raw:", results[structure]["raw_signals"], "| account:", results[structure]["account"],
                  "end", round(equity, 2), "maxDD%", round(100 * maxdd, 1), "| skips:", skips)
    return results


if __name__ == "__main__":
    signals = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    START = float(sys.argv[3]) if len(sys.argv) > 3 else 1000.0
    rq = RealQuotes(ROOT, fill=sys.argv[4] if len(sys.argv) > 4 else "adverse")
    results = run_all(signals, START)
    Path(sys.argv[2]).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
