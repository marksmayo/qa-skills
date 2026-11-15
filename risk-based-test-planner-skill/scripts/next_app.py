#!/usr/bin/env python3
"""Static reader of a Next.js project (stdlib only; regular expressions and a small bracket scanner, no Node).

    python next_app.py <repo> [--out next_app.json]     # exit 3 when no Next.js project is found
    python next_app.py --self-test

    from next_app import read_next_app
    model = read_next_app(repo)          # JSON-able dict, see below

A Next.js project is a folder with a next.config.* or a package.json depending on next (monorepos give several).
Everything is read from source, best effort: values built at run time (environment variables, imported constants,
function calls) are kept as written and listed under `unresolved`; nothing is executed. Paths are '/'-separated and
relative to the repository; lines are 1-based.

Returned dict:
    {tool: "next_app", schema_version: 1, repo, is_next, notes: [...],
     projects: [{
       root            "" for the repository root, else the folder ("apps/web")
       next_version    from package.json, or null
       routers         ["app", "pages"] (those present); app_dir, pages_dir: folders relative to the repo, or null
       routes          [{kind, url, file, line, ...}]; kind is
                         page            App Router page; params [names], segment_config, inherited_config,
                                         client (a 'use client' page), slot ("@modal"), intercepts, optional_catch_all
                         route_handler   route.(ts|js): methods [{method, line}], segment_config
                         metadata        robots, sitemap, manifest, favicon, icon, apple-icon, opengraph-image,
                                         twitter-image: metadata (the name), form "file" | "generated"
                         pages_page      pages/ router page (getStaticPaths / getServerSideProps noted)
                         pages_api       pages/api route (method ANY)
                       url uses {name} for [name] and {name*} for catch-alls; route groups and @slots are not URL
                       segments; an intercepting route has the URL it intercepts
       segment_config  {generate_static_params: {line}, dynamic_params: {value, line}, dynamic: {value, line},
                        revalidate: {value, line}, runtime: {value, line}, fetch_cache: {value, line}}
       segments        [{dir, url, files: {layout|template|loading|error|not-found|global-error|default|forbidden|
                        unauthorized: {file, line}}, segment_config}]  (folders of the app dir holding any of these)
       root_special    {dir, present: [...], missing: [...]}  of layout, not-found, error, global-error, loading
       pages_special   _app, _document, _error, 404, 500 present in pages/
       middleware      {file, line, matcher: [...], unresolved} or null (middleware.* or proxy.* at the root or src/)
       server_actions  [{file, line, scope: "file" | "inline"}]
       client_components [{file, line}]  ('use client' files outside tests)
       scripts         [{kind, file, line, src|href, host, first_party, strategy, id, inline, body_hosts,
                        component, unresolved}]: kind next/script (<Script> from next/script, inline when it has a body
                        or dangerouslySetInnerHTML), script (JSX <script>), preconnect | dns-prefetch | preload |
                        prefetch | modulepreload (<link rel> hints and react-dom preconnect()/prefetchDNS()),
                        next/third-parties (@next/third-parties components, with the host they load from)
       config          {file, headers: [{source, line, headers: [{key, value, line, unresolved}]}],
                        csp: [{source, file, line, header, directives: {name: [sources]}, unresolved}],
                        images: {remote_patterns: [{protocol, hostname, port, pathname}], domains, unoptimized, line},
                        redirects: {line, entries: [{source, destination, line}]} | null, rewrites: (same) | null,
                        settings: {output, basePath, trailingSlash, poweredByHeader, assetPrefix},
                        plugins: ["next-pwa", ...]} or null
       platform        {"vercel.json": {headers, redirects, rewrites, csp}, "netlify.toml": {...},
                        "public/_headers": {...}}  (those present)
       service_workers [{url, worker_file, exists, registrations: [{file, line, url, scope}], generated_by,
                        events: [...], skip_waiting, clients_claim, cache_names, precache_count,
                        fetch_strategies: [{when, condition, line, strategy, caches_response}], heuristic: true}]
                        when: navigation | cross-origin | same-origin | path | destination | method | other | default;
                        strategy: network-only | network-first | cache-first | cache-only | stale-while-revalidate |
                        passthrough (the worker leaves the request to the browser) | synthetic | unknown
       counts          {pages, route_handlers, metadata, client_components, server_actions, scripts}
     }]}

Other helpers: next_url(folder_parts) -> (url, info), parse_csp(text) -> {directive: [sources]},
find_next_roots(repo) -> [folder].
"""
from __future__ import annotations
import argparse, contextvars, json, os, re, sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

SKIP_DIRS = {'node_modules', '.git', '.next', 'out', 'dist', 'build', 'coverage', '.vercel', '.turbo', '.cache',
             'test-results', 'playwright-report', 'storybook-static', '.svelte-kit', '.netlify', '__pycache__', '.venv',
             'venv'}
JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts')
PAGE_EXT = ('.tsx', '.ts', '.jsx', '.js', '.mdx', '.md')
NEXT_CONFIG = re.compile(r'^next\.config\.(?:js|mjs|cjs|ts|mts)$')
TEST_FILE = re.compile(r'\.(?:test|spec|stories|story|cy|e2e)\.[cm]?[jt]sx?$')
TEST_DIRS = {'__tests__', '__mocks__', 'tests', 'test', 'e2e', 'cypress', 'playwright', '__fixtures__', 'fixtures'}
SPECIAL = ('layout', 'template', 'loading', 'error', 'not-found', 'global-error', 'default', 'forbidden', 'unauthorized')
ROOT_EXPECTED = ('layout', 'not-found', 'error', 'global-error', 'loading')
HTTP_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
META_ANY = re.compile(r'^(?:(sitemap)\.(xml|js|jsx|ts|tsx)|(icon|apple-icon|opengraph-image|twitter-image)(\d*)\.'
                      r'(ico|jpg|jpeg|png|gif|svg|js|jsx|ts|tsx))$')
META_ROOT = re.compile(r'^(?:(robots)\.(txt|js|ts)|(manifest)\.(json|webmanifest|js|ts)|(favicon)\.(ico))$')
INTERCEPT = re.compile(r'^(\(\.\)|(?:\(\.\.\))+|\(\.\.\.\))(.+)$')
CSP_DIRECTIVES = {
    'default-src', 'script-src', 'script-src-elem', 'script-src-attr', 'style-src', 'style-src-elem', 'style-src-attr',
    'img-src', 'font-src', 'connect-src', 'media-src', 'object-src', 'frame-src', 'child-src', 'worker-src',
    'manifest-src', 'prefetch-src', 'fenced-frame-src', 'base-uri', 'form-action', 'frame-ancestors', 'navigate-to',
    'report-uri', 'report-to', 'sandbox', 'upgrade-insecure-requests', 'block-all-mixed-content',
    'require-trusted-types-for', 'trusted-types', 'plugin-types'}
THIRD_PARTIES = {   # @next/third-parties components and the host each loads from
    'GoogleTagManager': 'www.googletagmanager.com', 'GoogleAnalytics': 'www.googletagmanager.com',
    'GoogleMapsEmbed': 'www.google.com', 'YouTubeEmbed': 'www.youtube.com', 'sendGAEvent': None, 'sendGTMEvent': None}
URL_IN_TEXT = re.compile(r'https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\'"`<>)\\]*)?')


