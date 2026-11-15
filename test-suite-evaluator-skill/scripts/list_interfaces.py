#!/usr/bin/env python3
"""List an application's interfaces and the test files that reach them (stdlib only).

    python list_interfaces.py <repo> [--tests <dir> ...] [--out interfaces.json]
    python list_interfaces.py --self-test

An optional helper for SKILL.md Step 4 (interface coverage). It reads source, never imports or runs
it, so it works without the application's dependencies:

    FastAPI    @app.get/post/.../api_route/websocket and add_api_route on FastAPI() and APIRouter()
               objects (and a project's own router subclass, or a class or factory whose name ends in
               Router/router), with APIRouter(prefix=...) and include_router(router, prefix=...)
               resolved across modules (relative and absolute imports)
    Flask      @app.route / @bp.route / @bp.get ..., add_url_rule, Blueprint(url_prefix=...) and
               register_blueprint(bp, url_prefix=...)
    Azure      @app.route(route=...) on a FunctionApp or functions Blueprint, under host.json's
    Functions  routePrefix (default api)
               Python is read by py_routes.py, the route reader shared with the journey mapper, the
               API contract tester and the security auditor: it parses with ast, so decorators in
               strings and docstrings are never read
    Next.js    app/**/route.(ts|js) exported GET/POST/... handlers at the line of each export (route
               groups dropped, [id] -> {id}), and pages/api/** files (method ANY). In a Next.js project
               (a next.config.* or a package.json depending on next) also: App Router pages
               (page.tsx/jsx/js/mdx, kind pages; [...x] and [[...x]] catch-alls, generateStaticParams
               and dynamicParams recorded, a parallel-route @slot page at its parent's URL with `slot`,
               an intercepting (.)x/(..)x/(...)x page at the URL it intercepts with `intercepts`),
               metadata routes (robots, sitemap, manifest, favicon.ico, icon, apple-icon,
               opengraph-image and twitter-image, as files or generators: framework nextjs-metadata),
               and pages/ router pages. What such a project has that is not read (server actions,
               middleware, public/ files, next.config rewrites and redirects) is listed in `not_read`
               and on stderr, so a partial list never looks complete.
    Express    app.get/post/... and router.get/post/... with a literal path (pattern-based; a
               router's mount prefix in another file is not resolved: those are marked
               prefix_unresolved)

With --tests, every string literal in the test files (Python string and f-string literals, with
the f-string's fields as wildcards; JavaScript strings and template literals) is matched against
each interface's path template ({id} and :id match one segment); the HTTP method is taken from the
call around the literal when it is a client method (client.get(...), request(app).post(...)). An
interface is "reached" when a test literal matches its path and the method agrees or is unknown.
Files under --tests that declare one of the listed interfaces are product code and are not read as tests;
when none of the files under --tests looks like a test, a warning goes to stderr and `tests_warnings`. For a
repository with no tests, --no-tests lists every interface as not reached (summary.tests: none).
This is test_derived evidence: it says a test names the URL, not which code ran or whether the
real authentication dependency was in place (check fixtures for dependency overrides and mocked
auth, and record that as a separate interface_coverage entry).

Besides HTTP routes (kind `routes`), it lists the interfaces of a library, a CLI or a tool server:

    cli              package.json "bin" entries (node-bin), commander / yargs / cac sub-commands
                     (`program.command('run')`), Python argparse sub-commands and click/typer commands
                     (from py_routes.py), pyproject [project.scripts], Cargo [[bin]] and src/main.rs
    package_exports  package.json "exports" subpaths (or "main") of every package that is not private
    tools            MCP tools: registerTool('name') / tool('name') on an @modelcontextprotocol/sdk server
                     (transport noted: stdio or HTTP), and Python FastMCP @mcp.tool() functions
    routes           also a plain Node server (http.createServer) as one ANY / entry: its routing is in
                     the handler, so read it by hand; app.mount(path, StaticFiles(...)) and Flask static
                     folders (py_extras.py); PHP front controllers routing on REQUEST_URI (pattern only)
    cli              also a flags-only argparse program (one entry with its options) and sys.argv[1]
                     dispatch (`if command == "pack":`)

Routes, includes and middleware under an `if` carry `condition`; `middleware` lists add_middleware and
@app.middleware functions. dist/, build/ and out/ are skipped only when something produces them (skipped_dirs,
--include DIR); a committed folder nothing produces is read as source (source_build_dirs).

Go (go.mod and *.go, read with comment- and string-aware patterns, never compiled):

    cli              urfave/cli tables (cli.App / cli.Command literals, Commands and Subcommands lists,
                     elided {Name: ...} elements, commands held in variables or returned by functions),
                     cobra (Use: and parent.AddCommand(child)), and a main package using the standard
                     flag package (its flag.NewFlagSet names as sub-commands)
    routes           net/http ServeMux HandleFunc/Handle (Go 1.22 "GET /x/{id}" patterns, gorilla
                     .Methods), gin/echo/chi/fiber/httprouter calls with a literal path (a group or
                     mount prefix is marked prefix_unresolved), and a listener in a file that
                     registers no route (go-listener: never counted as reached)
    package_exports  every importable package with an exported identifier (not main, not internal/)

Only _test.go files are Go tests. A command is also reached when a _test.go file of its package
names the variable holding it, and a package by its own _test.go files.

Languages are counted by the shared lang_census.py. Its excluded trees (vendor/, third_party/, header
trees, bundles, submodules, embedded third-party packages) and bundled copies of other projects (a
directory with its own licence whose manifest names another repository than the root's) are skipped
and named in notes. `language_coverage` is the census's coverage block: when the dominant language is
not read (status unsupported_dominant), a WARNING goes to stderr and notes and the exit status is 3;
a mostly-Rust repository (read in part) is partial_dominant, with a warning and exit 0.

A CLI command or tool is reached when a test string literal names it (the command word, `prog cmd`,
or the tool name); a package export when a test imports it by package name, or imports the source
file its built target comes from (dist/x.js -> src/x.ts). That is weaker than a URL match: it says a
test names the command, not that it ran it. Every interface carries its `kind`, the value for
interface_coverage[].kind.

When nothing is found the output says so in `notes` and on stderr ("no supported interface found"),
never an empty success: map the interfaces by hand then.

A directory named tests, test, e2e, spec, __tests__ or testing is test code unless it is a product
package (a manifest that ships code: a src/ directory, main/exports/bin, a [lib]), so a product
named e2e is listed, not skipped as tests.

Read the output before using it: a computed path is shown as <dynamic> and an f-string prefix with
its expression in braces, and routers never included in an app are listed with included: false.
"""
from __future__ import annotations
import argparse, ast, json, os, re, sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lang_census, py_routes  # noqa: E402  (shared language census and route reader)
import py_extras  # noqa: E402  (static mounts, conditions, middleware, flags-only CLIs, PHP, build-output folders)

PRUNE = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', 'env', '__pycache__', '.mypy_cache', '.pytest_cache',
         '.tox', '.nox', 'dist', 'build', 'out', '.next', '.nuxt', 'coverage', 'site-packages', '.turbo', '.cache',
         'vendor', 'third_party', 'third-party', 'Pods', 'Carthage'}
HTTP = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts')
TEST_NAME = re.compile(r'(^test_.*\.py$|_test\.py$|^conftest\.py$|\.(test|spec)\.[cm]?[jt]sx?$|_test\.go$)')
TEST_DIRS = {'tests', 'test', '__tests__', 'spec', 'e2e', 'testing'}


def walk(root: Path, include: tuple = (), skipped: list | None = None, kept: list | None = None) -> list[str]:
    """Repo-relative files, pruning dependency, build and cache directories before descending. A build-output folder
    (dist/, build/, out/) is read when nothing in the repository produces it (py_extras.build_producer: hand-written and
    served as it is) or when named in include; otherwise it is pruned and recorded in skipped ({path, reason, files}).
    kept collects the build-output folders read as source, with the reason."""
    out = []
    for dp, dns, fns in os.walk(root):
        rel = Path(dp).relative_to(root).as_posix()
        keep = []
        for d in sorted(dns):
            sub = d if rel == '.' else f'{rel}/{d}'
            if d in py_extras.BUILD_DIRS and d in PRUNE:
                if sub in include or d in include:
                    keep.append(d)
                    if kept is not None:
                        kept.append({'path': sub, 'reason': 'read because --include names it'})
                    continue
                producer = py_extras.build_producer(root, sub)
                if producer is None:
                    keep.append(d)
                    if kept is not None:
                        kept.append({'path': sub, 'reason': 'no build step found (no package.json build script, bundler '
                                                            'config or .gitignore entry produces it): read as hand-written '
                                                            'source'})
                    continue
                if skipped is not None:
                    skipped.append({'path': sub, 'reason': f'build output: {producer}',
                                    'files': sum(len(f) for _, _, f in os.walk(Path(dp) / d))})
                continue
            if d not in PRUNE and not d.endswith('.egg-info'):
                keep.append(d)
        dns[:] = keep
        out += [f if rel == '.' else f'{rel}/{f}' for f in sorted(fns)]
    return out


MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle',
             'build.gradle.kts')
_PRODUCT: dict[tuple[str, str], bool] = {}


def product_package(root: Path | None, rel_dir: str) -> bool:
    """The directory holds a manifest that ships code: a src/ directory, a package.json with main/module/exports/bin, a
    Cargo.toml with [lib] or [[bin]], or the Python package a pyproject/setup names. The rule of the shared
    diff_scope.py (copied: this skill does not carry it). A workspace package of spec files stays a test directory."""
    if root is None:
        return False
    key = (str(root), rel_dir)
    if key not in _PRODUCT:
        d, why = root / rel_dir, False
        try:
            found = [m for m in MANIFESTS if (d / m).is_file()]
            if found:
                m = found[0]
                if (d / 'src').is_dir():
                    why = True
                elif m == 'package.json':
                    try:
                        pkg = json.loads((d / m).read_text(encoding='utf-8', errors='replace'))
                    except ValueError:
                        pkg = {}
                    why = isinstance(pkg, dict) and any(pkg.get(k) for k in ('main', 'module', 'exports', 'bin'))
                elif m == 'Cargo.toml':
                    why = bool(re.search(r'^\s*\[(lib|\[bin\])\]', (d / m).read_text(encoding='utf-8', errors='replace'), re.M))
                elif m in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                    text = (d / m).read_text(encoding='utf-8', errors='replace')
                    names = {re.sub(r'[-.]', '_', n).lower() for n in re.findall(r'(?m)^\s*name\s*[=:]\s*["\']?([\w.-]+)', text)}
                    why = any(x.is_dir() and x.name.lower() in names and (x / '__init__.py').is_file() for x in d.iterdir())
        except OSError:
            why = False
        _PRODUCT[key] = bool(why)
    return _PRODUCT[key]


def is_test(rel: str, root: Path | None = None) -> bool:
    p = PurePosixPath(rel)
    if TEST_NAME.search(p.name):
        return True
    dirs = p.parts[:-1]
    for i, part in enumerate(dirs):
        if part in TEST_DIRS and not product_package(root, '/'.join(dirs[:i + 1])):
            return True
    return False


def join(*parts: str) -> str:
    path = '/'.join(p.strip('/') for p in parts if p and p != '/')
    return '/' + path if path else '/'


# ---------------------------------------------------------------- Python (FastAPI, Flask, Azure Functions)
ROUTE_ATTRS = set(HTTP) | {'api_route', 'route', 'add_api_route', 'add_url_rule', 'websocket'}
BARE_HANDLER = re.compile(r'^def\s+handler\s*\(\s*request\s*,\s*response\b', re.M)
PY_HINT = re.compile(r'FastAPI|Router|router|Flask|Blueprint|register_blueprint|add_url_rule|\.route\(|FunctionApp')


