#!/usr/bin/env python3
"""Tell a repository's own code apart from its sample projects and its copied files (stdlib only).

    python repo_layout.py <repo> [--fixture-dir NAME|PATH ...] [--no-copies] [--out layout.json]
    python repo_layout.py --self-test

Tool and library repositories often carry deliberately broken sample projects (examples/shop/ with its own
package.json, testdata/golden/ with an expected.json) and files kept identical in several folders (a script
copied into every package, a vendored helper). A scanner that reads them as the host repository's own code
reports the samples' planted defects as real ones and every copy once per copy. This module names both, with
reasons, so a scanner can leave fixtures out (and say so) and report a finding once, on the canonical copy.

API (paths are repository-relative, '/'-separated):

    fixture_projects(root, extra=()) -> [{dir, reason, markers}]
        Nested sample or fixture projects: a directory at or below a fixture parent (examples/, example/,
        fixtures/, fixture/, testdata/, test-data/, test_data/, samples/, sample/, __fixtures__/, or a directory
        the caller names in `extra`, by name or by path) that holds its own manifest (package.json,
        pyproject.toml, setup.cfg, setup.py, requirements*.txt, Cargo.toml, go.mod, *.csproj, pom.xml,
        build.gradle[.kts], Gemfile, composer.json, Package.swift, pubspec.yaml), an expected*.json beside
        sample sources, or (a direct child of the fixture parent) an app of its own with no manifest: an entry or
        service file at its top (server.py, serve.py, app.py, main.go, index.html, alembic.ini,
        docker-compose.yml, Dockerfile, openapi.yaml ...) or a site/, src/, web/, app/, public/ or static/
        folder holding sources, or a test-only folder (examples/regression/ holding test_*.py and recorded runs,
        whose every source file is a test file: the tests of a sample project kept beside it). Only the outermost
        such directory is listed (its nested projects belong to it). Never the
        repository root, never a workspace package the root declares (package.json workspaces,
        pnpm-workspace.yaml). A path named in `extra` that holds no such project is itself listed ("named by
        the caller"). markers: the files that decided it.
    is_fixture(path, fixtures) -> str | None
        The fixture directory path lies in, or None.
    fixture_dirs(root, extra=(), files=None) -> [{dir, reason, data_files, projects}]
        The outermost fixture parent folders (as above, or named in `extra`) that hold data files (DATA_EXT,
        manifests left out) or fixture projects; a fixture parent holding neither is not listed.
    walk_files(root) -> [path]
        Every file below root, repository-relative, with VCS, dependency, cache and virtual-environment folders
        left out (the walk copy_groups uses).
    declared_shared(root) -> {canonical: [copies]}
        The declared copy mapping of a tools/check_shared.py-style script, read with ast (never imported): a
        module-level SHARED = {'scripts/x.py': ['skill-a', 'skill-b:other/x.py']} dict whose keys live under
        shared/. {} when there is none.
    copy_groups(root, files=None, declared=None) -> [{canonical, copies, how, ...}]
        Files kept identical in several places: same content (line endings normalised), at least MIN_BYTES long,
        not an empty or boilerplate file (__init__.py, LICENSE, .gitkeep, py.typed ...). how: 'declared' when the
        declared mapping names the canonical copy (then also `undeclared`: identical copies the mapping does not
        name, and `drifted`: declared copies whose content differs), else 'identical' with the shortest path
        (fewest segments, then shortest, then first in order) as canonical. files: limit to these paths;
        declared: a mapping as declared_shared returns (default: read from root).
    copy_index(groups) -> {path: canonical}  every copy (and canonical) mapped to its canonical path.
    classify(root, extra=(), copies=True) -> {root, fixtures, copy_groups, notes, stats}

Every decision carries its reason; nothing is excluded silently. Directories never walked: VCS, dependency,
cache and virtual-environment folders (.git, node_modules, __pycache__, .venv, ...).
"""
from __future__ import annotations
import ast, fnmatch, hashlib, json, os, re, stat, sys
from pathlib import Path, PurePosixPath

