#!/usr/bin/env python3
"""Scan a repository's CSS, JavaScript and HTML for features the browserslist targets don't all support (stdlib).

    python feature_scan.py --repo examples/travel-site --out run/features.json [--query "defaults"] [--built-css .next/static/css]

Patterns are in data/feature-patterns.json; support comes from web-features (the first version of each
core browser that supports the feature), and targets from browserslist_lite.py (the repo's
package.json or .browserslistrc, else Next.js's defaults for a Next.js project, else a Vite project's
build.target or Vite's default, else `defaults`). CSS is read with a small tokeniser (comments removed,
rules split into selectors and declarations, declarations before a nested rule kept with their parent,
@supports blocks tracked); scripts and markup are matched with the patterns' regexes, ignoring comments
in scripts. The walk prunes dependency and cache folders (node_modules, .next, coverage, ...) and the
trees the shared census excludes (vendored code, and build output such as dist/ unless the census finds
it is source) before descending, listing each in skipped.pruned with its reason; --include DIR
(repeatable) scans one anyway (a committed front end a server mounts with StaticFiles, express.static or
send_from_directory). Excluded folders holding code are listed in skipped.excluded_with_code with the
file that serves them, and the language warning names them with the --include to use. It leaves out
what doesn't ship to browsers: test and story files (*.test.*, *.spec.*, *.e2e.*, *.cy.*, *.stories.*),
test directories (e2e, test, tests, spec, specs, unless the directory holds a package manifest, which
makes it a product package of that name), and Node-only scripts (a node shebang, or an import of a node:
module or of fs, child_process and other Node built-ins: CLIs, build scripts, dev servers; not a script
written for both, with an environment check and browser globals, such as Emscripten glue). `skipped`
(always written) lists what was left out. Patterns found that every target supports are listed in
supported_features, so a reader can tell "checked" from "not in the pattern set".

Data directories are pruned too: a directory holding at least 200 files of which at least 90% are data files
(.json, .geojson, .csv, .tsv, .xml, .ndjson, .jsonl, .txt, .yaml, .yml, .parquet, .avro) and none are code or
markup is a data tree, not browser code (a site's data directory of thousands of JSON records); it is listed in
skipped.data_dirs with its file count and never walked. --exclude DIR (repeatable, repository-relative) leaves
out any other directory (skipped.excluded). `scanned_files` lists every file read (count, and up to 300 paths),
so a stale page or a retired build script that was scanned can be seen.

Server-rendered sites: when the repository is mostly Python, Ruby or PHP and the scanned markup includes
templates (HTML under a templates/, views/, layouts/ or partials/ directory, or with {% %}, {{ }}, <% %> or
@extends syntax: Flask and Django with Jinja or Django templates named .html, Rails, Laravel), the
server-side language is the code that renders those templates, not browser code. language_coverage then
has status "server_rendered", names the templates scanned (and markup outside the template and static
folders, which may not be served), and no language limitation or warning is written: the templates and
stylesheets are the browser code, and they were scanned.

CSS-in-JS: when package.json depends on Emotion, MUI, styled-components (CSS_IN_JS_DEPS), scripts that use
it are also read for CSS written literally: object styles (camelCase property with a string value, or a
selector / at-rule key) and tagged template literals (styled.div`...`, css`...`). Those uses carry
via "css-in-js", `css_in_js` records the libraries, and a limitation says runtime-computed styles were
not scanned. Minified built CSS (lines over 1000 characters) gives each use a column and collapses
repeats of one feature on one line (occurrences). With Tailwind 4, its `@layer properties` fallback in
built CSS counts as the fallback for @property, and targets below Tailwind 4's own browser floor are
listed (utility_css.targets_below_floor) with a limitation.

Utility-class CSS: when package.json depends on Tailwind CSS, class strings in scripts and markup are
matched against the `utility-class` patterns (known non-baseline utilities such as text-balance,
backdrop-blur-*, bg-clip-text, has-[...], @container), and `utility_css` records that only those were
scanned. The CSS such a framework generates is scanned only when built CSS is given (--built-css, or
.next/static/css when it exists, in the repo or beside any next.config.* up to three levels down, for a
Next.js project kept in a subfolder); otherwise `limitations` says it wasn't, for the report to carry.

A target is unsupported when web-features gives no version for its browser, or a later one. Browsers
web-features doesn't cover (Opera, Samsung Internet, UC, QQ, KaiOS, Opera Mini, Android WebView) are
`not_assessed` for that feature. A fallback is an @supports block around the use, or one of the
pattern's fallback regexes in the same file. Every finding's status is `possible`: it becomes
`confirmed_at_runtime` only when compare_engines.py links it to a difference an engine produced.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import browserslist_lite as bl  # noqa: E402
import lang_census  # noqa: E402  (shared copy; edit shared/scripts/lang_census.py)
import next_app  # noqa: E402  (shared copy; edit shared/scripts/next_app.py)

WF_BROWSER = {'chrome': 'chrome', 'and_chr': 'chrome_android', 'edge': 'edge', 'firefox': 'firefox', 'and_ff': 'firefox_android',
              'safari': 'safari', 'ios_saf': 'safari_ios'}
# Dependency, cache and tool-output folders that never hold hand-written browser code. Build output (dist/, build/, out/)
# is NOT listed here: whether such a folder is generated or a committed front end served as written is the shared
# census's call (lang_census), and --include overrides it. Every prune is recorded in skipped.pruned.
SKIP_DIRS = {'node_modules', '.git', 'vendor', '__pycache__', '.next', '.nuxt', 'coverage', '.venv', 'venv',
             '.turbo', '.cache', '.svelte-kit', '.vercel', 'playwright-report', 'test-results', 'storybook-static',
             '__tests__', '__mocks__'}
TEST_FILE = re.compile(r'\.(test|spec|e2e|cy|stories)\.[cm]?[jt]sx?$')   # tests and stories don't ship to browsers
TEST_DIR_NAMES = {'e2e', 'test', 'tests', 'spec', 'specs'}
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle',
             'build.gradle.kts', 'composer.json', 'Gemfile', 'deno.json')
INLINE_BLOCK = re.compile(r'<(script|style)\b[^>]*>', re.I)
SCRIPT_SUFFIXES = ('.js', '.mjs', '.cjs', '.ts', '.mts', '.cts')
# A script written for both Node and the browser (Emscripten glue, a UMD bundle): its require("fs") sits behind an
# environment check, and it touches browser globals. It runs in the browser, so it is scanned.
DUAL_ENV = re.compile(r'''\bENVIRONMENT_IS_(?:NODE|WEB|WORKER)\b|typeof\s+(?:window|document|process|importScripts)\s*[!=]=''')
BROWSER_USE = re.compile(r'\b(?:document|window|navigator|location)\s*\.|\bWebAssembly\s*\.')
NODE_ONLY = re.compile(r'''\A#!.*\bnode\b|(?:\bfrom\s*|\brequire\(\s*|\bimport\(\s*|^\s*import\s+)['"](?:node:[\w/]+|'''
                       r'''(?:fs|child_process|worker_threads|readline|net|cluster|dgram|tls|v8|vm|inspector)(?:/promises)?)['"]''',
                       re.M)
SUFFIXES = ('.css', '.scss', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte')
MARKUP = ('.html', '.htm', '.vue', '.svelte')
CLASS_SOURCES = ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx') + MARKUP
# server-side code that may hold a whole HTML page in a string constant (a Go raw string for html/template, a Python
# or Java/Kotlin triple-quoted string, a Rust raw string), and template files: their <style>/<script> blocks ship
HOST_SUFFIXES = ('.go', '.py', '.rb', '.rs', '.java', '.kt', '.cs', '.php', '.scala', '.ex', '.exs', '.swift')
TEMPLATE_FILE = re.compile(r'\.(tmpl|gohtml|gotmpl|tpl|jinja2?|j2|erb|hbs|handlebars|mustache|njk|liquid|twig|ejs)$', re.I)
HTML_STRING = re.compile(r'(`[^`]*`|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|r#+"[\s\S]*?"#+)')
HTML_DOC = re.compile(r'<(!doctype\s+html|html\b|body\b|style\b|script\b)', re.I)
# the languages whose browser code this scan reads (CSS and HTML files are not source languages in the census)
SCANNED_LANGUAGES = {'JavaScript', 'TypeScript', 'Vue', 'Svelte'}
# a data tree: at least DATA_DIR_MIN files, at least DATA_DIR_SHARE of them data files, none code or markup
DATA_EXT = {'.json', '.geojson', '.csv', '.tsv', '.xml', '.ndjson', '.jsonl', '.txt', '.yaml', '.yml', '.parquet', '.avro'}
DATA_DIR_MIN, DATA_DIR_SHARE = 200, 0.9
# server-side languages that render templates (a Flask or Django app, Rails, Laravel)
SERVER_LANGUAGES = {'Python', 'Ruby', 'PHP'}
TEMPLATE_DIRS = {'templates', 'views', 'layouts', 'partials', 'includes'}
TEMPLATE_SYNTAX = re.compile(r'\{%-?\s*\w+|\{\{[^}]*\}\}|<%[=-]?\s|@(?:extends|section|yield)\b')
STATIC_DIRS = {'static', 'public', 'assets', 'www', 'wwwroot'}
# tool configuration read by Node at build time (next.config.ts, eslint.config.mjs, postcss.config.mjs, vite.config.ts,
# tailwind.config.js, .eslintrc.cjs, ...) and type declarations: never shipped to a browser
CONFIG_FILE = re.compile(r'^(?:[\w-]+\.)*config\.[cm]?[jt]s$|^\.[\w-]+rc\.[cm]?[jt]s$|\.d\.[cm]?ts$')
# a module that declares itself server-only (import 'server-only') or a server-actions file ('use server' first)
SERVER_ONLY_IMPORT = re.compile(r'''^\s*import\s+['"]server-only['"]''', re.M)
USE_SERVER_FILE = re.compile(r'''\A(?:\s|//[^\n]*\n|/\*.*?\*/)*['"]use server['"]''', re.S)


def embedded_html(text: str) -> bool:
    """Whether server-side source holds an HTML document (or a <style>/<script> block) in a string constant."""
    return any(HTML_DOC.search(m.group(0)) for m in HTML_STRING.finditer(text))


# browsers a CSS framework major itself requires (its documented floor), web-features browser keys of caniuse names
FRAMEWORK_FLOORS = {('tailwind', 4): {'chrome': '111', 'edge': '111', 'firefox': '128', 'safari': '16.4', 'ios_saf': '16.4'}}


def css_rules(text: str, columns: bool = False):
    """Yield (selector, declarations, line, inside_supports_condition, ancestors) for every rule, with nesting depth;
    with columns, a sixth item: the rule's column on its line (minified CSS puts every rule on line 1)."""
    # comments go; with columns they are blanked instead, so offsets stay true
    text = re.sub(r'/\*.*?\*/', (lambda m: re.sub(r'[^\n]', ' ', m.group(0))) if columns else
                  (lambda m: '\n' * m.group(0).count('\n')), text, flags=re.S)
    stack: list[tuple[str, int, int]] = []
    buf, start_line, start_col, i, line, line_start = '', 1, 1, 0, 1, 0
    while i < len(text):
        ch = text[i]
        if ch == '\n':
            line += 1
            line_start = i + 1
        if ch == '{':
            if ';' in buf and stack:
                # nested CSS (SCSS, CSS nesting, CSS-in-JS): declarations before a nested rule belong to the parent
                decl, buf = buf.rsplit(';', 1)
                psel, pln, pcol = stack[-1]
                row = (psel, '' if psel.startswith('@') else decl.strip(), pln,
                       ' '.join(s for s, _, _ in stack[:-1] if s.startswith('@supports')), [s for s, _, _ in stack[:-1]])
                yield row + (pcol,) if columns else row
            stack.append((buf.strip(), start_line, start_col))
            buf, start_line, start_col = '', line, i - line_start + 2
        elif ch == '}':
            sel, ln, col = stack.pop() if stack else ('', line, 1)
            supports = ' '.join(s for s, _, _ in stack if s.startswith('@supports'))
            row = (sel, '' if sel.startswith('@') else buf.strip(), ln, supports, [s for s, _, _ in stack])
            yield row + (col,) if columns else row
            buf, start_line, start_col = '', line, i - line_start + 2
        else:
            if not buf.strip():
                start_line, start_col = line, i - line_start + 1
            buf += ch
        i += 1


