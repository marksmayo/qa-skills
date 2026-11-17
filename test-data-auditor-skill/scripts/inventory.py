#!/usr/bin/env python3
"""Inventory the data tests depend on, and map each source to the tests that load it (stdlib only).

    python inventory.py <repo> --out inventory.json [--paths tests db ...] [--markers markers.json]
                        [--data-dir DIR ...] [--max-files-per-dir N] [--no-fixture-dirs]

Fixture folders (examples/, samples/, testdata/ ... holding data files: sample projects and recorded runs, named by
repo_layout.py) are read as data trees by default and listed under fixture_dirs (--no-fixture-dirs leaves them out).
A file that a documented command (Markdown shell blocks, shell scripts, Makefiles, CI files, package.json scripts)
names, or a folder of it, or that a used data file names, is used_by_commands rather than unused; a script's own
self-test that builds a path to it (or to its sample project) loads it as <script>::*.

A directory outside the test areas holding 10+ data files that test code names, but not inventoried, is listed under
unlisted_data_dirs (and warned about); a data file anywhere that test code names by its repository path
("catalog/features.json") is inventoried, its folder listed under named_by_path. Files a test reaches through a script it
loads by path (spec_from_file_location, runpy, a sys.path folder: py_loads.py) count as loaded by that test; a glob
reaches only the folder it runs on; a file name code only writes or probes (.write_text, .exists) is not a load. A zip
archive is read in memory (archive_text.py): its members are listed as <archive>!<member>, text members scanned, the
rest not_assessed with the reason; other archives are listed as not opened. --max-files-per-dir samples a big directory evenly and records the rest as
not_assessed ("not sampled") under sampled_dirs.

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
and the like; --data-dir names any other tree (repeatable). Under a --data-dir, a JS/TS/Python data module (a records
array written as code) is listed as a fixture and other code is counted under data_dir_code; a served folder
(public/, static/) keeps only the files a test loads, the rest counted under served_dirs. --markers reads {"anonymised_by": {"path": "reference"}} so a
file anonymised on purpose is recorded with its reference.
"""
from __future__ import annotations
import argparse, ast, codecs, fnmatch, json, os, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive_text, datafiles, detect_conventions, jsparse, py_loads, repo_layout  # noqa: E402

SKIP_DIRS = detect_conventions.SKIP_DIRS | {'.git', '.turbo', '.cache', '.venv', 'venv', '__pycache__'}
# text formats read line by line (no records): pages, tickets and notes, patches, settings and styles keep invented people
# in their prose as often as JSON does
TEXT_DATA_EXT = {'.html', '.htm', '.md', '.markdown', '.diff', '.patch', '.toml', '.css',
                 # run logs and captured output: where machine paths, account names and host names end up
                 '.log', '.out', '.err', '.stderr', '.stdout', '.trace'}
DATA_EXT = {'.json', '.yaml', '.yml', '.csv', '.tsv', '.sql', '.xml', '.har', '.snap', '.ambr', '.txt', '.ndjson', '.jsonl',
            '.svg'} | TEXT_DATA_EXT
# documentation beside test data (a fixture tree's README) is not test data
DOC_NAME = re.compile(r'^(README|CHANGELOG|CHANGES|CONTRIBUTING|LICEN[CS]E|SECURITY|CODE_OF_CONDUCT|AUTHORS|NOTICE)([.-].*)?$', re.I)
# images, fonts and media in a test area are test data too; their contents are not read, so they are listed as not_assessed
MEDIA_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.bmp', '.ico', '.tif', '.tiff', '.heic', '.otf', '.ttf', '.woff',
             '.woff2', '.eot', '.mp3', '.mp4', '.wav', '.ogg', '.webm', '.mov', '.m4a', '.flac'}
BINARY_EXT = {'.db', '.sqlite', '.sqlite3', '.dump', '.bak', '.gz', '.zip', '.tar', '.tgz', '.7z', '.enc', '.gpg', '.age',
              '.pgp', '.bin', '.parquet', '.avro', '.xlsx', '.xls', '.pdf'}
BINARY_EXT |= set(archive_text.ARCHIVE_EXT)     # every archive format is listed; a .zip is opened in memory
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
# source code under a --data-dir tree is code, not data: everything else there is listed (unknown formats as not_assessed).
# A module that is mostly a records array written as code (a data module) is listed and scanned as data; other code
# there is counted under data_dir_code, stays product code, and is named in a warning
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts', '.go', '.rb', '.java', '.kt', '.cs', '.php', '.rs',
            '.swift', '.dart', '.c', '.h', '.cpp', '.sh'}
# folders a web app serves as they are (public/, static/): their files are the site's assets, not test fixtures, even when
# named with --data-dir; only the files a test loads are listed, the rest are counted under served_dirs
SERVED_DIRS = {'public', 'static', 'wwwroot', 'www', 'htdocs'}
DATA_MODULE_MAX_BYTES = 1_000_000
MOBILE_ASSETS = re.compile(r'(^|/)(androidTest|test)/assets/|\.xctest|TestResources?/|(^|/)Fixtures/')


NO_TESTS = ('no test files found (no pytest or JavaScript test files, no test functions in a test directory): the repository has '
            'no tests, so there is no test data to audit; say so in the report instead of calling the audit complete')


def walk(root: Path) -> list[str]:
    """Every file under root; build output, dependencies and VCS directories are pruned before descending."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.endswith('.egg-info'))
        for f in sorted(fns):
            out.append((Path(dp) / f).relative_to(root).as_posix())
    return out


# Directories named like a test directory (e2e, tests, spec...) that are product packages (a manifest that ships code),
# repo-relative. Filled by set_product_dirs() from the walked tree; the same rule as diff_scope.is_test_path.
PRODUCT_DIRS: dict[str, str] = {}
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'composer.json', 'pubspec.yaml', 'deno.json', 'deno.jsonc')


def product_package(root: Path, rel_dir: str, names: set[str], subdirs: set[str]) -> str | None:
    """Why rel_dir (a directory with a test-like name) is a product package, or None.

    A product package holds a package manifest AND ships code: a src/ directory, a package.json with
    main/module/exports/bin, a Cargo.toml with [lib] or [[bin]], non-test .go files beside a go.mod, or the Python
    package a pyproject.toml/setup.py/setup.cfg names. A manifest alone does not make one: a workspace package that
    holds only specs and a runner config (a Playwright e2e/ package) is the known test layout and stays test code.
    The rule is diff_scope.py's is_test_path (shared), copied because this skill does not carry diff_scope.py."""
    d = root / rel_dir
    found = [m for m in MANIFESTS if m in names]
    if not found:
        return None
    m = found[0]
    try:
        if 'src' in subdirs:
            return f'{m} and a src/ directory'
        if m == 'package.json':
            try:
                pkg = json.loads((d / m).read_text(encoding='utf-8', errors='replace'))
            except ValueError:
                pkg = {}
            fields = [k for k in ('main', 'module', 'exports', 'bin') if isinstance(pkg, dict) and pkg.get(k)]
            return f'package.json declaring {"/".join(fields)}' if fields else None
        if m == 'Cargo.toml':
            text = (d / m).read_text(encoding='utf-8', errors='replace')
            return 'Cargo.toml with a [lib] or [[bin]] target' if re.search(r'^\s*\[(lib|\[bin\])\]', text, re.M) else None
        if m == 'go.mod':
            return 'go.mod and non-test .go files' if any(n.endswith('.go') and not n.endswith('_test.go') for n in names) else None
        if m in ('pyproject.toml', 'setup.py', 'setup.cfg'):
            text = (d / m).read_text(encoding='utf-8', errors='replace')
            pk = {re.sub(r'[-.]', '_', n).lower() for n in re.findall(r'''(?m)^\s*name\s*[=:]\s*["']?([\w.-]+)''', text)}
            hit = [s for s in subdirs if s.lower() in pk and (d / s / '__init__.py').is_file()]
            return f'{m} and its Python package {hit[0]}/' if hit else None
    except OSError:
        return None
    return None


