#!/usr/bin/env python3
"""Build "harbour-suite", the sample repository the release candidate is cut from (stdlib + git).

    python make_repo.py <empty dir>

The example can't ship a .git directory, so this script creates one with fixed authors, dates and
contents. The commit ids are therefore the same on every machine; candidate.json and the provenance
sidecars name them. Three commits: the previous release (tagged v2026.11.1), a fix to the stored
theme, and a change to the booking page copy, which is the candidate. The files are placeholders for
the sample apps the reports describe; the gate only reads the history.
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

COMMITS = [
    ('2026-11-03', 'Release 2026.11.1', {
        'README.md': '# harbour-suite\n\nSample monorepo for the release readiness gate example.\n',
        'web/src/prefs.ts': 'export function loadPrefs() {\n  const raw = localStorage.getItem("prefs");\n  return raw ? JSON.parse(raw) : {};\n}\n',
        'invoicer/__init__.py': '"""Invoicer app (placeholder)."""\n',
        'site/book.html': '<h1>Book an appointment</h1>\n<p>Choose a day.</p>\n',
    }),
    ('2026-11-06', 'Guard the stored theme against old formats', {
        'web/src/prefs.ts': 'export function loadPrefs() {\n  try {\n    const raw = localStorage.getItem("prefs");\n    return raw ? JSON.parse(raw) : {};\n  } catch {\n    return {};\n  }\n}\n',
    }),
    ('2026-11-09', 'Booking page: clearer day picker copy', {
        'site/book.html': '<h1>Book an appointment</h1>\n<p>Choose a day, then a time.</p>\n',
    }),
]


def git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME='Sample Author', GIT_AUTHOR_EMAIL='author@example.test',
               GIT_COMMITTER_NAME='Sample Author', GIT_COMMITTER_EMAIL='author@example.test')
    if when:
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{when}T06:00:00+00:00'
    res = subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false', *args],
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
        git(repo, 'commit', '-q', '-m', msg, when=when)
        if i == 0:
            git(repo, 'tag', 'v2026.11.1')
    for line in git(repo, 'log', '--format=%H %ad %s', '--date=short').splitlines():
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
