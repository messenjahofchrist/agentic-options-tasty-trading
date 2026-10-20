"""End-of-day summary for the small-account PAPER ledger (read-only).

`python -m trader.cli profile-eod` lists every contract bought today with its exit, plus the day's opening and
closing ledger, and writes eod/eod-YYYY-MM-DD.json and .html into the profile's data directory. It emails the
summary and pushes a one-line result to the phone when those channels are configured. A position still open
when it runs breaks the no-overnight rule (the engine flattens ~1 minute before the close), so the push is
urgent and the subject says so. No broker calls; writes only the two files and the notifications.
"""
from __future__ import annotations

import html
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .account_profiles import ProfileConfig
from .logging_utils import append_jsonl, atomic_replace
from .notifications import email_configured, push_configured, send_email, send_push
from .profile_report import _local, _money, _option_label, _pnl_class, _read_json

MARKET_TZ = ZoneInfo("America/New_York")


def _trading_day(ts: str) -> date:
    return datetime.fromisoformat(ts).astimezone(MARKET_TZ).date()


def _cash_flow(fill: dict[str, Any]) -> float:
    gross = float(fill["fill_price"]) * 100 * int(fill.get("quantity", 1))
    fees = float(fill.get("fees") or 0)
    return -(gross + fees) if fill.get("side") == "BUY" else gross - fees


ORIGINAL = "original"


def _source(root: Path, profile_name: str, original_data_dir: Path | None) -> tuple[str, str, Path, float]:
    """(ledger_id, label, data directory, starting balance) of an account profile or the original ledger."""
    if profile_name == ORIGINAL:
        return "original_paper", "$10K account", Path(original_data_dir or root / "data"), 10_000.0
    config = ProfileConfig.load(root, profile_name)
    return config.ledger_id, "small account", config.data_dir, config.starting_balance


