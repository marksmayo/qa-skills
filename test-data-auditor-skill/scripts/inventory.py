#!/usr/bin/env python3
"""Inventory the data tests depend on, and map each source to the tests that load it (stdlib only).

    python inventory.py <repo> --out inventory.json [--paths tests db ...] [--markers markers.json]

Finds fixture files (JSON, YAML, CSV, SQL, XML), factories, seed scripts, snapshots, HTTP
recordings (VCR/Polly cassettes, WireMock mappings, HAR files, MSW handlers), mobile fixture
assets and the inline literals in test files. Each source gets a kind
(fixture|factory|seed|snapshot|recording|inline), a format, and `loaded_by`: the pytest test ids
that load it, found by its file name appearing in

- a test function (that test),
- a test module outside any function (every test in the module),
- a conftest.py fixture (every test requesting that fixture, directly or through other fixtures),
- a source module (every test in modules that import it).

JavaScript/TypeScript tests are mapped per file (`file::*`). Binary, compressed or encrypted
files are listed as not_assessed. --markers reads {"anonymised_by": {"path": "reference"}} so a
file anonymised on purpose is recorded with its reference.
"""
from __future__ import annotations
import argparse, ast, json, os, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, detect_conventions  # noqa: E402

SKIP_DIRS = detect_conventions.SKIP_DIRS | {'.git'}
DATA_EXT = {'.json', '.yaml', '.yml', '.csv', '.tsv', '.sql', '.xml', '.har', '.snap', '.ambr', '.txt', '.ndjson', '.jsonl'}
BINARY_EXT = {'.db', '.sqlite', '.sqlite3', '.dump', '.bak', '.gz', '.zip', '.tar', '.tgz', '.7z', '.enc', '.gpg', '.age',
              '.pgp', '.bin', '.parquet', '.avro', '.xlsx', '.xls', '.pdf'}
TEST_DIRS = {'tests', 'test', '__tests__', 'spec', 'e2e', 'fixtures', 'testdata', 'test_data', 'androidTest', 'testFixtures'}
RECORDING_DIRS = {'cassettes', 'recordings', '__recordings__', 'mappings', 'wiremock', 'vcr', 'har', 'mocks', '__mocks__'}
SEED_RX = re.compile(r'(^|/)(seeds?|db/seed[^/]*|seed[^/]*\.(sql|json|ya?ml|csv|py|ts|js))(/|$)', re.I)
FACTORY_RX = re.compile(r'(^|/)(factories|factory|builders?)(/|\.|_)|(_factory|Factory|Builder)\.[a-z]+$|(^|/)factories\.py$')
MOBILE_ASSETS = re.compile(r'(^|/)(androidTest|test)/assets/|\.xctest|TestResources?/|(^|/)Fixtures/')


def walk(root: Path) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.endswith('.egg-info'))
        for f in sorted(fns):
            out.append((Path(dp) / f).relative_to(root).as_posix())
    return out


def in_test_area(path: str) -> bool:
    parts = set(PurePosixPath(path).parts[:-1])
    return bool(parts & TEST_DIRS) or detect_conventions.is_test_file(path) or PurePosixPath(path).name == 'conftest.py'


def kind_of(path: str) -> str | None:
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    parts = set(p.parts[:-1])
    if ext in ('.snap', '.ambr') or '__snapshots__' in parts:
        return 'snapshot'
    if parts & RECORDING_DIRS or ext == '.har' or (p.name.startswith('handlers.') and 'mocks' in parts):
        return 'recording'
    if SEED_RX.search(path):
        return 'seed'
    if FACTORY_RX.search(path) and ext in ('.py', '.ts', '.js', '.rb', '.kt', '.swift', '.dart'):
        return 'factory'
    if detect_conventions.is_test_file(path) or p.name == 'conftest.py':
        return 'inline'
    if (in_test_area(path) or MOBILE_ASSETS.search(path)) and (ext in DATA_EXT or ext in BINARY_EXT):
        return 'fixture'
    return None