def set_product_dirs(root: Path, files: list[str]) -> dict[str, str]:
    """Find the directories named like test directories that are product packages, from the walked file list."""
    PRODUCT_DIRS.clear()
    names: dict[str, set[str]] = {}
    subdirs: dict[str, set[str]] = {}
    for f in files:
        p = PurePosixPath(f)
        names.setdefault(p.parent.as_posix(), set()).add(p.name)
        parts = p.parts[:-1]
        for i in range(1, len(parts)):
            subdirs.setdefault('/'.join(parts[:i]), set()).add(parts[i])
    for d, ns in names.items():
        if PurePosixPath(d).name in TEST_DIRS and ns & set(MANIFESTS):
            why = product_package(root, d, ns, subdirs.get(d, set()))
            if why:
                PRODUCT_DIRS[d] = f'{d}/ is a product package ({why})'
    return dict(PRODUCT_DIRS)


def test_dirs_of(path: str) -> set[str]:
    """The test-like directory names on path, leaving out any that is a product package (packages/e2e with its own
    manifest and src/ is product code; its tests/ is still tests)."""
    parts = PurePosixPath(path).parts[:-1]
    return {seg for i, seg in enumerate(parts) if seg in TEST_DIRS and '/'.join(parts[:i + 1]) not in PRODUCT_DIRS}


def in_data_dir(path: str) -> bool:
    return any(path.startswith(d.rstrip('/') + '/') for d in DATA_DIRS)


def in_test_area(path: str) -> bool:
    # code under a --data-dir tree does not become test code by it (build() lists the data modules there)
    return bool(test_dirs_of(path)) or detect_conventions.is_test_file(path) or PurePosixPath(path).name == 'conftest.py' or \
        (in_data_dir(path) and PurePosixPath(path).suffix.lower() not in CODE_EXT)


def data_dir_of(path: str) -> str | None:
    """The --data-dir tree a path is under (the longest), or None."""
    hits = [d for d in DATA_DIRS if path.startswith(d.rstrip('/') + '/')]
    return max(hits, key=len) if hits else None


def is_served_dir(d: str) -> bool:
    """A --data-dir that is a folder the app serves as is (public/, web/static/), not inside a test area."""
    parts = set(PurePosixPath(d).parts)
    return bool(parts & SERVED_DIRS) and not parts & TEST_DIRS


def data_module(root: Path, rel: str) -> int:
    """How many records a source file under --data-dir holds when it is a data module (a records array written as code:
    `export const birds = [{...}, {...}]`, `ROWS = [dict(...), ...]`), else 0. A data module has three or more literal
    records of two or more constant fields, at least two with the same keys, and its record fields sit on at least 40% of
    its non-blank lines, so a component with a style object or a config module is not one."""
    p = root / rel
    try:
        if p.stat().st_size > DATA_MODULE_MAX_BYTES:
            return 0
        text = p.read_text(encoding='utf-8')
        if jsparse.is_js(rel):
            recs = jsparse.records(text, rel)
        elif rel.endswith('.py'):
            recs = datafiles.python_records(text, rel)
        else:
            return 0
    except (OSError, UnicodeDecodeError, datafiles.NotAssessed, RecursionError, ValueError):
        return 0
    recs = [r for r in recs if len(r['fields']) >= 2]
    shapes: dict[frozenset, int] = {}
    for r in recs:
        k = frozenset(r['fields'])
        shapes[k] = shapes.get(k, 0) + 1
    if len(recs) < 3 or max(shapes.values(), default=0) < 2:
        return 0
    field_lines = {v['line'] for r in recs for v in r['fields'].values()}
    body = [ln for ln in text.split('\n') if ln.strip() and not ln.strip().startswith(('//', '#', '/*', '*'))]
    return len(recs) if body and len(field_lines) / len(body) >= 0.4 else 0


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
            test_dirs_of(path):
        return True                        # e2e/helpers.ts, e2e/fixtures.ts (Playwright fixtures)
    if any(d in ('test-support', 'test_support', 'test-helpers', 'test_helpers', 'test-utils', 'test_utils', 'testutils') for d in dirs):
        return not re.search(r'\.(test|spec)\.', p.name)
    return bool(set(dirs) & SUPPORT_DIRS) and bool(test_dirs_of(path)) and not re.search(r'\.(test|spec)\.', p.name)


SEED_SCRIPT_STEM = re.compile(r'^seeds?([-_](data|db|database|dev|demo|test|users?|rows|records|fixtures?))?$', re.I)


def product_script_named_seed(path: str) -> bool:
    """A code file that matched the seed pattern only by its name (seed_report.py, seeder_utils.ts) outside a test area,
    a seeds/ or db/ folder and a migrations tree: "seed" is a verb there, not seed rows. A script named seed.py,
    seeds.ts or seed_data.py still is one."""
    p = PurePosixPath(path)
    if p.suffix.lower() not in CODE_EXT:
        return False
    dirs = {d.lower() for d in p.parts[:-1]}
    if dirs & {'seeds', 'seed', 'db', 'database', 'migrations', 'versions'} or in_test_area(path):
        return False
    return not SEED_SCRIPT_STEM.match(p.stem)


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
    if DOC_NAME.match(p.name) and ext in ('.md', '.markdown', '.txt', ''):
        return None                        # a README beside the data documents it
    if SEED_RX.search(path) and not product_script_named_seed(path):
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
    if in_data_dir(path) and ext not in CODE_EXT:
        return 'fixture'          # a tree named with --data-dir: every file is listed, an unknown format as not_assessed
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

    def path_reach(self) -> dict[str, set[str]]:
        """{test file: the repository files it loads by path}: scripts loaded with spec_from_file_location, runpy or
        an import from a folder the test (or a conftest.py above it) puts on sys.path, and what those scripts import
        from their own folder (py_loads.loaded_files). Computed once, only for test files whose text names a loader."""
        if '_path_reach' not in self.__dict__:
            out: dict[str, set[str]] = {}
            for tp in self.tests:
                try:
                    text = (self.root / tp).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                if not py_loads.may_load(text):
                    continue
                reach = set(py_loads.loaded_files(self.root, tp).get('reaches') or [])
                if reach:
                    out[tp] = reach
            self._path_reach = out
        return self._path_reach

    def test_id(self, path: str, fn: ast.FunctionDef) -> str:
        cache = self.__dict__.setdefault('_cache_ids', {})
        hit = cache.get(id(fn))
        if hit is None or hit[0] is not fn:
            hit = cache[id(fn)] = (fn, self._test_id(path, fn))
        return hit[1]

    def requesting(self, fixture_file: str, names: set[str]) -> set[str]:
        """Every test that requests one of the named fixtures defined in fixture_file, directly or through other
        fixtures (cached: every file of a data tree a fixture globs asks the same question)."""
        cache = self.__dict__.setdefault('_cache_requesting', {})
        key = (fixture_file, frozenset(names))
        if key not in cache:
            out = set()
            for tp, fn in self.all_tests():
                vis = self.visible_fixtures(tp)
                if any(vis.get(f, (None,))[0] == fixture_file for f in self.requested(fn, vis) & names):
                    out.add(self.test_id(tp, fn))
            cache[key] = out
        return cache[key]

    def _test_id(self, path: str, fn: ast.FunctionDef) -> str:
        tree = self.modules[path]
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and fn in cls.body:
                return f'{path}::{cls.name}::{fn.name}'
        return f'{path}::{fn.name}'

    def visible_fixtures(self, test_path: str) -> dict[str, tuple[str, ast.FunctionDef]]:
        """The fixtures a test module sees (cached per module: asked once per test and data file)."""
        cache = self.__dict__.setdefault('_cache_visible', {})
        if test_path not in cache:
            cache[test_path] = self._visible_fixtures(test_path)
        return cache[test_path]

    def _visible_fixtures(self, test_path: str) -> dict[str, tuple[str, ast.FunctionDef]]:
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
        """Fixture names a function requests, closed over fixtures requesting fixtures (cached per function and scope)."""
        cache = self.__dict__.setdefault('_cache_requested', {})
        key = (id(fn), id(visible))
        hit = cache.get(key)
        if hit is None or hit[0] is not fn or hit[1] is not visible:
            hit = cache[key] = (fn, visible, self._requested(fn, visible))
        return hit[2]

    def _requested(self, fn: ast.FunctionDef, visible: dict) -> set[str]:
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