FIXTURE_PARENTS = {'examples', 'example', 'fixtures', 'fixture', 'testdata', 'test-data', 'test_data', 'samples',
                   'sample', '__fixtures__'}
MANIFEST_NAMES = {'package.json', 'pyproject.toml', 'setup.cfg', 'setup.py', 'Cargo.toml', 'go.mod', 'pom.xml',
                  'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json', 'Package.swift', 'pubspec.yaml'}
SOURCE_EXT = {'.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.go', '.rs', '.java', '.kt', '.cs', '.rb', '.php',
              '.swift', '.dart', '.scala', '.c', '.cc', '.cpp', '.h', '.html', '.vue', '.svelte', '.sql'}
PRUNE = {'.git', '.hg', '.svn', 'node_modules', 'bower_components', '__pycache__', '.venv', 'venv', '.tox', '.nox',
         '.mypy_cache', '.pytest_cache', '.ruff_cache', '.cache', '.next', '.nuxt', '.turbo', '.gradle', '.idea',
         'site-packages', 'target', '.terraform'}
# a sample app with no manifest: an entry point or service file at its top, or a web root or source folder below it
APP_FILES = {'server.py', 'serve.py', 'app.py', 'main.py', 'manage.py', 'wsgi.py', 'asgi.py', 'main.go', 'index.html',
             'server.js', 'app.js', 'index.js', 'alembic.ini', 'docker-compose.yml', 'docker-compose.yaml', 'Dockerfile',
             'openapi.yaml', 'openapi.json', 'openapi.yml'}
APP_DIRS = {'site', 'src', 'web', 'app', 'public', 'static', 'www'}
BOILERPLATE_NAMES = {'__init__.py', 'py.typed', '.gitkeep', '.keep', '.gitignore', '.npmignore', '.dockerignore',
                     '.gitattributes', '.editorconfig', '.nojekyll'}
BOILERPLATE_RX = re.compile(r'^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)([.-].*)?$', re.I)
MIN_BYTES = 100
SHARED_SCRIPTS = ('tools/check_shared.py', 'scripts/check_shared.py', 'check_shared.py')
SHARED_BASE = 'shared'


def _manifests(names) -> list[str]:
    return sorted(n for n in names if n in MANIFEST_NAMES or n.endswith('.csproj')
                  or re.match(r'^requirements[\w.-]*\.txt$', n))


def _workspace_globs(root: Path) -> list[str]:
    """Workspace package globs the root declares (package.json workspaces, pnpm-workspace.yaml packages)."""
    globs: list[str] = []
    try:
        pj = json.loads((root / 'package.json').read_text(encoding='utf-8'))
        w = pj.get('workspaces') if isinstance(pj, dict) else None
        globs += list(w if isinstance(w, list) else (w or {}).get('packages', []) if isinstance(w, dict) else [])
    except (OSError, ValueError):
        pass
    try:
        text = (root / 'pnpm-workspace.yaml').read_text(encoding='utf-8')
        m = re.search(r'^packages\s*:\s*\n((?:\s+-\s*.*\n?)+)', text, re.M)
        if m:
            globs += [x.strip().strip('\'"') for x in re.findall(r'-\s*(.+)', m.group(1))]
    except OSError:
        pass
    return [g.strip('./').rstrip('/') for g in globs if isinstance(g, str) and not g.startswith('!')]


def _has_sources(d: Path, cap: int = 2000) -> int:
    n = seen = 0
    for dp, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in PRUNE]
        for f in fns:
            seen += 1
            if PurePosixPath(f).suffix.lower() in SOURCE_EXT:
                n += 1
        if seen > cap:
            break
    return n


TEST_FILE_RX = re.compile(r'^(test_.+\.py|.+_test\.py|.+\.(test|spec)\.[cm]?[jt]sx?|.+_test\.go|.+_spec\.rb|'
                          r'.+Tests?\.(java|kt|cs))$')


