from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


class MarketCalendar:
    """NYSE regular/early close sessions via exchange_calendars; errors fail closed."""
    def __init__(self):
        try:
            import exchange_calendars as xcals
            self._calendar = xcals.get_calendar("XNYS")
        except Exception as exc:
            raise RuntimeError("Reliable XNYS calendar unavailable; entries must remain blocked") from exc

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        import pandas as pd
        label = pd.Timestamp(day)
        if not self._calendar.is_session(label):
            return None
        return self._calendar.session_open(label).to_pydatetime().astimezone(ZoneInfo("America/New_York")), self._calendar.session_close(label).to_pydatetime().astimezone(ZoneInfo("America/New_York"))

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo("America/New_York"))
        bounds = self.session_bounds(local.date())
        return bool(bounds and bounds[0] <= local < bounds[1])

    def entry_window_ok(self, now: datetime, start: str, end: str) -> bool:
        local = now.astimezone(ZoneInfo("America/New_York"))
        if not self.is_open(local): return False
        hm = local.time().replace(tzinfo=None)
        start_t = time.fromisoformat(start); end_t = time.fromisoformat(end)
        bounds = self.session_bounds(local.date())
        return bool(bounds and start_t <= hm <= end_t and local < bounds[1])
