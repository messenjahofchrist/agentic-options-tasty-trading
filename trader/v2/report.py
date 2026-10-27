"""Professional Strategy dashboard (data/v2_dashboard.html): read-only, built from the engines' own deterministic
records. Each ledger starts with a plain-language summary (today's result, wins and losses, open trades winning or
losing, closed trades and why they closed); the technical detail (market context, regime, events, management state,
every recent candidate report stage by stage) follows in a collapsed section. No AI-written text: the reasons shown
are the recorded rule outcomes."""
from __future__ import annotations

import html
import json
import os
import tempfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..logging_utils import atomic_replace

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")   # the owner's local time; the summary shows times in CT

# Why a position closed, in plain words (paper_close "reason").
CLOSE_REASONS = {
    "underlying_profit_target": "Hit its profit target",
    "underlying_invalidation_stop": "Hit its stop-loss",
    "v2_trailing_stop": "Trailing stop (locked in part of a gain)",
    "v2_runner_stop": "Passed its target, rode it higher, sold on the pullback",
    "v2_breakeven_stop": "Breakeven stop (the gain faded)",
    "v2_time_stop_no_progress": "Closed: no progress in time",
    "v2_partial_profit": "Sold part for profit",
    "end_of_session_close": "Closed at end of day",
    "flatten_at_session_close": "Closed at end of day",
    "time_exit": "Closed: held for the maximum time",
    "emergency_daily_risk_exit": "Closed: daily loss limit reached",
    "market_quote_invalid_emergency_review": "Closed: option price unavailable",
}


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    rows: deque = deque(maxlen=limit)
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return list(rows)


def _local(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).astimezone(ET).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return ts


def _pct(v: Any) -> str:
    return "—" if v is None else f"{float(v) * 100:+.2f}%"


