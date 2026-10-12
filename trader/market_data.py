from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from .models import Bar, OptionQuote
from .tastytrade_client import TastytradeClient


def fetch_massive_historical_candles(*, api_key: str, symbol: str, start: datetime, end: datetime,
                                     timeout_seconds: float = 30) -> list[dict]:
    """Fetch one-minute historical stock aggregates from Massive's REST API.

    The API key is only included in the HTTPS request and is never included in
    errors. Pagination URLs are accepted only from Massive's own API host.
    """
    if not api_key:
        raise RuntimeError("Missing MASSIVE_API_KEY. Add it to the ignored local .env file; never paste credentials into chat.")
    if symbol not in {"SPY", "QQQ"}:
        raise ValueError("Massive historical candles are currently enabled only for SPY and QQQ")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("start/end must be timezone-aware and start must precede end")

    base = ("https://api.massive.com/v2/aggs/ticker/" + urllib.parse.quote(symbol, safe="") +
            f"/range/1/minute/{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}")
    query = {"adjusted": "true", "sort": "asc", "limit": "50000", "apiKey": api_key}
    url = base + "?" + urllib.parse.urlencode(query)
    rows: dict[int, dict] = {}
    seen_urls: set[str] = set()
    page_count = 0
    while url:
        page_count += 1
        if page_count > 30:
            raise RuntimeError("Massive returned too many pages for one date range; split the backtest range")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "api.massive.com":
            raise RuntimeError("Massive pagination returned an unexpected host")
        safe_identity = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                                 urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
                                                                         if k.lower() != "apikey"]), ""))
        if safe_identity in seen_urls:
            raise RuntimeError("Massive pagination repeated a page")
        seen_urls.add(safe_identity)
        request = urllib.request.Request(url, headers={"User-Agent": "optio-bot/0.1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Massive candle request failed with HTTP {exc.code}; check the API key, plan access, and request limit") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Massive candle request failed to connect or timed out") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Massive returned a non-JSON candle response") from None
        if payload.get("status") not in {"OK", "DELAYED"}:
            raise RuntimeError("Massive did not return candle data successfully; check plan access and date range")
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError("Massive returned an invalid candle result")
        for item in results:
            try:
                stamp_ms = int(item["t"])
                values = [float(item[name]) for name in ("o", "h", "l", "c", "v")]
            except (KeyError, TypeError, ValueError, OverflowError):
                raise RuntimeError("Massive returned an incomplete candle") from None
            opening, high, low, close, volume = values
            if (not all(math.isfinite(value) for value in values) or opening <= 0 or close <= 0 or
                    high < max(opening, close, low) or low > min(opening, close, high) or volume < 0):
                raise RuntimeError("Massive returned an invalid OHLCV candle")
            stamp = datetime.fromtimestamp(stamp_ms / 1000, timezone.utc)
            if start <= stamp <= end:
                row = {"timestamp": stamp.isoformat(), "open": opening, "high": high, "low": low,
                       "close": close, "volume": volume}
                if item.get("vw") is not None:
                    row["vwap"] = float(item["vw"])
                rows[stamp_ms] = row
        next_url = payload.get("next_url")
        if next_url:
            next_parts = urllib.parse.urlsplit(next_url)
            if next_parts.scheme != "https" or next_parts.hostname != "api.massive.com":
                raise RuntimeError("Massive pagination returned an unexpected host")
            next_query = urllib.parse.parse_qsl(next_parts.query, keep_blank_values=True)
            if not any(key.lower() == "apikey" for key, _ in next_query):
                next_query.append(("apiKey", api_key))
            url = urllib.parse.urlunsplit((next_parts.scheme, next_parts.netloc, next_parts.path,
                                           urllib.parse.urlencode(next_query), ""))
        else:
            url = ""
        # Basic is limited to five API calls per minute. Keep pagination within that limit.
        if url:
            time.sleep(12)
    if not rows:
        raise RuntimeError(f"Massive returned no one-minute candles for {symbol} in the requested date range")
    return [rows[key] for key in sorted(rows)]


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
        query = urllib.parse.urlencode({f"{instrument_type}[]": ",".join(symbols)})
        result = self.client.quotes(query)
        values = result.get("data", {}).get("items", [])
        out = []
        for item in values:
            stamp = datetime.fromisoformat(item["updated-at"].replace("Z", "+00:00"))
            quote = CachedQuote(item["symbol"], float(item.get("bid") or 0), float(item.get("ask") or 0), stamp, item)
            self.cache.update(quote); out.append(quote)
        return out


