"""One scoreboard for every idea (review 2026-11-11): each is judged on real prices, net of option cost, before it
is allowed to change what the bot trades. Runs after the close, once the option recorder has saved the day's quotes.
It reads ledgers and logs and writes reports; it never touches the engines, ledgers or strategy files.

  1 shorter-dated contracts   every real trade re-priced on recorded quotes with the contract the bot bought (7-21
                              days out) and with a 1-6 day contract, same entry and exit minutes, two fill models
  2 cost gate                 what a cost limit in R would have kept and dropped among the real trades
  3 agent trade ideas         ideas the advisor logged (never traded): stock-move R, R net of option cost, option P&L
  4 agent vetoes              signals blocked (or flagged) by an advisor veto and what they would have done
  5 agent watchlist           did "in play" names move more than the bot's other symbols
  6 wide SPY opening range    logged lead from the review: results of real trades on wide-open days vs other days

    python -m trader.scoreboard run [--no-email] [--skip-cost-table]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cost_model
from .config import load_dotenv
from .quote_history import ET, QuoteHistory, select_contract

FEES_OPEN, FEES_CLOSE, SLIP = 1.12, 0.13, 0.02     # tastytrade per contract, as the PAPER ledger charges them
ROUND_TRIP_EXTRAS = FEES_OPEN + FEES_CLOSE + 2 * SLIP
TRIAL_START = date(2026, 11, 11)                    # shorter-dated contract trial (pass mark set in the review)
TRIAL_SESSIONS = 10
LEDGERS = {"$10K": ("data/paper_account.json", None),
           "$1K": ("data/profiles/small_account_paper/paper_account.json", 350.0)}
SIGNAL_OUTCOMES = {"$10K": "data/v2_signal_outcomes.jsonl", "$1K": "data/profiles/small_account_paper/v2_signal_outcomes.jsonl"}
COST_LIMITS = (0.20, 0.30)
WIDE_OPEN_FRACTION = 0.30                           # SPY 15-minute opening range above this share of its usual daily range


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _when(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _profit_factor(values: list[float]) -> float | None:
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return round(gains / losses, 2) if losses else None


def _stats(values: list[float], digits: int = 2) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "total": round(sum(values), digits), "average": round(sum(values) / len(values), digits),
            "win_rate": round(sum(v > 0 for v in values) / len(values), 3), "profit_factor": _profit_factor(values)}


# --- real trades -------------------------------------------------------------------------------------------
def closed_trades(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Every closed long-option trade in a PAPER ledger, matched buy to close by trade id."""
    buys: dict[str, dict] = {}
    trades = []
    for row in ledger.get("ledger") or []:
        if row.get("type") == "fill" and row.get("side") == "BUY":
            buys[row.get("trade_id")] = row
        elif row.get("type") == "paper_close" and row.get("trade_id") in buys:
            buy = buys.pop(row["trade_id"])
            occ = str(buy.get("symbol") or "")
            forecast = row.get("forecast") or {}
            quote = buy.get("quote") or {}
            trades.append({"trade_id": row["trade_id"], "symbol": row.get("underlying"), "occ": occ,
                           "side": "CALL" if occ.strip()[-9:-8] == "C" else "PUT",
                           "entry_time": buy["timestamp"], "exit_time": row["timestamp"], "pnl": float(row["realized_pnl"]),
                           "premium": float(buy["fill_price"]) * 100, "entry_bid": quote.get("bid"), "entry_ask": quote.get("ask"),
                           "delta": forecast.get("entry_delta"), "entry_underlying": forecast.get("entry_underlying"),
                           "stop_underlying": forecast.get("stop_underlying"), "version": row.get("strategy_version"),
                           "exit_reason": row.get("reason")})
    return trades


