#!/usr/bin/env python3
"""Next.js entry points and their auth, for recon.py.

Route handlers (app/**/route.ts and pages/api), server actions ('use server') and middleware
(middleware.ts, or proxy.ts from Next 16) are found, each exported handler is cut out of its file
by bracket matching, and its body is checked for:

- a session or guard call: getServerSession, auth(), getToken, currentUser and similar, or a call
  to a local helper whose own body makes one (one level of helpers, found across the repository);
- an admin check;
- a machine credential: a webhook signature check, a cron secret, an API key header, or a
  single-purpose token from the URL;
- middleware: whether a middleware/proxy matcher (and any protected-prefix list in it) covers the path.

A handler with none of these is reported `public` with the reason, which is the lead the review
needs ("no auth call in this handler"). Everything is regex-level: the agent confirms by reading.
"""
from __future__ import annotations
import re

HTTP = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
ROUTE_FILE = re.compile(r'(?:^|/)app/(.*?)/?route\.(?:ts|js|tsx|jsx|mjs)$')
PAGES_API = re.compile(r'(?:^|/)pages/(api/.*)\.(?:ts|js|tsx|jsx)$')
MIDDLEWARE_FILE = re.compile(r'^(?:src/)?(middleware|proxy)\.(?:ts|js|mjs)$')
USE_SERVER = re.compile(r'^\s*(?:(?://[^\n]*|/\*.*?\*/)\s*)*["\']use server["\'];?', re.S)
SERVER_ONLY = re.compile(r'^\s*import\s+["\']server-only["\']', re.M)
GUARD_CALL = re.compile(r'\bgetServerSession\s*\(|(?<![\w.])auth\s*\(\s*\)|\bgetToken\s*\(|\bcurrentUser\s*\(|\bgetSession\s*\(|'
                        r'\bgetAuth\s*\(|\bauth\.protect\s*\(|\bsupabase\.auth\.getUser\s*\(|\bverifySession\s*\(|'
                        r'\bvalidateRequest\s*\(|\bgetIronSession\s*\(|\bgetUserFromSession\s*\(')
ADMIN = re.compile(r'(?i)\b(role|roles)\b[^\n;]{0,40}["\'](admin|owner|superuser)["\']|\bisAdmin\b|\brequireAdmin\b|\bisSuperuser\b')
MACHINE = [  # (auth value, regex, evidence)
    ('signature', re.compile(r'constructEvent(?:Async)?\s*\(|verifySignature|validateSignature|\bWebhook\s*\(|\bwh\.verify\s*\(|'
                             r'(?i:stripe-signature|svix-signature|x-hub-signature|x-signature)|createHmac\s*\('), 'webhook signature check'),
    ('cron_secret', re.compile(r'(?i)CRON_SECRET|x-cron-secret|x-vercel-cron'), 'cron secret header'),
    ('api_key', re.compile(r'(?i)x-api-key|["\']api[-_]key["\']|process\.env\.\w*API_?KEY\w*[^\n;]{0,40}(===|!==)|'
                           r'(===|!==)[^\n;]{0,40}process\.env\.\w*API_?KEY'), 'API key header'),
    ('token', re.compile(r'\b(?:params|query)\.token\b|searchParams\.get\(\s*["\']token["\']\s*\)|\{\s*token\s*\}\s*=\s*(?:await\s+)?params|'
                         r'where\s*:\s*\{\s*token\b'), 'single-purpose token from the URL'),
]
NAME_DEF = re.compile(r'(?:^|\n)[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(|'
                      r'(?:^|\n)[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::[^=\n]+)?=>')