def dotted(rel: str) -> str:
    parts = list(PurePosixPath(rel).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def python_interfaces(root: Path, files: list[str]) -> tuple[list[dict], list[str]]:
    """Python interfaces from the shared route reader (py_routes.py): the syntax tree, routers followed across modules."""
    texts, hosts, notes = {}, {}, []
    for rel in files:
        if rel.endswith('host.json'):
            hosts[rel] = (root / rel).read_text(encoding='utf-8', errors='replace')
        if not rel.endswith('.py') or is_test(rel, root):
            continue
        texts[rel] = (root / rel).read_text(encoding='utf-8', errors='replace')
    project = py_routes.Project(texts, py_routes.host_route_prefixes(hosts))
    for rel, err in sorted(project.parse_errors.items()):
        if PY_HINT.search(texts[rel]):
            notes.append(f'{rel}: not parsed ({err})')
    for d in project.dynamic_includes:
        notes.append(f"{d['file']}:{d['line']}: could not resolve the router included by {d['text']}")
    for rel, text in sorted(texts.items()):
        bare = BARE_HANDLER.search(text)
        if bare and not PY_HINT.search(text):
            notes.append(f'{rel}:{text.count(chr(10), 0, bare.start()) + 1}: defines handler(request, response) (a serverless '
                         f'function shape), which is not a supported framework: not read as an interface; check by hand '
                         f'whether it is deployed or retired')
    name = lambda key: f'{dotted(key[0])}:{key[1]}'  # noqa: E731
    conds = py_extras.Conditions(project)
    out = []
    for h in project.hits():
        m, fn, call, attr, key = h['module'], h['fn'], h['call'], h['attr'], h['key']
        line = fn.lineno if fn is not None else call.lineno
        func = {'function': fn.name} if fn is not None else {}
        router = project.router(key) if key else None
        if router and router['framework'] == 'azure-functions':
            if attr != 'route' or fn is None:
                continue
            rnode = py_routes._kw(call, 'route')
            route = py_routes._str(rnode, m.consts)[0] if rnode is not None else fn.name
            route = '<dynamic>' if route is None else route
            for meth in py_routes._methods(py_routes._kw(call, 'methods')) or ['ANY']:
                out.append({'kind': 'routes', 'framework': 'azure-functions', 'method': meth, 'path': join(project.host_prefix(m.rel), route),
                            'file': m.rel, 'line': line, **func, 'router': name(key), 'included': True})
            continue
        if attr not in ROUTE_ATTRS:
            continue
        pnode = call.args[0] if call.args else py_routes._kw(call, 'path', 'rule')
        if pnode is None:
            continue
        path = py_routes._str(pnode, m.consts)[0]
        path = '<dynamic>' if path is None else path
        if attr in HTTP:
            methods = [attr.upper()]
        else:
            methods = ['WEBSOCKET'] if attr == 'websocket' else (py_routes._methods(py_routes._kw(call, 'methods')) or ['GET'])
        if router is None:
            owner = call.func.value
            while isinstance(owner, ast.Attribute):
                owner = owner.value
            if not isinstance(owner, ast.Name):
                continue                          # super().add_api_route(...) inside a router class, a call result...
            for meth in methods:
                out.append({'kind': 'routes', 'framework': 'python', 'method': meth, 'path': join(path), 'file': m.rel, 'line': line, **func,
                            'router': f'{dotted(m.rel)}:{py_routes._unparse(call.func.value)}', 'included': False,
                            'note': 'the object the route is declared on is not a recognised app or router'})
            continue
        for c in project.mount_chains(key):
            cond = py_extras.join_conditions([conds.at(m.rel, call.lineno)] + conds.chain(c['chain']))
            for meth in methods:
                out.append({'kind': 'routes', 'framework': router['framework'], 'method': meth, 'path': join(c['prefix'], path), 'file': m.rel,
                            'line': line, **func, 'router': ' > '.join(name(k) for k in c['chain']), 'included': c['reached'],
                            **({'condition': cond} if cond else {}),
                            **({'note': 'a router prefix is computed, so the path is partial'} if c['status'] == 'dynamic' else {})})
    for st in py_extras.static_mounts(project, root):
        out.append({'kind': 'routes', **st})
    return out, notes, project


# ---------------------------------------------------------------- JavaScript / TypeScript
NEXT_METHOD = re.compile(r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|'
                         r'export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b')
NEXT_REEXPORT = re.compile(r'export\s*\{([^}]*)\}')
EXPRESS = re.compile(r'\b(\w+)\s*\.\s*(get|post|put|patch|delete|all)\s*\(\s*([\'"`])(/[^\'"`]*)\3')
COMMENT = re.compile(r'/\*.*?\*/|(?<![:\'"])//[^\n]*', re.S)


INTERCEPT = re.compile(r'^(\(\.\)|(?:\(\.\.\))+|\(\.\.\.\))(.+)$')


def next_url(parts: list[str]) -> tuple[str, dict]:
    """The URL of a Next.js route from its folder segments, and what shaped it: route groups `(x)` and parallel-route
    slots `@x` are not URL segments (a slot page renders inside its parent's layout at the parent's URL: `slot`);
    an intercepting segment `(.)x`, `(..)x`, `(..)(..)x` or `(...)x` replaces the URL with the route it intercepts
    (`intercepts`: shown on client-side navigation, the intercepted route itself on a full load); `[id]` is `{id}`,
    `[...x]` and `[[...x]]` are `{x*}` (`optional_catch_all` for the second: it also matches the bare parent)."""
    segs, info = [], {}
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
            continue                              # a route group
        m = re.fullmatch(r'\[(\[?)\.\.\.(\w+)\]?\]', p)
        if m:
            segs.append('{' + m.group(2) + '*}')
            if m.group(1):
                info['optional_catch_all'] = True
            continue
        m = re.fullmatch(r'\[(\w+)\]', p)
        segs.append('{' + m.group(1) + '}' if m else p)
    return join(*segs), info


def next_path(parts: list[str]) -> str:
    return next_url(parts)[0]


NEXT_CONFIG = re.compile(r'^next\.config\.(?:js|mjs|cjs|ts|mts)$')
NEXT_DEFAULT = re.compile(r'^[ \t]*export\s+default\b', re.M)
PAGE_EXT = ('.tsx', '.ts', '.jsx', '.js', '.mdx', '.md')
META_ANY = re.compile(r'^(?:(sitemap)\.(xml|js|jsx|ts|tsx)|(icon|apple-icon|opengraph-image|twitter-image)(\d*)\.'
                      r'(ico|jpg|jpeg|png|gif|svg|js|jsx|ts|tsx))$')
META_ROOT = re.compile(r'^(?:(robots)\.(txt|js|ts)|(manifest)\.(json|webmanifest|js|ts)|(favicon)\.(ico))$')
USE_SERVER = re.compile(r'''(?:^|[{;]\s*)['"]use server['"]''', re.M)


def mask_keep_lines(text: str) -> str:
    return COMMENT.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text)


def next_roots(root: Path, files: list[str]) -> list[str]:
    """Directories holding a Next.js project: a next.config.* or a package.json depending on next ('' for the root)."""
    out = set()
    for rel in files:
        p = PurePosixPath(rel)
        d = '' if p.parent.as_posix() == '.' else p.parent.as_posix()
        if NEXT_CONFIG.match(p.name):
            out.add(d)
        elif p.name == 'package.json':
            try:
                pkg = json.loads((root / rel).read_text(encoding='utf-8', errors='replace'))
            except ValueError:
                continue
            if isinstance(pkg, dict) and any(isinstance(pkg.get(k), dict) and 'next' in pkg[k]
                                             for k in ('dependencies', 'devDependencies')):
                out.add(d)
    return sorted(out)


def under(rel: str, d: str) -> str | None:
    """rel relative to directory d ('' is the root), or None when it is not inside it."""
    if not d:
        return rel
    return rel[len(d) + 1:] if rel.startswith(d + '/') else None


def next_route_handler(rel: str, text: str, path: str) -> list[dict]:
    """The HTTP methods an App Router route.ts exports, each with the line of its export."""
    masked = mask_keep_lines(text)
    found = {}
    for m in NEXT_METHOD.finditer(masked):
        found.setdefault(m.group(1) or m.group(2), line_at(masked, m.start()))
    for m in NEXT_REEXPORT.finditer(masked):
        for x in m.group(1).split(','):
            name = x.split(' as ')[-1].strip()
            if name in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
                found.setdefault(name, line_at(masked, m.start()))
    return [{'kind': 'routes', 'framework': 'nextjs', 'method': meth, 'path': path, 'file': rel, 'line': found[meth],
             'included': True} for meth in sorted(found)]


def default_line(text: str) -> int:
    m = NEXT_DEFAULT.search(mask_keep_lines(text))
    return line_at(text, m.start()) if m else 1


def next_interfaces(root: Path, files: list[str]) -> tuple[list[dict], list[dict], set[str]]:
    """Next.js projects: App Router pages, route handlers and metadata routes, and pages/ router pages. Returns the
    interfaces, what the project has that is not read (not_read: server actions, middleware, public/ files, next.config
    rewrites and redirects), and the files handled here."""
    out, not_read, handled = [], [], set()
    for nroot in next_roots(root, files):
        apps = [d for d in (f'{nroot}/app'.lstrip('/'), f'{nroot}/src/app'.lstrip('/'))]
        pages = [d for d in (f'{nroot}/pages'.lstrip('/'), f'{nroot}/src/pages'.lstrip('/'))]
        actions, mids, public, config = [], [], [], []
        for rel in files:
            inner = under(rel, nroot)
            if inner is None or is_test(rel, root):
                continue
            p = PurePosixPath(rel)
            if inner.startswith('public/'):
                public.append(rel)
                continue
            if NEXT_CONFIG.match(inner):
                if re.search(r'\b(?:rewrites|redirects|headers)\b', (root / rel).read_text(encoding='utf-8', errors='replace')):
                    config.append(rel)
                continue
            if re.fullmatch(r'(?:src/)?(?:middleware|proxy)\.(?:ts|js|mjs|mts)', inner):
                mids.append(rel)
                handled.add(rel)
                continue
            if p.suffix in JS_EXT and USE_SERVER.search(mask_keep_lines((root / rel).read_text(encoding='utf-8', errors='replace'))):
                actions.append(rel)
            app = next((a for a in apps if under(rel, a) is not None), None)
            if app is not None:
                segs = list(PurePosixPath(under(rel, app)).parts)
                name, dirs = segs[-1], segs[:-1]
                path, info = next_url(dirs)
                if p.stem == 'page' and p.suffix in PAGE_EXT:
                    text = (root / rel).read_text(encoding='utf-8', errors='replace')
                    itf = {'kind': 'pages', 'framework': 'nextjs', 'method': 'GET', 'path': path, 'file': rel,
                           'line': default_line(text), 'included': True, **info}
                    masked = mask_keep_lines(text)
                    if re.search(r'export\s+(?:async\s+)?function\s+generateStaticParams\b|export\s+const\s+generateStaticParams\b',
                                 masked):
                        itf['generate_static_params'] = True
                    dp = re.search(r'export\s+const\s+dynamicParams\s*=\s*(true|false)\b', masked)
                    if dp:
                        itf['dynamic_params'] = dp.group(1) == 'true'
                    notes = []
                    if itf.get('dynamic_params') is False:
                        notes.append('dynamicParams = false: only the params generateStaticParams returns are served, '
                                     'any other value is a 404')
                    if 'slot' in info:
                        notes.append(f'a parallel-route slot ({info["slot"]}): renders inside the layout at this URL, '
                                     f'not a URL of its own')
                    if info.get('intercepts'):
                        notes.append('an intercepting route: shown on client-side navigation to this URL; a full load '
                                     'shows the intercepted page')
                    if notes:
                        itf['note'] = '; '.join(notes)
                    out.append(itf)
                    handled.add(rel)
                    continue
                if p.stem == 'route' and p.suffix in JS_EXT:
                    out += next_route_handler(rel, (root / rel).read_text(encoding='utf-8', errors='replace'), path)
                    handled.add(rel)
                    continue
                mr = META_ROOT.match(name) if not dirs else None
                ma = META_ANY.match(name)
                if mr or ma:
                    gen = p.suffix in JS_EXT
                    text = (root / rel).read_text(encoding='utf-8', errors='replace') if gen else ''
                    note = None
                    if mr and mr.group(1):
                        url = '/robots.txt'
                    elif mr and mr.group(3):
                        url = '/manifest.webmanifest' if gen else f'/{name}'
                    elif mr:
                        url = '/favicon.ico'
                    elif ma.group(1):
                        url = join(path, 'sitemap/{id}.xml') if 'generateSitemaps' in text else join(path, 'sitemap.xml')
                    elif gen:
                        stem = ma.group(3) + ma.group(4)
                        url = join(path, stem + '/{id}') if 'generateImageMetadata' in text else join(path, stem)
                        note = ('a generated image: Next.js may add a build hash to its URL under a dynamic segment; '
                                'match tests by prefix')
                    else:
                        url = join(path, name)
                    out.append({'kind': 'routes', 'framework': 'nextjs-metadata', 'method': 'GET', 'path': url, 'file': rel,
                                'line': default_line(text) if gen else 1, 'included': True,
                                'metadata': 'generated' if gen else 'file', **({'note': note} if note else {})})
                    handled.add(rel)
                    continue
            pg = next((d for d in pages if under(rel, d) is not None), None)
            if pg is not None and p.suffix in PAGE_EXT:
                segs = list(PurePosixPath(under(rel, pg)).parts)
                if segs[0] == 'api' or any(s.startswith('_') for s in segs) or p.stem in ('404', '500'):
                    continue                  # API routes are read below; _app, _document, error pages are not URLs
                path, info = next_url(segs[:-1] + ([] if p.stem == 'index' else [p.stem]))
                text = (root / rel).read_text(encoding='utf-8', errors='replace')
                itf = {'kind': 'pages', 'framework': 'nextjs-pages', 'method': 'GET', 'path': path, 'file': rel,
                       'line': default_line(text), 'included': True, **info}
                if re.search(r'\bgetStaticPaths\b', text):
                    itf['note'] = 'getStaticPaths: check its fallback for which params are served'
                out.append(itf)
                handled.add(rel)
        label = nroot or 'the repository root'
        for kind, fs, why in (
                ('server_actions', actions, "server actions ('use server' functions): called by POST from forms and "
                                            "client components, not listed; map them by hand (kind server_actions)"),
                ('middleware', mids, 'middleware runs before every matched request (often authentication or '
                                     'redirects); its matcher and logic are not read'),
                ('static_files', public, 'files in public/ are served at their own path (a service worker, robots.txt, '
                                         'images, downloads) and are not listed'),
                ('config_routes', config, 'rewrites, redirects and headers in next.config are not read')):
            if fs:
                not_read.append({'framework': 'nextjs', 'project': label, 'kind': kind, 'count': len(fs),
                                 'files': fs[:20], 'why': why})
    return out, not_read, handled


