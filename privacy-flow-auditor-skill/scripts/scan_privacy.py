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

Identity SDKs: where MSAL, oidc-client-ts or the Auth0 SPA SDK keep tokens (cacheLocation, storeAuthStateInCookie,
userStore: new WebStorageStateStore({store}), or the SDK default sessionStorage when an MSAL or oidc-client-ts client
sets none) is an insecure_storage candidate (subtype auth_token_storage) with the standard OIDC scopes requested. ID
and access tokens, idTokenClaims and account claims, and variables holding a decoded token (jwtDecode,
JSON.parse(atob(...))) or one read back from storage are identity_read collection points, so a log line that names
them is a candidate, with identity_source saying where the value came from; a bearer token sent to an API is not.
File inputs, dropzones, client-side parsing (SheetJS, Papa Parse, FileReader) and files appended to FormData are
collection points with category null (what the files hold is unknown). collection_by_category counts the points.

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
import lang_census  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
SINKS = json.loads((DATA / 'sinks.json').read_text(encoding='utf-8'))
LANG = {'.py': 'py', '.js': 'js', '.mjs': 'js', '.cjs': 'js', '.jsx': 'js', '.ts': 'ts', '.tsx': 'ts', '.mts': 'ts', '.cts': 'ts',
        '.html': 'html', '.htm': 'html', '.java': 'java', '.kt': 'kt', '.swift': 'swift', '.m': 'm', '.rb': 'rb', '.go': 'go'}
# the languages lang_census counts that this scan reads: sinks, collection points and sends (Java, Kotlin, Swift and
# Objective-C get logging and storage sinks only, besides manifests and usage strings)
ANALYSED = {'Python', 'JavaScript', 'TypeScript', 'Go', 'Ruby', 'Java', 'Kotlin', 'Swift', 'Objective-C', 'Vue', 'Svelte'}
SKIP = {'node_modules', '.git', 'dist', 'build', 'out', 'venv', '.venv', '__pycache__', 'Pods', '.next', '.nuxt', 'coverage',
        'bower_components', '.svelte-kit', '.parcel-cache', '.turbo', '.cache', '.tox', '.mypy_cache', '.pytest_cache'}
TEST_DIRS = {'__tests__', '__mocks__', '__snapshots__', 'test', 'tests', 'spec', 'specs', 'e2e', 'cypress', 'playwright'}
# A directory named like tests is a product package when it holds a package manifest (a workspace package named
# e2e), and one named after a test tool (playwright, cypress) is test code only when it holds tests: the rule the
# shared diff_scope.py uses for test layouts, copied here because this skill does not carry diff_scope.py.
PACKAGE_MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml',
                     'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json', 'pubspec.yaml')
TOOL_NAMED_DIRS = {'playwright', 'cypress'}
TEST_EVIDENCE = re.compile(r'[._-](test|spec|e2e|cy)\.[cm]?[jt]sx?$|^(playwright|cypress)\.config\.[cm]?[jt]s$|^test_.*\.py$|_test\.py$', re.I)
# Node processes: a package with a "bin" (a CLI) or a file that reads Node built-ins; their sends never pass through
# a browser either
NODE_FILE = re.compile(r'''^#!.*\bnode\b|\bfrom\s+["']node:|\brequire\(\s*["']node:|\bprocess\.argv\b|\bos\.homedir\(\)''', re.M)
UI_FRAMEWORKS = {'react', 'react-dom', 'vue', 'svelte', 'next', '@angular/core', 'preact', 'solid-js', 'astro', 'nuxt', 'lit'}
BROWSER_FILE = re.compile(r'\bdocument\.|\bwindow\.|\blocalStorage\b|["\']use client["\']')
TEST_FILE = re.compile(r'[._-](test|spec|e2e)\.(js|jsx|ts|tsx|mjs|cjs|mts|cts|py|rb)$|^test_.*\.py$|_test\.(py|go)$', re.I)
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
# identity tokens and claims read from an identity SDK or browser storage: collected identity data, so a log or
# analytics line that names them is a candidate. A bearer token sent to an API is how sign-in works: http sinks
# naming only token fields are not candidates.
TOKEN_FIELDS = {'idTokenClaims', 'idToken', 'rawIdToken', 'id_token', 'accessToken', 'access_token'}
CLAIM_CATEGORY = {'username': 'identity', 'preferred_username': 'identity', 'upn': 'identity', 'name': 'identity', 'given_name': 'identity',
                  'family_name': 'identity', 'email': 'contact', 'oid': 'online_identifier'}
