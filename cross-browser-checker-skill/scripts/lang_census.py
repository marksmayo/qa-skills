#!/usr/bin/env python3
"""Count a repository's source files per language, leaving out vendored and third-party trees (stdlib only).

    python lang_census.py <repo> [--supported Go,Python] [--tool scan_x.py] [--out census.json]
    python lang_census.py --self-test

Skills that analyse only some languages call this before they report, so that a handful of incidental
files in a supported language is never presented as an analysis of a repository written in another one.

What is left out, before any file is counted (directories are pruned before they are walked):
  * dependency, VCS, cache and build output directories: vendor/, node_modules/, third_party/, Pods/,
    Carthage/, bower_components/, .git, dist/, build/, out/, target/, DerivedData/, __pycache__, ...;
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

Data files (JSON, CSV, YAML, XML, images ...) are never a language. A directory holding at least 50 of them, no
source file and little else is a data tree: it is not walked and is listed under `data_trees` with its file count.
`prune_data_dirs(dirpath, dirnames, skipped)` applies the same rule inside any os.walk loop, so walkers looking for
code never open thousands of data records (`data_dir_reason(files)` is the test on one directory's file names).

Markup is counted apart, under `markup` (HTML, CSS, Templates; an .html file under templates/, views/, layouts/ or
partials/ is a template). `server_rendered` names a Python web app that renders templates (Flask, Django, Jinja2,
Starlette/FastAPI templating, Bottle, Pyramid ...: an import or a render call) with its template count and
directories. language_coverage(..., reads_templates=True), or 'HTML' among the supported languages, tells the guard
the tool reads those templates: a Python-dominant server-rendered site is then 'partial' with a `note` (the Python is
the server, outside the tool's scope), never 'unsupported_dominant'. A tool that does not read templates still gets
the warning.

Library use:
    import lang_census
    c = lang_census.census(repo)                     # {'languages': {'Go': 225, ...}, 'dominant': 'Go', ...}
    cov = lang_census.language_coverage(c, {'Python', 'JavaScript', 'TypeScript'}, tool='scan_x.py')
    if cov['status'] == 'unsupported_dominant':
        print(cov['warning'], file=sys.stderr)       # and put cov in the output as `language_coverage`
    lang_census.excluded_path(c, 'drivers/ios/Agent/x.ts')   # the exclusion reason, or None
"""
from __future__ import annotations
import argparse, json, os, re, sys, tempfile
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

PRUNE_DIRS = {'.git', '.hg', '.svn', 'node_modules', 'vendor', 'vendored', 'third_party', 'third-party', 'thirdparty',
              'bower_components', 'jspm_packages', 'Pods', 'Carthage', '.venv', 'venv', '.tox', '.nox',
              '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'site-packages', 'dist',
              'target', 'DerivedData', '.gradle', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache',
              'coverage', 'htmlcov', '.idea', '.vscode', 'Headers', 'PrivateHeaders'}
# build output names that are also plausible Go package names: left out unless they hold .go files themselves
BUILD_OUTPUT = {'build', 'out', 'obj'}
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
DATA_DIR_MIN = 50
DATA_SHARE = 0.9
# Markup is counted apart from the languages: browser-facing, but never the dominant language.
MARKUP_BY_EXT = {'.html': 'HTML', '.htm': 'HTML', '.xhtml': 'HTML', '.css': 'CSS', '.scss': 'CSS', '.sass': 'CSS',
                 '.less': 'CSS', '.styl': 'CSS',
                 '.jinja': 'Templates', '.jinja2': 'Templates', '.j2': 'Templates', '.njk': 'Templates',
                 '.hbs': 'Templates', '.handlebars': 'Templates', '.mustache': 'Templates', '.erb': 'Templates',
                 '.ejs': 'Templates', '.twig': 'Templates', '.liquid': 'Templates', '.tmpl': 'Templates',
                 '.gohtml': 'Templates', '.pug': 'Templates', '.jade': 'Templates', '.haml': 'Templates',
                 '.slim': 'Templates', '.cshtml': 'Templates', '.razor': 'Templates', '.blade.php': 'Templates'}
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


def exclusion_reason(rel_dir: str, name: str, files: list[str], subs: set[str]) -> str | None:
    """Why the directory rel_dir (its last part is name; files are its file names) is not product code, or None."""
    if name in PRUNE_DIRS:
        return f'{name}/ is a dependency, VCS, cache, build-output or header directory'
    if name in BUILD_OUTPUT and not any(f.endswith('.go') for f in files):
        return f'{name}/ is a build-output directory'
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


