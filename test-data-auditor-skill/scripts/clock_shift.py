"""A pytest plugin that moves the clock, for confirming time bombs without libfaketime (stdlib only).

    TDA_CLOCK_SHIFT=+400d python -m pytest -p clock_shift ...      # shift by days (or +6h, -30d)
    TDA_CLOCK_DATE=2027-01-31 python -m pytest -p clock_shift ...  # jump to a date, keeping the time of day
    python clock_shift.py --self-test                              # check it in this interpreter

Put this file on the test run's import path (the directory pytest is started from, or
PYTHONPATH). It replaces `datetime.date`, `datetime.datetime`, `time.time` and `time.time_ns`
before the tests import anything, so `date.today()`, `datetime.now()`, `datetime.utcnow()` and
`time.time()` return the moved time, and `isinstance(x, datetime)` still holds for real
datetimes.

C extensions are protected from the replacement: while a compiled module (pandas, numpy,
pyarrow, any Cython module) is being imported, the real classes are put back, because such a
module binds `datetime.datetime` as a C type at import and crashes the interpreter (access
violation or stack overflow, no traceback) when it finds a Python subclass there. The cost is
that their own clocks are not moved: `pandas.Timestamp.now()`, `numpy.datetime64('now')` and
other C code keep the real time. Code that imported the names before the plugin loaded, other
processes and databases keep the real clock too. File times are not moved either (unlike
clock_shift.mjs, which moves what fs.stat reports): `os.stat(f).st_mtime` stays real, so code that
ages a file against `time.time()` (lock expiry, cache freshness) sees a file written a moment ago
as hundreds of days old under +400d; read such a failure as skew between the two clocks, not as a
time bomb, unless the code under test really compares the two. Prefer the repo's own freezegun, time-machine,
Jest fake timers or injected clock when it has one, and libfaketime for whole processes.
"""
from __future__ import annotations
import datetime as _dt
import importlib.machinery as _machinery
import os
import re
import sys
import time as _time

_real_date, _real_datetime, _real_time, _real_time_ns = _dt.date, _dt.datetime, _time.time, _time.time_ns


def _offset() -> float:
    shift = os.environ.get('TDA_CLOCK_SHIFT')
    target = os.environ.get('TDA_CLOCK_DATE')
    if target:
        now = _real_datetime.now()
        want = _real_datetime.combine(_real_date.fromisoformat(target.strip()), now.time())
        return (want - now).total_seconds()
    if shift:
        m = re.fullmatch(r'([+-]?\d+)([dhm])', shift.strip())
        if not m:
            raise ValueError(f'TDA_CLOCK_SHIFT must look like +400d, -6h or +30m, not {shift!r}')
        n, unit = int(m.group(1)), m.group(2)
        return n * {'d': 86400, 'h': 3600, 'm': 60}[unit]
    return 0.0


OFFSET = _offset()


class _RealInstances(type):
    """Lets `isinstance(real_datetime, FakeDatetime)` hold, as it does for the real class."""
    def __instancecheck__(cls, obj):
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls._real)


class FakeDate(_real_date, metaclass=_RealInstances):
    __slots__ = ()          # same instance layout as the real class
    _real = _real_date

    @classmethod
    def today(cls):
        t = _real_datetime.fromtimestamp(_real_time() + OFFSET)
        return cls(t.year, t.month, t.day)


class FakeDatetime(_real_datetime, metaclass=_RealInstances):
    __slots__ = ()
    _real = _real_datetime

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


class _RealClassesWhileLoading:
    """Puts the real date and datetime back in the datetime module while a block runs."""
    depth = 0

    def __enter__(self):
        if _RealClassesWhileLoading.depth == 0:
            _dt.date, _dt.datetime = _real_date, _real_datetime
        _RealClassesWhileLoading.depth += 1

    def __exit__(self, *exc):
        _RealClassesWhileLoading.depth -= 1
        if _RealClassesWhileLoading.depth == 0:
            _dt.date, _dt.datetime = FakeDate, FakeDatetime
        return False


class _GuardedExtensionLoader(_machinery.ExtensionFileLoader):
    """An ExtensionFileLoader whose module initialisation sees the real datetime classes."""
    def create_module(self, spec):
        with _RealClassesWhileLoading():
            return super().create_module(spec)

    def exec_module(self, module):
        with _RealClassesWhileLoading():
            return super().exec_module(module)


class _ExtensionGuard:
    """A meta path finder that wraps the loader of every compiled extension module."""
    def find_spec(self, fullname, path=None, target=None):
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, 'find_spec'):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            loader = spec.loader
            if isinstance(loader, _machinery.ExtensionFileLoader) and not isinstance(loader, _GuardedExtensionLoader):
                spec.loader = _GuardedExtensionLoader(loader.name, loader.path)
            return spec
        return None