def fill_from_decisions(trades: list[dict], decision_files: list[Path]) -> None:
    """Older ledger rows do not carry the entry delta and stop; take them from the engine's own approved-candidate
    records (same symbol, same minute)."""
    missing = {(t["symbol"], t["entry_time"][:16]): t for t in trades
               if t.get("delta") is None or t.get("stop_underlying") is None or t.get("entry_bid") is None}
    if not missing:
        return
    for path in decision_files:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"outcome": "APPROVED"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trade = missing.get((record.get("symbol"), str(record.get("timestamp"))[:16]))
                entry = record.get("entry") or {}
                option = entry.get("option") or {}
                if trade is None or not option:
                    continue
                trade["delta"] = trade.get("delta") if trade.get("delta") is not None else option.get("delta")
                for key in ("entry_underlying", "stop_underlying"):
                    trade[key] = trade.get(key) if trade.get(key) is not None else entry.get(key)
                trade["entry_bid"] = trade.get("entry_bid") if trade.get("entry_bid") is not None else option.get("bid")
                trade["entry_ask"] = trade.get("entry_ask") if trade.get("entry_ask") is not None else option.get("ask")


def trade_cost_r(trade: dict[str, Any]) -> float | None:
    """Entry-time cost in R, as the live cost gate computes it (trader.cost_model)."""
    try:
        cost = cost_model.round_trip_cost(float(trade["entry_bid"]), float(trade["entry_ask"]), round_trip_fees=1.40,
                                          slippage_per_contract=SLIP)
        return cost_model.cost_in_r(cost, delta=float(trade["delta"]),
                                    stop_distance=float(trade["entry_underlying"]) - float(trade["stop_underlying"]))
    except (TypeError, ValueError):
        return None


# --- 1 shorter-dated contracts ------------------------------------------------------------------------------
CONTRACT_RULES = {"standard": {"min_dte": 7, "max_dte": 21}, "short": {"min_dte": 1, "max_dte": 6, "nearest_expiry_first": True}}


def reprice(trade: dict[str, Any], histories: dict[str, QuoteHistory], premium_cap: float | None = None) -> dict[str, Any] | None:
    """The same entry and exit minutes priced with both contract choices. None when the day was not recorded."""
    t_in, t_out = _when(trade["entry_time"]), _when(trade["exit_time"])
    day = t_in.astimezone(ET).date()
    base = histories["open"]
    if not base.has(day, trade["symbol"]):
        return None
    chain = base.chain(day, trade["symbol"], t_in)
    row: dict[str, Any] = {"trade_id": trade["trade_id"], "symbol": trade["symbol"], "side": trade["side"],
                           "entry_time": trade["entry_time"], "actual_pnl": round(trade["pnl"], 2),
                           "actual_premium": round(trade["premium"], 2)}
    for name, rule in CONTRACT_RULES.items():
        quote = select_contract(chain, trade["side"], day, **rule)
        if quote is None:
            row[name] = None
            continue
        priced: dict[str, Any] = {"occ": quote.occ, "dte": (quote.expiration - day).days, "premium": round(quote.ask * 100, 2),
                                  "fits_cap": premium_cap is None or quote.ask * 100 + ROUND_TRIP_EXTRAS <= premium_cap}
        for fill, history in histories.items():
            a, b = history.quote(day, trade["symbol"], t_in, quote.occ), history.quote(day, trade["symbol"], t_out, quote.occ)
            if a is None or b is None:
                priced = None
                break
            priced[f"pnl_{fill}"] = round((b.bid - a.ask) * 100 - ROUND_TRIP_EXTRAS, 2)
            if fill == "open":
                priced["cost"] = round(a.spread * 50 + b.spread * 50 + ROUND_TRIP_EXTRAS, 2)
        row[name] = priced
    return row