def js_interfaces(root: Path, files: list[str], handled: frozenset | set = frozenset()) -> list[dict]:
    """Route handlers in an app/ folder (also outside a detected Next.js project), pages/api routes, Express routes and
    plain Node servers; files next_interfaces already read (handled) are skipped."""
    out = []
    for rel in files:
        if not rel.endswith(JS_EXT) or is_test(rel, root) or rel in handled:
            continue
        p = PurePosixPath(rel)
        parts = list(p.parts)
        if p.stem == 'route' and 'app' in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index('app')
            out += next_route_handler(rel, (root / rel).read_text(encoding='utf-8', errors='replace'), next_path(parts[i + 1:-1]))
            continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        if 'pages' in parts[:-1] and 'api' in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index('pages')
            if parts[i + 1:i + 2] == ['api']:
                segs = parts[i + 1:-1] + ([] if p.stem == 'index' else [p.stem])
                out.append({'kind': 'routes', 'framework': 'nextjs-pages', 'method': 'ANY', 'path': next_path(segs), 'file': rel,
                            'line': default_line(text), 'included': True})
                continue
        masked = COMMENT.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text)
        out += node_server(rel, text, masked)
        if not re.search(r'\bexpress\b|\bRouter\s*\(|\bfastify\b', text):
            continue
        for m in EXPRESS.finditer(masked):
            var, meth, _, path = m.groups()
            if var in ('axios', 'request', 'supertest', 'http', 'https', 'fetch', 'client', 'api'):
                continue
            line = masked.count('\n', 0, m.start()) + 1
            out.append({'kind': 'routes', 'framework': 'express', 'method': meth.upper() if meth != 'all' else 'ANY', 'path': path, 'file': rel,
                        'line': line, 'router': var, 'included': var == 'app',
                        **({} if var == 'app' else {'prefix_unresolved': True})})
    return out


# ---------------------------------------------------------------- CLIs, package entry points, tool servers
CLI_LIBS = re.compile(r'''(?:from\s*|require\s*\(\s*)['"](?:commander|@commander-js/extra-typings|yargs(?:/[\w/]+)?|cac|'''
                      r'''sade|clipanion|meow)['"]''')
MCP_LIB = re.compile(r'''['"](?:@modelcontextprotocol/[\w/.-]+|fastmcp)['"]''')
HTTP_LIB = re.compile(r'''['"](?:node:)?(?:http|https|http2)['"]''')


def line_at(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def node_server(rel: str, text: str, masked: str) -> list[dict]:
    """A plain Node HTTP server (http.createServer) without a routing framework: one ANY / entry."""
    m = re.search(r'\b(?:https?|http2)?\s*\.?\s*create(?:Secure)?Server\s*\(', masked)
    if not m or not HTTP_LIB.search(text) or re.search(r'\bexpress\b|\bfastify\b|\bkoa\b|\bhono\b', text):
        return []
    return [{'kind': 'routes', 'framework': 'node-http', 'method': 'ANY', 'path': '/', 'file': rel, 'line': line_at(masked, m.start()),
             'included': True, 'note': 'a plain Node HTTP server: its routes are decided inside the handler; read it to list them'}]


def bin_names(pkg: dict) -> dict[str, str]:
    b = pkg.get('bin')
    if isinstance(b, str):
        return {str(pkg.get('name', 'cli')).split('/')[-1]: b}
    return {k: v for k, v in b.items() if isinstance(v, str)} if isinstance(b, dict) else {}


def export_target(v) -> str | None:
    """The file an exports entry points at: the import/default/require condition, depth first."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ('import', 'default', 'require', 'node'):
            if k in v:
                t = export_target(v[k])
                if t:
                    return t
        for x in v.values():
            t = export_target(x)
            if t:
                return t
    return None


def manifest_interfaces(root: Path, files: list[str]) -> tuple[list[dict], dict[str, str]]:
    """CLI bins and exports of every package.json, scripts of pyproject.toml, binaries of Cargo.toml. Also returns
    {package dir: program name} for naming sub-commands."""
    out, progs = [], {}
    for rel in files:
        name = PurePosixPath(rel).name
        pdir = PurePosixPath(rel).parent.as_posix()
        if name == 'package.json':
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
            try:
                pkg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(pkg, dict):
                continue
            bline = line_at(text, max(text.find('"bin"'), 0))
            for b, target in sorted(bin_names(pkg).items()):
                progs.setdefault(pdir, b)
                out.append({'kind': 'cli', 'framework': 'node-bin', 'method': 'CLI', 'path': b, 'file': rel, 'line': bline,
                            'target': (PurePosixPath(pdir) / target).as_posix() if pdir != '.' else target, 'included': True})
            if pkg.get('private') or not pkg.get('name'):
                continue
            exp = pkg.get('exports')
            if exp is None and pkg.get('main'):
                exp = {'.': pkg['main']}
            elif isinstance(exp, (str, list)) or (isinstance(exp, dict) and exp and not any(k.startswith('.') for k in exp)):
                exp = {'.': exp}
            if not isinstance(exp, dict):
                continue
            eline = line_at(text, max(text.find('"exports"'), text.find('"main"'), 0))
            for sub, v in sorted(exp.items()):
                if not sub.startswith('.') or sub.endswith('package.json'):
                    continue
                target = export_target(v)
                out.append({'kind': 'package_exports', 'framework': 'package-exports', 'method': 'IMPORT',
                            'path': pkg['name'] + sub[1:], 'file': rel, 'line': eline, 'included': True,
                            **({'target': (PurePosixPath(pdir) / target).as_posix() if pdir != '.' else target.lstrip('./')}
                               if target else {})})
        elif name == 'pyproject.toml':
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
            sec = re.search(r'^\[(?:project\.scripts|tool\.poetry\.scripts)\]\s*$(.*?)(?=^\[|\Z)', text, re.M | re.S)
            if sec:
                for m in re.finditer(r'^\s*["\']?([\w.-]+)["\']?\s*=\s*["\']([^"\']+)["\']', sec.group(1), re.M):
                    progs.setdefault(pdir, m.group(1))
                    out.append({'kind': 'cli', 'framework': 'python-script', 'method': 'CLI', 'path': m.group(1), 'file': rel,
                                'line': line_at(text, sec.start(1) + m.start()), 'target': m.group(2), 'included': True})
        elif name == 'Cargo.toml':
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
            bins = [m.group(1) for m in re.finditer(r'^\[\[bin\]\]\s*\n(?:[^\[]*?\n)?\s*name\s*=\s*"([^"]+)"', text, re.M)]
            if not bins and (root / pdir / 'src' / 'main.rs').is_file():
                pm = re.search(r'^\[package\][^\[]*?^\s*name\s*=\s*"([^"]+)"', text, re.M | re.S)
                bins = [pm.group(1)] if pm else []
            for b in bins:
                progs.setdefault(pdir, b)
                out.append({'kind': 'cli', 'framework': 'cargo-bin', 'method': 'CLI', 'path': b, 'file': rel, 'line': 1,
                            'included': True, 'note': 'sub-commands (clap derive or a dispatch table) are not listed: map them by hand'})
    return out, progs


def prog_for(rel: str, progs: dict[str, str]) -> str:
    """The program name of the nearest package holding rel (its bin), or the package directory's name."""
    d = PurePosixPath(rel).parent
    while True:
        if d.as_posix() in progs:
            return progs[d.as_posix()]
        if d.as_posix() in ('.', '') or d == d.parent:
            return PurePosixPath(rel).parent.name or 'cli'
        d = d.parent


def js_commands_and_tools(root: Path, files: list[str], progs: dict[str, str]) -> list[dict]:
    """commander / yargs / cac sub-commands and MCP tools, from JavaScript and TypeScript source."""
    out = []
    for rel in files:
        if not rel.endswith(JS_EXT) or is_test(rel, root):
            continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        cli, mcp = CLI_LIBS.search(text), MCP_LIB.search(text)
        if not cli and not mcp:
            continue
        masked = COMMENT.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text)
        if cli:
            prog = prog_for(rel, progs)
            nm = re.search(r'''\.name\(\s*['"]([\w.-]+)['"]\s*\)|\bcac\(\s*['"]([\w.-]+)['"]|\.scriptName\(\s*['"]([\w.-]+)['"]''', masked)
            if nm:
                prog = next(g for g in nm.groups() if g)
            for m in re.finditer(r'''\.command\(\s*(?:\{\s*command\s*:\s*)?(['"`])([^'"`]+)\1''', masked):
                word = m.group(2).split()[0] if m.group(2).split() else ''
                if not word or word in ('*', '$0') or word.startswith(('<', '[')):
                    continue
                out.append({'kind': 'cli', 'framework': 'node-cli', 'method': 'CLI', 'path': f'{prog} {word}', 'file': rel,
                            'line': line_at(masked, m.start()), 'included': True})
        if mcp:
            transport = 'stdio' if 'StdioServerTransport' in text else \
                'http' if re.search(r'StreamableHTTPServerTransport|SSEServerTransport', text) else 'unknown'
            for m in re.finditer(r'''\.(?:registerTool|tool|addTool)\(\s*(?:\{\s*name\s*:\s*)?(['"`])([\w.:-]+)\1''', masked):
                out.append({'kind': 'tools', 'framework': 'mcp', 'method': 'TOOL', 'path': m.group(2), 'file': rel,
                            'line': line_at(masked, m.start()), 'included': True, 'transport': transport})
            for m in re.finditer(r'''\.(?:registerTool|addTool)\(\s*(?![\s'"`{])''', masked):
                out.append({'kind': 'tools', 'framework': 'mcp', 'method': 'TOOL', 'path': '<dynamic>', 'file': rel,
                            'line': line_at(masked, m.start()), 'included': True, 'transport': transport,
                            'note': 'the tool names are computed (a loop over tool specs): read them from the specs and '
                                    'map them by hand'})
    return out


def py_commands_and_tools(root: Path, files: list[str], project) -> list[dict]:
    """argparse / click / typer commands (py_routes.py) and FastMCP @mcp.tool() functions."""
    out = []
    try:
        clis = project.scan().get('clis', [])
    except Exception:                                   # a reader failure leaves the routes; say nothing more here
        clis = []
    for c in clis:
        out.append({'kind': 'cli', 'framework': c['framework'], 'method': 'CLI', 'path': c['command'], 'file': c['file'],
                    'line': c['line'], 'included': True})
    have = {c['command'] for c in clis}
    for c in py_extras.command_lines(project):          # flags-only argparse programs, sys.argv[1] dispatch
        if c['command'] not in have:
            out.append({'kind': 'cli', 'framework': c['framework'], 'method': 'CLI', 'path': c['command'], 'file': c['file'],
                        'line': c['line'], 'included': True, **({'options': c['options']} if c.get('options') else {})})
    for rel in files:
        if not rel.endswith('.py') or is_test(rel, root):
            continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        if not re.search(r'^\s*(?:from|import)\s+(?:mcp|fastmcp)\b', text, re.M):
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                func = call.func if call else dec
                if isinstance(func, ast.Attribute) and func.attr == 'tool':
                    name = next((k.value.value for k in (call.keywords if call else []) if k.arg == 'name'
                                 and isinstance(k.value, ast.Constant)), None) or node.name
                    out.append({'kind': 'tools', 'framework': 'mcp', 'method': 'TOOL', 'path': name, 'file': rel,
                                'line': dec.lineno, 'included': True})
    return out


# ---------------------------------------------------------------- Go
def go_mask(text: str) -> str:
    """Comments and the insides of string and rune literals blanked (same length, newlines kept), so braces and
    words inside them are never read as code. Quotes stay, so a literal's position still finds its value."""
    out, i, n = list(text), 0, len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        c = text[i]
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            blank(i + 1, min(j, n))
            i = j + 1
        elif c == '`':
            j = text.find('`', i + 1)
            j = n if j < 0 else j
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def go_close(masked: str, open_pos: int) -> int:
    """The index of the brace closing the one at open_pos (in masked text)."""
    depth = 0
    for k in range(open_pos, len(masked)):
        if masked[k] in '{([':
            depth += 1
        elif masked[k] in '})]':
            depth -= 1
            if depth == 0:
                return k
    return len(masked) - 1


def go_top_level(masked: str, a: int, b: int) -> list[int]:
    """Positions between a and b at nesting depth 0 (inside a composite literal's own braces)."""
    out, depth = [], 0
    for k in range(a, b):
        ch = masked[k]
        if ch in '{([':
            if depth == 0:
                out.append(k)
            depth += 1
        elif ch in '})]':
            depth -= 1
        elif depth == 0:
            out.append(k)
    return out


GO_STR = re.compile(r'"((?:\\.|[^"\\\n])*)"|`([^`]*)`')
GO_FIELD = r'(?<![\w.])({field})\s*:\s*'
GO_TEST = re.compile(r'_test\.go$')


def go_files(root: Path, files: list[str], tests: bool = False) -> dict[str, tuple[str, str]]:
    """{rel: (text, masked)} for the repo's Go files: product files, or with tests=True the _test.go files."""
    out = {}
    for rel in files:
        if not rel.endswith('.go') or bool(GO_TEST.search(rel)) != tests or '/testdata/' in f'/{rel}':
            continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        out[rel] = (text, go_mask(text))
    return out


