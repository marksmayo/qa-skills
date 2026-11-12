#!/usr/bin/env python3
"""List an application's HTTP interfaces and the test files whose URLs reach them (stdlib only).

    python list_interfaces.py <repo> [--tests <dir> ...] [--out interfaces.json]

An optional helper for SKILL.md Step 4 (interface coverage). It reads source, never imports or runs
it, so it works without the application's dependencies:

    FastAPI    @app.get/post/.../api_route and add_api_route on FastAPI() and APIRouter() objects
               (and a project's own router class or factory whose name contains Router),
               with APIRouter(prefix=...) and include_router(router, prefix=...) resolved across
               modules (relative and absolute imports), parsed with ast (decorators in strings
               and docstrings are never read)
    Flask      @app.route / @bp.route / @bp.get ..., add_url_rule, Blueprint(url_prefix=...) and
               register_blueprint(bp, url_prefix=...)
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

Read the output before using it: dynamic prefixes (a prefix from a variable) are shown as
<dynamic>, and routers never included in an app are listed with included: false.
"""
from __future__ import annotations
import argparse, ast, json, os, re, sys
from pathlib import Path, PurePosixPath

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


# ---------------------------------------------------------------- Python (FastAPI, Flask)
def call_name(n: ast.AST) -> str:
    f = n.func if isinstance(n, ast.Call) else n
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ''


def const_str(n: ast.AST | None) -> str | None:
    if n is None:
        return None
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    return '<dynamic>'


def kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def str_list(n: ast.AST | None) -> list[str] | None:
    if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
        return [e.value.upper() for e in n.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return None


class PyModule:
    def __init__(self, rel: str, name: str, tree: ast.Module):
        self.rel, self.name, self.tree = rel, name, tree
        self.routers: dict[str, dict] = {}       # var -> {kind, prefix, line}
        self.imports: dict[str, str] = {}        # local name -> dotted origin
        self.routes: list[dict] = []
        self.includes: list[dict] = []
        pkg = name.rsplit('.', 1)[0] if '.' in name else ''
        is_pkg = rel.endswith('__init__.py')
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                base = n.module or ''
                if n.level:
                    parts = (name if is_pkg else pkg).split('.') if (name if is_pkg else pkg) else []
                    parts = parts[:len(parts) - (n.level - 1)] if n.level > 1 else parts
                    base = '.'.join([*parts, *([n.module] if n.module else [])])
                for a in n.names:
                    self.imports[a.asname or a.name] = f'{base}.{a.name}' if base else a.name
            elif isinstance(n, ast.Import):
                for a in n.names:
                    self.imports[a.asname or a.name.split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
            elif isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call):
                kind = call_name(n.value)
                # a project's own router class or factory (APIRouterWithAuth(...), make_router(...)) counts by its name
                if kind not in ('FastAPI', 'Flask') and ('Router' in kind or kind.endswith('router')):
                    kind = 'APIRouter'
                elif kind.endswith('Blueprint'):
                    kind = 'Blueprint'
                if kind in ('FastAPI', 'APIRouter', 'Flask', 'Blueprint'):
                    prefix = const_str(kw(n.value, 'prefix') if kind == 'APIRouter' else
                                       kw(n.value, 'url_prefix') if kind == 'Blueprint' else None) or ''
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                        if isinstance(t, ast.Name):
                            self.routers[t.id] = {'kind': kind, 'prefix': prefix, 'line': n.lineno}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                        self.route_call(d, n.lineno, n.name)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                attr = n.func.attr
                if attr in ('add_api_route', 'add_url_rule'):
                    self.route_call(n, n.lineno, None)
                elif attr in ('include_router', 'register_blueprint') and n.args:
                    owner = self.ref(n.func.value)
                    pref = kw(n, 'prefix' if attr == 'include_router' else 'url_prefix')
                    self.includes.append({'owner': owner, 'child': self.ref(n.args[0]), 'line': n.lineno,
                                          'prefix': const_str(pref) if pref is not None else None,
                                          'override': attr == 'register_blueprint' and pref is not None})

    def ref(self, n: ast.AST) -> str | None:
        """A dotted reference for a Name or Attribute chain: local routers stay local (module:var)."""
        if isinstance(n, ast.Name):
            if n.id in self.routers:
                return f'{self.name}:{n.id}'
            return self.imports.get(n.id, n.id)
        if isinstance(n, ast.Attribute):
            base = self.ref(n.value)
            return f'{base}.{n.attr}' if base else None
        return None

    def route_call(self, call: ast.Call, line: int, func: str | None):
        attr = call.func.attr
        owner = self.ref(call.func.value)
        path = const_str(call.args[0] if call.args else kw(call, 'path') or kw(call, 'rule'))
        if path is None or not owner:
            return
        if attr in HTTP:
            methods = [attr.upper()]
        elif attr in ('api_route', 'route', 'add_api_route', 'add_url_rule', 'websocket'):
            methods = ['WEBSOCKET'] if attr == 'websocket' else (str_list(kw(call, 'methods')) or ['GET'])
        else:
            return
        self.routes.append({'owner': owner, 'path': path, 'methods': methods, 'line': line, 'function': func})


def python_interfaces(root: Path, files: list[str]) -> tuple[list[dict], list[str]]:
    mods: dict[str, PyModule] = {}
    notes = []
    for rel in files:
        if not rel.endswith('.py') or is_test(rel):
            continue
        try:
            text = (root / rel).read_text(encoding='utf-8', errors='replace')
            if not re.search(r'FastAPI|Router|router|Flask|Blueprint|register_blueprint|add_url_rule|\.route\(', text):
                continue
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            notes.append(f'{rel}: not parsed ({exc})')
            continue
        parts = list(PurePosixPath(rel).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts = parts[:-1]
        mods['.'.join(parts)] = PyModule(rel, '.'.join(parts), tree)
    names = list(mods)

    def module_for(dotted: str, near: str) -> PyModule | None:
        """The module a dotted import names. The repo root need not be the import root, so a suffix match counts; when
        several modules match (two services that each have api.routes.x), the one sharing the longest leading path
        with the importing module (near) wins, and a tie resolves nothing."""
        if dotted in mods:
            return mods[dotted]
        hits = [n for n in names if n.endswith('.' + dotted)]
        if len(hits) <= 1:
            return mods[hits[0]] if hits else None
        shared = lambda n: len(os.path.commonprefix([n.split('.'), near.split('.')]))  # noqa: E731
        best = max(shared(n) for n in hits)
        top = [n for n in hits if shared(n) == best]
        return mods[top[0]] if len(top) == 1 else None

    def router_key(ref: str | None, near: str) -> str | None:
        """module:var for a reference (local module:var, or an imported pkg.mod.var / pkg.mod attribute chain)."""
        if not ref:
            return None
        if ':' in ref:
            return ref
        mod, _, var = ref.rpartition('.')
        m = module_for(mod, near) if mod else None
        if m and var in m.routers:
            return f'{m.name}:{var}'
        return None

    routers = {f'{m.name}:{v}': {**info, 'module': m} for m in mods.values() for v, info in m.routers.items()}
    children: dict[str, list[dict]] = {}
    included = set()
    for m in mods.values():
        for inc in m.includes:
            o, c = router_key(inc['owner'], m.name), router_key(inc['child'], m.name)
            if o and c and o in routers and c in routers:
                children.setdefault(o, []).append({**inc, 'child_key': c})
                included.add(c)
            else:
                notes.append(f'{m.rel}:{inc["line"]}: could not resolve {inc["child"]} included into {inc["owner"]}')
    routes_of: dict[str, list[dict]] = {}
    for m in mods.values():
        for r in m.routes:
            key = router_key(r['owner'], m.name)
            routes_of.setdefault(key or f'?{m.name}:{r["owner"]}', []).append({**r, 'rel': m.rel})
    out = []

    def emit(key: str, prefix: str, chain: list[str], seen: set, is_root: bool):
        if key in seen:
            return
        seen = seen | {key}
        info = routers[key]
        for r in routes_of.get(key, []):
            for meth in r['methods']:
                out.append({'framework': 'flask' if info['kind'] in ('Flask', 'Blueprint') else 'fastapi', 'method': meth,
                            'path': join(prefix, r['path']), 'file': r['rel'], 'line': r['line'],
                            **({'function': r['function']} if r['function'] else {}), 'router': ' > '.join(chain),
                            'included': is_root})
        for inc in children.get(key, []):
            child = routers[inc['child_key']]
            own = child['prefix']
            if inc['override']:
                own = ''                          # register_blueprint(url_prefix=...) replaces the blueprint's own
            emit(inc['child_key'], join(prefix, inc['prefix'] or '', own), chain + [inc['child_key']], seen, is_root)
    apps = [k for k, v in routers.items() if v['kind'] in ('FastAPI', 'Flask')]
    for k in apps:
        emit(k, '', [k], set(), True)
    for k, v in routers.items():                  # routers never included anywhere: listed with their own prefix
        if k not in included and k not in apps:
            emit(k, v['prefix'], [k], set(), False)
    for k, rs in routes_of.items():
        if k.startswith('?'):
            for r in rs:
                for meth in r['methods']:
                    out.append({'framework': 'python', 'method': meth, 'path': join(r['path']), 'file': r['rel'],
                                'line': r['line'], 'router': k[1:], 'included': False,
                                'note': 'the object the route is declared on is not a recognised app or router'})
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