AUTH_LOCATION = re.compile(r'''cacheLocation\s*:\s*(?:["'`](\w+)["'`]|(?:msal\.)?BrowserCacheLocation\.(\w+))|store\s*:\s*(?:window\.)?(localStorage|sessionStorage)|(storeAuthStateInCookie)''')
SCOPES = re.compile(r'''\bscopes?\s*:\s*(\[[^\]]{0,400}\]|["'`][^"'`]{0,200}["'`])''')
FILE_KINDS = ('file_upload', 'file_parse', 'form_data_upload')
# Go (and other compiled command-line tools): a send to a host, the opt-out it reads, and what identifies the machine
GO_SEND = re.compile(r'\bhttp\.(Post|PostForm|Get|Head|NewRequest|NewRequestWithContext)\(|\.Do\(\s*req|\bhttp\.DefaultClient\b|\bresty\.New\(')
GO_ENV = re.compile(r'\bos\.(?:Getenv|LookupEnv)\(\s*"([A-Za-z0-9_]+)"')
OPT_OUT_ENV = re.compile(r'TELEMETRY|TRACK|ANALYTICS|METRICS|USAGE|OPT_?OUT|NO_UPDATE|UPDATE_CHECK|STATS|DIAGNOSTIC|CRASH_?REPORT', re.I)
GO_IDENTIFIERS = {'os.Hostname': r'\bos\.Hostname\(', 'user.Current': r'\buser\.Current\(', 'net.Interfaces': r'\bnet\.Interfaces\(|\.HardwareAddr\b',
                  'machineid': r'\bmachineid\.', 'os.Getuid': r'\bos\.Getuid\(', 'uuid.New': r'\buuid\.(New|NewString|NewRandom)\('}
GO_HASH = re.compile(r'\b(sha256|sha1|sha512|md5|hmac|fnv)\.')
TELEMETRY_PATH = re.compile(r'(^|/)(telemetry|analytics|metrics|usage|tracking|stats|crash(report)?s?)(/|$)', re.I)
GO_FLAG = re.compile(r'\b(?:flag|\w+\.(?:Persistent)?Flags\(\))\.(?:String|StringP|StringVar|StringVarP)\(\s*(?:&[\w.]+\s*,\s*)?"(?P<name>[\w-]+)"')
NOT_RECIPIENT = re.compile(r'(^|\.)(localhost|example\.(com|org|net)|[\w-]+\.(test|invalid|local|localhost|example))$|^127\.|^0\.0\.0\.0$'
                           r'|(^|\.)(w3\.org|xmlsoap\.org|json-schema\.org)$|^schemas\.', re.I)


def sdk_like(host: str, path: str) -> bool:
    """False for a link that only names a vendor: documentation hosts, and profile or share pages on social hosts."""
    if DOC_HOST.match(host):
        return False
    if registrable(host) in SOCIAL:
        return bool(SDK_PATH.search(path or ''))
    return True


def is_server_file(rel: str, text: str, cli_packages: set[str] = frozenset()) -> bool:
    """Server or Node-process code: route handlers, middleware and server configs, "use server" modules, and Node
    code that no browser runs (a file reading Node built-ins, or any file of a package that declares a "bin")
    unless the file itself touches the DOM."""
    if SERVER_PATH.search(rel) or re.match(r'''\s*["']use server["']''', text):
        return True
    if BROWSER_FILE.search(text):
        return False
    if NODE_FILE.search(text):
        return True
    return any(p == '' or rel.startswith(p + '/') for p in cli_packages)


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


def redact_strings(v, key: str = ''):
    """v with REDACT applied to every string value except file paths."""
    if isinstance(v, str):
        return v if key in ('file', 'dir') else REDACT.redact(v)
    if isinstance(v, list):
        return [redact_strings(x, key) for x in v]
    if isinstance(v, dict):
        return {k: redact_strings(x, k) for k, x in v.items()}
    return v


