from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    discrepancies: tuple[str, ...]


def reconcile_positions(local: list[dict], broker: list[dict]) -> ReconciliationResult:
    def aggregate(items: list[dict]) -> dict[tuple[str, str], int]:
        totals: dict[tuple[str, str], int] = {}
        for item in items:
            key = (str(item["symbol"]), str(item.get("instrument_type", "Equity Option")))
            totals[key] = totals.get(key, 0) + int(item["quantity"])
        return totals
    left, right = aggregate(local), aggregate(broker)
    discrepancies = tuple(f"{key}: local={left.get(key, 0)} broker={right.get(key, 0)}"
                           for key in sorted(set(left) | set(right)) if left.get(key, 0) != right.get(key, 0))
    return ReconciliationResult(not discrepancies, discrepancies)
