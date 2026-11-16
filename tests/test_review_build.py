"""2026-11-11 build: vendor-neutral agents, cost gate in R, advisor veto, cost model."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader import agents, cost_model
from trader.v2 import pipeline
from trader.v2.vetoes import active_veto

from tests.test_v2 import PipelineTests as _Base
from trader.models import OptionQuote

UTC = timezone.utc


class CostGateAndVetoTests(_Base):
    """The base fixture's contract: bid 4.00 / ask 4.05, delta 0.55, stop 0.40 -> cost (5 + 1.40 + 0.04) / 22 = 0.29R."""

    def data(self, *, limit=0.20, enforce=False, veto_enforce=True):
        data = json.loads(json.dumps(self.c.data))
        data["cost_gate"] = {"enabled": True, "max_cost_r": {"ORIGINAL_ACCOUNT_PAPER": limit, "SMALL_ACCOUNT_PAPER": limit},
                             "enforce": {"ORIGINAL_ACCOUNT_PAPER": enforce, "SMALL_ACCOUNT_PAPER": False},
                             "replaces_expected_net_r_gate": True}
        data["ai_veto"] = {"enabled": True, "enforce": veto_enforce, "max_age_minutes": 90}
        return data

    def veto_file(self, **over):
        veto = {"symbol": "NVDA", "side": None, "until_utc": (self.now + timedelta(hours=1)).isoformat(),
                "reason": "guidance cut this morning"}
        veto.update(over)
        return {"generated_at_utc": (self.now - timedelta(minutes=10)).isoformat(), "agent": "claude", "note_id": "n1",
                "vetoes": [veto]}

    def test_cost_gate_logs_without_blocking_when_not_enforced(self):
        report = pipeline.evaluate_candidate(self.inputs(v2=self.data()))
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        gate = report.stages["position_sizing"]["cost_gate"]
        self.assertAlmostEqual(gate["cost_r"], 6.44 / 22, places=3)
        self.assertTrue(gate["over_limit"])
        self.assertFalse(gate["enforced"])
        self.assertAlmostEqual(report.entry["cost_r"], 6.44 / 22, places=3)
        json.dumps(pipeline.report_record(report))

    def test_cost_gate_blocks_only_when_enforced_and_over_the_limit(self):
        blocked = pipeline.evaluate_candidate(self.inputs(v2=self.data(enforce=True)))
        self.assertEqual(blocked.outcome, "REJECTED")
        self.assertIn("cost_gate", blocked.reasons[0])
        allowed = pipeline.evaluate_candidate(self.inputs(v2=self.data(limit=0.35, enforce=True)))
        self.assertEqual(allowed.outcome, "APPROVED", allowed.reasons)
        # the other profile's switch is separate
        small = pipeline.evaluate_candidate(self.inputs(v2=self.data(enforce=True), profile_name="SMALL_ACCOUNT_PAPER"))
        self.assertNotIn("cost_gate", " ".join(small.reasons))

    def test_enforced_cost_gate_takes_the_place_of_the_expected_net_r_gate(self):
        strategy = self.strategy
        high = type(strategy)(**{**strategy.__dict__, "execution": {**strategy.execution, "min_expected_net_r": 99.0}}) \
            if hasattr(strategy, "__dict__") else None
        if high is None:
            self.skipTest("strategy object cannot be copied")
        old = pipeline.evaluate_candidate(self.inputs(v2=self.data(limit=0.35, enforce=False), strategy=high))
        self.assertEqual(old.outcome, "REJECTED")
        self.assertIn("expected net R", old.reasons[0])
        new = pipeline.evaluate_candidate(self.inputs(v2=self.data(limit=0.35, enforce=True), strategy=high))
        self.assertEqual(new.outcome, "APPROVED", new.reasons)

    def test_without_the_new_sections_nothing_changes(self):
        data = {k: v for k, v in json.loads(json.dumps(self.c.data)).items() if k not in ("cost_gate", "ai_veto")}
        report = pipeline.evaluate_candidate(self.inputs(v2=data, vetoes=self.veto_file()))
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        self.assertNotIn("cost_gate", report.stages["position_sizing"])
        self.assertIsNone(report.entry["cost_r"])

    def test_veto_blocks_its_symbol_and_costs_no_option_requests(self):
        report = pipeline.evaluate_candidate(self.inputs(v2=self.data(), vetoes=self.veto_file()))
        self.assertEqual(report.outcome, "REJECTED")
        self.assertIn("ai_veto", report.reasons[0])
        self.assertEqual(self.calls, 0)

    def test_veto_does_not_apply_to_another_side_symbol_or_time(self):
        for doc in (self.veto_file(side="PUT"), self.veto_file(symbol="AAPL"),
                    self.veto_file(until_utc=(self.now - timedelta(minutes=1)).isoformat()),
                    self.veto_file(reason=""), None, {"vetoes": "bad"},
                    dict(self.veto_file(), generated_at_utc=(self.now - timedelta(minutes=91)).isoformat()),
                    dict(self.veto_file(), generated_at_utc=(self.now + timedelta(hours=1)).isoformat())):
            report = pipeline.evaluate_candidate(self.inputs(v2=self.data(), vetoes=doc))
            self.assertEqual(report.outcome, "APPROVED", (doc, report.reasons))

    def test_veto_not_enforced_is_recorded_only(self):
        report = pipeline.evaluate_candidate(self.inputs(v2=self.data(veto_enforce=False), vetoes=self.veto_file(side="CALL")))
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        self.assertEqual(report.stages["technical_signal"]["ai_veto_not_enforced"]["reason"], "guidance cut this morning")


