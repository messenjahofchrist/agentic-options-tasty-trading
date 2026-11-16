"""Nightly scoreboard (2026-11-11): scoring on a small synthetic recorded session."""
import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trader import scoreboard as sb
from trader.quote_history import ET, QuoteHistory, black_scholes, select_contract

DAY = date(2026, 11, 11)
OPEN = datetime(2026, 11, 11, 9, 30, tzinfo=ET)


def record_session(root: Path, symbol: str, closes: list[float]) -> None:
    """Flat-volatility option quotes around a given minute-by-minute stock path; 5-day and 9-day expirations."""
    contracts = {}
    for expiration, label in ((DAY + timedelta(days=5), "s"), (DAY + timedelta(days=9), "l")):
        for strike in (98.0, 99.0, 100.0, 101.0, 102.0):
            for right in ("C", "P"):
                bid, ask = [], []
                for minute, spot in enumerate(closes):
                    t = ((expiration - DAY).days * 390 - minute) / (252 * 390)
                    mid = black_scholes(spot, strike, t, 0.25, right)[0]
                    half = 0.02 if label == "s" else 0.04
                    bid.append([minute, mid - half, mid - half, mid - half - 0.01, mid - half])
                    ask.append([minute, mid + half, mid + half + 0.01, mid + half, mid + half])
                contracts[f"{symbol}{label}{right}{strike:.0f}"] = {"expiration": expiration.isoformat(), "strike": strike,
                                                                      "right": right, "bid": bid, "ask": ask}
    data = {"date": DAY.isoformat(), "symbol": symbol, "session_open_et": OPEN.isoformat(),
            "underlying": [[m, c, c + 0.02, c - 0.02, c] for m, c in enumerate(closes)], "contracts": contracts}
    folder = root / "data" / "option_history" / DAY.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{symbol}.json.gz").write_bytes(gzip.compress(json.dumps(data).encode()))


class ScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        record_session(self.root, "UP", [100 + 0.01 * m for m in range(390)])       # rises 3.9% over the day
        record_session(self.root, "FLAT", [100.0] * 390)
        self.history = QuoteHistory(self.root)

    def idea(self, **over):
        base = {"id": "n-1", "note_id": "n", "agent": "some-bot", "symbol": "UP", "side": "CALL", "stop_pct": 0.5,
                "target_pct": 1.0, "logged_at_utc": (OPEN + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(),
                "timestamp_utc": OPEN.astimezone(timezone.utc).isoformat(), "thesis": "t"}
        base.update(over)
        return base

    def test_idea_is_scored_from_the_minute_it_was_logged(self):
        won = sb.score_idea(self.idea(), self.history, {"symbols": {"UP": {"standard_7_21_dte": {"cost_bps": 5.0}}}})
        self.assertEqual((won["status"], won["result"]), ("scored", "target"))
        self.assertAlmostEqual(won["entry"], 100.30, places=2)
        self.assertGreater(won["gross_r"], 1.9)
        self.assertAlmostEqual(won["gross_r"] - won["net_r"], 0.1, places=2)        # 5 bps against a 50 bps stop
        self.assertGreater(won["option_pnl"], 0)
        lost = sb.score_idea(self.idea(side="PUT"), self.history, None)
        self.assertEqual(lost["result"], "stop")
        self.assertLess(lost["gross_r"], -0.99)
        self.assertIsNone(lost["net_r"])
        self.assertIsNone(sb.score_idea(self.idea(symbol="NOPE"), self.history, None))
        board = sb.agent_ideas([self.idea(), self.idea(id="n-2", side="PUT"), self.idea(id="n-3", symbol="NOPE")], self.history, None)
        self.assertEqual((board["logged"], board["scored"], board["awaiting_recorded_prices"]), (3, 2, 1))
        self.assertEqual(board["by_agent"]["some-bot"]["gross_r"]["n"], 2)

    def test_real_trades_are_repriced_with_both_contracts_and_cost_gate_split(self):
        ledger = {"ledger": [
            {"type": "fill", "side": "BUY", "trade_id": "a", "symbol": "UP    261120C00100000", "underlying": "UP", "fill_price": 2.0,
             "quote": {"bid": 1.95, "ask": 2.0}, "timestamp": (OPEN + timedelta(minutes=10)).astimezone(timezone.utc).isoformat()},
            {"type": "paper_close", "trade_id": "a", "underlying": "UP", "realized_pnl": 50.0,
             "timestamp": (OPEN + timedelta(minutes=200)).astimezone(timezone.utc).isoformat(),
             "forecast": {"entry_delta": 0.5, "entry_underlying": 100.1, "stop_underlying": 99.9}},
            {"type": "paper_close", "trade_id": "orphan", "underlying": "UP", "realized_pnl": 1.0, "timestamp": "2026-11-11T16:00:00+00:00"}]}
        trades = sb.closed_trades(ledger)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "CALL")
        self.assertAlmostEqual(sb.trade_cost_r(trades[0]), (5 + 1.40 + 0.04) / (0.5 * 0.2 * 100), places=4)   # 0.644R
        histories = {"open": self.history, "adverse": QuoteHistory(self.root, "adverse")}
        result = sb.shorter_dated(trades, histories, 350.0)
        row = result["rows"][0]
        self.assertEqual((row["standard"]["dte"], row["short"]["dte"]), (9, 5))
        self.assertLess(row["short"]["premium"], row["standard"]["premium"])
        self.assertLess(row["short"]["cost"], row["standard"]["cost"])
        self.assertLessEqual(row["short"]["pnl_adverse"], row["short"]["pnl_open"])
        self.assertEqual(result["trial"]["n"], 1)
        self.assertIn("trial_pass_mark", result)
        gate = sb.cost_gate(trades)
        self.assertEqual((gate["limit_0.20R"]["kept"]["n"], gate["limit_0.20R"]["dropped"]["n"]), (0, 1))

    def test_watchlist_compares_listed_names_with_the_rest(self):
        item = {"note_id": "n", "symbol": "UP", "direction": "up", "agent": "some-bot",
                "logged_at_utc": (OPEN + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(),
                "timestamp_utc": OPEN.astimezone(timezone.utc).isoformat()}
        board = sb.agent_watchlist([item, dict(item, symbol="ZZZZ")], self.history)
        self.assertEqual((board["scored"], board["not_recorded_symbols"]), (1, 1))
        self.assertGreater(board["rows"][0]["move_in_stated_direction_pct"], 3)
        self.assertEqual(board["moved_more_than_other_symbols_share"], 1.0)

    def test_full_board_renders_with_no_data_at_all(self):
        empty = Path(tempfile.mkdtemp())
        board = sb.build(empty, rebuild_cost_table=False)
        text = sb.render(board)
        self.assertIn("Agent trade ideas", text)
        self.assertEqual(board["accounts"]["$10K"]["closed_trades"], 0)

    def test_a_context_file_without_session_times_does_not_break_the_board(self):
        folder = self.root / "data" / "option_history" / DAY.isoformat()
        (folder / "VIX.json.gz").write_bytes(gzip.compress(json.dumps(
            {"date": DAY.isoformat(), "symbol": "VIX", "underlying": [[0, 18, 18, 18, 18], [1, 18, 18, 18, 18]]}).encode()))
        item = {"note_id": "n", "symbol": "UP", "direction": "up", "agent": "a",
                "logged_at_utc": (OPEN + timedelta(minutes=30)).astimezone(timezone.utc).isoformat(),
                "timestamp_utc": OPEN.astimezone(timezone.utc).isoformat()}
        self.assertEqual(sb.agent_watchlist([item], QuoteHistory(self.root))["scored"], 1)
        self.assertEqual(QuoteHistory(self.root).chain(DAY, "VIX", OPEN + timedelta(minutes=1)), [])
        board = sb.build(self.root, rebuild_cost_table=False)
        self.assertEqual(board["errors"], {})

    def test_contract_rule_prefers_the_nearest_expiry_when_asked(self):
        chain = self.history.chain(DAY, "FLAT", OPEN + timedelta(minutes=5))
        short = select_contract(chain, "CALL", DAY, min_dte=1, max_dte=6, nearest_expiry_first=True)
        self.assertEqual((short.expiration - DAY).days, 5)
        self.assertIsNone(select_contract(chain, "CALL", DAY, min_dte=12, max_dte=21))


if __name__ == "__main__":
    unittest.main()
