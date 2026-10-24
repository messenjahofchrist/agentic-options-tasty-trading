"""PROFESSIONAL_STRATEGY_V2 unit tests. All MOCKED / offline: no network. Live tastytrade checks are the separate
`python -m trader.cli v2-readiness` integration run, which these tests do not replace."""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.models import Bar, OptionQuote
from trader.risk_engine import RiskState
from trader.strategy_loader import parse_strategy
from trader.v2 import config as v2config
from trader.v2 import indicators, management, pipeline, portfolio, sizing
from trader.v2.confirmation import BreakoutTracker
from trader.v2.context import DailyLevels, MarketContext, VixReading
from trader.v2.correlation import CorrelationModel, log_returns, pearson
from trader.v2.datahub import RequestLog, SharedCache, history_batch
from trader.v2.events import EventEngine
from trader.v2.options import evaluate_option, select_near_money, volatility_inputs
from trader.v2.regime import classify
from trader.v2.sectors import SectorMap, sector_confirmation

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def cfg():
    return v2config.load_v2_config(ROOT)


def bar(ts: datetime, close: float, *, high=None, low=None, open_=None, volume=1000.0) -> Bar:
    return Bar(ts, open_ if open_ is not None else close, high if high is not None else close + 0.05,
               low if low is not None else close - 0.05, close, volume)


def trend_bars(start: datetime, n: int, base: float, step: float, volume: float = 1000.0) -> list[Bar]:
    return [bar(start + timedelta(minutes=i), base + step * i, open_=base + step * i - step / 2, volume=volume) for i in range(n)]


OPEN = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)   # Monday 09:30 ET


class ConfigTests(unittest.TestCase):
    def test_loads_and_is_pinned_to_the_unchanged_base_strategy(self):
        c = cfg()
        base = parse_strategy(ROOT / "strategy.txt")
        self.assertEqual(c.base.sha256, base.sha256)
        self.assertEqual(c.data["base_strategy"]["configuration_hash"], "c597cfe86d7971a7e96b08ed71bc58eff6dca6942fcb0288b24498dedf6e902b")
        self.assertEqual(c.data["mode"], "PAPER")

    def _with_root(self, mutate):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("strategy.txt", "strategies"):
            src = ROOT / name
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, tmp / name)
        (tmp / v2config.MANIFEST_FILE).unlink(missing_ok=True)   # each test starts un-activated
        path = tmp / v2config.CONFIG_FILE
        data = json.loads(v2config._strip_json_comments(path.read_text(encoding="utf-8")))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        return tmp

    def test_unknown_key_fails_closed(self):
        tmp = self._with_root(lambda d: d["regime"].__setitem__("sentiment_score", 1))
        with self.assertRaises(v2config.V2ConfigError):
            v2config.load_v2_config(tmp)

    def test_missing_key_fails_closed(self):
        tmp = self._with_root(lambda d: d["portfolio"].pop("aggregate_max_loss_fraction"))
        with self.assertRaises(v2config.V2ConfigError):
            v2config.load_v2_config(tmp)

    def test_base_hash_mismatch_refused(self):
        tmp = self._with_root(lambda d: d["base_strategy"].__setitem__("configuration_hash", "0" * 64))
        with self.assertRaises(v2config.V2ConfigError):
            v2config.load_v2_config(tmp)

    def test_live_mode_refused(self):
        tmp = self._with_root(lambda d: d.__setitem__("mode", "LIVE"))
        with self.assertRaises(v2config.V2ConfigError):
            v2config.load_v2_config(tmp)

    def test_manifest_must_match(self):
        tmp = self._with_root(lambda d: None)
        c = v2config.load_v2_config(tmp)
        with self.assertRaises(v2config.V2ConfigError):
            v2config.verify_v2_manifest(tmp, c)          # not activated
        v2config.write_v2_manifest(tmp, c, approved_by="test", note="t")
        self.assertEqual(v2config.verify_v2_manifest(tmp, c)["configuration_hash"], c.sha256)
        tmp2 = self._with_root(lambda d: d["regime"].__setitem__("vix_disorderly_level", 31.0))
        shutil.copy2(tmp / v2config.MANIFEST_FILE, tmp2 / v2config.MANIFEST_FILE)
        with self.assertRaises(v2config.V2ConfigError):
            v2config.verify_v2_manifest(tmp2, v2config.load_v2_config(tmp2))

    def test_owner_authorized_values(self):
        d = cfg().data
        self.assertEqual(d["portfolio"]["aggregate_max_loss_fraction"], {"ORIGINAL_ACCOUNT_PAPER": 0.30, "SMALL_ACCOUNT_PAPER": 0.40})
        self.assertEqual(d["portfolio"]["cluster_max_loss"], "one_trade_budget")
        self.assertEqual(d["sizing"]["max_contracts"], 1)
        self.assertFalse(d["sizing"]["quality_score_affects_size"])
        self.assertFalse(d["management"]["partial_profit"]["enabled"])

    def test_sector_map_covers_both_universes(self):
        from trader.account_profiles import ProfileConfig
        sm = SectorMap(cfg().sector_map)
        self.assertEqual(sm.unmapped(parse_strategy(ROOT / "strategy.txt").universe), [])
        self.assertEqual(sm.unmapped(ProfileConfig.load(ROOT, "small").small_universe), [])