def _test_only(d: Path, cap: int = 500) -> list[str]:
    """The test files of a folder whose every source file is a test file (none of its own sources), else []."""
    tests, seen = [], 0
    for dp, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in PRUNE]
        for f in fns:
            seen += 1
            if PurePosixPath(f).suffix.lower() not in SOURCE_EXT or f in ('conftest.py', '__init__.py'):
                continue
            if not TEST_FILE_RX.match(f):
                return []
            tests.append((Path(dp) / f).relative_to(d).as_posix())
        if seen > cap:
            return []
    return sorted(tests)


def fixture_projects(root, extra=()) -> list[dict]:
    """Nested sample or fixture projects (see the module doc): [{dir, reason, markers}], sorted by dir."""
    root = Path(root).resolve()
    names = set(FIXTURE_PARENTS)
    paths = set()
    for e in extra or ():
        e = str(e).replace('\\', '/').strip('/')
        if not e:
            continue
        (paths if '/' in e or (root / e).is_dir() else names).add(e)
        if '/' not in e:
            names.add(e)
    ws = _workspace_globs(root)
    out: list[dict] = []
    base = str(root)
    cut = len(base) + (0 if base.endswith(os.sep) else 1)
    for dp, dns, fns in os.walk(base):
        rel = dp[cut:].replace(os.sep, '/') if len(dp) > len(base) else ''   # as relative_to().as_posix(), cheaply
        dns[:] = sorted(d for d in dns if d not in PRUNE and not d.endswith('.egg-info'))
        if not rel:
            continue
        parts = rel.split('/')
        parent = next((p for p in parts if p in names), None)
        named = next((p for p in paths if rel == p or rel.startswith(p + '/')), None)
        if not parent and not named:
            continue
        if any(fnmatch.fnmatch(rel, g) for g in ws):
            continue   # a workspace package the root declares is product code
        markers = _manifests(fns)
        why = None
        if markers:
            why = f'its own manifest ({", ".join(markers)})'
        else:
            expected = sorted(f for f in fns if re.match(r'^expected[\w.-]*\.json$', f, re.I))
            if expected:
                n = _has_sources(Path(dp))
                if n:
                    markers = expected + [f'{n} source file(s)']
                    why = f'{expected[0]} beside {n} sample source file(s)'
        if not why and Path(dp).parent.name in names:
            entry = sorted(f for f in fns if f in APP_FILES)
            webroot = sorted(d for d in dns if d in APP_DIRS and _has_sources(Path(dp) / d))
            if entry or webroot:
                markers = entry + [d + '/' for d in webroot]
                why = 'an app of its own (' + ', '.join(markers) + ')'
            else:
                tests = _test_only(Path(dp))
                if tests:
                    markers = tests
                    why = (f'a test-only folder ({len(tests)} test file(s), no sources of its own): the tests of a '
                           'sample project, kept beside it')
        if why:
            where = f'{named}/ (named by the caller)' if named else f'{parent}/'
            out.append({'dir': rel, 'reason': f'a nested project under {where} with {why}', 'markers': markers})
            dns[:] = []   # its nested projects belong to it
    found = {f['dir'] for f in out}
    for p in sorted(paths):
        if (root / p).is_dir() and not any(d == p or d.startswith(p + '/') for d in found):
            out.append({'dir': p, 'reason': 'named by the caller as a fixture directory', 'markers': []})
    return sorted(out, key=lambda f: f['dir'])


def is_fixture(path: str, fixtures) -> str | None:
    p = path.replace('\\', '/').strip('/')
    for f in fixtures:
        d = f['dir'] if isinstance(f, dict) else f
        if p == d or p.startswith(d + '/'):
            return d
    return None


