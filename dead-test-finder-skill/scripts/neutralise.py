#!/usr/bin/env python3
"""Neutralise the code a test is meant to check, in a scratch copy, and see whether the test notices (stdlib only).

    python neutralise.py --repo <repo> --candidates candidates.json --test <test id> [--test ...] --run-dir run
        [--tests-file ids.txt] [--target <test id>=<module.function>] [--cwd client=/copy/with/node_modules]
        [--patch fix.diff] [--env K=V ...] [--npm-script test] [--runner-args "..."]

For each test the targets are the functions it calls that come from product code (Python: the
imported names in candidates.json; JavaScript/TypeScript: the names it imports from relative or
alias paths, `@/lib/x` through tsconfig `paths` or a vite/vitest `alias`, and calls or renders as
JSX). A JavaScript target is followed through default imports (`export default function`,
`export default Name`) and re-exports (`export { x } from './y'`, `export * from './y'`) to the
function's definition. The test runs once unchanged (`original`), then once per target and variant:

    return   the body becomes `return None` (Python) or `return undefined` (JS/TS)
    raise    the body raises RuntimeError / Error("neutralised by dead-test-finder")

Runs go through run_tests.py, appended to <run-dir>/runs.json with labels like
"neutralise gst return". A test that passes with the `return` variant can't fail on that code;
whether it passes with `raise` says whether it at least checks that nothing raises. When
run_tests.py records no run, its own error output is shown and kept in the result (`errors`),
never replaced by a missing-file error. --env, --npm-script and --runner-args are passed to
run_tests.py (a fake DATABASE_URL for a setup file, the repo's `npm test` flags).

--patch applies a proposed test fix (a unified diff with repo-relative paths) to the scratch copy
first, so a strengthening fix can be proved: it passes on the original code and fails when the
code is neutralised. Test ids may then name tests that only exist after the patch.

Python packages are copied once per invocation to a temporary directory outside the repo (a
root-level package too), leaving out VCS data, caches, virtualenvs and node_modules, the run
directory, and anything matching --copy-ignore (for example large images the tests never open).
Every test in the invocation shares that copy. A --cwd directory is used as given (it must be a
scratch copy, and can be reused across invocations). Every file patched in either is restored
byte for byte (line endings included) straight after its run and checked against its original
SHA-256; the script stops if a restore doesn't match. neutralisations.json records each copy and
what it left out. The repository's tree hash is checked before and after; the script exits 3 if
it changed. Playwright tests drive a running app and are not neutralised (recorded as not_run with
the reason).

A parametrised test can be named by its function (`tests/test_x.py::test_y`: every case runs,
and the test passes only if every case passes) or by one case (`tests/test_x.py::test_y[a-1]`).
"""
from __future__ import annotations
import argparse, ast, fnmatch, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests, scan_tests, unified_diff  # noqa: E402

MARK = 'neutralised by dead-test-finder'
# Never copied into the scratch copy: VCS data, caches and installed dependencies. --copy-ignore adds more.
DEFAULT_IGNORE = ['.git', '.hg', '.svn', '__pycache__', '*.pyc', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.tox', '.nox',
                  '.venv', 'venv', 'node_modules']
JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts')


class RestoreError(Exception):
    pass


def copy_package(src: Path, dest: Path, patterns: list[str], run_dir: Path) -> int:
    """Copy src to dest once, leaving out names matching the glob patterns (a pattern with a '/' matches the path
    relative to src) and the run directory when it sits inside src. Returns the number of files copied. Paths go
    through the long-path prefix on Windows, so a deep tree copies instead of failing at 260 characters."""
    count = 0
    lsrc = run_tests.long_path(src)
    skip_run = os.path.normcase(run_tests.long_path(run_dir))

    def ignore(dirpath: str, names: list[str]) -> set[str]:
        nonlocal count
        rel_dir = os.path.relpath(dirpath, lsrc).replace('\\', '/')
        rel_dir = '' if rel_dir == '.' else rel_dir
        out = set()
        for n in names:
            rel = f'{rel_dir}/{n}' if rel_dir else n
            full = os.path.join(dirpath, n)
            if os.path.normcase(full) == skip_run or any(fnmatch.fnmatch(n, p) or ('/' in p and fnmatch.fnmatch(rel, p.strip('/')))
                                                         for p in patterns):
                out.add(n)
            elif os.path.isfile(full):
                count += 1
        return out
    try:
        shutil.copytree(lsrc, run_tests.long_path(dest), ignore=ignore)
    except shutil.Error as exc:
        first = exc.args[0][0] if exc.args and exc.args[0] else exc
        raise SystemExit(f'could not copy {src} to the scratch copy: {first}. On Windows, put the repository or the '
                         '--cwd copy at a short path (for example C:\\t\\repo)')
    return count


