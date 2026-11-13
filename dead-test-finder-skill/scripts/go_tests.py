#!/usr/bin/env python3
"""Go support for the dead test finder (stdlib only): scan *_test.go files, neutralise Go functions, record
`go test -json` runs in the runs.json shape.

    python go_tests.py record --json <go test -json output> --out runs.json --label original --run-prefix N1
        [--command "go test -json -count=1 -run '^TestX$' ./pkg/x"] [--module example.test/mod]
    python go_tests.py run --cwd <module dir> --package ./pkg/x --run '^TestX$' --out runs.json --label original
        --run-prefix N1 [--env K=V ...]

Scanning (called by scan_tests.py) reads Go source with comment- and string-aware patterns, never compiles it. For
each `func TestX(t *testing.T)` in a _test.go file it records a definition (id `<file>::TestX`, framework `go-test`)
and these candidates:

    no_assertion / no_assertion   no failure path: no t.Error*/t.Fatal*/t.Fail*, no require./assert./is. call, no
                                  panic or log.Fatal, and no helper that fails (a same-package helper is read; one in
                                  another package, or a method given t, is assumed to fail). fmt.Errorf is never a
                                  failure path: only the test's own t (and t.Run closures' t) count
    no_assertion / log_only       the same, with t.Log/t.Logf where a failure was meant
    skipped / skip                the body starts with an unconditional t.Skip/t.Skipf/t.SkipNow (check_skips.py
                                  gives it a ticket and age verdict)
    unreached_assertion / empty_table   a table declared empty in the test and ranged over: its checks never run
    tautology / compare_to_self   `if x != x`, assert/require.Equal(t, x, x), assert.True(t, true)
    never_runs / name_pattern     `func Testlower(t *testing.T)`: a malformed test name. go test's vet check fails
                                  the package's build over it (no test there runs); with -vet=off it is skipped

`calls` lists the product functions a test calls as `<package dir>:<Func>` or `<package dir>:<Type>.<Method>` (same
package, or a package of the same module it imports), the targets neutralise.py patches.

Neutralising a Go function replaces its body with zero values (`return *new(T1), *new(T2)`, or a bare return for
named results) or `panic(...)`, then turns each import the file no longer uses into a blank import (`_ "path"`), so
the file still compiles. A neutralised package that does not build is an errored run, never a failure.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MARK = 'neutralised by dead-test-finder'
KEY = re.compile(json.loads((Path(__file__).resolve().parent.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))
                 ['ticket_key'])


# ---------------------------------------------------------------- lexing
def mask(text: str) -> str:
    """Comments and the insides of string and rune literals blanked (same length, newlines kept)."""
    out, i, n = list(text), 0, len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        c = text[i]
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            blank(i + 1, min(j, n))
            i = j + 1
        elif c == '`':
            j = text.find('`', i + 1)
            j = n if j < 0 else j
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def close_of(masked: str, open_pos: int) -> int:
    """The index of the bracket closing the one at open_pos."""
    depth = 0
    for k in range(open_pos, len(masked)):
        if masked[k] in '{([':
            depth += 1
        elif masked[k] in '})]':
            depth -= 1
            if depth == 0:
                return k
    return len(masked) - 1


def split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, ''
    for ch in s:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def line_at(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


STR = re.compile(r'"((?:\\.|[^"\\\n])*)"|`([^`]*)`')
FUNC = re.compile(r'^func\s+(?:\(\s*(\w+)?\s*\*?\s*(\w+)(?:\[[^\]]*\])?\s*\)\s*)?(\w+)\s*(?:\[[^\]]*\])?\s*\(', re.M)
TEST_FUNC = re.compile(r'^func\s+(Test\w*)\s*\(\s*(\w+)\s+\*testing\.T\s*\)\s*\{', re.M)
T_PARAM = re.compile(r'\b(\w+)\s+(?:\*testing\.(?:T|B|F)|testing\.TB)\b')
EXTERNAL_FAIL = re.compile(r'\b(?:require|assert|is|must|qt|check|expect|gomega|Expect)\.\w+\(|\bExpect\(|\bpanic\(|'
                           r'\blog\.(?:Fatal\w*|Panic\w*)\(|\bos\.Exit\(')


def functions(text: str, masked: str) -> list[dict]:
    """Top-level function declarations: name, receiver type, the masked body span, its parameters text."""
    out = []
    for m in FUNC.finditer(masked):
        po = m.end() - 1
        pc = close_of(masked, po)
        brace, k = -1, pc + 1
        while k < len(masked):                       # the body's brace: skip (results), [type args], interface{}...
            ch = masked[k]
            if ch == '\n':
                break                                # no body on this line: a declaration without one
            if ch in '([':
                k = close_of(masked, k) + 1
                continue
            if ch == '{':
                if re.search(r'\b(?:interface|struct)\s*$', masked[pc + 1:k]):
                    k = close_of(masked, k) + 1
                    continue
                brace = k
                break
            k += 1
        if brace < 0:
            continue
        bc = close_of(masked, brace)
        out.append({'name': m.group(3), 'recv': m.group(2), 'start': m.start(), 'params': masked[po + 1:pc],
                    'params_close': pc, 'brace': brace, 'end': bc, 'line': line_at(text, m.start())})
    return out


# ---------------------------------------------------------------- packages and imports
def imports(text: str, masked: str) -> list[dict]:
    """[{alias, path, start, end}] for every import spec (single or in a block); alias is the explicit name or the
    package name guessed from the path (a /vN suffix and a .vN or go- prefix dropped)."""
    out = []
    for m in re.finditer(r'(?m)^import\s*(\(|(?=[\w."`]))', masked):
        if m.group(1) == '(':
            a, b = m.end(), close_of(masked, m.end() - 1)
        else:
            a, b = m.end(), masked.find('\n', m.end())
            b = len(masked) if b < 0 else b
        for sm in re.finditer(r'(?m)^[ \t]*(?:(\w+|\.|_)[ \t]+)?("|`)', masked[a:b] if m.group(1) == '(' else masked[a:b]):
            q = a + sm.end() - 1
            lit = STR.match(text, q)
            if not lit:
                continue
            path = lit.group(1) if lit.group(1) is not None else lit.group(2)
            alias = sm.group(1)
            if not alias:
                segs = path.split('/')
                last = segs[-1]
                if re.fullmatch(r'v\d+', last) and len(segs) > 1:
                    last = segs[-2]
                last = re.sub(r'\.v\d+$', '', last)
                last = re.sub(r'^go-', '', last)
                alias = re.sub(r'[^\w]', '', last)
            out.append({'alias': alias, 'path': path, 'start': a + sm.start(), 'end': lit.end()})
    return out


def module_of(root: Path, rel_dir: str) -> tuple[str, str] | None:
    """(module path, module dir relative to root) of the nearest go.mod at or above rel_dir."""
    d = PurePosixPath(rel_dir)
    while True:
        gm = root / d / 'go.mod'
        if gm.is_file():
            m = re.search(r'(?m)^\s*module\s+"?([^\s"]+)', gm.read_text(encoding='utf-8', errors='replace'))
            if m:
                return m.group(1), ('' if d.as_posix() == '.' else d.as_posix())
        if d.as_posix() in ('.', ''):
            return None
        d = d.parent


def package_dir_of(root: Path, from_dir: str, import_path: str) -> str | None:
    mod = module_of(root, from_dir)
    if not mod or not (import_path == mod[0] or import_path.startswith(mod[0] + '/')):
        return None
    rest = import_path[len(mod[0]):].strip('/')
    return '/'.join(x for x in (mod[1], rest) if x) or '.'


# ---------------------------------------------------------------- scanning
class Package:
    """One directory's Go files: product functions and methods (non-test files) and test helpers (test files)."""

    def __init__(self, root: Path, rel_dir: str):
        self.root, self.dir = root, rel_dir
        self.product: dict[str, list[str]] = {}      # name -> ['Func'] or ['Recv.Name', ...]
        self.helpers: dict[str, tuple[str, str]] = {}   # helper name -> (masked body, t param names regex)
        base = root / rel_dir if rel_dir != '.' else root
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            if not n.endswith('.go'):
                continue
            try:
                text = (base / n).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            masked = mask(text)
            for f in functions(text, masked):
                if n.endswith('_test.go'):
                    tps = [p.group(1) for p in T_PARAM.finditer(f['params'])]
                    if tps and not f['name'].startswith(('Test', 'Benchmark', 'Fuzz', 'Example')):
                        self.helpers[f['name']] = (masked[f['brace']:f['end'] + 1], tps)
                else:
                    self.product.setdefault(f['name'], []).append(f'{f["recv"]}.{f["name"]}' if f['recv'] else f['name'])


