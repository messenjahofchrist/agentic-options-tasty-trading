"""Nightly option-quote recorder for research (owner-approved 2026-11-08). Read-only market data; never orders.

tastytrade's DXLink serves real 1-minute option bid and ask candles (Candle `price=bid` / `price=ask`), but a history
request returns at most ~8,000 candles counted back from now, so only the last ~10-15 sessions can be fetched. This
recorder saves each session's candles before they age out, building a real-quote history for replays.

Per symbol and session it saves, to data/option_history/<date>/<SYMBOL>.json.gz:
  - the underlying's regular-hours 1-minute bars,
  - 1-minute bid and ask candles for the near-the-money contracts: expirations up to MAX_DTE days out (every weekday
    for DAILY_EXPIRY_ROOTS, Fridays otherwise; expired ones are reconstructed for backfills) and the
    STRIKES_EACH_SIDE listed strikes either side of the session's closing price, calls and puts,
  - for the latest session only, a closing snapshot per contract from the REST quote (bid/ask, Greeks, IV, volume,
    open interest).
For CREDIT_SPREAD_ROOTS it also saves the credit-spread bot's band: expirations CREDIT_DTE days out and every listed
out-of-the-money strike within CREDIT_OTM_FRACTION of the price (puts below, calls above).
Rows are [minute_of_session, open, high, low, close] with minute 0 = 09:30 ET. A minute without a quote change has
no candle (carry the previous close forward). Contracts that do not exist simply return no candles.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_DTE = 35
STRIKES_EACH_SIDE = 20
CHUNK = 50          # series per DXLink request; an older session returns every candle up to now, so keep it small
DAILY_EXPIRY_ROOTS = ("SPY", "QQQ", "IWM")
CONTEXT_SYMBOLS = ("VIX",)
CREDIT_SPREAD_ROOTS = ("SPY", "QQQ")      # strategies/credit_spreads.json underlyings
CREDIT_DTE = (21, 49)
CREDIT_OTM_FRACTION = 0.12


@dataclass(frozen=True)
class Contract:
    root: str
    expiration: date
    right: str          # "C" or "P"
    strike: float

    @property
    def streamer(self) -> str:
        return f".{self.root}{self.expiration:%y%m%d}{self.right}{self.strike:g}"

    @property
    def occ(self) -> str:
        return f"{self.root:<6}{self.expiration:%y%m%d}{self.right}{int(round(self.strike * 1000)):08d}"


def chain_grid(items: list[dict], root: str) -> tuple[list[float], list[date]]:
    """(sorted strikes, sorted expirations) of a symbol's standard, active chain."""
    strikes, expirations = set(), set()
    for item in items:
        if item.get("root-symbol") != root or item.get("option-chain-type", "Standard") != "Standard" or not item.get("active", True):
            continue
        strikes.add(float(item["strike-price"]))
        expirations.add(date.fromisoformat(item["expiration-date"]))
    return sorted(strikes), sorted(expirations)


def session_expirations(day: date, listed: list[date], today: date, is_session: Callable[[date], bool], root: str,
                        max_dte: int = MAX_DTE) -> list[date]:
    """Expirations 0..max_dte days after `day`: the listed ones, plus reconstructed ones that have already expired
    (every session for daily-expiry roots, Fridays otherwise)."""
    out = {e for e in listed if day <= e <= day + timedelta(days=max_dte)}
    probe = day
    while probe < today and probe <= day + timedelta(days=max_dte):
        if is_session(probe) and (root in DAILY_EXPIRY_ROOTS or probe.weekday() == 4):
            out.add(probe)
        probe += timedelta(days=1)
    return sorted(out)


def near_strikes(strikes: list[float], spot: float, each_side: int = STRIKES_EACH_SIDE) -> list[float]:
    below = [s for s in strikes if s <= spot][-each_side:]
    above = [s for s in strikes if s > spot][:each_side]
    return below + above


def contracts_for(root: str, day: date, spot: float, strikes: list[float], expirations: list[date]) -> list[Contract]:
    return [Contract(root, e, right, k) for e in expirations for k in near_strikes(strikes, spot) for right in ("C", "P")]