def fetch_option_quotes(client: TastytradeClient, *, underlying: str, spot: float,
                        now: datetime, min_dte: int, max_dte: int, max_contracts: int = 100) -> list[OptionQuote]:
    """Fetch current quotes and Greeks for near-the-money contracts in the configured DTE range."""
    if underlying not in {"SPY", "QQQ"} or spot <= 0 or min_dte < 1 or max_dte < min_dte:
        raise ValueError("Invalid underlying, spot, or DTE range for option quote request")
    if not 1 <= max_contracts <= 100:
        raise ValueError("Tastytrade market-data requests support 1 to 100 option symbols")
    chain = client.option_chain(underlying).get("data", {}).get("items", [])
    contracts = [item for item in chain if item.get("active") and
                 min_dte <= int(item.get("days-to-expiration", -1)) <= max_dte and
                 abs(float(item.get("strike-price", 0)) - spot) <= spot * 0.05 and item.get("symbol")]
    contracts.sort(key=lambda item: (abs(float(item["strike-price"]) - spot),
                                    int(item.get("days-to-expiration", 999)), item["symbol"]))
    contracts = contracts[:max_contracts]
    if not contracts:
        return []
    by_symbol = {item["symbol"]: item for item in contracts}
    query = urllib.parse.urlencode({"equity-option[]": ",".join(by_symbol)})
    items = client.quotes(query).get("data", {}).get("items", [])
    out: list[OptionQuote] = []
    for item in items:
        contract = by_symbol.get(item.get("symbol"))
        if not contract:
            continue
        try:
            expiration_raw = contract.get("expiration-date") or contract.get("expires-at")
            expiration = datetime.fromisoformat(expiration_raw.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            quote_time_raw = item.get("updated-at")
            quote_time = datetime.fromisoformat(quote_time_raw.replace("Z", "+00:00")) if quote_time_raw else None
            optional = lambda name: float(item[name]) if item.get(name) not in (None, "") else None
            out.append(OptionQuote(
                symbol=item["symbol"], underlying=underlying, expiration=expiration,
                strike=float(contract["strike-price"]), right=contract.get("option-type", "").upper(),
                bid=float(item.get("bid") or 0), ask=float(item.get("ask") or 0),
                bid_size=int(float(item.get("bid-size") or 0)), ask_size=int(float(item.get("ask-size") or 0)),
                delta=optional("delta"), gamma=optional("gamma"), theta=optional("theta"),
                vega=optional("vega"), iv=optional("volatility"), volume=int(float(item.get("volume") or 0)),
                open_interest=int(float(item.get("open-interest") or 0)), quote_timestamp=quote_time))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out


async def dxlink_events(dxlink_url: str, quote_token: str, subscriptions: list[dict], *, channel: int = 3) -> AsyncIterator[dict]:
    """Yield DXLink events; requires optional `websockets` package and instrument streamer symbols."""
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError("Install the streaming extra: pip install -e '.[streaming]'") from exc
    try:
        import truststore
    except ImportError as exc:
        raise RuntimeError("Install the streaming extra: pip install -e '.[streaming]'") from exc
    candle_requested = any(item.get("type") == "Candle" for item in subscriptions)
    event_fields = {
        "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
        "Trade": ["eventType", "eventSymbol", "time", "price", "dayVolume", "size"],
        "Greeks": ["eventType", "eventSymbol", "delta", "gamma", "theta", "vega", "rho", "volatility"],
    }
    if candle_requested:
        event_fields["Candle"] = ["eventType", "eventSymbol", "eventTime", "time", "sequence", "count",
                                  "open", "high", "low", "close", "volume", "eventFlags"]
    # Use the OS trust store while retaining normal certificate and hostname
    # verification for DXLink's TLS connection.
    tls_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with connect(dxlink_url, ping_interval=None, ssl=tls_context) as ws:
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
                await ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": channel,
                                          "reset": True, "add": subscriptions}))
            elif msg.get("type") == "FEED_DATA":
                yield msg
            elif msg.get("type") in {"ERROR", "AUTH_STATE"} and msg.get("state") == "CLOSED":
                raise ConnectionError(f"DXLink closed: {msg}")