_STRINGS: dict[int, tuple[ast.AST, list[str], str, list[str]]] = {}


def _node_strings(node: ast.AST) -> tuple[ast.AST, list[str], str, list[str]]:
    hit = _STRINGS.get(id(node))
    if hit and hit[0] is node:
        return hit
    out = [n.value.replace('\\', '/') for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # the strings joined (one substring search per data file instead of one per string) and the glob patterns alone,
    # each glob's last segment compiled once (fnmatch.fnmatch semantics: case folded as the OS does)
    # a bare wildcard ("*", "*.*") names no file: listing a temporary folder is not a loader of every fixture; a loader of
    # a whole directory is found by the directory it names (FileRef.dir_named)
    tails = sorted({t for t in (s.rsplit('/', 1)[-1] for s in out if '*' in s or '?' in s) if t.strip('*?.')})
    hit = (node, out, '\n'.join(out), [re.compile(fnmatch.translate(os.path.normcase(t))) for t in tails])
    _STRINGS[id(node)] = hit
    return hit


def strings_in(node: ast.AST) -> list[str]:
    """The string constants under a node, with backslashes as slashes (cached per node)."""
    return _node_strings(node)[1]


# calls that write a path, and calls that only probe one: a file name used only to write (and to probe a path built on a
# scratch or output folder) is the code's own output, never a load; a probe of a committed fixture is a use of it
WRITE_ATTRS = {'write_text', 'write_bytes', 'touch', 'mkdir', 'unlink', 'rmdir'}
PROBE_ATTRS = {'exists', 'is_file', 'is_dir'}
OUTPUT_NAMES = {'tmp_path', 'tmpdir', 'tmp', 'tmp_dir', 'temp_dir', 'tempdir', 'tmpdirname', 'output', 'out', 'out_dir',
                'output_dir', 'outdir', 'scratch', 'workdir', 'work_dir'}
_WRITE_ONLY: dict[int, tuple[ast.AST, set[str]]] = {}


def _write_mode(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == 'mode'), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and bool(set(mode.value) & set('wax'))


def write_only_names(module: ast.AST) -> set[str]:
    """The string constants of a module that are used only to build paths it writes or probes for existence
    (`path = out / "report.json"; path.write_text(...)`, `(tmp / "x.json").exists()`, `open(p, "w")`): naming such a
    file is not loading one. Cached per module; empty unless the module writes."""
    hit = _WRITE_ONLY.get(id(module))
    if hit and hit[0] is module:
        return hit[1]
    out: set[str] = set()
    calls = [n for n in ast.walk(module) if isinstance(n, ast.Call)]
    def on_output(e) -> bool:
        return any(isinstance(x, ast.Name) and x.id in OUTPUT_NAMES for x in ast.walk(e))
    writes = [c for c in calls if (isinstance(c.func, ast.Attribute) and (c.func.attr in WRITE_ATTRS or (
              c.func.attr in PROBE_ATTRS and on_output(c.func.value))))
              or (isinstance(c.func, ast.Name) and c.func.id == 'open' and c.args and _write_mode(c))]
    if writes:
        targets = [c.func.value if isinstance(c.func, ast.Attribute) else c.args[0] for c in writes]
        written_vars = {t.id for t in targets if isinstance(t, ast.Name)}
        target_ids = {id(t) for t in targets}
        # a variable counts as written only when every use of it is the receiver of a write or probe
        for n in ast.walk(module):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in written_vars and id(n) not in target_ids:
                written_vars.discard(n.id)
        exprs = [t for t in targets if not isinstance(t, ast.Name)]
        for n in ast.walk(module):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in written_vars for t in n.targets):
                exprs.append(n.value)
        written: dict[str, int] = {}
        for e in exprs:
            for c in ast.walk(e):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    written[c.value] = written.get(c.value, 0) + 1
        if written:
            total: dict[str, int] = {}
            for c in ast.walk(module):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value in written:
                    total[c.value] = total.get(c.value, 0) + 1
            out = {v.replace('\\', '/') for v, k in written.items() if total.get(v) == k}
    _WRITE_ONLY[id(module)] = (module, out)
    return out


_PARENT: dict[tuple[int, str], tuple[list[str], bool]] = {}
_ROOT_OF: dict[str, str] = {}


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
        self.name_case = os.path.normcase(self.name)
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
        here = _ROOT_OF.get(where)
        if here is None:
            here = _ROOT_OF[where] = root_of(where, self.roots)
        if here == self.root:
            return 'same'
        if self.root == '.' or here.startswith(self.root + '/'):
            return 'enclosing'
        return None

    def parent_named(self, strs: list[str]) -> bool:
        if not self.parent:
            return False
        key = (id(strs), self.parent)
        hit = _PARENT.get(key)
        if hit is None or hit[0] is not strs:
            hit = _PARENT[key] = (strs, any(self.parent in s.strip('/').split('/') for s in strs))
        return hit[1]

    def glob_dir_named(self, strs: list[str]) -> bool:
        """The code names this file's folder as a folder to glob: a string ending in it (`data/results`, "results"), or
        a pattern in it (`results/*.json`). A folder above it (`data` in `data/results`) or beside a named file
        (`data/features.json`) is not this file's folder."""
        if not self.parent:
            return False
        key = (id(strs), '\0glob', self.parent)
        hit = _PARENT.get(key)
        if hit is None or hit[0] is not strs:
            found = False
            for s in strs:
                segs = s.strip('/').split('/')
                if segs[-1] == self.parent or (len(segs) > 1 and segs[-2] == self.parent and ('*' in segs[-1] or '?' in segs[-1])):
                    found = True
                    break
            hit = _PARENT[key] = (strs, found)
        return hit[1]

    def dir_named(self, strs: list[str]) -> bool:
        """The code names this file's directory (`seeds/families`, or Path(...) / "seeds" / "families") and none of the
        other files in it: a loader of the files there (DIR.glob(f"{name}.yaml"), os.listdir(DIR))."""
        if not (self.parent and self.grand):
            return False
        pair = f'{self.grand}/{self.parent}'
        named = any(s.rstrip('/').endswith(pair) for s in strs) or (self.parent in strs and self.grand in strs)
        return named and not any(n in s for s in strs for n in self.siblings)

    def decide(self, bare: bool, globbed: bool, dir_ref: bool, parent_in_module: bool, rel: str,
               glob_dir: bool | None = None) -> bool:
        if dir_ref:
            return True
        if not (bare or globbed):
            return False
        if globbed and not bare:
            # a glob reaches the files of the folder it is run on: the module must name this file's own folder
            return parent_in_module if glob_dir is None else glob_dir
        # a bare name: in the same root it must be the only file of that name (or the directory is named too); from an
        # enclosing root the code must name the directory as well, or it may only be naming a file it never reads
        return (self.unique or parent_in_module) if rel == 'same' else parent_in_module

    def named_in(self, node: ast.AST, where: str, module: ast.AST) -> bool:
        """True when the code under node (in module file `where`) names this file. Each node's strings are searched
        joined (cached per node), so a data tree of thousands of files costs a few C-level searches per file and node."""
        _, strs, blob, globs = _node_strings(node)
        if self.name not in blob and not globs and not (self.parent and self.parent in blob):
            return False                   # every way of naming the file needs its name, a glob or its directory
        if self.rel in blob or self.tail in blob:
            return True
        rel = self.relation(where)
        if not rel:
            return False
        bare = self.name in blob and bool(self.name_rx.search(blob))
        if bare and self.name in write_only_names(module):
            bare = False                   # the code only writes or probes a file of this name (its own output)
        globbed = any(rx.match(self.name_case) for rx in globs)
        dir_ref = self.dir_named(strs) if self.parent and self.parent in blob else False
        mod_strs = strings_in(module)
        return self.decide(bare, globbed, dir_ref, self.parent_named(mod_strs), rel,
                           self.glob_dir_named(mod_strs) if globbed and not bare else None)

    def named_in_text(self, view: tuple[str, list[str]], where: str) -> bool:
        """The same test for a JavaScript or TypeScript file, on its text (view: js_view()'s text and string literals)."""
        t, strs = view
        if self.rel in t or self.tail in t:
            return True
        rel = self.relation(where)
        if not rel:
            return False
        bare = bool(self.name_rx.search(t) or re.search(r'''['"/]''' + re.escape(self.stem) + r'''['"]''', t))
        globbed = any(('*' in s or '?' in s) and s.rsplit('/', 1)[-1].strip('*?.')
                      and fnmatch.fnmatch(self.name, s.rsplit('/', 1)[-1]) for s in strs)
        return self.decide(bare, globbed, self.dir_named(strs), self.parent_named(strs), rel,
                           self.glob_dir_named(strs) if globbed and not bare else None)