def decls(block: str) -> list[tuple[str, str]]:
    out = []
    for d in block.split(';'):
        if ':' in d:
            k, v = d.split(':', 1)
            out.append((k.strip().lower(), v.strip().lower()))
    return out


def is_test_dir(d: Path) -> bool:
    """A directory named e2e, test, tests, spec or specs is test code only when it holds no package manifest: with one
    (package.json, pyproject.toml, Cargo.toml, ...) it is a product package that happens to have that name (a pnpm
    workspace package called e2e), and is scanned like any other. The same rule as the shared diff_scope.py."""
    return d.name.lower() in TEST_DIR_NAMES and not any((d / m).is_file() for m in MANIFESTS)


def next_server_files(repo: Path) -> dict[str, str]:
    """Files of a Next.js project that never ship to a browser, from the shared static reader (next_app.py): App Router
    route handlers (route.ts), generated metadata routes (robots.ts, sitemap.ts, manifest.ts, opengraph-image.tsx,
    icon.tsx: rendered on the server or at build time), Pages Router API routes, middleware and the instrumentation
    hook. {repo-relative path: reason}. Pages, layouts and components are kept: their markup and class names reach the
    browser even when they render on the server."""
    out: dict[str, str] = {}
    try:
        model = next_app.read_next_app(repo, max_files=20000)
    except Exception:  # noqa: BLE001 - the reader is best effort here; without it nothing is left out
        return out
    for proj in model.get('projects') or []:
        for r in proj.get('routes') or []:
            if r.get('kind') == 'route_handler':
                out[r['file']] = 'Next.js route handler: runs on the server'
            elif r.get('kind') == 'metadata' and r.get('form') == 'generated':
                out[r['file']] = f'Next.js generated metadata route ({r.get("metadata")}): rendered on the server or at build time'
            elif r.get('kind') == 'pages_api':
                out[r['file']] = 'Next.js API route: runs on the server'
        mw = proj.get('middleware') or {}
        if mw.get('file'):
            out[mw['file']] = 'Next.js middleware: runs on the server (edge)'
        base = f'{proj["root"]}/' if proj.get('root') else ''
        for name in ('instrumentation.ts', 'instrumentation.js', 'src/instrumentation.ts', 'src/instrumentation.js'):
            if (repo / (base + name)).is_file():
                out[base + name] = 'Next.js instrumentation hook: runs on the server'
    return out


