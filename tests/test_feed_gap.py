"""A live-feed gap on a symbol that holds a PAPER position: retry the backfill, then exit that position at a
fresh bid and keep the rest of the session running (2026-10-16: a one-minute PDD gap stopped the whole
session and left three positions unmanaged through the close)."""
from __future__ import annotations

import asyncio
import json
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
from trader.portfolio import PaperAccount, load_or_initialize, save_account
from trader.strategy_loader import parse_strategy
from tests import test_profiles
from tests.test_profiles import make_root

OPTION = "SPY   261030C00100000"


def carried_position(strategy, entry_time: datetime) -> dict:
    return {"trade_id": "carried-spy", "symbol": OPTION, "underlying": "SPY", "right": "C", "quantity": 1,
            "entry_price": 1.00, "entry_fees": 0.70, "entry_underlying": 100.0, "stop_underlying": 50.0,
            "target_underlying": 500.0, "entry_time": entry_time.isoformat(), "max_holding_minutes": 600,
            "initial_risk_dollars": 101.44, "modeled_risk_dollars": 101.44, "planned_risk_dollars": 101.44,
            "premium_exposure_dollars": 100.0, "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.version, "strategy_hash": strategy.sha256, "last_bid": 1.0}


class FeedGapTests(unittest.TestCase):
    def _run(self, root: Path, *, gap_fetch_failures: int, quote_fresh: bool = True):
        """SPY (holding a position) and IWM. SPY's first live bar skips a minute; the backfill for that minute
        fails `gap_fetch_failures` times before the history provider can serve it."""
        now = datetime.now(timezone.utc)
        open_utc = now.replace(second=0, microsecond=0) - timedelta(minutes=22)
        template = test_profiles.ProfileLoopTests._bars()
        shift = open_utc - template[0].timestamp
        session = [Bar(b.timestamp + shift, b.open, b.high, b.low, b.close, b.volume) for b in template]
        # Startup history runs through the last completed minute; the gap is the minute after that.
        history, gap_minute = session[:-1], session[-1]
        live_bar = Bar(gap_minute.timestamp + timedelta(minutes=1), gap_minute.close, gap_minute.close + .02,
                       gap_minute.close - .02, gap_minute.close, 1000)
        fetches = {"gap": 0}

        class FakeCalendar:
            def session_bounds(self, day):
                return (open_utc.astimezone(ZoneInfo("America/New_York")),
                        (datetime.now(timezone.utc) + timedelta(seconds=3)).astimezone(ZoneInfo("America/New_York")))
            def is_open(self, at): return True
            def entry_window_ok(self, at, start, end): return False   # management only; no new entries

        class FakeClient:
            def __init__(self, *a, **k): pass
            def clock_offset_seconds(self): return 0.0
            def quote_token(self): return {"data": {"token": "t", "dxlink-url": "wss://example"}}

        class FakeQuotes:
            def __init__(self, client): pass
            def snapshots(self, symbols):
                return [CachedQuote(symbols[0], 100.0, 100.02, datetime.now(timezone.utc), {})]

        class FakeHistory:
            name = "fixture"
            async def fetch(self, *, symbol, start, end):
                rows = [b for b in history if start <= b.timestamp <= end]
                if symbol == "SPY" and start == gap_minute.timestamp:
                    fetches["gap"] += 1
                    if fetches["gap"] > gap_fetch_failures:
                        rows = [gap_minute]
                candles = [{"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low,
                            "close": b.close, "volume": b.volume} for b in rows]
                return types.SimpleNamespace(candles=candles, audit={}, provider="fixture", retrieval_method="fixture",
                                             retrieved_at_utc=now, requested_start_utc=start, requested_end_utc=end)

        async def fake_live(url, token, symbols):
            for symbol in symbols:  # IWM's feed is contiguous; SPY's skips gap_minute
                yield symbol, live_bar if symbol == "SPY" else gap_minute
            await asyncio.sleep(3600)

        def fake_marks(client, positions):
            stamp = datetime.now(timezone.utc) - (timedelta(0) if quote_fresh else timedelta(minutes=5))
            return [OptionQuote(p["symbol"], p["underlying"], now, 100.0, p["right"], 1.20, 1.25, quote_timestamp=stamp)
                    for p in positions]

        strategy = parse_strategy(root / "strategy.txt")
        no_flat = replace(strategy, execution={**strategy.execution, "flatten_at_session_close": False})
        cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
        save_account(cfg.data_dir / "paper_account.json", PaperAccount(
            starting_capital=1000, cash=898.56, positions=[carried_position(strategy, open_utc + timedelta(minutes=5))]))
        error = None
        with patch.object(paper_runner, "MarketCalendar", FakeCalendar), \
                patch.object(paper_runner, "TastytradeClient", FakeClient), \
                patch.object(paper_runner, "ProductionMarketData", FakeQuotes), \
                patch.object(paper_runner, "create_history_provider", lambda *a, **k: FakeHistory()), \
                patch.object(paper_runner, "live_minute_bars", fake_live), \
                patch.object(paper_runner, "_option_marks", fake_marks), \
                patch.object(paper_runner, "GAP_RECOVERY_RETRY_SECONDS", 0.01), \
                patch.object(paper_runner, "ENTRY_ARBITRATION_SECONDS", 0.05), \
                patch("trader.notifications.email_configured", return_value=False), \
                patch("trader.notifications.push_configured", return_value=False):
            try:
                asyncio.run(paper_runner._run(root=root, cfg=replace(cfg, starting_cash=1000), strategy=no_flat,
                                              symbols=("SPY", "IWM")))
            except RuntimeError as exc:
                error = exc
            finally:
                paper_runner._close_session_log_file(paper_runner._session_logger(cfg.data_dir))
        account = load_or_initialize(cfg.data_dir / "paper_account.json", 1000)
        alerts_path = cfg.data_dir / "critical_alerts.jsonl"
        alerts = [json.loads(line) for line in alerts_path.read_text().splitlines()] if alerts_path.exists() else []
        return account, alerts, error, fetches

    def test_unrecoverable_gap_exits_the_position_and_the_session_keeps_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, alerts, error, fetches = self._run(make_root(Path(tmp)), gap_fetch_failures=99)
        self.assertIsNone(error)  # the session reached its close instead of failing
        self.assertEqual(fetches["gap"], paper_runner.GAP_RECOVERY_ATTEMPTS)
        self.assertEqual(account.positions, [])
        [close] = [r for r in account.ledger if r.get("type") == "paper_close"]
        self.assertEqual(close["reason"], "data_failure_protective_exit")
        self.assertAlmostEqual(close["realized_pnl"], (1.20 - .0002 - 1.00) * 100 - 0.70 - 0.70, places=2)
        events = [a["event"] for a in alerts]
        self.assertIn("paper_position_protective_exit", events)
        self.assertIn("paper_symbol_disabled", events)

    def test_gap_recovered_on_retry_keeps_the_position_open_and_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, alerts, error, fetches = self._run(make_root(Path(tmp)), gap_fetch_failures=1)
        self.assertIsNone(error)
        self.assertEqual(fetches["gap"], 2)
        self.assertEqual([p["trade_id"] for p in account.positions], ["carried-spy"])
        self.assertEqual(account.positions[0]["last_bid"], 1.20)  # managed on the live bar after recovery
        self.assertNotIn("paper_symbol_disabled", [a["event"] for a in alerts])

    def test_without_a_fresh_quote_the_position_stays_and_the_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            account, alerts, error, _ = self._run(make_root(Path(tmp)), gap_fetch_failures=99, quote_fresh=False)
        self.assertIsNotNone(error)
        self.assertIn("live-feed gap could not be recovered", str(error))
        self.assertEqual([p["trade_id"] for p in account.positions], ["carried-spy"])

    def test_failed_heartbeat_reports_the_ledger_open_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            save_account(data / "paper_account.json", PaperAccount(starting_capital=1000, cash=1000,
                                                                   positions=[{"trade_id": "a"}, {"trade_id": "b"}]))
            self.assertEqual(paper_runner._ledger_position_count(data), 2)
            self.assertEqual(paper_runner._ledger_position_count(data / "missing"), 0)


if __name__ == "__main__":
    unittest.main()