def shorter_dated(trades: list[dict], histories: dict[str, QuoteHistory], premium_cap: float | None) -> dict[str, Any]:
    rows = [r for r in (reprice(t, histories, premium_cap) for t in trades) if r]
    both = [r for r in rows if r["standard"] and r["short"]]

    def summary(sample: list[dict]) -> dict[str, Any]:
        if not sample:
            return {"n": 0}
        out: dict[str, Any] = {"n": len(sample), "actual_total": round(sum(r["actual_pnl"] for r in sample), 2)}
        for fill in ("open", "adverse"):
            for name in ("standard", "short"):
                out[f"{name}_{fill}"] = round(sum(r[name][f"pnl_{fill}"] for r in sample), 2)
        out["short_better_share"] = round(sum(r["short"]["pnl_open"] > r["standard"]["pnl_open"] for r in sample) / len(sample), 3)
        out["average_premium"] = {name: round(statistics.mean(r[name]["premium"] for r in sample), 2) for name in ("standard", "short")}
        out["average_cost"] = {name: round(statistics.mean(r[name]["cost"] for r in sample), 2) for name in ("standard", "short")}
        return out

    trial = [r for r in both if _when(r["entry_time"]).astimezone(ET).date() >= TRIAL_START]
    result = {"priced_trades": len(rows), "all_recorded": summary(both), "trial": summary(trial), "rows": rows,
              "trial_start": TRIAL_START.isoformat()}
    t = result["trial"]
    if t.get("n"):
        result["trial_pass_mark"] = {"rule": "short better on at least 60% of the same trades and a higher total under both fill models",
                                     "better_share_ok": t["short_better_share"] >= 0.60,
                                     "total_ok": t["short_open"] > t["standard_open"] and t["short_adverse"] > t["standard_adverse"]}
    if premium_cap is not None:
        result["short_fits_cap_share"] = (round(sum(bool(r["short"] and r["short"]["fits_cap"]) for r in rows) / len(rows), 3)
                                          if rows else None)
    return result


# --- 2 cost gate ----------------------------------------------------------------------------------------------
def cost_gate(trades: list[dict]) -> dict[str, Any]:
    priced = [(trade_cost_r(t), t) for t in trades]
    priced = [(c, t) for c, t in priced if c is not None]
    out: dict[str, Any] = {"trades_with_cost": len(priced), "of_trades": len(trades)}
    if priced:
        out["median_cost_r"] = round(statistics.median(c for c, _ in priced), 3)
    for limit in COST_LIMITS:
        kept = [t["pnl"] for c, t in priced if c <= limit]
        dropped = [t["pnl"] for c, t in priced if c > limit]
        out[f"limit_{limit:.2f}R"] = {"kept": _stats(kept), "dropped": _stats(dropped)}
    return out


# --- 3 agent trade ideas ----------------------------------------------------------------------------------------
def score_idea(idea: dict[str, Any], history: QuoteHistory, table: dict | None) -> dict[str, Any] | None:
    """An idea becomes a trade at the first minute after it was logged, on the stock's recorded one-minute bars.
    Exits follow the bot's own convention: a bar CLOSE through the stop or target, else the session close."""
    logged = _when(idea.get("logged_at_utc") or idea["timestamp_utc"])
    day = logged.astimezone(ET).date()
    symbol = idea["symbol"]
    if not history.has(day, symbol):
        return None
    bars = [b for b in history.underlying_bars(day, symbol) if b[0] >= logged.astimezone(ET)]
    if len(bars) < 2:
        return {"id": idea["id"], "symbol": symbol, "side": idea["side"], "agent": idea.get("agent"), "status": "too_late_in_session"}
    direction = 1 if idea["side"] == "CALL" else -1
    entry_time, entry = bars[0][0], bars[0][1]
    stop_distance = entry * float(idea["stop_pct"]) / 100
    stop, target = entry - direction * stop_distance, entry + direction * entry * float(idea["target_pct"]) / 100
    result, exit_time, exit_price = "session_close", bars[-1][0] + timedelta(minutes=1), bars[-1][4]
    for start, _open, _high, _low, close in bars:
        if (close - stop) * direction <= 0:
            result, exit_time, exit_price = "stop", start + timedelta(minutes=1), close
            break
        if (close - target) * direction >= 0:
            result, exit_time, exit_price = "target", start + timedelta(minutes=1), close
            break
    gross_r = (exit_price - entry) * direction / stop_distance
    net = cost_model.net_r(gross_r, cost_bps=cost_model.symbol_cost_bps(table, symbol), stop_fraction=stop_distance / entry)
    scored = {"id": idea["id"], "symbol": symbol, "side": idea["side"], "agent": idea.get("agent"), "status": "scored",
              "date": day.isoformat(), "entry_time": entry_time.isoformat(), "entry": entry, "exit": exit_price, "result": result,
              "gross_r": round(gross_r, 3), "net_r": round(net, 3) if net is not None else None, "option_pnl": None,
              "thesis": idea.get("thesis"), "confidence": idea.get("confidence")}
    quote = select_contract(history.chain(day, symbol, entry_time), idea["side"], day, **CONTRACT_RULES["standard"])
    if quote is not None:
        a = history.quote(day, symbol, entry_time, quote.occ)
        b = history.quote(day, symbol, min(exit_time, bars[-1][0]), quote.occ)
        if a is not None and b is not None:
            scored["option_pnl"] = round((b.bid - a.ask) * 100 - ROUND_TRIP_EXTRAS, 2)
            scored["option_premium"] = round(a.ask * 100, 2)
    return scored


