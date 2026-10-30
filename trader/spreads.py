"""CREDIT_SPREADS (PAPER): sell a protected option spread and buy it back cheaper.

A put credit spread sells one put and buys a lower-strike put as protection; a call credit spread sells one call
and buys a higher-strike call. The credit is kept if the price stays on the right side of the sold strike; the
protection caps the worst loss at (width - credit) x 100.

One run (`trader.cli spreads-run`, every 15 minutes in market hours) manages every open spread, then, inside the
entry window, considers one new spread per underlying for each ledger. State lives in data/credit_spreads/; the
opening-range bots, their config and their ledgers are never read or written. PAPER only: no order is ever sent.
"""
from __future__ import annotations

import asyncio
import json
import math
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .fees import FeeSchedule
from .logging_utils import append_jsonl, atomic_replace
from .strategy_loader import _strip_json_comments

ET = ZoneInfo("America/New_York")
CONFIG_FILE = "strategies/credit_spreads.json"
STATE_DIR = "credit_spreads"
FEES = FeeSchedule()


class SpreadConfigError(ValueError):
    pass


# --- config (fail closed) ---------------------------------------------------------------------------------------

def _num(lo: float, hi: float, *, integer: bool = False):
    def check(value: Any, where: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SpreadConfigError(f"{where} must be a finite number")
        if integer and not float(value).is_integer():
            raise SpreadConfigError(f"{where} must be an integer")
        if not lo <= value <= hi:
            raise SpreadConfigError(f"{where} must be between {lo} and {hi}")
    return check


def _bool(value: Any, where: str) -> None:
    if not isinstance(value, bool):
        raise SpreadConfigError(f"{where} must be true or false")


def _hhmm(value: Any, where: str) -> None:
    try:
        time.fromisoformat(value)
    except (TypeError, ValueError):
        raise SpreadConfigError(f"{where} must be HH:MM") from None


def _const(expected: str):
    def check(value: Any, where: str) -> None:
        if value != expected:
            raise SpreadConfigError(f"{where} must be {expected!r}")
    return check


def _string(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SpreadConfigError(f"{where} must be a non-empty string")


def _tickers(value: Any, where: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(s, str) or not s.isupper() for s in value):
        raise SpreadConfigError(f"{where} must be a non-empty list of uppercase tickers")


def _sides(value: Any, where: str) -> None:
    if value not in ("trend", "puts_only", "calls_only"):
        raise SpreadConfigError(f"{where} must be trend, puts_only or calls_only")


def _widths(value: Any, where: str) -> None:
    if not isinstance(value, list) or not value:
        raise SpreadConfigError(f"{where} must be a non-empty list")
    for i, width in enumerate(value):
        _num(0.5, 50)(width, f"{where}[{i}]")
    if value != sorted(value, reverse=True):
        raise SpreadConfigError(f"{where} must be widest first")


ACCOUNT_SCHEMA = {"starting_capital": _num(100, 10_000_000), "max_loss_per_trade_dollars": _num(10, 100_000),
                  "widths": _widths, "max_open": _num(1, 20, integer=True),
                  "max_total_risk_fraction": _num(0.01, 1.0), "contracts": _num(1, 10, integer=True)}

SCHEMA = {
    "strategy_id": _const("CREDIT_SPREADS"),
    "version": _string,
    "mode": _const("PAPER"),
    "enabled": _bool,
    "underlyings": _tickers,
    "entry": {
        "window_from_et": _hhmm, "window_until_et": _hhmm, "trend_sma_sessions": _num(5, 200, integer=True),
        "sides": _sides, "dte_min": _num(1, 120, integer=True), "dte_target": _num(1, 120, integer=True),
        "dte_max": _num(1, 120, integer=True), "short_delta_min": _num(0.01, 0.5),
        "short_delta_target": _num(0.01, 0.5), "short_delta_max": _num(0.01, 0.5),
        "min_credit_fraction_of_width": _num(0.01, 0.9), "max_leg_spread_fraction": _num(0.01, 1.0),
        "min_open_interest": _num(0, 1_000_000, integer=True), "max_quote_age_seconds": _num(5, 3600),
        "max_vix": _num(5, 200), "max_new_per_underlying_per_day": _num(1, 10, integer=True),
    },
    "exits": {"take_profit_fraction": _num(0.05, 0.95), "stop_loss_multiple": _num(0.25, 10),
              "close_at_dte": _num(0, 60, integer=True)},
}


def _validate(data: Any, schema: dict, where: str) -> None:
    if not isinstance(data, dict):
        raise SpreadConfigError(f"{where or 'config'} must be an object")
    unknown, missing = set(data) - set(schema), set(schema) - set(data)
    if unknown:
        raise SpreadConfigError(f"Unknown {where or 'config'} keys: {', '.join(sorted(unknown))}")
    if missing:
        raise SpreadConfigError(f"Missing {where or 'config'} keys: {', '.join(sorted(missing))}")
    for key, rule in schema.items():
        path = f"{where}.{key}" if where else key
        _validate(data[key], rule, path) if isinstance(rule, dict) else rule(data[key], path)


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SpreadConfigError("config must be an object")
    _validate({k: v for k, v in data.items() if k != "accounts"}, SCHEMA, "")
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise SpreadConfigError("accounts must define at least one ledger")
    for name, account in accounts.items():
        if not isinstance(name, str) or not name.endswith("_PAPER"):
            raise SpreadConfigError(f"account {name!r} must be named *_PAPER")
        _validate(account, ACCOUNT_SCHEMA, f"accounts.{name}")
    entry = data["entry"]
    if not entry["dte_min"] <= entry["dte_target"] <= entry["dte_max"]:
        raise SpreadConfigError("entry: dte_min <= dte_target <= dte_max required")
    if not entry["short_delta_min"] <= entry["short_delta_target"] <= entry["short_delta_max"]:
        raise SpreadConfigError("entry: short_delta_min <= short_delta_target <= short_delta_max required")
    if entry["dte_min"] <= data["exits"]["close_at_dte"]:
        raise SpreadConfigError("exits.close_at_dte must be below entry.dte_min (a new spread would close at once)")
    if time.fromisoformat(entry["window_from_et"]) >= time.fromisoformat(entry["window_until_et"]):
        raise SpreadConfigError("entry window must start before it ends")
    return data


def load_config(root: Path, path: str = CONFIG_FILE) -> dict[str, Any]:
    file = Path(root) / path
    try:
        data = json.loads(_strip_json_comments(file.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise SpreadConfigError(f"{path} is not valid JSON: {exc}") from exc
    return validate_config(data)


# --- quotes ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    symbol: str
    strike: float
    right: str          # "P" or "C"
    expiration: date
    bid: float
    ask: float
    delta: float | None
    open_interest: int
    updated_at: datetime | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def fresh(self, now: datetime, max_age: float) -> bool:
        # A quote stamped after `now` (the run's start; the chain and daily history take seconds) is fresh.
        return self.updated_at is not None and (now - self.updated_at).total_seconds() <= max_age

    def tradeable(self, max_spread_fraction: float) -> bool:
        return self.ask > 0 and self.bid >= 0 and self.ask >= self.bid and (
            self.ask - self.bid <= max(0.05, max_spread_fraction * self.mid))


def _stamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _f(raw: Any) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def quote_legs(client, contracts: dict[str, dict]) -> dict[str, Leg]:
    """REST quotes (with Greeks) for chain entries keyed by option symbol, 100 per request."""
    out: dict[str, Leg] = {}
    symbols = list(contracts)
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        items = client.quotes(urllib.parse.urlencode({"equity-option[]": ",".join(chunk)})).get("data", {}).get("items", [])
        for item in items:
            contract = contracts.get(item.get("symbol"))
            if contract is None:
                continue
            bid, ask = _f(item.get("bid")), _f(item.get("ask"))
            if bid is None or ask is None:
                continue
            out[item["symbol"]] = Leg(symbol=item["symbol"], strike=float(contract["strike-price"]),
                                      right=contract["option-type"].upper()[0],
                                      expiration=date.fromisoformat(contract["expiration-date"]),
                                      bid=bid, ask=ask, delta=_f(item.get("delta")),
                                      open_interest=int(float(item.get("open-interest") or 0)),
                                      updated_at=_stamp(item.get("updated-at")))
    return out


def spot_quotes(client, symbols: list[str]) -> dict[str, dict]:
    items = client.quotes(urllib.parse.urlencode({"equity[]": ",".join(symbols)})).get("data", {}).get("items", [])
    out = {}
    for item in items:
        price = _f(item.get("mark")) or _f(item.get("last"))
        if price and price > 0:
            out[item["symbol"]] = {"price": price, "updated_at": _stamp(item.get("updated-at"))}
    return out


def vix_level(client) -> float | None:
    items = client.quotes(urllib.parse.urlencode({"index[]": "VIX"})).get("data", {}).get("items", [])
    item = next((i for i in items if i.get("symbol") == "VIX"), None)
    return _f(item.get("last")) if item else None


# --- ledger ---------------------------------------------------------------------------------------------------

class Ledger:
    def __init__(self, path: Path, name: str, starting_capital: float):
        self.path, self.name = path, name
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"account": name, "mode": "PAPER", "starting_capital": float(starting_capital),
                         "cash": float(starting_capital), "realized_pnl": 0.0, "fees_paid": 0.0,
                         "open": [], "closed": []}

    @property
    def open(self) -> list[dict]:
        return self.data["open"]

    def open_risk(self) -> float:
        return sum(p["max_loss_dollars"] for p in self.open)

    def equity(self) -> float:
        """Cash minus the cost to buy back every open spread at its last mid."""
        return self.data["cash"] - sum(p.get("last_mark", p["credit"]) * 100 * p["contracts"] for p in self.open)

    def entered_today(self, underlying: str, today: date) -> int:
        return sum(1 for p in self.open + self.data["closed"] if p["underlying"] == underlying
                   and datetime.fromisoformat(p["entry_time"]).astimezone(ET).date() == today)

    def add(self, position: dict) -> None:
        self.data["cash"] += position["credit"] * 100 * position["contracts"] - position["fees_open"]
        self.data["fees_paid"] += position["fees_open"]
        self.open.append(position)

    def close(self, position: dict, *, debit: float, reason: str, now: datetime, fees: float) -> dict:
        qty = position["contracts"]
        pnl = (position["credit"] - debit) * 100 * qty - position["fees_open"] - fees
        self.data["cash"] -= debit * 100 * qty + fees
        self.data["fees_paid"] += fees
        self.data["realized_pnl"] += pnl
        self.open.remove(position)
        closed = {**position, "exit_time": now.isoformat(), "exit_debit": round(debit, 4), "exit_reason": reason,
                  "fees_close": fees, "realized_pnl": round(pnl, 2)}
        self.data["closed"].append(closed)
        return closed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        atomic_replace(tmp, self.path)


# --- decisions ------------------------------------------------------------------------------------------------

def choose_expiration(chain: list[dict], today: date, entry: dict) -> date | None:
    days = sorted({date.fromisoformat(c["expiration-date"]) for c in chain if c.get("active")})
    inside = [d for d in days if entry["dte_min"] <= (d - today).days <= entry["dte_max"]]
    if not inside:
        return None
    return min(inside, key=lambda d: (abs((d - today).days - entry["dte_target"]), d))


def candidate_contracts(chain: list[dict], expiration: date, right: str, spot: float) -> dict[str, dict]:
    """Out-of-the-money contracts of one expiration within 15% of the price (the short and long legs live here)."""
    out = {}
    for c in chain:
        if not c.get("active") or c.get("option-type", "").upper()[:1] != right:
            continue
        if date.fromisoformat(c["expiration-date"]) != expiration:
            continue
        strike = float(c["strike-price"])
        if (right == "P" and spot * 0.85 <= strike < spot) or (right == "C" and spot < strike <= spot * 1.15):
            out[c["symbol"]] = c
    return out


def pick_spread(legs: dict[str, Leg], *, right: str, entry: dict, account: dict, now: datetime) -> tuple[dict | None, list[str]]:
    """The short leg nearest the target delta and the widest affordable protection. (spread, reasons if none)."""
    max_age, max_sf = entry["max_quote_age_seconds"], entry["max_leg_spread_fraction"]
    by_strike = {leg.strike: leg for leg in legs.values()}
    shorts = [leg for leg in legs.values() if leg.delta is not None
              and entry["short_delta_min"] <= abs(leg.delta) <= entry["short_delta_max"]]
    if not shorts:
        return None, ["no option with delta in range"]
    shorts.sort(key=lambda leg: (abs(abs(leg.delta) - entry["short_delta_target"]), leg.strike))
    reasons: list[str] = []
    qty = int(account["contracts"])
    fees_round_trip = 2 * FEES.estimate(qty, legs=2)
    for short in shorts[:3]:
        if not short.fresh(now, max_age):
            reasons.append(f"{short.symbol.strip()}: quote stale"); continue
        if not short.tradeable(max_sf) or short.bid <= 0:
            reasons.append(f"{short.symbol.strip()}: bid/ask too wide or no bid"); continue
        if short.open_interest < entry["min_open_interest"]:
            reasons.append(f"{short.symbol.strip()}: open interest {short.open_interest}"); continue
        for width in account["widths"]:
            long_strike = short.strike - width if right == "P" else short.strike + width
            protect = by_strike.get(round(long_strike, 2))
            if protect is None:
                reasons.append(f"width {width:g}: no {long_strike:g} strike"); continue
            if not protect.fresh(now, max_age) or not protect.tradeable(max_sf):
                reasons.append(f"width {width:g}: protection quote stale or too wide"); continue
            credit = round(short.bid - protect.ask, 4)       # sell at the bid, buy the protection at the ask
            if credit < entry["min_credit_fraction_of_width"] * width:
                reasons.append(f"width {width:g}: credit {credit:.2f} under "
                               f"{entry['min_credit_fraction_of_width']:.0%} of the width"); continue
            max_loss = (width - credit) * 100 * qty + fees_round_trip
            if max_loss > account["max_loss_per_trade_dollars"]:
                reasons.append(f"width {width:g}: worst loss ${max_loss:.0f} over "
                               f"${account['max_loss_per_trade_dollars']:.0f}"); continue
            return {"short": short, "long": protect, "width": width, "credit": credit,
                    "max_loss_dollars": round(max_loss, 2), "contracts": qty}, []
    return None, reasons or ["no affordable spread"]


def exit_reason(position: dict, mark: float, today: date, exits: dict) -> str | None:
    credit = position["credit"]
    if mark <= credit * (1 - exits["take_profit_fraction"]):
        return "take_profit"
    if mark - credit >= exits["stop_loss_multiple"] * credit:
        return "stop_loss"
    if (date.fromisoformat(position["expiration"]) - today).days <= exits["close_at_dte"]:
        return "time_exit"
    return None


def intrinsic(position: dict, spot: float) -> float:
    width, k = position["width"], position["short_strike"]
    value = k - spot if position["right"] == "P" else spot - k
    return max(0.0, min(width, value))


def trend_side(closes: list[float], spot: float, sessions: int, sides: str) -> tuple[str | None, dict]:
    if sides == "puts_only":
        return "P", {"rule": "puts_only"}
    if sides == "calls_only":
        return "C", {"rule": "calls_only"}
    if len(closes) < sessions:
        return None, {"rule": "trend", "reason": f"only {len(closes)} daily closes (need {sessions})"}
    sma = sum(closes[-sessions:]) / sessions
    info = {"rule": "trend", "sma": round(sma, 2), "spot": spot, "sessions": sessions}
    if spot > sma:
        return "P", {**info, "reason": f"price {spot:.2f} above its {sessions}-day average {sma:.2f}: sell a put spread"}
    if spot < sma:
        return "C", {**info, "reason": f"price {spot:.2f} below its {sessions}-day average {sma:.2f}: sell a call spread"}
    return None, {**info, "reason": "price exactly at its average"}


def daily_closes(cfg, symbols: list[str], today: date, cache_file: Path) -> dict[str, list[float]]:
    """Completed daily closes (oldest first), fetched once a day from tastytrade DXLink {=1d} candles."""
    if cache_file.is_file():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("date") == today.isoformat() and all(s in cached["closes"] for s in symbols):
            return cached["closes"]
    from .tastytrade_client import TastytradeClient
    from .v2.datahub import completed_daily, history_batch
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    token = client.quote_token().get("data", {})
    got, _ = asyncio.run(history_batch(dxlink_url=token["dxlink-url"], quote_token=token["token"],
                                       series={s: f"{s}{{=1d}}" for s in symbols},
                                       start=datetime.now(timezone.utc) - timedelta(days=140), timeout_seconds=120))
    closes = {s: [float(r["close"]) for r in sorted(completed_daily(rows, today), key=lambda r: r["session_date"])]
              for s, rows in got.items()}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"date": today.isoformat(), "closes": closes}), encoding="utf-8")
    return closes


