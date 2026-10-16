"""LIVE execution tests. Every broker interaction is a local fake: no test can reach tastytrade."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader.config import AppConfig, Mode
from trader.live_execution import (LIVE_AUTHORIZATION_STATEMENT, ExecutionSettings, LiveTrader, parse_fills,
                                   verify_live_authorization)
from trader.live_session import authorize_live, confirm_live_start, live_readiness, run_live_session
from trader.market_data import CachedQuote
from trader.models import Bar, OptionQuote
from trader.paper_runner import _fresh_quote
from trader.portfolio import PaperAccount
from trader.strategy_loader import parse_strategy, write_manifest
from trader.tastytrade_client import BrokerError, TastytradeClient, TransportError, UncertainOrderSubmission

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "5WT00001"
PHRASE = "I UNDERSTAND LIVE TRADING USES REAL MONEY"
FAST = ExecutionSettings(entry_fill_timeout_seconds=0.05, exit_fill_timeout_seconds=0.05, exit_max_attempts=3,
                         cancel_confirm_timeout_seconds=0.2, poll_interval_seconds=0.005,
                         uncertain_lookup_attempts=2, uncertain_lookup_delay_seconds=0.005, broker_refresh_seconds=0)


def live_cfg(data_dir: Path) -> AppConfig:
    return AppConfig(mode=Mode.LIVE, live_enabled=True, live_confirmation=PHRASE, client_id="x", client_secret="y",
                     refresh_token="z", account_number=ACCOUNT, data_dir=data_dir)


class FakeBroker:
    """In-memory tastytrade account. `fill_mode` controls how submitted orders behave."""

    def __init__(self, *, net_liq=10_000.0, buying_power=10_000.0, fill_mode="fill", options_level="Covered And Cash Secured",
                 settled_cash=None):
        self.net_liq, self.buying_power, self.fill_mode = net_liq, buying_power, fill_mode
        self.settled_cash = net_liq if settled_cash is None else settled_cash
        self.options_level = options_level
        self.orders: dict[int, dict] = {}
        self.holdings: dict[str, int] = {}
        self.submitted: list[dict] = []
        self.dry_runs: list[dict] = []
        self.cancels: list[int] = []
        self.dry_run_warnings: list = []
        self.accounts_list = [{"account": {"account-number": "5WT99999", "margin-or-cash": "Margin"}, "authority-level": "owner"},
                              {"account": {"account-number": ACCOUNT, "margin-or-cash": "Cash", "is-closed": False,
                                           "account-type-name": "Individual"}, "authority-level": "owner"}]
        self.trading = {"options-level": options_level}
        self.bid = 1.00
        self.ignore_cancel = False
        self.foreign_orders: list[dict] = []

    # REST surface used by LiveTrader / live_readiness
    def accounts(self): return {"data": {"items": self.accounts_list}}
    def trading_status(self, account): return {"data": dict(self.trading)}
    def balances(self, account):
        return {"data": {"net-liquidating-value": str(self.net_liq), "derivative-buying-power": str(self.buying_power),
                         "cash-balance": str(self.net_liq),
                         **({"cash-settle-balance": str(self.settled_cash)} if self.settled_cash != "missing" else {})}}
    def positions(self, account):
        return {"data": {"items": [{"symbol": s, "instrument-type": "Equity Option", "underlying-symbol": s.split()[0],
                                    "quantity": str(abs(q)), "quantity-direction": "Long" if q > 0 else "Short"}
                                   for s, q in self.holdings.items() if q]}}
    def live_orders(self, account): return {"data": {"items": list(self.orders.values()) + self.foreign_orders}}
    def orders(self, account): return self.live_orders(account)
    def order(self, account, order_id): return {"data": self.orders[int(order_id)]}
    def find_order_by_external_id(self, account, external_id):
        return next((o for o in self.orders.values() if o.get("external-identifier") == external_id), None)

    def dry_run_order(self, account, body):
        self.dry_runs.append(body)
        return {"data": {"order": body, "warnings": self.dry_run_warnings, "errors": [],
                         "fee-calculation": {"total-fees": str(0.70 * body["legs"][0]["quantity"])}}}

    def _accept(self, body) -> dict:
        order_id = len(self.orders) + 1
        leg = body["legs"][0]
        order = {"id": order_id, "status": "Live", "external-identifier": body["external-identifier"],
                 "underlying-symbol": leg["symbol"].split()[0], "price": body["price"],
                 "legs": [{"symbol": leg["symbol"], "quantity": leg["quantity"], "action": leg["action"],
                           "remaining-quantity": leg["quantity"], "fills": []}]}
        self.orders[order_id] = order
        return order

    def _fill(self, order, quantity):
        leg = order["legs"][0]
        leg["fills"].append({"quantity": str(quantity), "fill-price": order["price"]})
        leg["remaining-quantity"] = leg["quantity"] - sum(int(f["quantity"]) for f in leg["fills"])
        sign = 1 if leg["action"].startswith("Buy") else -1
        self.holdings[leg["symbol"]] = self.holdings.get(leg["symbol"], 0) + sign * quantity
        if leg["remaining-quantity"] == 0:
            order["status"] = "Filled"

    def submit_order(self, account, body, *, dry_run_result=None):
        if dry_run_result is None:
            raise BrokerError("dry run required")
        self.submitted.append(body)
        mode = self.fill_mode(body) if callable(self.fill_mode) else self.fill_mode
        if mode == "reject":
            raise BrokerError("tastytrade HTTP 422 for POST: invalid_order (status=422)")
        if mode == "timeout_lost":
            raise UncertainOrderSubmission("timed out")
        order = self._accept(body)
        if mode == "fill":
            self._fill(order, body["legs"][0]["quantity"])
        elif mode == "partial":
            self._fill(order, 1)
        elif mode == "timeout_accepted":
            self._fill(order, body["legs"][0]["quantity"])
            raise UncertainOrderSubmission("timed out after accept")
        return {"data": {"order": dict(order)}}

    def cancel_order(self, account, order_id):
        self.cancels.append(int(order_id))
        if not self.ignore_cancel:
            order = self.orders[int(order_id)]
            if order["status"] not in {"Filled"}:
                order["status"] = "Cancelled"
        return {"data": {}}

    # market data used by live_readiness when market_data=False is not set (not used here)
    def _access_token(self): return "token"
    def clock_offset_seconds(self): return 0.1
    def quote_token(self): return {"data": {"token": "t", "dxlink-url": "wss://example", "level": "api",
                                            "expires-at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}}


def option(symbol="SPY   261030C00100000", underlying="SPY", bid=1.00, ask=1.02, age=1.0, **kw) -> OptionQuote:
    now = datetime.now(timezone.utc)
    return OptionQuote(symbol, underlying, now + timedelta(days=12), 100.0, "C", bid, ask, delta=.55, gamma=.05,
                       theta=-.03, vega=.1, volume=500, open_interest=1000,
                       quote_timestamp=now - timedelta(seconds=age), **kw)


READY = {"final_readiness": "READY", "historical_to_live_transition_status": "VERIFIED"}

_NO_EMAIL = patch("trader.notifications.email_configured", return_value=False)


def setUpModule():  # alerts raised by these tests must never reach a real mailbox
    _NO_EMAIL.start()


def tearDownModule():
    _NO_EMAIL.stop()


class LiveTraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.strategy = parse_strategy(ROOT / "strategy.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self, broker: FakeBroker) -> LiveTrader:
        trader = LiveTrader(cfg=live_cfg(self.root / "live"), client=broker, strategy=self.strategy,
                            data_dir=self.root / "live", kill_switch_path=self.root / "KILL_SWITCH", settings=FAST)
        trader.startup()
        return trader

    def template(self, symbol="SPY   261030C00100000", underlying="SPY") -> dict:
        return {"trade_id": "t1", "symbol": symbol, "underlying": underlying, "right": "C", "entry_underlying": 100.0,
                "stop_underlying": 99.0, "target_underlying": 102.0,
                "entry_time": datetime.now(timezone.utc).isoformat(), "max_holding_minutes": 180,
                "initial_risk_dollars": 105.0, "planned_risk_dollars": 1000.0, "modeled_risk_dollars": 105.0,
                "premium_exposure_dollars": 102.0, "strategy_id": self.strategy.strategy_id,
                "strategy_version": self.strategy.version, "strategy_hash": self.strategy.sha256,
                "mfe": 0.0, "mae": 0.0, "last_bid": 1.0}

    def enter(self, trader, quote=None, quantity=1, decision_option=None, readiness=READY):
        quote = quote or option()
        return asyncio.run(trader.open_position(template=self.template(quote.symbol, quote.underlying), quote=quote,
                                                quantity=quantity, decision_option=decision_option or quote.symbol,
                                                readiness=readiness, now=datetime.now(timezone.utc),
                                                quote_fresh=_fresh_quote))

    # --- account verification -------------------------------------------------------------
    def test_startup_uses_only_the_configured_account_and_never_falls_back(self):
        broker = FakeBroker()
        broker.accounts_list = broker.accounts_list[:1]  # only a different account is visible
        trader = LiveTrader(cfg=live_cfg(self.root), client=broker, strategy=self.strategy, data_dir=self.root,
                            kill_switch_path=self.root / "KILL_SWITCH", settings=FAST)
        with self.assertRaisesRegex(RuntimeError, "not accessible.*none is used as a fallback"):
            trader.startup()

    def test_startup_blocks_frozen_account_and_missing_options_permission(self):
        for change, message in ((("is-frozen", True), "is-frozen"), (("options-level", "No Options"), "options level")):
            broker = FakeBroker()
            broker.trading[change[0]] = change[1]
            trader = LiveTrader(cfg=live_cfg(self.root), client=broker, strategy=self.strategy, data_dir=self.root,
                                kill_switch_path=self.root / "KILL_SWITCH", settings=FAST)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, message):
                trader.startup()

    def test_live_trader_requires_explicit_live_configuration(self):
        with self.assertRaises(RuntimeError):
            LiveTrader(cfg=AppConfig(mode=Mode.PAPER), client=FakeBroker(), strategy=self.strategy,
                       data_dir=self.root, kill_switch_path=self.root / "KILL_SWITCH")
        with self.assertRaises(RuntimeError):  # LIVE mode without the confirmation phrase
            LiveTrader(cfg=replace(live_cfg(self.root), live_confirmation=""), client=FakeBroker(),
                       strategy=self.strategy, data_dir=self.root, kill_switch_path=self.root / "KILL_SWITCH")

    # --- entries ---------------------------------------------------------------------------
    def test_entry_dry_runs_submits_and_books_the_broker_fill(self):
        broker = FakeBroker()
        trader = self.trader(broker)
        record = self.enter(trader)
        self.assertIsNotNone(record)
        self.assertEqual(len(broker.dry_runs), 1)
        self.assertEqual(len(broker.submitted), 1)
        body = broker.submitted[0]
        self.assertEqual((body["order-type"], body["time-in-force"], body["price"], body["price-effect"]),
                         ("Limit", "Day", "1.02", "Debit"))
        self.assertEqual(body["legs"][0]["action"], "Buy to Open")
        self.assertTrue(body["external-identifier"].startswith("optiobot-"))
        self.assertEqual((record["quantity"], record["entry_price"], record["mode"]), (1, 1.02, "LIVE"))
        saved = json.loads((self.root / "live" / "live_account.json").read_text())
        self.assertEqual(len(saved["positions"]), 1)
        self.assertEqual(json.loads((self.root / "live" / "live_open_intents.json").read_text()), {})
        self.assertTrue(trader.reconciled)

    def test_dry_run_warning_blocks_submission(self):
        broker = FakeBroker()
        broker.dry_run_warnings = [{"code": "review"}]
        self.assertIsNone(self.enter(self.trader(broker)))
        self.assertEqual(broker.submitted, [])

    def test_rejected_order_books_nothing(self):
        broker = FakeBroker(fill_mode="reject")
        trader = self.trader(broker)
        self.assertIsNone(self.enter(trader))
        self.assertEqual(trader.account.positions, [])
        self.assertTrue(trader.reconciled)

    def test_unfilled_entry_is_cancelled_and_confirmed_before_returning(self):
        broker = FakeBroker(fill_mode="rest")
        trader = self.trader(broker)
        self.assertIsNone(self.enter(trader))
        self.assertEqual(broker.cancels, [1])
        self.assertEqual(broker.orders[1]["status"], "Cancelled")
        self.assertEqual(trader.account.positions, [])

    def test_unconfirmed_cancellation_blocks_new_entries(self):
        broker = FakeBroker(fill_mode="rest")
        broker.ignore_cancel = True
        trader = self.trader(broker)
        self.assertIsNone(self.enter(trader))
        self.assertFalse(trader.reconciled)
        self.assertTrue(any(key.startswith("order:") for key in trader.entry_block_reasons))
        broker.ignore_cancel = False
        self.assertIsNone(self.enter(trader, option("QQQ   261030C00100000", "QQQ")))
        self.assertEqual(len(broker.submitted), 1, "a second entry must not be sent while an order is unresolved")

    def test_partial_fill_books_filled_quantity_and_cancels_the_rest(self):
        broker = FakeBroker(fill_mode="partial")
        trader = self.trader(broker)
        with patch.dict(self.strategy.rules, {"max_contracts": 3}):
            record = self.enter(trader, quantity=3)
        self.assertEqual(record["quantity"], 1)
        self.assertEqual(broker.orders[1]["status"], "Cancelled")
        self.assertEqual(broker.holdings["SPY   261030C00100000"], 1)
        self.assertTrue(trader.reconciled)

    def test_timeout_after_broker_accepted_is_resolved_by_lookup_not_resubmission(self):
        broker = FakeBroker(fill_mode="timeout_accepted")
        trader = self.trader(broker)
        record = self.enter(trader)
        self.assertEqual(len(broker.submitted), 1)
        self.assertEqual(record["quantity"], 1)
        self.assertTrue(trader.reconciled)

    def test_timeout_with_order_not_found_is_not_resubmitted_and_blocks_entries(self):
        broker = FakeBroker(fill_mode="timeout_lost")
        trader = self.trader(broker)
        self.assertIsNone(self.enter(trader))
        self.assertEqual(len(broker.submitted), 1)
        self.assertFalse(trader.reconciled)
        intents = json.loads((self.root / "live" / "live_open_intents.json").read_text())
        self.assertEqual([i["stage"] for i in intents.values()], ["uncertain_not_found"])

    def test_execution_safety_rejects_stale_invalid_wide_unaffordable_and_killed(self):
        cases = {
            "option_quote_stale_or_invalid": dict(quote=option(age=120)),
            "contract_does_not_match_strategy_decision": dict(decision_option="SPY   261030C00105000"),
            "option_spread_exceeds_strategy_limit": dict(quote=option(bid=1.00, ask=1.30)),
            "symbol_not_ready:WARMING_UP_INDICATORS": dict(readiness={"final_readiness": "WARMING_UP_INDICATORS",
                                                                      "historical_to_live_transition_status": "VERIFIED"}),
            "quantity_outside_strategy_contract_limit": dict(quantity=2),
        }
        for reason, kwargs in cases.items():
            broker = FakeBroker()
            with self.subTest(reason=reason):
                self.assertIsNone(self.enter(self.trader(broker), **kwargs))
                self.assertEqual(broker.submitted, [])
                self.assertEqual(broker.dry_runs, [])
        broker = FakeBroker(buying_power=50.0)
        self.assertIsNone(self.enter(self.trader(broker)))
        self.assertEqual(broker.submitted, [])
        broker = FakeBroker()
        trader = self.trader(broker)
        (self.root / "KILL_SWITCH").write_text("{}")
        self.assertIsNone(self.enter(trader))
        self.assertEqual(broker.submitted, [])

    # --- cash-account settlement rule ---------------------------------------------------------
    def sale(self, trader, when, proceeds_price=2.00, fees=0.70):
        trader.account.ledger.append({"type": "fill", "mode": "LIVE", "side": "SELL", "symbol": "SPY   261030C00100000",
                                      "quantity": 1, "fill_price": proceeds_price, "fees": fees,
                                      "timestamp": when.isoformat()})

    def test_cash_account_entry_needs_settled_cash_not_just_buying_power(self):
        broker = FakeBroker(buying_power=10_000.0, settled_cash=50.0)
        trader = self.trader(broker)
        self.assertTrue(trader.settlement_rule_applies)
        self.assertIsNone(self.enter(trader))
        self.assertEqual((broker.submitted, broker.dry_runs), ([], []))
        self.assertIn("insufficient_settled_funds_cash_account", self.safety(trader))

    def test_same_day_sale_proceeds_cannot_fund_a_new_entry_until_settled(self):
        broker = FakeBroker(buying_power=300.0, settled_cash=10_000.0)
        trader = self.trader(broker)
        now = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)   # Friday
        self.sale(trader, now - timedelta(hours=1))                  # $199.30 net, settles Monday
        self.assertAlmostEqual(trader.unsettled_sale_proceeds(now), 199.30)
        self.assertAlmostEqual(trader.settled_funds_available(now), 100.70)
        self.assertIn("insufficient_settled_funds_cash_account", self.safety(trader, now=now))
        monday = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(trader.settlement_date(now), monday.date())
        self.assertEqual(trader.unsettled_sale_proceeds(monday), 0.0)
        self.assertNotIn("insufficient_settled_funds_cash_account", self.safety(trader, now=monday))

    def test_missing_settled_cash_blocks_cash_account_and_margin_account_is_exempt(self):
        trader = self.trader(FakeBroker(settled_cash="missing"))
        self.assertIn("account", trader.entry_block_reasons)
        broker = FakeBroker(settled_cash=0.0)
        broker.accounts_list[1]["account"]["margin-or-cash"] = "Margin"
        trader = self.trader(broker)
        self.assertFalse(trader.settlement_rule_applies)
        self.assertNotIn("insufficient_settled_funds_cash_account", self.safety(trader))

    def safety(self, trader, now=None, quote=None):
        quote = quote or option()
        return trader.entry_safety_failures(underlying=quote.underlying, quote=quote, quantity=1,
                                            decision_option=quote.symbol, readiness=READY,
                                            now=now or datetime.now(timezone.utc), quote_fresh=lambda q, n: True)

    def test_reconciliation_mismatch_or_foreign_order_blocks_entries(self):
        broker = FakeBroker()
        broker.holdings["SPY   261030P00090000"] = 1  # opened outside the bot on a strategy underlying
        trader = self.trader(broker)
        self.assertFalse(trader.reconciled)
        self.assertIsNone(self.enter(trader, option("QQQ   261030C00100000", "QQQ")))
        self.assertEqual(broker.submitted, [])
        broker = FakeBroker()
        broker.foreign_orders = [{"id": 99, "status": "Live", "underlying-symbol": "SPY"}]
        self.assertFalse(self.trader(broker).reconciled)
        broker = FakeBroker()
        broker.holdings["MSFT  261030C00400000"] = 1  # outside the strategy universe: reported, not blocking
        trader = self.trader(broker)
        self.assertTrue(trader.reconciled)
        self.assertEqual(len(trader.unmanaged_positions), 1)

    # --- exits -----------------------------------------------------------------------------
    def test_exit_sells_at_bid_and_realizes_actual_fill_pnl(self):
        broker = FakeBroker()
        trader = self.trader(broker)
        record = self.enter(trader)
        realized = asyncio.run(trader.close_position(record=record, reason="underlying_profit_target",
                                                     now=datetime.now(timezone.utc),
                                                     requote=lambda r: option(bid=1.50, ask=1.52), quote_fresh=_fresh_quote))
        body = broker.submitted[-1]
        self.assertEqual((body["legs"][0]["action"], body["price"], body["price-effect"]), ("Sell to Close", "1.50", "Credit"))
        self.assertAlmostEqual(realized, (1.50 - 1.02) * 100 - 0.70 - 0.70)
        self.assertEqual(trader.account.positions, [])
        closes = [row for row in trader.account.ledger if row["type"] == "live_close"]
        self.assertEqual(closes[0]["reason"], "underlying_profit_target")
        self.assertEqual(broker.holdings["SPY   261030C00100000"], 0)

    def test_unfilled_exit_reprices_only_after_confirmed_cancel(self):
        broker = FakeBroker()
        trader = self.trader(broker)
        record = self.enter(trader)
        attempts = iter(["rest", "fill"])
        broker.fill_mode = lambda body: next(attempts)
        bids = iter([1.40, 1.35])
        realized = asyncio.run(trader.close_position(record=record, reason="underlying_invalidation_stop",
                                                     now=datetime.now(timezone.utc),
                                                     requote=lambda r: option(bid=next(bids), ask=1.60), quote_fresh=_fresh_quote))
        self.assertIsNotNone(realized)
        sells = [b for b in broker.submitted if b["legs"][0]["action"] == "Sell to Close"]
        self.assertEqual([b["price"] for b in sells], ["1.40", "1.35"])
        self.assertEqual(broker.orders[2]["status"], "Cancelled", "first exit order cancelled before re-pricing")

    def test_exit_never_stacks_on_an_unresolved_exit_order(self):
        broker = FakeBroker()
        trader = self.trader(broker)
        record = self.enter(trader)
        broker.fill_mode, broker.ignore_cancel = "rest", True
        kwargs = dict(record=record, reason="underlying_invalidation_stop", now=datetime.now(timezone.utc),
                      requote=lambda r: option(bid=1.4, ask=1.6), quote_fresh=_fresh_quote)
        self.assertIsNone(asyncio.run(trader.close_position(**kwargs)))
        sent = len(broker.submitted)
        self.assertIsNone(asyncio.run(trader.close_position(**kwargs)))
        self.assertEqual(len(broker.submitted), sent)
        self.assertFalse(trader.protection_verified)

    # --- restart recovery ------------------------------------------------------------------
    def test_restart_books_fills_of_orders_left_open_by_a_crash(self):
        broker = FakeBroker()
        live_dir = self.root / "live"
        live_dir.mkdir()
        body = {"external-identifier": "optiobot-crash", "price": "1.02",
                "legs": [{"symbol": "SPY   261030C00100000", "quantity": 1, "action": "Buy to Open"}]}
        order = broker._accept(body)
        broker._fill(order, 1)
        (live_dir / "live_open_intents.json").write_text(json.dumps({"optiobot-crash": {
            "purpose": "entry", "stage": "submitting", "external_identifier": "optiobot-crash", "order": body,
            "fees_per_contract": 0.7, "booked_quantity": 0, "position_template": self.template(),
            "updated_at_utc": datetime.now(timezone.utc).isoformat()}}))
        trader = self.trader(broker)
        self.assertEqual(len(trader.account.positions), 1)
        self.assertEqual(trader.account.positions[0]["entry_price"], 1.02)
        self.assertEqual(json.loads((live_dir / "live_open_intents.json").read_text()), {})
        self.assertTrue(trader.reconciled)
        self.assertEqual(broker.submitted, [], "recovery never submits")

    def test_restart_cancels_and_confirms_a_working_order_left_by_a_crash(self):
        broker = FakeBroker()
        live_dir = self.root / "live"
        live_dir.mkdir()
        body = {"external-identifier": "optiobot-working", "price": "1.02",
                "legs": [{"symbol": "SPY   261030C00100000", "quantity": 1, "action": "Buy to Open"}]}
        broker._accept(body)
        (live_dir / "live_open_intents.json").write_text(json.dumps({"optiobot-working": {
            "purpose": "entry", "stage": "submitted", "order_id": 1, "external_identifier": "optiobot-working",
            "order": body, "booked_quantity": 0, "position_template": self.template(),
            "updated_at_utc": datetime.now(timezone.utc).isoformat()}}))
        trader = self.trader(broker)
        self.assertEqual(broker.orders[1]["status"], "Cancelled")
        self.assertEqual(trader.account.positions, [])
        self.assertTrue(trader.reconciled)

    def test_parse_fills_averages_multiple_executions(self):
        order = {"legs": [{"fills": [{"quantity": "1", "fill-price": "1.00"}, {"quantity": "3", "fill-price": "1.20"}]}]}
        self.assertEqual(parse_fills(order), (4, 1.15))


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "strategy.txt").write_bytes((ROOT / "strategy.txt").read_bytes())
        self.strategy = parse_strategy(self.root / "strategy.txt")
        write_manifest(self.strategy, self.root / "active_strategy.json",
                       {"status": "research_test", "mode": "PAPER", "live_enabled": False})

    def tearDown(self):
        self.tmp.cleanup()

    def digest(self) -> tuple[str, str]:
        return tuple(hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                     for name in ("strategy.txt", "active_strategy.json"))

    def authorize(self, cfg):
        return authorize_live(root=self.root, cfg=cfg, strategy_hash=self.strategy.sha256,
                              account_suffix=ACCOUNT[-4:], statement=LIVE_AUTHORIZATION_STATEMENT)

    def test_authorization_binds_hash_and_account_without_touching_strategy_files(self):
        cfg = live_cfg(self.root / "data")
        before = self.digest()
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            authorize_live(root=self.root, cfg=cfg, strategy_hash="0" * 64, account_suffix=ACCOUNT[-4:],
                           statement=LIVE_AUTHORIZATION_STATEMENT)
        self.authorize(cfg)
        self.assertEqual(self.digest(), before)
        verify_live_authorization(self.root, self.strategy, ACCOUNT)
        with self.assertRaisesRegex(RuntimeError, "different|differs"):
            verify_live_authorization(self.root, self.strategy, "5WT00002")
        changed = replace(self.strategy, sha256="f" * 64)
        with self.assertRaisesRegex(RuntimeError, "strategy hash differs"):
            verify_live_authorization(self.root, changed, ACCOUNT)

    def test_live_session_refuses_paper_mode_missing_authorization_and_bad_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "TRADING_MODE=LIVE"):
            run_live_session(root=self.root, cfg=AppConfig(mode=Mode.PAPER), interactive=True, prompt=lambda _: "")
        cfg = live_cfg(self.root / "data")
        with self.assertRaisesRegex(RuntimeError, "live_authorization.json not found"):
            run_live_session(root=self.root, cfg=cfg, interactive=True, prompt=lambda _: f"LIVE {ACCOUNT[-4:]}")
        self.authorize(cfg)
        with self.assertRaisesRegex(RuntimeError, "aborted"):
            run_live_session(root=self.root, cfg=cfg, interactive=True, prompt=lambda _: "yes")
        with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
            run_live_session(root=self.root, cfg=cfg, interactive=False)
        with self.assertRaisesRegex(RuntimeError, "explicit enable flag"):
            run_live_session(root=self.root, cfg=replace(cfg, live_enabled=False), interactive=True,
                             prompt=lambda _: f"LIVE {ACCOUNT[-4:]}")

    def test_confirmation_requires_account_suffix(self):
        cfg = live_cfg(self.root)
        confirm_live_start(cfg, prompt=lambda _: f"LIVE {ACCOUNT[-4:]}", interactive=True)
        with self.assertRaises(RuntimeError):
            confirm_live_start(cfg, prompt=lambda _: "LIVE", interactive=True)

    def test_readiness_reports_invalid_credentials_without_raising(self):
        class BadAuth(FakeBroker):
            def _access_token(self):
                raise BrokerError('tastytrade HTTP 401 for POST /oauth/token: {"error":"invalid_grant"} (status=401)')
        report = live_readiness(root=self.root, cfg=live_cfg(self.root / "data"), market_data=False, client=BadAuth())
        self.assertTrue(any(item.startswith("oauth_access_token") and "invalid_grant" in item for item in report["blockers"]))
        self.assertEqual(report["orders_submitted"], 0)

    def test_readiness_flags_underfunded_account_and_cash_settlement_without_orders(self):
        broker = FakeBroker(net_liq=50.0, buying_power=50.0)
        self.authorize(live_cfg(self.root / "data"))
        report = live_readiness(root=self.root, cfg=live_cfg(self.root / "data"), market_data=False, client=broker)
        self.assertEqual(report["strategy_risk_budget_per_trade"], 5.0)
        rule = next(c for c in report["checks"] if c["check"] == "cash_account_settlement_rule")
        self.assertEqual((rule["status"], report["balances"]["settled_cash"]), ("PASS", 50.0))
        self.assertIn("ENFORCED", rule["detail"])
        broker.settled_cash = "missing"
        report = live_readiness(root=self.root, cfg=live_cfg(self.root / "data"), market_data=False, client=broker)
        self.assertTrue(any(item.startswith("cash_account_settlement_rule") for item in report["blockers"]))
        self.assertEqual((broker.submitted, broker.dry_runs), ([], []))


class ClientTokenTests(unittest.TestCase):
    def test_expired_access_token_is_renewed_with_the_refresh_token(self):
        client = TastytradeClient("x", "y", "z", base_url="https://invalid.local")
        grants = []

        def fake_request(method, path, *, body=None, auth=True):
            if path == "/oauth/token":
                grants.append(body["grant_type"])
                return {"access_token": f"a{len(grants)}", "expires_in": 900}
            return {}
        with patch.object(client, "_request", side_effect=fake_request):
            self.assertEqual(client._access_token(), "a1")
            self.assertEqual(client._access_token(), "a1")
            client._token.expires_at = 0  # expired
            self.assertEqual(client._access_token(), "a2")
        self.assertEqual(grants, ["refresh_token", "refresh_token"])

    def test_submit_timeout_and_5xx_are_uncertain_not_failed(self):
        client = TastytradeClient("x", "y", "z", base_url="https://invalid.local")
        good = {"data": {"order": {"id": 1}, "warnings": [], "errors": []}}
        order = {"external-identifier": "optiobot-1"}
        for error in (TransportError("timed out"), BrokerError("tastytrade HTTP 503 (status=503)")):
            with self.subTest(error=error), patch.object(client, "_request", side_effect=error):
                with self.assertRaises(UncertainOrderSubmission):
                    client.submit_order("acct", order, dry_run_result=good)
        with patch.object(client, "_request", side_effect=BrokerError("HTTP 422 (status=422)")):
            with self.assertRaises(BrokerError) as caught:
                client.submit_order("acct", order, dry_run_result=good)
            self.assertNotIsInstance(caught.exception, UncertainOrderSubmission)


class SharedLoopTests(unittest.TestCase):
    """PAPER and LIVE run the same loop; identical market data must yield identical strategy decisions."""

    @staticmethod
    def _bars():
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

    def _run(self, root: Path, *, live: bool, history_gap: bool = False):
        from trader import paper_runner
        now = datetime.now(timezone.utc)
        open_utc = now.replace(second=0, microsecond=0) - timedelta(minutes=22)
        template = self._bars()
        shift = open_utc - template[0].timestamp
        session = [Bar(b.timestamp + shift, b.open, b.high, b.low, b.close, b.volume) for b in template]
        history, breakout = session[:-1], session[-1]
        if history_gap:
            history = history[:5] + history[6:]

        class FakeCalendar:
            def session_bounds(self, day):
                return (open_utc.astimezone(ZoneInfo("America/New_York")),
                        (datetime.now(timezone.utc) + timedelta(seconds=4)).astimezone(ZoneInfo("America/New_York")))
            def is_open(self, at): return True
            def entry_window_ok(self, at, start, end): return True

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
                rows = [{"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "volume": b.volume} for b in history if start <= b.timestamp <= end]
                return types.SimpleNamespace(candles=rows, audit={}, provider="fixture", retrieval_method="fixture",
                                             retrieved_at_utc=now, requested_start_utc=start, requested_end_utc=end)

        async def fake_live(url, token, symbols):
            for symbol in symbols:
                yield symbol, breakout
            await asyncio.sleep(3600)

        def fake_options(client, *, underlying, spot, now, min_dte, max_dte):
            return [OptionQuote(f"{underlying}  261030C00100000", underlying, now + timedelta(days=12), 100.0, "C",
                                4.00, 4.05, delta=.55, gamma=.01, theta=-.02, vega=.1, volume=500, open_interest=1000,
                                quote_timestamp=datetime.now(timezone.utc))]

        (root / "strategy.txt").write_bytes((ROOT / "strategy.txt").read_bytes())
        strategy = parse_strategy(root / "strategy.txt")
        write_manifest(strategy, root / "active_strategy.json", {"status": "research_test", "mode": "PAPER", "live_enabled": False})
        no_flat = replace(strategy, execution={**strategy.execution, "flatten_at_session_close": False})
        broker = FakeBroker(net_liq=10_000.0, buying_power=10_000.0)
        trader = None
        if live:
            cfg = live_cfg(root / "data" / "live")
            trader = LiveTrader(cfg=cfg, client=broker, strategy=strategy, data_dir=cfg.data_dir,
                                kill_switch_path=root / "KILL_SWITCH", settings=FAST)
            trader.startup()
        else:
            cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
        with patch.object(paper_runner, "MarketCalendar", FakeCalendar), \
                patch.object(paper_runner, "TastytradeClient", FakeClient), \
                patch.object(paper_runner, "ProductionMarketData", FakeQuotes), \
                patch.object(paper_runner, "create_history_provider", lambda *a, **k: FakeHistory()), \
                patch.object(paper_runner, "live_minute_bars", fake_live), \
                patch.object(paper_runner, "fetch_option_quotes", fake_options), \
                patch.object(paper_runner, "ENTRY_ARBITRATION_SECONDS", 0.05):
            try:
                asyncio.run(paper_runner._run(root=root, cfg=cfg, strategy=no_flat, symbols=("SPY", "IWM"), live=trader))
            except RuntimeError as exc:
                if "Every symbol in the session was disabled" not in str(exc):
                    raise
            finally:
                paper_runner._close_session_log_file(paper_runner._session_logger(cfg.data_dir))
        decisions = [json.loads(line) for line in (cfg.data_dir / "decisions.jsonl").read_text().splitlines()] \
            if (cfg.data_dir / "decisions.jsonl").exists() else []
        return decisions, broker, cfg

    @staticmethod
    def _key(rows):
        fields = ("underlying", "decision", "reason")
        diag = ("option_candidate", "quantity", "expected_net_r", "modeled_risk_dollars", "underlying_stop_trigger",
                "underlying_target_trigger", "risk_budget_dollars")
        return sorted((tuple(r[f] for f in fields), tuple(r["diagnostics"].get(d) for d in diag)) for r in rows)

    def test_identical_market_data_produces_identical_decisions_and_live_orders_match_paper_entries(self):
        with tempfile.TemporaryDirectory() as paper_dir, tempfile.TemporaryDirectory() as live_dir:
            paper_decisions, paper_broker, paper_cfg = self._run(Path(paper_dir), live=False)
            live_decisions, broker, live_cfg_used = self._run(Path(live_dir), live=True)
            self.assertTrue(paper_decisions)
            self.assertEqual(self._key(paper_decisions), self._key(live_decisions))
            paper_entries = sorted(r["diagnostics"]["option_candidate"] for r in paper_decisions if r["decision"] == "PAPER_ENTRY")
            self.assertEqual(len(paper_entries), 2)
            live_orders = sorted(b["legs"][0]["symbol"] for b in broker.submitted)
            self.assertEqual(live_orders, paper_entries, "LIVE submits exactly the contracts the strategy selected")
            self.assertTrue(all(b["legs"][0]["quantity"] == 1 and b["price"] == "4.05" for b in broker.submitted))
            self.assertEqual(paper_broker.submitted, [], "PAPER never touches a broker")
            # Separation: LIVE writes only under data/live, PAPER ledger untouched by LIVE and vice versa.
            self.assertFalse((Path(live_dir) / "data" / "paper_account.json").exists())
            self.assertTrue((live_cfg_used.data_dir / "live_account.json").exists())
            self.assertFalse((paper_cfg.data_dir / "live").exists())
            live_ledger = json.loads((live_cfg_used.data_dir / "live_account.json").read_text())
            self.assertEqual(sorted(p["underlying"] for p in live_ledger["positions"]), ["IWM", "SPY"])
            report = json.loads(next((live_cfg_used.data_dir / "recovery_reports").glob("*-SPY.json")).read_text())
            self.assertEqual((report["mode"], report["broker_order_submission"]), ("LIVE", "ENABLED_LIVE"))

    def test_live_with_missing_history_candles_submits_nothing(self):
        with tempfile.TemporaryDirectory() as live_dir:
            decisions, broker, _ = self._run(Path(live_dir), live=True, history_gap=True)
            self.assertEqual(broker.submitted, [])
            self.assertEqual(broker.dry_runs, [])

    def test_live_kill_switch_blocks_strategy_entries(self):
        with tempfile.TemporaryDirectory() as live_dir:
            (Path(live_dir) / "KILL_SWITCH").write_text("{}")
            decisions, broker, _ = self._run(Path(live_dir), live=True)
            self.assertEqual(broker.submitted, [])
            self.assertTrue(any(r["reason"] == "kill_switch_active" for r in decisions))


if __name__ == "__main__":
    unittest.main()