def agent_ideas(ideas: list[dict], history: QuoteHistory, table: dict | None) -> dict[str, Any]:
    rows = [r for r in (score_idea(i, history, table) for i in ideas) if r]
    scored = [r for r in rows if r["status"] == "scored"]
    by_agent: dict[str, Any] = {}
    for agent in sorted({str(r.get("agent")) for r in scored}):
        mine = [r for r in scored if str(r.get("agent")) == agent]
        by_agent[agent] = {"gross_r": _stats([r["gross_r"] for r in mine]),
                           "net_r": _stats([r["net_r"] for r in mine if r["net_r"] is not None]),
                           "option_pnl": _stats([r["option_pnl"] for r in mine if r["option_pnl"] is not None])}
    return {"logged": len(ideas), "scored": len(scored), "awaiting_recorded_prices": len(ideas) - len(rows),
            "by_agent": by_agent, "rows": rows,
            "bar": "an agent's ideas earn a small paper allocation only after at least 50 scored ideas over 4+ weeks "
                   "with a positive net R total"}


# --- 4 agent vetoes ---------------------------------------------------------------------------------------------
def agent_vetoes(root: Path, veto_log: list[dict]) -> dict[str, Any]:
    """Signals an advisor veto blocked, with what they would have done (the engines' own hypothetical outcomes).
    A negative total means the vetoes kept the bot out of losing trades."""
    out: dict[str, Any] = {"vetoes_issued": len(veto_log), "by_ledger": {}}
    for name, rel in SIGNAL_OUTCOMES.items():
        blocked = [o for o in _jsonl(root / rel) if any("ai_veto" in str(r) for r in o.get("reasons") or [])]
        values = [float(o["hypothetical_underlying_r"]) for o in blocked if o.get("hypothetical_underlying_r") is not None]
        out["by_ledger"][name] = {"blocked_signals": len(blocked), "would_have_made_r": _stats(values, 3),
                                  "rows": [{k: o.get(k) for k in ("symbol", "side", "decided_at", "hypothetical_result",
                                                                  "hypothetical_underlying_r", "reasons")} for o in blocked[-50:]]}
    return out


# --- 5 agent watchlist ------------------------------------------------------------------------------------------
def agent_watchlist(items: list[dict], history: QuoteHistory) -> dict[str, Any]:
    """For watchlist names the recorder covers: the stock's move from the note to the close, against the same-window
    move of the recorded symbols that were NOT on that note's list."""
    rows, outside = [], 0
    for note_id in sorted({i["note_id"] for i in items}):
        mine = [i for i in items if i["note_id"] == note_id]
        logged = _when(mine[0].get("logged_at_utc") or mine[0]["timestamp_utc"])
        day = logged.astimezone(ET).date()
        listed = {i["symbol"] for i in mine}

        def move(symbol: str) -> float | None:
            if not history.has(day, symbol):
                return None
            bars = [b for b in history.underlying_bars(day, symbol) if b[0] >= logged.astimezone(ET)]
            return (bars[-1][4] / bars[0][1] - 1) * 100 if len(bars) >= 2 else None

        try:
            others = [abs(m) for m in (move(s) for s in history.symbols(day) if s not in listed) if m is not None]
        except OSError:
            others = []
        for item in mine:
            m = move(item["symbol"])
            if m is None:
                outside += 1
                continue
            signed = m if item["direction"] == "up" else -m if item["direction"] == "down" else None
            rows.append({"note_id": note_id, "date": day.isoformat(), "symbol": item["symbol"], "direction": item["direction"],
                         "agent": item.get("agent"), "move_to_close_pct": round(m, 3),
                         "move_in_stated_direction_pct": round(signed, 3) if signed is not None else None,
                         "other_symbols_median_abs_move_pct": round(statistics.median(others), 3) if others else None})
    in_direction = [r["move_in_stated_direction_pct"] for r in rows if r["move_in_stated_direction_pct"] is not None]
    bigger = [abs(r["move_to_close_pct"]) > r["other_symbols_median_abs_move_pct"] for r in rows
              if r["other_symbols_median_abs_move_pct"] is not None]
    return {"logged": len(items), "scored": len(rows), "not_recorded_symbols": outside,
            "move_in_stated_direction_pct": _stats(in_direction, 3),
            "moved_more_than_other_symbols_share": round(sum(bigger) / len(bigger), 3) if bigger else None,
            "rows": rows[-60:],
            "note": "names outside the recorded symbols cannot be scored yet (no price history is saved for them)"}


