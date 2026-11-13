#!/usr/bin/env python3
"""The builtin engine's Go half (stdlib only): text-level mutants on the changed lines of Go packages.

Called by builtin_mutator.py when the scope's changed source is Go; not meant to run alone.

Mutants are made from Go tokens (go_scan.py), so nothing inside a comment, string, raw string or
rune literal is ever changed. Operators (data/operators.json names):
  comparison_boundary   <  <=  >  >=  (one step: < -> <=, <= -> <, ...)
  comparison_negate     == <-> !=
  logical               && <-> ||
  boolean_constant      true <-> false
  arithmetic            binary + <-> -, * <-> /, % -> *; += <-> -=, *= <-> /=; ++ <-> --
                        (a + next to a string literal is concatenation and is left alone)
  control_flow          break <-> continue
  return_value          return ..., err -> return ..., nil
  negate_condition      if cond { -> if !(cond) {   (after an init statement's ';')

Every go command runs from the module root (the nearest go.mod) on one package directory, with
-count=1 and -vet=off, and never writes into the repository: each mutant is handed to the
compiler with `go test -overlay`, which swaps the mutated file in at build time.

1. Coverage: the package's test binary is built once (`go test -c -cover -covermode=set`), the
   tests are listed (`-test.list`), and each top-level test runs alone with its own
   -test.coverprofile, so each changed line knows the tests that ran it. Test ids are
   `<package dir>/<file>_test.go::TestName`. A failing test stops the run (the baseline is not
   green); --go-run / --go-skip narrow the tests first (environment-only failures).
2. Runs: per mutant, `go test -count=1 -vet=off -overlay <file> -run '^(covering tests)$' ./<pkg>`
   with a timeout. Exit 0 = survived; a `--- FAIL: TestX` = killed (killed_by is TestX's id);
   a build failure = compile_error (not viable, never a kill); a timeout = timeout; anything else
   = runtime_error. A mutant on a line no test ran is no_coverage and is not run.
"""
from __future__ import annotations
import hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import go_scan  # noqa: E402
import run_tests  # noqa: E402

CMP = {'<': [('<=', 'comparison_boundary')], '<=': [('<', 'comparison_boundary')],
       '>': [('>=', 'comparison_boundary')], '>=': [('>', 'comparison_boundary')],
       '==': [('!=', 'comparison_negate')], '!=': [('==', 'comparison_negate')]}
ARITH = {'+': '-', '-': '+', '*': '/', '/': '*', '%': '*'}
ASSIGN = {'+=': '-=', '-=': '+=', '*=': '/=', '/=': '*='}
OPERAND_END = {'ident', 'number'}
FAIL_LINE = re.compile(r'^\s*--- FAIL: (Test[^\s/]*)', re.M)
BUILD_FAILED = re.compile(r'\[(?:build|setup) failed\]|^# \S+', re.M)


def is_go_source(path: str) -> bool:
    return path.endswith('.go') and not path.endswith('_test.go')


def binary_at(toks: list, k: int, text: str) -> bool:
    """Is the operator at toks[k] binary? An operand must end just before it, and gofmt spaces a binary
    operator the same on both sides (`a * b`, `a*b`) but a unary one only before (`x := *p`, `f(-n)`)."""
    if k == 0 or k + 1 >= len(toks):
        return False
    prev = toks[k - 1]
    if not (prev.kind in OPERAND_END or prev.text in (')', ']')) or prev.kind == 'keyword':
        return False
    t = toks[k]
    before = t.start > 0 and text[t.start - 1] in ' \t'
    after = t.end < len(text) and text[t.end] in ' \t'
    return before == after


