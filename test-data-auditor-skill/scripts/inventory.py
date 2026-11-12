#!/usr/bin/env python3
"""Inventory the data tests depend on, and map each source to the tests that load it (stdlib only).

    python inventory.py <repo> --out inventory.json [--paths tests db ...] [--markers markers.json]

Finds fixture files (JSON, YAML, CSV, SQL, XML, SVG), factories, seed scripts, snapshots, HTTP
recordings (VCR/Polly cassettes, WireMock mappings, HAR files, MSW handlers), mobile fixture
assets and the inline literals in test files. Each source gets a kind
(fixture|factory|seed|snapshot|recording|inline), a format, and `loaded_by`: the pytest test ids
that load it, found by its file name appearing in

- a test function (that test),
- a test module outside any function (every test in the module),
- a conftest.py fixture (every test requesting that fixture, directly or through other fixtures),
- a source module (every test in modules that import it).

JavaScript/TypeScript tests are mapped per file (`file::*`). Setup files (vitest.setup.ts,
auth.setup.ts, setupTests.js) and helpers in a test area (__tests__/helpers/) are `support`: scanned
like test code, loaded by the tests that import them (a runner's setup file by every test file in
its package), and never counted as tests. Binary, compressed or encrypted files, and images, fonts
and media, are listed as not_assessed; an .xls that is really HTML or XML (sniffed) is scanned as
text, and an .xlsx workbook is read with stdlib zipfile. A file name counts as a mention only
from code in the same package root (nearest pyproject.toml, package.json, ...), unless the code
gives the path tail (`parent/name`); a glob (`DIR.glob("*.yaml")`) counts when the module also
names the directory. Test data trees are tests/, fixtures/, testdata/, test-data/, sample-data/
and the like; --data-dir names any other tree (repeatable). --markers reads {"anonymised_by": {"path": "reference"}} so a
file anonymised on purpose is recorded with its reference.
"""
from __future__ import annotations
import argparse, ast, fnmatch, json, os, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, detect_conventions  # noqa: E402

SKIP_DIRS = detect_conventions.SKIP_DIRS | {'.git', '.turbo', '.cache', '.venv', 'venv', '__pycache__'}
DATA_EXT = {'.json', '.yaml', '.yml', '.csv', '.tsv', '.sql', '.xml', '.har', '.snap', '.ambr', '.txt', '.ndjson', '.jsonl', '.svg'}
# images, fonts and media in a test area are test data too; their contents are not read, so they are listed as not_assessed
MEDIA_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.bmp', '.ico', '.tif', '.tiff', '.heic', '.otf', '.ttf', '.woff',
             '.woff2', '.eot', '.mp3', '.mp4', '.wav', '.ogg', '.webm', '.mov', '.m4a', '.flac'}
BINARY_EXT = {'.db', '.sqlite', '.sqlite3', '.dump', '.bak', '.gz', '.zip', '.tar', '.tgz', '.7z', '.enc', '.gpg', '.age',
              '.pgp', '.bin', '.parquet', '.avro', '.xlsx', '.xls', '.pdf'}
TEST_DIRS = {'tests', 'test', '__tests__', 'spec', 'e2e', 'fixtures', 'testdata', 'test_data', 'test-data', 'testData', 'TestData',
             'test-fixtures', 'test_fixtures', 'sample-data', 'sample_data', 'sampledata', 'sampleData', 'androidTest', 'testFixtures'}
# extra trees named with --data-dir (repo-relative), treated as test data areas; scan_data.py reads them back from the inventory
DATA_DIRS: set[str] = set()
# files that mark the root of a package or service: a bare file name is matched to code in the same root only
PROJECT_FILES = {'pyproject.toml', 'setup.py', 'setup.cfg', 'package.json', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'go.mod',
                 'Cargo.toml', 'composer.json', 'Gemfile'}
RECORDING_DIRS = {'cassettes', 'recordings', '__recordings__', 'mappings', 'wiremock', 'vcr', 'har', 'mocks', '__mocks__'}
SEED_RX = re.compile(r'(^|/)(seeds?|db/seed[^/]*|seed[^/]*\.(sql|json|ya?ml|csv|py|ts|js))(/|$)'
                     # a data migration that seeds rows (Alembic versions/007_seed_rooms.py, Django migrations/0004_seed_rooms.py)
                     r'|(^|/)(versions|migrations)/[\w-]*seed[\w-]*\.py$', re.I)
