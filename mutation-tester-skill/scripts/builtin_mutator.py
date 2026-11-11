#!/usr/bin/env python3
"""Stdlib mutation engine for Python repos tested with pytest (the fallback when no tool is set up).

    python builtin_mutator.py --repo <repo> --scope scope.json --run-dir <dir> [--minutes 30]
        [--timeout-factor 3] [--min-timeout 10] [--max-mutants N] [--coverage-only] [--restore]

1. Coverage: runs the suite once with a small pytest plugin (written to the run directory and
   loaded with -p) that records, per test, the lines it executed in the scoped files. Lines run
   only while importing a module are recorded under "<import>". Written to <dir>/coverage.json.
2. Mutants: AST mutations on the changed lines of the scoped Python source files (see
   data/operators.json, engine "builtin"). Each mutant replaces one node's source text.
3. Runs: for each mutant, writes the mutated file, runs only the tests that executed that line
   (every test that touched the file, for a module-level line), and restores the original bytes.
   pytest exit 0 = survived, 1 = killed (the first failing test is recorded), a timeout =
   timeout, any other exit or no parsed results = runtime_error, a mutant that doesn't compile =
   compile_error. A mutant on a line no test ran is no_coverage and isn't run. The per-mutant
   timeout is --timeout-factor times one untraced run of the suite, at least --min-timeout.
4. Budget: when the estimate exceeds --minutes, mutants are sampled evenly across operators and
   files; the skipped ones are recorded.

The working tree is hashed before and after (caches skipped). Originals are backed up in
<dir>/backup before each write; --restore puts them back after a crash. Bytecode is redirected
to an empty cache so a stale .pyc can never stand in for a mutant. Output: <dir>/builtin.json.
"""
from __future__ import annotations
import argparse, ast, copy, hashlib, json, os, random, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_results  # noqa: E402
import run_tests  # noqa: E402

VERSION = '1.0.0'

PLUGIN = r'''
import json, os, sys, threading
_FILES = {os.path.normcase(os.path.abspath(p)) for p in json.loads(os.environ["MT_FILES"])}
_OUT = os.environ["MT_COV_OUT"]
_hits = {}
_current = ["<import>"]

def _tracer(frame, event, arg):
    f = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
    if f not in _FILES:
        return None
    def local(frame, event, arg):
        if event == "line":
            _hits.setdefault(_current[0], {}).setdefault(f, set()).add(frame.f_lineno)
        return local
    if event == "call":
        _hits.setdefault(_current[0], {}).setdefault(f, set()).add(frame.f_lineno)
    return local

sys.settrace(_tracer)
threading.settrace(_tracer)

import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = "<import>"

def pytest_sessionfinish(session, exitstatus):
    sys.settrace(None)
    data = {t: {f: sorted(l) for f, l in files.items()} for t, files in _hits.items()}
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump({"exitstatus": int(exitstatus), "hits": data}, fh)
'''

CMP_SWAP = {ast.Lt: [(ast.LtE, 'comparison_boundary'), (ast.NotEq, 'comparison_relational')],
            ast.LtE: [(ast.Lt, 'comparison_boundary')],
            ast.Gt: [(ast.GtE, 'comparison_boundary'), (ast.NotEq, 'comparison_relational')],
            ast.GtE: [(ast.Gt, 'comparison_boundary')],
            ast.Eq: [(ast.NotEq, 'comparison_negate')], ast.NotEq: [(ast.Eq, 'comparison_negate')],
            ast.Is: [(ast.IsNot, 'comparison_negate')], ast.IsNot: [(ast.Is, 'comparison_negate')],
            ast.In: [(ast.NotIn, 'comparison_negate')], ast.NotIn: [(ast.In, 'comparison_negate')]}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.FloorDiv: ast.Div,
            ast.Mod: ast.Mult, ast.Pow: ast.Mult}
SKIP_CALLS = {'print'}


def rel(p: Path, root: Path) -> str:
    return p.resolve().relative_to(root).as_posix()


def docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def annotation_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns = [a.annotation for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs] + [node.returns]
            anns += [node.args.vararg.annotation if node.args.vararg else None,
                     node.args.kwarg.annotation if node.args.kwarg else None]
        elif isinstance(node, ast.AnnAssign):
            anns = [node.annotation]
        for a in anns:
            if a is not None:
                out.update(id(n) for n in ast.walk(a))
    return out


