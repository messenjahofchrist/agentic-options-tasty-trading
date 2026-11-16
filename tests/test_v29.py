"""V2 2.9.0 side test (owner 2026-11-12): stop policy, stocks in play, side config/manifest, pages."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.v2 import in_play, side_report
from trader.v2.config import (load_v2_config, side_manifest_path, stop_and_target, verify_v2_manifest)

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
SIDE = ROOT / "strategies/v2_versions/professional_v2_2.9.0.json"


def test_stop_policy_absent_changes_nothing():
    assert stop_and_target({}, 100.0, 0.5, 1.0) == (0.5, 1.0)
    assert stop_and_target({"stop_policy": {"enabled": False, "min_stop_fraction": 0.01, "min_target_multiple": 2}},
                           100.0, 0.5, 1.0) == (0.5, 1.0)


def test_stop_policy_widens_stop_and_target():
    cfg = {"stop_policy": {"enabled": True, "min_stop_fraction": 0.01, "min_target_multiple": 2.0}}
    assert stop_and_target(cfg, 100.0, 0.5, 1.0) == (1.0, 2.0)
    assert stop_and_target(cfg, 100.0, 1.5, 4.0) == (1.5, 4.0)      # already wider: kept


def test_side_config_loads_and_active_is_untouched():
    side = load_v2_config(ROOT, SIDE)
    assert side.version == "2.9.0"
    assert side.data["in_play"]["enabled"] and side.data["stop_policy"]["enabled"]
    assert side.data["contract_policy"] == {"enabled": True, "min_dte": 1, "max_dte": 6, "nearest_expiry_first": True}
    assert side.data["management"]["breakeven"]["enabled"] is False
    verify_v2_manifest(ROOT, side, side_manifest_path(SIDE))
    active = load_v2_config(ROOT)
    assert active.version != "2.9.0" and "in_play" not in active.data and "stop_policy" not in active.data
    verify_v2_manifest(ROOT, active)


def _rows(day, volumes, start_minute=0):
    base = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    return [{"timestamp": (base + timedelta(minutes=start_minute + i)).astimezone(timezone.utc).isoformat(),
             "close": 50.0 + i, "volume": v} for i, v in enumerate(volumes)]


def test_opening_window_counts_only_the_first_minutes():
    day = date(2026, 11, 12)
    rows = _rows(day, [10] * 20) + _rows(day, [999], start_minute=-5)   # 09:25 premarket bar ignored
    got = in_play.opening_from_rows(rows, 15)[day]
    assert got["volume"] == 150 and got["bars"] == 15 and got["last"] == 64.0


def test_rank_picks_unusual_volume_only():
    cfg = {"opening_minutes": 15, "lookback_sessions": 20, "min_relative_volume": 2.0, "max_symbols": 2, "min_price": 10}
    today = {"AAA": {"volume": 300, "bars": 15, "last": 20}, "BBB": {"volume": 500, "bars": 15, "last": 20},
             "CCC": {"volume": 150, "bars": 15, "last": 20}, "DDD": {"volume": 900, "bars": 15, "last": 5},
             "EEE": {"volume": 990, "bars": 15, "last": 20}, "FFF": {"volume": 900, "bars": 3, "last": 20}}
    base = {s: {"avg_open_volume": 100, "sessions": 20} for s in today}
    base["EEE"]["sessions"] = 4                       # too little history
    out = in_play.rank(today, base, cfg)
    assert [r["symbol"] for r in out["selected"]] == ["BBB", "AAA"]     # DDD under $10, CCC 1.5x, EEE/FFF incomplete


def test_side_pages_render_with_no_ledgers(tmp_path):
    out = side_report.write_all(tmp_path, tmp_path / "data", now=datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc))
    page = Path(out["v29_dashboard.html"]).read_text(encoding="utf-8")
    assert "OptioBot 2.9" in page and "Not scanned yet" in page
    duel = Path(out["compare_28_vs_29.html"]).read_text(encoding="utf-8")
    assert "No trades yet" in duel


def test_crown():
    assert side_report.crown(10, 5, 1, 1) == "2.8"
    assert side_report.crown(-10, 5, 1, 1) == "2.9"
    assert side_report.crown(0, 0, 0, 0) == "none"
    assert side_report.crown(3, 3, 1, 1) == "tie"


def test_side_session_refuses_the_active_config(monkeypatch):
    from trader.v2 import side
    from trader.config import AppConfig
    with pytest.raises(Exception):
        side.run_side_session(root=ROOT, cfg=AppConfig.from_env(), ledger="10k", config_path="strategies/professional_v2.json")


def test_v29_profile_files_point_to_their_own_ledgers():
    from trader.account_profiles import ProfileConfig
    small = ProfileConfig.load(ROOT, "profiles/v29_small_account.json")
    assert small.data_dir == ROOT / "data/v29/small_account_paper"
    assert "AVGO" in small.small_universe and small.small_per_trade_cap_dollars == 350
    big = json.loads((ROOT / "profiles/v29_original_account.json").read_text(encoding="utf-8"))
    assert big["data_dir"] == "data/v29/original_paper" and big["starting_balance"] == 10000