FACTORY_RX = re.compile(r'(^|/)(factories|factory|builders?)(/|\.|_)|(_factory|Factory|Builder)\.[a-z]+$|(^|/)factories\.py$')
# tool and package configuration that sits in a test directory (e2e/package.json) is not test data
TOOLING = re.compile(r'^(package(-lock)?\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb|composer\.(json|lock)|'
                     r'(ts|js)config(\.[\w-]+)?\.json|\.?eslintrc(\.\w+)?|\.prettierrc(\.\w+)?|\.babelrc|babel\.config\.json|'
                     r'renovate\.json|\.?nycrc(\.json)?|lerna\.json|nx\.json|turbo\.json|angular\.json|project\.json|'
                     r'(Pipfile|poetry|Gemfile|Cargo|uv)\.lock|requirements[\w.-]*\.txt|\.pre-commit-config\.yaml|'
                     r'docker-compose[\w.-]*\.ya?ml|\.gitlab-ci\.yml|codecov\.ya?ml|\.?codeclimate\.yml|sonar-project\.properties|'
                     r'cypress\.(json|env\.json)|playwright\.config\.json|jest\.config\.json|vitest\.config\.json|\.\w+rc\.json)$', re.I)
# setup and helper code that tests share: inventoried as support (scanned like test code), never counted as tests
SUPPORT_NAME = re.compile(r'^((vitest|jest|mocha|ava|karma|setup[-_.]?tests?|test[-_.]?setup|global[-_.]?setup|'
                          r'global[-_.]?teardown)([-_.][\w-]+)?\.[cm]?[jt]sx?|[\w-]+\.(setup|teardown)\.[cm]?[jt]sx?|setupTests\.[cm]?[jt]sx?)$', re.I)
SUPPORT_DIRS = {'helpers', 'helper', 'test-helpers', 'test_helpers', 'support', 'test-support', 'test_support', 'test-utils',
                'test_utils', 'testutils', 'testing-library', '__helpers__', 'utils'}
GLOBAL_SETUP = re.compile(r'^(vitest|jest|setup[-_.]?tests?|test[-_.]?setup|setupTests)[-_.]', re.I)
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
    return bool(parts & TEST_DIRS) or detect_conventions.is_test_file(path) or PurePosixPath(path).name == 'conftest.py' or \
        any(path.startswith(d.rstrip('/') + '/') for d in DATA_DIRS)


def package_roots(files: list[str]) -> set[str]:
    """Directories holding a project file (pyproject.toml, package.json, ...): each is a package or service root."""
    return {PurePosixPath(f).parent.as_posix() for f in files if PurePosixPath(f).name in PROJECT_FILES}


def root_of(path: str, roots: set[str]) -> str:
    """The nearest package root above path ('.' when none is)."""
    d = PurePosixPath(path).parent
    while True:
        if d.as_posix() in roots:
            return d.as_posix()
        if d.as_posix() in ('.', ''):
            return '.'
        d = d.parent

def is_support(path: str) -> bool:
    """Setup files (vitest.setup.ts, auth.setup.ts, setupTests.js) and helpers in a test area (__tests__/helpers/x.ts)."""
    p = PurePosixPath(path)
    if p.suffix.lower() not in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts'):
        return False
    if re.search(r'(^|[-_.])config([-_.]|$)', p.stem, re.I):
        return False                       # vitest.config.ts, jest.config.js: runner configuration, not test data
    if SUPPORT_NAME.match(p.name) and not re.search(r'\.(test|spec)\.', p.name):
        return True
    dirs = [d.lower() for d in p.parts[:-1]]
    if p.stem.lower() in ('helpers', 'helper', 'utils', 'test-utils', 'test_utils', 'support', 'setup', 'fixtures') and \
            set(p.parts[:-1]) & TEST_DIRS:
        return True                        # e2e/helpers.ts, e2e/fixtures.ts (Playwright fixtures)
    if any(d in ('test-support', 'test_support', 'test-helpers', 'test_helpers', 'test-utils', 'test_utils', 'testutils') for d in dirs):
        return not re.search(r'\.(test|spec)\.', p.name)
    return bool(set(dirs) & SUPPORT_DIRS) and (bool(set(p.parts[:-1]) & TEST_DIRS)) and not re.search(r'\.(test|spec)\.', p.name)


