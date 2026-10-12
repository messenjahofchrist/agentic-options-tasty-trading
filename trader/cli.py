from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backtest import load_bars, replay
from .config import AppConfig, Mode
from .dashboard import create_app
from .portfolio import load_or_initialize
from .strategy_loader import parse_strategy, verify_manifest, write_manifest
from .market_data import fetch_historical_candles, fetch_massive_historical_candles
from .models import Bar
from .tastytrade_client import TastytradeClient
import asyncio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optio-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-strategy")
    sub.add_parser("init-paper")
    paper = sub.add_parser("paper-session", help="Run a supervised local paper simulation from the equity open to close")
    paper.add_argument("--symbol", choices=("SPY", "QQQ"), default="SPY")
    paper_test = sub.add_parser("paper-test-session", help="Run an isolated afternoon paper test with a rolling range and fake test capital")
    paper_test.add_argument("--symbol", choices=("SPY", "QQQ"), default="SPY")
    paper_test.add_argument("--test-capital", type=float, default=100_000,
                            help="Fake capital for the isolated paper test ledger (default: 100000)")
    back = sub.add_parser("backtest")
    back.add_argument("csv", nargs="?", help="Optional local OHLCV CSV; otherwise candles are retrieved from the selected provider")
    back.add_argument("--symbol", choices=("SPY", "QQQ"))
    back.add_argument("--start", help="Timezone-aware ISO-8601 start for automatic history")
    back.add_argument("--end", help="Timezone-aware ISO-8601 end for automatic history")
    back.add_argument("--source", choices=("massive", "tastytrade"), default="massive",
                      help="Historical candle source when no CSV is supplied (default: massive)")
    back.add_argument("--in-sample-end", required=True)
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
            run_paper_session(root=root, cfg=cfg, strategy=strategy, symbol=args.symbol)
        elif args.command == "paper-test-session":
            from .paper_runner import run_paper_test_session
            if cfg.mode is Mode.LIVE:
                raise RuntimeError("paper-test-session is forbidden in LIVE mode")
            strategy = parse_strategy(root / "strategy.txt")
            run_paper_test_session(root=root, cfg=cfg, strategy=strategy, symbol=args.symbol,
                                   test_capital=args.test_capital)
        elif args.command == "backtest":
            strategy = parse_strategy(root / "strategy.txt")
            in_sample_end = datetime.fromisoformat(args.in_sample_end.replace("Z", "+00:00"))
            if in_sample_end.tzinfo is None or in_sample_end.utcoffset() is None:
                raise ValueError("--in-sample-end must include a timezone/UTC offset")
            if args.csv:
                bars = load_bars(args.csv)
            else:
                if not args.symbol or not args.start or not args.end:
                    raise ValueError("Provide a CSV path, or --symbol, --start and --end to retrieve historical candles")
                start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
                end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
                if args.source == "massive":
                    candle_rows = fetch_massive_historical_candles(api_key=cfg.massive_api_key, symbol=args.symbol,
                                                                   start=start, end=end)
                else:
                    cfg.require_market_data_credentials()
                    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
                    token_data = client.quote_token().get("data", {})
                    if not token_data.get("token") or not token_data.get("dxlink-url"):
                        raise RuntimeError("tastytrade quote-token response lacked token or dxlink-url")
                    candle_rows = asyncio.run(fetch_historical_candles(dxlink_url=token_data["dxlink-url"],
                        quote_token=token_data["token"], symbol=args.symbol, start=start, end=end))
                bars = [Bar(datetime.fromisoformat(row["timestamp"]), row["open"], row["high"], row["low"],
                            row["close"], row["volume"], row.get("vwap")) for row in candle_rows]
            result = replay(bars, strategy, in_sample_end=in_sample_end)
            result["bars_retrieved"] = len(bars)
            result["data_source"] = ("local_csv" if args.csv else
                                      "massive_historical_minute_aggregates" if args.source == "massive" else
                                      "tastytrade_dxlink_candles")
            print(json.dumps(result, indent=2, allow_nan=False))
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


if __name__ == "__main__":
    raise SystemExit(main())
