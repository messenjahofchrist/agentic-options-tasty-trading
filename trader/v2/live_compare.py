"""LIVE 2.8 against the paper accounts, all day (owner, 2026-11-17). Read-only: ledgers and logs are only read.

  data/live_vs_paper.html            rebuilt every minute with the other side pages (task \\OptioBot\\V29Report)
  reports/live_vs_paper/DATE.html    the page as it stood after the close (kept per day)

Accounts, in this order: LIVE 2.8 (real money), 2.8 $1K paper, 2.9 $1K paper, 2.9 $10K paper, 2.8 $10K paper.
LIVE runs the same 2.8 rules as the $1K paper account, so those two should buy the same signals; the "same signal"
table shows where real fills differ from paper fills, and why LIVE skipped a trade the paper account took.
"""
from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .side_report import CSS, _json, _money, _tail

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
TWIN_MINUTES = 20          # a LIVE and a paper entry in the same stock and direction this close together are one signal

EXTRA_CSS = """
.banner.live{background:linear-gradient(120deg,#0f5132 0%,#1f6f8b 55%,#6d3fd1 100%)}
.card.real{border:2px solid var(--up)}.tag.real{background:var(--up);color:#fff}.tag.paper{background:var(--line);color:var(--ink)}
.acct-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(205px,100%),1fr))}
.small{font-size:12.5px}.mid{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal;min-width:180px}
"""


def accounts(root: Path, data_dir: Path) -> list[dict[str, Any]]:
    return [
        {"key": "live", "name": "LIVE 2.8", "kind": "real", "version": "2.8", "dir": data_dir / "live", "file": "live_account.json",
         "note": "real money · tastytrade cash account · $1K rules"},
        {"key": "p28_1k", "name": "2.8 $1K paper", "kind": "paper", "version": "2.8",
         "dir": root / "data/profiles/small_account_paper", "file": "paper_account.json", "note": "same rules as LIVE"},
        {"key": "p29_1k", "name": "2.9 $1K paper", "kind": "paper", "version": "2.9",
         "dir": root / "data/v29/small_account_paper", "file": "paper_account.json", "note": "stocks in play"},
        {"key": "p29_10k", "name": "2.9 $10K paper", "kind": "paper", "version": "2.9",
         "dir": root / "data/v29/original_paper", "file": "paper_account.json", "note": "stocks in play"},
        {"key": "p28_10k", "name": "2.8 $10K paper", "kind": "paper", "version": "2.8", "dir": data_dir,
         "file": "paper_account.json", "note": "original account rules"},
    ]