def kind_of(path: str) -> str | None:
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    parts = set(p.parts[:-1])
    if is_support(path):
        return 'support'
    if ext in ('.snap', '.ambr') or '__snapshots__' in parts:
        return 'snapshot'
    if parts & RECORDING_DIRS or ext == '.har' or (p.name.startswith('handlers.') and 'mocks' in parts):
        return 'recording'
    if SEED_RX.search(path):
        return 'seed'
    # a factory by file name counts only in a test area: product code has reader and formatter factories too
    if FACTORY_RX.search(path) and ext in ('.py', '.ts', '.js', '.rb', '.kt', '.swift', '.dart') and in_test_area(path):
        return 'factory'
    if detect_conventions.is_test_file(path) or p.name == 'conftest.py':
        return 'inline'
    if TOOLING.match(p.name):
        return None
    if (in_test_area(path) or MOBILE_ASSETS.search(path)) and (ext in DATA_EXT or ext in BINARY_EXT or ext in MEDIA_EXT):
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


_STRINGS: dict[int, tuple[ast.AST, list[str]]] = {}


def strings_in(node: ast.AST) -> list[str]:
    """The string constants under a node, with backslashes as slashes (cached per node)."""
    hit = _STRINGS.get(id(node))
    if hit and hit[0] is node:
        return hit[1]
    out = [n.value.replace('\\', '/') for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    _STRINGS[id(node)] = (node, out)
    return out


class FileRef:
    """How code can name one data file, from most to least specific:

    - its repo path, or a path tail (`parent/name`, also as separate strings: Path(DIR) / "parent" / "name");
    - its bare file name, counted only from code in the same package root (nearest pyproject.toml, package.json, ...),
      and only when no other file in that root has the name, unless the module also names the parent directory;
    - a glob over its directory (`DIR.glob("*.yaml")`, `rglob`, `import.meta.glob`), when the module also names the
      directory, from code in the same package root.

    A file name that merely appears somewhere in the repo is not enough: a test that names `config.yaml` in another
    service does not load this one."""

    def __init__(self, rel: str, roots: set[str], name_counts: dict[tuple[str, str], int], siblings: set[str] | None = None):
        p = PurePosixPath(rel)
        self.rel, self.name, self.stem, self.parent = rel, p.name, p.stem, p.parent.name
        self.grand = p.parent.parent.name
        self.siblings = (siblings or set()) - {self.name}
        self.tail = f'{self.parent}/{self.name}' if self.parent else self.name
        self.roots = roots
        self.root = root_of(rel, roots)
        self.unique = name_counts.get((self.root, self.name), 0) <= 1
        self.name_rx = re.compile(r'(?<![\w-])' + re.escape(self.name) + r'(?![\w])')

    def relation(self, where: str) -> str | None:
        """'same' when the code is in this file's package root, 'enclosing' when the file sits in a root above the code's
        (a repo-level test-data/ tree read by a service's tests), else None (another service: never a match)."""
        here = root_of(where, self.roots)
        if here == self.root:
            return 'same'
        if self.root == '.' or here.startswith(self.root + '/'):
            return 'enclosing'
        return None

    def parent_named(self, strs: list[str]) -> bool:
        if not self.parent:
            return False
        return any(self.parent in s.strip('/').split('/') for s in strs)

    def dir_named(self, strs: list[str]) -> bool:
        """The code names this file's directory (`seeds/families`, or Path(...) / "seeds" / "families") and none of the
        other files in it: a loader of the files there (DIR.glob(f"{name}.yaml"), os.listdir(DIR))."""
        if not (self.parent and self.grand):
            return False
        pair = f'{self.grand}/{self.parent}'
        named = any(s.rstrip('/').endswith(pair) for s in strs) or (self.parent in strs and self.grand in strs)
        return named and not any(n in s for s in strs for n in self.siblings)

    def decide(self, bare: bool, globbed: bool, dir_ref: bool, parent_in_module: bool, rel: str) -> bool:
        if dir_ref:
            return True
        if not (bare or globbed):
            return False
        if globbed and not bare:
            return parent_in_module
        # a bare name: in the same root it must be the only file of that name (or the directory is named too); from an
        # enclosing root the code must name the directory as well, or it may only be naming a file it never reads
        return (self.unique or parent_in_module) if rel == 'same' else parent_in_module

    def named_in(self, node: ast.AST, where: str, module: ast.AST) -> bool:
        """True when the code under node (in module file `where`) names this file."""
        strs = strings_in(node)
        if any(self.rel in s or self.tail in s for s in strs):
            return True
        rel = self.relation(where)
        if not rel:
            return False
        bare = any(self.name_rx.search(s) for s in strs)
        globbed = any(('*' in s or '?' in s) and fnmatch.fnmatch(self.name, s.rsplit('/', 1)[-1]) for s in strs)
        return self.decide(bare, globbed, self.dir_named(strs), self.parent_named(strings_in(module)), rel)

    def named_in_text(self, text: str, where: str) -> bool:
        """The same test for a JavaScript or TypeScript file, on its text."""
        t = text.replace('\\', '/')
        if self.rel in t or self.tail in t:
            return True
        rel = self.relation(where)
        if not rel:
            return False
        bare = bool(self.name_rx.search(t) or re.search(r'''['"/]''' + re.escape(self.stem) + r'''['"]''', t))
        strs = re.findall(r'''['"`]([^'"`\n]*)['"`]''', t)
        globbed = any(('*' in s or '?' in s) and fnmatch.fnmatch(self.name, s.rsplit('/', 1)[-1]) for s in strs)
        return self.decide(bare, globbed, self.dir_named(strs), self.parent_named(strs), rel)


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


def loaded_by(rel: str, py: PyTests, js_tests: list[str], root: Path, ref: FileRef) -> list[str]:
    out = set()
    autouse_or_module = {}
    for p, tree in py.modules.items():
        if p == rel or not ref.named_in(tree, p, tree):
            continue
        if p in py.tests:
            for fn in py.tests[p]:
                if ref.named_in(fn, p, tree):
                    out.add(py.test_id(p, fn))
            # module-level code, and helper functions that are neither tests nor fixtures (a loader used by parametrize)
            mine = set(py.tests[p]) | set(py.fixtures.get(p, {}).values())
            module_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                            or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n not in mine)]
            if any(ref.named_in(n, p, tree) for n in module_level):
                out |= {py.test_id(p, fn) for fn in py.tests[p]}
        fx = {n for n, f in py.fixtures.get(p, {}).items() if ref.named_in(f, p, tree)}
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
        if t != rel and ref.named_in_text(text, t):
            out.add(f'{t}::*')
    return sorted(out)