# --- 6 wide SPY opening range -----------------------------------------------------------------------------------
def wide_open_days(trades: list[dict], history: QuoteHistory) -> dict[str, Any]:
    """Review lead, logged only: SPY's first 15 minutes already covering a large share of its usual daily range.
    Usual range = average high-low of up to 14 earlier recorded sessions (at least 5)."""
    ranges: dict[date, tuple[float, float]] = {}
    for day in history.days():
        if history.has(day, "SPY"):
            bars = history.underlying_bars(day, "SPY")
            if len(bars) > 20:
                ranges[day] = (max(b[2] for b in bars[:15]) - min(b[3] for b in bars[:15]),
                               max(b[2] for b in bars) - min(b[3] for b in bars))
    days = sorted(ranges)
    wide: dict[date, bool] = {}
    for index, day in enumerate(days):
        earlier = [ranges[d][1] for d in days[max(0, index - 14):index]]
        if len(earlier) >= 5:
            wide[day] = ranges[day][0] / statistics.mean(earlier) > WIDE_OPEN_FRACTION
    on_wide = [t["pnl"] for t in trades if wide.get(_when(t["entry_time"]).astimezone(ET).date()) is True]
    on_other = [t["pnl"] for t in trades if wide.get(_when(t["entry_time"]).astimezone(ET).date()) is False]
    return {"threshold": WIDE_OPEN_FRACTION, "classified_sessions": len(wide), "wide_sessions": sum(wide.values()),
            "trades_on_wide_days": _stats(on_wide), "trades_on_other_days": _stats(on_other),
            "status": "logged lead; lost in every period of the 5-month history but was not confirmed on new data"}


# --- report -----------------------------------------------------------------------------------------------------
def build(root: Path, *, rebuild_cost_table: bool = True) -> dict[str, Any]:
    histories = {"open": QuoteHistory(root, "open"), "adverse": QuoteHistory(root, "adverse")}
    history = histories["open"]
    if rebuild_cost_table:
        cost_model.write_table(root, cost_model.build_table(root))
    table = cost_model.load_table(root)
    days = history.days()
    board: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                             "recorded_sessions": {"first": days[0].isoformat() if days else None,
                                                   "last": days[-1].isoformat() if days else None, "count": len(days)},
                             "option_cost_table": None if not table else {
                                 s: {k: v["cost_bps"] for k, v in row.items()} for s, row in table["symbols"].items()},
                             "accounts": {}}
    all_trades: list[dict] = []
    for name, (rel, cap) in LEDGERS.items():
        trades = closed_trades(_json(root / rel, {}) or {})
        fill_from_decisions(trades, sorted((root / rel).parent.glob("v2_decisions.jsonl*")))
        all_trades += trades if name == "$10K" else []
        board["accounts"][name] = {"closed_trades": len(trades), "realized_total": round(sum(t["pnl"] for t in trades), 2),
                                   "shorter_dated_contracts": shorter_dated(trades, histories, cap),
                                   "cost_gate": cost_gate(trades)}
    advisor_dir = root / "data" / "advisor"
    sections = {
        "agent_trade_ideas": lambda: agent_ideas(_jsonl(advisor_dir / "ideas.jsonl"), history, table),
        "agent_vetoes": lambda: agent_vetoes(root, _jsonl(advisor_dir / "vetoes_log.jsonl")),
        "agent_watchlist": lambda: agent_watchlist(_jsonl(advisor_dir / "watchlist.jsonl"), history),
        "wide_spy_open": lambda: wide_open_days(all_trades, history),
    }
    board["errors"] = {}
    for name, make in sections.items():
        try:   # one broken section must not lose the rest of the report
            board[name] = make()
        except Exception as exc:
            board[name] = None
            board["errors"][name] = f"{type(exc).__name__}: {exc}"[:300]
    return board


