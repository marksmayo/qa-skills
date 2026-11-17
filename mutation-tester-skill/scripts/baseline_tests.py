#!/usr/bin/env python3
"""List the Python test files that exercise the scoped modules: the scoped baseline of SKILL.md Step 3
(stdlib only).

    python baseline_tests.py --repo <repo> --scope scope.json [--out baseline_tests.json]
    python baseline_tests.py --repo <repo> --path scripts/levels.py [--path pkg/rules.py]

A test file is listed when it reaches a scoped file by reading the code, never by running it:

    imports <file>            it imports the module (from the repository root, src/, its own folder,
                              a folder it or a conftest.py above it puts on sys.path, or relatively)
    through <file>            a module it imports does (a re-export, a caller); `chain` is the path
    loads <file> by path      importlib's spec_from_file_location, SourceFileLoader or runpy.run_path
                              with a path built from __file__ (py_loads.py)
    runs <file> as a child    it starts the file with subprocess (a line names it); the tracer does
                              not see child processes, so the engine reports those lines no_coverage

Output: {scoped, tests: [{path, via, reaches, chain}], not_reaching: [test files that reach no scoped
file], unresolved: [{path, line, expr}: a load whose path could not be read], tests_args}. The last
is the `--tests` list for run_tests.py, builtin_mutator.py and prove_kill.py. This is a static
reading: a test that reaches the code only through a plugin, an entry point or a name built at run
time is not seen, so check `not_reaching` before leaving a test out, and name in baseline.note the
test files that reach the scope but were not run. Exit 0; 2 when no scoped Python file is given.
"""
from __future__ import annotations
import argparse, ast, json, sys
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_scope  # noqa: E402
import py_loads  # noqa: E402
import repo_layout  # noqa: E402

LIMIT = 4000        # files followed from one test file


def parse(root: Path, rel: str, cache: dict):
    if rel not in cache:
        try:
            cache[rel] = ast.parse((root / rel).read_text(encoding='utf-8', errors='replace'))
        except (OSError, SyntaxError, ValueError):
            cache[rel] = None
    return cache[rel]


def package_inits(found: str, base: str, exists: set) -> list[str]:
    """The __init__.py files of the packages between a base folder and a resolved module: importing a.b.c runs
    a/__init__.py and a/b/__init__.py, where re-exports live."""
    out = []
    d = PurePosixPath(found).parent
    stop = PurePosixPath(base) if base not in ('', '.') else PurePosixPath('.')
    while d != stop and str(d) not in ('.', ''):
        init = (d / '__init__.py').as_posix()
        if init in exists and init != found:
            out.append(init)
        d = d.parent
    return out


def imports_of(root: Path, rel: str, exists: set, cache: dict, extra_dirs: list[str]) -> list[str]:
    """Repository files a Python file imports with ordinary import statements."""
    tree = parse(root, rel, cache)
    if tree is None:
        return []
    here = PurePosixPath(rel).parent.as_posix()
    here = '' if here == '.' else here
    own, _unknown = py_loads.sys_path_dirs(tree, rel)
    dirs = [here, *own, *extra_dirs, '', 'src', 'lib']
    out: list[str] = []

    def add(spec: str, bases: list[str]) -> None:
        for base in bases:
            found = py_loads.resolve_in_dirs(spec, [base], exists)
            if found and found != rel:
                for p in [found, *package_inits(found, base, exists)]:
                    if p not in out:
                        out.append(p)
                return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name, dirs)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = PurePosixPath(rel).parent
                for _ in range(node.level - 1):
                    base = base.parent
                b = '' if str(base) == '.' else base.as_posix()
                for a in node.names:
                    add(f'{node.module}.{a.name}' if node.module else a.name, [b])
                if node.module:
                    add(node.module, [b])
                else:
                    init = f'{b}/__init__.py' if b else '__init__.py'
                    if init in exists and init != rel and init not in out:
                        out.append(init)
            elif node.module:
                for a in node.names:
                    add(f'{node.module}.{a.name}', dirs)
                add(node.module, dirs)
    return out


def conftest_dirs(root: Path, rel: str, exists: set, cache: dict) -> tuple[list[str], list[str]]:
    """(folders the conftest.py files above a test put on sys.path, those conftest files)."""
    dirs, files = [], py_loads.conftests_above(rel, exists)
    for c in files:
        tree = parse(root, c, cache)
        if tree is not None:
            dirs += [d for d in py_loads.sys_path_dirs(tree, c)[0] if d not in dirs]
    return dirs, files


