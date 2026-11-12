"""A pytest plugin that moves the clock, for confirming time bombs without libfaketime (stdlib only).

    TDA_CLOCK_SHIFT=+400d python -m pytest -p clock_shift ...      # shift by days (or +6h, -30d)
    TDA_CLOCK_DATE=2027-01-31 python -m pytest -p clock_shift ...  # jump to a date, keeping the time of day

Put this file on the test run's import path (the directory pytest is started from, or
PYTHONPATH). It replaces `datetime.date`, `datetime.datetime`, `time.time` and `time.time_ns`
before the tests import anything, so `date.today()`, `datetime.now()`, `datetime.utcnow()` and
`time.time()` return the moved time. Code that imported those names before the plugin loaded,
C extensions, other processes and databases keep the real clock: prefer the repo's own
freezegun, time-machine, Jest fake timers or injected clock when it has one, and libfaketime for
whole processes.
"""
from __future__ import annotations
import datetime as _dt
import os
import re
import time as _time

_real_date, _real_datetime, _real_time, _real_time_ns = _dt.date, _dt.datetime, _time.time, _time.time_ns


def _offset() -> float:
    shift = os.environ.get('TDA_CLOCK_SHIFT')
    target = os.environ.get('TDA_CLOCK_DATE')
    if target:
        now = _real_datetime.now()
        want = _real_datetime.combine(_real_date.fromisoformat(target), now.time())
        return (want - now).total_seconds()
    if shift:
        m = re.fullmatch(r'([+-]?\d+)([dhm])', shift.strip())
        if not m:
            raise ValueError(f'TDA_CLOCK_SHIFT must look like +400d, -6h or +30m, not {shift!r}')
        n, unit = int(m.group(1)), m.group(2)
        return n * {'d': 86400, 'h': 3600, 'm': 60}[unit]
    return 0.0


OFFSET = _offset()


class FakeDate(_real_date):
    @classmethod
    def today(cls):
        t = _real_datetime.fromtimestamp(_real_time() + OFFSET)
        return cls(t.year, t.month, t.day)


class FakeDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):
        t = _real_datetime.now(tz) + _dt.timedelta(seconds=OFFSET)
        return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, t.tzinfo)

    @classmethod
    def utcnow(cls):
        t = _real_datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=OFFSET)
        return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond)

    @classmethod
    def today(cls):
        return cls.now()

    def date(self):
        return FakeDate(self.year, self.month, self.day)


if OFFSET:
    _dt.date = FakeDate
    _dt.datetime = FakeDatetime
    _time.time = lambda: _real_time() + OFFSET
    _time.time_ns = lambda: _real_time_ns() + int(OFFSET * 1e9)


def pytest_report_header(config):
    if OFFSET:
        return f'clock_shift: clock moved by {OFFSET / 86400:+.2f} days (today is {FakeDate.today().isoformat()})'
    return 'clock_shift: no shift set'
