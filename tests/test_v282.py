"""V2 2.8.2 (owner, 2026-11-19): ADR breakout ruler, pullback-only entries, room to the target, LIVE entry retry
limits. Offline: no network."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.models import Bar, OptionQuote
from trader.live_execution import EntryRetry, entry_retry_stop
from trader.v2 import config as v2config
from trader.v2 import pipeline
from trader.v2.confirmation import BreakoutTracker
from trader.v2.live_support import unsupported_for_live
from trader.v2.replay import variant_config

ROOT = Path(__file__).resolve().parents[1]
V282 = "strategies/v2_versions/professional_v2_2.8.2.json"
OPEN = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)


def bar(i: int, close: float, *, low=None, high=None) -> Bar:
    return Bar(OPEN + timedelta(minutes=i), close, close + 0.02 if high is None else high,
               close - 0.02 if low is None else low, close, 1000.0)


class _Sig:
    def __init__(self, side): self.side = side


def metrics():
    # level 101.00, opening range 99.10-100.90, one-minute ATR 0.20
    return {"breakout_level_call": 101.0, "breakout_level_put": 99.0, "atr": 0.2, "opening_range_high": 100.9,
            "opening_range_low": 99.1, "relative_volume": 1.5, "stop_distance": 0.4, "target_distance": 0.8}


def confirmation(**changes):
    c = json.loads(json.dumps(v2config.load_v2_config(ROOT).data["confirmation"]))   # 2.8.1: no 2.8.2 keys
    pull = changes.pop("pullback", {})
    c.update(changes)
    c["pullback"].update(pull)
    return c


def feed(tracker, closes, lows=None, start=0):
    out = None
    for i, close in enumerate(closes):
        out = tracker.update(bar(start + i, close, low=lows[i] if lows else None),
                             _Sig("CALL") if start == 0 and i == 0 else None, metrics())
    return out


class RulerTests(unittest.TestCase):
    def test_small_poke_does_not_confirm_under_the_daily_range_ruler(self):
        t = BreakoutTracker(confirmation(min_distance_adr=0.10), 15)
        t.set_daily_range(2.0)                                    # needs 0.20 past the level
        self.assertEqual(feed(t, [101.02, 101.08])["state"], "AWAITING_CONFIRMATION")
        t2 = BreakoutTracker(confirmation(min_distance_adr=0.10), 15)
        t2.set_daily_range(2.0)
        r = feed(t2, [101.02, 101.25])
        self.assertEqual(r["state"], "CONFIRMED")
        self.assertTrue(r["actionable"])

    def test_without_the_ruler_the_same_poke_confirms(self):
        r = feed(BreakoutTracker(confirmation(), 15), [101.02, 101.08])   # 2.8.1 behaviour unchanged
        self.assertEqual(r["state"], "CONFIRMED")

    def test_unknown_daily_range_fails_closed(self):
        t = BreakoutTracker(confirmation(min_distance_adr=0.10), 15)     # set_daily_range never called
        self.assertNotIn(feed(t, [101.02, 101.6])["state"], ("CONFIRMED", "PULLBACK_CONFIRMED"))

    def test_retest_tolerance_widens_with_the_daily_range(self):
        # low 101.08 is 0.08 above the level: outside 0.25 x 0.20 ATR = 0.05, inside 0.05 x 2.0 = 0.10
        narrow = BreakoutTracker(confirmation(pullback_only=True), 15)
        wide = BreakoutTracker(confirmation(pullback_only=True, pullback={"retest_tolerance_adr": 0.05}), 15)
        wide.set_daily_range(2.0)
        closes, lows = [101.02, 101.10, 101.12], [101.0, 101.09, 101.08]     # confirmed on bar 2, dip on bar 3
        self.assertEqual(feed(narrow, closes, lows=lows)["state"], "CONFIRMED")
        self.assertEqual(feed(wide, closes, lows=lows)["state"], "PULLBACK_HOLDING")


class PullbackOnlyTests(unittest.TestCase):
    def test_confirmed_breakout_is_not_a_buy_until_a_retest_and_continuation(self):
        t = BreakoutTracker(confirmation(pullback_only=True), 15)
        r = feed(t, [101.02, 101.08])
        self.assertEqual(r["state"], "CONFIRMED")
        self.assertFalse(r["actionable"])
        r = t.update(bar(2, 101.03, low=101.0), None, metrics())          # back at the level
        self.assertEqual(r["state"], "PULLBACK_HOLDING")
        self.assertFalse(r["actionable"])
        r = t.update(bar(3, 101.2, low=101.1), None, metrics())           # closes past the retest bar's high
        self.assertEqual(r["state"], "PULLBACK_CONFIRMED")
        self.assertTrue(r["actionable"])

    def test_no_retest_expires(self):
        t = BreakoutTracker(confirmation(pullback_only=True), 15)
        r = feed(t, [101.02, 101.3] + [101.5] * 16, lows=[101.0, 101.25] + [101.45] * 16)
        self.assertEqual(r["state"], "EXPIRED")

    def test_pipeline_waits_on_a_non_actionable_confirmed_state(self):
        t = BreakoutTracker(confirmation(pullback_only=True), 15)
        tracked = feed(t, [101.02, 101.08])
        self.assertEqual(tracked["state"], "CONFIRMED")
        self.assertFalse(tracked["actionable"])


class RoomToTargetTests(unittest.TestCase):
    room = {"enabled": True, "max_day_range_adr": 1.25, "reject_target_past_prior_extreme": True}
    cfg = {}

    def check(self, *, side="CALL", price=60.495, low=59.42, high=60.5, adr=1.7, prior_high=61.005, prior_low=59.405,
              target_d=0.71, room=None):
        bars = [Bar(OPEN, low, high, low, price, 1.0)]
        return pipeline.room_to_target_check(room or self.room, side=side, price=price, bars=bars,
                                             setup={"stop_distance": 0.355, "target_distance": target_d}, cfg=self.cfg,
                                             adr=adr, prior_high=prior_high, prior_low=prior_low)

    def test_nflx_20261118_target_past_the_prior_high_is_rejected(self):
        r = self.check()
        self.assertEqual(r["status"], "REJECT")
        self.assertIn("prior session's high", r["reason"])

    def test_target_needing_an_oversized_day_is_rejected(self):
        r = self.check(adr=1.0, prior_high=70.0)                          # day range at target 1.785 = 1.79 ADR
        self.assertEqual(r["status"], "REJECT")
        self.assertIn("average daily range", r["reason"])

    def test_room_left_passes(self):
        self.assertEqual(self.check(prior_high=62.0)["status"], "PASS")

    def test_put_uses_the_prior_low(self):
        self.assertEqual(self.check(side="PUT", price=59.6, prior_low=59.405)["status"], "REJECT")   # target 58.89
        self.assertEqual(self.check(side="PUT", price=59.6, prior_low=58.0)["status"], "PASS")

    def test_prior_extreme_check_can_be_off(self):
        room = {**self.room, "reject_target_past_prior_extreme": False}
        self.assertEqual(self.check(room=room)["status"], "PASS")

    def test_unknown_inputs_fail_closed(self):
        self.assertEqual(self.check(adr=None)["status"], "INSUFFICIENT_DATA")
        self.assertEqual(self.check(prior_high=None)["status"], "INSUFFICIENT_DATA")

    def test_average_daily_range_needs_enough_sessions(self):
        self.assertIsNone(pipeline.average_daily_range([1.0] * 13, 14))
        self.assertAlmostEqual(pipeline.average_daily_range([5.0] + [1.0] * 14, 14), 1.0)


class ConfigTests(unittest.TestCase):
    def test_282_loads_and_live_can_run_it(self):
        c = v2config.load_v2_config(ROOT, V282)
        self.assertEqual(c.version, "2.8.2")
        self.assertEqual(unsupported_for_live(c), [])
        self.assertTrue(c.data["confirmation"]["pullback_only"])
        self.assertTrue(c.data["live_entry"]["enabled"])

    def test_282_only_adds_the_new_rules_to_281(self):
        new = v2config.load_v2_config(ROOT, V282).data
        old = v2config.load_v2_config(ROOT, "strategies/v2_versions/professional_v2_2.8.1.json").data
        off = variant_config(new, {"confirmation": {"min_distance_adr": 0, "pullback_only": False,
                                                    "pullback": {"retest_tolerance_adr": 0}},
                                   "room_to_target": {"enabled": False}})
        for key in set(old) | set(new):
            if key in ("version", "room_to_target", "live_entry", "context", "confirmation"):
                continue
            self.assertEqual(old[key], new[key], key)
        self.assertEqual(off["confirmation"]["pullback"]["retest_tolerance_atr"], old["confirmation"]["pullback"]["retest_tolerance_atr"])
        self.assertEqual({k: v for k, v in new["context"].items() if k != "average_range_sessions"}, old["context"])

    def test_283_is_281_plus_the_live_entry_retry_only(self):
        new = v2config.load_v2_config(ROOT, "strategies/v2_versions/professional_v2_2.8.3.json")
        old = v2config.load_v2_config(ROOT, "strategies/v2_versions/professional_v2_2.8.1.json").data
        self.assertEqual(new.version, "2.8.3")
        self.assertEqual(unsupported_for_live(new), [])
        self.assertEqual(set(new.data) - set(old), {"live_entry"})
        for key in old:
            if key != "version":
                self.assertEqual(old[key], new.data[key], key)
        self.assertEqual(new.data["live_entry"], v2config.load_v2_config(ROOT, V282).data["live_entry"])

    def _root_with(self, mutate):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copy2(ROOT / "strategy.txt", tmp / "strategy.txt")
        shutil.copytree(ROOT / "strategies", tmp / "strategies")
        data = json.loads(v2config._strip_json_comments((ROOT / V282).read_text(encoding="utf-8")))
        mutate(data)
        (tmp / v2config.CONFIG_FILE).write_text(json.dumps(data), encoding="utf-8")
        return tmp

    def test_adr_rules_need_the_range_window(self):
        tmp = self._root_with(lambda d: d["context"].pop("average_range_sessions"))
        with self.assertRaisesRegex(v2config.V2ConfigError, "average_range_sessions"):
            v2config.load_v2_config(tmp)

    def test_pullback_only_needs_pullbacks(self):
        tmp = self._root_with(lambda d: d["confirmation"]["pullback"].__setitem__("enabled", False))
        with self.assertRaisesRegex(v2config.V2ConfigError, "pullback_only"):
            v2config.load_v2_config(tmp)

    def test_live_entry_limits_are_bounded(self):
        tmp = self._root_with(lambda d: d["live_entry"].__setitem__("max_chase_fraction", 0.5))
        with self.assertRaises(v2config.V2ConfigError):
            v2config.load_v2_config(tmp)


def quote(ask, bid=None):
    return OptionQuote("IWM   261127P00268000", "IWM", OPEN + timedelta(days=8), 268.0, "P", bid or ask - 0.05, ask,
                       quote_timestamp=datetime.now(timezone.utc))


class EntryRetryLimitTests(unittest.TestCase):
    retry = EntryRetry(max_attempts=3, attempt_timeout_seconds=8, max_chase_fraction=0.03, max_move_toward_target=0.5,
                       max_cost_dollars=350.0)
    # the LIVE IWM put of 2026-11-19: decision ask 3.36, entry 268.58, stop 269.65, target 266.44
    template = {"right": "P", "entry_underlying": 268.58, "stop_underlying": 269.65, "target_underlying": 266.44,
                "modeled_risk_dollars": 340.0, "premium_exposure_dollars": 336.0}

    def stop(self, ask, spot):
        return entry_retry_stop(decision=quote(3.36), fresh=quote(ask), spot=spot, template=self.template,
                                retry=self.retry, quantity=1)

    def test_iwm_20261119_would_have_been_bought_at_341(self):
        self.assertIsNone(self.stop(3.41, 268.4))                         # 1.5% over the decision ask

    def test_ask_past_the_chase_limit_stops(self):
        self.assertIn("chase limit", self.stop(3.47, 268.4))              # limit 3.46

    def test_budget_stops(self):
        retry = EntryRetry(3, 8, 0.10, 0.5, max_cost_dollars=344.0)
        why = entry_retry_stop(decision=quote(3.36), fresh=quote(3.41), spot=268.4, template=self.template,
                               retry=retry, quantity=1)
        self.assertIn("budget", why)

    def test_stock_past_the_stop_or_too_far_toward_the_target_stops(self):
        self.assertIn("stop", self.stop(3.30, 269.70))
        self.assertIn("of the way to the target", self.stop(3.40, 267.40))   # 55% of the 2.14 move
        self.assertIsNone(self.stop(3.40, 267.60))                          # 46%

    def test_unknown_underlying_price_stops(self):
        self.assertIn("unknown", self.stop(3.40, None))


if __name__ == "__main__":
    unittest.main()