def holds_tests(d: Path, depth: int = 3) -> bool:
    """A test file or test-runner config within a few levels of d (dependency and build directories skipped)."""
    try:
        entries = list(os.scandir(d))
    except OSError:
        return False
    for e in entries:
        if e.is_file() and TEST_EVIDENCE.search(e.name):
            return True
    if depth > 1:
        return any(holds_tests(Path(e.path), depth - 1) for e in entries
                   if e.is_dir(follow_symlinks=False) and e.name not in SKIP and not e.name.startswith('.'))
    return False


def test_dir(d: Path) -> bool:
    """Whether a directory with a test-like name holds test code: not when it holds a package manifest (a product
    package that happens to be called e2e or test), and for directories named after a test tool only when they hold
    test files (a folder of pages served at /playwright is product code)."""
    if any((d / m).is_file() for m in PACKAGE_MANIFESTS):
        return False
    if d.name in TOOL_NAMED_DIRS:
        return holds_tests(d)
    return True


def files(repo: Path, pruned: list, skip: set, tests: set = frozenset(), kept: list | None = None):
    """Every file under repo, pruning skipped directories while walking (each pruned directory is added to pruned).
    Directories named in tests are pruned only when test_dir() says they hold test code; the others go to kept."""
    for root, dirs, names in os.walk(repo):
        drop = set()
        for d in sorted(dirs):
            if d in skip:
                drop.add(d)
            elif d in tests:
                if test_dir(Path(root) / d):
                    drop.add(d)
                elif kept is not None:
                    kept.append((Path(root) / d).relative_to(repo).as_posix())
        pruned += [(Path(root) / d).relative_to(repo).as_posix() for d in sorted(drop) if d not in QUIET]
        dirs[:] = sorted(d for d in dirs if d not in drop)
        for n in sorted(names):
            yield Path(root) / n


