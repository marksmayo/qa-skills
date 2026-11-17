#!/usr/bin/env python3
"""Count a repository's source files per language, leaving out vendored and third-party trees (stdlib only).

    python lang_census.py <repo> [--supported Go,Python] [--tool scan_x.py] [--include DIR] [--out census.json]
    python lang_census.py --self-test

Skills that analyse only some languages call this before they report, so that a handful of incidental
files in a supported language is never presented as an analysis of a repository written in another one.

What is left out, before any file is counted (directories are pruned before they are walked):
  * dependency, VCS and cache directories: vendor/, node_modules/, third_party/, Pods/, Carthage/,
    bower_components/, .git, target/, DerivedData/, __pycache__, ...;
  * folders named like build output (dist/, build/, out/, obj/) only when the evidence says so
    (BuildEvidence, also exposed as build_output_dirs(root) -> {dir: {build_output, reason}} and
    is_build_output(root, dir) -> (bool, reason)): gitignored and untracked, produced by a build config in
    the repository (a package.json build tool, script or entry point, a bundler config, a tsconfig outDir,
    Python packaging, a .NET project), or holding source maps or nearly all minified bundles. A folder the
    app serves (FastAPI/Starlette StaticFiles, Flask static_folder, express.static, http.Dir, a PHP or
    .htaccess docroot) or that is git-tracked and hand-written is SOURCE and counted. With no evidence either
    way it stays build output. Every decision is listed under `build_output` with its reason, and
    --include DIR (census(include=[...])) counts a folder any rule would leave out (listed under `included`);
  * bundles and header trees: *.app, *.framework, *.xcframework, *.bundle, *.xcassets, Headers/,
    PrivateHeaders/;
  * git submodules (the paths in .gitmodules);
  * embedded third-party packages: a nested directory with its own package manifest (package.json, go.mod,
    Cargo.toml, pyproject.toml, setup.py, *.podspec, Package.swift, pom.xml, build.gradle) AND its own
    LICENSE/COPYING file, under a directory that holds drivers, SDKs or external code (drivers/, driver/,
    sdk/, sdks/, external/, extern/, deps/, contrib/, third-party/, vendored/). A workspace package
    (packages/web with a LICENSE) is product code and stays counted;
  * a header-only tree (10+ .h files, no .c/.m/.cpp next to them) under such a directory.

Every exclusion is listed with its reason and file count, so a reader can check none of it is product code.

Generated JavaScript and TypeScript (generated_reason(path, text): a .min name or minified body, a generated
header, a bundler's runtime, Emscripten loader glue, an Emscripten file-packager manifest) is counted under
`generated` and listed in `generated_files`, not as hand-written code; the folder holding it is still walked.

language_coverage() carries `notes` for a reader: every unsupported language present with its file count and
example paths (one PHP front controller next to a Python app is named, never silent), folders named like build
output that were counted as source and why, generated files counted apart, and build-output folders left out
that hold a supported language (the unsupported_dominant warning names them too, with the --include to use).

Data files (JSON, CSV, YAML, XML, images ...) are never a language. A directory holding at least 200 of them, no
source file and little else is a data tree: it is not walked and is listed under `data_trees` with its file count.
`prune_data_dirs(dirpath, dirnames, skipped)` applies the same rule inside any os.walk loop, so walkers looking for
code never open thousands of data records (`data_dir_reason(files)` is the test on one directory's file names).

UI markup for desktop and mobile toolkits (.slint Slint, .qml QML, .xaml XAML, .ui Qt Designer or GtkBuilder
'UI definition', .glade Glade, .kv Kivy) is counted under `markup` and `ui_markup` ({kind: files}, examples in
`ui_markup_examples`): never a language, never dominant. language_coverage() lists it under `ui_markup` with a note
that it is present but not analysed (unless the caller names the kind among its supported languages); the status is
unchanged.

Script languages no skill parses (PowerShell .ps1/.psm1, shell .sh/.bash/.zsh, batch, and the rest of SCRIPT_BY_EXT)
are counted under `scripts` ({kind: files}), `script_lines` (non-blank lines outside test and tooling folders),
`script_total_lines` and `script_examples`: never a language of the census, never its dominant one, but
language_coverage() always names them (`scripts` and a note each), because an installer or packaging script is
shipped code. When their product lines outweigh every counted language's the status is 'unsupported_dominant'.

A tooling-named folder may hold the product (a repository whose scripts/ are what it ships). The default is
unchanged, but `tooling_dirs` lists each such folder with its lines and the `evidence` that it is product code (a
test loads it, CI measures coverage on it or runs it, a manifest ships it); language_coverage() then says so
(`tooling_as_product` and a note), and says when a supported language has no product line because all of it sits in
tooling folders. `census(product_dirs=[...])` / `--product-dir DIR` counts such a folder as product code.

Markup is counted apart, under `markup` (HTML, CSS, Templates; an .html file under templates/, views/, layouts/ or
partials/ is a template). `server_rendered` names a Python web app that renders templates (Flask, Django, Jinja2,
Starlette/FastAPI templating, Bottle, Pyramid ...: an import or a render call) with its template count and
directories. language_coverage(..., reads_templates=True), or 'HTML' among the supported languages, tells the guard
the tool reads those templates: a Python-dominant server-rendered site is then 'partial' with a `note` (the Python is
the server, outside the tool's scope), never 'unsupported_dominant'. A tool that does not read templates still gets
the warning.

The dominant language is the one with the most non-blank lines of PRODUCT code (`dominant_by: 'lines'`). Test code
(is_test_file: test_x.py, x_test.go, x.test.ts, XTest.java, tests.rs, or anything under tests/, test/, __tests__/,
spec/, e2e/, testdata/, benches/, benchmarks/ ... unless that folder holds its own package manifest) is counted apart
under `test_files` and `test_lines` and decides only a repository with no product line (a standalone test
repository). Helper scripts under tooling folders (is_tooling_file: scripts/, tools/, docs/, hack/, ci/ ...) are
counted apart under `tooling_files` and `tooling_lines` and decide only when neither product nor test code holds a
line. `dominant_basis` says which lines decided ('product', 'test' or 'tooling') and `dominant_lines` counts them. `languages` keeps every counted file (product and test) per
language; `product_files` and `lines` are the product part. With no product line at all the most product files decide
(`dominant_by: 'files'`). In-source test modules (a Rust #[cfg(test)] module) are product lines here.

Library use:
    import lang_census
    c = lang_census.census(repo)   # {'languages': {'Python': 36, 'Rust': 31}, 'lines': {'Rust': 9100, 'Python': 2400},
                                   #  'dominant': 'Rust', 'dominant_by': 'lines', 'test_files': {...}, ...}
    cov = lang_census.language_coverage(c, {'Python', 'JavaScript', 'TypeScript'}, tool='scan_x.py')
    if cov['status'] == 'unsupported_dominant':
        print(cov['warning'], file=sys.stderr)       # and put cov in the output as `language_coverage`
    lang_census.excluded_path(c, 'drivers/ios/Agent/x.ts')   # the exclusion reason, or None
"""
from __future__ import annotations
import argparse, json, os, re, shutil, sys, tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

LANG_BY_EXT = {
    '.py': 'Python', '.pyi': 'Python',
    '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript', '.mts': 'TypeScript', '.cts': 'TypeScript',
    '.vue': 'Vue', '.svelte': 'Svelte',
    '.go': 'Go', '.rs': 'Rust',
    '.java': 'Java', '.kt': 'Kotlin', '.kts': 'Kotlin', '.scala': 'Scala', '.groovy': 'Groovy',
    '.cs': 'C#', '.fs': 'F#', '.vb': 'Visual Basic',
    '.swift': 'Swift', '.m': 'Objective-C', '.mm': 'Objective-C',
    '.c': 'C', '.h': 'C/C++ header', '.cc': 'C++', '.cpp': 'C++', '.cxx': 'C++', '.hpp': 'C++', '.hh': 'C++',
    '.rb': 'Ruby', '.php': 'PHP', '.dart': 'Dart',
    '.ex': 'Elixir', '.exs': 'Elixir', '.erl': 'Erlang', '.clj': 'Clojure', '.cljs': 'Clojure',
    '.hs': 'Haskell', '.ml': 'OCaml', '.mli': 'OCaml', '.lua': 'Lua', '.pl': 'Perl', '.pm': 'Perl',
    '.r': 'R', '.R': 'R', '.jl': 'Julia', '.zig': 'Zig', '.nim': 'Nim', '.cr': 'Crystal', '.elm': 'Elm',
}

# script languages no skill parses: counted apart (census `scripts`, `script_lines`, `script_examples`), never a
# language of the census and never its dominant one, but always named by language_coverage(): an installer or
# packaging script is shipped code even when nothing here reads it
SCRIPT_BY_EXT = {'.ps1': 'PowerShell', '.psm1': 'PowerShell', '.sh': 'Shell', '.bash': 'Shell', '.zsh': 'Shell',
                 '.ksh': 'Shell', '.fish': 'Shell', '.bat': 'Batch', '.cmd': 'Batch', '.awk': 'Awk', '.tcl': 'Tcl',
                 '.vbs': 'VBScript', '.applescript': 'AppleScript', '.nsi': 'NSIS', '.iss': 'Inno Setup'}

# installed package trees: excluded by name and listed without walking them (source_files_counted: false)
DEPENDENCY_TREES = {'node_modules', 'bower_components', 'jspm_packages'}
PRUNE_DIRS = {'.git', '.hg', '.svn', 'node_modules', 'vendor', 'vendored', 'third_party', 'third-party', 'thirdparty',
              'bower_components', 'jspm_packages', 'Pods', 'Carthage', '.venv', 'venv', '.tox', '.nox',
              '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'site-packages',
              'target', 'DerivedData', '.gradle', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache',
              'coverage', 'htmlcov', '.idea', '.vscode', 'Headers', 'PrivateHeaders'}
# Folders named like build output. Each is decided by evidence (build_output_reason): build output when gitignored,
# produced by a build config in the repository, or full of minified bundles and source maps; source when the app
# serves it (StaticFiles, static_folder, express.static, a PHP or .htaccess docroot ...) or it is git-tracked and
# hand-written. With no evidence either way it stays build output, as before. build/, out/ and obj/ holding .go files
# are Go packages. public/ and www/ are never left out.
BUILD_NAMES = {'dist', 'build', 'out', 'obj'}
BUILD_OUTPUT = {'build', 'out', 'obj'}   # also plausible Go package names: kept when they hold .go files themselves
BUNDLE_SUFFIXES = ('.app', '.framework', '.xcframework', '.bundle', '.xcassets', '.appex', '.dSYM', '.egg-info')
# directories under which a nested package with its own licence is someone else's code
EXTERNAL_PARENTS = {'drivers', 'driver', 'sdk', 'sdks', 'external', 'externals', 'extern', 'deps', 'contrib',
                    'third-party', 'third_party', 'thirdparty', 'vendored', 'lib-external'}
MANIFESTS = ('package.json', 'go.mod', 'Cargo.toml', 'pyproject.toml', 'setup.py', 'Package.swift', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'composer.json', 'Gemfile', 'mix.exs', 'pubspec.yaml')
LICENSE_RX = re.compile(r'^(LICEN[CS]E|COPYING|COPYRIGHT)([.-].*)?$', re.I)
HEADER_ONLY_MIN = 10

