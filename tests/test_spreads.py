"""CREDIT_SPREADS (PAPER) unit tests. All MOCKED / offline: a fake tastytrade client, no network."""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trader import spreads

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 2)                       # a Monday
SESSION = (datetime(2026, 11, 2, 9, 30, tzinfo=ET), datetime(2026, 11, 2, 16, 0, tzinfo=ET))


def at(hh: int, mm: int, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).astimezone(timezone.utc)


class FakeClient:
    """Option prices from a smooth curve: price(K) = 12 exp(-distance / 30), |delta| = price / 30, so the 0.20-delta
    put sits about 21 points below the price. `scale` multiplies every option price (to move a spread's value)."""

    def __init__(self, spot: float = 760.0, vix: float = 17.0):
        self.spot, self.vix, self.scale, self.now = spot, vix, 1.0, at(11, 0)
        self.expirations = [TODAY + timedelta(days=d) for d in (4, 18, 32, 39, 60)]
        self.chain = []
        for exp in self.expirations:
            for k in range(600, 921):
                for right in ("P", "C"):
                    self.chain.append({"active": True, "symbol": self.symbol(exp, right, k), "strike-price": f"{k}.0",
                                       "option-type": right, "expiration-date": exp.isoformat(),
                                       "days-to-expiration": (exp - TODAY).days})
        self.by_symbol = {c["symbol"]: c for c in self.chain}
        self.option_chain_calls = 0

    @staticmethod
    def symbol(exp: date, right: str, strike: int) -> str:
        return f"SPY   {exp:%y%m%d}{right}{strike * 1000:08d}"

    def option_chain(self, underlying):
        self.option_chain_calls += 1
        return {"data": {"items": self.chain}}

    def quotes(self, query):
        params = urllib.parse.parse_qs(query)
        stamp = self.now.isoformat().replace("+00:00", "Z")
        if "equity[]" in params:
            return {"data": {"items": [{"symbol": s, "mark": str(self.spot), "updated-at": stamp}
                                       for s in params["equity[]"][0].split(",")]}}
        if "index[]" in params:
            return {"data": {"items": [{"symbol": "VIX", "last": str(self.vix)}]}}
        items = []
        for sym in params["equity-option[]"][0].split(","):
            c = self.by_symbol[sym]
            k = float(c["strike-price"])
            distance = self.spot - k if c["option-type"] == "P" else k - self.spot
            price = 12 * math.exp(-distance / 30) * self.scale
            delta = (price / self.scale) / 30 * (-1 if c["option-type"] == "P" else 1)
            items.append({"symbol": sym, "bid": f"{max(price - 0.02, 0):.2f}", "ask": f"{price + 0.02:.2f}",
                          "delta": str(delta), "open-interest": 1000, "updated-at": stamp})
        return {"data": {"items": items}}


class SpreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "strategies").mkdir()
        shutil.copy(ROOT / spreads.CONFIG_FILE, self.tmp / spreads.CONFIG_FILE)
        self.data = self.tmp / "data"
        self.client = FakeClient()
        self.closes = [700.0] * 60                     # price 760 is above its 50-day average: put spreads

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit_config(self, change):
        file = self.tmp / spreads.CONFIG_FILE
        data = json.loads(spreads._strip_json_comments(file.read_text(encoding="utf-8")))
        change(data)
        file.write_text(json.dumps(data), encoding="utf-8")

    def run_at(self, when: datetime, session=SESSION, underlyings=("SPY",)):
        self.edit_config(lambda d: d.__setitem__("underlyings", list(underlyings)))
        self.client.now = when
        return spreads.run_once(self.tmp, self.data, self.client, now=when, session=session,
                                closes_fn=lambda symbols, today: {s: self.closes for s in symbols})

    def ledger(self, name):
        return json.loads((self.data / "credit_spreads" / f"{name}.json").read_text(encoding="utf-8"))

    def test_repo_config_and_saved_versions_are_valid(self):
        spreads.load_config(ROOT)
        versions = sorted((ROOT / "strategies" / "credit_spreads_versions").glob("credit_spreads_*.json"))
        self.assertTrue(versions)
        for path in versions:
            spreads.load_config(ROOT, str(path.relative_to(ROOT)))

    def test_config_fails_closed(self):
        self.edit_config(lambda d: d["entry"].__setitem__("surprise", 1))
        with self.assertRaisesRegex(spreads.SpreadConfigError, "Unknown entry keys"):
            spreads.load_config(self.tmp)
        self.edit_config(lambda d: (d["entry"].pop("surprise"), d["exits"].__setitem__("close_at_dte", 30)))
        with self.assertRaisesRegex(spreads.SpreadConfigError, "close_at_dte"):
            spreads.load_config(self.tmp)
        self.edit_config(lambda d: (d["exits"].__setitem__("close_at_dte", 21), d.__setitem__("mode", "LIVE")))
        with self.assertRaisesRegex(spreads.SpreadConfigError, "mode"):
            spreads.load_config(self.tmp)

    def test_uptrend_opens_a_put_spread_sized_to_each_ledger(self):
        summary = self.run_at(at(11, 0))
        self.assertEqual(len(summary["opened"]), 2)
        big, small = self.ledger("CREDIT_SPREADS_10K_PAPER"), self.ledger("CREDIT_SPREADS_1K_PAPER")
        for book, width, cap in ((big, 5, 500), (small, 2, 225)):
            [p] = book["open"]
            self.assertEqual(p["kind"], "PUT_CREDIT_SPREAD")
            self.assertEqual(p["expiration"], (TODAY + timedelta(days=32)).isoformat())   # nearest to 35 days
            self.assertAlmostEqual(abs(p["short_delta"]), 0.20, delta=0.01)
            self.assertEqual(p["short_strike"] - p["long_strike"], width)
            self.assertLessEqual(p["max_loss_dollars"], cap)
            self.assertGreaterEqual(p["credit"], 0.15 * width)
            # credit = short bid - protection ask; cash = start + credit x 100 - opening fees (2 legs x $0.70)
            self.assertAlmostEqual(book["cash"], book["starting_capital"] + p["credit"] * 100 - 1.40, places=6)

    def test_downtrend_sells_a_call_spread(self):
        self.closes = [820.0] * 60
        self.run_at(at(11, 0))
        [p] = self.ledger("CREDIT_SPREADS_10K_PAPER")["open"]
        self.assertEqual(p["kind"], "CALL_CREDIT_SPREAD")
        self.assertEqual(p["long_strike"] - p["short_strike"], 5)
        self.assertGreater(p["short_strike"], self.client.spot)

    def test_one_new_spread_per_underlying_per_day(self):
        self.run_at(at(11, 0))
        chain_calls = self.client.option_chain_calls
        summary = self.run_at(at(11, 15))
        self.assertEqual(summary["opened"], [])
        self.assertEqual(self.client.option_chain_calls, chain_calls)      # no chain fetch when nothing to do
        self.assertEqual(len(self.ledger("CREDIT_SPREADS_10K_PAPER")["open"]), 1)

    def test_take_profit_at_half_the_credit(self):
        self.run_at(at(11, 0))
        self.client.scale = 0.4
        summary = self.run_at(at(14, 0))
        self.assertEqual({c["exit_reason"] for c in summary["closed"]}, {"take_profit"})
        book = self.ledger("CREDIT_SPREADS_10K_PAPER")
        [c] = book["closed"]
        self.assertGreater(c["realized_pnl"], 0)
        self.assertAlmostEqual(book["cash"], 10_000 + c["realized_pnl"], places=6)
        self.assertAlmostEqual(book["realized_pnl"], c["realized_pnl"], places=6)

    def test_stop_loss_at_twice_the_credit(self):
        self.run_at(at(11, 0))
        self.client.scale = 3.5
        summary = self.run_at(at(14, 0))
        self.assertEqual({c["exit_reason"] for c in summary["closed"]}, {"stop_loss"})
        [c] = self.ledger("CREDIT_SPREADS_1K_PAPER")["closed"]
        self.assertLess(c["realized_pnl"], 0)
        self.assertLessEqual(-c["realized_pnl"], c["max_loss_dollars"])

    def test_time_exit_with_21_days_left(self):
        self.run_at(at(11, 0))
        later = TODAY + timedelta(days=11)                                  # 32 - 11 = 21 days left
        session = (datetime.combine(later, datetime.min.time(), ET).replace(hour=9, minute=30),
                   datetime.combine(later, datetime.min.time(), ET).replace(hour=16))
        self.edit_config(lambda d: d.__setitem__("enabled", False))
        self.client.now = at(11, 0, later)
        summary = spreads.run_once(self.tmp, self.data, self.client, now=at(11, 0, later), session=session,
                                   closes_fn=lambda s, t: {})
        self.assertEqual({c["exit_reason"] for c in summary["closed"]}, {"time_exit"})

    def test_switched_off_still_manages_open_spreads(self):
        self.run_at(at(11, 0))
        self.edit_config(lambda d: d.__setitem__("enabled", False))
        self.client.scale = 0.4
        self.client.now = at(14, 0)
        summary = spreads.run_once(self.tmp, self.data, self.client, now=at(14, 0), session=SESSION,
                                   closes_fn=lambda s, t: {})
        self.assertEqual(len(summary["closed"]), 2)
        self.assertEqual(summary["opened"], [])
        self.assertIn("new spreads switched off (enabled: false)", summary["skipped"])

    def test_no_entries_outside_the_window_or_when_closed_or_disorderly(self):
        self.assertEqual(self.run_at(at(9, 50))["opened"], [])
        self.assertEqual(self.run_at(at(15, 10))["opened"], [])
        self.assertIn("market not open (or in its first/last 5 minutes)", self.run_at(at(11, 0), session=None)["skipped"])
        self.client.vix = 31
        self.assertEqual(self.run_at(at(11, 0))["opened"], [])
        self.client.vix = 17
        self.assertEqual(len(self.run_at(at(11, 0))["opened"]), 2)

    def test_budget_too_small_records_why(self):
        self.edit_config(lambda d: d["accounts"]["CREDIT_SPREADS_1K_PAPER"].__setitem__("max_loss_per_trade_dollars", 50))
        self.run_at(at(11, 0))
        self.assertEqual(self.ledger("CREDIT_SPREADS_1K_PAPER")["open"], [])
        checks = [json.loads(line) for line in (self.data / "credit_spreads" / "decisions.jsonl").read_text().splitlines()]
        [check] = [c for c in checks if c["type"] == "entry_check"]
        self.assertIn("worst loss", check["accounts"]["CREDIT_SPREADS_1K_PAPER"])
        self.assertEqual(check["accounts"]["CREDIT_SPREADS_10K_PAPER"], "OPENED")

    def test_stale_quotes_hold_the_position(self):
        self.run_at(at(11, 0))
        self.client.scale = 0.4
        self.client.now = at(11, 0)                                         # quotes stamped three hours ago
        summary = spreads.run_once(self.tmp, self.data, self.client, now=at(14, 0), session=SESSION,
                                   closes_fn=lambda s, t: {s_: self.closes for s_ in s})
        self.assertEqual(summary["closed"], [])
        self.assertTrue(any("stale" in s for s in summary["skipped"]))

    def test_quotes_stamped_after_the_run_started_are_fresh(self):
        self.client.now = at(11, 1)                                         # the chain fetch took a minute
        summary = spreads.run_once(self.tmp, self.data, self.client, now=at(11, 0), session=SESSION,
                                   closes_fn=lambda s, t: {s_: self.closes for s_ in s})
        self.assertEqual(len(summary["opened"]), 2 * len(spreads.load_config(self.tmp)["underlyings"]))

    def test_expired_spread_settles_at_its_intrinsic_value(self):
        p = {"right": "P", "width": 5, "short_strike": 740.0}
        self.assertEqual(spreads.intrinsic(p, 760), 0)
        self.assertEqual(spreads.intrinsic(p, 738), 2)
        self.assertEqual(spreads.intrinsic(p, 700), 5)


if __name__ == "__main__":
    unittest.main()