# ---------------------------------------------------------------- text scanning
def mask_comments(text: str) -> str:
    """The text with // and /* */ comments replaced by spaces (newlines kept), strings and templates left alone."""
    out, i, n = list(text), 0, len(text)
    stack = []   # open template literals ('`') and ${ } expressions inside them ('{')
    quote = None
    while i < n:
        c = text[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if stack and stack[-1] == '`':
            if c == '\\':
                i += 2
                continue
            if c == '`':
                stack.pop()
            elif c == '$' and text[i + 1:i + 2] == '{':
                stack.append('{')
                i += 1
            i += 1
            continue
        if c in '\'"':
            quote = c
        elif c == '`':
            stack.append('`')
        elif c == '{' and stack:
            stack.append('{')
        elif c == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif c == '/' and text[i + 1:i + 2] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = ' '
            i = j
            continue
        elif c == '/' and text[i + 1:i + 2] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
            continue
        i += 1
    return ''.join(out)


def line_at(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def skip_string(text: str, i: int) -> int:
    """Index just past the string or template literal starting at text[i]."""
    q, i, n = text[i], i + 1, len(text)
    while i < n:
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if c == q:
            return i + 1
        if q == '`' and c == '$' and text[i + 1:i + 2] == '{':
            i = matching(text, i + 1) + 1
            continue
        i += 1
    return n


def matching(text: str, i: int) -> int:
    """Index of the bracket closing the one at text[i] ((, [ or {), strings skipped; len(text) - 1 if unbalanced."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"`':
            i = skip_string(text, i)
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def expr_end(text: str, i: int, newline_ends: bool = False) -> int:
    """Index where the expression starting at text[i] ends: a , ; or closing bracket at depth 0 (and, with newline_ends,
    a line break that does not continue the expression)."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"`':
            i = skip_string(text, i)
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and c in ',;':
            return i
        elif depth == 0 and c == '\n' and newline_ends:
            before = text[:i].rstrip()[-1:]
            after = text[i:].lstrip()[:1]
            if before not in ('=', '+', '?', ':', '(', ',', '[', '{', '|', '&', '') and after not in ('.', '?', ':', '+', '|', '&'):
                return i
        i += 1
    return n


def split_top(expr: str, sep: str) -> list[str]:
    """expr split on sep at bracket depth 0, outside strings."""
    parts, depth, i, start, n = [], 0, 0, 0, len(expr)
    while i < n:
        c = expr[i]
        if c in '\'"`':
            i = skip_string(expr, i)
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif depth == 0 and expr.startswith(sep, i):
            if sep == '+' and (expr[i + 1:i + 2] in '+=' or expr[i - 1:i] == '+'):
                i += 1
                continue
            parts.append(expr[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(expr[start:])
    return parts


STRING = re.compile(r'''(['"])((?:\\.|(?!\1)[^\\\n])*)\1''')
IDENT = re.compile(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*')
CONST_DEF = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;\n]+?)?=\s*')


def constants(masked: str) -> dict:
    """Top-level-looking `const NAME = expr` definitions: name -> (expr, offset)."""
    out = {}
    for m in CONST_DEF.finditer(masked):
        end = expr_end(masked, m.end(), newline_ends=True)
        out.setdefault(m.group(1), (masked[m.end():end].strip(), m.end()))
    return out


def unquote(lit: str) -> str:
    body = lit[1:-1]
    return re.sub(r'\\(.)', r'\1', body)


def evaluate(expr: str, consts: dict, depth: int = 0) -> tuple[str | None, list[str]]:
    """Best-effort value of a JavaScript string expression: literals, templates, constants, + concatenation,
    [..].join(sep), ?? / || fallbacks and ternaries (both branches kept). Returns (text or None, unresolved names)."""
    e = (expr or '').strip()
    if depth > 8 or not e:
        return None, [e[:60]] if e else []
    stripped = True
    while stripped:   # trailing string clean-ups: .trim(), .replace(...), .replaceAll(...), last call first
        stripped = False
        for m in reversed(list(re.finditer(r'\.\s*(?:trim|trimEnd|trimStart|replace|replaceAll)\s*\(', e))):
            if not e[matching(e, m.end() - 1) + 1:].strip():
                e, stripped = e[:m.start()].strip(), True
                break
    if e.startswith('(') and matching(e, 0) == len(e) - 1:
        return evaluate(e[1:-1], consts, depth + 1)
    tern = split_top(e, '?')
    if len(tern) == 2 and not tern[1].startswith(('?', '.')) and not tern[0].endswith('?'):
        branches = split_top(tern[1], ':')
        if len(branches) == 2:
            a, ua = evaluate(branches[0], consts, depth + 1)
            b, ub = evaluate(branches[1], consts, depth + 1)
            u = ua + ub + [f'condition: {tern[0].strip()[:60]}']
            return ' '.join(x for x in (a, b) if x) or None, u
    for op in ('??', '||'):
        alts = split_top(e, op)
        if len(alts) > 1:
            vals, unresolved = [], []
            for alt in alts:
                v, u = evaluate(alt, consts, depth + 1)
                unresolved += u
                if v is not None:
                    vals.append(v)
            return (vals[0] if vals else None), unresolved
    m = re.fullmatch(r'(\[.*\]|[A-Za-z_$][\w$]*)\s*(?:\.\s*filter\s*\(\s*Boolean\s*\)\s*)?\.\s*join\s*\(\s*(.*?)\s*\)', e, re.S)
    if m:
        arr = m.group(1)
        unresolved = []
        if not arr.startswith('['):
            if arr not in consts:
                return None, [arr]
            arr = consts[arr][0]
        sep, u = evaluate(m.group(2), consts, depth + 1) if m.group(2) else (',', [])
        unresolved += u
        if not arr.startswith('['):
            return None, unresolved + [arr[:60]]
        vals = []
        for item in split_top(arr[1:matching(arr, 0)], ','):
            if not item.strip():
                continue
            v, u = evaluate(item, consts, depth + 1)
            unresolved += u
            if v:
                vals.append(v)
        return (sep or '').join(vals), unresolved
    parts = split_top(e, '+')
    if len(parts) > 1:
        vals, unresolved = [], []
        for p in parts:
            v, u = evaluate(p, consts, depth + 1)
            unresolved += u
            vals.append(v if v is not None else '${' + p.strip()[:40] + '}')
        return ''.join(vals), unresolved
    if e[0] in '\'"' and STRING.fullmatch(e):
        return unquote(e), []
    if e[0] == '`' and skip_string(e, 0) == len(e):
        out, unresolved, i, body = [], [], 0, e[1:-1]
        while i < len(body):
            if body[i] == '\\' and i + 1 < len(body):
                out.append(body[i + 1])
                i += 2
                continue
            if body.startswith('${', i):
                close = matching(body, i + 1)
                v, u = evaluate(body[i + 2:close], consts, depth + 1)
                unresolved += u
                out.append(v if v is not None else '${' + body[i + 2:close].strip() + '}')
                i = close + 1
                continue
            out.append(body[i])
            i += 1
        return ''.join(out), unresolved
    if re.fullmatch(r'-?\d+(?:\.\d+)?', e):
        return e, []
    if IDENT.fullmatch(e):
        if e in consts:
            return evaluate(consts[e][0], consts, depth + 1)
        return None, [e]
    return None, [e[:60]]


def string_after(text: str, name: str) -> str | None:
    """The string literal following `name:` in text, unquoted, or None."""
    m = re.search(r'\b' + re.escape(name) + r'\s*:\s*', text)
    lit = STRING.match(text, m.end()) if m else None
    return unquote(lit.group(0)) if lit else None


def literal_value(expr: str, consts: dict):
    """A JSON-like value for simple literals: true/false, numbers, strings (evaluated); else the text."""
    e = (expr or '').strip()
    if e in ('true', 'false'):
        return e == 'true'
    if re.fullmatch(r'-?\d+(?:\.\d+)?', e):
        return float(e) if '.' in e else int(e)
    v, _ = evaluate(e, consts)
    return v if v is not None else e


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r'^(?:https?:)?//([^/:?#\s\'"`{}$]+)', url.strip())
    return m.group(1).lower() if m else None


def parse_csp(value: str) -> dict:
    """{directive: [sources]} from a Content-Security-Policy value; the first occurrence of a directive wins, as in
    browsers. Unknown tokens at the start of a part are kept as directives too."""
    out: dict = {}
    for part in re.split(r'[;\n]', value or ''):
        toks = part.split()
        if not toks:
            continue
        name = toks[0].lower()
        if name not in out:
            out[name] = toks[1:]
    return out


# ---------------------------------------------------------------- files and projects
def walk_files(root: Path, max_files: int) -> tuple[list[str], bool]:
    files, truncated = [], False
    for cur, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.git'))
        rel = Path(cur).relative_to(root).as_posix()
        for nm in sorted(names):
            files.append(nm if rel == '.' else f'{rel}/{nm}')
            if len(files) >= max_files:
                return files, True
    return files, truncated


# texts a caller has already read (repo-relative path -> text), set for the length of one read_next_app call
_GIVEN_TEXTS: contextvars.ContextVar[dict | None] = contextvars.ContextVar('next_app_texts', default=None)


def read(root: Path, rel: str) -> str:
    given = _GIVEN_TEXTS.get()
    if given is not None and rel in given:
        return given[rel]
    try:
        return (root / rel).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def is_test(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(TEST_FILE.search(p.name)) or any(part in TEST_DIRS for part in p.parts[:-1])


def find_next_roots(root, files: list[str] | None = None) -> list[str]:
    """Folders holding a Next.js project: a next.config.* or a package.json depending on next ('' for the root)."""
    root = Path(root)
    files = files if files is not None else walk_files(root, 50_000)[0]
    out = set()
    for rel in files:
        p = PurePosixPath(rel)
        d = '' if p.parent.as_posix() == '.' else p.parent.as_posix()
        if NEXT_CONFIG.match(p.name):
            out.add(d)
        elif p.name == 'package.json':
            try:
                pkg = json.loads(read(root, rel))
            except ValueError:
                continue
            if isinstance(pkg, dict) and any(isinstance(pkg.get(k), dict) and 'next' in pkg[k]
                                             for k in ('dependencies', 'devDependencies')):
                out.add(d)
    return sorted(out)


def under(rel: str, d: str) -> str | None:
    if not d:
        return rel
    return rel[len(d) + 1:] if rel.startswith(d + '/') else None


def join_url(*segs: str) -> str:
    s = '/'.join(x.strip('/') for x in segs if x and x.strip('/'))
    return '/' + s


def next_url(parts: list[str]) -> tuple[str, dict]:
    """The URL of a route from its folder segments and what shaped it (borrowed from the test suite evaluator):
    route groups `(x)` and slots `@x` are not URL segments (slot), an intercepting `(.)x`, `(..)x`, `(...)x` takes the
    URL it intercepts (intercepts), `[id]` -> {id}, `[...x]` and `[[...x]]` -> {x*} (optional_catch_all)."""
    segs, info, params = [], {}, []
    for p in parts:
        if p.startswith('@'):
            info['slot'] = p
            continue
        m = INTERCEPT.match(p)
        if m:
            marker, p = m.groups()
            info['intercepts'] = True
            if marker == '(...)':
                segs = []
            elif marker != '(.)':
                segs = segs[:max(0, len(segs) - marker.count('(..)'))]
        if p.startswith('(') and p.endswith(')'):
            continue
        m = re.fullmatch(r'\[(\[?)\.\.\.([\w-]+)\]?\]', p)
        if m:
            segs.append('{' + m.group(2) + '*}')
            params.append(m.group(2))
            info['catch_all'] = True
            if m.group(1):
                info['optional_catch_all'] = True
            continue
        m = re.fullmatch(r'\[([\w-]+)\]', p)
        if m:
            params.append(m.group(1))
        segs.append('{' + m.group(1) + '}' if m else p)
    if params:
        info['params'] = params
    return join_url(*segs), info


def default_line(masked: str) -> int:
    m = re.search(r'^[ \t]*export\s+default\b', masked, re.M)
    return line_at(masked, m.start()) if m else 1


def directive(masked: str, name: str) -> int | None:
    """Line of a 'use client' / 'use server' directive at the top of a module."""
    m = re.match(r'''\s*(?:(['"])use (?:strict|client|server)\1\s*;?\s*)*''', masked)
    head = masked[:m.end()] if m else ''
    d = re.search(r'''(['"])''' + re.escape(name) + r'''\1''', head)
    return line_at(masked, d.start()) if d else None


SEG_CONFIG = (('dynamic_params', r'export\s+const\s+dynamicParams\s*=\s*'),
              ('dynamic', r'export\s+const\s+dynamic\s*=\s*'),
              ('revalidate', r'export\s+const\s+revalidate\s*=\s*'),
              ('runtime', r'export\s+const\s+runtime\s*=\s*'),
              ('fetch_cache', r'export\s+const\s+fetchCache\s*=\s*'))


def segment_config(masked: str, consts: dict) -> dict:
    out = {}
    m = re.search(r'export\s+(?:async\s+)?function\s+generateStaticParams\b|export\s+const\s+generateStaticParams\b', masked)
    if m:
        out['generate_static_params'] = {'line': line_at(masked, m.start())}
    for key, rx in SEG_CONFIG:
        m = re.search(rx, masked)
        if m:
            end = expr_end(masked, m.end(), newline_ends=True)
            out[key] = {'value': literal_value(masked[m.end():end], consts), 'line': line_at(masked, m.start())}
    return out


# ---------------------------------------------------------------- routes
def read_routes(root: Path, files: list[str], nroot: str, app: str | None, pages: str | None, cache: dict) -> dict:
    routes, segments, pages_special = [], {}, []
    if app:
        for rel in files:
            inner = under(rel, app)
            if inner is None:
                continue
            p = PurePosixPath(rel)
            parts = list(PurePosixPath(inner).parts)
            name, dirs = parts[-1], parts[:-1]
            seg_dir = PurePosixPath(rel).parent.as_posix()
            url, info = next_url(dirs)
            if p.stem in SPECIAL and p.suffix in JS_EXT + ('.mdx',):
                masked, consts = cache_get(root, rel, cache)
                seg = segments.setdefault(seg_dir, {'dir': seg_dir, 'url': url, 'files': {}})
                seg['files'][p.stem] = {'file': rel, 'line': default_line(masked)}
                if p.stem == 'layout':
                    cfg = segment_config(masked, consts)
                    if cfg:
                        seg['segment_config'] = cfg
                continue
            if p.stem == 'page' and p.suffix in PAGE_EXT:
                masked, consts = cache_get(root, rel, cache)
                r = {'kind': 'page', 'url': url, 'file': rel, 'line': default_line(masked), **info}
                cfg = segment_config(masked, consts)
                if cfg:
                    r['segment_config'] = cfg
                if directive(masked, 'use client'):
                    r['client'] = True
                routes.append(r)
                continue
            if p.stem == 'route' and p.suffix in JS_EXT:
                masked, consts = cache_get(root, rel, cache)
                methods = {}
                for m in re.finditer(r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|'
                                     r'export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b', masked):
                    methods.setdefault(m.group(1) or m.group(2), line_at(masked, m.start()))
                for m in re.finditer(r'export\s*\{([^}]*)\}', masked):
                    for x in m.group(1).split(','):
                        nm = x.split(' as ')[-1].strip()
                        if nm in HTTP_METHODS:
                            methods.setdefault(nm, line_at(masked, m.start()))
                r = {'kind': 'route_handler', 'url': url, 'file': rel,
                     'line': min(methods.values()) if methods else 1,
                     'methods': [{'method': k, 'line': methods[k]} for k in HTTP_METHODS if k in methods], **info}
                cfg = segment_config(masked, consts)
                if cfg:
                    r['segment_config'] = cfg
                routes.append(r)
                continue
            mr = META_ROOT.match(name) if not dirs else None
            ma = META_ANY.match(name)
            if mr or ma:
                gen = p.suffix in JS_EXT
                masked = cache_get(root, rel, cache)[0] if gen else ''
                if mr and mr.group(1):
                    kind_name, murl = 'robots', '/robots.txt'
                elif mr and mr.group(3):
                    kind_name, murl = 'manifest', '/manifest.webmanifest' if gen else f'/{name}'
                elif mr:
                    kind_name, murl = 'favicon', '/favicon.ico'
                elif ma.group(1):
                    kind_name = 'sitemap'
                    murl = join_url(url, 'sitemap/{id}.xml') if 'generateSitemaps' in masked else join_url(url, 'sitemap.xml')
                else:
                    kind_name = ma.group(3)
                    stem = ma.group(3) + ma.group(4)
                    if gen:
                        murl = join_url(url, stem + '/{id}') if 'generateImageMetadata' in masked else join_url(url, stem)
                    else:
                        murl = join_url(url, name)
                r = {'kind': 'metadata', 'metadata': kind_name, 'form': 'generated' if gen else 'file', 'url': murl,
                     'file': rel, 'line': default_line(masked) if gen else 1, **info}
                if gen and kind_name in ('icon', 'apple-icon', 'opengraph-image', 'twitter-image'):
                    r['note'] = 'a generated image: Next.js may add a hash suffix to the URL; match it by prefix'
                routes.append(r)
        # layouts' segment config applies to every page below them
        for r in routes:
            if r['kind'] not in ('page', 'route_handler'):
                continue
            inherited = {}
            d = PurePosixPath(r['file']).parent
            while True:
                seg = segments.get(d.as_posix())
                for k, v in ((seg or {}).get('segment_config') or {}).items():
                    inherited.setdefault(k, {**v, 'file': seg['files']['layout']['file']})
                if d.as_posix() == app or d.as_posix() in ('.', ''):
                    break
                d = d.parent
            own = r.get('segment_config') or {}
            inherited = {k: v for k, v in inherited.items() if k not in own}
            if inherited:
                r['inherited_config'] = inherited
    if pages:
        for rel in files:
            inner = under(rel, pages)
            if inner is None:
                continue
            p = PurePosixPath(rel)
            if p.suffix not in PAGE_EXT:
                continue
            segs = list(PurePosixPath(inner).parts)
            if len(segs) == 1 and p.stem in ('_app', '_document', '_error', '404', '500'):
                pages_special.append({'name': p.stem, 'file': rel})
                continue
            if any(s.startswith('_') for s in segs):
                continue
            url, info = next_url(segs[:-1] + ([] if p.stem == 'index' else [p.stem]))
            masked, _ = cache_get(root, rel, cache)
            if segs[0] == 'api':
                routes.append({'kind': 'pages_api', 'url': url, 'file': rel, 'line': default_line(masked), 'method': 'ANY', **info})
                continue
            r = {'kind': 'pages_page', 'url': url, 'file': rel, 'line': default_line(masked), **info}
            for fn in ('getStaticPaths', 'getStaticProps', 'getServerSideProps'):
                m = re.search(r'export\s+(?:async\s+)?(?:function|const)\s+' + fn + r'\b', masked)
                if m:
                    r.setdefault('data', {})[fn] = {'line': line_at(masked, m.start())}
            routes.append(r)
    routes.sort(key=lambda r: (r['url'], r['kind'], r['file']))
    root_special = None
    if app:
        present = sorted(((segments.get(app) or {}).get('files') or {}))
        root_special = {'dir': app, 'present': present, 'missing': [k for k in ROOT_EXPECTED if k not in present]}
    return {'routes': routes, 'segments': [segments[k] for k in sorted(segments)], 'root_special': root_special,
            'pages_special': pages_special}


def cache_get(root: Path, rel: str, cache: dict) -> tuple[str, dict]:
    if rel not in cache:
        masked = mask_comments(read(root, rel))
        cache[rel] = (masked, constants(masked))
    return cache[rel]


# ---------------------------------------------------------------- JSX tags
ATTR = re.compile(r'([A-Za-z_][\w:.-]*)(?:\s*=\s*)?')


def tag_attrs(masked: str, start: int) -> tuple[dict, int, bool]:
    """Attributes of the JSX opening tag starting at masked[start] ('<Name ...'): {name: raw value}, the index just past
    the tag, and whether it closes itself. Raw values keep their quotes or braces."""
    i = start + 1
    m = re.match(r'[\w.]+', masked[i:])
    i += m.end() if m else 0
    attrs, n = {}, len(masked)
    while i < n:
        while i < n and masked[i].isspace():
            i += 1
        if masked.startswith('/>', i):
            return attrs, i + 2, True
        if i >= n or masked[i] == '>':
            return attrs, i + 1, False
        if masked[i] == '{':   # {...spread}
            i = matching(masked, i) + 1
            continue
        m = ATTR.match(masked, i)
        if not m:
            i += 1
            continue
        name, i = m.group(1), m.end()
        if masked[m.end(1):m.end()].strip() == '=':
            if masked[i] in '\'"':
                j = skip_string(masked, i)
            elif masked[i] == '{':
                j = matching(masked, i) + 1
            else:
                j = i
                while j < n and not masked[j].isspace() and masked[j] not in '/>':
                    j += 1
            attrs[name] = masked[i:j]
            i = j
        else:
            attrs[name] = True
    return attrs, n, False


def attr_value(raw, consts: dict) -> tuple[str | None, list[str]]:
    if raw is True or raw is None:
        return (None, []) if raw is None else ('true', [])
    raw = raw.strip()
    if raw.startswith('{') and raw.endswith('}'):
        return evaluate(raw[1:-1], consts)
    if raw[:1] in '\'"':
        return raw[1:-1], []
    return raw, []


def read_scripts(root: Path, files: list[str], cache: dict) -> list[dict]:
    out = []
    for rel in files:
        if not rel.endswith(('.tsx', '.jsx', '.js', '.ts', '.mjs', '.mdx')) or is_test(rel) or rel.startswith('public/') \
                or '/public/' in rel:
            continue
        masked, consts = cache_get(root, rel, cache)
        if '<' not in masked and 'preconnect' not in masked and 'prefetchDNS' not in masked:
            continue
        aliases = set()
        for m in re.finditer(r'''import\s+([A-Za-z_$][\w$]*)\s+from\s+['"]next/script['"]''', masked):
            aliases.add(m.group(1))
        tp = {}
        for m in re.finditer(r'''import\s*\{([^}]*)\}\s*from\s+['"]@next/third-parties/[\w-]+['"]''', masked):
            for x in m.group(1).split(','):
                bits = [b.strip() for b in x.split(' as ')]
                if bits[0] in THIRD_PARTIES:
                    tp[bits[-1]] = bits[0]
        names = '|'.join(re.escape(a) for a in sorted(aliases | set(tp))) or None
        rx = r'<(script|link' + (f'|{names}' if names else '') + r')\b'
        for m in re.finditer(rx, masked):
            tag = m.group(1)
            attrs, end, closed = tag_attrs(masked, m.start())
            line = line_at(masked, m.start())
            if tag == 'link':
                rel_attr, _ = attr_value(attrs.get('rel'), consts)
                rel_attr = (rel_attr or '').lower()
                kinds = [k for k in ('preconnect', 'dns-prefetch', 'modulepreload', 'preload', 'prefetch')
                         if k in rel_attr.split()]
                if not kinds:
                    continue
                href, un = attr_value(attrs.get('href'), consts)
                out.append({'kind': kinds[0], 'file': rel, 'line': line, 'href': href, 'host': host_of(href),
                            'first_party': bool(href and href.startswith('/') and not href.startswith('//')),
                            **({'as': attr_value(attrs.get('as'), consts)[0]} if 'as' in attrs else {}),
                            **({'unresolved': un} if un else {})})
                continue
            if tag in tp:
                comp = tp[tag]
                if THIRD_PARTIES.get(comp) is None:
                    continue
                props = {k: attr_value(v, consts)[0] for k, v in attrs.items() if k not in ('children',)}
                out.append({'kind': 'next/third-parties', 'component': comp, 'file': rel, 'line': line,
                            'host': THIRD_PARTIES[comp], 'props': props})
                continue
            src, un = attr_value(attrs.get('src'), consts)
            body = ''
            if not closed:
                close = re.compile(r'</\s*' + re.escape(tag) + r'\s*>').search(masked, end)
                body = masked[end:close.start()] if close else ''
            inline_html = attrs.get('dangerouslySetInnerHTML')
            inline_text = body + (inline_html if isinstance(inline_html, str) else '')
            rec = {'kind': 'next/script' if tag != 'script' else 'script', 'file': rel, 'line': line, 'src': src,
                   'host': host_of(src), 'first_party': bool(src and src.startswith('/') and not src.startswith('//'))}
            for a in ('strategy', 'id', 'async', 'defer', 'type', 'crossOrigin', 'crossorigin', 'integrity', 'nonce'):
                if a in attrs:
                    v, u = attr_value(attrs[a], consts)
                    rec[a.lower() if a != 'crossOrigin' else 'crossorigin'] = v
                    un += u
            if tag != 'script' and 'strategy' not in rec:
                rec['strategy'] = 'afterInteractive (default)'
            if inline_text.strip() and not src:
                rec['inline'] = True
                consts_text = inline_text
                for nm in set(re.findall(r'\$\{\s*([A-Za-z_$][\w$]*)\s*\}', inline_text)):
                    v, _ = evaluate(nm, consts)
                    if v:
                        consts_text += ' ' + v
                rec['body_hosts'] = sorted({host_of(u) for u in URL_IN_TEXT.findall(consts_text)} - {None})
            if un:
                rec['unresolved'] = sorted(set(un))
            out.append(rec)
        if re.search(r'''from\s+['"]react-dom['"]''', masked):
            for m in re.finditer(r'\b(preconnect|prefetchDNS|preload|preinit)\s*\(', masked):
                arg_end = expr_end(masked, m.end())
                href, un = evaluate(masked[m.end():arg_end], consts)
                kind = {'preconnect': 'preconnect', 'prefetchDNS': 'dns-prefetch', 'preload': 'preload',
                        'preinit': 'preload'}[m.group(1)]
                out.append({'kind': kind, 'file': rel, 'line': line_at(masked, m.start()), 'href': href,
                            'host': host_of(href), 'via': f'react-dom {m.group(1)}()', **({'unresolved': un} if un else {})})
    out.sort(key=lambda s: (s['file'], s['line'], s['kind']))
    return out


# ---------------------------------------------------------------- next.config
def find_function_body(masked: str, name: str) -> tuple[int, int] | None:
    """(start, end) offsets of the body of `name() {...}`, `name: () => ...` or `name: async function () {...}`."""
    m = re.search(r'\b(?:async\s+)?' + name + r'\s*\(\s*\)\s*\{', masked) or \
        re.search(r'\b' + name + r'\s*:\s*(?:async\s+)?(?:function\s*\(\s*\)|\(\s*\)\s*=>)\s*[\{\[\(]', masked)
    if not m:
        return None
    start = m.end() - 1
    return start, matching(masked, start)


def objects_in(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """(start, end) of each top-level {...} inside masked[start:end] (an array literal's elements)."""
    out, i = [], start
    while i < end:
        c = masked[i]
        if c in '\'"`':
            i = skip_string(masked, i)
            continue
        if c == '{':
            j = matching(masked, i)
            out.append((i, j))
            i = j + 1
            continue
        i += 1
    return out


def prop(masked: str, start: int, end: int, name: str) -> tuple[str, int] | None:
    """The raw expression of `name:` at the top level of the object masked[start:end] (start at its '{'), and its offset."""
    depth, i = 0, start
    rx = re.compile(r'''(?:['"]?)''' + re.escape(name) + r'''(?:['"]?)\s*:\s*''')
    while i < end:
        c = masked[i]
        if c in '\'"`':
            if depth == 1:
                m = rx.match(masked, i)
                if m and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in '_$')):
                    e = expr_end(masked, m.end())
                    return masked[m.end():e].strip(), m.end()
            i = skip_string(masked, i)
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif depth == 1:
            m = rx.match(masked, i)
            if m and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in '_$')):
                e = expr_end(masked, m.end())
                return masked[m.end():e].strip(), m.end()
        i += 1
    return None


