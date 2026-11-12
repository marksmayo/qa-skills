#!/usr/bin/env python3
"""Extract a candidate user-facing surface from a repository: routes, screens, API endpoints,
entry points (deep links) and goal events (analytics), plus context (feature flags, existing
end-to-end tests). Stdlib only; never executes or modifies the repository.

Detection is heuristic. Every item records file, line, detector and confidence so the agent can
verify it. Anything the extractor can't see must be added by the agent with source "agent".

    python extract_surface.py /path/to/repo --out surface.json [--goal-wrapper trackEvent2]
"""
from __future__ import annotations
import argparse, bisect, json, os, re, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from py_routes import scan_python, blank_strings          # noqa: E402
from js_routes import scan_js_routes, Constants           # noqa: E402

SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', 'bower_components', 'dist', 'build', 'out',
             '.next', '.nuxt', '.svelte-kit', '.output', '.vercel', '.turbo', '.cache', '.expo',
             'coverage', 'vendor', 'venv', '.venv', 'env', '__pycache__', '.mypy_cache',
             '.pytest_cache', '.tox', 'Pods', 'Carthage', 'DerivedData', '.gradle', '.idea',
             '.dart_tool', 'target', 'bin', 'obj', 'storybook-static', 'site-packages', 'journeys'}
SOURCE_EXT = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte', '.dart', '.kt',
              '.java', '.swift', '.py', '.rb', '.go', '.cs', '.php', '.xml', '.plist', '.json',
              '.yaml', '.yml', '.storyboard', '.mdx'}
TEST_DIRS = {'test', 'tests', '__tests__', 'e2e', 'cypress', 'playwright', 'spec', 'specs',
             'androidTest', 'integration_test', 'uitests', '.maestro', 'maestro'}
TEST_NAME_RE = re.compile(r'(\.(test|spec|e2e|cy)\.[a-z]+$)|(^test_.*\.py$)|(_test\.(py|go|dart)$)'
                          r'|(Tests?\.(kt|java|swift|cs)$)|(UITests?\.swift$)', re.I)
MAX_BYTES = 1_000_000
PREFIX = {'route': 'R', 'screen': 'S', 'endpoint': 'E', 'entry_point': 'N', 'goal': 'G'}

FRAMEWORK_DEPS = {
    'next': 'nextjs', 'nuxt': 'nuxt', '@sveltejs/kit': 'sveltekit', '@remix-run/react': 'remix',
    'react-router': 'react-router', 'react-router-dom': 'react-router', 'vue-router': 'vue-router',
    '@angular/router': 'angular', 'react-native': 'react-native', 'expo-router': 'expo-router',
    '@react-navigation/native': 'react-navigation', 'express': 'express', 'fastify': 'fastify',
    'koa': 'koa', '@nestjs/core': 'nestjs', '@playwright/test': 'playwright', 'cypress': 'cypress',
    'webdriverio': 'webdriverio', 'detox': 'detox', 'vitest': 'vitest', 'jest': 'jest', 'mocha': 'mocha',
    '@azure/functions': 'azure-functions',
}
UNIT_RUNNERS = {'vitest', 'jest', 'mocha', 'pytest'}
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt', 'go.mod', 'Gemfile',
             'pom.xml', 'build.gradle', 'build.gradle.kts', 'host.json', 'Cargo.toml', 'composer.json')
WEB_FW = {'nextjs', 'nuxt', 'sveltekit', 'remix', 'react-router', 'vue-router', 'angular'}
API_FW = {'express', 'fastify', 'koa', 'nestjs', 'flask', 'fastapi', 'django', 'rails', 'spring',
          'go-http', 'aspnet', 'azure-functions'}

# --- regex detectors over file content ---
Q = r'["\'`]'
RN_SCREEN = re.compile(r'<(\w+)\.Screen\b[^>]*?\bname=\{?' + Q + r'([^"\'`]+)' + Q)
GO_ROUTE = re.compile(r'GoRoute\(\s*(?:name\s*:\s*' + Q + r'[^"\'`]*' + Q + r'\s*,\s*)?path\s*:\s*' + Q + r'([^"\'`]+)' + Q)
DART_ROUTE_MAP = re.compile(Q + r'(/[^"\'`]*)' + Q + r'\s*:\s*\(\s*\w*\s*\)\s*=>')
DART_SCREEN = re.compile(r'class\s+(\w+(?:Screen|Page|View))\s+extends\s+(?:Stateless|Stateful)Widget')
COMPOSE_ROUTE = re.compile(r'\bcomposable\(\s*(?:route\s*=\s*)?"([^"]+)"')
KT_ACTIVITY = re.compile(r'class\s+(\w+(?:Activity|Fragment|Screen))\b[^{]*?:\s*\w*(?:Activity|Fragment)\b')
NAV_XML_DEST = re.compile(r'<(fragment|activity|dialog)\b[^>]*?android:id="@\+id/(\w+)"', re.S)
NAV_XML_DEEPLINK = re.compile(r'<deepLink\b[^>]*?app:uri="([^"]+)"', re.S)
MANIFEST_ACTIVITY = re.compile(r'<activity\b[^>]*?android:name="([^"]+)"', re.S)
MANIFEST_DATA = re.compile(r'<data\b([^>]*)/?>', re.S)
SWIFT_VIEW = re.compile(r'struct\s+(\w+(?:View|Screen|Page))\s*:\s*(?:some\s+)?View\b')
SWIFT_VC = re.compile(r'class\s+(\w+)\s*:\s*UI\w*ViewController\b')
SWIFT_OPEN_URL = re.compile(r'\.onOpenURL\b|func\s+application\([^)]*open\s+url')
STORYBOARD_ID = re.compile(r'storyboardIdentifier="([^"]+)"')
PLIST_SCHEMES = re.compile(r'<key>CFBundleURLSchemes</key>\s*<array>((?:\s*<string>[^<]+</string>)+)', re.S)

JS_ENDPOINT = re.compile(r'\b(?:app|router|server|api|routes?)\.(get|post|put|patch|delete|all)\(\s*' + Q + r'(/[^"\'`]*)' + Q)
NEST_CONTROLLER = re.compile(r'@Controller\(\s*' + Q + r'?([^"\'`)]*)')
NEST_METHOD = re.compile(r'@(Get|Post|Put|Patch|Delete)\(\s*' + Q + r'?([^"\'`)]*)')
FLASK_ROUTE = re.compile(r'@\w+\.route\(\s*["\']([^"\']+)["\'](?:[^)]*methods\s*=\s*\[([^\]]+)\])?')
FASTAPI_ROUTE = re.compile(r'@\w+\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)')
DJANGO_PATH = re.compile(r'\b(?:re_)?path\(\s*r?["\']([^"\']*)["\']')
RAILS_ROUTE = re.compile(r'^\s*(get|post|put|patch|delete)\s+["\']([^"\']+)', re.M)
RAILS_RESOURCES = re.compile(r'^\s*resources?\s+:(\w+)', re.M)
SPRING_MAPPING = re.compile(r'@(Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*"([^"]*)"')
GO_HANDLER = re.compile(r'\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|HandleFunc|Handle)\(\s*"(/[^"]*)"')
ASPNET_ROUTE = re.compile(r'\[(?:Http(Get|Post|Put|Patch|Delete)|Route)\(\s*"([^"]*)"')

NEXT_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
NEXT_METHOD_EXPORT = re.compile(r'^[ \t]*export\s+(?:async\s+)?(?:function\s*\*?\s*|(?:const|let|var)\s+)'
                                r'(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b', re.M)
NEXT_METHOD_GROUP = re.compile(r'^[ \t]*export\s+(?:const\s*)?\{([^}]*)\}', re.M)
JS_ROUTE_CHAIN = re.compile(r'\.route\(\s*' + Q + r'(/[^"\'`]*)' + Q + r'\s*\)')
CHAIN_VERB = re.compile(r'\.(get|post|put|patch|delete|all)\(')
SERVER_ACTION_EXPORT = re.compile(r'^[ \t]*export\s+(?:default\s+)?(?:async\s+function\s+(\w+)|const\s+(\w+)\s*=\s*async\b)', re.M)
INLINE_SERVER_ACTION = re.compile(r'(?:async\s+function\s+(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{|(?:const|let)\s+(\w+)\s*=\s*async\s*'
                                  r'\([^)]*\)\s*(?::[^=]+)?=>\s*\{)\s*["\']use server["\']')
