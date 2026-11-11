#!/usr/bin/env python3
"""Build "harbour-loyalty", the sample repository behind the traceability example (stdlib + git).

    python make_repo.py <empty dir>

Fixed authors, dates and contents, so the commit ids are the same on every machine. Commits name the
ticket keys they implement, as teams usually do:
  1. Points per dollar (SHOP-401)            - code and tests naming SHOP-401 AC-1..AC-3
  2. Tier multipliers for SHOP-402           - code and tests for AC-1 and AC-2 only (AC-3 has no test)
  3. Points expiry (SHOP-403)                - code with a planted off-by-one; its test names no key
                                               (it is linked through Jira and the Xray export)
  4. Referral bonus, SHOP-404                - code only, no test
  5. Legacy import fixes (SHOP-4010)         - a different ticket; its test must not link to SHOP-401
plus an orphan test that names no requirement.
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

POINTS_1 = '''"""Loyalty points."""
from decimal import Decimal

MIN_SUBTOTAL = Decimal("10.00")
CAP = 500


def earn(subtotal: Decimal) -> int:
    """SHOP-401: 1 point per whole dollar from $10.00, capped at 500 points."""
    if subtotal < MIN_SUBTOTAL:
        return 0
    return min(int(subtotal), CAP)
'''
POINTS_2 = POINTS_1 + '''

MULTIPLIER = {"Gold": Decimal("2"), "Silver": Decimal("1.5"), "Bronze": Decimal("1")}


def earn_for_tier(subtotal: Decimal, tier: str) -> int:
    # SHOP-402: tier multipliers, rounded down
    return int(earn(subtotal) * MULTIPLIER[tier])
'''
EXPIRY = '''"""Points expiry."""
from datetime import date


def expired(earned_on: date, today: date) -> bool:
    """Points expire 12 months after the day they were earned."""
    months = (today.year - earned_on.year) * 12 + today.month - earned_on.month
    return months > 12  # planted: should be >= 12 (expires on the anniversary)
'''
REFERRAL = '''"""Referral bonus."""

BONUS = 250


def referral_bonus(referrer_orders: int) -> int:
    """SHOP-404: 250 points when the referred friend's first order is delivered, if the referrer has ordered before."""
    return BONUS if referrer_orders > 0 else 0
'''
IMPORTER = '''"""Legacy points import."""


def parse_legacy(line: str) -> tuple[str, int]:
    member, points = line.split(";")
    return member.strip(), int(points)
'''
TEST_POINTS_1 = '''from decimal import Decimal

from loyalty.points import earn


def test_earn_one_point_per_whole_dollar():
    """SHOP-401 AC-1: 1 point per whole dollar."""
    assert earn(Decimal("123.45")) == 123


def test_no_points_below_ten_dollars():
    """SHOP-401 AC-2: orders below $10.00 earn nothing."""
    assert earn(Decimal("9.99")) == 0


def test_points_capped_at_500():
    """SHOP-401 AC-3: orders above $500.00 earn 500 points."""
    assert earn(Decimal("842.10")) == 500
'''
TEST_POINTS_2 = TEST_POINTS_1 + '''

def test_gold_members_earn_double():
    """SHOP-402 AC-1"""
    from loyalty.points import earn_for_tier
    assert earn_for_tier(Decimal("100.00"), "Gold") == 200


def test_silver_members_earn_one_and_a_half_rounded_down():
    """SHOP-402 AC-2: 1.5 times, rounded down."""
    from loyalty.points import earn_for_tier
    assert earn_for_tier(Decimal("101.00"), "Silver") == 151
'''
TEST_EXPIRY = '''from datetime import date

from loyalty.expiry import expired


def test_points_expire_after_12_months():
    assert expired(date(2025, 11, 12), date(2026, 11, 12))
'''
TEST_LEGACY = '''from loyalty.importer import parse_legacy


def test_bulk_import_line():
    """Regression for SHOP-4010: legacy lines with spaces around the separator."""
    assert parse_legacy(" M-1 ; 40 ") == ("M-1", 40)


def test_legacy_bonus_flag_is_ignored():
    assert parse_legacy("M-2;10") == ("M-2", 10)
'''
PYPROJECT = '[project]\nname = "harbour-loyalty"\nversion = "0.3.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

COMMITS = [
    ('2026-11-02', 'Points per dollar (SHOP-401)', {'pyproject.toml': PYPROJECT, 'loyalty/__init__.py': '', 'loyalty/points.py': POINTS_1,
                                                   'tests/test_points.py': TEST_POINTS_1, 'README.md': '# harbour-loyalty\n'}),
    ('2026-11-04', 'Tier multipliers for SHOP-402', {'loyalty/points.py': POINTS_2, 'tests/test_points.py': TEST_POINTS_2}),
    ('2026-11-06', 'Points expiry (SHOP-403)', {'loyalty/expiry.py': EXPIRY, 'tests/test_expiry.py': TEST_EXPIRY}),
    ('2026-11-08', 'Referral bonus, SHOP-404', {'loyalty/referral.py': REFERRAL}),
    ('2026-11-09', 'Legacy import fixes (SHOP-4010)', {'loyalty/importer.py': IMPORTER, 'tests/test_legacy.py': TEST_LEGACY}),
]


def git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME='Sample Author', GIT_AUTHOR_EMAIL='author@example.test',
               GIT_COMMITTER_NAME='Sample Author', GIT_COMMITTER_EMAIL='author@example.test')
    if when:
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{when}T03:00:00+00:00'
    res = subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args], capture_output=True, text=True, env=env)
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
    for when, msg, files in COMMITS:
        for rel, text in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode('utf-8'))
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', msg, when=when)
    print(git(repo, 'log', '--format=%h %ad %s', '--date=short'), end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
