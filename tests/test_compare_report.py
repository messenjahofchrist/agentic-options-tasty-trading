"""Side-by-side page: both PAPER ledgers summarized from their own files, compared by percent and by day,
and the original ledger's end-of-day summary built from the original data directory. Nothing leaves the process."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from trader.account_profiles import ProfileConfig, ProfileLedger
from trader.compare_report import build_comparison, render_comparison_html
from trader.portfolio import PaperAccount, save_account
from trader.profile_eod import build_eod
from tests.test_profiles import make_root

NOW = datetime(2026, 10, 21, 20, 30, tzinfo=timezone.utc)
PROFILE = "profiles/small_account.json"


def trade(trade_id, symbol, underlying, buy, sell, pnl, day):
    return [{"type": "fill", "side": "BUY", "trade_id": trade_id, "symbol": symbol, "underlying": underlying,
             "fill_price": buy, "quantity": 1, "fees": 1.12, "timestamp": f"{day}T14:00:00+00:00"},
            {"type": "fill", "side": "SELL", "trade_id": trade_id + "-x", "symbol": symbol, "fill_price": sell,
             "quantity": 1, "fees": 0.13, "timestamp": f"{day}T16:00:00+00:00"},
            {"type": "paper_close", "trade_id": trade_id, "underlying": underlying, "realized_pnl": pnl,
             "reason": "underlying_profit_target", "timestamp": f"{day}T16:00:00+00:00"}]


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        self.original = self.root / "data"
        big = PaperAccount(10_000, 10_000 + 198.75 - 61.25)
        big.ledger = (trade("a", "QQQ   261106C00714000", "QQQ", 8.99, 10.98, 198.75, "2026-10-21")
                      + trade("b", "SPY   261030P00773000", "SPY", 7.43, 6.82, -61.25, "2026-10-20"))
        big.realized_pnl = 137.50
        save_account(self.original / "paper_account.json", big)
        ledger = ProfileLedger(ProfileConfig.load(self.root, PROFILE), original_data_dir=self.original)
        ledger.initialize()
        small = ledger.account()
        small.ledger = trade("c", "T     261030P00025000", "T", 0.62, 0.51, -11.25, "2026-10-21")
        small.cash, small.realized_pnl = 1000 - 11.25, -11.25
        small.positions = [{"trade_id": "d", "symbol": "XLF   261106C00056000", "underlying": "XLF", "quantity": 1,
                            "entry_price": 0.79, "entry_fees": 1.12, "last_bid": 0.70, "modeled_risk_dollars": 20,
                            "stop_underlying": 55.54, "target_underlying": 56.33,
                            "entry_time": "2026-10-21T15:00:00+00:00", "max_holding_minutes": None}]
        small.cash -= 80.14
        save_account(ledger.account_path, small)

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_ledgers_compare_by_percent_and_line_up_by_day(self):
        report = build_comparison(self.root, self.original, PROFILE, now=NOW)
        big, small = report["ledgers"]
        self.assertEqual((big["label"], small["label"]), ("$10K account", "$1K account"))
        self.assertEqual((big["change_pct"], big["today_realized"], big["win_rate"]), (1.38, 198.75, 50))
        self.assertEqual(small["equity"], round(1000 - 11.25 - 80.14 + 70, 2))
        self.assertEqual(report["days"], ["2026-10-20", "2026-10-21"])
        self.assertEqual(small["by_day"], {"2026-10-21": -11.25})
        page = render_comparison_html(report)
        self.assertIn("$10K vs $1K", page)
        self.assertIn("XLF", page)
        self.assertNotIn("http://", page.replace("http-equiv", ""))

    def test_original_ledger_end_of_day_reads_the_original_data_directory(self):
        eod = build_eod(self.root, "original", day=date(2026, 10, 21), now=NOW, original_data_dir=self.original)
        self.assertEqual((eod["ledger_id"], eod["label"]), ("original_paper", "$10K account"))
        [qqq] = eod["contracts"]
        self.assertEqual((qqq["underlying"], qqq["pnl"]), ("QQQ", 198.75))
        self.assertEqual(eod["ledger"]["day_realized_pnl"], 198.75)


if __name__ == "__main__":
    unittest.main()
