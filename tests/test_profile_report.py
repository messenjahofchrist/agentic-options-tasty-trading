"""Small-account HTML report: read-only, built from the profile ledger files, counts rotated decision logs."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trader.account_profiles import ProfileConfig, ProfileLedger
from trader.portfolio import save_account
from trader.profile_report import build_report, render_html, write_report
from tests.test_profiles import make_root

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
PROFILE = "profiles/small_account.json"


def decision(ts: str, decision: str, signal: str | None, reason: str) -> str:
    return json.dumps({"decision": decision, "diagnostics": {"underlying_signal": signal}, "reason": reason,
                       "timestamp": ts}, sort_keys=True)


class ProfileReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        self.original = self.root / "data"
        self.ledger = ProfileLedger(ProfileConfig.load(self.root, PROFILE), original_data_dir=self.original)
        self.ledger.initialize()
        account = self.ledger.account()
        buy = {"type": "fill", "side": "BUY", "trade_id": "t1", "symbol": "MARA  261023P00008000", "fill_price": 0.41,
               "timestamp": "2026-10-16T15:52:00+00:00"}
        sell = {"type": "fill", "side": "SELL", "trade_id": "x9", "symbol": "MARA  261023P00008000", "fill_price": 0.56,
                "timestamp": "2026-10-16T17:19:00+00:00"}
        close = {"type": "paper_close", "trade_id": "t1", "underlying": "MARA", "realized_pnl": 13.56,
                 "actual_r_multiple": 0.32, "reason": "underlying_profit_target", "timestamp": "2026-10-16T17:19:00+00:00"}
        account.ledger = [buy, sell, close]
        account.realized_pnl = 13.56
        account.cash = 1000 - 42.44 + 13.56 + 42.44 - 70.44
        account.positions = [{"trade_id": "t2", "symbol": "XLF   261030P00055500", "underlying": "XLF", "quantity": 1,
                              "entry_price": 0.69, "entry_fees": 0.70, "last_bid": 0.74, "modeled_risk_dollars": 70.44,
                              "stop_underlying": 55.72, "target_underlying": 55.045,
                              "entry_time": "2026-10-16T16:34:00+00:00", "max_holding_minutes": 180}]
        save_account(self.ledger.account_path, account)
        data = self.ledger.data_dir
        (data / "decisions.jsonl.20261016T170519728264Z.42471f.archive").write_text("\n".join([
            decision("2026-10-16T15:52:00+00:00", "PAPER_ENTRY", "orb_down", "paper_risk_and_greek_economic_gate_passed"),
            decision("2026-10-16T15:53:00+00:00", "NO_TRADE", "orb_down", "option_filters_rejected_candidates"),
            decision("2026-10-15T15:53:00+00:00", "NO_TRADE", "orb_down", "yesterday")]) + "\n")
        (data / "decisions.jsonl").write_text("\n".join([
            decision("2026-10-16T16:34:00+00:00", "PAPER_ENTRY", "orb_down", "paper_risk_and_greek_economic_gate_passed"),
            decision("2026-10-16T16:35:00+00:00", "NO_TRADE", None, "no_signal")]) + "\n")
        (data / "critical_alerts.jsonl").write_text(json.dumps(
            {"event": "paper_symbol_disabled", "details": {"symbol": "PLTR"}, "timestamp_utc": "2026-10-16T15:25:26+00:00"}) + "\n")
        (data / "paper_session.log").write_text(
            "2026-10-16 15:25:26,354Z ERROR optio_bot.paper Disabling PLTR for the rest of the session: "
            "RuntimeError: DXLink candle snapshot is out of order at 2026-10-16T15:25:00+00:00\n")
        (data / "engine_heartbeat.json").write_text(json.dumps(
            {"status": "running", "updated_at_utc": "2026-10-16T17:59:30+00:00", "last_bar_at_utc": "2026-10-16T17:59:00+00:00"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_summarizes_positions_closed_trades_and_todays_scan_including_rotated_logs(self):
        report = build_report(self.root, self.original, PROFILE, now=NOW)
        self.assertEqual(report["engine"]["state"], "RUNNING")
        self.assertEqual(report["profile"]["signals_today"], 3)
        self.assertEqual(report["profile"]["rejected_today"], 1)
        [pos] = report["positions"]
        self.assertEqual((pos["option"], pos["mark_pnl"], pos["minutes_held"]), ("Oct 30 $55.50 put", 4.3, 86))
        [trade] = report["closed_trades"]
        self.assertEqual((trade["entry_price"], trade["exit_price"], trade["pnl"]), (0.41, 0.56, 13.56))
        self.assertEqual(report["disabled_today"], [{"symbol": "PLTR", "reason": "candle history out of order"}])
        self.assertEqual(report["alerts_today"], {"paper_symbol_disabled: PLTR": 1})
        page = render_html(report)
        self.assertIn('http-equiv="refresh"', page)
        self.assertIn("MARA", page)
        self.assertNotIn("http://", page.replace('http-equiv', ''))  # self-contained: no external resources

    def test_stale_heartbeat_reads_not_responding(self):
        (self.ledger.data_dir / "engine_heartbeat.json").write_text(json.dumps(
            {"status": "running", "updated_at_utc": "2026-10-16T17:50:00+00:00"}))
        self.assertEqual(build_report(self.root, self.original, PROFILE, now=NOW)["engine"]["state"], "NOT RESPONDING")

    def test_write_report_only_adds_the_two_report_files(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        path = write_report(self.root, self.original, PROFILE)
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(set(after) - set(before), {path, path.with_name("report.json")})
        self.assertTrue(all(after[p] == before[p] for p in before))


if __name__ == "__main__":
    unittest.main()
