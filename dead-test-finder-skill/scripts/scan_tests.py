#!/usr/bin/env python3
"""Find every test definition and the ones that can't fail, never run, or are skipped (stdlib only).

    python scan_tests.py <repo> --out candidates.json

Python test files are parsed with `ast`; JavaScript and TypeScript test files with patterns and
bracket matching (`it`/`test` inside `describe`). Every test function or case found is a
definition, whether or not the framework would collect it; collect_check.py then compares the
definitions with what the framework really collects.

Candidate categories: never_runs (a file outside the collect patterns, a stray .only),
no_assertion, tautology, unreached_assertion, swallowed_failure, async_not_awaited, mock_only,
skipped and duplicate (normalised AST similarity >= the threshold in data/rules.json, within one
package, and for Python with the same literal values: same-shape tests with different values are
input cases, counted in `near_duplicates` rather than flagged). Each candidate cites the test ids,
file and line, and why it was flagged.
"""
from __future__ import annotations
import argparse, ast, copy, difflib, fnmatch, json, os, re, sys
from collections import Counter
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))
PY, JS = RULES['python'], RULES['javascript']
ASSERT_CALL = re.compile(PY['assert_calls'])
KEY = re.compile(RULES['ticket_key'])
TEST_DIR_PARTS = {'tests', 'test', '__tests__', 'spec', 'e2e', 'testing'}
# Directories that hold only test support code wherever they sit (helpers/ and fixtures/ count only under a test dir).
SUPPORT_DIR_PARTS = {'__mocks__', '__fixtures__', 'test-utils', 'test_utils', 'testutils', 'test-helpers', 'test_helpers'}
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle',
             'build.gradle.kts', 'composer.json', 'Gemfile', 'mix.exs', 'pubspec.yaml')
TEST_RUNNER_DEPS = ('@playwright/test', 'playwright', 'cypress', 'vitest', 'jest', 'mocha', 'webdriverio', '@wdio/cli',
                    'detox', 'testcafe', 'pytest')
_TEST_DIR: dict[str, bool] = {}


