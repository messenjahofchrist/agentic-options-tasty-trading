"""Market-news advisor (owner requests 2026-11-10 and 2026-11-11).

Once before the open and then hourly, an AI agent reads the news (web search only) with the bot's own state: open
positions on both PAPER ledgers, today's closed trades, V2's market regime and upcoming events. Which agent does
this is set in agents.json (role "advisor"): Claude, Codex, Gemini or any other command-line bot (trader/agents.py).

It returns market sentiment, per-symbol news, a view on every open position, and three things the bot keeps score of:
  watchlist     stocks "in play" today (earnings, news, unusual volume). Logged; the bot's own rules decide entries.
  vetoes        block entries in one symbol for a stated reason until a stated time. Written to
                data/advisor/vetoes.json, which the engines read when V2's ai_veto stage is on. Block-only.
  trade_ideas   trades the rules would not take. NEVER traded: logged to data/advisor/ideas.jsonl and scored after
                the close on real prices, net of option cost (trader/scoreboard.py).
Position advice and suggested_instructions stay advice only: nothing in the engines reads them.

    python -m trader.advisor run            # one review (scheduled \\OptioBot\\Advisor, weekdays)
    python -m trader.advisor run --no-email
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import agents
from .config import load_dotenv
from .logging_utils import append_jsonl
from .notifications import email_configured, routine_email_enabled, send_email

ET = ZoneInfo("America/New_York")
ROOT = Path.cwd()
DATA = ROOT / "data"
OUT = DATA / "advisor"
LEDGERS = {"original_paper ($10K)": DATA, "small_account_paper ($1K)": DATA / "profiles" / "small_account_paper"}
LEANS = {"bullish", "bearish", "mixed", "neutral", "caution"}
ADVICE = {"hold", "exit", "tighten", "no_view"}
INSTRUCTION_TYPES = {"EXIT", "HOLD", "TIGHTEN_STOP", "BLOCK_SIDE", "BLOCK_SYMBOL", "PREFER"}
MAX_VETOES, MAX_IDEAS, MAX_WATCHLIST = 5, 5, 10
TICKER = re.compile(r"^[A-Z][A-Z.]{0,7}$")
POSITION_KEYS = ("trade_id", "underlying", "symbol", "right", "quantity", "entry_time", "entry_price", "entry_underlying",
                 "stop_underlying", "target_underlying")


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _ledger_state(data_dir: Path, today: str) -> dict[str, Any]:
    account = _json(data_dir / "paper_account.json", {}) or {}
    positions = []
    for p in account.get("positions") or []:
        item = {k: p.get(k) for k in POSITION_KEYS}
        m = p.get("v2_management") or {}
        item["management"] = {k: m.get(k) for k in ("max_favourable_r", "breakeven_armed", "breakeven_option_floor",
                                                     "runner", "runner_stop_underlying", "best_underlying")}
        positions.append(item)
    closed = [{k: t.get(k) for k in ("underlying", "reason", "realized_pnl", "timestamp")}
              for t in account.get("ledger") or [] if t.get("type") == "paper_close" and str(t.get("timestamp", "")).startswith(today)]
    return {"cash": account.get("cash"), "realized_pnl_total": account.get("realized_pnl"), "open_positions": positions,
            "closed_today": closed}


def _market_state() -> dict[str, Any]:
    snap = _json(DATA / "v2_context_snapshot.json", {}) or {}
    broad = {s: {k: v.get(k) for k in ("close", "session_open", "prior_close", "change_from_prior_close", "overnight_gap", "bias")}
             for s, v in (snap.get("broad") or {}).items()}
    sectors = {s: v.get("bias") for s, v in (snap.get("sectors") or {}).items()}
    regime = snap.get("regime") or {}
    return {"snapshot_utc": snap.get("generated_at_utc"), "broad": broad, "sector_bias": sectors,
            "regime": regime.get("regime"), "allowed_directions": regime.get("allowed_directions"),
            "vix": (regime.get("inputs") or {}).get("vix"), "events_upcoming": snap.get("events_upcoming")}


def _earnings_dates(limit_bytes: int = 3_000_000) -> dict[str, Any]:
    """Each symbol's next earnings date as the bot itself has it (tastytrade market-metrics, via V2's event check)."""
    out: dict[str, Any] = {}
    path = DATA / "v2_decisions.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit_bytes))
            lines = handle.read().decode("utf-8", "ignore").splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        earnings = (((record.get("stages") or {}).get("events") or {}).get("earnings") or {})
        if record.get("symbol") and earnings.get("report_date"):
            out[record["symbol"]] = {k: earnings.get(k) for k in ("report_date", "time_of_day", "estimated")}
    return out


def gather(now: datetime) -> dict[str, Any]:
    today = now.astimezone(ET).date().isoformat()
    small_profile = _json(ROOT / "profiles" / "small_account_universe.json", {}) or {}
    return {"now_et": now.astimezone(ET).isoformat(timespec="minutes"),
            "trade_symbols": small_profile.get("symbols") or ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "AMZN", "GOOGL", "PLTR",
                                                              "NFLX", "INTC", "UBER"],
            "ledgers": {name: _ledger_state(path, today) for name, path in LEDGERS.items()},
            "market": _market_state(), "earnings_dates_from_bot": _earnings_dates()}


def prompt(state: dict[str, Any]) -> str:
    return (
        "You are the market-news advisor for an automated PAPER options bot. It buys 1-2 week calls and puts on "
        "intraday opening-range breakouts and sells everything by the close. It cannot read news; you can. You have "
        "web search only. You cannot trade: your output is logged, emailed to the owner, and scored "
        "later against what actually happened, so be specific, honest about uncertainty, and cite sources.\n\n"
        "Do at most 8 searches. Cover: (1) today's macro drivers (Fed, inflation, yields, economic releases, geopolitics) "
        "and how US stocks are trading right now; (2) fresh news on every symbol with an open position; (3) any trade "
        "symbol with notable news in the last 24 hours (earnings, guidance, analyst moves, legal or management news). "
        "Ignore news older than 3 days unless it is still driving the stock. Say 'no news found' rather than guessing. "
        "When a stock moved a lot, find its specific cause (company, legal, management or regulatory news) instead of a "
        "sector narrative. Use earnings_dates_from_bot for earnings dates; do not guess them.\n\n"
        "For each open position, say hold / exit / tighten / no_view with the reason. 'hold' means the news supports "
        "the position's direction; 'exit' means the news is against it. The bot's own exits (stop, breakeven, runner "
        "stop, end of day) still apply; you are saying whether you would override them.\n\n"
        "Three lists are scored every night against real prices:\n"
        "- watchlist: up to 10 US stocks or ETFs that are 'in play' today (earnings reaction, company news, unusual "
        "pre-market or opening volume), in or outside trade_symbols. Give the catalyst.\n"
        "- vetoes: up to 5. A veto BLOCKS the bot from opening new trades in one of trade_symbols until a time today. "
        "Use it only for a concrete, sourced, company-specific reason the bot cannot see (earnings tonight, a pending "
        "ruling or announcement at a known time, a halt, a deal). Never veto on general market mood. side null blocks "
        "both directions. This is the one output the bot acts on, so an empty list is the normal answer.\n"
        "- trade_ideas: up to 5 intraday trades in trade_symbols that the bot's breakout rules would probably NOT "
        "take, where news or sentiment gives you a directional view for the rest of today. They are never traded; "
        "each is scored from the minute you give it to the close. Give stop_pct and target_pct as percent moves of "
        "the STOCK from the current price (for example 0.6 and 1.2). An empty list is fine; do not force ideas.\n\n"
        "Return ONLY one JSON object, no markdown, with exactly these keys:\n"
        '{"as_of_et": str, "market_sentiment": {"lean": "bullish|bearish|mixed|neutral", "confidence": "low|medium|high", '
        '"summary": str, "drivers": [str]}, '
        '"symbols": [{"symbol": str, "lean": "bullish|bearish|neutral|caution", "headline": str, "why_it_matters": str, '
        '"sources": [url]}], '
        '"positions": [{"ledger": str, "trade_id": str, "underlying": str, "advice": "hold|exit|tighten|no_view", '
        '"reason": str, "sources": [url]}], '
        '"suggested_instructions": [{"type": "EXIT|HOLD|TIGHTEN_STOP|BLOCK_SIDE|BLOCK_SYMBOL|PREFER", "symbol": str, '
        '"side": "CALL|PUT|null", "until_et": "HH:MM", "reason": str}], '
        '"watchlist": [{"symbol": str, "direction": "up|down|either", "catalyst": str, "sources": [url]}], '
        '"vetoes": [{"symbol": str, "side": "CALL|PUT|null", "until_et": "HH:MM", "reason": str, "sources": [url]}], '
        '"trade_ideas": [{"symbol": str, "side": "CALL|PUT", "thesis": str, "stop_pct": number, "target_pct": number, '
        '"confidence": "low|medium|high", "sources": [url]}], '
        '"upcoming_risks": [str]}\n'
        "Only list symbols you found news for. suggested_instructions may be empty; include one only when the news "
        "gives a concrete reason.\n\nBOT_STATE_JSON:\n" + json.dumps(state, default=str)
    )


def check(data: Any) -> dict[str, Any]:
    """Structural check of an agent's reply. The three scored lists may be absent (treated as empty)."""
    required = {"market_sentiment", "symbols", "positions", "suggested_instructions", "upcoming_risks"}
    if not isinstance(data, dict) or required - set(data):
        raise ValueError(f"advisor JSON is missing {sorted(required - set(data if isinstance(data, dict) else {}))}")
    if (data["market_sentiment"] or {}).get("lean") not in LEANS:
        raise ValueError("advisor market_sentiment.lean is invalid")
    for key in ("symbols", "positions", "suggested_instructions", "upcoming_risks"):
        if not isinstance(data[key], list):
            raise ValueError(f"advisor {key} must be a list")
    for p in data["positions"]:
        if p.get("advice") not in ADVICE:
            raise ValueError(f"advisor position advice is invalid: {p.get('advice')!r}")
    for i in data["suggested_instructions"]:
        if i.get("type") not in INSTRUCTION_TYPES:
            raise ValueError(f"advisor instruction type is invalid: {i.get('type')!r}")
    for key in ("watchlist", "vetoes", "trade_ideas"):
        if not isinstance(data.setdefault(key, []), list):
            raise ValueError(f"advisor {key} must be a list")
    return data


