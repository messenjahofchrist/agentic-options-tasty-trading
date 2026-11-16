from __future__ import annotations

import asyncio
import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trader import option_recorder as rec

ET = ZoneInfo("America/New_York")


def chain(root, expirations, strikes):
    return [{"root-symbol": root, "option-chain-type": "Standard", "active": True, "expiration-date": e,
             "strike-price": str(k), "option-type": t} for e in expirations for k in strikes for t in ("C", "P")]


class ContractTests(unittest.TestCase):
    def test_symbols_match_tastytrade_formats(self):
        c = rec.Contract("SPY", date(2026, 11, 13), "C", 735.0)
        self.assertEqual(c.streamer, ".SPY261113C735")
        self.assertEqual(c.occ, "SPY   261113C00735000")
        half = rec.Contract("NFLX", date(2026, 11, 20), "P", 62.5)
        self.assertEqual((half.streamer, half.occ), (".NFLX261120P62.5", "NFLX  261120P00062500"))

    def test_expirations_add_expired_ones_for_backfills(self):
        sessions = lambda d: d.weekday() < 5
        listed = [date(2026, 11, 13), date(2026, 11, 20), date(2026, 12, 31)]
        # NFLX on Oct 27 (recorded on Nov 8): the listed ones within 35 days plus the expired Fridays Oct 30 and Nov 6
        got = rec.session_expirations(date(2026, 10, 27), listed, date(2026, 11, 8), sessions, "NFLX")
        self.assertEqual(got, [date(2026, 10, 30), date(2026, 11, 6), date(2026, 11, 13), date(2026, 11, 20)])
        # SPY expires every session, so every expired weekday is added
        spy = rec.session_expirations(date(2026, 11, 4), [date(2026, 11, 9)], date(2026, 11, 8), sessions, "SPY")
        self.assertEqual(spy, [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)])

    def test_credit_band_covers_the_credit_spread_bot_for_spy_and_qqq_only(self):
        strikes = [float(k) for k in range(600, 800)]
        exps = [date(2026, 11, 20), date(2026, 12, 4), date(2026, 12, 18), date(2027, 1, 15)]
        band = rec.credit_band("SPY", date(2026, 11, 9), 700.0, strikes, exps)
        self.assertEqual({c.expiration for c in band}, {date(2026, 12, 4), date(2026, 12, 18)})   # 25 and 39 days
        self.assertEqual(min(c.strike for c in band if c.right == "P"), 616.0)                      # 12% below
        self.assertEqual(max(c.strike for c in band if c.right == "P"), 699.0)
        self.assertEqual(max(c.strike for c in band if c.right == "C"), 784.0)
        self.assertEqual(rec.credit_band("NFLX", date(2026, 11, 9), 70.0, strikes, exps), [])

    def test_near_strikes_takes_the_listed_strikes_either_side_of_spot(self):
        strikes = [float(k) for k in range(50, 80)]
        self.assertEqual(rec.near_strikes(strikes, 63.4, each_side=2), [62.0, 63.0, 64.0, 65.0])
        self.assertEqual(rec.near_strikes(strikes, 50.0, each_side=2), [50.0, 51.0, 52.0])


class RecordSessionTests(unittest.TestCase):
    def test_records_bars_and_bid_ask_candles_inside_the_session(self):
        day = date(2026, 11, 6)
        open_et = datetime(2026, 11, 6, 9, 30, tzinfo=ET); close_et = datetime(2026, 11, 6, 16, 0, tzinfo=ET)

        def row(minute, price):
            stamp = (open_et + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat()
            return {"timestamp": stamp, "open": price, "high": price, "low": price, "close": price, "volume": 1}

        requested = []

        async def fetch(series, start):
            requested.append(dict(series))
            got = {}
            for name, candle in series.items():
                if name == "NFLX":
                    got[name] = [row(-5, 63.0), row(0, 63.5), row(389, 63.4), row(400, 63.9)]   # pre/after hours dropped
                elif name == "VIX":
                    got[name] = [row(0, 20.0)]
                elif name == ".NFLX261113C63":
                    got[name] = [row(0, 1.10 if "price=bid" in candle else 1.14), row(5, 1.2 if "price=bid" in candle else 1.24)]
            return got, {n: "no candles returned" for n in series if n not in got}

        with tempfile.TemporaryDirectory() as tmp:
            summary = asyncio.run(rec.record_session(
                day=day, symbols=["NFLX"], chains={"NFLX": chain("NFLX", ["2026-11-13", "2026-11-20"], [62, 63, 64, 65])},
                fetch=fetch, bounds=(open_et, close_et), today=date(2026, 11, 8), is_session=lambda d: d.weekday() < 5,
                out_dir=Path(tmp), snapshot=lambda cs: {c.occ: {"delta": 0.5} for c in cs}))
            data = json.loads(gzip.decompress((Path(tmp) / "2026-11-06" / "NFLX.json.gz").read_bytes()))
            self.assertTrue((Path(tmp) / "2026-11-06" / "VIX.json.gz").exists())
        self.assertEqual(data["underlying"], [[0, 63.5, 63.5, 63.5, 63.5], [389, 63.4, 63.4, 63.4, 63.4]])
        self.assertEqual(data["spot_close"], 63.4)
        self.assertEqual(data["expirations"], ["2026-11-06", "2026-11-13", "2026-11-20"])   # Nov 6 itself expires that day
        quote = data["contracts"]["NFLX  261113C00063000"]
        self.assertEqual(quote["bid"], [[0, 1.1, 1.1, 1.1, 1.1], [5, 1.2, 1.2, 1.2, 1.2]])
        self.assertEqual(quote["ask"][0], [0, 1.14, 1.14, 1.14, 1.14])
        self.assertEqual(quote["close_snapshot"], {"delta": 0.5})
        self.assertEqual(len(data["contracts"]), 1)            # contracts without candles are not stored
        self.assertEqual(summary["symbols"]["NFLX"]["contracts_with_quotes"], 1)
        self.assertEqual(summary["symbols"]["NFLX"]["fetch_failures"], 0)
        # 3 expirations x 4 strikes x 2 rights, asked once for bids and once for asks
        self.assertEqual(sum(len(s) for s in requested if all(k.startswith(".") for k in s)), 2 * 3 * 4 * 2)

    def test_latest_completed_session_waits_for_the_close(self):
        bounds = lambda d: (None if d.weekday() >= 5 else (datetime.combine(d, datetime.min.time(), ET).replace(hour=9, minute=30),
                                                           datetime.combine(d, datetime.min.time(), ET).replace(hour=16)))
        sunday = datetime(2026, 11, 8, 12, 0, tzinfo=ET)
        self.assertEqual(rec.latest_completed_session(sunday, bounds), date(2026, 11, 6))
        friday_noon = datetime(2026, 11, 6, 12, 0, tzinfo=ET)
        self.assertEqual(rec.latest_completed_session(friday_noon, bounds), date(2026, 11, 5))
        friday_evening = datetime(2026, 11, 6, 16, 30, tzinfo=ET)
        self.assertEqual(rec.latest_completed_session(friday_evening, bounds), date(2026, 11, 6))


if __name__ == "__main__":
    unittest.main()
