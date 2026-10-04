from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class PaperAccount:
    starting_capital: float
    cash: float
    deposits: float = 0.0
    withdrawals: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)

    @property
    def equity(self) -> float:
        return self.cash + self.unrealized_pnl

    @property
    def buying_power(self) -> float:
        return max(0.0, self.cash)

    def capital_movement(self, amount: float, kind: str, at: datetime | None = None) -> None:
        if amount <= 0 or kind not in ("deposit", "withdrawal"):
            raise ValueError("Use a positive amount and deposit/withdrawal kind")
        if kind == "deposit":
            self.deposits += amount
            self.cash += amount
        else:
            if amount > self.cash:
                raise ValueError("Withdrawal exceeds available cash")
            self.withdrawals += amount
            self.cash -= amount
        self.ledger.append({"type": kind, "amount": amount, "timestamp": (at or datetime.now(timezone.utc)).isoformat()})


def load_or_initialize(path: str | Path, starting_cash: float = 10_000) -> PaperAccount:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.exists():
        data = json.loads(file.read_text(encoding="utf-8"))
        return PaperAccount(**data)
    account = PaperAccount(starting_cash, starting_cash)
    save_account(file, account)
    return account


def save_account(path: str | Path, account: PaperAccount) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix(file.suffix + ".tmp")
    temp.write_text(json.dumps(asdict(account), indent=2) + "\n", encoding="utf-8")
    os.replace(temp, file)
