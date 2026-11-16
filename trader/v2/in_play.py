"""Stocks in play (V2 2.9.0, owner 2026-11-12).

Each morning, the opening `opening_minutes` of one-minute volume of every symbol on the 2.9 scan list is compared with
that symbol's own average for the same minutes over the previous `lookback_sessions` sessions (relative volume). The
`max_symbols` highest at or above `min_relative_volume` (price at least `min_price`) are the only symbols 2.9 trades
that day. Unusual opening volume is how a stock shows it has news, earnings or a big gap.

  baseline   nightly, from the Massive month chunks (trader.v2.history_cache): data/in_play/baseline.json
  scan       at scan_at_et, today's opening bars from tastytrade DXLink: data/in_play/YYYY-MM-DD.json
             (both 2.9 engines use the same file; whichever starts first writes it)

    python -m trader.v2.in_play baseline [--no-download]     # refresh this month's chunks, rebuild the baseline
    python -m trader.v2.in_play replay-scan --date 2026-11-12  # what the scan would have picked, from history
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAY_DIR = "data/in_play"
BASELINE_FILE = "data/in_play/baseline.json"
SIDE_CONFIG = "strategies/v2_versions/professional_v2_2.9.0.json"


def _window(day: date, minutes: int) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(9, 30), ET)
    return start, start + timedelta(minutes=minutes)


def opening_from_rows(rows: Iterable[dict], minutes: int) -> dict[date, dict[str, float]]:
    """{ET date: {"volume", "bars", "last"}} of the first `minutes` regular-session minutes, from Massive/DXLink rows."""
    out: dict[date, dict[str, float]] = {}
    for row in rows:
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(ET)
        start, end = _window(stamp.date(), minutes)
        if not start <= stamp < end:
            continue
        item = out.setdefault(stamp.date(), {"volume": 0.0, "bars": 0, "last": 0.0, "_t": None})
        item["volume"] += float(row.get("volume") or 0)
        item["bars"] += 1
        if item["_t"] is None or stamp >= item["_t"]:
            item["_t"], item["last"] = stamp, float(row["close"])
    for item in out.values():
        item.pop("_t", None)
    return out


def opening_from_bars(bars, minutes: int) -> dict[str, float] | None:
    """The same figure from one session's Bar list (replay)."""
    if not bars:
        return None
    day = bars[0].timestamp.astimezone(ET).date()
    start, end = _window(day, minutes)
    inside = [b for b in bars if start <= b.timestamp.astimezone(ET) < end]
    if not inside:
        return None
    return {"volume": sum(b.volume or 0 for b in inside), "bars": len(inside), "last": inside[-1].close}