class IndicatorAndContextTests(unittest.TestCase):
    def test_session_vwap_typical_price_weighting(self):
        bars = [Bar(OPEN, 10, 12, 8, 10, 100), Bar(OPEN + timedelta(minutes=1), 20, 22, 18, 20, 300)]
        self.assertAlmostEqual(indicators.session_vwap(bars), (10 * 100 + 20 * 300) / 400)
        self.assertIsNone(indicators.session_vwap([Bar(OPEN, 1, 1, 1, 1, 0)]))

    def test_directional_bias_votes_and_missing(self):
        up = trend_bars(OPEN, 30, 100, 0.1)
        b = indicators.directional_bias(up, fast=9, slow=21)
        self.assertEqual(b["bias"], 3)
        self.assertEqual(b["missing"], [])
        short = indicators.directional_bias(up[:5], fast=9, slow=21)
        self.assertIn("ema", short["missing"])

    def test_efficiency_needs_window(self):
        self.assertIsNone(indicators.trend_efficiency([1, 2, 3], 5))
        self.assertAlmostEqual(indicators.trend_efficiency([1, 2, 3, 4, 5, 6], 5), 1.0)

    def _ctx(self):
        c = cfg().data
        return MarketContext(("SPY", "QQQ", "IWM"), tuple(c["context"]["sector_etfs"]), 9, 21, 14, 30, 150, 180)

    def test_freshness_and_prior_levels(self):
        ctx = self._ctx()
        bars = trend_bars(OPEN, 30, 100, 0.1)
        ctx.register("SPY", bars)
        ctx.prior["SPY"] = DailyLevels("2026-10-23", 99, 101, 98, 99.5)
        ctx.premarket["SPY"] = {"high": 100.2, "low": 99.4}
        now = bars[-1].timestamp + timedelta(minutes=1)
        state = ctx.symbol_state("SPY", now)
        self.assertTrue(state["fresh"])
        self.assertEqual(state["vs_prior_range"], "above_prior_high")
        self.assertAlmostEqual(state["overnight_gap"], (bars[0].open - 99.5) / 99.5)
        self.assertEqual(state["vs_premarket_range"], "above_premarket_high")
        self.assertFalse(ctx.fresh("SPY", now + timedelta(minutes=5)))   # stale after 150 s
        ctx.mark_unavailable("SPY", "feed failed")
        self.assertFalse(ctx.fresh("SPY", now))

    def test_breadth_counts_only_fresh(self):
        ctx = self._ctx()
        ctx.register("A", trend_bars(OPEN, 30, 100, 0.1))
        ctx.register("B", trend_bars(OPEN, 30, 100, -0.1))
        ctx.register("C", trend_bars(OPEN - timedelta(hours=1), 30, 100, 0.1))   # stale
        b = ctx.breadth(["A", "B", "C", "D"], OPEN + timedelta(minutes=30))
        self.assertEqual((b["counted"], b["above_vwap"], b["below_vwap"]), (2, 1, 1))


def snapshot(*, spy=3, qqq=3, iwm=3, fresh=True, vix=18.0, vix_prev=18.0, eff=0.5, atrf=0.0005, breadth=0.7):
    def st(bias):
        return {"bias": bias, "fresh": fresh, "missing": [], "efficiency": eff, "atr_fraction": atrf}
    return {"broad": {"SPY": st(spy), "QQQ": st(qqq), "IWM": st(iwm)},
            "vix": None if vix is None else {"value": vix, "change_fraction": (vix - vix_prev) / vix_prev, "fresh": True},
            "breadth": {"above_fraction": breadth, "below_fraction": 1 - breadth}}


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.r = cfg().data["regime"]

    def test_rules_in_order(self):
        self.assertEqual(classify(snapshot(fresh=False), self.r).regime, "UNCLEAR")
        self.assertEqual(classify(snapshot(vix=31), self.r).regime, "HIGH_VOLATILITY")
        self.assertEqual(classify(snapshot(vix=21, vix_prev=18), self.r).regime, "HIGH_VOLATILITY")   # +16.7%
        self.assertEqual(classify(snapshot(atrf=0.003), self.r).regime, "HIGH_VOLATILITY")
        self.assertEqual(classify(snapshot(), self.r).regime, "STRONG_BULLISH")
        self.assertEqual(classify(snapshot(spy=-3, qqq=-3, iwm=-3, breadth=0.2), self.r).regime, "STRONG_BEARISH")
        self.assertEqual(classify(snapshot(spy=2, qqq=1, iwm=0), self.r).regime, "WEAK_BULLISH")
        self.assertEqual(classify(snapshot(spy=-1, qqq=-1, iwm=-1), self.r).regime, "WEAK_BEARISH")
        self.assertEqual(classify(snapshot(spy=1, qqq=-1, iwm=1), self.r).regime, "RANGE_BOUND")

    def test_low_breadth_or_efficiency_is_not_strong(self):
        self.assertEqual(classify(snapshot(breadth=0.4), self.r).regime, "WEAK_BULLISH")
        self.assertEqual(classify(snapshot(eff=0.1), self.r).regime, "WEAK_BULLISH")

    def test_missing_vix_falls_back_to_spy_atr_and_says_so(self):
        result = classify(snapshot(vix=None), self.r)
        self.assertEqual(result.regime, "STRONG_BULLISH")
        self.assertTrue(any("VIX unavailable" in n for n in result.notes))

    def test_allowed_directions(self):
        self.assertEqual(classify(snapshot(), self.r).allowed, ("CALL",))
        self.assertEqual(classify(snapshot(spy=1, qqq=-1, iwm=1), self.r).allowed, ())


class SectorTests(unittest.TestCase):
    def setUp(self):
        self.sm = SectorMap(cfg().sector_map)
        self.c = cfg().data["sector"]

    def test_mapping(self):
        self.assertEqual(self.sm.sector_etf("NVDA"), "XLK")
        self.assertEqual(self.sm.sector_etf("PYPL"), "XLF")
        self.assertTrue(self.sm.is_single_stock("BABA"))
        self.assertFalse(self.sm.is_single_stock("SPY"))

    def test_confirmation_outcomes(self):
        sectors = {"XLK": {"bias": 2, "fresh": True, "missing": [], "change_from_open": 0.01, "relative_strength_vs_spy": 0.002}}
        self.assertEqual(sector_confirmation("NVDA", "CALL", self.sm, sectors, self.c)["status"], "PASS")
        self.assertEqual(sector_confirmation("NVDA", "PUT", self.sm, sectors, self.c)["status"], "REJECT")
        self.assertEqual(sector_confirmation("SPY", "CALL", self.sm, sectors, self.c)["status"], "NOT_APPLICABLE")
        self.assertEqual(sector_confirmation("AAPL", "CALL", self.sm, {}, self.c)["status"], "INSUFFICIENT_DATA")
        self.assertEqual(sector_confirmation("ZZZZ", "CALL", self.sm, sectors, self.c)["status"], "INSUFFICIENT_DATA")


