"""Earnings and economic-event awareness.

Economic events come from strategies/v2_economic_calendar.json (each entry copied from the agency's own schedule,
with its source and whether the time is verified). Earnings come from tastytrade market-metrics
('earnings': expected-report-date, time-of-day BTO/AMC, estimated flag). Every check returns what it found, how
reliable it is, and whether it rejected, delayed or flagged the trade for review.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class EventEngine:
    def __init__(self, calendar: dict[str, Any], cfg: dict[str, Any], *, next_session: Callable[[date], date | None]):
        self.calendar = calendar
        self.cfg = cfg
        self.next_session = next_session
        self.valid_through = date.fromisoformat(calendar["calendar_valid_through"])

    def economic_events_on(self, day: date) -> list[dict[str, Any]]:
        return [e for e in self.calendar["events"] if e["date"] == day.isoformat()]

    def upcoming(self, now: datetime, days: int = 7) -> list[dict[str, Any]]:
        today = now.astimezone(ET).date()
        return [e for e in self.calendar["events"] if today <= date.fromisoformat(e["date"]) <= today + timedelta(days=days)]

    def economic_check(self, now: datetime) -> dict[str, Any]:
        local = now.astimezone(ET)
        out: dict[str, Any] = {"events_today": self.economic_events_on(local.date()), "outcome": "PASS", "reasons": [],
                               "review": []}
        if local.date() > self.valid_through:
            out.update(outcome="REJECT", reasons=[f"economic_calendar_expired_after_{self.valid_through.isoformat()}"])
            return out
        econ = self.cfg["economic"]
        for event in out["events_today"]:
            if event["type"] == "FOMC_RATE_DECISION":
                rule = econ["FOMC_RATE_DECISION"]
                start, end = time.fromisoformat(rule["block_entries_from_et"]), time.fromisoformat(rule["block_entries_until_et"])
                if start <= local.time() < end:
                    out["outcome"] = "DELAY"
                    out["reasons"].append(f"FOMC window {rule['block_entries_from_et']}-{rule['block_entries_until_et']} ET"
                                          + ("" if event["time_verified"] else " (statement time customary, unverified)"))
                    out["delay_until_et"] = rule["block_entries_until_et"]
                out["review"].append("FOMC_DECISION_DAY")
            elif event["type"] in econ["pre_open_release_types"]:
                block = int(econ["pre_open_release_block_minutes_after_open"])
                opened = local.replace(hour=9, minute=30, second=0, microsecond=0)
                if block and opened <= local < opened + timedelta(minutes=block):
                    out["outcome"] = "DELAY"
                    out["reasons"].append(f"{event['type']} release; entries resume {block} min after the open")
                out["review"].append(f"{event['type']}_DAY")
        return out

    def earnings_check(self, symbol: str, metrics: dict[str, Any] | None, now: datetime, *, single_stock: bool) -> dict[str, Any]:
        rules = self.cfg["earnings"]
        if not single_stock:
            return {"outcome": "PASS", "status": "not_applicable_non_equity"}
        earnings = (metrics or {}).get("earnings") if metrics else None
        if metrics is None:
            outcome = "REJECT" if rules["metrics_unavailable"] == "reject" else "PASS"
            return {"outcome": outcome, "status": "unverified_metrics_unavailable",
                    "reasons": ["earnings_data_unavailable"] if outcome == "REJECT" else [], "review": ["earnings_unverified"]}
        if not earnings or not earnings.get("expected-report-date"):
            return {"outcome": "PASS", "status": "no_scheduled_report_listed", "source": "tastytrade market-metrics"}
        report = date.fromisoformat(earnings["expected-report-date"])
        timing = earnings.get("time-of-day")
        estimated = bool(earnings.get("estimated"))
        today = now.astimezone(ET).date()
        next_day = self.next_session(today)
        out: dict[str, Any] = {"report_date": report.isoformat(), "time_of_day": timing, "estimated": estimated,
                               "verification": "estimated_unverified" if estimated else "confirmed_by_provider",
                               "source": "tastytrade market-metrics earnings", "outcome": "PASS", "reasons": [], "review": []}
        unknown = timing not in ("BTO", "AMC")
        after_today = report == today and (timing == "AMC" or unknown)
        before_next = next_day is not None and report == next_day and (timing == "BTO" or unknown)
        before_today = report == today and (timing == "BTO" or unknown)
        if after_today and rules["reject_if_reports_today_after_close"]:
            out["reasons"].append("earnings_after_today_close" + ("_time_unknown" if unknown else ""))
        if before_next and rules["reject_if_reports_next_session_before_open"]:
            out["reasons"].append("earnings_before_next_open" + ("_time_unknown" if unknown else ""))
        if before_today and not after_today:
            policy = rules["reported_today_before_open"]
            if policy == "reject":
                out["reasons"].append("earnings_reported_this_morning")
            elif policy == "review":
                out["review"].append("earnings_reported_this_morning")
        if out["reasons"]:
            out["outcome"] = "REJECT"
        if estimated:
            out["review"].append("earnings_date_estimated")
        return out

    def check(self, symbol: str, metrics: dict[str, Any] | None, now: datetime, *, single_stock: bool) -> dict[str, Any]:
        if not self.cfg["enabled"]:
            return {"outcome": "PASS", "status": "disabled"}
        econ = self.economic_check(now)
        earn = self.earnings_check(symbol, metrics, now, single_stock=single_stock)
        outcome = ("REJECT" if "REJECT" in (econ["outcome"], earn["outcome"]) else
                   "DELAY" if econ["outcome"] == "DELAY" else "PASS")
        return {"outcome": outcome, "economic": econ, "earnings": earn,
                "reasons": list(econ.get("reasons", [])) + list(earn.get("reasons", [])),
                "review": list(econ.get("review", [])) + list(earn.get("review", []))}

    def open_position_review(self, now: datetime) -> list[str]:
        """Flags for open positions (the strategy's own exits still decide; nothing is closed here)."""
        econ = self.economic_check(now)
        return [f"open_position_review:{flag}" for flag in econ["review"] if flag == "FOMC_DECISION_DAY"
                and self.cfg["economic"]["FOMC_RATE_DECISION"]["open_positions"] == "review"]