def credit_band(root: str, day: date, spot: float, strikes: list[float], expirations: list[date]) -> list[Contract]:
    """The credit-spread bot's contracts: CREDIT_DTE expirations, out-of-the-money strikes within CREDIT_OTM_FRACTION."""
    if root not in CREDIT_SPREAD_ROOTS:
        return []
    exps = [e for e in expirations if CREDIT_DTE[0] <= (e - day).days <= CREDIT_DTE[1]]
    puts = [k for k in strikes if spot * (1 - CREDIT_OTM_FRACTION) <= k < spot]
    calls = [k for k in strikes if spot < k <= spot * (1 + CREDIT_OTM_FRACTION)]
    return [Contract(root, e, "P", k) for e in exps for k in puts] + [Contract(root, e, "C", k) for e in exps for k in calls]


def session_rows(rows: list[dict], open_et: datetime, close_et: datetime) -> list[list[float]]:
    """Compact [minute_of_session, o, h, l, c] rows inside [open, close)."""
    out = []
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp"]).astimezone(ET)
        if open_et <= stamp < close_et:
            out.append([int((stamp - open_et).total_seconds() // 60), row["open"], row["high"], row["low"], row["close"]])
    out.sort(key=lambda r: r[0])
    return out


HistoryFetch = Callable[[dict[str, str], datetime], Awaitable[tuple[dict[str, list[dict]], dict[str, str]]]]


async def fetch_chunked(fetch: HistoryFetch, series: dict[str, str], start: datetime,
                        keep: Callable[[list[dict]], Any] = lambda rows: rows) -> tuple[dict, dict]:
    """Fetch CHUNK series at a time; `keep` reduces each series right away (a backfill of an older session returns
    every candle from that day to now, which must not pile up in memory)."""
    got_all: dict[str, Any] = {}; failed_all: dict[str, str] = {}
    names = list(series)
    for i in range(0, len(names), CHUNK):
        got, failed = await fetch({n: series[n] for n in names[i:i + CHUNK]}, start)
        got_all.update({n: keep(rows) for n, rows in got.items()}); failed_all.update(failed)
        del got
    return got_all, failed_all


def closing_snapshot(client, contracts: list[Contract]) -> dict[str, dict]:
    """REST quotes (bid/ask, Greeks, IV, volume, open interest) for listed contracts, 100 per request."""
    out = {}
    occs = [c.occ for c in contracts]
    for i in range(0, len(occs), 100):
        query = urllib.parse.urlencode({"equity-option[]": ",".join(occs[i:i + 100])})
        for item in client.quotes(query).get("data", {}).get("items", []):
            out[item["symbol"]] = {k: item.get(k) for k in ("bid", "ask", "mark", "last", "delta", "gamma", "theta", "vega",
                                                            "rho", "volatility", "volume", "open-interest", "updated-at")}
    return out


async def record_session(*, day: date, symbols: list[str], chains: dict[str, list[dict]], fetch: HistoryFetch,
                         bounds: tuple[datetime, datetime], today: date, is_session: Callable[[date], bool],
                         out_dir: Path, snapshot: Callable[[list[Contract]], dict] | None = None) -> dict[str, Any]:
    """Record one session for every symbol; returns a summary. Writes <out_dir>/<day>/<SYMBOL>.json.gz."""
    open_et, close_et = bounds
    start = datetime.combine(day, time(0, 0), timezone.utc)
    folder = out_dir / day.isoformat(); folder.mkdir(parents=True, exist_ok=True)
    in_session = lambda rows: session_rows(rows, open_et, close_et)
    bars, bar_fail = await fetch_chunked(fetch, {s: f"{s}{{=1m,a=s,tho=true}}" for s in list(symbols) + list(CONTEXT_SYMBOLS)},
                                         start, in_session)
    summary: dict[str, Any] = {"date": day.isoformat(), "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    for symbol in list(symbols) + list(CONTEXT_SYMBOLS):
        rows = bars.get(symbol, [])
        if symbol in CONTEXT_SYMBOLS:
            (folder / f"{symbol}.json.gz").write_bytes(gzip.compress(json.dumps({"date": day.isoformat(), "symbol": symbol,
                                                                                  "underlying": rows}).encode()))
            continue
        if not rows:
            summary["symbols"][symbol] = {"error": f"no underlying bars ({bar_fail.get(symbol, 'none returned')})"}
            continue
        spot = rows[-1][4]
        strikes, listed = chain_grid(chains.get(symbol, []), symbol)
        expirations = session_expirations(day, listed, today, is_session, symbol)
        credit_exps = session_expirations(day, listed, today, is_session, symbol, max_dte=CREDIT_DTE[1]) if symbol in CREDIT_SPREAD_ROOTS else []
        contracts = list(dict.fromkeys(contracts_for(symbol, day, spot, strikes, expirations)
                                       + credit_band(symbol, day, spot, strikes, credit_exps)))
        # history_batch keys results by the streamer symbol, so fetch bids and asks separately
        quotes: dict[str, dict] = {}
        failures = 0
        for side in ("bid", "ask"):
            got, failed = await fetch_chunked(fetch, {c.streamer: f"{c.streamer}{{=1m,price={side},tho=true}}" for c in contracts},
                                              start, in_session)
            failures += sum(1 for why in failed.values() if why != "no candles returned")
            for c in contracts:
                kept = got.get(c.streamer)
                if kept:
                    quotes.setdefault(c.occ, {"expiration": c.expiration.isoformat(), "strike": c.strike, "right": c.right,
                                              "streamer": c.streamer})[side] = kept
        close_quotes = snapshot([c for c in contracts if c.occ in quotes]) if snapshot else {}
        for occ, snap in close_quotes.items():
            if occ in quotes:
                quotes[occ]["close_snapshot"] = snap
        payload = {"date": day.isoformat(), "symbol": symbol, "session_open_et": open_et.isoformat(),
                   "session_close_et": close_et.isoformat(), "spot_close": spot, "underlying": rows,
                   "expirations": sorted({e.isoformat() for e in expirations} | {c.expiration.isoformat() for c in contracts}),
                   "contracts": quotes,
                   "row_format": "[minute_of_session, open, high, low, close]; minute 0 = session open"}
        (folder / f"{symbol}.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
        summary["symbols"][symbol] = {"contracts_requested": len(contracts), "contracts_with_quotes": len(quotes),
                                      "fetch_failures": failures, "closing_snapshot": len(close_quotes)}
    return summary


def latest_completed_session(now: datetime, session_bounds: Callable[[date], tuple[datetime, datetime] | None]) -> date:
    probe = now.astimezone(ET).date()
    while True:
        bounds = session_bounds(probe)
        if bounds and bounds[1] + timedelta(minutes=15) <= now.astimezone(ET):
            return probe
        probe -= timedelta(days=1)


def run(root: Path, cfg, *, day: date | None = None, backfill_sessions: int = 0, symbols: list[str] | None = None) -> list[dict]:
    """CLI entry: record the latest completed session (or `day`), plus up to `backfill_sessions` earlier ones that
    are not on disk yet. Only the latest session gets a closing snapshot (older REST quotes would be today's)."""
    from .calendar import MarketCalendar
    from .strategy_loader import parse_strategy
    from .tastytrade_client import TastytradeClient
    from .v2.datahub import history_batch
    calendar = MarketCalendar()
    if symbols is None:
        universe = list(parse_strategy(root / "strategy.txt").universe)
        profile = json.loads((root / "profiles/small_account_universe.json").read_text(encoding="utf-8"))
        symbols = list(dict.fromkeys(universe + list(profile["symbols"])))
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    token = client.quote_token()["data"]
    now = datetime.now(timezone.utc)
    latest = day or latest_completed_session(now, calendar.session_bounds)
    days = [latest]
    probe = latest
    while len(days) <= backfill_sessions:
        probe -= timedelta(days=1)
        if calendar.session_bounds(probe):
            days.append(probe)
    out_dir = root / "data" / "option_history"
    chains = {s: client.option_chain(s).get("data", {}).get("items", []) for s in symbols}

    async def fetch(series: dict[str, str], start: datetime):
        return await history_batch(dxlink_url=token["dxlink-url"], quote_token=token["token"], series=series,
                                   start=start, timeout_seconds=240)

    summaries = []
    for d in sorted(days):
        if d != latest and (out_dir / d.isoformat()).is_dir():
            continue
        summary = asyncio.run(record_session(day=d, symbols=symbols, chains=chains, fetch=fetch, bounds=calendar.session_bounds(d),
                                             today=now.astimezone(ET).date(), is_session=lambda x: calendar.session_bounds(x) is not None,
                                             out_dir=out_dir,
                                             snapshot=(lambda cs: closing_snapshot(client, cs)) if d == latest and day is None else None))
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "recorder_log.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(summary) + "\n")
        summaries.append(summary)
    return summaries
