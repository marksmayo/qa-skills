#!/usr/bin/env python3
"""Stdlib mutation engine for Python (pytest) and Go (go test) (the fallback when no tool is set up).

    python builtin_mutator.py --repo <repo> --scope scope.json --run-dir <dir> [--minutes 30]
        [--tests tests/unit [--tests tests/test_x.py ...]] [--pytest-args "-p no:xdist -m 'not slow'"]
        [--timeout-factor 3] [--min-timeout 10] [--max-mutants N] [--coverage-only] [--restore]
        [--language auto|python|go] [--go-run REGEX] [--go-skip REGEX] [--go-flags "-tags integration"]
        [--runner auto|pytest|unittest|selftest] [--selftest-cmd "python tools/x.py --self-test"]

The language is the one the scope's changed source lines are in (--language picks one when both
are). Go is handled by go_mutator.py (text-level operators on Go tokens, `go test -overlay`, so the
repository is never written; see that file); --go-run / --go-skip narrow the Go tests and
--go-flags is added to every go command. A scope with changed source in no supported language
stops with exit 2 and names the files it could not mutate, with the tool to use instead.
The rest of this text is about Python.

--tests limits every pytest run (coverage, the untraced timing run and each mutant's run) to
those test files, directories or node ids: the scoped baseline of SKILL.md Step 3. Without it
pytest collects what the repo's own configuration collects, which may include slow integration
tests that need services. --pytest-args is added to every run too (one quoted string).

1. Coverage: runs the suite once with a small pytest plugin (written to the run directory and
   loaded with -p) that records, per test, the lines it executed in the scoped files. Lines run
   only while importing a module are recorded under "<import>". Written to <dir>/coverage.json.
2. Mutants: AST mutations on the changed lines of the scoped Python source files (see
   data/operators.json, engine "builtin"). Each mutant replaces one node's source text. Ids
   (B1..) follow file, line and column, and builtin.json records "ids": "position", so
   normalize_results.py keeps them: the console, builtin.json and the report use the same id.
   The coverage run also checks that the tests imported each scoped file itself; if they
   imported another copy of the module (an editable install pointing at another clone, an
   installed wheel), the run stops, because no mutant could ever be seen by the tests.
3. Runs: for each mutant, writes the mutated file, runs only the tests that executed that line
   (every test that touched the file, for a module-level line), and restores the original bytes.
   A line inside a function that ran only while modules were imported (test collection: data
   loaded in conftest or at module import, a cached loader, an app factory run at import) is
   covered by every test that touched the file (or, when none did, every test the coverage run
   saw): any of them may depend on what that code built. Such mutants carry a note saying so.
   pytest exit 0 = survived, 1 = killed (the first failing test is recorded), a timeout =
   timeout, any other exit or no parsed results = runtime_error, a mutant that doesn't compile =
   compile_error. A mutant on a line no test ran is no_coverage and isn't run. The per-mutant
   timeout is --timeout-factor times one untraced run of the suite, at least --min-timeout.
4. Budget: mutants run in an order that interleaves operators and files (round-robin, seeded),
   so any prefix of the run is an even sample; the run keeps going while --minutes remain and
   records the ones it did not reach as not_run ("budget exhausted"). Before the loop it prints
   the plan, estimating each mutant from the share of the suite's tests that cover it (a mutant
   runs only its covering tests, with -x), not from a whole-suite run. --max-mutants caps the
   count (the rest are "sampled out by --max-mutants"). Progress lines are flushed as they go.

Test ids are relative to --repo (each mutant run starts there), whatever rootdir pytest picked
from an ini file in a subfolder; coverage.json records that rootdir, and the engine collects the
covering ids once before the first mutant and stops (exit 2) when they do not resolve.

--runner selftest: the tests are self-test commands (--tests x.py runs `python x.py --self-test`,
--selftest-cmd any command that runs a Python script), one test each. A sitecustomize tracer
records the scoped lines in every Python process a command starts; a failing check or a non-zero
exit kills the mutant.

The `if __name__ == '__main__':` guard is never mutated, and a module-level statement that never
ran is no_coverage.

The working tree is hashed before and after (caches skipped), and again after every mutant: a
mutant whose test run changed a file outside the scoped files stops the run (exit 3; the later
mutants are not_run, builtin.json records `stopped`). Originals are backed up in <dir>/backup
before each write; Ctrl-C, SIGTERM, SIGHUP and SIGBREAK put them back and record the run as
interrupted (exit 130); after a run killed outright, the next run into the same <dir> refuses
until --restore has put them back. Bytecode is redirected to an empty cache so a stale .pyc can
never stand in for a mutant. Output: <dir>/builtin.json.
"""
from __future__ import annotations
import argparse, ast, atexit, copy, hashlib, json, os, random, re, shlex, shutil, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import parse_results  # noqa: E402
import run_tests  # noqa: E402

VERSION = '1.5.0'

PLUGIN = r'''
import json, os, sys, threading
_FILES = {os.path.normcase(os.path.abspath(p)) for p in json.loads(os.environ["MT_FILES"])}
_OUT = os.environ["MT_COV_OUT"]
_REPO = os.environ.get("MT_REPO", os.getcwd())
_hits = {}
_current = ["<import>"]
_seen = {}

def _tracer(frame, event, arg):
    co = frame.f_code.co_filename
    f = _seen.get(co)
    if f is None:
        f = _seen[co] = os.path.normcase(os.path.abspath(co))
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

def _repo_id(item):
    # node ids are relative to pytest's rootdir, which an ini file in a subfolder (benchmarks/pytest.ini) moves away
    # from the repository; mutant runs start in the repository, so record the id relative to it
    nid = item.nodeid
    try:
        rel = os.path.relpath(str(item.path), _REPO).replace(os.sep, "/")
    except (ValueError, TypeError, AttributeError):
        return nid
    if rel == ".." or rel.startswith("../"):
        return nid
    rest = nid.split("::", 1)[1] if "::" in nid else ""
    return rel + ("::" + rest if rest else "")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = _repo_id(item)
    yield
    _current[0] = "<import>"

def _tail(p):
    return p.replace(os.sep, "/").split("/")[-2:]

def pytest_sessionfinish(session, exitstatus):
    sys.settrace(None)
    data = {t: {f: sorted(l) for f, l in files.items()} for t, files in _hits.items()}
    loaded = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f:
            loaded.add(os.path.normcase(os.path.abspath(f)))
    modules = {p: {"loaded": p in loaded,
                   "other_copies": sorted(f for f in loaded if f != p and _tail(f) == _tail(p))} for p in _FILES}
    cfg = session.config
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump({"exitstatus": int(exitstatus), "hits": data, "modules": modules,
                   "rootdir": str(cfg.rootpath), "inifile": str(cfg.inipath) if cfg.inipath else None}, fh)
'''