def test_layout_manifest(d: Path) -> bool:
    """A directory with a manifest that is still a test layout: a package.json that publishes nothing (no main, module,
    exports, bin or types) and depends on a test runner (an apps/x/e2e/ Playwright package of spec files)."""
    try:
        pj = json.loads((d / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if any(k in pj for k in ('main', 'module', 'exports', 'bin', 'types', 'typings')):
        return False
    deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
    return any(k in deps for k in TEST_RUNNER_DEPS)


def is_test_dir(root: Path, rel_dir: str) -> bool:
    """A directory named e2e, test, tests, spec, __tests__ or testing is test code only when it holds no package
    manifest that makes it a product package (packages/e2e/ with a package.json that publishes the product), or when the
    manifest is a test layout's (see test_layout_manifest). The same rule as the shared diff_scope.py classification."""
    name = PurePosixPath(rel_dir).name
    if name not in TEST_DIR_PARTS:
        return False
    key = f'{root}|{rel_dir}'
    if key not in _TEST_DIR:
        d = root / rel_dir
        has = [m for m in MANIFESTS if (d / m).is_file()]
        _TEST_DIR[key] = not has or (has == ['package.json'] and test_layout_manifest(d))
    return _TEST_DIR[key]


def in_test_dir(root: Path, rel: str, parts: frozenset | set = frozenset()) -> bool:
    """rel (a file, repo-relative) sits under a test directory by the rule above; a directory named in parts
    (__mocks__, test-utils) counts too."""
    dirs = PurePosixPath(rel).parts[:-1]
    for i, part in enumerate(dirs):
        if part in TEST_DIR_PARTS:
            if is_test_dir(root, '/'.join(dirs[:i + 1])):
                return True
        elif part in parts:
            return True
    return False


def is_test_support(root: Path, rel: str) -> bool:
    """A test file or test support module (a helper, fixture or mock): never the code under test."""
    name = PurePosixPath(rel).name
    if any(fnmatch.fnmatch(name, pat) for pat in JS['test_files']) or re.search(r'\.(test|spec|e2e)\.[cm]?[jt]sx?$', name) \
            or re.match(r'(test_.*|.*_test|conftest)\.py$', name):
        return True
    return in_test_dir(root, rel, SUPPORT_DIR_PARTS)

# scan_tests can parse only Python and JavaScript/TypeScript test suites. We recognise other common
# languages purely to warn when the repository's dominant language is one we cannot analyse, so that a
# handful of incidental supported files is never presented as a real dead-test analysis of the repo.
SUPPORTED_LANG = 'Python / JavaScript / TypeScript'
LANG_BY_EXT = {
    '.py': SUPPORTED_LANG, '.pyi': SUPPORTED_LANG,
    '.js': SUPPORTED_LANG, '.jsx': SUPPORTED_LANG, '.mjs': SUPPORTED_LANG, '.cjs': SUPPORTED_LANG,
    '.ts': SUPPORTED_LANG, '.tsx': SUPPORTED_LANG, '.mts': SUPPORTED_LANG, '.cts': SUPPORTED_LANG,
    '.rs': 'Rust',
    '.go': 'Go',
    '.java': 'Java', '.kt': 'Kotlin', '.kts': 'Kotlin',
    '.rb': 'Ruby',
    '.php': 'PHP',
    '.cs': 'C#', '.fs': 'F#',
    '.c': 'C', '.cpp': 'C++', '.cc': 'C++', '.cxx': 'C++', '.hpp': 'C++', '.hh': 'C++',
    '.swift': 'Swift',
    '.scala': 'Scala',
    '.dart': 'Dart',
    '.ex': 'Elixir', '.exs': 'Elixir',
    '.clj': 'Clojure', '.cljs': 'Clojure',
    '.erl': 'Erlang',
    '.hs': 'Haskell',
    '.lua': 'Lua',
    '.r': 'R', '.R': 'R',
    '.pl': 'Perl', '.pm': 'Perl',
    '.groovy': 'Groovy',
    '.ml': 'OCaml',
    '.jl': 'Julia',
    '.zig': 'Zig',
    '.vb': 'Visual Basic',
}


def language_breakdown(files: list[str]) -> Counter:
    """Count source files per recognised programming language (documentation, config and data files are
    not counted), so scan() can tell whether the dominant language is one it can actually parse."""
    counts: Counter = Counter()
    for rel in files:
        lang = LANG_BY_EXT.get(PurePosixPath(rel).suffix)
        if lang:
            counts[lang] += 1
    return counts


def unsupported_dominant(counts: Counter) -> dict | None:
    """When the repository is dominated by a language scan_tests cannot parse, return the honesty-guard
    payload; otherwise None. The guard fires when the single largest unsupported language has more source
    files than all supported (Python/JS/TS) files combined, so a minority of supported files is never
    presented as a dead-test analysis of the whole repository."""
    supported = counts.get(SUPPORTED_LANG, 0)
    unsupported = {k: v for k, v in counts.items() if k != SUPPORTED_LANG}
    if not unsupported:
        return None
    lang, n = max(unsupported.items(), key=lambda kv: (kv[1], kv[0]))
    if n <= supported:
        return None
    warning = (f'This repository is predominantly {lang} ({n} source files), which scan_tests.py cannot '
               f'analyse: it parses Python and JavaScript/TypeScript test suites only. '
               f'Only {supported} supported (Python/JS/TS) source file(s) were found, so the definitions and '
               f'candidates below cover that small minority and are NOT a dead-test analysis of this '
               f'repository\'s {lang} test suite. Do not present the result as one.')
    return {'warning': warning, 'dominant_unsupported_language': lang, 'dominant_unsupported_files': n,
            'supported_files': supported, 'breakdown': dict(counts.most_common())}


PRUNE = detect_conventions.SKIP_DIRS | {'.git', '.hg', '.svn', 'node_modules', '.next', '.nuxt', 'dist', 'build', 'out',
                                       'coverage', '.venv', 'venv', '__pycache__', '.turbo', '.cache'}


def walk(root: Path) -> list[str]:
    """Every file under root, pruning dependency, build and cache directories before descending into them (a
    node_modules tree is never walked)."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in PRUNE and not d.endswith('.egg-info'))
        rel_dir = Path(dp).relative_to(root).as_posix()
        for f in sorted(fns):
            out.append(f if rel_dir == '.' else f'{rel_dir}/{f}')
    return sorted(out)


_CONFIG: dict[tuple[str, str], dict] = {}


def pytest_config(root: Path, pkg: str) -> dict:
    """python_files / python_functions / python_classes from the package's pytest configuration (read once per package)."""
    key = (str(root), pkg)
    if key not in _CONFIG:
        _CONFIG[key] = _pytest_config(root, pkg)
    return _CONFIG[key]


def _pytest_config(root: Path, pkg: str) -> dict:
    conf = {'python_files': PY['default_python_files'], 'python_functions': PY['default_python_functions'],
            'python_classes': PY['default_python_classes'], 'source': 'pytest defaults'}
    base = root / pkg if pkg != '.' else root
    # pytest reads the first configuration file it finds in the package directory or above it (up to the repo root)
    dirs = [base] + [d for d in base.parents if d == root or root in d.parents]
    for d in dirs:
        for name in ('pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg'):
            p = d / name
            if not p.is_file():
                continue
            text = p.read_text(encoding='utf-8', errors='replace')
            if name == 'pyproject.toml' and '[tool.pytest.ini_options]' not in text:
                continue
            if name in ('tox.ini', 'setup.cfg') and '[pytest]' not in text and '[tool:pytest]' not in text:
                continue
            found = False
            for key in ('python_files', 'python_functions', 'python_classes'):
                m = re.search(rf'^\s*{key}\s*=\s*(.+)$', text, re.M)
                if m:
                    found = True
                    val = m.group(1).strip()
                    conf[key] = re.findall(r'"([^"]+)"|\'([^\']+)\'', val) and \
                        [a or b for a, b in re.findall(r'"([^"]+)"|\'([^\']+)\'', val)] or val.split()
            rel = p.relative_to(root).as_posix()
            conf['source'] = rel if found else f'pytest defaults; {rel} sets no patterns'
            return conf
    return conf


# ---------------------------------------------------------------- Python
def call_name(node: ast.AST) -> str:
    f = node.func if isinstance(node, ast.Call) else node
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ''


def is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        name = call_name(node)
        if ASSERT_CALL.match(name) or name.startswith('assert_'):
            return True
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if isinstance(item.context_expr, ast.Call) and call_name(item.context_expr) in PY['raises_contexts']:
                return True
    return False


def normalise(fn: ast.FunctionDef) -> str:
    """An AST dump with names, attributes and constants replaced, for similarity."""
    tree = copy.deepcopy(fn)
    tree.name = 'f'
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Load)):
            n.id = 'v'
        elif isinstance(n, ast.arg):
            n.arg = 'a'
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            n.value = 's'
        elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            n.value = 0
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def tokens(dump: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", dump)


STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | {'pytest', 'unittest', 'mock', '_pytest', 'hypothesis', 'freezegun',
                                                          'pytest_mock', 'responses', 'httpx', 'requests', 'faker'}


def asserts_in(fn: ast.AST, helpers: dict) -> bool:
    """fn holds an assertion, or calls a helper (helpers: name -> function node) that does, two calls deep at most."""
    seen: set[str] = set()

    def visit(node: ast.AST, depth: int) -> bool:
        for n in ast.walk(node):
            if is_assertion(n):
                return True
            if depth < 2 and isinstance(n, ast.Call):
                name = call_name(n)
                f = n.func
                local = isinstance(f, ast.Name) or (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                                                    and f.value.id in ('self', 'cls'))
                if local and name in helpers and name not in seen:
                    seen.add(name)
                    if visit(helpers[name], depth + 1):
                        return True
        return False
    return visit(fn, 0)


class PyScanner:
    def __init__(self, root: Path, rel: str, pkg: str, conf: dict):
        self.root, self.rel, self.pkg, self.conf = root, rel, pkg, conf
        self.text = (root / rel).read_text(encoding='utf-8', errors='replace')
        self.tree = ast.parse(self.text)
        self.imports = self.imported_names()
        # module-level functions and class methods that are not tests themselves: a test that calls one holding
        # asserts (_test_shape(result), self._check_row(row)) has assertions
        self.helpers: dict[str, ast.AST] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith('test'):
                self.helpers[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith('test'):
                        self.helpers.setdefault(m.name, m)

    def imported_names(self) -> dict[str, str]:
        """local name -> dotted origin for `from x import y` and `import x`."""
        out = {}
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    out[a.asname or a.name] = f'{n.module}.{a.name}'
            elif isinstance(n, ast.Import):
                for a in n.names:
                    out[a.asname or a.name.split('.')[0]] = a.name
        return out

    def matches(self, name: str, pats: list[str]) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in pats)

    def tests(self):
        """(test id, function node, class name) for every test-like function, collectable or not."""
        out = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                out.append((f'{self.rel}::{node.name}', node, None))
            elif isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith('test'):
                        out.append((f'{self.rel}::{node.name}::{m.name}', m, node))
        return out

    def scan(self, add, defs):
        file_ok = self.matches(PurePosixPath(self.rel).name, self.conf['python_files'])
        all_ids = [tid for tid, _, _ in self.tests()]
        for m in self.module_skips():
            cat = 'never_runs' if m['kind'] == 'module_skip_call' and not m['conditional'] else 'skipped'
            why = (f'pytest.skip(allow_module_level=True) at line {m["line"]} skips the whole module, so pytest collects none '
                   f'of its {len(all_ids)} tests: {m["reason"] or "no reason given"}' if cat == 'never_runs' else
                   f'{m["kind"]} skips every test in the module' + (f' ({m["condition"]})' if m['condition'] else '') +
                   f': {m["reason"] or "no reason given"}')
            add(cat, m['kind'], all_ids, m['line'], why, m)
        for tid, fn, cls in self.tests():
            defs.append({'id': tid, 'file': self.rel, 'line': fn.lineno, 'name': fn.name, 'lang': 'python', 'package': self.pkg,
                         'calls': self.called_targets(fn)})
            if not file_ok:
                add('never_runs', 'file_pattern', [tid], fn.lineno,
                    f'{PurePosixPath(self.rel).name} does not match python_files {self.conf["python_files"]} '
                    f'({self.conf["source"]}), so pytest never collects it', {'python_files': self.conf['python_files']})
            elif cls is not None and not self.matches(cls.name, self.conf['python_classes']):
                add('never_runs', 'class_pattern', [tid], fn.lineno, f'class {cls.name} does not match python_classes', {})
            self.rules(tid, fn, add, cls)

    def called_targets(self, fn) -> list[str]:
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name) and f.id in self.imports:
                    out.append(self.imports[f.id])
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.imports:
                    out.append(f'{self.imports[f.value.id]}.{f.attr}')
        # the standard library and test tooling are never the code under test
        return sorted({t for t in out if t.split('.')[0] not in STDLIB})

    def rules(self, tid, fn, add, cls=None):
        body_nodes = list(ast.walk(fn))
        asserts = [n for n in body_nodes if is_assertion(n)]
        info = self.skip_info(fn, cls)
        if info is not None:
            add('skipped', info['kind'], [tid], info['line'], f'{info["kind"]}: {info["reason"] or "no reason given"}', info)
        if not asserts:
            if asserts_in(fn, self.helpers):
                return                                  # every check is in a helper of this file (_test_x, self._check)
            add('no_assertion', 'no_assertion', [tid], fn.lineno, f'{fn.name} has no assert, assertion call or pytest.raises', {})
            return
        for a in asserts:
            if isinstance(a, ast.Assert):
                t = a.test
                if isinstance(t, ast.Constant) and t.value:
                    add('tautology', 'assert_constant', [tid], a.lineno, f'assert {t.value!r} can never fail', {})
                elif isinstance(t, ast.Compare) and len(t.comparators) == 1 and \
                        ast.dump(t.left) == ast.dump(t.comparators[0]) and isinstance(t.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)):
                    add('tautology', 'compare_to_self', [tid], a.lineno, 'the assertion compares a value with itself', {})
        handler_asserts = {id(x) for h in body_nodes if isinstance(h, ast.ExceptHandler) for s in h.body for x in ast.walk(s)
                           if is_assertion(x)}
        for n in body_nodes:
            if isinstance(n, (ast.For, ast.AsyncFor)) and any(is_assertion(x) for x in ast.walk(n)) and \
                    not isinstance(n.iter, (ast.List, ast.Tuple, ast.Set)) and not emptiness_guarded(fn, n, self.helpers):
                add('unreached_assertion', 'assert_in_loop', [tid], n.lineno,
                    'every assertion in this loop is skipped when the iterable is empty', {})
            if isinstance(n, ast.Try):
                inner = any(is_assertion(x) for s in n.body for x in ast.walk(s))
                for h in n.handlers:
                    if inner and catches_assertion(h) and not reports_failure(h, n, fn):
                        add('swallowed_failure', 'except_around_assert', [tid], h.lineno,
                            f'except {ast.unparse(h.type) if h.type is not None else "(bare)"} catches the AssertionError '
                            'raised by the assertion in its try block and carries on', {})
                    in_handler = {id(x) for s in h.body for x in ast.walk(s) if is_assertion(x)}
                    if in_handler and all(id(a) in handler_asserts for a in asserts):     # the only checks are in except
                        add('unreached_assertion', 'assert_in_except', [tid], h.lineno,
                            'assertions inside except run only if the call raises; nothing fails when it does not', {})
        for i, st in enumerate(fn.body):
            if isinstance(st, ast.Return) and any(is_assertion(x) for s in fn.body[i + 1:] for x in ast.walk(s)):
                add('unreached_assertion', 'assert_after_return', [tid], st.lineno, 'assertions after return never run', {})
        # mock_only: the test patches a function and then checks that same function
        patched = {}
        for n in body_nodes:
            if isinstance(n, ast.Call) and call_name(n) in ('patch',) and n.args and isinstance(n.args[0], ast.Constant):
                patched[str(n.args[0].value).split('.')[-1]] = str(n.args[0].value)
            if isinstance(n, ast.Call) and call_name(n) == 'object' and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant):
                patched[str(n.args[1].value)] = ast.unparse(n.args[0]) + '.' + str(n.args[1].value) if hasattr(ast, 'unparse') else ''
        if patched:
            subject = [call_name(n) for n in body_nodes if isinstance(n, ast.Call) and call_name(n) in patched]
            checks = [a for a in asserts if isinstance(a, ast.Assert) and
                      any(isinstance(x, ast.Call) and call_name(x) in patched for x in ast.walk(a.test))]
            if subject and (checks or all(not isinstance(a, ast.Assert) for a in asserts)):
                add('mock_only', 'subject_is_mocked', [tid], fn.lineno,
                    f'{fn.name} patches {", ".join(sorted(patched.values()))} and then asserts on that same function, so it '
                    'only checks the mock', {'patched': sorted(patched.values())})

    def skip_info(self, fn, cls=None):
        for d in list(fn.decorator_list) + (list(cls.decorator_list) if cls is not None else []):
            info = mark_info(d)
            if info:
                return info
        calls = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and is_skip_call(n):
                reason = skip_reason(n)
                guard = guard_of(fn, n)
                key = KEY.search(reason or '')
                calls.append({'kind': 'skip_call', 'line': n.lineno, 'reason': reason, 'conditional': guard is not None,
                              'condition': guard, 'ticket': key.group(1) if key else None})
        calls.sort(key=lambda c: (c['conditional'], c['line']))     # an unconditional skip wins over guard skips
        return calls[0] if calls else None

    def module_skips(self) -> list[dict]:
        """Module-level skips, each applying to every test in the module: pytestmark = pytest.mark.skip / skipif / xfail,
        pytest.skip(..., allow_module_level=True) and pytest.importorskip(...)."""
        out = []

        def visit(stmts, guard):
            for st in stmts:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in targets) and st.value is not None:
                        marks = st.value.elts if isinstance(st.value, (ast.List, ast.Tuple)) else [st.value]
                        for m in marks:
                            info = mark_info(m)
                            if info:
                                info = {**info, 'kind': 'module_' + info['kind']}
                                if guard:
                                    info.update(conditional=True,
                                                condition=f'{guard} and {info["condition"]}' if info['condition'] else guard)
                                out.append(info)
                        continue
                if isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign)) and st.value is not None:
                    for n in ast.walk(st.value):
                        if not isinstance(n, ast.Call):
                            continue
                        if is_skip_call(n) and call_name(n) == 'skip':
                            reason = skip_reason(n)
                            key = KEY.search(reason or '')
                            out.append({'kind': 'module_skip_call', 'line': n.lineno, 'reason': reason, 'conditional': bool(guard),
                                        'condition': guard, 'ticket': key.group(1) if key else None})
                        elif call_name(n) == 'importorskip':
                            arg = n.args[0] if n.args else next((kw.value for kw in n.keywords if kw.arg == 'modname'), None)
                            mod = arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg) if arg is not None else '?'
                            reason = next((kw.value.value for kw in n.keywords if kw.arg == 'reason' and
                                           isinstance(kw.value, ast.Constant)), None) or f'needs {mod}'
                            key = KEY.search(reason)
                            out.append({'kind': 'importorskip', 'line': n.lineno, 'reason': reason, 'conditional': True,
                                        'condition': f'{mod} is not importable' + (f' ({guard})' if guard else ''),
                                        'ticket': key.group(1) if key else None})
                elif isinstance(st, ast.If):
                    g = f'if {ast.unparse(st.test)}'
                    visit(st.body, g)
                    visit(st.orelse, f'not ({g})')
                elif isinstance(st, ast.Try):
                    visit(st.body, guard or 'inside try')
                    for h in st.handlers:
                        visit(h.body, f'except {ast.unparse(h.type)}' if h.type is not None else 'except')
                    visit(st.orelse, guard or 'when the try block did not raise')
                    visit(st.finalbody, guard)
        visit(self.tree.body, None)
        return out


def loop_subjects(it: ast.AST) -> list[str]:
    """The expressions a loop iterates (as ast dumps): x for `x`, `enumerate(x)`, `sorted(x)`, `x.items()`, `zip(a, b)`."""
    out = [ast.dump(it)]
    if isinstance(it, ast.Call):
        if call_name(it) in ('enumerate', 'sorted', 'reversed', 'list', 'tuple', 'zip', 'iter') and isinstance(it.func, ast.Name):
            for a in it.args:
                out += loop_subjects(a)
        elif isinstance(it.func, ast.Attribute) and it.func.attr in ('items', 'values', 'keys'):
            out += loop_subjects(it.func.value)
    return out


