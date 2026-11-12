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
documents; OpenAPI documents built in code and Prisma schemas are listed as contracts. Boundary
definitions that nothing renders are marked used: false.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'.git', 'node_modules', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', '.output', 'coverage',
             'vendor', 'Pods', 'Carthage', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__',
             'storybook-static', '.expo', 'venv', '.venv', 'target', '.turbo', '.cache'}
TEST_PART = re.compile(r'(^|/)(tests?|__tests__|__mocks__|e2e|cypress|playwright|androidTest|integration_test|\w*UITests?)(/|$)')
TEST_NAME = re.compile(r'(\.|_)(test|spec|stories)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|\.d\.ts$', re.I)
CODE_EXT = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.svelte', '.kt', '.java', '.swift', '.dart'}
Q = r'["\'`]'

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
NEXT_FILES = re.compile(r'(^|/)(app|src/app)/(.*/)?(page|error|global-error|layout)\.(tsx|jsx|js|ts)$|(^|/)(pages|src/pages)/(?!api/).+\.(tsx|jsx|js|ts)$')
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
                if p.name not in SKIP_DIRS and not (p.name.startswith('.') and p.name not in {'.github', '.maestro'}):
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

def site_model(root: Path, files: list[Path], platforms: list[str]) -> dict:
    """Single-page app, server-rendered framework, or a static multi-page site where each page loads its own scripts."""
    html = [f for f in files if f.suffix.lower() in ('.html', '.htm') and not TEST_PART.search(f.relative_to(root).as_posix())]
    fw = sorted(set(platforms) & (SPA_PLATFORMS | SSR_PLATFORMS))
    if fw:
        return {'kind': 'ssr' if set(fw) & SSR_PLATFORMS else 'spa', 'frameworks': fw, 'html_pages': len(html)}
    if set(platforms) & {'android', 'ios', 'flutter'}:
        return {'kind': 'native', 'frameworks': [], 'html_pages': len(html)}
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