def fails_in(body: str, tnames: list[str], pkg: Package, depth: int = 0) -> bool:
    """The masked body has a failure path for one of the t names, directly, through a t.Run closure (its own t), or
    through a helper given t."""
    names = set(tnames) | {p.group(1) for p in T_PARAM.finditer(body)}
    alt = '|'.join(re.escape(n) for n in sorted(names))
    if re.search(r'(?<![\w.])(?:' + alt + r')\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\(', body):
        return True
    if EXTERNAL_FAIL.search(body):
        return True
    for m in re.finditer(r'(?<![\w])((?:\w+\.)*\w+)\(\s*(?:' + alt + r')\s*[,)]', body):
        callee = m.group(1)
        if callee.split('.')[-1] in ('Run', 'Parallel', 'Helper', 'Cleanup', 'Log', 'Logf', 'Skip', 'Skipf', 'Setenv',
                                     'TempDir', 'Chdir', 'Context', 'Name', 'Deadline'):
            continue
        if '.' in callee:
            return True                               # a helper in another package or a method: assumed to fail
        h = pkg.helpers.get(callee)
        if h is None:
            return True                               # not a helper we can read: assumed to fail
        if depth < 3 and fails_in(h[0], h[1], pkg, depth + 1):
            return True
    # t.Run("name", namedHelper)
    for m in re.finditer(r'(?<![\w.])(?:' + alt + r')\.Run\([^,]+,\s*(\w+)\s*\)', body):
        h = pkg.helpers.get(m.group(1))
        if h is None or (depth < 3 and fails_in(h[0], h[1], pkg, depth + 1)):
            return True
    return False