def go_modules(root: Path, files: list[str]) -> list[tuple[str, str]]:
    """(module path, module directory) for every go.mod, the deepest directory first."""
    mods = []
    for rel in files:
        if PurePosixPath(rel).name == 'go.mod':
            m = re.search(r'(?m)^\s*module\s+"?([^\s"]+)', (root / rel).read_text(encoding='utf-8', errors='replace'))
            if m:
                d = PurePosixPath(rel).parent.as_posix()
                mods.append((m.group(1), '' if d == '.' else d))
    return sorted(mods, key=lambda x: -len(x[1]))


def go_import_path(mods: list[tuple[str, str]], rel_dir: str) -> str | None:
    for mod, d in mods:
        if not d or rel_dir == d or rel_dir.startswith(d + '/'):
            rest = rel_dir[len(d):].strip('/') if d else ('' if rel_dir == '.' else rel_dir)
            return mod + ('/' + rest if rest else '')
    return None


def go_prog(rel: str, mods: list[tuple[str, str]]) -> str:
    """A Go program's name: its main package's directory (cmd/harbour -> harbour), or the module's last segment."""
    d = PurePosixPath(rel).parent
    if d.as_posix() not in ('.', ''):
        return d.name
    return (mods[0][0].rsplit('/', 1)[-1] if mods else 'cli')


def go_command_tables(root: Path, gofiles: dict, mods: list) -> list[dict]:
    """urfave/cli (cli.App / cli.Command literals, Commands/Subcommands lists, elided {Name: ...} elements, commands
    held in variables or returned by functions) and cobra (cobra.Command{Use: ...} with parent.AddCommand(child)),
    read across every product file at once. Each command's path is its program and the chain of names."""
    nodes = []          # {fw, name, file, line, ident, children: [node idx or ident], app}
    by_ident: dict[str, list[int]] = {}
    for rel, (text, masked) in gofiles.items():
        has_urfave = 'github.com/urfave/cli' in text
        has_cobra = 'github.com/spf13/cobra' in text
        if not has_urfave and not has_cobra:
            continue
        lits = []       # (open, close, node index)
        rx = re.compile(r'&?\b(cli)\.(Command|App)\s*\{' if has_urfave else r'(?!)')
        found = [(m, 'urfave-cli') for m in rx.finditer(masked)]
        if has_cobra:
            found += [(m, 'cobra') for m in re.finditer(r'&?\bcobra\.(Command)\s*\{', masked)]
        for m, fw in sorted(found, key=lambda x: x[0].start()):
            if re.search(r'\]\s*\*?\s*$', masked[max(0, m.start() - 4):m.start()]):
                continue                                  # []*cli.Command{...} is a list, not a command
            op = masked.index('{', m.start())
            lits.append((op, go_close(masked, op), fw, m.start(), m.group(0).endswith('App{') or 'App' in m.group(0)))
        # elided elements of a Commands/Subcommands list: []*cli.Command{ {Name: ...}, ... }
        for m in re.finditer(r'\b(?:Subcommands|Commands)\s*:\s*\[\]\*?cli\.Command\s*\{', masked):
            op = m.end() - 1
            cl = go_close(masked, op)
            for k in go_top_level(masked, op + 1, cl):
                if masked[k] == '{':
                    lits.append((k, go_close(masked, k), 'urfave-cli', k, False))
        lits.sort()
        idx_of = {}
        for op, cl, fw, start, is_app in lits:
            if op in idx_of:
                continue
            tops = go_top_level(masked, op + 1, cl)
            field = 'Use' if fw == 'cobra' else 'Name'
            name = None
            for k in tops:
                mm = re.compile(GO_FIELD.format(field=field)).match(masked, k)
                if mm and (k == 0 or not (masked[k - 1].isalnum() or masked[k - 1] == '_')):
                    sm = GO_STR.match(text, mm.end())
                    if sm:
                        name = (sm.group(1) if sm.group(1) is not None else sm.group(2)).split()
                        name = name[0] if name else None
                    break
            before = masked[max(0, start - 200):start]
            ident = None
            bm = re.search(r'(?:\bvar\s+)?\b(\w+)\s*(?::=|=)\s*$', before)
            fm = re.search(r'\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\([^)]*\)\s*\*?(?:cli|cobra)\.Command\s*\{\s*return\s*$', before)
            if fm:
                ident = fm.group(1)
            elif bm:
                ident = bm.group(1)
            node = {'fw': fw, 'name': name, 'file': rel, 'line': line_at(text, start), 'ident': ident,
                    'children': [], 'app': is_app, 'span': (op, cl), 'parent': None}
            idx_of[op] = len(nodes)
            if ident:
                by_ident.setdefault(ident, []).append(len(nodes))
            nodes.append(node)
        # nesting and list references, within this file
        for op, cl, fw, start, is_app in lits:
            me = idx_of[op]
            for m in re.finditer(r'\b(?:Subcommands|Commands)\s*:\s*\[\]\*?cli\.Command\s*\{', masked[op + 1:cl]):
                lop = op + 1 + m.end() - 1
                lcl = go_close(masked, lop)
                if go_top_level(masked, op + 1, cl).count(op + 1 + m.start()) == 0:
                    continue                            # a list inside a nested literal belongs to that literal
                depth, k, elem_start = 0, lop + 1, lop + 1
                elems = []
                while k <= lcl:
                    ch = masked[k] if k < lcl else ','
                    if ch in '{([':
                        depth += 1
                    elif ch in '})]':
                        depth -= 1
                    if (ch == ',' and depth == 0) or k == lcl:
                        elems.append((elem_start, k))
                        elem_start = k + 1
                    k += 1
                for a, b in elems:
                    seg = masked[a:b]
                    if not seg.strip():
                        continue
                    child = next((idx_of[o] for o in idx_of if a <= o < b), None)
                    if child is not None:
                        nodes[me]['children'].append(child)
                        nodes[child]['parent'] = me
                        continue
                    im = re.match(r'\s*&?\s*(?:\w+\.)?(\w+)\s*(?:\(\s*\))?\s*$', seg)
                    if im:
                        nodes[me]['children'].append(im.group(1))
        for m in re.finditer(r'\b(\w+)\.AddCommand\(([^)]*(?:\([^)]*\)[^)]*)*)\)', masked):
            parents = by_ident.get(m.group(1)) or []
            for p in parents[:1]:
                for arg in m.group(2).split(','):
                    am = re.match(r'\s*(?:\w+\.)?(\w+)\s*(?:\(\s*\))?\s*$', arg)
                    if am:
                        nodes[p]['children'].append(am.group(1))
    # resolve identifier children
    for i, nd in enumerate(nodes):
        kids = []
        for c in nd['children']:
            if isinstance(c, str):
                for j in by_ident.get(c, [])[:1]:
                    kids.append(j)
                    nodes[j]['parent'] = i if nodes[j]['parent'] is None else nodes[j]['parent']
            else:
                kids.append(c)
        nd['children'] = kids
    out = []

    def emit(i: int, prefix: list[str], seen: set) -> None:
        if i in seen:
            return
        nd = nodes[i]
        path = prefix + ([nd['name']] if nd['name'] else [])
        if path:
            out.append({'kind': 'cli', 'framework': nd['fw'], 'method': 'CLI', 'path': ' '.join(path), 'file': nd['file'],
                        'line': nd['line'], 'included': True,
                        **({'go_ident': nd['ident'], 'go_dir': PurePosixPath(nd['file']).parent.as_posix()} if nd['ident'] else {})})
        for c in nd['children']:
            emit(c, path, seen | {i})
    roots = [i for i, nd in enumerate(nodes) if nd['parent'] is None]
    for i in roots:
        nd = nodes[i]
        attached = nd['app'] or nd['children']
        if attached:
            prefix = [] if nd['name'] else [go_prog(nd['file'], mods)]
            emit(i, prefix, set())
        elif nd['name']:
            out.append({'kind': 'cli', 'framework': nd['fw'], 'method': 'CLI', 'path': nd['name'], 'file': nd['file'],
                        'line': nd['line'], 'included': False,
                        'note': 'a command not found in any command table: check whether it is wired up',
                        **({'go_ident': nd['ident'], 'go_dir': PurePosixPath(nd['file']).parent.as_posix()} if nd['ident'] else {})})
    return out


def go_flag_programs(gofiles: dict, mods: list) -> list[dict]:
    """A main package parsing the standard flag package: the program, and each flag.NewFlagSet("name") as a
    sub-command. Flags are listed in the note."""
    out = []
    for rel, (text, masked) in gofiles.items():
        if not re.search(r'(?m)^package\s+main\b', text) or not re.search(r'"flag"', text) \
                or 'github.com/urfave/cli' in text or 'github.com/spf13/cobra' in text:
            continue
        flags = []
        for m in re.finditer(r'\bflag\.(?:String|Bool|Int|Int64|Uint|Uint64|Float64|Duration|Func|Var)(?:Var)?\(', masked):
            sm = GO_STR.search(text, m.end(), m.end() + 80)
            if sm and sm.group(1):
                flags.append('-' + sm.group(1))
        sets = [(GO_STR.match(text, m.end()), m) for m in re.finditer(r'\bflag\.NewFlagSet\(\s*', masked)]
        if not flags and not sets and 'flag.Parse()' not in masked:
            continue
        prog = go_prog(rel, mods)
        out.append({'kind': 'cli', 'framework': 'go-flag', 'method': 'CLI', 'path': prog, 'file': rel, 'line': 1,
                    'included': True, **({'note': 'flags: ' + ', '.join(sorted(set(flags)))} if flags else {})})
        for sm, m in sets:
            if sm and sm.group(1) and sm.group(1) != prog:      # NewFlagSet(os.Args[0]-style name) is the program itself
                out.append({'kind': 'cli', 'framework': 'go-flag', 'method': 'CLI', 'path': f'{prog} {sm.group(1)}',
                            'file': rel, 'line': line_at(text, m.start()), 'included': True})
    return out


GO_ROUTERS = re.compile(r'"github\.com/(?:gin-gonic/gin|labstack/echo[^"]*|go-chi/chi[^"]*|gofiber/fiber[^"]*|'
                        r'julienschmidt/httprouter|gorilla/mux)"')


def go_routes(gofiles: dict) -> list[dict]:
    """net/http ServeMux registrations (Go 1.22 "METHOD /path" patterns, gorilla .Methods(...)), gin / echo / chi /
    fiber / httprouter method calls with a literal path, and a listener in a file that registers no route."""
    out = []
    for rel, (text, masked) in gofiles.items():
        found = []
        for m in re.finditer(r'\.(HandleFunc|Handle)\(\s*', masked):
            sm = GO_STR.match(text, m.end())
            if not sm or sm.group(1) is None:
                continue
            pat = sm.group(1).strip()
            meth = 'ANY'
            pm = re.match(r'([A-Z]+)\s+(.*)$', pat)
            if pm:
                meth, pat = pm.group(1), pm.group(2)
            else:
                eol = masked.find('\n', m.end())
                mm = re.search(r'\.Methods\(\s*"([A-Z]+)"', text[m.end():eol if eol > 0 else None])
                if mm:
                    meth = mm.group(1)
            if not pat.startswith('/'):
                pat = pat[pat.find('/'):] if '/' in pat else '/' + pat    # a host-qualified pattern
            pat = re.sub(r'\{(\w+)\.\.\.\}', r'{\1*}', pat)
            found.append({'kind': 'routes', 'framework': 'go-net-http', 'method': meth, 'path': pat, 'file': rel,
                          'line': line_at(text, m.start()), 'included': True})
        if GO_ROUTERS.search(text):
            fw = GO_ROUTERS.search(text).group(0).strip('"').split('/')[2]
            for m in re.finditer(r'\b\w+\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Get|Post|Put|Patch|Delete|Head|Options|Any|'
                                 r'All)\(\s*', masked):
                sm = GO_STR.match(text, m.end())
                if not sm or not (sm.group(1) or '').startswith('/'):
                    continue
                meth = m.group(1).upper()
                found.append({'kind': 'routes', 'framework': f'go-{fw}', 'method': 'ANY' if meth in ('ANY', 'ALL') else meth,
                              'path': re.sub(r':(\w+)', r'{\1}', sm.group(1)), 'file': rel, 'line': line_at(text, m.start()),
                              'included': True,
                              **({'prefix_unresolved': True} if re.search(r'\.(?:Group|Route|Mount)\(', masked) else {})})
        out += found
        if not found:
            lm = re.search(r'\bhttp\.ListenAndServe(?:TLS)?\(|\bnet\.Listen(?:TCP|Unix)?\(|\bhttp\.Server\s*\{', masked)
            if lm:
                out.append({'kind': 'routes', 'framework': 'go-listener', 'method': 'ANY', 'path': '/', 'file': rel,
                            'line': line_at(text, lm.start()), 'included': True,
                            'note': 'a listener with no route registered in this file: what it serves is decided by its '
                                    'handler or protocol; read it to list the interface'})
    return out


def go_packages(gofiles: dict, mods: list) -> list[dict]:
    """Every importable Go package with an exported identifier (not main, not under internal/): kind package_exports,
    path its import path."""
    dirs: dict[str, list[str]] = {}
    for rel in gofiles:
        dirs.setdefault(PurePosixPath(rel).parent.as_posix(), []).append(rel)
    out = []
    for d, rels in sorted(dirs.items()):
        if 'internal' in PurePosixPath(d).parts:
            continue
        texts = [gofiles[r][1] for r in rels]
        if any(re.search(r'(?m)^package\s+main\b', t) for t in texts):
            continue
        exported = [(r, m) for r in rels for m in [re.search(r'(?m)^(?:func(?:\s*\([^)]*\))?|type|var|const)\s+[A-Z]\w*',
                                                               gofiles[r][1])] if m]
        path = go_import_path(mods, d)
        if not exported or not path:
            continue
        r, m = exported[0]
        out.append({'kind': 'package_exports', 'framework': 'go-package', 'method': 'IMPORT', 'path': path, 'file': r,
                    'line': line_at(gofiles[r][0], m.start()), 'included': True, 'go_dir': d})
    return out