def nextjs_model(root: Path, code: list[Path]) -> dict:
    """Next.js structure that decides blast radius: layouts (the root layout renders around every page
    and sits outside every error.tsx; only global-error catches it), templates, not-found and loading
    files, middleware/proxy, next/dynamic imports, and route handlers (API code, not UI)."""
    layouts, special, handlers, dynamic, middleware = [], [], [], [], []
    for f in code:
        rel = f.relative_to(root).as_posix()
        m = NEXT_SPECIAL.match(rel)
        text = read(f)
        if m:
            seg = re.sub(r'^(src/)?app', '', m['dir'])
            d = DEFAULT_EXPORT.search(text)
            entry = {'kind': m['kind'], 'file': rel, 'line': line_of(text, d.start()) if d else 1, 'path': next_path(seg),
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
            handlers.append({'path': path, 'file': rel, 'methods': sorted(set(methods)), 'line': 1})
        if re.match(r'^(src/)?(middleware|proxy)\.(ts|js|mts|mjs)$', rel):
            mm = re.search(r'\bmatcher\s*:\s*(\[[^\]]*\]|["\'][^"\']*["\'])', text, re.S)
            middleware.append({'file': rel, 'line': line_of(text, mm.start()) if mm else 1,
                               'matcher': ' '.join(mm.group(1).split())[:300] if mm else None,
                               'note': 'Runs before every matched request (pages and API): redirects, auth and rate limits '
                                       'here change what a probe sees, and a throw here fails every matched page.'})
        for x in DYNAMIC.finditer(text):
            tail = text[x.end():x.end() + 200]
            ssr = re.match(r'\s*,\s*\{[^}]*\bssr\s*:\s*false', tail)
            dynamic.append({'file': rel, 'line': line_of(text, x.start()), 'import': x.group(1), 'ssr': not bool(ssr)})
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
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    files = list(walk(root))
    platforms, targets = detect_platforms(root, files)
    code = [f for f in files if f.suffix in CODE_EXT and not TEST_NAME.search(f.name) and not is_bundle(f)
            and not TEST_PART.search(f.relative_to(root).as_posix())]

    routes, boundaries, data_layer, persisted, contracts = [], [], {}, [], []
    code_set = set(code)
    data_rx = {k: re.compile(v) for k, v in DATA_LAYER.items()}
    pers_rx = {k: re.compile(v) for k, v in PERSISTED.items()}
    for f in code:
        rel = f.relative_to(root).as_posix()
        text = read(f)
        if not text:
            continue
        if NEXT_FILES.search(rel):
            kind = 'route_file' if re.search(r'(^|/)(global-)?error\.\w+$', rel) else None
            if kind:
                boundaries.append({'framework': 'nextjs', 'kind': kind, 'name': f.name, 'file': rel, 'line': 1,
                                   'scope': 'app' if 'global-error' in f.name else 'route'})
            elif re.search(r'(^|/)page\.\w+$|(^|/)pages/', rel):
                routes.append({'kind': 'route', 'path': next_path(re.sub(r'^(src/)?(app|pages)/?|/?page\.\w+$|\.\w+$', '', rel)),
                               'file': rel, 'line': 1, 'detector': 'nextjs-file'})
        handler = bool(NEXT_HANDLER.match(rel))
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
        for k, rx in data_rx.items():
            n = len(rx.findall(text))
            if n:
                d = data_layer.setdefault(k, {'uses': 0, 'files': []})
                d['uses'] += n
                d['files'].append(rel)
        for k, rx in pers_rx.items():
            for m in rx.finditer(text):
                persisted.append({'store': k, 'file': rel, 'line': line_of(text, m.start()),
                                  'snippet': text.splitlines()[line_of(text, m.start()) - 1].strip()[:160]})
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
    site = site_model(root, files, platforms)
    nextjs = None
    if 'nextjs' in platforms:
        nextjs = nextjs_model(root, code)
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
                seg = re.sub(r'^(src/)?app|/(global-)?error\.\w+$', '', b['file'])
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
        if site['inline_script_pages']:
            notes.append(f"{site['inline_script_pages']} page(s) have inline scripts, which scan_pitfalls.py does not read.")
    else:
        native = set(platforms) & {'android', 'ios', 'flutter', 'react-native', 'expo'}
        blast = ({'model': 'per_root', 'note': 'Single render tree: an uncaught render error above every boundary blanks the app (B1).'}
                 if site['kind'] in ('spa', 'ssr') else
                 {'model': 'per_process', 'note': 'An uncaught exception terminates the app (B1) unless code or a handler catches it.'}
                 if native else
                 {'model': 'unknown', 'note': 'No framework, app platform or HTML pages recognised; decide by reading the code.'})
        notes = ['No error boundary or global handler found: any uncaught render error is app-fatal (B1).'] if not boundaries else []
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'platforms': platforms,
        'targets': targets,
        'routes_screens': uniq_routes,
        'boundaries': boundaries,
        'data_layer': [{'library': k, **v} for k, v in sorted(data_layer.items(), key=lambda kv: -kv[1]['uses'])],
        'contracts': contracts,
        'persisted_state': persisted,
        'source_files': len(code),
        'site': site,
        **({'nextjs': nextjs, 'api_endpoints': len(nextjs['route_handlers'])} if nextjs else {}),
        'blast_radius': blast,
        'notes': ['Heuristic: confirm which boundary wraps each finding by reading the render tree.'] + notes,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"platforms: {', '.join(platforms) or 'unknown'} | site: {site['kind']} ({site['html_pages']} HTML page(s)) | "
              f"blast radius: {blast['model']} | routes/screens: {len(uniq_routes)} | boundaries: {len(boundaries)} | "
              f"contracts: {len(contracts)} | persisted-state uses: {len(persisted)} -> {args.out}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
