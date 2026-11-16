"""AI vetoes (owner request 2026-11-11): the advisor may block entries in a symbol, never add or enlarge one.

The advisor (any agent, see agents.json) writes data/advisor/vetoes.json after each review:

    {"generated_at_utc": "...", "agent": "claude", "note_id": "...",
     "vetoes": [{"symbol": "NFLX", "side": "CALL" | "PUT" | null, "until_utc": "...", "reason": "...", "sources": [...]}]}

A veto applies to one symbol (side null = both directions), must carry a reason, and ends at until_utc, which the
advisor caps at the session close. The whole file is ignored once it is older than max_age_minutes, so a stopped or
failing advisor can never leave a block in place. The engines only read this file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VETO_FILE = "data/advisor/vetoes.json"


def load_vetoes(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / VETO_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("vetoes"), list) else None


def active_veto(document: dict[str, Any] | None, symbol: str, side: str | None, now: datetime,
                max_age_minutes: int) -> dict[str, Any] | None:
    """The veto that blocks this symbol and side right now, or None. Malformed entries are ignored (fail open: a
    veto is advice layered on the rules, so bad advice must not stop the bot's own decisions)."""
    if not document:
        return None
    try:
        generated = datetime.fromisoformat(str(document["generated_at_utc"]).replace("Z", "+00:00"))
        if generated.tzinfo is None or not generated <= now + timedelta(minutes=2):
            return None
        if now - generated > timedelta(minutes=max_age_minutes):
            return None
    except (KeyError, ValueError):
        return None
    for veto in document["vetoes"]:
        try:
            if veto.get("symbol") != symbol or veto.get("side") not in (None, side):
                continue
            until = datetime.fromisoformat(str(veto["until_utc"]).replace("Z", "+00:00"))
            if until.tzinfo is None or now >= until or not str(veto.get("reason") or "").strip():
                continue
        except (AttributeError, KeyError, ValueError):
            continue
        return {"symbol": symbol, "side": veto.get("side"), "until_utc": until.isoformat(), "reason": str(veto["reason"])[:400],
                "agent": document.get("agent"), "note_id": document.get("note_id"),
                "generated_at_utc": generated.isoformat()}
    return None
