from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from .tastytrade_client import TastytradeClient


@dataclass(frozen=True)
class CachedQuote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    raw: dict[str, Any]

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


class MarketDataCache:
    def __init__(self): self._quotes: dict[str, CachedQuote] = {}
    def update(self, quote: CachedQuote) -> None: self._quotes[quote.symbol] = quote
    def get(self, symbol: str, max_age_seconds: float = 10) -> CachedQuote | None:
        quote = self._quotes.get(symbol)
        return quote if quote and 0 <= quote.age_seconds <= max_age_seconds else None


class ProductionMarketData:
    """Production REST snapshots, with quote freshness checks. Continuous feeds use DXLink below."""
    def __init__(self, client: TastytradeClient, cache: MarketDataCache | None = None):
        self.client, self.cache = client, cache or MarketDataCache()

    def snapshots(self, symbols: list[str], instrument_type: str = "equity") -> list[CachedQuote]:
        if not symbols or len(symbols) > 100:
            raise ValueError("Quote request must contain 1 to 100 symbols")
        query = instrument_type + "=" + ",".join(symbols)
        result = self.client.quotes(query)
        values = result.get("data", {}).get("items", [])
        out = []
        for item in values:
            stamp = datetime.fromisoformat(item["updated-at"].replace("Z", "+00:00"))
            quote = CachedQuote(item["symbol"], float(item.get("bid") or 0), float(item.get("ask") or 0), stamp, item)
            self.cache.update(quote); out.append(quote)
        return out


async def dxlink_events(dxlink_url: str, quote_token: str, subscriptions: list[dict], *, channel: int = 3) -> AsyncIterator[dict]:
    """Yield DXLink events; requires optional `websockets` package and instrument streamer symbols."""
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError("Install the streaming extra: pip install -e '.[streaming]'") from exc
    candle_requested = any(item.get("type") == "Candle" for item in subscriptions)
    event_fields = {
        "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
        "Trade": ["eventType", "eventSymbol", "price", "dayVolume", "size"],
        "Greeks": ["eventType", "eventSymbol", "delta", "gamma", "theta", "vega", "rho", "volatility"],
    }
    if candle_requested:
        event_fields["Candle"] = ["eventType", "eventSymbol", "eventTime", "time", "sequence", "count",
                                  "open", "high", "low", "close", "volume", "eventFlags"]
    async with connect(dxlink_url, ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                                  "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except TimeoutError:
                await ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                continue
            msg = json.loads(raw)
            if msg.get("type") == "AUTH_STATE" and msg.get("state") == "UNAUTHORIZED":
                await ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": quote_token}))
            elif msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                await ws.send(json.dumps({"type": "CHANNEL_REQUEST", "channel": channel, "service": "FEED", "parameters": {"contract": "AUTO"}}))
            elif msg.get("type") == "CHANNEL_OPENED" and msg.get("channel") == channel:
                await ws.send(json.dumps({"type": "FEED_SETUP", "channel": channel, "acceptAggregationPeriod": 0.1,
                                          "acceptDataFormat": "COMPACT", "acceptEventFields": event_fields}))
                await ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": channel, "add": subscriptions}))
            elif msg.get("type") == "FEED_DATA":
                yield msg
            elif msg.get("type") in {"ERROR", "AUTH_STATE"} and msg.get("state") == "CLOSED":
                raise ConnectionError(f"DXLink closed: {msg}")


def parse_candle_feed(messages: list[dict], *, start: datetime, end: datetime, symbol: str) -> list[dict]:
    """Extract 1-minute Candle rows from DXLink COMPACT messages; reject truncated snapshots."""
    rows: dict[datetime, dict] = {}
    snapshot_complete = False
    requested_symbol = f"{symbol}{{=1m,a=s,tho=true}}"
    for message in messages:
        payload = message.get("data")
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, list) or len(row) < 12 or row[0] != "Candle":
                continue
            event_symbol = str(row[1])
            if event_symbol not in {requested_symbol, f"{symbol}{{=1m,tho=true,a=s}}"}:
                continue
            stamp = _candle_time(row[3])
            flags = _candle_flags(row[11])
            if flags & 16:
                raise RuntimeError("DXLink candle history was truncated (SNAPSHOT_SNIP); refusing an incomplete backtest")
            if start <= stamp <= end:
                values = [float(row[index]) for index in range(6, 11)]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(f"DXLink returned non-finite OHLCV for {symbol} at {stamp.isoformat()}")
                opening, high, low, close, volume = values
                if high < max(opening, close, low) or low > min(opening, close, high) or volume < 0:
                    raise ValueError(f"DXLink returned invalid OHLCV bounds for {symbol} at {stamp.isoformat()}")
                rows[stamp] = {"timestamp": stamp.isoformat(), "open": opening, "high": high, "low": low,
                               "close": close, "volume": volume}
            if flags & 8:
                snapshot_complete = True
    if not snapshot_complete:
        raise RuntimeError("DXLink did not mark the historical candle snapshot complete")
    if not rows:
        raise RuntimeError(f"DXLink returned no candles for {symbol} in the requested date range")
    return [rows[key] for key in sorted(rows)]


async def fetch_historical_candles(*, dxlink_url: str, quote_token: str, symbol: str,
                                   start: datetime, end: datetime, timeout_seconds: float = 180) -> list[dict]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("start/end must be timezone-aware and start must precede end")
    streamer_symbol = f"{symbol}{{=1m,a=s,tho=true}}"
    subscription = [{"type": "Candle", "symbol": streamer_symbol, "fromTime": int(start.timestamp())}]
    messages = []
    feed = dxlink_events(dxlink_url, quote_token, subscription)
    try:
        async with asyncio.timeout(timeout_seconds):
            async for message in feed:
                messages.append(message)
                if _has_candle_snapshot_end(message):
                    break
    except TimeoutError as exc:
        raise TimeoutError("Timed out waiting for DXLink historical candle snapshot") from exc
    return parse_candle_feed(messages, start=start, end=end, symbol=symbol)


def _has_candle_snapshot_end(message: dict) -> bool:
    data = message.get("data")
    return isinstance(data, list) and any(isinstance(row, list) and len(row) >= 12 and row[0] == "Candle" and _candle_flags(row[11]) & (8 | 16) for row in data)


def _candle_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"Unsupported candle timestamp: {value!r}")


def _candle_flags(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            flags = value.upper()
            result = 0
            if "SNAPSHOT_END" in flags: result |= 8
            if "SNAPSHOT_SNIP" in flags: result |= 16
            return result
        return 0
