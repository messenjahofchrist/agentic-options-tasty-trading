from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .fees import FeeSchedule
from .models import OptionQuote
from .portfolio import PaperAccount


@dataclass(frozen=True)
class PaperFill:
    trade_id: str
    side: str
    quantity: int
    fill_price: float
    fees: float
    slippage: float
    timestamp: datetime
    quote: dict
    fill_model: str
    reason: str


class PaperExecutor:
    def __init__(self, account: PaperAccount, *, buy_price: str = "ask", sell_price: str = "bid",
                 slippage_per_contract: float = .02, fees: FeeSchedule | None = None):
        self.account, self.buy_price, self.sell_price = account, buy_price, sell_price
        self.slippage_per_contract = slippage_per_contract
        self.fees = fees or FeeSchedule()

    def fill(self, quote: OptionQuote, side: str, quantity: int, reason: str,
             now: datetime | None = None) -> PaperFill:
        if side not in ("BUY", "SELL") or quantity <= 0:
            raise ValueError("side must be BUY/SELL and quantity positive")
        when = now or datetime.now(timezone.utc)
        base = (quote.ask if self.buy_price == "ask" else quote.mid) if side == "BUY" else (quote.bid if self.sell_price == "bid" else quote.mid)
        adverse = self.slippage_per_contract / 100
        fill_price = base + adverse if side == "BUY" else max(0.0, base - adverse)
        gross_cash = fill_price * 100 * quantity
        # A side-aware broker schedule (trader/broker_fees.py) prices each fill; FeeSchedule charges a flat rate.
        fees = (self.fees.fill_fees(side, quantity, gross_cash, when) if hasattr(self.fees, "fill_fees")
                else self.fees.estimate(quantity))
        slippage = self.slippage_per_contract * quantity
        if side == "BUY":
            if gross_cash + fees > self.account.cash:
                raise ValueError("Insufficient paper cash")
            self.account.cash -= gross_cash + fees
        else:
            self.account.cash += gross_cash - fees
        self.account.fees_paid += fees
        self.account.slippage_paid += slippage
        fill = PaperFill(str(uuid.uuid4()), side, quantity, fill_price, fees, slippage, when,
                         {"timestamp": quote.quote_timestamp.isoformat() if quote.quote_timestamp else None,
                          "bid": quote.bid, "ask": quote.ask, "mid": quote.mid},
                         f"{self.buy_price if side == 'BUY' else self.sell_price}_plus_conservative_slippage", reason)
        self.account.ledger.append({"type": "fill", "trade_id": fill.trade_id, "side": side, "symbol": quote.symbol,
                                    "quantity": quantity, "fill_price": fill_price, "fees": fees,
                                    "slippage": slippage, "reason": reason, "quote": fill.quote,
                                    "timestamp": when.isoformat(), "fill_model": fill.fill_model})
        return fill


def realized_option_pnl(*, entry_fill_price: float, exit_fill_price: float, quantity: int,
                        entry_fees: float, exit_fees: float) -> float:
    """Net realized P/L from the actual simulated option fills; underlying ATR is not used."""
    if quantity <= 0 or entry_fill_price < 0 or exit_fill_price < 0 or entry_fees < 0 or exit_fees < 0:
        raise ValueError("Invalid option fill or fee values")
    return ((exit_fill_price - entry_fill_price) * 100 * quantity) - entry_fees - exit_fees