TAUTOLOGY = [
    re.compile(r'\bif\s+([\w.]+(?:\[[\w.]+\])?)\s*!=\s*\1\s*\{'),
    re.compile(r'\b(?:assert|require)\.(?:Equal|EqualValues|Exactly)\(\s*\w+\s*,\s*([^,()]+?)\s*,\s*\1\s*[,)]'),
    re.compile(r'\b(?:assert|require)\.True\(\s*\w+\s*,\s*true\s*[,)]'),
    re.compile(r'\b(?:assert|require)\.False\(\s*\w+\s*,\s*false\s*[,)]'),
    re.compile(r'\b(?:assert|require)\.Nil\(\s*\w+\s*,\s*nil\s*[,)]'),
]


def scan_file(root: Path, rel: str, pkg_name: str, add, defs: list, packages: dict) -> None:
    """Definitions and candidates for one _test.go file. add(category, rule, test_ids, line, why, details)."""
    text = (root / rel).read_text(encoding='utf-8', errors='replace')
    masked = mask(text)
    d = PurePosixPath(rel).parent.as_posix()
    pkg = packages.get(d) or packages.setdefault(d, Package(root, d))
    imps = {i['alias']: i['path'] for i in imports(text, masked)}
    for f in functions(text, masked):
        tm = re.match(r'(Test\w*)$', f['name'])
        tps = [p.group(1) for p in T_PARAM.finditer(f['params'])]
        if not tm or f['recv'] or not re.fullmatch(r'\s*\w+\s+\*testing\.T\s*', f['params']):
            continue
        name, tvar = f['name'], tps[0] if tps else 't'
        tid = f'{rel}::{name}'
        body = masked[f['brace'] + 1:f['end']]
        boff = f['brace'] + 1
        calls = []
        for m in re.finditer(r'(?<![\w.])([A-Za-z_]\w*)\s*\(', body):
            for q in pkg.product.get(m.group(1), [])[:1]:
                calls.append(f'{d}:{q}')
        for m in re.finditer(r'(?<![\w.])(\w+)\.([A-Z]\w*)\s*\(', body):
            alias, fn = m.group(1), m.group(2)
            if alias in imps:
                pd = package_dir_of(root, d, imps[alias])
                if pd is not None:
                    other = packages.get(pd) or packages.setdefault(pd, Package(root, pd))
                    if fn in other.product and fn in other.product[fn]:
                        calls.append(f'{pd}:{fn}')
            else:
                for q in pkg.product.get(fn, []):
                    if '.' in q:
                        calls.append(f'{d}:{q}')
                        break
        calls = list(dict.fromkeys(calls))[:12]
        defs.append({'id': tid, 'file': rel, 'line': f['line'], 'name': name, 'lang': 'go', 'package': pkg_name,
                     'framework': 'go-test', 'calls': calls, 'parametrised': False, 'name_pattern': None})
        if len(name) > 4 and name[4].islower():
            add('never_runs', 'name_pattern', [tid], f['line'],
                f'{name}: a test name needs an upper-case letter, a digit or an underscore after Test. go test\'s vet '
                'check rejects the malformed name, so none of the package\'s tests build; with -vet=off it is silently '
                'not a test', {})
            continue
        stripped = body.lstrip()
        first = re.match(r'(?:' + re.escape(tvar) + r')\.(Skip|Skipf|SkipNow)\(', stripped)
        if first:
            pos = boff + (len(body) - len(stripped))
            lit = STR.search(text, pos, f['end'])
            reason = (lit.group(1) or lit.group(2)) if lit and first.group(1) != 'SkipNow' else None
            key = KEY.search(reason or '')
            add('skipped', 'skip', [tid], line_at(text, pos), f't.{first.group(1)} skips the whole test: '
                f'{reason or "no reason given"}',
                {'kind': f't.{first.group(1)}', 'reason': reason, 'conditional': False, 'condition': None,
                 'ticket': key.group(1) if key else None})
            continue
        for rx in TAUTOLOGY:
            m = rx.search(body)
            if m:
                add('tautology', 'compare_to_self', [tid], line_at(text, boff + m.start()),
                    'the check compares a value with itself or a constant, so it can never fail', {})
                break
        for m in re.finditer(r'\bfor\s+[\w\s,]*:?=\s*range\s+(\w+)\s*\{', body):
            var = m.group(1)
            decl = re.search(r'(?<![\w.])' + re.escape(var) + r'\s*:?=\s*(?:\[\]|map\[[^\]]*\])\*?[\w.]*'
                             r'(?:\s*(?:struct|interface)\s*\{[^{}]*\})?\s*\{\s*\}', body)
            if decl and decl.start() < m.start() and not re.search(
                    r'(?<![\w.])' + re.escape(var) + r'\s*=\s*append\(|(?<![\w.])' + re.escape(var) + r'\[[^\]]+\]\s*=', body):
                add('unreached_assertion', 'empty_table', [tid], line_at(text, boff + m.start()),
                    f'the table {var} is declared empty and ranged over, so the checks in the loop never run', {})
                break
        if not fails_in(body, [tvar], pkg):
            logs = re.search(r'(?<![\w.])' + re.escape(tvar) + r'\.(?:Log|Logf)\(', body)
            if logs:
                add('no_assertion', 'log_only', [tid], line_at(text, boff + logs.start()),
                    f'{name} only logs with {tvar}.Log/Logf; nothing fails the test (no {tvar}.Error, Fatal or Fail)', {})
            else:
                add('no_assertion', 'no_assertion', [tid], f['line'],
                    f'{name} has no failure path: no {tvar}.Error/Fatal/Fail, no assertion library call, no panic and no '
                    'helper that fails', {})