def build_eod(root: Path, profile_name: str = "small", day: date | None = None,
              now: datetime | None = None, original_data_dir: Path | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    day = day or now.astimezone(MARKET_TZ).date()
    ledger_id, label, data_dir, starting_balance = _source(root, profile_name, original_data_dir)
    account = _read_json(data_dir / "paper_account.json", {})
    ledger = account.get("ledger", [])
    fills_today = [f for f in ledger if f.get("type") == "fill" and _trading_day(f["timestamp"]) == day]
    closes = {c["trade_id"]: c for c in ledger if c.get("type") == "paper_close"}
    # A closing fill carries its own id; it shares the option symbol and bar timestamp with its paper_close.
    sells = {(f.get("symbol"), f.get("timestamp")): f for f in ledger if f.get("type") == "fill" and f.get("side") == "SELL"}
    open_ids = {p.get("trade_id") for p in account.get("positions", [])}

    contracts = []
    for buy in (f for f in fills_today if f.get("side") == "BUY"):
        qty = int(buy.get("quantity", 1))
        close = closes.get(buy["trade_id"])
        sell = sells.get((buy.get("symbol"), close.get("timestamp"))) if close else None
        contracts.append({
            "underlying": buy.get("underlying"), "contract": _option_label(buy.get("symbol", "")),
            "option_symbol": buy.get("symbol"), "right": "call" if (buy.get("symbol") or "")[-9:-8] == "C" else "put",
            "quantity": qty, "bought_at": buy["timestamp"], "buy_price": round(float(buy["fill_price"]), 4),
            "cost": round(-_cash_flow(buy), 2),
            "sold_at": close.get("timestamp") if close else None,
            "sell_price": None if not sell else round(float(sell["fill_price"]), 4),
            "exit_reason": close.get("reason") if close else ("STILL OPEN" if buy["trade_id"] in open_ids else "unknown"),
            "pnl": None if not close else round(float(close["realized_pnl"]), 2),
            "r_multiple": None if not close or close.get("actual_r_multiple") is None else round(float(close["actual_r_multiple"]), 2)})

    cash_end = float(account.get("cash", 0))
    cash_start = cash_end - sum(_cash_flow(f) for f in fills_today)
    closed_today = [c for c in closes.values() if _trading_day(c["timestamp"]) == day]
    realized_today = sum(float(c["realized_pnl"]) for c in closed_today)
    week = day.isocalendar()[:2]
    realized_week = sum(float(c["realized_pnl"]) for c in closes.values()
                        if _trading_day(c["timestamp"]).isocalendar()[:2] == week)
    open_positions = [{"underlying": p.get("underlying"), "contract": _option_label(p.get("symbol", "")),
                       "entry_time": p.get("entry_time"), "entry_price": p.get("entry_price"), "last_bid": p.get("last_bid")}
                      for p in account.get("positions", [])]
    wins = sum(1 for c in closed_today if float(c["realized_pnl"]) > 0)
    return {
        "generated_at_utc": now.isoformat(), "trading_day": day.isoformat(), "ledger_id": ledger_id, "label": label, "mode": "PAPER",
        "contracts": contracts,
        "ledger": {"starting_balance": float(account.get("starting_capital", starting_balance)),
                   "day_start_cash": round(cash_start, 2), "day_end_cash": round(cash_end, 2),
                   "day_realized_pnl": round(realized_today, 2), "week_realized_pnl": round(realized_week, 2),
                   "total_realized_pnl": round(float(account.get("realized_pnl", 0)), 2),
                   "day_fees": round(sum(float(f.get("fees") or 0) for f in fills_today), 2),
                   "trades_closed": len(closed_today), "wins": wins, "losses": len(closed_today) - wins},
        "open_positions": open_positions,
        "overnight_violation": bool(open_positions),
    }


def _reason(text: str | None) -> str:
    return (text or "—").replace("_", " ")


def render_eod_text(eod: dict[str, Any]) -> str:
    led = eod["ledger"]
    lines = [f"OptioBot {eod['label']} (PAPER) - {eod['trading_day']}", ""]
    if eod["overnight_violation"]:
        lines += [f"*** {len(eod['open_positions'])} POSITION(S) STILL OPEN AFTER THE CLOSE - nothing should carry overnight ***",
                  *[f"    {p['underlying']} {p['contract']} bought {_local(p['entry_time'])}" for p in eod["open_positions"]], ""]
    lines.append(f"Contracts bought today: {len(eod['contracts'])}")
    for i, c in enumerate(eod["contracts"], 1):
        exit_text = (f"sold {_local(c['sold_at'], '%I:%M %p')} at ${c['sell_price']:.2f} ({_reason(c['exit_reason'])}) "
                     f"P/L {_money(c['pnl'], sign=True)}" if c["pnl"] is not None else c["exit_reason"])
        lines.append(f" {i}. {c['underlying']} {c['contract']} x{c['quantity']}: bought {_local(c['bought_at'], '%I:%M %p')} "
                     f"at ${c['buy_price']:.2f} (cost {_money(c['cost'])}); {exit_text}")
    lines += ["", "Ledger",
              f"  Day start cash:  {_money(led['day_start_cash'])}",
              f"  Day end cash:    {_money(led['day_end_cash'])}",
              f"  Day P/L:         {_money(led['day_realized_pnl'], sign=True)}  ({led['trades_closed']} closed: "
              f"{led['wins']} won, {led['losses']} lost; fees {_money(led['day_fees'])})",
              f"  Week P/L:        {_money(led['week_realized_pnl'], sign=True)}",
              f"  Since start:     {_money(led['total_realized_pnl'], sign=True)} on {_money(led['starting_balance'])}",
              f"  Open positions:  {len(eod['open_positions'])}",
              "", "Paper trading only. No broker order was placed."]
    return "\n".join(lines)


def render_eod_html(eod: dict[str, Any]) -> str:
    esc = html.escape
    led = eod["ledger"]
    def price(value: float | None) -> str:
        return "—" if value is None else f"${value:.2f}"

    rows = "".join(
        f"<tr><td>{esc(str(c['underlying']))}</td><td>{esc(c['contract'])}</td><td>{c['quantity']}</td>"
        f"<td>{esc(_local(c['bought_at'], '%I:%M %p'))}</td><td>{price(c['buy_price'])}</td><td>{_money(c['cost'])}</td>"
        f"<td>{esc(_local(c['sold_at'], '%I:%M %p')) if c['sold_at'] else '—'}</td>"
        f"<td>{price(c['sell_price'])}</td>"
        f"<td>{esc(_reason(c['exit_reason']))}</td><td class='{_pnl_class(c['pnl'])}'>{_money(c['pnl'], sign=True)}</td></tr>"
        for c in eod["contracts"]) or "<tr><td colspan='10' class='muted'>No contracts bought today.</td></tr>"
    warning = (f"<div class='warn'>{len(eod['open_positions'])} position(s) still open after the close. "
               "Nothing should carry overnight.</div>" if eod["overnight_violation"] else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>OptioBot Day Summary</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--pos:#18794e;--neg:#c2410c;--warn:#fde68a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#9a9993;--card:#1f1f1d;--line:#33332f;--pos:#4ade80;--neg:#fb923c;--warn:#713f12}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1000px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0;overflow-x:auto}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}} .warn{{background:var(--warn);padding:12px;border-radius:8px;font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}} .grid b{{display:block;font-size:20px}}
</style></head><body><main>
<h1>Day summary · {esc(eod['trading_day'])}</h1><div class="muted">{esc(eod['label'])} · PAPER · generated {esc(_local(eod['generated_at_utc']))}</div>
{warning}
<div class="card grid">
<div>Day start cash<b>{_money(led['day_start_cash'])}</b></div><div>Day end cash<b>{_money(led['day_end_cash'])}</b></div>
<div>Day P/L<b class="{_pnl_class(led['day_realized_pnl'])}">{_money(led['day_realized_pnl'], sign=True)}</b></div>
<div>Week P/L<b class="{_pnl_class(led['week_realized_pnl'])}">{_money(led['week_realized_pnl'], sign=True)}</b></div>
<div>Trades<b>{led['trades_closed']} ({led['wins']}W / {led['losses']}L)</b></div><div>Fees<b>{_money(led['day_fees'])}</b></div>
</div>
<div class="card"><table><thead><tr><th>Stock</th><th>Contract</th><th>Qty</th><th>Bought</th><th>Price</th><th>Cost</th>
<th>Sold</th><th>Price</th><th>Why</th><th>P/L</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="muted">Since start: {_money(led['total_realized_pnl'], sign=True)} on {_money(led['starting_balance'])}. Paper trading only; no broker order was placed.</p>
</main></body></html>"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    atomic_replace(Path(tmp), path)


def run_eod(root: Path, profile_name: str = "small", *, notify: bool = True,
            now: datetime | None = None, original_data_dir: Path | None = None) -> dict[str, Any]:
    eod = build_eod(root, profile_name, now=now, original_data_dir=original_data_dir)
    out = _source(root, profile_name, original_data_dir)[2] / "eod"
    _write(out / f"eod-{eod['trading_day']}.json", json.dumps(eod, indent=2) + "\n")
    _write(out / f"eod-{eod['trading_day']}.html", render_eod_html(eod))
    result = {"trading_day": eod["trading_day"], "html": str(out / f"eod-{eod['trading_day']}.html"),
              "overnight_violation": eod["overnight_violation"], "email": "not_configured", "push": "not_configured"}
    if notify:
        led = eod["ledger"]
        flag = f"{len(eod['open_positions'])} OPEN OVERNIGHT - " if eod["overnight_violation"] else ""
        headline = (f"{flag}{len(eod['contracts'])} bought, day {_money(led['day_realized_pnl'], sign=True)}, "
                    f"cash {_money(led['day_end_cash'])}")
        if email_configured():
            try:
                send_email(f"OptioBot {eod['label']} {eod['trading_day']}: {headline}", render_eod_text(eod))
                result["email"] = "sent"
            except Exception as exc:  # best effort; the files are the record
                result["email"] = f"failed: {type(exc).__name__}"
        if push_configured():
            try:
                send_push(f"OptioBot {eod['label']} {eod['trading_day']}", headline,
                          priority="urgent" if eod["overnight_violation"] else "default",
                          tags="rotating_light" if eod["overnight_violation"] else "bar_chart")
                result["push"] = "sent"
            except Exception as exc:
                result["push"] = f"failed: {type(exc).__name__}"
    append_jsonl(out / "eod_runs.jsonl", {**result, "timestamp_utc": datetime.now(timezone.utc).isoformat()})
    return result