def candidates(text: str):
    """Yield (line, start, end, replacement, operator) for every mutation on any line."""
    toks = go_scan.tokens(text)
    for k, t in enumerate(toks):
        if t.kind == 'op':
            if t.text in CMP and binary_at(toks, k, text):
                for new, op in CMP[t.text]:
                    yield t.line, t.start, t.end, new, op
            elif t.text in ('&&', '||'):
                yield t.line, t.start, t.end, '||' if t.text == '&&' else '&&', 'logical'
            elif t.text in ARITH and binary_at(toks, k, text):
                if toks[k - 1].kind == 'str' or toks[k + 1].kind == 'str':
                    continue
                yield t.line, t.start, t.end, ARITH[t.text], 'arithmetic'
            elif t.text in ASSIGN:
                yield t.line, t.start, t.end, ASSIGN[t.text], 'arithmetic'
            elif t.text in ('++', '--'):
                yield t.line, t.start, t.end, '--' if t.text == '++' else '++', 'arithmetic'
        elif t.kind == 'ident' and t.text in ('true', 'false'):
            yield t.line, t.start, t.end, 'false' if t.text == 'true' else 'true', 'boolean_constant'
        elif t.kind == 'keyword' and t.text in ('break', 'continue'):
            yield t.line, t.start, t.end, 'continue' if t.text == 'break' else 'break', 'control_flow'
        elif t.kind == 'keyword' and t.text == 'return':
            # return ..., err -> return ..., nil: an error result that is returned as is
            j = k + 1
            while j < len(toks) and toks[j].line == t.line:
                x = toks[j]
                if x.kind == 'ident' and x.text == 'err' and toks[j - 1].text in ('return', ',') and \
                        (j + 1 >= len(toks) or toks[j + 1].line != t.line or toks[j + 1].text in (',', '}')):
                    yield t.line, x.start, x.end, 'nil', 'return_value'
                j += 1
        elif t.kind == 'keyword' and t.text == 'if':
            line_toks = [x for x in toks[k + 1:] if x.line == t.line]
            opens = [i for i, x in enumerate(line_toks) if x.text == '{']
            if not opens or line_toks[-1].text != '{':
                continue
            last = len(line_toks) - 1
            depth, start = 0, 0
            for i, x in enumerate(line_toks[:last]):
                if x.text in '([{':
                    depth += 1
                elif x.text in ')]}':
                    depth -= 1
                elif x.text == ';' and depth == 0:
                    start = i + 1
            cond = line_toks[start:last]
            if not cond:
                continue
            ops = [x.text for x in cond if x.kind == 'op']
            if ops.count('==') + ops.count('!=') == 1 and not {'&&', '||', '!', '<', '>', '<=', '>='} & set(ops):
                continue        # `if a != b` negated is `if a == b`: comparison_negate already makes that mutant
            a, b = cond[0].start, cond[-1].end
            yield t.line, a, b, '!(' + text[a:b] + ')', 'negate_condition'


def make_mutants(repo: Path, scope: dict) -> tuple[list[dict], list[dict]]:
    mutants, files = [], []
    for f in scope['files']:
        if f['kind'] != 'source' or not is_go_source(f['path']) or f['status'] == 'deleted':
            continue
        changed = {n for a, b in f['ranges'] for n in range(a, b + 1)}
        if not changed:
            continue
        raw = (repo / f['path']).read_bytes()
        text = raw.decode('utf-8')
        lines = text.splitlines(keepends=True)
        starts = [0]
        for l in lines:
            starts.append(starts[-1] + len(l))
        files.append({'path': f['path'], 'changed_lines': sorted(changed)})
        seen = set()
        for line, a, b, new, op in candidates(text):
            if line not in changed:
                continue
            mutated_text = text[:a] + new + text[b:]
            if mutated_text == text or mutated_text in seen:
                continue
            seen.add(mutated_text)
            original = lines[line - 1].rstrip('\r\n')
            col = a - starts[line - 1]
            mutated = original[:col] + new + original[col + (b - a):]
            mutants.append({'file': f['path'], 'line': line, 'end_line': line, 'col': col, 'operator': op,
                            'original': original, 'mutated': mutated, '_text': mutated_text,
                            '_pos': (len(files), line, col, b - a, len(mutants))})
    mutants.sort(key=lambda m: m.pop('_pos'))
    for i, m in enumerate(mutants, 1):
        m['id'] = f'B{i}'
    return mutants, files


def go_env(run_dir: Path) -> dict:
    """The go commands' environment: build cache and temporary files under the run directory are left to
    go itself, but GOFLAGS from the caller are kept; nothing is added that changes the module."""
    return {**os.environ, 'GOTOOLCHAIN': os.environ.get('GOTOOLCHAIN', 'local')}