def module_dir(root: Path, rel: str) -> str:
    mod = module_of(root, PurePosixPath(rel).parent.as_posix())
    return (mod[1] or '.') if mod else '.'


# ---------------------------------------------------------------- neutralising
def zero_return(results: str) -> str:
    """The return statement for a neutral body: bare for named results, else *new(T) per result type."""
    r = results.strip()
    if not r:
        return 'return'
    if r.startswith('('):
        inner = r[1:-1].strip()
        parts = split_top(inner)
        if not parts:
            return 'return'
        if any(re.match(r'^(?!chan\b)\w+\s+\S', p) for p in parts):
            return 'return'                               # named results: (n int, err error), (a, b int)
        return 'return ' + ', '.join(f'*new({p})' for p in parts)
    return f'return *new({r})'


def patch(text: str, spec: str, variant: str) -> str | None:
    """Replace the body of Func or Type.Method in a Go file; None when it is not found. Imports the file no longer uses
    become blank imports so the package still builds."""
    masked = mask(text)
    recv, _, name = spec.rpartition('.')
    target = None
    for f in functions(text, masked):
        if f['name'] == name and (f['recv'] or '') == recv:
            target = f
            break
    if target is None:
        return None
    results = masked[target['params_close'] + 1:target['brace']]
    eol = '\r\n' if '\r\n' in text else '\n'
    stmt = zero_return(results) if variant == 'return' else f'panic("{MARK}")'
    new = text[:target['brace']] + '{' + eol + f'\t{stmt} // {MARK}' + eol + '}' + text[target['end'] + 1:]
    return fix_imports(new)


