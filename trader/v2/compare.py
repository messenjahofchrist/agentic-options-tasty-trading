"""Original-strategy shadow vs PROFESSIONAL_STRATEGY_V2, from the PAPER ledgers (read-only).

For each ledger: V2's closed trades (the ledger itself) against the original strategy's closed trades on the same
data (v2_shadow_original), after fees and modeled slippage, plus the signals V2 did not take and their hypothetical
underlying outcomes (v2_signal_outcomes.jsonl). A short sample proves nothing; the report says how many trades
each figure rests on.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _closed(account: dict) -> list[dict]:
    """Whole-trade results: partial closes of one trade are summed into that trade."""
    fills = {f["trade_id"]: f for f in account.get("ledger", []) if f.get("type") == "fill" and f.get("side") == "BUY"}
    trades: dict[str, dict] = {}
    for row in account.get("ledger", []):
        if row.get("type") != "paper_close":
            continue
        t = trades.setdefault(row["trade_id"], {"trade_id": row["trade_id"], "underlying": row.get("underlying"),
                                                "pnl": 0.0, "risk": 0.0, "reasons": [], "closed_at": row["timestamp"]})
        t["pnl"] += float(row["realized_pnl"])
        t["risk"] += float(row.get("modeled_risk_dollars") or 0)
        t["reasons"].append(row.get("reason"))
        t["closed_at"] = row["timestamp"]
        t["entry_fees"] = float(fills.get(row["trade_id"], {}).get("fees", 0))
    return list(trades.values())


def metrics(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    pnl = [t["pnl"] for t in trades]
    r = [t["pnl"] / t["risk"] for t in trades if t["risk"] > 0]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    equity = peak = dd = 0.0
    for p in pnl:
        equity += p; peak = max(peak, equity); dd = max(dd, peak - equity)
    return {"trades": len(pnl), "net_pnl": round(sum(pnl), 2), "win_rate": round(len(wins) / len(pnl), 3),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "expectancy_r": round(sum(r) / len(r), 3) if r else None, "max_drawdown": round(dd, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None}


def ledger_comparison(name: str, data_dir: Path) -> dict[str, Any]:
    v2_account = _read(data_dir / "paper_account.json", {})
    shadow = _read(data_dir / "v2_shadow_original" / "paper_account.json", {})
    v2_trades = _closed(v2_account)
    positions = {p.get("trade_id"): p for p in v2_account.get("positions", [])}
    entries = {r["trade_id"]: r for r in _jsonl(data_dir / "v2_decisions.jsonl") if r.get("type") == "entry"}
    by_regime: dict[str, list] = defaultdict(list)
    by_setup: dict[str, list] = defaultdict(list)
    for t in v2_trades:
        e = entries.get(t["trade_id"], {})
        by_regime[str(e.get("regime"))].append(t)
        by_setup[str(e.get("setup_type"))].append(t)
    outcomes = _jsonl(data_dir / "v2_signal_outcomes.jsonl")
    untaken_r = [o["hypothetical_underlying_r"] for o in outcomes]
    return {"ledger": name, "v2": metrics(v2_trades), "original_shadow": metrics(_closed(shadow)),
            "fees": {"v2": round(float(v2_account.get("fees_paid", 0)), 2), "original_shadow": round(float(shadow.get("fees_paid", 0)), 2)},
            "slippage": {"v2": round(float(v2_account.get("slippage_paid", 0)), 2),
                         "original_shadow": round(float(shadow.get("slippage_paid", 0)), 2)},
            "v2_by_regime": {k: metrics(v) for k, v in by_regime.items()},
            "v2_by_setup_type": {k: metrics(v) for k, v in by_setup.items()},
            "signals_not_taken_by_v2": {"count": len(outcomes),
                                        "hypothetical_underlying_r_mean": round(sum(untaken_r) / len(untaken_r), 3) if untaken_r else None,
                                        "model": "underlying proxy after the decision bar"},
            "confirmation_delay_minutes": [e.get("confirmation_delay_minutes") for e in entries.values()],
            "open_positions": len(positions)}


def compare_ledgers(root: Path, original_data_dir: Path) -> dict[str, Any]:
    from ..account_profiles import ProfileConfig
    small = ProfileConfig.load(root, "small")
    return {"note": "Do not judge V2 on a short period or a few trades; see the trade counts.",
            "ledgers": [ledger_comparison("ORIGINAL_ACCOUNT_PAPER ($10K)", Path(original_data_dir)),
                        ledger_comparison("SMALL_ACCOUNT_PAPER ($1K)", small.data_dir)]}
