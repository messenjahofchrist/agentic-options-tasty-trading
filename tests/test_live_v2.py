"""LIVE PROFESSIONAL_STRATEGY_V2 (owner request 2026-11-15): the active V2 version runs through the same loop as
PAPER, with broker execution. Every broker interaction is the in-memory fake from tests/test_live.py; no test can
reach tastytrade, and nothing here turns LIVE on."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trader import paper_runner
from trader.account_profiles import SMALL
from trader.config import AppConfig, Mode
from trader.live_execution import LIVE_AUTHORIZATION_STATEMENT, verify_live_authorization
from trader.live_session import (LiveAlreadyRunning, _acquire_live_lock, authorize_live, disable_live_autostart,
                                 enable_live_autostart, live_plan, live_readiness, run_live_session)
from trader.strategy_loader import parse_strategy
from trader.v2 import config as v2config
from trader.v2.live_support import unsupported_for_live
from tests.test_live import ACCOUNT, FakeBroker, live_cfg
from tests.test_profiles import make_root
from tests.test_v2_engine import V2EngineTests

ROOT = Path(__file__).resolve().parents[1]
_NO_EMAIL = patch("trader.notifications.email_configured", return_value=False)


def setUpModule():  # alerts raised by these tests must never reach a real mailbox
    _NO_EMAIL.start()


def tearDownModule():
    _NO_EMAIL.stop()


class ActiveVersionTests(unittest.TestCase):
    """The owner's rule: every new version must also run LIVE. This fails when the ACTIVE version turns on a V2
    feature the broker path cannot execute, so the LIVE path is updated in the same piece of work."""

    def test_the_active_v2_version_can_run_live(self):
        config = v2config.load_v2_config(ROOT)
        self.assertEqual(unsupported_for_live(config), [], f"V2 {config.version} uses features LIVE cannot execute")

    def test_unsupported_features_are_named(self):
        config = v2config.load_v2_config(ROOT)
        data = json.loads(json.dumps(config.data))
        data["management"]["partial_profit"]["enabled"] = True
        data["in_play"] = {"enabled": True}
        problems = unsupported_for_live(replace(config, data=data))
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("partial_profit" in p for p in problems) and any("in_play" in p for p in problems))


class LiveV2LoopTests(unittest.TestCase):
    session = V2EngineTests._session

    def test_live_v2_buys_the_contract_paper_v2_selects_and_books_the_broker_fill(self):
        with tempfile.TemporaryDirectory() as paper_dir, tempfile.TemporaryDirectory() as live_dir:
            paper_rows, paper, _, _, _ = self.session(Path(paper_dir))
            rows, ledger, shadow, _, data = self.session(Path(live_dir), live=True)
            broker = self.broker
            wrote_shadow = (data / "v2_shadow_original").exists()
            wrote_paper = (Path(live_dir) / "data" / "paper_account.json").exists()
        outcomes = lambda items: [r["outcome"] for r in items if r.get("type") == "candidate"]
        self.assertIn("APPROVED", outcomes(rows))
        self.assertEqual(outcomes(rows)[:outcomes(rows).index("APPROVED") + 1],
                         outcomes(paper_rows)[:outcomes(paper_rows).index("APPROVED") + 1],
                         "LIVE and PAPER reach the same V2 decisions on the same bars")
        buys = [b for b in broker.submitted if b["legs"][0]["action"] == "Buy to Open"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["legs"][0]["symbol"], paper.positions[0]["symbol"])
        self.assertEqual((buys[0]["legs"][0]["quantity"], buys[0]["price"], buys[0]["order-type"]), (1, "2.00", "Limit"))
        self.assertEqual(len(broker.dry_runs), len(broker.submitted), "every order is dry-run first")
        self.assertEqual(len(ledger.positions), 1)
        position = ledger.positions[0]
        self.assertEqual((position["mode"], position["strategy_id"], position["quantity"], position["entry_price"]),
                         ("LIVE", "PROFESSIONAL_STRATEGY_V2", 1, 2.0))
        self.assertEqual(position["strategy_version"], paper.positions[0]["strategy_version"])
        for key in ("stop_underlying", "target_underlying", "entry_underlying"):
            self.assertEqual(position[key], paper.positions[0][key])
        self.assertIn(position["v2_entry"]["setup_type"], ("CONFIRMED", "PULLBACK_CONFIRMED"))
        self.assertIn("best_underlying", position["v2_management"])   # V2 management ran on the LIVE position
        self.assertTrue(any(r.get("type") == "entry" for r in rows))
        self.assertIsNone(shadow)
        self.assertFalse(wrote_shadow, "LIVE keeps no simulated benchmark ledger")
        self.assertFalse(wrote_paper, "LIVE never writes a paper ledger")

    def test_live_v2_exit_is_a_real_sell_order_booked_from_the_broker_fill(self):
        with tempfile.TemporaryDirectory() as live_dir:
            _, ledger, _, _, _ = self.session(Path(live_dir), live=True, run_up=True,
                                              overrides={"let_winners_run": {"enabled": False},
                                                         "trailing": {"enabled": False}})
            broker = self.broker
        sells = [b for b in broker.submitted if b["legs"][0]["action"] == "Sell to Close"]
        self.assertGreaterEqual(len(sells), 1)
        self.assertEqual((sells[0]["price"], sells[0]["price-effect"]), ("2.10", "Credit"))   # the live bid
        closes = [e for e in ledger.ledger if e.get("type") == "live_close"]
        self.assertEqual(closes[0]["reason"], "underlying_profit_target")
        self.assertAlmostEqual(closes[0]["realized_pnl"], (2.10 - 2.00) * 100 - 1.40, places=2)

    def test_live_v2_runner_holds_past_the_target_like_paper(self):
        with tempfile.TemporaryDirectory() as live_dir:
            _, ledger, _, _, _ = self.session(Path(live_dir), live=True, run_up=True,
                                              overrides={"let_winners_run": {"enabled": True},
                                                         "trailing": {"enabled": False}})
            broker = self.broker
        self.assertEqual([b for b in broker.submitted if b["legs"][0]["action"] == "Sell to Close"], [])
        self.assertEqual(len(ledger.positions), 1)
        self.assertTrue(ledger.positions[0]["v2_management"]["runner"])

    def test_live_v2_kill_switch_sends_no_order(self):
        with tempfile.TemporaryDirectory() as live_dir:
            rows, ledger, _, _, _ = self.session(Path(live_dir), live=True, kill_switch=True)
            broker = self.broker
        self.assertEqual((broker.submitted, broker.dry_runs, ledger.positions), ([], [], []))

    def test_live_refuses_a_v2_feature_it_cannot_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), universe=["SPY"])
            shutil.copytree(ROOT / "strategies", root / "strategies")
            config = v2config.load_v2_config(root)
            data = json.loads(json.dumps(config.data))
            data["management"]["partial_profit"]["enabled"] = True
            with self.assertRaisesRegex(RuntimeError, "LIVE cannot execute"):
                asyncio.run(paper_runner._run(root=root, cfg=AppConfig(data_dir=root / "data"), strategy=config.base,
                                              symbols=("SPY",), live=object(), v2=replace(config, data=data)))


class LiveV2GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name), universe=["SPY"])
        shutil.copytree(ROOT / "strategies", self.root / "strategies")
        (self.root / v2config.MANIFEST_FILE).unlink(missing_ok=True)
        self.v2 = v2config.load_v2_config(self.root)
        v2config.write_v2_manifest(self.root, self.v2, approved_by="test", note="live v2 gate test")
        self.strategy = parse_strategy(self.root / "strategy.txt")
        self.cfg = live_cfg(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def authorize(self, **kw):
        return authorize_live(root=self.root, cfg=self.cfg, account_suffix=ACCOUNT[-4:],
                              statement=LIVE_AUTHORIZATION_STATEMENT, **kw)

    def test_v2_authorization_binds_the_version_hash_and_the_account_profile(self):
        with self.assertRaisesRegex(RuntimeError, "does not match"):   # the original strategy's hash is not V2's
            self.authorize(strategy_hash=self.strategy.sha256, v2=self.v2, profile_name="small")
        path = self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
        record = json.loads(path.read_text())
        self.assertEqual((record["v2_version"], record["v2_sha256"], record["account_profile"]),
                         (self.v2.version, self.v2.sha256, SMALL))
        verify_live_authorization(self.root, self.strategy, ACCOUNT, v2=self.v2, profile=SMALL)
        with self.assertRaisesRegex(RuntimeError, "account profile"):
            verify_live_authorization(self.root, self.strategy, ACCOUNT, v2=self.v2, profile=None)
        with self.assertRaisesRegex(RuntimeError, "differs from the authorized V2"):   # a new version: new hash
            verify_live_authorization(self.root, self.strategy, ACCOUNT, v2=replace(self.v2, sha256="f" * 64), profile=SMALL)
        with self.assertRaisesRegex(RuntimeError, "not the original strategy"):
            verify_live_authorization(self.root, self.strategy, ACCOUNT)

    def test_an_original_strategy_authorization_does_not_cover_v2(self):
        self.authorize(strategy_hash=self.strategy.sha256)
        verify_live_authorization(self.root, self.strategy, ACCOUNT)
        with self.assertRaisesRegex(RuntimeError, "differs from the authorized V2"):
            run_live_session(root=self.root, cfg=self.cfg, interactive=True, prompt=lambda _: f"LIVE {ACCOUNT[-4:]}",
                             v2=self.v2, profile_name="small")

    def test_live_v2_needs_live_mode_an_activated_version_and_the_owner_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "TRADING_MODE=LIVE"):
            run_live_session(root=self.root, cfg=AppConfig(mode=Mode.PAPER), interactive=True, prompt=lambda _: "",
                             v2=self.v2, profile_name="small")
        with self.assertRaisesRegex(RuntimeError, "live_authorization.json not found"):
            run_live_session(root=self.root, cfg=self.cfg, interactive=True, prompt=lambda _: f"LIVE {ACCOUNT[-4:]}",
                             v2=self.v2, profile_name="small")
        self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
        with self.assertRaisesRegex(RuntimeError, "aborted"):
            run_live_session(root=self.root, cfg=self.cfg, interactive=True, prompt=lambda _: "yes",
                             v2=self.v2, profile_name="small")
        with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
            run_live_session(root=self.root, cfg=self.cfg, interactive=False, v2=self.v2, profile_name="small")
        (self.root / v2config.MANIFEST_FILE).unlink()
        with self.assertRaisesRegex(v2config.V2ConfigError, "not activated"):
            run_live_session(root=self.root, cfg=self.cfg, interactive=True, prompt=lambda _: f"LIVE {ACCOUNT[-4:]}",
                             v2=self.v2, profile_name="small")

    def alerts(self):
        path = self.root / "data" / "live" / "critical_alerts.jsonl"
        return [json.loads(line)["event"] for line in path.read_text().splitlines()] if path.exists() else []

    def test_unattended_start_needs_the_owner_standing_order_and_still_the_authorization(self):
        """Owner order 2026-11-18: LIVE trades daily without intervention until the owner says stop."""
        def no_prompt(_):
            raise AssertionError("an unattended start never asks for the typed confirmation")
        started = []
        with patch("trader.live_session._run_live_once", side_effect=lambda **kw: started.append(kw)):
            enable_live_autostart(root=self.root, cfg=self.cfg, note="test")
            with self.assertRaisesRegex(RuntimeError, "live_authorization.json not found"):   # still required
                run_live_session(root=self.root, cfg=self.cfg, prompt=no_prompt, v2=self.v2, profile_name="small",
                                 unattended=True)
            self.assertEqual(self.alerts(), ["live_autostart_blocked"], "a blocked unattended start alerts the owner")
            self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
            run_live_session(root=self.root, cfg=self.cfg, prompt=no_prompt, v2=self.v2, profile_name="small",
                             unattended=True)
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0]["cfg"].data_dir, self.root / "data" / "live")
            result = disable_live_autostart(root=self.root, cfg=self.cfg)   # the owner says stop
            self.assertEqual((result["autostart_was_on"], result["autostart_on"]), (True, False))
            expectation = json.loads((self.root / "data" / "live" / "engine_expectation.json").read_text())
            self.assertEqual((expectation["expected_running"], expectation["state"]), (False, "STOPPED"))
            with self.assertRaisesRegex(RuntimeError, "no owner standing order"):
                run_live_session(root=self.root, cfg=self.cfg, prompt=no_prompt, v2=self.v2, profile_name="small",
                                 unattended=True)
            self.assertEqual(len(started), 1)
            with self.assertRaisesRegex(RuntimeError, "interactive terminal"):   # without --unattended: unchanged
                run_live_session(root=self.root, cfg=self.cfg, interactive=False, v2=self.v2, profile_name="small")

    def test_a_standing_order_for_another_account_does_not_start_live(self):
        self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
        enable_live_autostart(root=self.root, cfg=replace(self.cfg, account_number="5WT99999"))
        with patch("trader.live_session._run_live_once") as run_once:
            with self.assertRaisesRegex(RuntimeError, "different account"):
                run_live_session(root=self.root, cfg=self.cfg, v2=self.v2, profile_name="small", unattended=True)
        run_once.assert_not_called()

    def test_a_second_live_session_on_the_same_pc_is_refused(self):
        self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
        enable_live_autostart(root=self.root, cfg=self.cfg)
        held = _acquire_live_lock(self.root / "data" / "live")   # e.g. a session started by hand in a terminal
        try:
            with patch("trader.live_session._run_live_once") as run_once:
                with self.assertRaises(LiveAlreadyRunning):
                    run_live_session(root=self.root, cfg=self.cfg, v2=self.v2, profile_name="small", unattended=True)
                with self.assertRaises(LiveAlreadyRunning):
                    run_live_session(root=self.root, cfg=self.cfg, interactive=True, v2=self.v2, profile_name="small",
                                     prompt=lambda _: f"LIVE {ACCOUNT[-4:]}")
            run_once.assert_not_called()
            self.assertEqual(self.alerts(), [], "LIVE already running is not a failure")
        finally:
            held.close()
        with patch("trader.live_session._run_live_once") as run_once:
            run_live_session(root=self.root, cfg=self.cfg, v2=self.v2, profile_name="small", unattended=True)
        run_once.assert_called_once()

    def test_the_small_profile_gives_live_the_small_account_rules(self):
        strategy, policy = live_plan(self.root, self.strategy, self.v2, "small")
        self.assertEqual((policy.name, policy.allocation_fraction), (SMALL, 0.40))
        self.assertEqual(tuple(strategy.universe), ("SPY",))
        self.assertEqual(strategy.risk["max_daily_loss_fraction"], 0.20)
        self.assertEqual(strategy.sha256, self.strategy.sha256)
        self.assertFalse((self.root / "data" / "profiles").exists(), "the PAPER profile ledger is not touched")
        with self.assertRaisesRegex(RuntimeError, "only with --strategy v2"):
            live_plan(self.root, self.strategy, None, "small")

    def test_readiness_for_v2_reports_the_profile_budget_and_sends_no_order(self):
        broker = FakeBroker(net_liq=1050.0, buying_power=1050.0)
        report = live_readiness(root=self.root, cfg=self.cfg, market_data=False, client=broker, v2=self.v2,
                                profile_name="small")
        checks = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(checks["v2_version_ready_for_live"], "PASS")
        self.assertEqual(checks["owner_live_authorization"], "FAIL")
        self.assertEqual(report["strategy"]["v2"]["version"], self.v2.version)
        self.assertAlmostEqual(report["strategy_risk_budget_per_trade"], 420.0)   # 40% of $1,050; no dollar cap here
        self.assertEqual(report["account_profile"]["rules"]["name"], SMALL)
        self.authorize(strategy_hash=self.v2.sha256, v2=self.v2, profile_name="small")
        report = live_readiness(root=self.root, cfg=self.cfg, market_data=False, client=broker, v2=self.v2,
                                profile_name="small")
        self.assertEqual({c["check"]: c["status"] for c in report["checks"]}["owner_live_authorization"], "PASS")
        self.assertEqual((broker.submitted, broker.dry_runs, report["orders_submitted"]), ([], [], 0))


if __name__ == "__main__":
    unittest.main()