def census(root: Path | str, extra_exclude: list[str] | None = None) -> dict:
    """Source files per language under root, with vendored and third-party trees left out (see the module doc).
    extra_exclude: repo-relative directories the caller also knows are not product code."""
    root = Path(root).resolve()
    subs = submodules(root)
    extra = {e.strip('/') for e in (extra_exclude or [])}
    langs: Counter = Counter()
    markup: Counter = Counter()
    by_lang_dirs: dict[str, Counter] = {}
    excluded: list[dict] = []
    data_trees: list[dict] = []
    py_files: list[str] = []
    template_files: list[str] = []
    for dp, dns, fns in os.walk(root):
        rel = Path(dp).relative_to(root).as_posix()
        rel = '' if rel == '.' else rel
        keep = []
        for d in sorted(dns):
            sub = f'{rel}/{d}' if rel else d
            try:
                files = [f for f in os.listdir(os.path.join(dp, d))]
            except OSError:
                files = []
            why = 'excluded by the caller' if sub in extra else exclusion_reason(sub, d, files, subs)
            if why:
                if d not in ('.git', '.hg', '.svn', '__pycache__'):   # noise nobody needs listed
                    excluded.append({'path': sub, 'reason': why, 'source_files': count_sources(Path(dp) / d)})
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
                langs[lang] += 1
                top = rel.split('/')[0] if rel else '.'
                by_lang_dirs.setdefault(lang, Counter())[top] += 1
                if lang == 'Python':
                    py_files.append(frel)
                continue
            mk = markup_kind(frel)
            if mk:
                markup[mk] += 1
                if mk == 'Templates':
                    template_files.append(frel)
    ordered = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    dominant = next(iter(ordered), None)
    # app modules first (shallow paths, app/main/server names), so the framework import is found early
    py_files.sort(key=lambda r: (r.count('/'), not re.search(r'(^|/)(app|main|server|wsgi|asgi|views|urls|routes)\.py$', r), r))
    return {'root': root.name, 'languages': ordered, 'total_source_files': sum(langs.values()),
            'dominant': dominant, 'dominant_files': ordered.get(dominant, 0) if dominant else 0,
            'top_dirs': {k: dict(v.most_common(5)) for k, v in by_lang_dirs.items()},
            'excluded': excluded,
            'excluded_source_files': sum(e['source_files'] for e in excluded),
            'markup': dict(sorted(markup.items())),
            'data_trees': data_trees,
            'server_rendered': server_rendered(root, py_files, template_files)}


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

    status 'unsupported_dominant' (with a loud `warning`) when the largest unsupported language has more files than
    all supported languages together (and at least min_files); 'partial' when some unsupported languages are present
    but do not dominate; 'ok' when everything counted is supported; 'no_source' when nothing was counted.

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
    if not langs:
        out['status'] = 'no_source'
        return out
    if uns:
        lang, n = max(uns.items(), key=lambda kv: (kv[1], kv[0]))
        sr = c.get('server_rendered') or {}
        if n > n_sup and n >= min_files and reads_templates and sr.get('server_language') == lang:
            out['status'] = 'partial'
            out['server_rendered'] = sr
            out['note'] = (f'This repository is a server-rendered {sr["framework"]} web app: its {sr["templates"]} '
                           f'template file(s) ({", ".join(sr["template_dirs"][:3])}) are the browser-facing code, which '
                           f'{tool} reads. Its {n} {lang} file(s) are the server that renders them, not browser code; '
                           f'record that server code as outside this {what}, not as the UI left unread.')
        elif n > n_sup and n >= min_files:
            out['status'] = 'unsupported_dominant'
            out['dominant_unsupported_language'], out['dominant_unsupported_files'] = lang, n
            out['warning'] = (f'This repository is predominantly {lang} ({n} source files, vendored and third-party '
                              f'trees excluded), which {tool} does not analyse: it covers '
                              f'{", ".join(sorted(supported)) or "no language"} only. {n_sup} supported source '
                              f'file(s) were found, so its output covers that minority and is NOT a {what} of this '
                              f'repository\'s {lang} code. Do not present it as one; record {lang} as not assessed.')
        else:
            out['status'] = 'partial'
    return out


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
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    c = census(args.repo, args.exclude)
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
    print(f'dominant: {c["dominant"]}; {langs}; {len(c["excluded"])} tree(s) excluded '
          f'({c["excluded_source_files"]} source files)', file=sys.stderr)
    cov = c.get('language_coverage')
    if cov and cov['warning']:
        print('WARNING: ' + cov['warning'], file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