if OFFSET:
    sys.meta_path.insert(0, _ExtensionGuard())
    _dt.date = FakeDate
    _dt.datetime = FakeDatetime
    _time.time = lambda: _real_time() + OFFSET
    _time.time_ns = lambda: _real_time_ns() + int(OFFSET * 1e9)


def pytest_report_header(config):
    if OFFSET:
        return f'clock_shift: clock moved by {OFFSET / 86400:+.2f} days (today is {FakeDate.today().isoformat()})'
    return 'clock_shift: no shift set'


_SELF_TEST_CODE = r'''
import json, sys
import clock_shift
import datetime, time
from datetime import date, datetime as DT
out = {"today": date.today().isoformat(), "now_year": DT.now().year, "time": time.time(),
       "utc_year": DT.utcnow().year, "isinstance_real": isinstance(clock_shift._real_datetime(2020, 1, 1), DT),
       "isinstance_date": isinstance(clock_shift._real_date(2020, 1, 1), date)}
try:
    import importlib.util
    if importlib.util.find_spec("pandas") is not None:
        import pandas as pd
        ts = pd.Timestamp(DT.now())
        out["pandas"] = {"ok": True, "ts_year": ts.year, "to_datetime": str(pd.to_datetime(["2030-01-02"])[0].date()),
                         "isinstance": isinstance(pd.Timestamp("2020-01-01"), DT)}
    if importlib.util.find_spec("numpy") is not None:
        import numpy as np
        out["numpy"] = {"ok": True, "dt64": str(np.datetime64(DT(2030, 1, 2, 3, 4, 5)))}
except Exception as e:  # an exception is a failure, but not a crash
    out["extension_error"] = repr(e)
print(json.dumps(out))
'''


def _self_test() -> int:
    import json
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    checks = []

    def run(env):
        e = {k: v for k, v in os.environ.items() if k not in ('TDA_CLOCK_SHIFT', 'TDA_CLOCK_DATE')}
        e.update(env)
        e['PYTHONPATH'] = here + (os.pathsep + e['PYTHONPATH'] if e.get('PYTHONPATH') else '')
        return subprocess.run([sys.executable, '-c', _SELF_TEST_CODE], env=e, capture_output=True, text=True, timeout=300)

    real_now = _real_datetime.now()
    r = run({'TDA_CLOCK_SHIFT': '+400d'})
    crashed = r.returncode != 0
    checks.append(('+400d run exits cleanly (no interpreter crash)', not crashed, f'exit {r.returncode} {r.stderr.strip()[-300:]}' if crashed else ''))
    if not crashed:
        out = json.loads(r.stdout.strip().splitlines()[-1])
        want = (real_now + _dt.timedelta(days=400)).date()
        checks.append(('+400d moves date.today()', abs((_real_date.fromisoformat(out['today']) - want).days) <= 1, out['today']))
        checks.append(('+400d moves time.time()', abs(out['time'] - (_real_time() + 400 * 86400)) < 600))
        checks.append(('+400d moves utcnow()', out['utc_year'] == (real_now + _dt.timedelta(days=400)).year))
        checks.append(('isinstance(real datetime, datetime) holds', out['isinstance_real'] and out['isinstance_date']))
        checks.append(('compiled modules import and work', 'extension_error' not in out, out.get('extension_error', '')))
        if 'pandas' in out:
            checks.append(('pandas accepts a shifted datetime', out['pandas']['ts_year'] == want.year and out['pandas']['isinstance']))
        if 'numpy' in out:
            checks.append(('numpy datetime64 from a datetime', out['numpy']['dt64'].startswith('2030-01-02T03:04:05')))
        if 'pandas' not in out and 'numpy' not in out:
            print('NOTE pandas and numpy are not installed here; the compiled-module checks only covered the stdlib')
    r = run({'TDA_CLOCK_DATE': '2031-02-28'})
    ok = r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1])['today'] == '2031-02-28'
    checks.append(('TDA_CLOCK_DATE jumps to the date', ok, r.stderr.strip()[-200:]))
    r = run({'TDA_CLOCK_SHIFT': 'tomorrow'})
    checks.append(('a malformed shift is an error', r.returncode != 0 and 'TDA_CLOCK_SHIFT must look like' in r.stderr))
    r = run({})
    ok = r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1])['today'] == _real_date.today().isoformat()
    checks.append(('no variable, no change', ok))
    failed = 0
    for name, ok, *extra in checks:
        failed += not ok
        note = extra[0] if extra and extra[0] else ''
        print(f"{'PASS' if ok else 'FAIL'} {name}{f'  [{note}]' if note else ''}")
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


if __name__ == '__main__' and '--self-test' in sys.argv:
    raise SystemExit(_self_test())