def server_only_reason(name: str, rel: str, text: str | None, next_server: dict[str, str]) -> str | None:
    """Why a script never ships to a browser (a server-only file of a framework, tool configuration, a type
    declaration, a module that imports 'server-only' or starts with 'use server'), or None."""
    if rel in next_server:
        return next_server[rel]
    if CONFIG_FILE.search(name):
        return 'type declarations' if re.search(r'\.d\.[cm]?ts$', name) else 'tool configuration read at build time'
    if text is not None and SERVER_ONLY_IMPORT.search(text[:20000]):
        return "imports 'server-only'"
    if text is not None and USE_SERVER_FILE.match(text):
        return "a 'use server' module (server actions)"
    return None


def node_only(text: str) -> bool:
    """A script that runs under Node, never in a browser: a node shebang, or an import of a node: module or of a Node
    built-in that has no browser counterpart (fs, child_process, worker_threads, ...). A script written for both (an
    environment check such as ENVIRONMENT_IS_NODE or typeof window, and use of document, window, navigator or
    WebAssembly: Emscripten glue, a UMD bundle) is browser code, whatever it requires under Node."""
    head = text[:20000]
    if re.match(r'#!.*\bnode\b', head):
        return True
    if not NODE_ONLY.search(head):
        return False
    return not (DUAL_ENV.search(text) and BROWSER_USE.search(text))


def data_dir_files(d: Path) -> int | None:
    """The number of files in d when it is a data tree (DATA_DIR_MIN files or more, DATA_DIR_SHARE of them data files,
    none code or markup), else None. Only d's own listing is read, never a file."""
    try:
        names = [n for n in os.listdir(d) if not os.path.isdir(os.path.join(d, n))]
    except OSError:
        return None
    if len(names) < DATA_DIR_MIN:
        return None
    exts = [os.path.splitext(n)[1].lower() for n in names]
    if any(e in SUFFIXES or e in HOST_SUFFIXES for e in exts):
        return None
    data = sum(1 for e in exts if e in DATA_EXT)
    return len(names) if data >= DATA_DIR_SHARE * len(names) else None


def record_prune(skipped: dict | None, rel: str, reason: str, cap: int = 200) -> None:
    """One pruned directory in skipped.pruned (path and reason); past `cap`, only counted (pruned_more)."""
    if skipped is None:
        return
    lst = skipped.setdefault('pruned', [])
    if len(lst) < cap:
        lst.append({'path': rel, 'reason': reason})
    else:
        skipped['pruned_more'] = skipped.get('pruned_more', 0) + 1


def files(repo: Path, skipped: dict | None = None, excluded: list | None = None, user_excluded: list[str] | None = None,
          included: list[str] | None = None, server_only: dict[str, str] | None = None):
    """Source files, walking with excluded directories pruned before descending (a node_modules tree is never listed).
    Test directories (is_test_dir), test and story files, and Node-only scripts (CLIs, build scripts, servers) don't ship
    to browsers and are left out; with `skipped`, the test directories and Node-only files are listed there. Data trees
    (data_dir_files) and the directories given with --exclude (user_excluded) are pruned and listed too, and so is
    every dependency or cache folder (SKIP_DIRS) and every tree the shared census excludes (`excluded`: paths, or
    census entries with their reason), under skipped.pruned. A directory given with --include (`included`) is walked
    whatever those rules say. Scripts that never ship to a browser (server_only_reason: a framework's server files from
    `server_only`, tool configuration, type declarations, 'server-only' modules) are listed in skipped.server_only."""
    user = {e.replace('\\', '/').strip('/') for e in user_excluded or []}
    inc = {e.replace('\\', '/').strip('/') for e in included or []}
    census_ex = {'excluded': [{'path': e['path'] if isinstance(e, dict) else e,
                               'reason': (e.get('reason') if isinstance(e, dict) else '') or 'excluded by the shared census'}
                              for e in excluded or []]}
    for root, dirs, names in os.walk(repo):
        keep = []
        for d in sorted(dirs):
            rel = rel_name(Path(root) / d, repo)
            if rel in inc or any(i.startswith(rel + '/') for i in inc):
                keep.append(d)   # --include: walked whatever the prune rules say (and the folders on the way to it)
                continue
            under_inc = any(rel.startswith(i + '/') for i in inc)   # inside it, only the census exclusion is lifted
            if d in SKIP_DIRS:
                if d not in ('.git', '__pycache__'):
                    record_prune(skipped, rel, f'{d}/ is a dependency, cache or tool folder')
                continue
            if rel in user:
                if skipped is not None:
                    skipped.setdefault('excluded', []).append(rel)
                continue
            why = lang_census.excluded_path(census_ex, rel) if excluded and not under_inc else None
            if why is not None:
                # vendored, third-party or build output by the shared census: recorded, and --include overrides it
                record_prune(skipped, rel, f'{why} (shared census); pass --include {rel} if it is served as written')
                continue
            n_data = data_dir_files(Path(root) / d)
            if n_data is not None:
                if skipped is not None:
                    skipped.setdefault('data_dirs', []).append({'path': rel, 'files': n_data})
                continue
            if is_test_dir(Path(root) / d):
                if skipped is not None:
                    skipped.setdefault('test_dirs', []).append(rel_name(Path(root) / d, repo))
                continue
            keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            p = Path(root) / n
            if p.suffix in HOST_SUFFIXES or TEMPLATE_FILE.search(n):
                # a page held in a Go/Python/... string constant or a template file: its inline blocks ship
                if re.search(r'_test\.go$|^test_.*\.py$|_test\.py$', n):
                    continue
                try:
                    text = p.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                if (TEMPLATE_FILE.search(n) and HTML_DOC.search(text)) or (p.suffix in HOST_SUFFIXES and embedded_html(text)):
                    if skipped is not None:
                        skipped.setdefault('embedded_html', []).append(rel_name(p, repo))
                    yield p, True
                continue
            if p.suffix not in SUFFIXES or TEST_FILE.search(n):
                continue
            if p.suffix in SCRIPT_SUFFIXES + ('.tsx', '.jsx'):
                try:
                    text = p.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    text = ''
                why = server_only_reason(n, rel_name(p, repo), text, server_only or {})
                if why:
                    if skipped is not None:
                        skipped.setdefault('server_only', []).append({'path': rel_name(p, repo), 'reason': why})
                    continue
                if p.suffix in SCRIPT_SUFFIXES and node_only(text):
                    # its own code runs in Node; pages it renders from template literals ship their inline blocks
                    html = bool(INLINE_BLOCK.search(text))
                    if skipped is not None:
                        skipped.setdefault('node_only_files', []).append(rel_name(p, repo) + (' (inline <script>/<style> blocks scanned)' if html else ''))
                    if html:
                        yield p, True
                    continue
            yield p, None


