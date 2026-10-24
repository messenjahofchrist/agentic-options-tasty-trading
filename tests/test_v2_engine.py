"""End-to-end V2 engine test through trader.paper_runner._run with MOCKED tastytrade/DXLink (no network):
breakout -> confirmation -> twelve-stage approval -> PAPER fill with V2 fields -> management on the next bar, the
original-strategy shadow on the same bars, context symbols streamed but never traded, and LIVE refused."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader import paper_runner
from trader.config import AppConfig, Mode
from trader.market_data import CachedQuote
from trader.models import Bar, OptionQuote
from trader.portfolio import load_or_initialize
from trader.strategy_loader import parse_strategy
from trader.v2 import config as v2config
from trader.v2 import datahub
from tests import test_profiles
from tests.test_profiles import make_root

ROOT = Path(__file__).resolve().parents[1]


class V2EngineTests(unittest.TestCase):
    def _session(self, tmp: Path):
        root = make_root(tmp, universe=["SPY"])
        shutil.copytree(ROOT / "strategies", root / "strategies")
        (root / v2config.MANIFEST_FILE).unlink(missing_ok=True)
        config = v2config.load_v2_config(root)
        v2config.write_v2_manifest(root, config, approved_by="test", note="engine test")
        now = datetime.now(timezone.utc)
        open_utc = now.replace(second=0, microsecond=0) - timedelta(minutes=22)
        template = test_profiles.ProfileLoopTests._bars()
        shift = open_utc - template[0].timestamp
        session = [Bar(b.timestamp + shift, b.open, b.high, b.low, b.close, b.volume) for b in template]
        history, breakout = session[:-1], session[-1]
        confirm = Bar(breakout.timestamp + timedelta(minutes=1), 100.40, 100.62, 100.38, 100.60, 1500)
        follow = Bar(confirm.timestamp + timedelta(minutes=1), 100.60, 100.70, 100.55, 100.65, 1200)
        requests = {"option_chain": 0, "metrics": 0, "vix": 0}

        class FakeCalendar:
            def session_bounds(self, day):
                return (open_utc.astimezone(ZoneInfo("America/New_York")),
                        (datetime.now(timezone.utc) + timedelta(seconds=6)).astimezone(ZoneInfo("America/New_York")))
            def is_open(self, at): return True
            def entry_window_ok(self, at, start, end): return True

        class FakeClient:
            def __init__(self, *a, **k): pass
            def clock_offset_seconds(self): return 0.0
            def quote_token(self): return {"data": {"token": "t", "dxlink-url": "wss://example"}}
            def market_metrics(self, symbols):
                requests["metrics"] += 1
                return {"data": {"items": [{"symbol": s, "implied-volatility-index": "0.20", "tw-implied-volatility-index-rank": "0.40",
                                            "historical-volatility-30-day": "12.0", "earnings": {"visible": False}} for s in symbols]}}
            def option_chain(self, underlying):
                requests["option_chain"] += 1
                return {"data": {"items": [{"symbol": f"{underlying:<6}261106C00100000", "active": True, "days-to-expiration": 14,
                                            "strike-price": "100", "option-type": "C", "expiration-date": "2026-11-06"}]}}
            def quotes(self, query):
                stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                if "index" in query:
                    requests["vix"] += 1
                    return {"data": {"items": [{"symbol": "VIX", "last": "17.5", "updated-at": stamp}]}}
                return {"data": {"items": [{"symbol": "SPY   261106C00100000", "bid": "1.95", "ask": "2.00", "bid-size": "20",
                                            "ask-size": "20", "delta": "0.55", "gamma": "0.01", "theta": "-0.02", "vega": "0.1",
                                            "volatility": "0.20", "volume": "500", "open-interest": "1000", "updated-at": stamp}]}}

        class FakeQuotes:
            def __init__(self, client): pass
            def snapshots(self, symbols):
                return [CachedQuote(symbols[0], 100.0, 100.02, datetime.now(timezone.utc), {})]

        class FakeHistory:
            name = "fixture"
            async def fetch(self, *, symbol, start, end):
                rows = [{"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "volume": b.volume} for b in history if start <= b.timestamp <= end]
                return types.SimpleNamespace(candles=rows, audit={}, provider="fixture", retrieval_method="fixture",
                                             retrieved_at_utc=now, requested_start_utc=start, requested_end_utc=end)

        async def fake_batch(*, dxlink_url, quote_token, series, start, timeout_seconds=90.0, open_feed=None):
            out = {}
            for symbol, candle in series.items():
                if "=1d" in candle:      # 61 synthetic sessions, the same path for every symbol (corr 1.0)
                    base = datetime(2026, 7, 1, 4, tzinfo=timezone.utc)
                    out[symbol] = [{"timestamp": (base + timedelta(days=i)).isoformat(), "open": 100 + i % 3, "high": 102 + i % 3,
                                    "low": 99 + i % 3, "close": 100.5 + (i % 3), "volume": 1e6} for i in range(61)]
                else:
                    out[symbol] = [{"timestamp": (open_utc - timedelta(minutes=30)).isoformat(), "open": 99.5, "high": 99.8,
                                    "low": 99.3, "close": 99.6, "volume": 100}]
            return out, {}

        async def fake_live(url, token, symbols):
            for item in (breakout, confirm, follow):
                for symbol in symbols:
                    yield symbol, item
                await asyncio.sleep(0.3)
            await asyncio.sleep(3600)

        def fake_marks(client, positions):
            return [OptionQuote(p["symbol"], p["underlying"], now, 100.0, p["right"], 2.10, 2.15,
                                quote_timestamp=datetime.now(timezone.utc)) for p in positions]

        strategy = parse_strategy(root / "strategy.txt")
        no_flat = replace(strategy, execution={**strategy.execution, "flatten_at_session_close": False})
        # The fixture's opening bars span 0.6 on a $100 price; lower its price volatility so the SPY-ATR guard does
        # not classify the synthetic session as disorderly (the guard itself is unit-tested separately).
        data = json.loads(json.dumps(config.data))
        data["regime"]["spy_disorderly_atr_fraction"] = 0.05
        config = replace(config, base=no_flat, data=data)
        cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
        with patch.object(paper_runner, "MarketCalendar", FakeCalendar), \
                patch.object(paper_runner, "TastytradeClient", FakeClient), \
                patch.object(paper_runner, "ProductionMarketData", FakeQuotes), \
                patch.object(paper_runner, "create_history_provider", lambda *a, **k: FakeHistory()), \
                patch.object(paper_runner, "live_minute_bars", fake_live), \
                patch.object(paper_runner, "_option_marks", fake_marks), \
                patch.object(paper_runner, "ENTRY_ARBITRATION_SECONDS", 0.05), \
                patch.object(datahub, "history_batch", fake_batch), \
                patch("trader.notifications.email_configured", return_value=False), \
                patch("trader.notifications.push_configured", return_value=False):
            try:
                asyncio.run(paper_runner._run(root=root, cfg=replace(cfg, starting_cash=10_000), strategy=no_flat,
                                              symbols=("SPY",), v2=config))
            finally:
                paper_runner._close_session_log_file(paper_runner._session_logger(cfg.data_dir))
        data = cfg.data_dir
        rows = [json.loads(l) for l in (data / "v2_decisions.jsonl").read_text().splitlines()]
        account = load_or_initialize(data / "paper_account.json", 10_000)
        shadow = load_or_initialize(data / "v2_shadow_original" / "paper_account.json", 10_000)
        return rows, account, shadow, requests, data

    def test_breakout_is_confirmed_approved_filled_and_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, account, shadow, requests, data = self._session(Path(tmp))
            snapshot = json.loads((data / "v2_context_snapshot.json").read_text())
            status = json.loads((data / "v2_status.json").read_text())
        candidates = [r for r in rows if r.get("type") == "candidate"]
        outcomes = [r["outcome"] for r in candidates]
        self.assertIn("WAIT_FOR_CONFIRMATION", outcomes, "the breakout bar alone must not trade")
        self.assertIn("APPROVED", outcomes, [r["reasons"] for r in candidates])
        approved = next(r for r in candidates if r["outcome"] == "APPROVED")
        self.assertEqual(len(approved["stages"]), 12)
        self.assertTrue(all(s["status"] in ("PASS", "NOT_APPLICABLE", "APPROVED") for s in approved["stages"].values()))
        # filled once, with the V2 identity and the original strategy's stop/target distances
        self.assertEqual(len(account.positions), 1)
        position = account.positions[0]
        self.assertEqual(position["strategy_id"], "PROFESSIONAL_STRATEGY_V2")
        self.assertEqual(position["base_strategy_hash"], parse_strategy(ROOT / "strategy.txt").sha256)
        self.assertIn(position["v2_entry"]["setup_type"], ("CONFIRMED", "PULLBACK_CONFIRMED"))
        self.assertEqual(position["quantity"], 1)
        self.assertIn("best_underlying", position["v2_management"])   # managed on the following bar
        self.assertTrue(any(r.get("type") == "entry" for r in rows))
        # context symbols were streamed and summarised but never traded
        self.assertEqual({p["underlying"] for p in account.positions}, {"SPY"})
        self.assertTrue(snapshot["sectors"]["XLK"]["bars"] > 0)
        self.assertEqual(status["daily_history"]["failures"], {})
        # the original strategy benchmark traded the breakout bar itself, on its own ledger, no orders
        self.assertEqual(len(shadow.positions), 1)
        self.assertLess(shadow.positions[0]["entry_time"], position["entry_time"])
        # cached: the chain listing once per symbol per day
        self.assertEqual(requests["option_chain"], 1)

    def test_v2_with_live_execution_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), universe=["SPY"])
            shutil.copytree(ROOT / "strategies", root / "strategies")
            config = v2config.load_v2_config(root)
            with self.assertRaises(RuntimeError):
                asyncio.run(paper_runner._run(root=root, cfg=AppConfig(data_dir=root / "data"), strategy=config.base,
                                              symbols=("SPY",), live=object(), v2=config))


if __name__ == "__main__":
    unittest.main()
