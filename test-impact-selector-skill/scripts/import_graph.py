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
dynamic sites, with the files they could load when the name has a literal prefix. Files in
languages with no parser here (Go, Java, Kotlin, C#, Swift, Dart, Rust, Ruby, PHP) are listed
under `languages` as `no_parser`; nothing is guessed for them.

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

KINDS = json.loads((HERE.parent / 'data' / 'file_kinds.json').read_text(encoding='utf-8'))
SKIP_DIRS = detect_conventions.SKIP_DIRS | {'.git', '.hg', '.svn', '__snapshots__'}
PY_EXT = {'.py'}
JS_EXT = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte']
JS_RESOLVE = JS_EXT + ['.json', '.css', '.scss', '.sass', '.less', '.html', '.svg']
NO_PARSER = {'.go': 'go', '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin', '.cs': 'csharp', '.swift': 'swift',
             '.dart': 'dart', '.rs': 'rust', '.rb': 'ruby', '.php': 'php', '.scala': 'scala', '.m': 'objective-c'}
DYNAMIC_ANY = {'iter_modules', 'walk_packages', 'entry_points', 'load_entry_point', 'iter_entry_points'}

JS_STATIC = re.compile(r'''(?:^|[^\w$.])(?:import|export)\s+(?:type\s+)?(?:[\w$*{}\s,]+?\s+from\s+)?["']([^"'\n]+)["']''', re.M)
JS_REQUIRE = re.compile(r'''(?:^|[^\w$.])require\s*\(\s*["']([^"'\n]+)["']\s*\)''')
JS_DYN_IMPORT = re.compile(r'''(?:^|[^\w$.])import\s*\(\s*(?:/\*.*?\*/\s*)?["']([^"'\n]+)["']\s*\)''')
JS_DYN_ANY = re.compile(r'''(?:^|[^\w$.])(import|require)\s*\(\s*(?:/\*.*?\*/\s*)?(?!["'\s)])([^)\n]{0,120})''')
JS_GLOB = re.compile(r'''import\.meta\.glob(?:Eager)?\s*\(\s*(\[[^\]]*\]|["'][^"']+["'])''')
JS_CONTEXT = re.compile(r'''require\.context\s*\(\s*["']([^"']+)["']\s*(?:,\s*(true|false))?''')


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


class Graph:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, dict] = {}
        self.languages: dict[str, str] = {}
        self._fixtures: dict[str, tuple[set[str], bool]] = {}
        self.packages: list[dict] = []
        self.product_roots: set[str] = set()
        self._stripped: dict[str, str | None] = {}
        self.notes: list[str] = []

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
        det = detect_conventions.Detector(self.root, 50_000)
        res = det.run(None)
        for a in res['areas']:
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
        py_roots = sorted({p['root'] for p in self.packages} | {'.'})
        for path in paths:
            if path.endswith('.py'):
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
        self.js_aliases = self.tsconfig_aliases(paths)
        self.workspace_names = {p['name']: p['root'] for p in self.packages if p['name'] != p['root']}
        self.all_paths = set(paths)
        for path in paths:
            ext = PurePosixPath(path).suffix.lower()
            entry = {'package': self.package_of(path), 'lang': None, 'test_level': test_level(self.test_path(path)), 'imports': [],
                     'unresolved': [], 'dynamic': []}
            if ext in PY_EXT:
                entry['lang'] = 'python'
                self.python(path, entry)
                self.languages['python'] = 'parsed'
            elif ext in JS_EXT:
                entry['lang'] = 'typescript' if ext in ('.ts', '.tsx', '.mts', '.cts') else 'javascript'
                self.javascript(path, entry)
                self.languages[entry['lang']] = 'parsed'
            elif ext in NO_PARSER:
                entry['lang'] = NO_PARSER[ext]
                self.languages.setdefault(NO_PARSER[ext], 'no_parser')
            self.files[path] = entry
        if self.product_roots:
            self.notes.append('Product package(s) named like a test directory: ' + ', '.join(sorted(self.product_roots)) +
                              '. Their files are classified by their path inside the package, so only their own test '
                              'files are tests.')
        for path, entry in self.files.items():
            if entry['lang'] == 'python' and (entry['test_level'] or PurePosixPath(path).name == 'conftest.py'):
                self.conftests(path, entry)
        for entry in self.files.values():
            for d in entry['dynamic']:
                d['targets'] = self.dynamic_targets(d, entry)
        self.reverse = {}
        for path, entry in self.files.items():
            for imp in entry['imports']:
                self.reverse.setdefault(imp['path'], []).append((path, imp['via']))
        return self

    def module_of(self, path: str) -> str | None:
        for mod, files in self.py_index.items():
            if path in files:
                return mod
        return None

    def resolve_py(self, mod: str, near: str) -> str | None:
        files = self.py_index.get(mod)
        if not files:
            return None
        pkg = self.package_of(near)
        same = [f for f in files if self.package_of(f) == pkg]
        return (same or files)[0]

    def python(self, path: str, entry: dict) -> None:
        try:
            tree = ast.parse((self.root / path).read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError) as exc:
            entry['parse_error'] = str(exc).splitlines()[0][:120]
            return
        me = self.module_of(path) or ''
        is_pkg = path.endswith('__init__.py')
        here_pkg = me if is_pkg else me.rpartition('.')[0]

        def add(target: str | None, via: str, line: int, spec: str):
            if target and target != path:
                if not any(i['path'] == target for i in entry['imports']):
                    entry['imports'].append({'path': target, 'via': via, 'line': line})
            elif not target and spec.split('.')[0] in self.top_level_modules():
                entry['unresolved'].append({'spec': spec, 'line': line})

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
                                                 'text': ast.get_source_segment((self.root / path).read_text(encoding='utf-8',
                                                                                errors='replace'), node) or name})
                elif name in DYNAMIC_ANY:
                    entry['dynamic'].append({'line': node.lineno, 'call': name, 'lang': 'python', 'prefix': None, 'text': name + '(...)'})

    def top_level_modules(self) -> set[str]:
        if not hasattr(self, '_tops'):
            self._tops = {m.split('.')[0] for m in self.py_index}
        return self._tops

    def fixtures_of(self, path: str) -> tuple[set[str], bool]:
        """(fixture names, has an autouse fixture) defined in a conftest.py."""
        if path not in self._fixtures:
            names, autouse = set(), False
            try:
                tree = ast.parse((self.root / path).read_text(encoding='utf-8', errors='replace'))
            except (SyntaxError, ValueError, OSError):
                tree = None
            for node in ast.walk(tree) if tree else []:
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
        try:
            tree = ast.parse((self.root / path).read_text(encoding='utf-8', errors='replace'))
        except (SyntaxError, ValueError, OSError):
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
        try:
            text = strip_js_comments((self.root / path).read_text(encoding='utf-8', errors='replace'))
        except OSError:
            return
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
    def tests_reaching(self, target: str, max_depth: int = 50) -> dict[str, list[str]]:
        """{test file: [test file, ..., target]} by the shortest import chain, for every test that reaches target."""
        parents = {target: None}
        q = deque([(target, 0)])
        while q:
            cur, depth = q.popleft()
            if depth >= max_depth:
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
        for t, chain in sorted(g.tests_reaching(args.reach).items()):
            print(f'{t}: ' + ' -> '.join(chain))
        return 0
    data = g.to_json()
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
