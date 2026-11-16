# Harness check: reprice every actual current-run trade (same contract, same entry/exit minute) from the recorded
# real quotes and compare with the ledger. Also writes the actual trades as signals for evaluate.py.
import json, statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pricing import RealQuotes, FEES_OPEN, FEES_CLOSE, SLIP

ET = ZoneInfo("America/New_York")
ROOT = Path(r"C:\development\optiobot\agentic-options-tasty-trading")
P1 = Path(r"C:\Users\messe\AppData\Local\Temp\claude\C--development-optiobot\da318926-28df-4e0e-a8f1-60cfc6634e82\scratchpad\p1\out\enriched.json")
OUT = Path(__file__).parent
T = [t for t in json.loads(P1.read_text(encoding="utf-8")) if t["run"] == "current"]
import sys
rq = RealQuotes(ROOT, fill=sys.argv[1] if len(sys.argv) > 1 else "open")
rows = []
for t in T:
    t_in = datetime.fromisoformat(t["entry_utc"]); t_out = datetime.fromisoformat(t["exit_utc"])
    day = t_in.astimezone(ET).date()
    if not rq.has(day, t["underlying"]):
        continue
    occ = t["option"]
    qi = rq.quote(day, t["underlying"], t_in, occ); qo = rq.quote(day, t["underlying"], t_out, occ)
    if qi is None or qo is None:
        rows.append((t, None)); continue
    q = t["qty"]
    pnl = (qo.bid - qi.ask) * 100 * q - FEES_OPEN * q - FEES_CLOSE * q - 2 * SLIP * q
    rows.append((t, round(pnl, 2)))
ok = [(t, p) for t, p in rows if p is not None]
diff = [p - t["pnl"] for t, p in ok]
print("repriced", len(ok), "of", len(rows), "with quotes on disk; missing quotes", sum(p is None for _, p in rows))
if ok:
    print("sum ledger", round(sum(t["pnl"] for t, _ in ok), 2), "sum repriced", round(sum(p for _, p in ok), 2),
          "| per-trade diff mean", round(st.mean(diff), 2), "median", round(st.median(diff), 2), "max abs", round(max(map(abs, diff)), 2))
    for t, p in sorted(ok, key=lambda x: -abs(x[1] - x[0]["pnl"]))[:6]:
        print("  ", t["ledger"], t["underlying"], t["entry_et"], "ledger", t["pnl"], "repriced", p)
signals = [{"symbol": t["underlying"], "side": t["side"], "entry_time": t["entry_utc"], "exit_time": t["exit_utc"],
            "exit_reason": t["reason"], "r": t.get("exit_underlying_move_r"), "ledger": t["ledger"]} for t in T]
(OUT / "signals_actual.json").write_text(json.dumps(signals, indent=1), encoding="utf-8")
