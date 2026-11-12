#!/usr/bin/env python3
"""Find every test definition and the ones that can't fail, never run, or are skipped (stdlib only).

    python scan_tests.py <repo> --out candidates.json

Python test files are parsed with `ast`; JavaScript and TypeScript test files with patterns and
bracket matching (`it`/`test` inside `describe`). Every test function or case found is a
definition, whether or not the framework would collect it; collect_check.py then compares the
definitions with what the framework really collects.

Candidate categories: never_runs (a file outside the collect patterns, a stray .only),
no_assertion, tautology, unreached_assertion, swallowed_failure, async_not_awaited, mock_only,
skipped and duplicate (normalised AST similarity >= the threshold in data/rules.json). Each
candidate cites the test ids, file and line, and why it was flagged.
"""
from __future__ import annotations
import argparse, ast, copy, difflib, fnmatch, json, re, sys
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


def walk(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not set(PurePosixPath(rel).parts) & detect_conventions.SKIP_DIRS:
            out.append(rel)
    return out


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
    for name in ('pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg'):
        p = base / name
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
        where = f'{(pkg + "/") if pkg != "." else ""}{name}'
        conf['source'] = where if found else f'pytest defaults; {where} sets no patterns'
        break
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


class PyScanner:
    def __init__(self, root: Path, rel: str, pkg: str, conf: dict):
        self.root, self.rel, self.pkg, self.conf = root, rel, pkg, conf
        self.text = (root / rel).read_text(encoding='utf-8', errors='replace')
        self.tree = ast.parse(self.text)
        self.imports = self.imported_names()

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
        return sorted({t for t in out if not t.startswith(('pytest', 'unittest', 'mock', 'json', 'os', 'sys', 're'))})

    def rules(self, tid, fn, add, cls=None):
        body_nodes = list(ast.walk(fn))
        asserts = [n for n in body_nodes if is_assertion(n)]
        info = self.skip_info(fn, cls)
        if info is not None:
            add('skipped', info['kind'], [tid], info['line'], f'{info["kind"]}: {info["reason"] or "no reason given"}', info)
        if not asserts:
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
                    not isinstance(n.iter, (ast.List, ast.Tuple, ast.Set)):
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
def match_paren(text: str, i: int) -> int:
    """Index of the bracket closing the one at text[i] (skips strings and comments)."""
    open_c = text[i]
    close_c = {'(': ')', '{': '}', '[': ']'}[open_c]
    depth, j, n = 0, i, len(text)
    while j < n:
        c = text[j]
        if c in '"\'`':
            q = c
            j += 1
            while j < n and text[j] != q:
                j += 2 if text[j] == '\\' else 1
        elif text.startswith('//', j):
            j = text.find('\n', j)
            j = n if j < 0 else j
        elif text.startswith('/*', j):
            j = text.find('*/', j)
            j = n if j < 0 else j + 1
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


def js_mask(text: str) -> str:
    """The text with comments and string contents blanked (quotes and newlines kept, offsets unchanged), so a call
    pattern can't match words in a comment ("before each test (order matters)") or inside a string."""
    out, i, n = list(text), 0, len(text)

    def blank(a: int, b: int):
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
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
        elif text[i] in '"\'`':
            q, j = text[i], i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == '\\' else 1
            blank(i + 1, min(j, n))
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def js_scan(root: Path, rel: str, pkg: str, add, defs):
    text = (root / rel).read_text(encoding='utf-8', errors='replace')
    code = js_mask(text)                                    # calls are found in code; names are read from the text
    line_of = lambda pos: text.count('\n', 0, pos) + 1  # noqa: E731
    blocks = []                                             # (start, end, name) of describe blocks
    for m in re.finditer(JS['describe_call'], code):
        open_i = m.end() - 1
        close = match_paren(text, open_i)
        nm = re.match(r'\s*["\'`]([^"\'`]*)["\'`]', text[open_i + 1:])
        blocks.append((m.start(), close, nm.group(1) if nm else '?', m.group(2) or ''))
        if m.group(3) == 'only':
            add('never_runs', 'only', [f'{rel}::*'], line_of(m.start()), f'describe.only at line {line_of(m.start())} silences every '
                'other test in the file', {'marker': 'describe.only'})
        if m.group(3) == 'skip':
            add('skipped', 'describe.skip', [f'{rel}::*'], line_of(m.start()), 'describe.skip skips the whole block', {'kind': 'describe.skip'})
    for m in re.finditer(JS['test_call'], code):
        open_i = m.end() - 1
        close = match_paren(text, open_i)
        call = text[open_i + 1:close]
        nm = re.match(r'\s*["\'`]([^"\'`]*)["\'`]', call)
        name = nm.group(1) if nm else '?'
        parents = [b[2] for b in blocks if b[0] < m.start() < b[1]]
        full = ' > '.join(parents + [name])
        tid = f'{rel}::{full}'
        line = line_of(m.start())
        defs.append({'id': tid, 'file': rel, 'line': line, 'name': full, 'lang': 'typescript' if rel.endswith(('.ts', '.tsx')) else
                     'javascript', 'package': pkg, 'calls': sorted({c for c in re.findall(r'\b([a-z]\w*)\s*\(', call)
                                                                     if not re.match(r'to[A-Z]', c)} -
                                                                    {'expect', 'it', 'test', 'describe', 'async', 'await'})})
        modifier = m.group(3) or ''
        if modifier == 'only':
            add('never_runs', 'only', [tid], line, f'{m.group(1)}.only silences every other test in the file', {'marker': f'{m.group(1)}.only'})
        if modifier in ('skip', 'todo'):
            key = KEY.search(call[:200])
            add('skipped', f'{m.group(1)}.{modifier}', [tid], line, f'{m.group(1)}.{modifier}', {'kind': f'{m.group(1)}.{modifier}',
                                                                                              'reason': None, 'conditional': False,
                                                                                              'ticket': key.group(1) if key else None})
            continue
        body = call
        if not re.search(JS['assert_pattern'], body):
            add('no_assertion', 'no_assertion', [tid], line, 'the test body has no expect or assert', {})
            continue
        for e in re.finditer(r'expect\s*\(', body):
            stmt_start = body.rfind('\n', 0, e.start()) + 1
            prefix = body[stmt_start:e.start()]
            close_e = match_paren(body, e.end() - 1)
            rest = body[close_e + 1:close_e + 40]
            if re.match(r'\s*\.\s*(resolves|rejects)\b', rest) and not re.search(r'\b(await|return)\s*$', prefix):
                add('async_not_awaited', 'resolves_not_awaited', [tid], line_of(open_i + 1 + e.start()),
                    'expect(...).resolves/.rejects is neither awaited nor returned, so the assertion can finish after the test',
                    {'matcher': re.match(r'\s*\.\s*(resolves|rejects)\b', rest).group(1)})
            inner = body[e.end():close_e].strip()
            tb = re.match(r'\s*\.\s*(toBe|toEqual|toStrictEqual)\s*\(', rest)
            if tb:
                arg_open = close_e + 1 + tb.end() - 1
                arg = body[arg_open + 1:match_paren(body, arg_open)].strip()
                if arg == inner or inner in ('true', '1') and arg in ('true', '1'):
                    add('tautology', 'compare_to_self', [tid], line_of(open_i + 1 + e.start()),
                        'the expectation compares a value with itself', {})
        if re.search(r'\.catch\s*\(\s*\(\s*\w*\s*\)\s*=>\s*\{\s*\}\s*\)|catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}', body):
            add('swallowed_failure', 'empty_catch', [tid], line, 'an empty catch swallows the failure', {})
        if re.search(r'\bdone\b', call[:call.find('=>') if '=>' in call else 0]) and not re.search(r'\bdone\s*\(', body):
            add('async_not_awaited', 'done_never_called', [tid], line, 'the test takes a done callback it never calls', {})


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


def duplicates(pyscanners: list[PyScanner], add, threshold: float):
    """Pairs whose normalised token sequences have a SequenceMatcher ratio >= threshold.

    The exact ratio is O(n*m) per pair, so pairs are pruned first with two bounds that can never drop a pair at or
    above the threshold (the result is the same as comparing every pair):
      - length: ratio <= 2*min/(len_a+len_b), so with the tests sorted by length only a window of similar lengths is
        compared;
      - token counts: ratio <= 2*|multiset intersection|/(len_a+len_b), computed from per-test token counts.
      - longest common subsequence: SequenceMatcher's matching blocks are a common subsequence, so
        ratio <= 2*LCS/(len_a+len_b); the LCS length comes from a bit-parallel algorithm, about a millisecond a pair.
    Identical token sequences score 1.0 without a comparison. Only the pairs left get the exact SequenceMatcher ratio,
    with the pair kept in its original order (a before b)."""
    fns = []
    for s in pyscanners:
        for tid, fn, _ in s.tests():
            if any(is_assertion(n) for n in ast.walk(fn)):
                toks = tokens(normalise(fn))
                fns.append((tid, s.rel, fn, toks, Counter(toks)))
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
        if ratio >= threshold:
            add('duplicate', 'ast_similarity', [a[0], b[0]], b[2].lineno,
                f'{ratio:.0%} similar to {a[0]} after normalising names and literals', {'similarity': round(ratio, 4)},
                file=b[1])


def scan(root: Path) -> dict:
    files = walk(root)
    det = detect_conventions.Detector(root, 50_000).run(None)
    roots = sorted({a['root'] for a in det['areas']}, key=len, reverse=True)
    pkg_of = lambda rel: next((r for r in roots if r != '.' and (rel == r or rel.startswith(r + '/'))), '.')  # noqa: E731
    cands, defs, notes = [], [], []

    def adder(rel):
        def add(category, rule, test_ids, line, why, details, file=None):
            cands.append({'category': category, 'rule': rule, 'test_ids': test_ids, 'file': file or rel, 'line': line, 'why': why,
                          'details': details})
        return add

    pyscanners = []
    for rel in files:
        parts = set(PurePosixPath(rel).parts[:-1])
        name = PurePosixPath(rel).name
        pkg = pkg_of(rel)
        if rel.endswith('.py') and (parts & TEST_DIR_PARTS or detect_conventions.is_test_file(rel)) and name != 'conftest.py':
            try:
                s = PyScanner(root, rel, pkg, pytest_config(root, pkg))
            except SyntaxError as exc:
                notes.append(f'{rel}: not parsed ({exc.msg})')
                continue
            if not s.tests():
                continue
            pyscanners.append(s)
            s.scan(adder(rel), defs)
        elif any(fnmatch.fnmatch(name, p) for p in JS['test_files']):
            js_scan(root, rel, pkg, adder(rel), defs)
    duplicates(pyscanners, adder(None), RULES['similarity_threshold'])
    out = []
    for i, c in enumerate(sorted(cands, key=lambda c: (c['file'], c['line'], c['category'])), 1):
        out.append({'id': f'C{i}', **c})
    return {'repo': root.name, 'definitions': defs, 'candidates': out,
            'pytest_config': {r: pytest_config(root, r) for r in roots if (root / r / 'pyproject.toml').exists() or r == '.'},
            'summary': {'tests_defined': len(defs), 'candidates': len(out),
                        'by_category': {k: len([c for c in out if c['category'] == k]) for k in sorted({c['category'] for c in out})}},
            'notes': notes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = scan(args.repo.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["tests_defined"]} tests defined, {s["candidates"]} candidates: '
          + ', '.join(f'{v} {k}' for k, v in s['by_category'].items()) + f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
