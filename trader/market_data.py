from __future__ import annotations

import asyncio
import json
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
                                          "acceptDataFormat": "COMPACT", "acceptEventFields": {
                                              "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
                                              "Trade": ["eventType", "eventSymbol", "price", "dayVolume", "size"],
                                              "Greeks": ["eventType", "eventSymbol", "delta", "gamma", "theta", "vega", "rho", "volatility"]}}))
                await ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": channel, "add": subscriptions}))
            elif msg.get("type") == "FEED_DATA":
                yield msg
            elif msg.get("type") in {"ERROR", "AUTH_STATE"} and msg.get("state") == "CLOSED":
                raise ConnectionError(f"DXLink closed: {msg}")
