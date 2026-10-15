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
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from .models import Bar, OptionQuote, require_ticker
from .tastytrade_client import TastytradeClient

# DXLink drops a feed that sends no KEEPALIVE for 60 seconds (keepaliveTimeout in SETUP).
DXLINK_KEEPALIVE_SECONDS = 25.0


def fetch_massive_historical_candles(*, api_key: str, symbol: str, start: datetime, end: datetime,
                                     timeout_seconds: float = 30) -> list[dict]:
    """Fetch one-minute historical stock aggregates from Massive's REST API.

    The API key is only included in the HTTPS request and is never included in
    errors. Pagination URLs are accepted only from Massive's own API host.
    """
    if not api_key:
        raise RuntimeError("Missing MASSIVE_API_KEY. Add it to the ignored local .env file; never paste credentials into chat.")
    require_ticker(symbol)
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
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            provider_status = str(error_payload.get("status") or "HTTP_ERROR")
            provider_message = str(error_payload.get("message") or "No provider message returned")
            provider_message = provider_message.replace(api_key, "<redacted>")
            raise RuntimeError(
                f"Massive candle request failed with HTTP {exc.code} ({provider_status}): "
                f"{provider_message}"
            ) from None
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
    require_ticker(underlying)
    if spot <= 0 or min_dte < 1 or max_dte < min_dte:
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