# --- one run --------------------------------------------------------------------------------------------------

def run_once(root: Path, data_dir: Path, client, *, now: datetime, session: tuple[datetime, datetime] | None,
             closes_fn: Callable[[list[str], date], dict[str, list[float]]], log=print) -> dict[str, Any]:
    """Manage open spreads, then consider new ones. `session` is today's (open, close) in ET, or None if closed."""
    cfg = load_config(root)
    state = data_dir / STATE_DIR
    decisions = state / "decisions.jsonl"
    local = now.astimezone(ET)
    today = local.date()
    summary: dict[str, Any] = {"time_et": local.isoformat(timespec="minutes"), "version": cfg["version"],
                               "closed": [], "opened": [], "skipped": []}
    if session is None or not session[0] + timedelta(minutes=5) <= local < session[1] - timedelta(minutes=5):
        summary["skipped"].append("market not open (or in its first/last 5 minutes)")
        return summary
    ledgers = {name: Ledger(state / f"{name}.json", name, acc["starting_capital"]) for name, acc in cfg["accounts"].items()}
    exits, entry = cfg["exits"], cfg["entry"]
    spots = spot_quotes(client, cfg["underlyings"] + sorted({p["underlying"] for l in ledgers.values() for p in l.open}))

    # 1. Manage open spreads.
    open_contracts = {}
    for ledger in ledgers.values():
        for p in ledger.open:
            for symbol, strike in ((p["short_symbol"], p["short_strike"]), (p["long_symbol"], p["long_strike"])):
                open_contracts[symbol] = {"strike-price": strike, "option-type": p["right"], "expiration-date": p["expiration"]}
    legs = quote_legs(client, open_contracts) if open_contracts else {}
    for ledger in ledgers.values():
        for p in list(ledger.open):
            short, protect = legs.get(p["short_symbol"]), legs.get(p["long_symbol"])
            if date.fromisoformat(p["expiration"]) <= today and p["underlying"] in spots:
                value = intrinsic(p, spots[p["underlying"]]["price"])
                closed = ledger.close(p, debit=value, reason="expired", now=now, fees=0.0 if value == 0 else FEES.estimate(p["contracts"], legs=2))
                summary["closed"].append({"account": ledger.name, **_brief(closed)})
                continue
            if short is None or protect is None or not (short.fresh(now, entry["max_quote_age_seconds"])
                                                        and protect.fresh(now, entry["max_quote_age_seconds"])):
                summary["skipped"].append(f"{ledger.name} {p['underlying']}: spread quote missing or stale; held")
                continue
            mark = max(0.0, min(p["width"], short.mid - protect.mid))
            p["last_mark"], p["last_mark_time"] = round(mark, 4), now.isoformat()
            p["max_profit_seen"] = max(p.get("max_profit_seen", 0.0), round((p["credit"] - mark) * 100 * p["contracts"], 2))
            why = exit_reason(p, mark, today, exits)
            if why:
                debit = max(0.0, min(p["width"], short.ask - protect.bid))     # buy back at the ask, sell protection at the bid
                closed = ledger.close(p, debit=debit, reason=why, now=now, fees=FEES.estimate(p["contracts"], legs=2))
                summary["closed"].append({"account": ledger.name, **_brief(closed)})
                append_jsonl(decisions, {"type": "exit", "account": ledger.name, "time_et": local.isoformat(),
                                         "position": closed, "mark_mid": mark})

    # 2. New spreads.
    from_t, until_t = time.fromisoformat(entry["window_from_et"]), time.fromisoformat(entry["window_until_et"])
    in_window = from_t <= local.time().replace(tzinfo=None) <= until_t and local < session[1] - timedelta(minutes=30)
    if not cfg["enabled"]:
        summary["skipped"].append("new spreads switched off (enabled: false)")
    elif not in_window:
        summary["skipped"].append(f"outside the entry window {entry['window_from_et']}-{entry['window_until_et']} ET")
    else:
        vix = vix_level(client)
        if vix is None or vix >= entry["max_vix"]:
            summary["skipped"].append(f"VIX {vix} unavailable or at/above {entry['max_vix']}: no new spreads")
        else:
            closes = closes_fn(cfg["underlyings"], today) if entry["sides"] == "trend" else {}
            for underlying in cfg["underlyings"]:
                _consider(client, underlying, cfg, ledgers, spots, closes, vix, now, summary, decisions)

    for ledger in ledgers.values():
        ledger.save()
    summary["accounts"] = {name: {"cash": round(l.data["cash"], 2), "equity": round(l.equity(), 2),
                                  "realized_pnl": round(l.data["realized_pnl"], 2), "open": len(l.open)}
                           for name, l in ledgers.items()}
    append_jsonl(decisions, {"type": "run", **summary})
    return summary


