"""Shared, cached, logged market-data access for V2. tastytrade is the only provider.

* SharedCache       JSON files under data/v2_shared_cache, keyed by name, each stored with its retrieval time and
                    source. Both PAPER engines read the same files, so slow-changing data (daily candles, premarket
                    range, market metrics, option-chain listings, VIX) is requested once and reused.
* history_batch     one DXLink HISTORY connection for many candle series at once, with the same completeness rules
                    as the engine's own history (SNAPSHOT_BEGIN/END, SNIP refused); incomplete series are reported
                    missing, never filled in.
* RequestLog        records every tastytrade REST request the process makes (path without credentials, status,
                    duration, attempt) via the client's request observer.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..logging_utils import append_jsonl, atomic_replace
from ..market_data import _candle_flags, _candle_time, _compact_candle_rows, dxlink_events

ET = ZoneInfo("America/New_York")


class SharedCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def get(self, key: str, max_age_seconds: float | None = None) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            stored = datetime.fromisoformat(item["stored_at_utc"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if max_age_seconds is not None and (datetime.now(timezone.utc) - stored).total_seconds() > max_age_seconds:
            return None
        return item

    def put(self, key: str, payload: Any, *, source: str) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        item = {"stored_at_utc": datetime.now(timezone.utc).isoformat(), "source": source, "payload": payload}
        path = self._path(key)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(item, default=str), encoding="utf-8")
        atomic_replace(temporary, path)
        return item


class RequestLog:
    """Observer for TastytradeClient requests. Query strings are kept (they name symbols, never credentials)."""

    def __init__(self, path: Path):
        self.path = path
        self.counts: dict[str, int] = {}

    def __call__(self, method: str, path: str, status: int | str, seconds: float, attempt: int) -> None:
        # Never log a full account identifier: keep only its last 2 characters.
        path = re.sub(r"/accounts/([^/?]+)", lambda m: "/accounts/***" + m.group(1)[-2:], path)
        endpoint = path.split("?", 1)[0]
        endpoint = re.sub(r"/(option-chains|instruments/equity-options)/[^/?]+", r"/\1/{symbol}", endpoint)
        self.counts[endpoint] = self.counts.get(endpoint, 0) + 1
        try:
            append_jsonl(self.path, {"method": method, "endpoint": endpoint, "path": path[:300], "status": status,
                                     "seconds": round(seconds, 3), "attempt": attempt})
        except OSError:
            pass


def _base(event_symbol: str) -> str:
    return event_symbol.split("{", 1)[0]


async def history_batch(*, dxlink_url: str, quote_token: str, series: dict[str, str], start: datetime,
                        timeout_seconds: float = 90.0, open_feed: Callable | None = None) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Fetch complete candle snapshots for {base_symbol: streamer_candle_symbol} from `start`.

    Returns ({base_symbol: rows oldest-first}, {base_symbol: failure reason}). A series is accepted only after its
    snapshot completes; a SNIP (truncated) snapshot or a timeout leaves it in the failures."""
    feed_factory = open_feed or dxlink_events
    start_ms = int(start.timestamp() * 1000)
    subscriptions = [{"type": "Candle", "symbol": candle, "fromTime": start_ms} for candle in series.values()]
    rows: dict[str, dict[datetime, dict]] = {symbol: {} for symbol in series}
    state = {symbol: {"started": False, "end_waiting": False, "done": False, "error": None} for symbol in series}
    feed = feed_factory(dxlink_url, quote_token, subscriptions, contract="HISTORY")
    try:
        async with asyncio.timeout(timeout_seconds):
            async for message in feed:
                data = message.get("data")
                for row in _compact_candle_rows(data if isinstance(data, list) else []):
                    symbol = _base(str(row[1]))
                    if symbol not in state or state[symbol]["done"]:
                        continue
                    track = state[symbol]
                    stamp = _candle_time(row[3])
                    flags = _candle_flags(row[11])
                    if flags & 16:
                        track.update(done=True, error="snapshot truncated (SNAPSHOT_SNIP)")
                        continue
                    if flags & 4:
                        track["started"] = True
                    if not track["started"]:
                        continue
                    if not flags & 2 and stamp >= start:
                        values = [row[index] for index in range(6, 11)]
                        try:
                            o, h, l, c = (float(v) for v in values[:4])
                            volume = float(values[4]) if values[4] not in (None, "NaN") else None
                        except (TypeError, ValueError):
                            o = h = l = c = float("nan"); volume = None
                        if all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) and h >= max(o, c, l) and l <= min(o, c, h):
                            rows[symbol][stamp] = {"timestamp": stamp.isoformat(), "open": o, "high": h, "low": l,
                                                   "close": c, "volume": volume}
                    if flags & 8:
                        track["end_waiting"] = True
                        if not flags & 1:
                            track["done"] = True
                    elif track["end_waiting"] and not flags & 1:
                        track["done"] = True
                    elif stamp < start and not flags & 1:
                        track["done"] = True
                if all(item["done"] for item in state.values()):
                    break
    except TimeoutError:
        pass
    finally:
        await feed.aclose()
    out, failures = {}, {}
    for symbol, track in state.items():
        if track["error"]:
            failures[symbol] = track["error"]
        elif not track["done"]:
            failures[symbol] = "history snapshot did not complete before the timeout"
        elif not rows[symbol]:
            failures[symbol] = "no candles returned"
        else:
            out[symbol] = [rows[symbol][stamp] for stamp in sorted(rows[symbol])]
    return out, failures


