from __future__ import annotations

import asyncio
import json
import io
import os
import tempfile
import urllib.error
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader.config import AppConfig, Mode
from trader.config import load_dotenv
from trader.calendar import MarketCalendar
from trader.backtest import load_bars
from trader.fees import FeeSchedule, economic_gate
from trader.metrics import calculate_performance
from trader.models import Bar, OptionQuote, Position
from trader.option_selector import evaluate_options, select_option
from trader.market_data import (CachedQuote, fetch_historical_candles, fetch_massive_historical_candles,
                                parse_candle_feed)
from trader.history_providers import (_normalize_rows, create_history_provider,
                                     MassiveHistoryProvider, TastytradeDXLinkHistoryProvider)
from trader.paper_execution import PaperExecutor, realized_option_pnl
from trader.paper_runner import (_classify_live_transition, _fresh_underlying_quote,
                                 _same_market_bar, _validated_session_history,
                                 _write_heartbeat, _write_recovery_checkpoint)
from trader.portfolio import PaperAccount, load_or_initialize
from trader.position_manager import exit_reason
from trader.reconciliation import reconcile_positions
from trader.risk_engine import RiskState, assess_entry
from trader.strategy_engine import atr, ema, evaluate_underlying
from trader.strategy_loader import StrategyError, parse_strategy, verify_manifest
from trader.strategy_loader import write_manifest
from trader.tastytrade_client import TastytradeClient
from trader.engine import decision_cycle
from trader.paper_runner import run_paper_test_session
from trader.live_execution import LiveExecution
from trader.watchdog import AgentLease, KillSwitch
from trader.supervisor import watchdog_check


ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.strategy = parse_strategy(ROOT / "strategy.txt")

    def test_strategy_parses_and_hash_manifest_matches(self):
        manifest = verify_manifest(ROOT / "strategy.txt", ROOT / "active_strategy.json")
        self.assertEqual(manifest["sha256"], self.strategy.sha256)
        self.assertEqual(manifest["configuration_hash"], self.strategy.sha256)
        self.assertEqual(manifest["configuration"], self.strategy.configuration)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "strategy.txt"
            changed.write_text((ROOT / "strategy.txt").read_text(encoding="utf-8").replace(
                '"max_spread_dollars": 0.1', '"max_spread_dollars": 0.11'), encoding="utf-8")
            self.assertNotEqual(parse_strategy(changed).sha256, self.strategy.sha256)

    def test_paper_entry_window_runs_until_actual_regular_or_early_close(self):
        calendar = MarketCalendar()
        eastern = ZoneInfo("America/New_York")
        self.assertEqual(self.strategy.version, "0.1.6")
        self.assertEqual(self.strategy.entry_window_et, {"start": "09:30", "end": "16:00"})
        self.assertIs(self.strategy.execution["enable_time_exit"], False)
        self.assertIs(self.strategy.execution["flatten_at_session_close"], True)
        for hour, minute in ((9, 30), (9, 44), (14, 31), (15, 45), (15, 59)):
            with self.subTest(hour=hour, minute=minute):
                self.assertTrue(calendar.entry_window_ok(
                    datetime(2026, 10, 13, hour, minute, tzinfo=eastern),
                    self.strategy.entry_window_et["start"], self.strategy.entry_window_et["end"]))
        self.assertFalse(calendar.entry_window_ok(
            datetime(2026, 10, 13, 16, 0, tzinfo=eastern),
            self.strategy.entry_window_et["start"], self.strategy.entry_window_et["end"]))

        early_close = date(2026, 11, 27)
        self.assertEqual(calendar.session_bounds(early_close)[1].hour, 13)
        self.assertTrue(calendar.entry_window_ok(
            datetime(2026, 11, 27, 12, 59, tzinfo=eastern),
            self.strategy.entry_window_et["start"], self.strategy.entry_window_et["end"]))
        self.assertFalse(calendar.entry_window_ok(
            datetime(2026, 11, 27, 13, 0, tzinfo=eastern),
            self.strategy.entry_window_et["start"], self.strategy.entry_window_et["end"]))

    def test_strategy_rejects_unknown_or_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.txt"
            path.write_text('{"version":"x","unknown":true}', encoding="utf-8")
            with self.assertRaises(StrategyError): parse_strategy(path)

    def test_ema_atr_need_sufficient_bars(self):
        self.assertEqual(ema([1, 2, 3], 2), 2.5)
        with self.assertRaises(ValueError): atr([], 14)

    def test_missing_backtest_csv_explains_required_real_data(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "bars.csv"
            with self.assertRaisesRegex(FileNotFoundError, "real 1-minute OHLCV file"):
                load_bars(missing)

    def test_dotenv_loads_local_credentials_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OPTIOBOT_TEST_SECRET=local-value\nOPTIOBOT_TEST_EXISTING=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPTIOBOT_TEST_EXISTING": "process-value"}, clear=False):
                load_dotenv(path)
                self.assertEqual(os.environ["OPTIOBOT_TEST_SECRET"], "local-value")
                self.assertEqual(os.environ["OPTIOBOT_TEST_EXISTING"], "process-value")
                os.environ.pop("OPTIOBOT_TEST_SECRET", None)

    def test_candle_feed_filters_requested_range_and_requires_complete_snapshot(self):
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        symbol = "SPY{=1m,a=s,tho=true}"
        latest = ["Candle", symbol, 0, int(end.timestamp() * 1000), 0, 1, 470.5, 472, 470, 471.5, 900, 4]
        oldest = ["Candle", symbol, 0, int(start.timestamp() * 1000), 0, 1, 470, 471, 469, 470.5, 1000, 8]
        audit = {}
        rows = parse_candle_feed([{"data": ["Candle", latest, "Candle", oldest]}], start=start,
                                 end=end - timedelta(milliseconds=1), symbol="SPY", audit=audit)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 470.5)
        self.assertTrue(audit["snapshot_begin"])
        self.assertTrue(audit["snapshot_end"])
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            parse_candle_feed([{"data": ["Candle", oldest[:-1] + [16]]}], start=start, end=end, symbol="SPY")
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            parse_candle_feed([{"data": ["Candle", latest]}], start=start, end=end, symbol="SPY")

    def test_candle_parser_detects_out_of_order_and_conflicting_duplicate_rows(self):
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        symbol = "SPY{=1m,a=s,tho=true}"
        older = ["Candle", symbol, 0, int(start.timestamp() * 1000), 0, 1, 470, 471, 469, 470.5, 1000, 4]
        newer = ["Candle", symbol, 0, int((start + timedelta(minutes=1)).timestamp() * 1000), 0, 1, 470, 472, 469, 471, 1000, 8]
        with self.assertRaisesRegex(RuntimeError, "out of order"):
            parse_candle_feed([{"data": ["Candle", older, "Candle", newer]}], start=start,
                              end=start + timedelta(minutes=1), symbol="SPY")
        begin = ["Candle", symbol, 0, int((start + timedelta(minutes=2)).timestamp() * 1000), 0, 1,
                 469, 472, 468, 470, 1000, 4]
        duplicate = ["Candle", symbol, 0, int((start + timedelta(minutes=1)).timestamp() * 1000), 0, 1,
                     470, 472, 469, 471, 1000, 0]
        conflict = duplicate.copy(); conflict[9] = 471.25; conflict[11] = 8
        with self.assertRaisesRegex(RuntimeError, "conflicting duplicate"):
            parse_candle_feed([{"data": ["Candle", begin, "Candle", duplicate, "Candle", conflict]}], start=start,
                              end=start + timedelta(minutes=2), symbol="SPY")

    def test_massive_403_reports_provider_entitlement_without_secret(self):
        start = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
        error_body = json.dumps({"status": "NOT_AUTHORIZED", "message": "Your plan doesn't include this data timeframe"}).encode()
        response = urllib.error.HTTPError("https://api.massive.com", 403, "Forbidden", {}, io.BytesIO(error_body))
        with patch("urllib.request.urlopen", side_effect=response):
            with self.assertRaisesRegex(RuntimeError, r"403 \(NOT_AUTHORIZED\).*doesn't include this data timeframe") as caught:
                fetch_massive_historical_candles(api_key="test-secret", symbol="SPY", start=start,
                                                 end=start + timedelta(minutes=1))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_dxlink_history_uses_epoch_milliseconds_and_history_contract(self):
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        symbol = "SPY{=1m,a=s,tho=true}"
        newest = ["Candle", symbol, 0, int(end.timestamp() * 1000), 0, 1, 470.5, 472, 470, 471.5, 900, 4]
        oldest = ["Candle", symbol, 0, int(start.timestamp() * 1000), 0, 1, 470, 471, 469, 470.5, 1000, 8]
        captured = {}

        async def fake_events(url, token, subscriptions, *, contract):
            captured.update({"contract": contract, "subscription": subscriptions[0]})
            yield {"type": "FEED_DATA", "data": ["Candle", newest]}
            yield {"type": "FEED_DATA", "data": ["Candle", oldest]}

        with patch("trader.market_data.dxlink_events", fake_events):
            rows = asyncio.run(fetch_historical_candles(dxlink_url="wss://example", quote_token="test",
                symbol="SPY", start=start, end=end, timeout_seconds=1))
        self.assertEqual(captured["contract"], "HISTORY")
        self.assertEqual(captured["subscription"]["fromTime"], int(start.timestamp() * 1000))
        self.assertEqual(len(rows), 2)

    def test_dxlink_history_timeout_is_explicit_and_audited(self):
        start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
        audit = {}

        async def silent_events(*args, **kwargs):
            await asyncio.sleep(1)
            if False:
                yield {}

        with patch("trader.market_data.dxlink_events", silent_events):
            with self.assertRaisesRegex(TimeoutError, "snapshot completion"):
                asyncio.run(fetch_historical_candles(dxlink_url="wss://example", quote_token="test",
                    symbol="SPY", start=start, end=start + timedelta(minutes=2),
                    timeout_seconds=0.01, audit=audit))
        self.assertFalse(audit["snapshot_end"])
        self.assertEqual(audit["expected_candle_count"], 3)

    def test_late_paper_start_requires_complete_contiguous_history(self):
        start = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
        rows = [{"timestamp": (start + timedelta(minutes=i)).isoformat(), "open": 500 + i,
                 "high": 501 + i, "low": 499 + i, "close": 500.5 + i, "volume": 1000}
                for i in range(3)]
        bars = _validated_session_history(rows, session_open=start,
                                          last_completed_bar=start + timedelta(minutes=2))
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0].timestamp, start)
        self.assertEqual(bars[-1].close, 502.5)
        with self.assertRaisesRegex(RuntimeError, "bootstrap incomplete"):
            _validated_session_history([rows[0], rows[2]], session_open=start,
                                       last_completed_bar=start + timedelta(minutes=2))

    def test_history_provider_selection_is_explicit_and_never_silently_fails_over(self):
        tasty = create_history_provider("tastytrade_dxlink", dxlink_url="wss://example", quote_token="test",
                                        massive_api_key="")
        self.assertIsInstance(tasty, TastytradeDXLinkHistoryProvider)
        with self.assertRaisesRegex(RuntimeError, "requires MASSIVE_API_KEY"):
            create_history_provider("massive", dxlink_url="wss://example", quote_token="test",
                                    massive_api_key="")

    def test_history_provider_normalization_rejects_duplicates_and_out_of_order_rows(self):
        start = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
        rows = [{"timestamp": start.isoformat(), "open": 500, "high": 501, "low": 499,
                 "close": 500.5, "volume": 1000}]
        normalized = _normalize_rows(rows, symbol="SPY", start=start,
                                     end=start + timedelta(minutes=2), provider="fixture")
        self.assertEqual(normalized[0]["source_provider"], "fixture")
        self.assertEqual(normalized[0]["interval_minutes"], 1)
        with self.assertRaisesRegex(RuntimeError, "duplicate candle"):
            _normalize_rows(rows * 2, symbol="SPY", start=start,
                            end=start + timedelta(minutes=2), provider="fixture")
        later = {**rows[0], "timestamp": (start + timedelta(minutes=1)).isoformat()}
        with self.assertRaisesRegex(RuntimeError, "out-of-order"):
            _normalize_rows([later, rows[0]], symbol="SPY", start=start,
                            end=start + timedelta(minutes=2), provider="fixture")

    def test_massive_history_provider_uses_shared_normalized_batch(self):
        start = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
        rows = [{"timestamp": start.isoformat(), "open": 500, "high": 501, "low": 499,
                 "close": 500.5, "volume": 1000}]
        provider = MassiveHistoryProvider(api_key="test-key")
        with patch("trader.history_providers.fetch_massive_historical_candles", return_value=rows):
            batch = asyncio.run(provider.fetch(symbol="SPY", start=start, end=start + timedelta(minutes=1)))
        self.assertEqual(batch.provider, "massive")
        self.assertEqual(batch.candles[0]["symbol"], "SPY")
        self.assertEqual(batch.retrieved_at_utc.tzinfo, timezone.utc)

    def test_heartbeat_writer_distinguishes_recovering_running_and_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine_heartbeat.json"
            _write_heartbeat(path, status="recovering", symbol="SPY")
            recovering = json.loads(path.read_text())
            self.assertEqual(recovering["lifecycle_state"], "RECOVERING")
            self.assertTrue(recovering["expected_running"])
            self.assertIn("startup_deadline_utc", recovering)
            _write_heartbeat(path, status="stopped", symbol="SPY")
            stopped = json.loads(path.read_text())
            self.assertEqual(stopped["lifecycle_state"], "STOPPED")
            self.assertFalse(stopped["expected_running"])

    def test_recovery_checkpoint_and_historical_live_join_are_auditable(self):
        stamp = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = _write_recovery_checkpoint(Path(directory), symbol="SPY", trading_date="2026-10-12",
                                              provider="fixture", timestamp=stamp)
            record = json.loads(path.read_text())
            self.assertEqual(record["last_validated_timestamp_utc"], stamp.isoformat())
            self.assertEqual(record["provider"], "fixture")
        prior = Bar(stamp, 500, 501, 499, 500.5, 1000)
        duplicate = Bar(stamp, 500, 501, 499, 500.5, 1000)
        conflicting = Bar(stamp, 500, 501, 499, 500.4, 1000)
        self.assertTrue(_same_market_bar(prior, duplicate))
        self.assertFalse(_same_market_bar(prior, conflicting))
        self.assertEqual(_classify_live_transition(stamp, stamp + timedelta(minutes=1)), "CONTIGUOUS")
        self.assertEqual(_classify_live_transition(stamp, stamp + timedelta(minutes=2)), "GAP")

    def test_watchdog_does_not_call_intentionally_stopped_engine_a_failure(self):
        now = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "engine_expectation.json").write_text(json.dumps({
                "expected_running": False, "state": "STOPPED"}), encoding="utf-8")
            result = watchdog_check(root=root, data_dir=data, now=now)
            self.assertEqual(result["status"], "not_expected_to_run")
            self.assertFalse((root / "KILL_SWITCH").exists())

    def test_watchdog_allows_bounded_recovering_state_without_clearing_switch(self):
        now = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (root / "KILL_SWITCH").write_text(json.dumps({"blocked": True, "reason": "owner hold"}), encoding="utf-8")
            (data / "engine_expectation.json").write_text(json.dumps({
                "expected_running": True, "state": "RECOVERING"}), encoding="utf-8")
            (data / "engine_heartbeat.json").write_text(json.dumps({
                "status": "recovering", "lifecycle_state": "RECOVERING", "expected_running": True,
                "updated_at_utc": now.isoformat(), "startup_deadline_utc": (now + timedelta(minutes=5)).isoformat()
            }), encoding="utf-8")
            result = watchdog_check(root=root, data_dir=data, now=now)
            self.assertEqual(result["status"], "startup_in_progress")
            self.assertTrue(KillSwitch(root / "KILL_SWITCH").entries_blocked())

    def test_watchdog_blocks_when_expected_running_heartbeat_is_stale(self):
        now = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "engine_expectation.json").write_text(json.dumps({
                "expected_running": True, "state": "RUNNING"}), encoding="utf-8")
            (data / "engine_heartbeat.json").write_text(json.dumps({
                "status": "running", "lifecycle_state": "RUNNING", "expected_running": True,
                "updated_at_utc": (now - timedelta(minutes=3)).isoformat()
            }), encoding="utf-8")
            with patch("trader.supervisor._email_configured", return_value=False):
                result = watchdog_check(root=root, data_dir=data, now=now)
            self.assertEqual(result["status"], "critical")
            self.assertTrue(KillSwitch(root / "KILL_SWITCH").entries_blocked())

    def test_underlying_quote_freshness_uses_snapshot_time_and_rejects_future_or_stale(self):
        now = datetime.now(timezone.utc)
        fresh = CachedQuote("SPY", 500.0, 500.1, now - timedelta(seconds=2), {})
        # Clock skew: on 2026-10-13 fresh quotes arrived 0.25-0.49s "early" and were wrongly rejected.
        skewed = CachedQuote("SPY", 500.0, 500.1, now + timedelta(seconds=0.5), {})
        future = CachedQuote("SPY", 500.0, 500.1, now + timedelta(seconds=5), {})
        stale = CachedQuote("SPY", 500.0, 500.1, now - timedelta(seconds=21), {})
        self.assertTrue(_fresh_underlying_quote(fresh, now))
        self.assertTrue(_fresh_underlying_quote(skewed, now))
        self.assertFalse(_fresh_underlying_quote(future, now))
        self.assertFalse(_fresh_underlying_quote(stale, now))

    def _option(self, **kwargs):
        base = dict(symbol="SPY 261016C00650000", underlying="SPY", expiration=datetime.now(timezone.utc) + timedelta(days=12),
                    strike=650, right="C", bid=4.0, ask=4.05, bid_size=20, ask_size=20, delta=.55, gamma=.01, theta=-.02,
                    vega=.1, iv=.25, volume=500, open_interest=1000, quote_timestamp=datetime.now(timezone.utc))
        return OptionQuote(**(base | kwargs))

    def test_option_selector_filters_liquidity_dte_delta_and_spread(self):
        best = self._option()
        rejected = self._option(symbol="illiquid", volume=0)
        selected = select_option([rejected, best], "CALL", datetime.now(timezone.utc), self.strategy)
        self.assertEqual(selected.quote.symbol, best.symbol)
        self.assertIsNone(select_option([best], "PUT", datetime.now(timezone.utc), self.strategy))

    def test_option_absolute_and_percentage_spread_limits_both_apply(self):
        now = datetime.now(timezone.utc)
        too_wide_dollars = self._option(bid=.42, ask=.56)
        selected, audit = evaluate_options([too_wide_dollars], "CALL", now, self.strategy)
        self.assertIsNone(selected)
        self.assertIn("spread_dollars_exceeded", audit[0]["rejection_reasons"])
        five_percent = self._option(bid=1.00, ask=1.05)
        selected, audit = evaluate_options([five_percent], "CALL", now, self.strategy)
        self.assertIsNotNone(selected)
        self.assertAlmostEqual(audit[0]["spread_fraction"] * 100, 100 * .05 / 1.025)
        twelve_percent = self._option(bid=.66, ask=.74)
        selected, audit = evaluate_options([twelve_percent], "CALL", now, self.strategy)
        self.assertIsNone(selected)
        self.assertIn("spread_percentage_exceeded", audit[0]["rejection_reasons"])

    def test_premium_risk_includes_round_trip_costs_and_never_uses_atr(self):
        one_percent = {**self.strategy.risk, "max_risk_per_trade_fraction": 0.01,
                       "hard_max_risk_per_trade_fraction": 0.01}
        costs = dict(estimated_entry_slippage_per_contract=.02,
                     estimated_exit_slippage_per_contract=.02,
                     estimated_round_trip_fees_per_contract=1.40)
        over = assess_entry(state=RiskState(30_000, 0, 0, 0, 0, 0), contract_price=3.0,
                            strategy_risk=one_percent, max_contracts=1, data_fresh=True,
                            account_certain=True, reconciled=True, protection_verified=True, **costs)
        self.assertFalse(over.allowed)
        self.assertEqual(over.premium_exposure_dollars, 300.0)
        self.assertAlmostEqual(over.modeled_risk_dollars, 301.44)
        self.assertEqual(over.reason, "premium_risk_budget_exceeded")
        under = assess_entry(state=RiskState(50_000, 0, 0, 0, 0, 0), contract_price=3.0,
                             strategy_risk=one_percent, max_contracts=1, data_fresh=True,
                             account_certain=True, reconciled=True, protection_verified=True, **costs)
        self.assertTrue(under.allowed)
        self.assertEqual(under.quantity, 1)
        self.assertAlmostEqual(under.risk_dollars, 301.44)
        # No ATR input is accepted by the sizing function: ATR is an exit trigger only.

    def test_risk_gate_blocks_daily_weekly_streak_trade_count_and_position_limits(self):
        # Pin caps so the test checks the risk-engine logic independent of the live strategy values.
        risk = {**self.strategy.risk, "max_consecutive_losses": 3, "max_trades_per_day": 3, "max_open_positions": 2}
        cases = [
            (RiskState(10_000, -200, 0, 0, 0, 0), "daily_loss_limit"),
            (RiskState(10_000, 0, -500, 0, 0, 0), "weekly_loss_limit"),
            (RiskState(10_000, 0, 0, 3, 0, 0), "consecutive_loss_limit"),
            (RiskState(10_000, 0, 0, 0, 3, 0), "trade_count_limit"),
            (RiskState(10_000, 0, 0, 0, 0, 2), "open_position_limit"),
            (RiskState(10_000, 0, 0, 0, 0, 1, ("SPY",)), "underlying_position_already_open_no_averaging"),
        ]
        for state, reason in cases:
            result = assess_entry(state=state, contract_price=1, strategy_risk=risk,
                                  max_contracts=1, data_fresh=True, account_certain=True,
                                  reconciled=True, protection_verified=True, underlying="SPY")
            self.assertFalse(result.allowed)
            self.assertIn(reason, result.reason)

    def test_actual_option_pnl_uses_execution_prices_and_costs(self):
        account = PaperAccount(10_000, 10_000)
        executor = PaperExecutor(account, slippage_per_contract=.02)
        entry_quote = self._option(bid=2.90, ask=3.00)
        exit_quote = self._option(bid=.50, ask=.60)
        entry = executor.fill(entry_quote, "BUY", 1, "entry")
        exit_fill = executor.fill(exit_quote, "SELL", 1, "exit")
        realized = realized_option_pnl(entry_fill_price=entry.fill_price, exit_fill_price=exit_fill.fill_price,
                                       quantity=1, entry_fees=entry.fees, exit_fees=exit_fill.fees)
        self.assertAlmostEqual(realized, -251.44)
        self.assertAlmostEqual(account.realized_pnl, 0)  # Realized result is persisted by the position lifecycle.
        position = Position("t", "option", "SPY", "C", 1, entry.fill_price, 100, 99.0, 102.0,
                            datetime.now(timezone.utc), 180, 301.44, "0.1.0", self.strategy.sha256)
        self.assertEqual(exit_reason(position, underlying=98.9, option_bid=exit_quote.bid,
                                     now=datetime.now(timezone.utc)), "underlying_invalidation_stop")
        self.assertEqual(exit_reason(position, underlying=100.0, option_bid=.01,
                                     now=datetime.now(timezone.utc), daily_loss_hit=True), "emergency_daily_risk_exit")

    def test_breakout_lookback_accepts_price_still_beyond_level_after_recent_cross(self):
        from dataclasses import replace
        start = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(15):
            close = 99.60 + index * .02
            bars.append(Bar(start + timedelta(minutes=index), close - .01, 100.0, 99.4, close, 1000))
        for index in range(15, 22):
            close = 99.90 + (index - 15) * .015
            bars.append(Bar(start + timedelta(minutes=index), close - .01, close + .015, close - .02, close, 1000))
        bars.append(Bar(start + timedelta(minutes=22), 100.0, 100.42, 99.98, 100.4, 1000))  # cross, low volume
        bars.append(Bar(start + timedelta(minutes=23), 100.4, 100.47, 100.38, 100.45, 2000))  # holds, volume arrives
        same_bar_only = replace(self.strategy, rules={**self.strategy.rules, "breakout_lookback_bars": 1})
        recent = replace(self.strategy, rules={**self.strategy.rules, "breakout_lookback_bars": 15})
        self.assertIsNone(evaluate_underlying(bars, same_bar_only))
        self.assertEqual(evaluate_underlying(bars, recent).side, "CALL")

    def _breakout_bars(self):
        start = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(15):
            close = 99.60 + index * .02
            bars.append(Bar(start + timedelta(minutes=index), close - .01, 100.0, 99.4, close, 1000))
        for index in range(15, 22):
            close = 99.90 + (index - 15) * .015
            bars.append(Bar(start + timedelta(minutes=index), close - .01, close + .015, close - .02, close, 1000))
        bars.append(Bar(start + timedelta(minutes=22), 100.0, 100.42, 99.98, 100.4, 2000))
        return bars

    def test_exits_use_opening_range_height_with_atr_floor(self):
        signal = evaluate_underlying(self._breakout_bars(), self.strategy)
        height = signal.metrics["opening_range_height"]
        self.assertAlmostEqual(height, 0.6)
        self.assertAlmostEqual(signal.metrics["stop_distance"],
                               max(0.5 * height, signal.metrics["atr"] * self.strategy.rules["stop_underlying_atr"]))
        self.assertAlmostEqual(100.4 - signal.stop_price, signal.metrics["stop_distance"])
        self.assertAlmostEqual(signal.target_price - 100.4, signal.metrics["target_distance"])
        self.assertGreater(signal.metrics["target_distance"], signal.metrics["stop_distance"])

    def test_volatility_spike_filter_skips_erratic_symbol(self):
        from dataclasses import replace
        from trader.strategy_engine import evaluate_underlying_diagnostics
        bars = self._breakout_bars()
        _, metrics, _ = evaluate_underlying_diagnostics(bars, self.strategy)
        jumpy = replace(self.strategy, rules={**self.strategy.rules, "max_atr_expansion": metrics["atr_expansion"] / 2})
        signal, _, reason = evaluate_underlying_diagnostics(bars, jumpy)
        self.assertIsNone(signal)
        self.assertEqual(reason, "volatility_spike_filter_failed")
        self.assertIsNotNone(evaluate_underlying(bars, self.strategy))

    def test_economic_gate_measures_r_against_loss_at_stop_not_full_premium(self):
        bars = self._breakout_bars()
        now = bars[-1].timestamp + timedelta(minutes=1)
        option = self._option(expiration=now + timedelta(days=12))  # ask 4.05: $405 premium
        with tempfile.TemporaryDirectory() as directory:
            decision = decision_cycle(strategy=self.strategy, strategy_path=ROOT / "strategy.txt",
                manifest_path=ROOT / "active_strategy.json", bars_by_symbol={"SPY": bars},
                option_chain_by_symbol={"SPY": [option]}, risk_state=RiskState(10_000, 0, 0, 0, 0, 0),
                now=now, data_fresh=True, account_certain=True, reconciled=True, protection_verified=True,
                market_open=True, within_entry_window=True, decision_log=Path(directory) / "d.jsonl",
                kill_switch_path=Path(directory) / "missing", paper_execution=True)[0]
        diagnostics = decision.diagnostics
        self.assertEqual(decision.decision, "PAPER_ENTRY", decision.reason)
        self.assertLess(diagnostics["modeled_loss_at_stop_dollars"], diagnostics["premium_exposure_dollars"] / 5)
        self.assertGreaterEqual(diagnostics["expected_net_r"], self.strategy.execution["min_expected_net_r"])
        # Against the full premium (the old definition) the same trade scored far below 1R.
        self.assertLess(diagnostics["net_opportunity"] / diagnostics["modeled_risk_dollars"], 0.2)

    def test_option_selector_prefers_affordable_in_range_contract(self):
        now = datetime.now(timezone.utc)
        pricey = self._option(symbol="QQQ C740", underlying="QQQ", delta=.56, bid=11.15, ask=11.18)
        affordable = self._option(symbol="QQQ C745", underlying="QQQ", delta=.47, bid=8.11, ask=8.14)
        unlimited, _ = evaluate_options([pricey, affordable], "CALL", now, self.strategy)
        self.assertEqual(unlimited.quote.symbol, "QQQ C740")
        selected, audit = evaluate_options([pricey, affordable], "CALL", now, self.strategy,
                                           max_risk_dollars_per_contract=1000, costs_per_contract=1.44)
        self.assertEqual(selected.quote.symbol, "QQQ C745")
        self.assertIn("premium_above_risk_budget", audit[0]["rejection_reasons"])

    def test_entry_arbiter_gives_limited_slots_to_best_ranked_symbols(self):
        from trader.paper_runner import _EntryArbiter
        slots = {"free": 1}
        minute = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)

        async def scenario():
            arbiter = _EntryArbiter(lambda: slots["free"], wait_seconds=0.05)
            results = await asyncio.gather(
                arbiter.approve(minute, "SPY", (1.2, 0.3)),
                arbiter.approve(minute, "NVDA", (1.9, 0.1)),
                arbiter.approve(minute, "IWM", (1.9, 0.6)))
            late_with_slot = await arbiter.approve(minute, "AAPL", (5.0, 1.0))
            slots["free"] = 0
            late_without_slot = await arbiter.approve(minute, "BAC", (5.0, 1.0))
            return results, late_with_slot, late_without_slot

        (spy, nvda, iwm), late_with_slot, late_without_slot = asyncio.run(scenario())
        self.assertEqual((spy, nvda, iwm), (False, False, True))  # tie on R broken by trend efficiency
        self.assertTrue(late_with_slot)
        self.assertFalse(late_without_slot)

    def test_live_bars_drop_the_minute_in_progress_at_connect(self):
        import trader.market_data as market_data
        base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        joined = base - timedelta(minutes=1)            # minute already running when we connected
        fresh = base + timedelta(minutes=1)             # minutes that start after the connection
        def trade(at, price): return ["Trade", "IWM", int(at.timestamp() * 1000), price, 0, 10]

        async def fake_events(*args, **kwargs):
            yield {"data": [trade(joined + timedelta(seconds=40), 276.0)]}
            yield {"data": [trade(fresh, 276.1), trade(fresh + timedelta(seconds=30), 276.3)]}
            yield {"data": [trade(fresh + timedelta(minutes=1), 276.2)]}
            yield {"data": [trade(fresh + timedelta(minutes=2), 276.4)]}

        async def collect():
            out = []
            async for item in market_data.live_minute_bars("wss://x", "t", ["IWM"]):
                out.append(item)
                if len(out) == 2:
                    break
            return out

        with patch("trader.market_data.dxlink_events", fake_events):
            bars = asyncio.run(collect())
        self.assertEqual([bar.timestamp for _, bar in bars], [fresh, fresh + timedelta(minutes=1)])
        self.assertEqual((bars[0][1].open, bars[0][1].close, bars[0][1].volume), (276.1, 276.3, 20))

    def test_multi_symbol_session_bootstraps_ranks_enters_and_carries_positions(self):
        """End-to-end run of the session loop with fake broker data for two underlyings."""
        import types
        from trader import paper_runner
        now = datetime.now(timezone.utc)
        open_utc = now.replace(second=0, microsecond=0) - timedelta(minutes=22)
        template = self._breakout_bars()
        shift = open_utc - template[0].timestamp
        session = [Bar(b.timestamp + shift, b.open, b.high, b.low, b.close, b.volume) for b in template]
        history, breakout = session[:-1], session[-1]  # 22 bars: started before indicator warm-up completed

        class FakeCalendar:
            def session_bounds(self, day):
                return (open_utc.astimezone(ZoneInfo("America/New_York")),
                        (datetime.now(timezone.utc) + timedelta(seconds=4)).astimezone(ZoneInfo("America/New_York")))
            def is_open(self, at): return True
            def entry_window_ok(self, at, start, end): return True

        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def clock_offset_seconds(self): return 0.0
            def quote_token(self): return {"data": {"token": "t", "dxlink-url": "wss://example"}}

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

        async def fake_live(url, token, symbols):
            for symbol in symbols:
                yield symbol, breakout
            await asyncio.sleep(3600)

        def fake_options(client, *, underlying, spot, now, min_dte, max_dte):
            return [self._option(symbol=f"{underlying} C100", underlying=underlying, expiration=now + timedelta(days=12))]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "strategy.txt").write_bytes((ROOT / "strategy.txt").read_bytes())
            strategy = parse_strategy(root / "strategy.txt")
            write_manifest(strategy, root / "active_strategy.json",
                           {"status": "research_test", "mode": "PAPER", "live_enabled": False})
            cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
            with patch.object(paper_runner, "MarketCalendar", FakeCalendar), \
                    patch.object(paper_runner, "TastytradeClient", FakeClient), \
                    patch.object(paper_runner, "ProductionMarketData", FakeQuotes), \
                    patch.object(paper_runner, "create_history_provider", lambda *a, **k: FakeHistory()), \
                    patch.object(paper_runner, "live_minute_bars", fake_live), \
                    patch.object(paper_runner, "fetch_option_quotes", fake_options), \
                    patch.object(paper_runner, "ENTRY_ARBITRATION_SECONDS", 0.05):
                try:
                    # Flatten off so the fake-bell path uses the carry branch (no broker marks needed);
                    # this test covers multi-symbol entry/ranking, not the end-of-day flatten.
                    from dataclasses import replace as _replace
                    no_flat = _replace(strategy, execution={**strategy.execution, "flatten_at_session_close": False})
                    asyncio.run(paper_runner._run(root=root, cfg=cfg, strategy=no_flat, symbols=("SPY", "IWM")))
                finally:
                    paper_runner._close_session_log_file(paper_runner._session_logger(cfg.data_dir))
            account = json.loads((root / "data" / "paper_account.json").read_text(encoding="utf-8"))
            decisions = [json.loads(line) for line in (root / "data" / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(position["underlying"] for position in account["positions"]), ["IWM", "SPY"],
                         [(row["underlying"], row["reason"]) for row in decisions])
        self.assertEqual({row["underlying"] for row in decisions if row["decision"] == "PAPER_ENTRY"}, {"SPY", "IWM"})
        for position in account["positions"]:  # exits come from the opening range, not one-minute ATR
            self.assertAlmostEqual(position["entry_underlying"] - position["stop_underlying"], 0.5 * 0.6, delta=0.02)

    def test_backtest_follows_each_trade_to_its_stop_or_target(self):
        from trader.backtest import replay
        eastern = ZoneInfo("America/New_York")
        bars = self._breakout_bars()
        start = datetime(2026, 10, 9, 9, 30, tzinfo=eastern)  # regular-session timestamps
        bars = [Bar(start + timedelta(minutes=i), b.open, b.high, b.low, b.close, b.volume) for i, b in enumerate(bars)]
        signal = evaluate_underlying(bars, self.strategy)
        after = bars[-1].timestamp
        bars += [Bar(after + timedelta(minutes=1), 100.4, 100.6, 100.35, 100.55, 1500),
                 Bar(after + timedelta(minutes=2), 100.55, signal.target_price + .05, 100.5, 100.9, 1500),
                 Bar(after + timedelta(minutes=3), 100.9, 101.0, 98.0, 98.5, 1500)]  # later stop must not count
        result = replay(bars, self.strategy, in_sample_end=after + timedelta(days=1))
        trade = result["signals"][0]
        self.assertEqual((trade["exit_reason"], trade["minutes_held"]), ("target", 2))
        self.assertAlmostEqual(trade["exit"], signal.target_price)
        self.assertGreater(trade["net_pnl"], 0)

    def test_only_connection_failures_restart_the_paper_engine(self):
        from trader.paper_runner import _is_connection_failure
        from trader.tastytrade_client import TransportError
        self.assertTrue(_is_connection_failure(ConnectionError("DXLink rejected the feed request: TIMEOUT")))
        self.assertTrue(_is_connection_failure(TransportError("tastytrade request failed")))
        try:
            try:
                raise TimeoutError("history snapshot")
            except TimeoutError as exc:
                raise RuntimeError("Paper startup blocked by historical recovery") from exc
        except RuntimeError as wrapped:
            self.assertTrue(_is_connection_failure(wrapped))
        self.assertFalse(_is_connection_failure(RuntimeError("This computer's clock is 2771.0s behind tastytrade's")))
        self.assertFalse(_is_connection_failure(RuntimeError("Live candle is out of order or conflicts with recovered history")))

    def test_dxlink_keepalive_continues_while_consumer_is_busy(self):
        """Regression: DXLink dropped the feed when the consumer spent >60s between events."""
        import sys, types
        sent: list[dict] = []

        class FakeSocket:
            def __init__(self):
                self.inbox: asyncio.Queue = asyncio.Queue()
            async def send(self, raw):
                msg = json.loads(raw); sent.append(msg)
                replies = {"SETUP": [{"type": "AUTH_STATE", "state": "UNAUTHORIZED"}],
                           "AUTH": [{"type": "AUTH_STATE", "state": "AUTHORIZED"}],
                           "CHANNEL_REQUEST": [{"type": "CHANNEL_OPENED", "channel": 3}],
                           "FEED_SETUP": [{"type": "FEED_CONFIG", "channel": 3}],
                           "FEED_SUBSCRIPTION": [{"type": "FEED_DATA", "channel": 3, "data": ["Trade", ["Trade", "SPY"]]}]}
                for reply in replies.get(msg["type"], []):
                    self.inbox.put_nowait(json.dumps(reply))
            def __aiter__(self):
                return self
            async def __anext__(self):
                return await self.inbox.get()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        fake_client = types.ModuleType("websockets.asyncio.client")
        fake_client.connect = lambda *args, **kwargs: FakeSocket()
        fake_truststore = types.ModuleType("truststore")
        fake_truststore.SSLContext = lambda *args: None

        async def scenario():
            from trader.market_data import dxlink_events
            feed = dxlink_events("wss://example", "token", [{"type": "Trade", "symbol": "SPY"}])
            await feed.__anext__()    # first event received
            await asyncio.sleep(0.3)  # consumer busy (history recovery, REST calls, ...) between events
            await feed.aclose()

        with patch.dict(sys.modules, {"websockets.asyncio.client": fake_client, "truststore": fake_truststore}), \
                patch("trader.market_data.DXLINK_KEEPALIVE_SECONDS", 0.05):
            asyncio.run(scenario())
        self.assertGreaterEqual(sum(1 for msg in sent if msg["type"] == "KEEPALIVE"), 3)

    def test_deterministic_paper_decision_and_fill_path(self):
        start = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(15):
            close = 99.60 + index * .02
            bars.append(Bar(start + timedelta(minutes=index), close - .01, 100.0, 99.4, close, 1000))
        for index in range(15, 22):
            close = 99.90 + (index - 15) * .015
            bars.append(Bar(start + timedelta(minutes=index), close - .01, close + .015, close - .02, close, 1000))
        bars.append(Bar(start + timedelta(minutes=22), 100.0, 100.42, 99.98, 100.4, 2000))
        now = bars[-1].timestamp + timedelta(minutes=1)
        option = self._option(expiration=now + timedelta(days=12), bid=.19, ask=.20,
                              delta=.65, gamma=.1, volume=10, open_interest=100)
        with tempfile.TemporaryDirectory() as directory:
            decisions = decision_cycle(strategy=self.strategy, strategy_path=ROOT / "strategy.txt",
                manifest_path=ROOT / "active_strategy.json", bars_by_symbol={"SPY": bars},
                option_chain_by_symbol={"SPY": [option]}, risk_state=RiskState(100_000, 0, 0, 0, 0, 0),
                now=now, data_fresh=True, account_certain=True, reconciled=True, protection_verified=True,
                market_open=True, within_entry_window=True, decision_log=Path(directory) / "decisions.jsonl",
                kill_switch_path=Path(directory) / "missing", paper_execution=True)
        self.assertEqual(decisions[0].decision, "PAPER_ENTRY")
        self.assertEqual(decisions[0].diagnostics["quantity"], 1)
        self.assertTrue(decisions[0].diagnostics["economic_gate_assumption"].startswith("target or stop reached"))
        account = PaperAccount(100_000, 100_000)
        fill = PaperExecutor(account).fill(option, "BUY", 1, "strategy_entry", now)
        account.ledger[-1].update({"strategy_id": self.strategy.strategy_id,
                                   "strategy_version": self.strategy.version,
                                   "strategy_hash": self.strategy.sha256})
        self.assertEqual(account.ledger[-1]["strategy_hash"], self.strategy.sha256)
        self.assertLess(account.cash, 100_000)

    def test_decision_log_records_identity_hash_and_diagnostic_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            now = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
            records = decision_cycle(strategy=self.strategy, strategy_path=ROOT / "strategy.txt",
                manifest_path=ROOT / "active_strategy.json", bars_by_symbol={"SPY": []},
                option_chain_by_symbol={"SPY": []}, risk_state=RiskState(10_000, 0, 0, 0, 0, 0),
                now=now, data_fresh=True, account_certain=True, reconciled=True, protection_verified=True,
                market_open=True, within_entry_window=True, decision_log=log, kill_switch_path=Path(directory) / "missing")
            self.assertEqual(records[0].reason, "no_minute_bars")
            row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["strategy_id"], self.strategy.strategy_id)
            self.assertEqual(row["strategy_version"], self.strategy.version)
            self.assertEqual(row["strategy_hash"], self.strategy.sha256)
            self.assertEqual(row["diagnostics"]["underlying_filter_reason"], "no_minute_bars")
            self.assertIn("timestamp_et", row["diagnostics"])

    def test_paper_test_refuses_live_and_does_not_mutate_strategy_manifest(self):
        with self.assertRaisesRegex(RuntimeError, "forbidden in LIVE"):
            run_paper_test_session(root=ROOT, cfg=AppConfig(mode=Mode.LIVE), strategy=self.strategy, symbols=("SPY",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "strategy.txt").write_bytes((ROOT / "strategy.txt").read_bytes())
            temp_strategy = parse_strategy(root / "strategy.txt")
            write_manifest(temp_strategy, root / "active_strategy.json",
                           {"status": "research_test", "mode": "PAPER", "live_enabled": False})
            before = (root / "active_strategy.json").read_bytes()
            strategy_before = (root / "strategy.txt").read_bytes()
            cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
            def close_coroutine(coroutine):
                coroutine.close()
            with patch("trader.paper_runner.asyncio.run", side_effect=close_coroutine):
                run_paper_test_session(root=root, cfg=cfg, strategy=temp_strategy, symbols=("SPY",))
            self.assertEqual((root / "active_strategy.json").read_bytes(), before)
            self.assertEqual((root / "strategy.txt").read_bytes(), strategy_before)

    def test_live_adapter_requires_preflight_and_does_not_submit_on_warning(self):
        cfg = AppConfig(mode=Mode.LIVE, live_enabled=True,
                        live_confirmation="I UNDERSTAND LIVE TRADING USES REAL MONEY",
                        client_id="x", client_secret="y", refresh_token="z", account_number="acct")
        class FakeClient:
            def dry_run_order(self, account, order): return {"data": {"warnings": ["review"]}}
            def submit_order(self, *args, **kwargs): raise AssertionError("submit must not be called")
        with self.assertRaisesRegex(RuntimeError, "warnings"):
            LiveExecution(cfg, FakeClient()).place({"symbol": "SPY"})

    def test_fees_and_economic_gate(self):
        self.assertAlmostEqual(FeeSchedule().estimate(2), 1.4)
        result = economic_gate(entry=1, exit_price=2, contracts=1, spread_cost=10, slippage=2, fees=1, risk_dollars=100, min_net_r=.5)
        self.assertEqual(result["net_opportunity"], 87)
        self.assertTrue(result["accepted"])

    def test_risk_blocks_daily_loss_and_sizes_without_streak_scaling(self):
        risk = {**self.strategy.risk, "max_risk_per_trade_fraction": 0.01, "hard_max_risk_per_trade_fraction": 0.01}
        state = RiskState(10_000, -200, 0, 0, 0, 0)
        result = assess_entry(state=state, contract_price=1, strategy_risk=risk, max_contracts=10,
                              data_fresh=True, account_certain=True, reconciled=True, protection_verified=True)
        self.assertFalse(result.allowed); self.assertIn("daily_loss_limit", result.reason)
        good = assess_entry(state=RiskState(10_000, 0, 0, 0, 0, 0), contract_price=1, strategy_risk=risk,
                             max_contracts=10, data_fresh=True, account_certain=True, reconciled=True, protection_verified=True)
        self.assertEqual(good.quantity, 1)

    def test_paper_fill_uses_ask_buy_bid_sell_and_records_costs(self):
        account = PaperAccount(10_000, 10_000)
        executor = PaperExecutor(account)
        quote = self._option()
        buy = executor.fill(quote, "BUY", 1, "test")
        self.assertGreaterEqual(buy.fill_price, quote.ask)
        sell = executor.fill(quote, "SELL", 1, "test-exit")
        self.assertLessEqual(sell.fill_price, quote.bid)
        self.assertAlmostEqual(account.fees_paid, 1.4)
        self.assertEqual(len(account.ledger), 2)

    def test_account_initialization_separates_capital_movements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_account.json"
            account = load_or_initialize(path, 1234)
            account.capital_movement(100, "deposit")
            self.assertEqual(account.cash, 1334)
            self.assertEqual(account.realized_pnl, 0)
            self.assertEqual(load_or_initialize(path, 999).starting_capital, 1234)

    def test_position_exits_are_deterministic(self):
        now = datetime.now(timezone.utc)
        pos = Position("id", "OPT", "SPY", "C", 1, 1, 100, 98, 104, now, 60, 100, "v", "hash")
        self.assertEqual(exit_reason(pos, underlying=97, option_bid=.5, now=now), "underlying_invalidation_stop")
        self.assertEqual(exit_reason(pos, underlying=101, option_bid=.5, now=now + timedelta(minutes=61)), "time_exit")
        self.assertIsNone(exit_reason(pos, underlying=101, option_bid=.5, now=now + timedelta(minutes=61),
                                      enable_time_exit=False))

    def test_reconciliation_blocks_quantity_mismatch(self):
        result = reconcile_positions([{"symbol": "SPY call", "quantity": 1}], [{"symbol": "SPY call", "quantity": 2}])
        self.assertFalse(result.ok)

    def test_performance_metrics(self):
        perf = calculate_performance([{"realized_pnl": 10, "r_multiple": 1, "fees": 1},
                                      {"realized_pnl": -5, "r_multiple": -.5, "fees": 1}])
        self.assertEqual(perf.trade_count, 2); self.assertEqual(perf.win_rate, .5)
        self.assertEqual(perf.total_pnl, 5); self.assertEqual(perf.profit_factor, 2)

    def test_live_requires_explicit_confirmation_and_credentials(self):
        with self.assertRaises(RuntimeError):
            AppConfig(mode=Mode.LIVE).validate_startup()

    def test_order_requires_dry_run_and_external_identifier_before_submit(self):
        client = TastytradeClient("x", "y", "z", base_url="https://invalid.local")
        with self.assertRaises(Exception): client.submit_order("acct", {"external-identifier": "id"})
        with self.assertRaises(Exception): client.submit_order("acct", {}, dry_run_result={})

    def test_kill_switch_and_agent_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            switch = KillSwitch(root / "KILL_SWITCH"); switch.block_entries("test")
            self.assertTrue(switch.entries_blocked()); switch.reset(); self.assertFalse(switch.entries_blocked())
            lease = AgentLease(root / "agent_lock.json", root / "agent_status.json")
            lease.acquire("Codex", "test")
            with self.assertRaises(RuntimeError): lease.acquire("Claude", "test")
            lease.release("Codex")


if __name__ == "__main__": unittest.main()
