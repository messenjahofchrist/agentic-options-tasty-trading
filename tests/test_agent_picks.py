"""V2 2.8.5 additions (owner, 2026-11-20): the runner trail tightens at 2R, and an advisor agent's almost_sure pick is
bought (agents need not agree). Offline; no agent CLI, broker or network."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from tests.test_v2 import ROOT, PipelineTests, position
from trader import advisor, agents
from trader.v2 import agent_picks as picks_mod
from trader.v2 import config as v2config
from trader.v2 import pipeline
from trader.v2.runtime import V2Session

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
PICKS_CFG = {"enabled": True, "valid_minutes": 50, "max_per_session": 1, "max_move_toward_target": 0.5,
             "min_expected_net_r": 0.3, "honor_vetoes": False}


def pick(**over):
    base = {"id": "20261123T150000Z-codex-1", "agent": "codex", "also_picked_by": [], "symbol": "NVDA", "side": "CALL",
            "stop_pct": 0.4, "target_pct": 0.8, "thesis": "contract win announced 10:05 ET", "sources": ["https://x"],
            "created_utc": datetime(2026, 10, 26, 14, 55, tzinfo=UTC).isoformat()}
    base.update(over)
    return base


class VersionTests(unittest.TestCase):
    def test_285_tightens_the_runner_trail_at_2r_and_buys_agent_picks(self):
        c = v2config.load_v2_config(ROOT, "strategies/v2_versions/professional_v2_2.8.5.json")
        runner = c.data["management"]["let_winners_run"]
        self.assertEqual((runner["tighten_after_r"], runner["tight_trail_r"], runner["trail_r"]), (2.0, 0.75, 1.5))
        self.assertEqual(c.data["agent_picks"], PICKS_CFG)
        self.assertEqual(c.data["management"]["profit_lock"]["activate_profit_fraction"], 0.10)   # 2.8.5 lock kept
        self.assertTrue(c.data["live_entry"]["enabled"])                                          # 2.8.3 retry kept
        from trader.v2.live_support import unsupported_for_live
        self.assertEqual(unsupported_for_live(c), [])

    def test_agents_json_runs_every_configured_agent(self):
        config = agents.load_config(ROOT)
        self.assertEqual(agents.panel_for("advisor", config), ["claude", "codex", "gemini"])


class PickFileTests(unittest.TestCase):
    def test_only_fresh_well_formed_picks_are_active(self):
        now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        document = {"picks": [pick(), pick(id="old", created_utc=(now - timedelta(minutes=51)).isoformat()),
                              pick(id="future", created_utc=(now + timedelta(minutes=10)).isoformat()),
                              pick(id="bad-side", side="SHORT"), pick(id="bad-stop", stop_pct=50)]}
        self.assertEqual([p["id"] for p in picks_mod.active_picks(document, now, 50)], [pick()["id"]])
        self.assertEqual(picks_mod.active_picks(None, now, 50), [])

    def test_levels_come_from_the_first_price_seen_and_never_chase(self):
        p = pick(side="PUT", stop_pct=1.0, target_pct=2.0)
        fresh = picks_mod.levels(p, anchor=100.0, price=100.0, max_move_toward_target=0.5)
        self.assertTrue(fresh["ok"])
        self.assertEqual((fresh["stop_underlying"], fresh["target_underlying"]), (101.0, 98.0))
        self.assertAlmostEqual(fresh["stop_distance"], 1.0)
        later = picks_mod.levels(p, anchor=100.0, price=99.5, max_move_toward_target=0.5)   # 25% of the way: still ok
        self.assertTrue(later["ok"])
        self.assertAlmostEqual(later["target_distance"], 1.5)
        chased = picks_mod.levels(p, anchor=100.0, price=99.0, max_move_toward_target=0.5)
        self.assertFalse(chased["ok"])
        self.assertFalse(chased["expired"])
        stopped = picks_mod.levels(p, anchor=100.0, price=101.0, max_move_toward_target=0.5)
        self.assertTrue(stopped["expired"])

    def test_pick_book_keeps_anchors_and_purchases_per_day(self):
        path = Path(tempfile.mkdtemp()) / "v2_agent_picks.json"
        book = picks_mod.PickBook(path)
        now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        self.assertEqual(book.anchor("2026-10-26", "a", 100.0, now), 100.0)
        self.assertEqual(book.anchor("2026-10-26", "a", 105.0, now), 100.0)      # the first price stays the anchor
        book.mark_taken("2026-10-26", "a", "t1", now.isoformat())
        again = picks_mod.PickBook(path)                                          # survives an engine restart
        self.assertTrue(again.is_done("2026-10-26", "a"))
        self.assertEqual(again.taken_count("2026-10-26"), 1)
        self.assertEqual(again.taken_count("2026-10-27"), 0)                     # a new day starts empty


class AgentPickPipelineTests(unittest.TestCase):
    setUp = PipelineTests.setUp
    chain = PipelineTests.chain
    regime = PipelineTests.regime
    inputs = PipelineTests.inputs

    def pick_inputs(self, **over):
        data = json.loads(json.dumps(self.c.data))
        data["agent_picks"] = dict(PICKS_CFG)
        tracker = {"state": "AGENT_PICK", "actionable": True, "failures": [],
                   "setup": {"side": "CALL", "stop_distance": 0.4, "target_distance": 0.8, "trigger_time": None}}
        base = dict(v2=data, tracker=tracker, agent_pick={"id": "p1", "agent": "codex"})
        base.update(over)
        return self.inputs(**base)

    def test_a_pick_needs_no_breakout_regime_sector_or_volume(self):
        bad_sector = {"sectors": {"XLK": {"bias": -2, "fresh": True, "missing": []}}}
        report = pipeline.evaluate_candidate(self.pick_inputs(regime=self.regime("HIGH_VOLATILITY"), snapshot=bad_sector,
                                                              core_metrics={"atr": 0.1}))
        self.assertEqual(report.outcome, "APPROVED", report.reasons)
        stages = pipeline.report_record(report)["stages"]
        for name in ("breakout_confirmation", "market_regime", "sector_confirmation", "volume_confirmation"):
            self.assertEqual(stages[name]["status"], "NOT_APPLICABLE", name)
        self.assertEqual(report.entry["setup_type"], "AGENT_PICK")
        self.assertEqual(report.entry["agent_pick"]["agent"], "codex")
        # the same candidate without the pick is a normal breakout candidate and is rejected
        plain = pipeline.evaluate_candidate(self.inputs(regime=self.regime("HIGH_VOLATILITY"), snapshot=bad_sector))
        self.assertEqual(plain.outcome, "REJECTED")

    def test_a_pick_keeps_the_safety_limits(self):
        self.assertEqual(pipeline.evaluate_candidate(self.pick_inputs(entries_blocked="kill_switch_active")).outcome,
                         "REJECTED")
        self.assertEqual(pipeline.evaluate_candidate(self.pick_inputs(data_fresh=False)).outcome, "INSUFFICIENT_DATA")
        earnings_today = dict(self.metrics, earnings={"expected-report-date": "2026-10-26", "time-of-day": "AMC"})
        self.assertEqual(pipeline.evaluate_candidate(self.pick_inputs(metrics=earnings_today)).outcome, "REJECTED")
        held = [position("AAPL", loss=1000)]
        self.assertEqual(pipeline.evaluate_candidate(self.pick_inputs(positions=held)).outcome, "REJECTED")
        late = self.pick_inputs()
        late.v2["late_entry_cutoff"].update(enabled=True, no_entries_final_minutes=330)   # cutoff 10:30 ET
        self.assertEqual(pipeline.evaluate_candidate(late).outcome, "REJECTED")

    def test_a_pick_uses_its_own_expected_net_r_minimum_and_ignores_favorites(self):
        strict = self.pick_inputs()
        strict.v2["agent_picks"]["min_expected_net_r"] = 9.0
        report = pipeline.evaluate_candidate(strict)
        self.assertEqual(report.outcome, "REJECTED")
        self.assertIn("agent-pick minimum", report.reasons[0])
        favorites = self.pick_inputs()
        favorites.v2["favorites"] = {"enabled": True, "symbols": ["SPY"], "others_min_expected_net_r": 9.0}
        self.assertEqual(pipeline.evaluate_candidate(favorites).outcome, "APPROVED")


class SessionPickTests(unittest.TestCase):
    """V2Session.evaluate_agent_pick with a stub session (no broker)."""

    def setUp(self):
        self.p = PipelineTests()
        PipelineTests.setUp(self.p)
        self.root = Path(tempfile.mkdtemp())
        (self.root / "data" / "advisor").mkdir(parents=True)
        data = json.loads(json.dumps(self.p.c.data))
        data["agent_picks"] = dict(PICKS_CFG)
        s = V2Session.__new__(V2Session)
        s.root, s.data_dir, s.cfg, s.config = self.root, self.root, data, SimpleNamespace(
            strategy_id="V2", version="test", sha256="x")
        s.profile_name, s.logger = "ORIGINAL_ACCOUNT_PAPER", logging.getLogger("test")
        s.decisions_log = self.root / "v2_decisions.jsonl"
        s._picks_cache, s._veto_cache, s._pick_notes = (None, None), (None, None), {}
        s.pick_book = picks_mod.PickBook(self.root / "v2_agent_picks.json")
        s.snapshot = lambda now: self.p.snap
        s.last_core, s.last_tracker, s.latest_reports = {"NVDA": (None, self.p.core)}, {}, {}
        s.sector_map, s.events, s.metrics, s.metrics_error = self.p.sm, self.p.inputs().events, {"NVDA": self.p.metrics}, None
        s.correlation, s.daily_closes, s.session_close, s.average_daily_range = self.p.inputs().correlation, {}, None, {}
        s.chain_sync = lambda *a, **k: self.p.chain()
        self.s = s
        self.account = SimpleNamespace(positions=[], cash=10_000)

    def write(self, *picks):
        (self.root / picks_mod.PICKS_FILE).write_text(json.dumps({"picks": list(picks)}), encoding="utf-8")

    def ask(self, now, blocked=None, bars=None):
        import dataclasses
        real = pipeline.evaluate_candidate
        fixed_clock = mock.patch.object(pipeline, "evaluate_candidate",   # fixture quotes are fresh at the fixture's time
                                        side_effect=lambda inp: real(dataclasses.replace(inp, clock=lambda: now)))
        with fixed_clock:
            return self._ask(now, blocked, bars)

    def _ask(self, now, blocked=None, bars=None):
        return asyncio.run(self.s.evaluate_agent_pick(
            symbol="NVDA", bars=bars or self.p.bars, now=now, account=self.account, equity=10_000,
            strategy=self.p.strategy, risk_state=self.p.inputs().risk_state, data_fresh=True, entries_blocked=blocked,
            pending=[]))

    def test_a_pick_is_bought_once_and_counts_against_the_daily_limit(self):
        now = self.p.now
        self.write(pick(), pick(id="second", side="CALL"))
        with mock.patch("trader.v2.runtime.classify", return_value=self.p.regime("HIGH_VOLATILITY")):
            self.assertIsNone(self.ask(now, blocked="market_closed_or_outside_entry_window"))   # waits, no anchor
            self.assertEqual(self.s.pick_book.state["anchors"], {})
            report = self.ask(now)
            self.assertEqual(report.outcome, "APPROVED", report.reasons)
            anchor = self.p.bars[-1].close
            self.assertAlmostEqual(report.entry["stop_underlying"], anchor * (1 - 0.004))
            self.assertAlmostEqual(report.entry["target_underlying"], anchor * (1 + 0.008))
            self.s.record_entry(report, {"entry_time": now.isoformat(), "trade_id": "t1", "underlying": "NVDA",
                                         "symbol": "NVDA C", "quantity": 1, "entry_price": 4.05,
                                         "v2_entry": {"regime": "HIGH_VOLATILITY", "setup_type": "AGENT_PICK"}})
            self.assertIsNone(self.ask(now + timedelta(minutes=1)))   # first bought; the second is over max_per_session
        log = [json.loads(line) for line in self.s.decisions_log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("BOUGHT", [r.get("status") for r in log if r["type"] == "agent_pick"])
        self.assertIn("SKIPPED", [r.get("status") for r in log if r["type"] == "agent_pick"])

    def test_a_pick_past_its_stop_is_dropped(self):
        from tests.test_v2 import bar
        now = self.p.now
        self.write(pick())
        with mock.patch("trader.v2.runtime.classify", return_value=self.p.regime("STRONG_BULLISH")):
            first = self.ask(now)
            self.assertEqual(first.outcome, "APPROVED")
            fallen = self.p.bars + [bar(self.p.bars[-1].timestamp + timedelta(minutes=1), self.p.bars[-1].close * 0.99)]
            self.assertIsNone(self.ask(now + timedelta(minutes=1), bars=fallen))
            self.assertIn(pick()["id"], self.s.pick_book.state["expired"])


GOOD = {"as_of_et": "x", "market_sentiment": {"lean": "bearish", "confidence": "high", "summary": "s", "drivers": []},
        "symbols": [], "positions": [], "suggested_instructions": [], "upcoming_risks": []}


class AdvisorPanelTests(unittest.TestCase):
    def test_every_panel_agent_answers_and_one_almost_sure_idea_is_enough(self):
        out = Path(tempfile.mkdtemp())
        claude = dict(GOOD, vetoes=[{"symbol": "NVDA", "side": None, "until_et": "23:59", "reason": "earnings tonight"}],
                      trade_ideas=[{"symbol": "NVDA", "side": "PUT", "thesis": "t", "stop_pct": 0.6, "target_pct": 1.2,
                                    "confidence": "high"}])
        codex = dict(GOOD, trade_ideas=[{"symbol": "NFLX", "side": "CALL", "thesis": "deal", "stop_pct": 0.5,
                                         "target_pct": 1.0, "confidence": "almost_sure"}])
        replies = {"claude": claude, "codex": codex}

        def answer(name, text, **kwargs):
            if name not in replies:
                raise agents.AgentError(f"{name}: 402 quota exhausted")
            return agents.AgentReply(name, advisor.check(json.loads(json.dumps(replies[name]))))
        config = {"roles": {"advisor": ["claude"], "advisor_panel": ["claude", "codex", "gemini"]},
                  "agents": {"claude": {}, "codex": {}, "gemini": {}}}
        with mock.patch.object(advisor, "OUT", out), mock.patch.object(advisor, "email_configured", return_value=False), \
                mock.patch.object(advisor, "gather", return_value={"trade_symbols": ["NVDA", "NFLX"]}), \
                mock.patch.object(agents, "load_config", return_value=config), \
                mock.patch.object(agents, "ask_agent_json", side_effect=answer):
            record = advisor.run()
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["agents"], {"claude": "complete", "codex": "complete", "gemini": "failed"})
        picks = json.loads((out / "agent_picks.json").read_text(encoding="utf-8"))["picks"]
        self.assertEqual([(p["symbol"], p["side"], p["agent"]) for p in picks], [("NFLX", "CALL", "codex")])
        self.assertEqual(picks[0]["id"], f"{record['id']}-codex-1")
        vetoes = json.loads((out / "vetoes.json").read_text(encoding="utf-8"))
        self.assertEqual([v["agent"] for v in vetoes["vetoes"]], ["claude"])     # vetoes combined, each with its agent
        ideas = [json.loads(line) for line in (out / "ideas.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(i["agent"] for i in ideas), ["claude", "codex"])
        self.assertEqual(len({i["id"] for i in ideas}), 2)
        self.assertEqual(len(list((out / "notes").glob("*.json"))), 3)
        self.assertIn("WILL BUY", advisor.panel_summary(record))

    def test_the_prompt_says_almost_sure_is_bought_with_real_money(self):
        text = advisor.prompt({})
        self.assertIn("almost_sure", text)
        self.assertIn("REAL MONEY", text)


if __name__ == "__main__":
    unittest.main()
