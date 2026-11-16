"""Month-chunked one-minute history cache for long replays (review 2026-11-11, recommendation 5).

The original cache (data/backtest_cache/massive-SYM-20260601T0000-20261010T0000.json) is one file per symbol for one
fixed window. Two years in one file per symbol would not fit comfortably in memory on this VM, so longer history is
stored one file per symbol per calendar month:

    data/backtest_cache/chunks/massive-SPY-2025-03.json

and the replay loads only the months it needs (trader.v2.replay.load_massive_cache with start/end).

    python -m trader.v2.history_cache download --start 2024-11 --end 2026-05     # resumes; skips finished months
    python -m trader.v2.history_cache status

The Massive Basic plan allows 5 requests a minute and about two years of one-minute history (checked 2026-11-11:
2024-11-12 returned data, 2024-06-04 was refused with "Your plan doesn't include this data timeframe").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

CHUNK_DIR = "data/backtest_cache/chunks"
SECONDS_BETWEEN_REQUESTS = 12.5


def months(start: str, end: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' strings."""
    year, month = map(int, start.split("-"))
    last = tuple(map(int, end.split("-")))
    out = []
    while (year, month) <= last:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def chunk_path(root: Path, symbol: str, month: str) -> Path:
    return root / CHUNK_DIR / f"massive-{symbol}-{month}.json"


def month_bounds(month: str) -> tuple[datetime, datetime]:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=timezone.utc)
    end = datetime(year + (number == 12), 1 if number == 12 else number + 1, 1, tzinfo=timezone.utc)
    return start, end


def iter_rows(root: Path, symbol: str, start: date, end: date) -> Iterator[dict]:
    """Rows from the month chunks covering [start, end], in time order."""
    for month in months(f"{start:%Y-%m}", f"{end:%Y-%m}"):
        path = chunk_path(root, symbol, month)
        if not path.is_file():
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            day = row["timestamp"][:10]
            if start.isoformat() <= day <= end.isoformat():
                yield row


def download(root: Path, symbols: list[str], month_list: list[str], api_key: str, log=print) -> dict:
    from ..market_data import fetch_massive_historical_candles
    (root / CHUNK_DIR).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    done = failed = skipped = 0
    first = True
    for symbol in symbols:
        for month in month_list:
            path = chunk_path(root, symbol, month)
            start, end = month_bounds(month)
            complete_month = end <= now
            if path.is_file() and complete_month:
                skipped += 1
                continue
            if not first:   # the limit is per request started, so only wait out what the last request left
                time.sleep(max(0.0, SECONDS_BETWEEN_REQUESTS - (time.monotonic() - started_at)))
            first = False
            started_at = time.monotonic()
            try:
                rows = fetch_massive_historical_candles(api_key=api_key, symbol=symbol, start=start,
                                                        end=min(end, now), timeout_seconds=60)
            except RuntimeError as exc:
                failed += 1
                log(f"{symbol} {month} FAILED {str(exc)[:160]}")
                continue
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
            done += 1
            log(f"{symbol} {month} {len(rows)} bars")
    return {"downloaded": done, "failed": failed, "skipped": skipped}


def status(root: Path) -> dict:
    found: dict[str, list[str]] = {}
    for path in sorted((root / CHUNK_DIR).glob("massive-*.json")):
        _, symbol, month = path.stem.split("-", 2)
        found.setdefault(symbol, []).append(month)
    return {s: f"{m[0]}..{m[-1]} ({len(m)} months)" for s, m in found.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.v2.history_cache")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("download")
    d.add_argument("--start", required=True, help="YYYY-MM")
    d.add_argument("--end", required=True, help="YYYY-MM")
    d.add_argument("--symbols", default="", help="comma-separated (default: V2 trade and context symbols)")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "status":
        print(json.dumps(status(root), indent=1))
        return 0
    from ..config import load_dotenv
    from .config import load_v2_config
    load_dotenv()
    config = load_v2_config(root)
    symbols = [s for s in args.symbols.split(",") if s] or list(dict.fromkeys([*config.base.universe, *config.context_symbols()]))
    result = download(root, symbols, months(args.start, args.end), os.getenv("MASSIVE_API_KEY", ""),
                      log=lambda text: print(f"{datetime.now():%H:%M:%S} {text}", flush=True))
    print(json.dumps(result))
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