class _Sig:
    def __init__(self, side): self.side = side


def metrics(level_call=101.0, level_put=99.0, atr=0.2, rh=100.9, rl=99.1, rv=1.5):
    return {"breakout_level_call": level_call, "breakout_level_put": level_put, "atr": atr, "opening_range_high": rh,
            "opening_range_low": rl, "relative_volume": rv, "stop_distance": 0.4, "target_distance": 0.8}


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg().data["confirmation"]

    def feed(self, tracker, closes, lows=None, first_signal=True):
        out = None
        for i, close in enumerate(closes):
            low = lows[i] if lows else close - 0.02
            out = tracker.update(bar(OPEN + timedelta(minutes=i), close, low=low, high=close + 0.02),
                                 _Sig("CALL") if (i == 0 and first_signal) else None, metrics())
        return out

    def test_two_closes_beyond_and_distance_confirm(self):
        t = BreakoutTracker(self.c, 15)
        r = self.feed(t, [101.02, 101.08])
        self.assertEqual(r["state"], "CONFIRMED")
        self.assertTrue(r["actionable"])

    def test_first_bar_back_inside_is_rejected(self):
        r = self.feed(BreakoutTracker(self.c, 15), [101.02, 100.8])
        self.assertEqual(r["state"], "REJECTED_BREAKOUT")

    def test_later_close_inside_range_fails(self):
        r = self.feed(BreakoutTracker(self.c, 15), [101.02, 101.08, 101.1, 100.7])
        self.assertEqual(r["state"], "FAILED_BREAKOUT")

    def test_pullback_retest_then_continuation(self):
        t = BreakoutTracker(self.c, 15)
        r = self.feed(t, [101.02, 101.01, 101.03], lows=[101.0, 100.97, 101.0])
        self.assertEqual(r["state"], "PULLBACK_HOLDING")
        r = t.update(bar(OPEN + timedelta(minutes=3), 101.2, low=101.1, high=101.25), None, metrics())
        self.assertEqual(r["state"], "PULLBACK_CONFIRMED")
        self.assertTrue(r["actionable"])

    def test_expires_without_confirmation(self):
        t = BreakoutTracker(self.c, 15)
        r = self.feed(t, [101.02] + [101.03] * 16, lows=[101.0] + [101.0] * 16)
        self.assertEqual(r["state"], "EXPIRED")

    def test_out_of_order_bar_refused(self):
        t = BreakoutTracker(self.c, 15)
        t.update(bar(OPEN + timedelta(minutes=1), 101.02), _Sig("CALL"), metrics())
        with self.assertRaises(ValueError):
            t.update(bar(OPEN, 101.1), None, metrics())

    def test_no_look_ahead_state_depends_only_on_processed_bars(self):
        a, b = BreakoutTracker(self.c, 15), BreakoutTracker(self.c, 15)
        self.feed(a, [101.02, 101.04])
        self.feed(b, [101.02, 101.04])
        before = a.state
        self.assertEqual(before, b.state)                                   # same bars, same state
        b.update(bar(OPEN + timedelta(minutes=2), 100.5), None, metrics())   # a later bar changes only b
        self.assertEqual(a.state, before)
        self.assertEqual(b.state, "FAILED_BREAKOUT")


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.sm = SectorMap(cfg().sector_map)
        self.c = cfg().data["correlation"]

    def series(self, values):
        return [(f"2026-07-{i + 1:02d}" if i < 31 else f"2026-08-{i - 30:02d}", v) for i, v in enumerate(values)]

    def test_pearson_and_sign_aware_same_bet(self):
        base = [100 * math.exp(0.01 * math.sin(i) + 0.002 * i) for i in range(50)]
        inverse = [100 * math.exp(-0.01 * math.sin(i) - 0.002 * i) for i in range(50)]
        model = CorrelationModel(self.c, self.sm, {"QQQ": self.series(base), "SQQQ": self.series(inverse),
                                                   "NVDA": self.series(base)})
        self.assertAlmostEqual(model.pair("QQQ", "NVDA")["corr"], 1.0, places=6)
        self.assertTrue(model.same_bet("QQQ", "CALL", "NVDA", "CALL")["same_bet"])
        self.assertFalse(model.same_bet("QQQ", "CALL", "NVDA", "PUT")["same_bet"])
        self.assertTrue(model.same_bet("QQQ", "CALL", "SQQQ", "PUT")["same_bet"])   # inverse ETF, opposite side

    def test_fallback_groups_without_enough_data(self):
        model = CorrelationModel(self.c, self.sm, {})
        self.assertEqual(model.pair("NVDA", "AAPL")["method"], "fallback")
        self.assertTrue(model.same_bet("NVDA", "CALL", "AAPL", "CALL")["same_bet"])     # same sector ETF
        self.assertTrue(model.same_bet("SPY", "CALL", "IWM", "CALL")["same_bet"])       # broad indexes
        self.assertTrue(model.same_bet("TQQQ", "CALL", "SQQQ", "PUT")["same_bet"])      # tracked index, signed
        self.assertFalse(model.same_bet("BAC", "CALL", "PFE", "CALL")["same_bet"])

    def test_clusters(self):
        model = CorrelationModel(self.c, self.sm, {})
        positions = [{"trade_id": "1", "underlying": "NVDA", "right": "C"}, {"trade_id": "2", "underlying": "AAPL", "right": "C"},
                     {"trade_id": "3", "underlying": "PFE", "right": "C"}]
        self.assertEqual(sorted(len(c) for c in model.clusters(positions)), [1, 2])


