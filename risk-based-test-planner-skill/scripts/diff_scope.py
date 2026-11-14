#!/usr/bin/env python3
"""Turn a git range or a unified diff into changed files, line ranges and functions (stdlib only).

    python diff_scope.py --repo <repo> [--base main] [--committed-only] --out scope.json
    python diff_scope.py --repo <repo> --diff change.diff --out scope.json
    python diff_scope.py --repo <repo> --last-merges 3 --out scope.json   # what the last 3 merges changed
    python diff_scope.py --repo <repo> --since-release [--release-tag 'core@*'] --out scope.json
    python diff_scope.py --self-test                                    # check the test/source classification

Git mode diffs the merge base of --base and HEAD against the working tree (uncommitted and
untracked files included) with rename detection; --committed-only diffs <merge base>...HEAD.
--diff reads a unified diff (git or plain) whose new side is the current state of --repo.
--last-merges N takes only the last N merges' own changes (each merge's diff against its first parent;
in a squash-merge history, the last N first-parent commits): direct commits between or after them are
left out, and line numbers are HEAD's. If a later commit edited the same lines, the files the merges
changed are diffed whole instead, with a warning.
--repo may be a subdirectory of the git repository (one package of a monorepo): git mode uses
`git diff --relative`, and a --diff whose paths start from the git top level is re-rooted at
--repo, so paths resolve against it and changes outside it are left out (with a warning). A
changed file that can't be found under --repo is named in `warnings` rather than passing as a
file with no functions.

Every file gets a status (added, modified, deleted, renamed), a kind (source, test, generated,
migration, vendored, types, template, markup, style, data, config, docs, binary, other) with the
reason, its added and modified lines as ranges of the new file, the points where lines were
deleted, and the functions those lines fall in. Web assets are never dropped as 'other':
`template` is a server-side template (a template extension such as .jinja or .erb, or HTML under
templates/, views/, layouts/ or partials/), `markup` another HTML page, `style` a stylesheet, and
`data` a data record the app loads (CSV and similar anywhere; JSON, YAML, XML or text under a data/
directory or in a directory of 50+ data files with no source; known config names stay config). Test code is decided by `is_test_path(path, root)`, the shared
rule other scripts should reuse: test file names count anywhere, and a directory named tests,
test, e2e or __tests__ counts only when it is not a product package (a manifest such as
package.json, pyproject.toml, Cargo.toml or go.mod together with shipped code such as src/), so a
workspace package named `e2e` is source while its own tests/ stay tests. `--self-test` checks it. Python functions come from `ast`; JavaScript and TypeScript use
a scanner that blanks strings, comments and regex literals and skips (multi-line, typed)
parameter lists and return types before looking for the body, so declarations, exported
async functions, arrow functions and function expressions assigned to variables or
properties, and class and object methods are found, while calls such as `new Error(...)` are
not. Other languages use a brace/indent heuristic. Each file records which method was used
(`heuristic` for both scanners); `functions(path, text)` picks the method for a file.
"""
from __future__ import annotations
import argparse, ast, bisect, json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
SOURCE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.go', '.rs', '.java', '.kt', '.kts', '.cs',
              '.swift', '.dart', '.rb', '.php', '.c', '.h', '.cc', '.cpp', '.hpp', '.scala', '.m', '.mm', '.vue',
              '.svelte', '.sql'}
CONFIG_EXT = {'.toml', '.cfg', '.ini', '.yaml', '.yml', '.json', '.lock', '.xml', '.gradle', '.properties', '.env'}
DOCS_EXT = {'.md', '.rst', '.txt', '.adoc'}
# Web assets: server-side templates, static HTML pages and stylesheets are product code a change can break, and data
# records the app loads are product data; each gets its own kind rather than 'other' or 'config'.
TEMPLATE_EXT = {'.jinja', '.jinja2', '.j2', '.njk', '.hbs', '.handlebars', '.mustache', '.erb', '.ejs', '.twig',
                '.liquid', '.tmpl', '.gohtml', '.pug', '.jade', '.haml', '.slim', '.cshtml', '.razor'}
HTML_EXT = {'.html', '.htm', '.xhtml'}
STYLE_EXT = {'.css', '.scss', '.sass', '.less', '.styl'}
TEMPLATE_DIRS = {'templates', 'template', 'views', 'layouts', 'partials', 'includes', '_includes', '_layouts'}
DATA_ONLY_EXT = {'.csv', '.tsv', '.jsonl', '.ndjson', '.geojson', '.parquet', '.avro', '.xlsx', '.ods'}
DATA_LIKE_EXT = {'.json', '.yaml', '.yml', '.xml', '.txt'}
DATA_DIRS = {'data', 'datasets', 'dataset', 'seeds', 'seed-data', 'records', 'content', '_data'}
DATA_TREE_MIN = 50   # files with a data extension in one directory, with no source file, make it a data tree
CONFIG_NAMES = re.compile(r'^(package|package-lock|tsconfig.*|jsconfig|composer|deno|renovate|lerna|nx|turbo|vercel|'
                          r'netlify|firebase|app|manifest|\.?eslintrc.*|\.?prettierrc.*|\.?babelrc|biome|angular|'
                          r'project|workspace|launch|settings|extensions|tasks|cspell|\.?stylelintrc.*|pyrightconfig|'
                          r'docker-compose.*|compose|mkdocs|\.?pre-commit-config|codecov|\.?travis|appveyor|'
                          r'azure-pipelines|bitbucket-pipelines|\.?gitlab-ci|openapi|swagger|schema|.*\.schema)'
                          r'\.(json|ya?ml|xml)$', re.I)
_DATA_TREE_CACHE: dict[str, bool] = {}
# Test files are recognised by name anywhere; test directories by name, unless the directory is a product package
# (see is_test_path). TEST_PATH is the purely textual union of both, kept for callers that have no tree to look at.
TEST_FILE = re.compile(r'(^|/)test_[^/]+\.py$|_test\.(py|go|rs)$|\.(test|spec|e2e)\.[cm]?[jt]sx?$|(^|/)conftest\.py$|'
                       r'Tests?\.(java|kt|cs|swift)$|_spec\.rb$|_test\.dart$')