# The self-test runner's tracer: a sitecustomize module on PYTHONPATH, so every Python process the self-test command
# starts (the script itself, and any script it runs as a child process) records the scoped lines it ran, written per
# process when it exits.
SITE = r'''
import atexit, json, os, sys, threading
if os.environ.get("MT_ST_FILES"):
    _FILES = {os.path.normcase(os.path.abspath(p)) for p in json.loads(os.environ["MT_ST_FILES"])}
    _OUT = os.environ["MT_ST_OUT"]
    _hits = {}
    _seen = {}

    def _tracer(frame, event, arg):
        co = frame.f_code.co_filename
        f = _seen.get(co)
        if f is None:
            f = _seen[co] = os.path.normcase(os.path.abspath(co))
        if f not in _FILES:
            return None
        def local(frame, event, arg):
            if event == "line":
                _hits.setdefault(f, set()).add(frame.f_lineno)
            return local
        if event == "call":
            _hits.setdefault(f, set()).add(frame.f_lineno)
        return local

    def _dump():
        sys.settrace(None)
        loaded = set()
        for mod in list(sys.modules.values()):
            fn = getattr(mod, "__file__", None)
            if fn:
                loaded.add(os.path.normcase(os.path.abspath(fn)))
        os.makedirs(_OUT, exist_ok=True)
        with open(os.path.join(_OUT, "%d.json" % os.getpid()), "w", encoding="utf-8") as fh:
            json.dump({"hits": {f: sorted(l) for f, l in _hits.items()}, "loaded": sorted(loaded)}, fh)

    sys.settrace(_tracer)
    threading.settrace(_tracer)
    atexit.register(_dump)
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
# Methods whose removal keeps the type (x.upper() -> x): a case-insensitive or trimmed comparison
# survives this mutant when no test uses an input whose case or whitespace differs.
STRIP_METHODS = {'upper', 'lower', 'casefold', 'strip', 'lstrip', 'rstrip', 'title', 'capitalize', 'swapcase',
                 'copy', 'removeprefix', 'removesuffix'}
SIMPLE = (ast.Name, ast.Attribute, ast.Subscript, ast.Call, ast.Constant)
NAMED_CONST = re.compile(r'^_?[A-Z][A-Z0-9_]*$')
LITERAL_NOTE = ('inside a typing Literal[...]: it changes behaviour only where the type is checked at run time '
                '(pydantic, typeguard, a schema built from the type)')


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


KEY_METHODS = {'get', 'pop', 'setdefault', 'getenv', 'getlist', 'get_all'}
ATTR_FUNCS = {'getattr', 'hasattr', 'setattr', 'delattr'}


def _strs(node) -> set[int]:
    """Ids of the string constants in a literal collection (or the constant itself)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {id(node)}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {id(e) for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if isinstance(node, ast.Dict):
        return {id(e) for e in [*node.keys, *node.values] if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return set()


def data_strings(tree: ast.AST) -> set[int]:
    """Ids of string constants used as data rather than text: a key (a subscript, the first argument of
    .get/.pop/.setdefault, a name given to getattr/hasattr, a dict literal's key), a default (the default of
    .get, a parameter default, including the elements of a default tuple, list or set)."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript):
            out |= _strs(n.slice) if isinstance(n.slice, ast.Constant) else set()
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in KEY_METHODS and n.args:
                out |= _strs(n.args[0]) if isinstance(n.args[0], ast.Constant) else set()
                if len(n.args) > 1:
                    out |= _strs(n.args[1])
            elif isinstance(f, ast.Name) and f.id in ATTR_FUNCS and len(n.args) > 1:
                out |= _strs(n.args[1]) if isinstance(n.args[1], ast.Constant) else set()
        elif isinstance(n, ast.Dict):
            out |= {id(k) for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for dflt in [*n.args.defaults, *[d for d in n.args.kw_defaults if d is not None]]:
                out |= _strs(dflt)
    return out


def string_contexts(tree: ast.AST) -> tuple[set[int], set[int], set[int]]:
    """Ids of string constants that are compared (an operand of ==, !=, in, ..., or an element of a
    literal tuple, list or set compared with in), assigned to an UPPER_CASE name at module or class
    level, or written inside a typing Literal[...]."""
    compared, named, literal = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare):
            for side in [n.left, *n.comparators]:
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    compared.add(id(side))
                elif isinstance(side, (ast.Tuple, ast.List, ast.Set)):
                    compared.update(id(e) for e in side.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
        elif isinstance(n, (ast.Module, ast.ClassDef)):
            for st in n.body:
                targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                if (targets and all(isinstance(t, ast.Name) and NAMED_CONST.match(t.id) for t in targets)
                        and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)):
                    named.add(id(st.value))
        elif isinstance(n, ast.Subscript):
            v = n.value
            if (isinstance(v, ast.Name) and v.id == 'Literal') or (isinstance(v, ast.Attribute) and v.attr == 'Literal'):
                literal.update(id(x) for x in ast.walk(n.slice))
    return compared, named, literal


def key_reads(tree: ast.AST) -> set[int]:
    """Ids of string constants that are the key of a subscript read (`row['event']`, not `out['rank'] = ...`).
    Emptying one raises the same KeyError at every read of that key, so repeated reads in a function are grouped."""
    return {id(n.slice) for n in ast.walk(tree)
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)
            and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}


RE_FUNCS = {'compile', 'match', 'fullmatch', 'search', 'sub', 'subn', 'split', 'findall', 'finditer'}
REGEX_NOTE = ('a regular expression: the engine only replaces the whole pattern; its alternatives, character classes, '
              'quantifiers and flags are not mutated, so a kill here says the pattern is used, not that it is tested')


def regex_strings(tree: ast.AST) -> set[int]:
    """Ids of string constants given as the pattern to re.compile/search/sub/... (or the `regex` module)."""
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in RE_FUNCS
                and isinstance(n.func.value, ast.Name) and n.func.value.id in ('re', 'regex') and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            out.add(id(n.args[0]))
    return out


def function_spans(tree: ast.AST) -> list[tuple[int, int, str]]:
    """(first line, last line, qualified name) of every function: `outer.<locals>.inner`, `Class.method`."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((child.lineno, child.end_lineno, prefix + child.name))
                walk(child, prefix + child.name + '.<locals>.')
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + '.')
            else:
                walk(child, prefix)
    walk(tree, '')
    return out


def innermost(spans: list[tuple[int, int, str]], line: int):
    hold = [s for s in spans if s[0] <= line <= s[1]]
    return min(hold, key=lambda s: s[1] - s[0]) if hold else None


def unmutated_changes(repo: Path, scope: dict, mutants: list[dict]) -> list[dict]:
    """What the change touched that no mutant stands for: a function that lost lines (the diff's deletions) with no
    mutant on its changed lines, and a file whose changed lines hold nothing to mutate (comments, blank lines,
    docstrings, imports). Removed code cannot be mutated; the function it was removed from can, with a path scope.
    [{file, function, lines: [first, last] of the outermost enclosing function (or null), deleted_at, reason}]."""
    out = []
    by_file: dict[str, list[dict]] = {}
    for m in mutants:
        by_file.setdefault(m['file'], []).append(m)
    for f in scope['files']:
        if f.get('kind') != 'source' or not f['path'].endswith('.py') or f.get('status') == 'deleted':
            continue
        if not (f.get('deleted_at') or f.get('ranges')):
            continue
        try:
            tree = ast.parse((repo / f['path']).read_text(encoding='utf-8'))
        except (OSError, SyntaxError, ValueError):
            continue
        spans = function_spans(tree)
        mine = by_file.get(f['path'], [])
        seen: dict[str, dict] = {}
        for d in f.get('deleted_at') or []:
            # a deletion is recorded at the line that follows it now; the line before belongs to the same block
            fn = innermost(spans, d) or innermost(spans, d - 1)
            if fn is None or any(fn[0] <= m['line'] <= fn[1] for m in mine):
                continue
            outer = max((x for x in spans if x[0] <= fn[0] and fn[1] <= x[1]), key=lambda x: x[1] - x[0])
            e = seen.setdefault(fn[2], {'file': f['path'], 'function': fn[2], 'lines': [outer[0], outer[1]],
                                        'deleted_at': [], 'reason': 'lines were removed from it and none of its '
                                        'changed lines can be mutated (removed code cannot be mutated)'})
            e['deleted_at'].append(d)
        out += list(seen.values())
        changed = sorted({n for a, b in f.get('ranges') or [] for n in range(a, b + 1)})
        if changed and not mine and not seen:
            out.append({'file': f['path'], 'function': None, 'lines': None, 'deleted_at': list(f.get('deleted_at') or []),
                        'reason': f'its {len(changed)} changed line(s) hold nothing the engine mutates (comments, blank '
                                  'lines, docstrings, imports or annotations)'})
    return out


def unmutated_warnings(items: list[dict]) -> list[str]:
    out = []
    for u in items:
        if u['function']:
            a, b = u['lines']
            out.append(f'{u["file"]}: {u["function"]}() lost lines (at {", ".join(map(str, u["deleted_at"]))}) and no '
                       'mutant covers the change: removed code cannot be mutated. To test what is left of it, mutate '
                       f'the function with a path scope: python scripts/path_scope.py --repo <repo> --path '
                       f'{u["file"]}:{a}-{b} --out scope.json. Until then say in the limitations that nothing here '
                       'speaks for this change.')
        else:
            out.append(f'{u["file"]}: {u["reason"]}; no mutant was made from this file. Say so in the limitations, or '
                       f'mutate the functions around the change with path_scope.py --path {u["file"]}:<first>-<last>.')
    return out


def is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == '__main__':` (either operand order)."""
    if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1):
        return False
    t = node.test
    sides = [t.left, t.comparators[0]]
    return (isinstance(t.ops[0], ast.Eq) and any(isinstance(s, ast.Name) and s.id == '__name__' for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == '__main__' for s in sides))


def main_guard_tests(tree: ast.AST) -> set[int]:
    """Ids of the nodes of every module-level `if __name__ == '__main__':` test. Mutating the guard runs the
    script's main() with the test runner's argv whenever the module is imported: noise, never a finding."""
    out: set[int] = set()
    for node in getattr(tree, 'body', []):
        if is_main_guard(node):
            out.update(id(n) for n in ast.walk(node.test))
    return out


def candidates(tree: ast.AST):
    """Yield (node, replacement source text, operator) for every mutation the engine knows."""
    skip = docstring_nodes(tree) | annotation_nodes(tree) | fstring_parts(tree) | main_guard_tests(tree)
    compared, named, _ = string_contexts(tree)
    data = data_strings(tree)
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
        elif isinstance(node, (ast.If, ast.While)) and not isinstance(node.test, ast.UnaryOp) and id(node.test) not in skip:
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
                op = ('constant_string_compared' if id(node) in compared else
                      'constant_string_named' if id(node) in named else
                      'constant_string_data' if id(node) in data else 'constant_string')
                yield node, repr('X' if v == '' else ''), op
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            yield node, 'return None', 'return_value'
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in STRIP_METHODS
              and not node.keywords and (not node.args or node.func.attr in ('strip', 'lstrip', 'rstrip'))):
            inner = ast.unparse(node.func.value)
            yield node, inner if isinstance(node.func.value, SIMPLE) else '(' + inner + ')', 'remove_method_call'
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


def make_mutants(repo: Path, scope: dict, group_key_reads: bool = True) -> tuple[list[dict], list[dict]]:
    """(mutants, files). With group_key_reads, the reads of one dict key in one function (`row['event']` three
    times) are one mutant, on the first read: each would die on the same KeyError. The kept mutant lists the other
    lines in `also_at`; writes (`out['rank'] = ...`), .get() reads and dict literal keys always keep their own."""
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
        literal = string_contexts(tree)[2]
        reads = key_reads(tree) if group_key_reads else set()
        regexes = regex_strings(tree)
        spans = function_spans(tree) if reads else []
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
            m = {'file': f['path'], 'line': first, 'end_line': last, 'col': node.col_offset,
                 'operator': op, 'original': original, 'mutated': mutated, '_text': new_text,
                 '_pos': (len(files), node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, len(mutants))}
            if id(node) in literal:
                m['note'] = LITERAL_NOTE
            if id(node) in regexes:
                m['note'] = REGEX_NOTE
            if id(node) in reads and op == 'constant_string_data':
                fn = innermost(spans, node.lineno)
                if fn is not None:
                    m['_read'] = (f['path'], fn[0], fn[1], node.value)
            mutants.append(m)
    # ids by position (scope file order, line, column, extent), so the console, builtin.json and the
    # normalised report show the same id for a mutant
    mutants.sort(key=lambda m: m.pop('_pos'))
    first: dict[tuple, dict] = {}
    kept = []
    for m in mutants:
        key = m.pop('_read', None)
        if key is None or key not in first:
            if key is not None:
                first[key] = m
            kept.append(m)
            continue
        first[key].setdefault('also_at', []).append(m['line'])
    for key, m in first.items():
        if m.get('also_at'):
            m['also_at'] = sorted(set(m['also_at']))
            note = (f'the key {key[3]!r} is also read at line{"s" if len(m["also_at"]) > 1 else ""} '
                    f'{", ".join(map(str, m["also_at"]))} of this function; this one mutant stands for those reads '
                    '(--all-key-reads mutates each)')
            m['note'] = m['note'] + '; ' + note if m.get('note') else note
    mutants = kept
    for i, m in enumerate(mutants, 1):
        m['id'] = f'B{i}'
    return mutants, files


class Runner:
    """How the Python tests are run: pytest (the default) or unittest (`python -m unittest discover`, for a
    suite CI runs with unittest; pytest would also collect plain functions named test_*, such as helpers)."""

    def __init__(self, kind: str, repo: Path, run_dir: Path, tests: list[str], pytest_args: list[str], ut: dict,
                 selftest_cmds: list[str] | None = None):
        self.kind, self.repo, self.run_dir, self.tests, self.extra, self.ut = kind, repo, run_dir, tests, pytest_args, ut
        self.n = 0
        self.jobs: list[tuple[str, list[str]]] = []
        if kind == 'selftest':
            # each self-test command is one "test": python <script> --self-test (or a self_test.py), or --selftest-cmd
            self.jobs = run_tests.selftest_jobs(argparse.Namespace(selftest_cmd=selftest_cmds or []), tests)
            seen: dict[str, int] = {}
            for i, (sid, argv) in enumerate(self.jobs):
                seen[sid] = seen.get(sid, 0) + 1
                if seen[sid] > 1:
                    self.jobs[i] = (f'{sid}#{seen[sid]}', argv)
        if kind == 'unittest':
            self.ut_dir = run_dir / '_ut'
            self.ut_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / 'unittest_runner.py', self.ut_dir / 'unittest_runner.py')

    def ut_cmd(self, **cfg) -> list[str]:
        files = [t for t in self.tests if '::' not in t]
        ids = [t for t in self.tests if '::' in t] or None
        conf = {**self.ut, 'files': files, 'ids': ids, **cfg}
        self.n += 1
        path = self.ut_dir / f'cfg-{self.n}.json'
        path.write_text(json.dumps(conf), encoding='utf-8')
        return [sys.executable, str(self.ut_dir / 'unittest_runner.py'), str(path)]

    def shown(self) -> str:
        if self.kind == 'selftest':
            return '; '.join(shown_argv(a) for _, a in self.jobs)
        if self.kind == 'unittest':
            u = self.ut
            return ('python -m unittest discover' + (f' -s {u["start"]}' if u.get('start') not in (None, '.') else '') +
                    (f' -p {shlex.quote(u["pattern"])}' if u.get('pattern') not in (None, 'test*.py') else '') +
                    (f' -t {u["top"]}' if u.get('top') else '') + ''.join(f' {shlex.quote(t)}' for t in self.tests))
        return ' '.join(['python -m pytest -q -p no:cacheprovider', *map(shlex.quote, [*self.extra, *self.tests])])

    def coverage_cmd(self, scoped: list[str], out: Path, env: dict) -> tuple[list[str], dict, str]:
        if self.kind == 'unittest':
            return (self.ut_cmd(trace=[str(self.repo / p) for p in scoped], cov_out=str(out)), env,
                    self.shown() + ' (traced per test by unittest_runner.py)')
        plug_dir = self.run_dir / '_plugin'
        plug_dir.mkdir(parents=True, exist_ok=True)
        (plug_dir / 'mt_cov_plugin.py').write_text(PLUGIN, encoding='utf-8')
        cenv = {**env, 'MT_FILES': json.dumps([str(self.repo / p) for p in scoped]), 'MT_COV_OUT': str(out),
                'MT_REPO': str(self.repo),
                'PYTHONPATH': os.pathsep.join([str(plug_dir), env.get('PYTHONPATH', '')]).rstrip(os.pathsep)}
        sel = [*self.extra, *self.tests]
        return ([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'mt_cov_plugin', *sel], cenv,
                ' '.join(['python -m pytest -q -p no:cacheprovider -p mt_cov_plugin', *map(shlex.quote, sel)]))

    def plain_cmd(self) -> list[str]:
        if self.kind == 'unittest':
            return self.ut_cmd()
        return [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *self.extra, *self.tests]

    def startup_cmd(self, ids: list[str] | None = None) -> list[str]:
        """A run that starts the runner and loads the tests but runs none: the fixed cost of every mutant run. Given
        the covering test ids, pytest collects exactly those from --repo, which also proves they resolve there."""
        if self.kind == 'unittest':
            return self.ut_cmd(ids=[])
        return [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', *self.extra,
                *(ids if ids else self.tests)]

    def mutant_cmd(self, m: dict, out: Path) -> list[str]:
        if self.kind == 'unittest':
            return self.ut_cmd(ids=m['covering_tests'], files=[], failfast=True, result_out=str(out))
        return [sys.executable, '-m', 'pytest', '-x', '-q', '-p', 'no:cacheprovider', f'--junitxml={out}', *self.extra,
                *m['covering_tests']]

    def results(self, out: Path, mid: str) -> tuple[list[dict], bool]:
        """(cases [{id, status}], whether a test module failed to import) from a mutant run's output file."""
        if not out.exists():
            return [], False
        if self.kind == 'unittest':
            cases = json.loads(out.read_text(encoding='utf-8')).get('tests', [])
            imp = [c for c in cases if c['id'].endswith('::(import)') and c['status'] in ('failed', 'error')]
            return [c for c in cases if not c['id'].endswith('::(import)')], bool(imp)
        return parse_results.parse_junit(out, mid)[0], False


def shown_argv(argv: list[str]) -> str:
    return ' '.join('python' if a == sys.executable else (shlex.quote(a) if re.search(r'\s', a) else a) for a in argv)


POPEN_KW = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
LIVE: dict = {'proc': None}   # the test run in progress, killed when the engine is stopped
NOT_FOUND = re.compile(r'ERROR: (?:file or directory not found|not found):[^\r\n]*')


def stop_live(proc) -> None:
    """Kill a test run that is still going (and its children), and wait for it, so nothing keeps the tree open."""
    if proc is not None and proc.poll() is None:
        run_tests.kill_tree(proc)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def run_selftests(jobs: list[tuple[str, list[str]]], repo: Path, env: dict, timeout: float | None) -> dict:
    """Run self-test commands one after another from --repo. {status: passed|failed|timeout|error, failed_by,
    output, codes}: failed_by is `<script>::<check>` for the first failing check, or `<script>::(exit status)` when the
    command exited non-zero without naming one."""
    t0 = time.monotonic()
    outputs, codes = [], []
    for sid, argv in jobs:
        left = None if timeout is None else timeout - (time.monotonic() - t0)
        if left is not None and left <= 0:
            return {'status': 'timeout', 'failed_by': None, 'output': '\n'.join(outputs), 'codes': codes}
        try:
            proc = subprocess.Popen(argv, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    **POPEN_KW)
        except OSError as exc:
            return {'status': 'error', 'failed_by': None, 'output': f'{shown_argv(argv)}: {exc}', 'codes': codes}
        LIVE['proc'] = proc
        try:
            out, _ = proc.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            run_tests.kill_tree(proc)
            out, _ = proc.communicate()
            return {'status': 'timeout', 'failed_by': None, 'output': out.decode('utf-8', 'replace'), 'codes': codes}
        finally:
            stop_live(proc)                 # interrupted while waiting: never leave the test run behind
            LIVE['proc'] = None
        text = out.decode('utf-8', 'replace')
        outputs.append(text)
        codes.append(proc.returncode)
        bad = [c for c in run_tests.parse_selftest_output(text) if c['status'] in ('failed', 'error')]
        if bad or proc.returncode != 0:
            # the first failing command decides (as pytest -x): the rest are not run
            return {'status': 'failed', 'failed_by': f'{sid}::{bad[0]["name"]}' if bad else f'{sid}::(exit status)',
                    'output': '\n'.join(outputs), 'codes': codes}
    return {'status': 'passed', 'failed_by': None, 'output': '\n'.join(outputs), 'codes': codes}


def _tail(p: str) -> list[str]:
    return p.replace(os.sep, '/').split('/')[-2:]


def run_coverage_selftest(repo: Path, run_dir: Path, scoped: list[str], env: dict, timeout: int,
                          runner: Runner) -> tuple[dict, float]:
    """Each self-test command once, traced by a sitecustomize module in every Python process it starts (children
    included); its lines are covered by that command. A line that ran is covered by the command, at import or not."""
    site_dir = run_dir / '_site'
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / 'sitecustomize.py').write_text(SITE, encoding='utf-8')
    files = [os.path.normcase(str((repo / p).resolve())) for p in scoped]
    by_line: dict[str, dict[str, list[str]]] = {}
    loaded: set[str] = set()
    codes, t0 = {}, time.monotonic()
    for i, (sid, argv) in enumerate(runner.jobs):
        out_dir = run_dir / '_stcov' / str(i)
        shutil.rmtree(out_dir, ignore_errors=True)
        cenv = {**env, 'MT_ST_FILES': json.dumps(files), 'MT_ST_OUT': str(out_dir),
                'PYTHONPATH': os.pathsep.join([str(site_dir), env.get('PYTHONPATH', '')]).rstrip(os.pathsep)}
        res = run_selftests([(sid, argv)], repo, cenv, timeout)
        codes[sid] = res['status'] if res['status'] != 'failed' else f'failed ({res["failed_by"]})'
        for f in sorted(out_dir.glob('*.json')) if out_dir.is_dir() else []:
            data = json.loads(f.read_text(encoding='utf-8'))
            loaded.update(data.get('loaded') or [])
            for path, lines in (data.get('hits') or {}).items():
                r = Path(path).resolve().relative_to(repo).as_posix()
                for n in lines:
                    by_line.setdefault(r, {}).setdefault(str(n), []).append(sid)
    took = time.monotonic() - t0
    for r in by_line.values():
        for n in r:
            r[n] = sorted(set(r[n]))
    imports = {}
    for p, full in zip(scoped, files):
        imports[p] = {'loaded': full in loaded,
                      'other_copies': [run_tests.redact_home(c) for c in sorted(loaded)
                                       if c != full and _tail(c) == _tail(full)]}
    failed = {s: c for s, c in codes.items() if c != 'passed'}
    cov = {'command': runner.shown(), 'runner': 'selftest', 'exit_code': 1 if failed else 0,
           'pytest_exit': None, 'duration_s': round(took, 3), 'files': scoped, 'lines': by_line, 'imports': imports,
           'rootdir': '.', 'commands': {sid: shown_argv(a) for sid, a in runner.jobs}, 'results': codes,
           'method': ('sys.settrace from a sitecustomize module in every Python process each self-test command starts '
                      '(child processes included); a line is covered by the commands that ran it')}
    shutil.rmtree(site_dir, ignore_errors=True)
    shutil.rmtree(run_dir / '_stcov', ignore_errors=True)
    return cov, took


def auto_runner(repo: Path, env: dict) -> str:
    """pytest when it can be imported in the repository, else unittest."""
    try:
        res = subprocess.run([sys.executable, '-c', 'import pytest'], cwd=repo, env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 'unittest'
    return 'pytest' if res.returncode == 0 else 'unittest'


def parse_unittest_args(text: str) -> dict:
    """-s START -p PATTERN -t TOP (as `python -m unittest discover`), with or without the word discover."""
    ap = argparse.ArgumentParser(prog='--unittest-args', add_help=False)
    ap.add_argument('-s', '--start-directory', default='.')
    ap.add_argument('-p', '--pattern', default='test*.py')
    ap.add_argument('-t', '--top-level-directory', default=None)
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('-q', '--quiet', action='store_true')
    a, rest = ap.parse_known_args([w for w in shlex.split(text or '') if w != 'discover'])
    if rest:
        raise SystemExit(f'--unittest-args: not understood: {" ".join(rest)} (give -s, -p and -t as for '
                         'python -m unittest discover; name tests with --tests)')
    return {'start': a.start_directory, 'pattern': a.pattern, 'top': a.top_level_directory}


LAUNCH = re.compile(r'subprocess|Popen|check_output|sys\.executable|os\.system|runpy|run_path')
TEST_PATH = re.compile(r'(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$')
WALK_SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.tox', '.nox', 'site-packages', 'build', 'dist',
             'target', '.mypy_cache', '.pytest_cache'}


def launched_as_child(repo: Path, scoped: list[str], skip: set[Path]) -> dict[str, list[str]]:
    """{scoped file: ['tests/test_x.py:12', ...]}: lines of test files that start child processes and name the
    scoped file (its file name in a string or path, or `-m <module>`). Lines run only in such a child are never
    traced in-process, so their mutants are no_coverage although a test may exercise them."""
    names = {f: (Path(f).name, Path(f).with_suffix('').as_posix().replace('/', '.')) for f in scoped}
    out: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in WALK_SKIP and Path(dirpath, d).resolve() not in skip)
        for fn in sorted(filenames):
            rel_p = Path(dirpath, fn).relative_to(repo).as_posix()
            if not fn.endswith('.py') or not TEST_PATH.search(rel_p):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if not LAUNCH.search(text):
                continue
            for f, (base, dotted) in names.items():
                file_rx = re.compile(r'''["'/\\]''' + re.escape(base) + r'''["']''')
                mod_rx = re.compile(r'''["']-m["']\s*,\s*["'](?:[\w.]*\.)?''' + re.escape(dotted.rsplit('.', 1)[-1]) + r'''["']''')
                for i, line in enumerate(text.splitlines(), 1):
                    if file_rx.search(line) or mod_rx.search(line):
                        out.setdefault(f, []).append(f'{rel_p}:{i}')
    return out


