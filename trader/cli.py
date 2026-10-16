from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest import load_bars, replay
from .config import AppConfig, Mode
from .dashboard import create_app
from .portfolio import load_or_initialize
from .strategy_loader import parse_strategy, verify_manifest, write_manifest
from .market_data import fetch_historical_candles, fetch_massive_historical_candles
from .metrics import calculate_performance
from .models import Bar
from .tastytrade_client import TastytradeClient
import asyncio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optio-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-strategy")
    sub.add_parser("init-paper")
    paper = sub.add_parser("paper-session", help="Run a supervised local paper simulation from the equity open to close")
    paper_test = sub.add_parser("paper-test-session", help="Run an isolated afternoon paper test with a rolling range and fake test capital")
    for command in (paper, paper_test):
        command.add_argument("--symbol", action="append", type=str.upper, dest="symbols",
                             help="Limit the session to this underlying (repeatable). Default: the whole strategy universe.")
    paper.add_argument("--account-profile", choices=("small",), default=None,
                       help="Run on an isolated account-profile PAPER ledger (small: $1,000, 40%% aggregate cap, "
                            "expanded universe; restores the original settings at $10,000). Default: the original ledger.")
    profile_status = sub.add_parser("profile-status", help="Show an account-profile PAPER ledger (read-only)")
    profile_status.add_argument("--account-profile", choices=("small",), default="small")
    profile_report = sub.add_parser("profile-report",
                                    help="Write a self-updating HTML report of an account-profile PAPER ledger (read-only)")
    profile_report.add_argument("--account-profile", choices=("small",), default="small")
    paper_test.add_argument("--test-capital", type=float, default=100_000,
                            help="Fake capital for the isolated paper test ledger (default: 100000)")
    back = sub.add_parser("backtest")
    activate = sub.add_parser("activate-strategy",
                              help="Backtest strategy.txt and, with --approve, make it the active strategy")
    for command in (back, activate):
        command.add_argument("csv", nargs="?", help="Optional local OHLCV CSV; otherwise candles are retrieved from the selected provider")
        command.add_argument("--symbol", action="append", type=str.upper, dest="symbols",
                             help="Underlying to backtest (repeatable). Default: the whole strategy universe.")
        command.add_argument("--start", help="Timezone-aware ISO-8601 start for automatic history")
        command.add_argument("--end", help="Timezone-aware ISO-8601 end for automatic history")
        command.add_argument("--source", choices=("massive", "tastytrade"), default="massive",
                             help="Historical candle source when no CSV is supplied (default: massive)")
        command.add_argument("--in-sample-end", required=True)
    activate.add_argument("--approve", action="store_true",
                          help="Owner approval: activate if the backtest passes. Without it, only report.")
    activate.add_argument("--note", default="", help="Why this change is being made (stored in the manifest)")
    live = sub.add_parser("live-session",
                          help="REAL-MONEY: run the active strategy through the configured tastytrade account")
    live_check = sub.add_parser("live-check",
                                help="Read-only LIVE readiness report (account, permissions, data); never places orders")
    for command in (live, live_check):
        command.add_argument("--symbol", action="append", type=str.upper, dest="symbols",
                             help="Limit to this underlying (repeatable). Default: the whole strategy universe.")
    live_check.add_argument("--skip-market-data", action="store_true", help="Only check account/authorization")
    authorize = sub.add_parser("authorize-live",
                               help="Owner action: authorize LIVE for the exact active strategy hash and account")
    authorize.add_argument("--strategy-hash", required=True, help="Full configuration hash shown by validate-strategy")
    authorize.add_argument("--account-suffix", required=True, help="Last 4 characters of TASTYTRADE_ACCOUNT_NUMBER")
    authorize.add_argument("--statement", required=True, help='Exactly: "I AUTHORIZE LIVE EXECUTION OF THIS EXACT STRATEGY HASH"')
    authorize.add_argument("--note", default="")
    dash = sub.add_parser("dashboard"); dash.add_argument("--host", default="127.0.0.1"); dash.add_argument("--port", type=int, default=8765)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = Path.cwd(); cfg = AppConfig.from_env()
    try:
        cfg.validate_startup()
        if args.command == "validate-strategy":
            strategy = parse_strategy(root / "strategy.txt")
            verify_manifest(root / "strategy.txt", root / "active_strategy.json")
            print(json.dumps({"strategy_id": strategy.strategy_id, "version": strategy.version, "status": strategy.status,
                              "filename": strategy.filename, "configuration_hash": strategy.sha256,
                              "mode": cfg.mode, "live_enabled": cfg.live_enabled}, indent=2))
        elif args.command == "init-paper":
            account = load_or_initialize(cfg.data_dir / "paper_account.json", cfg.starting_cash)
            print(json.dumps({"starting_capital": account.starting_capital, "cash": account.cash, "mode": "PAPER"}, indent=2))
        elif args.command == "paper-session":
            from .paper_runner import run_paper_session
            strategy = parse_strategy(root / "strategy.txt")
            if args.account_profile:
                from .account_profiles import run_profile_paper_session
                run_profile_paper_session(root=root, cfg=cfg, strategy=strategy, profile_name=args.account_profile,
                                          symbols=tuple(args.symbols) if args.symbols else None)
            else:
                run_paper_session(root=root, cfg=cfg, strategy=strategy,
                                  symbols=tuple(args.symbols) if args.symbols else None)
        elif args.command == "profile-status":
            from .account_profiles import ProfileConfig, ProfileLedger
            strategy = parse_strategy(root / "strategy.txt")
            ledger = ProfileLedger(ProfileConfig.load(root, args.account_profile), original_data_dir=cfg.data_dir)
            status = ledger.status(strategy)
            status.pop("universe", None)
            print(json.dumps(status, indent=2))
        elif args.command == "profile-report":
            from .profile_report import write_report
            print(write_report(root, cfg.data_dir, args.account_profile))
        elif args.command == "paper-test-session":
            from .paper_runner import run_paper_test_session
            if cfg.mode is Mode.LIVE:
                raise RuntimeError("paper-test-session is forbidden in LIVE mode")
            strategy = parse_strategy(root / "strategy.txt")
            run_paper_test_session(root=root, cfg=cfg, strategy=strategy,
                                   symbols=tuple(args.symbols) if args.symbols else None,
                                   test_capital=args.test_capital)
        elif args.command == "backtest":
            strategy = parse_strategy(root / "strategy.txt")
            print(json.dumps(_backtest(args, cfg, strategy), indent=2, allow_nan=False))
        elif args.command == "activate-strategy":
            return _activate_strategy(args, cfg, root)
        elif args.command == "live-session":
            from .live_session import run_live_session
            run_live_session(root=root, cfg=cfg, symbols=tuple(args.symbols) if args.symbols else None)
        elif args.command == "live-check":
            from .live_session import live_readiness, write_readiness_report
            report = live_readiness(root=root, cfg=cfg, symbols=tuple(args.symbols) if args.symbols else None,
                                    market_data=not args.skip_market_data)
            path = write_readiness_report(cfg, report)
            print(json.dumps({"overall": report["overall"], "blockers": report["blockers"],
                              "warnings": report["warnings"], "report": str(path)}, indent=2, default=str))
            return 0 if report["overall"] == "READY" else 3
        elif args.command == "authorize-live":
            from .live_session import authorize_live
            path = authorize_live(root=root, cfg=cfg, strategy_hash=args.strategy_hash,
                                  account_suffix=args.account_suffix, statement=args.statement, note=args.note)
            print(json.dumps({"authorized": True, "file": str(path)}, indent=2))
        elif args.command == "dashboard":
            import uvicorn
            uvicorn.run(create_app(cfg.data_dir, root), host=args.host, port=args.port)
        elif args.command == "status":
            strategy = parse_strategy(root / "strategy.txt")
            print(json.dumps({"mode": cfg.mode, "live_enabled": cfg.live_enabled, "strategy_id": strategy.strategy_id,
                              "strategy_version": strategy.version, "strategy_status": strategy.status,
                              "strategy_hash": strategy.sha256}, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _backtest(args: argparse.Namespace, cfg: AppConfig, strategy) -> dict:
    """Replay each requested symbol separately, then pool the trades into one in/out-of-sample result."""
    in_sample_end = datetime.fromisoformat(args.in_sample_end.replace("Z", "+00:00"))
    if in_sample_end.tzinfo is None or in_sample_end.utcoffset() is None:
        raise ValueError("--in-sample-end must include a timezone/UTC offset")
    data_source = ("local_csv" if args.csv else "massive_historical_minute_aggregates" if args.source == "massive"
                   else "tastytrade_dxlink_candles")
    if args.csv:
        series = {(args.symbols or ["CSV"])[0]: load_bars(args.csv)}
    else:
        if not args.start or not args.end:
            raise ValueError("Provide a CSV path, or --start and --end (and optionally --symbol) to retrieve historical candles")
        symbols = list(dict.fromkeys(args.symbols or strategy.universe))
        outside = [symbol for symbol in symbols if symbol not in strategy.universe]
        if outside:
            raise ValueError(f"{', '.join(outside)} not in the strategy universe")
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        token_data = None
        series = {}
        cache_dir = cfg.data_dir / "backtest_cache"
        fetched = 0
        for symbol in symbols:
            # Completed history does not change, so cache it: Massive Basic allows 5 calls/minute and
            # 14 symbols x 4 months otherwise takes ~12 minutes per backtest.
            cache = cache_dir / f"{args.source}-{symbol}-{start:%Y%m%dT%H%M}-{end:%Y%m%dT%H%M}.json"
            if end < datetime.now(timezone.utc) and cache.is_file():
                candle_rows = json.loads(cache.read_text(encoding="utf-8"))
            elif args.source == "massive":
                if fetched:
                    time.sleep(12)  # Massive Basic allows five calls per minute across symbols too
                fetched += 1
                candle_rows = fetch_massive_historical_candles(api_key=cfg.massive_api_key, symbol=symbol,
                                                               start=start, end=end)
            else:
                if token_data is None:
                    cfg.require_market_data_credentials()
                    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
                    token_data = client.quote_token().get("data", {})
                    if not token_data.get("token") or not token_data.get("dxlink-url"):
                        raise RuntimeError("tastytrade quote-token response lacked token or dxlink-url")
                candle_rows = asyncio.run(fetch_historical_candles(dxlink_url=token_data["dxlink-url"],
                    quote_token=token_data["token"], symbol=symbol, start=start, end=end))
            if end < datetime.now(timezone.utc) and not cache.is_file():
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(candle_rows, default=str), encoding="utf-8")
            series[symbol] = [Bar(datetime.fromisoformat(row["timestamp"]), row["open"], row["high"], row["low"],
                                  row["close"], row["volume"], row.get("vwap")) for row in candle_rows]
            print(f"backtest data: {symbol} {len(series[symbol])} bars", file=sys.stderr, flush=True)
    per_symbol, signals, model = {}, [], ""
    for symbol, bars in series.items():
        result = replay(bars, strategy, in_sample_end=in_sample_end)
        model = result["model"]
        per_symbol[symbol] = {"bars": len(bars), "trades": result["trade_count"],
                              "out_of_sample": result["out_of_sample"]}
        signals.extend({**row, "symbol": symbol} for row in result["signals"])
    signals.sort(key=lambda row: row["timestamp"])
    rows = [{**row, "realized_pnl": row["net_pnl"]} for row in signals]
    in_sample_end_iso = in_sample_end.astimezone(timezone.utc)
    train = [row for row in rows if datetime.fromisoformat(row["timestamp"]) <= in_sample_end_iso]
    test = [row for row in rows if datetime.fromisoformat(row["timestamp"]) > in_sample_end_iso]
    return {"model": model, "trade_count": len(signals), "in_sample": calculate_performance(train).__dict__,
            "out_of_sample": calculate_performance(test).__dict__, "per_symbol": per_symbol,
            "signals": signals, "bars_retrieved": sum(len(bars) for bars in series.values()),
            "symbols": list(series), "data_source": data_source}


def _config_changes(old: dict, new: dict, prefix: str = "") -> list[str]:
    changes = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if isinstance(before, dict) and isinstance(after, dict):
            changes.extend(_config_changes(before, after, f"{prefix}{key}."))
        elif before != after:
            changes.append(f"{prefix}{key}: {json.dumps(before)} -> {json.dumps(after)}")
    return changes


def _activate_strategy(args: argparse.Namespace, cfg: AppConfig, root: Path) -> int:
    """Backtest gate for strategy changes. Activation needs a passing backtest AND --approve."""
    strategy = parse_strategy(root / "strategy.txt")
    manifest_path = root / "active_strategy.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    changes = _config_changes(current.get("configuration") or {}, strategy.configuration)
    result = _backtest(args, cfg, strategy)
    # The replay is an underlying-only screen, so the gate checks that the rules produce trades on real
    # history; profitability judgement stays with the owner, who sees the numbers below before approving.
    failures = []
    if result["bars_retrieved"] == 0:
        failures.append("backtest retrieved no bars")
    if result["trade_count"] == 0:
        failures.append("strategy produced no signals on the backtest data")
    summary = {"configuration_hash": strategy.sha256, "version": strategy.version,
               "data_source": result["data_source"], "symbols": result["symbols"], "start": args.start, "end": args.end,
               "in_sample_end": args.in_sample_end, "bars_retrieved": result["bars_retrieved"],
               "trade_count": result["trade_count"], "in_sample": result["in_sample"],
               "out_of_sample": result["out_of_sample"], "per_symbol": result["per_symbol"], "model": result["model"],
               "passed": not failures, "failures": failures}
    report_dir = cfg.data_dir / "backtests"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{strategy.sha256[:12]}.json"
    report_path.write_text(json.dumps({**summary, "signals": result["signals"]}, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    activated = bool(args.approve and not failures and changes)
    if activated:
        write_manifest(strategy, manifest_path, {
            "status": "research_test", "mode": "PAPER", "live_enabled": False,
            "approved_by": "owner (activate-strategy --approve)", "note": args.note,
            "changes_from_previous": changes, "backtest_report": str(report_path.relative_to(root)
                                                                   if report_path.is_relative_to(root) else report_path),
            "backtest_summary": {key: summary[key] for key in ("data_source", "symbols", "start", "end",
                                                               "bars_retrieved", "trade_count", "in_sample", "out_of_sample")}})
        verify_manifest(root / "strategy.txt", manifest_path)
    print(json.dumps({"changes": changes or ["none: strategy.txt already matches the active strategy"],
                      "backtest": {key: value for key, value in summary.items() if key != "model"},
                      "backtest_report": str(report_path), "activated": activated,
                      "next_step": ("Restart paper-session to use this strategy." if activated else
                                    "Fix the failures above." if failures else
                                    "Nothing to activate." if not changes else
                                    "Review the numbers, then re-run with --approve to activate.")},
                     indent=2, allow_nan=False))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
