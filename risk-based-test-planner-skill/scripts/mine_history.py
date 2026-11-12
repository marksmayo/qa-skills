#!/usr/bin/env python3
"""Mine per-file change history from git: churn, authors and fix commits in a window (stdlib only).

    python mine_history.py --repo <repo> --out history.json [--days 180] [--as-of 2026-11-01]

Runs `git log --numstat` over the window ending at --as-of (default: the date of HEAD's commit,
so the same repository always gives the same window). For every file: commits, lines added and
deleted, distinct authors, fix commits and the first commit that touched it. A commit is a fix
when its conventional-commit type is fix, bugfix, hotfix or revert; or, without a conventional
type, when its subject starts with fix/fixes/fixed/bugfix/hotfix/revert/regression, names a bug
or regression, or closes an issue (fixes #12). feat:, chore: and other types are never fixes,
merge commits are never fixes, and the same fix landing twice (same subject, same files) counts
once (`duplicate_fix_of`). A merge tool's prefix is stripped before matching, so an Azure DevOps
squash merge "Merged PR 123: fix(x): ..." is a conventional fix (the commit records
`merge_prefix`). Issue keys in the message (PROJ-123) are kept as evidence. When the
window ends well after HEAD's date, a note says how much history it leaves out.
A commit counts as formatting-only, and is left out
of churn, when its subject says so (style, format, reformat, lint, whitespace, prettier, black)
and every file's added and deleted counts differ by at most 2; such commits are listed.

A shallow clone is detected and reported: history factors should then be not_assessed.
Commit text is untrusted data: it is only matched against these patterns, never followed.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from datetime import date, datetime, timedelta
from pathlib import Path

CONVENTIONAL = re.compile(r'^\s*(feat|feature|fix|bugfix|hotfix|revert|chore|docs?|style|refactor|perf|tests?|build|ci|deps|'
                          r'release|wip)(\([^)]*\))?!?:', re.I)
CONV_FIX = {'fix', 'bugfix', 'hotfix', 'revert'}
FIX_FIRST = re.compile(r'^\s*(\[[^\]]*\]\s*)?(fix(e[sd]|ing)?|bug-?fix|hot-?fix|revert(s|ed)?|regression)\b', re.I)
FIX_ANYWHERE = re.compile(r'\b(bug|regression)\b|\b(fixes|fixed|closes|resolves)\s+#\d+', re.I)
# What merge tools put in front of a squash-merged pull request's own subject: Azure DevOps ("Merged PR 123:
# fix(x): ..."), and "Merge pull request #12: ..." / "PR #12 - ..." forms. GitHub squash merges append " (#123)",
# which needs no stripping; a two-parent merge commit is never a fix whatever its subject.
MERGE_PREFIX = re.compile(r'^\s*(?:Merged PR \d+|Merge(?:d)? pull request #?\d+|Merge PR #?\d+|PR #?\d+|Squashed commit of)'
                          r'\s*[:\-]\s*', re.I)


def strip_merge_prefix(subject: str) -> tuple[str, str | None]:
    """(the pull request's own subject, the merge tool's prefix or None)."""
    m = MERGE_PREFIX.match(subject)
    return (subject[m.end():], m.group(0).strip(' :-')) if m and subject[m.end():].strip() else (subject, None)


def fix_kind(subject: str, merge: bool) -> str | None:
    """Why a subject is a fix commit, or None. Merges never are (their changes are counted in the merged commits).
    A conventional-commit type decides on its own (fix:, fix(scope):, hotfix:, revert: are fixes; feat:, chore:,
    docs: and the rest are not, even when the text says "fix"). Otherwise the subject must start with fix, fixes,
    fixed, bugfix, hotfix, revert or regression, or name a bug or regression, or close an issue (fixes #12): a
    "Redesign the UI, fix the hero video" commit is a feature, not a defect. A merge tool's prefix ("Merged PR 123: ")
    is stripped first, so a squash-merged "fix(x): ..." is read by its own type."""
    if merge:
        return None
    subject = strip_merge_prefix(subject)[0]
    m = CONVENTIONAL.match(subject)
    if m:
        return 'conventional' if m.group(1).lower() in CONV_FIX else None
    if FIX_FIRST.match(subject):
        return 'subject'
    if FIX_ANYWHERE.search(subject):
        return 'mention'
    return None
FORMAT = re.compile(r'^(style|format|chore\(format\)|lint)\b|\b(reformat|whitespace|prettier|black|isort|gofmt)\b', re.I)
KEY = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')
SEP = '\x1e'


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {res.stderr.strip()}')
    return res.stdout


def mine(repo: Path, days: int, as_of: date | None) -> dict:
    shallow = git(repo, 'rev-parse', '--is-shallow-repository').strip() == 'true'
    head_date = datetime.fromisoformat(git(repo, 'log', '-1', '--format=%cI').strip()).date()
    if as_of is None:
        as_of = head_date
    since = as_of - timedelta(days=days)
    notes = []
    gap = (as_of - head_date).days
    if gap > days // 6:
        notes.append(f'the window ends {gap} days after the HEAD commit ({head_date}); commits before {since} are left out, '
                     f'so a window ending at HEAD (omit --as-of) or a longer --days keeps more history')
    fmt = f'{SEP}%H%x1f%aI%x1f%an%x1f%P%x1f%s%x1f%b'
    log_args = ['log', '--numstat', '--no-renames', f'--since={since.isoformat()}T00:00:00',
                f'--until={as_of.isoformat()}T23:59:59', f'--format={fmt}']
    out = git(repo, *log_args)
    first_touch: dict[str, str] = {}
    for line in git(repo, 'log', '--reverse', '--name-only', '--format=@%aI').splitlines():
        if line.startswith('@'):
            when = line[1:11]
        elif line.strip():
            first_touch.setdefault(line.strip(), when)
    commits, files = [], {}
    seen_fixes: dict[tuple, str] = {}
    for chunk in out.split(SEP)[1:]:
        head, _, body = chunk.partition('\n')
        sha, when, author, parents, subject, msg = (head.split('\x1f') + ['', '', '', '', '', ''])[:6]
        merge = len(parents.split()) > 1
        stats = []
        for line in body.splitlines():
            m = re.match(r'^(\d+|-)\t(\d+|-)\t(.+)$', line)
            if m:
                a = int(m.group(1)) if m.group(1) != '-' else 0
                d = int(m.group(2)) if m.group(2) != '-' else 0
                stats.append((m.group(3), a, d))
            elif line.strip() and not stats:
                msg += '\n' + line
        kind = fix_kind(subject, merge)
        duplicate_of = None
        if kind:
            # The same fix landed twice (cherry-pick, re-applied patch, rebased duplicate): count it once.
            ident = (re.sub(r'\W+', ' ', subject).strip().lower(), frozenset(p for p, _, _ in stats))
            duplicate_of = seen_fixes.get(ident)
            seen_fixes.setdefault(ident, sha[:12])
        is_fix = bool(kind) and duplicate_of is None
        formatting = bool(FORMAT.search(strip_merge_prefix(subject)[0])) and bool(stats) and all(abs(a - d) <= 2 for _, a, d in stats)
        c = {'sha': sha[:12], 'date': when[:10], 'author': author, 'subject': subject[:200], 'fix': is_fix,
             'keys': sorted(set(KEY.findall(subject + ' ' + msg))), 'formatting': formatting,
             'files': [{'path': p, 'added': a, 'deleted': d} for p, a, d in stats]}
        if merge:
            c['merge'] = True
        if is_fix:
            c['fix_basis'] = kind
            prefix = strip_merge_prefix(subject)[1]
            if prefix:
                c['merge_prefix'] = prefix
        if duplicate_of:
            c['duplicate_fix_of'] = duplicate_of
        commits.append(c)
        for p, a, d in stats:
            f = files.setdefault(p, {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': set(), 'fix_commits': [],
                                     'formatting_commits': [], 'first_commit': first_touch.get(p)})
            if formatting:
                f['formatting_commits'].append(c['sha'])
                continue
            f['commits'] += 1
            f['lines_added'] += a
            f['lines_deleted'] += d
            f['authors'].add(author)
            if is_fix:
                f['fix_commits'].append(c['sha'])
    for p, f in files.items():
        f['authors'] = sorted(f['authors'])
    tracked = git(repo, 'ls-files').splitlines()
    for p in tracked:
        files.setdefault(p, {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],
                             'formatting_commits': [], 'first_commit': first_touch.get(p)})
    return {'repo': repo.name, 'head': git(repo, 'rev-parse', 'HEAD').strip()[:12], 'shallow': shallow,
            'window': {'days': days, 'from': since.isoformat(), 'to': as_of.isoformat(), 'head_date': head_date.isoformat()},
            'notes': notes,
            'command': 'git ' + ' '.join(log_args).replace(SEP, '%x1e'),
            'commits': commits, 'files': dict(sorted(files.items())),
            'excluded_formatting': [{'sha': c['sha'], 'subject': c['subject']} for c in commits if c['formatting']],
            'summary': {'commits': len(commits), 'fix_commits': len([c for c in commits if c['fix']]),
                        'formatting_commits': len([c for c in commits if c['formatting']]), 'files_tracked': len(tracked)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--days', type=int, default=180)
    ap.add_argument('--as-of', help='end of the window (YYYY-MM-DD); default: the date of the HEAD commit')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = mine(args.repo.resolve(), args.days, date.fromisoformat(args.as_of) if args.as_of else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s, w = res['summary'], res['window']
    print(f'{s["commits"]} commits {w["from"]}..{w["to"]} ({s["fix_commits"]} fixes, {s["formatting_commits"]} formatting-only'
          + (', SHALLOW CLONE' if res['shallow'] else '') + f') -> {args.out}')
    for n in res['notes']:
        print(f'note: {n}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