def position(underlying, right="C", loss=500.0, trade_id=None):
    return {"trade_id": trade_id or underlying, "underlying": underlying, "right": right, "modeled_risk_dollars": loss,
            "quantity": 1, "entry_price": loss / 100}


class PortfolioAndSizingTests(unittest.TestCase):
    def setUp(self):
        self.model = CorrelationModel(cfg().data["correlation"], SectorMap(cfg().sector_map), {})

    def test_cluster_cap_is_one_trade_budget(self):
        budgets = portfolio.budgets_for(equity=10_000, per_trade_budget=1_000, aggregate_fraction=0.30,
                                        positions=[position("NVDA", loss=800)])
        ok = portfolio.evaluate(model=self.model, symbol="AAPL", side="CALL", candidate_max_loss=300, entry_cash=300,
                                cash=9_000, positions=[position("NVDA", loss=800)], budgets=budgets)
        self.assertFalse(ok.ok)
        self.assertIn("correlated_cluster_cap", ok.reasons[0])
        hedge = portfolio.evaluate(model=self.model, symbol="AAPL", side="PUT", candidate_max_loss=300, entry_cash=300,
                                   cash=9_000, positions=[position("NVDA", loss=800)], budgets=budgets)
        self.assertTrue(hedge.ok)

    def test_aggregate_cap_and_buying_power(self):
        open_positions = [position("BAC", loss=1000), position("PFE", loss=1000)]
        budgets = portfolio.budgets_for(equity=10_000, per_trade_budget=1_000, aggregate_fraction=0.30, positions=open_positions)
        self.assertAlmostEqual(budgets.aggregate_remaining, 1000)
        over = portfolio.evaluate(model=self.model, symbol="KO", side="CALL", candidate_max_loss=1000.01, entry_cash=900,
                                  cash=9_000, positions=open_positions, budgets=budgets)
        self.assertFalse(over.ok)
        poor = portfolio.evaluate(model=self.model, symbol="KO", side="CALL", candidate_max_loss=500, entry_cash=600,
                                  cash=100, positions=open_positions, budgets=budgets)
        self.assertTrue(any("buying_power" in r for r in poor.reasons))

    def test_sizing_caps_at_one_contract_and_logs_risk_quantity(self):
        s = sizing.size_long_option(max_loss_per_contract=200, per_trade_budget=1000, aggregate_remaining=3000,
                                    cluster_remaining=1000, cash=5000, cash_per_contract=200, bid_size=50, ask_size=50,
                                    open_interest=5000, cfg=cfg().data["sizing"], hard_max_contracts=1)
        self.assertEqual((s["quantity"], s["risk_based_quantity"]), (1, 5))
        none = sizing.size_long_option(max_loss_per_contract=1200, per_trade_budget=1000, aggregate_remaining=3000,
                                       cluster_remaining=1000, cash=5000, cash_per_contract=1200, bid_size=50, ask_size=50,
                                       open_interest=5000, cfg=cfg().data["sizing"], hard_max_contracts=1)
        self.assertEqual(none["quantity"], 0)

    def test_one_lot_fits_a_one_contract_display_but_not_an_empty_one(self):
        one = sizing.size_long_option(max_loss_per_contract=900, per_trade_budget=1000, aggregate_remaining=3000,
                                      cluster_remaining=1000, cash=5000, cash_per_contract=900, bid_size=1, ask_size=93,
                                      open_interest=900, cfg=cfg().data["sizing"], hard_max_contracts=1)
        self.assertEqual(one["quantity"], 1)
        empty = sizing.size_long_option(max_loss_per_contract=900, per_trade_budget=1000, aggregate_remaining=3000,
                                        cluster_remaining=1000, cash=5000, cash_per_contract=900, bid_size=0, ask_size=93,
                                        open_interest=900, cfg=cfg().data["sizing"], hard_max_contracts=1)
        self.assertEqual(empty["quantity"], 0)

    def test_quality_score_cannot_drive_size(self):
        bad = dict(cfg().data["sizing"], quality_score_affects_size=True)
        with self.assertRaises(ValueError):
            sizing.size_long_option(max_loss_per_contract=100, per_trade_budget=1000, aggregate_remaining=1000,
                                    cluster_remaining=1000, cash=1000, cash_per_contract=100, bid_size=5, ask_size=5,
                                    open_interest=500, cfg=bad, hard_max_contracts=1)


def quote(symbol="SPY   261106C00700000", *, bid=5.0, ask=5.05, delta=0.55, iv=0.2, vega=0.4, ts=None, sizes=(10, 10), oi=1000, vol=100):
    return OptionQuote(symbol, "SPY", datetime(2026, 11, 6, tzinfo=UTC), 700, "C", bid, ask, sizes[0], sizes[1], delta,
                       0.02, -0.3, vega, iv, None, vol, oi, ts or datetime(2026, 10, 26, 15, tzinfo=UTC))


class OptionTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg().data["option_quality"]
        self.now = datetime(2026, 10, 26, 15, 0, 5, tzinfo=UTC)
        self.metrics = {"implied-volatility-index": "0.20", "tw-implied-volatility-index-rank": "0.40",
                        "historical-volatility-30-day": "12.0", "implied-volatility-percentile": "0.5",
                        "implied-volatility-index-5-day-change": "0.01", "_retrieved_at_utc": self.now.isoformat()}

    def test_selection_parity_with_original_fetch(self):
        from unittest.mock import MagicMock
        from trader.market_data import fetch_option_quotes
        from trader.v2.options import fetch_option_quotes_cached
        chain = [{"symbol": f"SPY   2611{d:02d}C00{k:03d}000", "active": True, "days-to-expiration": dte,
                  "strike-price": str(k), "option-type": "C", "expiration-date": f"2026-11-{d:02d}"}
                 for d, dte in ((2, 7), (6, 11), (13, 18), (27, 32)) for k in range(680, 721, 5)]
        quotes = {"data": {"items": [{"symbol": c["symbol"], "bid": "1", "ask": "1.1", "updated-at": "2026-10-26T15:00:00Z",
                                      "delta": "0.5", "gamma": "0.01", "theta": "-0.1", "volatility": "0.2"} for c in chain]}}
        client = MagicMock()
        client.option_chain.return_value = {"data": {"items": chain}}
        client.quotes.return_value = quotes

        class Hub:
            def option_chain(self, underlying, day):
                return client.option_chain(underlying)["data"]["items"]
        original = fetch_option_quotes(client, underlying="SPY", spot=701, now=self.now, min_dte=7, max_dte=21, max_contracts=7)
        v2 = fetch_option_quotes_cached(client, Hub(), underlying="SPY", spot=701, now=self.now, min_dte=7, max_dte=21, max_contracts=7)
        self.assertEqual(len(original), 7)
        self.assertEqual([q.symbol for q in original], [q.symbol for q in v2])
        self.assertEqual(client.quotes.call_args_list[0], client.quotes.call_args_list[1])   # identical request

    def test_clean_contract_passes(self):
        r = evaluate_option(quote(), self.metrics, self.now, self.c)
        self.assertTrue(r["ok"], r["reasons"])

    def test_expensive_and_stale_and_thin_contracts_rejected(self):
        hot = dict(self.metrics, **{"tw-implied-volatility-index-rank": "0.9"})
        self.assertIn("iv_rank", evaluate_option(quote(), hot, self.now, self.c)["reasons"][0])
        rich = dict(self.metrics, **{"historical-volatility-30-day": "5.0"})
        self.assertTrue(any("iv_to_hv30" in r for r in evaluate_option(quote(), rich, self.now, self.c)["reasons"]))
        self.assertTrue(any("contract_iv" in r for r in evaluate_option(quote(iv=0.3), self.metrics, self.now, self.c)["reasons"]))
        old = quote(ts=self.now - timedelta(seconds=60))
        self.assertTrue(any("stale" in r for r in evaluate_option(old, self.metrics, self.now, self.c)["reasons"]))
        thin = quote(sizes=(0, 5))
        self.assertTrue(any("displayed_size" in r for r in evaluate_option(thin, self.metrics, self.now, self.c)["reasons"]))

    def test_missing_volatility_data_is_rejected_not_assumed(self):
        r = evaluate_option(quote(), None, self.now, self.c)
        self.assertFalse(r["ok"])
        self.assertFalse(volatility_inputs({"historical-volatility-30-day": "0"}).get("hv30"))

    def test_select_near_money_window(self):
        chain = [{"symbol": "A", "active": True, "days-to-expiration": 8, "strike-price": "100"},
                 {"symbol": "B", "active": True, "days-to-expiration": 30, "strike-price": "100"},
                 {"symbol": "C", "active": False, "days-to-expiration": 8, "strike-price": "100"},
                 {"symbol": "D", "active": True, "days-to-expiration": 8, "strike-price": "120"}]
        self.assertEqual([c["symbol"] for c in select_near_money(chain, spot=100, min_dte=7, max_dte=21)], ["A"])