# Data files: never a language, never counted towards the dominant one. A directory holding at least DATA_DIR_MIN of
# them, no source file and little else is a data tree: walkers looking for code prune it (prune_data_dirs).
DATA_EXT = {'.json', '.jsonl', '.ndjson', '.geojson', '.csv', '.tsv', '.yaml', '.yml', '.xml', '.parquet', '.avro',
            '.txt', '.dat', '.xlsx', '.xls', '.ods', '.sqlite', '.db', '.gz', '.zip', '.npy', '.npz', '.pkl',
            '.arrow', '.feather', '.toml', '.ini', '.kml', '.gpx', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif'}
DATA_DIR_MIN = 200
DATA_SHARE = 0.9
# Markup is counted apart from the languages: browser-facing, but never the dominant language.
MARKUP_BY_EXT = {'.html': 'HTML', '.htm': 'HTML', '.xhtml': 'HTML', '.css': 'CSS', '.scss': 'CSS', '.sass': 'CSS',
                 '.less': 'CSS', '.styl': 'CSS',
                 '.jinja': 'Templates', '.jinja2': 'Templates', '.j2': 'Templates', '.njk': 'Templates',
                 '.hbs': 'Templates', '.handlebars': 'Templates', '.mustache': 'Templates', '.erb': 'Templates',
                 '.ejs': 'Templates', '.twig': 'Templates', '.liquid': 'Templates', '.tmpl': 'Templates',
                 '.gohtml': 'Templates', '.pug': 'Templates', '.jade': 'Templates', '.haml': 'Templates',
                 '.slim': 'Templates', '.cshtml': 'Templates', '.razor': 'Templates', '.blade.php': 'Templates'}
# UI markup for desktop and mobile toolkits: the UI is described in these files, but no tool here reads them. Counted
# apart (under `markup` and `ui_markup`), never a language and never the dominant one; language_coverage names them.
UI_MARKUP_BY_EXT = {'.slint': 'Slint', '.qml': 'QML', '.xaml': 'XAML', '.ui': 'UI definition', '.glade': 'Glade',
                    '.kv': 'Kivy'}
MARKUP_BY_EXT.update(UI_MARKUP_BY_EXT)
UI_MARKUP_KINDS = set(UI_MARKUP_BY_EXT.values())
TEMPLATE_DIRS = {'templates', 'template', 'views', 'layouts', 'partials', 'includes', '_includes', '_layouts'}
# a Python module that serves pages from templates: the web framework it imports, or a template render call
PY_WEB = re.compile(r'^\s*(?:from|import)\s+(flask|django|jinja2|bottle|pyramid|quart|sanic|starlette\.templating|'
                    r'fastapi\.templating|tornado\.web|aiohttp_jinja2|cherrypy|falcon)\b'
                    r'|\b(render_template|render_to_response|TemplateResponse|Jinja2Templates|aiohttp_jinja2\.template)\s*\(',
                    re.M)
PY_WEB_FILES_MAX = 400


def markup_kind(rel: str) -> str | None:
    """'Templates', 'HTML' or 'CSS' for a markup file (an .html file under a templates/ or views/ directory is a
    template), or None."""
    low = rel.lower()
    if low.endswith('.blade.php'):
        return 'Templates'
    kind = MARKUP_BY_EXT.get(PurePosixPath(low).suffix)
    if kind == 'HTML' and any(p in TEMPLATE_DIRS for p in low.split('/')[:-1]):
        return 'Templates'
    return kind


def data_dir_reason(files: list[str], min_files: int = DATA_DIR_MIN) -> str | None:
    """Why a directory holding `files` (its file names) is a data tree, or None: at least min_files data files (JSON,
    CSV, YAML, XML, images ...), no source file, and data files at least 90% of what it holds."""
    if len(files) < min_files:
        return None
    n_data = 0
    for f in files:
        suf = PurePosixPath(f).suffix.lower()
        if suf in LANG_BY_EXT or f.endswith('.R'):
            return None
        n_data += suf in DATA_EXT
    if n_data >= min_files and n_data >= DATA_SHARE * len(files):
        exts = Counter(PurePosixPath(f).suffix.lower() for f in files if PurePosixPath(f).suffix.lower() in DATA_EXT)
        top = ', '.join(f'{n} {e}' for e, n in exts.most_common(3))
        return f'a data tree ({n_data} data files: {top}; no source files)'
    return None


def prune_data_dirs(dirpath: str, dirnames: list[str], skipped: list | None = None, root: str | None = None,
                    min_files: int = DATA_DIR_MIN) -> list[str]:
    """For an os.walk loop: remove data-tree subdirectories of dirpath from dirnames (in place, so the walk never
    descends into them) and append {'path', 'files', 'reason'} for each to skipped. Paths are relative to root (or
    dirpath). Returns the names removed. Reuse it in any walker that looks for code, so thousands of data files are
    not opened."""
    removed = []
    for d in list(dirnames):
        full = os.path.join(dirpath, d)
        try:
            files = [e.name for e in os.scandir(full) if e.is_file()]
        except OSError:
            continue
        why = data_dir_reason(files, min_files)
        if why:
            dirnames.remove(d)
            removed.append(d)
            if skipped is not None:
                rel = os.path.relpath(full, root or dirpath).replace(os.sep, '/')
                skipped.append({'path': rel, 'files': len(files), 'reason': why})
    return removed


def server_rendered(root: Path, py_files: list[str], template_files: list[str]) -> dict | None:
    """{'framework', 'server_language', 'templates', 'template_dirs', 'evidence'} when Python modules import a web
    framework or render templates and the repository holds template files; None otherwise."""
    if not py_files or not template_files:
        return None
    for rel in py_files[:PY_WEB_FILES_MAX]:
        try:
            with open(root / rel, encoding='utf-8', errors='replace') as fh:
                text = fh.read(65536)
        except OSError:
            continue
        m = PY_WEB.search(text)
        if m:
            fw = (m.group(1) or '').split('.')[0] or ('jinja2' if 'Jinja2' in (m.group(2) or '') else 'templates')
            dirs = sorted({next((('/'.join(t.split('/')[:i + 1])) for i, p in enumerate(t.split('/')[:-1])
                                 if p.lower() in TEMPLATE_DIRS), PurePosixPath(t).parent.as_posix())
                           for t in template_files})
            return {'framework': fw, 'server_language': 'Python', 'templates': len(template_files),
                    'template_dirs': dirs[:20], 'evidence': f'{rel}: {m.group(0).strip()[:80]}'}
    return None


def submodules(root: Path) -> set[str]:
    try:
        text = (root / '.gitmodules').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return set()
    return {m.group(1).strip().strip('/') for m in re.finditer(r'^\s*path\s*=\s*(.+)$', text, re.M)}


JS_FAMILY = {'JavaScript', 'TypeScript'}
GENERATED_READ = 262144
GENERATED_HEAD = re.compile(r'@generated|\bgenerated by\b|\bdo not edit\b|auto-?generated|\bcode generated\b', re.I)
BUNDLE_MARK = re.compile(r'__webpack_require__|webpackBootstrap|__webpack_modules__|parcelRequire|'
                         r'System\.register\(|__vite__mapDeps|\bdefine\.amd\b.{0,200}\bwebpackJsonp')
EMSCRIPTEN_MARK = re.compile(r'\b(HEAPU?(?:8|16|32|64)|HEAPF(?:32|64)|wasmMemory|wasmExports|wasmTable|addRunDependency|'
                             r'removeRunDependency|UTF8ToString|stringToUTF8|lengthBytesUTF8|ENVIRONMENT_IS_(?:WEB|NODE|'
                             r'WORKER|SHELL)|FS_createPath|FS_createDataFile|FS_createPreloadedFile|ASM_CONSTS|'
                             r'_emscripten_\w+|__syscall_\w+|createWasm|GLctx|stackSave|stackRestore|dynCall_\w+)\b')
EMSCRIPTEN_WORD = re.compile(r'\bemscripten\b', re.I)
FILE_PACKAGER = re.compile(r'\bfiles\s*:\s*\[\s*\{\s*["\']?filename["\']?\s*:')


def generated_reason(rel: str, text: str) -> str | None:
    """Why a JavaScript/TypeScript file looks generated rather than hand-written, or None: a minified name or body
    (average line over 250 characters, or a line over 5000), a generated-file header, a bundler's runtime (webpack,
    Parcel, SystemJS, Vite), Emscripten loader glue (three or more distinct runtime names such as HEAP32,
    UTF8ToString, addRunDependency, wasmExports) or an Emscripten file-packager manifest (a files list of
    {filename, start, end} records: data, not code). Pass at least the first 256 KB of the file."""
    low = rel.lower()
    if re.search(r'[.-]min\.[cm]?[jt]s$', low):
        return 'minified (a .min file name)'
    if GENERATED_HEAD.search(text[:1024]):
        return 'generated (its header says so)'
    lines = text.splitlines() or ['']
    longest = max(len(x) for x in lines)
    avg = len(text) / len(lines)
    if len(text) >= 2000 and (avg > 250 or longest > 5000):
        return f'minified (average line {int(avg)} characters, longest {longest})'
    m = BUNDLE_MARK.search(text)
    if m:
        return f'a bundler\'s output ({m.group(0)[:30]})'
    if FILE_PACKAGER.search(text) and re.search(r'\bstart\s*:', text) and re.search(r'\bend\s*:', text):
        return 'an Emscripten file-packager manifest (a files list of {filename, start, end}: data, not code)'
    marks = sorted({re.sub(r'^(_emscripten_|__syscall_|dynCall_).*', r'\1*', x) for x in EMSCRIPTEN_MARK.findall(text)})
    if len(marks) >= 3 or (marks and EMSCRIPTEN_WORD.search(text)):
        return f'Emscripten loader glue ({", ".join(marks[:5])})'
    return None


# ---- test code: counted apart, never deciding the dominant language ----
# directories that hold tests, fixtures or benchmarks (unless the directory itself holds a package manifest: then it
# is a product package that happens to be named like tests, e.g. a CLI package called e2e/)
TEST_DIRS = {'tests', 'test', '__tests__', '__mocks__', 'spec', 'specs', 'e2e', 'testdata', 'test_data', 'benches',
             'benchmarks', 'cypress'}
TEST_FILE = re.compile(r'(^test_.*\.py$|.*_test\.py$|^conftest\.py$|.*_test\.go$|.*\.(test|spec|e2e|cy)\.[cm]?[jt]sx?$|'
                       r'.*(Test|Tests|IT)\.(java|kt|kts|cs|php|swift|scala)$|.*_(spec|test)\.rb$|^tests?\.rs$)')
# folders of helper scripts and documentation tooling: counted apart (tooling_files, tooling_lines); they decide the
# dominant language only when the repository holds no other product line
TOOLING_DIRS = {'scripts', 'script', 'tools', 'tooling', 'docs', 'doc', 'hack', 'ci', '.github', 'devtools'}
LINE_READ_MAX = 4 * 1024 * 1024
LINE_RULE_MIN_LINES = 500


def is_test_file(rel: str, manifest_dirs: set[str] | None = None) -> bool:
    """Is rel (repo-relative, '/'-separated) test code: a test file name (test_x.py, x_test.go, x.test.ts, XTest.java,
    tests.rs ...) or a file under a test directory (tests/, test/, __tests__/, spec/, e2e/, testdata/, benches/,
    benchmarks/ ...)? A test-named directory listed in manifest_dirs (it holds its own package manifest) is a product
    package, not test code."""
    parts = rel.split('/')
    if TEST_FILE.match(parts[-1]):
        return True
    for i, p in enumerate(parts[:-1]):
        if p.lower() in TEST_DIRS and '/'.join(parts[:i + 1]) not in (manifest_dirs or ()):
            return True
    return False


def is_tooling_file(rel: str) -> bool:
    """Is rel (repo-relative) under a tooling folder (scripts/, tools/, docs/, hack/, ci/ ...)?"""
    return any(p.lower() in TOOLING_DIRS for p in rel.split('/')[:-1])


def tooling_dir(rel: str) -> str | None:
    """The tooling folder rel lies in (the path up to its first tooling-named part), or None."""
    parts = rel.split('/')[:-1]
    for i, p in enumerate(parts):
        if p.lower() in TOOLING_DIRS:
            return '/'.join(parts[:i + 1])
    return None


CI_FILE = re.compile(r'^(\.github/workflows/[^/]+\.ya?ml|azure-pipelines[^/]*\.ya?ml|\.gitlab-ci\.yml|'
                     r'\.circleci/config\.yml|bitbucket-pipelines\.yml|Jenkinsfile)$')
COVERAGE_CONFIG = ('.coveragerc', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'noxfile.py', 'Makefile')
PY_MANIFESTS = ('pyproject.toml', 'setup.py', 'setup.cfg', 'MANIFEST.in')
TOOLING_EVIDENCE_TESTS_MAX = 300
TOOLING_EVIDENCE_READ = 65536
NO_EVIDENCE_DIRS = {'.github', 'docs', 'doc'}


def tooling_evidence(root: Path, dirs: list[str], test_paths: list[str]) -> dict[str, list[dict]]:
    """Signs that a tooling-named folder (scripts/, tools/ ...) holds product code rather than helper scripts:
    {dir: [{kind, file, line, text}]}, at most two per kind.

    tests        a test file names the folder (a sys.path insert, a path built with it, an import of it)
    ci_coverage  a CI file or coverage configuration measures it (--source=scripts, --cov=scripts, source = scripts)
    ci_runs      a CI file runs a file of it
    manifest     a package manifest ships it (package.json bin/main/files, a quoted path in pyproject.toml, setup.py,
                 setup.cfg or MANIFEST.in)

    Read only for the folders given (the census passes those that hold code): CI files, the root manifests and at
    most TOOLING_EVIDENCE_TESTS_MAX test files, the first 64 KB of each."""
    dirs = [d for d in dirs if d.rsplit('/', 1)[-1].lower() not in NO_EVIDENCE_DIRS]
    out: dict[str, list[dict]] = {d: [] for d in dirs}
    if not dirs:
        return out
    rx = {}
    for d in dirs:
        n, full = re.escape(d.rsplit('/', 1)[-1]), re.escape(d)
        rx[d] = {
            'tests': re.compile(r'[\'"/\\]%s[\'"/\\]|^\s*(?:from|import)\s+%s\b' % (n, n), re.M),
            'ci_coverage': re.compile(r'(?:--cov|--source|--include)[= ]\S*(?<![\w-])%s(?![\w-])|'
                                      r'^\s*source\s*=.*(?<![\w-])%s(?![\w-])' % (n, n)),
            'ci_runs': re.compile(r'(?<![\w/.-])(?:\./)?%s/[\w./-]+\.\w+' % full),
            'manifest': re.compile(r'[\'"](?:\./)?%s(?:/[^\'"]*)?[\'"]' % full),
        }

    def add(d, kind, file, line, text):
        have = [e for e in out[d] if e['kind'] == kind]
        if len(have) < 2:
            out[d].append({'kind': kind, 'file': file, 'line': line, 'text': text.strip()[:120]})

    def lines_of(rel):
        text = _read_head(root / rel, TOOLING_EVIDENCE_READ)
        return text, (text.splitlines() if text else [])
    ci_files = []
    wf = root / '.github' / 'workflows'
    if wf.is_dir():
        ci_files += sorted(f'.github/workflows/{f}' for f in os.listdir(wf) if f.endswith(('.yml', '.yaml')))
    try:
        top = sorted(os.listdir(root))
    except OSError:
        top = []
    ci_files += [f for f in top if CI_FILE.match(f)]
    if (root / '.circleci' / 'config.yml').is_file():
        ci_files.append('.circleci/config.yml')
    for rel in ci_files:
        _text, lines = lines_of(rel)
        for i, line in enumerate(lines, 1):
            for d in dirs:
                if rx[d]['ci_coverage'].search(line):
                    add(d, 'ci_coverage', rel, i, line)
                elif rx[d]['ci_runs'].search(line):
                    add(d, 'ci_runs', rel, i, line)
    for rel in (f for f in COVERAGE_CONFIG if f in top):
        _text, lines = lines_of(rel)
        for i, line in enumerate(lines, 1):
            for d in dirs:
                if rx[d]['ci_coverage'].search(line):
                    add(d, 'ci_coverage', rel, i, line)
    for rel in (f for f in PY_MANIFESTS if f in top):
        _text, lines = lines_of(rel)
        for i, line in enumerate(lines, 1):
            for d in dirs:
                if rx[d]['manifest'].search(line) and not rx[d]['ci_coverage'].search(line):
                    add(d, 'manifest', rel, i, line)
    if 'package.json' in top:
        try:
            pj = json.loads((root / 'package.json').read_text(encoding='utf-8', errors='replace'))
        except (OSError, ValueError):
            pj = {}
        named = []
        for key in ('bin', 'main', 'module', 'files', 'exports') if isinstance(pj, dict) else ():
            v = pj.get(key)
            stack = [v]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    named.append((key, x))
                elif isinstance(x, dict):
                    stack += list(x.values())
                elif isinstance(x, list):
                    stack += x
        for key, value in named:
            v = value.replace('\\', '/').lstrip('./')
            for d in dirs:
                if v == d or v.startswith(d + '/'):
                    add(d, 'manifest', 'package.json', 0, f'"{key}": {value}')
    for rel in test_paths[:TOOLING_EVIDENCE_TESTS_MAX]:
        text, _lines = lines_of(rel)
        for d in dirs:
            if rel.startswith(d + '/'):
                continue
            m = rx[d]['tests'].search(text)
            if m:
                line = text.count('\n', 0, m.start()) + 1
                add(d, 'tests', rel, line, text.splitlines()[line - 1] if text else '')
    return out


def count_lines(text: str) -> int:
    """Non-blank lines of a source file's text."""
    return sum(1 for line in text.splitlines() if line.strip())


def _read_head(path: Path, n: int = GENERATED_READ) -> str:
    """The first n characters of a file. A text-mode read(n) sizes its buffer by n, so the request is capped at the
    file's own size (bytes are never fewer than the characters they decode to): reading a small file costs microseconds,
    not the milliseconds a multi-megabyte buffer takes."""
    try:
        size = os.path.getsize(path)
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read(min(n, size + 1))
    except OSError:
        return ''


# ---- build output: decided by evidence ----
BUILDER = re.compile(r'\b(vite|webpack|rollup|parcel|tsup|esbuild|ncc|tsc|ng|vue-cli-service|react-scripts|next|astro|'
                     r'svelte-kit|gatsby|babel|microbundle|unbuild|snowpack|swc|rspack|rsbuild|eleventy|ember|nuxt|'
                     r'remix|gulp|grunt|electron-builder|electron-forge|vsce|bun build|tsdown|farm|turbopack)\b')
BUILD_CONFIG = re.compile(r'^(vite|webpack|rollup|tsup|esbuild|svelte|astro|next|nuxt|vue|rspack|rsbuild|farm|tsdown|'
                          r'gatsby-config|gulpfile|gruntfile)(\.config)?\.[cm]?[jt]s$|^(angular\.json|\.parcelrc|'
                          r'ember-cli-build\.js)$', re.I)
PY_PACKAGING = ('setup.py', 'pyproject.toml', 'setup.cfg')
# lines that serve a folder over HTTP: Python (Starlette/FastAPI, Flask, Django, http.server), Node (Express, Koa,
# Fastify, sirv), Go (http.Dir/FileServer), PHP readfile/include, Apache, nginx and Caddy docroots
SERVE_LINE = re.compile(r'StaticFiles\s*\(|static_folder\s*=|send_from_directory\s*\(|SimpleHTTPRequestHandler|'
                        r'STATICFILES_DIRS|express\.static\s*\(|serveStatic\s*\(|serve-static|\bsirv\s*\(|koa-static|'
                        r'fastify-static|@fastify/static|http\.Dir\s*\(|http\.FileServer|readfile\s*\(|'
                        r'file_get_contents\s*\(|DocumentRoot|RewriteRule|^\s*(?:root|alias)\s+\S|file_server|'
                        r'\bstatic_dir\b|StaticFileHandler', re.M)
SERVE_FILES = ('.py', '.js', '.mjs', '.cjs', '.ts', '.php', '.go', '.rb', '.conf', '.htaccess')
SERVE_NAMES = {'.htaccess', 'Caddyfile', 'nginx.conf', 'httpd.conf', 'Procfile', 'Dockerfile'}
SERVE_FILES_MAX = 3000


class BuildEvidence:
    """Evidence about folders named like build output in one repository (git state, build configs, what the app
    serves), gathered lazily and cached. decide(rel) -> (is_build_output, reason)."""

    def __init__(self, root: Path | str, include=()):
        self.root = Path(root).resolve()
        self.include = {i.replace('\\', '/').strip('/') for i in include or ()}
        self._git = None
        self._serve_lines = None
        self._cache: dict[str, tuple[bool, str]] = {}

    def _run_git(self, *args) -> tuple[int, str]:
        import subprocess
        try:
            res = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=60)
        except (OSError, subprocess.SubprocessError):
            return 128, ''
        return res.returncode, res.stdout

    def in_git(self) -> bool:
        if self._git is None:
            code, out = self._run_git('rev-parse', '--is-inside-work-tree')
            self._git = code == 0 and out.strip() == 'true'
        return self._git

    def serve_lines(self) -> list[tuple[str, int, str]]:
        """(file, line, text of that line and the next three) for every line that serves a folder."""
        if self._serve_lines is not None:
            return self._serve_lines
        out: list[tuple[str, int, str]] = []
        seen = 0
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in PRUNE_DIRS and d not in BUILD_NAMES and not d.startswith('.')]
            for f in fns:
                if not (f.endswith(SERVE_FILES) or f in SERVE_NAMES):
                    continue
                seen += 1
                if seen > SERVE_FILES_MAX:
                    break
                p = Path(dp) / f
                text = _read_head(p, 131072)
                if not SERVE_LINE.search(text):
                    continue
                rel = p.relative_to(self.root).as_posix()
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if SERVE_LINE.search(line):
                        out.append((rel, i + 1, '\n'.join(lines[i:i + 4])))
        self._serve_lines = out
        return out

    def served_by(self, rel: str) -> str | None:
        """'server.py:12 (StaticFiles(directory="dist"))' when a serving line names this folder, else None."""
        name = rel.rsplit('/', 1)[-1]
        seg = re.compile(r'(?<![\w.-])(?:\./)?' + re.escape(name) + r'(?![\w-])')
        for frel, line, window in self.serve_lines():
            first = window.splitlines()[0] if window else ''
            # the serving call's own line, or (for a call split over lines) the lines right after it
            if seg.search(first) or (first.rstrip().endswith(('(', ',')) and seg.search(window)):
                return f'{frel}:{line} ({first.strip()[:80]})'
        return None

    def build_config(self, rel: str) -> str | None:
        """The build config that produces this folder, or None: a package.json build tool or a script or entry point
        naming it, a bundler config file, a tsconfig outDir, Python packaging (dist/ or build/ holding wheels or
        build trees), or a .NET project next to obj/."""
        name = rel.rsplit('/', 1)[-1]
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        here = self.root / rel
        bases = [parent] + ([''] if parent else [])
        seg = re.compile(r'(?<![\w.-])(?:\./)?' + re.escape(name) + r'(?:/|\b)')
        for base in bases:
            d = self.root / base if base else self.root
            shown = (base + '/') if base else ''
            pj = d / 'package.json'
            if pj.is_file():
                try:
                    data = json.loads(pj.read_text(encoding='utf-8', errors='replace') or '{}')
                except ValueError:
                    data = {}
                scripts = data.get('scripts') if isinstance(data, dict) else None
                for sname, cmd in (scripts or {}).items() if isinstance(scripts, dict) else ():
                    if not isinstance(cmd, str):
                        continue
                    m = BUILDER.search(cmd)
                    if m and (name in ('dist', 'build', 'out') or seg.search(cmd)):
                        return f'{shown}package.json script "{sname}" runs {m.group(1)} ({cmd[:60]}), which writes {name}/'
                    if seg.search(cmd) and re.search(r'(--?out(-?dir|put)?|-o|-d|--dist-dir)[ =]+["\']?(\./)?'
                                                     + re.escape(name), cmd):
                        return f'{shown}package.json script "{sname}" writes {name}/ ({cmd[:60]})'
                for key in ('main', 'module', 'browser', 'types', 'exports', 'bin'):
                    v = json.dumps(data.get(key)) if isinstance(data, dict) and key in data else ''
                    if v and seg.search(v):
                        return f'{shown}package.json "{key}" points into {name}/ (a published build)'
            try:
                names = [e.name for e in os.scandir(d) if e.is_file()]
            except OSError:
                names = []
            for f in names:
                if BUILD_CONFIG.match(f):
                    return f'{shown}{f} is a bundler or framework build config (its output is {name}/)'
            for f in names:
                if re.match(r'^tsconfig(\.[\w-]+)?\.json$', f):
                    t = _read_head(d / f, 65536)
                    if re.search(r'"outDir"\s*:\s*"(\./)?' + re.escape(name) + r'/?"', t):
                        return f'{shown}{f} compiles into {name}/ (outDir)'
            if name in ('dist', 'build') and any(p in names for p in PY_PACKAGING) and here.is_dir():
                inside = [e.name for e in os.scandir(here)]
                if any(x.endswith(('.whl', '.tar.gz', '.egg')) or x in ('lib', 'wheel') or x.startswith('bdist')
                       for x in inside):
                    return f'{shown}{next(p for p in PY_PACKAGING if p in names)}: Python packaging writes {name}/'
            if name in ('obj', 'bin') and any(f.endswith(('.csproj', '.fsproj', '.vbproj')) for f in names):
                return f'{shown}a .NET project builds into {name}/'
        return None

    def looks_built(self, rel: str) -> str | None:
        """Source maps, or JavaScript and CSS that is (nearly) all minified or bundled."""
        here = self.root / rel
        n = built = 0
        for dp, dns, fns in os.walk(here):
            dns[:] = [d for d in dns if d not in PRUNE_DIRS]
            for f in fns:
                if f.endswith(('.js.map', '.css.map', '.mjs.map')):
                    return f'it holds source maps ({Path(dp, f).relative_to(self.root).as_posix()})'
                if f.endswith(('.js', '.mjs', '.cjs', '.css')) and n < 200:
                    n += 1
                    frel = Path(dp, f).relative_to(self.root).as_posix()
                    why = generated_reason(frel, _read_head(Path(dp, f), 65536))
                    built += bool(why and (why.startswith('minified') or why.startswith('a bundler')))
        if n and built >= max(1, 0.8 * n):
            return f'{built} of its {n} script and style file(s) are minified or bundled'
        return None

    def _root_gitignore_lists(self, rel: str) -> bool:
        """Outside a git checkout: does the root .gitignore name rel (as rel, /rel, rel/ or /rel/, or its last
        part alone when the pattern has no slash)? Negations and wildcards are not interpreted."""
        try:
            lines = (self.root / '.gitignore').read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return False
        name = rel.rsplit('/', 1)[-1]
        for line in lines:
            pat = line.strip()
            if not pat or pat.startswith(('#', '!')):
                continue
            core = pat.strip('/')
            if core == rel or ('/' not in core and core == name):
                return True
        return False

    def decide(self, rel: str) -> tuple[bool, str]:
        """(is_build_output, reason) for the folder rel (repo-relative)."""
        rel = rel.replace('\\', '/').strip('/')
        if rel in self._cache:
            return self._cache[rel]
        self._cache[rel] = res = self._decide(rel)
        return res

    def _decide(self, rel: str) -> tuple[bool, str]:
        name = rel.rsplit('/', 1)[-1]
        if rel in self.include:
            return False, f'{rel}/ included by the caller (--include)'
        tracked = None
        if self.in_git():
            code, out = self._run_git('ls-files', '-z', '--', rel)
            tracked = bool(out.strip('\0')) if code == 0 else None
            if tracked is False:
                code, _ = self._run_git('check-ignore', '-q', '--', rel + '/')
                if code == 0:
                    return True, f'{name}/ is gitignored build output'
        elif self._root_gitignore_lists(rel):
            return True, f'{name}/ is build output: the root .gitignore lists it (not a git checkout)'
        why = self.build_config(rel)
        if why:
            return True, f'{name}/ is build output: {why}'
        why = self.looks_built(rel)
        if why:
            return True, f'{name}/ looks built: {why}'
        served = self.served_by(rel)
        state = 'git-tracked, ' if tracked else ''
        if served:
            return False, f'{name}/ is source: served by {served}; {state}no build config in the repository produces it'
        if tracked:
            return False, (f'{name}/ is source: git-tracked and hand-written; no build config in the repository '
                           f'produces it')
        if tracked is False:
            return True, (f'{name}/ is named like build output and is not tracked by git; no evidence it is source '
                          f'(no build config, not served by the app)')
        return True, (f'{name}/ is named like build output; no evidence it is source (not a git checkout, no build '
                      f'config, not served by the app)')