def daily_session_date(timestamp: str) -> date:
    """DXLink stamps a daily candle at 00:00 UTC of its own session date (verified 2026-10-23: the candle stamped
    2026-10-22T00:00Z has 10/22's open 766.37 / high 767.71 / close 763.96). Converting that stamp to ET would label
    it the previous day and make today's partial session look like "yesterday"."""
    return datetime.fromisoformat(timestamp).astimezone(timezone.utc).date()


def completed_daily(rows: list[dict], today: date) -> list[dict]:
    """Daily candles for completed sessions before `today`; today's partial session is excluded."""
    out = []
    for row in rows:
        day = daily_session_date(row["timestamp"])
        if day < today:
            out.append({**row, "session_date": day.isoformat()})
    return out


class DataHub:
    """Process-wide V2 data access. All network access goes through the tastytrade client or DXLink."""

    def __init__(self, *, client, shared_dir: Path, request_log: RequestLog | None = None, clock=None):
        self.client = client
        self.cache = SharedCache(shared_dir)
        self.request_log = request_log
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._chains: dict[str, list[dict]] = {}

    # --- daily history (prior-session levels, correlation) ------------------------------------------
    async def daily_history(self, symbols: list[str], *, dxlink_url: str, quote_token: str, sessions: int,
                            today: date) -> tuple[dict[str, list[dict]], dict[str, str]]:
        key = f"daily_v2_{today.isoformat()}"   # v2: session-date fix (files keyed daily_<date> predate it)
        cached = self.cache.get(key)
        have: dict[str, list[dict]] = dict(cached["payload"]["rows"]) if cached else {}
        missing = [s for s in symbols if s not in have]
        failures: dict[str, str] = {}
        if missing:
            start = datetime.combine(today - timedelta(days=int(sessions * 1.6) + 10), datetime.min.time(), ET)
            for i in range(0, len(missing), 40):
                chunk = missing[i:i + 40]
                fetched, failed = await history_batch(dxlink_url=dxlink_url, quote_token=quote_token,
                                                      series={s: f"{s}{{=1d}}" for s in chunk}, start=start)
                for symbol, rows in fetched.items():
                    have[symbol] = completed_daily(rows, today)[-(sessions + 1):]
                failures.update(failed)
            self.cache.put(key, {"rows": have, "sessions": sessions}, source="tastytrade DXLink daily candles {=1d}")
        return {s: have[s] for s in symbols if s in have}, failures

    # --- premarket range ---------------------------------------------------------------------------
    async def premarket(self, symbols: list[str], *, dxlink_url: str, quote_token: str, day: date,
                        start_et: str) -> tuple[dict[str, dict], dict[str, str]]:
        key = f"premarket_{day.isoformat()}"
        cached = self.cache.get(key)
        if cached and all(s in cached["payload"] for s in symbols):
            return {s: cached["payload"][s] for s in symbols}, {}
        hour, minute = (int(x) for x in start_et.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
        regular_open = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
        fetched, failures = await history_batch(dxlink_url=dxlink_url, quote_token=quote_token,
                                                series={s: f"{s}{{=1m,tho=false}}" for s in symbols}, start=start)
        out = dict(cached["payload"]) if cached else {}
        for symbol, rows in fetched.items():
            pre = [r for r in rows if start <= datetime.fromisoformat(r["timestamp"]) < regular_open]
            out[symbol] = {"high": max(r["high"] for r in pre) if pre else None,
                           "low": min(r["low"] for r in pre) if pre else None, "bars": len(pre),
                           "window_et": f"{start_et}-09:30", "source": "tastytrade DXLink 1m candles, extended hours"}
        self.cache.put(key, out, source="tastytrade DXLink 1m extended-hours candles")
        return {s: out[s] for s in symbols if s in out}, failures

    # --- VIX ------------------------------------------------------------------------------------------
    def vix(self, symbol: str = "VIX", max_age_seconds: float = 55) -> dict[str, Any] | None:
        """Latest VIX value from a REST index quote, shared by both engines for up to max_age_seconds."""
        cached = self.cache.get(f"vix_{symbol}", max_age_seconds)
        if cached:
            return cached["payload"]
        items = self.client.quotes(urlencode({"index[]": symbol})).get("data", {}).get("items", [])
        item = next((i for i in items if i.get("symbol") == symbol), None)
        if not item or item.get("last") in (None, ""):
            return None
        payload = {"value": float(item["last"]), "quote_updated_at": item.get("updated-at"),
                   "prev_close": float(item["prev-close"]) if item.get("prev-close") not in (None, "") else None,
                   "source": "tastytrade REST /market-data/by-type index quote (last)"}
        self.cache.put(f"vix_{symbol}", payload, source=payload["source"])
        return payload

    # --- market metrics (IV rank, HV, earnings) ----------------------------------------------------------
    def market_metrics(self, symbols: list[str], max_age_seconds: float) -> dict[str, dict]:
        key = "market_metrics"
        cached = self.cache.get(key)
        have = dict(cached["payload"]) if cached else {}
        now = self.now()
        stale = [s for s in symbols if s not in have or
                 (now - datetime.fromisoformat(have[s]["_retrieved_at_utc"])).total_seconds() > max_age_seconds]
        if stale:
            retrieved = now.isoformat()
            for i in range(0, len(stale), 50):
                items = self.client.market_metrics(stale[i:i + 50]).get("data", {}).get("items", [])
                for item in items:
                    have[item["symbol"]] = {**item, "_retrieved_at_utc": retrieved}
            self.cache.put(key, have, source="tastytrade REST /market-metrics")
        return {s: have[s] for s in symbols if s in have}

    # --- option-chain listings (instrument metadata; quotes are always fetched fresh) ---------------------
    def option_chain(self, underlying: str, day: date) -> list[dict]:
        if underlying in self._chains:
            return self._chains[underlying]
        key = f"chain_{underlying}_{day.isoformat()}"
        cached = self.cache.get(key)
        if cached:
            items = cached["payload"]
        else:
            raw = self.client.option_chain(underlying).get("data", {}).get("items", [])
            fields = ("symbol", "active", "days-to-expiration", "strike-price", "option-type", "expiration-date",
                      "expires-at", "streamer-symbol")
            items = [{k: item.get(k) for k in fields} for item in raw]
            self.cache.put(key, items, source="tastytrade REST /option-chains (instrument listing)")
        self._chains[underlying] = items
        return items


def throttle_seconds(last: float | None, interval: float) -> float:
    return 0.0 if last is None else max(0.0, interval - (time.monotonic() - last))