def array_elements(masked: str, expr: str, offset: int, consts: dict, depth: int = 0) -> list[tuple[int, int]]:
    """Object offsets of an array expression: a literal [...] at offset, a constant holding one, and ...spreads."""
    if depth > 4:
        return []
    expr = expr.strip()
    if expr.startswith('['):
        a = masked.find('[', offset)
        b = matching(masked, a)
        out = objects_in(masked, a + 1, b)
        for m in re.finditer(r'\.\.\.\s*([A-Za-z_$][\w$]*)', masked[a:b]):
            if m.group(1) in consts:
                cexpr, coff = consts[m.group(1)]
                out += array_elements(masked, cexpr, coff, consts, depth + 1)
        return out
    name = expr.split('(')[0].strip()
    if name in consts:
        cexpr, coff = consts[name]
        return array_elements(masked, cexpr, coff, consts, depth + 1)
    return []


def header_entries(masked: str, rel: str, body: tuple[int, int], consts: dict) -> list[dict]:
    out = []
    a, b = body
    # the returned array: its {source, headers} objects
    ret = re.search(r'\breturn\s*\[', masked[a:b])
    arr_start = a + ret.end() - 1 if ret else (a if masked[a] == '[' else masked.find('[', a))
    if arr_start < 0 or arr_start > b:
        return out
    arr_end = matching(masked, arr_start)
    objs = objects_in(masked, arr_start + 1, arr_end)
    for m in re.finditer(r'\.\.\.\s*([A-Za-z_$][\w$]*)', masked[arr_start:arr_end]):
        if m.group(1) in consts:
            objs += array_elements(masked, *consts[m.group(1)], consts)
    for s, e in objs:
        src = prop(masked, s, e + 1, 'source')
        if not src:
            continue
        source, su = evaluate(src[0], consts)
        hdrs = prop(masked, s, e + 1, 'headers')
        entry = {'source': source if source is not None else src[0], 'line': line_at(masked, src[1]), 'headers': []}
        if su:
            entry['unresolved'] = su
        if hdrs:
            for hs, he in array_elements(masked, hdrs[0], hdrs[1], consts):
                k = prop(masked, hs, he + 1, 'key')
                v = prop(masked, hs, he + 1, 'value')
                if not k:
                    continue
                key, _ = evaluate(k[0], consts)
                val, vu = evaluate(v[0], consts) if v else (None, [])
                h = {'key': key or k[0], 'value': re.sub(r'\s+', ' ', val).strip() if val else val,
                     'line': line_at(masked, k[1])}
                if vu:
                    h['unresolved'] = sorted(set(vu))
                entry['headers'].append(h)
        out.append(entry)
    return out


