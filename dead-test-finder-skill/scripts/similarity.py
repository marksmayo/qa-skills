#!/usr/bin/env python3
"""Decide whether similar-looking tests are duplicates: same code shape, same lines run, same values (stdlib only).

    python similarity.py --repo <repo> --candidates candidates.json --run-dir run [--out similarity.json]

For the `duplicate` candidate pairs from scan_tests.py (Python tests), every test in any pair runs
once, in one pytest session per package (split only to keep the command line short), in a single
scratch copy of the package outside the repo, with a small sys.settrace pytest plugin that records
the product lines each test executes. Only product frames are traced line by line; test files and
installed libraries are skipped at the call, so tracing stays cheap. The cases of a parametrised
test count together as that test. The literal values in each test's assertions and calls are
compared too. A pair is a `duplicate` when the normalised AST similarity is at or above the
threshold, the executed lines are identical, and the values are identical; otherwise it is an
`overlap`. Pairs in other languages are `not_assessed` (no per-test coverage here).
similarity.json records each pytest session (`sessions`) and, per pair, the command that
reproduces it on its own and the exit code of the session that ran it.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, shutil, subprocess, sys, tempfile
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


_rel = {}


def _product(code):
    """The repo-relative path of a product file, or None for test files and anything outside the copy (cached)."""
    fn = code.co_filename
    if fn not in _rel:
        f = os.path.realpath(fn)
        rel = os.path.relpath(f, _root).replace(os.sep, "/") if f.startswith(_root) else None
        _rel[fn] = rel if rel and not _is_test(rel) else None
    return _rel[fn]


def _local(frame, event, arg):
    if event == "line" and _current[0]:
        _lines.setdefault(_current[0], set()).add(f"{_product(frame.f_code)}:{frame.f_lineno}")
    return _local


def _tracer(frame, event, arg):
    # Line events are traced only in product frames; library and test frames are not traced at all.
    return _local if _product(frame.f_code) else None


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


IGNORE = ('.git', '.hg', '.svn', '__pycache__', '*.pyc', '.pytest_cache', '.mypy_cache', '.tox', '.venv', 'venv', 'node_modules')


def ignore_for(run_dir: Path):
    """copytree ignore: VCS data, caches, virtualenvs, node_modules, and the run directory when it sits in the copy."""
    pat = shutil.ignore_patterns(*IGNORE)

    def ignore(dirpath: str, names: list[str]) -> set[str]:
        return set(pat(dirpath, names)) | {n for n in names if (Path(dirpath) / n).resolve() == run_dir}
    return ignore


def batches(nodes: list[str], limit: int = 20000) -> list[list[str]]:
    """Split node ids so each pytest command line stays well under the Windows 32k-character limit."""
    out, cur, size = [], [], 0
    for n in nodes:
        if cur and size + len(n) + 3 > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(n) + 3
    return out + ([cur] if cur else [])


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
    pairs, by_pkg = [], {}
    for c in data['candidates']:
        if c['category'] != 'duplicate':
            continue
        a, b = (defs[t] for t in c['test_ids'])
        row = {'candidate_id': c['id'], 'test_ids': c['test_ids'], 'similarity': c['details']['similarity'], 'threshold': threshold}
        if a['lang'] != 'python' or b['lang'] != 'python' or a['package'] != b['package']:
            pairs.append((c, a, b, {**row, 'verdict': 'not_assessed', 'reason': 'per-test coverage is only collected for Python tests'}))
            continue
        pairs.append((c, a, b, row))
        by_pkg.setdefault(a['package'], set()).update(c['test_ids'])
    # One scratch copy per package and one traced pytest session for every test in any pair (split only to keep the
    # command line short), instead of a copy and a session per pair.
    cov, sessions, session_of = {}, [], {}
    for pkg, ids in sorted(by_pkg.items()):
        prefix = pkg + '/' if pkg != '.' else ''
        src = root / pkg if pkg != '.' else root
        tmp = Path(tempfile.mkdtemp(prefix='dtf-sim-'))
        work = tmp / 'pkg'
        try:
            shutil.copytree(src, work, ignore=ignore_for(args.run_dir.resolve()))
            (work / 'dtf_lines.py').write_text(PLUGIN, encoding='utf-8')
            todo = batches(sorted(t[len(prefix):] for t in ids))
            while todo:
                batch = todo.pop(0)
                lines_file = work / '_lines.json'
                lines_file.unlink(missing_ok=True)
                cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'dtf_lines', *batch]
                res = subprocess.run(cmd, cwd=work, capture_output=True, text=True,
                                     env={**os.environ, 'DTF_LINES_OUT': str(lines_file), 'PYTHONDONTWRITEBYTECODE': '1'})
                if res.returncode in (3, 4) and len(batch) > 1:
                    # a usage or internal error (an id pytest can't find, a module skipped at import) stops the whole
                    # session: run those tests one at a time instead, so one bad id doesn't cost the others
                    todo = [[n] for n in batch] + todo
                    sessions.append({'package': pkg, 'tests': len(batch), 'exit_code': res.returncode,
                                     'command': f'python -m pytest -q -p no:cacheprovider -p dtf_lines <{len(batch)} test ids>',
                                     'note': 'stopped by a usage or internal error; its tests were rerun one at a time'})
                    continue
                got = json.loads(lines_file.read_text(encoding='utf-8')) if lines_file.exists() else {}
                for nodeid, lines in got.items():               # the cases of a parametrised test count as that test
                    cov.setdefault(prefix + re.sub(r'\[.*\]$', '', nodeid), set()).update(lines)
                sessions.append({'package': pkg, 'tests': len(batch), 'exit_code': res.returncode,
                                 'command': f'python -m pytest -q -p no:cacheprovider -p dtf_lines <{len(batch)} test ids>'})
                session_of.update({prefix + n: res.returncode for n in batch})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for c, a, b, row in pairs:
        if row.get('verdict') == 'not_assessed':
            out.append(row)
            continue
        prefix = a['package'] + '/' if a['package'] != '.' else ''
        nodes = [t[len(prefix):] for t in c['test_ids']]
        la, lb = (cov.get(t, set()) for t in c['test_ids'])
        va = values(root / a['file'], a['id'].split('::')[-1])
        vb = values(root / b['file'], b['id'].split('::')[-1])
        same_cov = bool(la) and la == lb
        same_vals = va == vb
        verdict = 'duplicate' if row['similarity'] >= threshold and same_cov and same_vals else 'overlap'
        out.append({**row, 'command': 'python -m pytest -q -p no:cacheprovider -p dtf_lines ' + ' '.join(nodes),
                    'exit_code': session_of.get(c['test_ids'][0]), 'lines_a': len(la), 'lines_b': len(lb), 'coverage_identical': same_cov,
                    'only_in_a': sorted(la - lb)[:10], 'only_in_b': sorted(lb - la)[:10],
                    'values_a': va, 'values_b': vb, 'values_identical': same_vals, 'verdict': verdict})
        print(f'{c["id"]}: {verdict} (similarity {row["similarity"]:.0%}, coverage {"identical" if same_cov else "differs"}, '
              f'values {"identical" if same_vals else "differ"})')
    h_after = tree_hash(root, args.run_dir)
    path = args.out or args.run_dir / 'similarity.json'
    path.write_text(json.dumps({'tree_hash_before': h_before, 'tree_hash_after': h_after, 'sessions': sessions, 'pairs': out},
                               indent=2) + '\n', encoding='utf-8')
    return 3 if h_before != h_after else 0


if __name__ == '__main__':
    sys.exit(main())