USE_SERVER_FIRST = re.compile(r'\A(?:\s|//[^\n]*\n|/\*.*?\*/)*["\']use server["\']', re.S)
TEST_ONLY_HINT = re.compile(r'(test-support|sentry-example|__tests?__|/e2e\b|/debug\b|/playground\b)', re.I)
LAYOUT_HINT = re.compile(r'redirect\(|notFound\(|getServerSession|\bauth\(\)|useSession\(|getToken\(|currentUser\(|'
                         r'\brole\b|isAdmin|forbidden\(|unauthorized\(')
MATCHER = re.compile(r'\bmatcher\s*:\s*(\[[^\]]*\]|' + Q + r'[^"\'`]*' + Q + ')', re.S)
PATH_LIST = re.compile(r'\b(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*\[([^\]]*)\]', re.S)
PATH_CHECK = re.compile(r'(?:startsWith|===|==|includes|match)\s*\(?\s*' + Q + r'(/[^"\'`]*)' + Q)
STRING_LIT = re.compile(r'"([^"\n]*)"|\'([^\'\n]*)\'|`([^`]*)`')
REDIRECT_CALL = re.compile(r'(?:NextResponse\.redirect|\bredirect)\(')
GUARD_FILE_STEMS = ('middleware', 'proxy')
INTERCEPT = re.compile(r'^(\(\.\.\.\)|\(\.\)|(?:\(\.\.\))+)(.*)$')

GOAL_CALL = re.compile(r'(?:\b|\.)(?:track|trackEvent|logEvent|logCustomEvent|capture|sendEvent|recordEvent)\(\s*'
                       r'(?:\w+\s*,\s*)?' + Q + r'([A-Za-z0-9_ .:/-]{2,80})' + Q)
# Analytics sinks a wrapper may forward its event-name parameter to (group 1 = the variable passed).
SINK_CALLS = [
    re.compile(r'\b(?:mixpanel|analytics|posthog|amplitude|segment|heap|rudderanalytics|Analytics|appInsights|'
               r'datadogRum|umami|fathom|snowplow|ReactGA|gtm|clarity|Sentry\.metrics)\s*(?:\.\w+)*\s*\.\s*'
               r'(?:track|trackEvent|capture|logEvent|event|addAction|trackCustomEvent)\(\s*(\w+)\b(?!\s*[.(\[])'),
    re.compile(r'\bgtag\(\s*["\']event["\']\s*,\s*(\w+)\b(?!\s*[.(\[])'),
    re.compile(r'\bplausible\(\s*(\w+)\b(?!\s*[.(\[])'),
    re.compile(r'\blogEvent\(\s*\w+\s*,\s*(\w+)\b(?!\s*[.(\[])'),
    re.compile(r'\bdataLayer\.push\(\s*\{[^}]*\bevent\s*:\s*(\w+)\b(?!\s*[.(\[])'),
]
FUNC_DEF_JS = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*(?:<[^>()]*>)?\s*\(\s*([A-Za-z_$][\w$]*)'
                         r'|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:function\s*)?'
                         r'\(\s*([A-Za-z_$][\w$]*)')
FUNC_DEF_PY = re.compile(r'^[ \t]*(?:async\s+)?def\s+(\w+)\(\s*(?:self\s*,\s*|cls\s*,\s*)?(\w+)', re.M)
STRING_ARG_CALL = re.compile(r'(?<![\w$.])([A-Za-z_$][\w$]*)\(\s*' + Q + r'([A-Za-z0-9_ .:/-]{2,80})' + Q)
JS_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'function', 'typeof', 'require', 'import'}
GTAG_EVENT = re.compile(r'gtag\(\s*["\']event["\']\s*,\s*["\']([^"\']+)')
FLAG_CALL = re.compile(r'\b(useFlag|useFeatureFlag|useFeatureFlagEnabled|useFeatureIsOn|isFeatureEnabled|'
                       r'featureEnabled|variation|boolVariation|getFeatureValue|getFlag|isEnabled)\(\s*' + Q + r'([\w.:-]{2,80})' + Q)

VISIT_PATTERNS = [
    ('playwright', re.compile(r'page\.goto\(\s*' + Q + r'([^"\'`]+)' + Q)),
    ('cypress', re.compile(r'cy\.visit\(\s*' + Q + r'([^"\'`]+)' + Q)),
    ('webdriverio', re.compile(r'browser\.url\(\s*' + Q + r'([^"\'`]+)' + Q)),
    ('generic', re.compile(r'\.navigateTo\(\s*' + Q + r'([^"\'`]+)' + Q)),
    ('detox', re.compile(r'device\.openURL\(\{\s*url\s*:\s*' + Q + r'([^"\'`]+)' + Q)),
    ('maestro', re.compile(r'^\s*-?\s*openLink\s*:\s*(\S+)', re.M)),
]
TEST_CASE = re.compile(r'\b(?:test|it|scenario)\(\s*' + Q)
PY_TEST_CASE = re.compile(r'^[ \t]*(?:async\s+)?def\s+test_\w*\(', re.M)
SUCCESS_MSG = re.compile(r'(?<![\w$.])(toast\.success|message\.success|notification\.success|notify\.success|showSuccess|'
                         r'setSuccessMessage|setSuccessMsg|enqueueSnackbar|showToast|showNotification)\(\s*'
                         + Q + r'([^"\'`\n]{3,120})' + Q)
AZ_JS_TRIGGER = re.compile(r'\bapp\.(http|timer|storageBlob|storageQueue|serviceBusQueue|serviceBusTopic|eventHub|'
                           r'eventGrid|cosmosDB)\(\s*(["\'])([^"\']+)\2\s*,\s*\{')


