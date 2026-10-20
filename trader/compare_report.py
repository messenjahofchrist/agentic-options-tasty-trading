"""Side-by-side HTML comparison of the original PAPER ledger and the small-account PAPER ledger (read-only).

Both ledgers run the same strategy on the same days; only the account differs. Because they start at different
sizes, the page compares percentages as well as dollars, and lines up realized P/L by trading day.
`python -m trader.cli profile-report` writes compare.html and compare.json into the original data directory next
to the small-account report. Reads ledger files only: no broker calls, no writes outside the two files.
"""
from __future__ import annotations

import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .account_profiles import ProfileConfig
from .logging_utils import atomic_replace
from .profile_report import _local, _money, _pnl_class, _r, _read_json, ledger_view

MARKET_TZ = ZoneInfo("America/New_York")


def _day(ts: str | None) -> str | None:
    return datetime.fromisoformat(ts).astimezone(MARKET_TZ).date().isoformat() if ts else None


def _summary(name: str, data_dir: Path, now: datetime) -> dict[str, Any]:
    account = _read_json(data_dir / "paper_account.json", {})
    view = ledger_view(data_dir, now)
    start = float(account.get("starting_capital") or 0)
    cash = float(account.get("cash", start))
    marked = sum(float(p.get("last_bid") or 0) * 100 * int(p.get("quantity", 0)) for p in account.get("positions", []))
    equity = cash + marked
    closed = view["closed_trades"]
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
    by_day: dict[str, float] = {}
    for trade in closed:
        day = _day(trade["exit_time"])
        if day:
            by_day[day] = by_day.get(day, 0.0) + trade["pnl"]
    today = now.astimezone(MARKET_TZ).date().isoformat()
    return {
        "name": name, "label": f"${start / 1000:g}K account" if start else name, "starting_balance": start,
        "cash": round(cash, 2), "open_market_value": round(marked, 2), "equity": round(equity, 2),
        "change": round(equity - start, 2), "change_pct": round((equity - start) / start * 100, 2) if start else None,
        "realized_pnl": round(float(account.get("realized_pnl") or 0), 2),
        "today_realized": round(by_day.get(today, 0.0), 2),
        "fees_paid": round(float(account.get("fees_paid") or 0), 2),
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "by_day": {day: round(pnl, 2) for day, pnl in sorted(by_day.items())},
        **view,
    }


