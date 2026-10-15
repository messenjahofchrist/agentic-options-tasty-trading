"""Standalone, read-only Tastytrade DXLink diagnostic.

Run from the repository root with:
    python diagnostics/dxlink_diagnostic.py

This script only refreshes market-data authentication and subscribes to DXLink.
It never calls account, order, engine, strategy mutation, or kill-switch APIs.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.config import AppConfig  # noqa: E402
from trader.market_data import (  # noqa: E402
    _candle_flags,
    _candle_time,
    parse_candle_feed,
)
from trader.tastytrade_client import TastytradeClient  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def scrub(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact token-like fields and known credential strings."""
    sensitive = {"token", "access_token", "refresh_token", "client_secret", "password",
                 "authorization", "auth-token", "account", "account_number", "account-number"}
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if str(key).lower().replace("-", "_") in sensitive
                           else scrub(item, secrets)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        # A server error may echo credentials in otherwise innocuous text.
        value = value.replace("Bearer ", "Bearer [REDACTED]")
        return value
    return value


def safe_error(exc: BaseException, secrets: tuple[str, ...]) -> str:
    return str(scrub(f"{type(exc).__name__}: {exc}", secrets))[:2000]


def previous_completed_session(session_date: date | None = None) -> tuple[date, datetime, datetime]:
    """Resolve a previous completed XNYS session including early closes/DST."""
    from trader.calendar import MarketCalendar

    calendar = MarketCalendar()
    candidate = session_date or datetime.now(ET).date()
    if candidate >= datetime.now(ET).date():
        candidate = datetime.now(ET).date()
    for _ in range(15):
        bounds = calendar.session_bounds(candidate)
        if bounds is not None and bounds[1].astimezone(UTC) < datetime.now(UTC):
            return candidate, bounds[0].astimezone(UTC), bounds[1].astimezone(UTC)
        candidate -= timedelta(days=1)
    raise RuntimeError("Could not find a completed XNYS session in the prior 15 calendar days")


def compact_rows(payload: Any, event_type: str, width: int) -> list[list[Any]]:
    result: list[list[Any]] = []
    if not isinstance(payload, list):
        return result
    for item in payload:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == event_type:
            # COMPACT FEED_DATA may pack many events in one positional list.
            # Keep every row; production's current helper truncates this case.
            if len(item) >= width and len(item) % width == 0:
                result.extend(item[offset:offset + width]
                              for offset in range(0, len(item), width)
                              if item[offset] == event_type)
        elif len(item) % width == 0:
            result.extend(item[offset:offset + width]
                          for offset in range(0, len(item), width)
                          if item[offset] == event_type)
    return result