def route_entries(masked: str, body: tuple[int, int], consts: dict) -> list[dict]:
    a, b = body
    out = []
    for s, e in objects_in(masked, a + 1, b):
        inner = objects_in(masked, s + 1, e) if not prop(masked, s, e + 1, 'source') else [(s, e)]
        for os_, oe in inner:
            src = prop(masked, os_, oe + 1, 'source')
            if not src:
                continue
            dst = prop(masked, os_, oe + 1, 'destination')
            out.append({'source': evaluate(src[0], consts)[0] or src[0],
                        'destination': (evaluate(dst[0], consts)[0] or dst[0]) if dst else None,
                        'line': line_at(masked, src[1])})
    return out


def csp_entries(headers: list[dict], file: str) -> list[dict]:
    out = []
    for entry in headers:
        for h in entry.get('headers') or []:
            if (h.get('key') or '').lower() in ('content-security-policy', 'content-security-policy-report-only'):
                rec = {'source': entry.get('source'), 'file': file, 'line': h.get('line'), 'header': h['key'],
                       'directives': parse_csp(h.get('value') or '')}
                if h.get('unresolved'):
                    rec['unresolved'] = h['unresolved']
                out.append(rec)
    return out


def read_config(root: Path, rel: str, cache: dict) -> dict:
    masked, consts = cache_get(root, rel, cache)
    conf: dict = {'file': rel, 'headers': [], 'csp': [], 'images': None, 'redirects': None, 'rewrites': None,
                  'settings': {}, 'plugins': []}
    body = find_function_body(masked, 'headers')
    if body:
        conf['headers'] = header_entries(masked, rel, body, consts)
        conf['headers_line'] = line_at(masked, body[0])
    conf['csp'] = csp_entries(conf['headers'], rel)
    for kind in ('redirects', 'rewrites'):
        b = find_function_body(masked, kind)
        if b:
            conf[kind] = {'line': line_at(masked, b[0]), 'entries': route_entries(masked, b, consts)}
    m = re.search(r'\bimages\s*:\s*\{', masked)
    if m:
        s = m.end() - 1
        e = matching(masked, s)
        img: dict = {'line': line_at(masked, m.start()), 'remote_patterns': [], 'domains': []}
        rp = prop(masked, s, e + 1, 'remotePatterns')
        if rp:
            for ps, pe in array_elements(masked, rp[0], rp[1], consts):
                pat = {}
                for k in ('protocol', 'hostname', 'port', 'pathname', 'search'):
                    v = prop(masked, ps, pe + 1, k)
                    if v:
                        pat[k] = evaluate(v[0], consts)[0] or v[0]
                if not pat:   # new URL('https://...') entries
                    v, _ = evaluate(re.sub(r'^new\s+URL\s*\((.*)\)$', r'\1', masked[ps:pe + 1].strip()), consts)
                img['remote_patterns'].append(pat or {'url': masked[ps:pe + 1].strip()[:120]})
            for m2 in re.finditer(r'new\s+URL\s*\(\s*([\'"`][^\'"`]+[\'"`])\s*\)', rp[0]):
                u = unquote(m2.group(1))
                img['remote_patterns'].append({'protocol': urlparse(u).scheme, 'hostname': urlparse(u).hostname,
                                               'pathname': urlparse(u).path or None})
        dm = prop(masked, s, e + 1, 'domains')
        if dm:
            img['domains'] = [unquote(x.group(0)) for x in STRING.finditer(dm[0])]
        for k in ('unoptimized', 'loader', 'formats', 'dangerouslyAllowSVG', 'contentSecurityPolicy'):
            v = prop(masked, s, e + 1, k)
            if v:
                img[k] = literal_value(v[0], consts)
        conf['images'] = img
    for k in ('output', 'basePath', 'trailingSlash', 'poweredByHeader', 'assetPrefix', 'reactStrictMode'):
        m = re.search(r'(?<![\w.$])' + k + r'\s*:\s*', masked)
        if m:
            conf['settings'][k] = literal_value(masked[m.end():expr_end(masked, m.end())], consts)
    for plug, rx in (('next-pwa', r'''['"](?:next-pwa|@ducanh2912/next-pwa)['"]'''), ('serwist', r'''['"]@serwist/next['"]'''),
                     ('next-offline', r'''['"]next-offline['"]''')):
        if re.search(rx, masked):
            conf['plugins'].append(plug)
    return conf


