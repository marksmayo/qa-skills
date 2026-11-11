#!/usr/bin/env python3
"""Build "tidewater", the sample repository behind the example CI run (stdlib + git).

    python make_repo.py <empty dir>

The example can't ship a .git directory, so this script creates one with fixed authors, dates and
contents; the commit ids are the same on every machine, and the recorded CI run names them.

History on main:
  1. a green commit (CI run 101 passed);
  2. "Use banker's rounding for fares"   - planted regression: a rounding test fails;
  3. "Add sample bookings fixture"        - planted data problem: a booking dated 2026-11-10;
  4. "Docs: describe fare rounding"       - decoy: docs only, mentions rounding, touches no code.
CI run 109 ran on commit 4. The slot-picking test is flaky from the start (about one run in four).
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

PYPROJECT = '''[project]
name = "tidewater"
version = "0.4.0"
requires-python = ">=3.10"

[tool.pytest.ini_options]
testpaths = ["tests"]
'''
FARES_V1 = '''from decimal import Decimal, ROUND_HALF_UP


def fare(base: Decimal, discount_pct: int = 0) -> Decimal:
    """Fare after a percentage discount, rounded to the cent (half up, as printed on tickets)."""
    total = base * Decimal(100 - discount_pct) / Decimal(100)
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
'''
FARES_V2 = FARES_V1.replace('ROUND_HALF_UP', 'ROUND_HALF_EVEN').replace('(half up, as printed on tickets)', '(banker\'s rounding)')
BOOKING = '''from datetime import date


def is_open(sailing: date, today: date | None = None) -> bool:
    """A sailing can be booked until the day it leaves."""
    return sailing >= (today or date.today())
'''
SLOTS = '''import random


def pick_two(slots: list[str]) -> tuple[str, str]:
    """Two boarding slots for a group of two."""
    return random.choice(slots), random.choice(slots)
'''
TEST_FARES = '''from decimal import Decimal

from tidewater.fares import fare


def test_fare_without_discount():
    assert fare(Decimal("42.00")) == Decimal("42.00")


def test_fare_rounds_half_up():
    # a half-price fare of 24.25 is 12.125: tickets print 12.13
    assert fare(Decimal("24.25"), discount_pct=50) == Decimal("12.13")
'''
TEST_SLOTS = '''from tidewater.slots import pick_two


def test_pick_two_gives_two_different_slots():
    a, b = pick_two(["A1", "A2", "A3", "A4"])
    assert a != b
'''
TEST_BOOKING_V1 = '''from datetime import date, timedelta

from tidewater.booking import is_open


def test_future_sailing_is_open():
    assert is_open(date.today() + timedelta(days=3))
'''
TEST_BOOKING_V2 = TEST_BOOKING_V1 + '''

def test_sample_booking_is_open():
    import json
    from pathlib import Path
    booking = json.loads((Path(__file__).parent / "fixtures" / "bookings.json").read_text())[0]
    sailing = date.fromisoformat(booking["sailing"])
    assert is_open(sailing), f"sample booking {booking['ref']} sails {sailing}, which is in the past"
'''
FIXTURE = '[\n  {"ref": "TW-1001", "sailing": "2026-11-10", "passengers": 2}\n]\n'
CI = '''name: ci
on: [push]
jobs:
  unit:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with: {python-version: "3.13"}
      - run: pip install pytest
      - run: python -m pytest --junitxml=junit.xml
      - uses: actions/upload-artifact@v5
        if: always()
        with: {name: junit-unit, path: junit.xml}
  install-check:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - name: Install internal pricing package
        run: pip install --index-url http://127.0.0.1:9/simple/ tidewater-internal-pricing==2.1
'''
README_V1 = '# tidewater\n\nFerry fares and bookings (sample package for the CI failure triager example).\n'
README_V2 = README_V1 + '\n## Fares\n\nFares are rounded to the cent. See `tidewater/fares.py` for the rounding mode.\n'

COMMITS = [
    ('2026-11-09', 'Fares, bookings and boarding slots', {
        'pyproject.toml': PYPROJECT, 'README.md': README_V1, '.github/workflows/ci.yml': CI,
        'tidewater/__init__.py': '', 'tidewater/fares.py': FARES_V1, 'tidewater/booking.py': BOOKING, 'tidewater/slots.py': SLOTS,
        'tests/test_fares.py': TEST_FARES, 'tests/test_slots.py': TEST_SLOTS, 'tests/test_booking.py': TEST_BOOKING_V1}),
    ('2026-11-10', "Use banker's rounding for fares", {'tidewater/fares.py': FARES_V2}),
    ('2026-11-11', 'Add sample bookings fixture', {'tests/fixtures/bookings.json': FIXTURE, 'tests/test_booking.py': TEST_BOOKING_V2}),
    ('2026-11-11', 'Docs: describe fare rounding', {'README.md': README_V2}),
]


def git(repo: Path, *args: str, when: str | None = None, n: int = 0) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME='Sample Author', GIT_AUTHOR_EMAIL='author@example.test',
               GIT_COMMITTER_NAME='Sample Author', GIT_COMMITTER_EMAIL='author@example.test')
    if when:
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{when}T0{n}:00:00+00:00'
    res = subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
                         capture_output=True, text=True, env=env)
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)}: {res.stderr}')
    return res.stdout


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__); return 2
    repo = Path(sys.argv[1])
    repo.mkdir(parents=True, exist_ok=True)
    if any(repo.iterdir()):
        raise SystemExit(f'{repo} is not empty')
    git(repo, 'init', '-q', '-b', 'main')
    for i, (when, msg, files) in enumerate(COMMITS):
        for rel, text in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode('utf-8'))
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', msg, when=when, n=i + 1)
    print(git(repo, 'log', '--format=%H %ad %s', '--date=iso-strict'), end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
