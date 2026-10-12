from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    underlying: str
    expiration: datetime
    strike: float
    right: str
    bid: float
    ask: float
    bid_size: int = 0
    ask_size: int = 0
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None
    iv_rank: float | None = None
    volume: int = 0
    open_interest: int = 0
    quote_timestamp: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_fraction(self) -> float:
        return self.spread / self.mid if self.mid > 0 else float("inf")


@dataclass
class Position:
    trade_id: str
    symbol: str
    underlying: str
    right: str
    quantity: int
    entry_price: float
    entry_underlying: float
    stop_underlying: float
    target_underlying: float
    entry_time: datetime
    max_holding_minutes: int
    initial_risk_dollars: float
    strategy_version: str
    strategy_hash: str
    fees: float = 0.0
    slippage: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    timestamp: datetime
    underlying: str
    underlying_price: float | None
    strategy_version: str
    strategy_hash: str
    strategy_id: str
    decision: str
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
