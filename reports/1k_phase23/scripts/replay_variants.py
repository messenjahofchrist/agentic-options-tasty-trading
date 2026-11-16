# Phase 3 exit variants on 2.5.0 signals (read-only): no-progress time stop 45/60/90 min, and fixed target vs let winners run.
# usage: replay_variants.py <massive_cache|tastytrade> <out>
import sys
from pathlib import Path
import trader.v2.replay as r
from trader.config import AppConfig, load_dotenv
HOLD = {"trailing": {"enabled": False}, "let_winners_run": {"lock_r": 1.0, "trail_r": 1.5}}
for n in (45, 60, 90):
    r.VARIANTS[f"time{n}"] = ("v2", True, {**HOLD, "time_stop": {"enabled": True, "after_minutes": n, "min_progress_r": 0.25}})
r.VARIANTS["fixed_target"] = ("v2", True, {"trailing": {"enabled": False}, "let_winners_run": {"enabled": False}})
load_dotenv(); cfg = AppConfig.from_env()
s = r.run_replay(Path(".").resolve(), cfg, source=sys.argv[1], in_sample_end="2026-08-31", out=Path(sys.argv[2]),
                 variants=["v23_hold_run_wide", "time45", "time60", "time90", "fixed_target"])
print(sys.argv[1], s.get("days"), s.get("first_day"), s.get("last_day"))