def count_lines(p: Path) -> int:
    """Lines in a UTF-8 text file, read in 1 MB chunks so a huge fixture is never held in memory whole. Raises
    UnicodeDecodeError for a file that is not UTF-8 (a NUL byte marks a binary file under a text extension)."""
    dec = codecs.getincrementaldecoder('utf-8')()
    lines, last, empty = 0, '', True
    with p.open('rb') as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            if b'\x00' in chunk:
                raise UnicodeDecodeError('utf-8', chunk[:1], 0, 1, 'NUL byte: a binary file')
            text = dec.decode(chunk)
            if text:
                empty, last = False, text[-1]
                lines += text.count('\n')
    dec.decode(b'', final=True)
    return lines + (0 if empty or last == '\n' else 1)


_VIEWS: dict[tuple[str, str], tuple[str, list[str]] | None] = {}


def js_view(root: Path, rel: str) -> tuple[str, list[str]] | None:
    """A JS/TS file's text (backslashes as slashes) and its string literals, read once per run (None if unreadable).
    Every data file is matched against every test file, so reading them per data file was 100k+ reads on a monorepo."""
    key = (str(root), rel)
    if key not in _VIEWS:
        try:
            t = (root / rel).read_text(encoding='utf-8', errors='replace').replace('\\', '/')
            _VIEWS[key] = (t, re.findall(r'''['"`]([^'"`\n]*)['"`]''', t))
        except OSError:
            _VIEWS[key] = None
    return _VIEWS[key]


def importers(py: PyTests, module_path: str) -> set[str]:
    """Every test in test modules that import the Python module at module_path (cached per module: a loader module
    that names a data tree is asked once per data file)."""
    cache = py.__dict__.setdefault('_cache_importers', {})
    if module_path not in cache:
        cache[module_path] = _importers(py, module_path)
    return cache[module_path]


def _importers(py: PyTests, module_path: str) -> set[str]:
    mod = PurePosixPath(module_path).with_suffix('').as_posix().replace('/', '.')
    out = set()
    reach = py.path_reach()
    for tp, fns in py.tests.items():
        if module_path in reach.get(tp, ()):
            out |= {py.test_id(tp, fn) for fn in fns}      # loaded by file path, not by an import statement
            continue
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
        if p in SELF_TEST_FILES and p not in py.tests:
            # a script with its own self-test reads a file only by a path that resolves from where the script sits
            # (UseIndex): a bare file name in a tool repository is shared by every sample project's run folder
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
            out |= py.requesting(p, fx)
        elif PurePosixPath(p).name != 'conftest.py':
            out |= importers(py, p)
    for t in js_tests:
        view = js_view(root, t)
        if view is not None and t != rel and ref.named_in_text(view, t):
            out.add(f'{t}::*')
    return sorted(out)


# ---------------------------------------------------------------- data files that commands and self-tests read
# A tool repository's fixtures are often read by commands (the README's or the repository guide's validator commands, a
# commands.sh that regenerates an example) and by scripts' own self-tests, not by test functions that name the file.
CMD_WORDS = {'python', 'python3', 'py', 'node', 'npx', 'npm', 'pnpm', 'yarn', 'bash', 'sh', 'go', 'cargo', 'pytest', 'uv',
             'make', 'deno', 'bun', 'pwsh', 'ruby', 'php', 'java', 'dotnet', 'jq', 'diff', 'cat', 'cp'}
SHELL_FENCES = {'', 'bash', 'sh', 'shell', 'console', 'zsh', 'powershell', 'ps1', 'pwsh', 'cmd', 'bat', 'shell-session',
                'sh-session', 'text'}
CI_FILES = re.compile(r'(^|/)(\.github/workflows/[^/]+\.ya?ml|azure-pipelines[\w.-]*\.ya?ml|\.gitlab-ci\.ya?ml|'
                      r'\.circleci/config\.ya?ml|Makefile|[\w.-]+\.mk|justfile|Justfile|tox\.ini|noxfile\.py)$')
SH_ASSIGN = re.compile(r'^\s*(?:export\s+)?([A-Za-z_]\w*)=(\S*)\s*$')
SH_VAR = re.compile(r'\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)')
SELF_TEST_FILES: set[str] = set()      # scripts with their own self-test (filled by build())


def _shell_tokens(cmd: str) -> list[str]:
    import shlex
    try:
        toks = shlex.split(cmd, comments=True, posix=True)
    except ValueError:
        toks = cmd.split()
    out = []
    for t in toks:
        if t.startswith('-'):
            if '=' not in t:
                continue
            t = t.split('=', 1)[1]
        t = t.strip('\'"`').rstrip(',;:)').lstrip('(')
        if t.startswith('./'):
            t = t[2:]
        t = t.replace('\\', '/')
        if not t or re.search(r'[<>*?{}$|&()\[\]]', t) or '://' in t or t.startswith(('/', '~')):
            continue
        if '/' in t or re.search(r'\.\w{1,6}$', t):
            out.append(t.rstrip('/'))
    return out