def build_output_dirs(root: Path | str, include=()) -> dict[str, dict]:
    """{rel_dir: {'build_output': bool, 'reason': str}} for every folder named dist/, build/, out/ or obj/ under root
    (dependency and VCS trees pruned, Go packages named build/ or out/ left alone). The decision is the one census()
    applies; pass include to force folders in."""
    ev = BuildEvidence(root, include)
    out: dict[str, dict] = {}
    for dp, dns, _ in os.walk(ev.root):
        rel = Path(dp).relative_to(ev.root).as_posix()
        rel = '' if rel == '.' else rel
        keep = []
        for d in dns:
            if d in PRUNE_DIRS or d.endswith(BUNDLE_SUFFIXES):
                continue
            sub = f'{rel}/{d}' if rel else d
            if d in BUILD_NAMES and not _go_package(Path(dp) / d, d):
                is_b, why = ev.decide(sub)
                out[sub] = {'build_output': is_b, 'reason': why}
                if is_b:
                    continue
            keep.append(d)
        dns[:] = keep
    return out


def is_build_output(root: Path | str, rel_dir: str, include=(), evidence: BuildEvidence | None = None
                    ) -> tuple[bool, str]:
    """(is_build_output, reason) for one folder (repo-relative) named like build output. Reuse one BuildEvidence
    (evidence=) for many calls on the same repository."""
    return (evidence or BuildEvidence(root, include)).decide(rel_dir)


