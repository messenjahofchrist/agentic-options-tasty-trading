from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


class KillSwitch:
    def __init__(self, path: str | Path = "KILL_SWITCH"):
        self.path = Path(path)
    def block_entries(self, reason: str, source: str = "manual") -> None:
        self.path.write_text(json.dumps({"blocked": True, "reason": reason, "source": source,
                                         "timestamp": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    def entries_blocked(self) -> bool:
        return self.path.exists()
    def source(self) -> str | None:
        """Who wrote the switch. A hand-made or unreadable file reports None and is never auto-cleared."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("source") if isinstance(data, dict) else None
    def reset(self) -> None:
        self.path.unlink(missing_ok=True)


class AgentLease:
    """Exclusive lock for AI review/edit workflows; trading engine does not need AI lease."""
    def __init__(self, lock_path: str | Path = "agent_lock.json", status_path: str | Path = "agent_status.json"):
        self.lock_path, self.status_path = Path(lock_path), Path(status_path)

    def acquire(self, owner: str, purpose: str, duration_minutes: int = 30) -> None:
        now = datetime.now(timezone.utc)
        if self.lock_path.exists():
            current = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if current.get("status") == "ACTIVE":
                try: expires = datetime.fromisoformat(current["expected_end_at"])
                except (KeyError, ValueError): raise RuntimeError("Agent lock is active with no valid expected_end_at")
                if expires > now: raise RuntimeError(f"Agent lease held by {current.get('owner')}")
        lock = {"owner": owner, "started_at": now.isoformat(), "expected_end_at": (now + timedelta(minutes=duration_minutes)).isoformat(),
                "heartbeat_at": now.isoformat(), "purpose": purpose, "status": "ACTIVE", "host": socket.gethostname(), "pid": os.getpid()}
        self._atomic(self.lock_path, lock)
        statuses = json.loads(self.status_path.read_text(encoding="utf-8")) if self.status_path.exists() else {}
        statuses[owner] = {"last_access_at": now.isoformat(), "started_at": now.isoformat(), "next_planned_access_at": lock["expected_end_at"],
                           "expected_end_at": lock["expected_end_at"], "heartbeat_at": now.isoformat(), "status": "ACTIVE", "purpose": purpose, "last_review_id": None}
        self._atomic(self.status_path, statuses)

    def release(self, owner: str) -> None:
        if not self.lock_path.exists(): return
        lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        if lock.get("owner") != owner: raise RuntimeError("Cannot release another agent's lease")
        lock["status"] = "RELEASED"; lock["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic(self.lock_path, lock)
        statuses = json.loads(self.status_path.read_text(encoding="utf-8")) if self.status_path.exists() else {}
        if owner in statuses:
            statuses[owner]["status"] = "IDLE"; statuses[owner]["heartbeat_at"] = lock["heartbeat_at"]
            self._atomic(self.status_path, statuses)

    @staticmethod
    def _atomic(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)


def lease_is_stale(lock: dict, now: datetime | None = None) -> bool:
    if lock.get("status") != "ACTIVE" or not lock.get("expected_end_at"):
        return False
    return datetime.fromisoformat(lock["expected_end_at"]) < (now or datetime.now(timezone.utc))