def run_cmd(cmd: list[str], cwd: Path, env: dict, timeout: float | None) -> tuple[int | None, str, bool]:
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode('utf-8', 'replace'), False
    except subprocess.TimeoutExpired:
        run_tests.kill_tree(proc)
        out, _ = proc.communicate()
        return proc.returncode, out.decode('utf-8', 'replace'), True


class Package:
    """One Go package directory in scope: where to run go, and how to name its tests."""

    def __init__(self, repo: Path, rel_dir: str):
        mod = go_scan.module_of(repo, rel_dir)
        if mod is None:
            raise SystemExit(f'{rel_dir}: no go.mod at or above it, so it cannot be built')
        self.repo, self.rel_dir = repo, rel_dir
        self.module_dir, self.module = mod
        self.mod_root = repo / self.module_dir
        rel_in_mod = os.path.relpath(repo / rel_dir, self.mod_root).replace('\\', '/')
        self.arg = './' + rel_in_mod if rel_in_mod != '.' else '.'
        self.index = go_scan.test_file_index(repo / rel_dir)

    def test_id(self, name: str) -> str:
        top = name.split('/')[0]
        f = self.index.get(top)
        prefix = '' if self.rel_dir in ('.', '') else self.rel_dir + '/'
        return f'{prefix}{f}::{name}' if f else f'{prefix}(package)::{name}'


def coverage(pkg: Package, scoped: list[str], run_dir: Path, env: dict, go_run: str, go_skip: str,
             flags: list[str], timeout: float) -> dict:
    """Per-test line coverage of the scoped files of one package (one coverprofile per top-level test)."""
    bdir = run_dir / '_gobin'
    bdir.mkdir(parents=True, exist_ok=True)
    exe = bdir / (re.sub(r'[^\w.-]', '_', pkg.rel_dir or 'root') + '.test' + ('.exe' if os.name == 'nt' else ''))
    t0 = time.monotonic()
    code, out, to = run_cmd(['go', 'test', '-c', '-cover', '-covermode=set', '-vet=off', *flags, '-o', str(exe), pkg.arg],
                            pkg.mod_root, env, timeout)
    if code != 0 or not exe.is_file():
        raise SystemExit(f'go test -c {pkg.arg} failed (exit {code}); the package does not build:\n'
                         + run_tests.redact_home(out[-1500:]))
    code, out, _ = run_cmd([str(exe), '-test.list', go_run or '.'], pkg.repo / pkg.rel_dir, env, timeout)
    names = [l.strip() for l in out.splitlines() if re.fullmatch(r'Test\w*', l.strip())]
    if go_skip:
        names = [n for n in names if not re.search(go_skip, n)]
    lines: dict[str, dict[str, list[str]]] = {}
    failed = []
    pdir = run_dir / '_goprof'
    pdir.mkdir(exist_ok=True)
    for name in names:
        prof = pdir / f'{name}.out'
        code, out, to = run_cmd([str(exe), '-test.run', f'^{name}$', '-test.count=1', f'-test.coverprofile={prof}'],
                                pkg.repo / pkg.rel_dir, env, timeout)
        if code != 0 or to:
            failed.append(pkg.test_id(name))
        if prof.is_file():
            for f, lns in go_scan.coverprofile(prof.read_text(encoding='utf-8', errors='replace'), pkg.module,
                                               pkg.module_dir).items():
                if f in scoped:
                    for n in lns:
                        lines.setdefault(f, {}).setdefault(str(n), []).append(pkg.test_id(name))
            prof.unlink()
    shutil.rmtree(pdir, ignore_errors=True)
    return {'tests': [pkg.test_id(n) for n in names], 'failed': failed, 'lines': lines,
            'duration_s': round(time.monotonic() - t0, 3)}


def classify(code: int | None, out: str, timed_out: bool, pkg: Package) -> tuple[str, str | None, str | None]:
    """(status, killed_by, note) for one mutant run."""
    if timed_out:
        return 'timeout', None, None
    fails = FAIL_LINE.findall(out)
    if code == 0:
        return 'survived', None, None
    if fails:
        return 'killed', pkg.test_id(fails[0]), None
    if BUILD_FAILED.search(out):
        first = next((l.strip() for l in out.splitlines() if re.search(r'\.go:\d+:\d+:', l)), '')
        return 'compile_error', None, run_tests.redact_home(first)[:300] or 'the mutant does not build'
    return 'runtime_error', None, run_tests.redact_home(out[-400:])