class EventTests(unittest.TestCase):
    def setUp(self):
        c = cfg()
        self.engine = EventEngine(c.calendar, c.data["events"], next_session=lambda d: d + timedelta(days=1))

    def at(self, hh, mm, day=date(2026, 10, 28)):
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)

    def test_fomc_window_delays_and_flags(self):
        inside = self.engine.check("SPY", {}, self.at(13, 45), single_stock=False)
        self.assertEqual(inside["outcome"], "DELAY")
        self.assertIn("unverified", inside["reasons"][0])
        self.assertEqual(self.engine.check("SPY", {}, self.at(11, 0), single_stock=False)["outcome"], "PASS")
        self.assertEqual(self.engine.check("SPY", {}, self.at(15, 0), single_stock=False)["outcome"], "PASS")
        self.assertEqual(self.engine.open_position_review(self.at(11, 0)), ["open_position_review:FOMC_DECISION_DAY"])

    def earn(self, d, tod=None, estimated=False):
        e = {"expected-report-date": d, "estimated": estimated}
        if tod:
            e["time-of-day"] = tod
        return {"earnings": e}

    def test_earnings_rules(self):
        now = self.at(11, 0, date(2026, 10, 29))
        self.assertEqual(self.engine.earnings_check("INTC", self.earn("2026-10-29", "AMC"), now, single_stock=True)["outcome"], "REJECT")
        self.assertEqual(self.engine.earnings_check("X", self.earn("2026-10-30", "BTO"), now, single_stock=True)["outcome"], "REJECT")
        self.assertEqual(self.engine.earnings_check("X", self.earn("2026-10-30", "AMC"), now, single_stock=True)["outcome"], "PASS")
        morning = self.engine.earnings_check("X", self.earn("2026-10-29", "BTO"), now, single_stock=True)
        self.assertEqual(morning["outcome"], "PASS")
        self.assertIn("earnings_reported_this_morning", morning["review"])
        unknown = self.engine.earnings_check("AMZN", self.earn("2026-10-30"), now, single_stock=True)
        self.assertEqual(unknown["outcome"], "REJECT")                     # unknown time: treated as both
        est = self.engine.earnings_check("X", self.earn("2026-11-20", "AMC", estimated=True), now, single_stock=True)
        self.assertEqual(est["verification"], "estimated_unverified")
        self.assertEqual(self.engine.earnings_check("X", None, now, single_stock=True)["outcome"], "REJECT")
        self.assertEqual(self.engine.earnings_check("SPY", None, now, single_stock=False)["outcome"], "PASS")

    def test_expired_calendar_fails_safe(self):
        late = self.engine.economic_check(datetime(2027, 1, 5, 11, tzinfo=ET))
        self.assertEqual(late["outcome"], "REJECT")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg().data["management"]

    def pos(self, qty=1):
        return {"trade_id": "t", "right": "C", "quantity": qty, "entry_price": 2.0, "entry_fees": 1.0,
                "entry_underlying": 100.0, "stop_underlying": 99.0, "target_underlying": 102.0,
                "entry_time": (OPEN + timedelta(minutes=20)).isoformat()}

    def step(self, p, u, bid, minutes=25, atr=0.2, c=None):
        return management.manage(p, underlying=u, atr=atr, option_bid=bid, now=OPEN + timedelta(minutes=minutes),
                                 cfg=c or self.c, atr_multiple=1.0, exit_fees_estimate=1.0, slippage_per_contract=0.02)

    def test_breakeven_arms_at_one_r_and_never_loses_the_premium_plus_costs(self):
        p = self.pos()
        self.assertIsNone(self.step(p, 100.5, 2.3)["exit_reason"])
        self.assertNotIn("breakeven_armed", p["v2_management"])
        self.step(p, 101.0, 2.5)
        floor = p["v2_management"]["breakeven_option_floor"]
        self.assertAlmostEqual(floor, 2.0 + 0.0002 + 2.0 / 100)
        self.assertEqual(self.step(p, 100.95, floor - 0.01)["exit_reason"], "v2_breakeven_stop")

    def test_trailing_stop_only_tightens(self):
        p = self.pos()
        self.step(p, 101.0, 2.5)
        first = p["v2_management"]["trail_stop_underlying"]
        self.assertAlmostEqual(first, 100.8)
        self.step(p, 101.5, 2.8)
        self.assertAlmostEqual(p["v2_management"]["trail_stop_underlying"], 101.3)
        self.step(p, 101.4, 2.7, atr=0.05)
        self.assertGreaterEqual(p["v2_management"]["trail_stop_underlying"], 101.3)
        self.assertEqual(self.step(p, 101.25, 2.6)["exit_reason"], "v2_trailing_stop")

    def test_time_stop(self):
        c = json.loads(json.dumps(self.c))
        c["time_stop"]["enabled"] = True
        p = self.pos()
        self.assertIsNone(self.step(p, 100.1, 2.0, minutes=60, c=c)["exit_reason"])
        self.assertEqual(self.step(p, 100.1, 2.0, minutes=81, c=c)["exit_reason"], "v2_time_stop_no_progress")

    def test_partial_never_sells_all(self):
        c = json.loads(json.dumps(self.c))
        c["partial_profit"]["enabled"] = True
        self.assertEqual(self.step(self.pos(1), 101.1, 2.5, c=c)["partial_quantity"], 0)
        self.assertEqual(self.step(self.pos(2), 101.1, 2.5, c=c)["partial_quantity"], 1)
        self.assertEqual(self.step(self.pos(5), 101.1, 2.5, c=c)["partial_quantity"], 2)

    def test_put_side_mirrors(self):
        p = dict(self.pos(), right="P", stop_underlying=101.0, target_underlying=98.0)
        self.step(p, 99.0, 2.5)
        self.assertAlmostEqual(p["v2_management"]["trail_stop_underlying"], 99.2)