def build_output_decision(root: Path | str, rel_dir: str, include=(), exclude=(),
                          evidence: BuildEvidence | None = None) -> dict:
    """{dir, decision: 'source'|'build_output', reason, source_files} for one folder (repo-relative), in the shape a
    skill records for its own walk. exclude forces build_output, include forces source (include wins)."""
    rel_dir = rel_dir.replace('\\', '/').strip('/')
    inc = {i.replace('\\', '/').strip('/') for i in include or ()}
    exc = {e.replace('\\', '/').strip('/') for e in exclude or ()}
    if rel_dir in exc and rel_dir not in inc:
        is_b, why = True, f'{rel_dir}/ excluded by the caller (--exclude)'
    else:
        is_b, why = (evidence or BuildEvidence(root, inc)).decide(rel_dir)
    return {'dir': rel_dir, 'decision': 'build_output' if is_b else 'source', 'reason': why,
            'source_files': count_sources(Path(root) / rel_dir)}


def _go_package(path: Path, name: str) -> bool:
    if name not in BUILD_OUTPUT:
        return False
    try:
        return any(f.endswith('.go') for f in os.listdir(path))
    except OSError:
        return False


def exclusion_reason(rel_dir: str, name: str, files: list[str], subs: set[str]) -> str | None:
    """Why the directory rel_dir (its last part is name; files are its file names) is not product code, or None.
    Folders named like build output (BUILD_NAMES) are decided by census() with BuildEvidence instead."""
    if name in PRUNE_DIRS:
        return f'{name}/ is a dependency, VCS, cache, build-output or header directory'
    if name.endswith(BUNDLE_SUFFIXES):
        return f'{name} is a built bundle'
    if rel_dir in subs:
        return 'a git submodule (.gitmodules)'
    parts = rel_dir.split('/')
    under_external = any(p.lower() in EXTERNAL_PARENTS for p in parts[:-1])
    if under_external:
        manifest = [m for m in MANIFESTS if m in files] + sorted(f for f in files if f.endswith('.podspec'))
        licence = sorted(f for f in files if LICENSE_RX.match(f))
        if manifest and licence:
            return (f'an embedded third-party package (its own {manifest[0]} and {licence[0]}) under '
                    f'{"/".join(p for p in parts[:-1] if p.lower() in EXTERNAL_PARENTS)}/')
        exts = Counter(PurePosixPath(f).suffix for f in files)
        if exts['.h'] >= HEADER_ONLY_MIN and not (exts['.c'] or exts['.m'] or exts['.mm'] or exts['.cpp'] or exts['.cc']):
            return f'a header-only tree ({exts[".h"]} .h files, no implementation) under a driver/SDK directory'
    return None


def census(root: Path | str, extra_exclude: list[str] | None = None, include: list[str] | None = None,
           exclude_fixtures: bool = False, fixture_dirs=(), product_dirs=()) -> dict:
    """Source files per language under root, with vendored and third-party trees left out (see the module doc).
    extra_exclude: repo-relative directories the caller also knows are not product code. include: repo-relative
    directories to count even though a rule would leave them out (recorded under `included`).
    exclude_fixtures: also leave out the nested sample and fixture projects repo_layout.fixture_projects() finds
    (fixture_dirs: further fixture parent names or paths); they are listed under `fixtures_excluded` ({dir, reason,
    markers}) and in `excluded` with kind 'fixture'. Off by default (the result then has no fixtures_excluded key).

    Folders named like build output (dist/, build/, out/, obj/) are decided by evidence (BuildEvidence) and every
    decision is listed under `build_output` ({path, decision: source|build_output, reason}). JavaScript and
    TypeScript files that look generated (minified, bundled, Emscripten glue or file-packager manifests) are counted
    under `generated` and listed in `generated_files`, not as hand-written code; their folder is still walked.

    Script languages no skill parses (SCRIPT_BY_EXT: PowerShell, shell, batch ...) are counted under `scripts`
    ({kind: files}), `script_lines` (non-blank lines outside test and tooling folders) and `script_examples`.
    `tooling_dirs` lists each tooling-named folder that holds code ({path, files, lines, languages: {lang: lines},
    evidence}) with the signs that it is product code after all (tooling_evidence()); product_dirs names
    tooling-named folders to count as product code (recorded under `product_dirs`)."""
    root = Path(root).resolve()
    subs = submodules(root)
    extra = {e.replace('\\', '/').strip('/') for e in (extra_exclude or [])}
    inc = {e.replace('\\', '/').strip('/') for e in (include or [])}
    evidence = BuildEvidence(root, inc)
    fixtures: list[dict] = []
    fixture_note = None
    if exclude_fixtures:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import repo_layout   # noqa: E402  (shipped beside this script)
            fixtures = repo_layout.fixture_projects(root, fixture_dirs)
        except ImportError:
            fixture_note = 'fixture projects were not excluded: repo_layout.py is not available beside lang_census.py'
        finally:
            sys.path.pop(0)
    fixture_why = {f['dir']: f['reason'] for f in fixtures}
    langs: Counter = Counter()
    markup: Counter = Counter()
    ui_markup_examples: dict[str, list[str]] = {}
    generated: Counter = Counter()
    generated_files: list[dict] = []
    examples: dict[str, list[str]] = {}
    by_lang_dirs: dict[str, Counter] = {}
    excluded: list[dict] = []
    build_output: list[dict] = []
    included: list[str] = []
    data_trees: list[dict] = []
    py_files: list[str] = []
    template_files: list[str] = []
    lines: Counter = Counter()
    product_files: Counter = Counter()
    test_files: Counter = Counter()
    test_lines: Counter = Counter()
    tooling_files: Counter = Counter()
    tooling_lines: Counter = Counter()
    manifest_dirs: set[str] = set()
    prod_dirs = sorted({e.replace('\\', '/').strip('/') for e in (product_dirs or []) if e.strip('/\\.')})
    prod_prefix = tuple(d + '/' for d in prod_dirs)
    tooling: dict[str, dict] = {}
    test_paths: list[str] = []
    scripts: Counter = Counter()
    script_lines: Counter = Counter()
    script_all_lines: Counter = Counter()
    script_examples: dict[str, list[str]] = {}
    for dp, dns, fns in os.walk(root):
        rel = Path(dp).relative_to(root).as_posix()
        rel = '' if rel == '.' else rel
        if rel and any(m in fns for m in MANIFESTS):
            manifest_dirs.add(rel)
        keep = []
        for d in sorted(dns):
            sub = f'{rel}/{d}' if rel else d
            try:
                files = [f for f in os.listdir(os.path.join(dp, d))]
            except OSError:
                files = []
            if sub in inc:
                included.append(sub)
                keep.append(d)
                continue
            why = 'excluded by the caller' if sub in extra else exclusion_reason(sub, d, files, subs)
            kind = None
            if not why and sub in fixture_why:
                why, kind = 'fixture project: ' + fixture_why[sub], 'fixture'
            if not why and d in BUILD_NAMES and not _go_package(Path(dp) / d, d):
                is_b, reason = evidence.decide(sub)
                build_output.append({'path': sub, 'decision': 'build_output' if is_b else 'source', 'reason': reason})
                why, kind = (reason, 'build_output') if is_b else (None, None)
            if why:
                if d not in ('.git', '.hg', '.svn', '__pycache__'):   # noise nobody needs listed
                    if d in DEPENDENCY_TREES:   # thousands of installed files: listed, never walked to count them
                        entry = {'path': sub, 'reason': why, 'source_files': 0, 'source_files_counted': False}
                    else:
                        entry = {'path': sub, 'reason': why, 'source_files': count_sources(Path(dp) / d)}
                    if kind:
                        entry['kind'] = kind
                        entry['languages'] = count_sources_by_language(Path(dp) / d)
                    excluded.append(entry)
                continue
            data_why = data_dir_reason([f for f in files if os.path.isfile(os.path.join(dp, d, f))])
            if data_why:   # never walked: data files are not a language, and a data tree holds no product code
                data_trees.append({'path': sub, 'files': len(files), 'reason': data_why})
                continue
            keep.append(d)
        dns[:] = keep
        for f in fns:
            frel = f'{rel}/{f}' if rel else f
            lang = LANG_BY_EXT.get(PurePosixPath(f).suffix)
            if lang:
                text = _read_head(Path(dp) / f, LINE_READ_MAX)
                if lang in JS_FAMILY:
                    gen = generated_reason(frel, text[:GENERATED_READ])
                    if gen:
                        generated[lang] += 1
                        if len(generated_files) < 200:
                            generated_files.append({'path': frel, 'language': lang, 'reason': gen})
                        continue
                langs[lang] += 1
                n_lines = count_lines(text)
                if is_test_file(frel, manifest_dirs):
                    test_files[lang] += 1
                    test_lines[lang] += n_lines
                    test_paths.append(frel)
                elif is_tooling_file(frel) and not (prod_prefix and frel.startswith(prod_prefix)):
                    tooling_files[lang] += 1
                    tooling_lines[lang] += n_lines
                    t = tooling.setdefault(tooling_dir(frel), {'files': 0, 'lines': 0, 'languages': Counter()})
                    t['files'] += 1
                    t['lines'] += n_lines
                    t['languages'][lang] += n_lines
                else:
                    product_files[lang] += 1
                    lines[lang] += n_lines
                if len(examples.setdefault(lang, [])) < 3:
                    examples[lang].append(frel)
                top = rel.split('/')[0] if rel else '.'
                by_lang_dirs.setdefault(lang, Counter())[top] += 1
                if lang == 'Python':
                    py_files.append(frel)
                continue
            sk = SCRIPT_BY_EXT.get(PurePosixPath(f).suffix.lower())
            if sk:
                scripts[sk] += 1
                n_lines = count_lines(_read_head(Path(dp) / f, LINE_READ_MAX))
                script_all_lines[sk] += n_lines
                product = not is_test_file(frel, manifest_dirs) and (
                    not is_tooling_file(frel) or bool(prod_prefix and frel.startswith(prod_prefix)))
                if product:
                    script_lines[sk] += n_lines
                ex = script_examples.setdefault(sk, [])
                if product and frel not in ex:
                    ex.insert(0, frel)      # shipped scripts are shown before test and tooling ones
                elif len(ex) < 3:
                    ex.append(frel)
                del ex[3:]
                continue
            mk = markup_kind(frel)
            if mk:
                markup[mk] += 1
                if mk in UI_MARKUP_KINDS and len(ui_markup_examples.setdefault(mk, [])) < 3:
                    ui_markup_examples[mk].append(frel)
                if mk == 'Templates':
                    template_files.append(frel)
    ordered = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    # the dominant language: the most non-blank lines of product code; with no product code (a standalone test
    # repository) the most lines of test code, and tooling folders only when neither holds a line; with no line at all
    # (empty files), the most product files, then the most files
    if sum(lines.values()):
        basis, deciding_lines, deciding_files = 'product', lines, product_files
    elif sum(test_lines.values()):
        basis, deciding_lines, deciding_files = 'test', test_lines, test_files
    else:
        basis, deciding_lines, deciding_files = 'tooling', tooling_lines, tooling_files
    if not sum(product_files.values()):
        deciding_files = test_files if basis == 'test' else tooling_files
    if sum(deciding_lines.values()):
        dominant = max(deciding_lines, key=lambda k: (deciding_lines[k], deciding_files[k], k))
        dominant_by = 'lines'
    elif sum(deciding_files.values()):
        dominant = min(deciding_files, key=lambda k: (-deciding_files[k], k))
        dominant_by = 'files'
    else:
        dominant = next(iter(ordered), None)
        dominant_by = 'files' if dominant else None
    def _sorted(cnt: Counter) -> dict:
        return dict(sorted(((k, v) for k, v in cnt.items()), key=lambda kv: (-kv[1], kv[0])))
    # tooling-named folders that hold code: is any of it product code after all? (read only when there is one)
    tool_ev = tooling_evidence(root, sorted(tooling), sorted(test_paths)) if tooling else {}
    tooling_dirs = [{'path': d, 'files': t['files'], 'lines': t['lines'], 'languages': _sorted(t['languages']),
                     'evidence': tool_ev.get(d, [])}
                    for d, t in sorted(tooling.items(), key=lambda kv: (-kv[1]['lines'], kv[0]))]
    # app modules first (shallow paths, app/main/server names), so the framework import is found early
    py_files.sort(key=lambda r: (r.count('/'), not re.search(r'(^|/)(app|main|server|wsgi|asgi|views|urls|routes)\.py$', r), r))
    return {'root': root.name, 'languages': ordered, 'total_source_files': sum(langs.values()),
            'dominant': dominant, 'dominant_files': ordered.get(dominant, 0) if dominant else 0,
            'dominant_by': dominant_by, 'dominant_lines': deciding_lines.get(dominant, 0) if dominant else 0,
            'dominant_basis': basis if dominant_by == 'lines' else None,
            'lines': _sorted(lines), 'product_files': _sorted(product_files),
            'test_files': _sorted(test_files), 'test_lines': _sorted(test_lines),
            'tooling_files': _sorted(tooling_files), 'tooling_lines': _sorted(tooling_lines),
            'tooling_dirs': tooling_dirs, 'product_dirs': prod_dirs,
            'scripts': dict(sorted(scripts.items())), 'script_lines': _sorted(script_lines),
            'script_total_lines': _sorted(script_all_lines), 'script_examples': script_examples,
            'total_product_lines': sum(lines.values()), 'total_test_lines': sum(test_lines.values()),
            'top_dirs': {k: dict(v.most_common(5)) for k, v in by_lang_dirs.items()},
            'examples': examples,
            'excluded': excluded,
            'excluded_source_files': sum(e['source_files'] for e in excluded),
            'build_output': build_output,
            'included': included,
            'generated': dict(sorted(generated.items())),
            'generated_source_files': sum(generated.values()),
            'generated_files': generated_files,
            'markup': dict(sorted(markup.items())),
            'ui_markup': {k: v for k, v in sorted(markup.items()) if k in UI_MARKUP_KINDS},
            'ui_markup_examples': ui_markup_examples,
            'data_trees': data_trees,
            'server_rendered': server_rendered(root, py_files, template_files),
            **({'fixtures_excluded': fixtures} if exclude_fixtures else {}),
            **({'fixtures_note': fixture_note} if fixture_note else {})}


