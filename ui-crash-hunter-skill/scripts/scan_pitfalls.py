#!/usr/bin/env python3
"""Scan UI source for crash pitfalls and merge candidates from other tools into one list.

    python scan_pitfalls.py <repo> [--packs web,android] [--sarif semgrep.sarif ...]
                                   [--extra overlay.json contract.json ...] [--out candidates.json]

Every hit is a *candidate* with pattern confidence: a line that matches a pitfall in
data/pitfalls.json. Candidates are not findings until the agent traces them to a realistic trigger.
--sarif adds results from SARIF-emitting analysers (Semgrep, detekt, SwiftLint, Android Lint),
mapped to pitfalls through each pitfall's 'tools' list. --extra adds candidates written by
strict_overlay.py and contract_diff.py. Hits on the same file, line and pitfall are merged, keeping
every source. Test files, dependencies, build output and minified files are skipped.

Scope is UI code: files are followed from the UI entry points through their imports (tsconfig and
jsconfig "paths" aliases such as "@/*" are resolved; see ui_graph.py). Route handlers (a throw there
is an HTTP 500, not a UI crash), Node HTTP server modules (node:http createServer, Express, Fastify,
Koa) and Node command-line scripts (a node shebang, or process.argv with no DOM use), tests and
modules no entry point imports are excluded, from this scan and from --sarif/--extra candidates
alike, and counted with examples in "scope.excluded". A test-named directory (tests/, e2e/) that
holds its own package manifest is a product package and is scanned.
--all-files keeps route handlers and unreachable modules (tests and tool configs such as
vite.config.ts or eslint.config.js stay out). An --extra candidate without a snippet gets its source line.
Promise chains (matcher "promise_chain") are parsed whole across lines; --self-test runs a built-in
check of that parser and exits.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(HERE))
import sarif  # noqa: E402
from langs import (vendored_roots, language_census, language_warning, embedded_documents, script_text,  # noqa: E402
                   HOST_EXT, TEMPLATE_FILE, under, prune_dir, is_template_path, server_rendered,
                   skipped_data_dirs, EXCLUDE, python_web_frameworks)
from ui_graph import graph as ui_graph, classify, SRC_EXT, MANIFESTS, TEST_DIR_NAME, node_role, note_packages, walk as walk_repo  # noqa: E402

CATALOGUE = HERE.parent / 'data' / 'pitfalls.json'
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', 'coverage', 'vendor',
             'Pods', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__', 'storybook-static',
             '.expo', 'e2e', 'cypress', 'playwright', '__tests__', '__mocks__', '.turbo', '.cache', '.venv', 'venv',
             'test-shims',
             'test', 'tests', 'androidTest', 'integration_test', 'UITests', 'Tests'}
TEST_FILE = re.compile(r'(\.|_)(test|spec|stories|story)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|\.d\.ts$|\.min\.js$', re.I)
COMMENT = re.compile(r'^\s*(//|/\*|\*|#|<!--)')
SSR_DEPS = {'next': 'next', 'nuxt': 'nuxt', '@remix-run/react': 'remix', '@sveltejs/kit': 'sveltekit',
            'astro': 'astro', 'gatsby': 'gatsby'}

def is_bundle(p: Path) -> bool:
    """Built output (a sibling .map file, or a very large script) is not source to analyse."""
    try:
        return p.with_name(p.name + '.map').exists() or (p.suffix in ('.js', '.mjs', '.cjs') and p.stat().st_size > 400_000)
    except OSError:
        return False

def load_catalogue(path: Path = CATALOGUE) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))

def ssr_frameworks(root: Path) -> set[str]:
    found: set[str] = set()
    for pkg in [root / 'package.json', *root.glob('*/package.json'), *root.glob('apps/*/package.json')]:
        try:
            data = json.loads(pkg.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        deps = {**(data.get('dependencies') or {}), **(data.get('devDependencies') or {})}
        found |= {name for dep, name in SSR_DEPS.items() if dep in deps}
    return found

def source_files(root: Path, extensions: set[str], vendored: list[str] = ()):
    """Source files with the given extensions: dependency, build and test directories (an Xcode `<Name>Tests`
    target included) and vendored trees are pruned before descending."""
    vendored = {root / v for v in vendored}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                # a test-named directory with a package manifest (packages/e2e/package.json) is a product package
                if p in vendored or prune_dir(root, p):
                    continue
                if ((p.name not in SKIP_DIRS and not TEST_DIR_NAME.match(p.name))
                        or (TEST_DIR_NAME.match(p.name) and any((p / m).is_file() for m in MANIFESTS)))                         and not p.name.startswith('.'):
                    stack.append(p)
            elif p.suffix in extensions and not TEST_FILE.search(p.name) and not is_bundle(p):
                yield p

REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^') | {''}
OPEN, CLOSE = '([{', ')]}'

def mask_code(text: str) -> str:
    """Blank out comments, string, template and regex literal contents (same length, newlines kept), so
    brackets can be balanced on what is left."""
    out = list(text)
    i, n = 0, len(text)
    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != '\n':
                out[k] = ' '
    prev = ''
    while i < n:
        c = text[i]
        if c == '/' and text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == '/' and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == '\\':
                    j += 1
                elif c != '`' and text[j] == '\n':
                    break
                j += 1
            blank(i + 1, j)
            i, prev = j + 1, c
        elif c == '/' and (prev in REGEX_BEFORE or re.search(r'\b(return|typeof|case|in|of)\s*$', text[max(0, i - 10):i])):
            j, cls = i + 1, False
            while j < n and text[j] != '\n' and (cls or text[j] != '/'):
                if text[j] == '\\':
                    j += 1
                elif text[j] == '[':
                    cls = True
                elif text[j] == ']':
                    cls = False
                j += 1
            blank(i + 1, j)
            i, prev = j + 1, '/'
        else:
            if not c.isspace():
                prev = c
            i += 1
    return ''.join(out)

def _match(code: str, i: int, step: int) -> int:
    """Index of the bracket matching code[i], scanning forwards (step 1) or backwards (step -1); -1 if none."""
    depth, j = 0, i
    while 0 <= j < len(code):
        ch = code[j]
        if ch in (OPEN if step == 1 else CLOSE):
            depth += 1
        elif ch in (CLOSE if step == 1 else OPEN):
            depth -= 1
            if depth == 0:
                return j
        j += step
    return -1

IDENT = re.compile(r'[\w$]')

def chain_start(code: str, dot: int) -> int:
    """Start of the receiver expression ending just before code[dot] ('.then'): identifiers, member
    accesses, calls and index expressions, across lines."""
    j = dot - 1
    while True:
        while j >= 0 and code[j].isspace():
            j -= 1
        if j < 0:
            return 0
        if code[j] in ')]':
            k = _match(code, j, -1)
            if k < 0:
                return j + 1
            j = k - 1
            continue
        if IDENT.match(code[j]):
            while j >= 0 and IDENT.match(code[j]):
                j -= 1
            k = j
            while k >= 0 and code[k].isspace():
                k -= 1
            if k >= 0 and code[k] == '.':
                j = k - 1 - (1 if k >= 1 and code[k - 1] == '?' else 0)
                continue
            return j + 1
        return j + 1

def chain_links(code: str, dot: int) -> tuple[list[tuple[str, int, int]], int]:
    """Calls in the chain from code[dot] onwards: (method, args, position), and where the chain ends."""
    links, j = [], dot
    while True:
        m = re.compile(r'\??\.\s*([\w$]+)\s*').match(code, j)
        if not m:
            return links, j
        name, j = m.group(1), m.end()
        if j < len(code) and code[j] == '(':
            end = _match(code, j, 1)
            if end < 0:
                return links, len(code)
            inner, depth, commas = code[j + 1:end], 0, 0
            for ch in inner:
                if ch in OPEN:
                    depth += 1
                elif ch in CLOSE:
                    depth -= 1
                elif ch == ',' and depth == 0:
                    commas += 1
            args = (commas + 1 if inner.strip() else 0) - (1 if inner.rstrip().endswith(',') else 0)
            links.append((name, args, m.start()))
            j = end + 1
        while j < len(code) and code[j] == '[':
            end = _match(code, j, 1)
            j = len(code) if end < 0 else end + 1
        k = j
        while k < len(code) and code[k].isspace():
            k += 1
        if code.startswith('.', k) or code.startswith('?.', k):
            j = k
        else:
            return links, j

def promise_chain_hits(text: str, rel: str, p: dict) -> list[dict]:
    """Promise chains with no rejection handler anywhere in the chain, parsed across lines."""
    code = mask_code(text)
    lines = text.splitlines()
    hits, done = [], set()
    for m in re.finditer(r'\??\.\s*then\s*\(', code):
        if m.start() in done:
            continue
        start = chain_start(code, m.start())
        j = start
        while j < len(code) and IDENT.match(code[j]):
            j += 1
        while j < len(code) and code[j] in '([':
            end = _match(code, j, 1)
            j = len(code) if end < 0 else end + 1
        every, _ = chain_links(code, j)
        if not any(pos == m.start() for _, _, pos in every):  # receiver not understood: judge from this link on
            every, _ = chain_links(code, m.start())
        for name, _, pos in every:
            if name == 'then':
                done.add(pos)
        handled = any(name == 'catch' or (name == 'then' and args >= 2) for name, args, _ in every)
        before = code[:start].rstrip()
        consumed = (re.search(r'(\breturn|\bawait|\byield|=>)$', before) is not None
                    or (before[-1:] in ('(', ',', '[') and not re.search(r'\b(if|while|for|switch)\s*\($', before)))
        if handled or consumed:
            continue
        ln = text.count('\n', 0, m.start()) + 1
        col = m.start() - (text.rfind('\n', 0, m.start()) + 1) + 1
        hits.append({'pitfall_id': p['id'], 'file': rel, 'line': ln, 'column': col,
                     'snippet': lines[ln - 1].strip()[:200], 'message': p['title'],
                     'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]})
    return hits

def scan_file(path: Path, rel: str, pitfalls: list[dict]) -> list[dict]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    return scan_text(text, rel, pitfalls, path.suffix)


def scan_embedded(path: Path, rel: str, pitfalls: list[dict]) -> tuple[list[dict], list[dict]]:
    """Inline scripts of HTML documents held in string constants (a Go raw string for html/template, a Python
    triple-quoted string) or template files, scanned with the web pitfalls at their real file and line.
    Returns (hits, documents)."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return [], []
    docs = embedded_documents(text, path.suffix, rel)
    hits = []
    for d in docs:
        for s in d.pop('_scripts'):
            for h in scan_text(script_text(text, s), rel, pitfalls, '.js'):
                h['embedded'] = d['kind']
                h['snippet'] = text.splitlines()[h['line'] - 1].strip()[:200]
                hits.append(h)
    return hits, [{'file': rel, **d} for d in docs]