def parse(raw: str) -> dict[str, Any]:
    try:
        return check(agents.extract_json(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"advisor returned malformed JSON: {exc}") from exc


def _number(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low < number <= high else None


def clean_lists(advice: dict[str, Any], trade_symbols: list[str], now: datetime) -> dict[str, list]:
    """Keep only well-formed watchlist / veto / idea items; everything dropped is listed with the reason. Vetoes and
    ideas must be in the bot's own symbols; a veto ends today at its stated time, never later than 16:00 ET."""
    local = now.astimezone(ET)
    close = datetime.combine(local.date(), time(16, 0), ET)
    out: dict[str, list] = {"watchlist": [], "vetoes": [], "trade_ideas": [], "dropped": []}

    def drop(kind: str, item: Any, why: str) -> None:
        out["dropped"].append({"list": kind, "item": item, "why": why})

    for item in advice.get("watchlist") or []:
        symbol = str(item.get("symbol") or "").upper() if isinstance(item, dict) else ""
        if not TICKER.match(symbol) or not str(item.get("catalyst") or "").strip():
            drop("watchlist", item, "needs a ticker and a catalyst")
        elif len(out["watchlist"]) >= MAX_WATCHLIST:
            drop("watchlist", item, f"more than {MAX_WATCHLIST}")
        else:
            direction = item.get("direction") if item.get("direction") in ("up", "down", "either") else "either"
            out["watchlist"].append({"symbol": symbol, "direction": direction, "catalyst": str(item["catalyst"])[:400],
                                     "sources": list(item.get("sources") or [])[:5], "in_bot_symbols": symbol in trade_symbols})
    for item in advice.get("vetoes") or []:
        if not isinstance(item, dict):
            drop("vetoes", item, "not an object")
            continue
        symbol, side = str(item.get("symbol") or "").upper(), item.get("side")
        side = None if side in (None, "", "null", "BOTH") else side
        try:
            until = datetime.combine(local.date(), time.fromisoformat(str(item.get("until_et"))), ET)
        except ValueError:
            until = None
        if symbol not in trade_symbols:
            drop("vetoes", item, "not one of the bot's symbols")
        elif side not in (None, "CALL", "PUT"):
            drop("vetoes", item, "side must be CALL, PUT or null")
        elif not str(item.get("reason") or "").strip():
            drop("vetoes", item, "no reason given")
        elif until is None or until <= local:
            drop("vetoes", item, "until_et must be a later time today (HH:MM ET)")
        elif len(out["vetoes"]) >= MAX_VETOES:
            drop("vetoes", item, f"more than {MAX_VETOES}")
        else:
            out["vetoes"].append({"symbol": symbol, "side": side,
                                  "until_utc": min(until, close).astimezone(timezone.utc).isoformat(),
                                  "reason": str(item["reason"])[:400], "sources": list(item.get("sources") or [])[:5]})
    for item in advice.get("trade_ideas") or []:
        if not isinstance(item, dict):
            drop("trade_ideas", item, "not an object")
            continue
        symbol = str(item.get("symbol") or "").upper()
        stop, target = _number(item.get("stop_pct"), 0.05, 5), _number(item.get("target_pct"), 0.05, 10)
        if symbol not in trade_symbols:
            drop("trade_ideas", item, "not one of the bot's symbols (cannot be scored)")
        elif item.get("side") not in ("CALL", "PUT"):
            drop("trade_ideas", item, "side must be CALL or PUT")
        elif stop is None or target is None:
            drop("trade_ideas", item, "stop_pct / target_pct must be percent moves of the stock (0.05-5 and 0.05-10)")
        elif not str(item.get("thesis") or "").strip():
            drop("trade_ideas", item, "no thesis given")
        elif len(out["trade_ideas"]) >= MAX_IDEAS:
            drop("trade_ideas", item, f"more than {MAX_IDEAS}")
        else:
            confidence = item.get("confidence") if item.get("confidence") in ("low", "medium", "high") else None
            out["trade_ideas"].append({"symbol": symbol, "side": item["side"], "thesis": str(item["thesis"])[:600],
                                       "stop_pct": stop, "target_pct": target, "confidence": confidence,
                                       "sources": list(item.get("sources") or [])[:5]})
    return out


def save_scored_lists(record: dict[str, Any], lists: dict[str, list], out_dir: Path) -> None:
    """Write the veto file the engines read and append the watchlist and ideas to their logs."""
    stamp, when, agent = record["id"], record["timestamp_utc"], record.get("agent")
    logged = datetime.now(timezone.utc).isoformat()   # when the reply arrived: ideas are scored from here, not from the start
    out_dir.mkdir(parents=True, exist_ok=True)
    document = {"generated_at_utc": when, "agent": agent, "note_id": stamp, "vetoes": lists["vetoes"]}
    temporary = out_dir / "vetoes.json.tmp"
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(out_dir / "vetoes.json")
    for veto in lists["vetoes"]:
        append_jsonl(out_dir / "vetoes_log.jsonl", {"note_id": stamp, "timestamp_utc": when, "agent": agent, **veto})
    for item in lists["watchlist"]:
        append_jsonl(out_dir / "watchlist.jsonl", {"note_id": stamp, "timestamp_utc": when, "logged_at_utc": logged,
                                                   "agent": agent, **item})
    for number, idea in enumerate(lists["trade_ideas"], 1):
        append_jsonl(out_dir / "ideas.jsonl", {"id": f"{stamp}-{number}", "note_id": stamp, "timestamp_utc": when,
                                               "logged_at_utc": logged, "agent": agent, **idea})


def summary_text(note: dict[str, Any]) -> str:
    advice = note["advice"]
    s = advice["market_sentiment"]
    lines = [f"Market: {s.get('lean')} ({s.get('confidence')} confidence). {s.get('summary', '')}"]
    lines += [f"  - {d}" for d in s.get("drivers") or []]
    if advice["positions"]:
        lines.append("\nOpen positions:")
        lines += [f"  {p.get('underlying')} [{p.get('ledger')}]: {p.get('advice').upper()}: {p.get('reason')}"
                  for p in advice["positions"]]
    if advice["symbols"]:
        lines.append("\nSymbol news:")
        lines += [f"  {x.get('symbol')}: {x.get('lean')}: {x.get('headline')}" for x in advice["symbols"]]
    lists = note.get("scored_lists") or {}
    if lists.get("vetoes"):
        lines.append("\nVetoes (the bot will not open new trades here while V2's ai_veto stage is on):")
        lines += [f"  {v['symbol']} {v['side'] or 'both sides'} until "
                  f"{datetime.fromisoformat(v['until_utc']).astimezone(ET):%H:%M} ET: {v['reason']}" for v in lists["vetoes"]]
    if lists.get("watchlist"):
        lines.append("\nIn play today (logged and scored; the bot's own rules decide entries):")
        lines += [f"  {w['symbol']} ({w['direction']}): {w['catalyst']}" for w in lists["watchlist"]]
    if lists.get("trade_ideas"):
        lines.append("\nTrade ideas (NOT traded; scored after the close):")
        lines += [f"  {i['symbol']} {i['side']} stop {i['stop_pct']}% target {i['target_pct']}%: {i['thesis']}"
                  for i in lists["trade_ideas"]]
    if advice["suggested_instructions"]:
        lines.append("\nSuggested instructions (NOT applied; advice only):")
        lines += [f"  {i.get('type')} {i.get('symbol')} {i.get('side') or ''} until {i.get('until_et')}: {i.get('reason')}"
                  for i in advice["suggested_instructions"]]
    if advice["upcoming_risks"]:
        lines.append("\nUpcoming risks:")
        lines += [f"  - {r}" for r in advice["upcoming_risks"]]
    return "\n".join(lines)


def worth_emailing(record: dict[str, Any]) -> bool:
    """Owner 2026-11-11: fewer emails. A run is emailed when it failed or when the agent decided or advised
    something: a veto, exit/tighten advice on an open position, or a trade idea. Routine notes stay on disk."""
    if routine_email_enabled("advisor_hourly", ROOT) or record.get("status") != "complete":
        return True
    lists = record.get("scored_lists") or {}
    acted = any(p.get("advice") in ("exit", "tighten") for p in record["advice"].get("positions") or [])
    return bool(lists.get("vetoes") or lists.get("trade_ideas") or acted)


def run(*, email: bool = True, timeout: int = 420) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = gather(now)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    record: dict[str, Any] = {"timestamp_utc": now.isoformat(), "id": stamp, "state": state}
    error = None
    try:
        reply = agents.ask_json("advisor", prompt(state), validate=check, timeout=timeout, root=ROOT)
        record.update(advice=reply.data, agent=reply.agent, attempts=reply.attempts, agent_errors=reply.errors)
    except agents.AgentError as exc:
        error = f"{type(exc).__name__}: {exc}"
    OUT.mkdir(parents=True, exist_ok=True)
    if "advice" not in record:
        # A failed run writes no veto file: the previous one simply ages out (ai_veto.max_age_minutes).
        record.update(status="failed", error=error)
    else:
        record["status"] = "complete"
        record["scored_lists"] = clean_lists(record["advice"], list(state["trade_symbols"]), now)
        save_scored_lists(record, record["scored_lists"], OUT)
    (OUT / "notes").mkdir(parents=True, exist_ok=True)
    (OUT / "notes" / f"{stamp}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    append_jsonl(OUT / "advice.jsonl", {k: record.get(k) for k in ("timestamp_utc", "id", "status", "error", "agent", "advice",
                                                                    "scored_lists")})
    record["email"] = None
    if email and email_configured() and not worth_emailing(record):
        record["email"] = "skipped: nothing to act on (notifications.json advisor_hourly is false)"
    elif email and email_configured():
        et = now.astimezone(ET).strftime("%a %H:%M ET")
        subject = f"OptioBot advisor {et} ({record.get('agent') or 'no agent'}): " + (
            record["advice"]["market_sentiment"]["lean"] if record["status"] == "complete" else "FAILED")
        body = summary_text(record) if record["status"] == "complete" else f"The advisor run failed: {error}"
        try:
            send_email(subject, body + f"\n\nFull note: data/advisor/notes/{stamp}.json")
            record["email"] = "sent"
        except Exception as exc:  # email trouble must not lose the note
            record["email"] = f"failed: {type(exc).__name__}"
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.advisor")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="one news/sentiment review by the agent set in agents.json")
    r.add_argument("--no-email", action="store_true")
    r.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args(argv)
    load_dotenv()
    record = run(email=not args.no_email, timeout=args.timeout)
    print(json.dumps({k: record.get(k) for k in ("id", "status", "agent", "error", "email")}))
    if record["status"] == "complete":
        print(summary_text(record))
    return 0 if record["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