def _skip(text: str, i: int) -> int:
    """Index after a string, template or comment starting at i (or i if none starts there)."""
    c = text[i]
    if text.startswith('//', i):
        j = text.find('\n', i)
        return len(text) if j < 0 else j
    if text.startswith('/*', i):
        j = text.find('*/', i + 2)
        return len(text) if j < 0 else j + 2
    if c == '/':  # a regex literal: after an operator, an opening bracket, a comma or a keyword
        k = i - 1
        while k >= 0 and text[k] in ' \t':
            k -= 1
        prev = text[k] if k >= 0 else '\n'
        if prev in '(,=:[!&|?{};+-*%<>~^\n' or re.search(r'\b(return|typeof|case|in|of)$', text[max(0, k - 7):k + 1]):
            j, cls = i + 1, False
            while j < len(text) and text[j] != '\n':
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '[':
                    cls = True
                elif text[j] == ']':
                    cls = False
                elif text[j] == '/' and not cls:
                    return j + 1
                j += 1
            return i  # not a regex after all (division at a line end)
        return i
    if c in '"\'`':
        j = i + 1
        while j < len(text):
            if text[j] == '\\':
                j += 2
                continue
            if text[j] == c:
                return j + 1
            if c != '`' and text[j] == '\n':
                return j
            j += 1
        return len(text)
    return i