def read_platform(root: Path, nroot: str, files: set) -> dict:
    out = {}
    base = f'{nroot}/' if nroot else ''
    vj = base + 'vercel.json'
    if vj in files:
        try:
            data = json.loads(read(root, vj))
        except ValueError as e:
            data = None
            out['vercel.json'] = {'file': vj, 'error': f'not valid JSON: {e}'}
        if isinstance(data, dict):
            heads = [{'source': h.get('source'), 'headers': [{'key': x.get('key'), 'value': x.get('value')}
                                                             for x in h.get('headers') or [] if isinstance(x, dict)]}
                     for h in data.get('headers') or [] if isinstance(h, dict)]
            out['vercel.json'] = {'file': vj, 'headers': heads, 'csp': csp_entries(heads, vj),
                                  'redirects': [{'source': r.get('source'), 'destination': r.get('destination')}
                                                for r in data.get('redirects') or [] if isinstance(r, dict)],
                                  'rewrites': [{'source': r.get('source'), 'destination': r.get('destination')}
                                               for r in data.get('rewrites') or [] if isinstance(r, dict)]}
    nt = base + 'netlify.toml'
    if nt in files:
        try:
            import tomllib
            data = tomllib.loads(read(root, nt))
            heads = [{'source': h.get('for'), 'headers': [{'key': k, 'value': v} for k, v in (h.get('values') or {}).items()]}
                     for h in data.get('headers') or [] if isinstance(h, dict)]
            out['netlify.toml'] = {'file': nt, 'headers': heads, 'csp': csp_entries(heads, nt),
                                   'redirects': [{'source': r.get('from'), 'destination': r.get('to'), 'status': r.get('status')}
                                                 for r in data.get('redirects') or [] if isinstance(r, dict)]}
        except Exception as e:   # noqa: BLE001  (tomllib missing before Python 3.11, or invalid TOML)
            out['netlify.toml'] = {'file': nt, 'error': f'not read: {e}'}
    hf = base + 'public/_headers'
    if hf in files:
        heads, cur = [], None
        for line in read(root, hf).splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if not line[:1].isspace():
                cur = {'source': line.strip(), 'headers': []}
                heads.append(cur)
            elif cur is not None and ':' in line:
                k, v = line.strip().split(':', 1)
                cur['headers'].append({'key': k.strip(), 'value': v.strip()})
        out['public/_headers'] = {'file': hf, 'headers': heads, 'csp': csp_entries(heads, hf)}
    return out


# ---------------------------------------------------------------- middleware, directives
def read_middleware(root: Path, files: set, nroot: str, cache: dict) -> dict | None:
    base = f'{nroot}/' if nroot else ''
    for d in ('', 'src/'):
        for stem in ('middleware', 'proxy'):
            for ext in ('.ts', '.js', '.mjs', '.mts', '.tsx'):
                rel = f'{base}{d}{stem}{ext}'
                if rel not in files:
                    continue
                masked, consts = cache_get(root, rel, cache)
                m = re.search(r'export\s+(?:default\s+)?(?:async\s+)?function\s*(?:middleware|proxy)?\b|'
                              r'export\s+(?:const\s+(?:middleware|proxy)\b|default\b)', masked)
                rec = {'file': rel, 'line': line_at(masked, m.start()) if m else 1, 'matcher': None}
                mm = re.search(r'\bmatcher\s*:\s*', masked)
                if mm:
                    end = expr_end(masked, mm.end())
                    expr = masked[mm.end():end]
                    vals, un = [], []
                    if expr.strip().startswith('['):
                        for item in split_top(expr.strip()[1:-1], ','):
                            item = item.strip()
                            if not item:
                                continue
                            if item.startswith('{'):
                                v = string_after(item, 'source')
                                u = [] if v is not None else [item[:60]]
                            else:
                                v, u = evaluate(item, consts)
                            un += u
                            if v is not None:
                                vals.append(v)
                    else:
                        v, un = evaluate(expr, consts)
                        vals = [v] if v is not None else []
                    rec['matcher'] = vals
                    rec['matcher_line'] = line_at(masked, mm.start())
                    if un:
                        rec['unresolved'] = un
                return rec
    return None


def read_directives(root: Path, files: list[str], cache: dict) -> tuple[list[dict], list[dict]]:
    actions, clients = [], []
    for rel in files:
        if not rel.endswith(JS_EXT) or is_test(rel) or rel.startswith('public/') or '/public/' in rel:
            continue
        text = read(root, rel)
        if 'use client' not in text and 'use server' not in text:
            continue
        masked, _ = cache_get(root, rel, cache)
        top_client = directive(masked, 'use client')
        top_server = directive(masked, 'use server')
        if top_client:
            clients.append({'file': rel, 'line': top_client})
        if top_server:
            actions.append({'file': rel, 'line': top_server, 'scope': 'file'})
        for m in re.finditer(r'''\{\s*(['"])use server\1''', masked):
            actions.append({'file': rel, 'line': line_at(masked, m.start(1)), 'scope': 'inline'})
    return actions, clients


# ---------------------------------------------------------------- service workers
def classify_strategy(text: str) -> tuple[str, bool]:
    """A guess at the caching strategy a fetch-handler branch uses, and whether it writes to a cache."""
    fetch = re.search(r'(?<![\w.])fetch\s*\(', text)
    match = re.search(r'\b(?:caches|cache|\w*[cC]ache\w*)\s*\.\s*match\s*\(|\bmatchPrecache\s*\(', text)
    put = bool(re.search(r'\.\s*(?:put|add|addAll)\s*\(', text))
    if not fetch and not match:
        return ('synthetic' if re.search(r'new\s+Response\s*\(', text) else 'unknown'), put
    if fetch and not match:
        return 'network-only', put
    if match and not fetch:
        return 'cache-only', put
    if fetch.start() < match.start():
        return 'network-first', put
    if re.search(r'(?:const|let|var)\s+\w+\s*=\s*fetch\s*\(', text) or re.search(r'waitUntil\s*\([^)]*fetch', text):
        return 'stale-while-revalidate', put
    return 'cache-first', put


