#!/usr/bin/env python3
"""Build a file-level import graph of a repository and find which tests reach a file (stdlib only).

    python import_graph.py <repo> --out graph.json
    python import_graph.py <repo> --reach api/harbourline_api/pricing.py

Python is parsed with `ast` (absolute and relative imports, `importlib.import_module` with a
literal); pytest test files also depend on every `conftest.py` above them. JavaScript and
TypeScript are parsed with patterns: `import`/`export ... from`, side-effect imports, `require`,
`import()` and `import.meta.glob`/`require.context` with literal arguments, relative paths,
workspace package names and tsconfig `paths`. Imports of CSS and other assets are edges too.

Calls that load code by a computed name (`importlib.import_module(f"pkg.{name}")`, `__import__`,
`pkgutil.iter_modules`, entry points, `import(variable)`, `require(variable)`) are recorded as
dynamic sites, with the files they could load when the name has a literal prefix. Go is read
from text: each go.mod's module path maps an import path to a package directory, and a file
depends on every non-test .go file of each package it imports; a `_test.go` file also depends
on its own package's files, and `//go:embed` patterns are edges to the embedded files. Files in
languages with no parser here (Java, Kotlin, C#, Swift, Dart, Rust, Ruby, PHP; Go without a
go.mod) are listed under `languages` as `no_parser`; nothing is guessed for them. graph.json
carries `language_coverage` (lang_census.py, vendored and embedded third-party trees left out),
and the console warns when the dominant language has no parser here.

Server-rendered templates: a Python call that renders a template by a literal name
(`render_template("x.html")`, `TemplateResponse`, `get_template`, Django's `render(request,
"x.html")`, `render_to_string`, a `template_name = "x.html"` attribute) is an edge from the module
to that file under a `templates/` directory, recorded with its line under `renders`. Jinja and
Django templates are read for `{% extends|include|import|from "x" %}` (edges to other templates)
and for static files they link (`url_for('static', filename='x')`, `{% static 'x' %}`, a literal
`/static/x`), so a partial or a stylesheet reaches the tests that import the views rendering it.

Rust: when the repository (or a top-level folder) holds a Cargo.toml, cargo_project.py reads the crates,
their targets and their tests. A file holding #[test] functions (in a #[cfg(test)] module or a tests/*.rs
target) is a test file; helper binaries under tests/fixtures and benches are not. A file's build is the
targets whose module tree (`mod x;`) holds it: a library file reaches its crate's lib, bin and test targets
and those of every crate that depends on it by path (transitively); a binary's module reaches that binary's
unit tests only. Each test file of a reached target gets an edge to the file (`via` names the target).

Python test files that put a folder on sys.path (`sys.path.insert(0, str(Path(__file__).parent.parent /
"scripts"))`, `os.path.join(os.path.dirname(__file__), "..", "scripts")`, also in a conftest.py above them)
and then import a module by its bare name reach that folder's module. When the folder cannot be worked out
statically, the bare import of a module that exists in the repository is recorded under `unresolved` with
its candidate files, never dropped.

Not followed: a package's `__init__.py` when a submodule is imported (Python runs it, but
following it makes every module depend on everything the package imports), and test-runner
setup files named only in configuration (Vitest/Jest `setupFiles`, `globalSetup`).
"""
from __future__ import annotations
import argparse, ast, fnmatch, json, os, re, sys
from collections import deque
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions  # noqa: E402

READS = 0   # source files read by graphs in this process (the self-test's timing check counts them)
STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | {'__future__'}
RUST_CONFIG = {'Cargo.toml', 'Cargo.lock', 'rust-toolchain', 'rust-toolchain.toml', '.cargo/config', '.cargo/config.toml'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte', '.java', '.kt', '.kts',
            '.cs', '.swift', '.dart', '.rb', '.php', '.scala', '.m'}

KINDS = json.loads((HERE.parent / 'data' / 'file_kinds.json').read_text(encoding='utf-8'))
SKIP_DIRS = detect_conventions.SKIP_DIRS | {'.git', '.hg', '.svn', '__snapshots__'}
PY_EXT = {'.py'}
JS_EXT = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte']
JS_RESOLVE = JS_EXT + ['.json', '.css', '.scss', '.sass', '.less', '.html', '.svg']
NO_PARSER = {'.go': 'go', '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin', '.cs': 'csharp', '.swift': 'swift',
             '.dart': 'dart', '.rs': 'rust', '.rb': 'ruby', '.php': 'php', '.scala': 'scala', '.m': 'objective-c'}
DYNAMIC_ANY = {'iter_modules', 'walk_packages', 'entry_points', 'load_entry_point', 'iter_entry_points'}

GO_IMPORT_BLOCK = re.compile(r'^[ \t]*import\s*\((.*?)\)', re.M | re.S)
GO_IMPORT_ONE = re.compile(r'^[ \t]*import\s+(?:[\w.]+\s+)?"([^"\n]+)"', re.M)
GO_SPEC = re.compile(r'(?:[\w.]+\s+)?"([^"\n]+)"')
GO_EMBED = re.compile(r'^[ \t]*//go:embed\s+(.+)$', re.M)
GO_MODULE = re.compile(r'^\s*module\s+"?([^\s"]+)"?', re.M)
JS_STATIC = re.compile(r'''(?:^|[^\w$.])(?:import|export)\s+(?:type\s+)?(?:[\w$*{}\s,]+?\s+from\s+)?["']([^"'\n]+)["']''', re.M)
JS_REQUIRE = re.compile(r'''(?:^|[^\w$.])require\s*\(\s*["']([^"'\n]+)["']\s*\)''')
JS_DYN_IMPORT = re.compile(r'''(?:^|[^\w$.])import\s*\(\s*(?:/\*.*?\*/\s*)?["']([^"'\n]+)["']\s*\)''')
JS_DYN_ANY = re.compile(r'''(?:^|[^\w$.])(import|require)\s*\(\s*(?:/\*.*?\*/\s*)?(?!["'\s)])([^)\n]{0,120})''')
JS_GLOB = re.compile(r'''import\.meta\.glob(?:Eager)?\s*\(\s*(\[[^\]]*\]|["'][^"']+["'])''')
JS_CONTEXT = re.compile(r'''require\.context\s*\(\s*["']([^"']+)["']\s*(?:,\s*(true|false))?''')
# server-rendered templates
TEMPLATE_CALLS = {'render_template', 'render_template_string', 'get_template', 'select_template', 'TemplateResponse',
                  'render', 'render_to_string', 'render_to_response', 'stream_template'}