def clean_server_message(message: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    # Do not persist opaque/raw non-JSON websocket data or secret-bearing fields.
    return scrub(message, secrets)


def snapshot_analysis(messages: list[dict[str, Any]], *, start: datetime,
                      session_close: datetime, symbol: str = "SPY") -> dict[str, Any]:
    expected_start = start.replace(second=0, microsecond=0)
    expected_end = session_close.replace(second=0, microsecond=0) - timedelta(minutes=1)
    expected = []
    stamp = expected_start
    while stamp <= expected_end:
        expected.append(stamp)
        stamp += timedelta(minutes=1)

    candles: list[dict[str, Any]] = []
    times: list[datetime] = []
    raw_count = 0
    returned_symbols: set[str] = set()
    flags_seen: list[int] = []
    snapshot_begin = snapshot_end = snapshot_snip = earlier_delimiter = False
    accepted_symbols = {f"{symbol}{{=1m,a=s,tho=true}}", f"{symbol}{{=m,a=s,tho=true}}"}
    for msg in messages:
        for row in compact_rows(msg.get("data"), "Candle", 12):
            if str(row[1]) not in accepted_symbols:
                continue
            raw_count += 1
            returned_symbols.add(str(row[1]))
            try:
                stamp = _candle_time(row[3]).astimezone(UTC)
                flags = _candle_flags(row[11])
                event_time = row[2]
                record = {
                    "timestamp_utc": stamp.isoformat(),
                    "timestamp_et": stamp.astimezone(ET).isoformat(),
                    "event_time_raw": event_time,
                    "open": float(row[6]), "high": float(row[7]), "low": float(row[8]),
                    "close": float(row[9]), "volume": float(row[10]),
                    "event_flags": flags, "event_flags_hex": hex(flags),
                    "sequence": row[4], "count": row[5], "event_symbol": row[1],
                    "raw_compact_event": scrub(row),
                    "snapshot_begin": bool(flags & 4),
                    "snapshot_end": bool(flags & 8),
                    "snapshot_snip": bool(flags & 16),
                }
            except (IndexError, TypeError, ValueError, OverflowError):
                candles.append({"parse_error": True, "raw_compact_event": scrub(row)})
                continue
            candles.append(record)
            flags_seen.append(flags)
            snapshot_begin |= bool(flags & 4)
            snapshot_end |= bool(flags & 8)
            snapshot_snip |= bool(flags & 16)
            if stamp < start and not (flags & 1):
                earlier_delimiter = True
            if expected_start <= stamp <= expected_end:
                times.append(stamp)

    counts = Counter(times)
    unique = sorted(counts)
    expected_set = set(expected)
    missing = sorted(expected_set - set(unique))
    duplicate_stamps = sorted(stamp for stamp, count in counts.items() if count > 1)
    gaps = []
    for left, right in zip(unique, unique[1:]):
        minutes = int((right - left).total_seconds() // 60)
        if minutes > 1:
            gaps.append({"after_utc": left.isoformat(), "before_utc": right.isoformat(),
                         "missing_minutes_between": minutes - 1})

    # Re-run the exact production parsing/completion code against the captured frames.
    production_parser: dict[str, Any] = {"status": "not_run"}
    try:
        parsed = parse_candle_feed(messages, start=start, end=session_close - timedelta(minutes=1),
                                   symbol=symbol, audit={})
        production_parser = {"status": "accepted", "candle_count": len(parsed),
                             "timestamps_match_diagnostic": sorted(
                                 datetime.fromisoformat(item["timestamp"]).astimezone(UTC) for item in parsed
                             ) == unique}
    except Exception as exc:  # diagnostic report records parser outcome, never suppresses raw data
        production_parser = {"status": "rejected", "error": str(exc)[:1000]}

    return {
        "requested_from_time_utc": start.isoformat(),
        "requested_from_time_epoch_ms": int(start.timestamp() * 1000),
        "session_close_utc": session_close.isoformat(),
        "expected_session_candle_count": len(expected),
        "raw_candle_event_count": raw_count,
        "returned_event_symbols": sorted(returned_symbols),
        "unique_in_session_candle_count": len(unique),
        "duplicate_timestamp_count": len(duplicate_stamps),
        "duplicate_timestamps_utc": [item.isoformat() for item in duplicate_stamps],
        # Match the production parser's assumed dxFeed history ordering: newest-to-oldest.
        "out_of_order_event_count": sum(1 for a, b in zip(times, times[1:]) if b > a),
        "timestamp_gap_count": len(gaps), "timestamp_gaps": gaps,
        "missing_expected_timestamp_count": len(missing),
        "missing_expected_timestamps_utc": [item.isoformat() for item in missing],
        "first_candle_utc": unique[0].isoformat() if unique else None,
        "last_candle_utc": unique[-1].isoformat() if unique else None,
        "snapshot_begin_received": snapshot_begin,
        "snapshot_end_received": snapshot_end,
        "snapshot_snip_received": snapshot_snip,
        "earlier_than_from_time_delimiter_received": earlier_delimiter,
        "event_flags_observed": sorted(set(flags_seen)),
        "candles_and_raw_metadata": candles,
        "production_parser_replay": production_parser,
    }


class Diagnostic:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started_at = utc_now()
        self.report: dict[str, Any] = {
            "tool": "standalone_tastytrade_dxlink_diagnostic",
            "started_at_utc": self.started_at,
            "safety": {"trading_engine_started": False, "order_api_called": False,
                       "kill_switch_modified": False, "watchdog_modified": False,
                       "strategy_or_provider_modified": False, "account_endpoints_called": False},
            "tests": {}, "server_messages": [], "errors": [],
        }
        self.client: TastytradeClient | None = None
        self.dxlink_url: str | None = None
        self.quote_token: str | None = None
        self.secrets: tuple[str, ...] = ()
        self.ws = None
        self.channel = 3
        self.channel_open = False
        self.snapshot_state = {"started": False, "end_waiting": False}

    def log_server(self, raw: str | bytes) -> dict[str, Any]:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                msg = {"non_object_json": scrub(msg, self.secrets)}
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            msg = {"non_json_frame": scrub(str(raw)[:1000], self.secrets)}
        safe = clean_server_message(msg, self.secrets)
        self.report["server_messages"].append({"received_at_utc": utc_now(), "message": safe})
        return msg if isinstance(msg, dict) else {}

    async def receive(self, timeout: float = 30) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        return self.log_server(raw)

    async def send(self, payload: dict[str, Any], stage: str, *, secret: bool = False) -> None:
        await self.ws.send(json.dumps(payload, separators=(",", ":")))
        self.report.setdefault("sent_stages", []).append({
            "stage": stage, "sent_at_utc": utc_now(),
            "message": "AUTH sent; token redacted" if secret else payload,
        })

    async def authenticate(self) -> None:
        try:
            import truststore
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise RuntimeError("Install the existing optional dependencies: pip install -e '.[streaming]'") from exc
        import ssl

        tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ws = await connect(self.dxlink_url, ping_interval=None, ssl=tls, close_timeout=5)
        await self.send({"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                         "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}, "SETUP")
        self.report["tests"]["connection_authentication"] = {"status": "in_progress", "stages": []}
        state = self.report["tests"]["connection_authentication"]["stages"]
        deadline = time.monotonic() + 60
        authorized = False
        channel_opened = False
        feed_configured = False
        while time.monotonic() < deadline:
            msg = await self.receive(timeout=max(1, min(30, deadline - time.monotonic())))
            kind, msg_channel = msg.get("type"), msg.get("channel")
            if kind == "AUTH_STATE":
                state.append({"stage": "AUTH_STATE", "server_state": msg.get("state"), "at_utc": utc_now()})
                if msg.get("state") == "UNAUTHORIZED":
                    await self.send({"type": "AUTH", "channel": 0, "token": self.quote_token},
                                    "AUTH", secret=True)
                    state.append({"stage": "AUTH", "status": "sent_redacted", "at_utc": utc_now()})
                elif msg.get("state") == "AUTHORIZED":
                    authorized = True
                    state.append({"stage": "AUTHORIZED", "at_utc": utc_now()})
                    await self.send({"type": "CHANNEL_REQUEST", "channel": self.channel,
                                     "service": "FEED", "parameters": {"contract": "HISTORY"}},
                                    "CHANNEL_REQUEST")
                elif msg.get("state") == "CLOSED":
                    raise ConnectionError("DXLink reported AUTH_STATE CLOSED")
            elif kind == "CHANNEL_OPENED" and msg_channel == self.channel:
                channel_opened = True
                state.append({"stage": "CHANNEL_OPENED", "at_utc": utc_now(),
                              "server_message": clean_server_message(msg, self.secrets)})
                fields = {
                    "Candle": ["eventType", "eventSymbol", "eventTime", "time", "sequence", "count",
                               "open", "high", "low", "close", "volume", "eventFlags"],
                    "Trade": ["eventType", "eventSymbol", "time", "price", "dayVolume", "size"],
                    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
                }
                await self.send({"type": "FEED_SETUP", "channel": self.channel,
                                 "acceptAggregationPeriod": 0.1, "acceptDataFormat": "COMPACT",
                                 "acceptEventFields": fields}, "FEED_SETUP")
                state.append({"stage": "FEED_SETUP", "at_utc": utc_now(), "fields": fields})
            elif kind == "FEED_CONFIG" and msg_channel == self.channel:
                feed_configured = True
                state.append({"stage": "FEED_CONFIG", "at_utc": utc_now(),
                              "server_message": clean_server_message(msg, self.secrets)})
                break
            elif kind in {"ERROR", "FEED_STATUS"}:
                state.append({"stage": kind, "at_utc": utc_now(),
                              "server_message": clean_server_message(msg, self.secrets)})
                if kind == "ERROR":
                    raise ConnectionError("DXLink returned an ERROR during handshake (see sanitized server_messages)")
        if not authorized or not channel_opened or not feed_configured:
            raise TimeoutError("Handshake did not reach AUTHORIZED, CHANNEL_OPENED, and FEED_CONFIG")
        self.channel_open = True
        self.report["tests"]["connection_authentication"].update({
            "status": "passed", "authorized": authorized, "channel_opened": channel_opened,
            "feed_configured": feed_configured,
        })

    async def historical(self, session_day: date, session_open: datetime, session_close: datetime) -> None:
        symbol = self.args.symbol
        streamer_symbol = f"{symbol}{{=1m,a=s,tho=true}}"
        from_time = int(session_open.timestamp() * 1000)
        subscription = {"type": "Candle", "symbol": streamer_symbol, "fromTime": from_time}
        self.report["historical_request"] = {
            "symbol": symbol, "candle_symbol": streamer_symbol,
            "session_date": session_day.isoformat(), "session_open_utc": session_open.isoformat(),
            "session_open_et": session_open.astimezone(ET).isoformat(),
            "session_close_utc": session_close.isoformat(), "fromTime_epoch_ms": from_time,
            "delivery_contract": "HISTORY", "expected_interval": "one minute",
        }
        started = time.monotonic()
        sent_at = utc_now()
        await self.send({"type": "FEED_SUBSCRIPTION", "channel": self.channel,
                         "reset": True, "add": [subscription]}, "HISTORICAL_CANDLE_SUBSCRIPTION")
        history_messages: list[dict[str, Any]] = []
        outcome = "timeout"
        deadline = started + self.args.snapshot_timeout
        while time.monotonic() < deadline:
            try:
                msg = await self.receive(timeout=max(0.1, min(30, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                await self.send({"type": "KEEPALIVE", "channel": 0}, "KEEPALIVE")
                continue
            if msg.get("type") == "FEED_DATA" and msg.get("channel") == self.channel:
                history_messages.append(msg)
                if _history_complete_for_message(msg, session_open, symbol, self.snapshot_state):
                    outcome = "completion_marker_or_delimiter"
                    break
            elif msg.get("type") in {"ERROR", "FEED_STATUS"}:
                if msg.get("type") == "ERROR":
                    outcome = "server_error"
                    break
        elapsed = time.monotonic() - started
        analysis = snapshot_analysis(history_messages, start=session_open,
                                     session_close=session_close, symbol=symbol)
        analysis.update({"subscription_sent_at_utc": sent_at, "elapsed_seconds": round(elapsed, 3),
                         "delivery_outcome": outcome, "frames_received": len(history_messages),
                         "timed_out": outcome == "timeout",
                         "status": ("passed" if outcome == "completion_marker_or_delimiter"
                                    and not analysis["snapshot_snip_received"]
                                    and analysis["missing_expected_timestamp_count"] == 0
                                    else "completed_partial" if outcome == "completion_marker_or_delimiter"
                                    and not analysis["snapshot_snip_received"]
                                    else "failed" if outcome == "server_error" else "incomplete")})
        self.report["tests"]["historical_candles"] = analysis
        self.report["_history_messages"] = history_messages

    async def live(self) -> None:
        from trader.calendar import MarketCalendar

        now = datetime.now(UTC)
        bounds = MarketCalendar().session_bounds(now.astimezone(ET).date())
        if bounds is None or not (bounds[0].astimezone(UTC) <= now < bounds[1].astimezone(UTC)):
            self.report["tests"]["live_feed"] = {
                "status": "inconclusive", "reason": "XNYS regular session is closed at diagnostic time",
                "checked_at_utc": now.isoformat(), "trade_events": 0, "quote_events": 0,
            }
            return
        duration = self.args.live_seconds
        result: dict[str, Any] = {"status": "in_progress", "started_at_utc": utc_now(),
                                  "requested_duration_seconds": duration, "trade_events": [],
                                  "quote_updates": [], "keepalives_sent": 0,
                                  "reconnect_attempts": 0, "errors": []}
        self.report["tests"]["live_feed"] = result
        start = time.monotonic()
        last_keepalive = start
        attempts = 0
        while time.monotonic() - start < duration:
            if self.ws is None:
                try:
                    await self.authenticate()
                    attempts = 0
                except Exception as exc:
                    result["errors"].append(safe_error(exc, self.secrets))
                    break
            if not self.channel_open:
                break
            if result.get("live_subscribed") is not True:
                await self.send({"type": "FEED_SUBSCRIPTION", "channel": self.channel,
                                 "reset": True, "add": [
                                     {"type": "Trade", "symbol": self.args.symbol},
                                     {"type": "Quote", "symbol": self.args.symbol},
                                 ]}, "LIVE_TRADE_QUOTE_SUBSCRIPTION")
                result["live_subscribed"] = True
                result["subscription_sent_at_utc"] = utc_now()
            remaining = duration - (time.monotonic() - start)
            try:
                msg = await self.receive(timeout=min(25, max(0.1, remaining)))
            except asyncio.TimeoutError:
                if time.monotonic() - last_keepalive >= 25:
                    await self.send({"type": "KEEPALIVE", "channel": 0}, "KEEPALIVE")
                    result["keepalives_sent"] += 1
                    last_keepalive = time.monotonic()
                continue
            except Exception as exc:
                result["errors"].append(safe_error(exc, self.secrets))
                if attempts >= 2:
                    break
                attempts += 1
                result["reconnect_attempts"] += 1
                result["live_subscribed"] = False
                result["channel_opened_after_reconnect"] = False
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.channel_open = False
                await asyncio.sleep(min(attempts, 3))
                continue
            if msg.get("type") != "FEED_DATA" or msg.get("channel") != self.channel:
                if msg.get("type") in {"ERROR", "FEED_STATUS"}:
                    result["errors"].append(clean_server_message(msg, self.secrets))
                continue
            received = utc_now()
            for row in compact_rows(msg.get("data"), "Trade", 6):
                if len(row) < 6 or row[1] != self.args.symbol:
                    continue
                try:
                    ts = _candle_time(row[2]).astimezone(UTC)
                    price, size = float(row[3]), float(row[5])
                except (TypeError, ValueError, OverflowError):
                    continue
                result["trade_events"].append({"event_time_utc": ts.isoformat(),
                                               "received_at_utc": received,
                                               "price": price, "size": size,
                                               "raw_compact_event": scrub(row, self.secrets)})
            for row in compact_rows(msg.get("data"), "Quote", 6):
                if len(row) < 6 or row[1] != self.args.symbol:
                    continue
                try:
                    bid, ask = float(row[2]), float(row[3])
                except (TypeError, ValueError, OverflowError):
                    continue
                result["quote_updates"].append({"received_at_utc": received, "bid": bid, "ask": ask,
                                                "bid_size": row[4], "ask_size": row[5],
                                                "raw_compact_event": scrub(row, self.secrets)})
        trades = result["trade_events"]
        quote_updates = result["quote_updates"]
        trade_advances = len({item["event_time_utc"] for item in trades}) >= 2
        quote_changes = len({(item["bid"], item["ask"]) for item in quote_updates}) >= 2
        result.update({"finished_at_utc": utc_now(), "elapsed_seconds": round(time.monotonic() - start, 3),
                       "trade_count": len(trades), "quote_update_count": len(quote_updates),
                       "trade_timestamps_advanced": trade_advances, "bid_ask_changed": quote_changes,
                       "reconnection_status": "not_exercised" if result["reconnect_attempts"] == 0 else "attempted",
                       "status": "passed" if trade_advances and quote_changes else "inconclusive",
                       "reason": None if trade_advances and quote_changes else
                       "No advancing trade timestamps and/or changing bid-ask were observed in the bounded live window"})

    async def run(self) -> None:
        cfg = AppConfig.from_env()
        cfg.require_market_data_credentials()
        self.secrets = tuple(value for value in
                             (cfg.client_id, cfg.client_secret, cfg.refresh_token) if value)
        self.report["execution_context"] = {"trading_mode_env": os.getenv("TRADING_MODE", "PAPER"),
                                            "provider_env": os.getenv("PAPER_HISTORY_PROVIDER", "tastytrade_dxlink"),
                                            "symbol": self.args.symbol}
        self.client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token,
                                       base_url=cfg.production_base_url)
        try:
            quote_data = self.client.quote_token().get("data", {})
            if self.client._token is not None:
                self.secrets = self.secrets + (self.client._token.access_token,)
            self.quote_token = quote_data.get("token")
            self.dxlink_url = quote_data.get("dxlink-url")
            if not self.quote_token or not self.dxlink_url:
                raise RuntimeError("Tastytrade quote-token response lacked token or dxlink-url")
            self.secrets = self.secrets + (self.quote_token,)
            self.report["tests"]["quote_token_rest"] = {"status": "passed", "endpoint": "GET /api-quote-tokens",
                                                        "token_logged": False, "dxlink_url": self.dxlink_url}
            session_day, session_open, session_close = previous_completed_session(self.args.session_date)
            self.report["market_session"] = {"calendar": "XNYS", "session_date": session_day.isoformat(),
                                              "open_utc": session_open.isoformat(),
                                              "open_et": session_open.astimezone(ET).isoformat(),
                                              "close_utc": session_close.isoformat(),
                                              "close_et": session_close.astimezone(ET).isoformat()}
            await self.authenticate()
            await self.historical(session_day, session_open, session_close)
            await self.live()
        except Exception as exc:
            self.report["errors"].append(safe_error(exc, self.secrets))
            self.report["tests"].setdefault("quote_token_rest", {
                "status": "failed", "endpoint": "GET /api-quote-tokens",
                "error": safe_error(exc, self.secrets), "token_logged": False,
            })
            if "connection_authentication" not in self.report["tests"]:
                self.report["tests"]["connection_authentication"] = {"status": "failed",
                                                                         "error": safe_error(exc, self.secrets)}
        finally:
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
            self.report.pop("_history_messages", None)
            self.report["finished_at_utc"] = utc_now()
            self.report["production_comparison"] = compare_production()
            self.report["assessment"] = assess(self.report)


def _history_complete_for_message(message: dict[str, Any], start: datetime,
                                  symbol: str, state: dict[str, bool]) -> bool:
    """Batch-aware completion detector, kept independent from production parser."""
    accepted = {f"{symbol}{{=1m,a=s,tho=true}}", f"{symbol}{{=m,a=s,tho=true}}"}
    for row in compact_rows(message.get("data"), "Candle", 12):
        if str(row[1]) not in accepted:
            continue
        stamp = _candle_time(row[3]).astimezone(UTC)
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
        elif state["started"] and stamp < start.astimezone(UTC) and not flags & 1:
            return True
    return False


def compare_production() -> dict[str, Any]:
    from trader import market_data

    try:
        source = inspect.getsource(market_data.dxlink_events)
        history_source = inspect.getsource(market_data.fetch_historical_candles)
        parser_source = inspect.getsource(market_data.parse_candle_feed)
    except (OSError, TypeError) as exc:
        return {"status": "unavailable", "reason": str(exc)}
    return {
        "auth": "Same TastytradeClient.quote_token() REST authentication is used.",
        "candle_symbol": "Same SPY{=1m,a=s,tho=true} construction as production.",
        "fromTime": "Computed from XNYS session_open converted to UTC epoch milliseconds; same start.timestamp()*1000 approach.",
        "subscription_order": "Diagnostic logs each server handshake stage and sends in the same order as production.",
        "history_contract": "HISTORY contract in CHANNEL_REQUEST, matching production.",
        "feed_setup": "COMPACT with Candle/Trade/Quote event fields; Candle field order matches production.",
        "feed_config": "Diagnostic waits for FEED_CONFIG before sending candle subscription, matching production.",
        "parser": "Diagnostic records raw 12-field candle rows and replays captured frames through production parse_candle_feed().",
        "compact_batch_handling": "Diagnostic splits all 12-field events in a compact batch; the production helper behavior is inspected below.",
        "snapshot_completion": "Diagnostic scans every decoded event in each batch; production parser result is included.",
        "timeout": "Production timeout defaults to 60 seconds; diagnostic timeout is configurable and reports elapsed time.",
        "source_checks": {"dxlink_events_contains_compact_negotiation": "acceptDataFormat" in source,
                          "history_uses_epoch_ms": "start.timestamp() * 1000" in history_source,
                          "parser_checks_snapshot_flags": all(text in parser_source for text in ("flags & 4", "flags & 8", "flags & 16")),
                          "production_compact_decoder_truncates_batch":
                              "rows.append(item[:12])" in inspect.getsource(market_data._compact_candle_rows)},
    }


def assess(report: dict[str, Any]) -> dict[str, Any]:
    hist = report.get("tests", {}).get("historical_candles", {})
    auth = report.get("tests", {}).get("connection_authentication", {})
    rows = []
    if auth.get("status") != "passed":
        rows.append({"candidate": "authentication problem", "evidence": "Handshake did not complete AUTHORIZED + FEED_CONFIG"})
    else:
        rows.append({"candidate": "authentication problem", "evidence": "Quote-token REST request and DXLink auth stages passed; authentication is unlikely to explain a two-candle partial replay."})
    if hist:
        if hist.get("unique_in_session_candle_count", 0) and hist.get("first_candle_utc"):
            rows.append({"candidate": "incorrect candle symbol", "evidence": f"Requested {report.get('historical_request', {}).get('candle_symbol')}; server returned {hist.get('returned_event_symbols')}. Both =1m (request) and =m (server alias) were decoded."})
        else:
            rows.append({"candidate": "incorrect candle symbol", "evidence": "No matching Candle events; insufficient to distinguish symbol rejection from provider/entitlement behavior unless server returned an explicit error."})
        rows.append({"candidate": "incorrect fromTime", "evidence": f"Requested UTC session open {report.get('historical_request', {}).get('session_open_utc')} as {report.get('historical_request', {}).get('fromTime_epoch_ms')} ms since epoch."})
        rows.append({"candidate": "incorrect HISTORY configuration", "evidence": "Diagnostic requests contract=HISTORY; inspect sanitized server error/status if the service rejects it."})
        rows.append({"candidate": "handshake/subscription ordering", "evidence": "Actual stage timestamps and responses are recorded; compare them with production_comparison."})
        rows.append({"candidate": "event parsing", "evidence": "Raw compact candle rows are retained and replayed through the production parser for a direct count/result comparison."})
        rows.append({"candidate": "snapshot completion handling", "evidence": f"begin={hist.get('snapshot_begin_received')}, end={hist.get('snapshot_end_received')}, snip={hist.get('snapshot_snip_received')}, earlier_delimiter={hist.get('earlier_than_from_time_delimiter_received')}, outcome={hist.get('delivery_outcome')}."})
        rows.append({"candidate": "historical entitlement/provider limitation", "evidence": "Only supported if server explicitly returns an entitlement/permission error; candle count alone cannot prove entitlement limits."})
        rows.append({"candidate": "connection interruption/timeout", "evidence": f"outcome={hist.get('delivery_outcome')}, elapsed={hist.get('elapsed_seconds')}s, recorded server/network errors={len(report.get('errors', []))}."})
    production = hist.get("production_parser_replay", {})
    if auth.get("status") == "passed" and hist.get("status") == "passed" \
            and production.get("status") == "rejected" \
            and "snapshot did not complete" in production.get("error", ""):
        likely = "production_compact_candle_batch_parser_drops_rows_and_snapshot_end"
        explanation = ("DXLink returned a complete regular-session snapshot in batched COMPACT frames. The "
                       "standalone decoder found the candles and completion flag, while production "
                       "parse_candle_feed() rejected those same frames. Production _compact_candle_rows() "
                       "takes only item[:12] when a nested positional list begins with Candle, discarding the "
                       "remaining rows and potentially a later SNAPSHOT_END in that batch. This is the best-supported "
                       "cause of the apparent two-candle result.")
    elif auth.get("status") == "passed" and hist.get("unique_in_session_candle_count", 0) > 0 \
            and hist.get("timed_out") and not hist.get("snapshot_end_received") \
            and not hist.get("earlier_than_from_time_delimiter_received"):
        likely = "historical_delivery_did_not_complete; provider_limit_vs_snapshot_delivery_unresolved"
        explanation = ("The socket authenticated and delivered matching Candle events, but no recognized completion "
                       "marker/delimiter arrived before timeout. This establishes incomplete delivery; it does not "
                       "prove an entitlement restriction without an explicit server denial.")
    elif auth.get("status") != "passed":
        likely = "authentication_or_handshake_failure"
        explanation = "The captured handshake did not reach the authenticated configured-feed state."
    elif hist.get("status") == "completed_partial":
        likely = "provider_returned_completed_but_partial_session_history"
        explanation = ("A completion condition arrived, but the completed snapshot omitted expected regular-session "
                       "timestamps. This is a data-coverage issue; entitlement is proven only if the server explicitly says so.")
    elif hist.get("status") == "passed" and hist.get("missing_expected_timestamp_count", 0) == 0:
        likely = "no_historical_candle_failure_reproduced"
        explanation = "The historical subscription completed and covered every expected regular-session minute."
    else:
        likely = "undetermined_without_server_evidence"
        explanation = "Two candles alone do not identify entitlement, symbol, timestamp, parser, snapshot, or connection as the cause."
    return {"most_likely_reason": likely,
            "explanation": explanation,
            "candidates": rows}


def render_text(report: dict[str, Any]) -> str:
    lines = ["Tastytrade DXLink Diagnostic", "=" * 32,
             f"Started UTC: {report.get('started_at_utc')}",
             f"Finished UTC: {report.get('finished_at_utc')}",
             f"Session: {report.get('market_session', {}).get('session_date', 'unresolved')}"]
    for name, result in report.get("tests", {}).items():
        lines.append(f"\n{name}: {result.get('status', 'recorded')}")
        if name == "historical_candles":
            for key in ("expected_session_candle_count", "raw_candle_event_count", "unique_in_session_candle_count",
                        "duplicate_timestamp_count", "timestamp_gap_count", "missing_expected_timestamp_count",
                        "first_candle_utc", "last_candle_utc", "snapshot_begin_received",
                        "snapshot_end_received", "snapshot_snip_received",
                        "earlier_than_from_time_delimiter_received", "delivery_outcome", "elapsed_seconds"):
                lines.append(f"  {key}: {result.get(key)}")
            for item in result.get("candles_and_raw_metadata", []):
                lines.append("  CANDLE " + json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        elif name == "live_feed":
            lines.append(f"  reason: {result.get('reason')}")
            lines.append(f"  trades={result.get('trade_count', 0)} quotes={result.get('quote_update_count', 0)} "
                         f"keepalives={result.get('keepalives_sent', 0)} reconnects={result.get('reconnect_attempts', 0)}")
    lines.append("\nAssessment:")
    lines.append(json.dumps(report.get("assessment", {}), indent=2, ensure_ascii=False))
    lines.append("\nCredential handling: OAuth/quote tokens and account endpoints are excluded/redacted.")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("SPY",), default="SPY")
    parser.add_argument("--session-date", type=date.fromisoformat,
                        help="Optional XNYS date. If omitted, select the latest completed regular session.")
    parser.add_argument("--snapshot-timeout", type=float, default=90,
                        help="Seconds to wait for historical completion (default 90).")
    parser.add_argument("--live-seconds", type=float, default=90,
                        help="Bounded live feed observation time while XNYS is open (default 90).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.snapshot_timeout <= 0 or args.live_seconds <= 0:
        print("Timeouts must be positive.", file=sys.stderr)
        return 2
    diagnostic = Diagnostic(args)
    try:
        asyncio.run(diagnostic.run())
    except KeyboardInterrupt:
        diagnostic.report["errors"].append("Interrupted by user; no trading actions were taken.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"dxlink_diagnostic_{stamp}.json"
    text_path = out_dir / f"dxlink_diagnostic_{stamp}.txt"
    # Final defense-in-depth scrub before persistence.
    safe_report = scrub(diagnostic.report, diagnostic.secrets)
    json_path.write_text(json.dumps(safe_report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    text_path.write_text(render_text(safe_report), encoding="utf-8")
    print(f"Diagnostic JSON: {json_path.relative_to(ROOT)}")
    print(f"Readable report: {text_path.relative_to(ROOT)}")
    print(f"Assessment: {safe_report.get('assessment', {}).get('most_likely_reason', 'undetermined')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
