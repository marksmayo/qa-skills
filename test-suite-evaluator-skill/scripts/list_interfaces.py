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
    Next.js    app/**/route.(ts|js) exported GET/POST/... handlers (route groups dropped, [id] ->
               {id}), and pages/api/** files (method ANY)
    Express    app.get/post/... and router.get/post/... with a literal path (pattern-based; a
               router's mount prefix in another file is not resolved: those are marked
               prefix_unresolved)

With --tests, every string literal in the test files (Python string and f-string literals, with
the f-string's fields as wildcards; JavaScript strings and template literals) is matched against
each interface's path template ({id} and :id match one segment); the HTTP method is taken from the
call around the literal when it is a client method (client.get(...), request(app).post(...)). An
interface is "reached" when a test literal matches its path and the method agrees or is unknown.
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
                     the handler, so read it by hand

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
import py_routes  # noqa: E402  (shared route reader)

PRUNE = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', 'env', '__pycache__', '.mypy_cache', '.pytest_cache',
         '.tox', '.nox', 'dist', 'build', 'out', '.next', '.nuxt', 'coverage', 'site-packages', '.turbo', '.cache'}
HTTP = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts')
TEST_NAME = re.compile(r'(^test_.*\.py$|_test\.py$|^conftest\.py$|\.(test|spec)\.[cm]?[jt]sx?$)')
TEST_DIRS = {'tests', 'test', '__tests__', 'spec', 'e2e', 'testing'}


def walk(root: Path) -> list[str]:
    """Repo-relative files, pruning dependency, build and cache directories before descending."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in PRUNE and not d.endswith('.egg-info'))
        rel = Path(dp).relative_to(root).as_posix()
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
    name = lambda key: f'{dotted(key[0])}:{key[1]}'  # noqa: E731
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
            for meth in methods:
                out.append({'kind': 'routes', 'framework': router['framework'], 'method': meth, 'path': join(c['prefix'], path), 'file': m.rel,
                            'line': line, **func, 'router': ' > '.join(name(k) for k in c['chain']), 'included': c['reached'],
                            **({'note': 'a router prefix is computed, so the path is partial'} if c['status'] == 'dynamic' else {})})
    return out, notes, project


# ---------------------------------------------------------------- JavaScript / TypeScript
NEXT_METHOD = re.compile(r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|'
                         r'export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b')
NEXT_REEXPORT = re.compile(r'export\s*\{([^}]*)\}')
EXPRESS = re.compile(r'\b(\w+)\s*\.\s*(get|post|put|patch|delete|all)\s*\(\s*([\'"`])(/[^\'"`]*)\3')
COMMENT = re.compile(r'/\*.*?\*/|(?<![:\'"])//[^\n]*', re.S)


def next_path(parts: list[str]) -> str:
    segs = []
    for p in parts:
        if (p.startswith('(') and p.endswith(')')) or p.startswith('@'):
            continue                              # route groups and parallel-route slots are not URL segments
        m = re.fullmatch(r'\[\[?\.\.\.(\w+)\]?\]', p)
        if m:
            segs.append('{' + m.group(1) + '*}')
            continue
        m = re.fullmatch(r'\[(\w+)\]', p)
        segs.append('{' + m.group(1) + '}' if m else p)
    return join(*segs)


def js_interfaces(root: Path, files: list[str]) -> list[dict]:
    out = []
    for rel in files:
        if not rel.endswith(JS_EXT) or is_test(rel, root):
            continue
        p = PurePosixPath(rel)
        parts = list(p.parts)
        if p.stem == 'route' and 'app' in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index('app')
            text = COMMENT.sub(' ', (root / rel).read_text(encoding='utf-8', errors='replace'))
            methods = [a or b for a, b in NEXT_METHOD.findall(text)]
            for m in NEXT_REEXPORT.finditer(text):
                methods += [x.split(' as ')[-1].strip() for x in m.group(1).split(',')
                            if x.split(' as ')[-1].strip() in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')]
            path = next_path(parts[i + 1:-1])
            for meth in sorted(set(methods)):
                out.append({'kind': 'routes', 'framework': 'nextjs', 'method': meth, 'path': path, 'file': rel, 'line': 1, 'included': True})
            continue
        if 'pages' in parts[:-1] and 'api' in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index('pages')
            if parts[i + 1:i + 2] == ['api']:
                segs = parts[i + 1:-1] + ([] if p.stem == 'index' else [p.stem])
                out.append({'kind': 'routes', 'framework': 'nextjs-pages', 'method': 'ANY', 'path': next_path(segs), 'file': rel, 'line': 1,
                            'included': True})
                continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
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
        if (part.startswith('{') and part.endswith('*}')) or part.startswith('<path:'):
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
        if itf.get('kind') == 'routes':
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


def match(interfaces: list[dict], lits: list[dict]) -> None:
    for itf in interfaces:
        if itf.get('kind', 'routes') != 'routes':
            continue
        rx = template_regex(itf['path'])
        hits = []
        for lit in lits:
            url = normalise_url(lit['url'])
            if url is None or not rx.match(url):
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
    ap.add_argument('--out', type=Path)
    ap.add_argument('--self-test', action='store_true', help='check the readers on a synthetic repository')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.repo is None:
        ap.error('the repository path is required')
    root = args.repo.resolve()
    files = walk(root)
    py, notes, project = python_interfaces(root, files)
    manifests, progs = manifest_interfaces(root, files)
    others = manifests + js_commands_and_tools(root, files, progs) + py_commands_and_tools(root, files, project)
    interfaces = sorted(py + js_interfaces(root, files) + others, key=lambda x: (x['kind'], x['path'], x['method'], x['file']))
    res = {'repo': root.name, 'interfaces': interfaces, 'notes': notes,
           'summary': {'total': len(interfaces), 'by_kind': {}, 'by_framework': {}}}
    for i in interfaces:
        res['summary']['by_framework'][i['framework']] = res['summary']['by_framework'].get(i['framework'], 0) + 1
        res['summary']['by_kind'][i['kind']] = res['summary']['by_kind'].get(i['kind'], 0) + 1
    if not interfaces:
        notes.append('no supported interface found: looked for HTTP routes (FastAPI, Flask, Azure Functions, Next.js, '
                     'Express, a plain Node server), CLI commands (package.json bin, commander/yargs/cac, argparse/click/'
                     'typer, pyproject scripts, Cargo binaries), package exports and MCP tools. An empty list is not '
                     '"no interfaces": map them by hand (SKILL.md Step 4) or record interface coverage as not assessed')
    if args.tests:
        lits, strings = [], []
        for t in args.tests:
            t = t.resolve()
            base = t if t.is_dir() else t.parent
            names = walk(t) if t.is_dir() else [t.name]
            rel_base = base.relative_to(root).as_posix() if base.is_relative_to(root) else str(base)
            for lit in test_literals(base, names, every_file=True):     # everything under --tests is test code
                lits.append({**lit, 'file': f'{rel_base}/{lit["file"]}' if rel_base != '.' else lit['file']})
            if others:
                strings += test_strings(base, names, rel_base)
        match(interfaces, lits)
        match_names(interfaces, strings)
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
        print('WARNING: ' + notes[-1], file=sys.stderr)
    print(f'{s["total"]} interfaces ' + ', '.join(f'{v} {k}' for k, v in s['by_framework'].items())
          + (f'; {s["reached"]} reached by a test literal' if 'reached' in s else '')
          + (f' -> {args.out}' if args.out else ''), file=sys.stderr)
    if not args.out:
        sys.stdout.write(text)
    return 0


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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('list_interfaces self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('list_interfaces self-test: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
