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

    python coverage_map.py per-file --repo <repo> --package packages/core --tests tests/unit --keep <dir>
        --out coverage_map.json [--runner vitest|jest] [--jobs 4] [--provider v8] [--extra-args "--project unit"]

`per-file` is for Vitest and Jest, which have no per-test coverage contexts: each test file runs
alone with the JSON coverage reporter (Istanbul's coverage-final.json; Vitest's V8 provider
writes the same shape), through the runner copy the package itself resolves. Its covered lines
belong to the whole file (test id `<file>::(whole file)`). Lines outside every function (module
level, run when the file is imported) are dropped and counted, as coverage.py's per-test
contexts drop them, so importing a module is not evidence that a test exercises its constants.
The coverage provider must already be installed (@vitest/coverage-v8); nothing is installed.
The manifest (`<keep>/per_file.json`) can be normalised again later with `normalise`. Test
files that failed or left no coverage are listed in the summary: their lines are missing.

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
    if data.get('format') == PER_FILE_FORMAT:
        return read_per_file(data)
    if data and all(isinstance(v, dict) and 'statementMap' in v for v in data.values()):
        raise SystemExit('this is one Istanbul/V8 coverage-final.json for a whole run: it cannot say which test ran a '
                         'line. Run each test file alone (coverage_map.py per-file) or give a per-file manifest.')
    raise SystemExit('unrecognised coverage format: expected coverage.py JSON with contexts, the mutation tester\'s '
                     'coverage.json, or a per-test-file Istanbul manifest (coverage_map.py per-file)')


PER_FILE_FORMAT = 'istanbul-per-test-file'
WHOLE_FILE = '(whole file)'


def istanbul_lines(cov: dict) -> tuple[dict[str, set[int]], int]:
    """{file key: covered lines run inside a function} and the count of covered lines run at module level.
    Istanbul (and Vitest's V8 provider, which writes the same shape) has no per-test contexts; a statement
    outside every function in fnMap ran when the module was imported, so it says nothing about which test
    exercises it (an importer runs a constants table too) and is left out, like coverage.py's per-test
    contexts leave out import-time lines."""
    out: dict[str, set[int]] = {}
    outside = 0
    for key, f in cov.items():
        if not isinstance(f, dict) or 'statementMap' not in f:
            continue
        fns = [(v['loc']['start']['line'], v['loc']['end']['line']) for v in (f.get('fnMap') or {}).values()
               if isinstance(v, dict) and 'loc' in v]
        lines = out.setdefault(f.get('path') or key, set())
        for sid, count in (f.get('s') or {}).items():
            loc = f['statementMap'].get(sid)
            if not count or not loc:
                continue
            ln = loc['start']['line']
            if any(a <= ln <= b for a, b in fns):
                lines.add(ln)
            else:
                outside += 1
    return out, outside


def read_per_file(data: dict) -> tuple[str, str | None, dict[str, dict[int, set[str]]], int]:
    """A per-test-file manifest: {"format": "istanbul-per-test-file", "base": dir, "runs": {test file:
    {"coverage": coverage-final.json relative to base, "exit_code": n}}}. Each test file ran alone, so its
    covered lines belong to the whole file (test id "<file>::(whole file)")."""
    base = Path(data.get('base') or '.')
    lines: dict[str, dict[int, set[str]]] = {}
    outside = 0
    for test, run in sorted(data['runs'].items()):
        if not run.get('coverage'):
            continue
        cov = json.loads((base / run['coverage']).read_text(encoding='utf-8'))
        per, out = istanbul_lines(cov)
        outside += out
        for key, lns in per.items():
            dst = lines.setdefault(key, {})
            for ln in lns:
                dst.setdefault(ln, set()).add(f'{test}::{WHOLE_FILE}')
    return PER_FILE_FORMAT, data.get('tool_version'), lines, outside


ENTRY = {'vitest': ('vitest', 'vitest.mjs'), 'jest': ('jest', 'bin/jest.js')}


def runner_entry(cwd: Path, runner: str) -> Path | None:
    """The runner's entry script as Node resolves it from the package (its own node_modules first): a
    workspace can hold two copies of a runner, and tests only see the one their package links."""
    name, script = ENTRY[runner]
    for d in (cwd, *cwd.parents):
        p = d / 'node_modules' / name / script
        if p.is_file():
            return p
    return None