def go_interfaces(root: Path, files: list[str]) -> list[dict]:
    gofiles = go_files(root, files)
    if not gofiles:
        return []
    mods = go_modules(root, files)
    return go_command_tables(root, gofiles, mods) + go_flag_programs(gofiles, mods) + go_routes(gofiles) + \
        go_packages(gofiles, mods)


def go_test_literals(base: Path, names: list[str], rel_base: str) -> tuple[list[dict], list[dict], dict[str, str]]:
    """From _test.go files: URL literals (with the method from http.MethodX or "X" just before), every short string,
    and {file: masked text} for identifier matching."""
    lits, strings, masked_by = [], [], {}
    for rel in names:
        if not GO_TEST.search(rel):
            continue
        text = (base / rel).read_text(encoding='utf-8', errors='replace')
        frel = f'{rel_base}/{rel}' if rel_base not in ('.', '') else rel
        masked_by[frel] = go_mask(text)
        for m in GO_STR.finditer(text):
            if masked_by[frel][m.start()] not in '"`':
                continue                                  # inside a comment
            v = m.group(1) if m.group(1) is not None else m.group(2)
            if not v or len(v) >= 300 or '\n' in v:
                continue
            line = text.count('\n', 0, m.start()) + 1
            strings.append({'file': frel, 'line': line, 'value': v})
            if '/' in v:
                before = text[max(0, m.start() - 60):m.start()]
                mm = re.search(r'(?:http\.Method(Get|Post|Put|Patch|Delete|Head|Options)|"(GET|POST|PUT|PATCH|DELETE|HEAD|'
                               r'OPTIONS)")\s*,\s*$', before)
                meth = (mm.group(1) or mm.group(2)).upper() if mm else None
                lits.append({'file': frel, 'line': line, 'url': v, 'method': meth})
    return lits, strings, masked_by


def match_go(interfaces: list[dict], masked_by: dict[str, str]) -> None:
    """Go reach beyond string literals: a command held in a variable is reached when a _test.go file of its package
    names the variable; a package is reached by a _test.go file in its directory (its own tests) as well as by an
    import of its path (matched as a string). Both are test_derived and weak: naming is not running."""
    for itf in interfaces:
        hits = list(itf.get('reached_by', []))
        if itf.get('go_ident') and itf['kind'] == 'cli':
            rx = re.compile(r'(?<![\w.])' + re.escape(itf['go_ident']) + r'\b')
            for f, masked in masked_by.items():
                if PurePosixPath(f).parent.as_posix() == itf['go_dir']:
                    m = rx.search(masked)
                    if m:
                        hits.append({'file': f, 'line': masked.count('\n', 0, m.start()) + 1})
        if itf.get('framework') == 'go-package':
            hits += [{'file': f, 'line': 1} for f in masked_by if PurePosixPath(f).parent.as_posix() == itf['go_dir']][:5]
        if hits:
            uniq = {(h['file'], h['line']): h for h in hits}
            itf['reached_by'] = [uniq[k] for k in sorted(uniq)][:20]
            itf['reached'] = True


# ---------------------------------------------------------------- what the readers cover
READ_CENSUS = {'Python', 'JavaScript', 'TypeScript', 'Go'}     # lang_census names of the languages read
PARTIAL_CENSUS = {'Rust': 'Cargo binaries only, not their sub-commands'}


def repo_slug(url: str) -> tuple[str, str] | None:
    """('owner', 'name') from a repository URL or a Go module path."""
    parts = [p for p in re.sub(r'\.git$', '', re.sub(r'^[a-z+]+://|^git@|^git\+', '', url.strip())).replace(':', '/').split('/') if p]
    return (parts[-2].lower(), parts[-1].lower()) if len(parts) >= 2 else None


def manifest_repo(d: Path) -> tuple[str, str] | None:
    for name in ('package.json', 'Cargo.toml', 'pyproject.toml', 'go.mod'):
        p = d / name
        if not p.is_file():
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        if name == 'package.json':
            try:
                rep = json.loads(text).get('repository')
            except (ValueError, AttributeError):
                rep = None
            url = rep.get('url') if isinstance(rep, dict) else rep
        elif name == 'go.mod':
            m = re.search(r'(?m)^\s*module\s+"?([^\s"]+)', text)
            url = m.group(1) if m else None
        else:
            m = re.search(r'(?m)^\s*(?:repository|Repository|Source)\s*=\s*"([^"]+)"', text)
            url = m.group(1) if m else None
        if isinstance(url, str) and url:
            return repo_slug(url)
    return None


def bundled_trees(root: Path, files: list[str]) -> dict[str, str]:
    """Directories that hold a bundled copy of another project (an embedded driver, an SDK): a non-root directory with
    its own licence file and a manifest whose repository names a different owner and project than the repository's own
    (root go.mod module, package.json repository, Cargo or pyproject repository). {dir: why}."""
    own = manifest_repo(root)
    if not own:
        return {}
    out = {}
    licensed = {PurePosixPath(f).parent.as_posix() for f in files if re.match(r'(?i)(?:licen[cs]e|copying)(?:\.\w+)?$',
                                                                               PurePosixPath(f).name)}
    for d in sorted(licensed, key=len):
        if d in ('.', '') or any(d.startswith(b + '/') for b in out):
            continue
        theirs = manifest_repo(root / d)
        if theirs and theirs[0] != own[0] and theirs[1] != own[1]:
            out[d] = f'its manifest names another repository ({"/".join(theirs)}) and it carries its own licence'
    return out


# ---------------------------------------------------------------- tests
PY_CLIENT = set(HTTP) | {'request', 'websocket_connect'}
JS_STRING = re.compile(r'(?:\.(get|post|put|patch|delete|head|options)\s*\(\s*)?([\'"`])((?:\\.|(?!\2).)*)\2', re.S)


def test_literals(root: Path, files: list[str], every_file: bool = False) -> list[dict]:
    """URL-like literals in test files (every file when every_file): {file, line, url (fields as {}), method or None}."""
    out = []
    for rel in files:
        if not every_file and not is_test(rel):
            continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        if rel.endswith('.py'):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            method_of = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in PY_CLIENT and n.args:
                    method_of[id(n.args[0])] = n.func.attr.upper()
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    url = n.value
                elif isinstance(n, ast.JoinedStr):
                    url = ''.join(v.value if isinstance(v, ast.Constant) else '{}' for v in n.values)
                else:
                    continue
                if '/' in url and len(url) < 300 and '\n' not in url:
                    out.append({'file': rel, 'line': n.lineno, 'url': url, 'method': method_of.get(id(n))})
        elif rel.endswith(JS_EXT):
            for m in JS_STRING.finditer(text):
                url = re.sub(r'\$\{[^}]*\}', '{}', m.group(3)) if m.group(2) == '`' else m.group(3)
                if '/' in url and len(url) < 300 and '\n' not in url:
                    out.append({'file': rel, 'line': text.count('\n', 0, m.start()) + 1, 'url': url,
                                'method': m.group(1).upper() if m.group(1) else None})
    return out


def template_regex(path: str) -> re.Pattern:
    rx = ''
    for part in re.split(r'(\{[^}]*\}|<[^>]*>|:\w+)', path):     # FastAPI {id}, Flask <int:id>, Express :id
        if (part.startswith('{') and part.endswith(('*}', ':path}'))) or part.startswith('<path:'):
            rx += '.+'
        elif part.startswith(('{', '<')) or (part.startswith(':') and len(part) > 1):
            rx += '[^/]+'
        else:
            rx += re.escape(part)
    return re.compile(rx.rstrip('/') + '/?$')


def normalise_url(url: str) -> str | None:
    url = re.sub(r'^[a-z]+://[^/]+', '', url.strip())
    url = url.split('?')[0].split('#')[0]
    if not url.startswith('/') and not url.startswith('{}/'):
        return None
    return re.sub(r'\{\}', 'X', url)


IMPORT_CTX = re.compile(r'(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+)$')


def stem_of(path: str) -> str:
    """A repo-relative module path without extension, with a built directory mapped to its source
    (dist/cli/bin.js -> src/cli/bin)."""
    p = re.sub(r'\.(?:[cm]?[jt]sx?|d\.ts)$', '', path.lstrip('./'))
    return re.sub(r'(^|/)(?:dist|build|lib|out)(/)', r'\1src\2', p)


def test_strings(base: Path, names: list[str], rel_base: str) -> list[dict]:
    """Every short string literal in the test files: {file, line, value, import (repo-relative module stem when the
    literal is a relative import)}. Used for command, tool and package-export matching."""
    out = []
    for rel in names:
        if not rel.endswith(JS_EXT + ('.py',)):
            continue
        text = (base / rel).read_text(encoding='utf-8', errors='replace')
        frel = f'{rel_base}/{rel}' if rel_base not in ('.', '') else rel
        if rel.endswith('.py'):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and 0 < len(n.value) < 200:
                    out.append({'file': frel, 'line': n.lineno, 'value': n.value})
            continue
        for m in JS_STRING.finditer(text):
            v = m.group(3)
            if not v or len(v) >= 200 or '\n' in v:
                continue
            item = {'file': frel, 'line': text.count('\n', 0, m.start()) + 1, 'value': v}
            if v.startswith('.') and IMPORT_CTX.search(text[max(0, m.start() - 12):m.start()]):
                target = os.path.normpath(os.path.join(PurePosixPath(frel).parent.as_posix(), v)).replace('\\', '/')
                item['import'] = stem_of(target)
            out.append(item)
    return out


def match_names(interfaces: list[dict], strings: list[dict]) -> None:
    """Commands, tools and package exports reached by a test string that names them (test_derived, weak: naming a
    command is not running it)."""
    by_value: dict[str, list[dict]] = {}
    for s_ in strings:
        by_value.setdefault(s_['value'], []).append(s_)
    imports = [s_ for s_ in strings if 'import' in s_]
    for itf in interfaces:
        if itf.get('kind') in URL_KINDS:
            continue
        hits = []
        if itf['kind'] == 'tools':
            hits = by_value.get(itf['path'], [])
        elif itf['kind'] == 'package_exports':
            hits = list(by_value.get(itf['path'], []))
            if itf.get('target'):
                src = stem_of(itf['target'])
                hits += [s_ for s_ in imports if s_['import'] in (src, re.sub(r'/index$', '', src))]
        elif itf['kind'] == 'cli':
            words = itf['path'].split()
            word = words[-1]
            rx = re.compile(r'(?:^|[\s/])' + re.escape(itf['path'] if len(words) > 1 else word) + r'(?:$|\s)')
            hits = [s_ for s_ in strings if s_['value'] == word or s_['value'].startswith(word + ' ') or rx.search(s_['value'])]
            if itf.get('target'):
                tgt = stem_of(itf['target'])
                built = re.sub(r'\.(?:[cm]?[jt]sx?)$', '', itf['target'])
                hits += [s_ for s_ in strings if s_.get('import') == tgt or (
                    '/' in s_['value'] and len(s_['value']) > 4 and
                    (tgt.endswith(stem_of(s_['value'])) or built.endswith(re.sub(r'\.(?:[cm]?[jt]sx?)$', '', s_['value'].lstrip('./')))))]
        uniq = {(h['file'], h['line']): h for h in hits}
        itf['reached_by'] = [{'file': f, 'line': ln} for f, ln in sorted(uniq)][:20]
        itf['reached'] = bool(uniq)


# routers that try a rule without converters before one with them, whatever the declaration order (Werkzeug sorts
# its rules, Next.js prefers a static segment, Go's ServeMux the most specific pattern); Express and Starlette match
# in declaration order, so they are left alone
STATIC_FIRST = {'flask': 'flask', 'nextjs': 'next', 'nextjs-pages': 'next', 'nextjs-metadata': 'next',
                'go-net-http': 'go-net-http'}
URL_KINDS = ('routes', 'pages')                 # interfaces reached by a URL literal


def has_params(path: str) -> bool:
    return bool(re.search(r'\{[^}]*\}|<[^>]*>|(?:^|/):\w+|\[[^\]]+\]', path))