def next_built_css(repo: Path, depth: int = 3) -> list[Path]:
    """The built CSS of every Next.js project in the repo: <dir>/.next/static/css for the repo itself and for each folder
    (up to `depth` levels down, the walk pruned) holding a next.config.* file, as in a project kept under app/ and built
    with `next build app`, or one app of a workspace."""
    out = []
    for root, dirs, names in os.walk(repo):
        here = Path(root)
        level = len(here.relative_to(repo).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS) if level < depth else []
        if here == repo or any(n.startswith('next.config.') for n in names):
            css = here / '.next' / 'static' / 'css'
            if css.is_dir():
                out.append(css)
    return out


def built_css_files(paths: list[Path]):
    for b in paths:
        if b.is_file():
            yield b
        elif b.is_dir():
            for root, dirs, names in os.walk(b):
                dirs[:] = sorted(d for d in dirs if d != 'node_modules')
                yield from (Path(root) / n for n in sorted(names) if n.endswith('.css'))


def utility_framework(repo: Path) -> dict | None:
    """Tailwind CSS, when package.json depends on it: its version, from the installed package or the range."""
    pkg = repo / 'package.json'
    if not pkg.is_file():
        return None
    try:
        d = json.loads(pkg.read_text(encoding='utf-8'))
    except ValueError:
        return None
    deps = {**(d.get('devDependencies') or {}), **(d.get('dependencies') or {})}
    name = next((k for k in ('tailwindcss', '@tailwindcss/postcss', '@tailwindcss/vite') if k in deps), None)
    if not name:
        return None
    version = deps[name]
    inst = repo / 'node_modules' / 'tailwindcss' / 'package.json'
    if inst.is_file():
        try:
            version = json.loads(inst.read_text(encoding='utf-8')).get('version') or version
        except ValueError:
            pass
    return {'framework': 'tailwind', 'version': version}


def strip_js_comments(t: str) -> str:
    return re.sub(r'//[^\n]*|/\*.*?\*/', lambda m: ' ' * len(m.group(0).split('\n')[0]) + '\n' * m.group(0).count('\n'), t, flags=re.S)


def rel_name(f: Path, repo: Path) -> str:
    try:
        return f.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return f.as_posix()


def scan(repo: Path, patterns: list[dict], utility: bool = False, built: list[Path] | None = None,
         css_in_js: bool = False, tailwind_major: int | None = None, skipped: dict | None = None,
         excluded: list | None = None, user_excluded: list[str] | None = None,
         scanned: list[str] | None = None, included: list[str] | None = None,
         server_only: dict[str, str] | None = None) -> list[dict]:
    """Uses of every pattern in the repo's sources; utility-class patterns only when `utility` (Tailwind found); CSS
    written in scripts when `css_in_js` (a CSS-in-JS library found); the built CSS files with the CSS patterns. With
    `scanned`, every file read is appended to it."""
    uses = []
    for f, markup in files(repo, skipped, excluded, user_excluded, included, server_only):
        if scanned is not None:
            scanned.append(rel_name(f, repo))
        uses += scan_file(f, rel_name(f, repo), patterns, utility, css_in_js, markup=markup)
    for f in built_css_files(built or []):
        uses += scan_file(f, rel_name(f, repo), patterns, False, tailwind_major=tailwind_major)
    return uses


CSS_IN_JS_DEPS = ('@emotion/react', '@emotion/styled', '@emotion/css', 'styled-components', '@mui/material', '@mui/system',
                  '@mui/joy', '@stitches/react', 'goober', '@linaria/core')
CSS_IN_JS_FILE = re.compile(r'''from\s+['"](@emotion/|@mui/|styled-components|@stitches/|goober|@linaria/)|\bsx\s*[=:]|'''
                            r'''\bstyled\s*[.(]|\bcss\s*`|\bmakeStyles\s*\(''')
# a tagged template literal holding CSS: styled.div`...`, styled(Box)`...`, css`...`, keyframes`...`, createGlobalStyle`...`
CSS_TAG = re.compile(r'\b(?:styled(?:\.\w+|\([^()`]*\))(?:\.attrs\([^`]*?\))?|css|keyframes|createGlobalStyle|injectGlobal|globalCss)\s*`')
# an object-style entry: backdropFilter: 'blur(4px)', '&:has(> img)': {...}, '@container (min-width: 30em)': {...}
OBJ_ENTRY = re.compile(r'''(?<![\w$.])(?:(['"])([^'"\n]{1,120})\1|([A-Za-z_$][\w$]*))\s*:\s*(?:(['"`])([^'"`\n]{0,200})\4|(\{))''')


def css_in_js_libraries(repo: Path) -> list[str]:
    pkg = repo / 'package.json'
    try:
        d = json.loads(pkg.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    deps = {**(d.get('devDependencies') or {}), **(d.get('dependencies') or {})}
    return [k for k in CSS_IN_JS_DEPS if k in deps]


def template_css(js: str) -> list[tuple[str, int]]:
    """CSS from tagged template literals, ${...} replaced by a placeholder; (css, line offset). Bare declarations
    (styled.div`color: red`) are wrapped in a rule so the tokeniser sees them."""
    out = []
    for m in CSS_TAG.finditer(js):
        i, depth, buf = m.end(), 0, []
        while i < len(js):
            c = js[i]
            if c == '\\':
                buf.append(js[i:i + 2])
                i += 2
                continue
            if depth == 0 and c == '`':
                break
            if js.startswith('${', i):
                depth += 1
                i += 2
                continue
            if depth and c == '}':
                depth -= 1
                if depth == 0:
                    buf.append('0')
                i += 1
                continue
            if depth == 0:
                buf.append(c)
            elif c == '\n':
                buf.append('\n')
            i += 1
        css = ''.join(buf)
        offset = js[:m.end()].count('\n')
        out.append((css if re.search(r'[{}]', css) and not re.match(r'\s*[\w-]+\s*:', css) else '& {' + css + '}', offset))
    return out


def kebab(key: str) -> str:
    """backdropFilter -> backdrop-filter, WebkitBackdropFilter -> -webkit-backdrop-filter."""
    k = re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), key)
    return k if not k.startswith(('-webkit', '-moz', '-ms')) or key[0].isupper() else k.lstrip('-')


