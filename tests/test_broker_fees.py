"""PAPER fills pay tastytrade's published stock/ETF option fees (schedule dated 2026-07-30)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from trader.broker_fees import TastytradeOptionFees
from trader.models import OptionQuote
from trader.paper_execution import PaperExecutor
from trader.portfolio import PaperAccount

OCT = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)   # FINRA TAF not assessed Oct 1 - Dec 31, 2026
JAN = datetime(2027, 1, 4, 15, 0, tzinfo=timezone.utc)


class TastytradeFeeTests(unittest.TestCase):
    fees = TastytradeOptionFees()

    def test_opening_pays_commission_clearing_and_orf(self):
        self.assertEqual(self.fees.fill_fees("BUY", 1, 379.02, OCT), 1.12)
        self.assertEqual(self.fees.fill_fees("BUY", 3, 100.0, OCT), 3.36)

    def test_commission_is_capped_at_ten_dollars_per_leg(self):
        self.assertEqual(self.fees.fill_fees("BUY", 20, 2000.0, OCT), round(10 + 20 * 0.12, 2))

    def test_closing_has_no_commission_but_pays_sale_fees_rounded_up_to_the_cent(self):
        self.assertEqual(self.fees.fill_fees("SELL", 1, 333.98, OCT), 0.13)   # 0.12 + SEC 0.0069 -> 0.01
        self.assertEqual(self.fees.fill_fees("SELL", 1, 333.98, JAN), 0.14)   # + FINRA TAF 0.00329 -> 0.01
        self.assertEqual(self.fees.fill_fees("SELL", 1, 0.0, OCT), 0.12)

    def test_paper_fills_book_the_broker_fees(self):
        account = PaperAccount(starting_capital=1000, cash=1000)
        executor = PaperExecutor(account, slippage_per_contract=.02, fees=self.fees)
        quote = OptionQuote("IWM   261028P00272000", "IWM", OCT, 272.0, "P", 3.34, 3.79, quote_timestamp=OCT)
        buy = executor.fill(quote, "BUY", 1, "strategy_entry", OCT)
        sell = executor.fill(quote, "SELL", 1, "stop", OCT)
        self.assertEqual((buy.fees, sell.fees), (1.12, 0.13))
        self.assertAlmostEqual(account.cash, 1000 - 379.02 - 1.12 + 333.98 - 0.13, places=6)
        self.assertAlmostEqual(account.fees_paid, 1.25)


if __name__ == "__main__":
    unittest.main()