def command_sources(root: Path, files: list[str]) -> list[tuple[str, int, str]]:
    """(file, line, command) for every command a repository documents or scripts: shell fences and inline command spans
    in Markdown, shell scripts (with their simple VAR=value assignments expanded), Makefiles, CI workflows and
    package.json scripts."""
    out: list[tuple[str, int, str]] = []
    for rel in files:
        p = PurePosixPath(rel)
        ext = p.suffix.lower()
        is_md, is_sh = ext in ('.md', '.markdown'), ext in ('.sh', '.bash')
        is_ci, is_pkg = bool(CI_FILES.search(rel)), p.name == 'package.json'
        if not (is_md or is_sh or is_ci or is_pkg):
            continue
        try:
            if (root / rel).stat().st_size > 2_000_000:
                continue
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if is_pkg:
            try:
                scripts = (json.loads(text) or {}).get('scripts') or {}
            except (ValueError, AttributeError):
                scripts = {}
            for v in scripts.values() if isinstance(scripts, dict) else []:
                if isinstance(v, str):
                    out.append((rel, 1, v))
            continue
        lines = text.replace('\\\r\n', ' ').replace('\\\n', ' ').split('\n')
        if is_sh:
            env: dict[str, str] = {}
            for n, ln in enumerate(lines, 1):
                ln = SH_VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), ln)
                a = SH_ASSIGN.match(ln)
                if a:
                    if '$(' not in a.group(2) and '`' not in a.group(2):
                        env[a.group(1)] = a.group(2).strip('\'"')
                    continue
                out.append((rel, n, ln))
            continue
        if is_ci:
            for n, ln in enumerate(lines, 1):
                s = re.sub(r'^\s*(?:-\s*)?(?:run|script|command|cmd)?\s*:?\s*\|?\s*', '', ln.lstrip('\t'))
                if s.split(' ', 1)[0] in CMD_WORDS:
                    out.append((rel, n, s))
            continue
        fence = None
        for n, ln in enumerate(lines, 1):
            m = re.match(r'^\s*(```+|~~~+)\s*([\w+-]*)', ln)
            if m:
                fence = None if fence is not None else m.group(2).lower()
                continue
            if fence is not None:
                if fence in SHELL_FENCES:
                    out.append((rel, n, re.sub(r'^\s*(?:\$|>|PS[^>]*>)\s+', '', ln)))
                continue
            for span in re.findall(r'`([^`\n]+)`', ln):
                if span.split(' ', 1)[0] in CMD_WORDS:
                    out.append((rel, n, span))
    return out


def _path_refs(tree: ast.AST) -> list[tuple[str, int]]:
    """Paths a Python script builds from string literals: Path(x) / "examples" / "shop", os.path.join(x, "a", "b.json"),
    and literals holding a relative path ("examples/run/x.json"). The part after the last non-literal is returned."""
    out: list[tuple[str, int]] = []
    seen: set[int] = set()
    for n in ast.walk(tree):
        parts = None
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and id(n) not in seen:
            flat, x = [], n
            while isinstance(x, ast.BinOp) and isinstance(x.op, ast.Div):
                seen.add(id(x))
                flat.insert(0, x.right)
                x = x.left
            flat.insert(0, x)
            parts = flat
        elif isinstance(n, ast.Call) and getattr(n.func, 'attr', getattr(n.func, 'id', '')) == 'join' and len(n.args) >= 2:
            parts = list(n.args)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and '/' in n.value and ' ' not in n.value \
                and '\n' not in n.value and len(n.value) < 300:
            out.append((n.value, n.lineno))
            continue
        if not parts:
            continue
        tail: list[str] = []
        for x in parts:
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                tail.append(x.value)
            else:
                tail = []
        if tail:
            out.append(('/'.join(s.strip('/') for s in tail if s.strip('/')), n.lineno))
    return out


class UseIndex:
    """Which data files the repository's commands and self-test scripts name, by file or by a directory holding them.
    A reference is resolved against the folder of the file it is written in and each folder above it (a commands.sh in
    <skill>/examples/run/ runs from <skill>/; the repository guide's commands run from the root). A directory counts only
    when the reference names two or more segments of it and it is not a bare fixture parent (examples/)."""

    def __init__(self, root: Path, files: list[str], self_tests: list[str]):
        self.files = set(files)
        self.dirs: set[str] = set()
        for f in files:
            parts = PurePosixPath(f).parts[:-1]
            for i in range(1, len(parts) + 1):
                self.dirs.add('/'.join(parts[:i]))
        self.cmd_files: dict[str, set[str]] = {}
        self.cmd_dirs: dict[str, set[str]] = {}
        self.test_files: dict[str, set[str]] = {}
        self.test_dirs: dict[str, set[str]] = {}
        self.commands = 0
        for origin, line, cmd in command_sources(root, files):
            self.commands += 1
            for t in _shell_tokens(cmd):
                self._add(t, origin, f'{origin}:{line}', self.cmd_files, self.cmd_dirs)
        for st in self_tests:
            try:
                tree = ast.parse((root / st).read_text(encoding='utf-8', errors='replace'))
            except (SyntaxError, ValueError, OSError):
                continue
            for t, _line in _path_refs(tree):
                self._add(t.replace('\\', '/').strip('/'), st, f'{st}::*', self.test_files, self.test_dirs)

    def _add(self, token: str, origin: str, label: str, files: dict, dirs: dict):
        if not token or token.startswith('../') or '/../' in token:
            return
        base = PurePosixPath(origin).parent
        while True:
            cand = (base / token).as_posix() if base.as_posix() not in ('.', '') else token
            cand = cand[2:] if cand.startswith('./') else cand
            if cand in self.files:
                files.setdefault(cand, set()).add(label)
                return
            if cand in self.dirs and token.count('/') >= 1 and PurePosixPath(cand).name not in repo_layout.FIXTURE_PARENTS:
                dirs.setdefault(cand, set()).add(label)
                return
            if base.as_posix() in ('.', ''):
                return
            base = base.parent

    def _hits(self, rel: str, files: dict, dirs: dict) -> list[str]:
        out = list(files.get(rel, []))
        parts = PurePosixPath(rel).parts[:-1]
        for i in range(len(parts), 0, -1):
            out += list(dirs.get('/'.join(parts[:i]), []))
        return out

    def commands_for(self, rel: str) -> list[str]:
        return sorted(set(self._hits(rel, self.cmd_files, self.cmd_dirs)))

    def self_tests_for(self, rel: str) -> list[str]:
        return sorted(set(self._hits(rel, self.test_files, self.test_dirs)))


PATH_TAIL = re.compile(r'([\w.@+-]+)/([\w.@+-]+\.[A-Za-z]\w{0,5})\b')


def named_by_used_data(root: Path, items: list[dict], rounds: int = 3) -> None:
    """A data file that no test or command names, but that a data file in use names by its folder and file name
    (run.json listing exchanges/get-001.json, history.json listing H01/api.xml), is used through it: marked in
    used_by_commands as 'via <file>'. Each used text file is read once for its path tails; only a file in the same data
    tree (or the same folder) as the one naming it counts."""
    def tree(i):
        return data_dir_of(i['path']) or PurePosixPath(i['path']).parent.as_posix()
    read: set[str] = set()
    for _ in range(rounds):
        pending: dict[str, list[dict]] = {}
        for i in items:
            if i['kind'] != 'inline' and not i['loaded_by'] and not i.get('used_by_commands') and not i.get('archive'):
                p = PurePosixPath(i['path'])
                pending.setdefault(f'{p.parent.name}/{p.name}', []).append(i)
        if not pending:
            return
        hit = False
        for src in items:
            if src['path'] in read or not (src['loaded_by'] or src.get('used_by_commands')) or src['status'] != 'scanned' \
                    or src.get('bytes', 0) > 5_000_000:
                continue
            read.add(src['path'])
            try:
                text = (root / src['path']).read_text(encoding='utf-8', errors='replace').replace('\\', '/')
            except OSError:
                continue
            for m in PATH_TAIL.finditer(text):
                for i in pending.get(f'{m.group(1)}/{m.group(2)}', []):
                    if i is not src and not i.get('used_by_commands') and tree(i) == tree(src):
                        i['used_by_commands'] = [f'via {src["path"]}']
                        hit = True
        if not hit:
            return


