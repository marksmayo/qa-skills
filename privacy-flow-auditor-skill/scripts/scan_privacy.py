#!/usr/bin/env python3
"""Static privacy scan: collection points, sinks, SDK inventory, mobile permissions, and candidate flows.

    python scan_privacy.py <repo> --out run/static.json [--exclude DIR] [--max-bytes N] [--semgrep semgrep.sarif]

Collection points: HTML inputs (name, id, type, autocomplete, label), JSX form elements in React code (input,
textarea, select and the common component-library fields such as MUI TextField, with name, id, type,
autoComplete, label and placeholder) and form-library registrations (register('email')), server-side form reads, and device APIs
and permissions (geolocation, contacts, camera, advertising id; AndroidManifest permissions; Info.plist usage
strings), each classified with data/categories.json. Sinks: data/sinks.json (logging, analytics, identify,
session replay, error trackers, storage, outbound HTTP). A candidate flow is a sink line that names a
collected field (or a variable holding it) - a pattern, for the agent to trace to a full path. Replay
masking switched off, a Datadog RUM init that records replays without setting defaultPrivacyLevel, client-side
persistence of a whole data cache (Apollo, React Query, Redux persist to localStorage or IndexedDB) and
error-tracker PII settings are candidates on their own. SDKs come from package manifests, from script hosts in
HTML (<script src>), from scripts injected by JavaScript (el.src = "https://...", setAttribute('src', ...), a URL
literal in a file that creates a script element), from URL literals on a listed vendor's domain
(data/vendors.json, such as the gtag.js and adsbygoogle.js loader URLs), and from importScripts() hosts.

A sink line names a collected field when the field appears as an identifier or a key: `email`, `{email: x}`,
`data['email']`, `${email}` / `{email}` inside a template or f-string. Ordinary string text ("Enter your name")
doesn't count, and for names that are everyday property names (data/categories.json generic_property_names,
such as name, title and type) neither does a property access (`path.name`).

A sink's code view covers its call's whole argument span, so keys on the lines after `Sentry.setUser({` count.
Identify, setUser and people.set calls also count identity keys (email, $email, name, username, phone) that no
form collects. Small app functions that pass one of their parameters to a log, analytics, identify or
error-tracker call are wrappers (listed under "wrappers"; --log-sinks / --analytics-sinks name more), and calls
to them are sinks, one level deep. Controlled React inputs take their field from value={form.email} or the
setter, and their label from aria-label, a label prop or the <label> before them. Candidates note
consent_check_nearby (a consent check in the 15 lines before: a heuristic) and side: server for server code
(route handlers, middleware, *.server.*, Sentry server and edge configs, Python and Ruby); a vendor URL called
from server code is a server_side_send candidate. Sentry Replay with no masking options, or with no consent check
nearby, is a candidate. Test directories and files are skipped unless --include-tests. Social profile and
documentation links are not SDKs.

Files are read once and every pattern runs over the whole text. Directories in SKIP and --exclude are pruned.
Files larger than --max-bytes (default 2 MB) are skipped; minified or generated files (*.min.js, bundles, or
very long lines) are scanned for SDK hosts only. Both are listed in the output under "skipped".

Every candidate carries file and line; no value from the code beyond the matched line (truncated) is kept,
and string literals that look like personal data are redacted.
"""
from __future__ import annotations
import argparse, bisect, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_canaries import Matcher  # noqa: E402
from domains import registrable  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
SINKS = json.loads((DATA / 'sinks.json').read_text(encoding='utf-8'))
LANG = {'.py': 'py', '.js': 'js', '.mjs': 'js', '.cjs': 'js', '.jsx': 'js', '.ts': 'ts', '.tsx': 'ts', '.mts': 'ts', '.cts': 'ts',
        '.html': 'html', '.htm': 'html', '.java': 'java', '.kt': 'kt', '.swift': 'swift', '.m': 'm', '.rb': 'rb'}
SKIP = {'node_modules', '.git', 'dist', 'build', 'out', 'venv', '.venv', '__pycache__', 'Pods', '.next', '.nuxt', 'coverage',
        'bower_components', '.svelte-kit', '.parcel-cache', '.turbo', '.cache', '.tox', '.mypy_cache', '.pytest_cache'}
