"""What the LIVE broker path can and cannot execute for a PROFESSIONAL_STRATEGY_V2 version.

Owner rule 2026-11-15: every new V2 version must also run LIVE. LIVE always runs the ACTIVE V2 configuration
(strategies/professional_v2.json), through the same loop as PAPER, so a new version reaches LIVE by being activated.
This module is the one place that names a V2 feature the broker path cannot execute yet. A version that turns such a
feature on is refused at LIVE startup and fails tests/test_live_v2.py, so the gap is closed in the same piece of work.
"""
from __future__ import annotations

from typing import Any


def unsupported_for_live(config: Any) -> list[str]:
    """Features of this V2 configuration that LIVE cannot execute. Empty means LIVE can run it."""
    data = config.data
    problems: list[str] = []
    if (data.get("management") or {}).get("partial_profit", {}).get("enabled"):
        problems.append("management.partial_profit (LIVE sells whole positions only)")
    if (data.get("in_play") or {}).get("enabled"):
        problems.append("in_play (the morning scan runs only in the PAPER side-session)")
    return problems
