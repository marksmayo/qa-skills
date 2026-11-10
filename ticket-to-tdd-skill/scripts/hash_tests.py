#!/usr/bin/env python3
"""Lock and check the red tests of a Ticket to TDD report (stdlib only).

    python hash_tests.py lock  tdd_report.json --repo .   # after RED: record test-file hashes
    python hash_tests.py check tdd_report.json --repo .   # any time later: have locked tests changed?
    python hash_tests.py markers tests/test_discount_codes.py   # skip/xfail/only markers in files

`lock` takes each test file's hash from the last red-phase run that hashed it (parse_results.py
--hash) and refuses if the file on disk no longer matches, because then the locked file is not the
one that was seen failing. It writes integrity.test_files, integrity.test_hashes_at_red and
integrity.red_lock_run.

`check` compares the files on disk with the lock and lists markers that would hide a failure
(skip, xfail, only, @Disabled, ...). Exit code 1 if a locked file changed or a marker was found.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

WEAKENING = [
    ('python', re.compile(r'@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail|importorskip)\(|'
                          r'@unittest\.(skip\w*|expectedFailure)\b|\bself\.skipTest\(')),
    ('javascript', re.compile(r'\b(it|test|describe|context|suite)\.(skip|only|todo|fails|fixme)\b|'
                              r'\b(xit|xtest|xdescribe|fit|fdescribe)\s*\(|\btest\.fixme\(')),
    ('jvm', re.compile(r'@(Disabled|Ignore|Ignored)\b|\bAssumptions?\.assume\w*\(|\bassume(True|False|That)\(')),
    ('go', re.compile(r'\bt\.(Skip|SkipNow|Skipf)\(')),
    ('ruby', re.compile(r'^\s*(xit|xdescribe|xcontext|pending|skip)\b', re.M)),
    ('dotnet', re.compile(r'\[(Ignore|Skip)\b|Skip\s*=\s*"|\bAssert\.Inconclusive\(')),
    ('rust', re.compile(r'#\[ignore\b')),
    ('php', re.compile(r'markTest(Skipped|Incomplete)\(|@group\s+(skip|incomplete)')),
    ('swift', re.compile(r'\bXCTSkip(If|Unless)?\b|\bthrow XCTSkip\(')),
    ('dart', re.compile(r'\bskip:\s*(true|\')')),
]

def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    except OSError:
        return None

def weakening_markers(path: Path) -> list[dict]:
    """Markers that skip, defer or focus tests, which would hide a red."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    found = []
    for lang, rx in WEAKENING:
        for m in rx.finditer(text):
            line = text.count('\n', 0, m.start()) + 1
            found.append({'line': line, 'marker': m.group(0).strip(), 'family': lang})
    return sorted(found, key=lambda f: f['line'])

def test_paths(report: dict) -> list[str]:
    return sorted({t['test_path'] for t in report.get('traceability', []) if (t.get('red') or {}).get('status') != 'not_run'})

def last_red_hashes(report: dict) -> tuple[dict, dict]:
    """test_path -> (hash, run_id) from the last red-phase run that hashed it."""
    hashes, runs = {}, {}
    for run in report.get('runs', []):
        if run.get('phase') != 'red':
            continue
        for p, h in (run.get('file_hashes') or {}).items():
            hashes[p], runs[p] = h, run['id']
    return hashes, runs

def lock(report: dict, repo: Path) -> list[str]:
    problems = []
    hashes, runs = last_red_hashes(report)
    paths = test_paths(report)
    locked = {}
    for p in paths:
        if not hashes.get(p):
            problems.append(f'{p}: no red-phase run recorded its hash (parse_results.py --phase red --hash {p})'); continue
        if sha256(repo / p) != hashes[p]:
            problems.append(f'{p}: changed since red run {runs[p]}; re-run the red phase before locking'); continue
        locked[p] = hashes[p]
        for m in weakening_markers(repo / p):
            problems.append(f"{p}:{m['line']}: {m['marker']} would hide a failing test; remove it")
    if problems:
        return problems
    integ = report.setdefault('integrity', {})
    integ['test_files'] = paths
    integ['test_hashes_at_red'] = locked
    integ['red_lock_run'] = max((runs[p] for p in paths), key=lambda r: [x['id'] for x in report['runs']].index(r)) if paths else None
    return []

def check(report: dict, repo: Path) -> list[str]:
    problems = []
    locked = (report.get('integrity') or {}).get('test_hashes_at_red') or {}
    if not locked:
        return ['no lock: run "hash_tests.py lock" after the red phase']
    for p, h in sorted(locked.items()):
        now = sha256(repo / p)
        if now is None:
            problems.append(f'{p}: locked test file is missing')
        elif now != h:
            problems.append(f'{p}: changed since it was locked at red')
        for m in weakening_markers(repo / p):
            problems.append(f"{p}:{m['line']}: {m['marker']} would hide a failing test")
    return problems

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Lock and check red test files.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('lock', 'check'):
        p = sub.add_parser(name)
        p.add_argument('report')
        p.add_argument('--repo', default='.')
    p = sub.add_parser('markers')
    p.add_argument('files', nargs='+')
    args = ap.parse_args(argv)
    if args.cmd == 'markers':
        n = 0
        for f in args.files:
            for m in weakening_markers(Path(f)):
                print(f"{f}:{m['line']}: {m['marker']}"); n += 1
        print(f'{n} marker(s)')
        return 1 if n else 0
    path = Path(args.report)
    report = json.loads(path.read_text(encoding='utf-8'))
    repo = Path(args.repo)
    problems = lock(report, repo) if args.cmd == 'lock' else check(report, repo)
    for pr in problems:
        print(f'ERROR: {pr}')
    if problems:
        return 1
    if args.cmd == 'lock':
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f"Locked {len(report['integrity']['test_files'])} test file(s) at run {report['integrity']['red_lock_run']}")
    else:
        print('Locked test files unchanged; no skip/xfail/only markers')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
