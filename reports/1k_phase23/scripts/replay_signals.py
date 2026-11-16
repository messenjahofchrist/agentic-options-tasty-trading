# Phase 2: 2.5.0 replay signals (underlying exits) for one source, read-only. usage: replay_signals.py <massive_cache|tastytrade> <out>
import sys
from pathlib import Path
import trader.v2.replay as r
from trader.config import AppConfig, load_dotenv
load_dotenv(); cfg = AppConfig.from_env()
s = r.run_replay(Path(".").resolve(), cfg, source=sys.argv[1], in_sample_end="2026-08-31", out=Path(sys.argv[2]), variants=["v23_hold_run_wide"])
print(sys.argv[1], s.get("days"), s.get("first_day"), s.get("last_day"), s.get("notes"))
