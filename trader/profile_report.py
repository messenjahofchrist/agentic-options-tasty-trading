"""Self-contained HTML report for the small-account PAPER ledger (read-only).

`python -m trader.cli profile-report` writes report.html and report.json into the profile's data directory.
The page embeds its data and reloads itself every minute, so it opens straight from disk (file://) with no
server; a scheduled task re-running the command keeps it current. It reads ledger files only: no broker
calls, no writes outside the two report files.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .account_profiles import ProfileConfig, ProfileLedger
from .logging_utils import atomic_replace

HEARTBEAT_STALE_SECONDS = 180


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _local(ts: str | None, fmt: str = "%a %b %d %I:%M %p") -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).astimezone().strftime(fmt)
    except ValueError:
        return ts


def _option_label(symbol: str) -> str:
    """'XLF   261030P00055500' -> 'Oct 30 $55.50 put'."""
    match = re.match(r"^\S+\s+(\d{6})([CP])(\d{8})$", symbol or "")
    if not match:
        return symbol or "—"
    expiry = datetime.strptime(match.group(1), "%y%m%d").strftime("%b %d")
    strike = int(match.group(3)) / 1000
    return f"{expiry} ${strike:,.2f} {'call' if match.group(2) == 'C' else 'put'}"


def ledger_view(data_dir: Path, now: datetime) -> dict[str, Any]:
    """Positions, closed trades, today's alerts and switched-off symbols, and engine state of one PAPER ledger.
    Any ledger directory works: an account profile's or the original ledger's."""
    account = _read_json(data_dir / "paper_account.json", {})
    entries = {f["trade_id"]: f for f in account.get("ledger", [])
               if f.get("type") == "fill" and f.get("side") == "BUY"}
    # A closing fill carries its own id; it shares the option symbol and bar timestamp with its paper_close.
    exits = {(f.get("symbol"), f.get("timestamp")): f for f in account.get("ledger", [])
             if f.get("type") == "fill" and f.get("side") == "SELL"}

    positions = []
    for p in account.get("positions", []):
        qty = int(p.get("quantity", 0))
        bid = p.get("last_bid")
        pnl = None if bid is None else (float(bid) - float(p["entry_price"])) * 100 * qty - float(p.get("entry_fees", 0))
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        positions.append({"underlying": p.get("underlying"), "option": _option_label(p.get("symbol", "")),
                          "quantity": qty, "entry_price": round(float(p["entry_price"]), 2),
                          "last_bid": bid, "mark_pnl": None if pnl is None else round(pnl, 2),
                          "max_loss": round(float(p.get("modeled_risk_dollars", 0)), 2),
                          "entry_underlying": p.get("entry_underlying"),
                          "stop_underlying": round(float(p["stop_underlying"]), 2),
                          "target_underlying": round(float(p["target_underlying"]), 2),
                          "entry_time": p.get("entry_time"), "minutes_held": round(held),
                          "max_holding_minutes": p.get("max_holding_minutes")})

    closed = []
    for c in account.get("ledger", []):
        if c.get("type") != "paper_close":
            continue
        buy = entries.get(c["trade_id"], {})
        sell = exits.get((buy.get("symbol"), c.get("timestamp")), {})
        closed.append({"underlying": c.get("underlying"), "option": _option_label(buy.get("symbol", "")),
                       "entry_time": buy.get("timestamp"), "exit_time": c.get("timestamp"),
                       "entry_price": None if not buy else round(float(buy["fill_price"]), 2),
                       "exit_price": None if not sell else round(float(sell["fill_price"]), 2),
                       "pnl": round(float(c.get("realized_pnl", 0)), 2),
                       "r_multiple": None if c.get("actual_r_multiple") is None else round(float(c["actual_r_multiple"]), 2),
                       "reason": c.get("reason")})
    closed.sort(key=lambda r: r["exit_time"] or "")

    today = now.date().isoformat()
    alerts: dict[str, int] = {}
    alert_rows = []
    for line in (data_dir / "critical_alerts.jsonl").read_text(encoding="utf-8").splitlines() \
            if (data_dir / "critical_alerts.jsonl").is_file() else []:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(row.get("timestamp_utc", "")).startswith(today):
            continue
        details = row.get("details", {})
        key = f"{row.get('event')}: {details.get('underlying') or details.get('symbol') or ''}".rstrip(": ")
        alerts[key] = alerts.get(key, 0) + 1
        alert_rows.append(row)

    disabled = []
    log_path = data_dir / "paper_session.log"
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^(\S+) \S+ ERROR \S+ Disabling (\S+) for the rest of the session: (.*)$", line)
            if match and match.group(1) == today:
                reason = match.group(3)
                reason = "candle history out of order" if "out of order" in reason else \
                         "live-feed gap could not be recovered" if "gap" in reason else \
                         "startup history missing a minute" if "bootstrap incomplete" in reason else reason[:120]
                disabled.append({"symbol": match.group(2), "reason": reason})

    heartbeat = _read_json(data_dir / "engine_heartbeat.json", {})
    updated = heartbeat.get("updated_at_utc")
    age = None if not updated else (now - datetime.fromisoformat(updated)).total_seconds()
    if heartbeat.get("status") == "running" and age is not None and age <= HEARTBEAT_STALE_SECONDS:
        engine = "RUNNING"
    elif heartbeat.get("status") == "running":
        engine = "NOT RESPONDING"
    else:
        engine = str(heartbeat.get("status") or "never started").upper()
    return {
        "engine": {"state": engine, "heartbeat_utc": updated,
                   "heartbeat_age_seconds": None if age is None else round(age),
                   "last_bar_utc": heartbeat.get("last_bar_at_utc"), "error": heartbeat.get("error")},
        "positions": positions,
        "closed_trades": closed,
        "alerts_today": alerts,
        "alerts_today_total": len(alert_rows),
        "disabled_today": disabled,
    }


def build_report(root: Path, original_data_dir: Path, profile_name: str = "small",
                 now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    config = ProfileConfig.load(root, profile_name)
    ledger = ProfileLedger(config, original_data_dir=original_data_dir)
    status = ledger.status(now=now)
    status.pop("universe", None)
    original = _read_json(original_data_dir / "paper_account.json", {})
    original_expect = _read_json(original_data_dir / "engine_expectation.json", {})
    return {
        "generated_at_utc": now.isoformat(),
        "profile": status,
        **ledger_view(config.data_dir, now),
        "original_ledger": {"cash": original.get("cash"), "realized_pnl": original.get("realized_pnl"),
                            "open_positions": len(original.get("positions", [])),
                            "closed_trades": sum(1 for f in original.get("ledger", []) if f.get("type") == "paper_close"),
                            "state": original_expect.get("state") or original_expect.get("lifecycle_state")},
    }


def _money(value: Any, sign: bool = False) -> str:
    if value is None:
        return "—"
    v = float(value)
    text = f"${abs(v):,.2f}"
    if sign:
        return ("+" if v > 0 else "−" if v < 0 else "") + text
    return ("−" if v < 0 else "") + text


def _r(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}R"


def _pnl_class(value: Any) -> str:
    if value is None or abs(float(value)) < 0.005:
        return ""
    return "up" if float(value) > 0 else "down"


def _equity_curve(start: float, closed: list[dict[str, Any]]) -> str:
    if not closed:
        return '<p class="muted">The curve appears after the first closed trade.</p>'
    points = [start]
    for trade in closed:
        points.append(points[-1] + trade["pnl"])
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    width, height, pad = 600, 120, 8
    step = (width - 2 * pad) / max(1, len(points) - 1)
    coords = " ".join(f"{pad + i * step:.1f},{height - pad - (p - lo) / span * (height - 2 * pad):.1f}"
                      for i, p in enumerate(points))
    base_y = height - pad - (start - lo) / span * (height - 2 * pad)
    return (f'<svg viewBox="0 0 {width} {height}" class="curve" role="img" aria-label="Equity after each closed trade">'
            f'<line x1="{pad}" x2="{width - pad}" y1="{base_y:.1f}" y2="{base_y:.1f}" class="baseline"/>'
            f'<polyline points="{coords}" class="line"/></svg>'
            f'<p class="muted small">Cash after each closed trade, starting at {_money(start)}. '
            f'Range {_money(lo)} to {_money(hi)}.</p>')


def render_html(report: dict[str, Any], refresh_seconds: int = 60) -> str:
    e = html.escape
    p = report["profile"]
    engine = report["engine"]
    start = float(p.get("starting_balance") or 0)
    equity = float(p.get("equity") or 0)
    change = equity - start
    pct = (change / start * 100) if start else 0
    cap = p.get("max_aggregate_allocation") or 0
    used = p.get("open_max_loss") or 0
    cap_pct = min(100, used / cap * 100) if cap else 0
    progress = (p.get("progress_to_threshold") or 0) * 100
    market_value = equity - float(p.get("cash") or 0)
    engine_class = {"RUNNING": "ok", "NOT RESPONDING": "bad"}.get(engine["state"], "warn")

    def row(cells: list[str]) -> str:
        return "<tr>" + "".join(cells) + "</tr>"

    pos_rows = "".join(row([
        f"<td><b>{e(x['underlying'] or '')}</b><div class='muted small'>{e(x['option'])}</div></td>",
        f"<td class='num'>{_money(x['entry_price'])}</td>",
        f"<td class='num'>{_money(x['last_bid'])}</td>",
        f"<td class='num {_pnl_class(x['mark_pnl'])}'>{_money(x['mark_pnl'], True)}</td>",
        f"<td class='num'>{_money(x['max_loss'])}</td>",
        f"<td class='num'>{x['stop_underlying']:.2f} / {x['target_underlying']:.2f}</td>",
        f"<td class='num'>{_local(x['entry_time'], '%I:%M %p')}<div class='muted small'>{x['minutes_held']} of {x['max_holding_minutes']} min</div></td>",
    ]) for x in report["positions"]) or "<tr><td colspan='7' class='muted'>No open positions.</td></tr>"

    closed = report["closed_trades"]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    closed_rows = "".join(row([
        f"<td>{_local(t['exit_time'], '%b %d %I:%M %p')}</td>",
        f"<td><b>{e(t['underlying'] or '')}</b><div class='muted small'>{e(t['option'])}</div></td>",
        f"<td class='num'>{_money(t['entry_price'])} → {_money(t['exit_price'])}</td>",
        f"<td class='num {_pnl_class(t['pnl'])}'>{_money(t['pnl'], True)}</td>",
        f"<td class='num'>{_r(t['r_multiple'])}</td>",
        f"<td class='small'>{e((t['reason'] or '').replace('_', ' '))}</td>",
    ]) for t in reversed(closed)) or "<tr><td colspan='6' class='muted'>No closed trades yet.</td></tr>"

    reasons = p.get("rejection_reasons_today") or {}
    reason_rows = "".join(f"<li><span>{e(k.replace('_', ' '))}</span><b>{v}</b></li>"
                          for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])) \
        or "<li class='muted'>None today.</li>"
    entered_today = (p.get("signals_today") or 0) - (p.get("rejected_today") or 0)
    alert_rows = "".join(f"<li><span>{e(k.replace('_', ' '))}</span><b>{v}</b></li>"
                         for k, v in sorted(report["alerts_today"].items(), key=lambda kv: -kv[1])) \
        or "<li class='muted'>None today.</li>"
    disabled_rows = "".join(f"<li><span><b>{e(d['symbol'])}</b> {e(d['reason'])}</span></li>"
                            for d in report["disabled_today"]) or "<li class='muted'>None today.</li>"
    orig = report["original_ledger"]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh_seconds)}">
<title>Small Ledger Report</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#16181d; --muted:#6b7280; --line:#e5e7eb;
        --up:#0f7b3f; --down:#b42318; --warn:#a15c00; --accent:#2f5bd3; --bar:#e8ecf4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --text:#e8eaed; --muted:#9aa1ac;
        --line:#2a2f37; --up:#4ccb7f; --down:#ff7a6e; --warn:#f0b450; --accent:#7c9cff; --bar:#262b34; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1040px; margin:0 auto; padding:20px 16px 40px; }}
header {{ display:flex; flex-wrap:wrap; gap:8px 16px; align-items:baseline; justify-content:space-between; margin-bottom:16px; }}
h1 {{ font-size:20px; margin:0; }} h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:0 0 10px; }}
.pill {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; border:1px solid currentColor; }}
.ok {{ color:var(--up); }} .bad {{ color:var(--down); }} .warn {{ color:var(--warn); }}
.grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-bottom:12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; overflow-x:auto; }}
.grid .card {{ margin:0; }}
.big {{ font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
.muted {{ color:var(--muted); }} .small {{ font-size:12px; }}
.bar {{ height:8px; background:var(--bar); border-radius:4px; overflow:hidden; margin:8px 0 4px; }}
.bar > div {{ height:100%; background:var(--accent); }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:8px 6px; border-top:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; border-top:none; }}
.num {{ text-align:right; white-space:nowrap; }}
ul.kv {{ list-style:none; padding:0; margin:0; }} ul.kv li {{ display:flex; justify-content:space-between; gap:12px; padding:4px 0; border-top:1px solid var(--line); }}
ul.kv li:first-child {{ border-top:none; }}
.cols {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }}
.cols .card {{ margin:0; }}
.curve {{ width:100%; height:120px; }} .curve .line {{ fill:none; stroke:var(--accent); stroke-width:2; }}
.curve .baseline {{ stroke:var(--line); stroke-dasharray:4 4; }}
footer {{ margin-top:16px; }}
</style></head>
<body><main>
<header>
  <div><h1>Small account · PAPER</h1>
  <div class="muted small">{e(p.get('active_profile') or '')} · strategy {e((p.get('strategy_hash') or '')[:8])}</div></div>
  <div><span class="pill {engine_class}">Engine {e(engine['state'])}</span>
  <span class="muted small">&nbsp;last bar {_local(engine['last_bar_utc'], '%I:%M %p')}</span></div>
</header>

<div class="grid">
  <div class="card"><h2>Equity</h2><div class="big">{_money(equity)}</div>
    <div class="{_pnl_class(change)}">{_money(change, True)} ({pct:+.2f}%) since start</div></div>
  <div class="card"><h2>Realized</h2><div class="big {_pnl_class(p.get('realized_pnl'))}">{_money(p.get('realized_pnl'), True)}</div>
    <div class="muted">{len(closed)} closed · {wins} won</div></div>
  <div class="card"><h2>Cash</h2><div class="big">{_money(p.get('cash'))}</div>
    <div class="muted">open positions marked at {_money(market_value)}</div></div>
  <div class="card"><h2>Risk in use</h2><div class="big">{_money(used)}</div>
    <div class="bar"><div style="width:{cap_pct:.1f}%"></div></div>
    <div class="muted small">of {_money(cap)} cap · {_money(p.get('remaining_risk_budget'))} left</div></div>
</div>

<div class="card"><h2>Progress to $10,000</h2>
  <div class="bar"><div style="width:{progress:.2f}%"></div></div>
  <div class="muted small">{_money(p.get('verified_equity_for_transition'))} booked cash · {progress:.1f}%.
  At {_money(p.get('transition_threshold'))} the original account settings are restored automatically.</div></div>

<div class="card"><h2>Open positions ({len(report['positions'])})</h2>
<table><thead><tr><th>Trade</th><th class="num">Entry</th><th class="num">Bid</th><th class="num">P/L</th>
<th class="num">Max loss</th><th class="num">Stop / target (stock)</th><th class="num">Opened</th></tr></thead>
<tbody>{pos_rows}</tbody></table>
<p class="muted small">P/L is at the current bid, net of entry fees, before exit fees.</p></div>

<div class="cols">
  <div class="card"><h2>Today's scan</h2>
    <ul class="kv"><li><span>Symbols scanned</span><b>{p.get('symbols_scanned')}</b></li>
    <li><span>Signals</span><b>{p.get('signals_today')}</b></li>
    <li><span>Entered</span><b>{entered_today}</b></li></ul>
    <h2 style="margin-top:14px">Why signals were skipped</h2><ul class="kv">{reason_rows}</ul></div>
  <div class="card"><h2>Alerts today ({report['alerts_today_total']})</h2><ul class="kv">{alert_rows}</ul>
    <h2 style="margin-top:14px">Symbols switched off today</h2><ul class="kv">{disabled_rows}</ul></div>
</div>

<div class="card" style="margin-top:12px"><h2>Equity curve</h2>{_equity_curve(start, closed)}</div>

<div class="card"><h2>Closed trades ({len(closed)})</h2>
<table><thead><tr><th>Closed</th><th>Trade</th><th class="num">Entry → exit</th><th class="num">P/L</th><th class="num">R</th><th>Reason</th></tr></thead>
<tbody>{closed_rows}</tbody></table></div>

<div class="card"><h2>Original $10K ledger · <a href="../../compare.html">side-by-side comparison</a></h2>
  <div>{e(str(orig.get('state') or 'unknown'))} · cash {_money(orig.get('cash'))} · realized {_money(orig.get('realized_pnl'), True)}
  · {orig.get('closed_trades')} closed · {orig.get('open_positions')} open</div></div>

<footer class="muted small">Generated {_local(report['generated_at_utc'], '%a %b %d %I:%M:%S %p')} from the ledger files.
The page reloads every {int(refresh_seconds)} s; the data is regenerated by <code>python -m trader.cli profile-report</code>.</footer>
</main></body></html>
"""


def write_report(root: Path, original_data_dir: Path, profile_name: str = "small") -> Path:
    report = build_report(root, original_data_dir, profile_name)
    out_dir = ProfileConfig.load(root, profile_name).data_dir
    targets = {out_dir / "report.json": json.dumps(report, indent=2, default=str),
               out_dir / "report.html": render_html(report)}
    for target, text in targets.items():
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=target.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        atomic_replace(tmp, target)
    return out_dir / "report.html"