def baseline(root: Path, scoped: list[str]) -> dict:
    files = [f for f in repo_layout.walk_files(root) if f.endswith('.py')]
    exists = set(files)
    cache: dict = {}
    edges: dict[str, list[str]] = {}
    targets = set(scoped)
    tests = [f for f in files if diff_scope.is_test_path(f, root)[0]
             and PurePosixPath(f).name not in ('conftest.py', '__init__.py') and f not in targets]
    out_tests, not_reaching, unresolved = [], [], []
    import builtin_mutator
    child = builtin_mutator.launched_as_child(root, scoped, set())
    child_by_test: dict[str, list[tuple[str, str]]] = {}
    for target, lines in child.items():
        for ln in lines:
            child_by_test.setdefault(ln.rsplit(':', 1)[0], []).append((target, ln))

    def out_edges(rel: str, extra: list[str]) -> list[str]:
        key = rel + '\0' + '\0'.join(extra)
        if key not in edges:
            edges[key] = imports_of(root, rel, exists, cache, extra)
        return edges[key]
    for t in tests:
        extra, conftests = conftest_dirs(root, t, exists, cache)
        loads = py_loads.loaded_files(root, t)
        by_path = {x['path']: x.get('via') or 'path' for x in loads.get('loads') or []
                   if not str(x.get('via', '')).startswith('import')}
        unresolved += [{'path': t, 'line': u.get('line'), 'expr': u.get('expr')} for u in loads.get('unresolved') or []]
        first = [*out_edges(t, extra), *[x['path'] for x in loads.get('loads') or []], *conftests]
        parent: dict[str, str] = {}
        todo = []
        for p in first:
            if p not in parent and p != t:
                parent[p] = t
                todo.append(p)
        hits = []
        while todo and len(parent) < LIMIT:
            p = todo.pop(0)
            if p in targets:
                hits.append(p)
                continue
            for q in out_edges(p, extra):
                if q not in parent and q != t:
                    parent[q] = p
                    todo.append(q)
        if hits:
            hit = min(hits, key=lambda h: (chain_len(parent, h, t), h))
            chain = [hit]
            while chain[-1] != t:
                chain.append(parent[chain[-1]])
            chain.reverse()
            if len(chain) == 2:
                via = f'loads {hit} by path ({by_path[hit]})' if hit in by_path else f'imports {hit}'
            else:
                via = f'through {chain[1]}'
            out_tests.append({'path': t, 'via': via, 'reaches': sorted(hits), 'chain': chain})
        elif t in child_by_test:
            target, ln = child_by_test[t][0]
            out_tests.append({'path': t, 'via': f'runs {target} as a child process ({ln}; not traced in-process)',
                              'reaches': sorted({x for x, _ in child_by_test[t]}), 'chain': [t, target]})
        else:
            not_reaching.append(t)
    out_tests.sort(key=lambda x: x['path'])
    return {'scoped': scoped, 'tests': out_tests, 'not_reaching': not_reaching, 'unresolved': unresolved,
            'tests_args': [a for x in out_tests for a in ('--tests', x['path'])],
            'method': 'static: import statements, sys.path folders, loads by file path and child-process launches; '
                      'tests that reach the code through plugins, entry points or names built at run time are not seen'}


def chain_len(parent: dict, node: str, root: str) -> int:
    n = 0
    while node != root and n < 10000:
        node = parent[node]
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--scope', type=Path, help='diff_scope.py or path_scope.py output')
    ap.add_argument('--path', action='append', default=[], help='a scoped Python file (repeatable), instead of --scope')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    scoped = [p.replace('\\', '/').split(':')[0] for p in args.path]
    if args.scope:
        scope = json.loads(args.scope.read_text(encoding='utf-8'))
        scoped += [f['path'] for f in scope['files'] if f.get('kind') == 'source' and f.get('status') != 'deleted'
                   and (f.get('ranges') or f.get('deleted_at'))]
    scoped = sorted({p for p in scoped if p.endswith('.py')})
    if not scoped:
        print('ERROR: no scoped Python source file (give --scope or --path); for other languages use the runner\'s own '
              'selection (go test ./pkg/..., cargo test -p <crate>, vitest related)', file=sys.stderr)
        return 2
    out = baseline(root, scoped)
    if args.out:
        args.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    for t in out['tests']:
        print(f'{t["path"]}: {t["via"]}')
    print(f'{len(out["tests"])} test file(s) reach the scope, {len(out["not_reaching"])} do not')
    if out['tests']:
        print(' '.join(out['tests_args']))
    else:
        print('WARNING: no test file reaches the scoped files by import, path or child process; every mutant would be '
              'no_coverage. Check for tests that reach them another way before reporting that.', file=sys.stderr)
    for u in out['unresolved'][:10]:
        print(f'WARNING: {u["path"]}:{u["line"]} loads a file whose path could not be read ({u["expr"]}); it may reach '
              'the scope', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