def count_sources_by_language(d: Path, cap: int = 200000) -> dict[str, int]:
    out: Counter = Counter()
    for _, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in ('.git', 'node_modules')]
        for f in fns:
            lang = LANG_BY_EXT.get(PurePosixPath(f).suffix)
            if lang:
                out[lang] += 1
        if sum(out.values()) > cap:
            break
    return dict(out.most_common())


def count_sources(d: Path, cap: int = 200000) -> int:
    n = 0
    for _, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in ('.git', 'node_modules')]
        n += sum(1 for f in fns if PurePosixPath(f).suffix in LANG_BY_EXT)
        if n > cap:
            break
    return n


def excluded_path(c: dict, rel: str) -> str | None:
    """The reason rel (a repo-relative path) lies in an excluded tree of census c, or None."""
    rel = rel.replace('\\', '/').strip('/')
    for e in c.get('excluded', []):
        if rel == e['path'] or rel.startswith(e['path'] + '/'):
            return e['reason']
    return None


def language_coverage(c: dict, supported, tool: str = 'this script', what: str = 'full analysis',
                      min_files: int = 3, reads_templates: bool = False) -> dict:
    """Supported against unsupported source-file counts for a tool that analyses only `supported` languages.

    status 'unsupported_dominant' (with a loud `warning`) when the unsupported language with the most lines of
    PRODUCT code (census `lines`; test files and test directories are counted apart and never decide) has more lines
    than all supported languages together, and at least min_files files (tests included) or LINE_RULE_MIN_LINES lines; so a
    long Rust workspace next to many short Python scripts and tests is warned about even though Python has more files.
    `decided_by` says which rule applied: 'lines', or 'files' (the old rule: more source files than all supported
    languages together, and at least min_files) when the census counted no lines or a caller merged other file counts
    into `languages`. 'partial' when some unsupported languages are present but do not dominate; 'ok' when everything
    counted is supported; 'no_source' when nothing was counted.

    A tool that reads server-side templates (reads_templates=True, or 'HTML' among `supported`) is not warned about a
    server-rendered web app (census `server_rendered`: a Flask, Django or other Python framework rendering template
    files): the templates are the browser-facing code it reads, and the server language is outside its scope rather
    than unanalysed product UI. That case is status 'partial' with `server_rendered` and a `note` to carry into the
    report instead of a warning."""
    supported = set(supported)
    reads_templates = reads_templates or 'HTML' in supported
    supported.discard('HTML')
    langs = c.get('languages', {})
    sup = {k: v for k, v in langs.items() if k in supported}
    uns = {k: v for k, v in langs.items() if k not in supported}
    n_sup, n_uns = sum(sup.values()), sum(uns.values())
    out = {'tool': tool, 'supported_languages': sorted(supported), 'supported': sup, 'unsupported': uns,
           'supported_files': n_sup, 'unsupported_files': n_uns, 'dominant': c.get('dominant'),
           'excluded_source_files': c.get('excluded_source_files', 0), 'status': 'ok', 'warning': None}
    notes: list[str] = []
    examples = c.get('examples') or {}
    for lang, n in sorted(uns.items(), key=lambda kv: (-kv[1], kv[0])):
        shown = ', '.join(examples.get(lang, [])[:3])
        notes.append(f'{n} {lang} source file(s){" (" + shown + ("" if n <= 3 else ", ...") + ")" if shown else ""} '
                     f'not analysed by {tool}: record {lang} as not assessed, never as clean.')
    # folders named like build output that were left out but hold a supported language: say so, with the override
    pruned = [e for e in c.get('excluded', []) if e.get('kind') == 'build_output'
              and sum(v for k, v in (e.get('languages') or {}).items() if k in supported)]
    hint = ''
    if pruned:
        parts = []
        for e in pruned[:3]:
            sup_n = {k: v for k, v in e['languages'].items() if k in supported}
            counts = ', '.join(f'{v} {k}' for k, v in sup_n.items())
            parts.append(f'{e["path"]}/ ({counts} file(s); {e["reason"]})')
        hint = (f' Left out as build output: {"; ".join(parts)}. If that folder is hand-written source, rerun with '
                f'--include {pruned[0]["path"]}.')
        notes.append(hint.strip())
    for b in c.get('build_output') or []:
        if b.get('decision') == 'source':
            notes.append(f'{b["path"]}/ is counted as source although named like build output: {b["reason"]}.')
    ui = {k: v for k, v in (c.get('ui_markup') or {}).items() if k not in supported}
    if ui:
        out['ui_markup'] = ui
        for kind, n in sorted(ui.items(), key=lambda kv: (-kv[1], kv[0])):
            shown = ', '.join((c.get('ui_markup_examples') or {}).get(kind, [])[:3])
            notes.append(f'{n} {kind} UI markup file(s){" (" + shown + ("" if n <= 3 else ", ...") + ")" if shown else ""} '
                         f'present but not analysed by {tool}: the user interface they describe is not assessed.')
    scr = {k: v for k, v in (c.get('scripts') or {}).items() if k not in supported}
    scr_lines = {k: (c.get('script_lines') or {}).get(k, 0) for k in scr}
    if scr:
        out['scripts'] = scr
        all_lines = c.get('script_total_lines') or {}
        for kind, n in sorted(scr.items(), key=lambda kv: (-kv[1], kv[0])):
            shown = ', '.join((c.get('script_examples') or {}).get(kind, [])[:3])
            notes.append(f'{n} {kind} script(s), {all_lines.get(kind, 0)} line(s)'
                         f'{" (" + shown + ("" if n <= 3 else ", ...") + ")" if shown else ""} present but not '
                         f'analysed by {tool}: record {kind} as not assessed, never as clean; an installer or '
                         f'packaging script among them is shipped code.')
    # code in tooling-named folders that the tests, the CI or a manifest treat as the product
    kind_text = {'tests': 'a test loads it', 'ci_coverage': 'coverage is measured on it', 'ci_runs': 'CI runs it',
                 'manifest': 'a manifest ships it'}
    as_product = []
    for t in c.get('tooling_dirs') or []:
        ev = t.get('evidence') or []
        mine = {k: v for k, v in (t.get('languages') or {}).items() if k in supported}
        if not ev or not mine:
            continue
        as_product.append({'path': t['path'], 'lines': sum(mine.values()), 'languages': mine,
                           'evidence': sorted({e['kind'] for e in ev})})
        seen, why = set(), []
        for e in ev:
            if e['kind'] not in seen:
                seen.add(e['kind'])
                why.append(f'{kind_text.get(e["kind"], e["kind"])} ({e["file"]}' + (f':{e["line"]}' if e.get('line') else '') + ')')
        total = sum((c.get('lines') or {}).values())
        notes.append(f'{sum(mine.values())} line(s) of {", ".join(mine)} under {t["path"]}/ are counted as tooling, '
                     f'apart from the {total} product line(s), although {"; ".join(why)}. If that folder is product '
                     f'code, say so (lang_census.py --product-dir {t["path"]}, or the calling script\'s own option) '
                     f'and do not read the product-line figures as the whole product.')
    if as_product:
        out['tooling_as_product'] = as_product
    for lang in sorted(k for k in (c.get('tooling_lines') or {}) if k in supported):
        if (c.get('lines') or {}).get(lang) or not sum((c.get('lines') or {}).values()):
            continue
        where = [t['path'] + '/' for t in c.get('tooling_dirs') or [] if lang in (t.get('languages') or {})][:3]
        notes.append(f'{lang} has no product line in this census: its {c["tooling_lines"][lang]} line(s) are all in '
                     f'tooling folders ({", ".join(where)}), counted apart from product code.')
    gen = c.get('generated') or {}
    if gen:
        out['generated'] = gen
        notes.append(f'{sum(gen.values())} generated file(s) ({", ".join(f"{v} {k}" for k, v in gen.items())}: '
                     f'minified bundles, Emscripten glue, file-packager manifests) are counted apart, not as '
                     f'hand-written code (census generated_files lists them).')
    if notes:
        out['notes'] = notes
    def script_dominant(counted_lines: int):
        """The script language whose product lines outweigh every counted language's, with its lines, else None."""
        if not scr_lines:
            return None
        kind = max(scr_lines, key=lambda k: (scr_lines[k], k))
        n = scr_lines[kind]
        return (kind, n) if n > counted_lines and (n >= LINE_RULE_MIN_LINES or scr[kind] >= min_files
                                                   or not counted_lines) and n else None
    if not langs:
        out['status'] = 'no_source'
        if hint:
            out['warning'] = f'No source file was counted.{hint}'
        sd = script_dominant(0)
        if sd:
            out.update({'status': 'unsupported_dominant', 'dominant_unsupported_language': sd[0],
                        'dominant_unsupported_files': scr[sd[0]], 'dominant_unsupported_lines': sd[1],
                        'decided_by': 'lines', 'supported_lines': 0, 'unsupported_lines': sum(scr_lines.values())})
            out['warning'] = (f'This repository holds no source file in a language {tool} counts, only {sd[0]} scripts '
                              f'({sd[1]} lines in {scr[sd[0]]} file(s)), which it does not analyse: its output is NOT a '
                              f'{what} of this repository. Record {sd[0]} as not assessed.' + hint)
        return out
    if uns:
        lines = c.get('lines') or {}
        tests = c.get('test_files') or {}
        prod = c.get('product_files') or {}
        tooling = c.get('tooling_files') or {}
        basis = 'product code'
        if not sum(lines.values()):   # as in census: test code decides a repository with no product code, tooling
            if sum((c.get('test_lines') or {}).values()):   # folders only when nothing else holds a line
                lines, prod, basis = c.get('test_lines') or {}, tests, 'test code (there is no product code)'
            else:
                lines, prod, basis = c.get('tooling_lines') or {}, tooling, 'tooling code (nothing else has a line)'
        # lines decide when the census counted them and nobody merged other counts into `languages` since
        by_lines = (c.get('dominant_by') == 'lines' and sum(lines.values()) > 0
                    and sum((c.get('product_files') or {}).values()) + sum(tests.values()) + sum(tooling.values())
                    == sum(langs.values()))
        if by_lines:
            uns_l = {k: lines.get(k, 0) for k in uns}
            sup_l = sum(lines.get(k, 0) for k in sup)
            lang = max(uns_l, key=lambda k: (uns_l[k], prod.get(k, 0), k))
            n_l, n = uns_l[lang], prod.get(lang, 0)
            dominates = n_l > sup_l and (langs.get(lang, 0) >= min_files or n_l >= LINE_RULE_MIN_LINES)
            out.update({'decided_by': 'lines', 'supported_lines': sup_l, 'unsupported_lines': sum(uns_l.values())})
            size = (f'{n_l} lines of {basis} in {n} file(s), against {sup_l} line(s) in the supported '
                    f'language(s); ' + ('test code and tooling folders counted apart' if basis == 'product code'
                                        else 'other code counted apart'))
        else:
            lang, n = max(uns.items(), key=lambda kv: (kv[1], kv[0]))
            dominates = n > n_sup and n >= min_files
            out['decided_by'] = 'files'
            size = f'{n} source files'
        sr = c.get('server_rendered') or {}
        if dominates and reads_templates and sr.get('server_language') == lang:
            out['status'] = 'partial'
            out['server_rendered'] = sr
            out['note'] = (f'This repository is a server-rendered {sr["framework"]} web app: its {sr["templates"]} '
                           f'template file(s) ({", ".join(sr["template_dirs"][:3])}) are the browser-facing code, which '
                           f'{tool} reads. Its {n} {lang} file(s) are the server that renders them, not browser code; '
                           f'record that server code as outside this {what}, not as the UI left unread.')
        elif dominates:
            out['status'] = 'unsupported_dominant'
            out['dominant_unsupported_language'], out['dominant_unsupported_files'] = lang, n
            if by_lines:
                out['dominant_unsupported_lines'] = n_l
            out['warning'] = (f'This repository is predominantly {lang} ({size}, vendored and third-party '
                              f'trees excluded), which {tool} does not analyse: it covers '
                              f'{", ".join(sorted(supported)) or "no language"} only. {n_sup} supported source '
                              f'file(s) were found, so its output covers that minority and is NOT a {what} of this '
                              f'repository\'s {lang} code. Do not present it as one; record {lang} as not assessed.'
                              + hint)
        else:
            out['status'] = 'partial'
    if out['status'] != 'unsupported_dominant' and c.get('dominant_by') == 'lines':
        product_lines = sum((c.get('lines') or {}).values())
        sd = script_dominant(product_lines) if product_lines or c.get('dominant_basis') == 'product' else None
        if sd:
            sup_l = sum((c.get('lines') or {}).get(k, 0) for k in sup)
            out.update({'status': 'unsupported_dominant', 'dominant_unsupported_language': sd[0],
                        'dominant_unsupported_files': scr[sd[0]], 'dominant_unsupported_lines': sd[1],
                        'decided_by': 'lines', 'supported_lines': sup_l,
                        'unsupported_lines': product_lines - sup_l + sum(scr_lines.values())})
            out['warning'] = (f'This repository is predominantly {sd[0]} scripts ({sd[1]} lines in {scr[sd[0]]} '
                              f'file(s) outside test and tooling folders, against {product_lines} product line(s) in '
                              f'every counted language), which {tool} does not analyse: it covers '
                              f'{", ".join(sorted(supported)) or "no language"} only, so its output is NOT a {what} '
                              f'of this repository\'s {sd[0]} code. Do not present it as one; record {sd[0]} as not '
                              f'assessed.' + hint)
    return out


