#!/usr/bin/env python3
"""Recon for crash hunting: platforms, routes and screens, the error-boundary map, the data layer,
API contracts, persisted state and supported targets. Stdlib only; never runs or modifies the repo.

    python recon.py <repo> [--journey-map journey_map.json] [--out recon.json]

Everything is heuristic and records file:line so the agent can verify it. The boundary map is what
decides whether a throw is B1 (nothing catches it) or B2 (a route or screen boundary does), so the
agent must confirm which boundary wraps each finding rather than trust this list alone.
--journey-map attaches journey priorities (P1/P2/P3) to routes, for the reach factor.

It also decides the site model (site.kind) and the blast-radius model. A single-page app or a
server-rendered framework has one render tree (per_root). A static multi-page site (HTML pages that
each load their own scripts, no SPA framework) is per_script: an uncaught error stops only the
script that threw, so it breaks a feature (B3) or only logs (B4), except on pages whose content is
itself rendered by script. For such sites it lists the pages (one route per page, or one per set of
scripts when there are many), the scripts each page loads, scripts that inject other scripts,
inline scripts, and error pages (404.html) whose relative asset URLs break at nested paths.

Next.js: pages become routes (route groups and parallel-route slots are not URL segments), the root
layout becomes a shell item, and "nextjs" lists layouts, template/not-found/loading files,
middleware.ts/proxy.ts with its matcher, next/dynamic imports (and whether they skip SSR) and the
route handlers with their HTTP methods. Route handlers are API code and never UI routes. A
`path: "/..."` object is a route only in router configuration, not in cookie options or OpenAPI
documents; OpenAPI documents built in code (including a FastAPI app, with the command that exports
it) and Prisma schemas are listed as contracts. Boundary definitions that nothing renders are marked
used: false. The Next.js file conventions apply only in a project whose package.json depends on next,
to app/ and pages/ at its root or under src/.

Router boundaries count: a route's errorElement / ErrorBoundary (React Router 6.4+ and 7, Remix), a
route module's exported ErrorBoundary, TanStack Router's errorComponent and react-error-boundary's
withErrorBoundary. Modules that read import.meta.env / process.env and throw at module scope are
listed in module_scope_config: they fail on import, before any boundary renders (Node server and CLI
modules are left out, and a module no UI entry point imports has reachable_from_ui false). A FastAPI
service with no UI is site.kind "api".

A Node HTTP server that renders pages itself (node:http createServer, Express, Fastify, Koa) from HTML
template literals is a multi_page / per_script site with site.served_by "node_server" and site.server:
its routes come from pathname comparisons, case '/x':, app.get('/x') and '/path': handler tables in the
server and the modules it imports; /api/*, JSON and non-GET handlers are api_endpoints. A directory
holding next.config.* is a Next.js project root even without its own package.json. A test-named
directory (e2e/, tests/) with its own package manifest is a package, not tests. Comment lines never
count as persisted-state uses.

Vendored and bundled third-party trees are left out of everything (langs.py, via the shared
lang_census.py). language_coverage counts source files per language; when an unanalysed language
dominates (a Go tool), a top-level warning says so, and site.kind and the blast radius are "unknown"
rather than modelled from incidental files. HTML held in string constants (a Go
html/template constant, a Python triple-quoted string) and template files are listed in
site.embedded_templates; scan_pitfalls.py scans their inline scripts. Xcode <Name>Tests targets are tests.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_graph import (projects, next_path_in_project, project_of, note_packages, in_test_dir, node_role,  # noqa: E402
                      graph as ui_graph, resolve as resolve_import, IMPORT, NEXT_CONFIG)
from scan_pitfalls import mask_code  # noqa: E402
from langs import (vendored_roots, language_census, language_warning, incidental, embedded_documents,  # noqa: E402
                   HOST_EXT, TEMPLATE_FILE, under, prune_dir, is_template_path, server_rendered,
                   skipped_data_dirs, EXCLUDE)

SKIP_DIRS = {'.git', 'node_modules', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', '.output', 'coverage',
             'vendor', 'Pods', 'Carthage', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__',
             'storybook-static', '.expo', 'venv', '.venv', 'target', '.turbo', '.cache'}
TEST_NAME = re.compile(r'(\.|_)(test|spec|stories)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|\.d\.ts$', re.I)
CODE_EXT = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.svelte', '.kt', '.java', '.swift', '.dart'}
Q = r'["\'`]'

# Python web frameworks named in requirements files or pyproject.toml (one dependency per line)
PY_WEB = re.compile(r'''^\s*["']?(fastapi|flask|django)(?=[\s"'<>=~!\[;,]|$)''', re.I | re.M)
FASTAPI_APP = re.compile(r'^(\w+)\s*(?::\s*[\w.]+\s*)?=\s*(?:fastapi\.)?FastAPI\(', re.M)
# configuration read from import.meta.env (Vite) or process.env at module scope, next to a top-level throw:
# it runs on import, before any boundary renders, so a missing or bad value blanks the app (B1)
ENV_READ = re.compile(r'\bimport\.meta\.env\b|\bprocess\.env\b')
COMMENT_LINE = re.compile(r'^\s*(//|/\*|\*|#|<!--)')
CONTROL_BLOCK = re.compile(r'(?:\b(?:if|else|try|catch|finally)\b\s*(?:\([^{};]*\))?|\}\s*)\s*$')


def module_scope_throws(code: str) -> list[int]:
    """Offsets of `throw` statements that run when the module is imported: at the top level, or only inside
    if/else/try blocks there (not inside a function, class or callback). `code` has strings and comments
    blanked (scan_pitfalls.mask_code)."""
    out, stack, last = [], [], 0
    for m in re.finditer(r'[{}]|\bthrow\b', code):
        tok = m.group(0)
        if tok == '{':
            stack.append('ctl' if CONTROL_BLOCK.search(code[last:m.start()].rsplit('\n', 1)[-1]) else 'other')
            last = m.end()
        elif tok == '}':
            if stack:
                stack.pop()
            last = m.end()
        elif all(k == 'ctl' for k in stack):
            out.append(m.start())
    return out
PLATFORM_DEPS = {'react': 'react', 'next': 'nextjs', 'vue': 'vue', 'nuxt': 'nuxt', '@angular/core': 'angular',
                 'svelte': 'svelte', '@sveltejs/kit': 'sveltekit', '@remix-run/react': 'remix', 'react-native': 'react-native',
                 'expo': 'expo', 'preact': 'preact', 'solid-js': 'solid'}
DATA_LAYER = {
    'fetch': r'\bfetch\(', 'axios': r'\baxios\b', 'react-query': r'@tanstack/react-query|\buseQuery\(', 'swr': r'\buseSWR\(',
    'apollo': r'@apollo/client', 'urql': r'\burql\b', 'retrofit': r'\bretrofit2?\b|@GET\(|@POST\(', 'okhttp': r'\bOkHttpClient\b',
    'ktor-client': r'\bHttpClient\(', 'urlsession': r'\bURLSession\b', 'alamofire': r'\bAlamofire\b|\bAF\.request\(',
    'dio': r'\bDio\(', 'dart-http': r'package:http/http\.dart',
}
PERSISTED = {
    'localStorage': r'\blocalStorage\b', 'sessionStorage': r'\bsessionStorage\b', 'indexedDB': r'\bindexedDB\b|\bidb-keyval\b|\bDexie\b',
    'AsyncStorage': r'\bAsyncStorage\b', 'MMKV': r'\bMMKV\b', 'SharedPreferences': r'\bSharedPreferences\b|getSharedPreferences\(',
    'DataStore': r'\bDataStore<|preferencesDataStore', 'Room': r'@Database\(|@Entity\b', 'UserDefaults': r'\bUserDefaults\b|@AppStorage\(',
    'CoreData': r'\bNSPersistentContainer\b|\bNSManagedObject\b', 'SwiftData': r'@Model\b', 'Keychain': r'\bSecItem(Add|CopyMatching)\b',
    'shared_preferences': r'\bSharedPreferences\.getInstance\(', 'Hive': r'\bHive\.(openBox|box)\(', 'sqflite': r'\bopenDatabase\(',
}
BOUNDARY_RX = [
    ('react', 'definition', re.compile(r'\b(componentDidCatch|getDerivedStateFromError)\b')),
    ('react', 'usage', re.compile(r'<((?:Sentry\.)?\w*ErrorBoundary\w*|\w*Boundary)\b(?![\w.])')),
    ('vue', 'definition', re.compile(r'\berrorCaptured\b|\bonErrorCaptured\(')),
    ('vue', 'global_handler', re.compile(r'\bapp\.config\.errorHandler\b')),
    ('angular', 'global_handler', re.compile(r'implements\s+ErrorHandler\b|provide\s*:\s*ErrorHandler\b')),
    ('web', 'global_handler', re.compile(r'\bwindow\.onerror\b|addEventListener\(\s*' + Q + r'(error|unhandledrejection)' + Q)),
    ('react-native', 'global_handler', re.compile(r'\bErrorUtils\.setGlobalHandler\(')),
    ('flutter', 'global_handler', re.compile(r'\bErrorWidget\.builder\b|\bFlutterError\.onError\b|PlatformDispatcher\.instance\.onError|\brunZonedGuarded\(')),
    ('android', 'global_handler', re.compile(r'setDefaultUncaughtExceptionHandler\(|\bCoroutineExceptionHandler\b')),
    ('ios', 'global_handler', re.compile(r'\bNSSetUncaughtExceptionHandler\(')),
]
# Router-level boundaries: a route's errorElement / ErrorBoundary (React Router 6.4+ data routers and 7, Remix),
# TanStack Router's errorComponent, a route module's exported ErrorBoundary, react-error-boundary's HOC.
# (framework, kind, regex, needs a router file); group 1, when present, names the fallback component.
ROUTE_BOUNDARY_RX = [
    ('react-router', 'route_prop', re.compile(r'\berrorElement\s*(?::|=\s*\{)\s*(?:<\s*([\w.]+))?'), False),
    ('react-router', 'route_prop', re.compile(r'<Route\b[^>]*?\bErrorBoundary\s*=\s*\{\s*([\w.]+)'), False),
    ('react-router', 'route_prop', re.compile(r'(?<![\w.$])ErrorBoundary\s*:\s*([A-Za-z_$][\w.$]*)'), True),
    ('tanstack-router', 'route_prop', re.compile(r'\b(?:defaultErrorComponent|errorComponent)\s*:\s*([A-Za-z_$][\w.$]*)?'), True),
    ('react-router', 'route_module', re.compile(r'^\s*export\s+(?:async\s+)?(?:function|const|let)\s+(ErrorBoundary)\b', re.M), False),
    ('react', 'hoc', re.compile(r'\b(withErrorBoundary)\s*\('), False),
]
ROUTE_BOUNDARY_COVERS = {
    'route_prop': "the route's element and its child routes up to the next errorElement: render errors, loader and "
                  'action errors and failed lazy() imports (not event handlers or async code)',
    'route_module': "this route module's component and its child routes: render, loader and action errors",
    'hoc': 'the wrapped component only',
}
CLASS_NAME = re.compile(r'class\s+(\w+)')
ROUTE_RX = [
    ('jsx-route', re.compile(r'<Route\b[^>]*?\bpath=\{?' + Q + r'([^"\'`]+)' + Q)),
    ('route-object', re.compile(r'\bpath\s*:\s*' + Q + r'(/[^"\'`]*|\*\*?)' + Q)),
    ('go-router', re.compile(r'GoRoute\([^)]*?path\s*:\s*' + Q + r'([^"\'`]+)' + Q)),
    ('compose', re.compile(r'\bcomposable\(\s*(?:route\s*=\s*)?"([^"]+)"')),
    ('rn-screen', re.compile(r'<\w+\.Screen\b[^>]*?\bname=\{?' + Q + r'([^"\'`]+)' + Q)),
    ('swiftui-view', re.compile(r'struct\s+(\w+(?:View|Screen|Page))\s*:\s*(?:some\s+)?View\b')),
    ('uikit-vc', re.compile(r'class\s+(\w+)\s*:\s*UI\w*ViewController\b')),
    ('android-screen', re.compile(r'class\s+(\w+(?:Activity|Fragment))\b[^{]*?:\s*\w*(?:Activity|Fragment)\b')),
    ('flutter-screen', re.compile(r'class\s+(\w+(?:Screen|Page))\s+extends\s+(?:Stateless|Stateful)Widget')),
]
# matched against the path inside a Next.js project (see ui_graph.next_path_in_project)
NEXT_FILES = re.compile(r'^(app|src/app)/(.*/)?(page|error|global-error|layout)\.(tsx|jsx|js|ts)$|^(pages|src/pages)/(?!api/).+\.(tsx|jsx|js|ts)$')
NEXT_SPECIAL = re.compile(r'^(?P<dir>(?:src/)?app(?:/.*)?)/(?P<kind>layout|template|not-found|loading|error|global-error|default)\.(tsx|jsx|js|ts)$')
NEXT_HANDLER = re.compile(r'^(?:src/)?app/(?P<seg>(?:.*/)?)route\.(ts|js|mts|mjs|tsx|jsx)$|^(?:src/)?pages/(?P<api>api/.+)\.(ts|js|tsx|jsx)$')
HTTP_EXPORT = re.compile(r'\bexport\s+(?:async\s+)?(?:function|const|let)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|'
                         r'\bexport\s*\{([^}]*)\}')
DYNAMIC = re.compile(r'\bdynamic\s*\(\s*(?:async\s*)?\(\s*\)\s*=>\s*import\(\s*["\']([^"\']+)["\']\s*\)')
DEFAULT_EXPORT = re.compile(r'^\s*export\s+default\b', re.M)
# 'path: "/..."' is a route only in router configuration: not in cookie options, OpenAPI documents or route handlers
ROUTER_FILE = re.compile(r'createBrowserRouter|createHashRouter|createRouter|createRoute\b|RouterModule|\bRoutes\b|'
                         r'\broutes\s*[:=]|\bcomponent\s*:|\belement\s*:|loadComponent|loadChildren|\blazy\s*:')
NOT_ROUTER_FILE = re.compile(r'["\']?(openapi|swagger)["\']?\s*:\s*["\']\d|\boperationId\b|\brequestBody\b')
OPENAPI_CODE = re.compile(r'["\']?(openapi|swagger)["\']?\s*:\s*["\'][23]\.\d')
COOKIE_CONTEXT =re.compile(r'\b(httpOnly|sameSite|secure|maxAge|expires|domain)\s*:|cookies?\b', re.I)

def walk(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not (p.name.startswith('.') and p.name not in {'.github', '.maestro'}) \
                        and not prune_dir(root, p):
                    stack.append(p)
            else:
                yield p

def is_bundle(p: Path) -> bool:
    """Built output (a sibling .map file, or a very large script) is not source to analyse."""
    try:
        return p.with_name(p.name + '.map').exists() or (p.suffix in ('.js', '.mjs', '.cjs') and p.stat().st_size > 400_000)
    except OSError:
        return False

def read(p: Path) -> str:
    try:
        return p.read_text(encoding='utf-8', errors='replace') if p.stat().st_size < 1_500_000 else ''
    except OSError:
        return ''

def line_of(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1

def detect_platforms(root: Path, files: list[Path]) -> tuple[list[str], dict]:
    plats: set[str] = set()
    targets: dict = {}
    for f in files:
        rel = f.relative_to(root).as_posix()
        if f.name == 'package.json' and 'node_modules' not in rel:
            try:
                pkg = json.loads(read(f) or '{}')
            except ValueError:
                continue
            deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
            plats |= {name for dep, name in PLATFORM_DEPS.items() if dep in deps}
            if pkg.get('browserslist'):
                targets['browserslist'] = {'value': pkg['browserslist'], 'file': rel}
        elif f.name == '.browserslistrc':
            targets['browserslist'] = {'value': [l.strip() for l in read(f).splitlines() if l.strip() and not l.startswith('#')], 'file': rel}
        elif f.name in ('build.gradle', 'build.gradle.kts'):
            m = re.search(r'minSdk(?:Version)?\s*[=(]?\s*(\d+)', read(f))
            plats.add('android')
            if m:
                targets['android_min_sdk'] = {'value': int(m.group(1)), 'file': rel}
        elif NEXT_CONFIG.match(f.name):  # a Next.js project, possibly nested below the package that depends on next
            plats.add('nextjs')
        elif f.name == 'AndroidManifest.xml':
            plats.add('android')
        elif f.name == 'project.pbxproj':
            plats.add('ios')
            m = re.search(r'IPHONEOS_DEPLOYMENT_TARGET\s*=\s*([\d.]+)', read(f))
            if m:
                targets['ios_deployment_target'] = {'value': m.group(1), 'file': rel}
        elif f.name == 'Podfile':
            m = re.search(r"platform\s+:ios,\s*'([\d.]+)'", read(f))
            if m:
                targets.setdefault('ios_deployment_target', {'value': m.group(1), 'file': rel})
        elif f.name == 'Package.swift':
            plats.add('ios')
        elif re.match(r'^(requirements[\w.-]*\.txt|pyproject\.toml|Pipfile|setup\.cfg)$', f.name):
            plats |= {m.lower() for m in PY_WEB.findall(read(f))}
        elif f.name == 'pubspec.yaml':
            text = read(f)
            if re.search(r'^\s*flutter\s*:', text, re.M):
                plats.add('flutter')
            m = re.search(r'sdk\s*:\s*[\'"]?([^\'"\n]+)', text)
            if m:
                targets['dart_sdk'] = {'value': m.group(1).strip(), 'file': rel}
    if any(p.suffix == '.swift' for p in files):
        plats.add('ios')
    if any(p.suffix == '.dart' for p in files):
        plats.add('flutter')
    if any(p.suffix in ('.kt', '.java') and '/src/main/' in p.as_posix() for p in files):
        plats.add('android')
    return sorted(plats), targets

SPA_PLATFORMS = {'react', 'vue', 'angular', 'svelte', 'preact', 'solid', 'react-native', 'expo'}
SSR_PLATFORMS = {'nextjs', 'nuxt', 'sveltekit', 'remix'}
SCRIPT_TAG = re.compile(r'<script\b([^>]*)>(.*?)</script\s*>', re.I | re.S)
ATTR = lambda name: re.compile(r'\b' + name + r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.I)
SRC, HREF, TYPE, REL = ATTR('src'), ATTR('href'), ATTR('type'), ATTR('rel')
LINK_TAG = re.compile(r'<link\b[^>]*>', re.I)
ASSET_TAG = re.compile(r'<(?:img|source|link|script)\b[^>]*>', re.I)
DATA_TYPES = re.compile(r'json|template|text/(?!javascript)|x-shader', re.I)
ERROR_PAGE = re.compile(r'^(404|500|50x|error|not[-_]?found)\.html?$', re.I)
PAGE_LIST_MAX = 60
INJECT = re.compile(r'createElement\(\s*["\']script["\']\s*\)')
INJECT_SRC = re.compile(r'\.src\s*=\s*["\'`]([^"\'`$]+)["\'`]')
# an empty element that script fills: <div id="app"></div>, <main id="root"></main>
MOUNT_POINT = re.compile(r'<(div|main|section)\b[^>]*\bid\s*=\s*["\']?(app|root|main|content|__next|__nuxt|svelte)["\']?[^>]*>\s*</\1\s*>', re.I)

def attr(rx: re.Pattern, tag: str) -> str | None:
    m = rx.search(tag)
    return next((g for g in m.groups() if g is not None), None) if m else None

def is_relative(url: str) -> bool:
    return bool(url) and not re.match(r'^([a-z][\w+.-]*:|//|/|#|\{|\$)', url, re.I)

def html_page(root: Path, f: Path) -> dict:
    """The scripts and stylesheets one HTML page loads, and whether its content is rendered by script."""
    rel = f.relative_to(root).as_posix()
    text = read(f)
    scripts, inline = [], 0
    for m in SCRIPT_TAG.finditer(text):
        tag_attrs, body = m.group(1), m.group(2)
        kind = attr(TYPE, tag_attrs) or ''
        if DATA_TYPES.search(kind):
            continue
        src = attr(SRC, tag_attrs)
        if not src:
            inline += 1 if body.strip() else 0
            continue
        entry: dict = {'src': src, 'line': line_of(text, m.start()), 'module': kind.lower() == 'module'}
        if re.match(r'^([a-z][\w+.-]*:)?//', src, re.I):
            entry['external'] = True
        else:
            path = src.split('?')[0].split('#')[0]
            target = (root / path.lstrip('/')) if path.startswith('/') else (f.parent / path)
            try:
                entry['file'] = target.resolve().relative_to(root).as_posix()
            except ValueError:
                entry['file'] = path
            entry['exists'] = target.exists()
        scripts.append(entry)
    styles = [attr(HREF, t) for t in LINK_TAG.findall(text) if 'stylesheet' in (attr(REL, t) or '').lower()]
    body = re.search(r'<body\b[^>]*>(.*)</body\s*>', text, re.I | re.S)
    visible = re.sub(r'<(script|style|template|noscript)\b.*?</\1\s*>', ' ', body.group(1) if body else text, flags=re.I | re.S)
    visible = re.sub(r'<[^>]+>', ' ', visible)
    text_len = len(' '.join(visible.split()))
    page = {'file': rel, 'scripts': scripts, 'inline_scripts': inline, 'stylesheets': [s for s in styles if s],
            'text_chars': text_len,
            'content_rendered_by_js': bool(scripts or inline) and (text_len < 20 or bool(MOUNT_POINT.search(text)))}
    if ERROR_PAGE.match(f.name):
        rels = sorted({u for t in ASSET_TAG.findall(text) for u in (attr(SRC, t), attr(HREF, t)) if u and is_relative(u)})
        if rels:
            page['error_page_relative_urls'] = rels
    return page

# A Node server that renders HTML itself (node:http createServer, Express, Fastify, Koa): routes are
# pathname comparisons, Express-style app.get('/x') calls, `case '/x':` branches, or an object of
# '/path': handler entries in the server or a module it imports; pages are HTML template literals.
PATH_CHECK = re.compile(r'''\b(?:pathname|url|path)\s*===?\s*["'`](/[^"'`\s]*)["'`]''')
CASE_PATH = re.compile(r'''\bcase\s+["'`](/[^"'`\s]*)["'`]\s*:''')
EXPRESS_ROUTE = re.compile(r'''\b(?:app|router|server|fastify)\.(get|post|put|patch|delete|all|route)\s*\(\s*["'`](/[^"'`]*)["'`]''')
TABLE_KEY = re.compile(r'''^[ \t]*["'](/[^"'\s]*)["']\s*:''', re.M)
METHOD_ON_LINE = re.compile(r'''\bmethod\s*===?\s*["'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["']''')
HTML_IN_TEMPLATE = re.compile(r'`[^`]*?<(?:!doctype|html|body|main|h1|form|div|section|nav)\b', re.I | re.S)
INLINE_SCRIPT_TAG = re.compile(r'<script\b(?![^>]*\bsrc\s*=)[^>]*>', re.I)
SRC_SCRIPT_TAG = re.compile(r'<script\b[^>]*\bsrc\s*=', re.I)
JSON_RESPONSE = re.compile(r'application/json|JSON\.stringify|\.json\s*\(')
SERVER_IMPORT_DEPTH = 4


def node_server_model(root: Path, code: list[Path]) -> dict | None:
    """Pages served by a Node HTTP server from HTML template literals (no framework, no .html files): the
    server modules, the modules they import (followed through relative imports), the routes they serve
    and the inline scripts in their templates. None when there is no such server."""
    servers = [f for f in code if f.suffix in ('.js', '.mjs', '.cjs', '.ts', '.mts') and node_role(read(f), f.name) == 'server']
    if not servers:
        return None
    modules: dict[Path, Path] = {}  # module -> the server that reaches it
    for s in servers:
        todo = [(s.resolve(), 0)]
        while todo:
            f, depth = todo.pop()
            if f in modules or not f.is_relative_to(root) or 'node_modules' in f.parts:
                continue
            modules[f] = s
            if depth >= SERVER_IMPORT_DEPTH:
                continue
            for m in IMPORT.finditer(read(f)):
                if m.group(1).startswith('.'):
                    t = resolve_import(m.group(1), f, root, [])
                    if t is not None:
                        todo.append((t.resolve(), depth + 1))
    routes, api, templates, seen = [], [], [], set()
    inline_total = 0
    for f, server in sorted(modules.items(), key=lambda kv: str(kv[0])):
        rel = f.relative_to(root).as_posix()
        text = read(f)
        if in_test_dir(rel):
            continue
        lines = text.splitlines()
        hits = []
        for rx, det in ((PATH_CHECK, 'node-path-check'), (CASE_PATH, 'node-path-check'), (EXPRESS_ROUTE, 'node-route-call'),
                        (TABLE_KEY, 'node-route-table')):
            for m in rx.finditer(text):
                path = m.group(m.lastindex)
                ln = line_of(text, m.start())
                if COMMENT_LINE.match(lines[ln - 1] if lines else ''):
                    continue
                hits.append((path, ln, det, m))
        for path, ln, det, m in hits:
            if (path, rel) in seen:
                continue
            seen.add((path, rel))
            near = '\n'.join(lines[ln - 1:ln + 4])
            method = METHOD_ON_LINE.search(lines[ln - 1]) if lines else None
            method = method.group(1) if method else (m.group(1).upper() if det == 'node-route-call' and
                                                     m.group(1) not in ('all', 'route') else None)
            entry = {'kind': 'route', 'path': path, 'file': rel, 'line': ln, 'detector': det,
                     'server': server.relative_to(root).as_posix()}
            if method:
                entry['method'] = method
            if path.startswith('/api/') or (det != 'node-route-table' and JSON_RESPONSE.search(near)
                                             and not HTML_IN_TEMPLATE.search(near)):
                api.append(entry)
            elif method in (None, 'GET', 'HEAD'):
                routes.append(entry)
            else:
                api.append(entry)  # a form POST handler: server code, not a page
        if HTML_IN_TEMPLATE.search(text):
            inline = len(INLINE_SCRIPT_TAG.findall(text))
            inline_total += inline
            templates.append({'file': rel, 'inline_scripts': inline, 'script_src': len(SRC_SCRIPT_TAG.findall(text))})
    if not templates and not routes:
        return None
    return {'servers': sorted(s.relative_to(root).as_posix() for s in servers),
            'modules': len(modules), 'routes': routes, 'api_endpoints': api,
            'templates': templates, 'inline_scripts': inline_total}


# ---------------------------------------------------------------- server-rendered Python sites (Flask, Django)

PY_ROUTE_ATTRS = {'route': None, 'get': 'GET', 'post': 'POST', 'put': 'PUT', 'patch': 'PATCH', 'delete': 'DELETE'}
PAGE_CALLS = {'render_template', 'stream_template', 'render_template_string'}
FILE_CALLS = {'send_from_directory', 'send_file', 'send_static_file'}


def _py_routes(root: Path, py: list[Path]) -> list[dict]:
    """Flask routes: through the shared ast reader (py_routes.py) when this skill carries a copy, else a small ast
    reader of @app.route / @bp.route / @app.get(...) with Blueprint(url_prefix=...) prefixes. Each route is
    {method, path, file, line, function}."""
    texts = {f.relative_to(root).as_posix(): read(f) for f in py}
    try:
        from py_routes import scan_python  # noqa: E402  (shared; present when the skill carries a copy)
        return [{k: r.get(k) for k in ('method', 'path', 'file', 'line', 'function')}
                for r in scan_python(texts)['routes'] if r.get('framework') == 'flask']
    except ImportError:
        pass
    import ast
    out = []
    for rel, text in texts.items():
        if 'flask' not in text.lower():
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        prefixes = {}
        for node in ast.walk(tree):   # bp = Blueprint('x', __name__, url_prefix='/y')
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and \
                    getattr(node.value.func, 'id', getattr(node.value.func, 'attr', None)) == 'Blueprint':
                kw = next((k.value for k in node.value.keywords if k.arg == 'url_prefix'), None)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        prefixes[t.id] = kw.value if isinstance(kw, ast.Constant) and isinstance(kw.value, str) else ''
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in PY_ROUTE_ATTRS and isinstance(dec.func.value, ast.Name)):
                    continue
                arg = dec.args[0] if dec.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith('/')):
                    continue
                path = (prefixes.get(dec.func.value.id, '').rstrip('/') + arg.value) or '/'
                methods = [PY_ROUTE_ATTRS[dec.func.attr]] if PY_ROUTE_ATTRS[dec.func.attr] else ['GET']
                mk = next((k.value for k in dec.keywords if k.arg == 'methods'), None)
                if isinstance(mk, (ast.List, ast.Tuple)):
                    methods = [e.value.upper() for e in mk.elts if isinstance(e, ast.Constant)] or methods
                for m in methods:
                    out.append({'method': m, 'path': path, 'file': rel, 'line': dec.lineno, 'function': fn.name})
    return out


def _view_output(root: Path, r: dict, trees: dict) -> tuple[str, str | None]:
    """('page', template) when the view renders a template, ('file', None) when it sends a file, else ('api', None)."""
    import ast
    if r['file'] not in trees:
        try:
            trees[r['file']] = ast.parse(read(root / r['file']))
        except (SyntaxError, ValueError):
            trees[r['file']] = None
    tree = trees[r['file']]
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == r.get('function')] if tree else []
    fn = next((n for n in fns if any(d.lineno == r['line'] for d in n.decorator_list)), fns[0] if len(fns) == 1 else None)
    if fn is None:
        return 'api', None
    sends = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', None))
            if name in PAGE_CALLS:
                a = node.args[0] if node.args else None
                return 'page', a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
            sends |= name in FILE_CALLS
    return ('file' if sends else 'api'), None


def template_server_model(root: Path, files: list[Path], platforms: list[str]) -> dict | None:
    """A Flask or Django site whose pages are server templates (.html under templates/, .jinja, .j2): the
    templates are the UI (their inline scripts run per page, like a static multi-page site), the views that
    render them are the routes (Flask; Django's urls.py is not read), and JSON views are API endpoints. None
    when the repository is not such a site."""
    fw = sorted(set(platforms) & {'flask', 'django'})
    py = [f for f in files if f.suffix == '.py' and not in_test_dir(f.relative_to(root).as_posix())
          and not re.search(r'(^|/)(test_\w+|\w+_test|conftest)\.py$', f.relative_to(root).as_posix())]
    if not fw and any('from flask import' in read(f) or 'import flask' in read(f) for f in py[:400]):
        fw = ['flask']
    tpl_files = [f for f in files if is_template_path(f.relative_to(root).as_posix())
                 and not in_test_dir(f.relative_to(root).as_posix())]
    if not fw or not tpl_files:
        return None
    routes, api = [], []
    rendered: dict[str, list[str]] = {}
    trees: dict = {}
    tpl_rel = {f.relative_to(root).as_posix() for f in tpl_files}

    def tpl_path(name: str, view: str) -> str | None:
        cands = [t for t in tpl_rel if t == f'templates/{name}' or t.endswith(f'/templates/{name}')]
        parts = view.split('/')[:-1]
        for i in range(len(parts), -1, -1):
            want = '/'.join(parts[:i] + ['templates', name])
            if want in cands:
                return want
        return sorted(cands, key=len)[0] if cands else None

    pages: dict[tuple, dict] = {}
    for r in _py_routes(root, py) if 'flask' in fw else []:
        kind, tpl = _view_output(root, r, trees)
        entry = {'kind': 'route', 'path': r['path'], 'file': r['file'], 'line': r['line'], 'detector': 'flask-view',
                 'view': r['function']}
        if kind == 'page':
            key = (r['path'], r['file'], r['line'])
            if key not in pages:
                t = tpl_path(tpl, r['file']) if tpl else None
                if t:
                    entry['template'] = t
                    rendered.setdefault(t, []).append(r['path'])
                elif tpl:
                    entry['template'] = tpl
                    entry['note'] = f'renders {tpl}, which was not found under a templates/ directory'
                else:
                    entry['note'] = 'renders a computed template name: find it by reading the view'
                entry['methods'] = []
                pages[key] = entry
                routes.append(entry)
            pages[key]['methods'].append(r['method'])
        else:
            api.append({**entry, 'method': r['method'], 'detector': 'flask-route',
                        **({'note': 'sends a file'} if kind == 'file' else {})})
    templates = []
    for f in sorted(tpl_files):
        rel = f.relative_to(root).as_posix()
        text = read(f)
        templates.append({'file': rel, 'inline_scripts': sum(1 for m in SCRIPT_TAG.finditer(text)
                                                             if not SRC.search(m.group(1)) and m.group(2).strip()
                                                             and not DATA_TYPES.search(attr(TYPE, m.group(1)) or '')),
                          'script_src': sorted({attr(SRC, m.group(1)) for m in SCRIPT_TAG.finditer(text)
                                                if attr(SRC, m.group(1))})[:20],
                          'rendered_by': sorted(set(rendered.get(rel, [])))})
    return {'frameworks': fw, 'servers': sorted({r['file'] for r in routes + api}), 'routes': routes,
            'api_endpoints': api, 'templates': templates,
            'inline_scripts': sum(t['inline_scripts'] for t in templates),
            **({'note': "Django views are routed in urls.py, which is not read: list the page routes by hand"}
               if fw == ['django'] else {})}


def site_model(root: Path, files: list[Path], platforms: list[str], code: list[Path] | None = None) -> dict:
    """Single-page app, server-rendered framework, or a static multi-page site where each page loads its own scripts."""
    html = [f for f in files if f.suffix.lower() in ('.html', '.htm') and not in_test_dir(f.relative_to(root).as_posix())]
    fw = sorted(set(platforms) & (SPA_PLATFORMS | SSR_PLATFORMS))
    if fw:
        out = {'kind': 'ssr' if set(fw) & SSR_PLATFORMS else 'spa', 'frameworks': fw, 'html_pages': len(html)}
        projs = projects(root, files)
        server = node_server_model(root, [c for c in code or [] if next_path_in_project(
            c.relative_to(root).as_posix(), projs) is None])
        if server:  # a Node server beside the framework app (a monorepo's fixture site): listed, not the site model
            out['server'] = server
        return out
    if set(platforms) & {'android', 'ios', 'flutter'}:
        return {'kind': 'native', 'frameworks': [], 'html_pages': len(html)}
    server = template_server_model(root, files, platforms)
    if server:
        # pages rendered from server templates: each response is a document whose inline and static scripts run on
        # their own, like a static multi-page site. HTML files outside the template directories are listed apart:
        # no view renders them, so they may be stale artefacts rather than served pages.
        tpl = {t['file'] for t in server['templates']}
        other = [f.relative_to(root).as_posix() for f in html if f.relative_to(root).as_posix() not in tpl]
        return {'kind': 'multi_page', 'frameworks': [], 'served_by': server['frameworks'][0], 'server': server,
                'html_pages': len(tpl), 'page_groups': [], 'scripts': [], 'pages': [], 'pages_listed': 0,
                'content_rendered_by_js': [],
                'inline_script_pages': sum(1 for t in server['templates'] if t['inline_scripts']),
                'error_pages': [],
                **({'other_html': other[:30],
                    'other_html_note': 'HTML files outside the template directories: no view renders them. Check '
                                       'whether they are served at all (stale artefacts are not pages).'} if other else {})}
    if 'fastapi' in platforms and len(html) <= 3:
        # an API service with an HTML file or two (a docs page, a template) is not a static site
        return {'kind': 'api', 'frameworks': ['fastapi'], 'html_pages': len(html),
                'note': 'An API service, not a UI: a throw in a handler is an HTTP 500, not a UI crash. Hunt crashes in '
                        'the UI that calls it, and use its OpenAPI document (contracts) as that UI\'s contract.'}
    server = node_server_model(root, code or [])
    if server:
        # server-rendered pages without a framework: each response is a document whose inline scripts run on
        # their own, like a static multi-page site
        return {'kind': 'multi_page', 'frameworks': [], 'served_by': 'node_server', 'server': server,
                'html_pages': len(html), 'page_groups': [], 'scripts': [], 'pages': [], 'pages_listed': 0,
                'content_rendered_by_js': [],
                'inline_script_pages': sum(1 for t in server['templates'] if t['inline_scripts']),
                'error_pages': []}
    if not html:
        return {'kind': 'unknown', 'frameworks': [], 'html_pages': 0}
    pages = [html_page(root, f) for f in html]
    groups: dict[tuple, list[dict]] = {}
    for p in pages:
        key = tuple(sorted(s.get('file') or s['src'] for s in p['scripts']))
        groups.setdefault(key, []).append(p)
    scripts: dict[str, dict] = {}
    for p in pages:
        for s in p['scripts']:
            k = s.get('file') or s['src']
            e = scripts.setdefault(k, {'script': k, 'external': bool(s.get('external')), 'pages': 0,
                                       'renders_content_on': 0, 'missing_on': 0})
            e['pages'] += 1
            e['renders_content_on'] += 1 if p['content_rendered_by_js'] else 0
            e['missing_on'] += 0 if s.get('exists', True) else 1
    for e in scripts.values():  # scripts that inject further scripts (a search box loaded on demand, an ad tag)
        text = '' if e['external'] else read(root / e['script'])
        if INJECT.search(text):
            e['injects'] = sorted({u.split('?')[0] for u in INJECT_SRC.findall(text)})[:10] or ['(computed URL)']
    js_rendered = [p['file'] for p in pages if p['content_rendered_by_js']]
    return {
        'kind': 'multi_page' if len(pages) > 1 else 'single_page_static', 'frameworks': [], 'html_pages': len(pages),
        'page_groups': [{'id': f'PG-{n}', 'scripts': list(k), 'pages': len(v), 'examples': [p['file'] for p in v[:5]],
                         'content_rendered_by_js': sum(1 for p in v if p['content_rendered_by_js'])}
                        for n, (k, v) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1)],
        'scripts': sorted(scripts.values(), key=lambda s: -s['pages']),
        'pages': pages if len(pages) <= PAGE_LIST_MAX else pages[:PAGE_LIST_MAX],
        'pages_listed': min(len(pages), PAGE_LIST_MAX),
        'content_rendered_by_js': js_rendered,
        'inline_script_pages': sum(1 for p in pages if p['inline_scripts']),
        'error_pages': [{'file': p['file'], 'relative_urls': p['error_page_relative_urls']} for p in pages
                        if p.get('error_page_relative_urls')],
    }

def next_path(seg: str) -> str:
    """App Router directory -> URL path: route groups "(x)" and parallel-route slots "@x" are not URL
    segments; intercepting routes "(.)x" keep their target segment."""
    parts = []
    for p in seg.strip('/').split('/'):
        if not p or p.startswith('@') or re.fullmatch(r'\([^.)][^)]*\)', p):
            continue
        parts.append(re.sub(r'^\((\.{1,3}|\.\.\)\(\.\.)\)', '', p))
    return '/' + '/'.join(x for x in parts if x)

def nextjs_model(root: Path, code: list[Path], projs: dict[str, dict]) -> dict:
    """Next.js structure that decides blast radius: layouts (the root layout renders around every page
    and sits outside every error.tsx; only global-error catches it), templates, not-found and loading
    files, middleware/proxy, next/dynamic imports, and route handlers (API code, not UI). File conventions
    are matched on the path inside the Next.js project (the directory of next.config.*, or the package.json
    that depends on next), so a project nested in app/ (`next build app`) is read like one at the root."""
    layouts, special, handlers, dynamic, middleware = [], [], [], [], []
    for f in code:
        full = f.relative_to(root).as_posix()
        rel = next_path_in_project(full, projs)
        text = read(f)
        if rel is None:
            for x in DYNAMIC.finditer(text):
                tail = text[x.end():x.end() + 200]
                ssr = re.match(r'\s*,\s*\{[^}]*\bssr\s*:\s*false', tail)
                dynamic.append({'file': full, 'line': line_of(text, x.start()), 'import': x.group(1), 'ssr': not bool(ssr)})
            continue
        m = NEXT_SPECIAL.match(rel)
        if m:
            seg = re.sub(r'^(src/)?app', '', m['dir'])
            d = DEFAULT_EXPORT.search(text)
            entry = {'kind': m['kind'], 'file': full, 'line': line_of(text, d.start()) if d else 1, 'path': next_path(seg),
                     'client': bool(re.match(r'\s*["\']use client["\']', text))}
            if m['kind'] == 'layout':
                entry['root'] = seg.strip('/') == '' or all(p.startswith('(') for p in seg.strip('/').split('/'))
                layouts.append(entry)
            elif m['kind'] not in ('error', 'global-error'):
                special.append(entry)
        h = NEXT_HANDLER.match(rel)
        if h:
            methods = []
            for x in HTTP_EXPORT.finditer(text):
                if x.group(1):
                    methods.append(x.group(1))
                else:  # export { handler as GET, POST }
                    methods += re.findall(r'(?:\bas\s+|^|,\s*)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b', x.group(2).strip())
            path = next_path(h['seg']) if h['seg'] is not None else '/' + re.sub(r'/index$', '', h['api'])
            handlers.append({'path': path, 'file': full, 'methods': sorted(set(methods)), 'line': 1})
        if re.match(r'^(src/)?(middleware|proxy)\.(ts|js|mts|mjs)$', rel):
            mm = re.search(r'\bmatcher\s*:\s*(\[[^\]]*\]|["\'][^"\']*["\'])', text, re.S)
            middleware.append({'file': full, 'line': line_of(text, mm.start()) if mm else 1,
                               'matcher': ' '.join(mm.group(1).split())[:300] if mm else None,
                               'note': 'Runs before every matched request (pages and API): redirects, auth and rate limits '
                                       'here change what a probe sees, and a throw here fails every matched page.'})
        for x in DYNAMIC.finditer(text):
            tail = text[x.end():x.end() + 200]
            ssr = re.match(r'\s*,\s*\{[^}]*\bssr\s*:\s*false', tail)
            dynamic.append({'file': full, 'line': line_of(text, x.start()), 'import': x.group(1), 'ssr': not bool(ssr)})
    return {'layouts': sorted(layouts, key=lambda l: l['file']), 'special_files': sorted(special, key=lambda s: s['file']),
            'middleware': middleware, 'dynamic_imports': dynamic,
            'route_handlers': sorted(handlers, key=lambda h: h['path']),
            'note': 'Route handlers are API code (a throw is an HTTP 500, not a UI crash) and are not UI routes. '
                    'The root layout renders around every page and is outside every error.tsx: only global-error '
                    'catches a throw there, so it replaces the whole document (effect app_fallback). An error.tsx '
                    'catches its segment\'s page and nested segments, not the layout in its own folder. '
                    'Server components run during SSR: their triggers are server data (seed the database), not '
                    'browser mocks. next/dynamic with ssr:false runs only in the browser, after hydration.'}

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):  # JSON on stdout may hold any character; a cp1252 console can't print it
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--journey-map', type=Path, help='journey_map.json from journey-mapper, for route priorities')
    ap.add_argument('--exclude', action='append', default=[], metavar='DIR',
                    help='repository-relative directory not to walk (repeatable); directories of hundreds of data files '
                         'with no code are skipped without it (skipped_data_dirs)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    EXCLUDE.update(e.replace('\\', '/').strip('/') for e in args.exclude)
    files = list(walk(root))
    # vendored and bundled third-party trees (a driver with its own LICENSE and package.json inside a Go module)
    # are not the app: they never decide the platforms, the site model or the screens
    lang = language_census(root, tool='the crash hunter')   # shared lang_census: counts and excluded trees
    vendored = lang['excluded']
    files = [f for f in files if not under(f.relative_to(root).as_posix(), vendored)]
    note_packages(root, files)  # a test-named directory with its own package manifest is a package, not tests
    platforms, targets = detect_platforms(root, [f for f in files if not in_test_dir(f.relative_to(root).as_posix())])
    code = [f for f in files if f.suffix in CODE_EXT and not TEST_NAME.search(f.name) and not is_bundle(f)
            and not in_test_dir(f.relative_to(root).as_posix())]

    routes, boundaries, data_layer, persisted, contracts, env_config = [], [], {}, [], [], []
    code_set = set(code)
    projs = projects(root, files)
    g = ui_graph(root, files)  # which modules a UI entry point imports, for module_scope_config
    reach = set(g['reachable']) if g['reachable'] is not None else None
    data_rx = {k: re.compile(v) for k, v in DATA_LAYER.items()}
    pers_rx = {k: re.compile(v) for k, v in PERSISTED.items()}
    for f in code:
        rel = f.relative_to(root).as_posix()
        text = read(f)
        if not text:
            continue
        # Next.js file conventions apply only inside a project whose package.json depends on next, and only to
        # app/ and pages/ at its root or under src/: a pages/ folder in a plain React app is not a router
        sub = next_path_in_project(rel, projs)
        if sub is not None and NEXT_FILES.match(sub):
            kind = 'route_file' if re.search(r'(^|/)(global-)?error\.\w+$', sub) else None
            if kind:
                boundaries.append({'framework': 'nextjs', 'kind': kind, 'name': f.name, 'file': rel, 'line': 1,
                                   'scope': 'app' if 'global-error' in f.name else 'route'})
            elif re.search(r'(^|/)page\.\w+$|^(src/)?pages/', sub):
                routes.append({'kind': 'route', 'path': next_path(re.sub(r'^(src/)?(app|pages)/?|/?page\.\w+$|\.\w+$', '', sub)),
                               'file': rel, 'line': 1, 'detector': 'nextjs-file'})
        handler = bool(NEXT_HANDLER.match(sub if sub is not None else rel))
        router_file = bool(ROUTER_FILE.search(text)) and not NOT_ROUTER_FILE.search(text) and not handler
        for det, rx in ROUTE_RX:
            if det == 'route-object' and not router_file:
                continue
            for m in rx.finditer(text):
                if det == 'route-object':
                    lo = text.rfind('{', 0, m.start())
                    ctx = text[max(lo, m.start() - 300):m.end() + 200]
                    if COOKIE_CONTEXT.search(ctx):
                        continue
                routes.append({'kind': 'screen' if det.endswith(('view', 'vc', 'screen')) else 'route', 'path': m.group(1),
                               'file': rel, 'line': line_of(text, m.start()), 'detector': det})
        for fw, kind, rx in BOUNDARY_RX:
            for m in rx.finditer(text):
                ln = line_of(text, m.start())
                name = m.group(1) if kind == 'usage' else None
                if kind == 'definition':
                    before = CLASS_NAME.findall(text[:m.start()])
                    name = before[-1] if before else f.stem
                    if any(b['kind'] == 'definition' and b['file'] == rel and b['name'] == name for b in boundaries):
                        continue
                boundaries.append({'framework': fw, 'kind': kind, 'name': name, 'file': rel, 'line': ln})
        for fw, kind, rx, needs_router in ROUTE_BOUNDARY_RX:
            if needs_router and not router_file:
                continue
            for m in rx.finditer(text):
                boundaries.append({'framework': 'remix' if kind == 'route_module' and 'remix' in platforms else fw,
                                   'kind': kind,
                                   'name': (m.group(1) if m.lastindex else None) or m.group(0).split(':')[0].split('=')[0].strip(' <{'),
                                   'file': rel, 'line': line_of(text, m.start()),
                                   'scope': 'component' if kind == 'hoc' else 'route',
                                   'covers': [ROUTE_BOUNDARY_COVERS[kind]]})
        if f.suffix in ('.js', '.jsx', '.mjs', '.ts', '.tsx') and ENV_READ.search(text) and 'throw' in text:
            throws = module_scope_throws(mask_code(text))
            role = node_role(text, f.name)  # a Node server or CLI script fails its own process, never the UI
            if throws and not role:
                ui_reached = reach is None or rel in reach
                env_config.append({'file': rel, 'line': line_of(text, throws[0]),
                                   'reads': sorted(set(re.findall(r'(?:import\.meta|process)\.env\.(\w+)', text)))[:20],
                                   'reachable_from_ui': ui_reached if reach is not None else None,
                                   'note': ('reads environment configuration and throws at module scope: this runs when the '
                                            'module is imported, before any error boundary renders, so a missing or invalid '
                                            'value blanks the app (B1) wherever the module is imported') if ui_reached else
                                           ('reads environment configuration and throws at module scope, but no UI entry point '
                                            'imports it: it cannot blank the app unless it is loaded in a way the import graph '
                                            'does not follow (a computed import)')})
        for k, rx in data_rx.items():
            n = len(rx.findall(text))
            if n:
                d = data_layer.setdefault(k, {'uses': 0, 'files': []})
                d['uses'] += n
                d['files'].append(rel)
        lines = text.splitlines()
        for k, rx in pers_rx.items():
            for m in rx.finditer(text):
                line = lines[line_of(text, m.start()) - 1] if lines else ''
                if COMMENT_LINE.match(line):  # "a todo list kept in localStorage" in a comment is not a use
                    continue
                persisted.append({'store': k, 'file': rel, 'line': line_of(text, m.start()),
                                  'snippet': line.strip()[:160]})
    for f in files:
        rel = f.relative_to(root).as_posix()
        if f.suffix in ('.graphql', '.gql') or f.name in ('schema.graphql',):
            contracts.append({'kind': 'graphql', 'file': rel})
        elif f.suffix in ('.json', '.yaml', '.yml') and f.stat().st_size < 5_000_000 and 'package' not in f.name:
            head = read(f)[:4000]
            if re.search(r'["\']?(openapi|swagger)["\']?\s*:\s*["\']?\d', head):
                contracts.append({'kind': 'openapi', 'file': rel})
        elif f.suffix == '.prisma':
            contracts.append({'kind': 'prisma', 'file': rel})
        elif f.suffix == '.py' and not in_test_dir(rel) and not TEST_NAME.search(f.name) and \
                not re.search(r'(^|/)test_\w+\.py$', rel):
            text = read(f)
            m = FASTAPI_APP.search(text) if 'FastAPI' in text else None
            if m:
                module = rel[:-3].replace('/', '.')
                contracts.append({
                    'kind': 'openapi-in-code', 'framework': 'fastapi', 'file': rel, 'line': line_of(text, m.start()),
                    'note': 'FastAPI builds its OpenAPI document at runtime. Export it (it imports the app, so it needs the '
                            "API's dependencies and any settings read at import; ask before running it): from the "
                            f'directory that makes {module} importable, python -c "import json, {module} as m; '
                            f'print(json.dumps(m.{m.group(1)}.openapi()))" > <out_dir>/openapi.json, then pass it to '
                            'contract_diff.py --contract'})
        elif f in code_set and f.suffix in ('.ts', '.js', '.mjs') and OPENAPI_CODE.search(read(f)):
            contracts.append({'kind': 'openapi-in-code', 'file': rel,
                              'note': 'an OpenAPI document built in code: export it (run its builder) to use it as a contract'})
    # boundary usages that refer to a defined boundary component, or a known library boundary
    defined = {b['name'] for b in boundaries if b['kind'] == 'definition'}
    used_names = {b['name'] for b in boundaries if b['kind'] == 'usage'}
    for b in boundaries:
        if b['kind'] == 'definition':
            b['used'] = b['name'] in used_names  # a boundary that is defined but never rendered protects nothing
    boundaries = [b for b in boundaries if b['kind'] != 'usage' or b['name'] in defined
                  or b['name'] in ('ErrorBoundary', 'Sentry.ErrorBoundary')]
    for n, b in enumerate(boundaries, 1):
        b['id'] = f'BND-{n}'
    seen = set()
    uniq_routes = []
    for r in routes:
        key = (r['path'], r['file'])
        if key not in seen:
            seen.add(key)
            uniq_routes.append(r)
    site = site_model(root, files, platforms, code)
    if site.get('served_by') in ('flask', 'django'):
        # a server-rendered Python site: the Python is the server and the templates are the UI, so the census's
        # "predominantly Python, not analysed" is not a reason to discard the site model
        lang = server_rendered(lang, [site['served_by']], [t['file'] for t in site['server']['templates']],
                               len(code))
    lang_warning = language_warning(lang)
    embedded = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        if (f.suffix in HOST_EXT and f.suffix not in CODE_EXT or is_template_path(rel)) and not in_test_dir(rel) \
                and not TEST_NAME.search(f.name) and not f.name.endswith('_test.go'):
            for d in embedded_documents(read(f), f.suffix, rel):
                d.pop('_scripts', None)
                embedded.append({'file': rel, **d})
    if incidental(lang):
        # most of the code is in a language no pack reads: a site model from the few analysed files (a bundled
        # driver's test fixture, a build script) would describe incidental code, not the product's UI
        site = {'kind': 'unknown', 'frameworks': [], 'html_pages': site.get('html_pages', 0),
                'note': f"not modelled: {lang_warning}"}
    if embedded:
        site['embedded_templates'] = embedded
    if site.get('server'):  # pages a Node server renders from template literals
        listed = {r['path'] for r in uniq_routes}
        for r in site['server']['routes']:
            if r['path'] not in listed:
                listed.add(r['path'])
                uniq_routes.append(dict(r))
    if args.journey_map and args.journey_map.exists():
        jm = json.loads(args.journey_map.read_text(encoding='utf-8'))
        prio: dict[str, str] = {}
        for j in jm.get('journeys', []):
            ep = j.get('entry_point')
            if ep and (ep not in prio or j.get('priority', 'P3') < prio[ep]):
                prio[ep] = j.get('priority', 'P3')
        for r in uniq_routes:
            if r['path'] in prio:
                r['journey_priority'] = prio[r['path']]
    nextjs = None
    if 'nextjs' in platforms:
        nextjs = nextjs_model(root, code, projs)
        handler_files = {h['file'] for h in nextjs['route_handlers']}
        uniq_routes = [r for r in uniq_routes if r['file'] not in handler_files]
        for lay in nextjs['layouts']:
            if lay['root']:  # the app shell: renders around every page, outside every error.tsx
                uniq_routes.append({'kind': 'shell', 'path': f"(root layout {lay['path']})".replace(' /)', ')'),
                                    'file': lay['file'], 'line': lay['line'], 'detector': 'nextjs-root-layout',
                                    'note': 'Renders around every page; only global-error catches a throw here, and its '
                                            'client components (and their next/dynamic imports) run on every page.'})
        for b in boundaries:
            if b.get('framework') == 'nextjs' and b['kind'] == 'route_file':
                seg = re.sub(r'^(src/)?app|/(global-)?error\.\w+$', '', next_path_in_project(b['file'], projs) or b['file'])
                b['covers'] = (['every page and the root layout (replaces the whole document)'] if b['scope'] == 'app' else
                               [f"pages under {next_path(seg)} and nested segments (not the layout in its own folder)"])
    static = site['kind'] in ('multi_page', 'single_page_static')
    if static:
        platforms = sorted(set(platforms) | {'static-html'})
        listed = {(r['path'], r['file']) for r in uniq_routes}
        if site['html_pages'] <= PAGE_LIST_MAX:
            for p in site['pages']:
                if ('/' + p['file'], p['file']) not in listed:
                    uniq_routes.append({'kind': 'route', 'path': '/' + p['file'], 'file': p['file'], 'line': 1,
                                        'detector': 'html-page', 'scripts': [s.get('file') or s['src'] for s in p['scripts']],
                                        'content_rendered_by_js': p['content_rendered_by_js']})
        else:  # many pages: one entry per set of scripts, since pages that share scripts share failure modes
            for g in site['page_groups']:
                uniq_routes.append({'kind': 'route', 'path': '/' + g['examples'][0], 'file': g['examples'][0], 'line': 1,
                                    'detector': 'html-page-group', 'page_group': g['id'], 'pages': g['pages'],
                                    'scripts': g['scripts'], 'content_rendered_by_js': g['content_rendered_by_js'] > 0})
    for n, r in enumerate(uniq_routes, 1):
        r['id'] = f'RT-{n}'
    if static:
        blast = {'model': 'per_script',
                 'note': 'Static pages: the HTML renders without script, and each page runs its own scripts. An uncaught '
                         'error stops the rest of the script (or event handler) that threw, so it usually breaks that '
                         "script's feature (B3) or only logs (B4); the page stays. It is B1 only where the page's "
                         'content is itself rendered by script (content_rendered_by_js), or the script hides or '
                         'removes the content.'}
        notes = [blast['note']]
        if site['content_rendered_by_js']:
            notes.append(f"Content rendered by script on {len(site['content_rendered_by_js'])} page(s), e.g. "
                         f"{', '.join(site['content_rendered_by_js'][:3])}: an uncaught error before render leaves that page blank (B1).")
        for e in site['error_pages']:
            notes.append(f"{e['file']} is served for any missing URL but loads {len(e['relative_urls'])} asset(s) by relative URL "
                         f"({', '.join(e['relative_urls'][:3])}): at a nested path they resolve under that path and fail.")
        if site.get('served_by') == 'node_server':
            sv = site['server']
            notes.append(f"Pages are rendered by a Node HTTP server ({', '.join(sv['servers'][:3])}) from HTML template "
                         f"literals in {len(sv['templates'])} module(s), with {sv['inline_scripts']} inline <script> "
                         f"block(s) inside the templates: {len({r['path'] for r in sv['routes']})} page route(s) and "
                         f"{len({(r['path'], r.get('method')) for r in sv['api_endpoints']})} "
                         'API or form endpoint(s) listed from pathname checks and route tables. The browser code lives '
                         'inside template strings, which scan_pitfalls.py, strict_overlay.py and ESLint do not read: '
                         'read those scripts yourself, or probe the pages. The server modules themselves are server '
                         'code (a throw there is an HTTP 500), not UI.')
        elif site.get('served_by') in ('flask', 'django'):
            sv = site['server']
            notes.append(f"Pages are rendered by {site['served_by']} from {len(sv['templates'])} server template(s) "
                         f"with {sv['inline_scripts']} inline <script> block(s): {len(sv['routes'])} page route(s) "
                         f"(views that render a template, each with its template) and {len(sv['api_endpoints'])} API or "
                         'file endpoint(s). scan_pitfalls.py scans the inline scripts at their template lines, and the '
                         'static scripts the templates load; read each template for code built from template variables '
                         "({{ ... }} inside a script), which no scanner sees. The Python views are server code: a throw "
                         'there is an HTTP 500 or an error page, not a UI crash.')
            if site.get('other_html'):
                notes.append(f"{len(site['other_html'])} HTML file(s) outside the template directories "
                             f"({', '.join(site['other_html'][:3])}) are rendered by no view: check whether they are "
                             'served before treating a finding in them as live.')
        elif site['inline_script_pages']:
            notes.append(f"{site['inline_script_pages']} page(s) have inline scripts, which scan_pitfalls.py does not read.")
    else:
        native = set(platforms) & {'android', 'ios', 'flutter', 'react-native', 'expo'}
        blast = ({'model': 'per_root', 'note': 'Single render tree: an uncaught render error above every boundary blanks the app (B1).'}
                 if site['kind'] in ('spa', 'ssr') else
                 {'model': 'per_process', 'note': 'An uncaught exception terminates the app (B1) unless code or a handler catches it.'}
                 if native and not incidental(lang) else
                 {'model': 'unknown', 'note': site['note']} if site['kind'] == 'api' or incidental(lang) else
                 {'model': 'unknown', 'note': 'No framework, app platform or HTML pages recognised; decide by reading the code.'})
        notes = ['No error boundary or global handler found: any uncaught render error is app-fatal (B1).'] \
            if not boundaries and site['kind'] != 'api' and code and not incidental(lang) else []
        if site.get('server'):
            sv = site['server']
            notes.append(f"A Node HTTP server ({', '.join(sv['servers'][:3])}) also renders pages from HTML template "
                         f"literals ({len({r['path'] for r in sv['routes']})} route(s), {sv['inline_scripts']} inline "
                         '<script> block(s)): those pages are per_script (an uncaught error stops only its own script), '
                         'and their browser code is inside template strings that the static tools do not read.')
        if any(b['kind'] in ('route_prop', 'route_module') for b in boundaries):
            notes.append('Router error boundaries (errorElement / ErrorBoundary / errorComponent on routes) catch render, '
                         'loader and lazy-import errors in their route and its children: a throw under one is B2 '
                         '(screen_replaced), not B1. Code above the router (providers, the root render, module-scope '
                         'code) is outside them.')
    if embedded:
        n_scripts = sum(e['inline_scripts'] for e in embedded)
        notes.append(f"{len(embedded)} HTML document(s) are held in string constants or template files "
                     f"({', '.join(sorted({e['file'] for e in embedded})[:3])}), with {n_scripts} inline <script> "
                     'block(s): browser code the framework detectors do not see. scan_pitfalls.py scans those inline '
                     'scripts at their real file and line (candidates marked "embedded"); read the rest of the '
                     'template by hand, and probe a rendered page (a generated file can be probed as file://). A page '
                     'whose list is rendered by the server but whose detail is rendered by its script is per_script: a '
                     'throw before the script renders the detail leaves that panel empty (B3 for the panel, the '
                     'server-rendered list stays).')
    for e in env_config:
        if e.get('reachable_from_ui') is False:
            continue
        notes.append(f"{e['file']}:{e['line']} reads environment configuration and throws at module scope: a missing "
                     'or invalid value blanks the app on import, before any boundary renders (B1).')
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'warnings': [lang_warning] if lang_warning else [],
        'language_coverage': lang,
        'skipped_data_dirs': skipped_data_dirs(),
        'platforms': platforms,
        'targets': targets,
        'routes_screens': uniq_routes,
        'boundaries': boundaries,
        'data_layer': [{'library': k, **v} for k, v in sorted(data_layer.items(), key=lambda kv: -kv[1]['uses'])],
        'contracts': contracts,
        'persisted_state': persisted,
        'module_scope_config': env_config,
        'source_files': len(code),
        'site': site,
        **({'nextjs': nextjs, 'api_endpoints': len(nextjs['route_handlers'])} if nextjs else {}),
        'blast_radius': blast,
        'notes': ['Heuristic: confirm which boundary wraps each finding by reading the render tree.'] + notes,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        served = (f", rendered by {site['served_by']} from {len(site['server']['templates'])} server template(s), "
                  f"{len(site['server']['api_endpoints'])} API endpoint(s)"
                  if site.get('served_by') in ('flask', 'django') else
                  f", plus pages a Node server renders from {len(site['server']['templates'])} template module(s)"
                  if site.get('server') else '')
        for s in out['skipped_data_dirs']:
            print(f"  skipped {s['path']}/ ({s['files'] if s['files'] is not None else '?'} files): {s['reason']}")
        print(f"platforms: {', '.join(platforms) or 'unknown'} | site: {site['kind']} ({site['html_pages']} HTML page(s){served}) | "
              f"blast radius: {blast['model']} | routes/screens: {len(uniq_routes)} | boundaries: {len(boundaries)} | "
              f"contracts: {len(contracts)} | persisted-state uses: {len(persisted)} -> {args.out}")
    else:
        print(text)
    for w in out['warnings']:
        print(f'WARN: {w}', file=sys.stderr)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