def scan_text(text: str, rel: str, pitfalls: list[dict], suffix: str) -> list[dict]:
    lines = text.splitlines()
    if lines and max(len(l) for l in lines) > 2000:  # minified or generated
        return []
    hits = []
    for p in pitfalls:
        if p.get('matcher') == 'promise_chain' and (not p.get('extensions') or suffix in p['extensions']):
            hits.extend(promise_chain_hits(text, rel, p))
    pitfalls = [p for p in pitfalls if not p.get('matcher')]
    for i, line in enumerate(lines):
        if COMMENT.match(line):
            continue
        for p in pitfalls:
            if p.get('extensions') and suffix not in p['extensions']:
                continue
            m = p['_rx'].search(line)
            if not m:
                continue
            if p.get('_suppress_line') and p['_suppress_line'].search(line):
                continue
            w = int(p.get('window', 6))
            before = '\n'.join(lines[max(0, i - w):i])
            after = '\n'.join(lines[i:i + w + 1])
            if p.get('_suppress_before') and p['_suppress_before'].search(before):
                continue
            if p.get('_suppress_after') and p['_suppress_after'].search(after):
                continue
            if p.get('_require_before') and not p['_require_before'].search(before):
                continue
            if p.get('_require_after') and not p['_require_after'].search(after):
                continue
            hits.append({'pitfall_id': p['id'], 'file': rel, 'line': i + 1, 'column': m.start() + 1,
                         'snippet': line.strip()[:200], 'message': p['title'],
                         'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]})
    return hits

def tool_index(catalogue: dict) -> dict[str, str]:
    """'tool:rule' -> pitfall id, for mapping SARIF and overlay results back to the catalogue."""
    idx: dict[str, str] = {}
    for p in catalogue['pitfalls']:
        for t in p.get('tools') or []:
            idx.setdefault(t.lower(), p['id'])
    return idx

def from_sarif(paths: list[Path], root: Path, catalogue: dict) -> tuple[list[dict], list[dict]]:
    idx = tool_index(catalogue)
    out, tools = [], []
    for path in paths:
        results = sarif.read(path, root)
        names = sorted({r['tool'] for r in results}) or [path.stem]
        tools.append({'tool': ', '.join(names), 'input': str(path), 'results': len(results)})
        for r in results:
            tool = (r['tool'] or '').lower().split()[0] if r['tool'] else 'sarif'
            rule = r.get('rule_id') or ''
            pid = idx.get(f'{tool}:{rule}'.lower()) or idx.get(f'{tool}:{rule.split(".")[-1]}'.lower())
            out.append({'pitfall_id': pid, 'file': r['file'], 'line': r['line'], 'column': r.get('column'),
                        'snippet': r.get('snippet') or '', 'message': r['message'],
                        'sources': [{'tool': r['tool'], 'version': r.get('version'), 'rule_id': rule}]})
    return out, tools

def merge(cands: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for c in cands:
        key = (c['file'], c['line'], c.get('pitfall_id') or c['message'])
        if key in merged:
            have = merged[key]
            for s in c['sources']:
                if s not in have['sources']:
                    have['sources'].append(s)
            for k in ('field', 'schema_ref', 'nullable_reason', 'guard_nearby'):
                if c.get(k) is not None and have.get(k) is None:
                    have[k] = c[k]
        else:
            merged[key] = dict(c)
    out = sorted(merged.values(), key=lambda c: (c['file'], c['line'] or 0, c.get('pitfall_id') or ''))
    for n, c in enumerate(out, 1):
        c['id'] = f'CAND-{n:03d}'
    return out

SELF_TEST_JS = r"""// a .then( in a comment, and "x.then(" in a string, are not code
const s = "fetch(a).then(b)";
const re = /\)\.then\(/g;
function returned() {
  return fetch("/a")
    .then((r) => r.json());
}
navigator.serviceWorker.getRegistration().then((reg) => {
  if (reg) return;
  navigator.serviceWorker
    .register("/sw.js")
    .then((registration) => {
      console.info("ok", registration);
    })
    .catch((error) => console.warn(error));
});
loadIndex().then(() => render(input.value));
loadIndex()
  .then(() => render(input.value))
  .finally(() => done());
api.get("/x").then(ok, (err) => showError(err));
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open("v1").then((c) => c.addAll(["/"])));
});
const load = () => fetch("/b").then((r) => r.json());
async function awaited() { return await fetch("/c").then((r) => r.text()); }
fetch(`/d/${id}`, { method: "POST" })
  .then((r) => r.json())
  .then((j) => { show(j.items.map((x) => (x.then ? 1 : 2))); })
  .catch(() => {});
const ratio = a / b; const half = (x) / 2; client.get(u).then(cb);
"""
# unhandled chains; the chains split across lines that end in .catch (10-15, 27-30) are handled
SELF_TEST_EXPECT = [8, 17, 19, 31]

# A synthetic workspace for the scope and recon checks: a Next.js app nested in web/ (next.config.ts and its
# tsconfig "@/*" alias there, next in the package above), a Node server rendering pages from template literals,
# a command-line script, and a test-named package directory (e2e/ with its own package.json).
SELF_TEST_TREE = {
    'package.json': '{"name": "shelf", "dependencies": {"next": "16.0.0", "react": "19.0.0"}}',
    'tsconfig.json': '{"include": ["checks/**/*.ts"]}',
    'web/next.config.ts': 'export default {};\n',
    'web/tsconfig.json': '{"compilerOptions": {"jsx": "react-jsx", "paths": {"@/*": ["./src/*"]}}}',
    'web/src/app/layout.tsx': 'export default function Root({ children }) { return children; }\n',
    'web/src/app/page.tsx': 'import { Price } from "@/parts/Price";\nexport default function Home() { return <Price />; }\n',
    'web/src/parts/Price.tsx': ('const FEE = 2;\n'
                                'export function Price({ amount }) {\n'
                                '  return <p>{FEE.toFixed(2)} {amount === null ? "0.00" : amount.toFixed(2)} {amount?.toFixed(1)}</p>;\n'
                                '}\n'),
    'site/server.mjs': ("import { createServer } from 'node:http';\nimport { pages } from './pages.mjs';\n"
                        "createServer((req, res) => {\n  const url = new URL(req.url, 'http://x');\n"
                        "  if (url.pathname === '/api/items') { res.end(JSON.stringify([])); return; }\n"
                        "  const body = JSON.parse('{}');\n  res.end(pages[url.pathname]());\n}).listen(8000);\n"),
    'site/pages.mjs': ("// the shelf list is kept in localStorage\nexport const pages = {\n"
                       "  '/': () => `<main><h1>Shelf</h1><script>const s = localStorage.getItem('shelf');</script></main>`,\n"
                       "  '/about': () => `<main><h1>About</h1></main>`,\n};\n"),
    'tools/report.mjs': '#!/usr/bin/env node\nconst n = Number(process.argv[2]);\nconsole.log(n.toFixed(2));\n',
    'e2e/package.json': '{"name": "e2e-kit"}',
    'e2e/src/index.ts': 'export const total = (x: number | null) => x.toFixed(2);\n',
}


def self_test_scope() -> list[str]:
    """Scope checks on SELF_TEST_TREE; returns the failures."""
    import subprocess, tempfile
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'shelf'
        for rel, text in SELF_TEST_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        g = ui_graph(root)
        if 'web/src/parts/Price.tsx' not in (g['reachable'] or []):
            fails.append('the nested project\'s "@/*" alias (web/tsconfig.json) was not followed')
        recon = subprocess.run([sys.executable, '-B', str(HERE / 'recon.py'), str(root)], capture_output=True, text=True,
                               encoding='utf-8')
        try:
            r = json.loads(recon.stdout)
        except ValueError:
            return fails + [f'recon.py failed: {recon.stderr[-300:]}']
        paths = {x['path'] for x in r['routes_screens']}
        if not {'/', '(root layout)'} <= paths:
            fails.append(f'nested Next.js routes not found as / and (root layout): {sorted(paths)}')
        server = (r['site'].get('server') or {})
        if sorted(x['path'] for x in server.get('routes', [])) != ['/', '/about']:
            fails.append(f"Node server page routes: {sorted(x['path'] for x in server.get('routes', []))}, expected / and /about")
        if [x['path'] for x in server.get('api_endpoints', [])] != ['/api/items']:
            fails.append('the Node server API endpoint /api/items was not kept apart from the pages')
        if [(p['file'], p['line']) for p in r['persisted_state']] != [('site/pages.mjs', 3)]:
            fails.append(f"persisted_state: {[(p['file'], p['line']) for p in r['persisted_state']]}, expected the "
                         'inline script only, not the comment')
        scan = subprocess.run([sys.executable, '-B', str(HERE / 'scan_pitfalls.py'), str(root), '--all-files'],
                              capture_output=True, text=True, encoding='utf-8')
        try:
            s = json.loads(scan.stdout)
        except ValueError:
            return fails + [f'scan_pitfalls.py failed: {scan.stderr[-300:]}']
        files = {c['file'] for c in s['candidates']}
        excl = s['scope']['excluded']
        if 'site/server.mjs' in files or 'server_code' not in excl:
            fails.append('the Node server module was scanned as UI code')
        if 'tools/report.mjs' in files or 'cli_scripts' not in excl:
            fails.append('the command-line script was scanned as UI code')
        if 'e2e/src/index.ts' not in files:
            fails.append('e2e/ with its own package.json was skipped as a test directory')
        if any(c['file'] == 'web/src/parts/Price.tsx' and c['pitfall_id'] == 'WEB-NUMBER-FORMAT-ON-NULL'
               for c in s['candidates']):
            fails.append('toFixed on a capitalised constant, a same-line null guard or ?. was flagged')
    return fails


# A Go command-line tool whose only browser code is an HTML page held in a Go string constant (rendered with
# html/template), beside a bundled third-party driver tree (its own LICENSE and package.json, an Xcode project,
# a TypeScript types file, a build script and an Xcode test target holding a SwiftUI fixture view).
SELF_TEST_GO_PAGE = ('package summary\n\nimport "html/template"\n\n'
                     '// page renders the run summary.\n'
                     'var page = template.Must(template.New("summary").Parse(pageHTML))\n\n'
                     'const pageHTML = `<!DOCTYPE html>\n<html>\n<head><style>body { font-family: sans-serif; }</style></head>\n'
                     '<body>\n<div id="list">{{range .Entries}}<div class="row">{{.Name}}</div>{{end}}</div>\n<script>\n'
                     "const saved = JSON.parse(sessionStorage.getItem('filters'));\n"
                     'document.title = saved.label;\n</script>\n</body>\n</html>`\n')
SELF_TEST_GO_TREE = {
    'go.mod': 'module example.test/harbourlog\n\ngo 1.22\n',
    'main.go': 'package main\n\nimport "example.test/harbourlog/cmd"\n\nfunc main() { cmd.Run() }\n',
    'cmd/run.go': 'package cmd\n\n// Run parses flags and runs the tool.\nfunc Run() {}\n',
    'internal/store/store.go': 'package store\n\n// Entry is one logged line.\ntype Entry struct{ Name string }\n',
    'internal/store/store_test.go': 'package store\n\nimport "testing"\n\nfunc TestEntry(t *testing.T) {}\n',
    'internal/flow/parse.go': 'package flow\n\n// Parse reads a flow file.\nfunc Parse() {}\n',
    'internal/flow/run.go': 'package flow\n\n// Run runs a flow.\nfunc Run() {}\n',
    'internal/driver/client.go': 'package driver\n\n// Client talks to a device driver.\ntype Client struct{}\n',
    'internal/driver/retry.go': 'package driver\n\n// Retry retries a call.\nfunc Retry() {}\n',
    'internal/summary/types.go': 'package summary\n\n// Summary is the page model.\ntype Summary struct{ Entries []string }\n',
    'internal/summary/page.go': SELF_TEST_GO_PAGE,
    'drivers/agent/LICENSE': 'Synthetic licence text for a bundled third-party tree.\n',
    'drivers/agent/package.json': '{"name": "agent-kit", "dependencies": {"axios": "1.0.0"}}',
    'drivers/agent/Agent.xcodeproj/project.pbxproj': '// IPHONEOS_DEPLOYMENT_TARGET = 15.0;\n',
    'drivers/agent/AgentTests/WatchApp/ContentView.swift': 'import SwiftUI\nstruct ContentView: View { var body: some View { Text("x") } }\n',
    'drivers/agent/lib/types.ts': 'export interface Caps {\n  environment?: Record<string, string>;\n}\n',
    'drivers/agent/lib/host.ts': 'export const home = (caps) => caps.environment.HOME;\n',
    'drivers/agent/Scripts/fetch.mjs': "import axios from 'axios';\nawait axios.get('/x');\n",
    'web/types/caps.ts': 'export interface Caps {\n  environment?: Record<string, string>;\n}\n',
    'macos/Sources/Env.swift': 'let home = ProcessInfo.processInfo.environment["HOME"]\n',
    'tools/probe/ProbeTests/Helpers.swift': 'func pick(x: [String]?) -> String { return x!.first! }\n',
    'tools/probe/IntegrationTests_watchOS/Driver.swift': 'func run(y: String?) { print(y!.count) }\n',
}


def self_test_go() -> list[str]:
    """Language guard, vendored trees, Xcode test targets and HTML held in Go string constants; returns failures."""
    import subprocess, tempfile
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'harbourlog'
        for rel, text in SELF_TEST_GO_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        for rel in ('tools/probe/ProbeTests/Helpers.swift', 'x/IntegrationTests_watchOS/Driver.swift',
                    'drivers/agent/WebDriverAgentTests/IntegrationApp/ContentView.swift'):
            if classify(rel) != 'test':
                fails.append(f'{rel}: an Xcode test target directory is not classified as test code ({classify(rel)})')
        run = lambda script, *a: subprocess.run([sys.executable, '-B', str(HERE / script), str(root), *a],
                                                capture_output=True, text=True, encoding='utf-8')
        recon = run('recon.py')
        try:
            r = json.loads(recon.stdout)
        except ValueError:
            return fails + [f'recon.py failed: {recon.stderr[-300:]}']
        lc = r.get('language_coverage') or {}
        if lc.get('dominant') != 'Go' or lc.get('status') != 'unsupported_dominant':
            fails.append(f'recon language_coverage does not say Go is dominant and not analysed: {lc or "missing"}')
        if not any('Go' in w for w in r.get('warnings') or []):
            fails.append('recon has no top-level warning that the dominant language (Go) is not analysed')
        if r['site']['kind'] != 'unknown' or 'ios_deployment_target' in r['targets'] or \
                r['blast_radius']['model'] != 'unknown':
            fails.append(f"recon modelled the repo from incidental files: site {r['site']['kind']}, targets {r['targets']}")
        if any('ContentView' in str(x.get('path')) for x in r['routes_screens']):
            fails.append('recon listed a SwiftUI view inside a vendored tree / Xcode test target as a screen')
        if any(d['library'] == 'axios' for d in r['data_layer']):
            fails.append("recon took the vendored tree's build script as the app's data layer (axios)")
        emb = r['site'].get('embedded_templates') or []
        if [(e['file'], e['inline_scripts']) for e in emb] != [('internal/summary/page.go', 1)]:
            fails.append(f"recon site.embedded_templates: {[(e['file'], e['inline_scripts']) for e in emb]}, expected "
                         'the Go string constant in internal/summary/page.go with 1 inline script')
        if any('No error boundary' in n for n in r['notes']):
            fails.append('recon claims "no error boundary ... B1" with no analysed UI code')
        if 'drivers/agent' not in (lc.get('excluded') or []):
            fails.append(f"recon language_coverage.excluded does not list drivers/agent (lang_census): {lc.get('excluded')}")
        scan = run('scan_pitfalls.py')
        try:
            s = json.loads(scan.stdout)
        except ValueError:
            return fails + [f'scan_pitfalls.py failed: {scan.stderr[-300:]}']
        want = SELF_TEST_GO_PAGE.splitlines().index("const saved = JSON.parse(sessionStorage.getItem('filters'));") + 1
        got = {(c['file'], c['line'], c['pitfall_id']) for c in s['candidates']}
        if ('internal/summary/page.go', want, 'WEB-JSON-PARSE') not in got:
            fails.append(f'the inline script in the Go string constant was not scanned (want JSON.parse at '
                         f'internal/summary/page.go:{want}; got {sorted(got)[:5]})')
        if any(c['file'].startswith(('drivers/', 'tools/probe/')) for c in s['candidates']):
            fails.append('scan_pitfalls scanned a vendored tree or an Xcode test target')
        if not any('Go' in w for w in s['scope'].get('warnings') or []) or (s.get('language_coverage') or {}).get('status') != 'unsupported_dominant':
            fails.append('scan_pitfalls does not warn that the dominant language (Go) is not analysed')
        cd = run('contract_diff.py')
        try:
            c = json.loads(cd.stdout)
        except ValueError:
            return fails + [f'contract_diff.py failed: {cd.stderr[-300:]}']
        if any(x['file'].startswith('drivers/') for x in c['contracts']) or c['candidates']:
            fails.append(f"contract_diff used a vendored tree or matched a TypeScript type in Swift: contracts "
                         f"{[x['file'] for x in c['contracts']]}, candidates {[x['file'] for x in c['candidates']]}")
        if not c.get('warnings'):
            fails.append('contract_diff does not warn that no UI entry point was recognised (UI scope not applied)')
    return fails


# A server-rendered Flask site (a recipe site): Python views render Jinja templates named .html under templates/,
# whose inline scripts are the browser code; a JSON API under /api/; a data directory of 250 JSON records; a map
# page reading a map library's always-numeric bounds getters.
SELF_TEST_FLASK_INDEX = ('{% extends "base.html" %}\n{% block content %}\n<input id="q">\n<ul id="hits"></ul>\n<script>\n'
                         "const box = document.getElementById('q');\n"
                         "box.addEventListener('input', () => {\n"
                         "  fetch('/api/search?q=' + encodeURIComponent(box.value))\n"
                         '    .then(r => r.json())\n'
                         '    .then(show)\n'
                         '    .catch(() => {});\n'
                         '});\n'
                         "const recent = JSON.parse(localStorage.getItem('recent'));\n"
                         '</script>\n{% endblock %}\n')
SELF_TEST_FLASK_MAP = ('{% extends "base.html" %}\n{% block content %}\n<div id="map"></div>\n<script>\n'
                       'map.on("moveend", () => {\n  const b = map.getBounds();\n'
                       '  const q = `s=${b.getSouth().toFixed(5)}&n=${b.getNorth().toFixed(5)}`;\n'
                       '  load(q);\n});\n</script>\n{% endblock %}\n')
SELF_TEST_FLASK_TREE = {
    'requirements.txt': 'flask==3.1.0\n',
    'app.py': ("from flask import Flask, jsonify, render_template\n\napp = Flask(__name__)\n\n\n"
               "@app.route('/')\ndef index():\n    return render_template('index.html')\n\n\n"
               "@app.route('/recipe/<slug>')\ndef recipe(slug):\n    return render_template('recipe.html', slug=slug)\n\n\n"
               "@app.route('/map')\ndef map_page():\n    return render_template('map.html')\n\n\n"
               "@app.route('/api/search')\ndef api_search():\n    return jsonify([])\n"),
    'kitchen/__init__.py': '', 'kitchen/units.py': 'GRAMS = 1\n', 'kitchen/scale.py': 'def scale(x):\n    return x\n',
    'kitchen/search.py': 'def search(q):\n    return []\n', 'kitchen/load.py': 'def load():\n    return {}\n',
    'templates/base.html': '<!doctype html>\n<html><body>\n{% block content %}{% endblock %}\n'
                           '<script src="/static/site.js"></script>\n</body></html>\n',
    'templates/index.html': SELF_TEST_FLASK_INDEX,
    'templates/recipe.html': '{% extends "base.html" %}\n{% block content %}<h1>{{ slug }}</h1>{% endblock %}\n',
    'templates/map.html': SELF_TEST_FLASK_MAP,
    'static/site.js': "document.documentElement.classList.add('js');\n",
    # a plain HTML page (served as a static file): its inline script is browser code too
    'static/offline.html': '<!doctype html>\n<html><body><p>Offline</p>\n<script>\n'
                           "const last = JSON.parse(sessionStorage.getItem('last'));\n</script>\n</body></html>\n",
}
SELF_TEST_FLASK_TREE.update({f'recipes_data/r{n:03d}.json': '{"id": %d}\n' % n for n in range(250)})


def self_test_flask() -> list[str]:
    """A Flask + Jinja site is a supported multi-page site: routes from the views, templates as the UI, inline scripts
    scanned at template lines, the data directory pruned; returns the failures."""
    import subprocess, tempfile, time
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'recipes'
        for rel, text in SELF_TEST_FLASK_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        run = lambda script, *a: subprocess.run([sys.executable, '-B', str(HERE / script), str(root), *a],
                                                capture_output=True, text=True, encoding='utf-8')
        recon = run('recon.py')
        try:
            r = json.loads(recon.stdout)
        except ValueError:
            return [f'recon.py failed: {recon.stderr[-300:]}']
        site = r['site']
        if site.get('kind') != 'multi_page' or site.get('served_by') != 'flask' or r['blast_radius']['model'] != 'per_script':
            fails.append(f"Flask site: site.kind {site.get('kind')}, served_by {site.get('served_by')}, blast radius "
                         f"{r['blast_radius']['model']}; expected multi_page served by flask, per_script")
        pages = {x['path']: x for x in r['routes_screens']}
        if not {'/', '/recipe/<slug>', '/map'} <= set(pages) or '/api/search' in pages:
            fails.append(f'Flask page routes: {sorted(pages)}; expected /, /recipe/<slug>, /map and not /api/search')
        elif pages['/'].get('template') != 'templates/index.html':
            fails.append(f"Flask route / should name its template templates/index.html: {pages['/']}")
        if [x['path'] for x in (site.get('server') or {}).get('api_endpoints', [])] != ['/api/search']:
            fails.append(f"Flask API endpoints: {(site.get('server') or {}).get('api_endpoints')}")
        if any('NOT a crash analysis' in w for w in r.get('warnings') or []) or \
                (r.get('language_coverage') or {}).get('status') != 'server_rendered':
            fails.append(f"Flask site called unsupported: warnings {r.get('warnings')}, status "
                         f"{(r.get('language_coverage') or {}).get('status')}")
        skipped = [(x['path'], x['files']) for x in r.get('skipped_data_dirs') or []]
        if skipped != [('recipes_data', 250)]:
            fails.append(f'recon skipped data dirs: {skipped}, expected recipes_data (250 files)')
        scan = run('scan_pitfalls.py')
        try:
            s = json.loads(scan.stdout)
        except ValueError:
            return fails + [f'scan_pitfalls.py failed: {scan.stderr[-300:]}']
        got = {(c['file'], c['line'], c['pitfall_id']) for c in s['candidates']}
        lines = SELF_TEST_FLASK_INDEX.splitlines()
        at = lambda text: next(i for i, l in enumerate(lines, 1) if text in l)
        want = {('templates/index.html', at('JSON.parse'), 'WEB-JSON-PARSE'),
                ('templates/index.html', at('r.json()'), 'WEB-FETCH-JSON-UNCHECKED'),
                ('templates/index.html', at('.catch(() => {})'), 'WEB-SWALLOWED-ERROR'),
                ('templates/index.html', at("addEventListener('input'"), 'WEB-FETCH-PER-KEYSTROKE'),
                ('static/offline.html', 4, 'WEB-JSON-PARSE')}
        if not want <= got:
            fails.append(f'inline scripts in Jinja .html templates: missing {sorted(want - got)}; got {sorted(got)}')
        if any(c['file'] == 'templates/map.html' and c['pitfall_id'] == 'WEB-NUMBER-FORMAT-ON-NULL' for c in s['candidates']):
            fails.append("toFixed on a map library's bounds getter (always a number) was flagged")
        if any('NOT a crash analysis' in w for w in s['scope'].get('warnings') or []):
            fails.append('scan_pitfalls calls the Flask site unsupported')
        t0 = time.time()
        cd = run('contract_diff.py')
        took = time.time() - t0
        try:
            c = json.loads(cd.stdout)
        except ValueError:
            return fails + [f'contract_diff.py failed: {cd.stderr[-300:]}']
        if [(x['path'], x['files']) for x in c.get('skipped_data_dirs') or []] != [('recipes_data', 250)]:
            fails.append(f"contract_diff opened the data directory (skipped {c.get('skipped_data_dirs')}, {took:.1f} s)")
    return fails


def self_test() -> int:
    p = next(q for q in load_catalogue()['pitfalls'] if q.get('matcher') == 'promise_chain')
    got = [h['line'] for h in promise_chain_hits(SELF_TEST_JS, 'app.js', p)]
    ok = got == SELF_TEST_EXPECT
    print(f"self-test {'passed' if ok else 'FAILED'}: promise chains flagged at lines {got}, expected {SELF_TEST_EXPECT}")
    fails = self_test_scope()
    print(f"self-test {'passed' if not fails else 'FAILED'}: scope and recon on a synthetic workspace (nested Next.js "
          'project, Node server pages, CLI script, test-named package)' + ''.join(f'\n  - {f}' for f in fails))
    go_fails = self_test_go()
    print(f"self-test {'passed' if not go_fails else 'FAILED'}: a Go tool with an HTML page in a string constant, a "
          'vendored driver tree and Xcode test targets' + ''.join(f'\n  - {f}' for f in go_fails))
    flask_fails = self_test_flask()
    print(f"self-test {'passed' if not flask_fails else 'FAILED'}: a Flask site whose UI is Jinja templates, with a "
          'JSON API and a data directory' + ''.join(f'\n  - {f}' for f in flask_fails))
    return 0 if ok and not fails and not go_fails and not flask_fails else 1

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):  # JSON on stdout may hold any character; a cp1252 console can't print it
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path, nargs='?')
    ap.add_argument('--self-test', action='store_true', help='run the built-in promise-chain test and exit')
    ap.add_argument('--packs', help='comma-separated packs (default: every pack with matching files)')
    ap.add_argument('--sarif', nargs='*', type=Path, default=[], help='SARIF files from other analysers')
    ap.add_argument('--extra', nargs='*', type=Path, default=[], help='candidate files from strict_overlay.py / contract_diff.py')
    ap.add_argument('--all-files', action='store_true',
                    help='scan route handlers and modules no UI entry point imports too (tests stay excluded)')
    ap.add_argument('--catalogue', type=Path, default=CATALOGUE)
    ap.add_argument('--exclude', action='append', default=[], metavar='DIR',
                    help='repository-relative directory not to walk (repeatable); directories of hundreds of data files '
                         'with no code are skipped without it (scope.skipped_data_dirs)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    EXCLUDE.update(e.replace('\\', '/').strip('/') for e in args.exclude)
    if args.self_test:
        return self_test()
    if args.repo is None:
        ap.error('repo is required')
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    cat = load_catalogue(args.catalogue)
    packs = cat['packs']
    wanted = set(args.packs.split(',')) if args.packs else set(packs)
    ssr = ssr_frameworks(root)
    active = []
    for p in cat['pitfalls']:
        if not p.get('regex') or p['pack'] not in wanted:
            continue
        if p.get('requires_framework') and not ssr & set(p['requires_framework']):
            continue
        q = dict(p)
        q['_rx'] = re.compile(p['regex'])
        for k in ('suppress_before', 'suppress_after', 'require_before', 'require_after', 'suppress_line'):
            if p.get(k):
                q['_' + k] = re.compile(p[k])
        active.append(q)
    exts = {e for name, pk in packs.items() if name in wanted for e in pk['extensions']}
    cands: list[dict] = []
    files_by_pack: dict[str, int] = {}
    # Scope: UI code only. Tests, route handlers (a throw is an HTTP 500, not a UI crash) and modules
    # no UI entry point imports are excluded and counted, unless --all-files.
    repo_files = list(walk_repo(root))
    g = None if args.all_files else ui_graph(root, repo_files)
    if g is None:
        note_packages(root, repo_files)
    # vendored and bundled third-party trees are not the app's code; the language census decides whether the
    # repository is mostly in a language no pack reads (a Go tool), which must be said loudly
    lang = language_census(root, tool='scan_pitfalls.py')   # shared lang_census: counts and excluded trees
    vendored = lang['excluded']
    roles: dict[str, str | None] = {}
    def role_of(rel: str) -> str | None:
        if rel not in roles:
            p = root / rel
            try:
                roles[rel] = node_role(p.read_text(encoding='utf-8', errors='replace'), p.name)                     if p.suffix in ('.js', '.mjs', '.cjs', '.ts', '.mts') and p.is_file() else None
            except OSError:
                roles[rel] = None
        return roles[rel]
    reach = set(g['reachable']) if g and g['reachable'] is not None else None
    excluded: dict[str, dict] = {}
    def why_excluded(rel: str) -> str | None:
        if under(rel, vendored):
            return 'vendored'
        kind = classify(rel)
        if kind == 'test':
            return 'tests'
        if kind == 'config':  # vite.config.ts, eslint.config.js: build tooling, never run by the UI
            return 'tool_configs'
        role = role_of(rel) if rel else None
        if role == 'server':  # node:http createServer, Express: a throw there is an HTTP 500, not a UI crash
            return 'server_code'
        if role == 'cli':  # a Node command-line script never runs in the browser
            return 'cli_scripts'
        if args.all_files:
            return None
        if kind == 'route_handler':
            return 'route_handlers'
        if reach is not None and rel.endswith(SRC_EXT) and rel not in reach:
            return 'unreachable'
        return None
    def note_excluded(reason: str, rel: str, n_cands: int, is_file: bool) -> None:
        e = excluded.setdefault(reason, {'files': 0, 'candidates': 0, 'examples': []})
        e['files'] += 1 if is_file else 0
        e['candidates'] += n_cands
        if rel not in e['examples'] and len(e['examples']) < 10:
            e['examples'].append(rel)
    embedded_docs: list[dict] = []
    web_pitfalls = [p for p in active if p['pack'] == 'web' and (not p.get('extensions') or '.js' in p['extensions'])]
    if 'web' in packs and 'web' in wanted:
        hosts = sorted({*HOST_EXT} - exts)
        for path in source_files(root, set(hosts)) if hosts else []:
            rel = path.relative_to(root).as_posix()
            if why_excluded(rel):
                continue
            found, docs = scan_embedded(path, rel, web_pitfalls)
            if docs:
                embedded_docs.extend(docs)
                files_by_pack['web (embedded HTML)'] = files_by_pack.get('web (embedded HTML)', 0) + 1
                cands.extend(found)
        for path in (f for f in repo_files if is_template_path(f.relative_to(root).as_posix())
                     or f.suffix.lower() in ('.html', '.htm')):
            rel = path.relative_to(root).as_posix()
            if why_excluded(rel):
                continue
            found, docs = scan_embedded(path, rel, web_pitfalls)
            if docs:
                embedded_docs.extend(docs)
                files_by_pack['web (embedded HTML)'] = files_by_pack.get('web (embedded HTML)', 0) + 1
                cands.extend(found)
    for path in source_files(root, exts, vendored):
        rel = path.relative_to(root).as_posix()
        reason = why_excluded(rel)
        if reason:
            note_excluded(reason, rel, 0, True)
            continue
        for name, pk in packs.items():
            if path.suffix in pk['extensions']:
                files_by_pack[name] = files_by_pack.get(name, 0) + 1
        cands.extend(scan_file(path, rel, [p for p in active if path.suffix in packs[p['pack']]['extensions']]))
    # a Flask or Django site whose templates were scanned is a server-rendered site, not "predominantly Python, not
    # analysed": the templates are its UI
    tpl_docs = sorted({d['file'] for d in embedded_docs if d['kind'] == 'template_file'})
    lang = server_rendered(lang, python_web_frameworks(root), tpl_docs, sum(files_by_pack.get(k, 0) for k in packs))
    lang_warning = language_warning(lang)
    tools = [{'tool': 'scan_pitfalls', 'status': 'ran', 'results': len(cands),
              'packs': sorted(n for n in files_by_pack if n in wanted)}]
    def in_scope(found: list[dict]) -> list[dict]:
        kept = []
        for c in found:
            reason = why_excluded(str(c.get('file') or ''))
            if reason:
                note_excluded(reason, c['file'], 1, False)
            else:
                kept.append(c)
        return kept
    if args.sarif:
        extra, info = from_sarif(args.sarif, root, cat)
        kept = in_scope(extra)
        cands.extend(kept)
        tools.extend({**t, 'status': 'ran'} for t in info)
        if len(kept) != len(extra):
            tools[-1]['excluded'] = len(extra) - len(kept)
    for path in args.extra:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'Unable to read {path}: {exc}', file=sys.stderr)
            return 2
        found = data.get('candidates') or []
        kept = in_scope(found)
        for c in kept:  # a tool that gave no snippet (older ESLint output): the source line, so snippet rules can match
            if not c.get('snippet') and c.get('file') and isinstance(c.get('line'), int):
                try:
                    c['snippet'] = (root / c['file']).read_text(encoding='utf-8', errors='replace').splitlines()[c['line'] - 1].strip()[:200]
                except (OSError, IndexError):
                    pass
        cands.extend(kept)
        tools.append({'tool': data.get('tool', path.stem), 'status': data.get('status', 'ran'),
                      'results': len(kept), 'input': str(path),
                      **({'excluded': len(found) - len(kept)} if len(kept) != len(found) else {})})
    scope = {'mode': 'all_files' if args.all_files else 'ui',
             'entry_points': len(g['entries']) if g else None,
             'reachable_files': len(reach) if reach is not None else None,
             'path_aliases': g['aliases'] if g else [],
             'unresolved_imports': g.get('unresolved', 0) if g else None,
             'warnings': ([lang_warning] if lang_warning else []) + ((g.get('warnings') or []) if g else []),
             'embedded_html': [{k: v for k, v in d.items() if k != 'script_spans'} for d in embedded_docs],
             'skipped_data_dirs': skipped_data_dirs(),
             'excluded': excluded,
             'note': ('every source file scanned (--all-files); tests and tool configs are still skipped' if args.all_files else
                      'UI code only: tests, route handlers and modules no UI entry point imports are excluded and counted '
                      'here; read the examples, and rerun with --all-files if a module is reached in a way the import '
                      'graph cannot follow (a computed import)' if reach is not None else
                      'no UI entry point recognised, so every non-test source file was scanned (route handlers, Node '
                      'server modules and command-line scripts excluded). Browser code inside HTML template literals '
                      '(pages a Node server renders) is not scanned: read it, or see recon site.server')}
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'files_scanned': files_by_pack,
        'language_coverage': lang,
        'scope': scope,
        'ssr_frameworks': sorted(ssr),
        'tools': tools,
        'note': 'Candidates are pattern-level leads, not findings. Trace each one to a realistic trigger or dismiss it.',
        'candidates': merge(cands),
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        by = {}
        for c in out['candidates']:
            by[c.get('pitfall_id') or 'unmapped'] = by.get(c.get('pitfall_id') or 'unmapped', 0) + 1
        print(f"{len(out['candidates'])} candidate(s) from {sum(files_by_pack.values())} file(s) -> {args.out}")
        for w in scope['warnings']:
            print(f'  WARN: {w}')
        for k, e in excluded.items():
            print(f"  excluded ({k}): {e['files']} file(s), {e['candidates']} tool candidate(s), e.g. {', '.join(e['examples'][:3])}")
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f'  {v:4}  {k}')
    else:
        print(text)
        for w in scope['warnings'][:1] if lang_warning else []:
            print(f'WARN: {w}', file=sys.stderr)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