TEST_DIRS = {'__tests__', '__mocks__', '__snapshots__', 'test', 'tests', 'spec', 'specs', 'e2e', 'cypress', 'playwright'}
TEST_FILE = re.compile(r'[._-](test|spec|e2e)\.(js|jsx|ts|tsx|mjs|cjs|mts|cts|py|rb)$|^test_.*\.py$|_test\.py$', re.I)
REDACT = Matcher({'id': 'none', 'fields': {}})
GENERIC = set(CATS.get('generic_property_names', {}).get('names', []))
VENDOR_DOMAINS = {d: v['vendor'] for d, v in json.loads((DATA / 'vendors.json').read_text(encoding='utf-8'))['domains'].items()}
QUIET = {'.git', '__pycache__', '.mypy_cache', '.pytest_cache', '.tox', '.parcel-cache', '.turbo', '.cache'}  # pruned without being listed
# profile and share links on these hosts are not SDKs: only a script, pixel or collection path is
SOCIAL = {'facebook.com', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com', 'linkedin.com', 'youtube.com', 'pinterest.com'}
SDK_PATH = re.compile(r'\.m?js(?:[?#]|$)|/(tr|signals|sdk|pixel|events?|collect|track|analytics|insights)(?:[/?#.]|$)', re.I)
DOC_HOST = re.compile(r'^(docs|doc|help|support|blog|status|community|developers?|learn|www\.docs)\.', re.I)
# a consent check near a sink or an SDK init (heuristic: a note for the agent, not a verdict)
CONSENT_RX = re.compile(r'consent|cookiePreferences|cookie_preferences|cookieChoice|optedIn|opted_in|hasOptedIn|gdpr|tracking_allowed|analyticsAllowed', re.I)
# server code: route handlers, middleware, server configs; sends from here never pass through the browser
SERVER_PATH = re.compile(r'(^|/)(api|server|servers|backend|functions|lambda|routes)/|(^|/)(route|middleware|proxy|server|instrumentation)\.(ts|js|mjs|cjs|mts)$'
                         r'|\.server\.(ts|js|tsx|jsx|mjs)$|(^|/)sentry\.(server|edge)\.config\.(ts|js|mjs)$|\.py$|\.rb$', re.I)
# keys that identify a person when sent to identify / error-tracker / people calls, even when no form collects them
IDENTITY_KEYS = {'email': 'contact', 'phone': 'contact', 'name': 'identity', 'username': 'identity', 'first_name': 'identity',
                 'last_name': 'identity', 'firstName': 'identity', 'lastName': 'identity', 'ip_address': 'online_identifier'}
SERVER_SEND_HINT = re.compile(r'\bfetch\(|\baxios\b|\brequests\.|\bhttpx\.|\burlopen\(|\bhttps?\.request\(|\bgot\(|\bundici\b|Net::HTTP|Faraday')
WRAPPER_STOP = {'error', 'warn', 'info', 'debug', 'log', 'init', 'main', 'render', 'handler', 'default', 'track', 'send', 'report', 'run'}
MANIFESTS = {'requirements.txt', 'Podfile', 'build.gradle', 'build.gradle.kts', 'pubspec.yaml', 'Gemfile'}
PLATFORM = {'AndroidManifest.xml', 'Info.plist', 'PrivacyInfo.xcprivacy'}
GENERATED_NAME = re.compile(r'\.(min|bundle|chunk|prod|umd)\.(js|mjs|css)$|[.-][0-9a-f]{8,}\.(js|mjs)$|\.map$', re.I)
STRING = re.compile(r'''(?P<p>\b[rRbBuUfF]{1,2})?(?P<s>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)''', re.S)
KEYWORD = re.compile(r'[A-Za-z_$][\w$-]*')
URL_LIT = re.compile(r'''["'`](?:https?:)?//([a-z0-9.-]+\.[a-z]{2,}|\[[0-9a-f:]+\]|\d+\.\d+\.\d+\.\d+)(?::\d+)?(/[^"'`\s]*)?["'`]''', re.I)
SRC_ASSIGN = re.compile(r'''\.src\s*=\s*["'`](?:https?:)?//([^/"'`\s:]+)|\.setAttribute\(\s*["']src["']\s*,\s*["'`](?:https?:)?//([^/"'`\s:]+)''', re.I)
CREATES_SCRIPT = re.compile(r'''createElement\(\s*["']script["']\s*\)''', re.I)
IMPORT = re.compile(r'''(?:\bfrom\s+|\brequire\(\s*|\bimport\s*\(\s*)["']([@\w./-]+)["']''')
ALIAS_ELEMENT = re.compile(r'''\b(?:var|let|const)\s+(\w+)\s*=\s*document\.(?:getElementById|querySelector)\(['"]#?([\w-]+)['"]\)''')
ALIAS_VALUE = re.compile(r'\b(?:var|let|const)\s+(\w+)\s*=\s*(\w+)\.value\b')
ALIAS_PARAM = re.compile(r'''\b(?:var|let|const)\s+(\w+)\s*=\s*new URLSearchParams\([^)]*\)\.get\(['"]([\w-]+)['"]\)''')
SCRIPT_TAG = re.compile(r'''<script\b[^>]*?\bsrc\s*=\s*["']?(?:https?:)?//([^/"'\s:>?#]+)''', re.I)


def sdk_like(host: str, path: str) -> bool:
    """False for a link that only names a vendor: documentation hosts, and profile or share pages on social hosts."""
    if DOC_HOST.match(host):
        return False
    if registrable(host) in SOCIAL:
        return bool(SDK_PATH.search(path or ''))
    return True


def is_server_file(rel: str, text: str) -> bool:
    return bool(SERVER_PATH.search(rel)) or bool(re.match(r'''\s*["']use server["']''', text))


def consent_nearby(T, n: int, back: int = 15) -> bool:
    """A consent check on one of the lines before line n (an if, &&, ternary or early return mentioning consent)."""
    lo = max(1, n - back)
    for k in range(lo, n + 1):
        ln = T.line(k)
        if CONSENT_RX.search(ln) and re.search(r'\bif\b|&&|\?|\breturn\b|\bwhen\b|function\s+\w*consent', ln, re.I):
            return True
    return False


def call_span(text: str, m) -> tuple[str, int]:
    """The argument span of the call a sink match opens (balanced parentheses, across lines, up to 3000
    characters), and its end offset; empty when the match doesn't open a call."""
    k = m.group(0).rfind('(')
    if k < 0:
        return '', m.end()
    i = m.start() + k
    depth, j, stop = 0, i, min(len(text), i + 3000)
    while j < stop:
        c = text[j]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if not depth:
                return text[i:j + 1], j + 1
        j += 1
    return text[i:stop], stop


class Text:
    """A file's text with line lookup by offset."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def line_of(self, off: int) -> int:
        return bisect.bisect_right(self.starts, off)

    def line(self, n: int) -> str:
        end = self.starts[n] - 1 if n < len(self.starts) else len(self.text)
        return self.text[self.starts[n - 1]:end].rstrip('\r')


def code_view(line: str) -> str:
    """The line with string text removed: a literal that is one identifier-like word stays as that word (a key),
    a template literal or f-string keeps its interpolations, and every other literal becomes ""."""
    def sub(m):
        body, pre = m.group('s'), (m.group('p') or '').lower()
        inner = body[1:-1]
        if KEYWORD.fullmatch(inner):
            return f' {inner} '
        if body[0] == '`':
            return ' ' + ' '.join(code_view(x) for x in re.findall(r'\$\{([^{}]*)\}', inner)) + ' '
        if 'f' in pre:
            return ' ' + ' '.join(code_view(x) for x in re.findall(r'\{([^{}!:]*)', inner)) + ' '
        return '""'
    return STRING.sub(sub, line)


def names_field(view: str, field: str) -> bool:
    """True when the field appears in the code view as an identifier or key (not inside another word; not as a
    property access for an everyday property name)."""
    lead = r'(?<![\w$.-])' if field in GENERIC else r'(?<![\w$-])'
    return re.search(lead + re.escape(field) + r'(?![\w$-])', view) is not None


def classify(*names: str) -> str | None:
    """The most sensitive category whose patterns match any of the names."""
    best = None
    for n in names:
        if not n:
            continue
        low = n.lower().replace('-', '_')
        for cat, c in CATS['categories'].items():
            hit = any(low == p or low.startswith(p + '_') or low.endswith('_' + p) or (len(p) > 4 and p in low) for p in c['names']) \
                or low in c.get('autocomplete', []) or low in c.get('types', [])
            if hit and (best is None or c['sensitivity'] > CATS['categories'][best]['sensitivity']):
                best = cat
    return best


def files(repo: Path, pruned: list, skip: set):
    """Every file under repo, pruning skipped directories while walking (each pruned directory is added to pruned)."""
    for root, dirs, names in os.walk(repo):
        pruned += [(Path(root) / d).relative_to(repo).as_posix() for d in sorted(dirs) if d in skip and d not in QUIET]
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for n in sorted(names):
            yield Path(root) / n


def attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def jsx_tags(text: str, start_rx: str):
    """(offset, tag text) of each JSX element opening that matches start_rx, up to its closing '>' (braces skipped)."""
    for m in re.finditer(start_rx, text):
        i, depth, j = m.start(), 0, m.end()
        stop = min(len(text), j + 4000)
        while j < stop:
            c = text[j]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == '>' and depth == 0:
                break
            j += 1
        yield i, text[i:j + 1]


def jsx_attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*(?:\{{\s*)?["\'`]([^"\'`]*)["\'`]', tag)
    return m.group(1) if m else None


def controlled_field(tag: str) -> str | None:
    """The state a controlled React input is bound to: value={formData.email} or value={email} gives "email";
    onChange={e => setEmail(...)} gives "email"; setFormData({...formData, email: e.target.value}) gives "email"."""
    m = re.search(r'\bvalue\s*=\s*\{\s*(?:[\w$]+(?:\?)?\.)*([A-Za-z_$][\w$]*)\s*(?:\?\?[^}]*)?\}', tag)
    if m and m.group(1) not in ('value', 'undefined', 'null', 'true', 'false'):
        return m.group(1)
    m = re.search(r'\b([A-Za-z_$][\w$]*)\s*:\s*e(?:vent|v)?\.target\.value', tag)
    if m:
        return m.group(1)
    m = re.search(r'onChange\s*=\s*\{[^}]*?\bset([A-Z][\w$]*)\(', tag)
    if m and not m.group(1).startswith(('Form', 'Data', 'State', 'Values')):
        return m.group(1)[0].lower() + m.group(1)[1:]
    return None


def preceding_label(text: str, off: int, el_id: str | None) -> str | None:
    """The text of a <label> just before an input (within 400 characters) that has no htmlFor or names this input."""
    for m in reversed(list(re.finditer(r'<(label|Label|FormLabel|InputLabel)\b([^>]*)>(.*?)</\1>', text[max(0, off - 400):off], re.S))):
        target = re.search(r'''htmlFor\s*=\s*\{?["'`]([^"'`]+)''', m.group(2))
        if target and el_id and target.group(1) != el_id:
            continue
        words = re.sub(r'\{[^}]*\}|<[^>]+>', ' ', m.group(3))
        words = ' '.join(words.split())
        return words or None
    return None


def sink_entry(rel: str, T, text: str, m, n: int, sid: str, kind: str, vendor: str | None, server_file: bool) -> dict:
    """A sink at line n. Its code view covers the whole argument span of the call (an object literal spread over
    the following lines still names its keys), and records the last line when the call spans several."""
    line = T.line(n)
    span, end = call_span(text, m)
    e = {'file': rel, 'line': n, 'sink': sid, 'kind': kind, 'vendor': vendor, 'excerpt': REDACT.redact(line.strip())[:160],
         '_view': code_view(line) + ' ' + code_view(span), '_consent': consent_nearby(T, n)}
    last = T.line_of(max(m.start(), end - 1))
    if span and last > n:
        e['end_line'] = last
    if server_file:
        e['side'] = 'server'
    return e


JS_DEF = re.compile(r'(?:^|[\s;(])(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\('
                    r'|(?:^|[\s;])(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s*)?'
                    r'(?:function\b[^(\n]*\(|\([^)]*\)\s*(?::[^=>{\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)'
                    r'|^[ \t]+(?:async\s+)?(?:static\s+)?([a-z][\w$]*)\s*\([^)\n]*\)\s*(?::[^{\n]+)?\{', re.M)
PY_DEF = re.compile(r'^([ \t]*)def\s+(\w+)\s*\(', re.M)
NOT_NAMES = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'constructor', 'else', 'with'}


def find_wrappers(rel: str, lang: str, text: str, T, sink_rx) -> list[dict]:
    """App functions whose body calls a logging, analytics, identify or error-tracker sink (logSecurityEvent,
    trackEvent, a logger.warn wrapper): calls to them are sinks too, one level deep."""
    out = []
    kinds = ('log', 'analytics', 'identify', 'error_tracker')
    if lang == 'py':
        defs = []
        lines = text.splitlines()
        for m in PY_DEF.finditer(text):
            n = T.line_of(m.start())
            ind = len(m.group(1).expandtabs())
            body = []
            for ln in lines[n:n + 120]:
                if ln.strip() and len(ln) - len(ln.lstrip()) <= ind:
                    break
                body.append(ln)
            defs.append((m.group(2), n, '\n'.join(body)))
    else:
        defs = []
        for m in JS_DEF.finditer(text):
            name = m.group(1) or m.group(2) or m.group(3)
            if not name or name in NOT_NAMES:
                continue
            i = text.find('{', m.end() - 1, m.end() + 400)
            if i < 0:
                body = text[m.end():text.find('\n', m.end())]
            else:
                depth, j = 0, i
                while j < min(len(text), i + 8000):
                    depth += text[j] == '{'
                    depth -= text[j] == '}'
                    if depth == 0:
                        break
                    j += 1
                body = text[i:j + 1]
            defs.append((name, T.line_of(m.start() + (1 if text[m.start()] in ' \t;(\n' else 0)), body))
    for name, n, body in defs:
        # a wrapper is a small function (not a component or an HTTP handler) that hands one of its own parameters to the sink
        if len(name) < 4 or name in WRAPPER_STOP or name[0].isupper() or body.count('\n') > 60:
            continue
        head = text[T.starts[n - 1]:T.starts[n - 1] + 400]
        pm = re.search(re.escape(name) + r'\s*(?:=\s*(?:async\s*)?(?:function\b[^(]*)?)?\(([^)]*)\)', head)
        params = [p for p in re.findall(r'(?:^|,)\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)', pm.group(1) if pm else '') if p not in ('self', 'cls')]
        if not params:
            continue
        for v, rhs in re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*([^;\n]+)', body) + \
                re.findall(r'^\s*([A-Za-z_]\w*)\s*=\s*([^\n]+)', body, re.M):  # one step: entry = {event, ...data}
            if any(re.search(rf'(?<![\w$.]){re.escape(p)}(?![\w$])', code_view(rhs)) for p in params):
                params.append(v)
        for sk, rx, hints in sink_rx:
            if sk['kind'] not in kinds or lang not in sk['langs']:
                continue
            hit = next((m for m in rx.finditer(body) if any(re.search(rf'(?<![\w$.]){re.escape(p)}(?![\w$])', code_view(call_span(body, m)[0]))
                                                              for p in params)), None)
            if hit:
                out.append({'name': name, 'kind': sk['kind'], 'vendor': sk.get('vendor'), 'via': sk['id'], 'file': rel, 'line': n})
                break
    return out


def call_object(text: str, start: int) -> str:
    """The text of the call starting at start, up to its closing parenthesis."""
    i = text.index('(', start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '(':
            depth += 1
        elif text[j] == ')':
            depth -= 1
            if not depth:
                return text[i:j + 1]
    return text[i:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--semgrep', type=Path)
    ap.add_argument('--exclude', action='append', default=[], help='directory name to skip (third-party or generated code); repeatable')
    ap.add_argument('--max-bytes', type=int, default=2_000_000, help='skip files larger than this (0: no limit); skipped files are listed')
    ap.add_argument('--include-tests', action='store_true', help='scan test directories and test files too (skipped by default)')
    ap.add_argument('--log-sinks', default='', help='comma-separated function names that write to logs (app logging wrappers)')
    ap.add_argument('--analytics-sinks', default='', help='comma-separated function names that send analytics (app tracking wrappers)')
    a = ap.parse_args()
    repo = a.repo
    skip = SKIP | set(a.exclude) | (set() if a.include_tests else TEST_DIRS)
    t0 = time.time()
    collection, sinks, sdks, permissions, scripts, rum_inits, imports, skipped = [], [], [], [], [], [], [], []
    replay_inits, server_sends, wrappers, texts_meta = [], [], {}, {}
    aliases: dict[str, dict] = {}
    sink_rx = [(s, re.compile(s['pattern'], re.M), s.get('hints') or []) for s in SINKS['sinks']]
    chint = SINKS.get('collection_hints', {})

    def has(text: str, hints) -> bool:
        """False only when a pattern can't match: none of its hints occurs (no hints: always search)."""
        return not hints or any(h in text for h in hints)

    coll_rx = {k: re.compile(v, re.M) for k, v in SINKS['collection'].items() if k not in ('html_input', 'jsx_input')}
    html_input = re.compile(SINKS['collection']['html_input'], re.I)
    apis = [(cat, api) for cat, c in CATS['categories'].items() for api in c.get('apis', [])]
    texts = {}
    n_files = 0
    pruned: list[str] = []
    tests_skipped = 0
    for p in files(repo, pruned, skip):
        rel = p.relative_to(repo).as_posix()
        lang = LANG.get(p.suffix.lower())
        if not (lang or p.name in MANIFESTS or p.name in PLATFORM or p.name == 'package.json'):
            continue
        if lang and not a.include_tests and TEST_FILE.search(p.name):
            tests_skipped += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if a.max_bytes and size > a.max_bytes:
            skipped.append({'file': rel, 'bytes': size, 'reason': f'larger than --max-bytes {a.max_bytes:,}'})
            continue
        n_files += 1
        if p.name == 'package.json':
            try:
                pj = json.loads(p.read_text(encoding='utf-8'))
                deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
            except (ValueError, AttributeError):
                deps = {}
            sdks += [{'file': rel, 'package': d, 'vendor': SINKS['sdk_packages'][d], 'source': 'manifest'} for d in deps if d in SINKS['sdk_packages']]
            continue
        if p.name in MANIFESTS:
            t = p.read_text(encoding='utf-8', errors='replace')
            sdks += [{'file': rel, 'package': d, 'vendor': v, 'source': 'manifest'} for d, v in SINKS['sdk_packages'].items() if d in t]
            continue
        if p.name in PLATFORM:
            t = p.read_text(encoding='utf-8', errors='replace')
            tl = t.splitlines()
            for cat, api in apis:
                if api not in t:
                    continue
                for n, line in enumerate(tl, 1):
                    if api in line:
                        nxt = tl[n] if n < len(tl) else ''
                        permissions.append({'file': rel, 'line': n, 'api': api, 'category': cat,
                                            'usage_string': bool(re.search(r'<string>\s*\S', nxt)) if p.name == 'Info.plist' else None})
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        T = Text(text)
        generated = bool(GENERATED_NAME.search(p.name))
        if not generated and lang in ('js', 'ts') and size > 20000:  # a bundle: few, very long lines
            generated = max(b - a_ for a_, b in zip(T.starts, T.starts[1:] + [len(text) + 1])) > 5000
        # ---- SDK hosts: script tags, injected scripts, vendor URLs (also in generated files)
        found_hosts = set()

        def script(host, off, source):
            host = (urlparse('//' + host).hostname or host).lower()  # no port, query or fragment
            n = T.line_of(off)
            if host in found_hosts:  # each host once per file, at its first mention
                return
            found_hosts.add(host)
            scripts.append({'file': rel, 'line': n, 'host': host, 'source': source, 'vendor': VENDOR_DOMAINS.get(registrable(host))})
        low = text.lower()
        server_file = lang in ('js', 'ts', 'py', 'rb') and is_server_file(rel, text)
        if lang == 'html' and '<script' in low:
            for m in SCRIPT_TAG.finditer(text):
                script(m.group(1), m.start(), 'script_tag')
        if lang in ('js', 'ts', 'html', 'py', 'rb') and '//' in text:
            if '.src' in text or 'setattribute(' in low:
                for m in SRC_ASSIGN.finditer(text):
                    script(m.group(1) or m.group(2), m.start(), 'injected_script')
            creates = 'createelement(' in low and CREATES_SCRIPT.search(text) is not None
            for m in (URL_LIT.finditer(text) if creates or any(d in low for d in VENDOR_DOMAINS) else ()):
                host, path = m.group(1), m.group(2) or ''
                if VENDOR_DOMAINS.get(registrable(host)):
                    if lang in ('js', 'ts', 'html') and (creates and re.search(r'\.m?js(?:[?#]|$)', path) or sdk_like(host, path)):
                        script(host, m.start(), 'injected_script' if creates else 'vendor_url')
                    if server_file and SERVER_SEND_HINT.search(text):
                        server_sends.append({'file': rel, 'line': T.line_of(m.start()), 'host': host.lower(),
                                             'vendor': VENDOR_DOMAINS[registrable(host)]})
                elif creates and re.search(r'\.m?js(?:[?#]|$)|/js(?:[/?#]|$)', path):
                    script(host, m.start(), 'injected_script')
        for m in (re.finditer(r'\bimportScripts\(\s*["\'`](https?:)?//([^/"\'`:]+)', text) if 'importScripts(' in text else ()):
            script(m.group(2), m.start(), 'importScripts')
        if generated:
            skipped.append({'file': rel, 'bytes': size, 'reason': 'minified or generated (scanned for SDK hosts only)'})
            continue
        texts[rel] = text
        if lang in ('js', 'ts') and p.suffix.lower() in ('.jsx', '.tsx', '.js', '.ts') and re.search(r'<[A-Za-z]', text):
            for off, tag in jsx_tags(text, SINKS['collection']['jsx_input']):
                ty = jsx_attr(tag, 'type')
                if (ty or '').lower() in ('hidden', 'submit', 'button', 'checkbox', 'radio'):
                    continue
                nm, i, ac = jsx_attr(tag, 'name'), jsx_attr(tag, 'id'), jsx_attr(tag, 'autoComplete') or jsx_attr(tag, 'autocomplete')
                state = controlled_field(tag)  # value={form.email}, onChange={e => setEmail(...)}
                label = (jsx_attr(tag, 'aria-label') or jsx_attr(tag, 'label') or preceding_label(text, off, i)
                         or jsx_attr(tag, 'placeholder') or '')
                if not (nm or i or ac or label or state):
                    continue
                cat = classify(nm, i, ac, state, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(off), 'kind': 'form_field', 'field': nm or i or state or ac, 'id': i,
                                   'type': ty, 'autocomplete': ac, 'label': label[:80], 'category': cat,
                                   **({'state': state} if state and not (nm or i) else {})})
        for m in (IMPORT.finditer(text) if ('from' in text or 'require(' in text or 'import' in text) else ()):
            pkg = next((k for k in SINKS['sdk_packages'] if m.group(1) == k or m.group(1).startswith(k + '/')), None)
            if pkg:
                imports.append({'file': rel, 'line': T.line_of(m.start()), 'package': pkg, 'vendor': SINKS['sdk_packages'][pkg],
                                'source': 'import'})
        for m in (re.finditer(r'\b(datadogRum|DD_RUM)\.init\(', text) if '.init(' in text else ()):
            init = call_object(text, m.start())
            replay = bool(re.search(r'sessionReplaySampleRate\s*:\s*[1-9]', init) or re.search(r'startSessionReplayRecording\(', text)
                          or re.search(r'premiumSampleRate\s*:\s*[1-9]|replaySampleRate\s*:\s*[1-9]', init))
            level = re.search(r'''defaultPrivacyLevel\s*:\s*["'](\w[\w-]*)["']''', init)
            rum_inits.append({'file': rel, 'line': T.line_of(m.start()), 'replay': replay,
                              'privacy_level': level.group(1) if level else None,
                              'action_names_private': bool(re.search(r'enablePrivacyForActionName\s*:\s*true', init)),
                              'tracks_interactions': bool(re.search(r'trackUserInteractions\s*:\s*true', init)),
                              'before_send': 'beforeSend' in init, 'site': (re.search(r'''\bsite\s*:\s*["']([\w.-]+)["']''', init) or [None, None])[1],
                              'privacy_attributes': bool(re.search(r'data-dd-privacy', text))})
        if lang == 'html':
            labels = {m.group(1): re.sub(r'<[^>]+>', '', m.group(2)).strip()
                      for m in re.finditer(r'''<label[^>]*for=["']([^"']+)["'][^>]*>(.*?)</label>''', text, re.I | re.S)} if '<label' in text else {}
            for m in (html_input.finditer(text) if has(low, chint.get('html_input')) else ()):
                tag = m.group(0)
                if (attr(tag, 'type') or '').lower() in ('hidden', 'submit', 'button', 'checkbox', 'radio'):
                    continue
                nm, i, ty, ac = attr(tag, 'name'), attr(tag, 'id'), attr(tag, 'type'), attr(tag, 'autocomplete')
                label = labels.get(i or '', '')
                cat = classify(nm, i, ac, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'form_field', 'field': nm or i, 'id': i, 'type': ty,
                                   'autocomplete': ac, 'label': label[:80], 'category': cat})
        # simple aliases: var x = document.getElementById('field'); var y = x.value...  -> x and y stand for field
        alias = {}
        for m in (ALIAS_ELEMENT.finditer(text) if 'getElementById(' in text or 'querySelector(' in text else ()):
            alias[m.group(1)] = m.group(2)
        for m in (ALIAS_VALUE.finditer(text) if alias and '.value' in text else ()):
            if m.group(2) in alias:
                alias[m.group(1)] = alias[m.group(2)]
        for m in (ALIAS_PARAM.finditer(text) if 'URLSearchParams' in text else ()):
            alias[m.group(1)] = m.group(2)
        aliases[rel] = alias
        for key in ('py_form_read', 'js_value_read', 'js_register'):
            for m in (coll_rx[key].finditer(text) if has(text, chint.get(key)) else ()):
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'code_read', 'field': m.group('name'),
                                   'category': classify(m.group('name'))})
        for cat, api in apis:
            if '.' in api and api in text:
                i = text.find(api)
                while i >= 0:
                    collection.append({'file': rel, 'line': T.line_of(i), 'kind': 'device_api', 'field': api, 'category': cat})
                    i = text.find(api, T.starts[T.line_of(i)] if T.line_of(i) < len(T.starts) else len(text))
        seen_lines = set()
        for sk, rx, hints in sink_rx:
            if lang not in sk['langs'] or not has(text, hints):
                continue
            for m in rx.finditer(text):
                n = T.line_of(m.start())
                if (sk['id'], n) in seen_lines:
                    continue
                seen_lines.add((sk['id'], n))
                sinks.append(sink_entry(rel, T, text, m, n, sk['id'], sk['kind'], sk.get('vendor'), server_file))
        if lang in ('js', 'ts', 'py'):
            for w in find_wrappers(rel, lang, text, T, sink_rx):
                wrappers.setdefault(w['name'], w)
        if lang in ('js', 'ts', 'html'):
            for m in (re.finditer(r'\b(?:Sentry\.)?replayIntegration\(|\bnew\s+(?:Sentry\.)?Replay\(', text) if 'sentry' in low else ()):
                opts = call_object(text, m.start())
                n = T.line_of(m.start())
                rate = re.search(r'replaysSessionSampleRate\s*:\s*([\d.]+)', text)
                replay_inits.append({'file': rel, 'line': n, 'excerpt': REDACT.redact(T.line(n).strip())[:160],
                                     'mask_options': bool(re.search(r'\b(maskAllText|maskAllInputs|blockAllMedia|mask|block|unmask|unblock|maskFn)\s*:', opts)),
                                     'session_sample_rate': float(rate.group(1)) if rate else None,
                                     'consent_check_nearby': consent_nearby(T, n), 'side': 'server' if server_file else 'client'})
        texts_meta[rel] = (lang, T, server_file)
    # calls to app wrappers around a sink (found above, or named with --log-sinks / --analytics-sinks), one level deep
    for opt, kind in ((a.log_sinks, 'log'), (a.analytics_sinks, 'analytics')):
        for name in filter(None, (x.strip() for x in opt.split(','))):
            wrappers[name] = {'name': name, 'kind': kind, 'vendor': None, 'via': 'option', 'file': None, 'line': None}
    for name, w in sorted(wrappers.items()):
        call = re.compile(r'(?<![\w$])(?:[\w$]+\.)?' + re.escape(name) + r'\s*\(')
        for rel, text in texts.items():
            lang, T, server_file = texts_meta[rel]
            if name not in text or lang not in ('js', 'ts', 'py', 'html'):
                continue
            for m in call.finditer(text):
                n = T.line_of(m.start())
                if (rel == w['file'] and n == w['line']) or re.search(r'(function\s*\*?\s*|def\s+)$', text[max(0, m.start() - 12):m.start()]):
                    continue  # the definition itself
                if re.match(r'\([^)]*\)\s*(?::[^{\n]+)?\{', text[m.end() - 1:m.end() + 200]):
                    continue  # another method definition with the same name
                e = sink_entry(rel, T, text, m, n, f'wrapper:{name}', w['kind'], w['vendor'], server_file)
                e['wrapper_of'] = f'{w["file"]}:{w["line"]} ({w["via"]})' if w['file'] else 'named with --log-sinks/--analytics-sinks'
                sinks.append(e)
    order = {k['id']: i for i, k in enumerate(SINKS['sinks'])}
    sinks.sort(key=lambda x: (x['file'], x['line'], order.get(x['sink'], len(order)), x['sink']))
    collection.sort(key=lambda x: (x['file'], x['line']))
    # fields known from collection points; variables in the same file that hold them are named after them here
    fields = {c['field']: c['category'] for c in collection if c.get('field') and c.get('category')}
    candidates = []

    def cand(**kw):
        kw['id'] = f'S{len(candidates) + 1:03d}'
        kw['source'] = 'static'
        candidates.append(kw)
        if 'sink' in kw:
            seen_sink[(kw['sink'], kw.get('excerpt'))] = kw

    seen_sink = {}
    for s in sinks:
        line = s['excerpt']
        if (s['sink'], line) in seen_sink:
            seen_sink[(s['sink'], line)].setdefault('also_at', []).append(f'{s["file"]}:{s["line"]}')
            continue
        view = s['_view']
        names = {f for f in fields if names_field(view, f)}
        names |= {f for v, f in aliases.get(s['file'], {}).items() if f in fields and re.search(rf'(?<![\w$.]){re.escape(v)}(?![\w$])', view)}
        if s['sink'] == 'cache-persist':
            same = next((c for c in candidates if c.get('sink') == 'cache-persist' and c['file'] == s['file']), None)
            if same:
                same.setdefault('also_at', []).append(f'{s["file"]}:{s["line"]}')
                continue
            cand(type='insecure_storage', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note='a whole client data cache is persisted to device storage: every query result it holds is '
                 'kept at rest; trace which personal data the queries return, whether it is encrypted, and whether it is cleared at sign-out')
        elif s['sink'] == 'datadog-rum-init':
            continue  # judged from the whole init call below
        elif s['sink'] == 'replay-masking-off':
            cand(type='session_replay_unmasked', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note='input masking switched off in a session-replay configuration')
        elif s['sink'] == 'default-pii':
            on = bool(re.search(r'[:=]\s*(true|True)', line))
            cand(type='personal_data_to_third_party', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note=f'error tracker default PII {"on" if on else "off"}', facts={'default_pii': on,
                 'scrubber': bool(re.search(r'beforeSend|before_send', line))})
        else:
            # identify, people.set and error-tracker user calls: identity keys count even when no form collects them
            ident = {}
            if s['kind'] in ('identify', 'error_tracker') or re.search(r'identify|setUser|set_user|people\.set|setUserProperties|register\(', view):
                ident = {k: c for k, c in IDENTITY_KEYS.items() if names_field(view, k) or re.search(r'\$' + k + r'\b', view)}
            named = sorted(names | set(ident))
            if not named:
                continue
            typ = {'log': 'personal_data_in_logs', 'storage': 'insecure_storage', 'replay': 'session_replay_unmasked'}.get(s['kind'], 'personal_data_to_third_party')
            cats = sorted({fields.get(f) or ident[f] for f in named})
            if typ == 'personal_data_in_logs' and any(c in CATS['special'] for c in cats):
                typ = 'sensitive_category_flow'
            extra = {}
            if s.get('end_line'):
                extra['end_line'] = s['end_line']
            if s.get('wrapper_of'):
                extra['wrapper_of'] = s['wrapper_of']
            if s['kind'] in ('analytics', 'identify', 'replay', 'error_tracker', 'http'):
                extra['consent_check_nearby'] = s['_consent']
            if s.get('side') == 'server' and typ in ('personal_data_to_third_party', 'session_replay_unmasked'):
                extra.update(side='server', note='sent from server code: a browser capture never sees it, and the visitor\'s consent '
                             'choice in the browser does not stop it unless the server checks it')
            cand(type=typ, file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=named, categories=cats, confidence='pattern',
                 vendor=s.get('vendor'), **extra)
    for r in replay_inits:
        if r['side'] != 'client':
            continue
        facts = {k: r[k] for k in ('mask_options', 'session_sample_rate', 'consent_check_nearby')}
        if not r['mask_options']:
            cand(type='session_replay_unmasked', subtype='sdk_default_masking', file=r['file'], line=r['line'], sink='sentry-replay', vendor='Sentry',
                 excerpt=r['excerpt'], fields=[], categories=[], confidence='pattern',
                 note='Sentry Session Replay with no masking options: the SDK version\'s default applies (current versions mask all text '
                      'and inputs and block media). Check the installed version, and a replay, before relying on it',
                 facts=dict(facts, masked_by_default=True))
        if not r['consent_check_nearby'] and r['session_sample_rate'] != 0:
            cand(type='tracking_before_consent', subtype='replay_before_consent', file=r['file'], line=r['line'], sink='sentry-replay',
                 vendor='Sentry', excerpt=r['excerpt'], fields=[], categories=[], confidence='pattern',
                 note='session replay is set up with no consent check nearby, so it may start recording before any choice; the live '
                      'capture decides', facts=facts)
    seen_sends = set()
    for x in server_sends:
        if (x['file'], x['vendor']) in seen_sends:
            continue
        seen_sends.add((x['file'], x['vendor']))
        cand(type='personal_data_to_third_party', subtype='server_side_send', file=x['file'], line=x['line'], vendor=x['vendor'], host=x['host'],
             excerpt=REDACT.redact(texts_meta[x['file']][1].line(x['line']).strip())[:160], fields=[], categories=[], confidence='pattern',
             side='server', note='a vendor endpoint called from server code: a browser capture never sees these requests, and the browser '
             'consent choice does not stop them unless the server checks it; trace what the payload carries')
    for r in rum_inits:
        if r['replay'] and r['privacy_level'] in (None, 'allow'):
            how = 'set to allow' if r['privacy_level'] else 'not set, so the SDK version\'s default applies'
            cand(type='session_replay_unmasked', file=r['file'], line=r['line'], sink='datadog-rum-init', vendor='Datadog',
                 excerpt=f'datadogRum.init: session replay on, defaultPrivacyLevel {how}', fields=[], categories=[], confidence='pattern',
                 note='session replay records pages as users see them; with no explicit privacy level, text and inputs may be recorded. '
                      'Check the SDK version\'s default and the replays themselves',
                 facts={k: r[k] for k in ('privacy_level', 'action_names_private', 'tracks_interactions', 'before_send', 'site', 'privacy_attributes')})
        elif r['tracks_interactions'] and not r['action_names_private'] and not r['before_send']:
            cand(type='personal_data_to_third_party', file=r['file'], line=r['line'], sink='datadog-rum-init', vendor='Datadog',
                 excerpt='datadogRum.init: user interactions tracked without enablePrivacyForActionName or beforeSend', fields=[],
                 categories=[], confidence='pattern', note='click action names are taken from on-screen text, which can include personal data',
                 facts={k: r[k] for k in ('privacy_level', 'action_names_private', 'tracks_interactions', 'before_send', 'site')})
    for p in permissions:
        if p['usage_string'] is False:
            cand(type='missing_usage_description', file=p['file'], line=p['line'], api=p['api'], categories=[p['category']], fields=[], confidence='pattern')
    used = dict.fromkeys(fields, False)
    if fields:
        any_field = re.compile(r'\b(' + '|'.join(sorted(map(re.escape, fields), key=len, reverse=True)) + r')\b')
        for r, t in texts.items():
            if not r.endswith(('.html', '.htm')):
                for m in any_field.finditer(t):
                    used[m.group(1)] = True
    for f, u in used.items():
        if not u:
            c = next(c for c in collection if c.get('field') == f)
            cand(type='excess_collection', file=c['file'], line=c['line'], fields=[f], categories=[fields[f]], confidence='pattern',
                 note='collected, but no code outside the form mentions the field (agent judgment)')
    if a.semgrep:
        import sarif
        for r in sarif.read(a.semgrep, repo):
            cand(type='personal_data_to_third_party', file=r['file'], line=r['line'], sink=f'semgrep:{r["rule_id"]}', excerpt=REDACT.redact(r['snippet'])[:160],
                 fields=[], categories=[], confidence='pattern', note=r['message'][:200])
    sdks += sorted(scripts, key=lambda x: (x['file'], x['line']))
    # code imports add an SDK only where no manifest declared it (a service worker or a vendored bundle)
    declared = {x.get('package') for x in sdks}
    seen_imports = set()
    for x in imports:
        if x['package'] not in declared and (x['package'], x['file']) not in seen_imports:
            seen_imports.add((x['package'], x['file']))
            sdks.append(x)
    for x in sinks:
        x.pop('_view', None)
        x.pop('_consent', None)
    skipped += [{'file': d + '/', 'bytes': None, 'reason': 'test directory skipped (--include-tests scans it)'
                 if d.rsplit('/', 1)[-1] in TEST_DIRS else 'directory skipped (dependencies, build output or --exclude)'} for d in pruned]
    if tests_skipped:
        skipped.append({'file': '*.test.* / *.spec.* / test_*.py', 'bytes': None, 'reason': f'{tests_skipped} test file(s) skipped (--include-tests scans them)'})
    out = {'repo': repo.as_posix(), 'files_scanned': n_files, 'seconds': round(time.time() - t0, 1), 'skipped': skipped,
           'collection': collection, 'sinks': sinks, 'sdks': sdks, 'permissions': permissions,
           'wrappers': sorted(wrappers.values(), key=lambda w: (w['file'] or '', w['line'] or 0, w['name'])), 'candidates': candidates}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{n_files} file(s) in {out["seconds"]}s: {len(collection)} collection point(s), {len(sinks)} sink(s), {len(sdks)} SDK reference(s), '
          f'{len(candidates)} candidate(s) -> {a.out}')
    if skipped:
        print(f'  skipped {len(skipped)} file(s) (listed under "skipped"): ' + ', '.join(f'{x["file"]} ({x["reason"].split(" (")[0]})' for x in skipped[:5])
              + (' ...' if len(skipped) > 5 else ''))
    hosts = sorted({(x['host'], x.get('vendor') or '-', x['source']) for x in sdks if x.get('host')})
    for h, v, src in hosts:
        print(f'  script host {h} ({v}, {src})')
    for c in candidates:
        print(f'  {c["id"]} {c["type"]:30} {c["file"]}:{c["line"]} {c.get("fields")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
