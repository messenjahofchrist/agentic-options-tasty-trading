# Read-only replay of the 2026-11-10 rule ideas on top of the active 2.5.0 config.
# usage: replay_new_rules.py <massive_cache|tastytrade> <out>
import sys
from pathlib import Path
import trader.v2.replay as r
from trader.config import AppConfig, load_dotenv
RE = {"failed_level_reentry": {"enabled": True, "lookback_minutes": 60}}
TT = {"time_to_target": {"enabled": True, "reach_multiple": 1.0}}
TR = {"let_winners_run": {"lock_r": 1.0, "trail_r": 1.0}}
r.VARIANTS.update({
    "base_250": ("v2", True, {}),
    "reentry60": ("v2", True, RE),
    "reentry30": ("v2", True, {"failed_level_reentry": {"enabled": True, "lookback_minutes": 30}}),
    "reentry120": ("v2", True, {"failed_level_reentry": {"enabled": True, "lookback_minutes": 120}}),
    "ttt100": ("v2", True, TT),
    "ttt075": ("v2", True, {"time_to_target": {"enabled": True, "reach_multiple": 0.75}}),
    "ttt150": ("v2", True, {"time_to_target": {"enabled": True, "reach_multiple": 1.5}}),
    "trail100": ("v2", True, TR),
    "all3": ("v2", True, {**RE, **TT, **TR}),
    "re_ttt": ("v2", True, {**RE, **TT}),
})
load_dotenv(); cfg = AppConfig.from_env()
names = ["base_250", "reentry30", "reentry60", "reentry120", "ttt075", "ttt100", "ttt150", "trail100", "re_ttt", "all3"]
s = r.run_replay(Path(".").resolve(), cfg, source=sys.argv[1], in_sample_end="2026-08-31", out=Path(sys.argv[2]), variants=names)
print(sys.argv[1], s.get("days"), s.get("first_day"), s.get("last_day"))
for n in names:
    v = s["variants"][n]
    print(n, {k: (v[k].get("trades"), v[k].get("profit_factor"), v[k].get("total_r")) for k in ("in_sample", "out_of_sample")})