def emptiness_guarded(fn: ast.AST, loop: ast.AST, helpers: dict) -> bool:
    """Something outside the loop fails when its iterable is empty, so the loop's assertions can't all be skipped:
    an assertion on its length (`assert len(rows) == 3`, also across several lines), on its truthiness or value
    (`assert rows`, `assert rows != []`), an indexed assertion (`assert rows[0] == ...`), an assertion helper given it
    (`_verify_shape(rows)`, `self.assert_rows(rows)`, or `_verify_shape(result)` for a loop over `result.rows`), or the
    loop runs over a name bound to a non-empty literal (`expected = ["a", "b"]`, `counts = {"a": 1}` and its `.items()`)."""
    subjects = set(loop_subjects(loop.iter))
    inside = {id(x) for x in ast.walk(loop)}
    # what a helper may be given instead: the object the iterable is read from (result for result.rows / result["rows"])
    owners = set()
    for e in [loop.iter] + [n for n in ast.walk(loop.iter) if isinstance(n, (ast.Attribute, ast.Subscript))]:
        while isinstance(e, (ast.Attribute, ast.Subscript, ast.Call)):
            e = e.func if isinstance(e, ast.Call) else e.value
            owners.add(ast.dump(e))

    def is_subject(e: ast.AST) -> bool:
        return ast.dump(e) in subjects

    def checks_subject(e: ast.AST) -> bool:
        if is_subject(e):                                       # assert rows / rows == [...]
            return True
        for x in ast.walk(e):
            if isinstance(x, ast.Call) and call_name(x) == 'len' and x.args and is_subject(x.args[0]):
                return True
            if isinstance(x, ast.Subscript) and is_subject(x.value):
                return True
        if isinstance(e, ast.Compare) and (is_subject(e.left) or any(is_subject(c) for c in e.comparators)):
            return True
        return False
    for st in ast.walk(fn):
        if id(st) in inside:
            continue
        if isinstance(st, ast.Assert) and checks_subject(st.test):
            return True
        if isinstance(st, ast.Call) and (is_assertion(st) or call_name(st).lstrip('_').startswith(('assert', 'verify', 'check', 'expect'))
                                         or (call_name(st) in helpers and asserts_in(helpers[call_name(st)], helpers))):
            args = list(st.args) + [k.value for k in st.keywords]
            if any(checks_subject(a) or ast.dump(a) in owners for a in args):
                return True
    # for x in names / names.items(): where names is bound only to a non-empty literal in the test
    for sub in [loop.iter] + [n for n in ast.walk(loop.iter) if isinstance(n, ast.Name)]:
        if not isinstance(sub, ast.Name) or ast.dump(sub) not in subjects:
            continue
        binds = [st.value for st in ast.walk(fn) if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and
                 any(isinstance(t, ast.Name) and t.id == sub.id for t in (st.targets if isinstance(st, ast.Assign) else [st.target]))]
        if binds and all((isinstance(b, (ast.List, ast.Tuple, ast.Set)) and b.elts) or (isinstance(b, ast.Dict) and b.keys)
                         for b in binds):
            return True
    return False


def mark_info(d: ast.AST) -> dict | None:
    """Skip details for a skip / skipif / non-strict xfail mark or a unittest skip decorator; None for anything else."""
    call = d if isinstance(d, ast.Call) else None
    name = call_name(d) if isinstance(d, (ast.Call, ast.Attribute, ast.Name)) else ''
    if name not in PY['skip_decorators']:
        return None
    reason = None
    if call is not None:
        for kw in call.keywords:
            if kw.arg == 'reason' and isinstance(kw.value, ast.Constant):
                reason = kw.value.value
        if reason is None and name in ('skip', 'skipIf', 'skipUnless') and call.args and \
                isinstance(call.args[-1], ast.Constant) and isinstance(call.args[-1].value, str):
            reason = call.args[-1].value
    strict = any(kw.arg == 'strict' and isinstance(kw.value, ast.Constant) and kw.value.value
                 for kw in (call.keywords if call else []))
    cond = name in ('skipif', 'skipIf', 'skipUnless') or (name == 'xfail' and call is not None and bool(call.args))
    if name == 'xfail' and strict:
        return None
    key = KEY.search(reason or '')
    return {'kind': name, 'line': d.lineno, 'reason': reason, 'conditional': cond,
            'condition': ast.unparse(call.args[0]) if cond and call and call.args else None,
            'ticket': key.group(1) if key else None}


def is_skip_call(n: ast.Call) -> bool:
    """pytest.skip(...) or self.skipTest(...)."""
    f = n.func
    return isinstance(f, ast.Attribute) and ((f.attr == 'skip' and getattr(f.value, 'id', '') == 'pytest') or f.attr == 'skipTest')


def skip_reason(n: ast.Call) -> str | None:
    arg = n.args[0] if n.args else next((kw.value for kw in n.keywords if kw.arg in ('reason', 'msg')), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return ast.unparse(arg) if arg is not None else None


def guard_of(fn: ast.AST, target: ast.AST) -> str | None:
    """Why a call inside fn runs only sometimes: the nearest enclosing if, except, try, loop, with, short-circuit
    expression or nested function, or an earlier statement in the test body that can return or raise first.
    None when it always runs."""
    parent = {}
    for n in ast.walk(fn):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
    node = target
    while node is not fn and node in parent:
        up = parent[node]
        if isinstance(up, ast.If) and node is not up.test:
            return f'if {ast.unparse(up.test)}' if node in up.body else f'else of if {ast.unparse(up.test)}'
        if isinstance(up, ast.ExceptHandler):
            return f'except {ast.unparse(up.type)}' if up.type is not None else 'except'
        if isinstance(up, ast.Try) and node in up.body:
            return 'inside try'
        if isinstance(up, (ast.For, ast.AsyncFor, ast.While)) and node in up.body:
            return f'inside the loop at line {up.lineno}'
        if isinstance(up, (ast.BoolOp, ast.IfExp)):
            return f'in {ast.unparse(up)}'
        if type(up).__name__ == 'match_case':
            return 'inside a match case'
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and up is not fn:
            return f'inside the nested function {getattr(up, "name", "lambda")}'
        node = up
    for st in fn.body:                                  # a top-level statement: can an earlier one end the test first?
        if st is node:
            break
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for x in ast.walk(st):
            if isinstance(x, (ast.Return, ast.Raise)):
                return f'after the early {"return" if isinstance(x, ast.Return) else "raise"} at line {x.lineno}'
    return None


CATCHES = {'AssertionError', 'Exception', 'BaseException'}


def catches_assertion(h: ast.ExceptHandler) -> bool:
    """A bare except, or one naming AssertionError, Exception or BaseException (alone or in a tuple)."""
    if h.type is None:
        return True
    types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
    return any(isinstance(t, (ast.Name, ast.Attribute)) and call_name(t) in CATCHES for t in types)


def reports_failure(h: ast.ExceptHandler, tr: ast.Try, fn: ast.AST) -> bool:
    """The handler re-raises, fails the test (pytest.fail, self.fail), asserts, or keeps the error for an assertion
    after the try block (errors.append(e) ... assert not errors)."""
    for n in ast.walk(h):
        if isinstance(n, ast.Raise) or is_assertion(n) or (isinstance(n, ast.Call) and call_name(n) == 'fail'):
            return True
    if h.name and any(isinstance(n, ast.Name) and n.id == h.name for s in h.body for n in ast.walk(s)):
        end = tr.end_lineno or tr.lineno
        if any(is_assertion(n) and n.lineno > end for n in ast.walk(fn)):
            return True
    return False


# ---------------------------------------------------------------- JavaScript / TypeScript
# A small lexer, not a parser: it knows where strings, template literals, regular expression literals and comments
# start and end, so brackets, quotes and words inside them are never mistaken for code.
REGEX_AFTER_WORDS = {'return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw', 'case', 'do', 'else',
                     'yield', 'await', 'export', 'default', 'extends'}
JS_KEYWORDS = {'const', 'let', 'var', 'await', 'async', 'return', 'if', 'else', 'for', 'while', 'do', 'of', 'in', 'new', 'function',
               'try', 'catch', 'finally', 'throw', 'typeof', 'instanceof', 'true', 'false', 'null', 'undefined', 'this',
               'expect', 'switch', 'case', 'break', 'continue', 'void', 'delete', 'yield', 'class'}
ASSERT_JS = re.compile(JS['assert_pattern'])
IMPLICIT_QUERY = re.compile(r'\b(?:get|find)(?:All)?By[A-Z]\w*\s*\(')


def js_lex(text: str) -> tuple[str, dict[int, tuple[str, int]]]:
    """(masked text, literals). The masked text has comments and the contents of strings, template literals and regular
    expression literals blanked with spaces (delimiters and newlines kept, offsets unchanged); template ${...}
    expressions stay code. literals maps each literal's start offset to (kind, end offset), kind string, template or
    regex. A / starts a regex where an expression can begin (after an operator, an opening bracket, a comma or a keyword
    such as return), so the apostrophe in /can't/ is not a string. A string ends at a newline at the latest, so stray
    text (an apostrophe in JSX text) costs one line at most."""
    out = list(text)
    n = len(text)
    lits: dict[int, tuple[str, int]] = {}
    tpl_stack: list[tuple[int, int]] = []                  # (brace depth of the ${ ... }, template start)
    depth = 0
    prev = 'op'                                            # op: an expression can start next; close: it can't
    last_sig = ''

    def blank(a: int, b: int):
        for k in range(a, min(b, n)):
            if out[k] != '\n':
                out[k] = ' '

    def template_text(i: int, start: int) -> tuple[int, bool]:
        """Blank template text from i; returns (next index, True if a ${ opened)."""
        j = i
        while j < n:
            c = text[j]
            if c == '\\':
                j += 2
                continue
            if c == '`':
                blank(i, j)
                lits[start] = ('template', j + 1)
                return j + 1, False
            if c == '$' and j + 1 < n and text[j + 1] == '{':
                blank(i, j)
                return j + 2, True
            j += 1
        blank(i, n)
        lits[start] = ('template', n)
        return n, False

    i = 0
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            j = min(j, n - 1)
            blank(i + 1, j)
            lits[i] = ('string', j + 1)
            i, prev, last_sig = j + 1, 'close', c
            continue
        if c == '`':
            start = i
            i, opened = template_text(i + 1, start)
            if opened:
                depth += 1
                tpl_stack.append((depth, start))
                prev = 'op'
            else:
                prev = 'close'
            last_sig = '`'
            continue
        if c == '/' and prev == 'op' and last_sig != '<':
            j, in_class = i + 1, False
            while j < n and text[j] != '\n':
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '[':
                    in_class = True
                elif text[j] == ']':
                    in_class = False
                elif text[j] == '/' and not in_class:
                    break
                j += 1
            if j < n and text[j] == '/':
                k = j + 1
                while k < n and (text[k].isalnum() or text[k] == '_'):
                    k += 1
                blank(i + 1, j)
                lits[i] = ('regex', k)
                i, prev, last_sig = k, 'close', '/'
                continue
        if c.isalpha() or c in '_$':
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in '_$'):
                j += 1
            word = text[i:j]
            prev = 'op' if word in REGEX_AFTER_WORDS else 'close'
            i, last_sig = j, 'w'
            continue
        if c.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in '._'):
                j += 1
            i, prev, last_sig = j, 'close', '0'
            continue
        if c == '{':
            depth += 1
            prev = 'op'
        elif c == '}':
            if tpl_stack and tpl_stack[-1][0] == depth:
                _, start = tpl_stack.pop()
                depth -= 1
                i, opened = template_text(i + 1, start)
                if opened:
                    depth += 1
                    tpl_stack.append((depth, start))
                    prev = 'op'
                else:
                    prev = 'close'
                last_sig = '`'
                continue
            depth -= 1
            prev = 'close'
        elif c in ')]':
            prev = 'close'
        else:
            prev = 'op'
        last_sig = c
        i += 1
    return ''.join(out), lits


