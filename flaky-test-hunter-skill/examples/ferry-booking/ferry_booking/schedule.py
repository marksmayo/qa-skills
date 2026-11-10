"""Departure times are stored as UTC timestamps."""
from datetime import date, datetime, timezone


def departure_date(ts: float) -> date:
    """The calendar date of a departure, in UTC."""
    return datetime.fromtimestamp(ts, timezone.utc).date()


def minutes_until(ts: float, now: float) -> int:
    return max(0, int((ts - now) // 60))