def fixture_data_dirs(root: Path, files: list[str], projects: list[dict]) -> list[dict]:
    """The fixture trees to read as test data by default: the outermost folder named examples/, fixtures/, testdata/,
    samples/ (repo_layout.FIXTURE_PARENTS) holding data files, outside the test areas the inventory already reads.
    Each records why and the sample projects in it."""
    parents: dict[str, int] = {}
    for f in files:
        parts = PurePosixPath(f).parts[:-1]
        i = next((k for k, seg in enumerate(parts) if seg in repo_layout.FIXTURE_PARENTS), None)
        if i is None:
            continue
        d = '/'.join(parts[:i + 1])
        if PurePosixPath(f).suffix.lower() in DATA_EXT and not test_dirs_of(d + '/x'):
            parents[d] = parents.get(d, 0) + 1
    out = []
    for d, n in sorted(parents.items()):
        projs = [p['dir'] for p in projects if p['dir'].startswith(d + '/')]
        why = (f'a fixture folder ({PurePosixPath(d).name}/) holding {n} data file(s)'
               + (f' and {len(projs)} sample project(s) with their own manifest or app' if projs else ''))
        out.append({'dir': d, 'reason': why, 'data_files': n, 'projects': projs})
    return out


UNLISTED_MIN_FILES = 10     # a directory outside the test areas with this many data files, named by test code, is reported


def unlisted_data_dirs(root: Path, all_files: list[str], py: 'PyTests', js_tests: list[str]) -> list[dict]:
    """Product data trees the tests read but the inventory does not cover: directories outside every test area (and
    every --data-dir) holding UNLISTED_MIN_FILES or more data files, whose name a test-area module mentions as a path
    segment (`Path(...) / "places"`, `"places/*.json"`). Reported, so a scan never reads as covering data it skipped."""
    per: dict[str, list[str]] = {}
    for f in all_files:
        pp = PurePosixPath(f)
        if pp.suffix.lower() in DATA_EXT and not in_test_area(f) and not TOOLING.match(pp.name):
            per.setdefault(pp.parent.as_posix(), []).append(f)
    out = []
    for d, fs in sorted(per.items()):
        if d in ('.', '') or len(fs) < UNLISTED_MIN_FILES:
            continue
        seg = PurePosixPath(d).name
        named = []
        for tp, tree in py.modules.items():
            if in_test_area(tp) and any(seg in x.strip('/').split('/') or d in x for x in strings_in(tree)):
                named.append(tp)
        for t in js_tests:
            view = js_view(root, t)
            if view is not None and any(seg in x.strip('/').split('/') or d in x for x in view[1]):
                named.append(t)
        if named:
            size = 0
            for f in fs:
                try:
                    size += (root / f).stat().st_size
                except OSError:
                    pass
            out.append({'dir': d, 'files': len(fs), 'bytes': size, 'named_in': sorted(named)[:20],
                        'hint': f'pass --data-dir {d} to inventory and scan it (add --max-files-per-dir N to sample it)'})
    return out


def archive_members(p: Path, rel: str, entry: dict) -> list[dict]:
    """Fill the inventory entry of an archive and return one entry per member of a zip (path <archive>!<member>):
    a text member read in memory is `scanned` with its line count, every other member is `not_assessed` with the
    reason (binary, nested archive, encrypted, a limit). The archive itself is `scanned` only when every member that
    could hold text was read whole; an archive in another format (tar, gzip, 7z ...) is not opened and says so."""
    fmt = archive_text.is_archive_name(rel)
    entry['format'] = fmt
    if PurePosixPath(rel).suffix.lower() not in archive_text.OPENED:
        entry.update(status='not_assessed', reason=f'{fmt} archive: not opened (only .zip archives are read), so its '
                                                   'contents were not scanned')
        return []
    res = datafiles.archive_read(p)
    if res['error']:
        entry.update(status='not_assessed', reason=f'zip archive that could not be opened ({res["error"]}): not scanned')
        entry['archive_members'] = {'members': 0, 'read': 0, 'not_read': 0, 'complete': False}
        return []
    listing = archive_text.list_archive(p)
    skipped = {s['name']: s['reason'] for s in res['skipped']}
    out = []
    for m in listing['members']:
        name = m['name']
        if m['kind'] == 'dir':
            continue
        e = {'path': f'{rel}{datafiles.MEMBER_SEP}{name}', 'kind': entry['kind'],
             'format': datafiles.fmt_of(Path(name)) if name in res['by_name'] else (PurePosixPath(name).suffix.lower()[1:] or 'unknown'),
             'bytes': m['size'], 'loaded_by': [], 'status': 'scanned', 'reason': None, 'archive': rel, 'member': name}
        if name in res['by_name']:
            text = res['by_name'][name]
            e['lines'] = text.count('\n') + (0 if text.endswith('\n') or not text else 1)
            cut = next((r for r in res['read'] if r['name'] == name and r['truncated']), None)
            if cut:
                e['note'] = f'only the first {cut["bytes"]} bytes were read (member size limit): the rest is not assessed'
        else:
            e.update(status='not_assessed', reason='archive member not read: ' + skipped.get(
                name, 'past the member or byte limit of one archive'))
        out.append(e)
    n_read = len(res['by_name'])
    entry['archive_members'] = {'members': len(out) + max(0, listing['member_count'] - len(listing['members'])),
                                'read': n_read, 'not_read': len(out) - n_read, 'complete': bool(res['complete'])}
    if not res['complete']:
        why = [f'{s["name"]} ({s["reason"]})' for s in res['skipped'] if 'binary' not in s['reason']]
        cut = [r['name'] + ' (cut at the member size limit)' for r in res['read'] if r['truncated']]
        entry.update(status='not_assessed', reason=f'zip archive partly read: {n_read} member(s) read as text, not read: '
                                                   + '; '.join((why + cut)[:5]) + (' ...' if len(why + cut) > 5 else ''))
    else:
        entry['note'] = (f'zip archive read in memory: {n_read} text member(s) scanned as {rel}!<member>'
                         + (f', {len(out) - n_read} binary member(s) not read' if len(out) > n_read else ''))
    return out