def _money(v: Any) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def _signed(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def _percent(fraction: float) -> str:
    rounded = round(fraction * 100)
    return "0%" if rounded == 0 else f"{'+' if rounded > 0 else '−'}{abs(rounded)}%"


def _tone(v: float) -> str:
    return "up" if v > 0.005 else "down" if v < -0.005 else ""


def _ct(ts: str | datetime | None) -> str:
    if not ts:
        return "—"
    try:
        stamp = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
        return stamp.astimezone(CT).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(ts)


def _bet(right: Any) -> str:
    return "Up ↑" if str(right).upper() in ("C", "CALL") else "Down ↓"


def open_trade(p: dict) -> dict[str, Any]:
    """An open position as it would stand if sold at the current bid (exit fees not included)."""
    quantity = int(p.get("quantity", 0))
    paid = float(p["entry_price"]) * 100 * quantity + float(p.get("entry_fees", 0))
    worth = float(p.get("last_bid") or 0) * 100 * quantity
    return {"paid": paid, "worth": worth, "pnl": worth - paid, "pct": (worth - paid) / paid if paid else 0.0}


def trade_results(account: dict) -> list[dict[str, Any]]:
    """Closed trades, oldest first: the entry fill joined to its paper_close by trade_id."""
    entries = {e["trade_id"]: e for e in account.get("ledger", []) if e.get("type") == "fill" and e.get("side") == "BUY"}
    results = []
    for close in account.get("ledger", []):
        if close.get("type") != "paper_close" or close.get("realized_pnl") is None:
            continue
        entry = entries.get(close.get("trade_id"), {})
        symbol = str(entry.get("symbol", ""))
        right = symbol[12:13] if len(symbol) > 12 else ""
        results.append({"underlying": close.get("underlying") or symbol[:6].strip() or "?", "right": right,
                        "opened": entry.get("timestamp"), "closed": close.get("timestamp"),
                        "pnl": float(close["realized_pnl"]), "reason": str(close.get("reason", ""))})
    return results


def _record(results: list[dict]) -> tuple[int, int, float]:
    won = sum(1 for r in results if r["pnl"] > 0)
    return won, sum(1 for r in results if r["pnl"] < 0), sum(r["pnl"] for r in results)


def _engine_status(heartbeat: dict, now: datetime) -> tuple[str, str]:
    state = str(heartbeat.get("lifecycle_state") or "").upper()
    try:
        age = (now - datetime.fromisoformat(heartbeat["updated_at_utc"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    if state == "RUNNING" and age is not None and age <= 180:
        return "Running", "up"
    if state == "RUNNING":
        return f"Not updating since {_ct(heartbeat.get('updated_at_utc'))}: check the engine", "down"
    if state == "STOPPED":
        return "Stopped (trading day over or not started)", ""
    return state.title() or "Unknown", "down" if state else ""


def _closes_when(p: dict, letting_winners_run: bool = False) -> str:
    """When the engine will sell this position, in plain words (only the exits that are active)."""
    stock, stop, target = p["underlying"], float(p["stop_underlying"]), float(p["target_underlying"])
    m = p.get("v2_management") or {}
    if m.get("runner"):
        falls = "falls" if str(p.get("right", "")).upper() in ("C", "CALL") else "rises"
        return (f"Passed its profit target: letting it run. Sells if {stock} {falls} back to "
                f"${float(m['runner_stop_underlying']):,.2f} (this line only moves in our favor); otherwise at the end of the day.")
    up = str(p.get("right", "")).upper() in ("C", "CALL")
    if letting_winners_run:
        moves = "rises" if up else "falls"
        return (f"If {stock} {moves} to its target ${target:,.2f}, it keeps riding with a rising safety line instead of "
                f"selling. Stop-loss if it {'falls' if up else 'rises'} to ${stop:,.2f}; otherwise sells at the end of the day.")
    if up:
        parts = [f"profit target if {stock} rises to ${target:,.2f}", f"stop-loss if it falls to ${stop:,.2f}"]
    else:
        parts = [f"profit target if {stock} falls to ${target:,.2f}", f"stop-loss if it rises to ${stop:,.2f}"]
    if m.get("trail_stop_underlying") is not None:
        parts.append(f"gain-protecting stop now at ${float(m['trail_stop_underlying']):,.2f}")
    if m.get("breakeven_armed"):
        parts.append("breakeven stop is on")
    return "Sells at the " + "; ".join(parts) + "; otherwise at the end of the day."


def _summary_block(view: dict, now: datetime) -> str:
    e = html.escape
    account = view["account"]
    today = now.astimezone(ET).date()
    results = trade_results(account)
    todays = [r for r in results if r["closed"] and datetime.fromisoformat(r["closed"]).astimezone(ET).date() == today]
    positions = account.get("positions", [])
    opened = [(p, open_trade(p)) for p in positions]
    open_pnl = sum(o["pnl"] for _, o in opened)
    won, lost, realized_today = _record(todays)
    all_won, all_lost, all_realized = _record(results)
    value = float(account.get("cash", 0)) + sum(o["worth"] for _, o in opened)
    start = float(account.get("starting_capital") or 0)
    today_total = realized_today + open_pnl
    winning = sum(1 for _, o in opened if o["pnl"] > 0)
    status, status_tone = _engine_status(view["heartbeat"], now)
    trades_line = (f"{won} won · {lost} lost" if todays else "none closed yet")
    tiles = (
        f"<div class='tile'><div class='label'>Today</div><div class='big {_tone(today_total)}'>{_signed(today_total)}</div>"
        f"<div class='small muted'>{_signed(realized_today)} from closed trades, {_signed(open_pnl)} on open ones</div></div>"
        f"<div class='tile'><div class='label'>Trades closed today</div><div class='big'>{len(todays)}</div>"
        f"<div class='small muted'>{e(trades_line)}</div></div>"
        f"<div class='tile'><div class='label'>Open right now</div><div class='big'>{len(positions)}</div>"
        f"<div class='small muted'>{winning} winning · {len(positions) - winning} losing</div></div>"
        f"<div class='tile'><div class='label'>Account value</div><div class='big'>{_money(value)}</div>"
        f"<div class='small {_tone(value - start)}'>{_signed(value - start)} since start ({_money(start)})</div></div>")
    open_rows = "".join(
        f"<tr><td><b>{e(p['underlying'])}</b></td><td>{_bet(p.get('right'))}</td><td class='num'>{_money(o['paid'])}</td>"
        f"<td class='num'>{_money(o['worth'])}</td><td class='num {_tone(o['pnl'])}'><b>{'Winning' if o['pnl'] > 0 else 'Losing' if o['pnl'] < 0 else 'Even'}"
        f"</b> {_signed(o['pnl'])} ({_percent(o['pct'])})</td><td>{_ct(p.get('entry_time'))}</td>"
        f"<td class='small'>{e(_closes_when(p, bool(view.get('let_winners_run'))))}<div class='muted'>Most it can lose: {_money(p.get('modeled_risk_dollars'))}</div></td></tr>"
        for p, o in opened)
    closed_rows = "".join(
        f"<tr><td><b>{e(r['underlying'])}</b></td><td>{_bet(r['right']) if r['right'] else '—'}</td>"
        f"<td>{_ct(r['opened'])} → {_ct(r['closed'])}</td>"
        f"<td class='num {_tone(r['pnl'])}'><b>{'Won' if r['pnl'] > 0 else 'Lost' if r['pnl'] < 0 else 'Even'}</b> {_signed(r['pnl'])}</td>"
        f"<td>{e(CLOSE_REASONS.get(r['reason'], r['reason'].replace('_', ' ')))}</td></tr>"
        for r in reversed(todays))
    return (f"<p class='small'>Engine: <b class='{status_tone}'>{e(status)}</b></p><div class='tiles'>{tiles}</div>"
            "<h3>Open trades</h3><p class='small muted'>“Up ↑” trades make money if the stock rises, “Down ↓” trades if it falls. "
            "Values are what the option would sell for right now.</p>"
            "<table><thead><tr><th>Stock</th><th>Bet</th><th class='num'>Paid</th><th class='num'>Worth now</th>"
            "<th class='num'>Result so far</th><th>Opened</th><th>When it closes</th></tr></thead><tbody>"
            + (open_rows or "<tr><td colspan='7' class='muted'>No open trades.</td></tr>") + "</tbody></table>"
            "<h3>Closed today</h3><table><thead><tr><th>Stock</th><th>Bet</th><th>Open → close</th>"
            "<th class='num'>Result</th><th>Why it closed</th></tr></thead><tbody>"
            + (closed_rows or "<tr><td colspan='5' class='muted'>No closed trades today.</td></tr>") + "</tbody></table>"
            f"<p class='small'>Since the start: <b>{len(results)}</b> trade{'' if len(results) == 1 else 's'} closed, "
            f"<b>{all_won}</b> won, <b>{all_lost}</b> lost, "
            f"net <b class='{_tone(all_realized)}'>{_signed(all_realized)}</b>"
            + (f" (won {all_won / (all_won + all_lost):.0%} of trades)" if all_won + all_lost else "") + ".</p>")


def _clusters(root: Path, positions: list[dict]) -> list[list[str]]:
    """Correlated exposure groups of the open positions, from the shared daily-history cache (no network)."""
    if not positions:
        return []
    try:
        from .config import load_v2_config
        from .correlation import CorrelationModel
        from .sectors import SectorMap
        config = load_v2_config(root)
        caches = sorted((root / "data" / "v2_shared_cache").glob("daily_*.json"))
        rows = json.loads(caches[-1].read_text(encoding="utf-8"))["payload"]["rows"] if caches else {}
        model = CorrelationModel(config.data["correlation"], SectorMap(config.sector_map),
                                 {s: [(r["session_date"], r["close"]) for r in v] for s, v in rows.items()})
        return model.clusters(positions)
    except Exception:
        return [[f"{p['underlying']} (clusters unavailable)"] for p in positions]


def ledger_view(name: str, data_dir: Path, now: datetime, root: Path | None = None, aggregate_fraction: float | None = None,
                label: str | None = None) -> dict[str, Any]:
    account = _read(data_dir / "paper_account.json", {})
    snapshot = _read(data_dir / "v2_context_snapshot.json", {})
    status = _read(data_dir / "v2_status.json", {})
    heartbeat = _read(data_dir / "engine_heartbeat.json", {})
    shadow = _read(data_dir / "v2_shadow_original" / "paper_account.json", {})
    rows = _tail_jsonl(data_dir / "v2_decisions.jsonl", 4000)
    today = now.astimezone(ET).date().isoformat()
    candidates = [r for r in rows if r.get("type") == "candidate" and str(r.get("timestamp", ""))[:10] >= today[:10]]
    latest: dict[str, dict] = {}
    for r in candidates:
        latest[r["symbol"]] = r
    entries = [r for r in rows if r.get("type") == "entry"]
    management = [r for r in rows if r.get("type") == "management"][-30:]
    outcomes = {}
    for r in candidates:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    positions = account.get("positions", [])
    return {"name": name, "label": label, "account": account, "snapshot": snapshot, "status": status, "heartbeat": heartbeat,
            "shadow": shadow, "latest": latest, "entries": entries[-20:], "management": management,
            "outcome_counts": outcomes, "clusters": _clusters(root, positions) if root else [],
            "aggregate_fraction": aggregate_fraction}


def _regime_block(view: dict) -> str:
    e = html.escape
    snap = view["snapshot"] or {}
    regime = snap.get("regime") or {}
    broad = snap.get("broad") or {}
    rows = "".join(
        f"<tr><td><b>{e(s)}</b></td><td class='num'>{st.get('bias', '—'):+}</td><td class='num'>{_pct(st.get('change_from_open'))}</td>"
        f"<td class='num'>{_pct(st.get('overnight_gap'))}</td><td>{e(str(st.get('vs_prior_range', '—')))}</td>"
        f"<td>{e(str(st.get('vs_premarket_range', '—')))}</td><td class='num'>{st.get('prior_high', '—')} / {st.get('prior_low', '—')}</td>"
        f"<td class='num'>{st.get('premarket_high', '—')} / {st.get('premarket_low', '—')}</td>"
        f"<td class='num'>{'—' if st.get('efficiency') is None else f'{st['efficiency']:.2f}'}</td>"
        f"<td>{'fresh' if st.get('fresh') else '<span class=bad>stale</span>'}</td></tr>"
        for s, st in broad.items() if isinstance(st.get("bias"), int))
    sectors = snap.get("sectors") or {}
    sector_cells = "".join(
        f"<span class='chip {'up' if (st.get('bias') or 0) > 0 else 'down' if (st.get('bias') or 0) < 0 else ''}'>"
        f"{e(s)} {st.get('bias', '?'):+} <small>RS {_pct(st.get('relative_strength_vs_spy'))}</small></span>"
        for s, st in sectors.items() if isinstance(st.get("bias"), int))
    vix = snap.get("vix") or {}
    breadth = snap.get("breadth") or {}
    events = "".join(f"<li>{e(ev['date'])} {e(ev['time_et'])} ET — {e(ev['name'])}"
                     f"{'' if ev.get('time_verified') else ' <small>(time customary)</small>'}</li>"
                     for ev in snap.get("events_upcoming") or [])
    return (f"<h3>Market regime: <span class='pill'>{e(regime.get('regime', 'n/a'))}</span> "
            f"<small>rule {e(str(regime.get('rule', '')))}; allows {e(', '.join(regime.get('allowed_directions', [])) or 'nothing')}</small></h3>"
            f"<p class='small muted'>{e('; '.join(regime.get('notes', [])))}</p>"
            f"<table><thead><tr><th></th><th>Bias</th><th>vs open</th><th>Gap</th><th>vs prior day</th><th>vs premarket</th>"
            f"<th>Prior H/L</th><th>Premarket H/L</th><th>Eff.</th><th>Data</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p>VIX {vix.get('value', '—')} ({_pct(vix.get('change_fraction'))} vs prior close) · Breadth above VWAP "
            f"{breadth.get('above_vwap', '—')}/{breadth.get('counted', '—')}</p><div>{sector_cells}</div>"
            f"<h4>Scheduled events (next 7 days)</h4><ul>{events or '<li>none</li>'}</ul>")


def _positions_block(view: dict) -> str:
    e = html.escape
    rows = []
    for p in view["account"].get("positions", []):
        m = p.get("v2_management") or {}
        v2 = p.get("v2_entry") or {}
        rows.append(f"<tr><td><b>{e(p['underlying'])}</b><div class='small muted'>{e(p['symbol'])}</div></td>"
                    f"<td>{e(str(v2.get('setup_type', 'original')))}<div class='small muted'>{e(str(v2.get('regime', '')))}</div></td>"
                    f"<td class='num'>{_money(p['entry_price'])} → {_money(p.get('last_bid'))}</td>"
                    f"<td class='num'>{p['stop_underlying']:.2f} / {p['target_underlying']:.2f}</td>"
                    f"<td>{'armed ' + _money(m.get('breakeven_option_floor')) if m.get('breakeven_armed') else 'not armed'}</td>"
                    f"<td>{m.get('trail_stop_underlying', 'not armed')}</td><td>{'stalled' if m.get('stalled') else ''}"
                    f"{' partial taken' if m.get('partial_taken') else ''}{' ' + ', '.join(m.get('review', [])) if m.get('review') else ''}</td>"
                    f"<td class='num'>{_money(p.get('modeled_risk_dollars'))}</td></tr>")
    return ("<table><thead><tr><th>Position</th><th>Setup</th><th>Entry → bid</th><th>Stop / target (underlying)</th>"
            "<th>Breakeven</th><th>Trailing stop</th><th>Status</th><th>Max loss</th></tr></thead><tbody>"
            + ("".join(rows) or "<tr><td colspan='8' class='muted'>No open positions.</td></tr>") + "</tbody></table>")


def _candidates_block(view: dict) -> str:
    e = html.escape
    rows = []
    for symbol, r in sorted(view["latest"].items(), key=lambda kv: kv[1].get("timestamp", ""), reverse=True)[:25]:
        stages = r.get("stages", {})
        cells = "".join(f"<td class='st {e(s.get('status', ''))}' title='{e(str(s.get('reason', '')))}'>{e(s.get('status', '')[:4])}</td>"
                        for s in stages.values())
        rows.append(f"<tr><td><b>{e(symbol)}</b> {e(str(r.get('side') or ''))}<div class='small muted'>{_local(r.get('timestamp'))}</div></td>"
                    f"<td><b class='{e(r.get('outcome', ''))}'>{e(r.get('outcome', ''))}</b><div class='small'>{e('; '.join(r.get('reasons', []))[:220])}</div></td>{cells}</tr>")
    header = "".join(f"<th class='st'>{i}</th>" for i in range(1, 13))
    return ("<p class='small muted'>Stages: 1 technical, 2 confirmation, 3 regime, 4 sector, 5 volume/VWAP, 6 events, "
            "7 correlation, 8 option pricing, 9 IV, 10 liquidity, 11 sizing/risk, 12 final. Hover a cell for its recorded reason.</p>"
            f"<table><thead><tr><th>Candidate</th><th>Decision</th>{header}</tr></thead><tbody>"
            + ("".join(rows) or "<tr><td colspan='14' class='muted'>No candidates yet today.</td></tr>") + "</tbody></table>")


def render(views: list[dict], now: datetime) -> str:
    e = html.escape
    sections = []
    for v in views:
        account = v["account"]
        equity = account.get("cash", 0) + sum(float(p.get("last_bid", 0)) * 100 * int(p.get("quantity", 0)) for p in account.get("positions", []))
        status = v["status"]
        exposure = sum(float(p.get("modeled_risk_dollars", 0)) for p in account.get("positions", []))
        cap = equity * v["aggregate_fraction"] if v.get("aggregate_fraction") else None
        remaining = "—" if cap is None else _money(max(0.0, cap - exposure))
        clusters = " · ".join("[" + ", ".join(group) + "]" for group in v.get("clusters") or []) or "none"
        shadow = v["shadow"]
        sections.append(
            f"<section class='card'><h2>{e(v.get('label') or v['name'])}</h2>{_summary_block(v, now)}"
            f"<details><summary>Technical details: market conditions, strategy checks, every candidate</summary>"
            f"<p class='small muted'>{e(v['name'])}</p>"
            f"<p>Strategy <b>{e(str(status.get('strategy_hash', ''))[:12])}</b> (base {e(str(status.get('base_strategy_hash', ''))[:12])}) · "
            f"engine <b>{e(str(v['heartbeat'].get('lifecycle_state', 'n/a')))}</b> · equity <b>{_money(equity)}</b> "
            f"(start {_money(account.get('starting_capital'))}) · open max loss {_money(exposure)} · remaining aggregate "
            f"budget {remaining} · correlated clusters {e(clusters)} · "
            f"original-strategy shadow: {_money(shadow.get('cash'))} cash, realized {_money(shadow.get('realized_pnl'))}</p>"
            f"<p class='small'>Today's candidate outcomes: {e(json.dumps(v['outcome_counts']))}</p>"
            f"{_regime_block(v)}<h3>Positions</h3>{_positions_block(v)}<h3>Candidates (latest per symbol)</h3>{_candidates_block(v)}"
            "</details></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Paper Trading Results</title><style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#16181d; --muted:#6b7280; --line:#e5e7eb; --up:#0f7b3f; --down:#b42318; --warn:#a15c00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --text:#e8eaed; --muted:#9aa1ac; --line:#2a2f37; --up:#4ccb7f; --down:#ff7a6e; --warn:#f0b450; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,"Segoe UI",sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:16px; }} .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px; margin-bottom:14px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }} th,td {{ text-align:left; padding:5px 6px; border-top:1px solid var(--line); vertical-align:top; }}
.num {{ text-align:right; white-space:nowrap; }} .small {{ font-size:12px; }} .muted {{ color:var(--muted); }} .bad {{ color:var(--down); }}
.pill {{ border:1px solid currentColor; border-radius:999px; padding:1px 8px; }} .chip {{ display:inline-block; margin:2px; padding:2px 8px; border:1px solid var(--line); border-radius:6px; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} td.st {{ text-align:center; font-size:11px; }} th.st {{ text-align:center; }}
td.PASS {{ color:var(--up); }} td.REJECT, .REJECTED {{ color:var(--down); }} td.WAIT, .WAIT_FOR_CONFIRMATION {{ color:var(--warn); }}
td.INSUFFICIENT_DATA, .INSUFFICIENT_DATA {{ color:var(--warn); }} .APPROVED {{ color:var(--up); }} td.NOT_EVALUATED, td.NOT_APPLICABLE {{ color:var(--muted); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:10px; margin:8px 0 4px; }}
.tile {{ border:1px solid var(--line); border-radius:8px; padding:10px 12px; }} .label {{ font-size:12px; color:var(--muted); }}
.big {{ font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; }}
details {{ margin-top:16px; border-top:1px solid var(--line); padding-top:10px; }} summary {{ cursor:pointer; color:var(--muted); font-weight:600; }}
</style></head><body><main><h1>Paper Trading Results</h1>
<p class="small muted">Practice money only: no real orders are placed. Updated {_ct(now)} CT; this page refreshes every minute.</p>
{''.join(sections)}</main></body></html>"""


def write_dashboard(root: Path, original_data_dir: Path, now: datetime | None = None) -> Path:
    from ..account_profiles import ProfileConfig
    now = now or datetime.now(timezone.utc)
    small = ProfileConfig.load(root, "small")
    from .config import load_v2_config
    v2_data = load_v2_config(root).data
    fractions = v2_data["portfolio"]["aggregate_max_loss_fraction"]
    views = [ledger_view("$10K ledger — ORIGINAL_ACCOUNT_PAPER", Path(original_data_dir), now, root,
                         fractions["ORIGINAL_ACCOUNT_PAPER"], label="$10,000 account"),
             ledger_view("$1K ledger — SMALL_ACCOUNT_PAPER", small.data_dir, now, root, fractions["SMALL_ACCOUNT_PAPER"],
                         label="$1,000 account")]
    for view in views:
        view["let_winners_run"] = bool(v2_data["management"]["let_winners_run"]["enabled"])
    out = Path(original_data_dir) / "v2_dashboard.html"
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(render(views, now))
    atomic_replace(tmp, out)
    return out