TEST_DIR = re.compile(r'^(tests?|e2e|__tests__|androidTest|integration_test)$')
TEST_PATH = re.compile(TEST_FILE.pattern + r'|(^|/)(tests?|e2e|__tests__|androidTest|integration_test)/|(^|/)src/test/')
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'composer.json', 'pubspec.yaml', 'deno.json', 'deno.jsonc')
MANIFEST_GLOBS = ('*.csproj', '*.fsproj', '*.gemspec')
_PRODUCT_CACHE: dict[tuple[str, str], str | None] = {}
MIGRATION_PATH = re.compile(r'(^|/)(migrations?|alembic/versions|db/migrate|flyway|liquibase|changelogs?)/', re.I)
VENDOR_PATH = re.compile(r'(^|/)(vendor|third_party|third-party|node_modules|Pods|external)/')
GENERATED_PATH = re.compile(r'_pb2(_grpc)?\.pyi?$|\.pb\.go$|\.g\.dart$|\.freezed\.dart$|\.min\.[jc]ss?$|'
                            r'(^|/)(dist|build|generated|gen)/|\.generated\.|_generated\.|(^|/)package-lock\.json$')
GENERATED_HEAD = re.compile(r'generated by|@generated|do not edit|auto-?generated|code generated', re.I)
FUNC_START = [
    re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\('),
    re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>'),
    re.compile(r'^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\('),
    re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)'),
    re.compile(r'^\s*(?:(?:public|private|protected|internal|static|final|override|open|suspend|async|virtual|abstract)\s+)*'
               r'(?:fun\s+)?(?!(?:new|return|throw|await|yield|typeof|void|delete|case|else|in|of|instanceof)\s)'
               r'(?:[\w<>\[\],.?]+\s+)?([A-Za-z_]\w*)\s*\([^;]*\)\s*(?::\s*[\w<>\[\],.? ]+)?\s*(?:throws [\w., ]+)?\{?\s*$'),
    re.compile(r'^\s*def\s+([A-Za-z_]\w*[?!]?)'),
]
NOT_FUNCS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'else', 'with', 'elif', 'when', 'match', 'new', 'throw',
             'typeof', 'await', 'yield', 'super', 'this', 'function', 'do', 'try', 'finally', 'import', 'require'}
JS_EXT = ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte')
_ID = r'[A-Za-z_$][\w$]*'
JS_STARTS = [
    # function declarations and expressions: [export] [default] [async] function [*] name<T>(
    re.compile(rf'^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*({_ID})\s*(?:<[^(]*>)?\s*\('),
    # const name = [async] (...) => / x => / function (...) / <T>(...) =>, with an optional type annotation
    re.compile(rf'^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=]+)?=\s*(?:async\s+)?'
               rf'(?:function\b\s*\*?\s*(?:{_ID})?\s*(?:<[^(]*>)?\s*\(|<[^(=]*>\s*\(|\(|{_ID}\s*=>)'),
    # class and object methods: [modifiers] name<T>( ; the body check below rejects plain calls
    re.compile(rf'^\s*(?:(?:public|private|protected|static|readonly|async|override|abstract|get|set|declare)\s+)*'
               rf'\*?\s*(#?{_ID})\s*(?:<[^(]*>)?\s*\('),
    # object properties holding functions: name: [async] function ( / name: [async] (...) =>
    re.compile(rf'^\s*({_ID})\s*:\s*(?:async\s+)?(?:function\b\s*\*?\s*(?:{_ID})?\s*\(|\(|{_ID}\s*=>)'),
]
TS_TYPE_BODY = re.compile(r'(?:void|string|number|boolean|any|unknown|never|undefined|null|object|bigint|symbol)(?:\[\])*'
                          r'|[A-Z][\w$.]*\s*<.*>(?:\[\])*', re.S)
JS_NOT_FUNCS = NOT_FUNCS | {'describe', 'it', 'test', 'expect', 'beforeEach', 'afterEach', 'beforeAll', 'afterAll',
                            'case', 'default', 'delete', 'void', 'in', 'of', 'instanceof', 'let', 'const', 'var',
                            'export', 'async', 'static'}


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8',
                         errors='replace')
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {res.stderr.strip()}')
    return res.stdout


def changes_of_commits(repo: Path, commits: list[str]) -> tuple[str, str, str | None]:
    """The diff of just these commits' own changes (each one's first-parent diff, `C^1..C`), in HEAD's line numbers:
    a scratch index starts from HEAD, each commit's change is reverse-applied to it, and the resulting tree is
    diffed against HEAD. Commits outside the list (direct commits before or after a merge) are left out. When a
    later commit touched the same lines of a file so the reversal does not apply, that file alone falls back to
    the oldest commit's parent...HEAD, with a warning naming it. (diff text, command, warning or None)"""
    import os, tempfile
    top = Path(git(repo, 'rev-parse', '--show-toplevel').strip())
    objects = Path(git(repo, 'rev-parse', '--git-common-dir').strip())
    objects = (objects if objects.is_absolute() else (repo / objects)).resolve() / 'objects'
    base = commits[-1] + '^1'
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix='diff-scope-') as d:
        # a scratch index and object store (the repository's objects as an alternate): nothing is written to the repo
        (Path(d) / 'objects').mkdir()
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(d) / 'index'), 'GIT_OBJECT_DIRECTORY': str(Path(d) / 'objects'),
               'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(objects)}

        def g(where, *args, stdin=None):
            res = subprocess.run(['git', '-C', str(where), *args], input=stdin, capture_output=True, env=env)
            if res.returncode and args[0] not in ('apply',):
                raise SystemExit(f'git {" ".join(args)} failed: {res.stderr.decode("utf-8", "replace").strip()}')
            return res
        g(top, 'read-tree', 'HEAD')
        for c in commits:
            patch = g(top, 'diff', '--binary', '--full-index', '--no-color', '--no-ext-diff', '--no-renames', f'{c}^1', c).stdout
            for chunk in re.split(rb'(?m)^(?=diff --git )', patch):
                m = re.match(rb'diff --git a/(.+?) b/(.+)\r?\n', chunk)
                if not m:
                    continue
                path = m.group(2).decode('utf-8', 'replace').strip('"')
                if path in failed:
                    continue
                if g(top, 'apply', '--cached', '-R', '--whitespace=nowarn', stdin=chunk).returncode:
                    failed.append(path)
                    g(top, 'reset', '-q', 'HEAD', '--', f':(top){path}')   # this file falls back to base...HEAD
        # the scratch index (HEAD without the commits' changes) against HEAD, reversed: HEAD's line numbers
        text = g(repo, 'diff', '-R', '--cached', '-M', '--relative', '--no-color', '--no-ext-diff', 'HEAD').stdout.decode(
            'utf-8', 'replace')
    command = (f'git diff -M --relative <HEAD without the changes of {len(commits)} commit(s)> HEAD '
               f'(commits {", ".join(c[:9] for c in commits)})')
    if not failed:
        return text, command, None
    text += git(repo, 'diff', '-M', '--relative', '--no-color', '--no-ext-diff', f'{base}...HEAD', '--',
                *[f':(top){p}' for p in failed])
    return text, command + f', plus {base}...HEAD for {len(failed)} file(s)', (
        f'later commits changed the same lines as the selected commit(s) in {len(failed)} file(s) '
        f'({", ".join(failed[:5])}{", ..." if len(failed) > 5 else ""}), so their changes could not be separated '
        f'there: those files are diffed from {base} to HEAD, which also includes other commits\' edits to them')