def when_of(cond: str) -> str:
    c = cond
    if 'navigate' in c or re.search(r'''destination\s*===?\s*['"]document['"]''', c):
        return 'navigation'
    if 'origin' in c:
        return 'cross-origin' if re.search(r'!==?|!\s*\w', c) else 'same-origin'
    if 'method' in c:
        return 'method'
    if 'destination' in c:
        return 'destination'
    if re.search(r'pathname|\.url\b|startsWith|endsWith|includes|\.test\s*\(|match\s*\(', c):
        return 'path'
    return 'other'


def fetch_strategies(masked: str) -> list[dict]:
    m = re.search(r'''addEventListener\s*\(\s*['"]fetch['"]\s*,\s*''', masked) or re.search(r'\bonfetch\s*=\s*', masked)
    if not m:
        return []
    i = m.end()
    nm = re.match(r'([A-Za-z_$][\w$]*)\s*\)', masked[i:])
    if nm:   # addEventListener('fetch', handleFetch)
        fn = re.search(r'function\s+' + re.escape(nm.group(1)) + r'\s*\([^)]*\)\s*\{|\b' + re.escape(nm.group(1)) +
                       r'\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{', masked)
        if not fn:
            return []
        start = fn.end() - 1
    else:
        start = masked.find('{', i)
        if start < 0:
            return []
    end = matching(masked, start)
    out, taken = [], []
    for im in re.finditer(r'\bif\s*\(', masked[start:end]):
        pos = start + im.start()
        if any(a <= pos <= b for a, b in taken):
            continue
        cs = start + im.end() - 1
        ce = matching(masked, cs)
        cond = re.sub(r'\s+', ' ', masked[cs + 1:ce]).strip()
        k = ce + 1
        while k < end and masked[k].isspace():
            k += 1
        if masked[k:k + 1] == '{':
            be = matching(masked, k)
            block = masked[k:be + 1]
        else:
            be = expr_end(masked, k, newline_ends=True)
            block = masked[k:be]
        if 'respondWith' in block:
            strategy, put = classify_strategy(block)
        elif re.match(r'\{?\s*return\s*;?\s*\}?$', block.strip()):
            strategy, put = 'passthrough', False
        else:
            continue
        taken.append((pos, be))
        out.append({'when': when_of(cond), 'condition': cond[:160], 'line': line_at(masked, pos), 'strategy': strategy,
                    'caches_response': put})
    rest = list(masked[start:end + 1])
    for a, b in taken:
        for x in range(a - start, min(b - start + 1, len(rest))):
            rest[x] = ' '
    rest_text = ''.join(rest)
    if 'respondWith' in rest_text:
        rw = rest_text.find('respondWith')
        strategy, put = classify_strategy(rest_text)
        out.append({'when': 'default', 'condition': None, 'line': line_at(masked, start + rw), 'strategy': strategy,
                    'caches_response': put})
    elif not out:
        out.append({'when': 'default', 'condition': None, 'line': line_at(masked, m.start()),
                    'strategy': 'passthrough', 'caches_response': False})
    return out


def analyse_worker(text: str) -> dict:
    masked = mask_comments(text)
    consts = constants(masked)
    events = sorted(set(re.findall(r'''addEventListener\s*\(\s*['"](\w+)['"]''', masked)) |
                    set(re.findall(r'\bon(install|activate|fetch|push|sync|message|notificationclick)\s*=', masked)))
    names = set()
    for m in re.finditer(r'caches\s*\.\s*open\s*\(\s*', masked):
        v, _ = evaluate(masked[m.end():expr_end(masked, m.end())], consts)
        if v:
            names.add(v)
    pre = 0
    for m in re.finditer(r'\.addAll\s*\(\s*', masked):
        arg = masked[m.end():expr_end(masked, m.end())].strip()
        if arg in consts:
            arg = consts[arg][0]
        pre += len(STRING.findall(arg))
    return {'events': events, 'skip_waiting': bool(re.search(r'skipWaiting\s*\(', masked)),
            'clients_claim': bool(re.search(r'clients\s*\.\s*claim\s*\(', masked)),
            'cache_names': sorted(names), 'precache_count': pre, 'fetch_strategies': fetch_strategies(masked),
            'heuristic': True,
            'imports': sorted(set(re.findall(r'''importScripts\s*\(\s*['"]([^'"]+)['"]''', masked)))}


def read_service_workers(root: Path, files: list[str], nroot: str, cache: dict, plugins: list[str]) -> list[dict]:
    base = f'{nroot}/' if nroot else ''
    regs = []
    for rel in files:
        if not rel.endswith(JS_EXT + ('.html',)) or is_test(rel):
            continue
        text = read(root, rel)
        if 'serviceWorker' not in text:
            continue
        masked, consts = cache_get(root, rel, cache)
        for m in re.finditer(r'serviceWorker\s*\.\s*register\s*\(\s*', masked):
            end = expr_end(masked, m.end())
            arg = masked[m.end():end].strip()
            nu = re.match(r'new\s+URL\s*\(\s*(.+?)\s*,', arg)
            url, un = evaluate(nu.group(1) if nu else arg, consts)
            scope = None
            if masked[end:end + 1] == ',':
                oend = expr_end(masked, end + 1)
                scope = string_after(masked[end + 1:oend], 'scope')
            regs.append({'file': rel, 'line': line_at(masked, m.start()), 'url': url if url is not None else arg,
                         'scope': scope, **({'unresolved': un} if un else {})})
    by_url: dict = {}
    for r in regs:
        u = r['url'] or ''
        path = u.split('?')[0]
        if path.startswith('./'):
            path = '/' + path[2:]
        by_url.setdefault(path, []).append(r)
    fileset = set(files)
    out = []
    for path, rs in sorted(by_url.items()):
        cand = None
        if path.startswith('/'):
            for c in (f'{base}public{path}', f'{base}{path.lstrip("/")}'):
                if c in fileset:
                    cand = c
                    break
        if not cand:
            name = PurePosixPath(path).name
            hits = [f for f in files if f.startswith(f'{base}public/') and PurePosixPath(f).name == name]
            cand = hits[0] if len(hits) == 1 else None
        rec = {'url': path, 'worker_file': cand, 'exists': bool(cand), 'registrations': rs,
               'generated_by': plugins[0] if plugins else None}
        if cand:
            rec.update(analyse_worker(read(root, cand)))
        elif plugins:
            rec['note'] = f'the worker is generated at build time by {plugins[0]}; read its configuration in next.config'
        else:
            rec['note'] = 'the registered worker file was not found in public/'
        out.append(rec)
    known = {r['worker_file'] for r in out}
    for f in files:   # workers in public/ that nothing in the source registers
        if f.startswith(f'{base}public/') and f.endswith('.js') and f not in known:
            t = read(root, f)
            if re.search(r'''self\.addEventListener\s*\(\s*['"](?:fetch|install)['"]''', t):
                out.append({'url': '/' + f[len(f'{base}public/'):], 'worker_file': f, 'exists': True, 'registrations': [],
                            'generated_by': plugins[0] if plugins else None,
                            'note': 'a service worker in public/ that no source file registers (it may be registered '
                                    'by a plugin, by HTML outside the source, or not at all)', **analyse_worker(t)})
    return out


# ---------------------------------------------------------------- entry point
def read_next_app(repo, max_files: int = 50_000, texts: dict | None = None) -> dict:
    """The static model of every Next.js project under repo (see the module docstring for the shape). texts maps
    repo-relative '/'-separated paths to the text a caller has already read, so those files are not read again."""
    token = _GIVEN_TEXTS.set(texts)
    try:
        return _read_next_app(Path(repo), repo, max_files)
    finally:
        _GIVEN_TEXTS.reset(token)


def _read_next_app(root: Path, repo, max_files: int) -> dict:
    files, truncated = walk_files(root, max_files)
    notes = []
    if truncated:
        notes.append(f'stopped listing files at {max_files}; the model may be incomplete')
    projects = []
    nroots = find_next_roots(root, files)
    for nroot in nroots:
        inner_roots = [r for r in nroots if r != nroot and (not nroot or r.startswith(nroot + '/'))]
        pfiles = [f for f in files if under(f, nroot) is not None
                  and not any(under(f, r) is not None for r in inner_roots)]
        fileset = set(pfiles)
        base = f'{nroot}/' if nroot else ''
        cache: dict = {}
        app = next((d for d in (f'{base}app', f'{base}src/app') if any(f.startswith(d + '/') for f in pfiles)), None)
        pages = next((d for d in (f'{base}pages', f'{base}src/pages') if any(f.startswith(d + '/') for f in pfiles)), None)
        version = None
        if f'{base}package.json' in fileset:
            try:
                pkg = json.loads(read(root, f'{base}package.json'))
                version = next((pkg[k]['next'] for k in ('dependencies', 'devDependencies')
                                if isinstance(pkg.get(k), dict) and 'next' in pkg[k]), None)
            except (ValueError, AttributeError):
                pass
        cfg_file = next((f for f in pfiles if under(f, nroot) is not None and NEXT_CONFIG.match(under(f, nroot))), None)
        config = read_config(root, cfg_file, cache) if cfg_file else None
        r = read_routes(root, pfiles, nroot, app, pages, cache)
        actions, clients = read_directives(root, pfiles, cache)
        scripts = read_scripts(root, pfiles, cache)
        sws = read_service_workers(root, pfiles, nroot, cache, (config or {}).get('plugins') or [])
        proj = {'root': nroot, 'next_version': version,
                'routers': [k for k, v in (('app', app), ('pages', pages)) if v], 'app_dir': app, 'pages_dir': pages,
                'routes': r['routes'], 'segments': r['segments'], 'root_special': r['root_special'],
                'pages_special': r['pages_special'], 'middleware': read_middleware(root, fileset, nroot, cache),
                'server_actions': actions, 'client_components': clients, 'scripts': scripts, 'config': config,
                'platform': read_platform(root, nroot, fileset), 'service_workers': sws}
        proj['counts'] = {'pages': sum(x['kind'] in ('page', 'pages_page') for x in r['routes']),
                          'route_handlers': sum(x['kind'] in ('route_handler', 'pages_api') for x in r['routes']),
                          'metadata': sum(x['kind'] == 'metadata' for x in r['routes']),
                          'client_components': len(clients), 'server_actions': len(actions), 'scripts': len(scripts),
                          'service_workers': len(sws)}
        projects.append(proj)
    return {'tool': 'next_app', 'schema_version': 1, 'repo': str(repo), 'is_next': bool(projects),
            'projects': projects, 'notes': notes}