def run_coverage(repo: Path, run_dir: Path, scoped: list[str], env: dict, timeout: int,
                 runner: Runner) -> tuple[dict, float]:
    out = run_dir / 'coverage.raw.json'
    cmd, cenv, shown = runner.coverage_cmd(scoped, out, env)
    t0 = time.monotonic()
    res = subprocess.run(cmd, cwd=repo, env=cenv, capture_output=True, timeout=timeout)
    took = time.monotonic() - t0
    if not out.exists():
        raise SystemExit('coverage run wrote no data:\n' + res.stdout.decode('utf-8', 'replace')[-1500:] +
                         res.stderr.decode('utf-8', 'replace')[-800:])
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
    imports = {}
    for f, info in (raw.get('modules') or {}).items():
        r = Path(f).resolve().relative_to(repo).as_posix()
        imports[r] = {'loaded': info['loaded'],
                      'other_copies': [run_tests.redact_home(c) for c in info['other_copies']]}
    def repo_rel(p):
        if not p:
            return None
        try:
            r = Path(p).resolve().relative_to(repo).as_posix()
            return r or '.'
        except ValueError:
            return run_tests.redact_home(str(p))
    cov = {'command': shown, 'runner': runner.kind, 'exit_code': res.returncode,
           'pytest_exit': raw['exitstatus'], 'duration_s': round(took, 3), 'files': scoped, 'lines': by_line,
           'imports': imports,
           # pytest's rootdir (where its node ids start) and ini file; test ids here are relative to --repo instead
           **({'rootdir': repo_rel(raw.get('rootdir')), 'inifile': repo_rel(raw.get('inifile')),
               'test_ids': 'relative to --repo, where every mutant run starts'} if runner.kind == 'pytest' else {}),
           'method': ('sys.settrace in a pytest plugin, per test (lines run at import are under "<import>")'
                      if runner.kind == 'pytest' else
                      'sys.settrace in a unittest runner, per TestCase method (lines run at import are under "<import>")')}
    out.unlink()
    shutil.rmtree(run_dir / '_plugin', ignore_errors=True)
    return cov, took


