#!/usr/bin/env python3
"""Decide whether similar-looking tests are duplicates: same code shape, same lines run, same values (stdlib only).

    python similarity.py --repo <repo> --candidates candidates.json --run-dir run [--out similarity.json]

For each `duplicate` candidate pair from scan_tests.py (Python tests), both tests run once in a
scratch copy of their package (in a temporary directory outside the repo) with a small sys.settrace pytest plugin that records the product
lines each test executes (test files excluded). The literal values in each test's assertions and
calls are compared too. A pair is a `duplicate` when the normalised AST similarity is at or above
the threshold, the executed lines are identical, and the values are identical; otherwise it is an
`overlap`. Pairs in other languages are `not_assessed` (no per-test coverage here).
"""
from __future__ import annotations
import argparse, ast, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests, scan_tests  # noqa: E402

PLUGIN = r'''
import json, os, sys
import pytest

_root = os.path.realpath(os.getcwd())
_lines = {}
_current = [None]


def _is_test(rel):
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "/tests/" in "/" + rel


def _tracer(frame, event, arg):
    if event == "line" and _current[0]:
        f = os.path.realpath(frame.f_code.co_filename)
        if f.startswith(_root):
            rel = os.path.relpath(f, _root).replace(os.sep, "/")
            if not _is_test(rel):
                _lines.setdefault(_current[0], set()).add(f"{rel}:{frame.f_lineno}")
    return _tracer


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _current[0] = item.nodeid
    sys.settrace(_tracer)
    try:
        yield
    finally:
        sys.settrace(None)
        _current[0] = None


def pytest_sessionfinish(session):
    with open(os.environ["DTF_LINES_OUT"], "w", encoding="utf-8") as fh:
        json.dump({k: sorted(v) for k, v in _lines.items()}, fh)
'''


def tree_hash(root: Path, skip: Path) -> str:
    """run_tests.code_hash over the repo, leaving out the run directory when it sits inside it."""
    h = hashlib.sha256()
    skip = skip.resolve()
    for p in sorted(run_tests.iter_files(root, {skip}), key=lambda p: p.relative_to(root).as_posix()):
        h.update(p.relative_to(root).as_posix().encode('utf-8') + b'\0' + p.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return h.hexdigest()


def values(path: Path, name: str) -> list:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
            return sorted((repr(n.value) for n in ast.walk(fn) if isinstance(n, ast.Constant) and not isinstance(n.value, str)
                           or isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != ''), key=str)
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    defs = {d['id']: d for d in data['definitions']}
    threshold = scan_tests.RULES['similarity_threshold']
    out = []
    args.run_dir.mkdir(parents=True, exist_ok=True)
    h_before = tree_hash(root, args.run_dir)
    for c in data['candidates']:
        if c['category'] != 'duplicate':
            continue
        a, b = (defs[t] for t in c['test_ids'])
        row = {'candidate_id': c['id'], 'test_ids': c['test_ids'], 'similarity': c['details']['similarity'], 'threshold': threshold}
        if a['lang'] != 'python' or b['lang'] != 'python' or a['package'] != b['package']:
            out.append({**row, 'verdict': 'not_assessed', 'reason': 'per-test coverage is only collected for Python tests'})
            continue
        pkg = a['package']
        tmp = Path(tempfile.mkdtemp(prefix='dtf-sim-'))
        work = tmp / 'pkg'
        shutil.copytree(root / pkg if pkg != '.' else root, work,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache', 'node_modules'))
        (work / 'dtf_lines.py').write_text(PLUGIN, encoding='utf-8')
        lines_file = work / '_lines.json'
        prefix = pkg + '/' if pkg != '.' else ''
        nodes = [t[len(prefix):] for t in c['test_ids']]
        cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'dtf_lines', *nodes]
        res = subprocess.run(cmd, cwd=work, capture_output=True, text=True,
                             env={**os.environ, 'DTF_LINES_OUT': str(lines_file), 'PYTHONDONTWRITEBYTECODE': '1'})
        cov = json.loads(lines_file.read_text(encoding='utf-8')) if lines_file.exists() else {}
        la, lb = (set(cov.get(n, [])) for n in nodes)
        va = values(root / a['file'], a['id'].split('::')[-1])
        vb = values(root / b['file'], b['id'].split('::')[-1])
        shutil.rmtree(tmp, ignore_errors=True)
        same_cov = bool(la) and la == lb
        same_vals = va == vb
        verdict = 'duplicate' if row['similarity'] >= threshold and same_cov and same_vals else 'overlap'
        out.append({**row, 'command': 'python -m pytest -q -p no:cacheprovider -p dtf_lines ' + ' '.join(nodes),
                    'exit_code': res.returncode, 'lines_a': len(la), 'lines_b': len(lb), 'coverage_identical': same_cov,
                    'only_in_a': sorted(la - lb)[:10], 'only_in_b': sorted(lb - la)[:10],
                    'values_a': va, 'values_b': vb, 'values_identical': same_vals, 'verdict': verdict})
        print(f'{c["id"]}: {verdict} (similarity {row["similarity"]:.0%}, coverage {"identical" if same_cov else "differs"}, '
              f'values {"identical" if same_vals else "differ"})')
    h_after = tree_hash(root, args.run_dir)
    path = args.out or args.run_dir / 'similarity.json'
    path.write_text(json.dumps({'tree_hash_before': h_before, 'tree_hash_after': h_after, 'pairs': out}, indent=2) + '\n',
                    encoding='utf-8')
    return 3 if h_before != h_after else 0


if __name__ == '__main__':
    sys.exit(main())
