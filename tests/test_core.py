from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.config import AppConfig, Mode
from trader.backtest import load_bars
from trader.fees import FeeSchedule, economic_gate
from trader.metrics import calculate_performance
from trader.models import Bar, OptionQuote, Position
from trader.option_selector import select_option
from trader.paper_execution import PaperExecutor
from trader.portfolio import PaperAccount, load_or_initialize
from trader.position_manager import exit_reason
from trader.reconciliation import reconcile_positions
from trader.risk_engine import RiskState, assess_entry
from trader.strategy_engine import atr, ema, evaluate_underlying
from trader.strategy_loader import StrategyError, parse_strategy, verify_manifest
from trader.tastytrade_client import TastytradeClient
from trader.watchdog import AgentLease, KillSwitch


ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.strategy = parse_strategy(ROOT / "strategy.txt")

    def test_strategy_parses_and_hash_manifest_matches(self):
        manifest = verify_manifest(ROOT / "strategy.txt", ROOT / "active_strategy.json")
        self.assertEqual(manifest["sha256"], self.strategy.sha256)

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

    def _option(self, **kwargs):
        base = dict(symbol="SPY 261016C00650000", underlying="SPY", expiration=datetime.now(timezone.utc) + timedelta(days=12),
                    strike=650, right="C", bid=4.0, ask=4.2, bid_size=20, ask_size=20, delta=.55, gamma=.01, theta=-.02,
                    vega=.1, iv=.25, volume=500, open_interest=1000, quote_timestamp=datetime.now(timezone.utc))
        return OptionQuote(**(base | kwargs))

    def test_option_selector_filters_liquidity_dte_delta_and_spread(self):
        best = self._option()
        rejected = self._option(symbol="illiquid", volume=0)
        selected = select_option([rejected, best], "CALL", datetime.now(timezone.utc), self.strategy)
        self.assertEqual(selected.quote.symbol, best.symbol)
        self.assertIsNone(select_option([best], "PUT", datetime.now(timezone.utc), self.strategy))

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