DATA_EXT = {'.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.csv', '.tsv', '.xml', '.txt', '.sql', '.har', '.snap',
            '.toml', '.ini', '.parquet', '.xlsx', '.xls', '.db', '.sqlite', '.sqlite3'}


def fixture_dirs(root, extra=(), files=None) -> list[dict]:
    """The outermost fixture parent folders (FIXTURE_PARENTS, or a name or path in `extra`) that hold data files or
    fixture projects: [{dir, reason, data_files, projects}], sorted by dir. data_files counts the files below it with a
    data extension (DATA_EXT), manifests left out; projects are the fixture_projects inside it. A fixture parent that
    holds neither (only documentation, say) is not listed. files: limit to these repository-relative paths."""
    root = Path(root).resolve()
    names = set(FIXTURE_PARENTS)
    paths = set()
    for e in extra or ():
        e = str(e).replace('\\', '/').strip('/')
        if e:
            (paths if '/' in e else names).add(e)
    rels = [f.replace('\\', '/').strip('/') for f in files] if files is not None else walk_files(root)
    projects = fixture_projects(root, extra)

    def parent_of(rel: str) -> str | None:
        parts = rel.split('/')[:-1]
        cands = [p for p in paths if rel.startswith(p + '/')]
        i = next((k for k, seg in enumerate(parts) if seg in names), None)
        named = min(cands, key=len) if cands else None
        found = '/'.join(parts[:i + 1]) if i is not None else None
        if named and (found is None or len(named) <= len(found)):
            return named
        return found
    data: dict[str, int] = {}
    for rel in rels:
        d = parent_of(rel)
        if d is None:
            continue
        data.setdefault(d, 0)
        name = PurePosixPath(rel).name
        if PurePosixPath(rel).suffix.lower() in DATA_EXT and not _manifests([name]):
            data[d] += 1
    for pr in projects:
        data.setdefault(parent_of(pr['dir'] + '/x') or pr['dir'], 0)
    out = []
    for d in sorted(data):
        projs = sorted(pr['dir'] for pr in projects if pr['dir'] == d or pr['dir'].startswith(d + '/'))
        n = data[d]
        if not n and not projs:
            continue
        bits = ([f'{n} data file(s)'] if n else []) + ([f'{len(projs)} sample project(s)'] if projs else [])
        out.append({'dir': d, 'reason': f'a fixture folder ({PurePosixPath(d).name}/) holding ' + ' and '.join(bits),
                    'data_files': n, 'projects': projs})
    return out