def _when(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(CT)
    except ValueError:
        return None


def _trades(ledger: list[dict]) -> list[dict[str, Any]]:
    """Closed trades from a PAPER (paper_close) or LIVE (live_close) ledger, matched to the buy by trade id."""
    buys: dict[str, dict] = {}
    sells: dict[str, list[dict]] = {}
    out = []
    for row in ledger:
        if row.get("type") == "fill" and row.get("side") == "BUY":
            buys[row.get("trade_id")] = row
        elif row.get("type") == "fill" and row.get("side") == "SELL":
            sells.setdefault(str(row.get("symbol")), []).append(row)
        elif row.get("type") in ("paper_close", "live_close") and row.get("trade_id") in buys:
            buy = buys[row["trade_id"]]
            occ = str(buy.get("symbol") or "")
            sell = next((s for s in reversed(sells.get(occ, [])) if s.get("timestamp") == row.get("timestamp")), None)
            out.append({"underlying": row.get("underlying") or buy.get("underlying") or occ.split()[0], "occ": occ.strip(),
                        "side": "CALL" if occ.strip()[-9:-8] == "C" else "PUT",
                        "entry": _when(buy.get("timestamp")), "exit": _when(row.get("timestamp")),
                        "quantity": int(row.get("quantity") or buy.get("quantity") or 1),
                        "entry_price": float(buy.get("fill_price") or 0), "exit_price": float(sell["fill_price"]) if sell else None,
                        "pnl": float(row.get("realized_pnl") or 0), "reason": row.get("reason") or (sell or {}).get("reason") or ""})
    return out


def account_view(spec: dict[str, Any], today: date) -> dict[str, Any]:
    data = _json(spec["dir"] / spec["file"], {}) or {}
    trades = _trades(data.get("ledger") or [])
    today_trades = [t for t in trades if t["exit"] and t["exit"].date() == today]
    positions = []
    for p in data.get("positions") or []:
        entry, bid, qty = float(p.get("entry_price") or 0), p.get("last_bid"), int(p.get("quantity") or 1)
        positions.append({"underlying": p.get("underlying"), "occ": str(p.get("symbol") or "").strip(),
                          "side": "CALL" if str(p.get("right")) == "C" else "PUT", "entry": _when(p.get("entry_time")),
                          "entry_price": entry, "bid": float(bid) if bid is not None else None, "quantity": qty,
                          "open_pnl": (float(bid) - entry) * 100 * qty if bid is not None else None,
                          "stop": p.get("stop_underlying"), "target": p.get("target_underlying")})
    beat = _json(spec["dir"] / "engine_heartbeat.json", {}) or {}
    expect = _json(spec["dir"] / "engine_expectation.json", {}) or {}
    updated = _when(beat.get("updated_at_utc"))
    realized = sum(t["pnl"] for t in today_trades)
    open_pnl = sum(p["open_pnl"] or 0 for p in positions)
    if spec["kind"] == "real":
        size = _live_net_liq(spec["dir"])
    else:
        size = float(data.get("starting_capital") or 0) or None
    return {**spec, "exists": bool(data), "trades": trades, "today": today_trades, "positions": positions,
            "realized": realized, "open_pnl": open_pnl, "day": realized + open_pnl, "size": size,
            "wins": sum(1 for t in today_trades if t["pnl"] > 0),
            "engine": {"state": (expect.get("state") or beat.get("lifecycle_state") or "NOT_STARTED"),
                       "heartbeat": beat.get("lifecycle_state"), "mode": beat.get("mode"), "error": beat.get("error"),
                       "age": (datetime.now(CT) - updated).total_seconds() if updated else None}}


def _live_net_liq(live_dir: Path) -> float | None:
    """Account value from the newest LIVE readiness report (the LIVE ledger itself does not hold a balance)."""
    reports = sorted((live_dir / "readiness").glob("*-live-readiness.json"))
    for path in reversed(reports[-5:]):
        value = ((_json(path, {}) or {}).get("balances") or {}).get("net_liquidating_value")
        if value is not None:
            return float(value)
    return None


# --- pieces ------------------------------------------------------------------------------------------------------------
def _engine(view: dict) -> str:
    e = view["engine"]
    age = e["age"]
    healthy = e["heartbeat"] in {"RUNNING", "READY", "TRADING"} and age is not None and age < 150
    words = {"NOT_STARTED": "not started", "STOPPED": "stopped", "NO_STOCKS_IN_PLAY": "no stocks in play today",
             "WAITING_FOR_SCAN": "waiting for the 8:45 CT scan", "SCAN_FAILED": "stock scan failed"}
    text = "running" if healthy else words.get(e["state"], str(e["state"]).lower().replace("_", " "))
    if view["kind"] == "real" and not view["exists"] and e["state"] == "NOT_STARTED":
        text = "not started (you start LIVE in its own terminal)"
    calm = healthy or e["state"] in {"STOPPED", "NO_STOCKS_IN_PLAY", "WAITING_FOR_SCAN", "NOT_STARTED"}
    return (f'<span class="{"state-ok" if calm else "state-bad"}">●</span> {html.escape(text)}'
            + (f' <span class="muted small">({age:.0f}s ago)</span>' if age is not None and age < 3600 and not healthy else "")
            + (f'<div class="down small">{html.escape(str(e["error"]))[:120]}</div>' if e["error"] and not healthy else ""))


def _pct(value: float, size: float | None) -> str:
    if not size:
        return ""
    return f'<span class="muted small"> ({value / size:+.1%} of ${size:,.0f})</span>'


def _account_card(view: dict) -> str:
    tag = '<span class="tag real">REAL MONEY</span>' if view["kind"] == "real" else '<span class="tag paper">paper</span>'
    opened = "".join(
        f'<div class="small">{html.escape(str(p["underlying"]))} {p["side"].lower()} · in {p["entry"]:%H:%M} at ${p["entry_price"]:.2f}'
        f' · bid {("$%.2f" % p["bid"]) if p["bid"] is not None else "–"} · {_money(p["open_pnl"])}</div>'
        for p in view["positions"] if p["entry"]) or '<div class="muted small">No open position.</div>'
    return (f'<div class="card{" real" if view["kind"] == "real" else ""}"><div>{tag}</div>'
            f'<h2 style="margin:6px 0 2px">{html.escape(view["name"])}</h2><div class="muted small">{html.escape(view["note"])}</div>'
            f'<div class="small" style="margin:6px 0">{_engine(view)}</div>'
            f'<div class="muted small">Today (closed + open)</div><div class="big">{_money(view["day"])}</div>'
            f'<div class="small">{_pct(view["day"], view["size"])}</div>'
            f'<div class="small" style="margin-top:6px">Closed {_money(view["realized"])} · open {_money(view["open_pnl"])}</div>'
            f'<div class="small">{len(view["today"])} closed trade(s), {view["wins"]} winner(s)</div>'
            f'<h3>Open now</h3>{opened}</div>')


def _reason(text: str) -> str:
    return {"end_of_session_close": "end-of-day close", "underlying_invalidation_stop": "stop (stock moved back)",
            "profit_target": "hit target", "data_failure_protective_exit": "protective sell (data failure)",
            "max_holding_time": "time limit"}.get(text, text.replace("_", " "))


def _all_trades(views: list[dict]) -> str:
    rows = []
    for v in views:
        for t in v["today"]:
            rows.append((t["entry"], v, t, False))
        for p in v["positions"]:
            if p["entry"] and p["entry"].date() == datetime.now(CT).date():
                rows.append((p["entry"], v, p, True))
    if not rows:
        return '<p class="muted">No trades yet today.</p>'
    rows.sort(key=lambda r: r[0])
    out = []
    for entry, v, t, is_open in rows:
        name = f'<b>{html.escape(v["name"])}</b>' if v["kind"] == "real" else html.escape(v["name"])
        result = _money(t["open_pnl"]) + ' <span class="muted small">open</span>' if is_open else _money(t["pnl"])
        exit_cell = "–" if is_open else f'{t["exit"]:%H:%M} at ${t["exit_price"]:.2f}' if t.get("exit_price") is not None else f'{t["exit"]:%H:%M}'
        why = "still open" if is_open else _reason(t["reason"])
        out.append(f'<tr><td>{entry:%H:%M}</td><td>{name}</td><td>{html.escape(str(t["underlying"]))}</td><td>{t["side"]}</td>'
                   f'<td class=num>${t["entry_price"]:.2f}</td><td>{exit_cell}</td><td class=num>{result}</td>'
                   f'<td class=muted>{html.escape(why)}</td></tr>')
    return ('<div class="scroll"><table><tr><th>In (CT)</th><th>Account</th><th>Stock</th><th>Type</th><th class=num>Paid</th>'
            '<th>Out</th><th class=num>Result</th><th>Why it closed</th></tr>' + "".join(out) + "</table></div>")


def _entries(view: dict, today: date) -> list[dict]:
    items = [{**t, "open": False} for t in view["trades"] if t["entry"] and t["entry"].date() == today]
    items += [{**p, "open": True, "pnl": p["open_pnl"], "exit": None, "exit_price": None, "reason": "still open"}
              for p in view["positions"] if p["entry"] and p["entry"].date() == today]
    return sorted(items, key=lambda t: t["entry"])


def _live_rejections(live_dir: Path, symbol: str, side: str, around: datetime) -> str:
    """Why LIVE did not buy a signal the paper account bought: its own decision records for that stock near that time."""
    reasons: dict[str, int] = {}
    for r in _tail(live_dir / "v2_decisions.jsonl", 1500):
        when = _when(r.get("timestamp_utc") or r.get("timestamp"))
        if r.get("symbol") != symbol or not when or abs((when - around).total_seconds()) > TWIN_MINUTES * 60:
            continue
        if r.get("outcome") == "APPROVED":
            return "LIVE approved it too, but no fill was booked (see data/live/live_orders.jsonl)"
        for text in (r.get("reasons") or [])[:1]:
            reasons[str(text)] = reasons.get(str(text), 0) + 1
    if not reasons:
        return "no LIVE decision recorded for it (LIVE was not running?)"
    top = max(reasons, key=reasons.get)
    return top[:160]


def _same_signal(views: dict[str, dict], today: date, live_dir: Path) -> str:
    live, paper = views["live"], views["p28_1k"]
    live_e, paper_e = _entries(live, today), _entries(paper, today)
    if not live_e and not paper_e:
        return '<p class="muted">No 2.8 $1K signals bought yet today, real or paper.</p>'
    used: set[int] = set()
    rows = []
    for p in paper_e:
        twin = next((i for i, l in enumerate(live_e) if i not in used and l["underlying"] == p["underlying"]
                     and l["side"] == p["side"] and abs((l["entry"] - p["entry"]).total_seconds()) <= TWIN_MINUTES * 60), None)
        if twin is None:
            rows.append(f'<tr><td>{p["entry"]:%H:%M}</td><td>{html.escape(str(p["underlying"]))} {p["side"].lower()}</td>'
                        f'<td class=num>${p["entry_price"]:.2f}</td><td class=num>–</td><td class=num>{_money(p["pnl"])}</td>'
                        f'<td class=num>–</td><td class="wrap muted">LIVE did not buy: '
                        f'{html.escape(_live_rejections(live_dir, str(p["underlying"]), p["side"], p["entry"]))}</td></tr>')
            continue
        used.add(twin)
        l = live_e[twin]
        both = l["pnl"] is not None and p["pnl"] is not None
        gap = (l["pnl"] - p["pnl"]) if both else None
        note = []
        if abs(l["entry_price"] - p["entry_price"]) >= 0.005:
            note.append(f'real fill ${l["entry_price"] - p["entry_price"]:+.2f} vs paper on the way in')
        if l.get("exit_price") is not None and p.get("exit_price") is not None and abs(l["exit_price"] - p["exit_price"]) >= 0.005:
            note.append(f'${l["exit_price"] - p["exit_price"]:+.2f} on the way out')
        if l["open"] or p["open"]:
            note.append("still open")
        rows.append(f'<tr><td>{l["entry"]:%H:%M}</td><td>{html.escape(str(l["underlying"]))} {l["side"].lower()}</td>'
                    f'<td class=num>${p["entry_price"]:.2f}</td><td class=num>${l["entry_price"]:.2f}</td>'
                    f'<td class=num>{_money(p["pnl"])}</td><td class=num>{_money(l["pnl"])}</td>'
                    f'<td class="wrap muted">{html.escape("; ".join(note) or "same prices")}'
                    f'{" · real minus paper " + _money(gap) if gap is not None else ""}</td></tr>')
    for i, l in enumerate(live_e):
        if i not in used:
            rows.append(f'<tr><td>{l["entry"]:%H:%M}</td><td>{html.escape(str(l["underlying"]))} {l["side"].lower()}</td>'
                        f'<td class=num>–</td><td class=num>${l["entry_price"]:.2f}</td><td class=num>–</td>'
                        f'<td class=num>{_money(l["pnl"])}</td><td class="wrap muted">LIVE only (the paper account did not buy it)</td></tr>')
    return ('<div class="scroll"><table><tr><th>In (CT)</th><th>Signal</th><th class=num>Paper paid</th><th class=num>Real paid</th>'
            '<th class=num>Paper result</th><th class=num>Real result</th><th>Difference</th></tr>' + "".join(rows) + "</table></div>")


def _ideas_record(root: Path) -> str | None:
    """The advisor's idea score from the nightly scoreboard (reports/scoreboard/latest.md), one line."""
    import re
    try:
        text = (root / "reports/scoreboard/latest.md").read_text(encoding="utf-8")
    except OSError:
        return None
    section = text.split("## Agent trade ideas", 1)[-1].split("\n## ", 1)[0]
    rows = re.findall(r"^\|\s*(\w[\w-]*)\s*\|\s*(\d+)\s*\|\s*([-+0-9.]+)\s*\|\s*([-+0-9.]+)\s*\|\s*([^|]+)\|\s*([^|]+)\|", section, re.M)
    if not rows:
        return None
    agent, n, move_r, net_r, pnl, win = rows[0]
    return f"{n} ideas, stock move {move_r}R, {net_r}R after option cost, option P&L {pnl.strip()}, win rate {win.strip()}"


def _advisor(root: Path, views: list[dict], today: date) -> str:
    rows = _tail(root / "data/advisor/advice.jsonl", 40)
    last = next((r for r in reversed(rows) if r.get("timestamp_utc")), None)
    parts = []
    if last is None:
        parts.append('<p class="down">The advisor has not run yet.</p>')
    else:
        when = _when(last["timestamp_utc"])
        fresh = when is not None and (datetime.now(CT) - when) < timedelta(minutes=75)
        ok = last.get("status") == "complete"
        market_hours = time(8, 0) <= datetime.now(CT).time() <= time(15, 0) and today.weekday() < 5
        state = ("state-ok" if ok and (fresh or not market_hours) else "state-bad")
        parts.append(f'<p><span class="{state}">●</span> Last advice {when:%a %H:%M} CT by '
                     f'<b>{html.escape(str(last.get("agent") or "?").capitalize())}</b> · {html.escape(str(last.get("status")))}'
                     f'{" · " + html.escape(str(last.get("error"))[:120]) if last.get("error") else ""}'
                     f'<span class="muted small"> · runs every hour 8:00–14:00 CT</span></p>')
        advice = last.get("advice") or {}
        mood = advice.get("market_sentiment") or {}
        if mood:
            drivers = "".join(f"<li>{html.escape(str(d))[:220]}</li>" for d in (mood.get("drivers") or [])[:3])
            parts.append(f'<p>Market read: <b>{html.escape(str(mood.get("lean")))}</b> '
                         f'<span class="muted">({html.escape(str(mood.get("confidence")))} confidence)</span></p>'
                         f'<ul class="small" style="margin:4px 0 8px;padding-left:18px">{drivers}</ul>')
        watch = advice.get("watchlist") or []
        if watch:
            items = "".join(
                f'<tr><td><b>{html.escape(str(w.get("symbol")))}</b></td><td>{html.escape(str(w.get("direction") or ""))}</td>'
                f'<td class="wrap small">{html.escape(str(w.get("catalyst") or ""))[:220]}</td></tr>' for w in watch[:10])
            parts.append('<h3>Keep an eye on (advisor watchlist, display only)</h3><div class="scroll"><table>'
                         '<tr><th>Stock</th><th>Lean</th><th>Why</th></tr>' + items + '</table></div>')
        ideas = advice.get("trade_ideas") or []
        record = _ideas_record(root)
        if ideas:
            items = "".join(
                f'<tr><td><b>{html.escape(str(i.get("symbol")))}</b></td><td>{html.escape(str(i.get("side") or ""))}</td>'
                f'<td>{html.escape(str(i.get("confidence") or ""))}</td>'
                f'<td class="wrap small">{html.escape(str(i.get("thesis") or ""))[:240]}</td></tr>' for i in ideas[:5])
            parts.append('<h3>Advisor trade ideas (not traded by the bot)</h3><div class="scroll"><table>'
                         '<tr><th>Stock</th><th>Type</th><th>Confidence</th><th>Idea</th></tr>' + items + '</table></div>')
        else:
            parts.append('<p class="small">Advisor trade ideas this hour: none.</p>')
        if record:
            parts.append(f'<p class="muted small">Track record of its ideas so far: {html.escape(record)}. They earn a paper trial '
                         'only after 50+ scored ideas over 4+ weeks with a profit after option costs.</p>')
        for p in (advice.get("positions") or [])[:6]:
            parts.append(f'<div class="small">{html.escape(str(p.get("underlying")))} [{html.escape(str(p.get("ledger")))}]: '
                         f'<b>{html.escape(str(p.get("advice")).upper())}</b> — {html.escape(str(p.get("reason"))[:260])}{"…" if len(str(p.get("reason"))) > 260 else ""}</div>')
    vetoes = (_json(root / "data/advisor/vetoes.json", {}) or {}).get("vetoes") or []
    if vetoes:
        items = "".join(f'<li><b>{html.escape(str(v.get("symbol")))}</b> {html.escape(str(v.get("side") or "both sides"))}: '
                        f'{html.escape(str(v.get("reason"))[:180])}</li>' for v in vetoes)
        parts.append(f'<h3>Blocked by the advisor right now</h3><ul class="small" style="padding-left:18px">{items}</ul>')
    else:
        parts.append('<p class="small">Blocked by the advisor right now: <b>nothing</b>.</p>')
    counts = []
    for v in views:
        n = sum(1 for r in _tail(v["dir"] / "v2_decisions.jsonl", 3000)
                if any("ai_veto" in str(x) for x in r.get("reasons") or [])
                and (_when(r.get("timestamp_utc") or r.get("timestamp")) or datetime.min.replace(tzinfo=CT)).date() == today)
        counts.append(f'{html.escape(v["name"])} {n}')
    parts.append(f'<p class="muted small">Signals the advisor blocked today: {" · ".join(counts)}. The advisor can only block a '
                 'stock (news the bot cannot see); it never picks, adds or sizes a trade. Its block applies to LIVE and paper alike.</p>')
    review = next(reversed(_tail(root / "data/supervisor_reviews.jsonl", 20)), None)
    if review:
        parts.append(f'<p class="muted small">Hourly two-AI review of the paper ledgers: last {(_when(review.get("timestamp_utc")) or datetime.now(CT)):%a %H:%M} CT '
                     f'({html.escape(str(review.get("status")))}).</p>')
    return '<div class="card" style="margin-top:14px"><h2>AI advisor</h2>' + "".join(parts) + "</div>"


def page_html(root: Path, data_dir: Path, now: datetime) -> str:
    now = now.astimezone(CT)
    today = now.date()
    specs = accounts(root, data_dir)
    views = [account_view(s, today) for s in specs]
    by_key = {v["key"]: v for v in views}
    after_close = now.time() >= time(15, 5) or now.weekday() >= 5
    live = by_key["live"]
    live_line = ("LIVE has not run today." if live["engine"]["state"] == "NOT_STARTED" and not live["today"] and not live["positions"]
                 else "LIVE is trading real money.")
    body = [f'<div class="banner live"><h1>LIVE 2.8 vs paper</h1><p>{today:%A, %B %d, %Y} · '
            f'{"final for the day" if after_close else "updates every minute"} · {now:%I:%M:%S %p} CT · {live_line}</p></div>',
            f'<div class="acct-grid">{"".join(_account_card(v) for v in views)}</div>',
            '<div class="card" style="margin-top:14px"><h2>Same signal: LIVE vs 2.8 $1K paper</h2>'
            '<p class="muted small">Both run 2.8 with the $1K rules, so they should buy the same signals. Differences are real fills vs '
            'paper fills, or a LIVE-only safety block (unsettled cash, broker not confirmed).</p>'
            + _same_signal(by_key, today, live["dir"]) + '</div>',
            '<div class="card" style="margin-top:14px"><h2>Every trade today, all accounts</h2>' + _all_trades(views) + '</div>',
            _advisor(root, views, today),
            '<footer>Results are after fees: closed trades from each ledger, open positions at the current bid. LIVE fills are the '
            'broker\'s reported fills; paper fills are simulated at the ask/bid. Account sizes differ, so compare the percentage '
            'for LIVE against the $1K paper accounts. Times are Central.</footer>']
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="{300 if after_close else 60}"><title>Live vs paper</title>'
            f'<style>{CSS}{EXTRA_CSS}</style></head><body><div class="wrap">{"".join(body)}</div></body></html>')
    return page


def write(root: Path, data_dir: Path, now: datetime | None = None) -> dict[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(CT)
    text = page_html(root, data_dir, now)
    path = root / "data" / "live_vs_paper.html"
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    out = {"live_vs_paper.html": str(path)}
    if now.time() >= time(15, 5) and now.weekday() < 5:
        dated = root / "reports" / "live_vs_paper" / f"{now.date().isoformat()}.html"
        dated.parent.mkdir(parents=True, exist_ok=True)
        dated.write_text(text, encoding="utf-8")
        out["live_vs_paper_dated"] = str(dated)
    return out