EMSCRIPTEN_GLUE_SAMPLE = ('var Module = typeof Module != "undefined" ? Module : {};\n'
                          'var ENVIRONMENT_IS_WEB = typeof window == "object";\n'
                          'var wasmMemory, HEAP8, HEAPU8, HEAP32;\n'
                          'function addRunDependency(id) {}\n'
                          'function UTF8ToString(ptr) { return ""; }\n')


def _git_commit_all(d: Path) -> bool:
    """git init + commit everything in d (self-test helper); False when git is not available."""
    import subprocess
    cmd = ['git', '-C', str(d), '-c', 'user.name=t', '-c', 'user.email=t@example.test', '-c', 'commit.gpgsign=false']
    try:
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'init']):
            if subprocess.run(cmd + args, capture_output=True, timeout=60).returncode:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _build_output_checks(tree) -> list:
    """Self-test cases for the build-output decision (dist/, build/, out/), generated JavaScript and the notes
    language_coverage carries about unanalysed languages and pruned trees."""
    checks = []

    def call(fn_name, *a, **k):
        fn = globals().get(fn_name)
        if fn is None:
            return {'error': f'{fn_name} is missing'}
        try:
            return fn(*a, **k)
        except Exception as e:   # noqa: BLE001
            return {'error': repr(e)}

    def safe_census(root, **k):
        try:
            return census(root, **k)
        except Exception as e:   # noqa: BLE001
            return {'error': repr(e), 'languages': {}, 'excluded': []}

    # a FastAPI server mounting StaticFiles on a committed, hand-written dist/ folder (no package.json, no bundler),
    # which also holds Emscripten loader glue, a file-packager manifest and a minified chart library
    spa = {'server.py': ('from fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles\n\napp = FastAPI()\n'
                         'app.mount("/", StaticFiles(directory="dist"), name="root")\n'),
           'tools/pack.py': 'def pack():\n    return 1\n',
           'dist/index.html': '<html><script src="app.js"></script></html>\n',
           'dist/app.js': 'function start() {\n  return 1;\n}\n',
           'dist/ui.js': 'export const panel = 1;\n',
           'dist/modules/menu.js': 'function menu() {\n  return [];\n}\n',
           'dist/modules/runtime.js': EMSCRIPTEN_GLUE_SAMPLE,
           'dist/modules/packages/en.js': ('var PACKAGE_DATA = {\n  files: [{\n    filename: "/assets/a.dat",\n'
                                           '    start: 0,\n    end: 10\n  }]\n};\n'),
           'dist/lib/chart.js': 'var a=' + ','.join(f'f{i}=function(x){{return x+{i}}}' for i in range(600)) + ';\n'}
    root = tree(spa)
    c = safe_census(root)
    bo = {b.get('path'): b for b in c.get('build_output') or []}
    checks.append(('a dist/ folder that the app serves with StaticFiles is counted as source, with the reason',
                   (bo.get('dist') or {}).get('decision') == 'source'
                   and 'server.py:5' in (bo.get('dist') or {}).get('reason', '')
                   and 'dist' not in {e['path'] for e in c.get('excluded', [])}, (bo, c.get('excluded'))))
    checks.append(('hand-written JavaScript in the served dist/ counts as JavaScript',
                   c.get('languages', {}).get('JavaScript') == 3, c.get('languages')))
    gen = {g['path']: g['reason'] for g in c.get('generated_files') or []}
    checks.append(('Emscripten glue, a file-packager manifest and a minified bundle are counted apart as generated',
                   (c.get('generated') or {}).get('JavaScript') == 3
                   and 'Emscripten' in gen.get('dist/modules/runtime.js', '')
                   and 'file-packager' in gen.get('dist/modules/packages/en.js', '')
                   and 'minified' in gen.get('dist/lib/chart.js', ''), (c.get('generated'), gen)))
    got = call('build_output_dirs', root)
    checks.append(('build_output_dirs() gives each build-named folder a decision and a reason',
                   isinstance(got, dict) and (got.get('dist') or {}).get('build_output') is False
                   and 'StaticFiles' in (got.get('dist') or {}).get('reason', ''), got))
    got = call('is_build_output', root, 'dist')
    checks.append(('is_build_output() says a served dist/ is not build output',
                   isinstance(got, tuple) and got[0] is False and 'served' in got[1], got))

    got = call('build_output_decision', root, 'dist')
    checks.append(('build_output_decision() gives {dir, decision, reason, source_files} for a skill\'s own walk',
                   isinstance(got, dict) and got.get('dir') == 'dist' and got.get('decision') == 'source'
                   and got.get('source_files') == 6 and 'StaticFiles' in got.get('reason', ''), got))
    got = call('build_output_decision', root, 'dist', exclude=['dist'])
    checks.append(('build_output_decision() honours an exclude list', isinstance(got, dict)
                   and got.get('decision') == 'build_output' and 'excluded by the caller' in got.get('reason', ''), got))

    # a Vite app: package.json builds into dist/, which holds the bundle (not served by any server in the repo)
    vite = {'package.json': '{"scripts": {"build": "vite build", "dev": "vite"}}',
            'src/main.js': 'import "./app.js";\n', 'src/app.js': 'export const a = 1;\n',
            'dist/assets/index.js': 'export const a = 1;\n', 'dist/index.html': '<html></html>\n'}
    root = tree(vite)
    c = safe_census(root)
    reasons = {e['path']: e['reason'] for e in c.get('excluded', [])}
    checks.append(('dist/ produced by a package.json build script is excluded as build output, naming the script',
                   'vite' in reasons.get('dist', '') and c.get('languages', {}).get('JavaScript') == 2, reasons))
    c = safe_census(root, include=['dist'])
    checks.append(('--include dist counts a folder the rules would leave out, and records the override',
                   c.get('languages', {}).get('JavaScript') == 3 and c.get('included') == ['dist']
                   and 'dist' not in {e['path'] for e in c.get('excluded', [])}, (c.get('languages'), c.get('included'))))

    # no git, no build config, not served: still left out (named like build output), and the guard names it
    plain = {f'server/m{i}.py': 'def f():\n    return 1\n' for i in range(5)}
    plain.update({f'dist/js/p{i}.js': 'function p() {\n  return 1;\n}\n' for i in range(4)})
    root = tree(plain)
    c = safe_census(root)
    reasons = {e['path']: e['reason'] for e in c.get('excluded', [])}
    checks.append(('with no evidence either way, a dist/ folder stays build output and says why',
                   'no evidence' in reasons.get('dist', ''), reasons))
    cov = language_coverage(c, {'JavaScript', 'TypeScript'}, tool='scan_ui.py') if 'error' not in c else {}
    checks.append(('the unsupported_dominant warning names the left-out folder that holds the supported language',
                   cov.get('status') == 'unsupported_dominant' and 'dist/' in (cov.get('warning') or '')
                   and '--include dist' in (cov.get('warning') or ''), cov.get('warning')))

    # not a git checkout, but a root .gitignore lists dist/: build output even though the app serves it
    ignored = {'.gitignore': 'node_modules/\ndist/\n', 'server.py': 'app.mount("/", StaticFiles(directory="dist"))\n',
               'dist/index.html': '<html></html>\n', 'dist/app.js': 'function a() {\n  return 1;\n}\n'}
    got = call('is_build_output', tree(ignored), 'dist')
    checks.append(('outside a git checkout, a dist/ folder the root .gitignore lists is build output, even when served',
                   isinstance(got, tuple) and got[0] is True and '.gitignore' in got[1], got))

    # git: a committed, hand-written dist/ (no build config) is source; a gitignored one is build output
    tracked = {'app.py': 'print(1)\n', 'site/dist/index.html': '<html></html>\n',
               'site/dist/main.js': 'function main() {\n  return 1;\n}\n'}
    root = tree(tracked)
    if _git_commit_all(root):
        got = call('is_build_output', root, 'site/dist')
        checks.append(('a git-tracked, hand-written dist/ with no build config is source',
                       isinstance(got, tuple) and got[0] is False and 'git-tracked' in got[1], got))
        (root / '.gitignore').write_text('out/\n', encoding='utf-8')
        (root / 'out').mkdir()
        (root / 'out' / 'bundle.js').write_text('var x = 1;\n', encoding='utf-8')
        got = call('is_build_output', root, 'out')
        checks.append(('a gitignored out/ folder is build output', isinstance(got, tuple) and got[0] is True
                       and 'gitignored' in got[1], got))
    else:
        print('SKIP git-tracked build-output cases: git is not available')

    # PHP next to a Python app: a tool that reads Python only must still say PHP was not analysed
    php = {f'app/m{i}.py': 'def f():\n    return 1\n' for i in range(4)}
    php.update({'index.php': '<?php echo 1;\n', '.htaccess': 'RewriteRule ^.*$ index.php [L]\n'})
    c = safe_census(tree(php))
    cov = language_coverage(c, {'Python'}, tool='list_x.py') if 'error' not in c else {}
    notes = ' '.join(cov.get('notes') or [])
    checks.append(('PHP present but not analysed is named in language_coverage notes, with its file',
                   cov.get('status') == 'partial' and 'PHP' in notes and 'index.php' in notes, cov))
    return checks