def declared_shared(root) -> dict[str, list[str]]:
    """{canonical: [copies]} (repository-relative) from a check_shared.py-style SHARED dict, read with ast."""
    root = Path(root)
    for rel in SHARED_SCRIPTS:
        p = root / rel
        if not p.is_file():
            continue
        try:
            tree = ast.parse(p.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        consts: dict = {}   # module-level names bound to literals (a list of skills the mapping reuses)

        def value(n):
            """A literal, with module-level names and list/tuple concatenation resolved; ValueError otherwise."""
            if isinstance(n, ast.Name):
                if n.id in consts:
                    return consts[n.id]
                raise ValueError(n.id)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                return value(n.left) + value(n.right)
            if isinstance(n, (ast.List, ast.Tuple)):
                return [value(x) for x in n.elts]
            if isinstance(n, ast.Dict):
                return {value(k): value(v) for k, v in zip(n.keys, n.values) if k is not None}
            return ast.literal_eval(n)
        for node in tree.body:
            target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else \
                node.target if isinstance(node, ast.AnnAssign) else None
            if not isinstance(target, ast.Name) or node.value is None:
                continue
            if target.id != 'SHARED':
                try:
                    consts[target.id] = value(node.value)
                except (ValueError, TypeError, SyntaxError):
                    pass
                continue
            try:
                mapping = value(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            if not isinstance(mapping, dict):
                continue
            out = {}
            for key, entries in mapping.items():
                if not isinstance(key, str) or not isinstance(entries, (list, tuple)):
                    continue
                copies = []
                for entry in entries:
                    if isinstance(entry, str):
                        where, _, other = entry.partition(':')
                        copies.append(f'{where}/{other or key}')
                out[f'{SHARED_BASE}/{key}'] = copies
            return out
    return {}


def walk_files(root) -> list[str]:
    """Every file under root, repository-relative and '/'-separated, sorted per folder; VCS, dependency, cache and
    virtual-environment folders (PRUNE) and *.egg-info are not walked."""
    base = str(Path(root))
    cut = len(base) + (0 if base.endswith(os.sep) else 1)
    out = []
    for dp, dns, fns in os.walk(base):
        dns[:] = sorted(d for d in dns if d not in PRUNE and not d.endswith('.egg-info'))
        rel = dp[cut:].replace(os.sep, '/') if len(dp) > len(base) else ''   # as relative_to().as_posix(), cheaply
        for f in sorted(fns):
            out.append(f'{rel}/{f}' if rel else f)
    return out


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalised(p) -> bytes | None:
    try:
        with open(p, 'rb') as fh:
            return fh.read().replace(b'\r\n', b'\n')
    except OSError:
        return None


def _digest(p) -> tuple[str, int] | None:
    data = _normalised(p)
    return None if data is None else (_hash_bytes(data), len(data))


def _boilerplate(name: str) -> bool:
    return name in BOILERPLATE_NAMES or bool(BOILERPLATE_RX.match(name))


def _shortest(paths) -> str:
    return min(paths, key=lambda r: (r.count('/'), len(r), r))


def copy_groups(root, files=None, declared=None) -> list[dict]:
    """Groups of identical files (see the module doc): [{canonical, copies, how[, undeclared, drifted]}]."""
    root = Path(root).resolve()
    declared = declared_shared(root) if declared is None else declared
    rels = [f.replace('\\', '/').strip('/') for f in files] if files is not None else walk_files(root)
    by_hash: dict[str, list[str]] = {}
    digests: dict[str, str] = {}
    base = str(root)
    # Identical files have the same normalised length, so a file is hashed only once another shares its length; the
    # first file of each length waits with its content (or, over 1 MB, its hash) until one does.
    first: dict[int, tuple[str, bytes | str]] = {}

    def add(rel: str, payload: bytes | str) -> None:
        h = payload if isinstance(payload, str) else _hash_bytes(payload)
        digests[rel] = h
        by_hash.setdefault(h, []).append(rel)
    for rel in rels:
        if _boilerplate(rel.rsplit('/', 1)[-1]):
            continue
        p = os.path.join(base, rel)
        try:
            st = os.stat(p)   # one call for both "a regular file" and its size
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size < MIN_BYTES:
            continue
        data = _normalised(p)
        if data is None or len(data) < MIN_BYTES:
            continue
        payload = _hash_bytes(data) if len(data) > 1_000_000 else data
        seen = first.get(len(data))
        if seen is None:
            first[len(data)] = (rel, payload)
            continue
        if seen[0]:
            add(*seen)
            first[len(data)] = ('', '')
        add(rel, payload)
    groups = []
    used = set()
    for canonical, copies in sorted(declared.items()):
        h = digests.get(canonical) or (_digest(root / canonical) or (None,))[0]
        if not h:
            continue
        same = sorted(r for r in by_hash.get(h, []) if r != canonical)
        drifted = sorted(c for c in copies if (root / c).is_file() and c not in same and c != canonical
                         and (_digest(root / c) or (None,))[0] != h)
        if not same and not drifted:
            continue
        g = {'canonical': canonical, 'copies': same, 'how': 'declared'}
        und = sorted(set(same) - set(copies))
        if und:
            g['undeclared'] = und
        if drifted:
            g['drifted'] = drifted
        groups.append(g)
        used.add(h)
    for h, members in by_hash.items():
        if h in used or len(members) < 2:
            continue
        can = _shortest(members)
        groups.append({'canonical': can, 'copies': sorted(m for m in members if m != can), 'how': 'identical'})
    return sorted(groups, key=lambda g: g['canonical'])


def copy_index(groups) -> dict[str, str]:
    out = {}
    for g in groups:
        out[g['canonical']] = g['canonical']
        for c in g['copies']:
            out[c] = g['canonical']
    return out


def classify(root, extra=(), copies=True) -> dict:
    root = Path(root).resolve()
    fx = fixture_projects(root, extra)
    groups = copy_groups(root) if copies else []
    notes = []
    if fx:
        notes.append(f'{len(fx)} sample or fixture project(s) found; scanners that audit the host repository should '
                     'leave them out and say so (' + ', '.join(f['dir'] for f in fx[:5])
                     + (' ...' if len(fx) > 5 else '') + ')')
    if groups:
        n = sum(len(g['copies']) for g in groups)
        notes.append(f'{len(groups)} file(s) are kept identical in {n} further place(s); report a finding once, on the '
                     'canonical copy, and list the copies')
    return {'root': root.name, 'fixtures': fx, 'copy_groups': groups, 'notes': notes,
            'stats': {'fixtures': len(fx), 'copy_groups': len(groups),
                      'copies': sum(len(g['copies']) for g in groups)}}


# ------------------------------------------------------------------------------------------------ self-test
def _tree(files: dict[str, str]) -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix='repo-layout-'))
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body.encode('utf-8'))
    return d


