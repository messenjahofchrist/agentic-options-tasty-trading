"""tastytrade's published fees for stock and ETF options, charged on PAPER fills (owner request 2026-10-20).

Source: tastytrade "Commissions & Fees", last updated 2026-07-30. Opening: $1.00/contract commission, capped at
$10 per leg. Closing: no commission. Every fill: $0.10/contract clearing + $0.02/contract Options Regulatory
Fee. Sales also pay the FINRA TAF ($0.00329/contract; not assessed 2026-10-01 through 2026-12-31) and the SEC
fee ($20.60 per $1,000,000 of proceeds), each rounded up to the cent. This bot only buys to open and sells to
close, so BUY is the opening side.

Only booked fills use this. The strategy's own commission/exchange values (strategy.txt) still drive its sizing
and economic gate, which are unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime


def _cents_up(value: float) -> float:
    return math.ceil(round(value * 100, 6)) / 100 if value > 0 else 0.0


@dataclass(frozen=True)
class TastytradeOptionFees:
    open_commission_per_contract: float = 1.00
    commission_cap_per_leg: float = 10.00
    clearing_per_contract: float = 0.10
    orf_per_contract: float = 0.02
    finra_taf_per_contract_sold: float = 0.00329
    finra_taf_waived: tuple[date, date] = (date(2026, 10, 1), date(2026, 12, 31))
    sec_fee_per_dollar_sold: float = 20.60 / 1_000_000

    def fill_fees(self, side: str, contracts: int, gross: float, when: datetime) -> float:
        if contracts < 0 or side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY/SELL and contracts nonnegative")
        fees = contracts * (self.clearing_per_contract + self.orf_per_contract)
        if side == "BUY":
            fees += min(contracts * self.open_commission_per_contract, self.commission_cap_per_leg)
        else:
            start, end = self.finra_taf_waived
            if not start <= when.date() <= end:
                fees += _cents_up(contracts * self.finra_taf_per_contract_sold)
            fees += _cents_up(max(0.0, gross) * self.sec_fee_per_dollar_sold)
        return round(fees, 2)

    def estimate(self, contracts: int, legs: int = 1) -> float:
        """Closing-side estimate used to value an open position's exit (the sale-based cents are left out)."""
        return contracts * (self.clearing_per_contract + self.orf_per_contract)
