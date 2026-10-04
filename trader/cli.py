from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backtest import load_bars, replay
from .config import AppConfig
from .dashboard import create_app
from .portfolio import load_or_initialize
from .strategy_loader import parse_strategy, verify_manifest, write_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optio-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-strategy")
    sub.add_parser("init-paper")
    back = sub.add_parser("backtest"); back.add_argument("csv"); back.add_argument("--in-sample-end", required=True)
    dash = sub.add_parser("dashboard"); dash.add_argument("--host", default="127.0.0.1"); dash.add_argument("--port", type=int, default=8765)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = Path.cwd(); cfg = AppConfig.from_env()
    try:
        cfg.validate_startup()
        if args.command == "validate-strategy":
            strategy = parse_strategy(root / "strategy.txt")
            verify_manifest(root / "strategy.txt", root / "active_strategy.json")
            print(json.dumps({"version": strategy.version, "filename": strategy.filename, "sha256": strategy.sha256, "mode": cfg.mode}, indent=2))
        elif args.command == "init-paper":
            account = load_or_initialize(cfg.data_dir / "paper_account.json", cfg.starting_cash)
            print(json.dumps({"starting_capital": account.starting_capital, "cash": account.cash, "mode": "PAPER"}, indent=2))
        elif args.command == "backtest":
            strategy = parse_strategy(root / "strategy.txt")
            in_sample_end = datetime.fromisoformat(args.in_sample_end.replace("Z", "+00:00"))
            if in_sample_end.tzinfo is None or in_sample_end.utcoffset() is None:
                raise ValueError("--in-sample-end must include a timezone/UTC offset")
            result = replay(load_bars(args.csv), strategy, in_sample_end=in_sample_end)
            print(json.dumps(result, indent=2, allow_nan=False))
        elif args.command == "dashboard":
            import uvicorn
            uvicorn.run(create_app(cfg.data_dir, root), host=args.host, port=args.port)
        elif args.command == "status":
            print(json.dumps({"mode": cfg.mode, "live_enabled": cfg.live_enabled, "strategy": parse_strategy(root / "strategy.txt").version}, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