class PipelineTests(unittest.TestCase):
    """End-to-end candidate evaluation with mocked inputs."""

    def setUp(self):
        self.c = cfg()
        self.strategy = self.c.base
        self.sm = SectorMap(self.c.sector_map)
        self.now = datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC)
        self.bars = trend_bars(OPEN, 90, 100, 0.02)
        self.snap = {"sectors": {"XLK": {"bias": 2, "fresh": True, "missing": []}}}
        self.tracker = {"state": "CONFIRMED", "actionable": True,
                        "setup": {"side": "CALL", "trigger_time": (self.bars[-3].timestamp).isoformat(),
                                  "trigger_relative_volume": 1.6, "stop_distance": 0.4, "target_distance": 0.8}}
        self.core = {"ema_fast": 101.7, "ema_slow": 101.6, "atr": 0.1, "atr_expansion": 1.0, "trend_efficiency": 0.6,
                     "relative_volume": 1.1}
        self.metrics = {"implied-volatility-index": "0.20", "tw-implied-volatility-index-rank": "0.40",
                        "historical-volatility-30-day": "12.0", "_retrieved_at_utc": self.now.isoformat(),
                        "earnings": {"expected-report-date": "2026-11-20", "time-of-day": "AMC", "estimated": False}}
        self.calls = 0

    def chain(self):
        self.calls += 1
        return [OptionQuote("NVDA  261106C00102000", "NVDA", datetime(2026, 11, 6, tzinfo=UTC), 102, "C", 4.00, 4.05, 20, 20,
                            0.55, 0.08, -0.15, 0.10, 0.20, None, 500, 5000, self.now - timedelta(seconds=2))]

    def regime(self, name="STRONG_BULLISH"):
        from trader.v2.regime import RegimeResult
        return RegimeResult(name, "R3", tuple(self.c.data["regime"]["allowed_directions"][name]), {})

    def inputs(self, **over):
        base = dict(symbol="NVDA", now=self.now, bars=self.bars, core_metrics=self.core, tracker=self.tracker,
                    snapshot=self.snap, regime=self.regime(), strategy=self.strategy, v2=self.c.data, sector_map=self.sm,
                    events=EventEngine(self.c.calendar, self.c.data["events"], next_session=lambda d: d + timedelta(days=1)),
                    metrics=self.metrics, correlation=CorrelationModel(self.c.data["correlation"], self.sm, {}),
                    risk_state=RiskState(10_000, 0, 0, 0, 0, 0), positions=[], pending=[], cash=10_000, equity=10_000,
                    profile_name="ORIGINAL_ACCOUNT_PAPER", fetch_chain=self.chain, clock=lambda: self.now)
        base.update(over)
        return pipeline.CandidateInputs(**base)

    def test_approved_report_has_all_twelve_stages(self):
        report = pipeline.evaluate_candidate(self.inputs())
        record = pipeline.report_record(report)
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        self.assertEqual(list(record["stages"]), list(pipeline.STAGES))
        self.assertEqual(report.entry["quantity"], 1)
        self.assertAlmostEqual(report.entry["stop_underlying"], self.bars[-1].close - 0.4)
        json.dumps(record)

    def test_waiting_candidate_costs_no_option_requests(self):
        waiting = dict(self.tracker, state="AWAITING_CONFIRMATION", actionable=False)
        report = pipeline.evaluate_candidate(self.inputs(tracker=waiting))
        self.assertEqual(report.outcome, "WAIT_FOR_CONFIRMATION")
        self.assertEqual(self.calls, 0)
        self.assertEqual(pipeline.report_record(report)["stages"]["option_pricing"]["status"], "NOT_EVALUATED")

    def test_with_confirmation_off_only_a_core_signal_bar_can_enter(self):
        data = json.loads(json.dumps(self.c.data)); data["confirmation"]["enabled"] = False
        waiting = dict(self.tracker, state="AWAITING_CONFIRMATION", actionable=False)
        on_signal = pipeline.evaluate_candidate(self.inputs(v2=data, tracker=waiting, core_signal_now=True))
        self.assertEqual(on_signal.outcome, "APPROVED", on_signal.reasons)
        later = pipeline.evaluate_candidate(self.inputs(v2=data, tracker=waiting, core_signal_now=False))
        self.assertEqual(later.outcome, "REJECTED")

    def test_regime_and_sector_and_correlation_rejections(self):
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(regime=self.regime("RANGE_BOUND"))).outcome, "REJECTED")
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(regime=self.regime("UNCLEAR"))).outcome, "INSUFFICIENT_DATA")
        bad_sector = {"sectors": {"XLK": {"bias": -2, "fresh": True, "missing": []}}}
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(snapshot=bad_sector)).outcome, "REJECTED")
        held = [position("AAPL", loss=1000)]
        r = pipeline.evaluate_candidate(self.inputs(positions=held))
        self.assertEqual(r.outcome, "REJECTED")
        self.assertIn("portfolio_correlation", r.reasons[0])

    def test_earnings_and_kill_switch_and_stale_data(self):
        earn = dict(self.metrics, earnings={"expected-report-date": "2026-10-26", "time-of-day": "AMC"})
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(metrics=earn)).outcome, "REJECTED")
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(entries_blocked="kill_switch_active")).outcome, "REJECTED")
        self.assertEqual(pipeline.evaluate_candidate(self.inputs(data_fresh=False)).outcome, "INSUFFICIENT_DATA")
        self.assertEqual(self.calls, 0)

    def test_original_risk_engine_still_blocks(self):
        r = pipeline.evaluate_candidate(self.inputs(risk_state=RiskState(10_000, -250, -250, 0, 0, 0)))
        self.assertEqual(r.outcome, "REJECTED")
        self.assertIn("daily_loss_limit", " ".join(r.reasons))

    def test_greek_gate_matches_original_engine(self):
        """The V2 gate is the original engine's projection: same expected net R for the same contract and signal."""
        from trader.engine import decision_cycle
        option = self.chain()[0]
        from trader.strategy_engine import evaluate_underlying
        from trader.fees import FeeSchedule
        from trader.risk_engine import assess_entry
        risk = assess_entry(state=RiskState(10_000, 0, 0, 0, 0, 0), contract_price=option.ask, strategy_risk=self.strategy.risk,
                            max_contracts=1, data_fresh=True, account_certain=True, reconciled=True, protection_verified=True,
                            estimated_entry_slippage_per_contract=0.02, estimated_exit_slippage_per_contract=0.02,
                            estimated_round_trip_fees_per_contract=FeeSchedule(0.65, 0.05).estimate(1, legs=2))
        v2 = pipeline.greek_projection(option, price=101.0, target=101.8, stop=100.6, quantity=1,
                                       risk_dollars=risk.risk_dollars, strategy=self.strategy)
        # recompute exactly as trader/engine.py does
        holding_days = 180 / 1440
        change = lambda m: option.delta * m + 0.5 * option.gamma * m * m + option.theta * holding_days
        expected_exit = max(0.0, option.ask + change(0.8) - option.spread)
        stop_exit = max(0.0, option.ask + change(-0.4) - option.spread)
        loss = min((option.ask - stop_exit) * 100 + 1.4 + 0.04, risk.risk_dollars)
        expected_r = ((expected_exit - option.ask) * 100 - 0.04 - 1.4) / loss
        self.assertAlmostEqual(v2["expected_net_r"], expected_r, places=9)