def _redact_dxlink_message(value: Any) -> Any:
    """Keep server diagnostics while ensuring credential-like fields are never logged."""
    sensitive = {"token", "authorization", "password", "access_token", "refresh_token", "client_secret"}
    if isinstance(value, dict):
        return {key: ("<redacted>" if str(key).lower().replace("-", "_") in sensitive
                      else _redact_dxlink_message(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_dxlink_message(item) for item in value]
    return value


async def dxlink_events(dxlink_url: str, quote_token: str, subscriptions: list[dict], *,
                        channel: int = 3, contract: str = "AUTO") -> AsyncIterator[dict]:
    """Yield DXLink events; requires optional `websockets` package and instrument streamer symbols."""
    if contract not in {"AUTO", "TICKER", "STREAM", "HISTORY"}:
        raise ValueError("Unsupported DXLink delivery contract")
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
    keepalive_interval = DXLINK_KEEPALIVE_SECONDS
    async with connect(dxlink_url, ping_interval=None, ssl=tls_context) as ws:
        # The socket is read and kept alive by background tasks, so DXLink still sees KEEPALIVEs
        # while the consumer of this generator is busy (history recovery, REST calls, decisions).
        # Previously both only ran while the consumer was waiting on the next event, and the
        # server dropped the feed after 60 seconds without a KEEPALIVE.
        events: asyncio.Queue[dict | BaseException] = asyncio.Queue()
        phase = "CONNECTED_WAITING_FOR_SETUP"

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(keepalive_interval)
                await ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

        async def reader() -> None:
            nonlocal phase
            configured = False
            subscribed = False
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "AUTH_STATE" and msg.get("state") == "UNAUTHORIZED":
                        await ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": quote_token}))
                        phase = "AUTH_SENT_WAITING_FOR_AUTHORIZATION"
                    elif msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                        await ws.send(json.dumps({"type": "CHANNEL_REQUEST", "channel": channel, "service": "FEED",
                                                  "parameters": {"contract": contract}}))
                        phase = "CHANNEL_REQUEST_SENT_WAITING_FOR_CHANNEL_OPENED"
                    elif msg.get("type") == "CHANNEL_OPENED" and msg.get("channel") == channel:
                        await ws.send(json.dumps({"type": "FEED_SETUP", "channel": channel, "acceptAggregationPeriod": 0.1,
                                                  "acceptDataFormat": "COMPACT", "acceptEventFields": event_fields}))
                        phase = "FEED_SETUP_SENT_WAITING_FOR_FEED_CONFIG"
                    elif msg.get("type") == "FEED_CONFIG" and msg.get("channel") == channel:
                        configured = True
                        if not subscribed:
                            await ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": channel,
                                                      "reset": True, "add": subscriptions}))
                            subscribed = True
                            phase = "FEED_SUBSCRIPTION_SENT_WAITING_FOR_FEED_DATA"
                    elif msg.get("type") == "FEED_DATA":
                        if not configured or not subscribed:
                            raise RuntimeError("DXLink sent feed data before FEED_CONFIG/subscription handshake completed")
                        phase = "FEED_ACTIVE"
                        events.put_nowait(msg)
                    elif msg.get("type") == "ERROR":
                        response = json.dumps(_redact_dxlink_message(msg), sort_keys=True, default=str)
                        requested_symbols = [str(item.get("symbol", "unknown")) for item in subscriptions]
                        raise ConnectionError(
                            "DXLink rejected the feed request: "
                            f"phase={phase}; response={response}; requested_channel={channel}; "
                            f"contract={contract}; symbols={requested_symbols}"
                        )
                    elif msg.get("type") == "AUTH_STATE" and msg.get("state") == "CLOSED":
                        raise ConnectionError("DXLink feed connection closed")
                raise ConnectionError("DXLink feed connection closed")
            except Exception as exc:
                events.put_nowait(exc)

        await ws.send(json.dumps({"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                                  "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}))
        phase = "SETUP_SENT_WAITING_FOR_AUTH_STATE"
        tasks = [asyncio.create_task(reader()), asyncio.create_task(keepalive())]
        try:
            while True:
                item = await events.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def live_minute_bars(dxlink_url: str, quote_token: str, symbols: list[str]) -> AsyncIterator[tuple[str, Bar]]:
    """Aggregate live DXLink Trade events into completed UTC one-minute OHLCV bars."""
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        raise RuntimeError("Install the streaming extra: pip install -e '.[streaming]'") from exc
    if not symbols:
        raise ValueError("At least one symbol is required for live paper candles")
    for symbol in symbols:
        require_ticker(symbol)
    subscriptions = [{"type": "Trade", "symbol": symbol} for symbol in symbols]
    current: dict[str, dict[str, float | datetime]] = {}
    reconnects = 0
    while True:
        # A minute that began before this connection opened is incomplete (we joined mid-minute, or
        # the event is the last-trade snapshot). It is never emitted; the caller recovers that minute
        # from complete history as a gap instead. Minutes starting after the connect are complete,
        # so a pre-open start still emits the 09:30 bar.
        connected_at = datetime.now(timezone.utc)
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
                            if not state.get("partial"):
                                yield symbol, Bar(state["timestamp"], state["open"], state["high"], state["low"],
                                                  state["close"], state["volume"])
                            state = None
                        if state is None:
                            current[symbol] = {"timestamp": minute, "open": price, "high": price, "low": price,
                                               "close": price, "volume": size,
                                               "partial": minute < connected_at}
                        else:
                            state["high"] = max(state["high"], price)
                            state["low"] = min(state["low"], price)
                            state["close"] = price
                            state["volume"] += size
        except (ConnectionClosed, ConnectionError, OSError) as exc:
            # Server-side drops (e.g. a KEEPALIVE timeout ERROR) arrive as ConnectionError, not
            # ConnectionClosed. Reconnect for both. Discard the partially built minute: the caller
            # recovers skipped minutes from complete history instead of using a partial bar.
            reconnects += 1
            if reconnects > 8:
                raise ConnectionError(f"DXLink repeatedly dropped the live Trade feed: {exc}") from exc
            current.clear()
            await asyncio.sleep(min(reconnects, 5))