def tree_hash(root: Path, skip: Path) -> str:
    """A hash of every file under the repo (line endings normalised), leaving out the run directory when it sits inside
    it; long paths are read through the long-path prefix, and an unreadable file stops the script with its name."""
    h = hashlib.sha256()
    errors: list[str] = []
    for p in sorted(run_tests.iter_files(root, {skip.resolve()}, errors), key=lambda p: p.relative_to(root).as_posix()):
        try:
            data = run_tests.read_bytes(p)
        except OSError as exc:
            raise SystemExit(f'cannot read {p.relative_to(root).as_posix()} to hash the repository ({exc.strerror or exc})')
        h.update(p.relative_to(root).as_posix().encode('utf-8') + b'\0' + data.replace(b'\r\n', b'\n') + b'\0')
    if errors:
        raise SystemExit('cannot hash the repository: ' + '; '.join(errors[:3]))
    return h.hexdigest()


def py_target_file(pkg_dir: Path, dotted: str) -> tuple[Path, str] | None:
    """'ledger.tax.gst' -> (pkg_dir/ledger/tax.py, 'gst')."""
    parts = dotted.split('.')
    for cut in range(len(parts) - 1, 0, -1):
        base = pkg_dir.joinpath(*parts[:cut])
        for cand in (base.with_suffix('.py'), base / '__init__.py', pkg_dir / 'src' / Path(*parts[:cut]).with_suffix('.py')):
            if cand.is_file():
                return cand, parts[cut]
    return None


def py_patch(text: str, func: str, variant: str) -> str | None:
    tree = ast.parse(text)
    lines = text.split('\n')
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            first = node.body[0]
            indent = ' ' * first.col_offset
            stmt = 'return None' if variant == 'return' else f'raise RuntimeError("{MARK}")'
            eol = '\r' if lines[first.lineno - 1].endswith('\r') else ''
            return '\n'.join(lines[:first.lineno - 1] + [f'{indent}{stmt}  # {MARK}{eol}'] + lines[node.end_lineno:])
    return None


# ---------------------------------------------------------------- JavaScript / TypeScript targets
def resolve_module(from_file: Path, spec: str, pkg_dir: Path) -> Path | None:
    """The file a relative or alias import names: tsconfig/jsconfig `paths` and vite/vitest `alias` of the package."""
    bases = []
    if spec.startswith('.'):
        bases.append(from_file.parent / spec)
    else:
        for pref, targets in scan_tests.ts_aliases(pkg_dir):
            if pref and spec.startswith(pref):
                bases += [pkg_dir / t / spec[len(pref):] for t in targets]
                break
    for base in bases:
        for cand in [base] + [Path(str(base) + e) for e in JS_EXT] + [base / f'index{e}' for e in JS_EXT]:
            if cand.is_file():
                return cand.resolve()
    return None


GENERIC = r'(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)'      # type parameters, nested two deep (<T extends Record<K, V>>)
DECL = r'(?:\bexport\s+)?(?:\bdefault\s+)?(?:\basync\s+)?\bfunction\s*\*?\s*{name}\s*' + GENERIC + r'?\s*\('
ARROW = r'\b(?:const|let|var)\s+{name}\s*(?::[^=;]+)?=\s*(?:async\s*)?(?:' + GENERIC + r'\s*)?(?=\(|[A-Za-z_$][\w$]*\s*=>)'