def sample_of(n: int, cap: int) -> set[int]:
    """Indices of an evenly spaced sample of cap items out of n (the first and the spread, deterministic)."""
    return {i * n // cap for i in range(cap)}


def build(root: Path, paths: list[str] | None, markers: dict, data_dirs: list[str] | None = None,
          max_files_per_dir: int = 0, progress: bool = False, fixture_dirs: bool = True) -> dict:
    """max_files_per_dir: above this many data files in one directory, only an evenly spaced sample of that many is
    scanned; the rest stay listed as not_assessed ('not sampled'), and sampled_dirs records the directory. 0 = all.
    progress: print a line to stderr every few seconds, so a long run is never mistaken for a hung one.
    fixture_dirs: also read the fixture trees (examples/, samples/, ... holding data files, repo_layout's fixture
    parents) as data trees, recorded under fixture_dirs; False leaves them out unless named with data_dirs."""
    import time
    DATA_DIRS.clear()
    DATA_DIRS.update(d.replace('\\', '/').strip('/') for d in data_dirs or [] if d.strip('/'))
    _VIEWS.clear()
    all_files = walk(root)
    set_product_dirs(root, all_files)
    projects = repo_layout.fixture_projects(root)
    fdirs = [f for f in fixture_data_dirs(root, all_files, projects)
             if f['dir'] not in DATA_DIRS] if fixture_dirs else []
    DATA_DIRS.update(f['dir'] for f in fdirs)
    SELF_TEST_FILES.clear()
    SELF_TEST_FILES.update(s['file'] for s in detect_conventions.find_self_tests(root, all_files))
    uses = UseIndex(root, all_files, sorted(SELF_TEST_FILES))
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
    kinds = {rel: kind_of(rel) for rel in files}
    kinds = {rel: k for rel, k in kinds.items() if k}
    # a module named like a test (test_paths.py, path_test.py) outside every test area, with no test functions and no
    # self-test, is product code
    for rel in [r for r, k in kinds.items() if k == 'inline' and r.endswith('.py')]:
        if not test_dirs_of(rel) and PurePosixPath(rel).name != 'conftest.py' and not py.tests.get(rel) \
                and rel not in SELF_TEST_FILES:
            del kinds[rel]
    # product data that test code names by its repository path (ROOT / "catalog/features.json"): read by the tests, so
    # inventoried whatever folder it sits in; its folder is named so the rest of it is not taken as covered
    named_by_path: dict[str, list[str]] = {}
    blob = '\n'.join([s for tp, tree in py.modules.items() if in_test_area(tp) for s in strings_in(tree)]
                     + [v[0] for v in (js_view(root, t) for t in js_tests) if v is not None])
    if blob:
        for rel in files:
            pp = PurePosixPath(rel)
            if rel in kinds or '/' not in rel or pp.suffix.lower() not in DATA_EXT or TOOLING.match(pp.name) \
                    or DOC_NAME.match(pp.name) or rel not in blob:
                continue
            if not fixture_dirs and set(pp.parts[:-1]) & set(repo_layout.FIXTURE_PARENTS):
                continue                   # --no-fixture-dirs: sample projects stay out, even those a test names
            if re.search(r'(?<![\w.-])' + re.escape(rel) + r'(?![\w/-])', blob):
                kinds[rel] = 'fixture'
                named_by_path.setdefault(pp.parent.as_posix(), []).append(rel)
    # source code under a --data-dir tree: a data module is listed as a fixture; the rest is recorded, never dropped silently
    module_notes: dict[str, str] = {}
    code_under: dict[str, list[str]] = {}
    for rel in files:
        if rel in kinds or PurePosixPath(rel).suffix.lower() not in CODE_EXT:
            continue
        d = data_dir_of(rel)
        if d is None:
            continue
        n = data_module(root, rel)
        if n:
            kinds[rel] = 'fixture'
            module_notes[rel] = f'a data module written as code ({n} records): scanned as data'
        else:
            code_under.setdefault(d, []).append(rel)
    skipped: dict[str, str] = {}
    sampled: dict[str, dict] = {}
    if max_files_per_dir and max_files_per_dir > 0:
        per_dir: dict[str, list[str]] = {}
        for rel, k in kinds.items():
            if k in ('fixture', 'recording', 'seed', 'snapshot'):
                per_dir.setdefault(PurePosixPath(rel).parent.as_posix(), []).append(rel)
        for d, fs in sorted(per_dir.items()):
            if len(fs) <= max_files_per_dir:
                continue
            keep = sample_of(len(fs), max_files_per_dir)
            why = (f'not sampled: {d}/ holds {len(fs)} data files, over --max-files-per-dir {max_files_per_dir}; an evenly '
                   f'spaced sample of {max_files_per_dir} was scanned')
            for i, rel in enumerate(sorted(fs)):
                if i not in keep:
                    skipped[rel] = why
            sampled[d] = {'files': len(fs), 'scanned': max_files_per_dir, 'how': 'evenly spaced in path order'}
    items = []
    archives: list[dict] = []
    t_start = t_last = time.monotonic()
    for n_done, rel in enumerate(kinds, 1):
        k = kinds[rel]
        if progress and time.monotonic() - t_last >= 5:
            t_last = time.monotonic()
            print(f'inventory: {n_done}/{len(kinds)} sources ({t_last - t_start:.0f} s)', file=sys.stderr, flush=True)
        p = root / rel
        ext = p.suffix.lower()
        size = p.stat().st_size
        entry = {'path': rel, 'kind': k, 'format': datafiles.fmt_of(p) if ext not in ('.snap', '.ambr', '.har') else ext[1:],
                 'bytes': size, 'loaded_by': [], 'status': 'scanned', 'reason': None}
        archive_entries: list[dict] = []
        if rel in skipped:
            entry.update(status='not_assessed', reason=skipped[rel])
        elif ext in ('.xlsx', '.xlsm'):
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
        elif archive_text.is_archive_name(rel):
            # a zip is opened in memory (never extracted): its text members are listed and scanned as <archive>!<member>;
            # any other archive is named as not opened, never passed over as "binary"
            archive_entries = archive_members(p, rel, entry)
        elif ext in BINARY_EXT:
            entry.update(status='not_assessed', reason='binary, compressed or encrypted file', format=ext[1:])
        elif ext in MEDIA_EXT:
            entry.update(status='not_assessed', reason='image, font or media file: its contents (metadata included) are not read',
                         format=ext[1:])
        elif k == 'fixture' and ext not in DATA_EXT and ext not in ('.xls',) and rel not in module_notes:
            # a file under --data-dir in a format the inventory does not know (a save file, a custom binary): listed so the
            # report cannot imply it was read
            entry.update(status='not_assessed', reason=f'unknown format ({ext or "no extension"}): not read', format=ext[1:] or 'unknown')
        else:
            try:
                entry['lines'] = count_lines(p)
            except UnicodeDecodeError:
                entry.update(status='not_assessed', reason='not UTF-8 text')
            except OSError as exc:
                entry.update(status='not_assessed', reason=f'cannot be read: {exc.__class__.__name__}')
        if rel in module_notes:
            entry['note'] = module_notes[rel]
        if rel in anonymised:
            entry['anonymised_by'] = anonymised[rel]
        if k == 'inline' and PurePosixPath(rel).name == 'conftest.py':
            entry['loaded_by'] = sorted(py.requesting(rel, set(py.fixtures.get(rel, {}))))
        elif k == 'inline':
            entry['loaded_by'] = ([py.test_id(rel, fn) for fn in py.tests.get(rel, [])] if rel.endswith('.py') else [f'{rel}::*'])
            if rel in SELF_TEST_FILES:
                entry['loaded_by'].append(f'{rel}::*')
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
                view = js_view(root, t)
                if view is not None and tail.search(view[0]):
                    out.append(f'{t}::*')
            if p.parent.name in TEST_DIRS or p.parent.as_posix() == '.':
                near = re.compile(r'''['"]\.{1,2}/(?:[\w.-]+/)*''' + re.escape(p.stem) + r'''(\.[cm]?[jt]sx?)?['"]''')
                for t in js_tests:
                    if f'{t}::*' in out or PurePosixPath(t).parent != p.parent:
                        continue
                    view = js_view(root, t)
                    if view is not None and near.search(view[0]):
                        out.append(f'{t}::*')
            entry['loaded_by'] = sorted(out)
        elif k == 'factory' and rel.endswith('.py'):
            entry['loaded_by'] = sorted(importers(py, rel) | set(loaded_by(rel, py, js_tests, root, FileRef(rel, roots, name_counts, by_dir.get(PurePosixPath(rel).parent.as_posix())))))
        else:
            entry['loaded_by'] = loaded_by(rel, py, js_tests, root, FileRef(rel, roots, name_counts, by_dir.get(PurePosixPath(rel).parent.as_posix())))
        if k != 'inline':
            # a self-test that builds the file's path (or its sample project's) reads it; a documented command that names
            # it (or a folder holding it) uses it, though no test function does
            st = [t for t in uses.self_tests_for(rel) if t != f'{rel}::*']
            if st:
                entry['loaded_by'] = sorted(set(entry['loaded_by']) | set(st))
            cmds = uses.commands_for(rel)
            if cmds:
                entry['used_by_commands'] = cmds[:5] + ([f'... {len(cmds) - 5} more'] if len(cmds) > 5 else [])
        items.append(entry)
        for m in archive_entries:          # a member is loaded by whatever loads its archive
            m['loaded_by'] = entry['loaded_by']
            if entry.get('used_by_commands'):
                m['used_by_commands'] = entry['used_by_commands']
            items.append(m)
        if entry['status'] == 'not_assessed' and archive_text.is_archive_name(rel) and rel not in skipped:
            archives.append(entry)
    named_by_used_data(root, items)
    tests = sorted({py.test_id(p, fn) for p, fn in py.all_tests()} | {f'{t}::*' for t in js_tests}
                   | {f'{t}::*' for t in SELF_TEST_FILES})
    warnings: list[str] = []
    # a served folder named with --data-dir: its files are the site's assets; only those a test loads are test data
    served: dict[str, dict] = {}
    for d in sorted(x for x in DATA_DIRS if is_served_dir(x)):
        under = [i for i in items if i['kind'] == 'fixture' and data_dir_of(i['path']) == d]
        keep = [i for i in under if i['loaded_by']]
        drop = {id(i) for i in under if not i['loaded_by']}
        items = [i for i in items if id(i) not in drop]
        served[d] = {'files': len(under), 'loaded_by_tests': len(keep),
                     'note': 'a folder the app serves as is: its files are site assets, not test fixtures; only the files a '
                             'test loads are inventoried, and none is called unused'}
        warnings.append(f'{d}/ is a served asset folder (named with --data-dir): its {len(under)} file(s) are the site\'s assets, '
                        f'not test fixtures; {len(keep)} that tests load are inventoried, the rest are not (personal data in '
                        'served files belongs to privacy-flow-auditor)')
    data_code = {}
    for d, fs in sorted(code_under.items()):
        data_code[d] = {'files': len(fs), 'paths': sorted(fs)[:50],
                        'reason': 'source code under --data-dir that is not a data module (no records array): not read as '
                                  'test data; it stays product code, read for clock and locale calls only'}
        warnings.append(f'{d}/ (--data-dir) holds {len(fs)} source file(s) that are not data modules, not read as test data '
                        f'(data_dir_code): {", ".join(sorted(fs)[:3])}{", ..." if len(fs) > 3 else ""}. A module of records '
                        'written as code is listed and scanned; personal data in other product code belongs to '
                        'code-security-auditor and privacy-flow-auditor')
    extra = {}
    if served:
        extra['served_dirs'] = served
    if data_code:
        extra['data_dir_code'] = data_code
    unlisted = unlisted_data_dirs(root, all_files, py, js_tests)
    if unlisted:
        extra['unlisted_data_dirs'] = unlisted
    if named_by_path:
        per_dir: dict[str, int] = {}
        for f in all_files:
            pp = PurePosixPath(f)
            if pp.parent.as_posix() in named_by_path and pp.suffix.lower() in DATA_EXT:
                per_dir[pp.parent.as_posix()] = per_dir.get(pp.parent.as_posix(), 0) + 1
        extra['named_by_path'] = [{'dir': d, 'files': sorted(fs), 'data_files_in_dir': per_dir.get(d, len(fs))}
                                  for d, fs in sorted(named_by_path.items())]
        for e in extra['named_by_path']:
            rest = e['data_files_in_dir'] - len(e['files'])
            warnings.append(f'{e["dir"]}/: {len(e["files"])} data file(s) there that tests read by repository path were '
                            'inventoried (named_by_path)' + (f'; the other {rest} data file(s) in it were NOT: pass '
                                                             f'--data-dir {e["dir"]} to read the folder whole' if rest > 0 else ''))
    if sampled:
        extra['sampled_dirs'] = sampled
    if fdirs:
        extra['fixture_dirs'] = fdirs
        warnings.append(f'{len(fdirs)} fixture folder(s) read as test data by default ('
                        + ', '.join(f['dir'] + '/' for f in fdirs[:5]) + (', ...' if len(fdirs) > 5 else '')
                        + '): sample projects and recorded runs there are test data; their code is listed under '
                          'data_dir_code. Pass --no-fixture-dirs to leave them out')
    if projects:
        extra['fixture_projects'] = projects
    if SELF_TEST_FILES:
        extra['self_tests'] = sorted(SELF_TEST_FILES)
    extra['commands_read'] = uses.commands
    for a in archives:
        warnings.append(f'{a["path"]}: {a["reason"]}. What the archive holds beyond the members listed as read was NOT '
                        'scanned; name it in the limitations')
    if not tests:
        # loud, like language_coverage: an empty inventory is not a clean one
        extra['no_tests'] = True
        warnings.insert(0, NO_TESTS)
    if warnings:
        extra['warnings'] = warnings
    return {'repo': root.name, 'data_dirs': sorted(DATA_DIRS), 'product_dirs': dict(sorted(PRODUCT_DIRS.items())),
            **extra, 'inventory': items, 'tests': tests,
            'summary': {'sources': len(items), 'by_kind': {k: len([i for i in items if i['kind'] == k])
                                                            for k in sorted({i['kind'] for i in items})},
                        'not_assessed': len([i for i in items if i['status'] == 'not_assessed']),
                        # with no tests, "no test loads it" says nothing about a file
                        'unused': [i['path'] for i in items if not i['loaded_by'] and i['kind'] != 'inline'
                                   and not i.get('used_by_commands') and not i.get('archive')] if tests else [],
                        # read by a documented command or script only: used, but by commands, not tests
                        'used_by_commands': [i['path'] for i in items if not i['loaded_by'] and i['kind'] != 'inline'
                                             and i.get('used_by_commands') and not i.get('archive')]}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--paths', nargs='*')
    ap.add_argument('--markers', type=Path)
    ap.add_argument('--data-dir', action='append', default=[], metavar='DIR',
                    help='a repo-relative tree that holds test data under a name the inventory does not know (repeatable)')
    ap.add_argument('--max-files-per-dir', type=int, default=0, metavar='N',
                    help='scan only an evenly spaced sample of N data files in any directory holding more (the rest are '
                         'listed as not_assessed, "not sampled"); 0, the default, scans every file')
    ap.add_argument('--no-fixture-dirs', action='store_true',
                    help='do not read fixture folders (examples/, samples/, ... with data files) as test data by default')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    markers = json.loads(args.markers.read_text(encoding='utf-8')) if args.markers else {}
    res = build(args.repo.resolve(), args.paths, markers, args.data_dir, args.max_files_per_dir, progress=True,
                fixture_dirs=not args.no_fixture_dirs)
    for u in res.get('unlisted_data_dirs', []):
        print(f"WARNING: {u['dir']}/ holds {u['files']} data files ({u['bytes'] / 1e6:.1f} MB) that test code names "
              f"({', '.join(u['named_in'][:3])}{', ...' if len(u['named_in']) > 3 else ''}) but the inventory does not cover: "
              f"its personal data, time bombs and duplicates were NOT scanned. {u['hint']}.", file=sys.stderr)
    for w in res.get('warnings', []):
        print(f'WARNING: {w}', file=sys.stderr)
    for d, sm in res.get('sampled_dirs', {}).items():
        print(f"NOTE: {d}/ sampled: {sm['scanned']} of {sm['files']} data files scanned (the rest are not_assessed)",
              file=sys.stderr)
    text = json.dumps(res, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
        s = res['summary']
        print(f'{s["sources"]} data sources ({", ".join(f"{v} {k}" for k, v in s["by_kind"].items())}), '
              f'{s["not_assessed"]} not assessed, {len(s["unused"])} unused, {len(s["used_by_commands"])} used by commands '
              f'only -> {args.out}')
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