def _line_checks(tree) -> list:
    """Self-test cases for deciding the dominant language by product lines, with test code counted apart."""
    checks = []
    # a tide-table Cargo workspace (8 Rust files, long) next to many short Python helper scripts, half of them tests
    rs_body = 'pub fn tide(h: f64) -> f64 {\n    h * 2.0\n}\n' * 40       # 120 lines per file
    ws = {'Cargo.toml': '[workspace]\nmembers = ["crates/*"]\n'}
    for crate in ('tides-core', 'tides-cli'):
        ws[f'crates/{crate}/Cargo.toml'] = f'[package]\nname = "{crate}"\n'
        ws.update({f'crates/{crate}/src/m{i}.rs': rs_body for i in range(3)})
        ws[f'crates/{crate}/tests/it.rs'] = '#[test]\nfn t() {}\n'
    py_body = 'def f():\n    return 1\n'
    ws.update({f'gauges/g{i}.py': py_body for i in range(8)})
    ws.update({f'tests/test_tool{i}.py': 'import unittest\n' + py_body * 30 for i in range(8)})
    c = census(tree(ws))
    checks.append(('dominance is decided by product lines: a long Rust workspace beats more, shorter Python files',
                   c.get('dominant') == 'Rust' and c.get('dominant_by') == 'lines'
                   and c['languages'].get('Python', 0) > c['languages'].get('Rust', 0),
                   (c.get('dominant'), c.get('dominant_by'), c.get('languages'), c.get('lines'))))
    checks.append(('test files and test directories are counted apart (test_files, test_lines), never as product lines',
                   (c.get('test_files') or {}).get('Python') == 8 and (c.get('test_files') or {}).get('Rust') == 2
                   and (c.get('lines') or {}).get('Python') == 16 and (c.get('lines') or {}).get('Rust') == 720
                   and (c.get('test_lines') or {}).get('Python', 0) > 400
                   and (c.get('product_files') or {}).get('Python') == 8,
                   (c.get('lines'), c.get('test_lines'), c.get('test_files'), c.get('product_files'))))
    # helper scripts in scripts/ and docs/ (longer than the Rust code) are tooling: counted apart, not deciding
    tl = dict(ws)
    tl.update({f'scripts/report{i}.py': py_body * 60 for i in range(8)})
    tl.update({'docs/build_site.py': py_body * 20})
    c2 = census(tree(tl))
    checks.append(('helper scripts under scripts/ and docs/ are tooling (tooling_lines) and never beat the product',
                   c2.get('dominant') == 'Rust' and (c2.get('tooling_lines') or {}).get('Python') == 1000
                   and (c2.get('tooling_files') or {}).get('Python') == 9 and (c2.get('lines') or {}).get('Python') == 16,
                   (c2.get('dominant'), c2.get('lines'), c2.get('tooling_lines'), c2.get('tooling_files'))))
    only = census(tree({f'scripts/s{i}.py': py_body for i in range(4)}))
    checks.append(('a repository of tooling scripts alone is still decided by them',
                   only.get('dominant') == 'Python' and only.get('dominant_by') == 'lines', only.get('dominant_by')))
    # a standalone Playwright test repository: TypeScript specs, no product code, three Python helper scripts
    spec = "import { test, expect } from '@playwright/test';\n" + "test('x', async ({ page }) => {\n  await page.goto('/');\n});\n" * 20
    pw = {'package.json': '{"devDependencies": {"@playwright/test": "1.48.0"}}\n',
          'e2e/playwright.config.ts': 'export default {};\n'}
    pw.update({f'e2e/recipe{i}.spec.ts': spec for i in range(6)})
    pw.update({f'scripts/seed{i}.py': py_body * 10 for i in range(3)})
    tr = census(tree(pw))
    checks.append(('a standalone test repository (no product code): its test lines decide before tooling lines',
                   tr.get('dominant') == 'TypeScript' and tr.get('dominant_by') == 'lines',
                   (tr.get('dominant'), tr.get('lines'), tr.get('test_lines'), tr.get('tooling_lines'))))
    cov = language_coverage(tr, {'JavaScript', 'TypeScript'}, tool='scan_ts.py') if tr.get('languages') else {}
    checks.append(('a TypeScript tool on that test repository gets no loud warning from the helper scripts',
                   cov.get('status') != 'unsupported_dominant' and not cov.get('warning'), cov))
    cov = language_coverage(c, {'Python'}, tool='scan_py.py') if c.get('languages') else {}
    checks.append(('a Python-only tool is warned loudly when Rust has the most product lines, though Python has more files',
                   cov.get('status') == 'unsupported_dominant' and cov.get('dominant_unsupported_language') == 'Rust'
                   and 'lines' in (cov.get('warning') or ''), cov))
    cov = language_coverage(c, {'Rust'}, tool='scan_rs.py') if c.get('languages') else {}
    checks.append(('a Rust tool on the same repository is partial, not warned because Python has more (test) files',
                   cov.get('status') == 'partial' and not cov.get('warning'), cov))
    # an e2e/ folder that holds a package manifest is a product package, not test code
    pk = {'e2e/package.json': '{"name": "e2e", "bin": "cli.js"}', 'e2e/cli.js': 'console.log(1);\n' * 5,
          'e2e/test/cli.test.js': 'test("x", () => {});\n'}
    c = census(tree(pk))
    checks.append(('a test-named folder holding a package manifest is product code; its own test/ is test code',
                   (c.get('lines') or {}).get('JavaScript') == 5 and (c.get('test_files') or {}).get('JavaScript') == 1,
                   (c.get('lines'), c.get('test_files'))))
    # a weather-station logger with a desktop UI described in UI markup files next to its Rust code
    ui = dict(ws)
    ui.update({f'crates/tides-cli/ui/panel{i}.slint': 'export component Panel {\n  Text { text: "x"; }\n}\n' * 200
               for i in range(4)})
    ui.update({'qml/Main.qml': 'Item {}\n', 'win/App.xaml': '<Window/>\n', 'forms/main.ui': '<ui/>\n',
               'gtk/main.glade': '<interface/>\n', 'kivy/app.kv': '<Root>:\n'})
    c = census(tree(ui))
    um = c.get('ui_markup') or {}
    checks.append(('UI markup (.slint, .qml, .xaml, .ui, .glade, .kv) is counted apart and never decides dominance',
                   um == {'Slint': 4, 'QML': 1, 'XAML': 1, 'UI definition': 1, 'Glade': 1, 'Kivy': 1}
                   and c.get('dominant') == 'Rust' and 'Slint' not in c['languages']
                   and (c.get('markup') or {}).get('Slint') == 4, (um, c.get('dominant'), c.get('markup'))))
    cov = language_coverage(c, {'Rust', 'Python'}, tool='scan_rs.py')
    notes = ' '.join(cov.get('notes') or [])
    checks.append(('language_coverage names UI markup as present but not analysed, without changing the status',
                   cov.get('status') == 'ok' and 'Slint' in notes and 'panel0.slint' in notes and 'not analysed' in notes
                   and (cov.get('ui_markup') or {}).get('Slint') == 4, cov))
    # installer and packaging scripts in a language no tool reads: counted apart and named, never invisible
    sc = dict(ws)
    sc.update({'packaging/Install-Runtime.ps1': 'Write-Host "installing"\n' * 60,
               'packaging/setup.sh': '#!/bin/sh\necho setup\n', 'scripts/fix-perms.psm1': 'function Fix {}\n',
               'tests/fixtures/fake.bash': 'echo fake\n'})
    c = census(tree(sc))
    checks.append(('PowerShell and shell scripts are counted apart (scripts, script_lines), never as a language',
                   c.get('scripts') == {'PowerShell': 2, 'Shell': 2} and 'PowerShell' not in c['languages']
                   and (c.get('script_lines') or {}).get('PowerShell') == 60 and c.get('dominant') == 'Rust',
                   (c.get('scripts'), c.get('script_lines'), c.get('dominant'))))
    cov = language_coverage(c, {'Rust', 'Python'}, tool='scan_rs.py')
    notes = ' '.join(cov.get('notes') or [])
    checks.append(('language_coverage names unanalysed script languages with an example, status unchanged',
                   cov.get('status') == 'ok' and 'PowerShell' in notes and 'Install-Runtime.ps1' in notes
                   and 'Shell' in notes and (cov.get('scripts') or {}).get('PowerShell') == 2, cov))
    cov = language_coverage(c, {'Rust', 'Python', 'PowerShell', 'Shell'}, tool='scan_all.py')
    checks.append(('a tool that reads the script languages gets no note about them',
                   'PowerShell' not in ' '.join(cov.get('notes') or []) and not cov.get('scripts'), cov))
    so = census(tree({'install.sh': 'echo step\n' * 600, 'lib/common.sh': 'echo lib\n' * 50}))
    cov = language_coverage(so, {'Python'}, tool='scan_py.py')
    checks.append(('a repository of shell scripts alone is unsupported_dominant with a warning, never no_source',
                   cov.get('status') == 'unsupported_dominant' and 'Shell' in (cov.get('warning') or '')
                   and cov.get('dominant_unsupported_language') == 'Shell', cov))
    sd = census(tree({'install.sh': 'echo step\n' * 600, 'helper.py': py_body * 5}))
    cov = language_coverage(sd, {'Python'}, tool='scan_py.py')
    checks.append(('shell scripts that outweigh the analysed product code are unsupported_dominant',
                   cov.get('status') == 'unsupported_dominant' and cov.get('dominant_unsupported_language') == 'Shell'
                   and cov.get('decided_by') == 'lines' and '600' in (cov.get('warning') or ''), cov))
    # product code that lives in a tooling-named folder: the CI measures it and the tests load it
    tp = dict(ws)
    tp.update({f'scripts/report{i}.py': py_body * 60 for i in range(8)})
    tp['tests/test_reports.py'] = ('import sys\nfrom pathlib import Path\n'
                                   'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))\n'
                                   'import report0\n')
    tp['.github/workflows/ci.yml'] = ('jobs:\n  test:\n    steps:\n'
                                      '      - run: coverage run --source=scripts -m unittest discover -s tests\n'
                                      '      - run: python scripts/report1.py --check\n')
    tp['pyproject.toml'] = '[project]\nname = "tides"\n[tool.setuptools]\nscript-files = ["scripts/report2.py"]\n'
    c = census(tree(tp))
    td = (c.get('tooling_dirs') or [{}])[0]
    kinds = {e.get('kind') for e in td.get('evidence') or []}
    checks.append(('a tooling-named folder lists the evidence that it holds product code (tooling_dirs)',
                   td.get('path') == 'scripts' and td.get('lines') == 960 and td.get('languages') == {'Python': 960}
                   and {'tests', 'ci_coverage', 'ci_runs', 'manifest'} <= kinds
                   and (c.get('lines') or {}).get('Python') == 16, (c.get('tooling_dirs'), c.get('lines'))))
    cov = language_coverage(c, {'Python'}, tool='scan_py.py')
    notes = ' '.join(cov.get('notes') or [])
    checks.append(('language_coverage says that scripts/ was counted as tooling although tests and CI use it',
                   'scripts/' in notes and 'counted as tooling' in notes and '--product-dir scripts' in notes
                   and '960' in notes and 'ci.yml:4' in notes
                   and [t.get('path') for t in cov.get('tooling_as_product') or []] == ['scripts'], cov))
    cov = language_coverage(c2, {'Python'}, tool='scan_py.py')
    checks.append(('helper scripts nothing tests, measures or ships are not called product code',
                   not cov.get('tooling_as_product') and '--product-dir' not in ' '.join(cov.get('notes') or []), cov))
    c = census(tree(tp), product_dirs=['scripts'])
    checks.append(('product_dirs counts a tooling-named folder as product code',
                   (c.get('lines') or {}).get('Python') == 976 and not (c.get('tooling_lines') or {}).get('Python')
                   and c.get('product_dirs') == ['scripts'], (c.get('lines'), c.get('tooling_lines'))))
    zp = census(tree({'scripts/build.py': py_body * 40, 'src/main.rs': rs_body}))
    cov = language_coverage(zp, {'Python'}, tool='scan_py.py')
    checks.append(('a language whose every line is in a tooling folder is said to have no product line',
                   'no product line' in ' '.join(cov.get('notes') or []) and 'scripts/' in ' '.join(cov.get('notes') or []),
                   cov.get('notes')))
    return checks


