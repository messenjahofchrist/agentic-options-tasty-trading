from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .watchdog import AgentLease


def propose_change(proposal: dict, directory: str | Path = "strategy_change_requests") -> Path:
    required = {"proposal_id", "timestamp", "author", "current_strategy_version", "proposed_strategy_version", "exact_change", "rationale",
                "evidence", "sample_size", "backtest_results", "out_of_sample_results", "walk_forward_results", "fee_spread_slippage_impact",
                "risk_impact", "expected_benefit_downside", "confidence", "rollback_plan"}
    missing = required - set(proposal)
    if missing: raise ValueError("Proposal missing fields: " + ", ".join(sorted(missing)))
    if proposal.get("active_strategy_modified"):
        raise ValueError("AI proposals may not modify the active strategy")
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    path = root / f"{proposal['proposal_id']}.json"
    if path.exists(): raise FileExistsError(path)
    path.write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
    return path


def record_review(review: dict, path: str | Path = "reviews/latest_review.json") -> None:
    required = {"review_id", "timestamp", "agent", "strategy_version", "strategy_hash", "mode", "decision", "findings", "proposals"}
    missing = required - set(review)
    if missing: raise ValueError("Review missing fields: " + ", ".join(sorted(missing)))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