def node_packages(repo: Path) -> set[str]:
    """Package directories (relative, '' for the root) whose package.json declares a "bin": command-line tools."""
    out = set()
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
        if 'package.json' in names:
            try:
                pj = json.loads((Path(root) / 'package.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            deps = {**(pj.get('dependencies') or {}), **(pj.get('peerDependencies') or {})} if isinstance(pj, dict) else {}
            if isinstance(pj, dict) and pj.get('bin') and not set(deps) & UI_FRAMEWORKS:
                rel = Path(root).relative_to(repo).as_posix()
                out.add('' if rel == '.' else rel)
    return out


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


def file_point(rel: str, line: int, name: str | None, accept: str | None, kind: str, via: str | None = None) -> dict:
    """A collection point for files the user picks or uploads: the category is unknown until someone says what the
    files hold, so it stays null and the report asks."""
    e = {'file': rel, 'line': line, 'kind': kind, 'field': name or 'file', 'category': None,
         'note': 'whatever the chosen file holds is collected: ask what the files contain (category unknown until then)'}
    if accept:
        e['accept'] = accept[:80]
    if via:
        e['via'] = via
    return e


def scope_words(text: str) -> list[str]:
    """OAuth scopes written as string literals in scope: / scopes: settings (openid, profile, email, ...); names of
    constants and spreads are not scopes and are left out."""
    out = set()
    for g in SCOPES.findall(text):
        for lit_ in re.findall(r'''["'`]([^"'`]{1,200})["'`]''', g):
            out |= {w for w in lit_.split() if re.fullmatch(r'[A-Za-z][\w.:/-]{0,59}', w)}
    return sorted(out)


def auth_storage(text: str, T, m, sid: str) -> dict:
    """Where an identity SDK keeps its tokens, from its configuration (or the SDK's default when none is set)."""
    window = text[max(0, m.start() - 200):m.end() + 600] if sid == 'auth-sdk-init' else m.group(0)
    loc = AUTH_LOCATION.search(window)
    where = None
    if loc:
        where = 'cookie (auth state)' if loc.group(4) else (loc.group(1) or loc.group(2) or loc.group(3))
        where = {'localstorage': 'localStorage', 'sessionstorage': 'sessionStorage'}.get(where.lower(), where)
    if 'UserManager' in m.group(0) or 'WebStorageStateStore' in m.group(0):
        sdk = 'oidc-client-ts'
    elif re.search(r'msal|PublicClientApplication|BrowserCacheLocation', text, re.I):
        sdk = 'MSAL'
    elif re.search(r'auth0', text, re.I):
        sdk = 'Auth0 SPA SDK'
    else:
        sdk = None
    default = sid == 'auth-sdk-init' and not loc
    if default:
        where = 'sessionStorage'  # MSAL browser and oidc-client-ts keep tokens in sessionStorage unless told otherwise
    words = scope_words(text)
    # the standard OpenID Connect scopes say which identity claims the app asks for; API scopes are only counted
    scopes = [w for w in words if w in ('openid', 'profile', 'email', 'phone', 'address', 'offline_access')]
    return {'location': where, 'sdk': sdk, 'sdk_default': default, 'scopes': scopes, 'other_scopes': len(words) - len(scopes),
            'refresh_tokens': bool(re.search(r'useRefreshTokens\s*:\s*true|offline_access', text))}


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
    t0 = time.time()
    # what the repository is written in (vendored and embedded third-party trees left out): a scan that reads only some
    # languages says so, loudly, when most of the code is in another
    cen = lang_census.census(repo, list(a.exclude))
    coverage = lang_census.language_coverage(cen, ANALYSED, tool='scan_privacy.py', what='privacy scan')
    coverage['needs_limitation'] = [coverage['dominant_unsupported_language']] if coverage['status'] == 'unsupported_dominant' else []
    collection, sinks, sdks, permissions, scripts, rum_inits, imports, skipped = [], [], [], [], [], [], [], []
    replay_inits, server_sends, wrappers, texts_meta = [], [], {}, {}
    aliases: dict[str, dict] = {}
    sink_rx = [(s, re.compile(s['pattern'], re.M), s.get('hints') or []) for s in SINKS['sinks']]
    chint = SINKS.get('collection_hints', {})
    xhint = SINKS.get('collection_hints_extra', {})

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
    test_names = set() if a.include_tests else TEST_DIRS - set(a.exclude)
    kept_dirs: list[str] = []
    cli_packages = node_packages(repo)
    go_pkgs: dict[str, dict] = {}   # package directory -> its sends, hosts, opt-out variables and identifiers
    for p in files(repo, pruned, SKIP | set(a.exclude), test_names, kept_dirs):
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
            # identity SDKs: they keep tokens (with the user's name, username and ids) in browser storage
            sdks += [{'file': rel, 'package': d, 'vendor': None, 'sdk': SINKS['auth_sdk_packages'][d], 'kind': 'identity', 'source': 'manifest'}
                     for d in deps if d in SINKS.get('auth_sdk_packages', {}) and d not in SINKS['sdk_packages']]
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
        server_file = lang in ('js', 'ts', 'py', 'rb') and is_server_file(rel, text, cli_packages)
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
                if (ty or '').lower() == 'file':
                    collection.append(file_point(rel, T.line_of(off), nm or i, jsx_attr(tag, 'accept'), 'file_upload'))
                    continue
                state = controlled_field(tag)  # value={form.email}, onChange={e => setEmail(...)}
                label = (jsx_attr(tag, 'aria-label') or jsx_attr(tag, 'label') or preceding_label(text, off, i)
                         or jsx_attr(tag, 'placeholder') or '')
                if not (nm or i or ac or label or state):
                    continue
                cat = classify(nm, i, ac, state, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(off), 'kind': 'form_field', 'field': nm or i or state or ac, 'id': i,
                                   'type': ty, 'autocomplete': ac, 'label': REDACT.redact(label)[:80], 'category': cat,
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
                if (attr(tag, 'type') or '').lower() == 'file':
                    collection.append(file_point(rel, T.line_of(m.start()), attr(tag, 'name') or attr(tag, 'id'), attr(tag, 'accept'), 'file_upload'))
                    continue
                nm, i, ty, ac = attr(tag, 'name'), attr(tag, 'id'), attr(tag, 'type'), attr(tag, 'autocomplete')
                label = labels.get(i or '', '')
                cat = classify(nm, i, ac, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'form_field', 'field': nm or i, 'id': i, 'type': ty,
                                   'autocomplete': ac, 'label': REDACT.redact(label)[:80], 'category': cat})
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
        if lang in ('js', 'ts', 'html'):
            # identity tokens and claims read in the browser (an identity SDK's account, a decoded ID token): one point per field and file
            seen_id = set()
            for m in (coll_rx['identity_read'].finditer(text) if has(text, xhint.get('identity_read')) else ()):
                fld = m.group('name') or m.group('claim')
                if fld in seen_id:
                    continue
                seen_id.add(fld)
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'identity_read', 'field': fld,
                                   'category': 'identity' if m.group('name') else CLAIM_CATEGORY[m.group('claim')],
                                   'note': 'read from an identity SDK or token: the signed-in user\'s identity'})
            for m in (coll_rx['token_alias'].finditer(text) if has(text, xhint.get('token_alias')) else ()):
                alias[m.group('var')] = 'idToken'  # a decoded token, or one read back from storage, stands for the ID token
                if 'idToken' not in seen_id:
                    seen_id.add('idToken')
                    collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'identity_read', 'field': 'idToken', 'category': 'identity',
                                       'note': f'{m.group("var")} holds a decoded or stored identity token'})
        # files the user picks: whatever they hold is collected (client-side parsing, uploads to an API)
        for key, kind_ in (('file_input', 'file_parse'), ('form_data_file', 'form_data_upload')):
            for m in (coll_rx[key].finditer(text) if has(text, xhint.get(key)) else ()):
                nm = m.groupdict().get('name')
                k2 = 'file_upload' if re.match(r'(useDropzone|request\.files|UploadFile)', m.group(0).lstrip('.')) else kind_
                collection.append(file_point(rel, T.line_of(m.start()), nm, None, k2, m.group(0).strip()[:40]))
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
                if sk['id'] in ('auth-token-storage', 'auth-sdk-init'):
                    au = sinks[-1]['_auth'] = auth_storage(text, T, m, sk['id'])
                    # the identity provider hands the app the user's identity: a collection point of its own
                    collection.append({'file': rel, 'line': n, 'kind': 'identity_provider', 'field': 'idToken', 'category': 'identity',
                                       'sdk': au['sdk'], 'scopes': au['scopes'],
                                       'note': 'ID token claims from the identity provider (name, username, directory ids; email with the email scope)'})
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
        if lang == 'go':
            # command-line flags that take personal data (--email): collection points, so a log line naming them counts
            for m in (GO_FLAG.finditer(text) if 'tring' in text else ()):
                nm = m.group('name').replace('-', '_')
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'cli_flag', 'field': nm, 'category': classify(nm)})
                v = re.match(r'^\s*(\w+)\s*:?=', T.line(T.line_of(m.start())))
                if v and v.group(1) != nm:
                    alias[v.group(1)] = nm      # email := flag.String("email", ...) and user := flag.String("email", ...)
            pkg = go_pkgs.setdefault(Path(rel).parent.as_posix(), {'hosts': [], 'sends': [], 'env': set(), 'ids': set(), 'hash': False})
            for m in (URL_LIT.finditer(text) if '//' in text else ()):
                host = (urlparse('//' + m.group(1)).hostname or m.group(1)).lower()
                if not NOT_RECIPIENT.search(host) and not DOC_HOST.match(host):
                    pkg['hosts'].append((host, rel, T.line_of(m.start())))
            pkg['sends'] += [f'{rel}:{T.line_of(m.start())}' for m in GO_SEND.finditer(text)]
            pkg['env'] |= {m.group(1) for m in GO_ENV.finditer(text)}
            pkg['ids'] |= {k for k, rx in GO_IDENTIFIERS.items() if re.search(rx, text)}
            pkg['hash'] = pkg['hash'] or bool(GO_HASH.search(text))
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
    id_points: dict = {}
    for c in collection:
        if c['kind'] in ('identity_read', 'identity_provider'):
            id_points.setdefault(c['field'], []).append(c)
    all_scopes = sorted({x for s in sinks for x in (s.get('_auth') or {}).get('scopes', [])} |
                        {x for t in texts.values() if 'scope' in t for x in scope_words(t) if x in ('openid', 'profile', 'email', 'offline_access')})
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
        if s['sink'] in ('auth-token-storage', 'auth-sdk-init'):
            au = s['_auth']
            if s['sink'] == 'auth-sdk-init' and any(x['sink'] == 'auth-token-storage' for x in sinks):
                continue  # the explicit cache configuration is the candidate
            if (au['location'] or '').lower().startswith('memory'):
                continue  # tokens kept in memory only
            scopes = au['scopes'] or all_scopes
            au = dict(au, scopes=scopes)
            cats_ = {'identity', 'online_identifier'} | ({'contact'} if 'email' in scopes else set())
            where = au['location'] or 'browser storage'
            cand(type='insecure_storage', subtype='auth_token_storage', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[],
                 categories=sorted(cats_), confidence='pattern', vendor=None,
                 note=f'{au["sdk"] or "an identity SDK"} keeps ID and access tokens in {where}'
                      + (' (the SDK default: no cache location is set)' if au['sdk_default'] else '')
                      + ': the ID token carries the signed-in user\'s name, username and directory ids (email with the email scope), and any '
                        'script running on the origin can read it' + (' after the tab closes too' if where == 'localStorage' else ''),
                 facts=au)
            continue
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
            if s['kind'] == 'http' and set(named) <= TOKEN_FIELDS:
                continue  # a bearer token sent to an API is how sign-in works, not a disclosure
            typ = {'log': 'personal_data_in_logs', 'storage': 'insecure_storage', 'replay': 'session_replay_unmasked'}.get(s['kind'], 'personal_data_to_third_party')
            cats = sorted({fields.get(f) or ident[f] for f in named})
            if typ == 'personal_data_in_logs' and any(c in CATS['special'] for c in cats):
                typ = 'sensitive_category_flow'
            extra = {}
            if s.get('end_line'):
                extra['end_line'] = s['end_line']
            if s.get('wrapper_of'):
                extra['wrapper_of'] = s['wrapper_of']
            src_ = {}
            for f in named:  # where an identity value came from (an identity SDK, a decoded or stored token)
                pts = id_points.get(f) or []
                pt = next((p for p in pts if p['file'] == s['file']), None) or (pts[0] if pts else None)
                if pt:
                    src_[f] = f'{pt["file"]}:{pt["line"]} ({pt["kind"]})'
            if src_:
                extra['identity_source'] = src_
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
             side='server', note='a vendor endpoint called from server or Node code (a route handler, a server module, or a command-line '
             'tool such as a package with a "bin" sending telemetry): a browser capture never sees these requests, and the browser '
             'consent choice does not stop them unless the server checks it; trace what the payload carries')
    # a Go package that sends to a host: a call home when it reads an opt-out variable, sits in a telemetry package, or
    # reads what identifies the machine; otherwise a vendor host is a server_side_send, and the rest are listed for review
    outbound_hosts = []
    for d, pkg in sorted(go_pkgs.items()):
        if not pkg['sends'] or not pkg['hosts']:
            continue
        opt_out = sorted(v for v in pkg['env'] if OPT_OUT_ENV.search(v))
        callhome = bool(opt_out or TELEMETRY_PATH.search(d) or pkg['ids'])
        seen_h = set()
        for host, rel_, ln in pkg['hosts']:
            if host in seen_h:
                continue
            seen_h.add(host)
            vendor = VENDOR_DOMAINS.get(registrable(host))
            excerpt = REDACT.redact(texts_meta[rel_][1].line(ln).strip())[:160]
            if callhome:
                cand(type='telemetry', subtype='call_home', file=rel_, line=ln, host=host, vendor=vendor, side='client', excerpt=excerpt,
                     fields=[], categories=['online_identifier'] if pkg['ids'] else [], confidence='pattern',
                     facts={'package': d, 'sends_at': pkg['sends'][:5], 'opt_out_env': opt_out, 'identifiers': sorted(pkg['ids']),
                            'hashing': pkg['hash']},
                     note="a command-line tool or service sends to this host from the user's machine: check whether it is on by default "
                          '(an opt-out variable as the only choice), what the payload carries, and whether an update check honours the '
                          'same opt-out. Point it at a loopback sink to observe it (SKILL.md, command-line tools)')
            elif vendor:
                cand(type='personal_data_to_third_party', subtype='server_side_send', file=rel_, line=ln, vendor=vendor, host=host,
                     excerpt=excerpt, fields=[], categories=[], confidence='pattern', side='server',
                     note='a vendor endpoint called from Go code: a browser capture never sees it; trace what the payload carries')
            else:
                outbound_hosts.append({'host': host, 'file': rel_, 'line': ln, 'package': d})
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
    # excess collection is about what a form asks for; identity read from a token or SDK is not asked of the user
    used = dict.fromkeys((f for f in fields if any(c.get('field') == f and c['kind'] not in ('identity_read', 'identity_provider')
                                                   for c in collection)), False)
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
        x.pop('_auth', None)
    skipped += [{'file': d + '/', 'bytes': None, 'reason': 'test directory skipped (--include-tests scans it)'
                 if d.rsplit('/', 1)[-1] in test_names else 'directory skipped (dependencies, build output or --exclude)'} for d in pruned]
    if tests_skipped:
        skipped.append({'file': '*.test.* / *.spec.* / test_*.py', 'bytes': None, 'reason': f'{tests_skipped} test file(s) skipped (--include-tests scans them)'})
    # in an internal business app most collection points are business parameters with no personal-data category
    by_cat: dict = {}
    for c in collection:
        by_cat[c.get('category') or 'none'] = by_cat.get(c.get('category') or 'none', 0) + 1
    by_cat = dict(sorted(by_cat.items()))
    out = {'repo': repo.as_posix(), 'files_scanned': n_files, 'seconds': round(time.time() - t0, 1), 'skipped': skipped,
           'collection': collection, 'collection_by_category': by_cat, 'sinks': sinks, 'sdks': sdks, 'permissions': permissions,
           'test_named_dirs_scanned': [{'dir': d + '/', 'reason': 'holds a package manifest (a product package)' if any(
               (repo / d / m).is_file() for m in PACKAGE_MANIFESTS) else 'named after a test tool but holds no test files'} for d in kept_dirs],
           'wrappers': sorted(wrappers.values(), key=lambda w: (w['file'] or '', w['line'] or 0, w['name'])), 'candidates': candidates,
           'outbound_hosts': outbound_hosts, 'language_coverage': coverage}
    # the validator rejects any email, phone or card-like value in the evidence: redact every remaining string
    # (labels, placeholders, notes) the same way, whatever pattern put it there; paths are left alone
    for k in ('collection', 'sinks', 'sdks', 'permissions', 'wrappers', 'candidates'):
        out[k] = redact_strings(out[k])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if coverage.get('warning'):
        print(f'WARNING: {coverage["warning"]}', file=sys.stderr)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{n_files} file(s) in {out["seconds"]}s: {len(collection)} collection point(s), {len(sinks)} sink(s), {len(sdks)} SDK reference(s), '
          f'{len(candidates)} candidate(s) -> {a.out}')
    print('  collection points by category: ' + ', '.join(f'{k} {v}' for k, v in by_cat.items())
          + ('  (category none: not personal data by name; triage only what the code shows holds personal data)' if by_cat.get('none') else ''))
    files_ = [c for c in collection if c['kind'] in FILE_KINDS]
    if files_:
        print(f'  {len(files_)} file upload or parsing point(s): ask what the files contain ' + ', '.join(f'{c["file"]}:{c["line"]}' for c in files_[:4]))
    if skipped:
        print(f'  skipped {len(skipped)} file(s) (listed under "skipped"): ' + ', '.join(f'{x["file"]} ({x["reason"].split(" (")[0]})' for x in skipped[:5])
              + (' ...' if len(skipped) > 5 else ''))
    if kept_dirs:
        print(f'  scanned {len(kept_dirs)} test-named directorie(s) as product code (a package manifest, or no test files): '
              + ', '.join(kept_dirs[:5]) + (' ...' if len(kept_dirs) > 5 else ''))
    hosts = sorted({(x['host'], x.get('vendor') or '-', x['source']) for x in sdks if x.get('host')})
    for h, v, src in hosts:
        print(f'  script host {h} ({v}, {src})')
    for c in candidates:
        print(f'  {c["id"]} {c["type"]:30} {c["file"]}:{c["line"]} {c.get("fields")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
