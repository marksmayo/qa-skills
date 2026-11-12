#!/usr/bin/env python3
"""List an application's HTTP interfaces and the test files whose URLs reach them (stdlib only).

    python list_interfaces.py <repo> [--tests <dir> ...] [--out interfaces.json]

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


def is_test(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(TEST_NAME.search(p.name)) or bool(set(p.parts[:-1]) & TEST_DIRS)


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
        if not rel.endswith('.py') or is_test(rel):
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
                out.append({'framework': 'azure-functions', 'method': meth, 'path': join(project.host_prefix(m.rel), route),
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
                out.append({'framework': 'python', 'method': meth, 'path': join(path), 'file': m.rel, 'line': line, **func,
                            'router': f'{dotted(m.rel)}:{py_routes._unparse(call.func.value)}', 'included': False,
                            'note': 'the object the route is declared on is not a recognised app or router'})
            continue
        for c in project.mount_chains(key):
            for meth in methods:
                out.append({'framework': router['framework'], 'method': meth, 'path': join(c['prefix'], path), 'file': m.rel,
                            'line': line, **func, 'router': ' > '.join(name(k) for k in c['chain']), 'included': c['reached'],
                            **({'note': 'a router prefix is computed, so the path is partial'} if c['status'] == 'dynamic' else {})})
    return out, notes


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
        if not rel.endswith(JS_EXT) or is_test(rel):
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
                out.append({'framework': 'nextjs', 'method': meth, 'path': path, 'file': rel, 'line': 1, 'included': True})
            continue
        if 'pages' in parts[:-1] and 'api' in parts[:-1]:
            i = len(parts) - 1 - parts[::-1].index('pages')
            if parts[i + 1:i + 2] == ['api']:
                segs = parts[i + 1:-1] + ([] if p.stem == 'index' else [p.stem])
                out.append({'framework': 'nextjs-pages', 'method': 'ANY', 'path': next_path(segs), 'file': rel, 'line': 1,
                            'included': True})
                continue
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        if not re.search(r'\bexpress\b|\bRouter\s*\(|\bfastify\b', text):
            continue
        masked = COMMENT.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text)
        for m in EXPRESS.finditer(masked):
            var, meth, _, path = m.groups()
            if var in ('axios', 'request', 'supertest', 'http', 'https', 'fetch', 'client', 'api'):
                continue
            line = masked.count('\n', 0, m.start()) + 1
            out.append({'framework': 'express', 'method': meth.upper() if meth != 'all' else 'ANY', 'path': path, 'file': rel,
                        'line': line, 'router': var, 'included': var == 'app',
                        **({} if var == 'app' else {'prefix_unresolved': True})})
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


def match(interfaces: list[dict], lits: list[dict]) -> None:
    for itf in interfaces:
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
    ap.add_argument('repo', type=Path)
    ap.add_argument('--tests', type=Path, action='append', default=[],
                    help='a directory (or file) of tests whose URL literals are matched against the interfaces; repeat')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    files = walk(root)
    py, notes = python_interfaces(root, files)
    interfaces = sorted(py + js_interfaces(root, files), key=lambda x: (x['path'], x['method'], x['file']))
    res = {'repo': root.name, 'interfaces': interfaces, 'notes': notes,
           'summary': {'total': len(interfaces), 'by_framework': {}}}
    for i in interfaces:
        res['summary']['by_framework'][i['framework']] = res['summary']['by_framework'].get(i['framework'], 0) + 1
    if args.tests:
        lits = []
        for t in args.tests:
            t = t.resolve()
            base = t if t.is_dir() else t.parent
            names = walk(t) if t.is_dir() else [t.name]
            rel_base = base.relative_to(root).as_posix() if base.is_relative_to(root) else str(base)
            for lit in test_literals(base, names, every_file=True):     # everything under --tests is test code
                lits.append({**lit, 'file': f'{rel_base}/{lit["file"]}' if rel_base != '.' else lit['file']})
        match(interfaces, lits)
        res['summary']['reached'] = sum(1 for i in interfaces if i['reached'])
        res['summary']['method'] = ('test string literals matched against path templates ({id} and :id match one segment), '
                                    'with the client method when the literal is its first argument: test_derived')
    text = json.dumps(res, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    s = res['summary']
    print(f'{s["total"]} interfaces ' + ', '.join(f'{v} {k}' for k, v in s['by_framework'].items())
          + (f'; {s["reached"]} reached by a test literal' if 'reached' in s else '')
          + (f' -> {args.out}' if args.out else ''), file=sys.stderr)
    if not args.out:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