def find_definition(file: Path, name: str, pkg_dir: Path, depth: int = 0) -> tuple[Path, str] | None:
    """(file, local name or 'default') where the exported name is defined as a function, following re-exports."""
    if depth > 6 or not file.is_file():
        return None
    text = file.read_text(encoding='utf-8', errors='replace')
    code, lits = scan_tests.js_lex(text)

    def spec_at(q: int) -> str | None:
        return text[q + 1:lits[q][1] - 1] if q in lits else None
    if name == 'default':
        if re.search(r'\bexport\s+default\s+(?:async\s+)?function\b', code):
            return file, 'default'
        m = re.search(r'\bexport\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*(?:$|\n)', code)
        if m and m.group(1) not in ('class', 'function', 'async'):
            return find_definition(file, m.group(1), pkg_dir, depth + 1) or None
    else:
        if re.search(DECL.format(name=re.escape(name)), code) or re.search(ARROW.format(name=re.escape(name)), code):
            return file, name
    # export { a as b } (local) and export { a as b } from './x'
    for m in re.finditer(r'\bexport\s*\{([^}]*)\}\s*(?:from\s*(["\']))?', code):
        for part in m.group(1).split(','):
            orig, _, alias = part.strip().partition(' as ')
            orig, alias = orig.strip(), (alias or orig).strip()
            if alias != name or not orig:
                continue
            if m.group(2):
                spec = spec_at(m.end() - 1)
                target = resolve_module(file, spec, pkg_dir) if spec else None
                if target:
                    return find_definition(target, orig, pkg_dir, depth + 1)
            elif orig != name:
                return find_definition(file, orig, pkg_dir, depth + 1)
    if name != 'default':
        for m in re.finditer(r'\bexport\s*\*\s*from\s*(["\'])', code):
            spec = spec_at(m.end() - 1)
            target = resolve_module(file, spec, pkg_dir) if spec else None
            found = find_definition(target, name, pkg_dir, depth + 1) if target else None
            if found:
                return found
    return None


def js_target(test_file: Path, name: str, pkg_dir: Path) -> tuple[Path, str] | None:
    """The definition of a name the test file imports (named, default or namespace member a.b)."""
    text = test_file.read_text(encoding='utf-8', errors='replace')
    code, lits = scan_tests.js_lex(text)
    imports = scan_tests.js_imports(text, code, lits)
    local, _, member = name.partition('.')
    if local not in imports:
        return None
    spec, orig = imports[local]
    target = resolve_module(test_file, spec, pkg_dir)
    if not target:
        return None
    if orig == '*':
        return find_definition(target, member, pkg_dir) if member else None
    return find_definition(target, orig, pkg_dir)


def body_after_params(code: str, close: int) -> int | None:
    """Index of the '{' opening a function body after the parameter list closing at close (skips a return type)."""
    j = close + 1
    angle = 0
    while j < len(code):
        c = code[j]
        if c == '<':
            angle += 1
        elif c == '>' and angle:
            angle -= 1
        elif c == '{' and angle == 0:
            k = j - 1
            while k > close and code[k].isspace():
                k -= 1
            if code[k] not in ':|&,<(':
                return j
            j = scan_tests.match_code(code, j)           # an object type in the return type
        elif c in ';=' and angle == 0 and not code.startswith('=>', j):
            return None
        j += 1
    return None


def js_patch(text: str, func: str, variant: str) -> str | None:
    """The text with func's body replaced by a neutral one (block or expression arrow body)."""
    code = scan_tests.js_mask(text)
    new_body = '{ return undefined; }' if variant == 'return' else f'{{ throw new Error("{MARK}"); }}'
    if func == 'default':
        m = re.search(r'\bexport\s+default\s+(?:async\s+)?function\s*\*?\s*[\w$]*\s*' + GENERIC + r'?\s*\(', code)
    else:
        m = re.search(DECL.format(name=re.escape(func)), code)
    if m:
        open_i = body_after_params(code, scan_tests.match_code(code, m.end() - 1))
        if open_i is None:
            return None
        close = scan_tests.match_code(code, open_i)
        return text[:open_i] + new_body + f' /* {MARK} */' + text[close + 1:]
    m = re.search(ARROW.format(name=re.escape(func)), code)
    if not m:
        return None
    j = m.end()
    if code[j] == '(':
        j = scan_tests.match_code(code, j) + 1
    arrow = code.find('=>', j)
    if arrow < 0:
        return None
    k = arrow + 2
    while k < len(code) and code[k].isspace():
        k += 1
    if code[k] == '{':
        close = scan_tests.match_code(code, k)
        return text[:k] + new_body + f' /* {MARK} */' + text[close + 1:]
    # an expression body: up to the end of the expression (a ; or , or closing bracket at depth 0, or a line end
    # that doesn't continue the expression)
    depth, e = 0, k
    while e < len(code):
        c = code[e]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ';,':
            break
        elif depth == 0 and c == '\n':
            rest = code[e:].lstrip()
            prev = code[:e].rstrip()
            if not (rest[:1] in tuple('.?:&|+-*/') or prev[-1:] in tuple('=?:&|+-*/,(')):
                break
        e += 1
    expr = '(undefined)' if variant == 'return' else f'(() => {{ throw new Error("{MARK}"); }})()'
    return text[:k] + expr + f' /* {MARK} */' + text[e:]


