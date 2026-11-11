#!/usr/bin/env python3
"""Turn per-test coverage into a map of source line -> tests (stdlib only).

    python coverage_map.py normalise coverage.json --repo <repo> --package api --out coverage_map.json [--commit SHA]
    python coverage_map.py generate --repo <repo> --package api --out coverage_map.json [--python python] [--source pkg]

`normalise` reads coverage the repo already produces:
  - coverage.py JSON with contexts: `pytest --cov=<pkg> --cov-context=test` then
    `coverage json --show-contexts` (contexts like `tests/test_x.py::test_y|run`);
  - the mutation tester's `coverage.json` (per-test lines from its sys.settrace plugin).
`generate` runs one instrumented full pytest run in the package (it needs coverage.py and
pytest-cov in that interpreter; nothing is installed) and normalises the result.

Paths are matched to repository files by their longest common suffix, so coverage collected
in another checkout or a container still maps. Test ids become repo-relative
(`api/tests/test_x.py::test_y`). Lines run outside any test (at import) are dropped and
counted. Every measured file's SHA-256 (line endings normalised) is recorded with when it was
taken: at collection for `generate`, at normalisation otherwise (normalise straight after
collecting, or pass --commit). select_tests.py compares these hashes with the base tree and
labels the map stale where they differ.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests  # noqa: E402

CTX_SUFFIX = re.compile(r'\|(run|setup|teardown)$')


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def repo_files(repo: Path, package: str) -> list[str]:
    base = repo if package in ('.', '') else repo / package
    return sorted(p.relative_to(repo).as_posix() for p in run_tests.iter_files(base))


def match_path(key: str, candidates: list[str]) -> str | None:
    """The repo file sharing the longest path suffix with a coverage key (at least the file name)."""
    parts = key.replace('\\', '/').split('/')
    best, best_n = None, 0
    for c in candidates:
        cp = c.split('/')
        n = 0
        while n < min(len(cp), len(parts)) and cp[-1 - n] == parts[-1 - n]:
            n += 1
        if n > best_n or (n == best_n and best is not None and len(c) < len(best)):
            best, best_n = c, n
    return best if best_n >= 1 else None


def read_coverage(data: dict) -> tuple[str, str | None, dict[str, dict[int, set[str]]], int]:
    """(format, tool version, {file key: {line: {raw test id}}}, lines run outside tests)."""
    lines: dict[str, dict[int, set[str]]] = {}
    outside = 0
    if 'meta' in data and 'files' in data and isinstance(data['files'], dict):
        if not data['meta'].get('show_contexts'):
            raise SystemExit('coverage.py JSON has no contexts: collect with --cov-context=test (or dynamic_context = '
                             'test_function) and export with `coverage json --show-contexts`.')
        for key, f in data['files'].items():
            per = lines.setdefault(key, {})
            for ln, ctxs in (f.get('contexts') or {}).items():
                tests = {CTX_SUFFIX.sub('', c) for c in ctxs if c}
                if not tests:
                    outside += 1
                    continue
                per.setdefault(int(ln), set()).update(tests)
        return 'coverage.py-json', data['meta'].get('version'), lines, outside
    if 'lines' in data and 'method' in data:
        raw = data['lines']
        for key, per_line in raw.items():
            per = lines.setdefault(key, {})
            for ln, tests in per_line.items():
                tests = {t for t in tests if t != '<import>'}
                if not tests:
                    outside += 1
                    continue
                per.setdefault(int(ln), set()).update(tests)
        return 'mutation-tester-coverage', None, lines, outside
    raise SystemExit('unrecognised coverage format: expected coverage.py JSON with contexts or the mutation tester\'s coverage.json')


def normalise(data: dict, repo: Path, package: str, hashed_at: str, commit: str | None, command: str | None) -> dict:
    fmt, version, raw, outside = read_coverage(data)
    cands = repo_files(repo, package)
    prefix = '' if package in ('.', '') else package.rstrip('/') + '/'
    tests: dict[str, int] = {}
    files: dict[str, dict[str, list[int]]] = {}
    unmatched = []
    for key, per in sorted(raw.items()):
        path = match_path(key, cands)
        if not path:
            unmatched.append(key)
            continue
        out = files.setdefault(path, {})
        for ln, ids in sorted(per.items()):
            idx = []
            for t in sorted(ids):
                tid = prefix + t.replace('\\', '/')
                idx.append(tests.setdefault(tid, len(tests)))
            out[str(ln)] = sorted(set(out.get(str(ln), []) + idx))
    hashes = {p: file_sha(repo / p) for p in sorted(files) if (repo / p).is_file()}
    return {'format': fmt, 'tool_version': version, 'package': package or '.', 'command': command,
            'collected': {'hashed_at': hashed_at, 'commit': commit,
                          'normalised_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                          'file_hashes': hashes},
            'tests': list(tests), 'files': files,
            'summary': {'tests': len(tests), 'files': len(files), 'lines_outside_tests': outside,
                        'unmatched_files': unmatched}}


def generate(repo: Path, package: str, python: str, source: str) -> tuple[dict, str]:
    cwd = repo / package if package not in ('.', '') else repo
    probe = subprocess.run([python, '-c', 'import coverage, pytest_cov; print(coverage.__version__)'], capture_output=True, text=True)
    if probe.returncode:
        why = probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else 'import failed'
        raise SystemExit(f'generate needs coverage.py and pytest-cov in that interpreter (nothing is installed): {why}')
    with tempfile.TemporaryDirectory(prefix='impact-cov-') as tmp:
        data_file = Path(tmp) / '.coverage'
        out_json = Path(tmp) / 'coverage.json'
        env = {**os.environ, 'COVERAGE_FILE': str(data_file), 'PYTHONDONTWRITEBYTECODE': '1'}
        cmd = [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', f'--cov={source}', '--cov-context=test', '--cov-report=']
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        if res.returncode not in (0, 1):
            raise SystemExit(f'pytest exited {res.returncode}: {res.stdout[-400:]}')
        subprocess.run([python, '-m', 'coverage', 'json', '--show-contexts', '-o', str(out_json)], cwd=cwd, env=env,
                       check=True, capture_output=True)
        data = json.loads(out_json.read_text(encoding='utf-8'))
    shown = ' '.join(['python' if c == python else c for c in cmd]) + ' && python -m coverage json --show-contexts'
    note = '' if res.returncode == 0 else ' (some tests failed; their coverage is still recorded)'
    return data, shown + note


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    n = sub.add_parser('normalise', help='read coverage the repo already produced')
    n.add_argument('coverage', type=Path)
    g = sub.add_parser('generate', help='run pytest once with per-test contexts (needs coverage.py and pytest-cov)')
    g.add_argument('--python', default=sys.executable)
    g.add_argument('--source', default='.', help='what to measure, relative to the package (--cov=...); default everything')
    for p in (n, g):
        p.add_argument('--repo', type=Path, required=True)
        p.add_argument('--package', default='.', help='package directory the tests ran in, relative to the repo')
        p.add_argument('--commit', help='commit the coverage was collected on, if known')
        p.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if args.cmd == 'generate':
        data, command = generate(repo, args.package, args.python, args.source)
        out = normalise(data, repo, args.package, 'collection', args.commit, command)
    else:
        data = json.loads(args.coverage.read_text(encoding='utf-8'))
        out = normalise(data, repo, args.package, 'normalisation', args.commit, data.get('command'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    s = out['summary']
    print(f'{s["tests"]} tests over {s["files"]} files ({s["lines_outside_tests"]} lines run outside tests'
          + (f', {len(s["unmatched_files"])} unmatched files' if s['unmatched_files'] else '') + f') -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
