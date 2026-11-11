#!/usr/bin/env python3
"""Collect the evidence that links requirements to code, commits, tests and results (stdlib + git).

    python collect_evidence.py --requirements requirements.json --repo <checkout> --junit results.xml [--test-management tm.json] --out evidence.json

Keys match exactly: a requirement id matches only where it isn't part of a longer key or word
(SHOP-401 never matches SHOP-4010). Evidence gathered, each with a reference that can be re-checked:
  tests          Python test functions (pytest ids module::name) and JS/TS it()/test() names, with every
                 line in the test (name, docstring, comments, decorators) naming a key: explicit links;
                 "<key> AC-n" or "<key>/AC-n" also names the criterion
  code           lines in non-test source files naming a key: implementation evidence
  commits        commits whose message names a key, the files they changed, and the test functions they
                 added: implementation evidence, and inferred links to those tests
  tickets        issue links (is tested by / tests) to test keys, comments naming a test id or test file,
                 attachment names (listed only)
  test_management export entries mapping a test key to requirement keys and an automated test id: explicit
  results        JUnit results per test id (shared parse_results.py), with the run's timestamp
"""
from __future__ import annotations
import argparse, ast, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402

SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.tox'}


def key_re(keys: list[str]) -> re.Pattern:
    return re.compile(r'(?<![A-Za-z0-9_-])(' + '|'.join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + r')(?![A-Za-z0-9_-]|-\d)'
                      + r'(?:\s*/?\s*(AC-\d+))?')


def git(repo: Path, *args) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    return r.stdout if r.returncode == 0 else ''


def is_test(rel: str) -> bool:
    n = Path(rel).name
    return bool(re.match(r'(test_.*\.py|.*_test\.py|.*\.(test|spec)\.[jt]sx?)$', n)) or '/tests/' in f'/{rel}'


def py_tests(rel: str, text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    mod = rel[:-3].replace('/', '.')
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out.append({'test_id': f'{mod}::{node.name}', 'name': node.name, 'file': rel, 'start': start, 'end': node.end_lineno})
    return out


def js_tests(rel: str, text: str) -> list[dict]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.search(r'\b(?:it|test)\s*\(\s*([\'"`])(.+?)\1', line)
        if m:
            out.append({'test_id': f'{rel}::{m.group(2)}', 'name': m.group(2), 'file': rel, 'start': n, 'end': n})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--requirements', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--junit', type=Path, action='append', default=[])
    ap.add_argument('--test-management', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    reqs = json.loads(a.requirements.read_text(encoding='utf-8'))['requirements']
    keys = [r['id'] for r in reqs]
    kre = key_re(keys)
    files = [p for p in sorted(a.repo.rglob('*')) if p.is_file() and not (set(p.relative_to(a.repo).parts) & SKIP_DIRS)
             and p.suffix in ('.py', '.js', '.jsx', '.ts', '.tsx', '.java', '.kt', '.go', '.rb', '.cs', '.feature')]
    tests, code = [], []
    for p in files:
        rel = p.relative_to(a.repo).as_posix()
        text = p.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        if is_test(rel):
            found = py_tests(rel, text) if p.suffix == '.py' else js_tests(rel, text)
            for t in found:
                refs = []
                for n in range(t['start'], t['end'] + 1):
                    for m in kre.finditer(lines[n - 1]):
                        refs.append({'key': m.group(1), 'criterion': f'{m.group(1)}/{m.group(2)}' if m.group(2) else None, 'file': rel, 'line': n,
                                     'text': lines[n - 1].strip()[:200]})
                t['refs'] = refs
                other = re.findall(r'(?<![A-Za-z0-9_-])([A-Z][A-Z0-9]+-\d+)(?![A-Za-z0-9_-])', '\n'.join(lines[t['start'] - 1:t['end']]))
                t['other_keys'] = sorted({k for k in other if k not in keys and not re.fullmatch(r'AC-\d+', k)})
                tests.append(t)
        else:
            for n, line in enumerate(lines, 1):
                for m in kre.finditer(line):
                    code.append({'key': m.group(1), 'file': rel, 'line': n, 'text': line.strip()[:200]})
    # commits --------------------------------------------------------------------------------------------
    commits = []
    head = git(a.repo, 'rev-parse', 'HEAD').strip() or None
    for line in git(a.repo, 'log', '--format=%H%x09%cI%x09%s').splitlines():
        sha, when, subject = line.split('\t', 2)
        ks = sorted({m.group(1) for m in kre.finditer(subject)})
        changed = [f for f in git(a.repo, 'show', '--format=', '--name-only', sha).splitlines() if f]
        added = []
        for f in changed:
            if f.endswith('.py') and is_test(f):
                now_t = {t['name'] for t in py_tests(f, git(a.repo, 'show', f'{sha}:{f}'))}
                before = {t['name'] for t in py_tests(f, git(a.repo, 'show', f'{sha}^:{f}'))} if git(a.repo, 'rev-parse', '--verify', '-q', f'{sha}^') else set()
                added += [f'{f[:-3].replace("/", ".")}::{n}' for n in sorted(now_t - before)]
        commits.append({'commit': sha, 'at': when, 'subject': subject, 'keys': ks, 'files': changed, 'tests_added': added})
    # tickets and test management ----------------------------------------------------------------------
    tm = json.loads(a.test_management.read_text(encoding='utf-8')) if a.test_management else {'tests': []}
    tm_by_key = {t['key']: t for t in tm.get('tests', [])}
    test_ids = {t['test_id'] for t in tests}
    test_files = {t['file'] for t in tests}
    tickets = []
    for r in reqs:
        ev = {'id': r['id'], 'links': [], 'comments': [], 'attachments': r.get('attachments', [])}
        for l in r.get('links', []):
            if (l.get('type') or '').lower() in ('is tested by', 'tested by', 'tests', 'is covered by', 'covers'):
                ev['links'].append({'type': l['type'], 'key': l['key'], 'summary': l.get('summary'), 'test_id': (tm_by_key.get(l['key']) or {}).get('test_id')})
        for i, c in enumerate(r.get('comments', [])):
            names = sorted({t for t in test_ids if t.split('::')[-1] in c['text']} | {f for f in test_files if f in c['text']})
            if names:
                ev['comments'].append({'index': i, 'author': c.get('author'), 'created': c.get('created'), 'mentions': names})
        tickets.append(ev)
    # results ---------------------------------------------------------------------------------------------
    results, runs = {}, []
    for j in a.junit:
        cases, notes = parse_junit(j, j.stem)
        ts = re.search(r'timestamp="([^"]+)"', j.read_text(encoding='utf-8', errors='replace')[:4000])
        runs.append({'path': j.as_posix(), 'timestamp': ts.group(1) if ts else None, 'cases': len(cases)})
        for c in cases:
            results[c['id']] = {'status': c['status'], 'run': j.as_posix(), 'at': ts.group(1) if ts else None, 'message': (c.get('message_excerpt') or '')[:160]}
    out = {'repo': {'head': head}, 'keys': keys, 'tests': tests, 'code': code, 'commits': commits, 'tickets': tickets,
           'test_management': {'path': a.test_management.as_posix() if a.test_management else None, 'source': tm.get('source'), 'tests': tm.get('tests', [])},
           'runs': runs, 'results': results}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(tests)} test(s) ({sum(1 for t in tests if t["refs"])} naming a key), {len(code)} code line(s), {sum(1 for c in commits if c["keys"])} commit(s) naming a key, '
          f'{sum(len(t["links"]) for t in tickets)} ticket link(s), {len(results)} result(s) -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