def per_file(repo: Path, package: str, runner: str, tests: list[str], jobs: int, provider: str, extra: list[str],
             env_pairs: list[str], keep: Path) -> tuple[dict, str]:
    """Run each test file alone with JSON coverage and write the per-file manifest."""
    from concurrent.futures import ThreadPoolExecutor
    cwd = repo / package if package not in ('.', '') else repo
    entry = runner_entry(cwd, runner)
    if entry is None:
        raise SystemExit(f'{runner} is not installed where {package} resolves it (nothing is installed)')
    files: list[str] = []
    for t in tests:
        p = cwd / t
        if p.is_dir():
            files += sorted(q.relative_to(cwd).as_posix() for q in p.rglob('*') if q.is_file() and TEST_FILE.search(q.name)
                            and 'node_modules' not in q.parts)
        elif p.is_file():
            files.append(p.relative_to(cwd).as_posix())
        else:
            raise SystemExit(f'--tests {t}: not found under {package}')
    if not files:
        raise SystemExit('no test files found under --tests')
    keep.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **dict(kv.split('=', 1) for kv in env_pairs)}

    def one(item):
        i, t = item
        d = keep / f'c{i:04d}'
        if runner == 'vitest':
            cmd = ['node', str(entry), 'run', t, '--coverage.enabled=true', f'--coverage.provider={provider}',
                   '--coverage.reporter=json', f'--coverage.reportsDirectory={d}', '--coverage.clean=true', *extra]
        else:
            cmd = ['node', str(entry), '--ci', t, '--coverage', '--coverageReporters=json', f'--coverageDirectory={d}',
                   *extra]
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        f = d / 'coverage-final.json'
        return t, {'exit_code': res.returncode, 'coverage': f.relative_to(keep).as_posix() if f.is_file() else None}

    with ThreadPoolExecutor(max(1, jobs)) as ex:
        runs = dict(ex.map(one, enumerate(files)))
    version = None
    try:
        version = json.loads((entry.parent if runner == 'vitest' else entry.parent.parent).joinpath('package.json')
                             .read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        pass
    manifest = {'format': PER_FILE_FORMAT, 'tool_version': f'{runner} {version}' if version else runner,
                'base': str(keep), 'runs': runs}
    (keep / 'per_file.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    shown = (f'npx vitest run <test file> --coverage.enabled=true --coverage.provider={provider} --coverage.reporter=json'
             if runner == 'vitest' else 'npx jest --ci <test file> --coverage --coverageReporters=json')
    shown += (' ' + ' '.join(extra) if extra else '') + f' (one run per test file, {len(files)} files)'
    return manifest, shown


TEST_FILE = re.compile(r'\.(test|spec)\.[cm]?[jt]sx?$')


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
                        'unmatched_files': unmatched, **per_file_summary(data)}}


def per_file_summary(data: dict) -> dict:
    """For a per-test-file run: which test files failed or left no coverage (their lines are missing)."""
    if data.get('format') != PER_FILE_FORMAT:
        return {}
    runs = data['runs']
    return {'test_files_run': len(runs),
            'test_files_failed': sorted(t for t, r in runs.items() if r.get('exit_code')),
            'test_files_without_coverage': sorted(t for t, r in runs.items() if not r.get('coverage'))}


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
    pf = sub.add_parser('per-file', help='Vitest or Jest: run each test file alone with JSON coverage (Istanbul or V8)')
    pf.add_argument('--runner', choices=['vitest', 'jest'], default='vitest')
    pf.add_argument('--tests', nargs='+', required=True, help='test files or directories, relative to the package')
    pf.add_argument('--jobs', type=int, default=4)
    pf.add_argument('--provider', default='v8', help='Vitest coverage provider (v8 or istanbul; must be installed)')
    pf.add_argument('--extra-args', default='', help='extra runner arguments, e.g. "--project unit"')
    pf.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V')
    pf.add_argument('--keep', type=Path, required=True, help='directory for the per-file coverage files and manifest')
    g = sub.add_parser('generate', help='run pytest once with per-test contexts (needs coverage.py and pytest-cov)')
    g.add_argument('--python', default=sys.executable)
    g.add_argument('--source', default='.', help='what to measure, relative to the package (--cov=...); default everything')
    for p in (n, g, pf):
        p.add_argument('--repo', type=Path, required=True)
        p.add_argument('--package', default='.', help='package directory the tests ran in, relative to the repo')
        p.add_argument('--commit', help='commit the coverage was collected on, if known')
        p.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if args.cmd == 'generate':
        data, command = generate(repo, args.package, args.python, args.source)
        out = normalise(data, repo, args.package, 'collection', args.commit, command)
    elif args.cmd == 'per-file':
        import shlex
        data, command = per_file(repo, args.package, args.runner, args.tests, args.jobs, args.provider,
                                 shlex.split(args.extra_args), [kv for grp in args.env for kv in grp], args.keep.resolve())
        out = normalise(data, repo, args.package, 'collection', args.commit, command)
    else:
        data = json.loads(args.coverage.read_text(encoding='utf-8'))
        if data.get('format') == PER_FILE_FORMAT and not Path(data.get('base') or '.').is_absolute():
            data['base'] = str(args.coverage.resolve().parent / (data.get('base') or '.'))
        out = normalise(data, repo, args.package, 'normalisation', args.commit, data.get('command'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    s = out['summary']
    print(f'{s["tests"]} tests over {s["files"]} files ({s["lines_outside_tests"]} lines run outside tests'
          + (f', {len(s["unmatched_files"])} unmatched files' if s['unmatched_files'] else '') + f') -> {args.out}')
    if s.get('test_files_failed') or s.get('test_files_without_coverage'):
        print(f'  {len(s["test_files_failed"])} test file(s) failed and {len(s["test_files_without_coverage"])} left no '
              'coverage: their lines are missing from the map (see summary)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