def js_mask(text: str) -> str:
    """The text with comments and literal contents blanked (see js_lex)."""
    return js_lex(text)[0]


def match_code(code: str, i: int) -> int:
    """Index of the bracket closing the one at code[i], in masked code (no literal or comment can hold a bracket)."""
    open_c = code[i]
    close_c = {'(': ')', '{': '}', '[': ']'}[open_c]
    depth, j, n = 0, i, len(code)
    while j < n:
        c = code[j]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


def match_paren(text: str, i: int) -> int:
    """Index of the bracket closing the one at text[i], skipping strings, templates, regex literals and comments."""
    return match_code(js_mask(text), i)


def split_args(code: str, a: int, b: int) -> list[tuple[int, int]]:
    """Top-level argument spans of code[a:b] (the inside of a call's parentheses), trimmed of whitespace."""
    spans, depth, start = [], 0, a
    for k in range(a, b):
        c = code[k]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            spans.append((start, k))
            start = k + 1
    spans.append((start, b))
    out = []
    for s, e in spans:
        while s < e and code[s].isspace():
            s += 1
        while e > s and code[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def unescape(raw: str) -> str:
    return re.sub(r'\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)', lambda m: {
        'n': '\n', 't': '\t', 'r': '\r', '0': '\0'}.get(m.group(1), chr(int(m.group(1)[1:].strip('{}'), 16))
                                                         if m.group(1)[0] in 'ux' and len(m.group(1)) > 1 else m.group(1)),
                  raw, flags=re.S)


def literal_at(text: str, lits: dict, span: tuple[int, int] | None) -> tuple[str, bool] | None:
    """(value, parametrised) when the argument span is exactly one string or template literal."""
    if not span or span[0] not in lits:
        return None
    kind, end = lits[span[0]]
    if end != span[1] or kind == 'regex':
        return None
    raw = text[span[0] + 1:end - 1]
    if kind == 'template':
        return unescape(raw), '${' in raw
    return unescape(raw), False


def name_regex(name: str, parametrised: bool) -> str:
    """A regular expression matching the names a runner lists for a templated name (printf %s/%d/%i/%f/%j/%o/%#/%%,
    $variable from an object table, and ${...} interpolations)."""
    if not parametrised:
        return re.escape(name)
    out, i = [], 0
    while i < len(name):
        if name.startswith('${', i):
            j = name.find('}', i)
            j = len(name) if j < 0 else j + 1
            out.append('.*')
            i = j
        elif name[i] == '%' and i + 1 < len(name) and name[i + 1] in 'sdifjoOc#$':
            out.append('.*')
            i += 2
        elif name.startswith('%%', i):
            out.append('%')
            i += 2
        elif name[i] == '$' and i + 1 < len(name) and (name[i + 1].isalpha() or name[i + 1] == '_'):
            j = i + 1
            while j < len(name) and (name[j].isalnum() or name[j] in '_.'):
                j += 1
            out.append('.*')
            i = j
        else:
            out.append(re.escape(name[i]))
            i += 1
    return ''.join(out)


JS_HEADS = ('describe', 'suite', 'xdescribe', 'fdescribe', 'it', 'test', 'xit', 'xtest', 'fit')
TEST_MODS = {'only', 'skip', 'todo', 'concurrent', 'sequential', 'fixme', 'fail', 'fails', 'failing', 'slow', 'each', 'for',
             'skipIf', 'runIf', 'serial', 'parallel', 'shuffle'}
NOT_TESTS = {'step', 'use', 'beforeEach', 'afterEach', 'beforeAll', 'afterAll', 'extend', 'info', 'setTimeout', 'expect',
             'configure', 'abort', 'mixin'}
PRODUCT_SPEC = re.compile(r'^(\.{1,2}/|/|@/|~/|#)')
_ALIASES: dict[str, list[tuple[str, list[str]]]] = {}


def json_lenient(text: str):
    """JSON with comments and trailing commas (tsconfig.json)."""
    masked, lits = js_lex(text)
    # the mask drops comments but also string contents: put the strings back
    keep, i, n = [], 0, len(text)
    while i < n:
        if i in lits and lits[i][0] == 'string':
            keep.append(text[i:lits[i][1]])
            i = lits[i][1]
        else:
            keep.append(masked[i])
            i += 1
    out = re.sub(r',(\s*[}\]])', r'\1', ''.join(keep))
    return json.loads(out)


def ts_aliases(pkg_dir: Path) -> list[tuple[str, list[str]]]:
    """[(alias prefix, [target directories relative to pkg_dir])] from tsconfig.json / jsconfig.json compilerOptions.paths
    ("@/*": ["./src/*"]) and simple `alias: { '@': path.resolve(__dirname, './src') }` entries in vite/vitest configs.
    A prefix ends in / when the alias maps a directory. Read once per package."""
    key = str(pkg_dir)
    if key in _ALIASES:
        return _ALIASES[key]
    out: list[tuple[str, list[str]]] = []
    for name in ('tsconfig.json', 'jsconfig.json'):
        p = pkg_dir / name
        if not p.is_file():
            continue
        try:
            opts = json_lenient(p.read_text(encoding='utf-8', errors='replace')).get('compilerOptions', {})
        except (ValueError, AttributeError):
            continue
        base = opts.get('baseUrl', '.')
        for k, vals in (opts.get('paths') or {}).items():
            pref = k[:-1] if k.endswith('*') else k
            out.append((pref, [(PurePosixPath(base) / (v[:-1] if v.endswith('*') else v)).as_posix() for v in vals]))
        break
    for cfg in sorted(list(pkg_dir.glob('vitest.config.*')) + list(pkg_dir.glob('vite.config.*'))):
        text = cfg.read_text(encoding='utf-8', errors='replace')
        for m in re.finditer(r'["\']([@~#][\w/-]*)["\']\s*:\s*(?:(?:path\.)?resolve\s*\([^,()]*,\s*)?["\']([^"\']+)["\']', text):
            pref = m.group(1) if m.group(1).endswith('/') else m.group(1) + '/'
            out.append((pref, [m.group(2).rstrip('/') + '/']))
    _ALIASES[key] = sorted(out, key=lambda a: len(a[0]), reverse=True)
    return _ALIASES[key]


def is_product(spec: str, aliases: list[tuple[str, list[str]]]) -> bool:
    return bool(PRODUCT_SPEC.match(spec)) or any(spec.startswith(a) for a, _ in aliases if a)


JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts')
GENERIC = r'(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)'      # type parameters, nested two deep (<T extends Record<K, V>>)
# an optional type annotation before '=': it may hold arrow types (`: [() => void, () => void][] =`)
ANNOT = r'(?::(?:=>|[^=;])+?)?'
DECL = r'(?:\bexport\s+)?(?:\bdefault\s+)?(?:\basync\s+)?\bfunction\s*\*?\s*{name}\s*' + GENERIC + r'?\s*\('
ARROW = r'\b(?:const|let|var)\s+{name}\s*' + ANNOT + r'=\s*(?:async\s*)?(?:' + GENERIC + r'\s*)?(?=\(|[A-Za-z_$][\w$]*\s*=>)'
# Modules whose test/it/describe are a test framework's: a head imported from anywhere else is the product's own API
# (a testing framework's source that exports test() and describe()), unless the file imports no framework at all.
FRAMEWORK_MODULES = re.compile(r'^(?:vitest|@vitest/[\w.-]+|jest|@jest/globals|@playwright/test|playwright/test|'
                               r'@playwright/experimental-ct-[\w-]+|node:test|mocha|bun:test|ava|uvu|tap|cypress)$')


def resolve_module(from_file: Path, spec: str, pkg_dir: Path) -> Path | None:
    """The file a relative or alias import names: tsconfig/jsconfig `paths` and vite/vitest `alias` of the package."""
    bases = []
    if spec.startswith('.'):
        bases.append(from_file.parent / spec)
    else:
        for pref, targets in ts_aliases(pkg_dir):
            if pref and spec.startswith(pref):
                bases += [pkg_dir / t / spec[len(pref):] for t in targets]
                break
    for base in bases:
        stem = base.with_suffix('') if base.suffix in ('.js', '.jsx', '.mjs', '.cjs') else None  # './x.js' for x.ts
        for cand in [base] + [Path(str(base) + e) for e in JS_EXT] + [base / f'index{e}' for e in JS_EXT] + \
                ([Path(str(stem) + e) for e in JS_EXT] if stem is not None else []):
            if cand.is_file():
                return cand.resolve()
    return None


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
            j = match_code(code, j)                       # an object type in the return type
        elif c in ';=' and angle == 0 and not code.startswith('=>', j):
            return None
        j += 1
    return None


def expr_end(code: str, k: int) -> int:
    """End of an expression starting at k: a ; or , or closing bracket at depth 0, or a line end that doesn't continue it."""
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
    return e


def function_spans(code: str) -> dict[str, list[tuple[int, int]]]:
    """name -> [(start, end)] of the bodies of named functions and arrow functions bound to a name, anywhere in code."""
    out: dict[str, list[tuple[int, int]]] = {}
    for m in re.finditer(r'\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*' + GENERIC + r'?\s*\(', code):
        open_i = body_after_params(code, match_code(code, m.end() - 1))
        if open_i is not None:
            out.setdefault(m.group(1), []).append((open_i, match_code(code, open_i)))
    for m in re.finditer(ARROW.replace('{name}', r'([A-Za-z_$][\w$]*)'), code):
        j = m.end()
        if code[j] == '(':
            j = match_code(code, j) + 1
        arrow = code.find('=>', j)
        between = code[j:arrow].strip() if arrow >= 0 else ''
        if arrow < 0 or (between and not between.startswith(':')) or ';' in between:
            continue                                       # `const x = (await y())` and a later arrow: not a function
        k = arrow + 2
        while k < len(code) and code[k].isspace():
            k += 1
        end = match_code(code, k) if k < len(code) and code[k] == '{' else expr_end(code, k)
        out.setdefault(m.group(1), []).append((k, end))
    return out


def assert_rx_for(names: set[str]) -> re.Pattern:
    """ASSERT_JS plus calls of the given names: aliases of expect/assert (`expect as vexpect`) and assertion helpers."""
    if not names:
        return ASSERT_JS
    alt = '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(JS['assert_pattern'] + r'|(?<![\w$.])(?:' + alt + r')(?:\s*\.\s*(?:soft|poll|not))?\s*[(`<]')


def expect_aliases(imports: dict[str, tuple[str, str]]) -> set[str]:
    """Local names of an imported expect or assert under another name (`import { expect as vexpect } from 'vitest'`)."""
    return {local for local, (spec, orig) in imports.items() if orig in ('expect', 'assert') and local != orig}


def assert_helpers(code: str, names: set[str], spans: dict | None = None) -> set[str]:
    """Names of the functions defined in code whose bodies assert, directly or through another such function (two calls
    deep): `function failsWith(p, code) { ... expect(...) }`, `const check = (x) => expect(x)...`. names: the assertion
    names already known (expect aliases, imported helpers)."""
    spans = function_spans(code) if spans is None else spans
    found: set[str] = set()
    for _ in range(3):
        cur = assert_rx_for(names | found)
        new = {n for n, sp in spans.items() if n not in found and any(cur.search(code[a:b]) for a, b in sp)}
        if not new:
            break
        found |= new
    return found


_MODULES: dict[str, dict] = {}


def module_info(path: Path) -> dict:
    """text, masked code, literals, imports and assertion helpers of a module (read once)."""
    key = str(path)
    if key not in _MODULES:
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = ''
        code, lits = js_lex(text)
        imports = js_imports(text, code, lits)
        info = {'text': text, 'code': code, 'lits': lits, 'imports': imports}
        info['helpers'] = assert_helpers(code, expect_aliases(imports)) if text else set()
        _MODULES[key] = info
    return _MODULES[key]


def spec_origin(from_file: Path, spec: str, name: str, pkg_dir: Path, depth: int = 0) -> tuple[str, str]:
    """Where an imported test head comes from: ('framework', spec) for a test framework (also through a relative fixtures
    module that extends or re-exports one), ('bare', spec) for another package (a repository's own runner, or the
    product's package), ('product', spec) for a relative or alias module that defines it itself."""
    if FRAMEWORK_MODULES.match(spec):
        return 'framework', spec
    if not is_product(spec, ts_aliases(pkg_dir)):
        return 'bare', spec
    target = resolve_module(from_file, spec, pkg_dir) if depth < 4 else None
    if target is None:
        return 'product', spec
    mi = module_info(target)
    code, text, lits, imports = mi['code'], mi['text'], mi['lits'], mi['imports']
    look = name
    if look in imports:                                            # import { test } from 'x'; export { test }
        s2, o2 = imports[look]
        return spec_origin(target, s2, o2 if o2 not in ('default', '*') else look, pkg_dir, depth + 1)
    for m in re.finditer(r'\bexport\s*\{([^}]*)\}\s*from\s*(["\'])', code):
        q = m.end() - 1
        for part in m.group(1).split(','):
            orig, _, alias = part.strip().partition(' as ')
            if (alias or orig).strip() == look and q in lits:
                return spec_origin(target, text[q + 1:lits[q][1] - 1], orig.strip(), pkg_dir, depth + 1)
    dm = re.search(r'\b(?:const|let|var)\s+' + re.escape(look) + r'\s*' + ANNOT + r'=\s*([A-Za-z_$][\w$]*)\b', code)
    if dm and dm.group(1) in imports:                              # export const test = base.extend({...})
        s2, o2 = imports[dm.group(1)]
        return spec_origin(target, s2, o2 if o2 not in ('default', '*') else dm.group(1), pkg_dir, depth + 1)
    for m in re.finditer(r'\bexport\s*\*\s*from\s*(["\'])', code):
        q = m.end() - 1
        if q in lits:
            got = spec_origin(target, text[q + 1:lits[q][1] - 1], look, pkg_dir, depth + 1)
            if got[0] != 'product':
                return got
    if re.search(r'\b(?:const|let|var|class|function\s*\*?)\s*' + re.escape(look) + r'(?![\w$])', code):
        return 'product', spec                                     # the module defines it: the product's own test()
    fw = next((s2 for s2, _ in imports.values() if FRAMEWORK_MODULES.match(s2)), None)
    return ('framework', fw) if fw else ('product', spec)


def runner_of(spec: str) -> str:
    if 'playwright' in spec:
        return 'playwright'
    if spec == 'node:test':
        return 'node-test'
    return 'js'


def enclosing_block(code: str, pos: int) -> tuple[int, int]:
    """(open, close) of the innermost { } block around pos, or the whole text."""
    depth, k = 0, pos - 1
    while k >= 0:
        c = code[k]
        if c == '}':
            depth += 1
        elif c == '{':
            if depth == 0:
                return k, match_code(code, k)
            depth -= 1
        k -= 1
    return 0, len(code)


def local_head_scopes(code: str, name: str, is_head) -> list[tuple[int, int]]:
    """Spans where a local declaration of name (a helper called test, a parameter-free const) hides the test head of
    that name. `const test = base.extend(...)` of a head stays a head."""
    out = []
    for m in re.finditer(r'(?:\bfunction\s*\*?\s*|\b(?:const|let|var)\s+)' + re.escape(name) + r'(?![\w$])', code):
        rhs = re.match(r'\s*' + ANNOT + r'=\s*([A-Za-z_$][\w$]*)\s*(?:\.\s*extend\b|[;\n,)])', code[m.end():])
        if rhs and is_head(rhs.group(1)):
            continue
        loop = re.search(r'\bfor\s*(?:await\s*)?\(\s*$', code[:m.start()])
        if loop:                                           # for (const it of rows): the loop alone
            h_close = match_code(code, code.index('(', loop.start()))
            bm = re.match(r'\s*\{', code[h_close + 1:])
            out.append((m.start(), match_code(code, h_close + 1 + bm.end() - 1) if bm else expr_end(code, h_close + 1)))
            continue
        out.append(enclosing_block(code, m.start()))
    return out


def match_back(code: str, i: int) -> int:
    """Index of the bracket opening the one closing at code[i]."""
    close_c = code[i]
    open_c = {')': '(', ']': '[', '}': '{'}[close_c]
    depth, j = 0, i
    while j >= 0:
        c = code[j]
        if c == close_c:
            depth += 1
        elif c == open_c:
            depth -= 1
            if depth == 0:
                return j
        j -= 1
    return 0


def stmt_context(code: str, pos: int) -> tuple[str, int]:
    """(the statement's text before pos, index of the unclosed ( or [ around pos within the statement, or -1)."""
    depth, k = 0, pos - 1
    while k >= 0:
        c = code[k]
        if c in ')]}':
            depth += 1
        elif c in '([':
            if depth == 0:
                return code[k + 1:pos], k
            depth -= 1
        elif c == '{':
            if depth == 0:
                return code[k + 1:pos], -1
            depth -= 1
        elif depth == 0 and c == ';':
            return code[k + 1:pos], -1
        elif depth == 0 and c == '\n':
            before = code[:k].rstrip()
            after = code[k + 1:pos].lstrip()
            if before and (before[-1].isalnum() or before[-1] in ')]}_$"\'`') and not after.startswith(('.', '?.')) \
                    and not re.search(r'\b(await|return|typeof|void|new|yield)$', before):
                return code[k + 1:pos], -1
        k -= 1
    return code[:pos], -1


def name_settled(code: str, name: str, after: int) -> bool:
    """A promise held in name is awaited or returned later (`await assertion`, `await Promise.all([a, b])`,
    `return a`), or handed to a call (`await settle(a)`)."""
    rest = code[after:]
    return bool(re.search(r'\b(?:await|return)\b[^;]*?(?<![\w$.])' + re.escape(name) + r'(?![\w$])', rest))


def promise_settled(code: str, pos: int) -> bool:
    """The expectation (a resolves/rejects chain) starting at pos is awaited, returned, the body of an arrow, held in a
    name settled later, or an element of an awaited or returned Promise.all/allSettled/race/any. An expectation passed
    to another call counts as handled: what that call does with it is not known."""
    prefix, opener = stmt_context(code, pos)
    while opener >= 0 and code[opener] == '[' and not prefix.strip(' \t\r\n,'):
        prefix, opener = stmt_context(code, opener)            # an element of an array literal: look at the array
    if opener >= 0:
        if code[opener] == '(' and not prefix.strip(' \t\r\n,') and \
                re.search(r'\bPromise\s*\.\s*(?:all|allSettled|race|any)\s*$', code[:opener]):
            pm = re.search(r'\bPromise\s*\.\s*(?:all|allSettled|race|any)\s*$', code[:opener])
            return promise_settled(code, pm.start())
        return True
    if re.search(r'\b(await|return)\s*$', prefix) or re.search(r'=>\s*$', prefix):
        return True
    am = re.match(r'^\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$.]*)\s*' + ANNOT + r'=\s*$', prefix)
    if am:
        return name_settled(code, am.group(1), pos)
    return prefix.strip() not in ('', 'void')


CLEANUP_CALL = re.compile(r'\b(?:rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync|close|kill|dispose|destroy|cleanup|cleanUp|'
                          r'teardown|stop|terminate|restore|remove|removeSync|abort|disconnect|shutdown|release|end|'
                          r'unmount|reset|clear|delete)\w*\s*\(')


def only_cleanup(block: str) -> bool:
    """Every statement of the block calls something that tidies up (rm, close, kill, dispose, unlink ...)."""
    stmts = [x.strip() for x in re.split(r'[;\n]', block) if x.strip() and x.strip() not in ('{', '}')]
    return bool(stmts) and all(CLEANUP_CALL.search(x) for x in stmts)


def spans_of(code: str, rx: str) -> list[tuple[int, int]]:
    """{ } spans of the blocks that rx (ending at the opening brace) introduces."""
    out = []
    for m in re.finditer(rx, code):
        out.append((m.end() - 1, match_code(code, m.end() - 1)))
    return out


def object_member_functions(code: str) -> list[tuple[int, int]]:
    """Bodies of functions written as object members (`runStep(step) { ... }`, `run: async () => { ... }`): the methods
    of a fake or stub the test builds, not the test's own code."""
    out = spans_of(code, r'(?<![\w$.])(?!if\b|for\b|while\b|switch\b|catch\b|function\b)[A-Za-z_$][\w$]*\s*\([^()]*\)\s*'
                         r'(?::\s*[^{=;]+)?\{')
    out += spans_of(code, r'[{,]\s*[A-Za-z_$][\w$]*\s*:\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*'
                          r'(?::\s*[^{=;]+)?=>\s*\{')
    return out


def js_imports(text: str, code: str, lits: dict) -> dict[str, tuple[str, str]]:
    """local name -> (module specifier, imported name or 'default' or '*') for ES imports and simple requires."""
    out = {}
    for m in re.finditer(r'\bimport\s+(type\s+)?([^;]*?)\s*\bfrom\s*(["\'])', code):
        if m.group(1):
            continue
        q = m.end() - 1
        if q not in lits:
            continue
        spec = text[q + 1:lits[q][1] - 1]
        clause = text[m.start(2):m.end(2)]
        dm = re.match(r'\s*([A-Za-z_$][\w$]*)\s*(,|$)', clause)
        if dm:
            out[dm.group(1)] = (spec, 'default')
        nm = re.search(r'\*\s*as\s+([A-Za-z_$][\w$]*)', clause)
        if nm:
            out[nm.group(1)] = (spec, '*')
        bm = re.search(r'\{([^}]*)\}', clause)
        if bm:
            for part in bm.group(1).split(','):
                part = re.sub(r'^\s*type\s+', '', part).strip()
                if not part:
                    continue
                orig, _, local = part.partition(' as ')
                out[(local or orig).strip()] = (spec, orig.strip())
    for m in re.finditer(r'\b(?:const|let|var)\s+(\{[^}]*\}|[A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*(["\'])', code):
        q = m.end() - 1
        if q not in lits:
            continue
        spec = text[q + 1:lits[q][1] - 1]
        if m.group(1).startswith('{'):
            for part in m.group(1).strip('{}').split(','):
                orig, _, local = part.partition(':')
                if orig.strip():
                    out[(local or orig).strip()] = (spec, orig.strip())
        else:
            out[m.group(1)] = (spec, '*')
    return out


def js_mocked_specs(text: str, code: str, lits: dict) -> set[str]:
    out = set()
    for m in re.finditer(r'\b(?:vi|jest)\s*\.\s*(?:mock|doMock)\s*\(\s*(["\'`])', code):
        q = m.end() - 1
        if q in lits:
            out.add(text[q + 1:lits[q][1] - 1])
    return out


def playwright_dirs(root: Path, pkg: str) -> list[str]:
    """testDir values (repo-relative) from the package's playwright.config.*."""
    base = root / pkg if pkg != '.' else root
    out = []
    for cfg in sorted(base.glob('playwright.config.*')):
        text = cfg.read_text(encoding='utf-8', errors='replace')
        for m in re.finditer(r'\btestDir\s*:\s*["\'`]([^"\'`]+)["\'`]', text):
            d = (PurePosixPath(pkg) / m.group(1)).as_posix() if pkg != '.' else PurePosixPath(m.group(1)).as_posix()
            d = re.sub(r'^(\./)+', '', d).rstrip('/')
            out.append(d)
    return out


def body_rules(rel, tid, line_of, body_off, bcode, btext, imports, mocked_specs, add, product=lambda s: bool(PRODUCT_SPEC.match(s)),
               array_names=frozenset(), assert_rx=None, expect_names=frozenset(), under_test=frozenset()):
    """The can't-fail rules on one test body (masked code bcode, raw text btext, both starting at body_off). assert_rx
    matches assertions, including the file's expect aliases and assertion helpers; expect_names are the aliases."""
    assert_rx = assert_rx or ASSERT_JS
    asserts = [m.start() for m in assert_rx.finditer(bcode)]
    if not asserts:
        q = len(IMPLICIT_QUERY.findall(bcode))
        add('no_assertion', 'no_assertion', [tid], None,
            'the test body has no expect or assert (comments are not code: a commented-out expect does not count)' +
            (f'; it uses {q} getBy/findBy quer{"y" if q == 1 else "ies"}, which throw when nothing matches, so it checks '
             'only that elements exist' if q else ''), {'implicit_queries': q} if q else {})
    # async expectations and tautologies
    expect_rx = re.compile(r'(?<![\w$.])(?:' + '|'.join(re.escape(n) for n in sorted({'expect'} | set(expect_names))) +
                           r')(?:\s*\.\s*soft)?\s*\(')
    for e in expect_rx.finditer(bcode):
        close_e = match_code(bcode, e.end() - 1)
        rest = bcode[close_e + 1:close_e + 40]
        rm = re.match(r'\s*\.\s*(resolves|rejects)\b', rest)
        if rm and not promise_settled(bcode, e.start()):
            add('async_not_awaited', 'resolves_not_awaited', [tid], line_of(body_off + e.start()),
                'expect(...).resolves/.rejects is neither awaited nor returned, so the assertion can finish after the test',
                {'matcher': rm.group(1)})
        inner = btext[e.end():close_e].strip()
        tb = re.match(r'\s*\.\s*(toBe|toEqual|toStrictEqual)\s*\(', rest)
        if tb and re.match(r'[\w$]+', e.group(0)).group(0) not in under_test:
            # an expect the product itself exports (a testing library testing its own matchers) is the code under test:
            # e2eExpect(1).toBe(1) checks toBe, it is no tautology
            arg_open = close_e + 1 + tb.end() - 1
            arg = btext[arg_open + 1:match_code(bcode, arg_open)].strip()
            if (arg == inner and '(' not in inner) or (inner in ('true', '1') and arg in ('true', '1')):
                add('tautology', 'compare_to_self', [tid], line_of(body_off + e.start()),
                    'the expectation compares a value with itself', {})
    # try/catch: a catch that swallows the assertion or the product's error
    catches = []
    finally_spans = spans_of(bcode, r'\bfinally\s*\{')
    member_spans = object_member_functions(bcode)
    for t in re.finditer(r'\btry\s*\{', bcode):
        t_open = t.end() - 1
        t_close = match_code(bcode, t_open)
        cm = re.match(r'\s*catch\s*(\([^)]*\))?\s*\{', bcode[t_close + 1:])
        if not cm:
            continue
        c_open = t_close + 1 + cm.end() - 1
        c_close = match_code(bcode, c_open)
        catches.append((c_open, c_close))
        cbody = bcode[c_open + 1:c_close]
        guarded = bool(assert_rx.search(bcode[t_open:t_close]))
        reports = re.search(r'\bthrow\b|\bfail\s*\(|\bdone\s*\(', cbody) or assert_rx.search(cbody)
        # tidying up is allowed to fail quietly: a catch in a finally block, around nothing but cleanup calls, or in a
        # method of a fake object the test builds (its runStep, not the test's own code)
        cleanup = any(a < t.start() < b for a, b in finally_spans + member_spans) or only_cleanup(bcode[t_open + 1:t_close])
        if guarded and not reports:
            add('swallowed_failure', 'catch_around_expect', [tid], line_of(body_off + c_open),
                'the catch block catches the error the expectation in its try block throws, and neither rethrows nor fails', {})
        elif not cbody.strip() and not cleanup:
            add('swallowed_failure', 'empty_catch', [tid], line_of(body_off + c_open),
                'an empty catch swallows any error from the code in its try block, so the test passes when that code throws', {})
    for m in re.finditer(r'\.\s*catch\s*\(\s*(?:\(\s*[\w$]*\s*\)|[\w$]+)\s*=>\s*(?:\{\s*\}|undefined|null|void\s+0)\s*\)'
                         r'|\.\s*catch\s*\(\s*(?:noop|\(\)\s*=>\s*\{\s*\})\s*\)', bcode):
        stmt = bcode[bcode.rfind('\n', 0, m.start()) + 1:m.start()]
        if re.search(r'\.\s*waitFor\w*\s*\(', stmt):
            continue                                        # a best-effort wait (waitForLoadState('networkidle')), not a check
        if CLEANUP_CALL.search(stmt) or any(a < m.start() < b for a, b in finally_spans + member_spans):
            continue                                        # cleanup allowed to fail quietly (rm(dir).catch(() => {}))
        add('swallowed_failure', 'empty_catch', [tid], line_of(body_off + m.start()),
            'an empty .catch() swallows the rejection, so the test passes when the promise fails', {})
    # assertions that may never run
    if asserts:
        loops = []
        for m in re.finditer(r'\bfor\s*(?:await\s*)?\(', bcode):
            h_close = match_code(bcode, m.end() - 1)
            header = bcode[m.end():h_close]
            it = re.search(r'\b(?:of|in)\s+(.*)$', header, re.S) or re.search(r'\b(?:of|in)(\s*\[.*)$', header, re.S)
            if ';' in header or (it and (it.group(1).lstrip().startswith('[') or it.group(1).strip() in array_names)):
                continue                                    # a literal array (inline or a const holding one), or a counted loop
            bm = re.match(r'\s*\{', bcode[h_close + 1:])
            end = match_code(bcode, h_close + 1 + bm.end() - 1) if bm else bcode.find(';', h_close)
            loops.append((m.start(), end if end > 0 else len(bcode)))
        for m in re.finditer(r'\.\s*(forEach|map|flatMap|filter|some|every|find)\s*\(', bcode):
            before = bcode[:m.start()].rstrip()
            recv = re.search(r'([A-Za-z_$][\w$]*)$', before)
            if before.endswith(']') or (recv and recv.group(1) in array_names and not before[:recv.start()].rstrip().endswith('.')):
                continue
            loops.append((m.start(), match_code(bcode, m.end() - 1)))
        if loops and all(any(a <= p <= b for a, b in loops) for p in asserts):
            add('unreached_assertion', 'assert_in_loop', [tid], line_of(body_off + min(a for a, _ in loops)),
                'every assertion is inside a loop over a value that is not an array literal, so none runs when it is empty', {})
        if catches and all(any(a <= p <= b for a, b in catches) for p in asserts):
            add('unreached_assertion', 'assert_in_catch', [tid], line_of(body_off + catches[0][0]),
                'the only assertions are inside catch, so they run only if the call throws; nothing fails when it does not', {})
        fm = re.search(r'=>\s*\{|\bfunction\b[^{]*\{', bcode)
        if fm:
            f_open = fm.end() - 1
            f_close = match_code(bcode, f_open)
            depth = 0
            k = f_open + 1
            while k < f_close:
                c = bcode[k]
                if c in '([{':
                    depth += 1
                elif c in ')]}':
                    depth -= 1
                elif depth == 0 and bcode.startswith('return', k) and not (bcode[k - 1].isalnum() or bcode[k - 1] in '_$') \
                        and not (k + 6 < len(bcode) and (bcode[k + 6].isalnum() or bcode[k + 6] in '_$')):
                    before = bcode[f_open + 1:k].rstrip()
                    guard = before.endswith('else') and not (before[-5:-4].isalnum() or before[-5:-4] in '_$')
                    if before.endswith(')'):
                        word = re.search(r'([A-Za-z_$][\w$]*)\s*$', bcode[f_open + 1:match_back(bcode, f_open + 1 + len(before) - 1)])
                        guard = bool(word and word.group(1) in ('if', 'while', 'for'))
                    if guard:
                        k += 6                              # if (x) return; only ends the test sometimes
                        continue
                    j, d2 = k + 6, 0
                    while j < f_close:
                        cj = bcode[j]
                        if cj in '([{':
                            d2 += 1
                        elif cj in ')]}':
                            if d2 == 0:
                                break
                            d2 -= 1
                        elif d2 == 0 and cj in ';\n':
                            break
                        j += 1
                    if any(p > j for p in asserts if p < f_close):
                        add('unreached_assertion', 'assert_after_return', [tid], line_of(body_off + k),
                            'assertions after return never run', {})
                    break
                k += 1
    # the subject is a mock
    called = {name for name in imports if product(imports[name][0]) and
              re.search(rf'(?<![\w$.]){re.escape(name)}\s*(?:\.\s*[\w$]+\s*)?\(|<{re.escape(name)}\b', bcode)}
    spied = set(re.findall(r'\b(?:vi|jest)\s*\.\s*spyOn\s*\(\s*([\w$]+)\s*,\s*["\'`]', btext))
    mocked = {nm for nm in called if imports[nm][0] in mocked_specs}
    if asserts and called and mocked == called:
        subjects = []
        for e in re.finditer(r'\bexpect(?:\s*\.\s*soft)?\s*\(', bcode):
            subjects.append(btext[e.end():match_code(bcode, e.end() - 1)])
        if subjects and all(any(re.search(rf'(?<![\w$.]){re.escape(nm)}\b', s) for nm in mocked | spied) for s in subjects):
            add('mock_only', 'subject_is_mocked', [tid], None,
                f'every product function the test calls ({", ".join(sorted(mocked))}) comes from a module it mocks '
                f'({", ".join(sorted({imports[nm][0] for nm in mocked}))}), and every expectation is on a mock, so it only '
                'checks the mock', {'patched': sorted(mocked)})


def js_tokens(bcode: str, text: str, lits: dict, off: int) -> list[str]:
    """Body tokens for similarity: local variable names are replaced, while called functions, JSX components, member
    names, keywords and literal values (strings and numbers) are kept. A JavaScript duplicate therefore means the same
    calls with the same values, not just the same shape: there is no per-test coverage run for JavaScript to tell a
    duplicate from an overlap later."""
    raw = [(m.group(0), m.start()) for m in re.finditer(r'[A-Za-z_$][\w$]*|\d[\w.]*|[^\s\w]', bcode)]
    out, prev, skip_to = [], '', -1
    for k, (t, pos) in enumerate(raw):
        if pos < skip_to:
            continue
        lit = lits.get(off + pos)
        if lit and t in '"\'`' and lit[0] in ('string', 'template'):
            out.append('S:' + text[off + pos + 1:lit[1] - 1])
            skip_to, prev = lit[1] - off, 'S'
            continue
        nxt = raw[k + 1][0] if k + 1 < len(raw) else ''
        key = nxt == ':' and prev in ('{', ',')               # an object key ({ has: ... } vs { hasNot: ... }) is behaviour
        if (t[0].isalpha() or t[0] in '_$') and t not in JS_KEYWORDS and prev not in ('.', '<') and nxt != '(' and not key:
            t = 'v'                                         # a variable
        out.append(t)
        prev = raw[k][0]
    return out


def js_scan(root: Path, rel: str, pkg: str, add, defs, js_bodies: list, pw_dirs: list[str]):
    text = (root / rel).read_text(encoding='utf-8', errors='replace')
    code, lits = js_lex(text)
    starts = [0] + [k + 1 for k, c in enumerate(text) if c == '\n']

    def line_of(pos: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1
    playwright = bool(re.search(r'["\']@playwright/test["\']', text)) or any(rel == d or rel.startswith(d + '/') for d in pw_dirs)
    imports = js_imports(text, code, lits)
    pkg_dir = root / pkg if pkg != '.' else root
    file_path = root / rel
    file_fw = [spec for spec, _ in imports.values() if FRAMEWORK_MODULES.match(spec)]
    origins: dict[str, tuple[str, str]] = {}

    def origin(local: str) -> tuple[str, str]:
        """framework, custom (a repository's own runner: a package import in a file that imports no framework) or
        product (the product's own test() API, used inside a framework's tests to build fixtures)."""
        if local not in origins:
            spec, orig = imports[local]
            o = spec_origin(file_path, spec, orig if orig not in ('default', '*') else local, pkg_dir)
            if o[0] == 'bare':
                o = ('product', o[1]) if file_fw else ('custom', o[1])
            origins[local] = o
        return origins[local]

    def is_head_name(nm: str) -> bool:
        if nm in imports:
            return origin(nm)[0] in ('framework', 'custom')
        return nm in JS_HEADS                              # a global (Vitest globals, Jest, Mocha)
    head_alias = {local: orig for local, (spec, orig) in imports.items() if orig in JS_HEADS and local not in JS_HEADS}
    active = {nm for nm in set(JS_HEADS) | set(head_alias) if is_head_name(nm)}
    hidden = {nm: local_head_scopes(code, nm, is_head_name) for nm in active}
    heads = '|'.join(sorted(active, key=len, reverse=True)) or r'(?!x)x'
    call_rx = re.compile(rf'(?<![\w$.])({heads})((?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*(?:{GENERIC}\s*)?(?=[(`])')
    used_origins = [origin(nm) for nm in active if nm in imports]
    custom = next((o[1] for o in used_origins if o[0] == 'custom'), None)
    fw_spec = next((o[1] for o in used_origins if o[0] == 'framework'), None)
    playwright = playwright or bool(fw_spec and 'playwright' in fw_spec)
    mocked_specs = js_mocked_specs(text, code, lits)
    aliases = ts_aliases(pkg_dir)
    array_names = set(re.findall(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*' + ANNOT + r'=\s*\[', code))
    product = lambda spec: is_product(spec, aliases)  # noqa: E731

    def support(spec: str) -> bool:
        """The import names a test helper, fixture or mock module: never the code under test."""
        target = resolve_module(file_path, spec, pkg_dir) if product(spec) else None
        if target is None:
            return False
        try:
            return is_test_support(root, target.relative_to(root.resolve()).as_posix())
        except ValueError:
            return False
    # assertions: expect/assert under another name, and helpers (same file or imported) whose bodies assert
    expect_names = expect_aliases(imports)
    product_expects = {local for local, (spec, orig) in imports.items() if orig == 'expect' and product(spec)
                       and origin(local)[0] == 'product'} if file_fw else set()
    imported_helpers = set()
    for local, (spec, orig) in imports.items():
        if product(spec) and re.search(rf'(?<![\w$.]){re.escape(local)}\s*\(', code):
            target = resolve_module(file_path, spec, pkg_dir)
            if target is not None and orig in module_info(target)['helpers']:
                imported_helpers.add(local)
    helper_names = assert_helpers(code, expect_names | imported_helpers) | imported_helpers
    assert_rx = assert_rx_for(expect_names | helper_names)
    blocks, tests, notes = [], [], []                      # blocks: dicts with start, end, name, param, skip, only
    for m in call_rx.finditer(code):
        if any(a <= m.start() <= b for a, b in hidden.get(m.group(1), [])):
            continue                                       # a local function or value named test, not the test head
        head = head_alias.get(m.group(1), m.group(1))
        mods = re.findall(r'[A-Za-z_$][\w$]*', m.group(2))
        pos = m.end()
        pre = None
        if mods and mods[-1] in ('each', 'for', 'skipIf', 'runIf'):
            if code[pos] == '(':
                c2 = match_code(code, pos)
                pre, pos = (pos + 1, c2), c2 + 1
            elif code[pos] == '`' and pos in lits:
                pre, pos = (pos + 1, lits[pos][1] - 1), lits[pos][1]
            while pos < len(code) and code[pos].isspace():
                pos += 1
            if pos >= len(code) or code[pos] != '(':
                continue
        elif code[pos] != '(':
            continue
        close = match_code(code, pos)
        args = split_args(code, pos + 1, close)
        lit = literal_at(text, lits, args[0] if args else None)
        is_block = head in ('describe', 'suite', 'xdescribe', 'fdescribe') or (head == 'test' and mods[:1] == ['describe'])
        bmods = mods[1:] if head == 'test' and mods[:1] == ['describe'] else mods
        if is_block:
            if 'configure' in bmods:
                continue
            if lit is None and args and re.match(r'(async\s*)?(\(|function\b)', code[args[0][0]:args[0][1]]):
                name, param = '', False                    # Playwright's anonymous test.describe(() => { ... })
            elif lit is None:
                name, param = text[args[0][0]:args[0][1]] if args else '?', True
            else:
                name, param = lit
            blocks.append({'start': m.start(), 'end': close, 'name': name, 'param': param or 'each' in bmods or 'for' in bmods,
                           'skip': head == 'xdescribe' or bool({'skip', 'fixme', 'todo'} & set(bmods)) or 'skipIf' in bmods,
                           'skip_kind': f'{head}.{"/".join(bmods)}' if bmods else head, 'only': head == 'fdescribe' or 'only' in bmods,
                           'cond': text[pre[0]:pre[1]].strip() if pre and 'skipIf' in bmods else None,
                           'line': line_of(m.start())})
            continue
        if mods and mods[0] in NOT_TESTS:
            continue
        if any(x not in TEST_MODS for x in mods):
            continue
        if lit is None and not ({'each', 'for'} & set(mods)):
            if {'skip', 'fixme'} & set(mods) or head in ('xit', 'xtest'):
                # a runtime annotation (Playwright): test.skip(), test.skip(condition, reason), test.fixme(condition)
                cond = text[args[0][0]:args[0][1]].strip() if args else None
                reason_lit = literal_at(text, lits, args[1] if len(args) > 1 else None)
                tests.append({'kind': 'annotation', 'start': m.start(), 'end': close, 'mods': mods, 'head': head,
                              'condition': None if cond in (None, 'true') else cond,
                              'reason': reason_lit[0] if reason_lit else (lit[0] if lit else None), 'line': line_of(m.start())})
                continue
            if args:
                lit = (text[args[0][0]:args[0][1]], True)  # a computed name: matched by pattern only
            else:
                continue
        name, param = lit
        param = param or bool({'each', 'for'} & set(mods))
        body = args[1] if len(args) > 1 else None
        tests.append({'kind': 'test', 'start': m.start(), 'end': close, 'name': name, 'param': param, 'mods': mods, 'head': head,
                      'body': (body[0], close) if body else None, 'line': line_of(m.start()),
                      'cond': text[pre[0]:pre[1]].strip() if pre and mods[-1] in ('skipIf', 'runIf') else None})
    lang = 'typescript' if rel.endswith(('.ts', '.tsx', '.mts', '.cts')) else 'javascript'
    # playwright, node-test, custom (the repository's own runner, named by runner_module) or js (Vitest or Jest, decided
    # per package by collect_check.py)
    framework = 'playwright' if playwright else 'custom' if custom else runner_of(fw_spec) if fw_spec else 'js'
    by_block: dict[int, list[str]] = {}
    defined = []
    for t in tests:
        if t['kind'] != 'test':
            continue
        parents = sorted((b for b in blocks if b['start'] < t['start'] < b['end']), key=lambda b: b['start'])
        parts = [b for b in parents if b['name'] != '']
        full = ' > '.join([b['name'] for b in parts] + [t['name']])
        param = t['param'] or any(b['param'] for b in parts)
        pattern = ' > '.join([name_regex(b['name'], b['param']) for b in parts] + [name_regex(t['name'], t['param'])]) \
            if param else None
        tid = f'{rel}::{full}'
        t['id'] = tid
        defined.append(t)
        for b in parents:
            by_block.setdefault(b['start'], []).append(tid)
        bcode = code[t['body'][0]:t['body'][1]] if t['body'] else ''
        btext = text[t['body'][0]:t['body'][1]] if t['body'] else ''
        # targets for neutralise.py: names imported from the product (relative or alias paths, not mocked) that the body
        # calls or renders as JSX
        calls = sorted(nm for nm, (spec, orig) in imports.items()
                       if product(spec) and spec not in mocked_specs and nm not in helper_names and nm not in active and
                       orig not in ('expect', 'assert') and
                       re.search(rf'(?<![\w$.]){re.escape(nm)}\s*(?:\.\s*[\w$]+\s*)?\(|<{re.escape(nm)}\b', bcode)
                       and not support(spec))
        conds = [b['cond'] for b in parents if b.get('cond')] + ([t['cond']] if t.get('cond') else [])
        defs.append({'id': tid, 'file': rel, 'line': t['line'], 'name': full, 'lang': lang, 'package': pkg, 'framework': framework,
                     'calls': calls, 'parametrised': param, 'name_pattern': pattern,
                     **({'runner_module': custom} if framework == 'custom' else {}),
                     **({'skip_condition': ' and '.join(conds)} if conds else {})})
        mods, head = t['mods'], t['head']
        if 'only' in mods or head == 'fit':
            add('never_runs', 'only', [tid], t['line'], f'{head}.only silences every other test in the file', {'marker': f'{head}.only'})
        skip_mod = next((x for x in mods if x in ('skip', 'todo', 'fixme')), None) or ('skip' if head in ('xit', 'xtest') else None)
        if skip_mod:
            key = KEY.search(text[t['start']:t['start'] + 300])
            add('skipped', f'{head}.{skip_mod}', [tid], t['line'], f'{head}.{skip_mod}',
                {'kind': f'{head}.{skip_mod}', 'reason': None, 'conditional': False, 'condition': None,
                 'ticket': key.group(1) if key else None})
            continue
        if 'skipIf' in mods or 'runIf' in mods:
            add('skipped', f'{head}.{mods[-1]}', [tid], t['line'], f'{head}.{mods[-1]}({t["cond"]})',
                {'kind': f'{head}.{mods[-1]}', 'reason': None, 'conditional': True, 'condition': t['cond'], 'ticket': None})
        if not t['body']:
            continue

        def add_here(category, rule, ids, line, why, details, _t=t):
            add(category, rule, ids, line or _t['line'], why, details)
        body_rules(rel, tid, line_of, t['body'][0], bcode, btext, imports, mocked_specs, add_here, product, array_names,
                   assert_rx, expect_names, product_expects)
        if assert_rx.search(bcode):
            toks = js_tokens(bcode, text, lits, t['body'][0])
            if len(toks) >= RULES['javascript'].get('duplicate_min_tokens', 40):
                js_bodies.append((tid, rel, t['line'], toks))
    for b in blocks:
        inside = by_block.get(b['start'], []) or [f'{rel}::*']
        if b['only']:
            add('never_runs', 'only', [f'{rel}::*'], b['line'], f'{b["skip_kind"]} at line {b["line"]} silences every other test '
                'in the file', {'marker': b['skip_kind']})
        if b['skip']:
            key = KEY.search(text[b['start']:b['start'] + 300])
            add('skipped', b['skip_kind'], inside, b['line'], f'{b["skip_kind"]} skips the whole block ({len(inside)} tests)',
                {'kind': b['skip_kind'], 'reason': None, 'conditional': bool(b['cond']), 'condition': b['cond'],
                 'ticket': key.group(1) if key else None})
    for a in tests:
        if a['kind'] != 'annotation':
            continue
        owner = next((t for t in defined if t['body'] and t['body'][0] <= a['start'] < t['body'][1]), None)
        if owner is not None:
            ids, where = [owner['id']], f'inside the test at line {owner["line"]}'
        else:
            parents = [b for b in blocks if b['start'] < a['start'] < b['end']]
            block = max(parents, key=lambda b: b['start']) if parents else None
            ids = (by_block.get(block['start'], []) if block else [t['id'] for t in defined]) or [f'{rel}::*']
            where = f'in the describe block at line {block["line"]}' if block else 'at the top of the file'
        kind = f'{a["head"]}.{a["mods"][0]}' if a['mods'] else a['head']
        key = KEY.search(a['reason'] or '')
        add('skipped', f'{kind} (runtime)', ids, a['line'],
            f'runtime {kind}({a["condition"] or ""}) {where}' + (f': {a["reason"]}' if a['reason'] else ''),
            {'kind': f'{kind} (runtime)', 'reason': a['reason'], 'conditional': a['condition'] is not None,
             'condition': a['condition'], 'ticket': key.group(1) if key else None})
    return notes


# ---------------------------------------------------------------- duplicates
def symbol_masks(b: list[str]) -> dict[str, int]:
    """For each token, an int with bit i set where b[i] is that token."""
    out: dict[str, int] = {}
    for i, t in enumerate(b):
        out[t] = out.get(t, 0) | (1 << i)
    return out


def lcs_length(a: list[str], b_masks: dict[str, int], m: int) -> int:
    """Length of the longest common subsequence of a and b (Hyyro's bit-parallel algorithm; b given as symbol_masks)."""
    full = (1 << m) - 1
    v = full
    for t in a:
        u = v & b_masks.get(t, 0)
        v = ((v + u) | (v - u)) & full
    return m - bin(v).count('1')


def duplicates(pyscanners: list[PyScanner], add, threshold: float, js_bodies: list | None = None):
    """Pairs whose normalised token sequences have a SequenceMatcher ratio >= threshold.

    The exact ratio is O(n*m) per pair, so pairs are pruned first with two bounds that can never drop a pair at or
    above the threshold (the result is the same as comparing every pair):
      - length: ratio <= 2*min/(len_a+len_b), so with the tests sorted by length only a window of similar lengths is
        compared;
      - token counts: ratio <= 2*|multiset intersection|/(len_a+len_b), computed from per-test token counts.
      - longest common subsequence: SequenceMatcher's matching blocks are a common subsequence, so
        ratio <= 2*LCS/(len_a+len_b); the LCS length comes from a bit-parallel algorithm, about a millisecond a pair.
    Identical token sequences score 1.0 without a comparison. Only the pairs left get the exact SequenceMatcher ratio,
    with the pair kept in its original order (a before b).

    Python tests are compared with Python tests (normalised AST dumps) and JavaScript/TypeScript tests with
    JavaScript/TypeScript tests (js_bodies: body tokens with variable names replaced but calls and literal values kept,
    bodies of at least javascript.duplicate_min_tokens tokens, at javascript.duplicate_similarity, 1.0 by default: there
    is no per-test coverage run for JavaScript, so only bodies identical up to variable names are candidates), never
    across languages."""
    near = 0
    by_pkg: dict[str, list] = {}
    for s in pyscanners:
        for tid, fn, _ in s.tests():
            if any(is_assertion(n) for n in ast.walk(fn)):
                toks = tokens(normalise(fn))
                by_pkg.setdefault(s.pkg, []).append((tid, s.rel, fn.lineno, toks, Counter(toks), literal_values(fn)))
    for fns in by_pkg.values():                                 # never across packages: each tests its own code
        near += similar_pairs(fns, add, threshold)
    js_by_pkg: dict[str, list] = {}
    for t, r, ln, toks in (js_bodies or []):
        js_by_pkg.setdefault(package_of.get(r, '.'), []).append((t, r, ln, toks, Counter(toks), None))
    for fns in js_by_pkg.values():
        similar_pairs(fns, add, RULES['javascript'].get('duplicate_similarity', 1.0))
    return near


def literal_values(fn: ast.AST) -> list[str]:
    """The literal values of a test (the constants normalise() hides), sorted, to tell a duplicate from an input case."""
    return sorted(repr(n.value) for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value is not None
                  and not (isinstance(n.value, str) and n.value == '') and not isinstance(n.value, bool))


package_of: dict[str, str] = {}


def similar_pairs(fns: list, add, threshold: float) -> int:
    """Flag the pairs at or above the threshold; returns how many were left out because their literal values differ
    (the same test written for other inputs: a parametrisation opportunity, not a duplicate)."""
    order = sorted(range(len(fns)), key=lambda i: len(fns[i][3]))
    masks: dict[int, dict[str, int]] = {}
    pairs = []
    for x, i in enumerate(order):
        li = len(fns[i][3])
        for j in order[x + 1:]:
            lj = len(fns[j][3])
            if li == 0 or li / lj < threshold:
                break                                           # every later j is longer still
            ci, cj = fns[i][4], fns[j][4]
            small, big = (ci, cj) if len(ci) <= len(cj) else (cj, ci)
            common = sum(min(v, big.get(k, 0)) for k, v in small.items())
            if 2 * common / (li + lj) < threshold:
                continue
            if fns[i][3] != fns[j][3]:
                if j not in masks:
                    masks[j] = symbol_masks(fns[j][3])
                if 2 * lcs_length(fns[i][3], masks[j], lj) / (li + lj) < threshold:
                    continue
            pairs.append((min(i, j), max(i, j)))
    matchers: dict[int, difflib.SequenceMatcher] = {}
    near = 0
    for i, j in sorted(pairs, key=lambda p: (p[1], p[0])):     # one matcher per b, so its index is built once
        a, b = fns[i], fns[j]
        if a[3] == b[3]:
            ratio = 1.0
        else:
            sm = matchers.get(j)
            if sm is None:
                matchers.clear()
                sm = matchers[j] = difflib.SequenceMatcher(None, autojunk=False)
                sm.set_seq2(b[3])
            sm.set_seq1(a[3])
            ratio = sm.ratio()
        if ratio >= threshold and a[5] is not None and a[5] != b[5]:
            near += 1                                           # the same test for other inputs: not a duplicate
        elif ratio >= threshold:
            add('duplicate', 'ast_similarity', [a[0], b[0]], b[2],
                f'{ratio:.0%} similar to {a[0]} after normalising names and literals',
                {'similarity': round(ratio, 4)}, file=b[1])
    return near


def is_playwright_setup(root: Path, rel: str, pkg: str) -> bool:
    """A *.setup.ts file is a Playwright setup project (auth.setup.ts) when it imports @playwright/test or sits in a
    Playwright testDir; a Vitest or Jest setup file (vitest.setup.ts) is not a test file."""
    if any(rel == d or rel.startswith(d + '/') for d in playwright_dirs(root, pkg)):
        return True
    return bool(re.search(r'["\']@playwright/test["\']', (root / rel).read_text(encoding='utf-8', errors='replace')))


def scan(root: Path, extra_test_files: list[str] | None = None) -> dict:
    files = walk(root)
    extra = list(extra_test_files or [])
    det = detect_conventions.Detector(root, 50_000).run(None)
    roots = sorted({a['root'] for a in det['areas']}, key=len, reverse=True)
    pkg_of = lambda rel: next((r for r in roots if r != '.' and (rel == r or rel.startswith(r + '/'))), '.')  # noqa: E731
    cands, defs, notes = [], [], []

    def adder(rel):
        def add(category, rule, test_ids, line, why, details, file=None):
            cands.append({'category': category, 'rule': rule, 'test_ids': test_ids, 'file': file or rel, 'line': line, 'why': why,
                          'details': details})
        return add

    pyscanners, js_bodies, pw_dirs = [], [], {}
    for rel in files:
        name = PurePosixPath(rel).name
        pkg = pkg_of(rel)
        if rel.endswith('.py') and (in_test_dir(root, rel) or detect_conventions.is_test_file(rel)) and name != 'conftest.py':
            try:
                s = PyScanner(root, rel, pkg, pytest_config(root, pkg))
            except SyntaxError as exc:
                notes.append(f'{rel}: not parsed ({exc.msg})')
                continue
            if not s.tests():
                continue
            pyscanners.append(s)
            s.scan(adder(rel), defs)
            package_of[rel] = pkg
        elif any(fnmatch.fnmatch(name, p) for p in JS['test_files']) or (
                rel.endswith(JS_EXT) and any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in extra)) or (
                any(fnmatch.fnmatch(name, p) for p in JS['playwright_setup_files']) and is_playwright_setup(root, rel, pkg)):
            if pkg not in pw_dirs:
                pw_dirs[pkg] = playwright_dirs(root, pkg)
            package_of[rel] = pkg
            try:
                js_scan(root, rel, pkg, adder(rel), defs, js_bodies, pw_dirs[pkg])
            except (RecursionError, IndexError, ValueError) as exc:
                notes.append(f'{rel}: not parsed ({exc!r}); its tests are missing from definitions')
    near = duplicates(pyscanners, adder(None), RULES['similarity_threshold'], js_bodies)
    out = []
    for i, c in enumerate(sorted(cands, key=lambda c: (c['file'], c['line'], c['category'])), 1):
        out.append({'id': f'C{i}', **c})
    result = {'repo': root.name, 'definitions': defs, 'candidates': out, **({'extra_test_files': extra} if extra else {}),
              'pytest_config': {r: pytest_config(root, r) for r in roots if (root / r / 'pyproject.toml').exists() or r == '.'},
              'summary': {'tests_defined': len(defs), 'candidates': len(out),
                          'by_category': {k: len([c for c in out if c['category'] == k]) for k in sorted({c['category'] for c in out})},
                          **({'near_duplicates': near} if near else {})},
              'notes': notes + ([f'{near} pairs of same-shape tests in one package differ only in their literal values (input '
                                 'cases, worth parametrising); they are not duplicate candidates'] if near else [])}
    guard = unsupported_dominant(language_breakdown(files))
    if guard:
        result['language_coverage'] = guard
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--test-files', action='append', default=[], metavar='GLOB',
                    help='also scan JavaScript/TypeScript files matching GLOB (a file name such as "*.e2e.ts" or a '
                         'repo-relative path pattern): the suites of a repository\'s own test runner')
    args = ap.parse_args(argv)
    res = scan(args.repo.resolve(), args.test_files)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    guard = res.get('language_coverage')
    if guard:
        print('=' * 78, file=sys.stderr)
        print('WARNING: unsupported dominant language - this is NOT a dead-test analysis.', file=sys.stderr)
        print(guard['warning'], file=sys.stderr)
        print('=' * 78, file=sys.stderr)
    s = res['summary']
    scope = ' (minority supported files only; see WARNING above)' if guard else ''
    print(f'{s["tests_defined"]} tests defined{scope}, {s["candidates"]} candidates: '
          + ', '.join(f'{v} {k}' for k, v in s['by_category'].items()) + f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