def self_test() -> int:
    import shutil
    checks = []
    helper = 'def helper(x):\n' + ''.join(f'    x = x + {i}\n' for i in range(12)) + '    return x\n'
    util = '"""A shared utility."""\n' + helper
    files = {
        'pyproject.toml': '[project]\nname = "tool"\n',                      # the repository itself
        'package.json': '{"name": "tool", "workspaces": ["packages/*"]}',
        'packages/samples/package.json': '{"name": "samples"}',              # a declared workspace package: product
        'packages/samples/index.js': 'module.exports = 1;\n',
        # the mapping names a module-level list and adds to it, as a real one may
        'tools/check_shared.py': ("import sys\nROOT = sys.argv[0]\nSKILLS = ['alpha-skill', 'beta-skill:lib/util.py']\n"
                                  "SHARED = {\n    'scripts/util.py': SKILLS + [\n"
                                  "                        'delta-skill'],\n}\n"),
        'shared/scripts/util.py': util,
        'alpha-skill/scripts/util.py': util,
        'beta-skill/lib/util.py': util.replace('\n', '\r\n'),              # CRLF: still identical
        'delta-skill/scripts/util.py': util + '# drifted\n',
        'gamma-skill/scripts/util.py': util,                                  # identical but not declared
        'vendor_a/helper.py': helper, 'libs/b/helper.py': helper,            # undeclared identical pair
        'alpha-skill/scripts/__init__.py': '', 'beta-skill/scripts/__init__.py': '',
        'alpha-skill/LICENSE': 'MIT licence text ' * 10, 'beta-skill/LICENSE': 'MIT licence text ' * 10,
        'a/tiny.txt': 'x = 1\n', 'b/tiny.txt': 'x = 1\n',
        'examples/shop/package.json': '{"name": "shop"}', 'examples/shop/src/cart.js': 'eval(input)\n',
        'examples/shop/nested/package.json': '{"name": "inner"}',
        'alpha-skill/examples/ledger/pyproject.toml': '[project]\nname = "ledger"\n',
        'alpha-skill/examples/ledger/ledger.py': 'import os\n',
        'alpha-skill/examples/run/output.json': '{"ok": true}',             # example outputs: not a project
        'alpha-skill/examples/report.example.json': '{}',
        'examples/stub-site/serve.py': 'import http.server\n', 'examples/stub-site/site/index.html': '<p>hi</p>',
        'examples/repro/repro_1.py': 'print(1)\n',                          # loose scripts: not an app
        # the regression tests of a sample project, kept in their own folder with their recorded runs
        'examples/regression/test_shop_regressions.py': 'def test_cart():\n    assert 1\n',
        'examples/regression/fixed_run.txt': '1 passed\n',
        'docs/fixtures/README.md': '# How the fixtures are made\n',          # a fixture parent with no data or project
        'tests/fixtures/data/a.json': '{}',
        'tests/fixtures/app/go.mod': 'module example.test/app\n', 'tests/fixtures/app/main.go': 'package main\n',
        'testdata/golden/expected.json': '[]', 'testdata/golden/main.go': 'package main\n',
        'testdata/plain/input.txt': 'hello\n',
        'samples/notes.txt': 'notes\n',
        'demo-apps/one/package.json': '{"name": "one"}', 'demo-apps/one/app.js': '',
        'src/app.py': 'print(1)\n',
    }
    root = _tree(files)
    try:
        fx = {f['dir']: f for f in fixture_projects(root, extra=['demo-apps'])}
        want = {'examples/shop', 'alpha-skill/examples/ledger', 'tests/fixtures/app', 'testdata/golden', 'demo-apps/one',
                'examples/stub-site', 'examples/regression'}
        checks.append(('nested sample projects under examples/, fixtures/ and testdata/ (manifest, expected.json '
                       'beside sources, or an app entry point) and under a caller-named directory are found, outermost only',
                       set(fx) == want, sorted(fx)))
        checks.append(('each fixture says why, with its markers',
                       all(f['reason'] and f['markers'] for f in fx.values())
                       and 'package.json' in fx.get('examples/shop', {}).get('markers', [])
                       and 'expected.json' in fx.get('testdata/golden', {}).get('markers', []), list(fx.values())[:2]))
        checks.append(('a test-only folder under a fixture parent (test files, no sources of its own, no manifest) is a '
                       'fixture project, with its reason',
                       'test-only' in fx.get('examples/regression', {}).get('reason', '')
                       and 'test_shop_regressions.py' in fx.get('examples/regression', {}).get('markers', []),
                       fx.get('examples/regression')))
        try:
            walked = walk_files(root)
        except NameError as e:
            walked = str(e)
        checks.append(('walk_files lists every file, repository-relative, pruning dependency and VCS folders',
                       isinstance(walked, list) and sorted(walked) == sorted(files),
                       walked if isinstance(walked, str) else sorted(set(walked) ^ set(files))))
        try:
            fdirs = {f['dir']: f for f in fixture_dirs(root, extra=['demo-apps'])}
        except NameError as e:
            fdirs = {'error': str(e)}
        checks.append(('fixture_dirs lists the outermost fixture parents that hold data files or fixture projects, with '
                       'their reason, data file count and projects; one holding neither is not listed',
                       set(fdirs) == {'examples', 'alpha-skill/examples', 'tests/fixtures', 'testdata', 'samples',
                                      'demo-apps'}
                       and fdirs['examples']['projects'] == ['examples/regression', 'examples/shop', 'examples/stub-site']
                       and fdirs['testdata']['data_files'] == 2 and fdirs['samples']['projects'] == []
                       and all(f['reason'] for f in fdirs.values()), fdirs))
        checks.append(('the repository root, a declared workspace package, example outputs and plain data are not '
                       'fixture projects', not ({'.', '', 'packages/samples', 'alpha-skill/examples/run',
                                                  'testdata/plain', 'samples', 'examples/repro'} & set(fx)), sorted(fx)))
        checks.append(('is_fixture places a file inside a fixture project',
                       is_fixture('examples/shop/src/cart.js', list(fx.values())) == 'examples/shop'
                       and is_fixture('src/app.py', list(fx.values())) is None, None))
        dec = declared_shared(root)
        checks.append(('declared_shared reads the SHARED mapping without importing it',
                       dec == {'shared/scripts/util.py': ['alpha-skill/scripts/util.py', 'beta-skill/lib/util.py',
                                                          'delta-skill/scripts/util.py']}, dec))
        groups = copy_groups(root)
        by_can = {g['canonical']: g for g in groups}
        g = by_can.get('shared/scripts/util.py', {})
        checks.append(('a declared shared file is one group with the declared canonical copy; an identical undeclared '
                       'copy and a drifted declared copy are named',
                       g.get('how') == 'declared' and sorted(g.get('copies', [])) == [
                           'alpha-skill/scripts/util.py', 'beta-skill/lib/util.py', 'gamma-skill/scripts/util.py']
                       and g.get('undeclared') == ['gamma-skill/scripts/util.py']
                       and g.get('drifted') == ['delta-skill/scripts/util.py'], g))
        h = by_can.get('vendor_a/helper.py', {})
        checks.append(('undeclared identical files form a group with the shortest path as canonical',
                       h.get('how') == 'identical' and h.get('copies') == ['libs/b/helper.py'], sorted(by_can)))
        checks.append(('empty, boilerplate (__init__.py, LICENSE) and tiny files never form groups',
                       set(by_can) == {'shared/scripts/util.py', 'vendor_a/helper.py'}, sorted(by_can)))
        idx = copy_index(groups)
        checks.append(('copy_index maps every copy to its canonical path',
                       idx.get('beta-skill/lib/util.py') == 'shared/scripts/util.py'
                       and idx.get('libs/b/helper.py') == 'vendor_a/helper.py', None))
        c = classify(root, extra=['demo-apps'])
        checks.append(('classify reports both, with notes and counts',
                       c['stats'] == {'fixtures': 7, 'copy_groups': 2, 'copies': 4} and len(c['notes']) == 2, c['stats']))
        only = copy_groups(root, files=['vendor_a/helper.py', 'libs/b/helper.py', 'src/app.py'], declared={})
        checks.append(('copy_groups can be limited to given files and an explicit (empty) mapping',
                       [x['canonical'] for x in only] == ['vendor_a/helper.py'], only))
        body = ''.join(f'line {k} of a shared table\n' for k in range(12))
        for rel, data in (('eol/lf.txt', body.encode()), ('eol/crlf.txt', body.replace('\n', '\r\n').encode()),
                          ('eol/same_len.txt', body.replace('table', 'tabla').encode()),
                          ('eol/unique.txt', (body + 'one more line\n').encode())):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        hashed = []
        real = globals()['_hash_bytes']
        globals()['_hash_bytes'] = lambda data: hashed.append(len(data)) or real(data)
        try:
            eol = copy_groups(root, files=['eol/lf.txt', 'eol/crlf.txt', 'eol/same_len.txt', 'eol/unique.txt'],
                              declared={})
        finally:
            globals()['_hash_bytes'] = real
        checks.append(('an LF and a CRLF copy are one group, a same-length file with other content is not, and a file '
                       'whose length no other file shares is never hashed',
                       [(x['canonical'], x['copies']) for x in eol] == [('eol/lf.txt', ['eol/crlf.txt'])]
                       and len(hashed) == 3, (eol, hashed)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {name}" + ('' if ok else f'  [{extra!r}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--fixture-dir', action='append', default=[], metavar='NAME|PATH',
                    help='also treat this directory (a name anywhere, or a repository-relative path) as a fixture parent')
    ap.add_argument('--no-copies', action='store_true', help='skip the copy-group detection (no file hashing)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    if not args.repo.is_dir():
        ap.error(f'{args.repo}: not a directory')
    out = classify(args.repo, args.fixture_dir, copies=not args.no_copies)
    text = json.dumps(out, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    print(f"{out['stats']['fixtures']} fixture project(s); {out['stats']['copy_groups']} copy group(s) "
          f"({out['stats']['copies']} copies)", file=sys.stderr)
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
