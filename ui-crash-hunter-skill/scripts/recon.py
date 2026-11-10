#!/usr/bin/env python3
"""Recon for crash hunting: platforms, routes and screens, the error-boundary map, the data layer,
API contracts, persisted state and supported targets. Stdlib only; never runs or modifies the repo.

    python recon.py <repo> [--journey-map journey_map.json] [--out recon.json]

Everything is heuristic and records file:line so the agent can verify it. The boundary map is what
decides whether a throw is B1 (nothing catches it) or B2 (a route or screen boundary does), so the
agent must confirm which boundary wraps each finding rather than trust this list alone.
--journey-map attaches journey priorities (P1/P2/P3) to routes, for the reach factor.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'.git', 'node_modules', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', '.output', 'coverage',
             'vendor', 'Pods', 'Carthage', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__',
             'storybook-static', '.expo', 'venv', '.venv', 'target'}
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
NEXT_FILES = re.compile(r'(^|/)(app|src/app)/(.*/)?(page|error|global-error|layout)\.(tsx|jsx|js|ts)$|(^|/)(pages|src/pages)/.+\.(tsx|jsx|js|ts)$')

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

def main() -> int:
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
                routes.append({'kind': 'route', 'path': '/' + re.sub(r'^(src/)?(app|pages)/?|/?page\.\w+$|\.\w+$', '', rel),
                               'file': rel, 'line': 1, 'detector': 'nextjs-file'})
        for det, rx in ROUTE_RX:
            for m in rx.finditer(text):
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
    # boundary usages that refer to a defined boundary component, or a known library boundary
    defined = {b['name'] for b in boundaries if b['kind'] == 'definition'}
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
    for n, r in enumerate(uniq_routes, 1):
        r['id'] = f'RT-{n}'
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
        'notes': ['Heuristic: confirm which boundary wraps each finding by reading the render tree.'] +
                 (['No error boundary or global handler found: any uncaught render error is app-fatal (B1).'] if not boundaries else []),
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"platforms: {', '.join(platforms) or 'unknown'} | routes/screens: {len(uniq_routes)} | boundaries: {len(boundaries)} | "
              f"contracts: {len(contracts)} | persisted-state uses: {len(persisted)} -> {args.out}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