def _consider(client, underlying: str, cfg: dict, ledgers: dict[str, Ledger], spots: dict, closes: dict,
              vix: float, now: datetime, summary: dict, decisions: Path) -> None:
    entry = cfg["entry"]
    local = now.astimezone(ET)
    today = local.date()
    record = {"type": "entry_check", "underlying": underlying, "time_et": local.isoformat(), "vix": vix}
    wanted = {}
    for name, ledger in ledgers.items():
        acc = cfg["accounts"][name]
        if ledger.entered_today(underlying, today) >= entry["max_new_per_underlying_per_day"]:
            continue
        if len(ledger.open) >= acc["max_open"]:
            wanted[name] = f"already {len(ledger.open)} open (max {acc['max_open']})"; continue
        room = acc["max_total_risk_fraction"] * ledger.equity() - ledger.open_risk()
        if room < 10:
            wanted[name] = "total open risk at its limit"; continue
        wanted[name] = None
    if not wanted:
        return
    spot = spots.get(underlying, {}).get("price")
    if not spot:
        record["outcome"] = "no price for the underlying"; append_jsonl(decisions, record); return
    right, why = trend_side(closes.get(underlying, []), spot, entry["trend_sma_sessions"], entry["sides"])
    record["direction"] = why
    if right is None:
        record["outcome"] = why.get("reason"); append_jsonl(decisions, record); return
    chain = client.option_chain(underlying).get("data", {}).get("items", [])
    expiration = choose_expiration(chain, today, entry)
    if expiration is None:
        record["outcome"] = "no expiration in the DTE range"; append_jsonl(decisions, record); return
    legs = quote_legs(client, candidate_contracts(chain, expiration, right, spot))
    record.update(expiration=expiration.isoformat(), right=right, quoted=len(legs), accounts={})
    for name, blocked in wanted.items():
        ledger, acc = ledgers[name], cfg["accounts"][name]
        if blocked:
            record["accounts"][name] = blocked; continue
        if any(p["underlying"] == underlying and p["expiration"] == expiration.isoformat() for p in ledger.open):
            record["accounts"][name] = "already holds a spread in this expiration"; continue
        room = acc["max_total_risk_fraction"] * ledger.equity() - ledger.open_risk()
        budget = dict(acc, max_loss_per_trade_dollars=min(acc["max_loss_per_trade_dollars"], room))
        spread, reasons = pick_spread(legs, right=right, entry=entry, account=budget, now=now)
        if spread is None:
            record["accounts"][name] = "; ".join(reasons[:4]); continue
        short, protect = spread["short"], spread["long"]
        position = {"trade_id": str(uuid.uuid4()), "strategy_id": cfg["strategy_id"], "strategy_version": cfg["version"],
                    "underlying": underlying, "right": right,
                    "kind": "PUT_CREDIT_SPREAD" if right == "P" else "CALL_CREDIT_SPREAD",
                    "expiration": expiration.isoformat(), "short_symbol": short.symbol, "short_strike": short.strike,
                    "long_symbol": protect.symbol, "long_strike": protect.strike, "width": spread["width"],
                    "contracts": spread["contracts"], "credit": spread["credit"], "short_delta": short.delta,
                    "short_bid_ask": [short.bid, short.ask], "long_bid_ask": [protect.bid, protect.ask],
                    "max_loss_dollars": spread["max_loss_dollars"], "fees_open": FEES.estimate(spread["contracts"], legs=2),
                    "entry_time": now.isoformat(), "entry_spot": spot, "entry_vix": vix, "direction": why,
                    "last_mark": round(short.mid - protect.mid, 4)}
        ledger.add(position)
        record["accounts"][name] = "OPENED"
        summary["opened"].append({"account": name, **_brief(position)})
    append_jsonl(decisions, record)