def build_comparison(root: Path, original_data_dir: Path, profile_name: str = "small",
                     now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    small = ProfileConfig.load(root, profile_name)
    ledgers = [_summary("original_paper", original_data_dir, now), _summary(small.ledger_id, small.data_dir, now)]
    days = sorted({day for ledger in ledgers for day in ledger["by_day"]})
    return {"generated_at_utc": now.isoformat(), "mode": "PAPER", "ledgers": ledgers, "days": days}


def render_comparison_html(report: dict[str, Any], refresh_seconds: int = 60) -> str:
    e = html.escape
    big, small = report["ledgers"]

    def pct(value: Any) -> str:
        return "—" if value is None else f"{float(value):+.2f}%"

    def metric(label: str, cells: list[str], note: str = "") -> str:
        note_html = f"<div class='muted small'>{e(note)}</div>" if note else ""
        return f"<tr><th scope='row'>{e(label)}{note_html}</th>" + "".join(f"<td class='num'>{c}</td>" for c in cells) + "</tr>"

    def engine(ledger: dict[str, Any]) -> str:
        state = ledger["engine"]["state"]
        return f"<span class='pill {({'RUNNING': 'ok', 'NOT RESPONDING': 'bad'}).get(state, 'warn')}'>{e(state)}</span>"

    rows = "".join([
        metric("Engine", [engine(big), engine(small)]),
        metric("Started with", [_money(x["starting_balance"]) for x in (big, small)]),
        metric("Equity now", [f"<b>{_money(x['equity'])}</b>" for x in (big, small)], "cash plus open positions at the bid"),
        metric("Return since start", [f"<b class='{_pnl_class(x['change'])}'>{pct(x['change_pct'])}</b>"
                                      f"<div class='muted small'>{_money(x['change'], True)}</div>" for x in (big, small)],
               "the fair comparison: percent, not dollars"),
        metric("Realized today", [f"<span class='{_pnl_class(x['today_realized'])}'>{_money(x['today_realized'], True)}</span>"
                                  for x in (big, small)]),
        metric("Closed trades", [f"{x['closed']} <span class='muted small'>({x['wins']} won)</span>" for x in (big, small)]),
        metric("Win rate", ["—" if x["win_rate"] is None else f"{x['win_rate']}%" for x in (big, small)]),
        metric("Average win / loss", [f"<span class='up'>{_money(x['avg_win'], True)}</span> / "
                                      f"<span class='down'>{_money(x['avg_loss'], True)}</span>" for x in (big, small)]),
        metric("Fees paid", [_money(x["fees_paid"]) for x in (big, small)]),
        metric("Open positions", [str(len(x["positions"])) for x in (big, small)]),
    ])

    day_rows = "".join(
        "<tr><td>" + e(datetime.fromisoformat(day).strftime("%a %b %d")) + "</td>" + "".join(
            f"<td class='num {_pnl_class(x['by_day'].get(day))}'>"
            + ("—" if day not in x["by_day"] else
               f"{_money(x['by_day'][day], True)}<div class='muted small'>"
               f"{pct(x['by_day'][day] / x['starting_balance'] * 100 if x['starting_balance'] else None)}</div>")
            + "</td>" for x in (big, small)) + "</tr>"
        for day in reversed(report["days"])) or "<tr><td colspan='3' class='muted'>No closed trades yet.</td></tr>"

    def column(ledger: dict[str, Any]) -> str:
        positions = "".join(
            f"<tr><td><b>{e(p['underlying'] or '')}</b><div class='muted small'>{e(p['option'])}</div></td>"
            f"<td class='num'>{_money(p['entry_price'])} → {_money(p['last_bid'])}</td>"
            f"<td class='num {_pnl_class(p['mark_pnl'])}'>{_money(p['mark_pnl'], True)}</td></tr>"
            for p in ledger["positions"]) or "<tr><td colspan='3' class='muted'>No open positions.</td></tr>"
        trades = "".join(
            f"<tr><td>{_local(t['exit_time'], '%b %d %I:%M %p')}</td>"
            f"<td><b>{e(t['underlying'] or '')}</b><div class='muted small'>{e(t['option'])}</div></td>"
            f"<td class='num {_pnl_class(t['pnl'])}'>{_money(t['pnl'], True)}<div class='muted small'>{_r(t['r_multiple'])}</div></td></tr>"
            for t in list(reversed(ledger["closed_trades"]))[:20]) or "<tr><td colspan='3' class='muted'>No closed trades yet.</td></tr>"
        return (f"<section class='card'><h2>{e(ledger['label'])}</h2>"
                f"<h3>Open positions ({len(ledger['positions'])})</h3>"
                f"<table><thead><tr><th>Trade</th><th class='num'>Entry → bid</th><th class='num'>P/L</th></tr></thead>"
                f"<tbody>{positions}</tbody></table>"
                f"<h3>Recent closed trades</h3>"
                f"<table><thead><tr><th>Closed</th><th>Trade</th><th class='num'>P/L</th></tr></thead>"
                f"<tbody>{trades}</tbody></table></section>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh_seconds)}">
<title>Account Comparison</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#16181d; --muted:#6b7280; --line:#e5e7eb;
        --up:#0f7b3f; --down:#b42318; --warn:#a15c00; --accent:#2f5bd3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --text:#e8eaed; --muted:#9aa1ac;
        --line:#2a2f37; --up:#4ccb7f; --down:#ff7a6e; --warn:#f0b450; --accent:#7c9cff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1040px; margin:0 auto; padding:20px 16px 40px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:0 0 10px; }}
h3 {{ font-size:13px; margin:14px 0 4px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; overflow-x:auto; }}
.cols {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }}
.cols .card {{ margin:0; }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:8px 6px; border-top:1px solid var(--line); vertical-align:top; }}
thead th {{ font-size:12px; color:var(--muted); font-weight:600; border-top:none; }}
th[scope=row] {{ font-weight:500; }}
.num {{ text-align:right; white-space:nowrap; }}
.pill {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; border:1px solid currentColor; }}
.ok, .up {{ color:var(--up); }} .bad, .down {{ color:var(--down); }} .warn {{ color:var(--warn); }}
.muted {{ color:var(--muted); }} .small {{ font-size:12px; }}
</style></head>
<body><main>
<h1>$10K vs $1K · PAPER</h1>
<p class="muted small">Same strategy, same days, different account size. The $1K account also runs the small-account
rules: 40% of equity at risk at once, at most $150 per trade, and the expanded symbol list.</p>

<div class="card"><table>
<thead><tr><th></th><th class="num">{e(big['label'])}</th><th class="num">{e(small['label'])}</th></tr></thead>
<tbody>{rows}</tbody></table></div>

<div class="card"><h2>Realized P/L by day</h2><table>
<thead><tr><th>Day</th><th class="num">{e(big['label'])}</th><th class="num">{e(small['label'])}</th></tr></thead>
<tbody>{day_rows}</tbody></table>
<p class="muted small">The percentage is of each account's starting balance.</p></div>

<div class="cols">{column(big)}{column(small)}</div>

<p class="muted small">Generated {_local(report['generated_at_utc'], '%a %b %d %I:%M:%S %p')} from the ledger files. The page
reloads every {int(refresh_seconds)} s.</p>
</main></body></html>
"""


def write_comparison(root: Path, original_data_dir: Path, profile_name: str = "small") -> Path:
    report = build_comparison(root, original_data_dir, profile_name)
    out_dir = Path(original_data_dir)
    for target, text in {out_dir / "compare.json": json.dumps(report, indent=2, default=str),
                         out_dir / "compare.html": render_comparison_html(report)}.items():
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=target.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        atomic_replace(tmp, target)
    return out_dir / "compare.html"