TEMPLATE_EXT = {'.html', '.htm', '.jinja', '.jinja2', '.j2', '.njk', '.xml', '.txt', '.tmpl', '.tpl'}
TEMPLATE_DIRS = {'templates', 'template'}
STATIC_DIRS = {'static', 'public', 'assets'}
TPL_REF = re.compile(r'''\{%-?\s*(extends|include|import|from|embed)\s+["']([^"']+)["']''')
TPL_STATIC = re.compile(r'''url_for\(\s*["']static["']\s*,\s*filename\s*=\s*["']([^"']+)["']|'''
                        r'''\{%-?\s*static\s+["']([^"']+)["']|["'(=]\s*/static/([^"')?#\s{}]+)''')


def match(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    for pat in patterns:
        if '/' in pat:
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch('/' + path, pat):
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
    return False


def test_level(path: str) -> str | None:
    """unit | integration | e2e | visual for a test file, None for anything else."""
    if not detect_conventions.is_test_file(path):
        return None
    if match(path, KINDS['visual_tests']):
        return 'visual'
    if match(path, KINDS['e2e_tests']):
        return 'e2e'
    return detect_conventions.level_of(path, set())


def family(path: str) -> str | None:
    """The test family a changed file or a test belongs to, for widening: 'rust' (.rs, and the workspace's Cargo
    files), 'go' (.go), 'code' (other source languages), None for anything else (data, styles, config), which can be
    read by any language and so widens to every test of its scope."""
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    if ext == '.rs' or path in RUST_CONFIG or p.name in ('Cargo.toml', 'Cargo.lock'):
        return 'rust'
    if ext == '.go':
        return 'go'
    return 'code' if ext in CODE_EXT else None


def same_family(path: str, tests: list[str]) -> list[str]:
    """The tests a change to path can reach by language: a Rust file only Rust tests, a Go file only Go tests, any
    other source file every test but Rust and Go ones (they cannot import it); a file of no language every test."""
    fam = family(path)
    if fam in ('rust', 'go'):
        return [t for t in tests if family(t) == fam]
    if fam == 'code':
        return [t for t in tests if family(t) not in ('rust', 'go')]
    return list(tests)


def is_rust_config(path: str) -> bool:
    """A repository-level Cargo file: the workspace manifest, the lockfile, the toolchain or cargo configuration."""
    return path in RUST_CONFIG


def strip_js_comments(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif text.startswith('/*', i) and not text.startswith('/*@vite', i):
            j = text.find('*/', i + 2)
            seg = text[i:(n if j < 0 else j + 2)]
            out.append('\n' * seg.count('\n'))
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def read_jsonc(p: Path) -> dict:
    try:
        text = strip_js_comments(p.read_text(encoding='utf-8', errors='replace'))
        d = json.loads(re.sub(r',(\s*[}\]])', r'\1', text))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


BUILD_DIRS = ('dist', 'build', 'lib', 'out', 'esm', 'cjs')


def build_output(pkg_dir: Path) -> str | None:
    """The build directory a package's published entry points at (package.json main/module/exports), or None.
    Tests elsewhere that import the package by name load that output, not its sources."""
    pj = read_jsonc(pkg_dir / 'package.json')
    targets: list[str] = []

    def collect(v) -> None:
        if isinstance(v, str):
            targets.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, list):
            for x in v:
                collect(x)
    for k in ('main', 'module', 'exports'):
        collect(pj.get(k))
    for t in targets:
        first = PurePosixPath(t.lstrip('./')).parts[:1]
        if first and first[0] in BUILD_DIRS:
            return first[0]
    return None


def eval_path(node, here: str, names: dict, depth: int = 0) -> str | None:
    """The repository-relative path a path expression names, for the sys.path idioms of test files: __file__,
    Path()/str()/os.fspath()/resolve()/absolute(), .parent, .parents[n], os.path.dirname/abspath/realpath/normpath/
    join, `/` with literal parts, and names assigned earlier in the module. None when it cannot be worked out (an
    environment variable, a computed value) or it leaves the repository."""
    if depth > 12 or node is None:
        return None
    rec = lambda n: eval_path(n, here, names, depth + 1)  # noqa: E731
    if isinstance(node, ast.Name):
        if node.id == '__file__':
            return here
        return rec(names.get(node.id)) if node.id in names else None
    if isinstance(node, ast.Attribute):
        if node.attr == 'parent':
            v = rec(node.value)
            return None if v is None else _norm(f'{v}/..')
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == 'parents':
        idx = node.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
            v = rec(node.value.value)
            return None if v is None else _norm(v + '/..' * (idx.value + 1))
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = rec(node.left)
        right = node.right.value if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str) else None
        return None if left is None or right is None else _norm(f'{left}/{right}')
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if name in ('resolve', 'absolute') and isinstance(f, ast.Attribute) and not node.args:
            return rec(f.value)
        if name in ('Path', 'PurePath', 'PosixPath', 'WindowsPath', 'PurePosixPath', 'str', 'fspath', 'abspath',
                    'realpath', 'normpath') and len(node.args) == 1:
            return rec(node.args[0])
        if name == 'dirname' and len(node.args) == 1:
            v = rec(node.args[0])
            return None if v is None else _norm(f'{v}/..')
        if name == 'join' and node.args:
            v = rec(node.args[0])
            rest = [a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if v is None or len(rest) != len(node.args) - 1:
                return None
            return _norm('/'.join([v, *rest]))
    return None


CFG_TOKEN = re.compile(r'\s*(?:(all|any|not)\s*\(|(\))|(,)|([A-Za-z_][\w]*)\s*(?:=\s*"([^"]*)")?)')


def cfg_gates(expr: str, enabled: set[str]) -> list[str]:
    """The cargo features a `#[cfg(...)]` expression needs beyond `enabled` when compiling tests: [] when it holds
    with `test` set and only the enabled features (`any(feature = "x", test)` does), else the features it names.
    Other predicates (target_os, unix, debug_assertions) are taken as satisfiable."""
    pos = 0
    names: list[str] = []

    def parse():
        nonlocal pos
        m = CFG_TOKEN.match(expr, pos)
        if not m:
            raise ValueError(expr)
        pos = m.end()
        if m.group(1):
            op, vals = m.group(1), []
            while True:
                m2 = CFG_TOKEN.match(expr, pos)
                if m2 and m2.group(2):
                    pos = m2.end()
                    break
                if m2 and m2.group(3):
                    pos = m2.end()
                    continue
                vals.append(parse())
            return all(vals) if op == 'all' else any(vals) if op == 'any' else not (vals[0] if vals else False)
        if m.group(4):
            if m.group(4) == 'feature' and m.group(5) is not None:
                names.append(m.group(5))
                return m.group(5) in enabled
            return True             # test, unix, target_os = "...": can hold when the tests are compiled
        raise ValueError(expr)
    try:
        ok = parse()
    except (ValueError, IndexError, RecursionError):
        return sorted({f for f in re.findall(r'feature\s*=\s*"([^"]+)"', expr) if f not in enabled})
    return [] if ok else sorted({f for f in names if f not in enabled})


def _norm(p: str) -> str | None:
    out = os.path.normpath(p).replace(os.sep, '/')
    return None if out == '..' or out.startswith('../') else ('.' if out in ('', '.') else out)


class Graph:
    def __init__(self, root: Path, reuse: 'Graph | None' = None, changed: set[str] | None = None,
                 reuse_packages: bool = False, cargo_metadata: dict | None = None):
        """reuse: a graph of another tree that differs from this one only in the `changed` paths; its file reads and
        parsed ASTs are used for every other file, and with reuse_packages its package detection and Cargo model."""
        self.root = root
        self.files: dict[str, dict] = {}
        self.languages: dict[str, str] = {}
        self._fixtures: dict[str, tuple[set[str], bool]] = {}
        self.packages: list[dict] = []
        self.product_roots: set[str] = set()
        self._stripped: dict[str, str | None] = {}
        self.notes: list[str] = []
        self._text: dict[str, str | None] = {}
        self._ast: dict[str, object] = {}
        self._syspath: dict[str, tuple[list[str], list[int]]] = {}
        self._parsed: dict[str, dict] = {}
        self.reuse, self.changed, self.reuse_packages = reuse, set(changed or ()), reuse_packages
        self.cargo_metadata = cargo_metadata
        self.python_runners: dict[str, dict] = {}
        self.ci_gates: list[dict] = []
        self.cargo_models: list[dict] = []
        self.rust_tests: dict[str, list[dict]] = {}
        self.rust_crates: dict[str, dict] = {}

    # ------------------------------------------------------------ reads (each file once; shared with `reuse`)
    def text(self, path: str) -> str | None:
        global READS
        if path not in self._text:
            other = self.reuse
            if other is not None and path not in self.changed and path in other._text:
                self._text[path] = other._text[path]
            else:
                READS += 1
                try:
                    self._text[path] = (self.root / path).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    self._text[path] = None
        return self._text[path]

    def tree(self, path: str):
        """The parsed module (ast.Module), a SyntaxError/ValueError when it does not parse, None when unreadable."""
        if path not in self._ast:
            other = self.reuse
            if other is not None and path not in self.changed and path in other._ast:
                self._ast[path] = other._ast[path]
            else:
                text = self.text(path)
                try:
                    self._ast[path] = ast.parse(text) if text is not None else None
                except (SyntaxError, ValueError) as exc:
                    self._ast[path] = exc
        return self._ast[path]

    PARSED_KEYS = ('imports', 'unresolved', 'dynamic', 'renders', 'parse_error')

    def parsed(self, path: str, entry: dict, handler) -> None:
        """Run a language handler on a file, or copy its result from `reuse` when the file is unchanged and nothing
        it resolves against can differ (same layout and configuration, no conftest.py changed)."""
        o = self.reuse
        if o is not None and self.reuse_packages and path not in self.changed and path in o._parsed and \
                not any(PurePosixPath(c).name == 'conftest.py' for c in self.changed):
            for k, v in o._parsed[path].items():
                entry[k] = [dict(x) for x in v] if isinstance(v, list) else v
        else:
            handler(path, entry)
        self._parsed[path] = {k: ([dict(x) for x in entry[k]] if isinstance(entry[k], list) else entry[k])
                              for k in self.PARSED_KEYS if k in entry}

    # ------------------------------------------------------------ discovery
    def walk(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info'))
            for f in sorted(filenames):
                p = Path(dirpath) / f
                out.append(p.relative_to(self.root).as_posix())
        return out

    def find_packages(self, paths: list[str]) -> None:
        if self.reuse is not None and self.reuse_packages:
            self.packages = [dict(p) for p in self.reuse.packages]
            self.python_runners, self.ci_gates = dict(self.reuse.python_runners), list(self.reuse.ci_gates)
            return
        det = detect_conventions.Detector(self.root, 50_000)
        res = det.run(None)
        self.ci_gates = res.get('ci_gates') or []
        for a in res['areas']:
            if a.get('python_runner'):
                self.python_runners[a['root']] = a['python_runner']
            frameworks = sorted({fw for cat in a.get('frameworks', {}).values() for fw in cat})
            manifests = a.get('manifests', [])
            name = a['root']
            pj = self.root / a['root'] / 'package.json'
            if pj.is_file():
                name = read_jsonc(pj).get('name') or name
            self.packages.append({'root': a['root'], 'name': name, 'manifests': manifests, 'frameworks': frameworks,
                                  'languages': sorted(a.get('languages', {}))})
        if not any(p['root'] == '.' for p in self.packages):
            self.packages.append({'root': '.', 'name': '.', 'manifests': [], 'frameworks': [], 'languages': []})
        self.packages.sort(key=lambda p: (-len(p['root']), p['root']))

    def test_path(self, path: str) -> str:
        """The path to judge test-ness by (the shared rule, detect_conventions.strip_product_dirs): inside a
        product package named like a test directory (a workspace package called e2e with its own manifest and
        src/), the path relative to that package; otherwise the path itself."""
        parent = str(PurePosixPath(path).parent)
        if parent not in self._stripped:
            rel = detect_conventions.strip_product_dirs(path, self.root)
            self._stripped[parent] = None if rel == path else path[:len(path) - len(rel) - 1]
        cut = self._stripped[parent]
        if cut is None:
            return path
        self.product_roots.add(cut)
        return path[len(cut) + 1:]

    def package_of(self, path: str) -> str:
        for p in self.packages:
            if p['root'] == '.' or path == p['root'] or path.startswith(p['root'] + '/'):
                return p['root']
        return '.'

    # ------------------------------------------------------------ build
    def build(self) -> 'Graph':
        paths = self.walk()
        self.find_packages(paths)
        self.py_index: dict[str, list[str]] = {}
        self.py_module: dict[str, str] = {}
        self.py_names: dict[str, list[str]] = {}   # bare module name -> files (for imports through sys.path)
        py_roots = sorted({p['root'] for p in self.packages} | {'.'})
        for path in paths:
            if path.endswith('.py'):
                pp = PurePosixPath(path)
                bare = pp.parent.name if pp.name == '__init__.py' else pp.stem
                if bare.isidentifier():
                    self.py_names.setdefault(bare, []).append(path)
                for r in py_roots:
                    for base in ([r, f'{r}/src'] if r != '.' else ['.', 'src']):
                        prefix = '' if base == '.' else base + '/'
                        if path.startswith(prefix):
                            mod = path[len(prefix):-3].replace('/', '.')
                            if mod.endswith('.__init__'):
                                mod = mod[:-9]
                            elif mod == '__init__':
                                continue
                            if all(part.isidentifier() for part in mod.split('.')):
                                self.py_index.setdefault(mod, []).append(path)
        for mod, files in self.py_index.items():       # module_of(): the first module (in index order) holding a file
            for f in files:
                self.py_module.setdefault(f, mod)
        self.js_aliases = self.tsconfig_aliases(paths)
        self.workspace_names = {p['name']: p['root'] for p in self.packages if p['name'] != p['root']}
        self.all_paths = set(paths)
        # templates and static files by the name views and templates use for them (relative to their directory)
        self.template_index: dict[str, list[str]] = {}
        self.static_index: dict[str, list[str]] = {}
        for path in paths:
            parts = PurePosixPath(path).parts
            for i, part in enumerate(parts[:-1]):
                if part in TEMPLATE_DIRS:
                    self.template_index.setdefault('/'.join(parts[i + 1:]), []).append(path)
                elif part in STATIC_DIRS:
                    self.static_index.setdefault('/'.join(parts[i + 1:]), []).append(path)
        # Go: module paths from every go.mod (nested modules too), and each package directory's files
        self.go_mods: dict[str, str] = {}
        for path in paths:
            if PurePosixPath(path).name == 'go.mod':
                m = GO_MODULE.search(self.text(path) or '')
                if m:
                    self.go_mods[m.group(1)] = str(PurePosixPath(path).parent)
        self.go_src: dict[str, list[str]] = {}
        for path in paths:
            if path.endswith('.go') and not path.endswith('_test.go'):
                self.go_src.setdefault(str(PurePosixPath(path).parent), []).append(path)
        # Rust: the Cargo model (crates, targets, tests) of each top-level Cargo project
        rust_files = self.read_cargo(paths)
        for path in paths:
            ext = PurePosixPath(path).suffix.lower()
            entry = {'package': self.package_of(path), 'lang': None, 'test_level': test_level(self.test_path(path)), 'imports': [],
                     'unresolved': [], 'dynamic': []}
            if ext in PY_EXT:
                entry['lang'] = 'python'
                self.parsed(path, entry, self.python)
                self.languages['python'] = 'parsed'
            elif ext in JS_EXT:
                entry['lang'] = 'typescript' if ext in ('.ts', '.tsx', '.mts', '.cts') else 'javascript'
                self.parsed(path, entry, self.javascript)
                self.languages[entry['lang']] = 'parsed'
            elif ext == '.go' and self.go_mods:
                entry['lang'] = 'go'
                self.parsed(path, entry, self.go)
                self.languages['go'] = 'parsed'
            elif ext == '.rs' and path in rust_files:
                entry['lang'] = 'rust'
                entry.update(rust_files[path])
                self.languages['rust'] = 'parsed'
            elif ext in NO_PARSER:
                entry['lang'] = NO_PARSER[ext]
                self.languages.setdefault(NO_PARSER[ext], 'no_parser')
            elif ext in TEMPLATE_EXT and self.is_template(path):
                self.parsed(path, entry, self.template)
            self.files[path] = entry
        if self.product_roots:
            self.notes.append('Product package(s) named like a test directory: ' + ', '.join(sorted(self.product_roots)) +
                              '. Their files are classified by their path inside the package, so only their own test '
                              'files are tests.')
        for path, entry in self.files.items():
            if entry['lang'] == 'python' and (entry['test_level'] or PurePosixPath(path).name == 'conftest.py'):
                self.conftests(path, entry)
        self.rust_edges()
        for entry in self.files.values():
            for d in entry['dynamic']:
                d['targets'] = self.dynamic_targets(d, entry)
        self.reverse = {}
        for path, entry in self.files.items():
            for imp in entry['imports']:
                self.reverse.setdefault(imp['path'], []).append((path, imp['via']))
        return self

    # ------------------------------------------------------------ Rust (cargo_project.py)
    def read_cargo(self, paths: list[str]) -> dict[str, dict]:
        """Read every top-level Cargo project (a Cargo.toml with none above it) with cargo_project.py and return
        {file: {test_level, rust_role, crate, inline_tests?}} for its .rs files. A file is a test file when the scan
        found a #[test] function in it. Roles: 'lib', 'bin', 'build', 'test_target' (in a tests/*.rs target),
        'bench', 'example', 'fixture_bin' (a helper binary under tests/), 'unreached' (in no target)."""
        self._rust_reach: dict[str, list[tuple[str, str, str]]] = {}
        self._rust_files: dict[str, dict] = {}
        roots = sorted({str(PurePosixPath(p).parent) for p in paths if PurePosixPath(p).name == 'Cargo.toml'},
                       key=lambda d: (0 if d == '.' else d.count('/') + 1, d))
        tops: list[str] = []
        for d in roots:
            if not any(t == '.' or d == t or d.startswith(t + '/') for t in tops):
                tops.append(d)
        if not tops:
            return {}
        if self.reuse is not None and self.reuse_packages and not any(
                p.endswith('.rs') or PurePosixPath(p).name in ('Cargo.toml', 'Cargo.lock') for p in self.changed):
            o = self.reuse
            self.cargo_models, self.rust_crates, self.rust_tests = o.cargo_models, o.rust_crates, o.rust_tests
            self._rust_reach, self._rust_files = o._rust_reach, o._rust_files
            return dict(o._rust_files)
        import cargo_project
        for top in tops:
            m = cargo_project.read_cargo(self.root / top, self.cargo_metadata if top == '.' else None)
            pre = '' if top == '.' else top + '/'
            for c in m['crates']:
                c['dir'] = top if c['dir'] == '.' else pre + c['dir']
                c['manifest'] = pre + c['manifest']
                for t in c['targets']:
                    if t.get('path'):
                        t['path'] = pre + t['path']
                for x in c['fixture_bins'] + c['tests']:
                    x['file'] = pre + x['file']
                c['unreached_files'] = [pre + f for f in c['unreached_files']]
                c['root'] = top
                self.rust_crates[c['name']] = c
            self.cargo_models.append({'root': top, 'workspace': m['workspace'], 'notes': m['notes']})
        # the targets each file belongs to: the module tree (`mod x;`) of each target's root file
        own: dict[str, list[tuple[str, str, str]]] = {}
        for c in self.rust_crates.values():
            cdir = self.root if c['dir'] == '.' else self.root / c['dir']
            pre = '' if c['dir'] == '.' else c['dir'] + '/'
            cache: dict = {}
            for t in c['targets']:
                if not t.get('path') or not t['path'].startswith(pre):
                    continue
                local = t['path'][len(pre):]
                try:
                    tree = cargo_project._module_walk(cdir, local, cache)
                except Exception:  # noqa: BLE001 - a helper of the shared reader; fall back to the target's root file
                    tree = {local: ''}
                for f in tree:
                    own.setdefault(pre + f, []).append((c['name'], t['name'], t['kind']))
        # the crates that depend on each crate by path, transitively (they link its library)
        dependents: dict[str, set[str]] = {n: set() for n in self.rust_crates}
        for c in self.rust_crates.values():
            for d in c['path_deps']:
                if d['name'] in dependents and d['name'] != c['name']:
                    dependents[d['name']].add(c['name'])
        closure: dict[str, list[str]] = {}
        for n in self.rust_crates:
            seen, todo = set(), [n]
            while todo:
                for x in dependents.get(todo.pop(), ()):
                    if x not in seen and x != n:
                        seen.add(x)
                        todo.append(x)
            closure[n] = sorted(seen)
        for c in self.rust_crates.values():
            default = set(c['features'].get('default') or [])
            # a test is gated by the cfg of its function and modules, including the `mod x;` declarations in other
            # files; `any(feature = "x", test)` does not gate it (cfg_gates)
            decl: dict[str, list[str]] = {}
            for md in c['modules']:
                decl.setdefault(md['module'], []).extend(md['cfg'])
            for t in c['tests']:
                parts = t['module'].split('::') if t.get('module') else []
                exprs = list(t.get('cfg') or [])
                for i in range(1, len(parts) + 1):
                    exprs += decl.get('::'.join(parts[:i]), [])
                t['features'] = sorted({f for e in exprs for f in cfg_gates(e, default)})
                if t.get('id'):
                    self.rust_tests.setdefault(t['file'], []).append({
                        'crate': c['name'], 'package': c['dir'], 'root': c['root'], 'target': t['target'],
                        'target_kind': t['target_kind'], 'id': t['id'], 'name': t['name'], 'ignored': t['ignored'],
                        'features': [f for f in t['features'] if f not in default]})
        fixture = {x['file'] for c in self.rust_crates.values() for x in c['fixture_bins']}
        by_depth = sorted(self.rust_crates.values(), key=lambda c: -len(c['dir']))
        out: dict[str, dict] = {}
        for path in paths:
            if not path.endswith('.rs'):
                continue
            crate = next((c for c in by_depth if c['dir'] == '.' or path.startswith(c['dir'] + '/')), None)
            if crate is None:
                continue
            mine = [x for x in own.get(path, []) if x[0] == crate['name']]
            kinds = {k for _c, _t, k in mine}
            role = ('lib' if 'lib' in kinds else 'build' if 'build' in kinds else 'bin' if 'bin' in kinds else
                    'test_target' if 'test' in kinds else 'bench' if 'bench' in kinds else 'example' if 'example' in kinds
                    else 'fixture_bin' if path in fixture else 'unreached')
            if role in ('lib', 'build'):
                reach = [(name, t['name'], t['kind']) for name in [crate['name'], *closure[crate['name']]]
                         for t in self.rust_crates[name]['targets'] if t['kind'] in ('lib', 'bin', 'test')]
            else:
                reach = [x for x in mine if x[2] in ('lib', 'bin', 'test')]
            self._rust_reach[path] = reach
            tests = self.rust_tests.get(path, [])
            level = None
            if tests:
                level = 'unit' if all(t['target_kind'] in ('lib', 'bin') for t in tests) else 'integration'
            out[path] = {'test_level': level, 'rust_role': role, 'crate': crate['name'],
                         **({'inline_tests': True} if level == 'unit' else {})}
        self._rust_files = out
        return out

    def rust_edges(self) -> None:
        """Each test file of a target whose build includes a .rs file gets an edge to it (`via` names the target)."""
        if not self.rust_crates:
            return
        files_of: dict[tuple[str, str], list[str]] = {}
        for f, ts in sorted(self.rust_tests.items()):
            for t in ts:
                key = (t['crate'], t['target'])
                if f not in files_of.setdefault(key, []):
                    files_of[key].append(f)
        for path, reach in self._rust_reach.items():
            seen: set[str] = set()
            for crate, target, kind in reach:
                for tf in files_of.get((crate, target), []):
                    if tf != path and tf not in seen and tf in self.files:
                        seen.add(tf)
                        self.files[tf]['imports'].append({'path': path, 'via': f'cargo {kind} target {crate}/{target}',
                                                          'line': 0})

    def is_template(self, path: str) -> bool:
        return any(part in TEMPLATE_DIRS for part in PurePosixPath(path).parts[:-1])

    def resolve_named(self, name: str, near: str, index: dict[str, list[str]]) -> list[str]:
        """Files a template or static name refers to (relative to a templates/ or static/ directory): the ones in
        the referring file's package when there are any, else every match."""
        found = index.get(name.lstrip('/'), [])
        pkg = self.package_of(near)
        return [f for f in found if self.package_of(f) == pkg] or found

    def template(self, path: str, entry: dict) -> None:
        """A Jinja or Django template's edges: the templates it extends, includes or imports, and the static files
        it links."""
        text = self.text(path)
        if text is None:
            return
        line_of = lambda pos: text.count('\n', 0, pos) + 1  # noqa: E731

        def add(targets: list[str], via: str, pos: int) -> None:
            for t in targets:
                if t != path and not any(i['path'] == t for i in entry['imports']):
                    entry['imports'].append({'path': t, 'via': via, 'line': line_of(pos)})
        for m in TPL_REF.finditer(text):
            add(self.resolve_named(m.group(2), path, self.template_index), m.group(1), m.start())
        for m in TPL_STATIC.finditer(text):
            name = m.group(1) or m.group(2) or m.group(3)
            add(self.resolve_named(name, path, self.static_index), 'static', m.start())

    def module_of(self, path: str) -> str | None:
        return self.py_module.get(path)

    def syspath_dirs(self, path: str) -> tuple[list[str], list[int]]:
        """(repo-relative folders a Python file puts on sys.path, lines of the sys.path changes that could not be
        worked out), the file's own and those of every conftest.py above it (pytest imports them first)."""
        if path in self._syspath:
            return self._syspath[path]
        dirs: list[str] = []
        unknown: list[int] = []
        text = self.text(path) or ''
        tree = self.tree(path) if ('sys.path' in text or 'addsitedir' in text) else None
        if isinstance(tree, ast.Module):
            names: dict[str, ast.AST] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    names.setdefault(node.targets[0].id, node.value)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                f = node.func
                arg = None
                if f.attr in ('insert', 'append') and isinstance(f.value, ast.Attribute) and f.value.attr == 'path' and \
                        isinstance(f.value.value, ast.Name) and f.value.value.id == 'sys':
                    arg = node.args[-1] if node.args else None
                elif f.attr == 'addsitedir' and node.args:
                    arg = node.args[0]
                else:
                    continue
                d = eval_path(arg, path, names)
                if d is not None and d != path:
                    if d not in dirs:
                        dirs.append(d)
                else:
                    unknown.append(node.lineno)
        if PurePosixPath(path).name != 'conftest.py':
            d = PurePosixPath(path).parent
            while True:
                c = 'conftest.py' if str(d) in ('.', '') else (d / 'conftest.py').as_posix()
                if c in self.all_paths:
                    cd, cu = self.syspath_dirs(c)
                    dirs += [x for x in cd if x not in dirs]
                    unknown += cu
                if str(d) in ('.', ''):
                    break
                d = d.parent
        self._syspath[path] = (dirs, unknown)
        return dirs, unknown

    def resolve_in_dirs(self, spec: str, dirs: list[str]) -> str | None:
        """The file a module name resolves to inside one of the folders put on sys.path (longest name first)."""
        parts = spec.split('.')
        for n in range(len(parts), 0, -1):
            rel = '/'.join(parts[:n])
            for d in dirs:
                pre = '' if d == '.' else d + '/'
                for cand in (f'{pre}{rel}.py', f'{pre}{rel}/__init__.py'):
                    if cand in self.all_paths:
                        return cand
        return None

    def resolve_py(self, mod: str, near: str) -> str | None:
        files = self.py_index.get(mod)
        if not files:
            return None
        pkg = self.package_of(near)
        same = [f for f in files if self.package_of(f) == pkg]
        return (same or files)[0]

    def python(self, path: str, entry: dict) -> None:
        tree = self.tree(path)
        if not isinstance(tree, ast.Module):
            if tree is not None:
                entry['parse_error'] = str(tree).splitlines()[0][:120]
            return
        me = self.module_of(path) or ''
        is_pkg = path.endswith('__init__.py')
        here_pkg = me if is_pkg else me.rpartition('.')[0]

        def add(target: str | None, via: str, line: int, spec: str):
            if not target and spec:
                dirs, unknown = self.syspath_dirs(path)
                found = self.resolve_in_dirs(spec, dirs) if dirs else None
                if found and found != path:
                    target, via = found, 'import (sys.path)'
            if target and target != path:
                if not any(i['path'] == target for i in entry['imports']):
                    entry['imports'].append({'path': target, 'via': via, 'line': line})
            elif not target and spec.split('.')[0] in self.top_level_modules():
                entry['unresolved'].append({'spec': spec, 'line': line})
            elif not target and spec and spec.split('.')[0] not in STDLIB:
                # a bare import of a module that exists in the repository under another folder: put on sys.path at
                # run time (or by PYTHONPATH / runner configuration). Recorded, never dropped.
                cands = [f for f in self.py_names.get(spec.split('.')[0], []) if f != path]
                if cands:
                    _dirs, unknown = self.syspath_dirs(path)
                    entry['unresolved'].append({
                        'spec': spec, 'line': line, 'candidates': cands[:5],
                        'reason': (f'sys.path is changed at line {unknown[0]} to a folder this graph cannot work out'
                                   if unknown else 'not on an import root this graph knows (a sys.path or PYTHONPATH '
                                   'set elsewhere?)')})

        def renders(name: str, line: int, via: str) -> None:
            if PurePosixPath(name).suffix.lower() not in TEMPLATE_EXT:
                return
            for t in self.resolve_named(name, path, self.template_index):
                if not any(i['path'] == t for i in entry['imports']):
                    entry['imports'].append({'path': t, 'via': via, 'line': line})
                entry.setdefault('renders', []).append({'template': t, 'line': line})

        if self.template_index:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                    if name in TEMPLATE_CALLS:
                        args = list(node.args) + [k.value for k in node.keywords
                                                  if k.arg in ('template_name', 'name', 'template_name_or_list')]
                        for a in args[:3]:
                            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                                renders(a.value, node.lineno, name)
                            elif isinstance(a, (ast.List, ast.Tuple)):
                                for e in a.elts:
                                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                        renders(e.value, node.lineno, name)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and \
                        isinstance(node.value.value, str) and \
                        any(isinstance(t, ast.Name) and t.id == 'template_name' for t in node.targets):
                    renders(node.value.value, node.lineno, 'template_name')

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    add(self.resolve_py(a.name, path), 'import', node.lineno, a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = here_pkg.split('.') if here_pkg else []
                    parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = '.'.join(parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ''
                for a in node.names:
                    sub = f'{base}.{a.name}' if base else a.name
                    target = self.resolve_py(sub, path)
                    add(target or self.resolve_py(base, path), 'import', node.lineno, sub if not target and not base else base)
            elif isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if name in ('import_module', '__import__') and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        spec = arg.value
                        if spec.startswith('.'):
                            spec = here_pkg + spec if spec != '.' else here_pkg
                        add(self.resolve_py(spec, path), 'import_module', node.lineno, spec)
                    else:
                        prefix = None
                        if isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.Constant):
                            prefix = arg.values[0].value
                        elif isinstance(arg, ast.BinOp) and isinstance(arg.left, ast.Constant) and isinstance(arg.left.value, str):
                            prefix = arg.left.value
                        if prefix and prefix.startswith('.'):
                            prefix = here_pkg + prefix
                        entry['dynamic'].append({'line': node.lineno, 'call': name, 'lang': 'python', 'prefix': prefix,
                                                 'text': ast.get_source_segment(self.text(path) or '', node) or name})
                elif name in DYNAMIC_ANY:
                    entry['dynamic'].append({'line': node.lineno, 'call': name, 'lang': 'python', 'prefix': None, 'text': name + '(...)'})

    def go_package_dir(self, spec: str) -> str | None:
        """The repo directory of a Go import path in one of the repository's modules (longest module path wins)."""
        for mod in sorted(self.go_mods, key=len, reverse=True):
            if spec == mod or spec.startswith(mod + '/'):
                d = PurePosixPath(self.go_mods[mod]) / spec[len(mod) + 1:] if spec != mod else PurePosixPath(self.go_mods[mod])
                return os.path.normpath(d.as_posix()).replace(os.sep, '/')
        return None

    def go(self, path: str, entry: dict) -> None:
        """Go: a file depends on every non-test file of each package it imports (a package is a directory, found
        from go.mod's module path); a _test.go file also on its own package's files; //go:embed on what it embeds.
        Standard-library and third-party imports are not files here."""
        raw = self.text(path)
        if raw is None:
            return
        text = strip_js_comments(raw)
        here = str(PurePosixPath(path).parent)
        line_of = lambda pos: text.count('\n', 0, pos) + 1  # noqa: E731

        def add(target: str, via: str, line: int) -> None:
            if target != path and not any(i['path'] == target for i in entry['imports']):
                entry['imports'].append({'path': target, 'via': via, 'line': line})

        specs = [(m.group(1), m.start(1)) for m in GO_IMPORT_ONE.finditer(text)]
        for blk in GO_IMPORT_BLOCK.finditer(text):
            specs += [(m.group(1), blk.start(1) + m.start(1)) for m in GO_SPEC.finditer(blk.group(1))]
        for spec, pos in specs:
            d = self.go_package_dir(spec)
            if d is None:
                continue
            files = self.go_src.get(d)
            if not files:
                entry['unresolved'].append({'spec': spec, 'line': line_of(pos)})
                continue
            for f in files:
                add(f, 'import', line_of(pos))
        if path.endswith('_test.go'):
            for f in self.go_src.get(here, []):
                add(f, 'same package', 0)
        for m in GO_EMBED.finditer(raw):
            for pat in m.group(1).split():
                pat = pat.strip('"`').removeprefix('all:')
                full = os.path.normpath((PurePosixPath(here) / pat).as_posix()).replace(os.sep, '/')
                for cand in sorted(self.all_paths):
                    if fnmatch.fnmatch(cand, full) or cand.startswith(full + '/'):
                        add(cand, 'go:embed', raw.count('\n', 0, m.start()) + 1)

    def top_level_modules(self) -> set[str]:
        if not hasattr(self, '_tops'):
            self._tops = {m.split('.')[0] for m in self.py_index}
        return self._tops

    def fixtures_of(self, path: str) -> tuple[set[str], bool]:
        """(fixture names, has an autouse fixture) defined in a conftest.py."""
        if path not in self._fixtures:
            names, autouse = set(), False
            tree = self.tree(path)
            for node in ast.walk(tree) if isinstance(tree, ast.Module) else []:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        call = dec if isinstance(dec, ast.Call) else None
                        target = call.func if call else dec
                        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, 'id', None)
                        if name == 'fixture':
                            fx = node.name
                            for kw in (call.keywords if call else []):
                                if kw.arg == 'name' and isinstance(kw.value, ast.Constant):
                                    fx = kw.value.value
                                if kw.arg == 'autouse' and isinstance(kw.value, ast.Constant) and kw.value.value:
                                    autouse = True
                            names.add(fx)
            self._fixtures[path] = (names, autouse)
        return self._fixtures[path]

    def requested_fixtures(self, path: str) -> set[str]:
        tree = self.tree(path)
        if not isinstance(tree, ast.Module):
            return set()
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out |= {a.arg for a in node.args.args + node.args.kwonlyargs}
            elif isinstance(node, ast.Call) and getattr(node.func, 'attr', getattr(node.func, 'id', None)) == 'usefixtures':
                out |= {a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
        return out

    def conftests(self, path: str, entry: dict) -> None:
        """A test file depends on a conftest.py above it when it requests one of its fixtures (or one is autouse);
        a conftest depends on the conftest files above it."""
        pkg = self.package_of(path)
        wanted = self.requested_fixtures(path) if entry['test_level'] else None
        d = PurePosixPath(path).parent
        while True:
            c = (d / 'conftest.py').as_posix() if str(d) != '.' else 'conftest.py'
            if c != path and c in self.all_paths:
                names, autouse = self.fixtures_of(c)
                used = sorted(names & wanted) if wanted is not None else []
                if wanted is None or used or autouse:
                    entry['imports'].append({'path': c, 'via': 'fixture ' + ', '.join(used) if used else
                                             'autouse fixture' if autouse else 'conftest', 'line': 0})
            if str(d) in ('.', '') or (pkg != '.' and d.as_posix() == pkg):
                break
            d = d.parent

    def conftest_reach(self, conftest: str) -> list[str]:
        """Every test file a conftest.py applies to (its directory and below), whether or not it uses a fixture."""
        d = PurePosixPath(conftest).parent.as_posix()
        return [t for t in self.tests() if d in ('.', '') or t.startswith(d + '/')]

    def tsconfig_aliases(self, paths: list[str]) -> list[tuple[str, str, list[str]]]:
        out = []
        for path in paths:
            if PurePosixPath(path).name.startswith('tsconfig') and path.endswith('.json'):
                opts = read_jsonc(self.root / path).get('compilerOptions', {})
                base = (PurePosixPath(path).parent / opts.get('baseUrl', '.')).as_posix()
                for alias, targets in (opts.get('paths') or {}).items():
                    out.append((str(PurePosixPath(path).parent), alias, [os.path.normpath(f'{base}/{t}').replace(os.sep, '/')
                                                                          for t in targets]))
        return out

    def resolve_js(self, spec: str, path: str) -> str | None:
        spec = spec.split('?')[0].split('#')[0]
        here = PurePosixPath(path).parent
        cands = []
        if spec.startswith('.'):
            cands.append(os.path.normpath((here / spec).as_posix()).replace(os.sep, '/'))
        elif spec.startswith('/'):
            cands.append(spec.lstrip('/'))
        else:
            for scope, alias, targets in self.js_aliases:
                if scope not in ('.', '') and not path.startswith(scope + '/'):
                    continue
                if alias.endswith('*') and spec.startswith(alias[:-1]):
                    cands += [t.replace('*', spec[len(alias) - 1:]) for t in targets]
                elif alias == spec:
                    cands += targets
            for name, root in self.workspace_names.items():
                if spec == name or spec.startswith(name + '/'):
                    rest = spec[len(name):].lstrip('/')
                    pj = read_jsonc(self.root / root / 'package.json')
                    entry = pj.get('source') or pj.get('module') or pj.get('main') or 'src/index'
                    cands.append(f'{root}/{rest}' if rest else f'{root}/{entry}')
        for c in cands:
            c = c.lstrip('./') if c.startswith('./') else c
            for cand in [c] + [c + e for e in JS_RESOLVE] + [f'{c}/index{e}' for e in JS_EXT]:
                if cand in self.all_paths:
                    return cand
        return None

    def javascript(self, path: str, entry: dict) -> None:
        raw = self.text(path)
        if raw is None:
            return
        text = strip_js_comments(raw)
        line_of = lambda pos: text.count('\n', 0, pos) + 1  # noqa: E731

        def add(spec: str, via: str, pos: int):
            target = self.resolve_js(spec, path)
            if target and target != path:
                if not any(i['path'] == target for i in entry['imports']):
                    entry['imports'].append({'path': target, 'via': via, 'line': line_of(pos)})
            elif not target and (spec.startswith('.') or spec.startswith('/')):
                entry['unresolved'].append({'spec': spec, 'line': line_of(pos)})

        for rx, via in ((JS_STATIC, 'import'), (JS_REQUIRE, 'require'), (JS_DYN_IMPORT, 'import()')):
            for m in rx.finditer(text):
                add(m.group(1), via, m.start(1))
        for m in JS_GLOB.finditer(text):
            pats = re.findall(r'''["']([^"']+)["']''', m.group(1))
            here = PurePosixPath(path).parent
            for pat in pats:
                neg = pat.startswith('!')
                full = os.path.normpath((here / pat.lstrip('!')).as_posix()).replace(os.sep, '/')
                if neg:
                    continue
                for cand in sorted(self.all_paths):
                    if fnmatch.fnmatch(cand, full) and cand != path:
                        if not any(i['path'] == cand for i in entry['imports']):
                            entry['imports'].append({'path': cand, 'via': 'import.meta.glob', 'line': line_of(m.start())})
        for m in JS_CONTEXT.finditer(text):
            base = os.path.normpath((PurePosixPath(path).parent / m.group(1)).as_posix()).replace(os.sep, '/')
            deep = m.group(2) != 'false'
            for cand in sorted(self.all_paths):
                rel = cand[len(base) + 1:] if cand.startswith(base + '/') else None
                if rel and (deep or '/' not in rel) and PurePosixPath(cand).suffix in JS_EXT:
                    entry['imports'].append({'path': cand, 'via': 'require.context', 'line': line_of(m.start())})
        for m in JS_DYN_ANY.finditer(text):
            arg = m.group(2).strip()
            prefix = None
            tm = re.match(r'`([^`$]*)\$\{', arg)
            if tm:
                prefix = tm.group(1)
            elif re.match(r'`[^`$]*`', arg):
                add(arg.strip('`'), m.group(1) + '()', m.start(2))
                continue
            entry['dynamic'].append({'line': line_of(m.start()), 'call': m.group(1), 'lang': entry['lang'] or 'javascript',
                                     'prefix': prefix, 'text': f'{m.group(1)}({arg[:60]}'})

    def dynamic_targets(self, d: dict, entry: dict) -> list[str] | None:
        """Python modules a dynamic site could load, by module-name prefix; None = anything (no literal prefix).
        JavaScript sites are resolved by path prefix in js_dynamic_targets()."""
        if d['prefix'] is None or d['lang'] != 'python':
            return None
        out = []
        for mod, files in self.py_index.items():
            if mod.startswith(d['prefix']) and mod != d['prefix'].rstrip('.'):
                out += [f for f in files if self.package_of(f) == entry['package']] or files
        return sorted(set(out))

    # ------------------------------------------------------------ queries
    def tests_reaching(self, target: str, max_depth: int | None = None) -> dict[str, list[str]]:
        """{test file: [test file, ..., target]} by the shortest import chain, for every test that reaches target
        (however long the chain: a depth limit silently dropped tests at the end of long chains). A Rust file with
        its own #[cfg(test)] tests reaches itself: {file: [file]}."""
        parents = {target: None}
        q = deque([(target, 0)])
        while q:
            cur, depth = q.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            for src, _via in self.reverse.get(cur, []):
                if src not in parents:
                    parents[src] = cur
                    q.append((src, depth + 1))
        out = {}
        for f in parents:
            if f != target and self.files.get(f, {}).get('test_level'):
                chain, cur = [], f
                while cur is not None:
                    chain.append(cur)
                    cur = parents[cur]
                out[f] = chain
        if self.files.get(target, {}).get('inline_tests'):
            out[target] = [target]
        return out

    def tests(self, package: str | None = None) -> list[str]:
        return sorted(p for p, e in self.files.items() if e['test_level'] and (package is None or e['package'] == package))

    def to_json(self) -> dict:
        edges = sum(len(e['imports']) for e in self.files.values())
        return {'root': self.root.name, 'packages': sorted(self.packages, key=lambda p: p['root']),
                'languages': dict(sorted(self.languages.items())),
                'summary': {'files': len(self.files), 'tests': len(self.tests()), 'edges': edges,
                            'dynamic_sites': sum(len(e['dynamic']) for e in self.files.values())},
                'files': {p: e for p, e in sorted(self.files.items()) if e['lang'] or e['imports'] or e['test_level']},
                **({'cargo': [{'root': m['root'], 'workspace': m['workspace'],
                               'crates': [{'name': c['name'], 'dir': c['dir'], 'root': c['root'],
                                           'targets': [{k: t.get(k) for k in ('kind', 'name', 'path')} for t in c['targets']],
                                           'fixture_bins': [x['file'] for x in c['fixture_bins']],
                                           'unreached_files': c['unreached_files']}
                                          for c in self.rust_crates.values() if c['root'] == m['root']],
                               'notes': m['notes']} for m in self.cargo_models]} if self.cargo_models else {}),
                'notes': self.notes}


def js_dynamic_targets(g: Graph) -> None:
    """Resolve JS template-literal prefixes (`./templates/${x}`) to the files under that path."""
    for path, entry in g.files.items():
        for d in entry['dynamic']:
            if d['lang'] == 'python':
                continue
            if not d['prefix']:
                d['targets'] = None
                continue
            here = PurePosixPath(path).parent
            base = None
            if d['prefix'].startswith('.'):
                base = os.path.normpath((here / d['prefix']).as_posix()).replace(os.sep, '/')
                if d['prefix'].endswith('/'):
                    base += '/'
            d['targets'] = sorted(p for p in g.all_paths if p.startswith(base) and p != path) if base else None


def build(root: Path) -> Graph:
    g = Graph(root).build()
    js_dynamic_targets(g)
    return g


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--reach', help='print the tests that reach this repo-relative file, with the import chain')
    args = ap.parse_args(argv)
    g = build(args.repo.resolve())
    if args.reach:
        hits = g.tests_reaching(args.reach)
        for t, chain in sorted(hits.items()):
            print(f'{t}: ' + ' -> '.join(chain))
        if not hits:
            node = g.files.get(args.reach)
            lang = (node or {}).get('lang')
            why = ('not a file in the graph' if node is None else
                   f'no import parser for {lang}, so nothing is known' if g.languages.get(lang) == 'no_parser' else
                   'no test reaches it by import')
            print(f'{args.reach}: {why}', file=sys.stderr)
            return 1
        return 0
    import lang_census
    supported = {'Python', 'JavaScript', 'TypeScript', 'Go'} | ({'Rust'} if g.rust_crates else set())
    cov = lang_census.language_coverage(lang_census.census(g.root), supported,
                                        tool='import_graph.py', what='import graph')
    if cov.get('warning'):
        print('WARNING: ' + cov['warning'], file=sys.stderr)
    unparsed = sorted(l for l, s in g.languages.items() if s == 'no_parser')
    if unparsed:
        print(f'note: no import parser for {", ".join(unparsed)}; those files have no edges', file=sys.stderr)
    data = g.to_json()
    data['language_coverage'] = cov
    text = json.dumps(data, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
        s = data['summary']
        print(f'{s["files"]} files, {s["tests"]} test files, {s["edges"]} edges, {s["dynamic_sites"]} dynamic sites -> {args.out}')
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
