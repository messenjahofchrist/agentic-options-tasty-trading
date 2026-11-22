"""2.8.2 two-year replay (2026-11-19): 2.8.2 with all new entry rules vs the same config with them off (= 2.8.1),
and each rule on its own. Same window as the 11/11 two-year replay (2024-12-01..2026-11-10). Run after the close:
    .venv\Scripts\python.exe reports\new_rules_20261119\run_replay.py
"""
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from trader.config import AppConfig  # noqa: E402
from trader.v2.replay import run_long_replay  # noqa: E402

VARIANTS = ["v2", "v282_all_off", "v282_only_ruler", "v282_only_pullback", "v282_only_room", "v282_no_prior_extreme",
            "v281_tighten_2r", "v282_tighten_2r"]


def log(message):
    print(datetime.now().strftime("%H:%M:%S"), message, flush=True)


SMOKE = "--smoke" in sys.argv    # two days, to catch a crash before the long run
summary = run_long_replay(ROOT, AppConfig.from_env(), start=date(2026, 11, 3) if SMOKE else date(2024, 12, 1),
                          end=date(2026, 11, 4) if SMOKE else date(2026, 11, 10), warmup_days=35,
                          out=ROOT / "reports" / "new_rules_20261119" / ("smoke" if SMOKE else "long_replay"), variants=VARIANTS, log=log,
                          config_path="strategies/v2_versions/professional_v2_2.8.2.json")
for name, v in summary["variants"].items():
    net = v["net_of_option_cost"]
    log(f"{name}: {v['all'].get('trades')} trades, gross {v['all'].get('expectancy_r')}R/trade PF {v['all'].get('profit_factor')}, "
        f"net {net.get('net_expectancy_r')}R/trade total {net.get('net_total_r')}R")
    for q, row in v["by_quarter"].items():
        log(f"   {q} {row['gross'].get('trades')} gross {row['gross'].get('total_r')} net {row['net'].get('net_total_r')}")
log(summary["files"])