IMPORT_NOTE = ('line ran only while modules were imported (test collection), never inside a test; covered by '
               'every test that touched the file, since any of them may depend on what it built')


def covering(cov: dict, m: dict, module_level: bool, stmt: tuple[int, int] | None = None) -> tuple[list[str], bool]:
    """(covering tests, ran only at import). A line that ran only under "<import>" is covered by every test
    that touched its file (every test the coverage run saw, when none did): a module-level statement, or a
    function called while conftest or a module was imported (cached loaders, data built at collection)."""
    lines = cov['lines'].get(m['file'], {})
    tests = set()
    for n in range(m['line'], m['end_line'] + 1):
        tests.update(lines.get(str(n), []))
    if module_level and not tests and stmt is not None:
        # a module-level statement that never ran at all (the body of `if __name__ == '__main__':` under a test
        # runner, an else branch of a module-level if) is not covered; a multi-line statement counts as run when
        # any of its lines ran
        if not any(lines.get(str(n)) for n in range(stmt[0], stmt[1] + 1)):
            return [], False
    at_import = tests == {'<import>'}
    if tests <= {'<import>'} and (module_level or at_import):
        tests = {t for ts in lines.values() for t in ts}
        tests.discard('<import>')
        if not tests and at_import:
            tests = {t for f in cov['lines'].values() for ts in f.values() for t in ts}
    tests.discard('<import>')
    return sorted(tests), at_import and not module_level