def parse_candle_feed(messages: list[dict], *, start: datetime, end: datetime, symbol: str,
                      audit: dict[str, Any] | None = None) -> list[dict]:
    """Extract a completed DXLink time-series snapshot; never accept a partial replay."""
    rows: dict[datetime, dict] = {}
    snapshot_complete = False
    snapshot_started = False
    snapshot_end_waiting = False
    previous_time: datetime | None = None
    raw_candles = duplicates = out_of_order = 0
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
            raw_candles += 1
            stamp = _candle_time(row[3])
            flags = _candle_flags(row[11])
            if flags & 16:
                if audit is not None:
                    audit.update({"raw_candle_events": raw_candles, "snapshot_snip": True,
                                  "duplicate_candle_count": duplicates, "out_of_order_count": out_of_order})
                raise RuntimeError("DXLink candle history was truncated (SNAPSHOT_SNIP); refusing incomplete history")
            if flags & 4:
                snapshot_started = True
            if not snapshot_started:
                continue
            if previous_time is not None and stamp > previous_time:
                out_of_order += 1
                if audit is not None:
                    audit.update({"raw_candle_events": raw_candles, "snapshot_begin": True,
                                  "duplicate_candle_count": duplicates, "out_of_order_count": out_of_order})
                raise RuntimeError(f"DXLink candle snapshot is out of order at {stamp.isoformat()}")
            previous_time = stamp
            below_from_time = stamp < start and not flags & 1
            if not flags & 2 and start <= stamp <= end:
                values = [float(row[index]) for index in range(6, 11)]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(f"DXLink returned non-finite OHLCV for {symbol} at {stamp.isoformat()}")
                opening, high, low, close, volume = values
                if opening <= 0 or close <= 0 or high < max(opening, close, low) or low > min(opening, close, high) or volume < 0:
                    raise ValueError(f"DXLink returned invalid OHLCV bounds for {symbol} at {stamp.isoformat()}")
                item = {"timestamp": stamp.isoformat(), "open": opening, "high": high, "low": low,
                        "close": close, "volume": volume}
                existing = rows.get(stamp)
                if existing is not None:
                    duplicates += 1
                    if existing != item:
                        raise RuntimeError(f"DXLink returned conflicting duplicate candle at {stamp.isoformat()}")
                else:
                    rows[stamp] = item
            if flags & 8:
                snapshot_end_waiting = True
                if not flags & 1:
                    snapshot_complete = True
                    break
            elif snapshot_end_waiting and not flags & 1:
                snapshot_complete = True
                break
            elif below_from_time:
                # dxFeed permits an event older than fromTime to delimit a time-series snapshot.
                snapshot_complete = True
                break
    if not snapshot_complete:
        if audit is not None:
            audit.update({"raw_candle_events": raw_candles, "snapshot_begin": snapshot_started,
                          "snapshot_end": False, "snapshot_snip": False,
                          "duplicate_candle_count": duplicates, "out_of_order_count": out_of_order,
                          "received_candle_count": len(rows)})
        raise RuntimeError("DXLink historical snapshot did not complete; refusing partial candle history")
    if not rows:
        raise RuntimeError(f"DXLink returned no candles for {symbol} in the requested date range")
    if audit is not None:
        audit.update({"raw_candle_events": raw_candles, "snapshot_begin": snapshot_started,
                      "snapshot_end": True, "snapshot_snip": False,
                      "duplicate_candle_count": duplicates, "out_of_order_count": out_of_order,
                      "received_candle_count": len(rows)})
    return [rows[key] for key in sorted(rows)]