def match_close(text: str, i: int) -> int:
    """text[i] is an opening bracket; index after its matching close (or len(text))."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    stack, j = [], i
    while j < len(text):
        k = _skip(text, j)
        if k != j:
            j = k
            continue
        c = text[j]
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return len(text)

def statement_end(text: str, i: int) -> int:
    """From i (after `=`), the end of the expression: the first ; or newline at bracket depth 0."""
    j, seen = i, False
    while j < len(text):
        k = _skip(text, j)
        if k != j:
            j, seen = k, True
            continue
        c = text[j]
        if c in '([{':
            j, seen = match_close(text, j), True
            continue
        if c == ';' or (c == '\n' and seen and not text[j + 1:j + 40].lstrip().startswith(('.', '?', ':', '&&', '||'))):
            return j
        if not c.isspace():
            seen = True
        j += 1
    return len(text)

def function_body(text: str, start: int) -> int:
    """`start` is where a function declaration begins; index after its body."""
    p = text.find('(', start)
    if p < 0:
        return len(text)
    q = match_close(text, p)
    b = text.find('{', q)
    if b < 0:
        return len(text)
    return match_close(text, b)

def definitions(text: str) -> dict[str, tuple[int, int]]:
    """name -> (start, end) of each top-level-looking function or arrow-function definition."""
    out = {}
    for m in NAME_DEF.finditer(text):
        name = m.group(1) or m.group(2)
        start = m.start() + (1 if text[m.start()] == '\n' else 0)
        if m.group(1):
            end = function_body(text, m.start(1))
        else:
            end = statement_end(text, text.find('=', m.end(2)) + 1)
        out.setdefault(name, (start, end))
    return out

def line_of(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1

HELPER_DEPTH = 3  # requireUser() -> getIdentity() -> getServerSession(): wrappers of wrappers are common
NOT_A_GUARD = re.compile(r'(?i)rate|limit|log|track|metric|telemetry|audit|cache|^use[A-Z]')
# A call to an unresolved function named like a guard: not proof of a check, but not "no auth call" either.
GUARD_NAME = re.compile(r'(?<![\w.])((?:require|ensure|assert|check|verify|authenticate|authorize|authorise|validate|guard|protect)'
                        r'\w*(?:Auth|Session|User|Access|Admin|Owner|Staff|Cron|Token|Key|Signature|Request|Identity|Permission|Role)\w*)\s*\(')

VERIFIER_NAME = re.compile(r'(?i)verif|validat|check|assert|authenticat|authori[sz]|require|guard|^run|^handle')

def guard_helpers(files: dict[str, str]) -> dict[str, tuple[str, str]]:
    """name -> (kind, 'file:line') for the repository's helpers that check who is calling. kind is session,
    admin (a session helper whose name says admin), or a machine credential (signature, cron_secret, api_key).
    Helpers that call other helpers count too, up to HELPER_DEPTH levels."""
    funcs = []  # (name, where, inner body)
    for rel, text in files.items():
        for name, (a, b) in definitions(text).items():
            if name in HTTP or len(name) < 4 or name in ('handler', 'middleware', 'proxy', 'default') \
                    or name[0].isupper() or NOT_A_GUARD.search(name):
                continue  # React components and rate limiters are not guards
            body = text[a:b]
            first = body.find('{')
            funcs.append((name, f'{rel}:{line_of(text, a)}', body[first:] if first >= 0 else body))
    helpers: dict[str, tuple[str, str]] = {}
    for _ in range(HELPER_DEPTH):
        rx = _helper_rx(helpers)
        added = False
        for name, where, inner in funcs:
            if name in helpers:
                continue
            kind = None
            if GUARD_CALL.search(inner):
                kind = 'session'
            elif rx and (m := rx.search(inner)):
                kind = 'session' if helpers[m.group(1)][0] in ('session', 'admin') else helpers[m.group(1)][0]
            elif VERIFIER_NAME.search(name):  # a verifier, not code that merely signs or builds something
                kind = next((value for value, mrx, _ in MACHINE if value != 'token' and mrx.search(inner)), None)
            if kind:
                if kind == 'session' and re.search(r'(?i)admin|superuser', name):
                    kind = 'admin'
                helpers[name], added = (kind, where), True
        if not added:
            break
    return helpers

def local_calls(text: str, body: str, defs: dict, depth: int = 2) -> str:
    """The handler body plus the bodies of same-file functions it calls, `depth` levels down:
    `export const POST = (req) => run(req)` where run() calls assertCronAccess()."""
    seen, frontier, parts = {body}, [body], [body]
    for _ in range(depth):
        nxt = []
        for chunk in frontier:
            for name in set(re.findall(r'(?<![\w.])(\w+)\s*\(', chunk)):
                span = defs.get(name)
                if not span:
                    d = re.search(r'(?:^|\n)(?:const|let|var)\s+' + re.escape(name) + r'\s*=', text)  # const h = NextAuth(opts)
                    span = (d.start(), statement_end(text, d.end())) if d else None
                if span and text[span[0]:span[1]] not in seen:
                    seen.add(text[span[0]:span[1]])
                    nxt.append(text[span[0]:span[1]])
        parts += nxt
        frontier = nxt
    return '\n'.join(parts)

def _helper_rx(names) -> re.Pattern | None:
    names = sorted(names, key=len, reverse=True)
    return re.compile(r'(?<![\w.])(' + '|'.join(map(re.escape, names)) + r')\s*\(') if names else None

def classify(body: str, helpers: dict[str, tuple[str, str]], covered_by: str | None) -> tuple[str, str]:
    rx = _helper_rx(helpers)
    called = [(m.group(1), *helpers[m.group(1)]) for m in rx.finditer(body)] if rx else []
    guard = GUARD_CALL.search(body)
    for name, kind, where in called:
        if kind == 'admin':
            return 'admin', f'calls {name}() ({where}), a session helper for admins'
    role = ADMIN.search(body)
    role_note = f'; also compares a role with admin ({role.group(0)[:40]}): read whether it is admin-only' if role else ''
    if guard:
        return 'authenticated', f'calls {guard.group(0).strip("( ")}() in the handler' + role_note
    for name, kind, where in called:
        if kind == 'session':
            return 'authenticated', f'calls {name}() ({where}), which makes a session or guard call' + role_note
    for name, kind, where in called:
        return kind, f'calls {name}() ({where}), which checks a {kind.replace("_", " ")}'
    for value, mrx, why in MACHINE:
        if m := mrx.search(body):
            return value, f'{why} ({m.group(0)[:40]})'
    if covered_by:
        return 'authenticated', covered_by
    if m := GUARD_NAME.search(body):
        return 'unknown', f'calls {m.group(1)}(), named like a guard but not resolved to a session check: read it'
    return 'public', 'no session, guard or credential check in this handler, and no middleware matcher covers it'

# ---------------------------------------------------------------- middleware
def _strings(s: str) -> list[str]:
    return re.findall(r'["\'`]([^"\'`\n]+)["\'`]', s)

def matcher_regex(pattern: str) -> re.Pattern | None:
    """A Next.js matcher (path-to-regexp style, or a raw regex group) as a Python regex."""
    p = pattern
    p = re.sub(r':(\w+)\*', '.*', p)
    p = re.sub(r':(\w+)\+', '.+', p)
    p = re.sub(r':(\w+)\?', '[^/]*', p)
    p = re.sub(r':(\w+)', '[^/]+', p)
    try:
        return re.compile('^' + p + '/?$')
    except re.error:
        return None

def parse_middleware(rel: str, text: str) -> dict:
    m = re.search(r'\bmatcher\s*:\s*(\[[^\]]*\]|["\'`][^"\'`]*["\'`])', text, re.S)
    matcher = _strings(m.group(1)) if m else []
    protected, excluded = [], []
    for am in re.finditer(r'\b(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*\[([^\]]*)\]', text, re.S):
        items = [s for s in _strings(am.group(2)) if s.startswith('/')]
        if not items:
            continue
        (excluded if re.search(r'(?i)public|ignore|exclude|skip|allow|open|unprotected|bypass', am.group(1)) else protected).extend(items)
    exp = re.search(r'export\s+(?:default\s+)?(?:async\s+)?function\s*(middleware|proxy)?|export\s+default\b|export\s+const\s+(middleware|proxy)\b', text)
    auth = bool(GUARD_CALL.search(text) or re.search(r'\bwithAuth\b|\bclerkMiddleware\b|\bauthMiddleware\b|NextAuth\(', text))
    info = {'file': rel, 'line': line_of(text, exp.start()) if exp else 1, 'matcher': matcher,
            'protected_prefixes': protected, 'excluded_prefixes': excluded, 'auth_check': auth}
    info['covers_api'] = bool(covers(info, '/api/x'))
    return info

def covers(mw: dict, path: str) -> str | None:
    """Why the middleware covers the path (or None)."""
    if not mw['auth_check']:
        return None
    probe = re.sub(r':\w+', 'x', path)
    if mw['matcher']:
        hit = next((p for p in mw['matcher'] if (rx := matcher_regex(p)) and rx.search(probe)), None)
        if not hit:
            return None
        why = f"{mw['file']} matcher {hit!r} covers it"
    else:
        why = f"{mw['file']} runs on every path (no matcher)"
    if any(probe == p or probe.startswith(p.rstrip('/') + '/') for p in mw['excluded_prefixes']):
        return None
    if mw['protected_prefixes'] and not any(probe == p or probe.startswith(p.rstrip('/') + '/') for p in mw['protected_prefixes']):
        return None
    return why + (' (protected prefixes listed)' if mw['protected_prefixes'] else '')

# ---------------------------------------------------------------- entry points
def _route_path(rel: str) -> str | None:
    m = ROUTE_FILE.search(rel) or PAGES_API.search(rel)
    if not m:
        return None
    segs = [s for s in m.group(1).split('/') if s and not (s.startswith('(') and s.endswith(')')) and not s.startswith('@')]
    path = '/' + '/'.join(re.sub(r'^\[\[?(?:\.\.\.)?(\w+)\]?\]$', r':\1', s) for s in segs)
    return re.sub(r'/index$', '', path) or '/'

def exported_handlers(text: str, names=HTTP) -> list[tuple[str, int, str]]:
    """(name, line, body text) for each exported handler: functions, consts and `export { a as GET }`."""
    out, defs = [], None
    alt = '|'.join(names)
    for m in re.finditer(r'export\s+(?:async\s+)?function\s+(' + alt + r')\s*\(', text):
        out.append((m.group(1), line_of(text, m.start()), text[m.start():function_body(text, m.start(1))]))
    for m in re.finditer(r'export\s+const\s+(' + alt + r')\s*(?::[^=\n]+)?=', text):
        out.append((m.group(1), line_of(text, m.start()), text[m.start():statement_end(text, m.end())]))
    for m in re.finditer(r'export\s+const\s*\{([^}]*)\}\s*=', text):  # export const { GET, POST } = handlers
        stmt = text[m.start():statement_end(text, m.end())]
        for item in m.group(1).split(','):
            im = re.match(r'\s*(\w+)\s*(?::\s*(\w+))?\s*$', item)
            if im and (im.group(2) or im.group(1)) in names:
                out.append((im.group(2) or im.group(1), line_of(text, m.start()), stmt))
    for m in re.finditer(r'export\s*\{([^}]*)\}', text):
        pairs = []
        for item in m.group(1).split(','):
            im = re.match(r'\s*(\w+)(?:\s+as\s+(\w+))?\s*$', item)
            if im:
                pairs.append((im.group(1), im.group(2) or im.group(1)))
        for local, exported in pairs:
            if exported not in names:
                continue
            defs = defs if defs is not None else definitions(text)
            span = defs.get(local)
            body = text[span[0]:span[1]] if span else ''
            if not body:  # const handler = NextAuth(options)
                d = re.search(r'(?:const|let|var)\s+' + re.escape(local) + r'\s*=', text)
                body = text[d.start():statement_end(text, d.end())] if d else ''
            out.append((exported, line_of(text, m.start()), body))
    return out

def route_entries(rel: str, text: str, helpers, middleware: list[dict]) -> list[dict]:
    path = _route_path(rel)
    if path is None:
        return []
    pages = bool(PAGES_API.search(rel))
    hs = exported_handlers(text)
    if pages and not hs:
        d = re.search(r'export\s+default\s+(?:async\s+)?function\b|export\s+default\b', text)
        if d:
            end = function_body(text, d.start()) if 'function' in d.group(0) else statement_end(text, d.end())
            hs = [('ANY', line_of(text, d.start()), text[d.start():end])]
    out = []
    defs = definitions(text)
    for meth, line, body in hs:
        body = local_calls(text, body, defs)
        cover = next((c for mw in middleware if (c := covers(mw, path))), None)
        if re.search(r'\bNextAuth\s*\(|\bhandlers\b|\btoNextJsHandler\s*\(', body):
            auth, ev = 'public', 'auth library endpoints (sign-in, callback, session): public by design; review its callbacks and providers'
        else:
            auth, ev = classify(body, helpers, cover)
        out.append({'kind': 'webhook' if 'webhook' in path.lower() else 'http_route', 'framework': 'nextjs', 'method': meth,
                    'path': path, 'handler': f'{rel}:{line}', 'function': meth, 'auth': auth, 'auth_evidence': ev})
    return out

def server_actions(rel: str, text: str, helpers) -> list[dict]:
    out = []
    if USE_SERVER.match(text):
        for m in re.finditer(r'export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(|export\s+const\s+(\w+)\s*=\s*async\b', text):
            name = m.group(1) or m.group(2)
            end = function_body(text, m.start(1)) if m.group(1) else statement_end(text, m.end())
            auth, ev = classify(text[m.start():end], helpers, None)
            out.append({'kind': 'server_action', 'framework': 'nextjs', 'method': 'POST', 'path': f'action:{name}',
                        'handler': f'{rel}:{line_of(text, m.start())}', 'function': name, 'auth': auth,
                        'auth_evidence': ev + '; callable by any client that knows the action id'})
    else:
        for m in re.finditer(r'(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*["\']use server["\']', text):
            auth, ev = classify(text[m.start():function_body(text, m.start(1))], helpers, None)
            out.append({'kind': 'server_action', 'framework': 'nextjs', 'method': 'POST', 'path': f'action:{m.group(1)}',
                        'handler': f'{rel}:{line_of(text, m.start())}', 'function': m.group(1), 'auth': auth,
                        'auth_evidence': ev + '; inline server action'})
    return out

def middleware_entry(mw: dict) -> dict:
    return {'kind': 'middleware', 'framework': 'nextjs', 'method': 'ANY', 'path': ', '.join(mw['matcher']) or '/*',
            'handler': f"{mw['file']}:{mw['line']}", 'function': None,
            'auth': 'authenticated' if mw['auth_check'] else 'unknown',
            'auth_evidence': ('session check for ' + (', '.join(mw['protected_prefixes']) or 'every matched path') +
                              ('' if mw['covers_api'] else '; does NOT cover /api/*, so each route handler must check auth itself'))
                             if mw['auth_check'] else 'no session check found in the middleware'}

def server_file(rel: str, text: str) -> bool:
    """Server-only code: route handlers, API routes, middleware, server actions and server-only modules."""
    return bool(ROUTE_FILE.search(rel) or PAGES_API.search(rel) or MIDDLEWARE_FILE.search(rel) or '/api/' in '/' + rel
                or USE_SERVER.match(text) or SERVER_ONLY.search(text))