class ContractPolicyTests(_Base):
    def chain(self):
        self.calls += 1

        def quote(day, ask):
            return OptionQuote(f"NVDA  2611{day:02d}C00102000", "NVDA", datetime(2026, 11, day, tzinfo=UTC), 102, "C",
                               ask - 0.05, ask, 20, 20, 0.55, 0.08, -0.15, 0.10, 0.20, None, 500, 5000,
                               self.now - timedelta(seconds=2))
        return [quote(6, 4.05), quote(13, 5.05), quote(28, 6.05)]   # 11, 18 and 33 days out from 10/26

    def picked(self, policy=None):
        data = json.loads(json.dumps(self.c.data))
        data.pop("contract_policy", None)
        if policy is not None:
            data["contract_policy"] = policy
        report = pipeline.evaluate_candidate(self.inputs(v2=data))
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        return report.stages["option_pricing"]["dte"]

    def test_absent_or_disabled_policy_keeps_the_strategy_range(self):
        self.assertIn(self.picked(), (11, 18))
        self.assertEqual(self.picked({"enabled": False, "min_dte": 30, "max_dte": 40, "nearest_expiry_first": True}),
                         self.picked())
        self.assertEqual(pipeline.contract_range({}, self.strategy)[:2],
                         (int(self.strategy.rules["min_dte"]), int(self.strategy.rules["max_dte"])))

    def test_enabled_policy_sets_the_range_and_can_prefer_the_nearest_expiry(self):
        self.assertEqual(self.picked({"enabled": True, "min_dte": 25, "max_dte": 40, "nearest_expiry_first": False}), 33)
        self.assertEqual(self.picked({"enabled": True, "min_dte": 5, "max_dte": 40, "nearest_expiry_first": True}), 11)

    def test_config_rejects_a_reversed_range(self):
        from trader.v2.config import V2ConfigError, load_v2_config
        root = Path(__file__).resolve().parents[1]
        data = json.loads(json.dumps(self.c.data))
        data["contract_policy"] = {"enabled": False, "min_dte": 9, "max_dte": 3, "nearest_expiry_first": True}
        folder = Path(tempfile.mkdtemp())
        (folder / "v.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(V2ConfigError):
            load_v2_config(root, folder / "v.json")


class VetoFileTests(unittest.TestCase):
    def test_active_veto_requires_fresh_file_reason_and_time(self):
        now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
        doc = {"generated_at_utc": (now - timedelta(minutes=5)).isoformat(),
               "vetoes": [{"symbol": "NFLX", "side": "CALL", "until_utc": (now + timedelta(minutes=30)).isoformat(), "reason": "x"},
                          "garbage", {"symbol": "NFLX"}]}
        self.assertEqual(active_veto(doc, "NFLX", "CALL", now, 90)["side"], "CALL")
        self.assertIsNone(active_veto(doc, "NFLX", "PUT", now, 90))
        self.assertIsNone(active_veto(doc, "NFLX", "CALL", now + timedelta(minutes=31), 90))
        self.assertIsNone(active_veto(doc, "NFLX", "CALL", now, 4))
        self.assertIsNone(active_veto({"vetoes": []}, "NFLX", "CALL", now, 90))


class AgentTests(unittest.TestCase):
    def root(self, config):
        folder = Path(tempfile.mkdtemp())
        (folder / "agents.json").write_text("// comment\n" + json.dumps(config), encoding="utf-8")
        return folder

    CONFIG = {"roles": {"advisor": ["first", "second"], "solo": "second"},
              "agents": {"first": {"command": ["first-bot"]}, "second": {"command": ["second-bot", "{output_file}"],
                                                                         "output_from": "file"}}}

    def test_role_falls_back_to_the_next_agent_and_validates(self):
        calls = []

        def runner(name, prompt, *, config, timeout):
            calls.append(name)
            return "not json" if name == "first" else 'ok: {"value": 3}'

        def validate(data):
            if data["value"] != 3:
                raise ValueError("bad")
        reply = agents.ask_json("advisor", "p", validate=validate, root=self.root(self.CONFIG), runner=runner)
        self.assertEqual((reply.agent, reply.data, reply.attempts), ("second", {"value": 3}, 3))
        self.assertEqual(calls, ["first", "first", "second"])
        self.assertEqual(len(reply.errors), 2)

    def test_all_agents_failing_raises_with_every_error(self):
        def runner(name, prompt, *, config, timeout):
            raise agents.AgentError("down")
        with self.assertRaises(agents.AgentError) as caught:
            agents.ask_json("advisor", "p", root=self.root(self.CONFIG), runner=runner)
        self.assertIn("first", str(caught.exception))
        self.assertIn("second", str(caught.exception))

    def test_config_errors_are_reported(self):
        for bad in ({"roles": {"advisor": "nobody"}, "agents": {}},
                    {"roles": {}, "agents": {"x": {"command": []}}},
                    {"roles": {}, "agents": {"x": {"command": ["x"], "output_from": "file"}}},
                    {"roles": {}, "agents": {"x": {"command": ["x"], "prompt_via": "argument"}}}):
            with self.assertRaises(agents.AgentError):
                agents.load_config(self.root(bad))
        with self.assertRaises(agents.AgentError):
            agents.agents_for("missing", agents.load_config(self.root(self.CONFIG)))

    def test_project_agents_file_is_valid_and_agents_get_no_secrets(self):
        config = agents.load_config(Path(__file__).resolve().parents[1])
        self.assertIn("advisor", config["roles"])
        import os
        os.environ["TASTY_REFRESH_TOKEN"] = "secret"
        try:
            self.assertNotIn("TASTY_REFRESH_TOKEN", agents.safe_environment())
        finally:
            del os.environ["TASTY_REFRESH_TOKEN"]


class CostModelTests(unittest.TestCase):
    def test_cost_in_r_and_net_r(self):
        cost = cost_model.round_trip_cost(4.00, 4.05, round_trip_fees=1.40, slippage_per_contract=0.02)
        self.assertAlmostEqual(cost, 6.44)
        self.assertAlmostEqual(cost_model.cost_in_r(cost, delta=-0.55, stop_distance=0.4), 6.44 / 22)
        self.assertIsNone(cost_model.cost_in_r(cost, delta=0.0, stop_distance=0.4))
        # 5 bps of cost against a stop 25 bps away is 0.2R
        self.assertAlmostEqual(cost_model.net_r(1.0, cost_bps=5.0, stop_fraction=0.0025), 0.8)
        self.assertIsNone(cost_model.net_r(1.0, cost_bps=None, stop_fraction=0.0025))
        table = {"symbols": {"SPY": {"standard_7_21_dte": {"cost_bps": 2.0}}}}
        self.assertEqual(cost_model.symbol_cost_bps(table, "SPY"), 2.0)
        self.assertIsNone(cost_model.symbol_cost_bps(table, "XYZ"))


class HistoryCacheTests(unittest.TestCase):
    def test_months_and_chunked_rows(self):
        from datetime import date
        from trader.v2 import history_cache as hc
        self.assertEqual(hc.months("2024-11", "2025-02"), ["2024-11", "2024-12", "2025-01", "2025-02"])
        self.assertEqual(hc.month_bounds("2024-12")[1].month, 1)
        root = Path(tempfile.mkdtemp())
        (root / hc.CHUNK_DIR).mkdir(parents=True)
        for month, day in (("2025-01", "2025-01-31"), ("2025-02", "2025-02-03")):
            hc.chunk_path(root, "SPY", month).write_text(json.dumps([{"timestamp": day + "T15:00:00+00:00", "close": 1}]))
        rows = list(hc.iter_rows(root, "SPY", date(2025, 1, 15), date(2025, 2, 28)))
        self.assertEqual([r["timestamp"][:10] for r in rows], ["2025-01-31", "2025-02-03"])
        self.assertEqual(list(hc.iter_rows(root, "SPY", date(2025, 2, 4), date(2025, 2, 28))), [])
        self.assertIn("SPY", hc.status(root))


if __name__ == "__main__":
    unittest.main()