def build(root: Path, paths: list[str] | None, markers: dict, data_dirs: list[str] | None = None) -> dict:
    DATA_DIRS.clear()
    DATA_DIRS.update(d.replace('\\', '/').strip('/') for d in data_dirs or [] if d.strip('/'))
    all_files = walk(root)
    files = all_files
    if paths:
        files = [f for f in files if any(f == p or f.startswith(p.rstrip('/') + '/') for p in paths)]
    py = PyTests(root, all_files)
    js_tests = [f for f in files if detect_conventions.is_test_file(f) and not f.endswith('.py') and not is_support(f)]
    roots = package_roots(all_files)
    name_counts: dict[tuple[str, str], int] = {}
    for f in all_files:
        key = (root_of(f, roots), PurePosixPath(f).name)
        name_counts[key] = name_counts.get(key, 0) + 1
    by_dir: dict[str, set[str]] = {}
    for f in all_files:
        by_dir.setdefault(PurePosixPath(f).parent.as_posix(), set()).add(PurePosixPath(f).name)
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
        if ext in ('.xlsx', '.xlsm'):
            # read with stdlib zipfile: shared and inline strings, one text line per row
            try:
                entry['lines'] = datafiles.xlsx_text(p).count('\n')
            except datafiles.NotAssessed as exc:
                entry.update(status='not_assessed', reason=str(exc))
        elif ext == '.xls' and entry['format'] in ('html', 'xml'):
            # an HTML table or XML spreadsheet saved as .xls: text, so it is scanned (the extension says binary)
            try:
                text = p.read_text(encoding='utf-8')
                entry['lines'] = text.count('\n') + (0 if text.endswith('\n') or not text else 1)
                entry['note'] = f'{entry["format"].upper()} text with an .xls extension (sniffed), scanned as text'
            except UnicodeDecodeError:
                entry.update(status='not_assessed', reason=f'{entry["format"].upper()} with an .xls extension, not UTF-8 text')
        elif ext in BINARY_EXT:
            entry.update(status='not_assessed', reason='binary, compressed or encrypted file', format=ext[1:])
        elif ext in MEDIA_EXT:
            entry.update(status='not_assessed', reason='image, font or media file: its contents (metadata included) are not read',
                         format=ext[1:])
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
        elif k == 'support' and GLOBAL_SETUP.match(PurePosixPath(rel).name):
            # a runner's setup file runs before every test file in its package
            pkg = PurePosixPath(rel).parent.as_posix()
            entry['loaded_by'] = sorted(f'{t}::*' for t in js_tests if pkg == '.' or t.startswith(pkg + '/'))
        elif k == 'support' and re.search(r'\.setup\.[cm]?[jt]sx?$', rel):
            # a Playwright setup project (auth.setup.ts) runs before the specs in its directory
            pkg = PurePosixPath(rel).parent.as_posix()
            entry['loaded_by'] = sorted(f'{t}::*' for t in js_tests if pkg == '.' or t.startswith(pkg + '/'))
        elif k == 'support':
            # a helper is loaded by the tests that import it: match the import path's tail (helpers/auth-mock, e2e/helpers)
            p = PurePosixPath(rel)
            tail = re.compile(r'''['"][^'"]*''' + re.escape(f'{p.parent.name}/{p.stem}') + r'''(\.[cm]?[jt]sx?)?['"]''')
            out = []
            for t in js_tests:
                try:
                    if tail.search((root / t).read_text(encoding='utf-8', errors='replace')):
                        out.append(f'{t}::*')
                except OSError:
                    pass
            if p.parent.name in TEST_DIRS or p.parent.as_posix() == '.':
                near = re.compile(r'''['"]\.{1,2}/(?:[\w.-]+/)*''' + re.escape(p.stem) + r'''(\.[cm]?[jt]sx?)?['"]''')
                for t in js_tests:
                    if f'{t}::*' in out or PurePosixPath(t).parent != p.parent:
                        continue
                    try:
                        if near.search((root / t).read_text(encoding='utf-8', errors='replace')):
                            out.append(f'{t}::*')
                    except OSError:
                        pass
            entry['loaded_by'] = sorted(out)
        elif k == 'factory' and rel.endswith('.py'):
            entry['loaded_by'] = sorted(importers(py, rel) | set(loaded_by(rel, py, js_tests, root, FileRef(rel, roots, name_counts, by_dir.get(PurePosixPath(rel).parent.as_posix())))))
        else:
            entry['loaded_by'] = loaded_by(rel, py, js_tests, root, FileRef(rel, roots, name_counts, by_dir.get(PurePosixPath(rel).parent.as_posix())))
        items.append(entry)
    tests = sorted({py.test_id(p, fn) for p, fn in py.all_tests()} | {f'{t}::*' for t in js_tests})
    return {'repo': root.name, 'data_dirs': sorted(DATA_DIRS), 'inventory': items, 'tests': tests,
            'summary': {'sources': len(items), 'by_kind': {k: len([i for i in items if i['kind'] == k])
                                                            for k in sorted({i['kind'] for i in items})},
                        'not_assessed': len([i for i in items if i['status'] == 'not_assessed']),
                        'unused': [i['path'] for i in items if not i['loaded_by'] and i['kind'] != 'inline']}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--paths', nargs='*')
    ap.add_argument('--markers', type=Path)
    ap.add_argument('--data-dir', action='append', default=[], metavar='DIR',
                    help='a repo-relative tree that holds test data under a name the inventory does not know (repeatable)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    markers = json.loads(args.markers.read_text(encoding='utf-8')) if args.markers else {}
    res = build(args.repo.resolve(), args.paths, markers, args.data_dir)
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