# ---------------------------------------------------------------- self-test
# A bike shop on the App Router: a route group, a dynamic segment with generateStaticParams and dynamicParams = false,
# an optional catch-all, a parallel @modal slot with an intercepting route, a route handler, metadata routes, root
# special files (no global-error), middleware, a server action file, client components, next/script loaders and
# resource hints, a CSP built in next.config headers(), images remotePatterns, vercel.json headers, a pages/ router
# page and API route, and a service worker in public/ registered from a client component.
FIXTURE = {
    'package.json': '{"name": "spokes", "private": true, "dependencies": {"next": "15.1.0", "react": "19.0.0"}}\n',
    'next.config.ts': (
        "import type { NextConfig } from 'next';\n"
        "\n"
        "const ANALYTICS = 'https://stats.example-analytics.test';\n"
        "// a comment with a URL https://ignored.example.test that is not a source\n"
        "const csp = [\n"
        "  \"default-src 'self'\",\n"
        "  `script-src 'self' 'unsafe-inline' ${ANALYTICS} https://cdn.example-tags.test`,\n"
        "  \"img-src 'self' data: https://images.example-cdn.test\",\n"
        "  \"frame-ancestors 'none'\",\n"
        "].join('; ');\n"
        "\n"
        "const securityHeaders = [\n"
        "  { key: 'Content-Security-Policy', value: csp },\n"
        "  { key: 'X-Frame-Options', value: 'DENY' },\n"
        "];\n"
        "\n"
        "const nextConfig: NextConfig = {\n"
        "  poweredByHeader: false,\n"
        "  images: {\n"
        "    remotePatterns: [{ protocol: 'https', hostname: 'images.example-cdn.test', pathname: '/bikes/**' }],\n"
        "  },\n"
        "  async headers() {\n"
        "    return [\n"
        "      { source: '/:path*', headers: securityHeaders },\n"
        "      { source: '/sw.js', headers: [{ key: 'Cache-Control', value: 'no-cache' }] },\n"
        "    ];\n"
        "  },\n"
        "  async redirects() {\n"
        "    return [{ source: '/shop', destination: '/bikes', permanent: true }];\n"
        "  },\n"
        "};\n"
        "\n"
        "export default nextConfig;\n"),
    'vercel.json': ('{"headers": [{"source": "/(.*)", "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"}]}],\n'
                    ' "redirects": [{"source": "/old-bikes", "destination": "/bikes", "permanent": true}]}\n'),
    'middleware.ts': ("import { NextResponse } from 'next/server';\n\n"
                      "export function middleware(request) {\n  return NextResponse.next();\n}\n\n"
                      "export const config = { matcher: ['/bikes/:path*', '/api/:path*'] };\n"),
    'app/layout.tsx': (
        "import Script from 'next/script';\n"
        "import { GoogleTagManager } from '@next/third-parties/google';\n"
        "import SwRegister from '../components/SwRegister';\n"
        "\n"
        "const ANALYTICS = 'https://stats.example-analytics.test';\n"
        "\n"
        "export default function RootLayout({ children }) {\n"
        "  return (\n"
        "    <html lang=\"en\">\n"
        "      <head>\n"
        "        <link rel=\"preconnect\" href=\"https://cdn.example-tags.test\" />\n"
        "        <link rel=\"dns-prefetch\" href=\"//stats.example-analytics.test\" />\n"
        "      </head>\n"
        "      <body>\n"
        "        {children}\n"
        "        <Script src={`${ANALYTICS}/tag.js?site=demo`} strategy=\"afterInteractive\" />\n"
        "        <Script id=\"tag-init\" strategy=\"afterInteractive\">\n"
        "          {`window.queue = []; window.queue.push('https://stats.example-analytics.test/collect');`}\n"
        "        </Script>\n"
        "        <script async src=\"https://widgets.example-chat.test/w.js\"></script>\n"
        "        <GoogleTagManager gtmId=\"GTM-DEMO\" />\n"
        "        <SwRegister />\n"
        "      </body>\n"
        "    </html>\n"
        "  );\n"
        "}\n"),
    'app/page.tsx': "export default function Home() {\n  return <h1>Spokes</h1>;\n}\n",
    'app/not-found.tsx': "export default function NotFound() {\n  return <p>No such bike</p>;\n}\n",
    'app/error.tsx': "'use client';\n\nexport default function Error() {\n  return <p>Something broke</p>;\n}\n",
    'app/robots.ts': "export default function robots() {\n  return { rules: { userAgent: '*' } };\n}\n",
    'app/sitemap.ts': "export default function sitemap() {\n  return [];\n}\n",
    'app/icon.png': 'not really a png\n',
    'app/actions.ts': "'use server';\n\nexport async function reserve(id) {\n  return id;\n}\n",
    'app/(shop)/bikes/page.tsx': "export default function Bikes() {\n  return <ul />;\n}\n",
    'app/(shop)/bikes/[slug]/page.tsx': (
        "export const dynamicParams = false;\n"
        "export const revalidate = 3600;\n"
        "\n"
        "export async function generateStaticParams() {\n"
        "  return [{ slug: 'gravel' }, { slug: 'road' }];\n"
        "}\n"
        "\n"
        "export default function Bike({ params }) {\n"
        "  return <h1>{params.slug}</h1>;\n"
        "}\n"),
    'app/(shop)/bikes/[slug]/loading.tsx': "export default function Loading() {\n  return <p>Loading</p>;\n}\n",
    'app/(shop)/bikes/[slug]/opengraph-image.tsx': ("export const size = { width: 1200, height: 630 };\n\n"
                                                    "export default async function Image() {\n  return null;\n}\n"),
    'app/(shop)/bikes/[slug]/BuyButton.tsx': ("'use client';\n\nexport default function BuyButton() {\n"
                                              "  async function buy() {\n    'use server';\n    return 1;\n  }\n"
                                              "  return <button>Buy</button>;\n}\n"),
    'app/docs/[[...path]]/page.tsx': "export default function Docs() {\n  return <article />;\n}\n",
    'app/@modal/default.tsx': "export default function Default() {\n  return null;\n}\n",
    'app/@modal/(.)bikes/[slug]/page.tsx': "export default function BikeModal() {\n  return <dialog />;\n}\n",
    'app/api/stock/route.ts': ("export const dynamic = 'force-dynamic';\n\n"
                               "export async function GET() {\n  return Response.json({ left: 3 });\n}\n"),
    'components/SwRegister.tsx': (
        "'use client';\n"
        "import { useEffect } from 'react';\n"
        "\n"
        "export default function SwRegister() {\n"
        "  useEffect(() => {\n"
        "    if ('serviceWorker' in navigator) {\n"
        "      navigator.serviceWorker.register('/sw.js', { scope: '/' });\n"
        "    }\n"
        "  }, []);\n"
        "  return null;\n"
        "}\n"),
    'components/SwRegister.test.tsx': "navigator.serviceWorker.register('/test-only.js');\n",
    'public/sw.js': (
        "const CACHE = 'spokes-v1';\n"
        "self.addEventListener('install', () => self.skipWaiting());\n"
        "self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));\n"
        "self.addEventListener('fetch', (event) => {\n"
        "  const url = new URL(event.request.url);\n"
        "  if (url.origin !== self.location.origin) return;\n"
        "  if (event.request.mode === 'navigate') {\n"
        "    event.respondWith(fetch(event.request));\n"
        "    return;\n"
        "  }\n"
        "  if (url.pathname.startsWith('/_next/static/')) {\n"
        "    event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));\n"
        "    return;\n"
        "  }\n"
        "  event.respondWith(caches.open(CACHE).then((cache) => cache.match(event.request).then((cached) => {\n"
        "    const network = fetch(event.request).then((res) => { cache.put(event.request, res.clone()); return res; });\n"
        "    return cached || network;\n"
        "  })));\n"
        "});\n"),
    'pages/legacy.tsx': "export default function Legacy() {\n  return <p>old</p>;\n}\n",
    'pages/api/ping.ts': "export default function handler(req, res) {\n  res.status(200).end();\n}\n",
    'node_modules/next/dist/server.js': "navigator.serviceWorker.register('/never.js');\n",
}