def _brief(p: dict) -> dict:
    out = {"underlying": p["underlying"], "kind": p["kind"], "expiration": p["expiration"],
           "strikes": f"{p['short_strike']:g}/{p['long_strike']:g}", "credit": p["credit"],
           "max_loss": p["max_loss_dollars"]}
    if "realized_pnl" in p:
        out.update(exit_reason=p["exit_reason"], exit_debit=p["exit_debit"], realized_pnl=p["realized_pnl"])
    return out


def status(root: Path, data_dir: Path) -> dict[str, Any]:
    cfg = load_config(root)
    out = {"strategy": f"{cfg['strategy_id']} {cfg['version']}", "new_spreads_enabled": cfg["enabled"], "accounts": {}}
    for name, acc in cfg["accounts"].items():
        ledger = Ledger(data_dir / STATE_DIR / f"{name}.json", name, acc["starting_capital"])
        closed = ledger.data["closed"]
        out["accounts"][name] = {
            "starting_capital": ledger.data["starting_capital"], "cash": round(ledger.data["cash"], 2),
            "equity": round(ledger.equity(), 2), "realized_pnl": round(ledger.data["realized_pnl"], 2),
            "fees_paid": round(ledger.data["fees_paid"], 2), "closed_trades": len(closed),
            "wins": sum(1 for c in closed if c["realized_pnl"] > 0),
            "open": [{**_brief(p), "last_mark": p.get("last_mark"),
                      "unrealized": round((p["credit"] - p.get("last_mark", p["credit"])) * 100 * p["contracts"], 2)}
                     for p in ledger.open]}
    return out


def run_command(root: Path, cfg) -> dict[str, Any]:
    """Entry point for `trader.cli spreads-run`: one pass with live tastytrade data. PAPER only."""
    from .calendar import MarketCalendar
    from .tastytrade_client import TastytradeClient
    cfg.require_market_data_credentials()
    now = datetime.now(timezone.utc)
    session = MarketCalendar().session_bounds(now.astimezone(ET).date())
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    cache = cfg.data_dir / STATE_DIR / "daily_closes.json"
    return run_once(root, cfg.data_dir, client, now=now, session=session,
                    closes_fn=lambda symbols, today: daily_closes(cfg, symbols, today, cache))