def strip_prefix(p: str) -> str | None:
    p = p.split('\t')[0].strip()
    if p == '/dev/null':
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p


def strip_one(old: str | None, new: str | None) -> tuple[str | None, str | None]:
    """Drop the a/ b/ (or old/ new/) prefix like `patch -p1`; keep -p0 paths whose first parts match."""
    heads = [x.split('/', 1)[0] for x in (old, new) if x]
    if not heads or any('/' not in x for x in (old, new) if x):
        return old, new
    if len(heads) == 2 and heads[0] == heads[1] and heads[0] not in ('a', 'b'):
        return old, new
    if len(heads) == 1 and heads[0] not in ('a', 'b'):
        return old, new
    cut = lambda x: x.split('/', 1)[1] if x else x  # noqa: E731
    return cut(old), cut(new)


def new_entry() -> dict:
    return {'path': None, 'old_path': None, 'status': 'modified', 'added': [], 'deleted_at': [], 'binary': False,
            'lines_added': 0, 'lines_deleted': 0}


def parse_diff(text: str) -> list[dict]:
    """Parse git or plain unified diffs; hunk line counts decide where each hunk ends."""
    files: list[dict] = []
    cur, new_line, old_left, new_left = None, 0, 0, 0
    for raw in text.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith('+'):
                cur['added'].append(new_line)
                cur['lines_added'] += 1
                new_line += 1
                new_left -= 1
            elif raw.startswith('-'):
                if not cur['deleted_at'] or cur['deleted_at'][-1] != new_line:
                    cur['deleted_at'].append(new_line)
                cur['lines_deleted'] += 1
                old_left -= 1
            elif raw.startswith('\\'):
                pass                                    # "\ No newline at end of file"
            else:
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith('diff --git '):
            cur = new_entry()
            m = re.match(r'diff --git (\S+) (\S+)$', raw)
            if m:
                cur['old_path'], cur['path'] = m.group(1), m.group(2)
            cur['git'] = True
            files.append(cur)
        elif raw.startswith('--- '):
            if cur is None or not cur.get('git') or cur.get('hunks'):
                cur = new_entry()
                files.append(cur)
            cur['old_path'] = strip_prefix(raw[4:])
            if cur['old_path'] is None:
                cur['status'] = 'added'
        elif raw.startswith('+++ ') and cur is not None:
            new = strip_prefix(raw[4:])
            if new is None:
                cur['status'], cur['path'] = 'deleted', cur['old_path']
            else:
                cur['path'] = new
        elif cur is None:
            continue
        elif raw.startswith('new file mode'):
            cur['status'] = 'added'
        elif raw.startswith('deleted file mode'):
            cur['status'] = 'deleted'
        elif raw.startswith('rename from '):
            cur['old_path'], cur['status'] = 'a/' + raw[12:], 'renamed'
        elif raw.startswith('rename to '):
            cur['path'] = 'b/' + raw[10:]
        elif raw.startswith('Binary files ') or raw.startswith('GIT binary patch'):
            cur['binary'] = True
        elif (m := HUNK.match(raw)):
            cur['hunks'] = True
            old_left = int(m.group(2) if m.group(2) is not None else 1)
            new_left = int(m.group(4) if m.group(4) is not None else 1)
            new_line = int(m.group(3))
            if new_left == 0:                           # pure deletion: the gap is after line c
                new_line += 1
    for f in files:
        f.pop('git', None)
        old, new = strip_one(f['old_path'] if f['status'] != 'added' else None, f['path'])
        f['old_path'], f['path'] = old, new
        f.pop('hunks', None)
        if f['status'] == 'modified' and f['old_path'] and f['path'] and f['old_path'] != f['path']:
            f['status'] = 'renamed'
        if f['status'] not in ('renamed', 'deleted'):
            f['old_path'] = None
    return [f for f in files if f['path']]


