"""Option trading cost measured in R (review 2026-11-11, recommendations 1 and 3).

R is a trade's planned loss on the stock: the stop distance. A long option moves about delta x the stock move, so one
R of stock move is worth about  |delta| x stop distance x 100  dollars on one contract. The round-trip cost of trading
that contract (the bid/ask gap paid going in and coming out, plus fees and modeled slippage) divided by that amount is
the cost in R: how much of the planned risk is spent before the trade can earn anything.

    cost_r = (spread x 100 + round-trip fees + 2 x slippage) / (|delta| x stop distance x 100)

The same cost can be written per symbol as basis points of the stock price (cost / (|delta| x price x 0.01)); dividing
by the stop as a fraction of price gives cost_r again, without needing the option's delta. That per-symbol table
(data/option_cost_table.json), measured on the recorded real quotes, lets the replay show results net of option cost.

    python -m trader.cost_model build        # rebuild the table from data/option_history (after the close)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from .quote_history import ET, QuoteHistory, select_contract

TABLE_FILE = "data/option_cost_table.json"
SAMPLE_TIMES_ET = (time(10, 0), time(10, 45), time(11, 30), time(12, 30), time(13, 30), time(14, 30), time(15, 15))
CONTRACTS = {"standard_7_21_dte": {"min_dte": 7, "max_dte": 21, "nearest_expiry_first": False},
             "short_1_6_dte": {"min_dte": 1, "max_dte": 6, "nearest_expiry_first": True}}


def round_trip_cost(bid: float, ask: float, *, round_trip_fees: float, slippage_per_contract: float) -> float:
    """Dollars to buy and later sell one contract, assuming the same bid/ask gap at the exit as at the entry."""
    return (ask - bid) * 100 + round_trip_fees + 2 * slippage_per_contract


def cost_in_r(cost_dollars: float, *, delta: float, stop_distance: float) -> float | None:
    r_dollars = abs(delta) * abs(stop_distance) * 100
    return cost_dollars / r_dollars if r_dollars > 0 else None


def strategy_costs(strategy) -> tuple[float, float]:
    """(round-trip fees, slippage per contract per side) for one contract, as the strategy models them."""
    execution = strategy.execution
    fees = 2 * (float(execution["commission_per_contract"]) + float(execution["exchange_fee_per_contract"]))
    return fees, float(execution["slippage_per_contract"])


def build_table(root: Path, *, round_trip_fees: float = 1.40, slippage_per_contract: float = 0.02,
                max_sessions: int = 40) -> dict[str, Any]:
    """Median round-trip cost per symbol in basis points of the stock price, for the strategy's contract (7-21 days
    out) and for the shorter-dated alternative (1-6 days out), sampled through each recorded session."""
    history = QuoteHistory(root)
    days = history.days()[-max_sessions:]
    samples: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for day in days:
        for symbol in history.symbols(day):
            for at in SAMPLE_TIMES_ET:
                when = datetime.combine(day, at, ET).astimezone(timezone.utc)
                try:
                    chain = history.chain(day, symbol, when)
                    spot = history.spot(day, symbol, when)
                except (OSError, ValueError, KeyError):
                    break
                if not spot:
                    continue
                for name, rule in CONTRACTS.items():
                    for side in ("CALL", "PUT"):
                        quote = select_contract(chain, side, day, **rule)
                        if quote is None or not quote.delta:
                            continue
                        cost = round_trip_cost(quote.bid, quote.ask, round_trip_fees=round_trip_fees,
                                               slippage_per_contract=slippage_per_contract)
                        bps = cost / (abs(quote.delta) * spot * 0.01)
                        samples.setdefault(symbol, {}).setdefault(name, []).append((bps, quote.ask * 100))
    table: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                             "source": "recorded 1-minute option quotes (data/option_history)",
                             "first_session": days[0].isoformat() if days else None,
                             "last_session": days[-1].isoformat() if days else None, "sessions": len(days),
                             "round_trip_fees": round_trip_fees, "slippage_per_contract": slippage_per_contract,
                             "unit": "cost_bps = round-trip cost / (|delta| x stock price x 0.01); "
                                     "cost in R = cost_bps / (stop distance as bps of the stock price)",
                             "symbols": {}}
    for symbol, by_contract in sorted(samples.items()):
        table["symbols"][symbol] = {name: {"cost_bps": round(statistics.median(b for b, _ in rows), 3),
                                           "median_premium": round(statistics.median(p for _, p in rows), 2),
                                           "samples": len(rows)}
                                    for name, rows in by_contract.items()}
    return table


def write_table(root: Path, table: dict[str, Any]) -> Path:
    path = root / TABLE_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def load_table(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((root / TABLE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def symbol_cost_bps(table: dict[str, Any] | None, symbol: str, contract: str = "standard_7_21_dte") -> float | None:
    entry = ((table or {}).get("symbols") or {}).get(symbol, {}).get(contract)
    return float(entry["cost_bps"]) if entry else None


def net_r(gross_r: float, *, cost_bps: float | None, stop_fraction: float | None) -> float | None:
    """A stock-move R result after the measured option cost. None when the symbol's cost or the stop is unknown."""
    if cost_bps is None or not stop_fraction or stop_fraction <= 0:
        return None
    return gross_r - (cost_bps / 1e4) / stop_fraction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.cost_model")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="rebuild data/option_cost_table.json from the recorded quotes")
    parser.parse_args(argv)
    root = Path.cwd()
    table = build_table(root)
    path = write_table(root, table)
    print(json.dumps({"written": str(path), "sessions": table["sessions"],
                      "symbols": {s: {k: v["cost_bps"] for k, v in row.items()} for s, row in table["symbols"].items()}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
