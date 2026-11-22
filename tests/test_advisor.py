import json
import unittest
from unittest import mock

from trader import advisor

GOOD = {"as_of_et": "2026-11-11T09:00-05:00",
        "market_sentiment": {"lean": "bearish", "confidence": "medium", "summary": "Hot CPI", "drivers": ["yields"]},
        "symbols": [{"symbol": "NVDA", "lean": "caution", "headline": "Board probe", "why_it_matters": "x", "sources": []}],
        "positions": [{"ledger": "x", "trade_id": "t1", "underlying": "NFLX", "advice": "hold", "reason": "r", "sources": []}],
        "suggested_instructions": [{"type": "EXIT", "symbol": "NFLX", "side": "PUT", "until_et": "15:00", "reason": "r"}],
        "upcoming_risks": ["PPI Friday"]}


class AdvisorTests(unittest.TestCase):
    def test_parses_json_even_with_text_around_it(self):
        data = advisor.parse("Here you go:\n```json\n" + json.dumps(GOOD) + "\n```")
        self.assertEqual(data["market_sentiment"]["lean"], "bearish")

    def test_rejects_missing_keys_and_unknown_values(self):
        with self.assertRaises(ValueError):
            advisor.parse(json.dumps({k: v for k, v in GOOD.items() if k != "positions"}))
        bad = json.loads(json.dumps(GOOD)); bad["positions"][0]["advice"] = "double_down"
        with self.assertRaises(ValueError):
            advisor.parse(json.dumps(bad))
        bad = json.loads(json.dumps(GOOD)); bad["suggested_instructions"][0]["type"] = "BUY_EVERYTHING"
        with self.assertRaises(ValueError):
            advisor.parse(json.dumps(bad))
        with self.assertRaises(ValueError):
            advisor.parse("no json here")

    def test_prompt_carries_state_and_names_no_particular_agent(self):
        text = advisor.prompt({"ledgers": {"a": {"open_positions": [{"underlying": "NFLX"}]}}})
        self.assertIn("NFLX", text)
        self.assertIn("almost_sure idea IS BOUGHT", text)   # 2.8.5: only almost_sure ideas are traded
        self.assertIn("vetoes", text)
        self.assertNotIn("claude", advisor.prompt({}).lower())

    def test_scored_lists_keep_only_well_formed_items(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)   # 10:00 ET
        advice = dict(GOOD, watchlist=[{"symbol": "crwd", "direction": "up", "catalyst": "earnings beat"}, {"symbol": "??"}],
                      vetoes=[{"symbol": "NFLX", "side": "null", "until_et": "23:00", "reason": "ruling at 2pm"},
                              {"symbol": "NFLX", "side": "CALL", "until_et": "09:00", "reason": "past"},
                              {"symbol": "TSLA", "side": None, "until_et": "12:00", "reason": "not a bot symbol"},
                              {"symbol": "NVDA", "side": None, "until_et": "12:00", "reason": ""}],
                      trade_ideas=[{"symbol": "NVDA", "side": "PUT", "thesis": "probe", "stop_pct": 0.6, "target_pct": 1.2},
                                   {"symbol": "NVDA", "side": "PUT", "thesis": "x", "stop_pct": 60, "target_pct": 1.2},
                                   {"symbol": "NVDA", "side": "SHORT", "thesis": "x", "stop_pct": 1, "target_pct": 1}])
        lists = advisor.clean_lists(advisor.check(advice), ["NFLX", "NVDA"], now)
        self.assertEqual([w["symbol"] for w in lists["watchlist"]], ["CRWD"])
        self.assertEqual(len(lists["vetoes"]), 1)
        self.assertIsNone(lists["vetoes"][0]["side"])
        self.assertEqual(lists["vetoes"][0]["until_utc"], "2026-11-11T21:00:00+00:00")   # capped at the 16:00 ET close
        self.assertEqual(len(lists["trade_ideas"]), 1)
        self.assertEqual(len(lists["dropped"]), 6)

    def test_run_uses_the_configured_agent_and_writes_the_veto_file(self):
        import tempfile
        from pathlib import Path
        from trader import agents
        out = Path(tempfile.mkdtemp())
        # no advisor_panel: the role's first answering agent is used (the 2.8.0 behaviour)
        single = {"roles": {"advisor": ["some-other-bot"]}, "agents": {"some-other-bot": {}}}
        no_panel = mock.patch.object(agents, "load_config", return_value=single)
        no_panel.start()
        self.addCleanup(no_panel.stop)
        advice = dict(GOOD, vetoes=[{"symbol": "NVDA", "side": None, "until_et": "23:59", "reason": "earnings tonight"}],
                      trade_ideas=[{"symbol": "NVDA", "side": "PUT", "thesis": "probe", "stop_pct": 0.6, "target_pct": 1.2}])
        reply = agents.AgentReply("some-other-bot", advisor.check(advice))
        with mock.patch.object(advisor, "OUT", out), mock.patch.object(advisor, "email_configured", return_value=False), \
                mock.patch.object(advisor, "gather", return_value={"trade_symbols": ["NVDA"]}), \
                mock.patch.object(agents, "ask_json", return_value=reply):
            record = advisor.run()
        self.assertEqual((record["status"], record["agent"]), ("complete", "some-other-bot"))
        document = json.loads((out / "vetoes.json").read_text(encoding="utf-8"))
        self.assertEqual((document["agent"], document["note_id"]), ("some-other-bot", record["id"]))
        self.assertEqual(len((out / "ideas.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        with mock.patch.object(advisor, "OUT", out), mock.patch.object(advisor, "email_configured", return_value=False), \
                mock.patch.object(advisor, "gather", return_value={"trade_symbols": ["NVDA"]}), \
                mock.patch.object(agents, "ask_json", side_effect=agents.AgentError("all agents down")):
            failed = advisor.run()
        self.assertEqual(failed["status"], "failed")
        # a failed run leaves the last veto file alone; it ages out on its own
        self.assertEqual(json.loads((out / "vetoes.json").read_text(encoding="utf-8"))["note_id"], record["id"])

    def test_summary_says_suggestions_are_not_applied(self):
        text = advisor.summary_text({"advice": GOOD})
        self.assertIn("NOT applied", text)
        self.assertIn("NFLX", text)


if __name__ == "__main__":
    unittest.main()