class PyTests:
    """Test functions, fixtures and string mentions in a repo's pytest files."""

    def __init__(self, root: Path, paths: list[str]):
        self.root = root
        self.modules: dict[str, ast.Module] = {}
        for p in paths:
            if p.endswith('.py'):
                try:
                    self.modules[p] = ast.parse((root / p).read_text(encoding='utf-8', errors='replace'))
                except SyntaxError:
                    pass
        self.tests: dict[str, list[ast.FunctionDef]] = {}
        self.fixtures: dict[str, dict[str, ast.FunctionDef]] = {}     # conftest path -> name -> def
        for p, tree in self.modules.items():
            if detect_conventions.is_test_file(p):
                self.tests[p] = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                 and n.name.startswith('test')]
            fx = {}
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        'fixture' in ast.dump(d) for d in n.decorator_list):
                    fx[n.name] = n
            if fx:
                self.fixtures[p] = fx

    def test_id(self, path: str, fn: ast.FunctionDef) -> str:
        tree = self.modules[path]
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and fn in cls.body:
                return f'{path}::{cls.name}::{fn.name}'
        return f'{path}::{fn.name}'

    def visible_fixtures(self, test_path: str) -> dict[str, tuple[str, ast.FunctionDef]]:
        out = {}
        d = PurePosixPath(test_path).parent
        chain = []
        while True:
            chain.append((d / 'conftest.py').as_posix() if str(d) != '.' else 'conftest.py')
            if str(d) in ('.', ''):
                break
            d = d.parent
        for c in reversed(chain):
            for name, fn in self.fixtures.get(c, {}).items():
                out[name] = (c, fn)
        for name, fn in self.fixtures.get(test_path, {}).items():
            out[name] = (test_path, fn)
        return out

    def requested(self, fn: ast.FunctionDef, visible: dict) -> set[str]:
        """Fixture names a function requests, closed over fixtures requesting fixtures."""
        seen, todo = set(), [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        while todo:
            name = todo.pop()
            if name in seen or name not in visible:
                continue
            seen.add(name)
            todo += [a.arg for a in visible[name][1].args.args]
        return seen

    def all_tests(self) -> list[tuple[str, ast.FunctionDef]]:
        return [(p, fn) for p, fns in self.tests.items() for fn in fns]


def mentions(node: ast.AST, names: set[str]) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and any(x in n.value for x in names):
            return True
    return False


def importers(py: PyTests, module_path: str) -> set[str]:
    """Every test in test modules that import the Python module at module_path."""
    mod = PurePosixPath(module_path).with_suffix('').as_posix().replace('/', '.')
    out = set()
    for tp, fns in py.tests.items():
        for n in ast.walk(py.modules[tp]):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ''] if isinstance(n, ast.ImportFrom) else []
            if any(x and (x == mod or mod.endswith('.' + x) or x.endswith('.' + mod)) for x in names):
                out |= {py.test_id(tp, fn) for fn in fns}
                break
    return out


def loaded_by(rel: str, py: PyTests, js_tests: list[str], root: Path) -> list[str]:
    name = PurePosixPath(rel).name
    stem = PurePosixPath(rel).stem
    keys = {name, rel}
    out = set()
    autouse_or_module = {}
    for p, tree in py.modules.items():
        if not mentions(tree, keys):
            continue
        if p in py.tests:
            for fn in py.tests[p]:
                if mentions(fn, keys):
                    out.add(py.test_id(p, fn))
            module_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            if any(mentions(n, keys) for n in module_level):
                out |= {py.test_id(p, fn) for fn in py.tests[p]}
        fx = {n for n, f in py.fixtures.get(p, {}).items() if mentions(f, keys)}
        if fx or (p not in py.tests and PurePosixPath(p).name != 'conftest.py'):
            autouse_or_module[p] = fx
    for p, fx in autouse_or_module.items():
        if fx:
            for tp, fn in py.all_tests():
                vis = py.visible_fixtures(tp)
                if any(vis.get(f, (None,))[0] == p for f in py.requested(fn, vis) & fx):
                    out.add(py.test_id(tp, fn))
        elif PurePosixPath(p).name != 'conftest.py':
            out |= importers(py, p)
    for t in js_tests:
        try:
            text = (root / t).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if name in text or re.search(r'''['"/]''' + re.escape(stem) + r'''['"]''', text):
            out.add(f'{t}::*')
    return sorted(out)


