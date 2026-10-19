"""End-of-day summary: every contract bought today with its exit, the day's opening and closing cash, and an
urgent push when anything is still open after the close. Nothing leaves the process."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trader import profile_eod
from trader.account_profiles import ProfileConfig, ProfileLedger
from trader.portfolio import save_account
from tests.test_profiles import make_root

DAY = date(2026, 10, 19)
AFTER_CLOSE = datetime(2026, 10, 19, 20, 20, tzinfo=timezone.utc)


def fill(side, trade_id, symbol, price, ts, underlying=None):
    return {"type": "fill", "side": side, "trade_id": trade_id, "symbol": symbol, "underlying": underlying,
            "fill_price": price, "quantity": 1, "fees": 0.70, "timestamp": ts}


class EodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        ledger = ProfileLedger(ProfileConfig.load(self.root, "profiles/small_account.json"),
                               original_data_dir=self.root / "data")
        ledger.initialize()
        self.account_path = ledger.account_path
        self.account = ledger.account()
        iwm = "IWM   261028P00272000"
        self.account.ledger = [
            fill("BUY", "old", "SPY   261023C00500000", 2.00, "2026-10-16T15:00:00+00:00", "SPY"),
            fill("SELL", "old-x", "SPY   261023C00500000", 2.10, "2026-10-16T16:00:00+00:00"),
            {"type": "paper_close", "trade_id": "old", "underlying": "SPY", "realized_pnl": 8.60,
             "reason": "underlying_profit_target", "timestamp": "2026-10-16T16:00:00+00:00"},
            fill("BUY", "t1", iwm, 3.7902, "2026-10-19T13:55:00+00:00", "IWM"),
            fill("SELL", "t1-x", iwm, 3.3398, "2026-10-19T14:16:00+00:00"),
            {"type": "paper_close", "trade_id": "t1", "underlying": "IWM", "realized_pnl": -46.44,
             "actual_r_multiple": -0.12, "reason": "underlying_invalidation_stop", "timestamp": "2026-10-19T14:16:00+00:00"},
        ]
        self.account.cash = 1000 + 8.60 - 46.44
        self.account.realized_pnl = 8.60 - 46.44
        save_account(self.account_path, self.account)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_todays_contracts_and_reconstructs_the_days_starting_cash(self):
        eod = profile_eod.build_eod(self.root, "profiles/small_account.json", day=DAY, now=AFTER_CLOSE)
        [iwm] = eod["contracts"]
        self.assertEqual((iwm["underlying"], iwm["contract"], iwm["right"]), ("IWM", "Oct 28 $272.00 put", "put"))
        self.assertEqual((iwm["sell_price"], iwm["pnl"], iwm["exit_reason"]), (3.3398, -46.44, "underlying_invalidation_stop"))
        self.assertAlmostEqual(iwm["cost"], 379.72, places=2)
        led = eod["ledger"]
        self.assertAlmostEqual(led["day_start_cash"], 1008.60, places=2)
        self.assertAlmostEqual(led["day_end_cash"], 962.16, places=2)
        self.assertEqual((led["day_realized_pnl"], led["trades_closed"], led["losses"]), (-46.44, 1, 1))
        self.assertEqual(led["week_realized_pnl"], -46.44)  # Friday's trade belongs to last week
        self.assertFalse(eod["overnight_violation"])
        self.assertIn("IWM Oct 28 $272.00 put", profile_eod.render_eod_text(eod))

    def test_a_position_open_after_the_close_is_flagged_and_pushed_urgently(self):
        nke = "NKE   261030P00037000"
        self.account.ledger.append(fill("BUY", "t2", nke, 0.98, "2026-10-19T14:38:00+00:00", "NKE"))
        self.account.positions = [{"trade_id": "t2", "symbol": nke, "underlying": "NKE", "quantity": 1,
                                   "entry_price": 0.98, "entry_time": "2026-10-19T14:38:00+00:00", "last_bid": 0.97}]
        save_account(self.account_path, self.account)
        pushes, emails = [], []
        with patch.object(profile_eod, "email_configured", return_value=True), \
                patch.object(profile_eod, "send_email", side_effect=lambda s, b: emails.append((s, b))), \
                patch.object(profile_eod, "push_configured", return_value=True), \
                patch.object(profile_eod, "send_push", side_effect=lambda t, m, **k: pushes.append((t, m, k))):
            result = profile_eod.run_eod(self.root, "profiles/small_account.json", now=AFTER_CLOSE)
        self.assertTrue(result["overnight_violation"])
        [(title, body, options)] = pushes
        self.assertEqual(options["priority"], "urgent")
        self.assertIn("1 OPEN OVERNIGHT", body)
        self.assertIn("STILL OPEN", emails[0][1])
        self.assertTrue(Path(result["html"]).is_file())


if __name__ == "__main__":
    unittest.main()