def parse_jsonc(text: str):
    """Parse JSON with comments and trailing commas (tsconfig.json). Strings are kept intact, so "@/*" survives."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith('//', i):
            j = text.find('\n', i); i = n if j == -1 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2); i = n if j == -1 else j + 2
        else:
            out.append(c); i += 1
    try:
        return json.loads(re.sub(r',(\s*[}\]])', r'\1', ''.join(out)))
    except ValueError:
        return None


API_PREFIX_SEGMENTS = {'api', 'rest', 'graphql', 'internal', 'public'}

def domain_hint(name: str) -> str | None:
    """Propose a domain from the first static path segment: `GET /api/orders/:id` -> orders, `/` -> home."""
    path = name.split(' ', 1)[1] if re.match(r'^[A-Z|]+ ', name) else name
    if not path.startswith('/'):
        return None
    segs = [s for s in path.split('/') if s and not s.startswith((':', '*', '{', '<', '['))]
    while segs and (segs[0].lower() in API_PREFIX_SEGMENTS or re.fullmatch(r'v\d+', segs[0], re.I)):
        segs = segs[1:]
    if not segs:
        return 'home' if path.strip('/') == '' else None
    slug = re.sub(r'[^a-z0-9]+', '-', segs[0].lower()).strip('-')
    return slug or None


class Extractor:
    def __init__(self, root: Path, max_files: int, goal_wrappers=()):
        self.root = root
        self.max_files = max_files
        self.items: list[dict] = []
        self.flags: list[dict] = []
        self.tests: list[dict] = []
        self.guards: list[dict] = []
        self.layouts: list[dict] = []
        self.aliases: list[dict] = []
        self.configured_wrappers = {w.strip() for w in goal_wrappers if w.strip()}
        self.func_defs: list[dict] = []        # {name, file, line, forwards: set of callees given the 1st param}
        self.string_calls: list[tuple] = []    # (callee, event, path, line) for wrapper resolution at the end
        self.frameworks: set[str] = set()
        self.platforms: set[str] = set()
        self.pkg_cache: dict[Path, set[str]] = {}
        self.files_scanned = 0
        self.truncated = False
        self.py_files: dict[str, str] = {}      # non-test .py files, read with ast after the walk
        self.host_files: dict[str, str] = {}    # Azure Functions host.json files
        self.js_texts: dict[str, str] = {}      # non-test JS/TS files: constants and components for route paths
        self.route_files: dict[str, str] = {}   # files with client-side route configs -> router framework
        self.unit_tests = {'files': 0, 'test_cases': 0}
        self.py_notes: list[str] = []
        self.manifest_dirs: dict[str, bool] = {}

    # ---- helpers ----
    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def add(self, kind, name, path, line, detector, confidence, framework=None, platform=None, **extra):
        name = str(name).strip()
        if not name:
            return
        item = {'kind': kind, 'name': name, 'file': self.rel(path), 'line': line,
                'detector': detector, 'confidence': confidence, 'source': 'extractor'}
        if framework: item['framework'] = framework
        if platform: item['platform'] = platform
        item.update(extra)
        self.items.append(item)

    def deps_for(self, path: Path) -> set[str]:
        """Frameworks declared by the nearest package.json above path."""
        d = path.parent
        while True:
            if d in self.pkg_cache:
                return self.pkg_cache[d]
            pkg = d / 'package.json'
            if pkg.is_file():
                try:
                    data = json.loads(pkg.read_text(encoding='utf-8'))
                    names = {**data.get('dependencies', {}), **data.get('devDependencies', {})}
                    deps = {FRAMEWORK_DEPS[n] for n in names if n in FRAMEWORK_DEPS}
                except (ValueError, OSError, AttributeError):
                    deps = set()
                self.pkg_cache[d] = deps
                return deps
            if d == self.root or d.parent == d:
                self.pkg_cache[d] = set()
                return set()
            d = d.parent

    @staticmethod
    def line_of(offsets, pos):
        return bisect.bisect_right(offsets, pos) + 1

    def walk(self):
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for p in entries:
                if p.is_dir():
                    if p.name not in SKIP_DIRS and not p.name.endswith(('.xcassets', '.framework')):
                        stack.append(p)
                elif p.suffix.lower() in SOURCE_EXT or p.name in ('routes.rb', 'Gemfile', 'go.mod', 'pubspec.yaml') \
                        or p.name.startswith(('build.gradle', 'requirements')) or p.suffix in ('.csproj', '.toml'):
                    if self.files_scanned >= self.max_files:
                        self.truncated = True
                        return
                    self.files_scanned += 1
                    yield p

    @staticmethod
    def is_test(rel_parts, name):
        return bool(TEST_NAME_RE.search(name)) or any(part.lower() in {t.lower() for t in TEST_DIRS}
                                                      for part in rel_parts[:-1])

    # ---- file-based routing ----
    @staticmethod
    def intercepts(segs) -> bool:
        return any(INTERCEPT.match(s) for s in segs)

    @staticmethod
    def segments_to_route(segs):
        """Route path for file-system segments. Route groups `(x)` and slots `@x` are dropped; an
        intercepting segment `(.)x`, `(..)x`, `(..)(..)x` or `(...)x` resolves to the route it intercepts."""
        parts = []
        for s in segs:
            m = INTERCEPT.match(s or '')
            if m:
                marker, s = m.group(1), m.group(2)
                if marker == '(...)':
                    parts = []
                elif marker != '(.)':
                    up = marker.count('(..)')
                    parts = parts[:-up] if up <= len(parts) else []
            if not s or (s.startswith('(') and s.endswith(')')) or s.startswith('@'):
                continue
            if s.startswith('_') and s != '_index':
                return None  # private / pathless folders are not routable
            m = re.fullmatch(r'\[\[?\.\.\.(\w+)\]\]?', s)
            if m: parts.append('*' + m.group(1)); continue
            m = re.fullmatch(r'\[(\w+)\]', s)
            if m: parts.append(':' + m.group(1)); continue
            parts.append(s)
        return '/' + '/'.join(parts)

    def next_methods(self, text: str) -> list[tuple[str, int]]:
        """HTTP methods a Next.js route handler exports: `export async function GET`, `export const POST =`,
        `export { handler as GET, handler as POST }`, `export const { GET, POST } = handlers`."""
        offsets = [i for i, c in enumerate(text) if c == '\n']
        found: dict[str, int] = {}
        for m in NEXT_METHOD_EXPORT.finditer(text):
            found.setdefault(m.group(1), self.line_of(offsets, m.start(1)))
        for m in NEXT_METHOD_GROUP.finditer(text):
            for part in m.group(1).split(','):
                name = part.strip().split(' as ')[-1].strip().split(':')[-1].strip()
                if name in NEXT_METHODS:
                    found.setdefault(name, self.line_of(offsets, m.start()))
        return sorted(found.items(), key=lambda kv: (kv[1], NEXT_METHODS.index(kv[0])))

    def scan_guard(self, path: Path, text: str):
        """Summarise a Next.js middleware.ts / proxy.ts (Next 16): matchers, path prefix lists, path checks and
        redirects, so the agent can trace guards instead of finding them by hand."""
        offsets = [i for i, c in enumerate(text) if c == '\n']
        L = lambda pos: self.line_of(offsets, pos)
        strings = lambda chunk: [next(g for g in sm.groups() if g is not None) for sm in STRING_LIT.finditer(chunk)]
        matchers = []
        for m in MATCHER.finditer(text):
            for sm in STRING_LIT.finditer(m.group(1)):
                matchers.append({'value': next(g for g in sm.groups() if g is not None),
                                 'line': L(m.start(1) + sm.start())})
        lists = []
        for m in PATH_LIST.finditer(text):
            values = [v for v in strings(m.group(2)) if v.startswith('/')]
            if values:
                lists.append({'name': m.group(1), 'line': L(m.start()), 'values': values})
        checks = [{'value': m.group(1), 'line': L(m.start())} for m in PATH_CHECK.finditer(text)]
        redirects = []
        for m in REDIRECT_CALL.finditer(text):
            line_start = text.rfind('\n', 0, m.start()) + 1
            redirects.append({'line': L(m.start()), 'text': text[line_start:].split('\n', 1)[0].strip()[:160]})
        self.guards.append({'file': self.rel(path), 'kind': 'next-' + path.stem, 'matchers': matchers,
                            'path_lists': lists, 'path_checks': checks, 'redirects': redirects,
                            'note': 'No matcher means the file runs on every request.' if not matchers else
                                    'Matchers decide which requests run this file; path lists and checks decide what it does.'})

    def scan_tsconfig(self, path: Path, text: str):
        data = parse_jsonc(text)
        if not isinstance(data, dict):
            return
        opts = data.get('compilerOptions') or {}
        if not isinstance(opts, dict) or not (opts.get('paths') or opts.get('baseUrl')):
            if data.get('extends'):
                self.aliases.append({'config': self.rel(path), 'extends': data['extends'], 'paths': {},
                                     'note': 'paths may be inherited from the extended config, which is not followed'})
            return
        entry = {'config': self.rel(path), 'paths': opts.get('paths') or {}}
        if opts.get('baseUrl'):
            entry['base_url'] = opts['baseUrl']
        if data.get('extends'):
            entry['extends'] = data['extends']
        self.aliases.append(entry)

    def is_guard_file(self, path: Path, deps: set[str]) -> bool:
        if path.stem not in GUARD_FILE_STEMS or path.suffix not in ('.ts', '.js', '.mjs', '.tsx') or 'nextjs' not in deps:
            return False
        d = path.parent
        return (d / 'package.json').is_file() or (d.name == 'src' and (d.parent / 'package.json').is_file())

    def collect_wrappers(self, path: Path, text: str, offsets):
        """Record function definitions that forward their first parameter to an analytics call or to another
        function, and every call with a string first argument; resolved into goals in resolve_wrappers()."""
        is_py = path.suffix == '.py'
        defs = list((FUNC_DEF_PY if is_py else FUNC_DEF_JS).finditer(text))
        for i, m in enumerate(defs):
            name = m.group(1) or m.group(3)
            param = m.group(2) or m.group(4)
            if not name or not param or name in JS_KEYWORDS:
                continue
            end = defs[i + 1].start() if i + 1 < len(defs) else len(text)
            body = text[m.end(): min(end, m.end() + 4000)]
            forwards = set()
            for rx in SINK_CALLS:
                if any(sm.group(1) == param for sm in rx.finditer(body)):
                    forwards.add('<sink>')
            for cm in re.finditer(r'(?<![\w$])([A-Za-z_$][\w$]*)\(\s*' + re.escape(param) + r'\b(?!\s*[.(\[])', body):
                if cm.group(1) != name:
                    forwards.add(cm.group(1))
            if forwards:
                self.func_defs.append({'name': name, 'file': self.rel(path),
                                       'line': self.line_of(offsets, m.start()), 'forwards': forwards})
        for m in STRING_ARG_CALL.finditer(text):
            if m.group(1) not in JS_KEYWORDS:
                self.string_calls.append((m.group(1), m.group(2), path, self.line_of(offsets, m.start())))

    def resolve_wrappers(self) -> list[dict]:
        wrappers: dict[str, dict] = {w: {'name': w, 'source': 'configured'} for w in self.configured_wrappers}
        changed = True
        while changed:   # a wrapper of a wrapper is also a wrapper
            changed = False
            for f in self.func_defs:
                if f['name'] in wrappers:
                    continue
                via = '<sink>' if '<sink>' in f['forwards'] else next((c for c in sorted(f['forwards']) if c in wrappers), None)
                if via:
                    wrappers[f['name']] = {'name': f['name'], 'file': f['file'], 'line': f['line'], 'source': 'detected',
                                           'forwards_to': 'an analytics call' if via == '<sink>' else via}
                    changed = True
        for callee, event, path, line in self.string_calls:
            if callee in wrappers:
                self.add('goal', event, path, line, 'analytics-wrapper', 'medium',
                         note=f'event passed to tracking wrapper {callee}()')
        return sorted(wrappers.values(), key=lambda w: w['name'])

    def file_route(self, path: Path, rel_parts, deps, text: str = ''):
        name, stem, ext = path.name, path.stem, path.suffix
        lower = [p for p in rel_parts[:-1]]

        def after(dirname):
            idx = max((i for i, p in enumerate(lower) if p == dirname), default=None)
            return None if idx is None else lower[idx + 1:]

        if 'sveltekit' in deps and (segs := after('routes')) is not None:
            if name.startswith('+page.'):
                self.add('route', self.segments_to_route(segs), path, 1, 'sveltekit-file', 'high', 'sveltekit', 'web')
            elif name.startswith('+server.'):
                self.add('endpoint', self.segments_to_route(segs), path, 1, 'sveltekit-file', 'high', 'sveltekit', 'api')
            return
        if 'nextjs' in deps and ext in ('.tsx', '.ts', '.jsx', '.js', '.mdx'):
            if stem == 'page' and (segs := after('app')) is not None:
                r = self.segments_to_route(segs)
                if r and self.intercepts(segs):
                    self.add('route', r, path, 1, 'next-intercepting-route', 'medium', 'nextjs', 'web',
                             note=f'intercepting route: on client-side navigation it renders {r} inside the current '
                                  f'layout (often a modal); a full page load shows the target page instead')
                elif r:
                    self.add('route', r, path, 1, 'next-app-router', 'high', 'nextjs', 'web')
                return
            if stem == 'route' and (segs := after('app')) is not None:
                r = self.segments_to_route(segs)
                if r:
                    methods = self.next_methods(text)
                    for verb, line in methods:
                        self.add('endpoint', f'{verb} {r}', path, line, 'next-route-handler', 'high', 'nextjs', 'api')
                    if not methods:
                        self.add('endpoint', f'ANY {r}', path, 1, 'next-route-handler', 'medium', 'nextjs', 'api',
                                 note='no exported GET/POST/... handler found; check how the handlers are exported')
                return
            if stem == 'layout' and (segs := after('app')) is not None:
                r = self.segments_to_route(segs)
                if r is not None:
                    offsets = [i for i, c in enumerate(text) if c == '\n']
                    hints = [{'line': self.line_of(offsets, m.start()),
                              'text': text[text.rfind('\n', 0, m.start()) + 1:].split('\n', 1)[0].strip()[:160]}
                             for m in LAYOUT_HINT.finditer(text)]
                    seen = set()
                    hints = [h for h in hints if not (h['line'] in seen or seen.add(h['line']))][:12]
                    self.layouts.append({'route': r, 'file': self.rel(path), 'guard_hints': hints})
                return
            if (segs := after('pages')) is not None and not stem.startswith('_'):
                r = self.segments_to_route(segs + ([] if stem == 'index' else [stem]))
                if r:
                    is_api = segs[:1] == ['api']
                    self.add('endpoint' if is_api else 'route', r, path, 1, 'next-pages-router', 'high',
                             'nextjs', 'api' if is_api else 'web')
                return
        if 'nuxt' in deps and ext == '.vue' and (segs := after('pages')) is not None:
            r = self.segments_to_route(segs + ([] if stem == 'index' else [stem]))
            if r: self.add('route', r, path, 1, 'nuxt-pages', 'high', 'nuxt', 'web')
            return
        if 'remix' in deps and ext in ('.tsx', '.jsx', '.ts', '.js') and (segs := after('routes')) is not None:
            base = segs[0] if segs and stem == 'route' else stem
            parts = []
            for s in base.split('.'):
                if s == '_index': continue
                if s.startswith('_'): continue
                s = s.rstrip('_')
                parts.append(':' + s[1:] if s.startswith('$') and len(s) > 1 else ('*' if s == '$' else s))
            self.add('route', '/' + '/'.join(parts), path, 1, 'remix-flat-routes', 'medium', 'remix', 'web')
            return
        if 'expo-router' in deps and ext in ('.tsx', '.jsx', '.ts', '.js') and (segs := after('app')) is not None:
            if stem.startswith(('_', '+')):
                return
            r = self.segments_to_route(segs + ([] if stem == 'index' else [stem]))
            if r: self.add('screen', r, path, 1, 'expo-router-file', 'high', 'expo-router', 'mobile')

    # ---- content detectors ----
    def scan_content(self, path: Path, text: str, deps: set[str]):
        offsets = [i for i, c in enumerate(text) if c == '\n']
        L = lambda m: self.line_of(offsets, m.start())
        ext, name = path.suffix.lower(), path.name

        if ext in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue'):
            self.js_texts[self.rel(path)] = text
            fw = ('react-router' if 'react-router' in text else 'vue-router' if 'vue-router' in text
                  else 'angular' if '@angular/router' in text else None)
            if fw:
                self.route_files[self.rel(path)] = fw   # read by js_routes after the walk (paths need every file)
            if '@azure/functions' in text:
                self.scan_azure_js(path, text, offsets)
            for m in SUCCESS_MSG.finditer(text):
                if m.group(1) in ('enqueueSnackbar', 'showToast', 'showNotification') and \
                        'success' not in text[m.end(): m.end() + 120].lower():
                    continue
                self.add('goal', m.group(2), path, L(m), 'success-message', 'low',
                         note=f'success message shown with {m.group(1)}(); a candidate goal: confirm it marks completion')
            if '@react-navigation' in text:
                for m in RN_SCREEN.finditer(text):
                    self.add('screen', m.group(2), path, L(m), 'react-navigation', 'high', 'react-navigation', 'mobile')
            for m in JS_ENDPOINT.finditer(text):
                self.add('endpoint', f'{m.group(1).upper()} {m.group(2)}', path, L(m), 'js-router-call', 'medium',
                         None, 'api')
            for m in JS_ROUTE_CHAIN.finditer(text):   # router.route('/x').get(...).post(...)
                stop = text.find(';', m.end())
                chunk = text[m.end(): stop if stop != -1 else m.end() + 2000]
                depth, verbs = 0, []
                for i, ch in enumerate(chunk):   # only verbs chained at the top level, not calls inside handlers
                    if ch in '([{': depth += 1
                    elif ch in ')]}': depth -= 1
                    elif ch == '.' and depth == 0 and (vm := CHAIN_VERB.match(chunk, i)):
                        verbs.append((vm.group(1), m.end() + i))
                for verb, pos in verbs:
                    self.add('endpoint', f'{verb.upper()} {m.group(1)}', path, self.line_of(offsets, pos),
                             'js-route-chain', 'medium', None, 'api')
            if 'nextjs' in deps and 'use server' in text:
                if USE_SERVER_FIRST.match(text):
                    for m in SERVER_ACTION_EXPORT.finditer(text):
                        self.add('endpoint', f'ACTION {m.group(1) or m.group(2)}', path, L(m), 'next-server-action',
                                 'medium', 'nextjs', 'api', note='server action module ("use server" file)')
                for m in INLINE_SERVER_ACTION.finditer(text):
                    self.add('endpoint', f'ACTION {m.group(1) or m.group(2)}', path, L(m), 'next-server-action',
                             'medium', 'nextjs', 'api', note='inline server action')
            if '@Controller' in text:
                c = NEST_CONTROLLER.search(text)
                prefix = '/' + c.group(1).strip('/') if c and c.group(1) else ''
                for m in NEST_METHOD.finditer(text):
                    sub = '/' + m.group(2).strip('/') if m.group(2) else ''
                    self.add('endpoint', f'{m.group(1).upper()} {prefix + sub or "/"}', path, L(m), 'nestjs', 'high',
                             'nestjs', 'api')
        elif ext == '.dart':
            if 'GoRoute' in text:
                for m in GO_ROUTE.finditer(text):
                    self.add('route', m.group(1), path, L(m), 'go_router', 'high', 'flutter', 'mobile')
            for m in DART_ROUTE_MAP.finditer(text):
                self.add('route', m.group(1), path, L(m), 'flutter-routes-map', 'medium', 'flutter', 'mobile')
            for m in DART_SCREEN.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'flutter-widget-name', 'low', 'flutter', 'mobile')
        elif ext in ('.kt', '.java'):
            for m in COMPOSE_ROUTE.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'compose-navigation', 'high', 'android', 'android')
            for m in KT_ACTIVITY.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'android-class-name', 'low', 'android', 'android')
            for m in SPRING_MAPPING.finditer(text):
                verb = 'ANY' if m.group(1) == 'Request' else m.group(1).upper()
                self.add('endpoint', f'{verb} {m.group(2) or "/"}', path, L(m), 'spring-mapping', 'medium',
                         'spring', 'api', note='class-level @RequestMapping prefix not applied')
        elif ext == '.swift':
            for m in SWIFT_VIEW.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'swiftui-view-name', 'low', 'swiftui', 'ios')
            for m in SWIFT_VC.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'uikit-view-controller', 'medium', 'uikit', 'ios')
            for m in SWIFT_OPEN_URL.finditer(text):
                self.add('entry_point', f'URL handler in {path.stem}', path, L(m), 'ios-open-url', 'medium', None, 'ios')
        elif ext == '.storyboard':
            for m in STORYBOARD_ID.finditer(text):
                self.add('screen', m.group(1), path, L(m), 'storyboard-id', 'medium', 'uikit', 'ios')
        elif ext == '.plist':
            for m in PLIST_SCHEMES.finditer(text):
                for s in re.findall(r'<string>([^<]+)</string>', m.group(1)):
                    self.add('entry_point', f'{s}://', path, L(m), 'ios-url-scheme', 'high', None, 'ios')
        elif ext == '.xml':
            if '/res/navigation/' in '/' + self.rel(path):
                for m in NAV_XML_DEST.finditer(text):
                    self.add('screen', m.group(2), path, L(m), 'android-nav-graph', 'high', 'android', 'android')
                for m in NAV_XML_DEEPLINK.finditer(text):
                    self.add('entry_point', m.group(1), path, L(m), 'android-nav-deeplink', 'high', None, 'android')
            if name == 'AndroidManifest.xml':
                for m in MANIFEST_ACTIVITY.finditer(text):
                    self.add('screen', m.group(1), path, L(m), 'android-manifest-activity', 'medium', 'android', 'android')
                for m in MANIFEST_DATA.finditer(text):
                    attrs = dict(re.findall(r'android:(\w+)="([^"]*)"', m.group(1)))
                    if 'scheme' in attrs:
                        uri = f"{attrs['scheme']}://{attrs.get('host', '')}{attrs.get('pathPrefix', attrs.get('path', ''))}"
                        self.add('entry_point', uri, path, L(m), 'android-intent-filter', 'high', None, 'android')
        elif ext == '.py':
            self.py_files[self.rel(path)] = text   # Flask, FastAPI, Azure Functions and CLIs: read with ast after the walk
            if name == 'urls.py':
                for m in DJANGO_PATH.finditer(text):
                    self.add('route', '/' + m.group(1).lstrip('^').rstrip('$'), path, L(m), 'django-urls', 'medium',
                             'django', 'web')
        elif name == 'routes.rb':
            for m in RAILS_ROUTE.finditer(text):
                self.add('endpoint', f'{m.group(1).upper()} {m.group(2)}', path, L(m), 'rails-routes', 'high', 'rails', 'api')
            for m in RAILS_RESOURCES.finditer(text):
                self.add('endpoint', f'RESOURCES /{m.group(1)}', path, L(m), 'rails-resources', 'medium', 'rails', 'api')
        elif ext == '.go':
            for m in GO_HANDLER.finditer(text):
                verb = m.group(1).upper() if m.group(1).upper() in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') else 'ANY'
                self.add('endpoint', f'{verb} {m.group(2)}', path, L(m), 'go-handler', 'medium', 'go-http', 'api')
        elif ext == '.cs':
            for m in ASPNET_ROUTE.finditer(text):
                self.add('endpoint', f'{(m.group(1) or "ANY").upper()} {m.group(2)}', path, L(m), 'aspnet-attribute',
                         'medium', 'aspnet', 'api')

        if ext in ('.json', '.yaml', '.yml') and re.match(r'(openapi|swagger)', name, re.I):
            self.scan_openapi(path, text, offsets)
        if name == 'host.json':
            self.host_files[self.rel(path)] = text
        elif name == 'function.json':
            self.scan_function_json(path, text)
        elif name == 'pyproject.toml':
            self.scan_pyproject_scripts(path, text)
        elif name == 'package.json':
            self.scan_package_bin(path, text)

        if ext in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.vue', '.svelte', '.dart', '.kt', '.java', '.swift', '.py'):
            for m in GOAL_CALL.finditer(text):
                self.add('goal', m.group(1), path, L(m), 'analytics-call', 'medium')
            for m in GTAG_EVENT.finditer(text):
                self.add('goal', m.group(1), path, L(m), 'gtag-event', 'medium')
            if ext in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.vue', '.svelte', '.py'):
                self.collect_wrappers(path, text, offsets)
            for m in FLAG_CALL.finditer(text):
                self.flags.append({'key': m.group(2), 'call': m.group(1), 'file': self.rel(path), 'line': L(m)})

    # ---- functions, command lines and the passes that need every file ----
    def host_prefix(self, path: Path) -> str:
        """Azure Functions HTTP route prefix from the nearest host.json (default `api`)."""
        d = path.parent
        while True:
            hj = d / 'host.json'
            if hj.is_file():
                try:
                    data = json.loads(hj.read_text(encoding='utf-8'))
                    p = ((data.get('extensions') or {}).get('http') or {}).get('routePrefix', 'api')
                    return p if isinstance(p, str) else 'api'
                except (ValueError, OSError, AttributeError):
                    return 'api'
            if d == self.root or d.parent == d:
                return 'api'
            d = d.parent

    def add_trigger(self, trigger: str, function: str, detail: str | None, path: Path, line: int, framework: str):
        self.add('entry_point', f'{trigger} trigger: {detail or function}', path, line, f'{framework}-trigger', 'high',
                 framework, 'api', note=f'function {function} runs when this {trigger} event happens; not user-facing '
                                        f'by itself: find the user action that causes it')

    def scan_function_json(self, path: Path, text: str):
        """Azure Functions v1 programming model: <function>/function.json bindings."""
        try:
            bindings = json.loads(text).get('bindings') or []
        except (ValueError, AttributeError):
            return
        fn = path.parent.name
        for b in bindings:
            t = str(b.get('type', ''))
            if not t.endswith('Trigger') or b.get('direction', 'in') != 'in':
                continue
            if t == 'httpTrigger':
                route = '/' + '/'.join(s for s in (self.host_prefix(path), b.get('route') or fn) if s)
                for verb in [m.upper() for m in b.get('methods') or []] or ['ANY']:
                    self.add('endpoint', f'{verb} {route}', path, 1, 'azure-functions-http', 'high', 'azure-functions', 'api')
            else:
                detail = b.get('path') or b.get('schedule') or b.get('queueName') or b.get('topicName') or b.get('eventHubName')
                self.add_trigger(t[:-len('Trigger')], fn, detail, path, 1, 'azure-functions')

    def scan_azure_js(self, path: Path, text: str, offsets):
        """Azure Functions v4 Node model: app.http('name', { route, methods }), app.timer(...), app.storageBlob(...)."""
        for m in AZ_JS_TRIGGER.finditer(text):
            kind, fn = m.group(1), m.group(3)
            opts = text[m.end(): m.end() + 800].split('handler', 1)[0]
            line = self.line_of(offsets, m.start())
            if kind == 'http':
                rm = re.search(r'\broute\s*:\s*' + Q + r'([^"\'`]+)' + Q, opts)
                methods = re.findall(r'["\'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["\']', opts.split(']', 1)[0]) \
                    if re.search(r'\bmethods\s*:', opts) else []
                route = '/' + '/'.join(s for s in (self.host_prefix(path), rm.group(1) if rm else fn) if s)
                for verb in methods or ['ANY']:
                    self.add('endpoint', f'{verb} {route}', path, line, 'azure-functions-http', 'high', 'azure-functions', 'api')
            else:
                dm = re.search(r'\b(?:path|schedule|queueName|topicName|eventHubName)\s*:\s*' + Q + r'([^"\'`]+)' + Q, opts)
                self.add_trigger(kind, fn, dm.group(1) if dm else None, path, line, 'azure-functions')

    def scan_pyproject_scripts(self, path: Path, text: str):
        """[project.scripts] and [tool.poetry.scripts] console commands."""
        section = None
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith('['):
                section = s.strip('[] ')
                continue
            if section in ('project.scripts', 'tool.poetry.scripts') and (m := re.match(r'["\']?([\w.-]+)["\']?\s*=', s)):
                self.add('entry_point', f'cli: {m.group(1)}', path, n, 'console-script', 'high', None, 'cli')

    def scan_package_bin(self, path: Path, text: str):
        try:
            data = json.loads(text)
        except ValueError:
            return
        b = data.get('bin') if isinstance(data, dict) else None
        names = list(b) if isinstance(b, dict) else [data.get('name')] if isinstance(b, str) and data.get('name') else []
        for name in names:
            m = re.search(r'"' + re.escape(str(name)) + r'"', text) if isinstance(b, dict) else re.search(r'"bin"', text)
            line = text.count('\n', 0, m.start()) + 1 if m else 1
            self.add('entry_point', f'cli: {name}', path, line, 'package-bin', 'high', None, 'cli')

    def scan_python_project(self):
        """Flask, FastAPI and Azure Functions routes (with router and blueprint prefixes), function triggers
        and command lines, read with ast so decorators inside docstrings never count."""
        res = scan_python(self.py_files, self.host_files)
        for r in res['routes']:
            path = self.root / r['file']
            note = None
            if r['mount'] == 'unmounted':
                note = ('no include_router/register_blueprint call was found for this router; a mount prefix may be '
                        'missing from the path')
            elif r['mount'] == 'dynamic':
                note = 'part of the path or its prefix is computed; check the full path by hand'
            self.add('endpoint', f"{r['method']} {r['path']}", path, r['line'], f"{r['framework']}-route",
                     'high' if r['mount'] == 'resolved' else 'medium', r['framework'], 'api',
                     **({'note': note} if note else {}))
            self.frameworks.add(r['framework'])
        for t in res['triggers']:
            detail = t.get('path') or t.get('schedule') or t.get('queue_name') or t.get('topic_name') or t.get('event_hub_name')
            self.add_trigger(t['trigger'], t['function'], detail, self.root / t['file'], t['line'], t['framework'])
            self.frameworks.add(t['framework'])
        for c in res['clis']:
            self.add('entry_point', f"cli: {c['command']}", self.root / c['file'], c['line'], f"{c['framework']}-command",
                     'high', None, 'cli')
        for d in res['dynamic_includes']:
            self.py_notes.append(f"{d['file']}:{d['line']}: a router is included through a variable or loop "
                                 f"({d['text']}); routes of routers marked unmounted may sit under its prefix")
        for rel in res['unparsed']:   # can't parse: fall back to text matching, with docstrings blanked
            text = blank_strings(self.py_files[rel])
            path = self.root / rel
            offsets = [i for i, c in enumerate(text) if c == '\n']
            for m in FLASK_ROUTE.finditer(text):
                verbs = re.findall(r'\w+', m.group(2) or 'GET')
                self.add('endpoint', f'{"|".join(v.upper() for v in verbs)} {m.group(1)}', path,
                         self.line_of(offsets, m.start()), 'flask-route-text', 'low', 'flask', 'api',
                         note='file did not parse; read as text, so blueprint prefixes are not applied')
            for m in FASTAPI_ROUTE.finditer(text):
                self.add('endpoint', f'{m.group(1).upper()} {m.group(2)}', path, self.line_of(offsets, m.start()),
                         'fastapi-route-text', 'low', 'fastapi', 'api',
                         note='file did not parse; read as text, so router prefixes are not applied')

    def scan_client_routes(self):
        consts = Constants()
        for rel, text in self.js_texts.items():
            consts.collect(rel, text)
        for r in scan_js_routes(self.js_texts, self.route_files, consts):
            extra = {'note': r['note']} if r.get('note') else {}
            if r['index']:
                extra['index'] = True
            self.add('route', r['name'], self.root / r['file'], r['line'], r['detector'], r['confidence'],
                     r['framework'], 'web', **extra)

    def app_root(self, rel: str) -> str:
        """The nearest directory above a file that holds a project manifest ('' for the repository root)."""
        parts = Path(rel).parts[:-1]
        for i in range(len(parts), 0, -1):
            d = '/'.join(parts[:i])
            if d not in self.manifest_dirs:
                try:
                    names = set(os.listdir(self.root / d))
                except OSError:
                    names = set()
                self.manifest_dirs[d] = bool(names & set(MANIFESTS)) or any(n.endswith('.csproj') for n in names)
            if self.manifest_dirs[d]:
                return d
        return ''

    def service_labels(self, items: list[dict]) -> dict[str, str]:
        """app root -> short label, when the items come from more than one app (multi-service monorepos)."""
        roots = sorted({self.app_root(it['file']) for it in items})
        if len(roots) < 2:
            return {}
        split = [r.split('/') if r else [] for r in roots]
        lead = 0
        while all(len(s) > lead for s in split) and len({s[lead] for s in split}) == 1:
            lead += 1
        trail = 0
        while all(len(s) > lead + trail for s in split) and len({s[-1 - trail] for s in split}) == 1:
            trail += 1
        out = {}
        for r, s in zip(roots, split):
            core = s[lead:len(s) - trail] if trail else s[lead:]
            out[r] = '-'.join(core) or (s[-1] if s else 'root')
        return out

    def scan_openapi(self, path, text, offsets):
        if path.suffix == '.json':
            try:
                spec = json.loads(text)
            except ValueError:
                return
            for route, ops in (spec.get('paths') or {}).items():
                for verb in ops if isinstance(ops, dict) else []:
                    if verb.lower() in ('get', 'post', 'put', 'patch', 'delete'):
                        self.add('endpoint', f'{verb.upper()} {route}', path, 1, 'openapi', 'high', 'openapi', 'api')
            return
        in_paths = False
        for m in re.finditer(r'^(\s*)([^\s#][^:]*):\s*$', text, re.M):
            indent, key = len(m.group(1)), m.group(2).strip().strip('"\'')
            if indent == 0:
                in_paths = key == 'paths'
            elif in_paths and key.startswith('/'):
                self.add('endpoint', key, path, self.line_of(offsets, m.start()), 'openapi-yaml', 'high', 'openapi', 'api')

    def scan_test(self, path: Path, text: str):
        visits, kinds = [], set()
        for kind, rx in VISIT_PATTERNS:
            for m in rx.finditer(text):
                visits.append(m.group(1)); kinds.add(kind)
        rel = self.rel(path)
        e2e_dir = any(p.lower() in ('e2e', 'cypress', 'playwright', 'androidtest', 'uitests', 'integration_test',
                                    '.maestro', 'maestro') for p in Path(rel).parts[:-1])
        if visits or e2e_dir or path.name.endswith(('UITests.swift', 'UITest.swift')):
            self.tests.append({'file': rel, 'framework_hint': sorted(kinds) or None,
                               'visits': sorted(set(visits)), 'test_cases': len(TEST_CASE.findall(text))})
        elif path.suffix in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.py') and TEST_NAME_RE.search(path.name):
            cases = len(PY_TEST_CASE.findall(text)) if path.suffix == '.py' else len(TEST_CASE.findall(text))
            self.unit_tests['files'] += 1
            self.unit_tests['test_cases'] += cases

    def detect_project(self, path: Path, text: str):
        name = path.name
        if name == 'package.json':
            try:
                data = json.loads(text)
                names = {**data.get('dependencies', {}), **data.get('devDependencies', {})}
                self.frameworks |= {FRAMEWORK_DEPS[n] for n in names if n in FRAMEWORK_DEPS}
            except (ValueError, AttributeError):
                pass
        elif name == 'pubspec.yaml' and re.search(r'^\s*flutter\s*:', text, re.M):
            self.frameworks.add('flutter')
        elif name.startswith('build.gradle') and 'com.android.application' in text:
            self.frameworks.add('android')
        elif name.startswith(('requirements', 'pyproject')):
            low = text.lower()
            for fw in ('flask', 'fastapi', 'django', 'pytest'):
                if re.search(rf'^\s*"?{fw}\b', low, re.M):
                    self.frameworks.add(fw)
        elif name == 'Gemfile' and re.search(r"gem\s+['\"]rails['\"]", text):
            self.frameworks.add('rails')
        elif name == 'go.mod':
            self.frameworks.add('go-http')
        elif path.suffix == '.csproj' and 'Microsoft.AspNetCore' in text:
            self.frameworks.add('aspnet')
        elif name == 'pom.xml' and 'spring-boot' in text:
            self.frameworks.add('spring')
        elif path.suffix == '.swift':
            self.frameworks.add('swift')

    def run(self) -> dict:
        for path in self.walk():
            try:
                if path.stat().st_size > MAX_BYTES:
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            rel_parts = Path(self.rel(path)).parts
            self.detect_project(path, text)
            if self.is_test(rel_parts, path.name):
                self.scan_test(path, text)
                continue
            deps = self.deps_for(path) if path.suffix in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue',
                                                          '.svelte', '.mdx') else set()
            if re.fullmatch(r'(tsconfig|jsconfig)([.\w-]*)\.json', path.name):
                self.scan_tsconfig(path, text)
            if self.is_guard_file(path, deps):
                self.scan_guard(path, text)
            self.file_route(path, list(rel_parts), deps, text)
            self.scan_content(path, text, deps)
        self.scan_python_project()
        self.scan_client_routes()
        self.wrappers = self.resolve_wrappers()
        fw = self.frameworks
        if any(i['platform'] == 'cli' for i in self.items if i.get('platform')): self.platforms.add('cli')
        if fw & WEB_FW: self.platforms.add('web')
        if fw & {'react-native', 'expo-router', 'react-navigation', 'flutter'}: self.platforms |= {'ios', 'android'}
        if 'android' in fw: self.platforms.add('android')
        if 'swift' in fw: self.platforms.add('ios')
        if fw & API_FW or any(i['kind'] == 'endpoint' for i in self.items): self.platforms.add('api')
        return self.result()

    def result(self) -> dict:
        seen, unique = set(), []
        for it in sorted(self.items, key=lambda i: (i['kind'], i['file'], i['line'], i['name'])):
            key = (it['kind'], it['name'], it['file'], bool(it.get('index')))
            if key not in seen:
                seen.add(key); unique.append(it)
        labels = {kind: self.service_labels([i for i in unique if i['kind'] == kind])
                  for kind in ('route', 'screen', 'endpoint')}
        for it in unique:
            service = labels.get(it['kind'], {}).get(self.app_root(it['file'])) if labels.get(it['kind']) else None
            if service:
                it['service'] = service
            if it['kind'] in ('route', 'screen', 'endpoint'):
                dom = domain_hint(it['name'])
                slug = re.sub(r'[^a-z0-9]+', '-', service.lower()).strip('-') if service else None
                if slug:
                    dom = f'{slug}-{dom}' if dom and dom != slug else slug
                if dom:
                    it['domain'] = dom
                if TEST_ONLY_HINT.search(it['name'] + ' ' + it['file']) and 'note' not in it:
                    it['note'] = 'looks like test-only or sample surface; confirm before mapping it'
        surface: dict[str, list] = {'routes': [], 'screens': [], 'endpoints': [], 'entry_points': [], 'goals': []}
        counters: dict[str, int] = {}
        for it in unique:
            counters[it['kind']] = counters.get(it['kind'], 0) + 1
            it = {'id': f"{PREFIX[it['kind']]}-{counters[it['kind']]:03d}", **it}
            surface[{'route': 'routes', 'screen': 'screens', 'endpoint': 'endpoints',
                     'entry_point': 'entry_points', 'goal': 'goals'}[it['kind']]].append(it)
        return {
            'repository': self.root.name,
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'platforms': sorted(self.platforms),
            'frameworks': sorted(self.frameworks),
            'surface': surface,
            'context': {'flags': self.flags, 'tests': self.tests, 'guards': self.guards, 'layouts': self.layouts,
                        'path_aliases': self.aliases, 'goal_wrappers': getattr(self, 'wrappers', []),
                        'unit_tests': {**self.unit_tests, 'runners': sorted(self.frameworks & UNIT_RUNNERS),
                                       'note': 'Unit and component test files outside end-to-end folders. They are '
                                               'not journey tests: count them, but match journeys to e2e tests '
                                               '(context.tests).'},
                        'services': sorted({it['service'] for it in unique if it.get('service')}),
                        'python_mounts': self.py_notes},
            'stats': {'files_scanned': self.files_scanned, 'truncated': self.truncated,
                      'counts': {k: len(v) for k, v in surface.items()}},
            'limitations': [
                'Heuristic extraction: verify every item. Client routes are joined through nested route objects, '
                '<Route> elements and splat mounts in other files, and constant paths from plain object literals are '
                'resolved; routes built at runtime are not, and keep a note.',
                'Python routes are read with ast: FastAPI router and include_router prefixes and Flask blueprint '
                'prefixes are applied when they are literals; decorators in docstrings are ignored. Routes marked '
                '"no include_router ... was found" may be missing a prefix.',
                'When endpoints (or routes) come from several apps in one repository, each item carries a service '
                'label (from the nearest project manifest) and its proposed domain starts with it.',
                'Entry points include command-line commands (argparse sub-commands, click/typer commands, console '
                'scripts, package.json bin) and Azure Functions triggers; triggers are not user-facing by themselves.',
                'Goals with detector success-message are low-confidence candidates from success toasts and messages.',
                'Low-confidence items (e.g. class names ending in Screen/View) may be components, not screens.',
                'Navigation edges and handlers are not extracted; the agent must trace them. Guards are only '
                'summarised (context.guards for Next.js middleware/proxy files, guard_hints in context.layouts); '
                'the agent must confirm what each one does.',
                'Goal events passed as constants or variables (e.g. EVENTS.SIGNUP) are not resolved; tracking '
                'wrappers are followed only when they forward their first parameter (see context.goal_wrappers).',
                'The domain on each item is a proposal from its path prefix; correct it during review.',
                'Import aliases are reported (context.path_aliases) but imports are not resolved.',
            ],
        }


SELF_TEST_ROUTER = '''from fastapi import APIRouter


class BakeryRouter(APIRouter):
    """Adds default responses."""
'''
SELF_TEST_FILES = {
    # two FastAPI services with an APIRouter subclass, router prefixes and an include_router prefix
    'services/orders/pyproject.toml': '[project]\nname = "orders"\n',
    'services/stock/pyproject.toml': '[project]\nname = "stock"\n',
    'services/orders/api/routes/__init__.py': SELF_TEST_ROUTER,
    'services/stock/api/routes/__init__.py': SELF_TEST_ROUTER,
    'services/orders/api/routes/loaves.py': 'from api.routes import BakeryRouter\n\nrouter = BakeryRouter(prefix="/v1")\n\n\n'
                                            '@router.get("/loaves")\ndef list_loaves():\n    return []\n\n\n'
                                            '@router.post("/loaves/{loaf_id}/bake")\ndef bake(loaf_id: int):\n    return {}\n',
    'services/stock/api/routes/loaves.py': 'from api.routes import BakeryRouter\n\nrouter = BakeryRouter(prefix="/v1")\n\n\n'
                                           '@router.get("/loaves")\ndef list_stock():\n    return []\n',
    'services/orders/api/main.py': 'from fastapi import FastAPI\nfrom api.routes import loaves\n\napp = FastAPI()\n'
                                   'app.include_router(loaves.router, prefix="/shop")\n\n\n@app.get("/status")\n'
                                   'def status():\n    return {"ok": True}\n',
    'services/stock/api/main.py': 'from fastapi import FastAPI\nfrom .routes.loaves import router as loaves_router\n\n'
                                  'app = FastAPI()\napp.include_router(loaves_router)\n',
    # decorators in docstrings are usage examples, not routes
    'libs/common/auth/deps.py': '"""Usage::\n\n    router = APIRouter(prefix="/v1")\n\n    @router.get("/secret")\n'
                                '    def secret(): ...\n"""\nfrom fastapi import Depends\n\n\ndef current_user():\n'
                                '    """@router.post("/not-a-route")"""\n',
    'libs/common/tools/cli.py': 'import argparse\n\n\ndef main():\n    parser = argparse.ArgumentParser(prog="bakery-admin")\n'
                                '    sub = parser.add_subparsers(dest="command")\n    sub.add_parser("export")\n'
                                '    sub.add_parser("import")\n',
    'services/stock/functions/watcher/host.json': '{"version": "2.0"}\n',
    'services/stock/functions/watcher/function_app.py':
        'import azure.functions as func\n\napp = func.FunctionApp()\n\n\n'
        '@app.blob_trigger(arg_name="blob", path="deliveries/{name}", connection="STORAGE")\ndef on_delivery(blob):\n'
        '    pass\n\n\n@app.route(route="ovens/{oven_id}", methods=["GET"])\ndef oven(req):\n    pass\n',
    # a React Router SPA: constant paths, index routes, a lazy route and a nested <Routes> under a splat mount
    'web/package.json': '{"dependencies": {"react-router-dom": "6.28.0"}, "devDependencies": {"vitest": "3.2.4"}}\n',
    'web/src/routes/paths.ts': "export const ROUTES = {\n  LOGIN: '/login',\n  SHOP: '/shop',\n"
                               "  ADMIN: { ROOT: '/admin' },\n} as const;\n",
    'web/src/routes/index.tsx':
        "import { createBrowserRouter, Navigate } from 'react-router-dom';\nimport { ROUTES } from './paths';\n"
        "import ShopModule from '../shop/ShopModule';\n\n// { path: '/commented-out', element: <Gone /> }\n"
        "export const router = createBrowserRouter([\n  { path: ROUTES.LOGIN, element: <LoginPage /> },\n  {\n"
        "    path: '/',\n    element: <Layout />,\n    children: [\n"
        "      { index: true, element: <Navigate to={ROUTES.SHOP} replace /> },\n"
        "      { path: `${ROUTES.SHOP}/*`, element: <ShopModule /> },\n"
        "      { path: 'reports', lazy: () => import('../reports/ReportsPage') },\n"
        "      { path: ROUTES.ADMIN.ROOT, element: <Admin />, children: [{ path: 'users', element: <Users /> }] },\n"
        "    ],\n  },\n]);\n",
    'web/src/shop/ShopModule.tsx':
        "import { Routes, Route, Navigate } from 'react-router-dom';\nimport { toast } from 'sonner';\n\n"
        "export default function ShopModule() {\n  const done = () => toast.success('Order placed');\n  return (\n"
        "    <Routes>\n      <Route index element={<Navigate to=\"cakes\" />} />\n"
        "      <Route path=\"cakes\" element={<Cakes />} />\n      <Route path=\"cakes/:cakeId\" element={<CakeDetail />} />\n"
        "      <Route path=\"orders/*\" element={<Orders />}>\n        <Route path=\"history\" element={<History />} />\n"
        "      </Route>\n    </Routes>\n  );\n}\n",
    'web/src/shop/Cakes.test.tsx': "import { it } from 'vitest';\nit('lists cakes', () => {});\nit('filters cakes', () => {});\n",
    'web/e2e/shop.spec.ts': "import { test } from '@playwright/test';\ntest('shop', async ({ page }) => {\n"
                            "  await page.goto('/shop/cakes');\n});\n",
}
SELF_TEST_WANT = {
    'endpoints': {('GET /shop/v1/loaves', 'orders-shop'), ('POST /shop/v1/loaves/{loaf_id}/bake', 'orders-shop'),
                  ('GET /status', 'orders-status'), ('GET /v1/loaves', 'stock-loaves'),
                  ('GET /api/ovens/{oven_id}', 'stock-functions-watcher-ovens')},
    'routes': {'/login', '/', '/shop/*', '/reports', '/admin', '/admin/users', '/shop', '/shop/cakes',
               '/shop/cakes/:cakeId', '/shop/orders/*', '/shop/orders/history'},
    'entry_points': {'cli: bakery-admin export', 'cli: bakery-admin import', 'blob trigger: deliveries/{name}'},
    'goals': {'Order placed'},
}


