"""Provider-neutral, auditable access to normalized one-minute candle history.

Provider selection is explicit. This module never silently fails over between
vendors because their session rules, delay, and entitlements can differ.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .market_data import fetch_historical_candles, fetch_massive_historical_candles
from .models import require_ticker


@dataclass(frozen=True)
class CandleHistory:
    provider: str
    symbol: str
    interval_minutes: int
    requested_start_utc: datetime
    requested_end_utc: datetime
    retrieved_at_utc: datetime
    candles: tuple[dict[str, Any], ...]
    retrieval_method: str
    audit: dict[str, Any]


class HistoricalCandleProvider(Protocol):
    name: str

    async def fetch(self, *, symbol: str, start: datetime, end: datetime) -> CandleHistory: ...


def _normalize_rows(rows: list[dict[str, Any]], *, symbol: str, start: datetime,
                    end: datetime, provider: str) -> tuple[dict[str, Any], ...]:
    require_ticker(symbol)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("History interval must be timezone-aware and increasing")
    normalized: list[dict[str, Any]] = []
    seen: dict[datetime, dict[str, Any]] = {}
    previous: datetime | None = None
    for row in rows:
        try:
            raw_timestamp = row["timestamp"]
            stamp = raw_timestamp if isinstance(raw_timestamp, datetime) else datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
            stamp = stamp.astimezone(timezone.utc)
            values = {name: float(row[name]) for name in ("open", "high", "low", "close", "volume")}
            if row.get("vwap") is not None:
                values["vwap"] = float(row["vwap"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError(f"{provider} returned a malformed {symbol} candle") from None
        if not start.astimezone(timezone.utc) <= stamp <= end.astimezone(timezone.utc):
            raise RuntimeError(f"{provider} returned a candle outside the requested interval at {stamp.isoformat()}")
        if stamp.second or stamp.microsecond:
            raise RuntimeError(f"{provider} returned a non-minute-aligned candle at {stamp.isoformat()}")
        o, h, low, close, volume = (values[name] for name in ("open", "high", "low", "close", "volume"))
        if (not all(math.isfinite(value) for value in values.values()) or o <= 0 or close <= 0 or volume < 0 or
                h < max(o, close, low) or low > min(o, close, h)):
            raise RuntimeError(f"{provider} returned invalid OHLCV for {symbol} at {stamp.isoformat()}")
        record = {"timestamp": stamp.isoformat(), **values, "symbol": symbol,
                  "interval_minutes": 1, "source_provider": provider}
        if stamp in seen:
            if seen[stamp] != record:
                raise RuntimeError(f"{provider} returned conflicting duplicate candle at {stamp.isoformat()}")
            raise RuntimeError(f"{provider} returned duplicate candle at {stamp.isoformat()}")
        if previous is not None and stamp < previous:
            raise RuntimeError(f"{provider} returned out-of-order candles at {stamp.isoformat()}")
        seen[stamp] = record
        normalized.append(record)
        previous = stamp
    return tuple(normalized)


class TastytradeDXLinkHistoryProvider:
    name = "tastytrade_dxlink"

    def __init__(self, *, dxlink_url: str, quote_token: str):
        self.dxlink_url = dxlink_url
        self.quote_token = quote_token

    async def fetch(self, *, symbol: str, start: datetime, end: datetime) -> CandleHistory:
        audit: dict[str, Any] = {}
        rows = await fetch_historical_candles(dxlink_url=self.dxlink_url, quote_token=self.quote_token,
                                               symbol=symbol, start=start, end=end, audit=audit)
        candles = _normalize_rows(rows, symbol=symbol, start=start, end=end, provider=self.name)
        return CandleHistory(self.name, symbol, 1, start.astimezone(timezone.utc),
                             end.astimezone(timezone.utc), datetime.now(timezone.utc), candles,
                             "DXLink Candle time-series subscription (HISTORY contract, fromTime)", audit)


class MassiveHistoryProvider:
    name = "massive"

    def __init__(self, *, api_key: str):
        if not api_key:
            raise RuntimeError("PAPER_HISTORY_PROVIDER=massive requires MASSIVE_API_KEY")
        self.api_key = api_key

    async def fetch(self, *, symbol: str, start: datetime, end: datetime) -> CandleHistory:
        rows = await asyncio.to_thread(fetch_massive_historical_candles, api_key=self.api_key,
                                       symbol=symbol, start=start, end=end)
        candles = _normalize_rows(rows, symbol=symbol, start=start, end=end, provider=self.name)
        audit = {"page_count": "handled_by_provider_client", "received_candle_count": len(candles)}
        return CandleHistory(self.name, symbol, 1, start.astimezone(timezone.utc),
                             end.astimezone(timezone.utc), datetime.now(timezone.utc), candles,
                             "Massive REST custom-bars endpoint, one-minute aggregates", audit)


def create_history_provider(name: str, *, dxlink_url: str, quote_token: str,
                            massive_api_key: str) -> HistoricalCandleProvider:
    """Construct exactly the configured provider; never auto-fail over."""
    normalized = name.strip().lower()
    if normalized == "tastytrade_dxlink":
        return TastytradeDXLinkHistoryProvider(dxlink_url=dxlink_url, quote_token=quote_token)
    if normalized == "massive":
        return MassiveHistoryProvider(api_key=massive_api_key)
    raise ValueError(f"Unsupported historical data provider: {name}")