async def live_minute_bars(dxlink_url: str, quote_token: str, symbols: list[str]) -> AsyncIterator[tuple[str, Bar]]:
    """Aggregate live DXLink Trade events into completed UTC one-minute OHLCV bars."""
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        raise RuntimeError("Install the streaming extra: pip install -e '.[streaming]'") from exc
    if not symbols or any(symbol not in {"SPY", "QQQ"} for symbol in symbols):
        raise ValueError("Live paper candles are currently limited to SPY and QQQ")
    subscriptions = [{"type": "Trade", "symbol": symbol} for symbol in symbols]
    current: dict[str, dict[str, float | datetime]] = {}
    reconnects = 0
    while True:
        try:
            async for message in dxlink_events(dxlink_url, quote_token, subscriptions):
                reconnects = 0
                payload = message.get("data")
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    if not isinstance(item, list) or not item:
                        continue
                    if item[0] == "Trade":
                        candidates = [item]
                    elif len(item) % 6 == 0:
                        candidates = [item[offset:offset + 6] for offset in range(0, len(item), 6)
                                      if item[offset] == "Trade"]
                    else:
                        candidates = []
                    for trade in candidates:
                        if len(trade) < 6 or str(trade[1]) not in symbols:
                            continue
                        symbol = str(trade[1])
                        try:
                            timestamp = _candle_time(trade[2])
                            price, size = float(trade[3]), float(trade[5])
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size < 0:
                            continue
                        minute = timestamp.replace(second=0, microsecond=0)
                        state = current.get(symbol)
                        if state and minute < state["timestamp"]:
                            continue
                        if state and minute > state["timestamp"]:
                            yield symbol, Bar(state["timestamp"], state["open"], state["high"], state["low"],
                                              state["close"], state["volume"])
                            state = None
                        if state is None:
                            current[symbol] = {"timestamp": minute, "open": price, "high": price, "low": price,
                                               "close": price, "volume": size}
                        else:
                            state["high"] = max(state["high"], price)
                            state["low"] = min(state["low"], price)
                            state["close"] = price
                            state["volume"] += size
        except ConnectionClosed:
            reconnects += 1
            if reconnects > 8:
                raise ConnectionError("DXLink repeatedly closed the live Trade feed") from None
            await asyncio.sleep(min(reconnects, 5))


def parse_candle_feed(messages: list[dict], *, start: datetime, end: datetime, symbol: str) -> list[dict]:
    """Extract 1-minute Candle rows from DXLink COMPACT messages; reject truncated snapshots."""
    rows: dict[datetime, dict] = {}
    snapshot_complete = False
    requested_symbol = f"{symbol}{{=1m,a=s,tho=true}}"
    accepted_symbols = {requested_symbol, f"{symbol}{{=m,a=s,tho=true}}"}
    for message in messages:
        payload = message.get("data")
        if not isinstance(payload, list):
            continue
        for row in _compact_candle_rows(payload):
            event_symbol = str(row[1])
            if event_symbol not in accepted_symbols:
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
    # DXLink's time-series subscription uses epoch milliseconds for fromTime.
    subscription = [{"type": "Candle", "symbol": streamer_symbol, "fromTime": int(start.timestamp() * 1000)}]
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
    return isinstance(data, list) and any(_candle_flags(row[11]) & (8 | 16) for row in _compact_candle_rows(data))


def _compact_candle_rows(payload: list[Any]) -> list[list[Any]]:
    """Unpack DXLink COMPACT Candle arrays into their 12 negotiated fields."""
    rows: list[list[Any]] = []
    for item in payload:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "Candle":
            if len(item) >= 12:
                rows.append(item[:12])
            continue
        if len(item) % 12 == 0:
            rows.extend(item[offset:offset + 12] for offset in range(0, len(item), 12)
                        if item[offset] == "Candle")
    return rows


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