def ranges(lines: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(set(lines)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def product_package(root: Path | str | None, rel_dir: str) -> str | None:
    """Why the directory rel_dir (relative to root) is a product package rather than a test directory, or None.

    A directory is a product package when it holds a package manifest (package.json, pyproject.toml, setup.py,
    Cargo.toml, go.mod, pom.xml, build.gradle, *.csproj, ...) AND ships code: a src/ directory, a package.json
    with main/module/exports/bin, a Cargo.toml with [lib] or [[bin]], top-level non-test .go files beside a go.mod,
    or the Python package a pyproject.toml/setup.py/setup.cfg names (a sub-directory with __init__.py). A manifest alone does not make one: a workspace package holding only spec files and a
    runner config (a Playwright `e2e/` package) is the known test layout and stays a test directory.
    Without a root (no tree to look at) nothing is a product package."""
    if root is None:
        return None
    key = (str(root), rel_dir)
    if key in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[key]
    d = Path(root) / rel_dir
    why = None
    try:
        found = [m for m in MANIFESTS if (d / m).is_file()] if d.is_dir() else []
        if d.is_dir() and not found:
            found = [p.name for g in MANIFEST_GLOBS for p in d.glob(g)][:1]
        if found:
            m = found[0]
            if (d / 'src').is_dir():
                why = f'{m} and a src/ directory'
            elif m == 'package.json':
                try:
                    pkg = json.loads((d / m).read_text(encoding='utf-8', errors='replace'))
                except (ValueError, OSError):
                    pkg = {}
                fields = [k for k in ('main', 'module', 'exports', 'bin') if isinstance(pkg, dict) and pkg.get(k)]
                if fields:
                    why = f'package.json declaring {"/".join(fields)}'
            elif m == 'Cargo.toml':
                text = (d / m).read_text(encoding='utf-8', errors='replace')
                if re.search(r'^\s*\[(lib|\[bin\])\]', text, re.M):
                    why = 'Cargo.toml with a [lib] or [[bin]] target'
            elif m == 'go.mod':
                if any(not p.name.endswith('_test.go') for p in d.glob('*.go')):
                    why = 'go.mod and non-test .go files'
            elif m in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                text = (d / m).read_text(encoding='utf-8', errors='replace')
                names = {re.sub(r'[-.]', '_', n).lower()
                         for n in re.findall(r'''(?m)^\s*name\s*[=:]\s*["']?([\w.-]+)''', text)}
                pkgs = [p.name for p in d.iterdir() if p.is_dir() and p.name.lower() in names
                        and (p / '__init__.py').is_file()]
                if pkgs:
                    why = f'{m} and its Python package {pkgs[0]}/'
    except OSError:
        why = None
    if why:
        why = f'{rel_dir}/ is a product package ({why})'
    _PRODUCT_CACHE[key] = why
    return why


def is_test_path(path: str, root: Path | str | None = None) -> tuple[bool, str]:
    """Whether path (relative to root, '/'-separated) is test code, and why.

    Test file names (test_*.py, *_test.go, *.test.ts, *.spec.js, *.e2e.ts, conftest.py, FooTest.java, *_spec.rb,
    *_test.dart) are tests wherever they are. A directory named tests, test, e2e, __tests__, androidTest,
    integration_test (or src/test) makes what is under it tests, unless that directory is a product package (see
    product_package): a workspace package named `e2e` with its own package.json and src/ is product code, while its
    own tests/ directory is still tests. Give root whenever a tree is available; without it only names count."""
    path = path.replace('\\', '/')
    if TEST_FILE.search(path):
        return True, 'test file name'
    parts = path.split('/')[:-1]
    product = None
    for i, seg in enumerate(parts):
        if TEST_DIR.match(seg):   # also covers src/test (Maven, Gradle)
            rel_dir = '/'.join(parts[:i + 1])
            why = product_package(root, rel_dir)
            if why:
                product = why
                continue
            return True, f'under the test directory {rel_dir}/'
    return False, product or ''


def data_tree(root: Path | str | None, rel_dir: str) -> bool:
    """True when the directory rel_dir under root holds at least DATA_TREE_MIN data files and no source file."""
    if root is None:
        return False
    key = f'{root}\0{rel_dir}'
    if key not in _DATA_TREE_CACHE:
        try:
            names = [e.name for e in __import__('os').scandir(Path(root) / rel_dir) if e.is_file()]
        except OSError:
            names = []
        sfx = [Path(n).suffix.lower() for n in names]
        n_data = sum(s in DATA_ONLY_EXT or s in DATA_LIKE_EXT for s in sfx)
        _DATA_TREE_CACHE[key] = n_data >= DATA_TREE_MIN and not any(s in SOURCE_EXT for s in sfx)
    return _DATA_TREE_CACHE[key]


def web_asset_kind(path: str, root: Path | str | None = None) -> tuple[str, str] | None:
    """(kind, reason) for a web asset or data file: 'template' (a server-side template: a template extension, or HTML
    under templates/, views/, layouts/ or partials/), 'markup' (another HTML page), 'style' (a stylesheet) or 'data'
    (CSV and similar anywhere; JSON, YAML, XML or text under a data/ directory or in a directory of 50+ data files with
    no source; known configuration file names stay config). None for anything else."""
    p = Path(path)
    ext, name = p.suffix.lower(), p.name
    dirs = [d.lower() for d in path.split('/')[:-1]]
    if ext in TEMPLATE_EXT or name.lower().endswith('.blade.php'):
        return 'template', 'server-side template'
    if ext in HTML_EXT:
        if any(d in TEMPLATE_DIRS for d in dirs):
            return 'template', 'server-side template (HTML under a template directory)'
        return 'markup', 'HTML page'
    if ext in STYLE_EXT:
        return 'style', 'stylesheet'
    if ext in DATA_ONLY_EXT:
        return 'data', 'data file'
    if ext in DATA_LIKE_EXT and not CONFIG_NAMES.match(name):
        if any(d in DATA_DIRS for d in dirs):
            return 'data', 'data file in a data directory'
        if data_tree(root, p.parent.as_posix()):
            return 'data', f'data file in a data tree ({p.parent.as_posix()}/ holds {DATA_TREE_MIN}+ data files, no source)'
    return None


def classify(path: str, head: str, binary: bool, root: Path | str | None = None) -> tuple[str, str]:
    """(kind, reason) for a changed file. Pass root (the directory paths are relative to) so a product package whose
    directory happens to be named e2e or tests is not taken for test code (see is_test_path)."""
    ext = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if binary:
        return 'binary', 'binary file'
    if VENDOR_PATH.search(path):
        return 'vendored', 'vendored or third-party directory'
    if GENERATED_PATH.search(path):
        return 'generated', 'generated-file name or directory'
    if head and GENERATED_HEAD.search(head):
        return 'generated', 'file header says it is generated'
    if MIGRATION_PATH.search(path):
        return 'migration', 'database migration directory'
    is_test, why = is_test_path(path, root)
    if is_test:
        return 'test', 'test file or directory'
    note = f' ({why})' if why else ''
    if ext == '.pyi' or path.endswith('.d.ts'):
        return 'types', 'type declarations only'
    web = web_asset_kind(path, root)
    if web:
        return web[0], web[1] + note
    if ext in DOCS_EXT or path.startswith('docs/'):
        return 'docs', 'documentation'
    if ext in CONFIG_EXT or name in ('dockerfile', 'makefile', '.gitignore', '.gitattributes'):
        return 'config', 'configuration or build file' + note
    if ext in SOURCE_EXT:
        return 'source', 'source code' + note
    return 'other', f'unrecognised extension {ext or "(none)"}'


def py_functions(text: str) -> list[dict] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                out.append({'name': child.name, 'qualname': prefix + child.name, 'start': start, 'end': child.end_lineno})
                walk(child, prefix + child.name + '.<locals>.')
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + '.')
    walk(tree, '')
    return out


def heuristic_functions(text: str) -> list[dict]:
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        for rx in FUNC_START:
            m = rx.match(line)
            if not m or m.group(1) in NOT_FUNCS:
                continue
            start = i + 1
            if line.rstrip().endswith(':') or rx.pattern.startswith(r'^\s*def'):   # indentation or Ruby def ... end
                indent = len(line) - len(line.lstrip())
                end = start
                for j in range(i + 1, len(lines)):
                    s = lines[j]
                    if s.strip() and len(s) - len(s.lstrip()) <= indent:
                        end = j + (1 if s.strip() == 'end' else 0)
                        break
                    end = j + 1
            else:
                depth, opened, end = 0, False, start
                for j in range(i, min(len(lines), i + 2000)):
                    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//.*$', '', lines[j])
                    depth += code.count('{') - code.count('}')
                    opened = opened or '{' in code
                    end = j + 1
                    if opened and depth <= 0:
                        break
                    if not opened and j > i + 3:
                        break
                if not opened:
                    continue
            recv = GO_METHOD.match(line)   # Go: (w *IndexWriter) EndCancelled -> IndexWriter.EndCancelled
            qual = f'{recv.group(1)}.{m.group(1)}' if recv and recv.group(2) == m.group(1) else m.group(1)
            out.append({'name': m.group(1), 'qualname': qual, 'start': start, 'end': end})
            break
    return out


GO_METHOD = re.compile(r'^\s*func\s+\(\s*(?:\w+\s+)?\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)')


def js_blank(text: str) -> str:
    """The text with comments, strings, template literals and regex literals replaced by spaces (newlines kept), so
    brackets can be counted. Approximate: a / after an operator or opening bracket starts a regex."""
    out, i, n = list(text), 0, len(text)
    last = ''   # last significant code character
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if c == '/' and nxt == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
        elif c == '/' and nxt == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
        elif c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == '\\':
                    j += 1
                elif text[j] == '\n' and c != '`':
                    break
                j += 1
            j = min(j + 1, n)
        elif c == '/' and (last == '' or last in '(,=:[!&|?{};+-*%<>~^'):
            j, in_class = i + 1, False
            while j < n and text[j] != '\n':
                if text[j] == '\\':
                    j += 1
                elif text[j] == '[':
                    in_class = True
                elif text[j] == ']':
                    in_class = False
                elif text[j] == '/' and not in_class:
                    break
                j += 1
            j = min(j + 1, n)
        else:
            if not c.isspace():
                last = c
            i += 1
            continue
        for k in range(i, j):
            if out[k] != '\n':
                out[k] = ' '
        last = 'x'   # a literal ends like an operand
        i = j
    return ''.join(out)


def js_body(code: str, pos: int) -> tuple[int, int] | None:
    """Given the offset of a parameter list's '(' (or of a lone arrow parameter), return (body start, body end)
    offsets, or None when what follows is not a function body (a call, a signature without a body)."""
    n = len(code)
    if code[pos] == '(':
        depth, i = 0, pos
        while i < n:
            if code[i] in '([{':
                depth += 1
            elif code[i] in ')]}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i >= n:
            return None
        i += 1
    else:   # x => ...
        m = re.compile(r'[A-Za-z_$][\w$]*').match(code, pos)
        if not m:
            return None
        i = m.end()
    # optional return type annotation, then an optional arrow, then the body
    j = i
    while j < n and code[j].isspace():
        j += 1
    if j < n and code[j] == ':':
        j += 1
        depth, prev = 0, ':'
        while j < n:
            c = code[j]
            if depth == 0 and c == '{' and prev not in ':|&<,(=?[':
                break                                   # a '{' after a complete type is the body
            if depth == 0 and code.startswith('=>', j):
                break
            if depth == 0 and c in ';)':
                return None
            if c in '([{<':
                depth += 1
            elif c in ')]}>' and not (c == '>' and j and code[j - 1] == '='):
                depth -= 1
            if not c.isspace():
                prev = c
            j += 1
    while j < n and code[j].isspace():
        j += 1
    arrow = code.startswith('=>', j)
    if arrow:
        j += 2
        while j < n and code[j].isspace():
            j += 1
    if j < n and code[j] == '{':
        depth, k = 0, j
        while k < n:
            if code[k] == '{':
                depth += 1
            elif code[k] == '}':
                depth -= 1
                if depth == 0:
                    return j, k
            k += 1
        return j, n - 1
    if not arrow or j >= n:
        return None
    depth, k = 0, j   # expression body: ends at ; or , or a closing bracket at depth 0, or a line break at depth 0
    while k < n:
        c = code[k]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                return j, k - 1
            depth -= 1
        elif depth == 0 and c in ';,':
            return j, k
        elif depth == 0 and c == '\n':
            rest = code[k:].lstrip()
            if not rest or rest[0] not in '.?:+-*/&|=<>':
                return j, k - 1
        k += 1
    return j, n - 1


def js_functions(text: str) -> list[dict]:
    """Functions in JavaScript/TypeScript: declarations, arrow functions and function expressions assigned to
    variables or properties, class and object methods. Multi-line parameter lists, typed parameters (object type
    literals included) and return types are skipped before the body brace is looked for."""
    code = js_blank(text)
    starts, off = [], 0
    for line in code.split('\n'):
        starts.append(off)
        off += len(line) + 1
    out = []
    for idx, line in enumerate(code.split('\n')):
        for rx in JS_STARTS:
            m = rx.match(line)
            if not m or m.group(1) in JS_NOT_FUNCS:
                continue
            if line[m.end() - 1] == '(':
                pos = starts[idx] + m.end() - 1
            else:   # x => ...: the parameter is the identifier before =>
                am = re.search(r'([A-Za-z_$][\w$]*)\s*=>\s*$', line[:m.end()])
                if not am:
                    continue
                pos = starts[idx] + am.start(1)
            body = js_body(code, pos)
            if body is None:
                continue
            if code[body[0]] != '{' and TS_TYPE_BODY.fullmatch(code[body[0]:body[1] + 1].strip().rstrip(',;')):
                continue   # `cb: (err: Error) => void` is a function type in a signature, not a function
            end = bisect.bisect_right(starts, body[1])
            out.append({'name': m.group(1), 'qualname': m.group(1), 'start': idx + 1, 'end': max(end, idx + 1)})
            break
    return out


def functions(path: str, text: str) -> tuple[list[dict], str]:
    """(functions, method) for a file: Python's ast, else a JavaScript/TypeScript scanner, else the brace/indent
    heuristic. Use this rather than calling the parsers directly, so every language gets its best method."""
    if path.endswith('.py'):
        funcs = py_functions(text)
        if funcs is not None:
            return funcs, 'ast'
    if path.lower().endswith(JS_EXT):
        return js_functions(text), 'heuristic'
    return heuristic_functions(text), 'heuristic'


def attach_functions(entry: dict, text: str | None) -> None:
    entry['functions'], entry['function_method'] = [], 'none'
    if text is None or entry['status'] == 'deleted' or entry['kind'] in ('binary', 'docs', 'config', 'data', 'style', 'markup', 'template'):
        return
    funcs, entry['function_method'] = functions(entry['path'], text)
    changed = [n for a, b in entry['ranges'] for n in range(a, b + 1)]
    for f in funcs:
        inner = [g for g in funcs if g is not f and f['start'] <= g['start'] and g['end'] <= f['end']]
        own = [n for n in changed if f['start'] <= n <= f['end'] and not any(g['start'] <= n <= g['end'] for g in inner)]
        dels = [n for n in entry['deleted_at'] if f['start'] <= n <= f['end'] + 1
                and not any(g['start'] <= n <= g['end'] for g in inner)]
        if own or dels:
            entry['functions'].append({**f, 'changed': ranges(own), 'deletions': len(dels)})


def scope_from_diff(text: str, repo: Path) -> list[dict]:
    out = []
    for f in parse_diff(text):
        p = repo / f['path']
        body = p.read_text(encoding='utf-8', errors='replace') if f['status'] != 'deleted' and p.is_file() else None
        head = '\n'.join((body or '').splitlines()[:5])
        kind, why = classify(f['path'], head, f['binary'], repo)
        entry = {'path': f['path'], 'old_path': f['old_path'], 'status': f['status'], 'kind': kind, 'kind_reason': why,
                 'ranges': ranges(f['added']), 'deleted_at': f['deleted_at'], 'lines_added': f['lines_added'],
                 'lines_deleted': f['lines_deleted'], 'present': body is not None}
        attach_functions(entry, body)
        out.append(entry)
    return out


def git_prefix(repo: Path) -> str:
    """repo's path inside its git work tree ('libs/core/'), '' at the top or outside git."""
    try:
        res = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-prefix'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ''
    return res.stdout.strip() if res.returncode == 0 else ''


def relative_diff(text: str, prefix: str) -> tuple[str, int]:
    """A diff whose paths are relative to the git top level, re-rooted at prefix when its paths start with it (as
    `git diff --relative` would give). Diffs already relative to --repo are returned unchanged. (text, files left out)"""
    files = parse_diff(text)
    if not files or not any((f['path'] or '').startswith(prefix) or (f['old_path'] or '').startswith(prefix)
                            for f in files):
        return text, 0
    out, keep, outside = [], True, 0
    marker = re.compile(r'^(diff --git |--- |\+\+\+ |rename from |rename to |copy from |copy to )')
    for line in text.splitlines(keepends=True):
        if line.startswith('diff --git '):
            m = re.match(r'diff --git (\S+) (\S+)', line)
            paths = [re.sub(r'^[ab]/', '', strip_prefix(x) or '') for x in m.groups()] if m else []
            keep = any(p.startswith(prefix) for p in paths)
            outside += not keep
        if not keep:
            continue
        if marker.match(line):
            line = re.sub(r'(^|\s)(a/|b/)?' + re.escape(prefix), r'\1\2', line)
        out.append(line)
    return ''.join(out), outside


RELEASE_TAG = re.compile(r'^(?:(?:@[\w.-]+/)?[\w.-]+?(?:@|-v|-|_v?))?v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?$')


def newest_release_tag(repo: Path, glob: str | None = None) -> tuple[str | None, str]:
    """The newest release-shaped tag that HEAD contains (by the tagged commit's date), and a note.
    Pre-releases (1.2.0-beta.1) are skipped when a final release is present."""
    args = ['tag', '--merged', 'HEAD', '--sort=-creatordate', '--format=%(refname:short)']
    if glob:
        args.append(glob)
    tags = [t for t in git(repo, *args).split() if RELEASE_TAG.match(t)]
    if not tags:
        return None, (f'no release tag matching {glob!r} is an ancestor of HEAD' if glob else
                      'no release-shaped tag (v1.2.3, name@1.2.3, ...) is an ancestor of HEAD')
    finals = [t for t in tags if not re.search(r'\d-[\w.]+$', t)]
    pick = (finals or tags)[0]
    note = ''
    if not glob:
        names = {re.sub(r'(@|-v|-|_v?)?v?\d+\.\d+\.\d+.*$', '', t) for t in tags}
        if len(names) > 1:
            note = (f'{pick} is the newest of {len(names)} release tag families ({", ".join(sorted(n or "(plain)" for n in names)[:6])}'
                    f'{", ..." if len(names) > 6 else ""}); pass --release-tag to pick one package')
    return pick, note


def self_test() -> int:
    """Check the test/source classification on a synthetic monorepo."""
    import tempfile
    files = {
        'packages/e2e/package.json': '{"name": "e2e", "exports": {".": "./dist/index.js"}}',
        'packages/e2e/src/runner.ts': 'export function run() {}\n',
        'packages/e2e/schema/config.json': '{}',
        'packages/e2e/tests/unit/runner.test.ts': "test('runs', () => {})\n",
        'packages/e2e/tests/helpers/repo.ts': 'export const repo = 1\n',
        'packages/kit/package.json': '{"name": "kit", "main": "index.js"}',
        'packages/kit/index.js': 'module.exports = {}\n',
        'packages/kit/e2e/package.json': '{"private": true, "devDependencies": {"runner": "1.0.0"}}',
        'packages/kit/e2e/checkout.ts': "test('checkout', () => {})\n",
        'apps/shop/e2e/cart.ts': "test('cart', () => {})\n",
        'apps/shop/bench/scenarios.e2e.ts': "test('bench', () => {})\n",
        'apps/shop/src/cart.ts': 'export const cart = []\n',
        'services/tests/pyproject.toml': '[project]\nname = "tests"\n',
        'services/tests/conftest.py': '',
        'services/tests/harness/__init__.py': '',
        'services/e2e/pyproject.toml': '[project]\nname = "ledger-sync"\n',
        'services/e2e/ledger_sync/__init__.py': '',
        'tools/e2e/go.mod': 'module example.test/e2e\n',
        'tools/e2e/client.go': 'package e2e\n',
        'tools/e2e/client_test.go': 'package e2e\n',
        'probe/e2e/go.mod': 'module example.test/probe\n',
        'probe/e2e/flow_test.go': 'package e2e\n',
        'probe/e2e/support/steps.go': 'package support\n',
        'crates/tests/Cargo.toml': '[package]\nname = "tests"\n\n[lib]\npath = "lib.rs"\n',
        'crates/tests/lib.rs': 'pub fn f() {}\n',
        'lib/test/java/AppTest.java': 'class AppTest {}\n',
        'lib/src/test/java/Helper.java': 'class Helper {}\n',
        # a server-rendered recipe site: Jinja templates, a stylesheet, a static page and a tree of JSON records
        'site/app.py': 'from flask import Flask\n',
        'site/templates/recipe.html': '{% extends "base.html" %}\n',
        'site/templates/partials/nav.jinja': '<nav></nav>\n',
        'site/static/site.css': 'body {}\n',
        'site/static/offline.html': '<html></html>\n',
        'site/config.json': '{}',
        'site/exports/top.csv': 'a,b\n',
        'site/data/units.yaml': 'g: gram\n',
    }
    files.update({f'site/recipes/r{i:03d}.json': '{"name": "soup"}' for i in range(60)})
    expect = {   # path -> (kind, reason fragment)
        'packages/e2e/src/runner.ts': ('source', 'packages/e2e/ is a product package (package.json and a src/'),
        'packages/e2e/schema/config.json': ('config', 'product package'),
        'packages/e2e/tests/unit/runner.test.ts': ('test', ''),
        'packages/e2e/tests/helpers/repo.ts': ('test', ''),
        'packages/kit/index.js': ('source', ''),
        'packages/kit/e2e/checkout.ts': ('test', ''),          # a spec-only workspace package: the known test layout
        'apps/shop/e2e/cart.ts': ('test', ''),
        'apps/shop/bench/scenarios.e2e.ts': ('test', ''),
        'apps/shop/src/cart.ts': ('source', ''),
        'services/tests/conftest.py': ('test', ''),
        'services/tests/harness/__init__.py': ('test', ''),   # a test-support distribution, not the product
        'services/e2e/ledger_sync/__init__.py': ('source', 'its Python package ledger_sync/'),
        'tools/e2e/client.go': ('source', 'go.mod and non-test .go files'),
        'tools/e2e/client_test.go': ('test', ''),
        'probe/e2e/support/steps.go': ('test', ''),
        'crates/tests/lib.rs': ('source', 'Cargo.toml with a [lib]'),
        'lib/test/java/AppTest.java': ('test', ''),
        'lib/src/test/java/Helper.java': ('test', ''),
        'site/templates/recipe.html': ('template', 'server-side template'),
        'site/templates/partials/nav.jinja': ('template', 'server-side template'),
        'site/static/site.css': ('style', 'stylesheet'),
        'site/static/offline.html': ('markup', 'HTML page'),
        'site/config.json': ('config', ''),
        'site/exports/top.csv': ('data', 'data file'),
        'site/data/units.yaml': ('data', 'data directory'),
        'site/recipes/r007.json': ('data', 'data tree'),
    }
    failed = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding='utf-8')
        for rel, (kind, frag) in expect.items():
            got, why = classify(rel, '', False, root)
            ok = got == kind and frag in why
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'} {rel}: {got} ({why})" + ('' if ok else f'  [expected {kind}, "{frag}"]'))
        got, _ = classify('packages/e2e/src/runner.ts', '', False)
        ok = got == 'test'
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} without a root only names count: packages/e2e/src/runner.ts is {got}")
    with tempfile.TemporaryDirectory() as d:   # --since-release on a changesets-style history
        repo = Path(d)
        env_git = ['-c', 'user.name=t', '-c', 'user.email=t@example.test', '-c', 'commit.gpgsign=false',
                   '-c', 'tag.gpgsign=false']
        run = lambda *a, date='2026-01-01T00:00:00Z': subprocess.run(  # noqa: E731
            ['git', '-C', d, *env_git, *a], capture_output=True, text=True,
            env={**__import__('os').environ, 'GIT_COMMITTER_DATE': date, 'GIT_AUTHOR_DATE': date})
        run('init', '-q')
        steps = [('one', ['core@1.0.0', 'kit@0.3.0']), ('two', ['core@1.1.0-beta.0']), ('three', ['kit@0.4.0']),
                 ('four', [])]
        for i, (msg, tags) in enumerate(steps):
            (repo / f'{msg}.txt').write_text(msg, encoding='utf-8')
            run('add', f'{msg}.txt')
            run('commit', '-q', '-m', msg, date=f'2026-01-0{i + 1}T00:00:00Z')
            for t in tags:
                run('tag', '-a', t, '-m', t, date=f'2026-01-0{i + 1}T00:00:00Z')
        for glob, want in ((None, 'kit@0.4.0'), ('core@*', 'core@1.0.0')):
            got, _ = newest_release_tag(repo, glob)
            ok = got == want
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'} --since-release {glob or ''}: {got}" + ('' if ok else f'  [expected {want}]'))
    go_src = ('package report\n\nfunc (w *IndexWriter) EndCancelled(seq int) error {\n\treturn nil\n}\n\n'
              'func (r Reader[T]) EndCancelled() {\n}\n\nfunc helper() {\n}\n')
    got = [f['qualname'] for f in functions('report/index.go', go_src)[0]]
    ok = got == ['IndexWriter.EndCancelled', 'Reader.EndCancelled', 'helper']
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} Go methods are qualified by their receiver type: {got}")
    with tempfile.TemporaryDirectory() as d:   # --last-merges takes the merges' changes, not later direct commits
        repo = Path(d)
        env_git = ['-c', 'user.name=t', '-c', 'user.email=t@example.test', '-c', 'commit.gpgsign=false',
                   '-c', 'init.defaultBranch=main', '-c', 'core.autocrlf=false']
        run = lambda *a: subprocess.run(['git', '-C', d, *env_git, *a], capture_output=True, text=True)  # noqa: E731
        run('init', '-q')
        lines = [f'line {i}' for i in range(1, 11)]
        (repo / 'fares.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        (repo / 'notes.txt').write_text('notes\n', encoding='utf-8')
        run('add', '.'); run('commit', '-q', '-m', 'base')
        run('checkout', '-q', '-b', 'feature')
        lines[4] = 'line 5 changed by the feature'
        (repo / 'fares.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        run('commit', '-q', '-am', 'feature change')
        run('checkout', '-q', 'main')
        (repo / 'notes.txt').write_text('notes\nbefore the merge\n', encoding='utf-8')
        run('commit', '-q', '-am', 'direct commit before the merge')
        run('merge', '-q', '--no-ff', '-m', 'merge feature', 'feature')
        (repo / 'notes.txt').write_text('notes\nbefore the merge\nafter the merge\n', encoding='utf-8')
        (repo / 'fares.txt').write_text('head 1\nhead 2\nhead 3\n' + '\n'.join(lines) + '\n', encoding='utf-8')
        run('commit', '-q', '-am', 'direct commit after the merge')
        out = repo / 'scope.json'
        try:
            main(['--repo', d, '--last-merges', '1', '--out', str(out)])
            res = json.loads(out.read_text(encoding='utf-8'))
            got = [(f['path'], f['ranges']) for f in res['files']]
        except (SystemExit, Exception) as e:   # noqa: BLE001
            got = repr(e)
        ok = got == [('fares.txt', [[8, 8]])]
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} --last-merges 1 is the merge's own change, in HEAD's line numbers: {got}"
              + ('' if ok else "  [expected [('fares.txt', [[8, 8]])]]"))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--base', default='main', help='branch or ref to diff against (git mode; default main)')
    ap.add_argument('--diff', type=Path, help='read this unified diff instead of running git')
    ap.add_argument('--committed-only', action='store_true', help='git mode: <merge base>...HEAD, no working-tree changes')
    ap.add_argument('--last-merges', type=int, metavar='N',
                    help='git mode: the changes of the last N merges into HEAD (first-parent history), committed only: '
                         "each merge's first-parent diff, in HEAD's line numbers, without direct commits made between "
                         'or after them. In a squash-merge history (no merge commits) the last N first-parent commits '
                         'are used')
    ap.add_argument('--since-release', action='store_true',
                    help='git mode: diff from the newest release tag that HEAD contains (v1.2.3, 1.2.3, name@1.2.3, '
                         '@scope/name@1.2.3, name-v1.2.3; the shapes changesets, semantic-release and lerna write), '
                         'committed only')
    ap.add_argument('--release-tag', metavar='GLOB',
                    help="with --since-release: only tags matching this glob, e.g. 'core@*' for one package of a "
                         'changesets monorepo')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    warnings = []
    release_tag = None
    if args.release_tag and not args.since_release:
        ap.error('--release-tag needs --since-release')
    if args.since_release:
        if args.diff or args.last_merges is not None:
            ap.error('--since-release is a git mode option; it cannot be combined with --diff or --last-merges')
        release_tag, why = newest_release_tag(repo, args.release_tag)
        if not release_tag:
            raise SystemExit(f'--since-release: {why}')
        args.base = release_tag
        args.committed_only = True
        if why:
            warnings.append(f'--since-release: {why}')
    if args.last_merges is not None:
        if args.diff:
            ap.error('--last-merges is a git mode option; it cannot be combined with --diff')
        if args.last_merges < 1:
            ap.error('--last-merges takes a positive number')
        merges = git(repo, 'rev-list', '--first-parent', '--merges', '-n', str(args.last_merges), 'HEAD').split()
        if len(merges) == args.last_merges:
            selected = merges
            args.base = merges[-1] + '^1'
            how = f'the last {args.last_merges} merge(s) on the first-parent history'
        else:
            commits = git(repo, 'rev-list', '--first-parent', '-n', str(args.last_merges + 1), 'HEAD').split()
            if len(commits) <= args.last_merges:
                raise SystemExit(f'--last-merges {args.last_merges}: the history has only {len(commits)} commit(s)')
            selected = commits[:-1]
            args.base = commits[-1]
            how = (f'the last {args.last_merges} first-parent commit(s): only {len(merges)} merge commit(s) were found, '
                   'so the history is read as squash merges')
            warnings.append(f'--last-merges {args.last_merges}: used {how}')
        args.committed_only = True
    if args.diff:
        text = args.diff.read_text(encoding='utf-8', errors='replace')
        source = {'kind': 'diff', 'diff_file': args.diff.as_posix(), 'base_ref': None, 'merge_base': None, 'head': None}
        untracked = []
        prefix = git_prefix(repo)
        if prefix:
            text, outside = relative_diff(text, prefix)
            if outside:
                warnings.append(f'{outside} file(s) in the diff are outside --repo ({prefix.rstrip("/")} of the git '
                                'repository) and were left out')
    else:
        merge_base = git(repo, 'merge-base', args.base, 'HEAD').strip()
        head = git(repo, 'rev-parse', 'HEAD').strip()
        rng = [f'{merge_base}...HEAD'] if args.committed_only else [merge_base]
        # --relative: when --repo is a subdirectory of the git repository, paths come relative to it (and changes
        # outside it are left out), so they resolve against --repo
        text = git(repo, 'diff', '-M', '--relative', '--no-color', '--no-ext-diff', *rng)
        command = 'git diff -M --relative ' + ' '.join(rng)
        if args.last_merges is not None:   # only the merges' own changes, not direct commits between or after them
            text, command, why = changes_of_commits(repo, selected)
            if why:
                warnings.append(f'--last-merges {args.last_merges}: {why}')
        source = {'kind': 'git', 'diff_file': None, 'base_ref': args.base, 'merge_base': merge_base, 'head': head,
                  'command': command, 'working_tree': not args.committed_only}
        if args.last_merges is not None:
            source['last_merges'] = args.last_merges
            source['commits'] = selected
        if release_tag:
            source['since_release'] = release_tag
        untracked = [] if args.committed_only else git(repo, 'ls-files', '--others', '--exclude-standard').splitlines()
    files = scope_from_diff(text, repo)
    for f in files:
        if f['status'] != 'deleted' and not f['present'] and f['kind'] != 'binary':
            warnings.append(f'{f["path"]}: changed in the diff but not found under --repo, so no lines or functions '
                            'were read (is --repo the directory the diff paths are relative to?)')
    for rel in untracked:
        p = repo / rel
        try:
            body = p.read_text(encoding='utf-8')
            n = len(body.splitlines())
            binary = False
        except (UnicodeDecodeError, OSError):
            body, n, binary = None, 0, True
        kind, why = classify(rel, '\n'.join((body or '').splitlines()[:5]), binary, repo)
        entry = {'path': rel, 'old_path': None, 'status': 'added', 'kind': kind, 'kind_reason': why + ' (untracked)',
                 'ranges': [[1, n]] if n else [], 'deleted_at': [], 'lines_added': n, 'lines_deleted': 0,
                 'present': body is not None}
        attach_functions(entry, body)
        files.append(entry)
    by_status, by_kind = {}, {}
    for f in files:
        by_status[f['status']] = by_status.get(f['status'], 0) + 1
        by_kind[f['kind']] = by_kind.get(f['kind'], 0) + 1
    out = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'repo': repo.name,
           'source': source, 'files': files,
           'summary': {'files': len(files), 'by_status': by_status, 'by_kind': by_kind,
                       'lines_added': sum(f['lines_added'] for f in files),
                       'lines_deleted': sum(f['lines_deleted'] for f in files)}}
    if warnings:
        out['warnings'] = warnings
        for w in warnings:
            print('WARN: ' + w, file=sys.stderr)
    text_out = json.dumps(out, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text_out, encoding='utf-8')
        s = out['summary']
        print(f'{s["files"]} files ({", ".join(f"{v} {k}" for k, v in sorted(s["by_kind"].items()))}); '
              f'+{s["lines_added"]} -{s["lines_deleted"]} -> {args.out}')
    else:
        sys.stdout.write(text_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
