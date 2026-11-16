"""Pages for the 2.9 side test (owner, 2026-11-12). Read-only: ledgers and logs are only read.

  data/v29_dashboard.html          live 2.9 page, rebuilt every minute (task \\OptioBot\\V29Report), refreshes itself
  data/compare_28_vs_29.html       2.8 vs 2.9 face-off: today, every day since the test began, and the crown
  reports/side_by_side/DATE.html   the face-off as it stood after the close (kept per day)

    python -m trader.cli side-report
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..scoreboard import closed_trades

ET = ZoneInfo("America/New_York")
TEST_START = date(2026, 11, 13)
OLD_SYMBOLS = {"SPY", "QQQ", "IWM", "NVDA", "AAPL", "AMZN", "GOOGL", "PLTR", "NFLX", "INTC", "UBER"}   # 2.8.0's list


def _ledgers(root: Path, data_dir: Path) -> dict[str, dict[str, Path]]:
    return {"2.8": {"$10K": data_dir, "$1K": root / "data/profiles/small_account_paper"},
            "2.9": {"$10K": root / "data/v29/original_paper", "$1K": root / "data/v29/small_account_paper"}}


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _tail(path: Path, limit: int = 400) -> list[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 600_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _et(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return None


def ledger_view(path: Path, today: date) -> dict[str, Any]:
    account = _json(path / "paper_account.json", {}) or {}
    trades = closed_trades(account) if account else []
    for t in trades:
        t["exit_day"] = (_et(t["exit_time"]) or datetime.min.replace(tzinfo=ET)).date()
        t["spread_paid"] = round((float(t["entry_ask"]) - float(t["entry_bid"])) * 100, 2) \
            if t.get("entry_ask") is not None and t.get("entry_bid") is not None else None
    beat = _json(path / "engine_heartbeat.json", {}) or {}
    expect = _json(path / "engine_expectation.json", {}) or {}
    updated = _et(beat.get("updated_at_utc"))
    age = (datetime.now(ET) - updated).total_seconds() if updated else None
    return {"exists": bool(account), "starting": float(account.get("starting_capital") or 0), "cash": float(account.get("cash") or 0),
            "positions": account.get("positions") or [], "trades": trades,
            "today": [t for t in trades if t["exit_day"] == today],
            "engine": {"state": expect.get("state") or beat.get("lifecycle_state") or "NOT_STARTED",
                       "heartbeat": beat.get("lifecycle_state"), "age_seconds": age, "symbols": beat.get("symbol")}}


def stats(trades: list[dict]) -> dict[str, Any]:
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    spread = [t["spread_paid"] for t in trades if t.get("spread_paid") is not None]
    return {"trades": len(pnl), "wins": len(wins), "win_rate": len(wins) / len(pnl) if pnl else None,
            "total": round(sum(pnl), 2), "average": round(sum(pnl) / len(pnl), 2) if pnl else None,
            "best": max(pnl) if pnl else None, "worst": min(pnl) if pnl else None,
            "spread_paid": round(sum(spread), 2) if spread else 0.0,
            "symbols": sorted({t["symbol"] for t in trades})}


def crown(a: float, b: float, a_trades: int, b_trades: int) -> str:
    if not a_trades and not b_trades:
        return "none"
    if abs(a - b) < 0.005:
        return "tie"
    return "2.8" if a > b else "2.9"


# --- HTML -------------------------------------------------------------------------------------------------------------
CSS = """
:root{--bg:#f6f5fb;--card:#fff;--ink:#1d1b26;--muted:#6b6880;--line:#e4e1ef;--v29:#6d3fd1;--v29soft:#efe8ff;
--v28:#1f6f8b;--v28soft:#e3f2f7;--up:#147a3d;--down:#b3261e;--gold:#b7860b;--goldsoft:#fff6d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15131c;--card:#1f1c29;--ink:#ece9f5;--muted:#a19cb6;
--line:#332f42;--v29:#a98bff;--v29soft:#2a2240;--v28:#6cc3df;--v28soft:#16303a;--up:#5fd08a;--down:#ff8a80;--gold:#f2c94c;--goldsoft:#3a3216}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:16px}
.banner{border-radius:14px;padding:18px 20px;margin-bottom:16px;color:#fff}
.banner.v29{background:linear-gradient(120deg,#5b2fc0,#8a5cf5)}.banner.duel{background:linear-gradient(120deg,#1f6f8b 0%,#3b3b6d 50%,#6d3fd1 100%)}
.banner h1{margin:0 0 4px;font-size:22px}.banner p{margin:0;opacity:.92}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.02em}
.tag.v29{background:var(--v29soft);color:var(--v29)}.tag.v28{background:var(--v28soft);color:var(--v28)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.card h2{font-size:16px;margin:0 0 10px}.card h3{font-size:14px;margin:14px 0 6px;color:var(--muted);font-weight:600}
.big{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}.muted{color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13.5px}
th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600}
td.num,th.num{text-align:right}.scroll{overflow-x:auto}
.crown{border:2px solid var(--gold);background:var(--goldsoft);border-radius:14px;padding:16px 18px;margin-bottom:16px}
.crown .who{font-size:30px;font-weight:800}.pill{padding:2px 8px;border-radius:6px;font-size:12px;background:var(--line)}
.state-ok{color:var(--up)}.state-bad{color:var(--down)}
footer{color:var(--muted);font-size:12.5px;margin:18px 0 8px}
@media (max-width:520px){.big{font-size:22px}.crown .who{font-size:22px}}
"""


def _money(value: float | None, sign: bool = True) -> str:
    if value is None:
        return "–"
    cls = "up" if value > 0 else "down" if value < 0 else ""
    text = f"{'+' if sign and value > 0 else '-' if value < 0 else ''}${abs(value):,.2f}"
    return f'<span class="{cls}">{text}</span>'


def _page(title: str, body: str, refresh: int) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="{refresh}"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


def _trade_rows(trades: list[dict], mark_new: bool = False) -> str:
    if not trades:
        return '<p class="muted">No closed trades.</p>'
    rows = []
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        opened, closed = _et(t["entry_time"]), _et(t["exit_time"])
        new = mark_new and t["symbol"] not in OLD_SYMBOLS
        rows.append(f"<tr><td>{html.escape(str(t['symbol']))}{' <span class=\"tag v29\">new</span>' if new else ''}</td>"
                    f"<td>{t['side']}</td><td>{opened:%H:%M}–{closed:%H:%M}</td>"
                    f"<td class=num>${t['premium']:,.0f}</td><td class=num>{_money(t['pnl'])}</td>"
                    f"<td class=muted>{html.escape(str(t.get('exit_reason') or ''))[:28]}</td></tr>")
    return ('<div class="scroll"><table><tr><th>Stock</th><th>Type</th><th>Held (ET)</th><th class=num>Cost</th>'
            '<th class=num>Result</th><th>Why it closed</th></tr>' + "".join(rows) + "</table></div>")


def _engine_line(view: dict) -> str:
    engine = view["engine"]
    age = engine["age_seconds"]
    healthy = engine["heartbeat"] in {"RUNNING", "READY", "TRADING"} and age is not None and age < 150
    text = {"NO_STOCKS_IN_PLAY": "no stocks in play today, sitting out", "SCAN_FAILED": "stock scan failed",
            "WAITING_FOR_SCAN": "waiting for the 9:45 ET scan", "NOT_STARTED": "not started yet",
            "STOPPED": "stopped (market closed)"}.get(engine["state"], engine["state"].lower().replace("_", " "))
    cls = "state-ok" if healthy or engine["state"] in {"STOPPED", "NO_STOCKS_IN_PLAY", "WAITING_FOR_SCAN", "NOT_STARTED"} else "state-bad"
    return f'<span class="{cls}">●</span> Engine: {html.escape(text)}' + (f' <span class=muted>(heartbeat {age:.0f}s ago)</span>' if age is not None and age < 3600 else "")


def _positions(view: dict) -> str:
    if not view["positions"]:
        return '<p class="muted">No open positions.</p>'
    rows = []
    for p in view["positions"]:
        opened = _et(p.get("entry_time"))
        rows.append(f"<tr><td>{html.escape(str(p.get('underlying')))}</td><td>{html.escape(str(p.get('symbol', '')).strip())}</td>"
                    f"<td>{opened:%H:%M}</td><td class=num>${float(p.get('entry_price') or 0) * 100:,.0f}</td>"
                    f"<td class=num>{float(p.get('stop_underlying') or 0):,.2f}</td><td class=num>{float(p.get('target_underlying') or 0):,.2f}</td></tr>"
                    if opened else "")
    return ('<div class="scroll"><table><tr><th>Stock</th><th>Contract</th><th>Opened</th><th class=num>Cost</th>'
            '<th class=num>Stop</th><th class=num>Target</th></tr>' + "".join(rows) + "</table></div>")


def dashboard_html(root: Path, views: dict, today: date, picks: dict | None) -> str:
    v29 = views["2.9"]
    now = datetime.now(ET)
    body = [f'<div class="banner v29"><h1>OptioBot 2.9 — the experiment</h1><p>Trades only the stocks that are '
            f'"in play" today, with wider stops and 1–6 day options. Paper money, running beside the real system '
            f'(2.8), which is not shown here. Updated {now:%a %b %d, %I:%M:%S %p} ET · refreshes every minute.</p></div>']
    # in-play picks
    if picks:
        if picks.get("status") == "ok":
            chosen = picks.get("selected") or []
            rows = "".join(f"<tr><td><b>{html.escape(r['symbol'])}</b>{' <span class=\"tag v29\">new</span>' if r['symbol'] not in OLD_SYMBOLS else ''}</td>"
                           f"<td class=num>{r['relative_volume']:.1f}×</td><td class=num>${r['price']:,.2f}</td></tr>" for r in chosen)
            others = ", ".join(f"{r['symbol']} {r['relative_volume']:.1f}×" for r in (picks.get("scored") or [])
                               if r["symbol"] not in {c["symbol"] for c in chosen})[:400]
            body.append('<div class="card"><h2>Today\'s stocks in play</h2>'
                        f'<p class="muted">Scanned {picks.get("universe")} stocks at 9:45 ET. "2.0×" means twice its usual '
                        'opening volume. Only these are traded today.</p>'
                        + (f'<table><tr><th>Stock</th><th class=num>Opening volume vs usual</th><th class=num>Price</th></tr>{rows}</table>'
                           if chosen else '<p><b>None today</b> — no stock reached 2× its usual opening volume, so 2.9 sits out.</p>')
                        + (f'<h3>Next closest</h3><p class="muted">{html.escape(others)}</p>' if others else "") + '</div>')
        else:
            body.append(f'<div class="card"><h2>Today\'s stocks in play</h2><p class="down">Scan failed: '
                        f'{html.escape(str(picks.get("error")))}</p></div>')
    else:
        body.append('<div class="card"><h2>Today\'s stocks in play</h2><p class="muted">Not scanned yet — the scan runs at 9:45 ET.</p></div>')
    cards = []
    for acct in ("$10K", "$1K"):
        view, old = v29[acct], views["2.8"][acct]
        today_29, today_28 = stats(view["today"]), stats(old["today"])
        start = view["starting"] or (10000 if acct == "$10K" else 1000)
        cash = view["cash"] if view["exists"] else start  # no ledger file until 2.9's first trade
        cards.append(f'<div class="card"><h2><span class="tag v29">2.9</span> {acct} paper account</h2>'
                     f'<div>{_engine_line(view)}</div>'
                     f'<p class="muted" style="margin:8px 0 2px">Today</p><div class="big">{_money(today_29["total"])}</div>'
                     f'<p class="muted">{today_29["trades"]} closed trade(s) · cash ${cash:,.2f} of ${start:,.0f} start'
                     f' · <span class="tag v28">2.8 today {_money(today_28["total"]).replace("class=", "data-c=")}</span></p>'
                     f'<h3>Open now</h3>{_positions(view)}<h3>Closed today</h3>{_trade_rows(view["today"], True)}</div>')
    body.append(f'<div class="grid" style="margin-top:14px">{"".join(cards)}</div>')
    # recent decisions
    rows = []
    for acct in ("$10K", "$1K"):
        for r in _tail(root / "data" / "v29" / ("original_paper" if acct == "$10K" else "small_account_paper") / "v2_decisions.jsonl", 300):
            if r.get("type") == "candidate" and (_et(r.get("timestamp")) or now).date() == today:
                rows.append((r.get("timestamp"), acct, r))
    rows.sort(key=lambda x: x[0], reverse=True)
    if rows:
        lines = "".join(f"<tr><td>{_et(ts):%H:%M}</td><td>{acct}</td><td>{html.escape(str(r.get('symbol')))}</td><td>{r.get('side') or ''}</td>"
                        f"<td>{'<b class=up>BOUGHT</b>' if r.get('outcome') == 'APPROVED' else html.escape(str(r.get('outcome', '')).replace('_', ' ').lower())}</td>"
                        f"<td class=muted style='white-space:normal'>{html.escape(('; '.join(r.get('reasons') or []))[:150])}</td></tr>"
                        for ts, acct, r in rows[:25])
        body.append('<div class="card" style="margin-top:14px"><h2>What 2.9 looked at (latest first)</h2><div class="scroll"><table>'
                    '<tr><th>ET</th><th>Acct</th><th>Stock</th><th>Type</th><th>Decision</th><th>Why</th></tr>' + lines + '</table></div></div>')
    body.append('<footer>Paper trading only; no real money. 2.8 keeps running unchanged on the original $10K and $1K paper accounts. '
                'Face-off page: compare_28_vs_29.html in this folder.</footer>')
    return _page("OptioBot 2.9 live", "".join(body), 60)


def compare_html(views: dict, today: date) -> str:
    now = datetime.now(ET)
    after_close = now.time() >= time(16, 5) or now.weekday() >= 5
    body = [f'<div class="banner duel"><h1>2.8 vs 2.9 — daily face-off</h1><p>{today:%A, %B %d, %Y} · '
            f'{"final for the day" if after_close else "in progress, updates every minute"} · paper money only</p></div>']
    day_totals = {}
    for version in ("2.8", "2.9"):
        day_totals[version] = sum(stats(views[version][a]["today"])["total"] for a in ("$10K", "$1K"))
    trades_today = {v: sum(len(views[v][a]["today"]) for a in ("$10K", "$1K")) for v in ("2.8", "2.9")}
    winner = crown(day_totals["2.8"], day_totals["2.9"], trades_today["2.8"], trades_today["2.9"])
    # every day since the test began
    days: dict[date, dict[str, float]] = defaultdict(lambda: {"2.8": 0.0, "2.9": 0.0, "n28": 0, "n29": 0})
    for version in ("2.8", "2.9"):
        for acct in ("$10K", "$1K"):
            for t in views[version][acct]["trades"]:
                if t["exit_day"] >= TEST_START:
                    days[t["exit_day"]][version] += t["pnl"]
                    days[t["exit_day"]]["n" + version.replace(".", "")] += 1
    wins = Counter(crown(d["2.8"], d["2.9"], d["n28"], d["n29"]) for d in days.values())
    total = {v: sum(d[v] for d in days.values()) for v in ("2.8", "2.9")}
    overall = crown(total["2.8"], total["2.9"], 1, 1) if days else "none"
    who = {"2.8": "2.8 (current system)", "2.9": "2.9 (stocks in play)", "tie": "A tie", "none": "No trades yet"}
    body.append(f'<div class="crown"><div class="muted">👑 Today\'s winner (both accounts together)</div>'
                f'<div class="who">{who[winner]}</div>'
                f'<div>2.8 {_money(day_totals["2.8"])} vs 2.9 {_money(day_totals["2.9"])}</div>'
                f'<div class="muted" style="margin-top:6px">Since {TEST_START:%b %d}: 2.8 {_money(total["2.8"])} vs 2.9 {_money(total["2.9"])} · '
                f'days won: 2.8 {wins["2.8"]}, 2.9 {wins["2.9"]}{", ties " + str(wins["tie"]) if wins["tie"] else ""} · '
                f'leader: <b>{who[overall]}</b></div>'
                '<div class="muted" style="margin-top:6px">One day is mostly luck. A fair verdict needs about 20 trading days.</div></div>')
    cards = []
    for acct in ("$10K", "$1K"):
        s28, s29 = stats(views["2.8"][acct]["today"]), stats(views["2.9"][acct]["today"])
        w = crown(s28["total"], s29["total"], s28["trades"], s29["trades"])
        def cell(s, key, fmt):
            v = s[key]
            return "–" if v is None else fmt(v)
        metric_rows = [("Result today", lambda s: _money(s["total"])), ("Trades", lambda s: str(s["trades"])),
                       ("Winners", lambda s: f'{s["wins"]}' + (f' ({s["win_rate"]:.0%})' if s["win_rate"] is not None else "")),
                       ("Average per trade", lambda s: cell(s, "average", _money)), ("Best trade", lambda s: cell(s, "best", _money)),
                       ("Worst trade", lambda s: cell(s, "worst", _money)),
                       ("Paid in bid/ask spread", lambda s: f'${s["spread_paid"]:,.2f}'),
                       ("Stocks traded", lambda s: ", ".join(s["symbols"]) or "–")]
        table = "".join(f"<tr><td>{name}</td><td class=num>{fn(s28)}</td><td class=num>{fn(s29)}</td></tr>" for name, fn in metric_rows)
        new_trades = [t for t in views["2.9"][acct]["today"] if t["symbol"] not in OLD_SYMBOLS]
        cards.append(f'<div class="card"><h2>{acct} accounts · winner: {who[w]}</h2><div class="scroll"><table>'
                     f'<tr><th></th><th class=num><span class="tag v28">2.8</span></th><th class=num><span class="tag v29">2.9</span></th></tr>{table}</table></div>'
                     f'<h3><span class="tag v28">2.8</span> trades</h3>{_trade_rows(views["2.8"][acct]["today"])}'
                     f'<h3><span class="tag v29">2.9</span> trades</h3>{_trade_rows(views["2.9"][acct]["today"], True)}'
                     f'<p class="muted">{len(new_trades)} of 2.9\'s trades today were in stocks 2.8 cannot trade '
                     '(not on its 11-stock list), marked <span class="tag v29">new</span>.</p></div>')
    body.append(f'<div class="grid">{"".join(cards)}</div>')
    if days:
        rows = "".join(f"<tr><td>{d:%a %b %d}</td><td class=num>{_money(v['2.8'])}</td><td class=num>{_money(v['2.9'])}</td>"
                       f"<td>{who[crown(v['2.8'], v['2.9'], v['n28'], v['n29'])]}</td></tr>" for d, v in sorted(days.items(), reverse=True))
        body.append('<div class="card" style="margin-top:14px"><h2>Every day so far</h2><div class="scroll"><table>'
                    '<tr><th>Day</th><th class=num>2.8 (both accounts)</th><th class=num>2.9 (both accounts)</th><th>Winner</th></tr>'
                    + rows + '</table></div></div>')
    body.append('<footer>Results are realized paper profit and loss after fees, from each ledger. 2.8 = the current system on the '
                'original $10K and $1K paper accounts. 2.9 = the same rules plus three changes (stocks in play, wider stops with no '
                'breakeven move, 1–6 day options) on two new paper accounts with the same account rules.</footer>')
    return _page(f"2.8 vs 2.9 {today:%b %d}", "".join(body), 300 if after_close else 60)


def write_all(root: Path, data_dir: Path, now: datetime | None = None) -> dict[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    today = now.date()
    views = {version: {acct: ledger_view(path, today) for acct, path in paths.items()}
             for version, paths in _ledgers(root, data_dir).items()}
    picks = _json(root / "data" / "in_play" / f"{today.isoformat()}.json")
    out = {}
    for name, text in (("v29_dashboard.html", dashboard_html(root, views, today, picks)),
                       ("compare_28_vs_29.html", compare_html(views, today))):
        path = root / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".html.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        out[name] = str(path)
    if now.time() >= time(16, 5) and today.weekday() < 5 and today >= TEST_START:
        dated = root / "reports" / "side_by_side" / f"{today.isoformat()}.html"
        dated.parent.mkdir(parents=True, exist_ok=True)
        dated.write_text(compare_html(views, today), encoding="utf-8")
        out["dated"] = str(dated)
    return out
