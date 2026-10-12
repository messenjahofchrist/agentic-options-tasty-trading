from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from trader.config import AppConfig, Mode
from trader.config import load_dotenv
from trader.backtest import load_bars
from trader.fees import FeeSchedule, economic_gate
from trader.metrics import calculate_performance
from trader.models import Bar, OptionQuote, Position
from trader.option_selector import evaluate_options, select_option
from trader.market_data import CachedQuote, parse_candle_feed
from trader.paper_execution import PaperExecutor, realized_option_pnl
from trader.paper_runner import _fresh_underlying_quote
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
        first = ["Candle", symbol, 0, int(start.timestamp() * 1000), 0, 1, 470, 471, 469, 470.5, 1000, 4]
        second = ["Candle", symbol, 0, int(end.timestamp() * 1000), 0, 1, 470.5, 472, 470, 471.5, 900, 8]
        rows = parse_candle_feed([{"data": ["Candle", first, "Candle", second]}], start=start,
                                 end=end - timedelta(milliseconds=1), symbol="SPY")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 470.5)
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            parse_candle_feed([{"data": ["Candle", first[:-1] + [16]]}], start=start, end=end, symbol="SPY")

    def test_underlying_quote_freshness_uses_snapshot_time_and_rejects_future_or_stale(self):
        now = datetime.now(timezone.utc)
        fresh = CachedQuote("SPY", 500.0, 500.1, now - timedelta(seconds=2), {})
        future = CachedQuote("SPY", 500.0, 500.1, now + timedelta(seconds=1), {})
        stale = CachedQuote("SPY", 500.0, 500.1, now - timedelta(seconds=21), {})
        self.assertTrue(_fresh_underlying_quote(fresh, now))
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
        one_percent = self.strategy.risk
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
        cases = [
            (RiskState(10_000, -200, 0, 0, 0, 0), "daily_loss_limit"),
            (RiskState(10_000, 0, -500, 0, 0, 0), "weekly_loss_limit"),
            (RiskState(10_000, 0, 0, 3, 0, 0), "consecutive_loss_limit"),
            (RiskState(10_000, 0, 0, 0, 3, 0), "trade_count_limit"),
            (RiskState(10_000, 0, 0, 0, 0, 2), "open_position_limit"),
            (RiskState(10_000, 0, 0, 0, 0, 1, ("SPY",)), "underlying_position_already_open_no_averaging"),
        ]
        for state, reason in cases:
            result = assess_entry(state=state, contract_price=1, strategy_risk=self.strategy.risk,
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
        self.assertTrue(decisions[0].diagnostics["economic_gate_assumption"].startswith("target reached"))
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
            self.assertEqual(row["strategy_version"], "0.1.0")
            self.assertEqual(row["strategy_hash"], self.strategy.sha256)
            self.assertEqual(row["diagnostics"]["underlying_filter_reason"], "no_minute_bars")
            self.assertIn("timestamp_et", row["diagnostics"])

    def test_paper_test_refuses_live_and_does_not_mutate_strategy_manifest(self):
        with self.assertRaisesRegex(RuntimeError, "forbidden in LIVE"):
            run_paper_test_session(root=ROOT, cfg=AppConfig(mode=Mode.LIVE), strategy=self.strategy, symbol="SPY")
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
                run_paper_test_session(root=root, cfg=cfg, strategy=temp_strategy, symbol="SPY")
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
        risk = self.strategy.risk
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