def function_lines(text: str) -> list[tuple[int, int]]:
    tree = ast.parse(text)
    return [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def statement_lines(text: str) -> list[tuple[int, int]]:
    """(first, last) line of every statement, for finding the smallest statement around a mutant."""
    return [(n.lineno, n.end_lineno) for n in ast.walk(ast.parse(text)) if isinstance(n, ast.stmt)]


def enclosing_stmt(stmts: list[tuple[int, int]], a: int, b: int) -> tuple[int, int] | None:
    inside = [s for s in stmts if s[0] <= a and b <= s[1]]
    return min(inside, key=lambda s: s[1] - s[0]) if inside else None


def sample(mutants: list[dict], keep: int, seed: int) -> list[dict]:
    """Round-robin across (operator, file) groups so every operator and file stays represented. The list is
    in run order: any prefix of it is itself an even sample."""
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
    return out


def restore(run_dir: Path, repo: Path) -> int:
    n = 0
    for b in (run_dir / 'backup').rglob('*'):
        if b.is_file():
            target = repo / b.relative_to(run_dir / 'backup')
            target.write_bytes(b.read_bytes())
            n += 1
    return n


LANG_OF = {'.py': 'python', '.go': 'go', '.rs': 'rust', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
           '.cjs': 'javascript', '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
           '.java': 'java', '.kt': 'kotlin', '.cs': 'csharp', '.swift': 'swift', '.dart': 'dart', '.rb': 'ruby',
           '.php': 'php', '.scala': 'scala', '.m': 'objective-c', '.c': 'c', '.cpp': 'c++', '.h': 'c'}
INSTEAD = {'javascript': 'run_engine.py stryker (StrykerJS)', 'typescript': 'run_engine.py stryker (StrykerJS)',
           'rust': 'run_engine.py cargo-mutants (cargo-mutants)', 'java': 'PIT', 'kotlin': 'PIT', 'csharp': 'Stryker.NET',
           'swift': 'Muter', 'dart': 'mutation_test'}


def scope_languages(scope: dict) -> dict[str, list[str]]:
    """{language: [files]} for the scope's source files that have changed lines (Go test files left out)."""
    out: dict[str, list[str]] = {}
    for f in scope['files']:
        if f['kind'] != 'source' or f['status'] == 'deleted' or not f['ranges'] or f['path'].endswith('_test.go'):
            continue
        ext = Path(f['path']).suffix.lower()
        out.setdefault(LANG_OF.get(ext, ext.lstrip('.') or 'unknown'), []).append(f['path'])
    return out


def pick_language(scope: dict, wanted: str, repo: Path | None = None) -> str:
    """The language to mutate. A scope with nothing this engine can mutate stops loudly (exit 2), naming the
    files it leaves out and the tool for their language."""
    langs = scope_languages(scope)
    supported = [l for l in ('python', 'go') if l in langs]
    others = {l: fs for l, fs in langs.items() if l not in ('python', 'go')}
    if others:
        print('WARNING: the builtin engine mutates only Python and Go; not mutated: ' +
              '; '.join(f'{l}: {", ".join(fs[:5])}' + (f' (+{len(fs) - 5} more)' if len(fs) > 5 else '') +
                        (f' (use {INSTEAD[l]})' if l in INSTEAD else '') for l, fs in sorted(others.items())),
              file=sys.stderr)
    if wanted != 'auto':
        if wanted not in langs:
            print(f'ERROR: --language {wanted}: the scope has no changed {wanted} source lines', file=sys.stderr)
            raise SystemExit(2)
        return wanted
    if not supported:
        if repo is not None:
            for w in unmutated_warnings(unmutated_changes(repo, scope, [])):
                if '() lost lines' in w:
                    print(f'WARNING: {w}', file=sys.stderr)
        print('ERROR: no changed Python or Go source lines in scope, so the builtin engine has nothing to mutate'
              + ('' if others else ' (the scope has no changed source at all)')
              + '. Record the engine as not_run with this reason, or use the tool for the language (SKILL.md, Step 1).',
              file=sys.stderr)
        raise SystemExit(2)
    if len(supported) > 1:
        print('ERROR: the scope has changed Python and Go source; run the engine once per language with '
              '--language python and --language go, into separate run directories', file=sys.stderr)
        raise SystemExit(2)
    return supported[0]


def stale_backup(run_dir: Path, repo: Path) -> list[str]:
    """Files whose original, backed up by an earlier run into <run-dir>/backup, differs from the source now: that run
    was killed outright (no handler can run) with a mutant in place."""
    b = run_dir / 'backup'
    out = []
    if b.is_dir():
        for f in b.rglob('*'):
            if f.is_file():
                rel_p = f.relative_to(b).as_posix()
                t = repo / rel_p
                if not t.is_file() or t.read_bytes() != f.read_bytes():
                    out.append(rel_p)
    return sorted(out)


class Stopped(KeyboardInterrupt):
    """Raised by the SIGTERM, SIGHUP and SIGBREAK handlers, so a stopped engine restores the source as for Ctrl-C."""


def on_signal(signum, frame):
    raise Stopped(f'signal {signum}')


def install_signal_handlers() -> dict:
    old = {}
    for name in ('SIGTERM', 'SIGHUP', 'SIGBREAK'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, on_signal)
        except (ValueError, OSError):       # not the main thread: Ctrl-C and atexit still restore
            pass
    return old


def unreached_note(stop: dict | None, interrupted: bool) -> str:
    if interrupted:
        return 'run interrupted (Ctrl-C or a signal) before this mutant finished'
    if stop and stop['kind'] == 'tree_changed':
        return (f'run stopped: mutant {stop["mutant"]}\'s test run changed {", ".join(stop["files"][:3])} outside the '
                'scoped files, so later runs would read a changed tree')
    if stop:
        return f'run stopped: mutant {stop["mutant"]}: {stop["detail"]}'
    return 'budget exhausted'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--scope', type=Path, required=True, help='diff_scope.py output')
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--minutes', type=float, default=30.0)
    ap.add_argument('--timeout-factor', type=float, default=3.0)
    ap.add_argument('--min-timeout', type=float, default=10.0)
    ap.add_argument('--max-mutants', type=int)
    ap.add_argument('--tests', action='append', default=[],
                    help='test file, directory or node id (relative to --repo) every run is limited to (repeatable); '
                         'with --runner selftest, a script whose --self-test is a test (a self_test.py runs as is)')
    ap.add_argument('--pytest-args', default='', help='extra pytest arguments for every run, as one quoted string')
    ap.add_argument('--runner', choices=['auto', 'pytest', 'unittest', 'selftest'], default='auto',
                    help='Python test runner (default: pytest when it imports in the repo, else unittest); use unittest '
                         'when CI runs python -m unittest, selftest when the tests are scripts\' own --self-test')
    ap.add_argument('--selftest-cmd', action='append', default=[],
                    help='with --runner selftest: a self-test command run from --repo, "python tools/x.py --self-test" '
                         '(repeatable); each command is one test')
    ap.add_argument('--unittest-args', default='',
                    help='unittest discovery as for python -m unittest discover: "-s tests -p \'test*.py\' -t ."')
    ap.add_argument('--seed', type=int, default=1, help='sampling seed')
    ap.add_argument('--all-key-reads', action='store_true',
                    help="mutate every read of a dict key (row['k']); by default the reads of one key in one function "
                         'are one mutant, since each dies on the same KeyError')
    ap.add_argument('--coverage-only', action='store_true')
    ap.add_argument('--restore', action='store_true', help='put backed-up originals back and exit')
    ap.add_argument('--language', choices=['auto', 'python', 'go'], default='auto',
                    help='which changed source to mutate (default: the one language the scope has)')
    ap.add_argument('--go-run', default='', help='Go: only tests whose names match this regex (go test -run)')
    ap.add_argument('--go-skip', default='', help='Go: leave out tests whose names match this regex (go test -skip)')
    ap.add_argument('--go-flags', default='', help='Go: extra flags for every go command, as one quoted string')
    args = ap.parse_args(argv)
    repo, run_dir = args.repo.resolve(), args.run_dir.resolve()
    if args.restore:
        print(f'restored {restore(run_dir, repo)} file(s)')
        return 0
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    # every file the mutator may write must be inside --repo: a scope naming ../x.py or an absolute path is refused
    # before anything runs (mutants are written into the scoped files, then restored)
    outside = [f['path'] for f in scope.get('files', []) if f.get('status') != 'deleted'
               and (Path(f['path']).is_absolute() or not (repo / f['path']).resolve().is_relative_to(repo))]
    if outside:
        raise SystemExit(f'scope names file(s) outside --repo {repo}: {", ".join(outside[:5])}; refusing to mutate them')
    language = pick_language(scope, args.language, repo)
    if language == 'go':
        import go_mutator
        return go_mutator.main(args, scope)
    if args.selftest_cmd and args.runner != 'selftest':
        raise SystemExit('--selftest-cmd needs --runner selftest')
    # an earlier run killed outright (no handler ran) left a mutant in a source file: never mutate a mutant
    stale = stale_backup(run_dir, repo)
    if stale:
        print(f'ERROR: {run_dir / "backup"} holds originals that differ from the source now ({", ".join(stale[:5])}): '
              'an earlier run was stopped with a mutant in place. Put them back with builtin_mutator.py --restore '
              '--repo <repo> --scope <scope> --run-dir <this run dir> (or check those files by hand), then run again.',
              file=sys.stderr)
        return 2
    shutil.rmtree(run_dir / 'backup', ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    skip = {run_dir}
    before = run_tests.tree_snapshot(repo, skip)
    pycache = run_dir / '_pycache'
    shutil.rmtree(pycache, ignore_errors=True)
    pycache.mkdir()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str(pycache)}
    mutants, files = make_mutants(repo, scope, group_key_reads=not args.all_key_reads)
    grouped_reads = sum(len(m.get('also_at') or []) for m in mutants)
    scoped = [f['path'] for f in files]
    unmutated = unmutated_changes(repo, scope, mutants)
    for w in unmutated_warnings(unmutated):
        print(f'WARNING: {w}', file=sys.stderr, flush=True)
    if not scoped:
        raise SystemExit('no changed Python source lines in scope')
    for t in args.tests:
        if not (repo / t.split('::')[0]).exists():
            raise SystemExit(f'--tests {t}: not found under {repo}')
    extra = shlex.split(args.pytest_args) if args.pytest_args else []
    kind = args.runner if args.runner != 'auto' else auto_runner(repo, env)
    if kind in ('unittest', 'selftest') and extra:
        raise SystemExit(f'--pytest-args with --runner {kind}: ' + ('give discovery options with --unittest-args'
                                                                    if kind == 'unittest' else 'name the commands with '
                                                                    '--selftest-cmd'))
    if kind == 'selftest' and not (args.tests or args.selftest_cmd):
        raise SystemExit('--runner selftest needs --tests <script.py> or --selftest-cmd "python <script.py> --self-test"')
    if args.runner == 'auto':
        print(f'runner: {kind} (' + ('pytest imports here' if kind == 'pytest' else 'pytest cannot be imported here') +
              '; pass --runner unittest when CI runs python -m unittest)', file=sys.stderr)
    runner = Runner(kind, repo, run_dir, args.tests, extra, parse_unittest_args(args.unittest_args), args.selftest_cmd)
    for sid, argv_ in runner.jobs:
        script = next((a for a in argv_[1:] if a.endswith('.py')), None)
        if argv_[0] != sys.executable or not script:
            raise SystemExit(f'--selftest-cmd {shown_argv(argv_)}: give a command that runs a Python script '
                             '("python tools/x.py --self-test"), so its lines can be traced')
        if not (repo / script).is_file():
            raise SystemExit(f'--selftest-cmd {shown_argv(argv_)}: {script} not found under {repo}')
    cov_timeout = max(600, int(args.minutes * 60))
    if kind == 'selftest':
        cov, cov_s = run_coverage_selftest(repo, run_dir, scoped, env, timeout=cov_timeout, runner=runner)
        child: dict[str, list[str]] = {}
    else:
        cov, cov_s = run_coverage(repo, run_dir, scoped, env, timeout=cov_timeout, runner=runner)
        child = launched_as_child(repo, scoped, {run_dir})
    if child:
        cov['launched_as_child'] = child
    (run_dir / 'coverage.json').write_text(json.dumps(cov, indent=1) + '\n', encoding='utf-8')
    print(f'coverage: {len(scoped)} file(s), {cov_s:.1f}s, {kind} exit {cov["exit_code"]}'
          + (f', pytest rootdir {cov["rootdir"]}' if cov.get('rootdir') not in (None, '.') else ''), flush=True)
    shadowed = {f: i['other_copies'] for f, i in cov['imports'].items() if not i['loaded'] and i['other_copies']}
    if shadowed:
        raise SystemExit('the tests imported another copy of the scoped code, so no mutant here could be seen: ' +
                         '; '.join(f'{f} -> {", ".join(c)}' for f, c in shadowed.items()) +
                         ' (an editable install or installed package pointing elsewhere; install this checkout '
                         'into the environment, or put it first on PYTHONPATH)')
    for f, i in cov['imports'].items():
        if not i['loaded']:
            print(f'WARNING: no test imported {f}; its mutants will all be no_coverage', file=sys.stderr)
    if args.coverage_only:
        return 0
    if cov['exit_code'] != 0:
        raise SystemExit('the suite does not pass unmutated; fix or exclude failing tests first (no mutants run)'
                         + (f': {cov["results"]}' if kind == 'selftest' else '')
                         + ('' if args.tests else '; --tests limits the runs to the tests that exercise the scope'))
    t0 = time.monotonic()
    if kind == 'selftest':
        plain_ok = run_selftests(runner.jobs, repo, env, None)['status'] == 'passed'
    else:
        plain_ok = subprocess.run(runner.plain_cmd(), cwd=repo, env=env, capture_output=True).returncode == 0
    plain_s = time.monotonic() - t0
    if not plain_ok:
        raise SystemExit('the suite does not pass unmutated without tracing; no mutants run')
    funcs = {f: function_lines((repo / f).read_text(encoding='utf-8')) for f in scoped}
    stmts = {f: statement_lines((repo / f).read_text(encoding='utf-8')) for f in scoped}
    for m in mutants:
        module_level = not any(a <= m['line'] <= b for a, b in funcs[m['file']])
        m['covering_tests'], at_import = covering(cov, m, module_level,
                                                  enclosing_stmt(stmts[m['file']], m['line'], m['end_line']))
        m['module_level'] = module_level
        if at_import and m['covering_tests']:
            m['note'] = IMPORT_NOTE
        if not m['covering_tests'] and child.get(m['file']):
            m['note'] = (f'no test ran this line in-process; {", ".join(child[m["file"]][:3])} runs this file as a child '
                         'process, which the tracer cannot see: read that test before calling this a coverage gap')
    # the fixed cost of every mutant run: start the runner, import conftest and the test modules, run nothing. For
    # pytest it collects the covering ids from --repo, as each mutant run will: an id that does not resolve there
    # stops the engine now, instead of every mutant becoming a runtime_error
    all_ids = sorted({t for m in mutants for t in m['covering_tests']})
    t0 = time.monotonic()
    if kind == 'pytest' and all_ids:
        ids = all_ids
        if sum(len(i) + 1 for i in ids) > 20000:     # command-line limits: one id per test file is enough to check
            ids = sorted({i.split('::')[0]: i for i in reversed(all_ids)}.values())
        st = subprocess.run(runner.startup_cmd(ids), cwd=repo, env=env, capture_output=True)
        if st.returncode != 0:
            print('ERROR: pytest could not collect the covering test ids from --repo '
                  f'(exit {st.returncode}; pytest\'s rootdir was {cov.get("rootdir")}, ini file {cov.get("inifile")}), '
                  'so every mutant run would fail before a test ran. Output:\n' +
                  run_tests.redact_home(st.stdout.decode('utf-8', 'replace')[-1200:]), file=sys.stderr)
            return 2
    elif kind == 'unittest':
        subprocess.run(runner.startup_cmd(), cwd=repo, env=env, capture_output=True)
    else:
        subprocess.run([sys.executable, '-c', 'pass'], cwd=repo, env=env, capture_output=True)
    startup_s = min(plain_s, time.monotonic() - t0)
    timeout = max(args.min_timeout, args.timeout_factor * plain_s)
    runnable = [m for m in mutants if m['covering_tests']]
    # run order: round-robin over operators and files, so stopping at the deadline still leaves an even sample
    order = sample(runnable, len(runnable), args.seed)
    if args.max_mutants is not None:
        order = order[:args.max_mutants]
    order_ids = {m['id'] for m in order}
    # estimate: a mutant runs only its covering tests (with -x): the measured start-up (runner, conftest and test
    # imports) plus the share of the suite's test time its tests take. The budget is spent by the clock, not by
    # this estimate; after the first few mutants the measured mean is printed as a second estimate.
    all_tests = {t for f in cov['lines'].values() for ts in f.values() for t in ts} - {'<import>'}
    body_s = max(0.0, plain_s - startup_s)
    per = {m['id']: startup_s + body_s * len(m['covering_tests']) / max(1, len(all_tests)) for m in order}
    est = sum(per.values())
    print(f'plan: {len(order)} of {len(runnable)} runnable mutants ({len(mutants)} generated'
          + (f'; {grouped_reads} repeated key read(s) grouped' if grouped_reads else '') + '), est. '
          f'{est / 60:.1f} min ({est / max(1, len(order)):.1f}s per mutant) of a {args.minutes:g} min budget; '
          f'start-up {startup_s:.1f}s, whole-suite run {plain_s:.1f}s, timeout {timeout:.0f}s per mutant', flush=True)
    estimate = {'startup_s': round(startup_s, 3), 'planned_s': round(est, 1),
                'per_mutant_planned_s': round(est / max(1, len(order)), 2), 'measured_mean_s': None, 'measured_over': 0}
    check_at = min(10, max(3, len(order) // 10))
    backup = run_dir / 'backup'
    deadline = time.monotonic() + args.minutes * 60
    junit_dir = run_dir / '_junit_builtin'
    junit_dir.mkdir(exist_ok=True)
    originals = {f: (repo / f).read_bytes() for f in scoped}
    for f, data in originals.items():
        (backup / f).parent.mkdir(parents=True, exist_ok=True)
        (backup / f).write_bytes(data)
    texts = {m['id']: m.pop('_text') for m in mutants}
    attempted = 0
    # the tree every mutant run is compared with: after the unmutated runs, so a suite that always writes the same
    # file is not blamed on a mutant (the before/after check at the end still reports it)
    ref = run_tests.tree_snapshot(repo, skip)

    def add_note(m: dict, note: str) -> None:
        m['note'] = m['note'] + '; ' + note if m.get('note') else note

    def put_back() -> None:
        stop_live(LIVE.get('proc'))
        for f, data in originals.items():
            try:
                if (repo / f).read_bytes() != data:
                    (repo / f).write_bytes(data)
            except OSError:
                pass

    for m in mutants:
        if not m['covering_tests']:
            m['status'] = 'no_coverage'
        elif m['id'] not in order_ids:
            m['status'] = 'not_run'
            add_note(m, 'sampled out by --max-mutants')
    stop: dict | None = None
    interrupted = False
    old_handlers = install_signal_handlers()
    atexit.register(put_back)       # an exception or sys.exit anywhere below still puts the originals back
    try:
        for m in order:
            if time.monotonic() > deadline:
                break
            attempted += 1
            m['run_index'] = attempted
            text = texts[m['id']]
            try:
                compile(text, m['file'], 'exec')
            except SyntaxError as exc:
                m['status'] = 'compile_error'
                add_note(m, str(exc))
                continue
            target = repo / m['file']
            junit = junit_dir / (f'{m["id"]}.xml' if kind == 'pytest' else f'{m["id"]}.json')
            t0 = time.monotonic()
            st_res, proc, out, timed_out = None, None, b'', False
            try:
                target.write_bytes(text.encode('utf-8'))
                if kind == 'selftest':
                    cover = set(m['covering_tests'])
                    st_res = run_selftests([j for j in runner.jobs if j[0] in cover], repo, env, timeout)
                else:
                    proc = subprocess.Popen(runner.mutant_cmd(m, junit), cwd=repo, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, **POPEN_KW)
                    LIVE['proc'] = proc
                    try:
                        out, _ = proc.communicate(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        run_tests.kill_tree(proc)
                        out, _ = proc.communicate()
                        timed_out = True
            finally:
                put_back()
                LIVE['proc'] = None
            m['duration_s'] = round(time.monotonic() - t0, 3)
            if st_res is not None:
                m['exit_code'] = st_res['codes'][-1] if st_res['codes'] else None
                if st_res['status'] == 'timeout':
                    m['status'] = 'timeout'
                elif st_res['status'] == 'passed':
                    m['status'] = 'survived'
                elif st_res['status'] == 'failed':
                    m['status'] = 'killed'
                    m['killed_by'] = st_res['failed_by']
                else:
                    m['status'] = 'runtime_error'
                    add_note(m, run_tests.redact_home(st_res['output'][-400:]))
            else:
                m['exit_code'] = proc.returncode
                cases, import_failed = runner.results(junit, m['id'])
                failed = [c['id'] for c in cases if c['status'] in ('failed', 'error')]
                if timed_out:
                    m['status'] = 'timeout'
                elif proc.returncode == 0 and cases:
                    m['status'] = 'survived'
                elif proc.returncode == 1 and failed and not import_failed:
                    m['status'] = 'killed'
                    m['killed_by'] = failed[0]
                else:
                    m['status'] = 'runtime_error'
                    add_note(m, 'a test module failed to import' if import_failed else
                             run_tests.redact_home(out.decode('utf-8', 'replace')[-400:]))
                    missing = NOT_FOUND.search(out.decode('utf-8', 'replace'))
                    if kind == 'pytest' and proc.returncode == 4 and not cases and missing:
                        # a test id that no longer resolves: the engine's command is wrong, not the mutant (a conftest
                        # that fails to import under the mutant is also exit 4, and stays a runtime_error)
                        stop = {'kind': 'engine_error', 'mutant': m['id'],
                                'detail': f'pytest could not find the covering tests (exit 4, {missing.group(0)[:200]})'}
                if junit.exists():
                    junit.unlink()
            print(f'{m["id"]:>5} {m["file"]}:{m["line"]} {m["operator"]:<22} {m["status"]}', flush=True)
            # a test run that wrote outside the scoped files (a dry run turned into a write by the mutant) changes
            # what every later run reads: stop here rather than record contaminated results
            now = run_tests.tree_snapshot(repo, skip)
            wrote = sorted(k for k in set(ref) | set(now) if ref.get(k) != now.get(k))
            if wrote:
                m['wrote_outside_scope'] = wrote
                add_note(m, 'its test run changed files outside the scoped files: ' + ', '.join(wrote[:5]))
                stop = {'kind': 'tree_changed', 'mutant': m['id'], 'files': wrote}
                print(f'ERROR: mutant {m["id"]}\'s test run changed {", ".join(wrote[:5])}; stopping so no later '
                      'result reads the changed tree. Restore those files (git checkout, or a fresh copy) and rerun.',
                      file=sys.stderr, flush=True)
            if stop:
                break
            done = [x['duration_s'] for x in order if 'duration_s' in x]
            if len(done) == check_at:
                mean = sum(done) / len(done)
                left = [x for x in order if 'status' not in x]
                estimate.update(measured_mean_s=round(mean, 2), measured_over=len(done))
                print(f'measured: {mean:.1f}s per mutant over the first {len(done)} (planned '
                      f'{estimate["per_mutant_planned_s"]:.1f}s); the {len(left)} left would take about '
                      f'{mean * len(left) / 60:.1f} min, {max(0.0, deadline - time.monotonic()) / 60:.1f} min of budget '
                      'remain', flush=True)
    except KeyboardInterrupt:
        interrupted = True
        stop = {'kind': 'interrupted', 'mutant': next((m['id'] for m in order if 'run_index' in m and 'status' not in m),
                                                      None), 'detail': 'Ctrl-C or a signal'}
        print('interrupted: the source files are restored; recording what ran', file=sys.stderr, flush=True)
    finally:
        put_back()
        atexit.unregister(put_back)
        for sig, h in old_handlers.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
    for m in order:
        if 'status' not in m:
            m['status'] = 'not_run'
            add_note(m, unreached_note(stop, interrupted))
    after = run_tests.tree_snapshot(repo, skip)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(pycache, ignore_errors=True)
    shutil.rmtree(junit_dir, ignore_errors=True)
    shutil.rmtree(run_dir / '_ut', ignore_errors=True)
    counts: dict[str, int] = {}
    for m in mutants:
        counts[m['status']] = counts.get(m['status'], 0) + 1
    command = 'python scripts/builtin_mutator.py --repo <repo> --scope <scope> --run-dir <dir>'
    command += ''.join(f' --tests {shlex.quote(t)}' for t in args.tests)
    if args.pytest_args:
        command += f' --pytest-args {shlex.quote(args.pytest_args)}'
    if kind == 'unittest':
        command += ' --runner unittest' + (f' --unittest-args {shlex.quote(args.unittest_args)}' if args.unittest_args else '')
    if kind == 'selftest':
        command += ' --runner selftest' + ''.join(f' --selftest-cmd {shlex.quote(c)}' for c in args.selftest_cmd)
    if estimate['measured_mean_s'] is None and any('duration_s' in m for m in order):
        done = [m['duration_s'] for m in order if 'duration_s' in m]
        estimate.update(measured_mean_s=round(sum(done) / len(done), 2), measured_over=len(done))
    out = {'engine': {'name': 'builtin', 'version': VERSION, 'python': sys.version.split()[0], 'command': command},
           'ids': 'position', 'runner': kind, 'tests': args.tests, 'pytest_args': extra,
           **({'selftest_commands': {sid: shown_argv(a) for sid, a in runner.jobs}} if kind == 'selftest' else {}),
           'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'files': files,
           'timeout_s': round(timeout, 1), 'suite_duration_s': round(plain_s, 3), 'coverage_duration_s': round(cov_s, 3),
           'estimate': estimate,
           'sampled': {'generated': len(mutants), 'runnable': len(runnable), 'chosen': attempted,
                       'skipped': len(runnable) - attempted, 'seed': args.seed},
           'tree': {'before': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    'after': hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
                    'changed': changed, 'checked': 'after every mutant',
                    **({'stopped': stop} if stop and stop['kind'] == 'tree_changed' else {})},
           'stopped': stop, 'interrupted': interrupted,
           # what the change touched that no mutant stands for (deletions, comment-only changes): the report says so
           'unmutated_changes': unmutated,
           'grouped': {'key_reads': grouped_reads, 'note': 'reads of one dict key in one function are one mutant '
                                                           '(also_at lists the other lines)'},
           'counts': counts, 'mutants': mutants}
    (run_dir / 'builtin.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(mutants)} mutants: ' + (', '.join(f'{v} {k}' for k, v in sorted(counts.items())) if mutants else
                                          'nothing on the scoped lines can be mutated (see the WARNING lines)') +
          f' -> {run_dir / "builtin.json"}')
    if interrupted:
        return 130
    if changed:
        print('ERROR: the working tree changed during the run: ' + ', '.join(changed), file=sys.stderr)
        return 3
    if stop:
        print(f'ERROR: {unreached_note(stop, False)}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