def build(root: Path, paths: list[str] | None, markers: dict) -> dict:
    files = walk(root)
    if paths:
        files = [f for f in files if any(f == p or f.startswith(p.rstrip('/') + '/') for p in paths)]
    py = PyTests(root, walk(root))
    js_tests = [f for f in files if detect_conventions.is_test_file(f) and not f.endswith('.py')]
    anonymised = markers.get('anonymised_by', {})
    items = []
    for rel in files:
        k = kind_of(rel)
        if not k:
            continue
        p = root / rel
        ext = p.suffix.lower()
        size = p.stat().st_size
        entry = {'path': rel, 'kind': k, 'format': datafiles.fmt_of(p) if ext not in ('.snap', '.ambr', '.har') else ext[1:],
                 'bytes': size, 'loaded_by': [], 'status': 'scanned', 'reason': None}
        if ext in BINARY_EXT:
            entry.update(status='not_assessed', reason='binary, compressed or encrypted file', format=ext[1:])
        else:
            try:
                text = p.read_text(encoding='utf-8')
                entry['lines'] = text.count('\n') + (0 if text.endswith('\n') or not text else 1)
            except UnicodeDecodeError:
                entry.update(status='not_assessed', reason='not UTF-8 text')
        if rel in anonymised:
            entry['anonymised_by'] = anonymised[rel]
        if k == 'inline' and PurePosixPath(rel).name == 'conftest.py':
            mine = set(py.fixtures.get(rel, {}))
            entry['loaded_by'] = sorted(py.test_id(tp, fn) for tp, fn in py.all_tests()
                                        if any(py.visible_fixtures(tp).get(f, (None,))[0] == rel
                                               for f in py.requested(fn, py.visible_fixtures(tp)) & mine))
        elif k == 'inline':
            entry['loaded_by'] = ([py.test_id(rel, fn) for fn in py.tests.get(rel, [])] if rel.endswith('.py') else [f'{rel}::*'])
        elif k == 'factory' and rel.endswith('.py'):
            entry['loaded_by'] = sorted(importers(py, rel) | set(loaded_by(rel, py, js_tests, root)))
        else:
            entry['loaded_by'] = loaded_by(rel, py, js_tests, root)
        items.append(entry)
    tests = sorted({py.test_id(p, fn) for p, fn in py.all_tests()} | {f'{t}::*' for t in js_tests})
    return {'repo': root.name, 'inventory': items, 'tests': tests,
            'summary': {'sources': len(items), 'by_kind': {k: len([i for i in items if i['kind'] == k])
                                                            for k in sorted({i['kind'] for i in items})},
                        'not_assessed': len([i for i in items if i['status'] == 'not_assessed']),
                        'unused': [i['path'] for i in items if not i['loaded_by'] and i['kind'] != 'inline']}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--paths', nargs='*')
    ap.add_argument('--markers', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    markers = json.loads(args.markers.read_text(encoding='utf-8')) if args.markers else {}
    res = build(args.repo.resolve(), args.paths, markers)
    text = json.dumps(res, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
        s = res['summary']
        print(f'{s["sources"]} data sources ({", ".join(f"{v} {k}" for k, v in s["by_kind"].items())}), '
              f'{s["not_assessed"]} not assessed, {len(s["unused"])} unused -> {args.out}')
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