def self_test() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'bakery'
        for rel, text in SELF_TEST_FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        data = Extractor(root.resolve(), 1000).run()
    s = data['surface']
    got = {'endpoints': {(i['name'], i.get('domain')) for i in s['endpoints']},
           'routes': {i['name'] for i in s['routes']},
           'entry_points': {i['name'] for i in s['entry_points']},
           'goals': {i['name'] for i in s['goals']}}
    problems = []
    for key, want in SELF_TEST_WANT.items():
        if got[key] != want:
            problems.append(f'{key}: missing {sorted(want - got[key])}, unexpected {sorted(got[key] - want)}')
    index = sorted(i['name'] for i in s['routes'] if i.get('index'))
    if index != ['/', '/shop']:
        problems.append(f'index routes: {index}')
    ut = data['context']['unit_tests']
    if (ut['files'], ut['test_cases'], ut['runners']) != (1, 2, ['vitest']):
        problems.append(f'unit tests: {ut}')
    if [t['file'] for t in data['context']['tests']] != ['web/e2e/shop.spec.ts']:
        problems.append(f"e2e tests: {data['context']['tests']}")
    if 'cli' not in data['platforms'] or data['context']['services'] != ['orders', 'stock', 'stock-functions-watcher']:
        problems.append(f"platforms {data['platforms']}, services {data['context']['services']}")
    for p in problems:
        print('FAIL', p)
    if problems:
        print('SELF-TEST FAILED'); return 1
    c = data['stats']['counts']
    print(f"self-test passed: {c['endpoints']} endpoints, {c['routes']} routes, {c['entry_points']} entry points, "
          f"{c['goals']} goals")
    return 0