def fstring_parts(tree: ast.AST) -> set[int]:
    """Literal parts and format specs of f-strings; before 3.12 their positions are unreliable, so skip all of it."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            if sys.version_info < (3, 12):
                out.update(id(v) for v in ast.walk(n) if v is not n)
                continue
            for v in n.values:
                if isinstance(v, ast.Constant):
                    out.add(id(v))
                elif isinstance(v, ast.FormattedValue) and v.format_spec is not None:
                    out.update(id(x) for x in ast.walk(v.format_spec))
    return out


def candidates(tree: ast.AST):
    """Yield (node, replacement source text builder, operator) for every mutation the engine knows."""
    skip = docstring_nodes(tree) | annotation_nodes(tree) | fstring_parts(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            for new_op, op_name in CMP_SWAP.get(type(node.ops[0]), []):
                m = copy.deepcopy(node)
                m.ops = [new_op()]
                yield node, '(' + ast.unparse(m) + ')', op_name
        elif isinstance(node, ast.BinOp) and type(node.op) in BIN_SWAP:
            m = copy.deepcopy(node)
            m.op = BIN_SWAP[type(node.op)]()
            yield node, '(' + ast.unparse(m) + ')', 'arithmetic'
        elif isinstance(node, ast.AugAssign) and type(node.op) in BIN_SWAP:
            m = copy.deepcopy(node)
            m.op = BIN_SWAP[type(node.op)]()
            yield node, ast.unparse(m), 'arithmetic'
        elif isinstance(node, ast.BoolOp):
            m = copy.deepcopy(node)
            m.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            yield node, '(' + ast.unparse(m) + ')', 'logical'
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield node, '(' + ast.unparse(node.operand) + ')', 'negate_condition'
        elif isinstance(node, (ast.If, ast.While)) and not isinstance(node.test, ast.UnaryOp):
            yield node.test, 'not (' + ast.unparse(node.test) + ')', 'negate_condition'
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                yield node, repr(not v), 'boolean_constant'
            elif isinstance(v, int):
                yield node, repr(1 if v == 0 else (0 if v == 1 else v + 1)), 'constant_number'
            elif isinstance(v, float):
                yield node, repr(v + 1.0), 'constant_number'
            elif isinstance(v, str):
                yield node, repr('X' if v == '' else ''), 'constant_string'
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            yield node, 'return None', 'return_value'
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else '')
            if name not in SKIP_CALLS:
                yield node, 'pass', 'remove_call'


def offsets(text: str) -> list[int]:
    out, pos = [0], 0
    for line in text.splitlines(keepends=True):
        pos += len(line)
        out.append(pos)
    return out


def char_index(line_starts: list[int], text_lines: list[str], lineno: int, col_bytes: int) -> int:
    line = text_lines[lineno - 1]
    col = len(line.encode('utf-8')[:col_bytes].decode('utf-8', 'replace'))
    return line_starts[lineno - 1] + col


def make_mutants(repo: Path, scope: dict) -> tuple[list[dict], list[dict]]:
    mutants, files = [], []
    for f in scope['files']:
        if f['kind'] != 'source' or not f['path'].endswith('.py') or f['status'] == 'deleted':
            continue
        changed = {n for a, b in f['ranges'] for n in range(a, b + 1)}
        if not changed:
            continue
        path = repo / f['path']
        text = path.read_text(encoding='utf-8')
        tree = ast.parse(text)
        lines = text.splitlines(keepends=True)
        starts = offsets(text)
        files.append({'path': f['path'], 'changed_lines': sorted(changed)})
        seen = set()
        for node, replacement, op in candidates(tree):
            if node.lineno not in changed:
                continue
            a = char_index(starts, lines, node.lineno, node.col_offset)
            b = char_index(starts, lines, node.end_lineno, node.end_col_offset)
            new_text = text[:a] + replacement + text[b:]
            if new_text == text or new_text in seen:
                continue
            seen.add(new_text)
            first, last = node.lineno, node.end_lineno
            new_lines = new_text.splitlines(keepends=True)
            delta = len(new_lines) - len(lines)
            original = ''.join(lines[first - 1:last]).rstrip('\n')
            mutated = ''.join(new_lines[first - 1:last + delta]).rstrip('\n')
            mutants.append({'file': f['path'], 'line': first, 'end_line': last, 'col': node.col_offset,
                            'operator': op, 'original': original, 'mutated': mutated, '_text': new_text})
    for i, m in enumerate(mutants, 1):
        m['id'] = f'B{i}'
    return mutants, files


def run_coverage(repo: Path, run_dir: Path, scoped: list[str], env: dict, timeout: int) -> tuple[dict, float]:
    plug_dir = run_dir / '_plugin'
    plug_dir.mkdir(parents=True, exist_ok=True)
    (plug_dir / 'mt_cov_plugin.py').write_text(PLUGIN, encoding='utf-8')
    out = run_dir / 'coverage.raw.json'
    cenv = {**env, 'MT_FILES': json.dumps([str(repo / p) for p in scoped]), 'MT_COV_OUT': str(out),
            'PYTHONPATH': os.pathsep.join([str(plug_dir), env.get('PYTHONPATH', '')]).rstrip(os.pathsep)}
    cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'mt_cov_plugin']
    t0 = time.monotonic()
    res = subprocess.run(cmd, cwd=repo, env=cenv, capture_output=True, timeout=timeout)
    took = time.monotonic() - t0
    if not out.exists():
        raise SystemExit('coverage run wrote no data:\n' + res.stdout.decode('utf-8', 'replace')[-1500:])
    raw = json.loads(out.read_text(encoding='utf-8'))
    by_line: dict[str, dict[str, list[str]]] = {}
    for test, files in raw['hits'].items():
        for f, lines in files.items():
            r = Path(f).resolve().relative_to(repo).as_posix()
            for n in lines:
                by_line.setdefault(r, {}).setdefault(str(n), []).append(test)
    for r in by_line.values():
        for n in r:
            r[n] = sorted(set(r[n]))
    cov = {'command': 'python -m pytest -q -p no:cacheprovider -p mt_cov_plugin', 'exit_code': res.returncode,
           'pytest_exit': raw['exitstatus'], 'duration_s': round(took, 3), 'files': scoped, 'lines': by_line,
           'method': 'sys.settrace in a pytest plugin, per test (lines run at import are under "<import>")'}
    out.unlink()
    shutil.rmtree(plug_dir, ignore_errors=True)
    return cov, took


def covering(cov: dict, m: dict, module_level: bool) -> list[str]:
    lines = cov['lines'].get(m['file'], {})
    tests = set()
    for n in range(m['line'], m['end_line'] + 1):
        tests.update(lines.get(str(n), []))
    if module_level and tests <= {'<import>'}:
        tests = {t for ts in lines.values() for t in ts}
    tests.discard('<import>')
    return sorted(tests)


def function_lines(text: str) -> list[tuple[int, int]]:
    tree = ast.parse(text)
    return [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def sample(mutants: list[dict], keep: int, seed: int) -> list[dict]:
    """Round-robin across (operator, file) groups so every operator and file stays represented."""
    groups: dict[tuple, list[dict]] = {}
    for m in mutants:
        groups.setdefault((m['operator'], m['file']), []).append(m)
    rng = random.Random(seed)
    for g in groups.values():
        rng.shuffle(g)
    out, keys = [], sorted(groups)
    while len(out) < keep and any(groups[k] for k in keys):
        for k in keys:
            if groups[k] and len(out) < keep:
                out.append(groups[k].pop())
    return sorted(out, key=lambda m: int(m['id'][1:]))


def restore(run_dir: Path, repo: Path) -> int:
    n = 0
    for b in (run_dir / 'backup').rglob('*'):
        if b.is_file():
            target = repo / b.relative_to(run_dir / 'backup')
            target.write_bytes(b.read_bytes())
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--scope', type=Path, required=True, help='diff_scope.py output')
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--minutes', type=float, default=30.0)
    ap.add_argument('--timeout-factor', type=float, default=3.0)
    ap.add_argument('--min-timeout', type=float, default=10.0)
    ap.add_argument('--max-mutants', type=int)
    ap.add_argument('--seed', type=int, default=1, help='sampling seed')
    ap.add_argument('--coverage-only', action='store_true')
    ap.add_argument('--restore', action='store_true', help='put backed-up originals back and exit')
    args = ap.parse_args(argv)
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    if args.restore:
        print(f'restored {restore(run_dir, repo)} file(s)')
        return 0
    run_dir.mkdir(parents=True, exist_ok=True)
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    skip = {run_dir}
    before = run_tests.tree_snapshot(repo, skip)
    pycache = run_dir / '_pycache'
    shutil.rmtree(pycache, ignore_errors=True)
    pycache.mkdir()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str(pycache)}
    mutants, files = make_mutants(repo, scope)
    scoped = [f['path'] for f in files]
    if not scoped:
        raise SystemExit('no changed Python source lines in scope')
    cov, cov_s = run_coverage(repo, run_dir, scoped, env, timeout=max(600, int(args.minutes * 60)))
    (run_dir / 'coverage.json').write_text(json.dumps(cov, indent=1) + '\n', encoding='utf-8')
    print(f'coverage: {len(scoped)} file(s), {cov_s:.1f}s, pytest exit {cov["exit_code"]}')
    if args.coverage_only:
        return 0
    if cov['exit_code'] != 0:
        raise SystemExit('the suite does not pass unmutated; fix or exclude failing tests first (no mutants run)')
    t0 = time.monotonic()
    plain = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'], cwd=repo, env=env,
                           capture_output=True)
    plain_s = time.monotonic() - t0
    if plain.returncode != 0:
        raise SystemExit('the suite does not pass unmutated without tracing; no mutants run')
    timeout = max(args.min_timeout, args.timeout_factor * plain_s)
    funcs = {f: function_lines((repo / f).read_text(encoding='utf-8')) for f in scoped}
    for m in mutants:
        module_level = not any(a <= m['line'] <= b for a, b in funcs[m['file']])
        m['covering_tests'] = covering(cov, m, module_level)
        m['module_level'] = module_level
    runnable = [m for m in mutants if m['covering_tests']]
    est_each = max(0.5, plain_s)
    keep = int(args.minutes * 60 / est_each)
    if args.max_mutants is not None:
        keep = min(keep, args.max_mutants)
    chosen = runnable if keep >= len(runnable) else sample(runnable, keep, args.seed)
    chosen_ids = {m['id'] for m in chosen}
    backup = run_dir / 'backup'
    deadline = time.monotonic() + args.minutes * 60
    junit_dir = run_dir / '_junit_builtin'
    junit_dir.mkdir(exist_ok=True)
    originals = {f: (repo / f).read_bytes() for f in scoped}
    for f, data in originals.items():
        (backup / f).parent.mkdir(parents=True, exist_ok=True)
        (backup / f).write_bytes(data)
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    try:
        for m in mutants:
            text = m.pop('_text')
            if not m['covering_tests']:
                m['status'] = 'no_coverage'
                continue
            if m['id'] not in chosen_ids:
                m['status'] = 'not_run'
                m['note'] = 'sampled out by the budget'
                continue
            if time.monotonic() > deadline:
                m['status'] = 'not_run'
                m['note'] = 'budget exhausted'
                continue
            try:
                compile(text, m['file'], 'exec')
            except SyntaxError as exc:
                m['status'] = 'compile_error'
                m['note'] = str(exc)
                continue
            target = repo / m['file']
            junit = junit_dir / f'{m["id"]}.xml'
            cmd = [sys.executable, '-m', 'pytest', '-x', '-q', '-p', 'no:cacheprovider', f'--junitxml={junit}',
                   *m['covering_tests']]
            t0 = time.monotonic()
            try:
                target.write_bytes(text.encode('utf-8'))
                proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
                try:
                    out, _ = proc.communicate(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    run_tests.kill_tree(proc)
                    out, _ = proc.communicate()
                    timed_out = True
            finally:
                target.write_bytes(originals[m['file']])
            m['duration_s'] = round(time.monotonic() - t0, 3)
            m['exit_code'] = proc.returncode
            cases, _ = parse_results.parse_junit(junit, m['id']) if junit.exists() else ([], [])
            failed = [c['id'] for c in cases if c['status'] in ('failed', 'error')]
            if timed_out:
                m['status'] = 'timeout'
            elif proc.returncode == 0 and cases:
                m['status'] = 'survived'
            elif proc.returncode == 1 and failed:
                m['status'] = 'killed'
                m['killed_by'] = failed[0]
            else:
                m['status'] = 'runtime_error'
                m['note'] = run_tests.redact_home(out.decode('utf-8', 'replace')[-400:])
            if junit.exists():
                junit.unlink()
            print(f'{m["id"]:>5} {m["file"]}:{m["line"]} {m["operator"]:<22} {m["status"]}')
    finally:
        for f, data in originals.items():
            if (repo / f).read_bytes() != data:
                (repo / f).write_bytes(data)
    after = run_tests.tree_snapshot(repo, skip)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(pycache, ignore_errors=True)
    shutil.rmtree(junit_dir, ignore_errors=True)
    counts: dict[str, int] = {}
    for m in mutants:
        counts[m['status']] = counts.get(m['status'], 0) + 1
    out = {'engine': {'name': 'builtin', 'version': VERSION, 'python': sys.version.split()[0],
                      'command': 'python scripts/builtin_mutator.py --repo <repo> --scope <scope> --run-dir <dir>'},
           'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'files': files,
           'timeout_s': round(timeout, 1), 'suite_duration_s': round(plain_s, 3), 'coverage_duration_s': round(cov_s, 3),
           'sampled': {'generated': len(mutants), 'runnable': len(runnable), 'chosen': len(chosen),
                       'skipped': len(runnable) - len(chosen), 'seed': args.seed},
           'tree': {'before': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    'after': hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
                    'changed': changed},
           'counts': counts, 'mutants': mutants}
    (run_dir / 'builtin.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(mutants)} mutants: ' + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())) +
          f' -> {run_dir / "builtin.json"}')
    if changed:
        print('ERROR: the working tree changed during the run: ' + ', '.join(changed), file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
