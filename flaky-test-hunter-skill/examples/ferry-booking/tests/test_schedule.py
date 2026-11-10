import time
from datetime import date

from ferry_booking.schedule import departure_date, minutes_until


def test_departs_today():
    now = time.time()
    assert departure_date(now) == date.today()


def test_minutes_until_departure():
    assert minutes_until(1_000_000 + 600, 1_000_000) == 10


def test_minutes_until_never_negative():
    assert minutes_until(1_000_000, 1_000_600) == 0
