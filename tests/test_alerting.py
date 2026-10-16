"""Phone push and dead-man's switch: the watchdog covers account-profile engines, pushes urgently when an engine
is down with open positions, and pings an external check so a dead PC is noticed too. Nothing leaves the process."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trader import notifications
from trader.supervisor import watchdog_all, watchdog_check

DURING_SESSION = datetime(2026, 10, 9, 19, 0, tzinfo=timezone.utc)


def write_engine(data: Path, *, status: str, updated: str, positions: int) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "paper_account.json").write_text(json.dumps({"positions": [{"symbol": f"o{i}"} for i in range(positions)]}))
    (data / "engine_heartbeat.json").write_text(json.dumps({"status": status, "lifecycle_state": status.upper(),
                                                            "updated_at_utc": updated, "open_positions": positions}))
    (data / "engine_expectation.json").write_text(json.dumps({"expected_running": True}))


def make_root(tmp: Path) -> Path:
    (tmp / "profiles").mkdir()
    (tmp / "profiles" / "small_account.json").write_text(json.dumps(
        {"ledger_id": "small_account_paper", "data_dir": "data/profiles/small_account_paper"}))
    (tmp / "profiles" / "small_account_universe.json").write_text(json.dumps({"symbols": ["SPY"]}))
    return tmp


class WatchdogProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        self.data = self.root / "data"
        self.profile = self.data / "profiles" / "small_account_paper"
        self.pushes, self.pings = [], []
        patches = [
            patch("trader.supervisor._email_configured", return_value=False),
            patch("trader.supervisor._save_status"),
            patch("trader.supervisor.push_configured", return_value=True),
            patch("trader.supervisor.send_push", side_effect=lambda t, m, **k: self.pushes.append((t, m, k))),
            patch("trader.supervisor.ping_dead_man",
                  side_effect=lambda **k: self.pings.append(k) or "sent"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crashed_profile_engine_with_open_positions_pushes_urgent_and_leaves_the_kill_switch_alone(self):
        (self.data).mkdir()
        (self.data / "engine_expectation.json").write_text(json.dumps({"expected_running": False}))
        write_engine(self.profile, status="failed", updated="2026-10-09T18:45:00+00:00", positions=3)
        result = watchdog_all(root=self.root, data_dir=self.data, now=DURING_SESSION)
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["critical"], ["small_account_paper"])
        self.assertEqual(result["engines"]["original"]["status"], "not_expected_to_run")
        [(title, body, options)] = self.pushes
        self.assertIn("small_account_paper engine DOWN - 3 OPEN", title)
        self.assertEqual(options["priority"], "urgent")
        self.assertFalse((self.root / "KILL_SWITCH").exists())
        self.assertEqual(self.pings, [{"failed": True, "detail": "original: not_expected_to_run; small_account_paper: critical"}])

    def test_the_same_failure_is_pushed_once_not_every_minute(self):
        write_engine(self.profile, status="failed", updated="2026-10-09T18:45:00+00:00", positions=1)
        for _ in range(3):
            watchdog_check(root=self.root, data_dir=self.profile, now=DURING_SESSION, manage_kill_switch=False)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0][2]["priority"], "urgent")

    def test_down_without_positions_is_high_not_urgent(self):
        write_engine(self.profile, status="running", updated="2026-10-09T18:00:00+00:00", positions=0)
        watchdog_check(root=self.root, data_dir=self.profile, now=DURING_SESSION, manage_kill_switch=False)
        self.assertEqual(self.pushes[0][2]["priority"], "high")

    def test_healthy_engines_send_a_plain_dead_man_ping_and_no_push(self):
        write_engine(self.data, status="running", updated="2026-10-09T18:59:30+00:00", positions=0)
        write_engine(self.profile, status="running", updated="2026-10-09T18:59:30+00:00", positions=2)
        result = watchdog_all(root=self.root, data_dir=self.data, now=DURING_SESSION)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.pushes, [])
        self.assertFalse(self.pings[0]["failed"])


class NotificationChannelTests(unittest.TestCase):
    def test_only_position_critical_events_are_pushed(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(notifications, "email_configured", return_value=False), \
                patch.object(notifications, "push_configured", return_value=True), \
                patch.object(notifications, "send_push", side_effect=lambda t, m, **k: sent.append((t, m, k))):
            notifications.record_critical_alert("paper_position_management_failed", mode="PAPER",
                                                details={"underlying": "MARA"}, data_dir=Path(tmp))
            record = notifications.record_critical_alert("paper_position_protective_exit", mode="PAPER",
                                                         details={"underlying": "PDD", "realized_pnl": 2.56,
                                                                  "trade_id": "secret-ish-id"}, data_dir=Path(tmp))
        self.assertEqual(len(sent), 1)
        title, body, options = sent[0]
        self.assertEqual(options["priority"], "urgent")
        self.assertIn("underlying=PDD", body)
        self.assertNotIn("secret-ish-id", body)
        self.assertEqual(record["push_status"], "sent")

    def test_unconfigured_channels_do_nothing(self):
        self.assertFalse(notifications.push_configured())
        self.assertEqual(notifications.ping_dead_man(), "not_configured")
        with self.assertRaises(RuntimeError):
            notifications.send_push("t", "m")

    def test_send_push_posts_to_the_configured_ntfy_url(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_urlopen(request, timeout):
            captured.update(url=request.full_url, data=request.data, headers=dict(request.header_items()))
            return Response()

        with patch.dict("os.environ", {"NTFY_URL": "https://ntfy.example/private-topic"}), \
                patch("urllib.request.urlopen", fake_urlopen):
            notifications.send_push("OptioBot DOWN", "3 open", priority="urgent")
        self.assertEqual(captured["url"], "https://ntfy.example/private-topic")
        self.assertEqual(captured["data"], b"3 open")
        self.assertEqual(captured["headers"]["Priority"], "urgent")


if __name__ == "__main__":
    unittest.main()