def _money(value: Any) -> str:
    return "n/a" if value is None else f"{'-' if value < 0 else '+'}${abs(value):,.0f}"


def render(board: dict[str, Any]) -> str:
    rec = board["recorded_sessions"]
    lines = [f"# OptioBot scoreboard, {datetime.fromisoformat(board['generated_at_utc']).astimezone(ET):%Y-%m-%d %H:%M ET}", "",
             f"Recorded option quotes: {rec['count']} sessions, {rec['first']} to {rec['last']}. Everything below is PAPER. "
             "Nothing on this page changes what the bot trades; it is the evidence for deciding that.", ""]
    for name, account in board["accounts"].items():
        sd, gate = account["shorter_dated_contracts"], account["cost_gate"]
        lines += [f"## {name} account: {account['closed_trades']} closed trades, {_money(account['realized_total'])} realized", "",
                  "### Shorter-dated contracts (1-6 days out) against the contract the bot buys (7-21 days out)", "",
                  "Same entry and exit minutes, recorded real quotes. \"Pessimistic fills\" uses the worst price inside the minute.", "",
                  "| Sample | Trades | Bot's contract | Shorter-dated | Bot's, pessimistic fills | Shorter, pessimistic fills | Shorter better on |",
                  "|---|---|---|---|---|---|---|"]
        for label, key in ((f"Trial (from {sd['trial_start']})", "trial"), ("All recorded sessions", "all_recorded")):
            s = sd[key]
            if not s.get("n"):
                lines.append(f"| {label} | 0 | | | | | |")
            else:
                lines.append(f"| {label} | {s['n']} | {_money(s['standard_open'])} | {_money(s['short_open'])} | "
                             f"{_money(s['standard_adverse'])} | {_money(s['short_adverse'])} | {s['short_better_share']:.0%} |")
        mark = sd.get("trial_pass_mark")
        lines.append("")
        if mark:
            lines.append(f"Pass mark ({mark['rule']}; judged after {TRIAL_SESSIONS} sessions): better-share "
                         f"{'met' if mark['better_share_ok'] else 'not met'}, totals {'met' if mark['total_ok'] else 'not met'} so far.")
        else:
            lines.append("No trial trades priced yet (the day's quotes are recorded after the close).")
        if sd.get("short_fits_cap_share") is not None:
            lines.append(f"Shorter-dated contract fits this account's $350 limit on {sd['short_fits_cap_share']:.0%} of its trades.")
        lines += ["", "### Cost gate (round-trip option cost as a share of the trade's planned risk)", ""]
        if gate["trades_with_cost"]:
            lines += [f"Median cost {gate['median_cost_r']:.2f}R over {gate['trades_with_cost']} trades.", "",
                      "| Limit | Trades kept | Kept made | Trades dropped | Dropped made |", "|---|---|---|---|---|"]
            for limit in COST_LIMITS:
                g = gate[f"limit_{limit:.2f}R"]
                lines.append(f"| {limit:.2f}R | {g['kept']['n']} | {_money(g['kept'].get('total'))} | {g['dropped']['n']} | "
                             f"{_money(g['dropped'].get('total'))} |")
        else:
            lines.append("No trades with a measurable entry cost yet.")
        lines.append("")
    if board.get("errors"):
        lines += ["## Sections that failed", ""] + [f"- {k}: {v}" for k, v in board["errors"].items()] + [""]
    empty_ideas = {"logged": 0, "scored": 0, "awaiting_recorded_prices": 0, "by_agent": {}, "bar": "n/a"}
    ideas = board.get("agent_trade_ideas") or empty_ideas
    lines += ["## Agent trade ideas (never traded)", "",
              f"{ideas['logged']} logged, {ideas['scored']} scored, {ideas['awaiting_recorded_prices']} waiting for recorded prices. "
              f"The bar: {ideas['bar']}.", ""]
    if ideas["by_agent"]:
        lines += ["| Agent | Ideas | Stock-move R | R after option cost | Option P&L | Win rate |", "|---|---|---|---|---|---|"]
        for agent, s in ideas["by_agent"].items():
            lines.append(f"| {agent} | {s['gross_r']['n']} | {s['gross_r'].get('total', 0):+.2f} | "
                         f"{s['net_r'].get('total', 0):+.2f} | {_money(s['option_pnl'].get('total'))} | "
                         f"{s['gross_r'].get('win_rate', 0):.0%} |")
        lines.append("")
    vetoes = board.get("agent_vetoes") or {"vetoes_issued": 0, "by_ledger": {}}
    lines += ["## Agent vetoes", "", f"{vetoes['vetoes_issued']} vetoes issued so far."]
    for name, v in vetoes["by_ledger"].items():
        total = v["would_have_made_r"].get("total")
        lines.append(f"- {name}: {v['blocked_signals']} signals blocked"
                     + (f"; they would have made {total:+.2f}R in total (negative means the veto helped)." if total is not None else "."))
    watch = board.get("agent_watchlist") or {"logged": 0, "scored": 0, "not_recorded_symbols": 0,
                                             "moved_more_than_other_symbols_share": None}
    lines += ["", "## Agent watchlist (\"in play\")", "",
              f"{watch['logged']} names logged, {watch['scored']} scored, {watch['not_recorded_symbols']} outside the recorded symbols."]
    if watch["moved_more_than_other_symbols_share"] is not None:
        lines.append(f"Listed names moved more than the bot's other symbols {watch['moved_more_than_other_symbols_share']:.0%} of the time.")
    wide = board.get("wide_spy_open") or {"wide_sessions": 0, "classified_sessions": 0, "trades_on_wide_days": {},
                                          "trades_on_other_days": {}}
    lines += ["", "## Wide SPY opening range (logged lead, $10K trades)", "",
              f"{wide['wide_sessions']} of {wide['classified_sessions']} classified sessions were wide. "
              f"Trades on wide days: {wide['trades_on_wide_days'].get('n', 0)} for {_money(wide['trades_on_wide_days'].get('total'))}; "
              f"other days: {wide['trades_on_other_days'].get('n', 0)} for {_money(wide['trades_on_other_days'].get('total'))}.", ""]
    return "\n".join(lines) + "\n"


