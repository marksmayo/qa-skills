#!/usr/bin/env python3
"""Decide whether similar-looking tests are duplicates: same code shape, same lines run, same values (stdlib only).

    python similarity.py --repo <repo> --candidates candidates.json --run-dir run [--out similarity.json]
        [--cwd PACKAGE=DIR] [--env K=V ...] [--package-env PACKAGE:K=V ...]

For the `duplicate` candidate pairs from scan_tests.py (Python tests), every test in any pair runs
once, in one pytest session per package (split only to keep the command line short), in a single
scratch copy of the package outside the repo, with a small sys.settrace pytest plugin that records
the product lines each test executes and its outcome. Only product frames are traced line by line;
test files and installed libraries are skipped at the call, so tracing stays cheap. The cases of a
parametrised test count together as that test. The literal values in each test's assertions and
calls are compared too, by hash: similarity.json never holds a test's literal values (they can be
personal data or credentials), only a short SHA-256 of each.

A pair is a `duplicate` when the normalised AST similarity is at or above the threshold, both tests
passed in the traced session, the executed lines are identical and not empty, and the values are
identical; an `overlap` when both passed and traced lines but the lines or values differ; and
`not_assessed` otherwise, with the reason: a test that failed, errored, was skipped or never ran
(the session's exit code is kept), a test that ran no product line in the copy, a pair across two
packages, or a pair outside Python (no per-test coverage here).

The scratch copy keeps the package's place under the pytest configuration files and conftest.py
files of the directories above it (an ini file in `services/` for `services/api`), so pytest finds
the same rootdir and options as in the repository; ids are mapped through each test's file, never
through pytest's rootdir-relative node ids. --cwd PACKAGE=DIR copies another directory instead of
the package (a prepared copy), --env K=V [K=V ...] sets variables for every session and
--package-env PACKAGE:K=V for one package's sessions (each service its own PYTHONPATH); only the
names are recorded. similarity.json records each pytest session (`sessions`) and, per pair, the
command that reproduces it on its own and the exit code of the session that ran it.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collect_check, run_tests, scan_tests  # noqa: E402

PLUGIN = r'''
import json, os, sys
import pytest

_root = os.path.realpath(os.getcwd())
_lines = {}
_outcomes = {}
_keys = {}
_current = [None]


def _key(item):
    """file relative to the working directory :: the rest of the node id (whatever pytest's rootdir is)."""
    path = str(getattr(item, "path", None) or item.fspath)
    rel = os.path.relpath(os.path.realpath(path), _root).replace(os.sep, "/")
    return rel + "::" + item.nodeid.split("::", 1)[1] if "::" in item.nodeid else rel


def _is_test(rel):
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name in ("conftest.py", "dtf_lines.py") or "/tests/" in "/" + rel


_rel = {}


def _product(code):
    """The repo-relative path of a product file, or None for test files and anything outside the copy (cached)."""
    fn = code.co_filename
    if fn not in _rel:
        # "<frozen os>", "<string>": not files, although realpath would put them under the working directory
        f = os.path.realpath(fn) if not fn.startswith("<") else ""
        rel = os.path.relpath(f, _root).replace(os.sep, "/") if f and f.startswith(_root + os.sep) else None
        _rel[fn] = rel if rel and not _is_test(rel) else None
    return _rel[fn]


def _local(frame, event, arg):
    if event == "line" and _current[0]:
        _lines.setdefault(_current[0], set()).add(f"{_product(frame.f_code)}:{frame.f_lineno}")
    return _local


def _tracer(frame, event, arg):
    # Line events are traced only in product frames; library and test frames are not traced at all.
    return _local if _product(frame.f_code) else None


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        _keys[item.nodeid] = _key(item)


def pytest_runtest_logreport(report):
    key = _keys.get(report.nodeid, report.nodeid)
    if report.failed:
        _outcomes[key] = "error" if report.when != "call" else "failed"
    elif report.skipped and _outcomes.get(key) not in ("failed", "error"):
        _outcomes[key] = "skipped"
    elif report.when == "call" and report.passed and key not in _outcomes:
        _outcomes[key] = "passed"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _current[0] = _keys.get(item.nodeid) or _key(item)
    sys.settrace(_tracer)
    try:
        yield
    finally:
        sys.settrace(None)
        _current[0] = None


def pytest_sessionfinish(session):
    with open(os.environ["DTF_LINES_OUT"], "w", encoding="utf-8") as fh:
        json.dump({"lines": {k: sorted(v) for k, v in _lines.items()}, "outcomes": _outcomes}, fh)
'''


def tree_hash(root: Path, skip: Path) -> str:
    """run_tests.code_hash over the repo, leaving out the run directory when it sits inside it."""
    h = hashlib.sha256()
    skip = skip.resolve()
    for p in sorted(run_tests.iter_files(root, {skip}), key=lambda p: p.relative_to(root).as_posix()):
        h.update(p.relative_to(root).as_posix().encode('utf-8') + b'\0' + p.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return h.hexdigest()


def values(path: Path, name: str) -> list:
    """The test's literal values, each as a short SHA-256 (never the value: a literal can be an email address or a
    credential), sorted."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
            lits = [repr(n.value) for n in ast.walk(fn) if isinstance(n, ast.Constant) and not isinstance(n.value, str)
                    or isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != '']
            return sorted('sha256:' + hashlib.sha256(v.encode('utf-8')).hexdigest()[:12] for v in lits)
    return []


CONFIG_FILES = ('pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini', 'conftest.py')


def ancestors_config(root: Path, pkg: str) -> list[str]:
    """Repo-relative pytest configuration and conftest.py files in the directories above the package (the repo root
    included), which pytest reads when it runs the package's tests in the repository."""
    if pkg == '.':
        return []
    out = []
    parts = PurePosixPath(pkg).parts
    for i in range(len(parts)):
        d = root.joinpath(*parts[:i])
        for name in CONFIG_FILES:
            if (d / name).is_file():
                out.append((PurePosixPath(*parts[:i]) / name).as_posix())
    return out


def test_status(outcomes: dict, test_id: str, prefix: str) -> str:
    """One status for a test from the traced session: its own outcome, or every parametrised case's (failed or error
    if any case did, skipped if any was, passed when all passed); not_run when the session never reported it."""
    tid = test_id[len(prefix):]
    if tid in outcomes:
        return outcomes[tid]
    cases = [v for k, v in outcomes.items() if re.sub(r'\[.*\]$', '', k) == tid]
    for st in ('error', 'failed', 'skipped'):
        if st in cases:
            return st
    return 'passed' if cases else 'not_run'


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
    ap.add_argument('--cwd', action='append', default=[], metavar='PACKAGE=DIR',
                    help="copy DIR instead of the package (a prepared copy of it)")
    ap.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V',
                    help='environment for every traced session (several pairs per flag, or repeat the flag)')
    ap.add_argument('--package-env', action='append', default=[], metavar='PACKAGE:K=V',
                    help="environment for one package's sessions only (repeat)")
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    defs = {d['id']: d for d in data['definitions']}
    threshold = scan_tests.RULES['similarity_threshold']
    env, pkg_env = collect_check.parse_env(args.env, args.package_env)
    overrides = dict(x.split('=', 1) for x in args.cwd)
    out = []
    args.run_dir.mkdir(parents=True, exist_ok=True)
    h_before = tree_hash(root, args.run_dir)
    pairs, by_pkg = [], {}
    for c in data['candidates']:
        if c['category'] != 'duplicate':
            continue
        a, b = (defs[t] for t in c['test_ids'])
        row = {'candidate_id': c['id'], 'test_ids': c['test_ids'], 'similarity': c['details']['similarity'], 'threshold': threshold}
        if a['lang'] != 'python' or b['lang'] != 'python':
            pairs.append((c, a, b, {**row, 'verdict': 'not_assessed', 'reason': 'per-test coverage is only collected for Python tests'}))
            continue
        if a['package'] != b['package']:
            pairs.append((c, a, b, {**row, 'verdict': 'not_assessed',
                                    'reason': f'the tests are in different packages ({a["package"]}, {b["package"]}); each '
                                              'checks its own copy of the code, and their coverage is not comparable'}))
            continue
        pairs.append((c, a, b, row))
        by_pkg.setdefault(a['package'], set()).update(c['test_ids'])
    # One scratch copy per package and one traced pytest session for every test in any pair (split only to keep the
    # command line short), instead of a copy and a session per pair.
    cov, outcomes, sessions, session_of = {}, {}, [], {}
    for pkg, ids in sorted(by_pkg.items()):
        prefix = pkg + '/' if pkg != '.' else ''
        src = Path(overrides[pkg]).resolve() if pkg in overrides else (root / pkg if pkg != '.' else root)
        tmp = Path(tempfile.mkdtemp(prefix='dtf-sim-'))
        work = tmp / 'r' / pkg if pkg != '.' else tmp / 'r'
        above = ancestors_config(root, pkg)
        penv = {**env, **pkg_env.get(pkg, {})}
        try:
            shutil.copytree(src, work, ignore=ignore_for(args.run_dir.resolve()))
            for rel in above:                                # the ini and conftest files pytest reads above the package
                (tmp / 'r' / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, tmp / 'r' / rel)
            (work / 'dtf_lines.py').write_text(PLUGIN, encoding='utf-8')
            todo = batches(sorted(t[len(prefix):] for t in ids))
            while todo:
                batch = todo.pop(0)
                lines_file = work / '_lines.json'
                lines_file.unlink(missing_ok=True)
                cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'dtf_lines', *batch]
                res = subprocess.run(cmd, cwd=work, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                     env={**os.environ, **penv, 'DTF_LINES_OUT': str(lines_file), 'PYTHONDONTWRITEBYTECODE': '1'})
                session = {'package': pkg, 'tests': len(batch), 'exit_code': res.returncode,
                           'command': f'python -m pytest -q -p no:cacheprovider -p dtf_lines <{len(batch)} test ids>',
                           **({'env': sorted(penv)} if penv else {}), **({'config_above': above} if above else {})}
                if res.returncode in (3, 4) and len(batch) > 1:
                    # a usage or internal error (an id pytest can't find, a module skipped at import) stops the whole
                    # session: run those tests one at a time instead, so one bad id doesn't cost the others
                    todo = [[n] for n in batch] + todo
                    sessions.append({**session, 'note': 'stopped by a usage or internal error; its tests were rerun one at a time'})
                    continue
                got = json.loads(lines_file.read_text(encoding='utf-8')) if lines_file.exists() else {}
                for key, lines in got.get('lines', {}).items():  # the cases of a parametrised test count as that test
                    cov.setdefault(prefix + re.sub(r'\[.*\]$', '', key), set()).update(lines)
                for key, st in got.get('outcomes', {}).items():
                    outcomes[prefix + key] = st
                if res.returncode not in (0, 1, 5):
                    tail = run_tests.redact_home(res.stdout + res.stderr).strip().splitlines()[-3:]
                    session['error'] = ' | '.join(tail)[-400:]
                sessions.append(session)
                session_of.update({prefix + n: res.returncode for n in batch})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for c, a, b, row in pairs:
        if row.get('verdict') == 'not_assessed':
            out.append(row)
            print(f'{c["id"]}: not assessed ({row["reason"]})')
            continue
        prefix = a['package'] + '/' if a['package'] != '.' else ''
        nodes = [t[len(prefix):] for t in c['test_ids']]
        la, lb = (cov.get(t, set()) for t in c['test_ids'])
        sa, sb = (test_status({k[len(prefix):]: v for k, v in outcomes.items() if k.startswith(prefix)}, t, prefix)
                  for t in c['test_ids'])
        va = values(root / a['file'], a['id'].split('::')[-1])
        vb = values(root / b['file'], b['id'].split('::')[-1])
        code = session_of.get(c['test_ids'][0])
        rec = {**row, 'command': 'python -m pytest -q -p no:cacheprovider -p dtf_lines ' + ' '.join(nodes),
               'exit_code': code, 'status_a': sa, 'status_b': sb, 'lines_a': len(la), 'lines_b': len(lb)}
        if sa != 'passed' or sb != 'passed':
            # a test that failed, errored, was skipped or never ran traced only part of its lines, or none: its coverage
            # says nothing about the other test
            rec.update(verdict='not_assessed', reason=f'the traced session did not pass both tests (a: {sa}, b: {sb}; exit '
                                                      f'{code})')
        elif not la or not lb:
            rec.update(verdict='not_assessed', reason='no product line of the package copy was traced for '
                       + ('either test' if not la and not lb else 'one test')
                       + ' (the code under test is outside the package, or the test runs none)')
        else:
            same_cov = la == lb
            same_vals = va == vb
            rec.update(coverage_identical=same_cov, only_in_a=sorted(la - lb)[:10], only_in_b=sorted(lb - la)[:10],
                       values_a=va, values_b=vb, values_identical=same_vals,
                       verdict='duplicate' if row['similarity'] >= threshold and same_cov and same_vals else 'overlap')
        out.append(rec)
        print(f'{c["id"]}: {rec["verdict"]} (similarity {row["similarity"]:.0%}'
              + (f', coverage {"identical" if rec["coverage_identical"] else "differs"}, values '
                 f'{"identical" if rec["values_identical"] else "differ"})' if 'coverage_identical' in rec else f'; {rec["reason"]})'))
    h_after = tree_hash(root, args.run_dir)
    path = args.out or args.run_dir / 'similarity.json'
    path.write_text(json.dumps({'tree_hash_before': h_before, 'tree_hash_after': h_after, 'sessions': sessions, 'pairs': out},
                               indent=2) + '\n', encoding='utf-8')
    return 3 if h_before != h_after else 0


if __name__ == '__main__':
    sys.exit(main())