class DataHubTests(unittest.TestCase):
    def test_shared_cache_ttl(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cache = SharedCache(tmp)
        cache.put("k", {"a": 1}, source="test")
        self.assertEqual(cache.get("k")["payload"], {"a": 1})
        self.assertEqual(cache.get("k", 60)["source"], "test")
        self.assertIsNone(cache.get("k", -1))
        self.assertIsNone(cache.get("missing"))

    def test_daily_candles_are_dated_by_their_utc_session_and_today_is_excluded(self):
        from trader.v2.datahub import completed_daily
        rows = [{"timestamp": "2026-10-22T00:00:00+00:00", "open": 766.37, "high": 767.71, "low": 761.165, "close": 763.96},
                {"timestamp": "2026-10-23T00:00:00+00:00", "open": 759.9, "high": 763.47, "low": 758.68, "close": 761.06}]
        done = completed_daily(rows, date(2026, 10, 23))
        self.assertEqual([r["session_date"] for r in done], ["2026-10-22"])     # today's partial candle excluded
        self.assertEqual(done[-1]["close"], 763.96)

    def test_vix_read_just_after_the_bar_is_fresh(self):
        ctx = MarketContext(("SPY", "QQQ", "IWM"), (), 9, 21, 14, 30, 150, 180)
        bar_time = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        ctx.vix = VixReading(18.0, 17.5, bar_time + timedelta(seconds=7), "test")
        self.assertTrue(ctx.vix_fresh(bar_time))
        self.assertFalse(ctx.vix_fresh(bar_time + timedelta(minutes=4)))

    def test_request_log_masks_account_numbers(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = RequestLog(tmp / "r.jsonl")
        log("GET", "/accounts/5WX12345/positions", 200, 0.1, 1)
        log("GET", "/option-chains/SPY", 200, 0.1, 1)
        text = (tmp / "r.jsonl").read_text()
        self.assertNotIn("5WX12345", text)
        self.assertIn("***45", text)
        self.assertEqual(log.counts["/option-chains/{symbol}"], 1)

    def test_option_chain_listing_requested_once_per_day(self):
        from unittest.mock import MagicMock
        from trader.v2.datahub import DataHub
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        client = MagicMock()
        client.option_chain.return_value = {"data": {"items": [{"symbol": "X", "active": True}]}}
        hub = DataHub(client=client, shared_dir=tmp)
        hub.option_chain("SPY", date(2026, 10, 26)); hub.option_chain("SPY", date(2026, 10, 26))
        DataHub(client=client, shared_dir=tmp).option_chain("SPY", date(2026, 10, 26))   # second process: disk cache
        self.assertEqual(client.option_chain.call_count, 1)

    def test_market_metrics_refreshed_only_when_stale(self):
        from unittest.mock import MagicMock
        from trader.v2.datahub import DataHub
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        client = MagicMock()
        client.market_metrics.side_effect = lambda syms: {"data": {"items": [{"symbol": s} for s in syms]}}
        hub = DataHub(client=client, shared_dir=tmp)
        hub.market_metrics(["SPY", "QQQ"], 600); hub.market_metrics(["SPY", "QQQ"], 600)
        self.assertEqual(client.market_metrics.call_count, 1)
        hub.market_metrics(["SPY", "QQQ", "IWM"], 600)
        self.assertEqual(client.market_metrics.call_args[0][0], ["IWM"])      # only the missing symbol

    def test_history_batch_accepts_complete_and_refuses_snipped(self):
        start = datetime(2026, 10, 20, tzinfo=UTC)
        t1, t2 = int((start + timedelta(days=1)).timestamp() * 1000), int((start + timedelta(days=2)).timestamp() * 1000)
        def row(sym, t, flags):
            return ["Candle", sym, 0, t, 0, 1, 10, 11, 9, 10.5, 100, flags]
        messages = [{"data": ["Candle", row("SPY{=d}", t2, 4)[1:] and row("SPY{=d}", t2, 4)[1:]]}]
        payload = row("SPY{=d}", t2, 4) + row("SPY{=d}", t1, 8) + row("QQQ{=d}", t2, 16)

        async def feed(url, token, subs, contract):
            yield {"data": [payload]}
        got, failed = asyncio.run(history_batch(dxlink_url="u", quote_token="t",
                                                series={"SPY": "SPY{=1d}", "QQQ": "QQQ{=1d}"}, start=start, open_feed=feed))
        self.assertEqual(len(got["SPY"]), 2)
        self.assertEqual(got["SPY"][0]["close"], 10.5)
        self.assertIn("QQQ", failed)

    def test_history_batch_times_out_to_failure_not_partial(self):
        start = datetime(2026, 10, 20, tzinfo=UTC)
        t2 = int((start + timedelta(days=2)).timestamp() * 1000)

        async def feed(url, token, subs, contract):
            yield {"data": [["Candle", "SPY{=d}", 0, t2, 0, 1, 10, 11, 9, 10.5, 100, 4]]}
            await asyncio.sleep(5)
        got, failed = asyncio.run(history_batch(dxlink_url="u", quote_token="t", series={"SPY": "SPY{=1d}"},
                                                start=start, timeout_seconds=0.2, open_feed=feed))
        self.assertEqual(got, {})
        self.assertIn("SPY", failed)


class SeparationTests(unittest.TestCase):
    def test_v2_refuses_live_and_test_sessions(self):
        from trader import paper_runner
        from trader.config import AppConfig
        with self.assertRaises(RuntimeError):
            asyncio.run(paper_runner._run(root=ROOT, cfg=AppConfig(), strategy=cfg().base, symbols=("SPY",), live=object(), v2=cfg()))
        with self.assertRaises(RuntimeError):
            asyncio.run(paper_runner._run(root=ROOT, cfg=AppConfig(), strategy=cfg().base, symbols=("SPY",), test_mode=True, v2=cfg()))

    def test_original_path_signature_unchanged_by_default(self):
        import inspect
        from trader import paper_runner
        self.assertIsNone(inspect.signature(paper_runner._run).parameters["v2"].default)
        self.assertIsNone(inspect.signature(paper_runner.run_paper_session).parameters["v2"].default)

    def test_cli_v2_paper_session_requires_activation(self):
        from trader import cli
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("strategy.txt", "active_strategy.json", "strategies", "profiles", "docs"):
            src = ROOT / name
            (shutil.copytree if src.is_dir() else shutil.copy2)(src, tmp / name)
        (tmp / v2config.MANIFEST_FILE).unlink(missing_ok=True)
        import os
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            self.assertEqual(cli.main(["paper-session", "--strategy", "v2"]), 2)
        finally:
            os.chdir(cwd)

    def test_ledgers_and_shadow_are_isolated_directories(self):
        from trader.account_profiles import ProfileConfig, ProfileLedger
        small = ProfileConfig.load(ROOT, "small")
        self.assertNotEqual(small.data_dir.resolve(), (ROOT / "data").resolve())
        with self.assertRaises(Exception):
            ProfileLedger(small, original_data_dir=small.data_dir)


if __name__ == "__main__":
    unittest.main()