def fix_imports(text: str) -> str:
    masked = mask(text)
    imps = imports(text, masked)
    if not imps:
        return text
    last = max(i['end'] for i in imps)
    code = masked[last:]
    out = text
    for i in sorted(imps, key=lambda x: -x['start']):    # from the end, so earlier offsets stay valid
        if i['alias'] in ('_', '.') or re.search(r'(?<![\w.])' + re.escape(i['alias']) + r'\.', code):
            continue
        spec = re.sub(r'^([ \t]*)(?:\w+[ \t]+)?', r'\1_ ', out[i['start']:i['end']], count=1)
        out = out[:i['start']] + spec + out[i['end']:]
    return out


def find_target(root: Path, spec: str) -> tuple[Path, str] | None:
    """'<dir>:<Func>' or '<dir>:<Type>.<Method>' -> (file, 'Func' / 'Type.Method') among the dir's non-test files."""
    d, _, fn = spec.partition(':')
    base = root / d if d not in ('', '.') else root
    recv, _, name = fn.rpartition('.')
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for n in names:
        if not n.endswith('.go') or n.endswith('_test.go'):
            continue
        text = (base / n).read_text(encoding='utf-8', errors='replace')
        for f in functions(text, mask(text)):
            if f['name'] == name and (f['recv'] or '') == recv:
                return base / n, fn
    return None


# ---------------------------------------------------------------- go test -json
def parse_json_events(text: str) -> tuple[list[dict], bool, str]:
    """(cases, build_failed, error) from `go test -json` output. A package that fails to build (a build-fail action,
    or a package fail with `[build failed]` / `[setup failed]`) has no test results: the run errored."""
    cases, build_failed, out_lines = {}, False, []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            if line:
                out_lines.append(line)
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        act, test = ev.get('Action'), ev.get('Test')
        if act == 'build-fail' or (act in ('output', 'build-output') and re.search(r'\[(?:build|setup) failed\]',
                                                                                   ev.get('Output', ''))):
            build_failed = True
        if act == 'output' and ev.get('Output'):
            out_lines.append(ev['Output'].rstrip())
        if test and act in ('pass', 'fail', 'skip'):
            cases[(ev.get('Package'), test)] = {'name': test, 'classname': ev.get('Package'),
                                                'status': {'pass': 'passed', 'fail': 'failed', 'skip': 'skipped'}[act],
                                                'time': ev.get('Elapsed')}
    error = 'the package did not build (go test reported [build failed]); no test ran' if build_failed else ''
    if not cases and not build_failed:
        error = 'go test -json reported no test results (nothing matched, or the run failed before the tests)'
    return list(cases.values()), build_failed, error