def value_hit(p: dict, val: str) -> bool:
    """A css-declaration pattern's value test: `value` is a substring, `value_regex` a regular expression (searched)."""
    if p.get('value') and p['value'] not in val:
        return False
    return not p.get('value_regex') or bool(re.search(p['value_regex'], val))


def object_style_uses(js: str, rel: str, patterns: list[dict], fb_of) -> list[dict]:
    """CSS features in object styles (sx props, styled({...}), theme styleOverrides): a camelCase property with a
    string value, or a selector / at-rule key."""
    uses = []
    lines = js.splitlines()
    for m in OBJ_ENTRY.finditer(js):
        key = m.group(2) or m.group(3)
        val = m.group(5)
        nested = bool(m.group(6))
        ln = js[:m.start()].count('\n') + 1
        snippet = lines[ln - 1].strip()[:160] if ln - 1 < len(lines) else ''
        for p in patterns:
            k, hit = p['kind'], False
            if nested and (key.startswith(('&', ':', '@')) or ':' in key):
                if k == 'css-selector' and p['match'] in key:
                    hit = True
                elif k == 'css-pseudo-regex' and re.search(p['match'], key):
                    hit = True
                elif k == 'css-at-rule' and key.startswith(p['match']):
                    hit = True
            elif val is not None and k == 'css-declaration' and re.fullmatch(r'[A-Za-z]+', key):
                prop = kebab(key)
                hit = bool(re.fullmatch(p['property'], prop)) and value_hit(p, val.lower())
            if hit:
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': snippet,
                             'selector': key if nested else None, 'fallback': fb_of(p), 'via': 'css-in-js'})
    return uses