def match(interfaces: list[dict], lits: list[dict]) -> None:
    static_rx: dict[str, list[re.Pattern]] = {}
    for itf in interfaces:
        fam = STATIC_FIRST.get(itf.get('framework'))
        if itf.get('kind', 'routes') in URL_KINDS and fam and not has_params(itf['path']):
            static_rx.setdefault(fam, []).append(template_regex(itf['path']))
    for itf in interfaces:
        if itf.get('kind', 'routes') not in URL_KINDS:
            continue
        if itf.get('framework') == 'go-listener':     # what it serves is unknown, so no URL can be said to reach it
            itf['reached_by'], itf['reached'] = [], False
            continue
        rxs = [template_regex(itf['path'])]
        if itf.get('optional_catch_all'):           # [[...x]] also matches the bare parent URL
            rxs.append(template_regex(re.sub(r'/?\{\w+\*\}$', '', itf['path']) or '/'))
        fam = STATIC_FIRST.get(itf.get('framework'))
        shadow = static_rx.get(fam, []) if fam and has_params(itf['path']) else []
        hits = []
        for lit in lits:
            url = normalise_url(lit['url'])
            if url is None or not any(rx.match(url) for rx in rxs):
                continue
            if any(s.match(url) for s in shadow):        # the router sends this URL to the static rule, not this one
                continue
            if lit['method'] and itf['method'] not in ('ANY', lit['method']):
                continue
            hits.append({'file': lit['file'], 'line': lit['line'], **({'method': lit['method']} if lit['method'] else {})})
        itf['reached_by'] = hits[:20]
        itf['reached'] = bool(hits)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path, nargs='?')
    ap.add_argument('--tests', type=Path, action='append', default=[],
                    help='a directory (or file) of tests whose URL literals are matched against the interfaces; repeat')
    ap.add_argument('--no-tests', action='store_true',
                    help='the repository has no tests: list the interfaces with nothing reached (not with --tests)')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--include', action='append', default=[], metavar='DIR',
                    help='read a directory that is skipped by default (dist/, build/, out/...); repeat')
    ap.add_argument('--self-test', action='store_true', help='check the readers on a synthetic repository')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.repo is None:
        ap.error('the repository path is required')
    if args.no_tests and args.tests:
        ap.error('--no-tests and --tests contradict each other: give one')
    root = args.repo.resolve()
    include = tuple(i.replace('\\', '/').strip('/') for i in args.include)
    skipped, kept = [], []
    files = walk(root, include, skipped, kept)
    kept_paths = {k['path'] for k in kept}
    cen = lang_census.census(root)
    bundled = bundled_trees(root, files)
    for e in cen['excluded']:
        if e['path'] in kept_paths or any(e['path'].startswith(k + '/') for k in kept_paths):
            continue                    # a hand-written dist/ the census counts as build output is read as source here
        if e['source_files'] and not any(e['path'] == b or e['path'].startswith(b + '/') for b in bundled):
            bundled[e['path']] = e['reason']
    if bundled:
        files = [f for f in files if not any(f.startswith(b + '/') for b in bundled)]
    py, notes, project = python_interfaces(root, files)
    for k in kept:
        notes.append(f"{k['path']}/ read as product source: {k['reason']}")
    for sk in skipped:
        notes.append(f"{sk['path']}/ skipped ({sk['files']} files): {sk['reason']}; pass --include {sk['path']} if it is "
                     f"hand-written source")
    php, php_notes = py_extras.php_routes(root, files)
    notes += php_notes
    for b, why in sorted(bundled.items()):
        notes.append(f'{b}/ skipped as third-party or generated: {why}; its interfaces are not the product\'s')
    coverage = lang_census.language_coverage(cen, READ_CENSUS, tool='list_interfaces.py',
                                             what='list of the product\'s interfaces')
    if coverage['status'] != 'unsupported_dominant' and coverage.get('dominant') in PARTIAL_CENSUS:
        coverage['status'] = 'partial_dominant'
        coverage['warning'] = (f'this repository is mostly {coverage["dominant"]}, which list_interfaces.py reads only '
                               f'in part ({PARTIAL_CENSUS[coverage["dominant"]]}); map the rest by hand')
    if coverage.get('warning'):
        notes.append('WARNING: ' + coverage['warning'])
    manifests, progs = manifest_interfaces(root, files)
    gos = go_interfaces(root, files)
    others = manifests + js_commands_and_tools(root, files, progs) + py_commands_and_tools(root, files, project) + \
        [g for g in gos if g['kind'] != 'routes']
    nxt, not_read, handled = next_interfaces(root, files)
    interfaces = sorted(py + [{'kind': 'routes', **r} for r in php] + nxt + js_interfaces(root, files, handled)
                        + [g for g in gos if g['kind'] == 'routes'] + others,
                        key=lambda x: (x['kind'], x['path'], x['method'], x['file']))
    for nr in not_read:
        notes.append(f"{nr['framework']} ({nr['project']}): {nr['kind']} not read ({nr['count']} file(s), e.g. "
                     f"{', '.join(nr['files'][:3])}): {nr['why']}; the list is partial for these")
    res = {'repo': root.name, 'interfaces': interfaces, 'notes': notes, 'language_coverage': coverage, 'not_read': not_read,
           'middleware': py_extras.middleware(project), 'skipped_dirs': skipped, 'source_build_dirs': kept,
           'summary': {'total': len(interfaces), 'by_kind': {}, 'by_framework': {}}}
    for i in interfaces:
        res['summary']['by_framework'][i['framework']] = res['summary']['by_framework'].get(i['framework'], 0) + 1
        res['summary']['by_kind'][i['kind']] = res['summary']['by_kind'].get(i['kind'], 0) + 1
    if not interfaces:
        notes.append('no supported interface found: looked for HTTP routes (FastAPI, Flask, Azure Functions, Next.js, '
                     'Express, a plain Node server), CLI commands (package.json bin, commander/yargs/cac, argparse/click/'
                     'typer, pyproject scripts, Cargo binaries), package exports and MCP tools. An empty list is not '
                     '"no interfaces": map them by hand (SKILL.md Step 4) or record interface coverage as not assessed')
    if args.no_tests:
        for itf in interfaces:
            itf['reached_by'], itf['reached'] = [], False
        res['summary'].update(reached=0, tests='none',
                              method='--no-tests: the repository has no tests, so no interface is reached by one')
    if args.tests:
        missing = [str(t) for t in args.tests if not t.exists()]
        if missing:
            print(f'error: --tests path does not exist: {", ".join(missing)} (for a repository with no tests, pass '
                  f'--no-tests)', file=sys.stderr)
            return 2
        lits, strings, go_masked = [], [], {}
        itf_files = {i['file'] for i in interfaces}
        res['tests_warnings'] = []
        for t in args.tests:
            t = t.resolve()
            test_files = [n for n in (walk(t) if t.is_dir() else [t.name]) if n.endswith(('.py', '.go') + JS_EXT)]
            if not test_files:
                notes.append(f'no test files under --tests {t}: "0 reached" says nothing about tests there')
                print(f'WARNING: no test files under --tests {t}', file=sys.stderr)
            base = t if t.is_dir() else t.parent
            names = walk(t) if t.is_dir() else [t.name]
            rel_base = base.relative_to(root).as_posix() if base.is_relative_to(root) else str(base)
            if bundled and base.is_relative_to(root):
                names = [n for n in names if not any(f'{rel_base}/{n}'.lstrip('./').startswith(b + '/') for b in bundled)]
            repo_rel = lambda n: f'{rel_base}/{n}' if rel_base not in ('.', '') else n  # noqa: E731
            product = [n for n in names if repo_rel(n) in itf_files and not is_test(repo_rel(n), root)]
            if product:
                names = [n for n in names if n not in product]
                msg = (f'--tests {t}: {len(product)} file(s) under it declare the product\'s interfaces (e.g. '
                       f'{", ".join(repo_rel(n) for n in product[:3])}); they are product code and are not read as tests')
                res['tests_warnings'].append(msg)
                notes.append(msg)
                print(f'WARNING: {msg}', file=sys.stderr)
            code_files = [n for n in names if n.endswith(('.py', '.go') + JS_EXT)]
            if code_files and not any(is_test(repo_rel(n), root) for n in code_files):
                msg = (f'--tests {t}: none of its {len(code_files)} code file(s) look like tests (no test_*.py, '
                       f'*.test.* or *.spec.*, _test.go, or tests/, e2e/, __tests__/ folder); they are read as test code '
                       f'anyway, so a "reached" may be product code naming a URL. If the repository has no tests, '
                       f'pass --no-tests')
                res['tests_warnings'].append(msg)
                notes.append(msg)
                print(f'WARNING: {msg}', file=sys.stderr)
            # everything under --tests is test code, except Go: Go tests sit beside the code, so only _test.go is read
            for lit in test_literals(base, [n for n in names if not n.endswith('.go')], every_file=True):
                lits.append({**lit, 'file': f'{rel_base}/{lit["file"]}' if rel_base != '.' else lit['file']})
            if others:
                strings += test_strings(base, names, rel_base)
            glits, gstrings, gmasked = go_test_literals(base, names, rel_base)
            lits += glits
            strings += gstrings
            go_masked.update(gmasked)
        match(interfaces, lits)
        match_names(interfaces, strings)
        match_go(interfaces, go_masked)
        res['summary']['reached'] = sum(1 for i in interfaces if i['reached'])
        res['summary']['method'] = ('test string literals matched against path templates ({id} and :id match one segment), '
                                    'with the client method when the literal is its first argument; commands, tools and '
                                    'package exports by a test string naming them or a test import of the export\'s '
                                    'source (naming is weaker than calling): test_derived')
    text = json.dumps(res, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    s = res['summary']
    if not interfaces:
        print('WARNING: no supported interface found (see notes)', file=sys.stderr)
    for sk in skipped:
        print(f"note: {sk['path']}/ skipped as build output ({sk['reason']}); --include {sk['path']} reads it",
              file=sys.stderr)
    for nr in not_read:
        print(f"note: {nr['framework']} ({nr['project']}) interfaces not read: {nr['kind']} ({nr['count']} file(s), e.g. "
              f"{', '.join(nr['files'][:3])}); the list is partial for these (see not_read)", file=sys.stderr)
    if coverage.get('warning'):
        bar = '!' * 78
        print(f'{bar}\nWARNING: {coverage["warning"]}\n{bar}', file=sys.stderr)
    print(f'{s["total"]} interfaces ' + ', '.join(f'{v} {k}' for k, v in s['by_framework'].items())
          + (f'; {s["reached"]} reached by a test literal' if 'reached' in s else '')
          + (f' -> {args.out}' if args.out else ''), file=sys.stderr)
    if not args.out:
        sys.stdout.write(text)
    return 3 if coverage['status'] == 'unsupported_dominant' else 0


SELF_TEST_FILES = {
    'package.json': '{"name": "tidepool-repo", "private": true, "workspaces": ["packages/*"]}\n',
    'packages/tidepool/package.json': (
        '{"name": "tidepool", "bin": {"tidepool": "./dist/cli/bin.js"},\n'
        ' "exports": {".": "./dist/index.js", "./reporters": {"import": "./dist/reporters/index.js"}}}\n'),
    'packages/tidepool/src/index.ts': 'export function sample(n: number) { return n; }\n',
    'packages/tidepool/src/reporters/index.ts': 'export const json = () => "{}";\n',
    'packages/tidepool/src/cli/bin.ts': (
        "import { Command } from 'commander';\n"
        "const program = new Command();\nprogram.name('tidepool');\n"
        "program.command('run [files...]').action(() => {});\n"
        "program.command('list').action(() => {});\n"
        "program.command('purge').action(() => {});\n"),
    'packages/tidepool/src/mcp/server.ts': (
        "import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';\n"
        "import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';\n"
        "const server = new McpServer({ name: 'tidepool', version: '1.0.0' });\n"
        "server.registerTool('list_samples', {}, async () => ({ content: [] }));\n"
        "server.registerTool('drop_sample', {}, async () => ({ content: [] }));\n"
        "await server.connect(new StdioServerTransport());\n"),
    'packages/tidepool/src/serve.ts': (
        "import http from 'node:http';\n"
        "export const start = () => http.createServer((req, res) => res.end('ok')).listen(0);\n"),
    'packages/tidepool/tests/cli.test.ts': (
        "import { sample } from '../src/index.ts';\n"
        "import { it } from 'vitest';\n"
        "it('runs', () => { spawn('node', ['dist/cli/bin.js', 'run', 'a.spec.ts']); });\n"
        "it('lists tools', async () => { await client.callTool({ name: 'list_samples' }); });\n"),
    'tools/e2e/pyproject.toml': '[project]\nname = "crawler"\n\n[project.scripts]\ncrawl = "crawler.cli:main"\n',
    'tools/e2e/crawler/__init__.py': '',
    'tools/e2e/crawler/cli.py': ('import argparse\n\ndef main():\n    p = argparse.ArgumentParser(prog="crawl")\n'
                             '    sub = p.add_subparsers()\n    sub.add_parser("fetch")\n'),
}


GO_SELF_TEST_FILES = {
    'go.mod': 'module example.test/harbour\n\ngo 1.26\n\nrequire github.com/urfave/cli/v2 v2.27.0\n',
    'cmd/harbour/main.go': ('package main\n\nimport (\n\t"os"\n\n\t"example.test/harbour/internal/app"\n)\n\n'
                            'func main() { _ = app.Run(os.Args) }\n'),
    'internal/app/app.go': ('package app\n\nimport "github.com/urfave/cli/v2"\n\n// Run builds the { "Name": "fake" } app.\n'
                            'func Run(args []string) error {\n\ta := &cli.App{\n\t\tName:  "harbour",\n'
                            '\t\tUsage: "manage berths {braces in a string}",\n'
                            '\t\tFlags: []cli.Flag{&cli.StringFlag{Name: "config"}},\n'
                            '\t\tCommands: []*cli.Command{\n\t\t\tsyncCommand,\n\t\t\t{\n\t\t\t\tName:   "serve",\n'
                            '\t\t\t\tAction: serve,\n\t\t\t},\n\t\t},\n\t}\n\treturn a.Run(args)\n}\n\n'
                            'func serve(c *cli.Context) error { return nil }\n'),
    'internal/app/sync.go': ('package app\n\nimport "github.com/urfave/cli/v2"\n\nvar syncCommand = &cli.Command{\n'
                             '\tName:  "sync",\n\tUsage: "sync berths",\n\tSubcommands: []*cli.Command{\n'
                             '\t\t{Name: "push", Action: push},\n\t\tpullCommand(),\n\t},\n}\n\n'
                             'func pullCommand() *cli.Command {\n\treturn &cli.Command{Name: "pull", Action: pull}\n}\n\n'
                             'func push(c *cli.Context) error { return nil }\nfunc pull(c *cli.Context) error { return nil }\n'),
    'internal/app/sync_test.go': ('package app\n\nimport (\n\t"testing"\n\n\t"github.com/urfave/cli/v2"\n)\n\n'
                                  'func TestSync(t *testing.T) {\n\ta := &cli.App{Commands: []*cli.Command{syncCommand}}\n'
                                  '\tif err := a.Run([]string{"x", "sync"}); err != nil {\n\t\tt.Fatal(err)\n\t}\n}\n'),
    'tools/ferryctl/main.go': ('package main\n\nimport "github.com/spf13/cobra"\n\n'
                               'var rootCmd = &cobra.Command{Use: "ferryctl", Short: "ferry control"}\n'
                               'var dockCmd = &cobra.Command{Use: "dock [berth]", Run: func(cmd *cobra.Command, args []string) {}}\n\n'
                               'func main() {\n\trootCmd.AddCommand(dockCmd)\n\t_ = rootCmd.Execute()\n}\n'),
    'tools/tidecheck/main.go': ('package main\n\nimport (\n\t"flag"\n\t"fmt"\n\t"os"\n)\n\nfunc main() {\n'
                                '\tdry := flag.Bool("dry-run", false, "plan only")\n'
                                '\tverify := flag.NewFlagSet("verify", flag.ExitOnError)\n\tflag.Parse()\n'
                                '\tfmt.Println(*dry, verify.Parse(os.Args[1:]))\n}\n'),
    'internal/api/server.go': ('package api\n\nimport "net/http"\n\nfunc Routes() *http.ServeMux {\n'
                               '\tmux := http.NewServeMux()\n\tmux.HandleFunc("GET /berths/{id}", getBerth)\n'
                               '\tmux.HandleFunc("POST /berths", createBerth)\n'
                               '\tmux.Handle("/healthz", http.HandlerFunc(health))\n\treturn mux\n}\n\n'
                               'func Serve() error { return http.ListenAndServe(":8080", Routes()) }\n\n'
                               'func getBerth(w http.ResponseWriter, r *http.Request)    {}\n'
                               'func createBerth(w http.ResponseWriter, r *http.Request) {}\n'
                               'func health(w http.ResponseWriter, r *http.Request)      {}\n'),
    'internal/api/server_test.go': ('package api\n\nimport (\n\t"net/http"\n\t"net/http/httptest"\n\t"testing"\n)\n\n'
                                    'func TestGetBerth(t *testing.T) {\n'
                                    '\treq := httptest.NewRequest(http.MethodGet, "/berths/7", nil)\n'
                                    '\tRoutes().ServeHTTP(httptest.NewRecorder(), req)\n}\n'),
    'internal/metrics/metrics.go': ('package metrics\n\nimport "net"\n\nfunc Listen(addr string) (net.Listener, error) {\n'
                                    '\treturn net.Listen("tcp", addr)\n}\n'),
    'pkg/tides/tides.go': 'package tides\n\n// Height is exported.\nfunc Height(hour int) float64 { return 1.5 }\n',
    'vendor/example.test/rope/rope.go': 'package rope\n\nimport "net/http"\n\nfunc init() { http.HandleFunc("/rope", nil) }\n',
    'drivers/berthkit/package.json': ('{"name": "berthkit", "exports": "./index.js",\n'
                                      ' "repository": {"type": "git", "url": "https://example.test/otherorg/berthkit.git"}}\n'),
    'drivers/berthkit/LICENSE': 'A permissive licence.\n',
    'drivers/berthkit/index.js': 'module.exports = {};\n',
    **{f'drivers/ios/BerthAgent/PrivateHeaders/Berth{i}.h': '@interface Berth : NSObject\n@end\n' for i in range(12)},
}


FLASK_SELF_TEST_FILES = {
    'app.py': ('from flask import Flask, render_template, send_from_directory\n\napp = Flask(__name__)\n\n\n'
               '@app.route("/")\ndef home():\n    return render_template("home.html")\n\n\n'
               '@app.route("/robots.txt")\ndef robots():\n    return "User-agent: *"\n\n\n'
               '@app.route("/<token>.txt")\ndef token_file(token):\n    return send_from_directory(".", token + ".txt")\n\n\n'
               '@app.route("/sailings/<code>")\ndef sailing(code):\n    return render_template("sailing.html", code=code)\n'),
    'templates/base.html': '<!doctype html><title>{% block title %}{% endblock %}</title>{% block body %}{% endblock %}\n',
    'templates/home.html': '{% extends "base.html" %}{% block body %}<a href="/sailings/abc">Sailings</a>{% endblock %}\n',
    'templates/sailing.html': '{% extends "base.html" %}{% block body %}{{ code }}{% endblock %}\n',
    'static/js/timetable.js': 'document.querySelectorAll("a").forEach(a => a.dataset.seen = "1");\n',
    'static/js/menu.js': 'export const open = () => document.body.classList.add("menu");\n',
    'retired/handlers.py': 'def handler(request, response):\n    response.status_code = 200\n    return response\n',
    'tests/test_pages.py': ('from app import app\n\n\ndef test_robots():\n    assert app.test_client().get("/robots.txt").status_code == 200\n\n\n'
                            'def test_sailing():\n    assert app.test_client().get("/sailings/abc").status_code == 200\n'),
    **{f'data/sailings/s{i:03d}.json': '{"code": "s%03d", "minutes": 40}\n' % i for i in range(300)},
}


# A FastAPI app with no tests: a flags-only argparse command line selecting serving modes, a router included and a
# middleware added only under a flag, a {path:path} catch-all, StaticFiles mounted on a committed, hand-written dist/
# (no package.json, no bundler), a PHP front controller beside it, and a helper dispatching on sys.argv[1].
KIOSK_SELF_TEST_FILES = {
    'server.py': (
        'import argparse\n'
        'from fastapi import FastAPI\n'
        'from fastapi.responses import HTMLResponse, Response\n'
        'from fastapi.staticfiles import StaticFiles\n'
        'from extras import uploads\n'
        'from extras.guard import SimpleAuthMiddleware\n\n'
        'parser = argparse.ArgumentParser(description="Serve the kiosk")\n'
        'parser.add_argument("--port", type=int, default=8000)\n'
        'parser.add_argument("--offline", action="store_true")\n'
        'parser.add_argument("--uploads", action="store_true")\n'
        'parser.add_argument("--user")\n'
        'args = parser.parse_args()\n\n'
        'app = FastAPI()\n'
        'if args.user:\n'
        '    app.add_middleware(SimpleAuthMiddleware, user=args.user)\n'
        'if args.uploads:\n'
        '    app.include_router(uploads.router)\n\n\n'
        '@app.get("/")\n'
        'async def index():\n'
        '    return HTMLResponse(open("dist/index.html").read())\n\n\n'
        '@app.api_route("/assets/{path:path}", methods=["GET", "HEAD"])\n'
        'async def assets(path: str):\n'
        '    return Response(b"")\n\n\n'
        'app.mount("/", StaticFiles(directory="dist", html=True), name="dist")\n'),
    'extras/__init__.py': '',
    'extras/uploads.py': ('from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/uploads")\n\n\n'
                          '@router.post("/put")\nasync def put():\n    return {}\n'),
    'extras/guard.py': 'class SimpleAuthMiddleware:\n    def __init__(self, app, user=None):\n        self.app = app\n',
    'tools/packer.py': ('import sys\n\n\ndef main():\n    command = sys.argv[1]\n    if command == "pack":\n        return 1\n'
                        '    elif command == "unpack":\n        return 2\n\n\nif __name__ == "__main__":\n    main()\n'),
    'dist/index.html': '<!doctype html><button id="go">Go</button><script src="app.js"></script>\n',
    'dist/app.js': 'document.getElementById("go").onclick = () => fetch("/uploads/put", {method: "POST"});\n',
    'dist/menu.js': 'export const open = () => document.body.classList.add("menu");\n',
    'index.php': ('<?php\n$BASE = \'\';\n$uri = parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH);\n'
                  'if (preg_match(\'#^\' . $BASE . \'/assets/(.*)#\', $uri, $m)) {\n    echo "asset";\n}\n'
                  'elseif (preg_match("#^/uploads/put$#", $uri)) {\n    echo "put";\n}\n'),
    '.htaccess': 'RewriteEngine On\nRewriteRule ^.*$ index.php [L]\n',
}


# A Next.js App Router bird-watching log with no tests: pages (a route group, a dynamic segment with
# generateStaticParams and dynamicParams = false, catch-alls, an .mdx page, a parallel-route slot, an intercepting
# route), a route handler whose export is not on line 1, metadata routes (files and generators), a pages/ router page,
# and interface kinds the reader does not list (a server action, middleware, a service worker in public/).
NEXT_SELF_TEST_FILES = {
    'package.json': ('{"name": "birdlog", "private": true, "scripts": {"build": "next build", "dev": "next dev"},\n'
                     ' "dependencies": {"next": "15.0.0", "react": "19.0.0"}}\n'),
    'next.config.mjs': 'export default { async redirects() { return [{ source: "/old", destination: "/", permanent: true }]; } };\n',
    'app/layout.tsx': 'export default function Root({ children }) { return <html><body>{children}</body></html>; }\n',
    'app/page.tsx': ('import Link from "next/link";\n\n'
                     'export default function Home() { return <Link href="/sightings/7">Latest</Link>; }\n'),
    'app/about/page.mdx': '# About the log\n',
    'app/(marketing)/membership/page.tsx': 'export default function Membership() { return <main />; }\n',
    'app/sightings/[id]/page.tsx': ('export const dynamicParams = false;\n\n'
                                    'export async function generateStaticParams() { return [{ id: "7" }]; }\n\n'
                                    'export default async function Sighting({ params }) { return <main />; }\n'),
    'app/sightings/[id]/opengraph-image.tsx': 'export default function Image() { return new Response(""); }\n',
    'app/species/[...family]/page.tsx': 'export default function Family() { return <main />; }\n',
    'app/guides/[[...topic]]/page.tsx': 'export default function Guide() { return <main />; }\n',
    'app/@modal/default.tsx': 'export default function Default() { return null; }\n',
    'app/@modal/(.)sightings/[id]/page.tsx': 'export default function SightingModal() { return <dialog />; }\n',
    'app/dashboard/page.tsx': 'export default function Dashboard() { return <main />; }\n',
    'app/dashboard/@tallies/page.tsx': 'export default function Tallies() { return <aside />; }\n',
    'app/dashboard/loading.tsx': 'export default function Loading() { return null; }\n',
    'app/feed.xml/route.ts': ('import { list } from "../../lib/sightings";\n\n'
                              'export const dynamic = "force-static";\n\n'
                              'export async function GET() { return new Response(list().join("")); }\n'),
    'app/robots.ts': 'export default function robots() { return { rules: [] }; }\n',
    'app/sitemap.ts': 'export default function sitemap() { return []; }\n',
    'app/manifest.ts': 'export default function manifest() { return { name: "Bird log" }; }\n',
    'app/favicon.ico': 'not really an icon\n',
    'app/icon.png': 'not really a png\n',
    'app/apple-icon.tsx': 'export default function Icon() { return new Response(""); }\n',
    'app/twitter-image.png': 'not really a png\n',
    'app/twitter-image.alt.txt': 'A heron\n',
    'app/actions.ts': '"use server";\n\nexport async function addSighting(form) { return form; }\n',
    'middleware.ts': 'export function middleware(request) { return undefined; }\n',
    'public/sw.js': 'self.addEventListener("fetch", () => {});\n',
    'public/heron.svg': '<svg />\n',
    'pages/archive/[year].tsx': 'export default function Archive() { return <main />; }\n',
    'pages/_app.tsx': 'export default function App({ Component, pageProps }) { return <Component {...pageProps} />; }\n',
    'lib/sightings.ts': 'export const list = () => ["/sightings/7"];\n',
}


def self_test() -> int:
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp(prefix='li-self-'))
    fails = []
    try:
        for rel, text in SELF_TEST_FILES.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text, encoding='utf-8')
        out = tmp / 'out.json'
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            main([str(tmp), '--tests', str(tmp / 'packages/tidepool/tests'), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        got = {(i['kind'], i['path']): i for i in res['interfaces']}

        def want(cond, what):
            if not cond:
                fails.append(what)
        want(('cli', 'tidepool') in got and got[('cli', 'tidepool')]['reached'], 'the package bin is listed and reached')
        want(('cli', 'tidepool run') in got and got[('cli', 'tidepool run')]['reached'], 'commander run is reached')
        want(('cli', 'tidepool purge') in got and not got[('cli', 'tidepool purge')]['reached'],
             'an untested destructive command is listed, not reached')
        want(('tools', 'list_samples') in got and got[('tools', 'list_samples')]['reached']
             and got[('tools', 'list_samples')].get('transport') == 'stdio', 'an MCP stdio tool is listed and reached')
        want(('tools', 'drop_sample') in got and not got[('tools', 'drop_sample')]['reached'], 'an untested tool is not reached')
        want(('package_exports', 'tidepool') in got and got[('package_exports', 'tidepool')]['reached'],
             'the root export is reached through a test import of its source')
        want(('package_exports', 'tidepool/reporters') in got and not got[('package_exports', 'tidepool/reporters')]['reached'],
             'an export subpath no test imports is not reached')
        want(('routes', '/') in got and got[('routes', '/')]['framework'] == 'node-http', 'a plain Node server is one route')
        want(('cli', 'crawl') in got and ('cli', 'crawl fetch') in got,
             'a Python product package in a directory named e2e is read (scripts and argparse)')
        empty = tmp / 'empty'
        empty.mkdir()
        (empty / 'README.md').write_text('nothing\n', encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            main([str(empty), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want(not res['interfaces'] and any('no supported interface found' in n for n in res['notes']),
             'an empty result says nothing supported was found')

        # ---- Go: urfave/cli and cobra command tables, flag, net/http routes and listeners, packages, _test.go reach
        go = tmp / 'go'
        for rel, text in GO_SELF_TEST_FILES.items():
            (go / rel).parent.mkdir(parents=True, exist_ok=True)
            (go / rel).write_text(text, encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            code = main([str(go), '--tests', str(go), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        got = {(i['kind'], i['path'], i['method']): i for i in res['interfaces']}
        paths = {(i['kind'], i['path']) for i in res['interfaces']}
        for p in ('harbour', 'harbour sync', 'harbour sync push', 'harbour sync pull', 'harbour serve'):
            want(('cli', p) in paths, f'the urfave/cli command {p!r} is listed')
        want(('cli', 'ferryctl') in paths and ('cli', 'ferryctl dock') in paths, 'cobra commands are listed with their parent')
        want(('cli', 'tidecheck') in paths and ('cli', 'tidecheck verify') in paths, 'a flag program and its flag set are listed')
        want(('routes', '/berths/{id}', 'GET') in got and ('routes', '/berths', 'POST') in got
             and ('routes', '/healthz', 'ANY') in got, 'net/http ServeMux routes are listed with their methods')
        want(any(i['framework'] == 'go-listener' for i in res['interfaces']), 'a bare listener is listed')
        want(('package_exports', 'example.test/harbour/pkg/tides') in paths
             and not any('internal' in p for k, p in paths if k == 'package_exports'),
             'an exported Go package is listed and internal packages are not')
        want(got.get(('cli', 'harbour sync', 'CLI'), {}).get('reached'), 'a command a _test.go names by its variable is reached')
        want(not got.get(('cli', 'harbour serve', 'CLI'), {}).get('reached', True), 'an untested command is not reached')
        want(got.get(('routes', '/berths/{id}', 'GET'), {}).get('reached'), 'a route a _test.go requests is reached')
        want(not got.get(('routes', '/berths', 'POST'), {}).get('reached', True), 'an unrequested route is not reached')
        want(not any(i['file'].startswith(('vendor/', 'drivers/berthkit/')) for i in res['interfaces']),
             'vendored and bundled third-party trees are not product interfaces')
        want(any('berthkit' in n for n in res['notes']), 'a skipped bundled tree is named in notes')
        want(code == 0 and not res.get('language_coverage', {}).get('warning'),
         'a Go repository is read: no language warning (a header-only driver tree is not counted)')

        # ---- a dominant language the script does not read: loud, machine-readable, distinct exit
        rb = tmp / 'rb'
        (rb / 'lib').mkdir(parents=True)
        for i in range(4):
            (rb / 'lib' / f'berth{i}.rb').write_text('class Berth; end\n', encoding='utf-8')
        (rb / 'package.json').write_text('{"name": "berth-assets", "bin": {"berth-assets": "bin/x.js"}}\n', encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main([str(rb), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want(code == 3 and (res.get('language_coverage', {}).get('dominant') or '').lower() == 'ruby'
             and 'WARNING' in err.getvalue() and any('ruby' in n.lower() for n in res['notes']),
             'an unread dominant language warns on stderr and in notes, sets language_coverage and exits 3')

        # ---- a Flask app rendering Jinja templates, beside a data directory of JSON files
        fl = tmp / 'flask'
        for rel, text in FLASK_SELF_TEST_FILES.items():
            (fl / rel).parent.mkdir(parents=True, exist_ok=True)
            (fl / rel).write_text(text, encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            code = main([str(fl), '--tests', str(fl / 'tests'), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        got = {(i['kind'], i['path']): i for i in res['interfaces']}
        want(code == 0 and not res.get('language_coverage', {}).get('warning'),
             'a Flask app with Jinja templates is read: no language warning, exit 0')
        want(('routes', '/robots.txt') in got and got[('routes', '/robots.txt')]['reached'],
             'a static route a test requests is reached')
        want(('routes', '/<token>.txt') in got and not got[('routes', '/<token>.txt')]['reached'],
             'a converter route is not reached by a URL that a static route matches exactly (the router prefers the static rule)')
        want(got.get(('routes', '/sailings/<code>'), {}).get('reached'), 'a converter route a test requests is reached')
        want(any('retired/handlers.py' in n and 'handler(request, response)' in n for n in res['notes']),
             'a module defining handler(request, response) outside a supported framework is named in notes, not dropped silently')

        # ---- a FastAPI kiosk: StaticFiles mount, conditional router and middleware, flags-only argparse, sys.argv
        # dispatch, a PHP front controller, a hand-written dist/ and --tests problems
        ks = tmp / 'kiosk'
        for rel, text in KIOSK_SELF_TEST_FILES.items():
            (ks / rel).parent.mkdir(parents=True, exist_ok=True)
            (ks / rel).write_text(text, encoding='utf-8')
        (ks / 'tests').mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main([str(ks), '--tests', str(ks / 'tests'), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        got = {(i['kind'], i['method'], i['path']): i for i in res['interfaces']}
        mount = [i for i in res['interfaces'] if i['kind'] == 'routes' and i.get('framework') == 'starlette-static']
        want(mount and mount[0]['path'] == '/{path:path}' and mount[0].get('directory') == 'dist',
             'app.mount("/", StaticFiles(directory="dist")) is listed as a static route')
        want(got.get(('routes', 'POST', '/uploads/put'), {}).get('condition') == 'args.uploads',
             'a route whose include_router sits under an if records the condition')
        want('condition' not in got.get(('routes', 'GET', '/'), {'condition': 1}),
             'an unconditional route records no condition')
        mws = res.get('middleware', [])
        want(any(m.get('class') == 'SimpleAuthMiddleware' and m.get('condition') == 'args.user' for m in mws),
             'app.add_middleware(...) under an if is listed in middleware with its condition')
        srv = got.get(('cli', 'CLI', 'server'))
        want(srv and {'--port', '--offline', '--uploads', '--user'} <= set(srv.get('options', [])),
             'a flags-only argparse program is one cli entry with its options')
        want(('cli', 'CLI', 'packer pack') in got and ('cli', 'CLI', 'packer unpack') in got,
             'sys.argv[1] dispatch is read as sub-commands')
        php = [i for i in res['interfaces'] if i.get('framework') == 'php']
        want({i['path'] for i in php} >= {'/assets/{param:path}', '/uploads/put'},
             'a PHP front controller routing on REQUEST_URI with preg_match is read')
        want(any('PHP' in n and 'index.php' in n for n in res['notes']), 'PHP is named in notes (read by pattern only)')
        want(not any(n.startswith('dist/ skipped') for n in res['notes'])
             and any(n.startswith('dist/') and 'no build step' in n for n in res['notes']),
             'a committed dist/ with no producer is read as source, and notes say why')
        want(any('no test files' in n for n in res['notes']) and 'no test files' in err.getvalue(),
             'an empty --tests directory warns that it held no test files')
        e2 = io.StringIO()
        with contextlib.redirect_stderr(e2):
            try:
                code = main([str(ks), '--tests', str(ks / 'missing'), '--out', str(out)])
            except Exception as exc:  # noqa: BLE001
                code = repr(exc)
        want(code == 2 and 'does not exist' in e2.getvalue(), f'--tests on a missing path exits 2 with a message (got {code})')
        # a dist/ that something builds stays skipped, and is said to be skipped
        (ks / 'package.json').write_text('{"name": "kiosk", "scripts": {"build": "vite build"}}\n', encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            main([str(ks), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want(any(s['path'] == 'dist' for s in res.get('skipped_dirs', [])) and 'dist/' in err.getvalue(),
             'a built dist/ is skipped, listed in skipped_dirs and named on stderr')
        with contextlib.redirect_stderr(io.StringIO()):
            main([str(ks), '--include', 'dist', '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        want(not any(s['path'] == 'dist' for s in res.get('skipped_dirs', [])), '--include dist reads a skipped dist/')

        # ---- Next.js App Router with no tests: pages, metadata routes, a pages/ router page, what is not read
        nx = tmp / 'next'
        for rel, text in NEXT_SELF_TEST_FILES.items():
            (nx / rel).parent.mkdir(parents=True, exist_ok=True)
            (nx / rel).write_text(text, encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = main([str(nx), '--no-tests', '--out', str(out)])
            except SystemExit as exc:
                code = f'exit {exc.code}'
        res = json.loads(out.read_text(encoding='utf-8')) if code == 0 else {'interfaces': [], 'summary': {}}
        got = {(i['kind'], i['path']): i for i in res['interfaces']}
        want(code == 0, f'--no-tests is accepted (got {code})')
        for p in ('/', '/about', '/membership', '/sightings/{id}', '/species/{family*}', '/guides/{topic*}', '/dashboard',
                  '/archive/{year}'):
            want(('pages', p) in got and got[('pages', p)]['method'] == 'GET', f'the page {p} is listed as kind pages')
        want(not any(p in ('/(marketing)/membership', '/_app') or '@' in p or '(' in p for k, p in got),
             'route groups and slots are not URL segments, and pages/_app is not a page')
        sp = got.get(('pages', '/sightings/{id}'), {})
        want(sp.get('generate_static_params') and sp.get('dynamic_params') is False and sp.get('line') == 5,
             'a dynamic page records generateStaticParams, dynamicParams = false and the line of its default export')
        want(got.get(('pages', '/guides/{topic*}'), {}).get('optional_catch_all'), 'an optional catch-all says so')
        slot = [i for i in res['interfaces'] if i['kind'] == 'pages' and i.get('slot') == '@tallies']
        want(slot and slot[0]['path'] == '/dashboard', 'a parallel-route slot page renders at its parent URL and names the slot')
        icpt = [i for i in res['interfaces'] if i.get('intercepts')]
        want(icpt and icpt[0]['path'] == '/sightings/{id}' and icpt[0].get('slot') == '@modal',
             'an intercepting route (.)x is listed at the URL it intercepts, with its slot')
        want(not any(i['file'].endswith(('default.tsx', 'loading.tsx', 'layout.tsx')) for i in res['interfaces']),
             'layouts, loading and default files are not interfaces')
        feed = got.get(('routes', '/feed.xml'), {})
        want(feed.get('method') == 'GET' and feed.get('line') == 5, f'a route handler records the line of its export (got {feed.get("line")})')
        for p in ('/robots.txt', '/sitemap.xml', '/manifest.webmanifest', '/favicon.ico', '/icon.png', '/apple-icon',
                  '/twitter-image.png', '/sightings/{id}/opengraph-image'):
            want(got.get(('routes', p), {}).get('framework') == 'nextjs-metadata', f'the metadata route {p} is listed')
        want(not any(i['path'].endswith('.alt.txt') for i in res['interfaces']), 'an image alt text file is not a route')
        nr = {(x['framework'], x['kind']): x for x in res.get('not_read', [])}
        want(('nextjs', 'server_actions') in nr and 'app/actions.ts' in nr[('nextjs', 'server_actions')]['files'],
             'server actions are named as not read (machine-readable not_read)')
        want(('nextjs', 'middleware') in nr and ('nextjs', 'static_files') in nr
             and 'public/sw.js' in nr[('nextjs', 'static_files')]['files'] and ('nextjs', 'config_routes') in nr,
             'middleware, public/ files (a service worker) and next.config redirects are named as not read')
        want('not read' in err.getvalue() and 'server_actions' in err.getvalue(),
             'what a detected framework has that is not read goes to stderr, so a partial list never looks complete')
        want(res['summary'].get('reached') == 0 and all(i.get('reached') is False and i.get('reached_by') == []
                                                         for i in res['interfaces'])
             and res['summary'].get('tests') == 'none',
             '--no-tests lists every interface as not reached and records that the repository has no tests')
        # --tests pointed at product code: interface files under it are product code, not tests
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main([str(nx), '--tests', str(nx / 'app'), '--out', str(out)])
        res = json.loads(out.read_text(encoding='utf-8'))
        got = {(i['kind'], i['path']): i for i in res['interfaces']}
        want(not got.get(('pages', '/sightings/{id}'), {}).get('reached', True),
             'a link in a page under --tests is product code: it does not reach the page')
        want('look like tests' in err.getvalue() and res.get('tests_warnings'),
             '--tests on a directory with no test-like file warns on stderr and in tests_warnings')
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main([str(nx), '--no-tests', '--tests', str(nx / 'app'), '--out', str(out)])
            except SystemExit as exc:
                code = exc.code
        want(code == 2, '--no-tests with --tests is refused')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('list_interfaces self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('list_interfaces self-test: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