def self_test() -> int:
    checks = []

    def tree(files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp(prefix='lang-census-'))
        for rel, body in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(body, encoding='utf-8')
        return d

    # a Go CLI that embeds a third-party iOS driver (TypeScript tooling, Objective-C, a header tree), a vendored Go
    # module, a built app bundle and node_modules
    files = {'go.mod': 'module example.test/kiosk\n', 'main.go': 'package main\n'}
    files.update({f'pkg/runner/f{i}.go': 'package runner\n' for i in range(12)})
    files.update({f'pkg/runner/f{i}_test.go': 'package runner\n' for i in range(6)})
    files.update({'drivers/ios/AgentKit/package.json': '{"name": "agent-kit"}', 'drivers/ios/AgentKit/LICENSE': 'BSD'})
    files.update({f'drivers/ios/AgentKit/lib/m{i}.ts': '' for i in range(30)})
    files.update({f'drivers/ios/AgentKit/Agent/Lib/c{i}.m': '' for i in range(25)})
    files.update({f'drivers/ios/AgentKit/PrivateHeaders/XCTest/XC{i}.h': '' for i in range(60)})
    files.update({f'drivers/ios/prebuilt/Runner.app/Frameworks/XCTest.framework/Headers/H{i}.h': '' for i in range(50)})
    files.update({f'drivers/ios/headers-only/XCT{i}.h': '' for i in range(40)})
    files.update({f'vendor/example.test/dep/d{i}.go': 'package dep\n' for i in range(40)})
    files.update({f'npm/wrapper/node_modules/x/i{i}.js': '' for i in range(40)})
    files.update({'npm/wrapper/package.json': '{"name": "kiosk"}', 'npm/wrapper/install.js': ''})
    root = tree(files)
    c = census(root)
    checks.append(('a Go repo with a big vendored driver tree is Go-dominant', c['dominant'] == 'Go'
                   and c['languages'].get('Go') == 19, (c['dominant'], c['languages'])))
    reasons = {e['path']: e['reason'] for e in c['excluded']}
    checks.append(('the embedded third-party package is excluded with its manifest and licence named',
                   'embedded third-party package' in reasons.get('drivers/ios/AgentKit', ''), reasons))
    checks.append(('a built .app bundle, vendor/ and node_modules/ are excluded',
                   all(p in reasons for p in ('drivers/ios/prebuilt/Runner.app', 'vendor', 'npm/wrapper/node_modules')),
                   sorted(reasons)))
    nm = next((e for e in c['excluded'] if e['path'] == 'npm/wrapper/node_modules'), {})
    # reading a small file never asks for a multi-megabyte buffer (text-mode read(n) sizes its buffer by n)
    asked = []
    real_open = open

    class _Recording:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.fh.__exit__(*a)

        def read(self, n=-1):
            asked.append(n)
            return self.fh.read(n)
    small_dir = Path(tempfile.mkdtemp(prefix='lang-census-read-'))
    small = small_dir / 'small.go'
    small.write_text('package main\n\nfunc main() {}\n', encoding='utf-8')
    globals()['open'] = lambda *a, **k: _Recording(real_open(*a, **k))
    try:
        text = _read_head(small, LINE_READ_MAX)
    finally:
        del globals()['open']
        shutil.rmtree(small_dir, ignore_errors=True)
    checks.append(('a small file is read with a buffer the size of the file, not the read cap',
                   text.startswith('package main') and all(0 <= n <= 4096 for n in asked), asked))
    checks.append(('a dependency tree (node_modules/) is listed without being walked to count it',
                   nm.get('source_files_counted') is False and nm.get('source_files') == 0, nm))
    checks.append(('a header-only tree under drivers/ is excluded', 'header-only' in reasons.get('drivers/ios/headers-only', ''),
                   reasons))
    checks.append(('excluded_path finds a file inside an excluded tree',
                   bool(excluded_path(c, 'drivers/ios/AgentKit/lib/m1.ts')) and not excluded_path(c, 'pkg/runner/f1.go'),
                   None))
    cov = language_coverage(c, {'Python', 'JavaScript', 'TypeScript'}, tool='scan_x.py')
    checks.append(('a JS/Python-only tool on the Go repo is flagged unsupported_dominant',
                   cov['status'] == 'unsupported_dominant' and cov['dominant_unsupported_language'] == 'Go'
                   and 'NOT a full analysis' in cov['warning'], cov))
    cov = language_coverage(c, {'Go'}, tool='scan_go.py')
    checks.append(('a Go tool on the Go repo is ok or partial, never unsupported_dominant',
                   cov['status'] in ('ok', 'partial'), cov['status']))
    # a JavaScript workspace whose packages carry their own LICENSE: product code, kept
    ws = {'package.json': '{"workspaces": ["packages/*"]}'}
    for p in ('web', 'api', 'ui'):
        ws.update({f'packages/{p}/package.json': '{}', f'packages/{p}/LICENSE': 'MIT'})
        ws.update({f'packages/{p}/src/f{i}.ts': '' for i in range(5)})
    ws.update({f'scripts/s{i}.py': '' for i in range(4)})
    c2 = census(tree(ws))
    checks.append(('workspace packages with their own LICENSE stay counted', c2['languages'].get('TypeScript') == 15
                   and not c2['excluded'], (c2['languages'], c2['excluded'])))
    # a git submodule is excluded
    sm = {'.gitmodules': '[submodule "ext"]\n\tpath = libs/ext\n\turl = https://example.test/ext.git\n',
          'app.py': '', 'core.py': ''}
    sm.update({f'libs/ext/e{i}.c': '' for i in range(9)})
    c3 = census(tree(sm))
    checks.append(('a git submodule is excluded', c3['dominant'] == 'Python' and 'libs/ext' in
                   {e['path'] for e in c3['excluded']}, (c3['languages'], c3['excluded'])))
    # a server-rendered Python web app (a recipe site): a Flask module, Jinja templates, a stylesheet, one script, and
    # a data directory of 300 JSON records the app loads at run time
    web = {'app.py': 'from flask import Flask, render_template\napp = Flask(__name__)\n',
           'api/__init__.py': '', 'static/site.css': 'body{}', 'static/menu.js': '',
           'templates/base.html': '<html>{% block body %}{% endblock %}</html>'}
    web.update({f'api/m{i}.py': 'def f():\n    return 1\n' for i in range(4)})
    web.update({f'templates/pages/p{i}.html': '{% extends "base.html" %}' for i in range(9)})
    web.update({f'recipes/r{i:03d}.json': '{"name": "soup"}' for i in range(300)})
    root4 = tree(web)
    c4 = census(root4)
    checks.append(('JSON data files never decide the dominant language', c4['dominant'] == 'Python', c4['languages']))
    checks.append(('a data directory of 300 JSON files is listed as a data tree and not walked',
                   [d['path'] for d in c4.get('data_trees', [])] == ['recipes']
                   and c4['data_trees'][0]['files'] == 300, c4.get('data_trees')))
    checks.append(('templates and stylesheets are counted as markup, apart from the languages',
                   c4.get('markup', {}).get('Templates') == 10 and c4.get('markup', {}).get('CSS') == 1
                   and 'HTML' not in c4['languages'], c4.get('markup')))
    sr = c4.get('server_rendered') or {}
    checks.append(('a Flask app with Jinja templates is recognised as server-rendered',
                   sr.get('framework') == 'flask' and sr.get('server_language') == 'Python' and sr.get('templates') == 10
                   and sr.get('template_dirs') == ['templates'], sr))
    browser = {'JavaScript', 'TypeScript'}
    cov = language_coverage(c4, browser, tool='scan_ui.py', reads_templates=True)
    checks.append(('a tool that reads templates is not told the Flask site is unsupported',
                   cov['status'] == 'partial' and not cov['warning'] and cov.get('server_rendered', {}).get('templates') == 10
                   and 'templates' in (cov.get('note') or ''), cov))
    cov = language_coverage(c4, browser, tool='scan_ui.py')
    checks.append(('a tool that does not read templates still gets the loud warning',
                   cov['status'] == 'unsupported_dominant' and cov['warning'], cov['status']))
    cov = language_coverage(c4, browser | {'HTML'}, tool='scan_ui.py')
    checks.append(('"HTML" among the supported languages means the tool reads templates',
                   cov['status'] == 'partial' and not cov['warning'], cov['status']))
    pruned: list = []
    dns = sorted(os.listdir(root4))
    prune_data_dirs(str(root4), dns, pruned, root=str(root4))
    checks.append(('prune_data_dirs drops the data directory from a walk and says why',
                   'recipes' not in dns and 'templates' in dns and pruned and pruned[0]['path'] == 'recipes'
                   and 'data' in pruned[0]['reason'], (dns, pruned)))
    # a Python tool repository whose examples/ holds a planted TypeScript sample project with its own manifest
    fx = {'pyproject.toml': '[project]\nname = "tool"\n', 'tool/scan.py': 'def scan():\n    return 1\n' * 20,
          'examples/shop/package.json': '{"name": "shop"}'}
    fx.update({f'examples/shop/src/m{i}.ts': 'export const a = 1;\n' * 30 for i in range(12)})
    root5 = tree(fx)
    c5 = census(root5)
    try:
        c6 = census(root5, exclude_fixtures=True)
    except TypeError as e:   # no such option yet
        c6 = {'error': str(e), 'languages': {}}
    checks.append(('by default a fixture project is counted as before (no fixtures_excluded key)',
                   c5['languages'].get('TypeScript') == 12 and 'fixtures_excluded' not in c5, c5['languages']))
    checks.append(('exclude_fixtures leaves a nested sample project out, listed with its reason',
                   'TypeScript' not in c6.get('languages', {}) and c6.get('dominant') == 'Python'
                   and [f['dir'] for f in c6.get('fixtures_excluded', [])] == ['examples/shop']
                   and any(e.get('kind') == 'fixture' and e['path'] == 'examples/shop' for e in c6.get('excluded', [])),
                   c6.get('fixtures_excluded', c6.get('error'))))
    checks += _build_output_checks(tree)
    checks += _line_checks(tree)
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--supported', help='comma-separated languages the calling tool analyses, e.g. "Go,Python"')
    ap.add_argument('--tool', default='this tool')
    ap.add_argument('--exclude', action='append', default=[], metavar='DIR', help='also leave out this directory')
    ap.add_argument('--include', action='append', default=[], metavar='DIR',
                    help='count this directory even if a rule would leave it out (e.g. a hand-written dist/)')
    ap.add_argument('--exclude-fixtures', action='store_true',
                    help='leave out nested sample and fixture projects (repo_layout.py); listed under fixtures_excluded')
    ap.add_argument('--fixture-dir', action='append', default=[], metavar='NAME|PATH',
                    help='with --exclude-fixtures: also treat this directory as a fixture parent')
    ap.add_argument('--product-dir', action='append', default=[], metavar='DIR',
                    help='count this tooling-named folder (scripts/, tools/ ...) as product code')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    c = census(args.repo, args.exclude, args.include, args.exclude_fixtures, args.fixture_dir, args.product_dir)
    if args.supported is not None:
        c['language_coverage'] = language_coverage(c, [s.strip() for s in args.supported.split(',') if s.strip()],
                                                   tool=args.tool)
    text = json.dumps(c, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    langs = ', '.join(f'{k} {v}' for k, v in list(c['languages'].items())[:6]) or 'none'
    by = (f' (by {c["dominant_by"]}: {c["dominant_lines"]} product lines)' if c.get('dominant_by') == 'lines'
          else f' (by {c.get("dominant_by")})')
    print(f'dominant: {c["dominant"]}{by}; files: {langs}; {len(c["excluded"])} tree(s) excluded '
          f'({c["excluded_source_files"]} source files); {c["generated_source_files"]} generated file(s) counted apart',
          file=sys.stderr)
    for b in c['build_output']:
        print(f'{b["decision"]}: {b["reason"]}', file=sys.stderr)
    if c['scripts']:
        print('scripts counted apart (no skill parses them): '
              + ', '.join(f'{k} {v} file(s), {c["script_total_lines"].get(k, 0)} line(s)' for k, v in c['scripts'].items()),
              file=sys.stderr)
    for t in c['tooling_dirs']:
        if t['evidence']:
            print(f'{t["path"]}/ is counted as tooling ({t["lines"]} lines) although '
                  f'{", ".join(sorted({e["kind"] for e in t["evidence"]}))} point at product code: '
                  f'--product-dir {t["path"]} counts it as product', file=sys.stderr)
    cov = c.get('language_coverage')
    if cov and cov['warning']:
        print('WARNING: ' + cov['warning'], file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
