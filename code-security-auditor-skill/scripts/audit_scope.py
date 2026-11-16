#!/usr/bin/env python3
"""What the audit reads: the host repository's own code, without its sample projects, each copied file once.

Used by recon.py, scan_pitfalls.py, scan_secrets.py, run_scanners.py, merge_findings.py and seed_report.py, on top of
the shared repo_layout.py (fixture projects and copy groups) and lang_census.py (whose census leaves the fixtures out).

- Fixture projects (a nested sample project with its own manifest, expected output or app under examples/,
  fixtures/, testdata/, samples/ ...; see repo_layout.fixture_projects) are left out by default when a repository is
  audited: a deliberately vulnerable sample is not the repository's finding. Every one left out is listed
  (`fixtures_excluded`) so the report can say so; --include-fixtures audits them too, --fixture-dir names more.
- Files kept identical in several places (a declared mapping such as tools/check_shared.py, or the same content)
  are read once: a finding or entry point is reported on the canonical copy (or, when the canonical copy is outside
  what was read, on the first copy read) with the other copies listed (`copies`).
- redact_home() replaces the user's home directory in a path or command with ~, so a seeded report does not carry
  the account name.
"""
from __future__ import annotations
import bisect, os, re, sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_layout  # noqa: E402


def in_code(spans, start: int, end: int) -> bool:
    """A match [start, end) counts as code when it starts in code, or starts inside a string but runs on past that
    string's end (claims["roles"] = os.getenv(...)). A match wholly inside one string, docstring or comment (a
    pattern table, a usage example, a fixture string) does not."""
    if not spans:
        return True
    i = bisect.bisect_right(spans, (start, float('inf'), '')) - 1
    if i < 0 or not (spans[i][0] <= start < spans[i][1]):
        return True
    return end > spans[i][1] + 1  # past the closing quote


def add_arguments(ap) -> None:
    ap.add_argument('--include-fixtures', action='store_true',
                    help='also audit the sample and fixture projects (examples/, fixtures/, testdata/ ... with their own '
                         'manifest or app); by default they are left out and listed')
    ap.add_argument('--fixture-dir', action='append', default=[], metavar='DIR',
                    help='a further fixture parent folder (a name such as demos, or a path); repeatable')


def fixture_projects(root: Path, extra=()) -> list[dict]:
    try:
        return repo_layout.fixture_projects(root, tuple(extra or ()))
    except Exception:  # noqa: BLE001 - an unreadable tree means no fixtures found, never a crash
        return []


def fixture_of(rel: str, fixtures: list[dict]) -> str | None:
    return repo_layout.is_fixture(rel, fixtures)


def fixture_parent(rel: str) -> str | None:
    """The examples/, fixtures/, testdata/, samples/ ... folder a file lies under (sample material even when it is not
    a project of its own: an example PoC request, a sample plan), or None."""
    parts = rel.split('/')[:-1]
    for i, p in enumerate(parts):
        if p.lower() in repo_layout.FIXTURE_PARENTS:
            return '/'.join(parts[:i + 1])
    return None


def representatives(root: Path, files: list[str]) -> tuple[dict[str, str], list[dict]]:
    """({path: representative} for every member of a copy group among `files`, the groups). The representative is
    the group's canonical copy when it is among `files`, else the first member that is."""
    fileset = set(files)
    try:
        groups = repo_layout.copy_groups(root, files=sorted(fileset))
    except Exception:  # noqa: BLE001
        return {}, []
    rep: dict[str, str] = {}
    out = []
    for g in groups:
        members = [m for m in [g['canonical'], *g['copies']] if m in fileset]
        if len(members) < 2:
            continue
        r = members[0]
        for m in members:
            rep[m] = r
        out.append({'canonical': r, 'copies': [m for m in members if m != r], 'how': g['how']})
    return rep, out


HOME_PATTERNS = [re.compile(r'(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s"\']+'), re.compile(r'(?<![\w.])/(?:home|Users)/[^/\s"\']+')]


def redact_home(text):
    """text with the home directory (and any C:\\Users\\<name> or /home/<name> prefix) replaced by ~."""
    if not isinstance(text, str):
        return text
    home = os.path.expanduser('~')
    if home and home not in ('~', '/'):
        for h in {home, home.replace('\\', '/'), home.replace('\\', '\\\\')}:
            text = text.replace(h, '~')
    for rx in HOME_PATTERNS:
        text = rx.sub('~', text)
    return text


def home_paths(text: str) -> list[str]:
    """Home-directory paths in text (C:\\Users\\<name>\\..., /home/<name>/..., /Users/<name>/...)."""
    return [m.group(0) for rx in HOME_PATTERNS for m in rx.finditer(text or '')]
