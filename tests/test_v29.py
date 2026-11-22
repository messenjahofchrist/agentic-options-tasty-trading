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


# --- the scan compares tastytrade with tastytrade (2026-11-15: a Massive baseline made every stock look quiet) -------
_SCAN = {"opening_minutes": 15, "lookback_sessions": 20, "min_relative_volume": 2.0, "max_symbols": 2, "min_price": 10,
         "scan_at_et": "09:45"}


def _stored(day, **over):
    base = {"source": in_play.SOURCE, "for_sessions_after": (day - timedelta(days=1)).isoformat(), "opening_minutes": 15,
            "lookback_sessions": 20, "built_at_utc": "x", "symbols": {"AAA": {"avg_open_volume": 100.0, "sessions": 20}}}
    base.update(over)
    return base


def test_only_a_fresh_tastytrade_baseline_is_accepted():
    day = date(2026, 11, 16)
    assert in_play.baseline_problem(_stored(day), day, _SCAN) is None
    assert in_play.baseline_problem(_stored(day, for_sessions_after="2026-11-13"), day, _SCAN) is None   # built Friday
    assert in_play.baseline_problem(None, day, _SCAN) == "missing"
    assert "not from tastytrade" in in_play.baseline_problem(_stored(day, source="massive"), day, _SCAN)
    old = _stored(day)
    del old["source"]                                                   # the file as written before the fix
    assert "not from tastytrade" in in_play.baseline_problem(old, day, _SCAN)
    assert in_play.baseline_problem(_stored(day, for_sessions_after="2026-11-09"), day, _SCAN) == "older than 5 days"
    assert in_play.baseline_problem(_stored(day, for_sessions_after="2026-11-16"), day, _SCAN) == "not for this session"
    assert in_play.baseline_problem(_stored(day, opening_minutes=30), day, _SCAN) == "built for other scan rules"


def test_whole_window_candles_count_only_the_0930_candle():
    day = date(2026, 11, 12)
    rows = _rows(day, [700]) + _rows(day, [55], start_minute=15) + _rows(day, [9], start_minute=-15)
    got = in_play.opening_from_period_rows(rows, 15)[day]
    assert got["volume"] == 700 and got["bars"] == 15


def _scan(tmp_path, monkeypatch, stored, rebuilt):
    day = date(2026, 11, 16)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies/universe.json").write_text(json.dumps({"symbols": ["AAA", "BBB"]}), encoding="utf-8")
    if stored is not None:
        in_play.write_json(tmp_path / in_play.BASELINE_FILE, stored)
    calls = []
    monkeypatch.setattr(in_play, "_quote_token", lambda cfg: {"dxlink-url": "u", "token": "t"})

    def rebuild(token, symbols, for_day, cfg):
        calls.append(for_day)
        if isinstance(rebuilt, Exception):
            raise rebuilt
        return rebuilt

    async def opening(token, symbols, d, minutes):
        return {"AAA": {"volume": 300.0, "bars": 15, "last": 20.0}, "BBB": {"volume": 300.0, "bars": 15, "last": 20.0}}, {}

    monkeypatch.setattr(in_play.time_mod, "sleep", lambda seconds: None)     # never wait for the real 09:45
    monkeypatch.setattr(in_play, "build_baseline_tastytrade", rebuild)
    monkeypatch.setattr(in_play, "_fetch_opening", opening)
    v2data = {"in_play": {**_SCAN, "enabled": True, "universe_file": "strategies/universe.json"}}
    record = in_play.todays_selection(tmp_path, None, v2data, now=datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc),
                                      log=lambda t: None)
    return record, calls, day


def test_scan_rebuilds_a_massive_baseline_from_tastytrade(tmp_path, monkeypatch):
    day = date(2026, 11, 16)
    fresh = _stored(day, symbols={s: {"avg_open_volume": 100.0, "sessions": 20} for s in ("AAA", "BBB")})
    record, calls, day = _scan(tmp_path, monkeypatch, _stored(day, source="massive"), fresh)
    assert calls == [day] and record["status"] == "ok" and record["baseline_source"] == in_play.SOURCE
    assert "not from tastytrade" in record["baseline_rebuilt_because"]
    assert [r["symbol"] for r in record["selected"]] == ["AAA", "BBB"]
    assert in_play.load_json(tmp_path / in_play.BASELINE_FILE)["source"] == in_play.SOURCE     # stored for the other engine


def test_scan_uses_a_good_stored_baseline_without_rebuilding(tmp_path, monkeypatch):
    record, calls, _ = _scan(tmp_path, monkeypatch, _stored(date(2026, 11, 16)), RuntimeError("must not be called"))
    assert calls == [] and record["status"] == "ok" and [r["symbol"] for r in record["selected"]] == ["AAA"]


def test_scan_fails_loudly_when_the_baseline_cannot_be_rebuilt(tmp_path, monkeypatch):
    record, calls, _ = _scan(tmp_path, monkeypatch, None, RuntimeError("feed down"))
    assert record["status"] == "failed" and record["selected"] == [] and "feed down" in record["error"]


# --- LIVE 2.8 vs paper page (owner 2026-11-17) ----------------------------------------------------------------------
def test_live_compare_pairs_a_live_fill_with_its_paper_twin(tmp_path):
    from trader.v2 import live_compare
    ct = ZoneInfo("America/Chicago")

    def ledger(close_type, entry_price, exit_price, pnl, minute):
        t_in = datetime(2026, 11, 17, 10, minute, tzinfo=ct).isoformat()
        t_out = datetime(2026, 11, 17, 14, 59, tzinfo=ct).isoformat()
        occ = "NFLX  261127P00062000"
        return {"starting_capital": 1000.0, "cash": 1000.0, "positions": [], "ledger": [
            {"type": "fill", "side": "BUY", "trade_id": "t1", "symbol": occ, "quantity": 1, "fill_price": entry_price, "timestamp": t_in},
            {"type": "fill", "side": "SELL", "trade_id": "s1", "symbol": occ, "quantity": 1, "fill_price": exit_price,
             "timestamp": t_out, "reason": "end_of_session_close"},
            {"type": close_type, "trade_id": "t1", "underlying": "NFLX", "quantity": 1, "realized_pnl": pnl,
             "timestamp": t_out, "reason": "end_of_session_close"}]}
    (tmp_path / "data/live").mkdir(parents=True)
    (tmp_path / "data/profiles/small_account_paper").mkdir(parents=True)
    (tmp_path / "data/live/live_account.json").write_text(json.dumps(ledger("live_close", 1.75, 2.00, 23.5, 7)), encoding="utf-8")
    (tmp_path / "data/profiles/small_account_paper/paper_account.json").write_text(
        json.dumps(ledger("paper_close", 1.72, 2.03, 29.71, 13)), encoding="utf-8")
    page = live_compare.page_html(tmp_path, tmp_path / "data", datetime(2026, 11, 17, 15, 30, tzinfo=ct))
    assert "LIVE 2.8 vs paper" in page and "REAL MONEY" in page
    assert "real fill $+0.03 vs paper on the way in" in page and "$-0.03 on the way out" in page
    assert "LIVE did not buy" not in page                       # the two entries were 6 minutes apart: one signal
    views = {v["key"]: v for v in (live_compare.account_view(s, date(2026, 11, 17))
                                   for s in live_compare.accounts(tmp_path, tmp_path / "data"))}
    assert views["live"]["realized"] == 23.5 and views["p28_1k"]["realized"] == 29.71