def scan_file(f: Path, rel: str, patterns: list[dict], utility: bool, css_in_js: bool = False,
              tailwind_major: int | None = None, markup: bool | None = None) -> list[dict]:
    """Uses in one file. `markup` reads a script as markup: only its inline <script> and <style> blocks and the html
    patterns (a Node server that renders pages from template literals ships those blocks, not its own code)."""
    uses = []
    text = f.read_text(encoding='utf-8', errors='replace')
    markup = f.suffix in MARKUP if markup is None else markup
    css_parts, js_parts = [], []
    if f.suffix in ('.css', '.scss'):
        css_parts.append((text, 0))
    elif markup:
        for m in re.finditer(r'<style[^>]*>(.*?)</style>', text, re.S | re.I):
            css_parts.append((m.group(1), text[:m.start(1)].count('\n')))
        for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', text, re.S | re.I):
            js_parts.append((m.group(1), text[:m.start(1)].count('\n')))
    else:
        js_parts.append((text, 0))
    fb_of = lambda p: bool(p.get('emits_fallback')) or any(re.search(x, text) for x in p.get('fallback') or [])
    if css_in_js and f.suffix in CLASS_SOURCES and not markup and CSS_IN_JS_FILE.search(text):
        clean = strip_js_comments(text)
        css_parts += template_css(clean)
        uses += object_style_uses(clean, rel, [p for p in patterns if p['kind'].startswith('css')], fb_of)
    # minified CSS (built output): every rule is on line 1, so uses carry a column and repeats of one feature collapse
    minified = f.suffix == '.css' and any(len(l) > 1000 for l in text.splitlines())
    # Tailwind 4 emits @layer properties { @supports (...) { *, ::before, ::after { --tw-...: initial } } } as the
    # fallback for engines without @property: its registered properties then fall back to plain custom properties
    tw4_props = bool(tailwind_major and tailwind_major >= 4 and re.search(r'@layer\s+properties\s*\{\s*@supports', text)
                     and '--tw-' in text)
    for p in patterns:
        # in minified built CSS every rule shares one file (and one line): a guard elsewhere in it says nothing about this
        # use, so only the @supports block around the use (or a fallback the framework always emits) counts
        fb = (bool(p.get('emits_fallback')) if minified and p['kind'].startswith('css') else fb_of(p)) or \
            (tw4_props and p['feature'] == 'registered-custom-properties')
        k = p['kind']
        if k.startswith('css'):
            for css, offset in css_parts:
                for sel, block, ln, supports, ancestors, col in css_rules(css, columns=True):
                    hit = None
                    if k == 'css-selector' and p['match'] in sel:
                        hit = sel
                    elif k == 'css-pseudo-regex' and re.search(p['match'], sel):
                        hit = sel
                    elif k == 'css-at-rule' and sel.startswith(p['match']):
                        hit = sel
                    elif k == 'css-declaration':
                        for prop, val in decls(block):
                            if re.fullmatch(p['property'], prop) and value_hit(p, val):
                                hit = f'{sel} {{ {prop}: {val} }}'
                    elif k == 'css-flex-gap':
                        ds = dict(decls(block))
                        if ds.get('display') in ('flex', 'inline-flex') and any(g in ds for g in ('gap', 'row-gap', 'column-gap')):
                            hit = f'{sel} {{ display: {ds["display"]}; gap: {ds.get("gap") or ds.get("row-gap") or ds.get("column-gap")} }}'
                    if hit:
                        in_supports = bool(supports) and any(re.search(x, supports) for x in p.get('fallback') or [])
                        use = {'feature': p['feature'], 'file': rel, 'line': ln + offset, 'snippet': hit[:160],
                               'selector': sel if not sel.startswith('@') else None, 'fallback': in_supports or fb}
                        if minified:
                            use['column'] = col
                        if f.suffix not in ('.css', '.scss') and not markup:
                            use['via'] = 'css-in-js'   # CSS from a tagged template literal in a script
                        uses.append(use)
        elif k == 'js-api':
            for js, offset in js_parts:
                clean = strip_js_comments(js)
                for m in re.finditer(p['match'], clean):
                    ln = clean[:m.start()].count('\n') + 1 + offset
                    uses.append({'feature': p['feature'], 'file': rel, 'line': ln,
                                 'snippet': js.splitlines()[ln - offset - 1].strip()[:160], 'selector': None, 'fallback': fb})
        elif k == 'html' and markup:
            for m in re.finditer(p['match'], text):
                ln = text[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
        elif k == 'utility-class' and utility and f.suffix in CLASS_SOURCES:
            # a class token, optionally behind variants ("md:hover:backdrop-blur-xl"), in any string of the file
            clean = strip_js_comments(text) if not markup else text
            rx = r'(?<![\w/-])(?:[\w\[\]@&*>=.()-]+:)*!?(?:' + p['match'] + r')'
            for m in re.finditer(rx, clean):
                ln = clean[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
    if minified:
        # one entry per feature, line and guard: the guarded uses (inside an @supports or with a fallback) and the
        # unguarded ones stay apart, each with how many uses it stands for and its first use's column and snippet, so a
        # reader sees the use that has no fallback; `guarded` counts the guarded uses of the line on its unguarded entry
        merged: dict[tuple, dict] = {}
        for u in uses:
            k = (u['feature'], u['line'], bool(u['fallback']))
            m = merged.get(k)
            if m is None:
                merged[k] = dict(u, occurrences=1)
            else:
                m['occurrences'] += 1
        for (feat, line, fb), m in merged.items():
            if not fb and (feat, line, True) in merged:
                m['guarded'] = merged[(feat, line, True)]['occurrences']
        uses = sorted(merged.values(), key=lambda u: (u['line'], bool(u['fallback']), u.get('column', 0)))   # unguarded first
    return uses


def server_templates(repo: Path, scanned: list[str]) -> tuple[list[str], list[str]]:
    """Scanned markup that is a server-side template (under a templates/views/layouts/partials directory, or using
    template syntax), and scanned markup outside the template and static folders (a stale page that may not be served)."""
    templates, outside = [], []
    for rel in scanned:
        if not rel.lower().endswith(('.html', '.htm')) and not TEMPLATE_FILE.search(rel):
            continue
        parts = rel.lower().split('/')[:-1]
        if any(p in TEMPLATE_DIRS for p in parts) or TEMPLATE_FILE.search(rel):
            templates.append(rel)
            continue
        try:
            text = (repo / rel).read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = ''
        if TEMPLATE_SYNTAX.search(text):
            templates.append(rel)
        elif not any(p in STATIC_DIRS for p in parts):
            outside.append(rel)
    return templates, outside


def server_rendered(cov: dict, repo: Path, scanned: list[str]) -> dict | None:
    """language_coverage for a server-rendered site (see the module doc), or None when it isn't one."""
    # the dominant language, whether the shared census calls it unsupported or (a newer census that counts a template
    # site's server language as supported) not
    lang = cov.get('dominant_unsupported_language') or cov.get('dominant')
    if cov.get('status') == 'no_source' or lang not in SERVER_LANGUAGES:
        return None
    templates, outside = server_templates(repo, scanned)
    if not templates:
        return None
    styles = [f for f in scanned if f.endswith(('.css', '.scss'))]
    scripts = [f for f in scanned if f.endswith(SCRIPT_SUFFIXES)]
    note = (f'Server-rendered {lang} site: {len(templates)} template(s) ({", ".join(templates[:3])}'
            f'{", ..." if len(templates) > 3 else ""}), {len(styles)} stylesheet(s) and {len(scripts)} script file(s) were '
            f'scanned as its browser code. The {lang} code ({(cov.get("unsupported") or {}).get(lang) or (cov.get("supported") or {}).get(lang)} '
            'files) is the server '
            'that renders them, not browser code, so it is outside a feature scan.')
    if outside:
        note += (f' {len(outside)} HTML file(s) outside the template and static folders were scanned too '
                 f'({", ".join(outside[:3])}): check they are served before reporting a use in them.')
    return {'status': 'server_rendered', 'dominant': cov.get('dominant'), 'supported': cov.get('supported'),
            'unsupported': cov.get('unsupported'), 'warning': None, 'note': note,
            'excluded_source_files': cov.get('excluded_source_files'), 'server_language': lang,
            'templates': templates[:100], 'markup_outside_templates': outside[:50]}


# a call that serves a directory as static files; the directory is a string literal shortly after it
SERVE_CALL = re.compile(r'StaticFiles\s*\(|express\.static\s*\(|send_from_directory\s*\(|static_folder\s*=|serveStatic\s*\(|'
                        r'http\.FileServer\s*\(|http\.Dir\s*\(|DocumentRoot\s|static_dir\s*=|directory\s*=')
QUOTED = re.compile(r'''['"]([\w./\\-]{1,120})['"]''')
SERVER_FILES = ('.py', '.js', '.mjs', '.cjs', '.ts', '.go', '.rb', '.php', '.conf')


def served_dirs(repo: Path, candidates: list[str], max_files: int = 300) -> dict[str, str]:
    """Which of the excluded `candidates` (repo-relative directories) the repository's own server code serves as static
    files: a StaticFiles(directory=...) mount, express.static(...), send_from_directory, a Flask static_folder, an
    http.FileServer, a web server root. {dir: the file that serves it}. Reads at most max_files server files, never
    descending into dependency folders."""
    want = {c.strip('/') for c in candidates}
    found: dict[str, str] = {}
    n = 0
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in want and not d.startswith('.'))
        for nm in sorted(names):
            if not nm.endswith(SERVER_FILES) and nm not in ('.htaccess', 'nginx.conf', 'Caddyfile'):
                continue
            n += 1
            if n > max_files:
                return found
            p = Path(root) / nm
            try:
                text = p.read_text(encoding='utf-8', errors='replace')[:200000]
            except OSError:
                continue
            for m in SERVE_CALL.finditer(text):
                for q in QUOTED.finditer(text[m.end():m.end() + 120]):
                    d = q.group(1).replace('\\', '/').removeprefix('./').strip('/')
                    if d in want and d not in found:
                        found[d] = rel_name(p, repo)
    return found


def apply_includes(census: dict, repo: Path, included: list[str]) -> dict:
    """The census with each --include directory counted as product code: its exclusion lifted and its own source
    files added to the language counts, so the language guard sees the browser code that is now scanned."""
    inc = [i.replace('\\', '/').strip('/') for i in included if i.strip('/')]
    if not inc:
        return census
    c = dict(census)
    c['excluded'] = [e for e in census['excluded'] if not any(e['path'] == i or i.startswith(e['path'] + '/') or
                                                              e['path'].startswith(i + '/') for i in inc)]
    c['excluded_source_files'] = sum(e['source_files'] for e in c['excluded'])
    langs = dict(census.get('languages') or {})
    for i in inc:
        if (repo / i).is_dir():
            for k, v in (lang_census.census(repo / i).get('languages') or {}).items():
                langs[k] = langs.get(k, 0) + v
    ordered = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    c['languages'] = ordered
    c['total_source_files'] = sum(ordered.values())
    c['dominant'] = next(iter(ordered), None)
    c['dominant_files'] = ordered.get(c['dominant'], 0) if c['dominant'] else 0
    return c


def static_served(cov: dict, scanned: list[str], serving: dict[str, str]) -> dict | None:
    """language_coverage for a server that serves a static front end from a folder (a FastAPI StaticFiles mount on a
    committed dist/, express.static, an http.FileServer): the server language is not browser code, and the served
    folder's files were scanned. None unless the dominant language is a server language and scanned files lie in a
    folder the server code serves."""
    lang = cov.get('dominant_unsupported_language')
    if cov.get('status') != 'unsupported_dominant' or lang not in SERVER_LANGUAGES | {'Go', 'Java', 'C#', 'Rust'}:
        return None
    dirs = sorted(d for d in serving if any(f.startswith(d + '/') for f in scanned))
    if not dirs:
        return None
    n = sum(1 for f in scanned if any(f.startswith(d + '/') for d in dirs))
    note = (f'A {lang} server serving a static front end: {n} file(s) under {", ".join(d + "/" for d in dirs)} '
            f'(served by {", ".join(sorted({serving[d] for d in dirs}))}) were scanned as its browser code. The '
            f'{cov.get("dominant_unsupported_files")} {lang} file(s) are the server, not browser code, so they are outside '
            'a feature scan.')
    return {'status': 'static_served', 'dominant': cov.get('dominant'), 'supported': cov.get('supported'),
            'unsupported': cov.get('unsupported'), 'warning': None, 'note': note,
            'excluded_source_files': cov.get('excluded_source_files'), 'server_language': lang,
            'served_dirs': [{'path': d, 'served_by': serving[d]} for d in dirs]}


def support_for(feature: str, pattern: dict, wf: dict) -> tuple[dict | None, str | None]:
    if pattern.get('support'):
        return pattern['support'], pattern.get('baseline')
    f = wf.get(pattern.get('web_feature') or feature)
    if not f:
        return None, None
    st = f.get('status') or {}
    b = st.get('baseline')
    return st.get('support') or {}, ('high' if b == 'high' else 'low' if b == 'low' else 'limited')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--query')
    ap.add_argument('--built-css', type=Path, action='append',
                    help='a built CSS file or folder to scan as well (repeatable); default .next/static/css when it exists')
    ap.add_argument('--exclude', action='append', default=[], metavar='DIR',
                    help='a repository-relative directory to leave out (repeatable), e.g. a data or archive folder')
    ap.add_argument('--include', action='append', default=[], metavar='DIR',
                    help='a repository-relative directory to scan although a prune rule would leave it out (repeatable): '
                         'a committed dist/, build/ or public/ folder that is the front end the server serves as written')
    a = ap.parse_args()
    from fetch_data import asset_path
    caniuse = json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))
    wf = json.loads(asset_path('web-features').read_text(encoding='utf-8'))['features']
    pats = json.loads((SKILL / 'data' / 'feature-patterns.json').read_text(encoding='utf-8'))['patterns']
    query, source = (a.query, 'argument') if a.query else bl.query_from_repo(a.repo)
    targets = bl.Resolver(caniuse).resolve(query)
    built = list(a.built_css or [])
    if not built:
        built = next_built_css(a.repo)
    built_files = [rel_name(f, a.repo) for f in built_css_files(built)]
    fw = utility_framework(a.repo)
    tw_major = None
    if fw:
        m = re.search(r'(\d+)', str(fw['version']))
        tw_major = int(m.group(1)) if m else None
    cij = css_in_js_libraries(a.repo)
    skipped: dict = {}
    census = apply_includes(lang_census.census(a.repo, a.exclude), a.repo, a.include)
    excluded = [e['path'] for e in census['excluded']]
    scanned: list[str] = []
    uses = scan(a.repo, pats, utility=bool(fw), built=built, css_in_js=bool(cij), tailwind_major=tw_major, skipped=skipped,
                excluded=census['excluded'], user_excluded=a.exclude, scanned=scanned, included=a.include,
                server_only=next_server_files(a.repo))
    if a.include:
        skipped['included'] = [i.replace('\\', '/').strip('/') for i in a.include]
    embedded = skipped.pop('embedded_html', [])
    cov = lang_census.language_coverage(census, SCANNED_LANGUAGES, tool='feature_scan.py', what='feature scan')
    served = server_rendered(cov, a.repo, scanned)
    limitations = []
    # excluded trees that hold browser code: a committed dist/ or public/ folder may be the front end itself
    user_ex = {e.replace('\\', '/').strip('/') for e in a.exclude}
    held = [e for e in census['excluded'] if e['path'] not in user_ex and
            (e['source_files'] or any((a.repo / e['path']).glob('*.htm*')))
            and not re.search(r'(^|/)(node_modules|vendor|vendored|third[-_]?party|bower_components|\.venv|venv)(/|$)', e['path'])]
    tops = {f.split('/')[0] for f in scanned if '/' in f}
    serving = served_dirs(a.repo, sorted(tops | {e['path'] for e in held}))
    served = served or static_served(cov, scanned, serving)
    # a framework's build output whose CSS was read with --built-css (.next/static/css): say so, and that its built
    # JavaScript was not read (APIs are checked in the source; polyfills the build adds are not seen)
    built_dirs: dict[str, Path] = {}
    build_note: dict[str, str] = {}
    for b in built:
        parts = Path(rel_name(b, a.repo)).parts
        if '.next' in parts:
            nxt = Path(*parts[:parts.index('.next') + 1]).as_posix()
            built_dirs[nxt] = a.repo / nxt
    for nxt, d in sorted(built_dirs.items()):
        build_note[nxt] = 'next build output: its CSS was scanned (--built-css); its JavaScript was not'
        js = sum(1 for _ in (d / 'static' / 'chunks').rglob('*.js')) if (d / 'static' / 'chunks').is_dir() else 0
        limitations.append(f'The built JavaScript in {nxt}/static/chunks ({js} file(s)) was not scanned: JavaScript '
                           'APIs were checked in the source, so polyfills the build adds (or leaves out) are not seen; '
                           f'only the built CSS in {nxt}/static/css was read')
    if held:
        skipped['excluded_with_code'] = [{'path': e['path'], 'reason': e['reason'], 'source_files': e['source_files'],
                                          'served_by': serving.get(e['path']) or build_note.get(e['path'])}
                                         for e in held[:30]]
    # the build output whose CSS was read is said in its own limitation, not offered for --include
    held = [e for e in held if e['path'] not in built_dirs]
    hint = ''
    if held:
        names = [e['path'] for e in held]
        first = next((n for n in names if n in serving), names[0])
        if served:   # a served-static or server-rendered site whose other excluded folders still hold code: say so
            served['note'] += ' Also left out: ' + ', '.join(f'{n}/' for n in names[:5]) + f' (--include {first} to scan it).'
        hint = (' Left out as build or third-party output: ' +
                ', '.join(f'{e["path"]}/ ({e["source_files"]} source file(s)' +
                          (f', served by {serving[e["path"]]}' if e['path'] in serving else '') + ')' for e in held[:5]) +
                f'. If one is the front end served as written (not generated), rerun with --include {first}.')
    if served:
        print(f'note: {served["note"]}', file=sys.stderr)
    elif cov['status'] == 'unsupported_dominant':
        lang = cov['dominant_unsupported_language']
        note = (f'The repository is predominantly {lang} ({cov["dominant_unsupported_files"]} source files, vendored and '
                f'third-party trees excluded), which the feature scan does not read as browser code. ' +
                (f'HTML, CSS and scripts held in {len(embedded)} {lang} string constant(s) or template file(s) were scanned '
                 f'({", ".join(embedded[:3])}); any page {lang} code builds another way was not.' if embedded else
                 f'No HTML held in a {lang} string constant or template file was found, so the browser code (if any) was not '
                 'scanned: 0 uses is not evidence that the targets support the page; scan a rendered page instead.') + hint)
        limitations.append(note)
        print(f'WARN: {note}', file=sys.stderr)
    elif any(e['path'] in serving for e in held):
        note = ('Directories the server serves as static files were left out as build output, so their browser code was '
                'not scanned.' + hint)
        limitations.append(note)
        print(f'WARN: {note}', file=sys.stderr)
    if fw:
        fw.update(classes='known non-baseline utilities only (utility-class patterns in data/feature-patterns.json)',
                  built_css=built_files)
        if not built_files:
            limitations.append(f'Utility-class CSS ({fw["framework"]} {fw["version"]}) was scanned only for the known non-baseline '
                               'utilities; the CSS it generates (colour functions, @property, cascade layers, nesting) was not '
                               'scanned: build the site and pass --built-css')
        floor = FRAMEWORK_FLOORS.get(('tailwind', tw_major)) if tw_major else None
        if floor:
            low: dict[str, list[str]] = {}
            for t in targets['resolved']:
                if t['browser'] in floor and bl.vnum(t['version']) < bl.vnum(floor[t['browser']]):
                    low.setdefault(t['browser'], []).append(t['version'])
            below = []
            for b, vs in sorted(low.items()):
                vs = sorted(vs, key=bl.vnum)
                below.append(f'{b} {vs[0]}' + (f'-{vs[-1]}' if len(vs) > 1 else ''))
            fw['framework_floor'] = {b: v for b, v in floor.items()}
            if below:
                fw['targets_below_floor'] = below
                limitations.append(f'Tailwind CSS {tw_major} itself requires {", ".join(f"{b} {v}" for b, v in floor.items())} '
                                   f'or later, above some declared targets ({", ".join(below[:6])}'
                                   f'{", ..." if len(below) > 6 else ""}): the project\'s real support floor is ambiguous, '
                                   'and its utility CSS is not expected to work below it')
    elif built_files:
        fw = {'framework': None, 'version': None, 'classes': 'not scanned', 'built_css': built_files}
    css_in_js = None
    if cij:
        n = sum(1 for u in uses if u.get('via') == 'css-in-js')
        css_in_js = {'libraries': cij, 'uses': n,
                     'scanned': 'styles written literally in scripts that use the library: object styles (sx props, '
                                'styled({...}), theme styleOverrides) and tagged template literals (styled.div`...`, css`...`)'}
        limitations.append(f'CSS-in-JS ({", ".join(cij)}) generates its CSS at runtime: only styles written literally in '
                           'scripts were scanned; styles computed at runtime (theme functions, spread or conditional '
                           'objects, values from variables) were not, and neither the source scan nor --built-css sees them')
    findings = []
    supported_feats: list[dict] = []
    by_feature: dict[str, list] = {}
    for u in uses:
        by_feature.setdefault(u['feature'], []).append(u)
    for feat, us in by_feature.items():
        pat = next(p for p in pats if p['feature'] == feat)
        support, baseline = support_for(feat, pat, wf)
        if support is None:
            findings.append({'feature': feat, 'name': pat['name'], 'baseline_status': None, 'files': us, 'unsupported_in': [],
                             'not_assessed': ['the feature is not in web-features'], 'fallback': all(u['fallback'] for u in us),
                             'status': 'not_assessed', 'linked_differences': []})
            continue
        unsupported, not_assessed = [], set()
        for t in targets['resolved']:
            key = WF_BROWSER.get(t['browser'])
            if not key:
                not_assessed.add(t['browser'])
                continue
            first = support.get(key)
            if first is None or bl.vnum(first.lstrip('≤')) > bl.vnum(t['version']):
                unsupported.append({'browser': t['browser'], 'version': t['version'], 'engine': t['engine'], 'usage': t['usage'],
                                    'first_supported': first})
        if not unsupported:
            # used, and every target supports it: recorded, so a reader can see the API was checked and not missed
            supported_feats.append({'feature': feat, 'name': pat['name'], 'baseline_status': baseline, 'uses': len(us),
                                    'first': {k: us[0][k] for k in ('file', 'line') if k in us[0]}})
            continue
        findings.append({'feature': feat, 'name': pat['name'], 'baseline_status': baseline,
                         'files': [{k: u[k] for k in ('file', 'line', 'column', 'occurrences', 'guarded', 'snippet', 'selector',
                                                       'fallback', 'via') if k in u} for u in us],
                         'unsupported_in': unsupported, 'unsupported_usage': round(sum(x['usage'] for x in unsupported), 3),
                         'not_assessed': sorted(not_assessed), 'fallback': all(u['fallback'] for u in us),
                         **({'degrades': pat['degrades']} if pat.get('degrades') else {}),
                         'status': 'possible', 'linked_differences': []})
    out = {'repo': a.repo.as_posix(), 'browserslist': {'query': query, 'source': source, 'resolved_count': len(targets['resolved']),
                                                       'coverage': targets['coverage'], 'unresolved': targets['unresolved']},
           'uses': sum(u.get('occurrences', 1) for u in uses), 'features': findings,
           'scanned_files': {'count': len(scanned), 'files': scanned[:300]}}
    if fw:   # only written when there is something to say, so a plain CSS/JS repo's output is unchanged
        out['utility_css'] = fw
    if css_in_js:
        out['css_in_js'] = css_in_js
    if served:
        out['language_coverage'] = served | {'embedded_html': embedded, 'excluded': excluded[:30]}
    elif cov['status'] == 'unsupported_dominant' or embedded:
        out['language_coverage'] = {k: cov.get(k) for k in ('status', 'dominant', 'supported', 'unsupported', 'warning',
                                                           'excluded_source_files')} | {
            'embedded_html': embedded, 'excluded': excluded[:30]}
    if supported_feats:   # patterns found in the code that every target supports (no finding, but checked)
        out['supported_features'] = supported_feats
    # what was left out and why: pruned folders, census exclusions, test directories, Node-only scripts, data trees
    out['skipped'] = skipped
    if limitations:
        out['limitations'] = limitations
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(uses)} use(s), {len(findings)} feature finding(s) for "{query}" -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