def self_test() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, body in FIXTURE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding='utf-8')
        model = read_next_app(root)
        json.dumps(model)   # JSON-able
        # a caller that has already read the files passes their texts and the reader uses them instead of the disk
        given = {'app/api/stock/route.ts': FIXTURE['app/api/stock/route.ts']
                 + '\nexport async function POST() {\n  return Response.json({});\n}\n'}
        reused = read_next_app(root, texts=given)
        (root / 'empty').mkdir()
        empty = read_next_app(root / 'empty')
    checks = []
    proj = (model.get('projects') or [{}])[0]

    def check(name, ok, extra=None):
        checks.append((name, bool(ok), extra))
    check('a Next.js project is found at the root', model.get('is_next') and proj.get('root') == ''
          and proj.get('next_version') == '15.1.0', {k: proj.get(k) for k in ('root', 'next_version')})
    stock = next((r for r in (reused.get('projects') or [{}])[0].get('routes') or [] if r.get('url') == '/api/stock'), {})
    check("texts= the caller already read are used instead of the files (a POST only the given text has is listed)",
          'POST' in {m.get('method') for m in stock.get('methods') or []}, stock.get('methods'))
    routes = proj.get('routes') or []
    by = {(r.get('kind'), r.get('url')): r for r in routes}
    check('pages: the home page and a route-group page, groups stripped',
          ('page', '/') in by and ('page', '/bikes') in by and by[('page', '/bikes')]['file'] == 'app/(shop)/bikes/page.tsx',
          sorted(by))
    bike = next((r for r in routes if r.get('file') == 'app/(shop)/bikes/[slug]/page.tsx'), {})
    cfg = bike.get('segment_config') or {}
    check('a dynamic segment page carries generateStaticParams, dynamicParams and revalidate with their lines',
          bike.get('params') == ['slug'] and (cfg.get('generate_static_params') or {}).get('line') == 4
          and (cfg.get('dynamic_params') or {}).get('value') is False and (cfg.get('dynamic_params') or {}).get('line') == 1
          and (cfg.get('revalidate') or {}).get('value') == 3600, bike)
    docs = by.get(('page', '/docs/{path*}'), {})
    check('an optional catch-all is marked', docs.get('optional_catch_all') is True, docs)
    modal = [r for r in routes if r.get('file') == 'app/@modal/(.)bikes/[slug]/page.tsx']
    check('a page in a parallel slot that intercepts a route is marked with both',
          len(modal) == 1 and modal[0].get('slot') == '@modal' and modal[0].get('intercepts') is True
          and modal[0].get('url') == '/bikes/{slug}', modal)
    api = by.get(('route_handler', '/api/stock'), {})
    check('a route handler lists its methods with export lines and its segment config',
          api.get('methods') == [{'method': 'GET', 'line': 3}]
          and ((api.get('segment_config') or {}).get('dynamic') or {}).get('value') == 'force-dynamic', api)
    meta = {r['url']: r for r in routes if r.get('kind') == 'metadata'}
    check('metadata routes: robots, sitemap, icon file and a generated opengraph image',
          meta.get('/robots.txt', {}).get('metadata') == 'robots' and meta.get('/sitemap.xml', {}).get('form') == 'generated'
          and meta.get('/icon.png', {}).get('form') == 'file'
          and meta.get('/bikes/{slug}/opengraph-image', {}).get('form') == 'generated', sorted(meta))
    check('pages/ router page and API route',
          ('pages_page', '/legacy') in by and ('pages_api', '/api/ping') in by, sorted(by))
    segs = {s.get('dir'): s for s in proj.get('segments') or []}
    rs = proj.get('root_special') or {}
    check('special files per segment, and what the root lacks (global-error, loading)',
          set((segs.get('app') or {}).get('files', {})) >= {'layout', 'not-found', 'error'}
          and 'loading' in (segs.get('app/(shop)/bikes/[slug]') or {}).get('files', {})
          and 'default' in (segs.get('app/@modal') or {}).get('files', {})
          and 'global-error' in rs.get('missing', []) and 'loading' in rs.get('missing', [])
          and 'not-found' in rs.get('present', []), (sorted(segs), rs))
    mw = proj.get('middleware') or {}
    check('middleware and its matcher', mw.get('file') == 'middleware.ts' and mw.get('line') == 3
          and mw.get('matcher') == ['/bikes/:path*', '/api/:path*'], mw)
    acts = {(a['file'], a['scope']) for a in proj.get('server_actions') or []}
    check("server actions: a 'use server' file and an inline action",
          ('app/actions.ts', 'file') in acts and ('app/(shop)/bikes/[slug]/BuyButton.tsx', 'inline') in acts, acts)
    clients = {c['file'] for c in proj.get('client_components') or []}
    check("client components ('use client'), tests left out",
          {'app/error.tsx', 'components/SwRegister.tsx', 'app/(shop)/bikes/[slug]/BuyButton.tsx'} <= clients
          and not any('.test.' in c for c in clients), clients)
    scripts = proj.get('scripts') or []
    sk = {(s.get('kind'), s.get('host'), s.get('line')) for s in scripts}
    check('next/script loaders with resolved host and strategy, an inline <Script id>, a JSX <script src>, '
          'resource hints and a @next/third-parties loader, each at its line',
          {('next/script', 'stats.example-analytics.test', 16), ('script', 'widgets.example-chat.test', 20),
           ('preconnect', 'cdn.example-tags.test', 11), ('dns-prefetch', 'stats.example-analytics.test', 12),
           ('next/third-parties', 'www.googletagmanager.com', 21)} <= sk
          and any(s.get('kind') == 'next/script' and s.get('inline') and s.get('id') == 'tag-init'
                  and 'stats.example-analytics.test' in (s.get('body_hosts') or []) and s.get('line') == 17 for s in scripts)
          and any(s.get('strategy') == 'afterInteractive' and s.get('line') == 16 for s in scripts), scripts)
    conf = proj.get('config') or {}
    hdr = {h.get('source'): h for h in conf.get('headers') or []}
    all_keys = {x.get('key') for x in (hdr.get('/:path*') or {}).get('headers', [])}
    check('next.config headers(): each source with its header names and values',
          all_keys == {'Content-Security-Policy', 'X-Frame-Options'}
          and [x.get('value') for x in (hdr.get('/sw.js') or {}).get('headers', [])] == ['no-cache'], hdr)
    csp = (conf.get('csp') or [{}])[0]
    dirs = csp.get('directives') or {}
    check('the CSP built from constants is parsed into directives and sources',
          dirs.get('script-src') == ["'self'", "'unsafe-inline'", 'https://stats.example-analytics.test',
                                     'https://cdn.example-tags.test']
          and dirs.get('frame-ancestors') == ["'none'"] and csp.get('source') == '/:path*'
          and 'https://ignored.example.test' not in json.dumps(dirs), csp)
    img = conf.get('images') or {}
    check('images remotePatterns, redirects and other settings',
          [p.get('hostname') for p in img.get('remote_patterns') or []] == ['images.example-cdn.test']
          and (conf.get('redirects') or {}).get('entries', [{}])[0].get('source') == '/shop'
          and (conf.get('settings') or {}).get('poweredByHeader') is False, conf)
    plat = (proj.get('platform') or {}).get('vercel.json') or {}
    check('vercel.json headers and redirects',
          any(x.get('key') == 'X-Content-Type-Options' for h in plat.get('headers') or [] for x in h.get('headers', []))
          and (plat.get('redirects') or [{}])[0].get('source') == '/old-bikes', plat)
    sws = proj.get('service_workers') or []
    sw = sws[0] if len(sws) == 1 else {}
    reg = (sw.get('registrations') or [{}])[0]
    strat = {(s.get('when'), s.get('strategy')) for s in sw.get('fetch_strategies') or []}
    check('the service worker: where it is registered, its file, and its fetch strategies',
          reg.get('file') == 'components/SwRegister.tsx' and reg.get('line') == 7 and reg.get('scope') == '/'
          and sw.get('worker_file') == 'public/sw.js' and sw.get('url') == '/sw.js'
          and {('cross-origin', 'passthrough'), ('navigation', 'network-only'), ('path', 'cache-first'),
               ('default', 'stale-while-revalidate')} <= strat
          and 'spokes-v1' in (sw.get('cache_names') or []) and sw.get('skip_waiting') is True, sws)
    tpl = "`\n  default-src 'self';\n  img-src 'self' ${CDN};\n`.replace(/\\s{2,}/g, ' ').trim()"
    v, un = evaluate(tpl, {'CDN': ("'https://img.example.test'", 0)})
    check('a CSP template cleaned with .replace(...).trim() is evaluated',
          parse_csp(v or '') == {'default-src': ["'self'"], 'img-src': ["'self'", 'https://img.example.test']} and not un,
          (v, un))
    check('a folder with no Next.js project says so', empty.get('is_next') is False and empty.get('projects') == [], empty)
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
    ap = argparse.ArgumentParser(description='Read a Next.js project statically (routes, special files, middleware, '
                                             'directives, script loaders, next.config headers and CSP, service workers).')
    ap.add_argument('repo')
    ap.add_argument('--out', help='write the JSON here instead of printing it')
    ap.add_argument('--max-files', type=int, default=50_000)
    args = ap.parse_args(argv)
    if not Path(args.repo).is_dir():
        print(f'not a directory: {args.repo}', file=sys.stderr)
        return 2
    model = read_next_app(args.repo, args.max_files)
    text = json.dumps(model, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        for p in model['projects']:
            c = p['counts']
            print(f"{p['root'] or '.'}: {c['pages']} page(s), {c['route_handlers']} route handler(s), {c['metadata']} "
                  f"metadata route(s), {c['scripts']} script/hint(s), {c['service_workers']} service worker(s)")
        if not model['is_next']:
            print('no Next.js project found (no next.config.* and no package.json depending on next)')
    else:
        print(text)
    return 0 if model['is_next'] else 3


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


if __name__ == '__main__':
    raise SystemExit(main())