def main() -> int:
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description='Extract a candidate user-facing surface from a repository.')
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, help='write JSON here instead of stdout')
    ap.add_argument('--max-files', type=int, default=20000)
    ap.add_argument('--goal-wrapper', action='append', default=[], metavar='NAME',
                    help='name of a function that sends analytics events (its first argument is the event name); '
                         'repeatable, comma-separated lists allowed. Wrappers that forward their first parameter to a '
                         'known analytics call are detected without this.')
    args = ap.parse_args()
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr); return 2
    wrappers = [w for arg in args.goal_wrapper for w in arg.split(',')]
    data = Extractor(root, args.max_files, wrappers).run()
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        c = data['stats']['counts']
        print(f"Wrote {args.out}: {c['routes']} routes, {c['screens']} screens, {c['endpoints']} endpoints, "
              f"{c['entry_points']} entry points, {c['goals']} goals, {len(data['context']['tests'])} e2e test files "
              f"and {data['context']['unit_tests']['files']} unit test files "
              f"({data['stats']['files_scanned']} files scanned{', TRUNCATED' if data['stats']['truncated'] else ''}); "
              f"{len(data['context']['guards'])} guard files, {len(data['context']['layouts'])} layouts, "
              f"{len(data['context']['goal_wrappers'])} tracking wrappers")
    else:
        print(text)
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