def record(runs_file: Path, events: str, label: str, prefix: str, command: str, exit_code: int | None,
           duration: float = 0.0, tests: list[str] | None = None) -> dict:
    """Append one run (and its cases) to runs.json in the shape run_tests.py writes."""
    data = json.loads(runs_file.read_text(encoding='utf-8')) if runs_file.exists() else {'runs': [], 'cases': []}
    ids = {r['id'] for r in data['runs']}
    k = 1
    while f'{prefix}-{k}' in ids:
        k += 1
    rid = f'{prefix}-{k}'
    cases, build_failed, error = parse_json_events(events)
    tot = {'tests': len(cases), 'passed': sum(c['status'] == 'passed' for c in cases),
           'failed': sum(c['status'] == 'failed' for c in cases), 'error': 0,
           'skipped': sum(c['status'] == 'skipped' for c in cases)}
    run = {'id': rid, 'command': command, 'exit_code': exit_code, 'junit_paths': [],
           'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'totals': tot, 'file_hashes': {},
           'notes': ([f'Errored: {error}. Treat this run as not having run the tests.'] if error else []),
           'duration_s': round(duration, 3), 'timed_out': False, 'errored': bool(error), 'error': error or None,
           'label': label, 'preset': 'go-test-json', 'tests_selected': tests or []}
    data['runs'].append(run)
    data['cases'] += [{'run_id': rid, 'id': f'{c["classname"]}::{c["name"]}', 'name': c['name'],
                       'classname': c['classname'], 'file': None, 'status': c['status'], 'failure_type': None,
                       'signals': [], 'message_excerpt': '', 'time': c['time']} for c in cases]
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    runs_file.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return run


def run_go(cwd: Path, package: str, run_pattern: str, runs_file: Path, label: str, prefix: str,
           env: dict | None = None, timeout: int = 600) -> dict:
    """Run one package's matching tests with `go test -json -count=1 -run <pattern>` and record the run."""
    cmd = ['go', 'test', '-json', '-count=1', '-run', run_pattern, package]
    t0 = time.monotonic()
    try:
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', errors='replace',
                             env={**os.environ, **(env or {})}, timeout=timeout)
        out, code = (res.stdout or '') + '\n' + (res.stderr or ''), res.returncode
    except FileNotFoundError:
        out, code = '', None
    except subprocess.TimeoutExpired as exc:
        out, code = (exc.stdout or '') if isinstance(exc.stdout, str) else '', None
    shown = f"go test -json -count=1 -run '{run_pattern}' {package}"
    run = record(runs_file, out, label, prefix, shown, code, time.monotonic() - t0, [f'{package} -run {run_pattern}'])
    if code is None and not run['errored']:
        run['errored'] = True
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('record', help='record a saved go test -json output as a run')
    r.add_argument('--json', type=Path, required=True)
    r.add_argument('--command', default='go test -json')
    r.add_argument('--exit-code', type=int)
    g = sub.add_parser('run', help='run go test -json for one package and record it')
    g.add_argument('--cwd', type=Path, required=True)
    g.add_argument('--package', required=True)
    g.add_argument('--run', dest='pattern', required=True)
    g.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V')
    for p in (r, g):
        p.add_argument('--out', type=Path, required=True)
        p.add_argument('--label', required=True)
        p.add_argument('--run-prefix', default='G')
    args = ap.parse_args(argv)
    if args.cmd == 'record':
        run = record(args.out, args.json.read_text(encoding='utf-8', errors='replace'), args.label, args.run_prefix,
                     args.command, args.exit_code)
    else:
        env = dict(kv.split('=', 1) for group in args.env for kv in group)
        run = run_go(args.cwd, args.package, args.pattern, args.out, args.label, args.run_prefix, env)
    t = run['totals']
    print(f"{run['id']} [{run['label']}] {t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped"
          + (f" ERRORED: {run['error']}" if run['errored'] else ''))
    return 1 if run['errored'] else 0


if __name__ == '__main__':
    sys.exit(main())