def rank(today: dict[str, dict], averages: dict[str, dict], cfg: dict[str, Any]) -> dict[str, list[dict]]:
    """today {symbol: {"volume", "bars", "last"}}, averages {symbol: {"avg_open_volume", "sessions"}} ->
    {"selected": [...], "scored": [...]} ranked by relative volume (highest first)."""
    minutes = int(cfg["opening_minutes"])
    need_sessions = max(3, int(cfg["lookback_sessions"]) * 3 // 4)
    scored = []
    for symbol, now in today.items():
        base = averages.get(symbol) or {}
        avg = float(base.get("avg_open_volume") or 0)
        if avg <= 0 or int(base.get("sessions") or 0) < need_sessions or now["bars"] < minutes * 0.6:
            continue
        scored.append({"symbol": symbol, "relative_volume": round(now["volume"] / avg, 3), "opening_volume": now["volume"],
                       "average_opening_volume": round(avg, 1), "price": now["last"]})
    scored.sort(key=lambda r: (-r["relative_volume"], r["symbol"]))
    eligible = [r for r in scored if r["relative_volume"] >= float(cfg["min_relative_volume"])
                and r["price"] >= float(cfg["min_price"])]
    return {"selected": eligible[:int(cfg["max_symbols"])], "scored": scored[:30]}


# --- baseline (nightly) -------------------------------------------------------------------------------------------
def build_baseline(root: Path, symbols: list[str], for_day: date, cfg: dict[str, Any]) -> dict[str, Any]:
    """Average opening volume over the lookback sessions strictly before for_day, from the month chunks."""
    from .history_cache import iter_rows
    minutes, lookback = int(cfg["opening_minutes"]), int(cfg["lookback_sessions"])
    out: dict[str, Any] = {}
    for symbol in symbols:
        days = opening_from_rows(iter_rows(root, symbol, for_day - timedelta(days=lookback * 2 + 14), for_day - timedelta(days=1)),
                                 minutes)
        recent = [days[d] for d in sorted(days) if d < for_day and days[d]["bars"] >= minutes * 0.6][-lookback:]
        if recent:
            out[symbol] = {"avg_open_volume": sum(r["volume"] for r in recent) / len(recent), "sessions": len(recent),
                           "last_session": max(d for d in days if d < for_day).isoformat()}
    return {"built_at_utc": datetime.now(timezone.utc).isoformat(), "for_sessions_after": (for_day - timedelta(days=1)).isoformat(),
            "opening_minutes": minutes, "lookback_sessions": lookback, "symbols": out}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- live scan ----------------------------------------------------------------------------------------------------
async def _fetch_opening(token: dict[str, Any], symbols: list[str], day: date, minutes: int, batch: int = 35) -> tuple[dict, dict]:
    from .datahub import history_batch
    start, _ = _window(day, minutes)
    today, failures = {}, {}
    for i in range(0, len(symbols), batch):
        part = symbols[i:i + batch]
        got, failed = await history_batch(dxlink_url=token["dxlink-url"], quote_token=token["token"],
                                          series={s: f"{s}{{=1m}}" for s in part}, start=start.astimezone(timezone.utc),
                                          timeout_seconds=60)
        failures.update(failed)
        for symbol, rows in got.items():
            opening = opening_from_rows(rows, minutes).get(day)
            if opening:
                today[symbol] = opening
    return today, failures


def todays_selection(root: Path, app_cfg, v2data: dict[str, Any], *, now: datetime | None = None, log=print) -> dict[str, Any]:
    """Today's in-play list: read it if another 2.9 engine already wrote it, else wait for scan time and scan."""
    from ..config import AppConfig  # noqa: F401  (type only)
    from .config import in_play_universe
    cfg = v2data["in_play"]
    day = (now or datetime.now(timezone.utc)).astimezone(ET).date()
    path = root / DAY_DIR / f"{day.isoformat()}.json"
    existing = load_json(path)
    if existing and existing.get("status") == "ok":
        return existing
    scan_at = datetime.combine(day, time.fromisoformat(cfg["scan_at_et"]), ET) + timedelta(seconds=40)
    wait = (scan_at - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        log(f"in_play: waiting {wait / 60:.1f} min for the {cfg['scan_at_et']} ET scan")
        time_mod.sleep(wait)
    existing = load_json(path)
    if existing and existing.get("status") == "ok":
        return existing
    universe = in_play_universe(root, v2data)
    baseline = load_json(root / BASELINE_FILE) or {}
    record: dict[str, Any] = {"day": day.isoformat(), "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
                              "universe": len(universe), "rules": cfg,
                              "baseline_built_at_utc": baseline.get("built_at_utc"),
                              "baseline_for_sessions_after": baseline.get("for_sessions_after")}
    stale = not baseline.get("for_sessions_after") or \
        (day - date.fromisoformat(baseline["for_sessions_after"])).days > 5
    if stale:
        record.update(status="failed", error="opening-volume baseline missing or older than 5 days "
                                             "(python -m trader.v2.in_play baseline)", selected=[], scored=[])
        write_json(path, record)
        return record
    from ..tastytrade_client import TastytradeClient
    client = TastytradeClient(app_cfg.client_id, app_cfg.client_secret, app_cfg.refresh_token, base_url=app_cfg.production_base_url)
    token = client.quote_token().get("data", {})
    today, failures = asyncio.run(_fetch_opening(token, universe, day, int(cfg["opening_minutes"])))
    ranked = rank(today, baseline.get("symbols") or {}, cfg)
    record.update(status="ok", symbols_with_opening_bars=len(today), fetch_failures=len(failures),
                  fetch_failure_examples=dict(list(failures.items())[:5]), **ranked)
    write_json(path, record)
    return record


# --- CLI ----------------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.v2.in_play")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--no-download", action="store_true")
    b.add_argument("--for-day", help="YYYY-MM-DD (default: the next weekday)")
    r = sub.add_parser("replay-scan")
    r.add_argument("--date", required=True)
    parser.add_argument("--config", default=SIDE_CONFIG)
    args = parser.parse_args(argv)
    root = Path.cwd()
    from .config import _load_json, in_play_universe
    v2data = _load_json(root / args.config)
    cfg = v2data["in_play"]
    universe = in_play_universe(root, v2data)
    if args.command == "baseline":
        today = datetime.now(ET).date()
        if not args.no_download:
            from ..config import load_dotenv
            from .history_cache import download, months
            load_dotenv()
            month = f"{today:%Y-%m}"
            first = today.replace(day=1)
            span = months(f"{first - timedelta(days=40):%Y-%m}", month)
            # this month always (it is incomplete, so always fetched); earlier months only when missing
            result = download(root, universe, span, os.getenv("MASSIVE_API_KEY", ""),
                              log=lambda t: print(f"{datetime.now():%H:%M:%S} {t}", flush=True))
            print(json.dumps(result))
        for_day = date.fromisoformat(args.for_day) if args.for_day else today + timedelta(days=1)
        while for_day.weekday() >= 5:
            for_day += timedelta(days=1)
        baseline = build_baseline(root, universe, for_day, cfg)
        write_json(root / BASELINE_FILE, baseline)
        print(json.dumps({"symbols": len(baseline["symbols"]), "for_sessions_after": baseline["for_sessions_after"],
                          "missing": sorted(set(universe) - set(baseline["symbols"]))}))
        return 0 if baseline["symbols"] else 1
    from .history_cache import iter_rows
    day = date.fromisoformat(args.date)
    baseline = build_baseline(root, universe, day, cfg)
    today = {}
    for symbol in universe:
        opening = opening_from_rows(iter_rows(root, symbol, day, day), int(cfg["opening_minutes"])).get(day)
        if opening:
            today[symbol] = opening
    print(json.dumps(rank(today, baseline["symbols"], cfg), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