def run_regex(names: list[str]) -> str:
    return '^(' + '|'.join(re.escape(n) for n in names) + ')$'


def tree_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def main(args, scope: dict) -> int:
    """Run the Go engine; args are builtin_mutator.py's parsed arguments."""
    from datetime import datetime, timezone
    import builtin_mutator as bm
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    if shutil.which('go') is None:
        raise SystemExit('the Go engine needs the go toolchain on PATH (go test, go list); none was found')
    gv = run_cmd(['go', 'version'], repo, os.environ.copy(), 60)[1].strip()
    run_dir.mkdir(parents=True, exist_ok=True)
    env = go_env(run_dir)
    flags = shlex.split(args.go_flags) if args.go_flags else []
    skip = {run_dir}
    before = run_tests.tree_snapshot(repo, skip)
    mutants, files = make_mutants(repo, scope)
    scoped = [f['path'] for f in files]
    pkgs: dict[str, Package] = {}
    for f in scoped:
        d = str(PurePosixPath(f).parent)
        if d not in pkgs:
            pkgs[d] = Package(repo, d)
    budget = max(600, int(args.minutes * 60))
    cov = {'tests': [], 'failed': [], 'lines': {}, 'duration_s': 0.0}
    for d, pkg in pkgs.items():
        c = coverage(pkg, scoped, run_dir, env, args.go_run, args.go_skip, flags, budget)
        cov['tests'] += c['tests']
        cov['failed'] += c['failed']
        cov['duration_s'] += c['duration_s']
        for f, per in c['lines'].items():
            cov['lines'].setdefault(f, {}).update(per)
    shutil.rmtree(run_dir / '_gobin', ignore_errors=True)
    for f in cov['lines'].values():
        for n in f:
            f[n] = sorted(set(f[n]))
    shown = 'per package: go test -c -cover -covermode=set -vet=off; per top-level test: <binary> -test.run ^Name$ ' \
            '-test.coverprofile' + (f' (-test.list {args.go_run})' if args.go_run else '') + \
            (f' (skipping {args.go_skip})' if args.go_skip else '')
    cov_rec = {'command': shown, 'exit_code': 1 if cov['failed'] else 0, 'duration_s': round(cov['duration_s'], 3),
               'files': scoped, 'lines': cov['lines'], 'tests': cov['tests'], 'failed_tests': cov['failed'],
               'method': 'Go coverprofile per top-level test (covermode=set); block ranges expanded to lines'}
    (run_dir / 'coverage.json').write_text(json.dumps(cov_rec, indent=1) + '\n', encoding='utf-8')
    print(f'coverage: {len(scoped)} file(s) in {len(pkgs)} package(s), {len(cov["tests"])} tests, '
          f'{cov["duration_s"]:.1f}s' + (f', {len(cov["failed"])} failed' if cov['failed'] else ''))
    if not cov['tests']:
        raise SystemExit('no Go tests were found for the scoped package(s); every mutant would be no_coverage '
                         '(the tests may live in another package: say so, and mutate with a tool that runs them)')
    if args.coverage_only:
        return 0
    if cov['failed']:
        raise SystemExit('the tests do not pass unmutated (' + ', '.join(cov['failed'][:5]) + '); no mutants run. '
                         'Narrow them with --go-run / --go-skip (and say why in the baseline note)')
    # timing: one plain run of each package's tests, which includes building it
    t0 = time.monotonic()
    for pkg in pkgs.values():
        code, out, _ = run_cmd(['go', 'test', '-count=1', '-vet=off', *flags,
                                *(['-run', args.go_run] if args.go_run else []),
                                *(['-skip', args.go_skip] if args.go_skip else []), pkg.arg], pkg.mod_root, env, budget)
        if code != 0:
            raise SystemExit(f'go test {pkg.arg} does not pass unmutated without coverage; no mutants run:\n'
                             + run_tests.redact_home(out[-800:]))
    plain_s = time.monotonic() - t0
    timeout = max(args.min_timeout, args.timeout_factor * plain_s / max(1, len(pkgs)))
    for m in mutants:
        m['covering_tests'] = sorted(cov['lines'].get(m['file'], {}).get(str(m['line']), []))
        m['module_level'] = False
    runnable = [m for m in mutants if m['covering_tests']]
    est_each = max(0.5, plain_s / max(1, len(pkgs)))
    keep = int(args.minutes * 60 / est_each)
    if args.max_mutants is not None:
        keep = min(keep, args.max_mutants)
    chosen = runnable if keep >= len(runnable) else bm.sample(runnable, keep, args.seed)
    chosen_ids = {m['id'] for m in chosen}
    deadline = time.monotonic() + args.minutes * 60
    odir = run_dir / '_overlay'
    odir.mkdir(exist_ok=True)
    try:
        for m in mutants:
            text = m.pop('_text')
            if not m['covering_tests']:
                m['status'] = 'no_coverage'
                continue
            if m['id'] not in chosen_ids:
                m['status'], m['note'] = 'not_run', 'sampled out by the budget'
                continue
            if time.monotonic() > deadline:
                m['status'], m['note'] = 'not_run', 'budget exhausted'
                continue
            pkg = pkgs[str(PurePosixPath(m['file']).parent)]
            mfile = odir / f'{m["id"]}.go'
            mfile.write_bytes(text.encode('utf-8'))
            overlay = odir / f'{m["id"]}.json'
            overlay.write_text(json.dumps({'Replace': {str((repo / m['file']).resolve()): str(mfile)}}), encoding='utf-8')
            names = sorted({t.split('::', 1)[1].split('/')[0] for t in m['covering_tests']})
            cmd = ['go', 'test', '-count=1', '-vet=off', '-failfast', *flags, f'-overlay={overlay}',
                   f'-timeout={int(timeout) + 30}s', '-run', run_regex(names), pkg.arg]
            t0 = time.monotonic()
            code, out, timed_out = run_cmd(cmd, pkg.mod_root, env, timeout)
            m['duration_s'] = round(time.monotonic() - t0, 3)
            m['exit_code'] = code
            status, killed_by, note = classify(code, out, timed_out, pkg)
            m['status'] = status
            if killed_by:
                m['killed_by'] = killed_by
            if note:
                m['note'] = re.sub(r'\S*_overlay[\\/]B\d+\.go', m['file'], note)
            mfile.unlink()
            overlay.unlink()
            print(f'{m["id"]:>5} {m["file"]}:{m["line"]} {m["operator"]:<22} {m["status"]}')
    finally:
        shutil.rmtree(odir, ignore_errors=True)
    after = run_tests.tree_snapshot(repo, skip)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    counts: dict[str, int] = {}
    for m in mutants:
        counts[m['status']] = counts.get(m['status'], 0) + 1
    command = 'python scripts/builtin_mutator.py --repo <repo> --scope <scope> --run-dir <dir>' + \
        (f' --go-run {shlex.quote(args.go_run)}' if args.go_run else '') + \
        (f' --go-skip {shlex.quote(args.go_skip)}' if args.go_skip else '') + \
        (f' --go-flags {shlex.quote(args.go_flags)}' if args.go_flags else '') + \
        ' (per mutant: go test -count=1 -vet=off -overlay <mutant> -run ^(covering tests)$ <package>)'
    out = {'engine': {'name': 'builtin', 'version': bm.VERSION, 'python': sys.version.split()[0], 'go': gv,
                      'command': command},
           'language': 'go', 'ids': 'position', 'tests': [args.go_run] if args.go_run else [], 'pytest_args': [],
           'go_flags': flags, 'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'files': files,
           'packages': [{'dir': p.rel_dir, 'module': p.module, 'module_dir': p.module_dir} for p in pkgs.values()],
           'timeout_s': round(timeout, 1), 'suite_duration_s': round(plain_s, 3),
           'coverage_duration_s': round(cov['duration_s'], 3),
           'sampled': {'generated': len(mutants), 'runnable': len(runnable), 'chosen': len(chosen),
                       'skipped': len(runnable) - len(chosen), 'seed': args.seed},
           'tree': {'before': tree_hash(before), 'after': tree_hash(after), 'changed': changed},
           'counts': counts, 'mutants': mutants}
    (run_dir / 'builtin.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(mutants)} mutants: ' + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())) +
          f' -> {run_dir / "builtin.json"}')
    if changed:
        print('ERROR: the working tree changed during the run: ' + ', '.join(changed), file=sys.stderr)
        return 3
    return 0