# ---------------------------------------------------------------- runs
def base_id(test_id: str) -> str:
    """tests/test_x.py::test_y[case-1] -> tests/test_x.py::test_y. Python ids only: a JS test name may end in ']'."""
    return re.sub(r'\[.*\]$', '', test_id) if '.py::' in test_id else test_id


def case_status(cases: list[dict], run_id: str, want: str, python: bool) -> str:
    """The test's status in a run. A Python id without a [case] matches every parametrised case of the function:
    failed or error if any case did, passed when they all ran and passed."""
    mine = [c for c in cases if c['run_id'] == run_id]
    if python:
        name = want.split('::')[-1]
        exact = [c['status'] for c in mine if c['name'] == name]
        if exact:
            return exact[0]
        params = [c['status'] for c in mine if '[' not in name and c['name'].startswith(name + '[')]
        if params:
            for s in ('error', 'failed'):
                if s in params:
                    return s
            return 'passed' if 'passed' in params else params[0]
        return 'not_found'
    for c in mine:
        if c['name'] == want or c['name'] == want.split(' > ')[-1]:
            return c['status']
    return 'not_found'


def run_test(defn: dict, work: Path, run_dir: Path, label: str, prefix: str, extra: list[str]) -> tuple[str, str, str | None]:
    """(run id, test status, error text when run_tests.py recorded no run)."""
    rel = defn['file'][len(defn['package']) + 1:] if defn['package'] != '.' else defn['file']
    if defn['lang'] == 'python':
        args = ['--preset', 'pytest', '--tests', rel + '::' + defn['run_id'].split('::', 1)[1]]
    else:
        args = ['--preset', 'vitest', '--tests', rel]
    runs_file = run_dir / 'runs.json'
    before = {r['id'] for r in json.loads(runs_file.read_text(encoding='utf-8'))['runs']} if runs_file.exists() else set()
    res = subprocess.run([sys.executable, str(HERE / 'run_tests.py'), *args, '--cwd', str(work), '--out', str(runs_file),
                          '--label', label, '--run-prefix', prefix, *extra], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(runs_file.read_text(encoding='utf-8')) if runs_file.exists() else {'runs': [], 'cases': []}
    new = [r for r in data['runs'] if r['id'] not in before]
    if not new:
        err = run_tests.redact_home((res.stderr or res.stdout or f'run_tests.py exited {res.returncode}').strip())[-1500:]
        print(f'run_tests.py recorded no run (exit {res.returncode}):\n{err}', file=sys.stderr)
        return 'none', 'not_run', err
    rid = new[-1]['id']
    return rid, case_status(data['cases'], rid, defn['run_id'].split('::', 1)[1], defn['lang'] == 'python'), None


def localise(text: str, prefix: str) -> str:
    """Make a repo-relative diff relative to the package directory."""
    if not prefix:
        return text
    p = re.escape(prefix)
    text = re.sub(r'^(---|\+\+\+) ([ab]/)' + p, r'\1 \2', text, flags=re.M)
    text = re.sub(r'^diff --git a/' + p + r'(\S+) b/' + p, r'diff --git a/\1 b/', text, flags=re.M)
    return re.sub(r'^(rename (?:from|to) )' + p, r'\1', text, flags=re.M)


def write_back(path: Path, original: bytes | None) -> None:
    """Restore a file byte for byte (or remove it when it didn't exist) and check its SHA-256."""
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(original)
    if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(original).hexdigest():
        raise RestoreError(f'{path} did not restore to its original bytes')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--test', action='append', default=[])
    ap.add_argument('--tests-file', type=Path, help='test ids, one per line (ids with quotes, > or $ need no shell quoting)')
    ap.add_argument('--target', action='append', default=[], help='TEST_ID=module.function (or JS function name)')
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--cwd', action='append', default=[], help='PACKAGE=DIR scratch copy to patch in place (JS with node_modules)')
    ap.add_argument('--patch', type=Path, help='unified diff (repo-relative paths) applied to the scratch copy first')
    ap.add_argument('--copy-ignore', action='append', default=[], metavar='GLOB',
                    help='leave matching files or directories out of the scratch copy ("*.jpg", "images", "data/raw/*"); '
                         'only what the tests never read')
    ap.add_argument('--env', action='append', default=[], metavar='K=V', help='passed to run_tests.py (repeat)')
    ap.add_argument('--npm-script', help='passed to run_tests.py: reuse package.json\'s script (its env and runner flags)')
    ap.add_argument('--runner-args', help='passed to run_tests.py: extra runner flags')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    tests = list(args.test)
    if args.tests_file:
        tests += [ln.strip() for ln in args.tests_file.read_text(encoding='utf-8').splitlines() if ln.strip() and not ln.startswith('#')]
    if not tests:
        ap.error('name at least one test with --test or --tests-file')
    extra = (['--env', *args.env] if args.env else []) + (['--npm-script', args.npm_script] if args.npm_script else []) + \
        (['--runner-args', args.runner_args] if args.runner_args else [])
    root = args.repo.resolve()
    defs = {d['id']: d for d in json.loads(args.candidates.read_text(encoding='utf-8'))['definitions']}
    packages = sorted({d['package'] for d in defs.values()}, key=len, reverse=True)
    overrides = dict(x.split('=', 1) for x in args.cwd)
    forced: dict[str, list[str]] = {}
    for t in args.target:
        tid, _, tgt = t.rpartition('=')
        forced.setdefault(tid, []).append(tgt)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or args.run_dir / 'neutralisations.json'
    prev = json.loads(out_path.read_text(encoding='utf-8')) if out_path.exists() else {}
    results, prev_copies = prev.get('results', []), prev.get('copies', [])
    runs_file = args.run_dir / 'runs.json'
    n = len(json.loads(runs_file.read_text(encoding='utf-8'))['runs']) if runs_file.exists() else 0
    h_before = tree_hash(root, args.run_dir)
    restore: dict[Path, bytes | None] = {}
    scratch = tempfile.mkdtemp(prefix='dtf-')
    patch_name = args.patch.as_posix() if args.patch else None
    ignore = DEFAULT_IGNORE + args.copy_ignore
    works: dict[str, Path] = {}                              # one copy per package for the whole invocation
    copies = []
    try:
        for tid in tests:
            d = defs.get(tid) or defs.get(base_id(tid))
            if not d:
                if not args.patch:
                    print(f'unknown test id {tid}', file=sys.stderr)
                    return 2
                file = tid.split('::')[0]
                pkg = next((p for p in packages if p == '.' or file.startswith(p + '/')), '.')
                d = {'id': tid, 'file': file, 'package': pkg, 'lang': 'python' if file.endswith('.py') else 'typescript', 'calls': []}
            d = {**d, 'run_id': tid}                         # run the id as given: a [case] runs that case only
            prefix = d['package'] + '/' if d['package'] != '.' else ''
            entry = {'test_id': tid, 'patch': patch_name, 'targets': [], 'note': None}
            if d.get('framework') == 'playwright':
                entry.update(original={'run_id': 'none', 'test_status': 'not_run'}, outcome='not_run', checks_no_exception=False,
                             not_run_reason='a Playwright test drives a running app; neutralise.py does not run it')
                results = [r for r in results if not (r['test_id'] == tid and r.get('patch') == patch_name)] + [entry]
                print(f'{tid}: not run (Playwright)')
                continue
            work = works.get(d['package'])
            if work is None:
                if d['package'] in overrides:
                    work = Path(overrides[d['package']]).resolve()
                    copies.append({'package': d['package'], 'work': 'given with --cwd', 'files': None, 'ignored': []})
                else:
                    src = root / d['package'] if prefix else root
                    work = Path(tempfile.mkdtemp(prefix='dtf-neutralise-', dir=scratch)) / (PurePosixPath(d['package']).name or 'repo')
                    count = copy_package(src, work, ignore, args.run_dir.resolve())
                    copies.append({'package': d['package'], 'work': 'temporary copy', 'files': count, 'ignored': ignore})
                if args.patch:
                    text = args.patch.read_text(encoding='utf-8')
                    for f in unified_diff.parse_patch(text):
                        for rel in {f['old'], f['new']} - {None}:
                            if rel.startswith(prefix):
                                p = work / rel[len(prefix):]
                                restore.setdefault(p, p.read_bytes() if p.is_file() else None)
                    unified_diff.apply_patch(localise(text, prefix), work)
                works[d['package']] = work
            n += 1
            rid, status, err = run_test(d, work, args.run_dir, 'original' + (' (patched)' if args.patch else ''), f'N{n}', extra)
            entry['original'] = {'run_id': rid, 'test_status': status}
            if err:
                entry['errors'] = [err]
            targets = forced.get(tid) or d['calls']
            if not targets:
                entry['note'] = 'no product function to neutralise (the test calls nothing it imports)'
            for tgt in targets:
                if d['lang'] == 'python':
                    loc, patcher = py_target_file(work, tgt), py_patch
                else:
                    loc, patcher = js_target(work / d['file'][len(prefix):], tgt, work), js_patch
                if not loc:
                    entry['targets'].append({'target': tgt, 'file': None, 'variants': [], 'note': 'definition not found'})
                    continue
                path, func = loc
                original = path.read_bytes()
                text = original.decode('utf-8')
                tres = {'target': tgt, 'file': prefix + path.relative_to(work.resolve()).as_posix() if path.is_relative_to(work.resolve())
                        else prefix + Path(os.path.relpath(path, work)).as_posix(), 'variants': []}
                for variant in ('return', 'raise'):
                    patched = patcher(text, func, variant)
                    if patched is None:
                        tres['note'] = f'{func} not found'
                        break
                    path.write_bytes(patched.encode('utf-8'))
                    try:
                        n += 1
                        rid, st, err = run_test(d, work, args.run_dir, f'neutralise {func} {variant}' +
                                                (' (patched)' if args.patch else ''), f'N{n}', extra)
                    finally:
                        write_back(path, original)
                    if err:
                        entry.setdefault('errors', []).append(err)
                    tres['variants'].append({'variant': variant, 'run_id': rid, 'test_status': st})
                entry['targets'].append(tres)
            ran = [t for t in entry['targets'] if t['variants']]
            passed_return = [t for t in ran if any(v['variant'] == 'return' and v['test_status'] == 'passed' for v in t['variants'])]
            entry['outcome'] = ('not_run' if not ran else 'passed_neutralised' if len(passed_return) == len(ran) else
                                'failed_neutralised')
            entry['checks_no_exception'] = any(v['variant'] == 'raise' and v['test_status'] in ('failed', 'error')
                                               for t in ran for v in t['variants'])
            results = [r for r in results if not (r['test_id'] == tid and r.get('patch') == patch_name)] + [entry]
            print(f'{tid}{" (patched)" if patch_name else ""}: original {status}, {entry["outcome"]}' +
                  ''.join(f'\n  {t["target"]}: ' + (', '.join(f'{v["variant"]} -> {v["test_status"]}' for v in t['variants'])
                                                    or t.get('note', ''))
                          for t in entry['targets']))
    except RestoreError as exc:
        print(f'stopped: {exc}', file=sys.stderr)
        return 4
    finally:
        for p, data in restore.items():
            write_back(p, data)
        shutil.rmtree(scratch, ignore_errors=True)
    h_after = tree_hash(root, args.run_dir)
    out_path.write_text(json.dumps({'tree_hash_before': h_before, 'tree_hash_after': h_after, 'copies': prev_copies + copies,
                                    'results': results}, indent=2) + '\n',
                        encoding='utf-8')
    if h_before != h_after:
        print('the repository changed during neutralisation', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