async def fetch_historical_candles(*, dxlink_url: str, quote_token: str, symbol: str,
                                   start: datetime, end: datetime, timeout_seconds: float = 60,
                                   audit: dict[str, Any] | None = None) -> list[dict]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("start/end must be timezone-aware and start must precede end")
    streamer_symbol = f"{symbol}{{=1m,a=s,tho=true}}"
    # DXLink's time-series subscription uses epoch milliseconds for fromTime.
    subscription = [{"type": "Candle", "symbol": streamer_symbol, "fromTime": int(start.timestamp() * 1000)}]
    if audit is not None:
        audit.update({"provider": "tastytrade_dxlink", "delivery_contract": "HISTORY",
                      "symbol": symbol, "from_time_epoch_ms": subscription[0]["fromTime"],
                      "requested_start_utc": start.astimezone(timezone.utc).isoformat(),
                      "requested_end_utc": end.astimezone(timezone.utc).isoformat()})
    messages = []
    feed = dxlink_events(dxlink_url, quote_token, subscription, contract="HISTORY")
    snapshot_state = {"started": False, "end_waiting": False}
    try:
        async with asyncio.timeout(timeout_seconds):
            async for message in feed:
                messages.append(message)
                if _has_candle_snapshot_end(message, start=start, symbol=symbol, state=snapshot_state):
                    break
    except TimeoutError as exc:
        if audit is not None:
            stamps = []
            raw_count = 0
            observed_flags: list[int] = []
            for message in messages:
                for row in _compact_candle_rows(message.get("data", [])):
                    if str(row[1]) not in {f"{symbol}{{=1m,a=s,tho=true}}", f"{symbol}{{=m,a=s,tho=true}}"}:
                        continue
                    stamp = _candle_time(row[3]).astimezone(timezone.utc)
                    if start.astimezone(timezone.utc) <= stamp <= end.astimezone(timezone.utc):
                        raw_count += 1
                        stamps.append(stamp)
                        observed_flags.append(_candle_flags(row[11]))
            unique = set(stamps)
            minute_start = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
            minute_end = end.astimezone(timezone.utc).replace(second=0, microsecond=0)
            expected_count = max(0, int((minute_end - minute_start).total_seconds() // 60) + 1)
            expected_timestamps = {minute_start + timedelta(minutes=index) for index in range(expected_count)}
            missing_timestamps = sorted(expected_timestamps - unique)
            audit.update({"snapshot_begin": snapshot_state["started"], "snapshot_end": False,
                          "raw_candle_events": raw_count, "expected_candle_count": expected_count,
                          "received_candle_count": len(unique),
                          "missing_candle_count": max(0, expected_count - len(unique)),
                          "missing_timestamps_utc": [stamp.isoformat() for stamp in missing_timestamps],
                          "first_missing_timestamp_utc": missing_timestamps[0].isoformat() if missing_timestamps else None,
                          "received_timestamps_utc": [stamp.isoformat() for stamp in sorted(unique)],
                          "observed_snapshot_flags": sorted(set(observed_flags)),
                          "duplicate_candle_count": max(0, raw_count - len(unique)),
                          "out_of_order_count": sum(1 for left, right in zip(stamps, stamps[1:]) if right > left),
                          "timeout_seconds": timeout_seconds})
        raise TimeoutError("Timed out waiting for DXLink historical candle snapshot completion") from exc
    finally:
        # Close the history connection now rather than whenever the generator is garbage collected.
        await feed.aclose()
    return parse_candle_feed(messages, start=start, end=end, symbol=symbol, audit=audit)


def _has_candle_snapshot_end(message: dict, *, start: datetime, symbol: str,
                              state: dict[str, bool]) -> bool:
    data = message.get("data")
    if not isinstance(data, list):
        return False
    for row in _compact_candle_rows(data):
        if str(row[1]) not in {f"{symbol}{{=1m,a=s,tho=true}}", f"{symbol}{{=m,a=s,tho=true}}"}:
            continue
        stamp = _candle_time(row[3])
        flags = _candle_flags(row[11])
        if flags & 4:
            state["started"] = True
        if flags & 16:
            return True
        if flags & 8:
            state["end_waiting"] = True
            if not flags & 1:
                return True
        elif state["end_waiting"] and not flags & 1:
            return True
        elif state["started"] and stamp < start.astimezone(timezone.utc) and not flags & 1:
            return True
    return False


def _compact_candle_rows(payload: list[Any]) -> list[list[Any]]:
    """Unpack every Candle in DXLink COMPACT rows and batched positional lists."""
    rows: list[list[Any]] = []
    for item in payload:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "Candle":
            if len(item) % 12:
                raise ValueError(f"Malformed DXLink COMPACT Candle batch length: {len(item)} fields")
            rows.extend(item[offset:offset + 12] for offset in range(0, len(item), 12)
                        if item[offset] == "Candle")
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
