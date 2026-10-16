"""Account-profile tests: isolated $1,000 PAPER ledger, 40% aggregate cap, expanded universe, one-way $10,000
restore of the original settings. Everything is local; no test reaches tastytrade."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader.account_profiles import (ORIGINAL, SMALL, AccountProfile, ProfileConfig, ProfileError, ProfileLedger,
                                     run_profile_paper_session, verify_original_baseline)
from trader.config import AppConfig, Mode
from trader.market_data import CachedQuote
from trader.models import Bar, OptionQuote
from trader.portfolio import PaperAccount, load_or_initialize, save_account
from trader.strategy_loader import parse_strategy, write_manifest

ROOT = Path(__file__).resolve().parents[1]
EXPANDED = ["SPY", "IWM", "F", "SOFI"]


def make_root(tmp: Path, *, universe=EXPANDED) -> Path:
    """A project copy: the real strategy files, a recorded baseline and a profile config."""
    (tmp / "strategy.txt").write_bytes((ROOT / "strategy.txt").read_bytes())
    strategy = parse_strategy(tmp / "strategy.txt")
    write_manifest(strategy, tmp / "active_strategy.json", {"status": "research_test", "mode": "PAPER", "live_enabled": False})
    (tmp / "profiles").mkdir()
    (tmp / "baseline").mkdir()
    (tmp / "baseline" / "original.json").write_text(json.dumps(
        {"configuration_hash": strategy.sha256, "universe": list(strategy.universe), "risk": dict(strategy.risk)}))
    (tmp / "profiles" / "universe.json").write_text(json.dumps({"symbols": universe}))
    (tmp / "profiles" / "small_account.json").write_text(json.dumps({
        "ledger_id": "small_account_paper", "mode": "PAPER", "data_dir": "data/profiles/small_account_paper",
        "starting_balance": 1000, "original_account_baseline": "baseline/original.json",
        "transition": {"equity_threshold": 10000, "to": ORIGINAL, "one_way": True},
        "profiles": {SMALL: {"mode": "PAPER", "max_aggregate_allocation_fraction": 0.40, "universe_file": "profiles/universe.json",
                             "risk_overrides": {"max_daily_loss_fraction": 0.20}},
                     ORIGINAL: {"mode": "PAPER", "restore": "original_account_baseline"}}}))
    return tmp


def position(symbol="SPY   261030C00100000", underlying="SPY", price=2.0, modeled=None):
    record = {"trade_id": underlying, "symbol": symbol, "underlying": underlying, "quantity": 1, "entry_price": price,
              "entry_fees": 0.70, "last_bid": price}
    if modeled is not None:
        record["modeled_risk_dollars"] = modeled
    return record


class ProfileUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        self.strategy = parse_strategy(self.root / "strategy.txt")
        self.config = ProfileConfig.load(self.root, "profiles/small_account.json")
        self.ledger = ProfileLedger(self.config, original_data_dir=self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_ledger_initializes_at_exactly_1000_in_its_own_directory(self):
        state = self.ledger.initialize()
        account = self.ledger.account()
        self.assertEqual((account.starting_capital, account.cash, account.positions, account.ledger), (1000.0, 1000.0, [], []))
        self.assertEqual((state["active_profile"], state["starting_balance"]), (SMALL, 1000.0))
        self.assertEqual(self.ledger.account_path, self.root / "data/profiles/small_account_paper/paper_account.json")
        self.assertFalse((self.root / "data" / "paper_account.json").exists())

    def test_ledger_cannot_share_the_original_directory_or_adopt_an_unknown_ledger(self):
        with self.assertRaises(ProfileError):
            ProfileLedger(replace(self.config, data_dir=self.root / "data"), original_data_dir=self.root / "data")
        save_account(self.ledger.account_path, PaperAccount(starting_capital=10_000, cash=10_000,
                                                            positions=[position()], ledger=[{"type": "fill"}]))
        with self.assertRaises(ProfileError):
            self.ledger.initialize()  # never inherits an existing ledger's balance or positions

    def test_initial_allocation_is_400_and_cap_counts_the_contract_multiplier(self):
        policy = AccountProfile(SMALL, 0.40)
        account = PaperAccount(starting_capital=1000, cash=1000)
        self.assertEqual(policy.allocation_cap(1000), 400.0)
        sized = policy.cycle_strategy(self.strategy, 1000, account)
        self.assertAlmostEqual(1000 * sized.risk["max_risk_per_trade_fraction"], 400.0)
        # $2.00 option without a modeled figure = $200 premium (x100) + $0.70 fees, not $2.
        account.positions.append(position(price=2.0))
        self.assertAlmostEqual(policy.open_exposure(account), 200.70)
        self.assertAlmostEqual(1000 * policy.cycle_strategy(self.strategy, 1000, account).risk["max_risk_per_trade_fraction"], 199.30)
        self.assertEqual(policy.entry_allowed(account, 1000, 199.30)[0], True)
        self.assertEqual(policy.entry_allowed(account, 1000, 199.31)[0], False)

    def test_only_sizing_keys_differ_from_the_strategy(self):
        sized = AccountProfile(SMALL, 0.40).cycle_strategy(self.strategy, 1000, PaperAccount(1000, 1000))
        for field in ("strategy_id", "version", "sha256", "universe", "rules", "execution", "entry_window_et",
                      "bar_interval_minutes", "opening_range_minutes", "configuration"):
            self.assertEqual(getattr(sized, field), getattr(self.strategy, field), field)
        changed = {k for k in self.strategy.risk if sized.risk[k] != self.strategy.risk[k]}
        self.assertEqual(changed, {"max_risk_per_trade_fraction", "hard_max_risk_per_trade_fraction"})
        self.assertIs(AccountProfile(ORIGINAL, None).cycle_strategy(self.strategy, 1000, PaperAccount(1000, 1000)), self.strategy)

    def test_expanded_universe_loads_from_its_own_file_and_strategy_is_untouched(self):
        self.ledger.initialize()
        run, policy = self.ledger.runtime(self.strategy)
        self.assertEqual((list(run.universe), policy.name, policy.allocation_fraction), (EXPANDED, SMALL, 0.40))
        self.assertEqual((run.rules, run.sha256, run.execution), (self.strategy.rules, self.strategy.sha256, self.strategy.execution))
        self.assertEqual({k for k in self.strategy.risk if run.risk[k] != self.strategy.risk[k]}, {"max_daily_loss_fraction"})
        self.assertEqual((run.risk["max_daily_loss_fraction"], self.strategy.risk["max_daily_loss_fraction"]), (0.20, 0.02))
        self.assertEqual(parse_strategy(self.root / "strategy.txt").universe, self.strategy.universe)

    def test_transition_at_10000_restores_original_exactly_and_never_reverts(self):
        self.ledger.initialize()
        account = self.ledger.account()
        account.cash, account.realized_pnl = 9_999.99, 8_999.99
        save_account(self.ledger.account_path, account)
        self.assertIsNone(self.ledger.check_transition(boundary="session_end"))
        account.cash, account.realized_pnl = 10_000.00, 9_000.00
        account.positions.append(position(underlying="IWM"))
        account.ledger.append({"type": "paper_close", "realized_pnl": 9000.0})
        save_account(self.ledger.account_path, account)
        record = self.ledger.check_transition(boundary="session_end")
        self.assertEqual((record["from"], record["to"], record["verified_equity"]), (SMALL, ORIGINAL, 10_000.0))
        run, policy = self.ledger.runtime(self.strategy)
        self.assertIs(run, self.strategy)  # exact original universe, risk (2% daily), rules: the unmodified object
        self.assertEqual((policy.name, policy.allocation_fraction), (ORIGINAL, None))
        after = self.ledger.account()
        self.assertEqual((after.positions, after.ledger), (account.positions, account.ledger))  # nothing closed or lost
        # Falling back below $10,000 does not switch back, and restarts keep the active profile.
        after.cash = 2_000
        save_account(self.ledger.account_path, after)
        self.assertIsNone(self.ledger.check_transition(boundary="session_start"))
        reloaded = ProfileLedger(ProfileConfig.load(self.root, "profiles/small_account.json"), original_data_dir=self.root / "data")
        self.assertEqual(reloaded.active_profile, ORIGINAL)
        events = [json.loads(line)["event"] for line in reloaded.audit_path.read_text().splitlines()]
        self.assertIn("profile_transition", events)
        self.assertTrue((reloaded.data_dir / "notifications.jsonl").exists())

    def test_quote_spike_cannot_trigger_transition(self):
        self.ledger.initialize()
        account = self.ledger.account()
        account.cash = 600.0
        account.positions.append({**position(price=4.0), "last_bid": 500.0})  # absurd mark: $50,000 of "value"
        save_account(self.ledger.account_path, account)
        self.assertGreater(self.ledger.account().equity, 10_000)
        self.assertIsNone(self.ledger.check_transition(boundary="session_end"))
        self.assertEqual(self.ledger.active_profile, SMALL)

    def test_original_cannot_be_restored_from_a_different_strategy(self):
        changed = replace(self.strategy, sha256="0" * 64)
        with self.assertRaises(ProfileError):
            verify_original_baseline(changed, self.config.baseline)
        with self.assertRaises(ProfileError):
            verify_original_baseline(replace(self.strategy, risk={**self.strategy.risk, "max_open_positions": 9}),
                                     {**self.config.baseline, "configuration_hash": self.strategy.sha256})

    def test_profiles_refuse_live_mode(self):
        cfg = AppConfig(mode=Mode.LIVE, live_enabled=True, live_confirmation="I UNDERSTAND LIVE TRADING USES REAL MONEY",
                        client_id="x", client_secret="y", refresh_token="z", account_number="5WT00001", data_dir=self.root / "data")
        with self.assertRaises(ProfileError):
            run_profile_paper_session(root=self.root, cfg=cfg, strategy=self.strategy, profile_name="profiles/small_account.json")

    def test_dashboard_shows_the_active_profile_separately_from_the_original_ledger(self):
        try:
            from trader.dashboard import create_app
            app = create_app(self.root / "data", self.root)
        except RuntimeError as exc:
            self.skipTest(str(exc))
        shutil.copytree(self.root / "profiles", self.root / "profiles_backup")
        (self.root / "profiles" / "small_account.json").rename(self.root / "profiles" / "small_account.json.bak")
        endpoints = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
        self.assertFalse(endpoints["/api/profile"]()["configured"])
        (self.root / "profiles" / "small_account.json.bak").rename(self.root / "profiles" / "small_account.json")
        with patch("trader.account_profiles.PROFILE_FILES", {"small": "profiles/small_account.json"}):
            self.ledger.initialize()
            panel = endpoints["/api/status"]()["account_profile"]
        self.assertEqual((panel["active_profile"], panel["max_aggregate_allocation"], panel["symbols_scanned"]),
                         (SMALL, 400.0, len(EXPANDED)))
        self.assertIn("PROFILE: ", endpoints["/"]())

    def test_status_reports_the_dashboard_fields(self):
        self.ledger.initialize()
        status = self.ledger.status(self.strategy)
        for key in ("active_profile", "starting_balance", "equity", "cash", "open_max_loss", "max_aggregate_allocation",
                    "remaining_risk_budget", "symbols_scanned", "signals_today", "affordable_candidates_today",
                    "rejected_today", "rejection_reasons_today", "progress_to_threshold"):
            self.assertIn(key, status)
        self.assertEqual((status["max_aggregate_allocation"], status["remaining_risk_budget"], status["symbols_scanned"]),
                         (400.0, 400.0, len(EXPANDED)))


class ProfileLoopTests(unittest.TestCase):
    """The shared PAPER loop with fake market data: identical analysis, aggregate cap at the fill."""

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

    def _session(self, root: Path, *, symbols, ask: float, profile_ledger: bool, original_ledger: bool = False):
        from trader import paper_runner
        now = datetime.now(timezone.utc)
        open_utc = now.replace(second=0, microsecond=0) - timedelta(minutes=22)
        template = self._bars()
        shift = open_utc - template[0].timestamp
        session = [Bar(b.timestamp + shift, b.open, b.high, b.low, b.close, b.volume) for b in template]
        history, breakout = session[:-1], session[-1]

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
            return [OptionQuote(f"{underlying:<6}261030C00100000", underlying, now + timedelta(days=12), 100.0, "C",
                                round(ask - .05, 2), ask, delta=.55, gamma=.01, theta=-.02, vega=.1, volume=500,
                                open_interest=1000, quote_timestamp=datetime.now(timezone.utc))]

        strategy = parse_strategy(root / "strategy.txt")
        no_flat = replace(strategy, execution={**strategy.execution, "flatten_at_session_close": False})
        cfg = AppConfig(mode=Mode.PAPER, data_dir=root / "data", client_id="x", client_secret="y", refresh_token="z")
        if original_ledger:
            # A pre-existing original ledger with history, to prove the profile never touches it.
            save_account(cfg.data_dir / "paper_account.json", PaperAccount(
                starting_capital=10_000, cash=10_920.16, realized_pnl=920.16,
                ledger=[{"type": "paper_close", "trade_id": "old", "realized_pnl": 920.16}]))
        before = (cfg.data_dir / "paper_account.json").read_bytes() if original_ledger else None
        with patch.object(paper_runner, "MarketCalendar", FakeCalendar), \
                patch.object(paper_runner, "TastytradeClient", FakeClient), \
                patch.object(paper_runner, "ProductionMarketData", FakeQuotes), \
                patch.object(paper_runner, "create_history_provider", lambda *a, **k: FakeHistory()), \
                patch.object(paper_runner, "live_minute_bars", fake_live), \
                patch.object(paper_runner, "fetch_option_quotes", fake_options), \
                patch.object(paper_runner, "ENTRY_ARBITRATION_SECONDS", 0.05):
            try:
                if profile_ledger:
                    ledger = ProfileLedger(ProfileConfig.load(root, "profiles/small_account.json"), original_data_dir=cfg.data_dir)
                    ledger.initialize()
                    run, policy = ledger.runtime(strategy)
                    run = replace(run, execution=no_flat.execution)
                    data_dir = ledger.data_dir
                    asyncio.run(paper_runner._run(root=root, cfg=replace(cfg, data_dir=data_dir, starting_cash=1000),
                                                  strategy=run, symbols=tuple(symbols),
                                                  profile=policy if policy.allocation_fraction is not None else None))
                else:
                    data_dir = cfg.data_dir
                    asyncio.run(paper_runner._run(root=root, cfg=replace(cfg, starting_cash=1000), strategy=no_flat,
                                                  symbols=tuple(symbols)))
            except RuntimeError as exc:
                if "Every symbol in the session was disabled" not in str(exc):
                    raise
            finally:
                paper_runner._close_session_log_file(paper_runner._session_logger(data_dir))
        decisions = [json.loads(line) for line in (data_dir / "decisions.jsonl").read_text().splitlines()]
        account = load_or_initialize(data_dir / "paper_account.json", 1000)
        return decisions, account, before, cfg

    def _root(self, tmp, universe=("SPY", "IWM", "F")):
        return make_root(Path(tmp), universe=list(universe))

    def test_two_simultaneous_signals_respect_the_combined_400_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            decisions, account, _, _ = self._session(root, symbols=("SPY", "IWM"), ask=2.00, profile_ledger=True)
            entries = [d for d in decisions if d["decision"] == "PAPER_ENTRY"]
            self.assertGreaterEqual(len(entries), 1)
            self.assertEqual(len(account.positions), 1, "a second $201.44 position would take open max loss past $400")
            blocked = [d for d in decisions if d["diagnostics"].get("underlying_signal") and d["decision"] != "PAPER_ENTRY"]
            self.assertEqual(len(entries) + len(blocked), 2)
            exposure = AccountProfile(SMALL, .4).open_exposure(account)
            self.assertLessEqual(exposure, 400.0)
            self.assertAlmostEqual(exposure, 201.44)  # $2.00 x 100 + modeled round-trip costs

    def test_unaffordable_contract_is_skipped_not_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            decisions, account, _, _ = self._session(root, symbols=("SPY",), ask=4.05, profile_ledger=True)  # $406.44 > $400
            self.assertEqual(account.positions, [])
            signal_rows = [d for d in decisions if d["diagnostics"].get("underlying_signal")]
            self.assertTrue(signal_rows)
            self.assertTrue(all(d["decision"] == "NO_TRADE" for d in signal_rows))

    def test_same_market_data_gives_identical_analysis_and_only_the_budget_differs(self):
        analysis = ("underlying_signal", "underlying_filter_reason", "trend_state", "breakout_state", "relative_volume",
                    "atr", "ema9", "ema21", "opening_range_high", "opening_range_low", "underlying_stop_trigger",
                    "underlying_target_trigger", "option_candidate", "expected_net_r", "modeled_risk_dollars", "quantity")
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            original, _, _, _ = self._session(self._root(a), symbols=("SPY",), ask=0.90, profile_ledger=False)
            profiled, account, _, _ = self._session(self._root(b), symbols=("SPY",), ask=0.90, profile_ledger=True)
            key = lambda rows: [(r["underlying"], r["decision"], r["reason"],
                                 tuple(json.dumps(r["diagnostics"].get(f), sort_keys=True) for f in analysis)) for r in rows]
            self.assertEqual(key(original), key(profiled))
            self.assertTrue(any(r["decision"] == "PAPER_ENTRY" for r in profiled))
            # Original per-trade budget is 10% of $1,000; the profile's is the remaining $400 pool.
            self.assertEqual({r["diagnostics"].get("risk_budget_dollars") for r in original if r["diagnostics"].get("quantity")}, {100.0})
            self.assertEqual({round(r["diagnostics"].get("risk_budget_dollars"), 2) for r in profiled if r["diagnostics"].get("quantity")}, {400.0})

    def test_original_ledger_is_untouched_by_a_profile_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _, account, before, cfg = self._session(root, symbols=("SPY",), ask=2.00, profile_ledger=True, original_ledger=True)
            self.assertEqual((cfg.data_dir / "paper_account.json").read_bytes(), before)
            self.assertFalse((cfg.data_dir / "decisions.jsonl").exists())
            self.assertEqual(len(account.positions), 1)
            self.assertEqual(account.starting_capital, 1000.0)

    def test_restart_resumes_the_profile_ledger_and_its_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            _, first, _, _ = self._session(root, symbols=("SPY",), ask=2.00, profile_ledger=True)
            self.assertEqual(len(first.positions), 1)
            _, second, _, _ = self._session(root, symbols=("SPY", "IWM"), ask=2.00, profile_ledger=True)
            # The carried SPY position is resumed and still counts against the cap (no averaging into SPY).
            self.assertEqual(second.starting_capital, 1000.0)
            self.assertIn("SPY", [p["underlying"] for p in second.positions])
            self.assertLessEqual(AccountProfile(SMALL, .4).open_exposure(second), 400.0)


class DailyLimitOverrideTests(unittest.TestCase):
    def test_only_the_daily_loss_fraction_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            path = root / "profiles" / "small_account.json"
            data = json.loads(path.read_text())
            data["profiles"][SMALL]["risk_overrides"] = {"max_trades_per_day": 50}
            path.write_text(json.dumps(data))
            with self.assertRaises(ProfileError):
                ProfileConfig.load(root, "profiles/small_account.json")

    def test_repository_small_profile_uses_a_20_percent_daily_limit(self):
        config = ProfileConfig.load(ROOT, "small")
        self.assertEqual(dict(config.small_risk_overrides), {"max_daily_loss_fraction": 0.20})


class RequestPacingTests(unittest.TestCase):
    def tearDown(self):
        from trader import tastytrade_client
        tastytrade_client.set_request_rate_limit(None)

    def _client(self):
        from trader.tastytrade_client import TastytradeClient
        client = TastytradeClient("x", "y", "z", base_url="https://example.invalid")
        client._access_token = lambda: "token"
        return client

    @staticmethod
    def _http_error(code):
        import io, urllib.error
        return urllib.error.HTTPError("https://example.invalid", code, "x", {}, io.BytesIO(b"limited"))

    def test_pacing_is_off_by_default_and_429_is_not_retried(self):
        from trader.tastytrade_client import BrokerError
        calls = []
        def urlopen(req, timeout):
            calls.append(req.get_method()); raise self._http_error(429)
        with patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(BrokerError):
                self._client()._request("GET", "/market-data/by-type?equity[]=SPY")
        self.assertEqual(calls, ["GET"])

    def test_paced_get_is_retried_after_429_but_orders_never_are(self):
        from trader import tastytrade_client
        from trader.tastytrade_client import BrokerError
        tastytrade_client.set_request_rate_limit(1000)
        tastytrade_client._PACER.backoff_seconds = 0.0
        calls = []
        class Response:
            headers = {}
            def read(self): return b'{"data": {"items": []}}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def urlopen(req, timeout):
            calls.append(req.get_method())
            if len(calls) < 3:
                raise self._http_error(429)
            return Response()
        with patch("urllib.request.urlopen", urlopen):
            self.assertEqual(self._client()._request("GET", "/market-data/by-type?equity[]=SPY"), {"data": {"items": []}})
        self.assertEqual(calls, ["GET", "GET", "GET"])
        calls.clear()
        with patch("urllib.request.urlopen", lambda req, timeout: (calls.append(req.get_method()), (_ for _ in ()).throw(self._http_error(429)))[1]):
            with self.assertRaises(BrokerError):
                self._client()._request("POST", "/accounts/x/orders", body={"legs": []})
        self.assertEqual(calls, ["POST"])

    def test_pacer_spaces_request_starts(self):
        import time
        from trader.tastytrade_client import _RequestPacer
        pacer = _RequestPacer(50)
        start = time.monotonic()
        for _ in range(6):
            pacer.wait()
        self.assertGreaterEqual(time.monotonic() - start, 5 / 50 - 0.01)


class RealProfileConfigTests(unittest.TestCase):
    def test_repository_profile_matches_the_recorded_original_and_expands_the_universe(self):
        strategy = parse_strategy(ROOT / "strategy.txt")
        config = ProfileConfig.load(ROOT, "small")
        verify_original_baseline(strategy, config.baseline)
        self.assertEqual((config.starting_balance, config.small_allocation_fraction, config.transition_equity),
                         (1000.0, 0.40, 10000.0))
        self.assertEqual(list(config.small_universe[:14]), list(strategy.universe))  # expanded, never replaced
        self.assertGreater(len(config.small_universe), len(strategy.universe))
        self.assertEqual(len(config.small_universe), len(set(config.small_universe)))
        self.assertNotEqual(config.data_dir.resolve(), (ROOT / "data").resolve())


class StrategyIntegrityTests(unittest.TestCase):
    def test_strategy_files_match_the_recorded_baseline(self):
        baseline = ROOT / "docs/profiles/baseline_20261016/strategy_sha256.txt"
        frozen = {"trader/paper_runner.py"}  # the shared loop: profile hook added (reported separately)
        for line in baseline.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            digest, name = line.split(" *", 1)
            if name in frozen:
                continue
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
