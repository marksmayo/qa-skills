#!/usr/bin/env python3
"""Mine per-file change history from git: churn, authors and fix commits in a window (stdlib only).

    python mine_history.py --repo <repo> --out history.json [--days 180] [--as-of 2026-11-01]

Runs `git log --numstat` over the window ending at --as-of (default: the date of HEAD's commit,
so the same repository always gives the same window). For every file: commits, lines added and
deleted, distinct authors (as pseudonyms for the run, author-1, author-2, ...: names are not kept,
only counted), fix commits and the first commit that touched it. A commit is a fix
when its conventional-commit type is fix, bugfix, hotfix or revert; or, without a conventional
type, when its subject starts with fix/fixes/fixed/bugfix/hotfix/revert (or "bug:"/"regression:"),
or closes an issue (fixes #12, closes PROJ-7). "bug" or "regression" elsewhere in a subject is a noun
("Add a bug reporter") and never makes a fix. feat:, chore: and other types are never fixes,
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
FIX_FIRST = re.compile(r'^\s*(\[[^\]]*\]\s*)?(fix(e[sd]|ing)?|bug-?fix|hot-?fix|revert(s|ed)?)\b|^\s*(bug|regression)\s*:', re.I)
# Only closing an issue counts outside the first word: "bug" or "regression" as a noun names what a commit is about
# ("Add a bug reporter", "Add visual regression checks"), not a defect it fixes.
FIX_ANYWHERE = re.compile(r'\b(fix(es|ed)?|close[sd]?|resolve[sd]?)\s+(#|[A-Z][A-Z0-9]+-)\d+', re.I)
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
    fixed, bugfix, hotfix or revert (or "bug:" / "regression:"), or close an issue (fixes #12, closes PROJ-7): a
    "Redesign the UI, fix the hero video" commit is a feature, not a defect, and "Add a bug reporter" or "Add visual
    regression checks" name a subject, not a defect. A merge tool's prefix ("Merged PR 123: ")
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
        return 'closes_issue'
    return None
FORMAT = re.compile(r'^(style|format|chore\(format\)|lint)\b|\b(reformat|whitespace|prettier|black|isort|gofmt)\b', re.I)
KEY = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')
DOCS = re.compile(r'(\.(md|rst|txt|adoc)$|(^|/)(docs?|documentation)/|(^|/)(LICENSE|CHANGELOG|AUTHORS|NOTICE)[^/]*$)', re.I)
SEP = '\x1e'


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {res.stderr.strip()}')
    return res.stdout


def mine(repo: Path, days: int, as_of: date | None) -> dict:
    shallow = git(repo, 'rev-parse', '--is-shallow-repository').strip() == 'true'
    head_date = datetime.fromisoformat(git(repo, 'log', '-1', '--format=%cI').strip()).date()
    explicit = as_of is not None
    if as_of is None:
        as_of = head_date
    since = as_of - timedelta(days=days)
    notes = []
    gap = (as_of - head_date).days
    if gap > days // 6:
        notes.append(f'the window ends {gap} days after the HEAD commit ({head_date}); commits before {since} are left out, '
                     f'so a window ending at HEAD (omit --as-of) or a longer --days keeps more history')
    fmt = f'{SEP}%H%x1f%aI%x1f%an%x1f%P%x1f%s%x1f%b'
    # The default window ends at HEAD and is bounded by HEAD's ancestry, not by a date: an ancestor dated after HEAD
    # (clock skew, a rewritten date) is history of this release and is kept. An explicit --as-of cuts by date, and the
    # ancestors it leaves out for being dated after it are counted in a note.
    until = [f'--until={as_of.isoformat()}T23:59:59'] if explicit else []
    if explicit:
        cut = git(repo, 'rev-list', '--count', f'--since={as_of.isoformat()}T23:59:59', 'HEAD').strip()
        if cut not in ('', '0'):
            notes.append(f'{cut} commit(s) of HEAD\'s ancestry are dated after --as-of {as_of} and are left out of the '
                         'window; omit --as-of to keep every ancestor of HEAD')
    log_args = ['log', '--numstat', '--no-renames', f'--since={since.isoformat()}T00:00:00', *until, f'--format={fmt}']
    out = git(repo, *log_args)
    first_touch: dict[str, str] = {}
    for line in git(repo, 'log', '--reverse', '--name-only', '--format=@%aI').splitlines():
        if line.startswith('@'):
            when = line[1:11]
        elif line.strip():
            first_touch.setdefault(line.strip(), when)
    commits, files = [], {}
    pseudonyms: dict[str, str] = {}
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
        docs_only = is_fix and bool(stats) and all(DOCS.search(pth) for pth, _, _ in stats)
        if docs_only:                      # "fix typo in README": not a defect in any code area
            is_fix = False
        formatting = bool(FORMAT.search(strip_merge_prefix(subject)[0])) and bool(stats) and all(abs(a - d) <= 2 for _, a, d in stats)
        # Authors are only counted (distinct authors per file), so names are not kept: each person gets a pseudonym
        # for this run (author-1 is the newest commit's author), stable across files and commits within the run.
        author = pseudonyms.setdefault(author, f'author-{len(pseudonyms) + 1}')
        c = {'sha': sha[:12], 'date': when[:10], 'subject': subject[:200], 'fix': is_fix,
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
        if docs_only:
            c['fix_ignored'] = 'touches only documentation'
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
        f['authors'] = sorted(f['authors'], key=lambda x: int(x.rsplit('-', 1)[1]))
    later = [c for c in commits if c['date'] > head_date.isoformat()]
    if later:
        notes.append(f'{len(later)} commit(s) of HEAD\'s ancestry are dated after HEAD ({head_date}): '
                     + ', '.join(f'{c["sha"][:7]} {c["date"]}' for c in later[:5])
                     + '; clock skew or rewritten dates; they are kept in the window because HEAD contains them')
    docs_fixes = [c for c in commits if c.get('fix_ignored')]
    if docs_fixes:
        notes.append(f'{len(docs_fixes)} fix commit(s) touch only documentation and are not counted as fixes: '
                     + ', '.join(c['sha'][:7] for c in docs_fixes[:5]))
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


def self_test() -> int:
    """A synthetic repository whose ancestry holds a commit dated after HEAD (clock skew) and a docs-only fix: the window
    ending at HEAD keeps the later-dated ancestor and says so, and the docs-only fix is not counted as a fix."""
    import os, shutil, tempfile
    tmp = Path(tempfile.mkdtemp(prefix='mh-self-'))
    fails = []
    try:
        def commit(msg: str, when: str, files: dict, author: str = 'Sample Person') -> None:
            for rel, text in files.items():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_text(text, encoding='utf-8')
            env = {**os.environ, 'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when, 'GIT_AUTHOR_NAME': author,
                   'GIT_AUTHOR_EMAIL': 'sample@example.test', 'GIT_COMMITTER_NAME': 'Sample Person',
                   'GIT_COMMITTER_EMAIL': 'sample@example.test'}
            subprocess.run(['git', '-C', str(tmp), 'add', '-A'], check=True, capture_output=True, env=env)
            subprocess.run(['git', '-C', str(tmp), 'commit', '-q', '-m', msg], check=True, capture_output=True, env=env)
        subprocess.run(['git', 'init', '-q', str(tmp)], check=True, capture_output=True)
        commit('Add the timer', '2026-03-01T10:00:00Z', {'app/timer.js': 'let a = 1;\n'})
        commit('fix the expiry date check', '2026-12-31T10:00:00Z', {'app/timer.js': 'let a = 2;\n'}, author='Other Person')
        commit('fix typo in README', '2026-04-01T10:00:00Z', {'README.md': 'Timer\n'})
        commit('Add the menu', '2026-05-29T10:00:00Z', {'app/menu.js': 'let m = 1;\n'})
        # "bug" and "regression" as nouns name what a feature is about, not a defect it fixes
        commit('Add bug reporter tool', '2026-06-01T10:00:00Z', {'app/reporter.js': 'let r = 1;\n'})
        commit('Add visual regression checker', '2026-06-02T10:00:00Z', {'app/visual.js': 'let v = 1;\n'})
        commit('Document the regression suite and the bug template', '2026-06-03T10:00:00Z', {'app/menu.js': 'let m = 2;\n'})
        commit('Fix the menu crash on an empty list', '2026-06-04T10:00:00Z', {'app/menu.js': 'let m = 3;\n'})
        commit('Handle an empty basket on save, closes #12', '2026-06-05T10:00:00Z', {'app/menu.js': 'let m = 4;\n'})
        commit('fix(visual): compare at the same zoom', '2026-06-06T10:00:00Z', {'app/visual.js': 'let v = 2;\n'})
        res = mine(tmp, 180, None)
        text = json.dumps(res)
        if 'Sample Person' in text or 'Other Person' in text:
            fails.append('commit author names are not written to the history (only distinct-author counts are used)')
        if len(res['files'].get('app/timer.js', {}).get('authors') or []) != 2:
            fails.append(f'two people changed app/timer.js: two distinct authors '
                         f'(got {res["files"].get("app/timer.js", {}).get("authors")})')
        if len(res['files'].get('app/menu.js', {}).get('authors') or []) != 1:
            fails.append('one person changed app/menu.js: one distinct author')
        subjects = {c['subject']: c for c in res['commits']}
        if 'fix the expiry date check' not in subjects:
            fails.append(f'an ancestor of HEAD dated after it is in the window (got {sorted(subjects)})')
        if not any('dated after' in n for n in res['notes']):
            fails.append(f'a note says ancestors are dated after HEAD (got {res["notes"]})')
        if subjects.get('fix typo in README', {}).get('fix'):
            fails.append('a fix commit touching only documentation is not counted as a fix')
        if res['files'].get('app/timer.js', {}).get('fix_commits') == []:
            fails.append('the later-dated fix counts against the file it touched')
        for subj, want in (('Add bug reporter tool', False), ('Add visual regression checker', False),
                           ('Document the regression suite and the bug template', False),
                           ('Fix the menu crash on an empty list', True), ('Handle an empty basket on save, closes #12', True),
                           ('fix(visual): compare at the same zoom', True)):
            if bool(subjects.get(subj, {}).get('fix')) != want:
                fails.append(f'{subj!r}: fix {not want}, expected {want} (a fix is named by a conventional type, a leading '
                             'fix verb or a closed issue, never by "bug" or "regression" as a noun)')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('mine_history self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('mine_history self-test: ok')
    return 0


def main(argv=None) -> int:
    if argv is None and '--self-test' in sys.argv[1:]:
        return self_test()
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