def run(root: Path, *, email: bool = True, rebuild_cost_table: bool = True) -> dict[str, Any]:
    board = build(root, rebuild_cost_table=rebuild_cost_table)
    text = render(board)
    stamp = datetime.now(timezone.utc).astimezone(ET).strftime("%Y%m%d")
    data_dir, report_dir = root / "data" / "scoreboard", root / "reports" / "scoreboard"
    data_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"scoreboard_{stamp}.json").write_text(json.dumps(board, indent=1, default=str), encoding="utf-8")
    (data_dir / "latest.json").write_text(json.dumps(board, indent=1, default=str), encoding="utf-8")
    (report_dir / f"scoreboard_{stamp}.md").write_text(text, encoding="utf-8")
    (report_dir / "latest.md").write_text(text, encoding="utf-8")
    result = {"report": str(report_dir / "latest.md"), "email": None}
    if email:
        from .notifications import email_configured, routine_email_enabled, send_email
        if not routine_email_enabled("scoreboard", root):
            result["email"] = "skipped (notifications.json scoreboard is false)"
        elif email_configured():
            try:
                send_email(f"OptioBot scoreboard {stamp}", text)
                result["email"] = "sent"
            except Exception as exc:  # the report file is already written
                result["email"] = f"failed: {type(exc).__name__}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.scoreboard")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="score everything and write reports/scoreboard/latest.md")
    r.add_argument("--no-email", action="store_true")
    r.add_argument("--skip-cost-table", action="store_true", help="reuse data/option_cost_table.json")
    args = parser.parse_args(argv)
    load_dotenv()
    print(json.dumps(run(Path.cwd(), email=not args.no_email, rebuild_cost_table=not args.skip_cost_table)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
