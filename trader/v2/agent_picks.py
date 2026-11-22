"""Agent picks (owner, 2026-11-20, V2 2.8.5): an advisor agent's "almost_sure" trade idea is bought.

Each hour every agent in agents.json roles.advisor_panel reviews the news on its own (trader/advisor.py). An idea one
agent marks confidence "almost_sure" is written to data/advisor/agent_picks.json; the agents do not need to agree.

    {"generated_at_utc": "...", "agents": "claude+codex", "note_id": "...",
     "picks": [{"id": "...", "agent": "codex", "also_picked_by": [], "symbol": "NVDA", "side": "PUT",
                "stop_pct": 0.8, "target_pct": 1.6, "thesis": "...", "sources": [...], "created_utc": "..."}]}

The engines (PAPER and LIVE) read the file. A pick is valid for valid_minutes after the agent's reply. The first time an
engine sees it, the stock's price is the pick's anchor: the stop and target are stop_pct / target_pct from that price,
and a later bar may still buy it only while the stock is not past the stop and has covered less than
max_move_toward_target of the way to the target (no chasing). The candidate then skips the breakout, regime, sector and
volume stages but keeps every safety limit (trader/v2/pipeline.py, agent_pick). At most max_per_session picks are
bought per engine per day; each pick is bought once (state in <data_dir>/v2_agent_picks.json).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PICKS_FILE = "data/advisor/agent_picks.json"


def load_picks(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / PICKS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("picks"), list) else None


def _when(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def active_picks(document: dict[str, Any] | None, now: datetime, valid_minutes: int) -> list[dict[str, Any]]:
    """Well-formed picks that are still valid at `now`. Malformed entries are ignored."""
    out = []
    for pick in (document or {}).get("picks") or []:
        try:
            created = _when(pick.get("created_utc"))
            if created is None or not created <= now + timedelta(minutes=2) or now - created > timedelta(minutes=valid_minutes):
                continue
            if pick.get("side") not in ("CALL", "PUT") or not pick.get("id") or not pick.get("symbol"):
                continue
            stop, target = float(pick["stop_pct"]), float(pick["target_pct"])
            if not (0 < stop <= 5 and 0 < target <= 10):
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        out.append(pick)
    return out


def levels(pick: dict[str, Any], *, anchor: float, price: float, max_move_toward_target: float) -> dict[str, Any]:
    """Stop and target from the anchor price, and whether `price` may still be bought."""
    direction = 1 if pick["side"] == "CALL" else -1
    stop = anchor * (1 - direction * float(pick["stop_pct"]) / 100)
    target = anchor * (1 + direction * float(pick["target_pct"]) / 100)
    out: dict[str, Any] = {"anchor_price": round(anchor, 4), "stop_underlying": round(stop, 4),
                           "target_underlying": round(target, 4), "price": price}
    if (price - stop) * direction <= 0:
        return {**out, "ok": False, "expired": True,
                "reason": f"the stock {price:.2f} is already past the pick's stop {stop:.2f}"}
    covered = (price - anchor) * direction / abs(target - anchor) if target != anchor else 0.0
    out["covered_toward_target"] = round(covered, 3)
    if covered >= max_move_toward_target:
        return {**out, "ok": False, "expired": False,
                "reason": f"the stock already covered {covered:.0%} of the way from {anchor:.2f} to the target "
                          f"{target:.2f} (limit {max_move_toward_target:.0%}; no chasing)"}
    return {**out, "ok": True, "stop_distance": (price - stop) * direction, "target_distance": (target - price) * direction}


class PickBook:
    """One engine's record of today's picks: the anchor price of each pick it has seen and the ones it bought."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def _today(self, session_date: str) -> dict[str, Any]:
        if self.state.get("session_date") != session_date:
            self.state = {"session_date": session_date, "anchors": {}, "taken": {}, "expired": {}}
        return self.state

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, default=str) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def taken_count(self, session_date: str) -> int:
        return len(self._today(session_date)["taken"])

    def is_done(self, session_date: str, pick_id: str) -> bool:
        state = self._today(session_date)
        return pick_id in state["taken"] or pick_id in state["expired"]

    def anchor(self, session_date: str, pick_id: str, price: float, now: datetime) -> float:
        state = self._today(session_date)
        if pick_id not in state["anchors"]:
            state["anchors"][pick_id] = {"price": price, "at": now.astimezone(timezone.utc).isoformat()}
            self._save()
        return float(state["anchors"][pick_id]["price"])

    def expire(self, session_date: str, pick_id: str, reason: str) -> None:
        self._today(session_date)["expired"][pick_id] = reason
        self._save()

    def mark_taken(self, session_date: str, pick_id: str, trade_id: str, at: str) -> None:
        self._today(session_date)["taken"][pick_id] = {"trade_id": trade_id, "at": at}
        self._save()
