#!/usr/bin/env python3
"""Python routes, function triggers and command-line entry points, read with `ast` (nothing is imported or run).

    from py_routes import scan_python, entries
    result = scan_python({'app/main.py': text, ...})      # relative path -> source text
    eps, unparsed = entries(files, root, classify)        # entry points with an auth label (security recon)
    python py_routes.py <dir> [--entries]                 # print what it finds, as JSON

Because the source is parsed, decorators inside strings and docstrings (usage examples) never count.
Every .py file given is parsed, so routers re-exported through a package's __init__.py are followed.

HTTP routes (result['routes']):
  fastapi   `@router.get('/x')`, `@router.api_route('/x', methods=[...])` and `router.add_api_route('/x', fn)`
            on an `APIRouter`, a subclass of it, a class or factory whose name ends in `Router`/`router`, or on
            `FastAPI()`. The full path is include_router(prefix=...) + APIRouter(prefix=...) + the decorator
            path, following `app.include_router(module.router, prefix=...)` across modules, nested routers
            included. Module-level string constants (`API = "/api/v1"`) count as literals.
  flask     `@bp.route('/x', methods=[...])` (default GET), `@bp.get('/x')` and `bp.add_url_rule('/x', ...)`;
            the full path is the register_blueprint(url_prefix=...) value, or else Blueprint(url_prefix=...), + the rule.
  azure-functions  `@app.route(route='items/{id}', methods=[...])` on `func.FunctionApp()` or a functions
            Blueprint; the path is host.json's routePrefix (default `api`) + route (default: the function name).
Each route records `mount`: `resolved` (every prefix was a literal), `unmounted` (a router that no
include_router/register_blueprint call was found for: its own prefix only) or `dynamic` (a prefix or the
included router is computed, e.g. a loop over routers). A router included twice gives two routes.
`response_classes` lists the response classes a handler declares (`response_class=`) or builds
(`StreamingResponse(...)`, Flask `send_file`), so a caller can compare them with the documented media type.
WebSocket routes are left out unless scan_python(..., websockets=True).

Triggers (result['triggers']): Azure Functions non-HTTP triggers (`@app.blob_trigger(path=...)`,
`timer_trigger(schedule=...)`, `queue_trigger(queue_name=...)`, Service Bus, Event Hub, Event Grid, Cosmos DB, Kafka).

Command lines (result['clis']): argparse sub-commands (`sub.add_parser('name')`), click and typer
commands (`@cli.command()`), with the program name from `ArgumentParser(prog=...)` or the file name.

Files that don't parse are listed in result['unparsed'] so a caller can fall back to text matching.

entries() adds what a security review needs: each FastAPI route's dependencies (APIRouter(dependencies=[...]),
include_router(..., dependencies=[...]), FastAPI(dependencies=[...]), route-level dependencies=[...] and
Depends()/Security() parameters, Annotated aliases included), labelled as auth when the dependency's name says
so (get_current_user, require_admin, verify_token, an API key header...); the decorators stacked on a Flask
view (labelled by the caller's classify_decorators); and Azure Functions auth levels and trigger sources. A
route whose router is never included anywhere the script can resolve keeps auth 'unknown'.

This file is shared: the canonical copy is shared/scripts/py_routes.py (tools/check_shared.py copies it).
"""
from __future__ import annotations
import ast, json, re, sys
from pathlib import Path, PurePosixPath

HTTP_VERBS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace')
FASTAPI_CTORS = {'FastAPI', 'APIRouter', 'Starlette'}
FLASK_CTORS = {'Flask', 'Blueprint'}
AZURE_CTORS = {'FunctionApp', 'AsgiFunctionApp', 'WsgiFunctionApp'}
APP_CTORS = {'FastAPI', 'Flask', 'Starlette'}
TRIGGER_DETAIL = ('path', 'schedule', 'queue_name', 'topic_name', 'event_hub_name', 'container_name',
                  'subscription_name', 'topic', 'connection')
ADD_ROUTE = ('add_api_route', 'add_url_rule')


def _name(node) -> str | None:
    """`APIRouter` for Name/Attribute callees (`fastapi.APIRouter` -> APIRouter)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _str(node, consts: dict | None = None):
    """(value, literal?) of a string expression. f-strings render their expressions as {expr}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.Name) and consts and node.id in consts:
        return consts[node.id], True
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                try:
                    parts.append('{' + ast.unparse(v.value) + '}')
                except Exception:
                    parts.append('{?}')
        return ''.join(parts), False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, la = _str(node.left, consts)
        b, lb = _str(node.right, consts)
        if a is not None and b is not None:
            return a + b, la and lb
    return None, False


def _kw(call: ast.Call, *names):
    for k in call.keywords:
        if k.arg in names:
            return k.value
    return None


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ''


def _methods(node) -> list[str]:
    out = []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.append(el.value.upper())
            elif isinstance(el, ast.Attribute):          # func.HttpMethod.GET
                out.append(el.attr.upper())
    return out


def response_classes(fn, dec: ast.Call) -> list[str]:
    """Response classes a handler declares (`response_class=`) or builds (`StreamingResponse(...)`, `send_file(...)`)."""
    out = set()
    rc = _kw(dec, 'response_class')
    if rc is not None and _name(rc):
        out.add(_name(rc))
    for node in ast.walk(fn) if fn is not None else ():
        if isinstance(node, ast.Call):
            n = _name(node.func)
            if n and (n.endswith('Response') or n in ('send_file', 'send_from_directory', 'stream_with_context')):
                out.add(n)
    return sorted(out)


def join(*parts: str) -> str:
    segs = [p.strip('/') for p in parts if p and p.strip('/')]
    return '/' + '/'.join(segs)


def module_names(rel: str) -> list[str]:
    p = PurePosixPath(rel)
    parts = list(p.with_suffix('').parts)
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return ['.'.join(parts[i:]) for i in range(len(parts)) if parts[i:]]


# ---------------------------------------------------------------- dependencies (FastAPI)
def dep_call(node, rel: str) -> dict | None:
    """Depends(x) / Security(y) -> {name, security, line, file}."""
    if isinstance(node, ast.Call) and _name(node.func) in ('Depends', 'Security') and (node.args or node.keywords):
        target = node.args[0] if node.args else _kw(node, 'dependency')
        return {'name': _unparse(target) if target is not None else '', 'security': _name(node.func) == 'Security',
                'line': node.lineno, 'file': rel}
    return None


def dep_list(node, rel: str) -> list[dict]:
    """[Depends(x), Security(y)] -> [dep, ...]."""
    out = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for el in node.elts:
            d = dep_call(el, rel)
            if d:
                out.append(d)
    return out


class Module:
    def __init__(self, rel: str, tree: ast.AST):
        self.rel = rel
        self.tree = tree
        self.imports: dict[str, tuple[str, str | None]] = {}   # local name -> (dotted module, attribute or None)
        self.routers: dict[str, dict] = {}                      # var -> {framework, prefix, literal, line, ctor, app, deps}
        self.consts: dict[str, str] = {}
        self.classes: dict[str, list[str]] = {}                 # class name -> base names
        self.text_imports: set[str] = set()                     # top-level packages imported (absolute imports)
        self.libs: set[str] = set()                             # dotted modules imported (absolute imports)

    def uses(self, lib: str) -> bool:
        return any(x == lib or x.startswith(lib + '.') for x in self.libs)


class Project:
    def __init__(self, files: dict[str, str], host_prefixes: dict[str, str] | None = None):
        self.modules: dict[str, Module] = {}
        self.unparsed: list[str] = []
        self.parse_errors: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = {}
        self.host_prefixes = host_prefixes or {}
        for rel, text in files.items():
            try:
                tree = ast.parse(text, filename=rel)
            except (SyntaxError, ValueError) as exc:
                self.unparsed.append(rel)
                self.parse_errors[rel] = str(exc)
                continue
            self.modules[rel] = Module(rel, tree)
            for n in module_names(rel):
                self.by_name.setdefault(n, []).append(rel)
        for m in self.modules.values():
            self._index(m)
        self.router_ctors = self._router_classes()
        self.collect_routers()
        self.collect_mounts()

    # ---- indexing ----
    def _package(self, rel: str) -> list[str]:
        return list(PurePosixPath(rel).parts[:-1])

    def _index(self, m: Module):
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m.imports[a.asname or a.name.split('.')[0]] = (a.name if a.asname else a.name.split('.')[0], None)
                    m.text_imports.add(a.name.split('.')[0])
                    m.libs.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    pkg = self._package(m.rel)
                    pkg = pkg[:len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                    base = '.'.join(pkg + ([base] if base else []))
                else:
                    m.text_imports.add(base.split('.')[0])
                    m.libs.add(base)
                for a in node.names:
                    m.imports[a.asname or a.name] = (base, a.name)
            elif isinstance(node, ast.ClassDef):
                m.classes[node.name] = [n for n in (_name(b) for b in node.bases) if n]
        for node in m.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                val, lit = _str(node.value)
                if val is not None and lit:
                    m.consts[node.targets[0].id] = val

    def _router_classes(self) -> dict[str, str]:
        """Constructor name -> framework, including project classes that subclass a router."""
        ctors = {n: 'fastapi' for n in FASTAPI_CTORS} | {n: 'flask' for n in FLASK_CTORS} | \
                {n: 'azure-functions' for n in AZURE_CTORS}
        changed = True
        while changed:
            changed = False
            for m in self.modules.values():
                for cls, bases in m.classes.items():
                    if cls in ctors:
                        continue
                    fw = next((ctors[b] for b in bases if b in ctors), None)
                    if fw is None and any('Router' in b for b in bases):
                        fw = 'fastapi'
                    if fw:
                        ctors[cls] = fw
                        changed = True
        return ctors

    def ctor_framework(self, call: ast.Call, m: Module) -> str | None:
        name = _name(call.func)
        if not name:
            return None
        if name == 'Blueprint':
            # flask.Blueprint vs azure.functions.Blueprint
            imported = m.imports.get('Blueprint', ('', None))[0]
            if isinstance(call.func, ast.Attribute) and _name(call.func.value) in ('func', 'functions', 'df') \
                    or imported.startswith('azure') \
                    or (m.uses('azure.functions') or 'azure' in m.text_imports) and not m.uses('flask'):
                return 'azure-functions'
            return 'flask'
        if name in self.router_ctors:
            return self.router_ctors[name]
        if name.endswith('Blueprint'):
            return 'flask'
        if name.endswith(('Router', 'router')):        # a project's router class or factory (make_router())
            return 'fastapi'
        return None

    def collect_routers(self):
        for m in self.modules.values():
            for node in ast.walk(m.tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    fw = self.ctor_framework(node.value, m)
                    if not fw:
                        continue
                    pnode = _kw(node.value, 'prefix') if fw == 'fastapi' else \
                        _kw(node.value, 'url_prefix') if fw == 'flask' else None
                    prefix, lit = _str(pnode, m.consts) if pnode is not None else ('', True)
                    level = _kw(node.value, 'http_auth_level')
                    for t in targets:
                        if isinstance(t, ast.Name):
                            m.routers[t.id] = {'framework': fw, 'prefix': prefix or '', 'literal': lit and prefix is not None,
                                               'line': node.lineno, 'ctor': _name(node.value.func),
                                               'app': _name(node.value.func) in APP_CTORS or fw == 'azure-functions',
                                               'deps': dep_list(_kw(node.value, 'dependencies'), m.rel),
                                               'auth_level': _name(level) if level is not None else None}

    # ---- resolution ----
    def find_module(self, dotted: str, near: str) -> str | None:
        cands = self.by_name.get(dotted) or []
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        nparts = PurePosixPath(near).parts

        def shared(rel):
            n = 0
            for a, b in zip(PurePosixPath(rel).parts, nparts):
                if a != b:
                    break
                n += 1
            return n
        # the longest dotted match is the most specific; then the module closest to the importer
        return max(cands, key=lambda r: (shared(r), -len(r)))

    def resolve(self, m: Module, node, depth: int = 0):
        """Router key (module rel, variable) for an expression naming a router, or None."""
        if depth > 4:
            return None
        if isinstance(node, ast.Name):
            if node.id in m.routers:
                return (m.rel, node.id)
            if node.id in m.imports:
                mod, attr = m.imports[node.id]
                if attr is None:
                    return None
                target = self.find_module(mod, m.rel)
                if target and target in self.modules:
                    tm = self.modules[target]
                    if attr in tm.routers:
                        return (target, attr)
                    if attr in tm.imports:
                        return self.resolve(tm, ast.Name(id=attr), depth + 1)
            return None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in m.imports:
                mod, attr = m.imports[base]
                dotted = f'{mod}.{attr}' if attr else mod
                target = self.find_module(dotted, m.rel) or (self.find_module(mod, m.rel) if attr else None)
                if target and target in self.modules:
                    tm = self.modules[target]
                    if node.attr in tm.routers:
                        return (target, node.attr)
                    if node.attr in tm.imports:
                        return self.resolve(tm, ast.Name(id=node.attr), depth + 1)
        return None

    def router(self, key) -> dict:
        return self.modules[key[0]].routers[key[1]]

    # ---- mounts ----
    def collect_mounts(self):
        self.mounts: dict[tuple, list[dict]] = {}
        self.dynamic_includes: list[dict] = []
        for m in self.modules.values():
            for node in ast.walk(m.tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('include_router', 'register_blueprint') and node.args):
                    continue
                parent = self.resolve(m, node.func.value)
                child = self.resolve(m, node.args[0])
                pnode = _kw(node, 'prefix', 'url_prefix')
                prefix, lit = _str(pnode, m.consts) if pnode is not None else (None, True)
                if child is None:
                    self.dynamic_includes.append({'file': m.rel, 'line': node.lineno,
                                                  'text': _unparse(node)[:160]})
                    continue
                self.mounts.setdefault(child, []).append({'parent': parent, 'prefix': prefix, 'literal': lit,
                                                          'kind': node.func.attr, 'file': m.rel, 'line': node.lineno,
                                                          'deps': dep_list(_kw(node, 'dependencies'), m.rel)})

    def mount_chains(self, key, seen=()) -> list[dict]:
        """Every chain of include calls from a router up to an app:
        [{prefix, status, deps, reached, via, chain}]. prefix is the full prefix (the router's own included);
        status is resolved/unmounted/dynamic; deps are the dependencies added above the router (include calls
        and the routers and app above it, not the router's own); reached is True when the chain ends at an app;
        via is the include call that mounts this router (file:line); chain lists the router keys from the top."""
        r = self.router(key)
        own = r['prefix'] or ''
        status_own = 'resolved' if r['literal'] else 'dynamic'
        edges = self.mounts.get(key) or []
        if not edges:
            if r['app']:
                return [{'prefix': own, 'status': status_own, 'deps': [], 'reached': True, 'via': None, 'chain': [key]}]
            return [{'prefix': own, 'status': 'unmounted' if status_own == 'resolved' else 'dynamic', 'deps': [],
                     'reached': False, 'via': None, 'chain': [key]}]
        out = []
        for e in edges:
            if r['framework'] == 'flask' and e['prefix'] is not None:
                local = e['prefix']                       # register_blueprint(url_prefix=) replaces the blueprint's own
            else:
                local = join(e['prefix'] or '', own) if (e['prefix'] or own) else ''
            status = status_own if e['literal'] else 'dynamic'
            via = f"{e['file']}:{e['line']}"
            if e['parent'] is None or e['parent'] in seen or e['parent'] == key:
                out.append({'prefix': local, 'status': 'dynamic' if e['parent'] is None else status, 'deps': list(e['deps']),
                            'reached': False, 'via': via, 'chain': [key]})
                continue
            parent = self.router(e['parent'])
            for up in self.mount_chains(e['parent'], seen + (key,)):
                worst = 'dynamic' if 'dynamic' in (up['status'], status) else \
                    'unmounted' if 'unmounted' in (up['status'], status) else 'resolved'
                out.append({'prefix': join(up['prefix'], local) if (up['prefix'] or local) else '', 'status': worst,
                            'deps': up['deps'] + parent['deps'] + e['deps'], 'reached': up['reached'], 'via': via,
                            'chain': up['chain'] + [key]})
        return out

    def prefixes(self, key, seen=()) -> list[tuple[str, str]]:
        """[(full prefix, mount status)] for a router: every chain of include calls up to an app."""
        return [(c['prefix'], c['status']) for c in self.mount_chains(key, seen)]

    def host_prefix(self, rel: str) -> str:
        d = PurePosixPath(rel).parent
        while True:
            key = d.as_posix()
            if key in self.host_prefixes:
                return self.host_prefixes[key]
            if key in ('.', '', '/'):
                return 'api'
            d = d.parent

    # ---- raw hits: every decorator or add_*_route call that may declare a route ----
    def hits(self):
        """Yield one dict per `@obj.attr(...)` decorator on a function and per `obj.add_api_route/add_url_rule(...)`:
        {module, fn, call, attr, key (router key or None), azure_name}."""
        for m in self.modules.values():
            for node in ast.walk(m.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    azure_name = None
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'function_name':
                            v = _kw(dec, 'name') or (dec.args[0] if dec.args else None)
                            azure_name, _ = _str(v, m.consts) if v is not None else (None, False)
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                            yield {'module': m, 'fn': node, 'call': dec, 'attr': dec.func.attr,
                                   'key': self.resolve(m, dec.func.value), 'azure_name': azure_name}
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ADD_ROUTE:
                    yield {'module': m, 'fn': None, 'call': node, 'attr': node.func.attr,
                           'key': self.resolve(m, node.func.value), 'azure_name': None}

    # ---- routes, triggers and command lines ----
    def scan(self, websockets: bool = False) -> dict:
        routes, triggers, clis = [], [], []
        for h in self.hits():
            m, node, dec, attr, key = h['module'], h['fn'], h['call'], h['attr'], h['key']
            fn_name = node.name if node is not None else (_view_name(dec) or '')
            router = self.router(key) if key else None
            fw = router['framework'] if router else None
            if fw is None:
                if 'flask' in m.text_imports:
                    fw = 'flask'
                elif 'fastapi' in m.text_imports:
                    fw = 'fastapi'
                elif 'azure' in m.text_imports:
                    fw = 'azure-functions'
            if fw is None:
                continue
            line = dec.lineno
            if fw == 'azure-functions':
                if node is None:
                    continue
                if attr == 'route':
                    rnode = _kw(dec, 'route')
                    route, lit = _str(rnode, m.consts) if rnode is not None else (h['azure_name'] or fn_name, True)
                    meths = _methods(_kw(dec, 'methods')) or ['ANY']
                    for meth in meths:
                        routes.append({'method': meth, 'path': join(self.host_prefix(m.rel), route or ''),
                                       'file': m.rel, 'line': line, 'framework': fw, 'function': fn_name,
                                       'mount': 'resolved' if lit else 'dynamic'})
                elif attr.endswith('_trigger') and attr != 'http_trigger':
                    triggers.append(_trigger(m, dec, attr, h['azure_name'] or fn_name, fw))
                continue
            if attr in HTTP_VERBS or attr in ('api_route', 'route') + ADD_ROUTE or (websockets and attr == 'websocket'):
                pnode = dec.args[0] if dec.args else _kw(dec, 'path', 'rule')
                if pnode is None:
                    continue
                path, lit = _str(pnode, m.consts)
                if path is None:
                    continue
                if key is None and not (path.startswith('/') or path == ''):
                    continue
                if attr in HTTP_VERBS:
                    meths = [attr.upper()]
                elif attr == 'websocket':
                    meths = ['WEBSOCKET']
                else:
                    meths = _methods(_kw(dec, 'methods')) or ['GET']
                mounts = self.prefixes(key) if key else [('', 'unmounted')]
                responses = response_classes(node, dec)
                for prefix, status in mounts:
                    full = join(prefix, path) if (prefix or path.strip('/')) else '/'
                    if path.endswith('/') and len(path) > 1:
                        full += '/'
                    for meth in meths:
                        routes.append({'method': meth, 'path': full, 'file': m.rel, 'line': line,
                                       'framework': fw, 'function': fn_name,
                                       'mount': status if lit else 'dynamic',
                                       **({'router_prefix': prefix} if prefix else {}),
                                       **({'response_classes': responses} if responses else {})})
        for m in self.modules.values():
            clis += self.command_lines(m)
        uniq = {}
        for r in routes:
            uniq.setdefault((r['method'], r['path'], r['file'], r['line']), r)
        return {'routes': sorted(uniq.values(), key=lambda r: (r['file'], r['line'], r['path'], r['method'])),
                'triggers': sorted(triggers, key=lambda t: (t['file'], t['line'])),
                'clis': clis, 'dynamic_includes': self.dynamic_includes, 'unparsed': sorted(self.unparsed)}

    def command_lines(self, m: Module) -> list[dict]:
        out = []
        stem = PurePosixPath(m.rel).stem
        prog = PurePosixPath(m.rel).parent.name if stem in ('__main__', '__init__') else stem
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and _name(node.func) == 'ArgumentParser':
                v = _kw(node, 'prog')
                s, _ = _str(v, m.consts) if v is not None else (None, False)
                if s:
                    prog = s
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_parser' and node.args:
                name, lit = _str(node.args[0], m.consts)
                if name:
                    out.append({'command': f'{prog} {name}', 'file': m.rel, 'line': node.lineno, 'framework': 'argparse'})
        if {'click', 'typer'} & m.text_imports:
            for node in ast.walk(m.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    call = dec if isinstance(dec, ast.Call) else None
                    func = call.func if call else dec
                    if isinstance(func, ast.Attribute) and func.attr == 'command':
                        v = (call.args[0] if call and call.args else _kw(call, 'name') if call else None)
                        name, _ = _str(v, m.consts) if v is not None else (None, False)
                        name = name or node.name.replace('_', '-')
                        fw = 'typer' if 'typer' in m.text_imports else 'click'
                        out.append({'command': f'{prog} {name}', 'file': m.rel, 'line': dec.lineno, 'framework': fw})
        return out


def _view_name(call: ast.Call) -> str | None:
    """The view a `add_api_route(path, endpoint)` / `add_url_rule(rule, endpoint, view_func)` call names."""
    if call.func.attr == 'add_api_route':
        node = call.args[1] if len(call.args) > 1 else _kw(call, 'endpoint')
    else:
        node = call.args[2] if len(call.args) > 2 else _kw(call, 'view_func')
    return _name(node) if node is not None else None


def _trigger(m: Module, dec: ast.Call, attr: str, function: str, fw: str) -> dict:
    detail = {}
    for k in TRIGGER_DETAIL:
        v = _kw(dec, k)
        if v is not None:
            s, _ = _str(v, m.consts)
            if s is not None:
                detail[k] = s
    return {'trigger': attr[:-len('_trigger')], 'function': function, 'file': m.rel, 'line': dec.lineno,
            'framework': fw, **detail}


def host_route_prefixes(files: dict[str, str]) -> dict[str, str]:
    """Directory -> Azure Functions HTTP routePrefix from each host.json (default `api`)."""
    out = {}
    for rel, text in files.items():
        if PurePosixPath(rel).name != 'host.json':
            continue
        try:
            data = json.loads(text.lstrip('﻿'))
            prefix = ((data.get('extensions') or {}).get('http') or {}).get('routePrefix', 'api')
        except (ValueError, AttributeError):
            prefix = 'api'
        out[PurePosixPath(rel).parent.as_posix()] = prefix if isinstance(prefix, str) else 'api'
    return out


def scan_python(files: dict[str, str], host_files: dict[str, str] | None = None, websockets: bool = False) -> dict:
    """files: relative .py path -> text. host_files: relative host.json path -> text (Azure Functions)."""
    return Project(files, host_route_prefixes(host_files or {})).scan(websockets=websockets)


def blank_strings(text: str) -> str:
    """Replace triple-quoted strings with blank lines (keeps line numbers); a fallback for files ast can't parse."""
    return re.sub(r'("""|\'\'\')(.*?)\1', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)


# ---------------------------------------------------------------- entry points with auth (security recon)
# trigger decorator -> (entry kind, method label, kwarg naming what feeds it)
AZ_TRIGGERS = {
    'blob_trigger': ('queue_consumer', 'BLOB', 'path'),
    'queue_trigger': ('queue_consumer', 'QUEUE', 'queue_name'),
    'service_bus_queue_trigger': ('queue_consumer', 'SERVICE_BUS', 'queue_name'),
    'service_bus_topic_trigger': ('queue_consumer', 'SERVICE_BUS', 'topic_name'),
    'event_hub_message_trigger': ('queue_consumer', 'EVENT_HUB', 'event_hub_name'),
    'event_grid_trigger': ('queue_consumer', 'EVENT_GRID', None),
    'cosmos_db_trigger': ('queue_consumer', 'COSMOS_DB', 'container_name'),
    'kafka_trigger': ('queue_consumer', 'KAFKA', 'topic'),
    'timer_trigger': ('other', 'TIMER', 'schedule'),
}
AZ_LEVEL = {'ANONYMOUS': ('public', 'auth_level ANONYMOUS: no function key needed'),
            'FUNCTION': ('api_key', 'auth_level FUNCTION: a function key (x-functions-key header or ?code=)'),
            'ADMIN': ('api_key', 'auth_level ADMIN: the host master key')}
DEP_ADMIN = re.compile(r'(?i)admin|superuser|staff')
DEP_KEY = re.compile(r'(?i)api_?key|apikey|x[-_]api')
DEP_AUTH = re.compile(r'(?i)auth|user|token|jwt|login|permission|role|scope|principal|claim|verif|guard|secur|oauth|'
                      r'bearer|credential|signed_?in|require|current_account|identity')
MIDDLEWARE_AUTH = re.compile(r'(?i)auth|jwt|session|token|login|bearer')


def annotated_aliases(project: Project) -> dict[str, list[dict]]:
    """CurrentUser = Annotated[User, Depends(get_current_user)] -> {'CurrentUser': [dep]} (by name, repo-wide)."""
    out: dict[str, list[dict]] = {}
    for m in project.modules.values():
        for n in m.tree.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(tgt, ast.Name) and isinstance(n.value, ast.Subscript) and _name(n.value.value) == 'Annotated':
                    deps = [d for d in (dep_call(x, m.rel) for x in ast.walk(n.value)) if d]
                    if deps:
                        out[tgt.id] = deps
    return out


def param_deps(fn, rel: str, aliases: dict) -> list[dict]:
    """Depends()/Security() parameters of a handler, Annotated[...] and Annotated aliases included."""
    out = []
    a = fn.args
    for d in list(a.defaults) + [x for x in a.kw_defaults if x is not None]:
        dc = dep_call(d, rel)
        if dc:
            out.append(dc)
    for arg in a.args + a.kwonlyargs:
        ann = arg.annotation
        if ann is None:
            continue
        if isinstance(ann, ast.Subscript) and _name(ann.value) == 'Annotated':
            out += [dc for dc in (dep_call(x, rel) for x in ast.walk(ann)) if dc]
        elif isinstance(ann, ast.Name) and ann.id in aliases:
            out += aliases[ann.id]
    return out


def dep_label(dep: dict) -> str | None:
    text = dep['name']
    if DEP_ADMIN.search(text):
        return 'admin'
    if DEP_KEY.search(text):
        return 'api_key'
    if dep['security'] or DEP_AUTH.search(text):
        return 'authenticated'
    return None


def _host_prefixes_on_disk(root: Path | None, rels) -> dict[str, str]:
    """host.json routePrefix for every directory between each file and root (Azure Functions)."""
    if root is None:
        return {}
    found = {}
    for rel in rels:
        d = PurePosixPath(rel).parent
        while True:
            key = d.as_posix()
            if key not in found:
                h = root / key / 'host.json'
                if h.is_file():
                    try:
                        found[key] = h.read_text(encoding='utf-8-sig')
                    except OSError:
                        found[key] = ''
            if key in ('.', ''):
                break
            d = d.parent
    return host_route_prefixes({f'{k}/host.json' if k not in ('.', '') else 'host.json': t for k, t in found.items()})


def entries(files: dict[str, str], root: Path | None = None, classify_decorators=None) -> tuple[list[dict], list[str]]:
    """Entry points from Python files {rel: text}, each with an auth label; returns (entries, files that did not parse).

    classify_decorators(text) -> (auth, evidence) labels a Flask view's stacked decorators."""
    project = Project(files, _host_prefixes_on_disk(root, files))
    aliases = annotated_aliases(project)
    mw_auth = [f"{m.rel}:{n.lineno}" for m in project.modules.values() for n in ast.walk(m.tree)
               if (isinstance(n, ast.Call) and _name(n.func) == 'add_middleware' and n.args
                   and MIDDLEWARE_AUTH.search(_unparse(n.args[0])))
               or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                   isinstance(d, ast.Call) and _name(d.func) == 'middleware' for d in n.decorator_list))]
    out = []
    for h in project.hits():
        m, fn, dec, verb, key = h['module'], h['fn'], h['call'], h['attr'], h['key']
        if fn is None:
            continue                                  # add_api_route/add_url_rule: listed by scan_python only
        if key:
            r = project.router(key)
            kind = {'fastapi': 'fastapi_app' if r['app'] else 'fastapi_router',
                    'flask': 'flask_app' if r['app'] else 'flask_bp', 'azure-functions': 'azfunc_app'}[r['framework']]
            obj = {'kind': kind, 'key': key, 'file': key[0], 'line': r['line'], 'deps': r['deps'],
                   'auth_level': r['auth_level'], 'prefix_dynamic': not r['literal'], 'unresolved': False}
        else:
            obj = _guess_object(m, verb)
            if obj is None:
                continue
        kind = obj['kind']
        if kind.startswith('fastapi') and (verb in HTTP_VERBS or verb in ('api_route', 'websocket')):
            out += _fastapi(project, m, fn, dec, verb, obj, aliases, mw_auth)
        elif kind.startswith('flask') and (verb in HTTP_VERBS or verb == 'route'):
            out += _flask(project, m, fn, dec, verb, obj, classify_decorators)
        elif kind == 'azfunc_app' and (verb == 'route' or verb in AZ_TRIGGERS):
            out.append(_azure(project, m, fn, dec, verb, obj))
    return out, sorted(project.unparsed)


def _guess_object(m: Module, verb: str) -> dict | None:
    """A route decorator on an object defined somewhere the script can't resolve: classify by the file's imports."""
    base = {'key': None, 'file': m.rel, 'line': None, 'deps': [], 'auth_level': None, 'prefix_dynamic': False,
            'unresolved': True}
    if m.uses('azure.functions') and (verb == 'route' or verb in AZ_TRIGGERS):
        return {**base, 'kind': 'azfunc_app'}
    if m.uses('fastapi') and (verb in HTTP_VERBS or verb in ('api_route', 'websocket')):
        return {**base, 'kind': 'fastapi_router'}
    if m.uses('flask') and (verb in HTTP_VERBS or verb == 'route'):
        return {**base, 'kind': 'flask_bp'}
    return None


def _route_path(dec: ast.Call, consts: dict, *kw) -> str | None:
    node = dec.args[0] if dec.args else _kw(dec, *kw)
    if node is None:
        return None
    s, _ = _str(node, consts)
    return s if s is not None else '<dynamic>'


def _full(prefix: str, path: str) -> str:
    """Prefix + route path, keeping a trailing slash the route declares."""
    full = join(prefix, path) if (prefix or path.strip('/')) else '/'
    if path.endswith('/') and len(path) > 1:
        full += '/'
    return full


def _chains(project: Project, obj: dict) -> list[dict]:
    if obj['unresolved']:
        return [{'prefix': '', 'status': 'unmounted', 'deps': [], 'reached': False, 'via': None, 'chain': []}]
    return project.mount_chains(obj['key'])


def _fastapi(project, m, fn, dec, verb, obj, aliases, mw_auth) -> list[dict]:
    path = _route_path(dec, m.consts, 'path')
    if path is None:
        return []
    meths = _methods(_kw(dec, 'methods'))
    method = 'WEBSOCKET' if verb == 'websocket' else (','.join(meths) or 'GET') if verb == 'api_route' else verb.upper()
    own = dep_list(_kw(dec, 'dependencies'), m.rel) + param_deps(fn, m.rel, aliases)
    out = []
    for mt in _chains(project, obj):
        full = _full(mt['prefix'], path)
        layers = [('route', own), ('app' if obj['kind'].endswith('app') else 'router', obj['deps']),
                  ('include_router/app', mt['deps'])]
        found = [(where, d, dep_label(d)) for where, ds in layers for d in ds]
        labelled = [x for x in found if x[2]]
        if labelled:
            order = ['admin', 'authenticated', 'api_key']
            auth = min((x[2] for x in labelled), key=order.index)
            evidence = '; '.join(f"{where} dependency {d['name']} ({d['file']}:{d['line']})" for where, d, _ in labelled[:3])
        elif obj['unresolved'] or not mt['reached']:
            auth, evidence = 'unknown', ('router is not included anywhere recon could resolve, so dependencies added by '
                                         'include_router() or the app are unknown')
        elif mw_auth:
            auth, evidence = 'unknown', f"no auth dependency, but app middleware may check auth ({', '.join(mw_auth[:2])})"
        else:
            auth, evidence = 'public', 'no auth dependency on the route, its router, include_router() or the app'
        if obj['prefix_dynamic']:
            evidence += '; the router prefix is computed, so the path is partial'
        elif mt['status'] == 'dynamic' or path == '<dynamic>':
            evidence += '; part of the path or a prefix above the router is computed, so the path is partial'
        out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': 'fastapi', 'method': method,
                    'path': full, 'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence,
                    'router': f"{obj['file']}:{obj['line']}" if not obj['unresolved'] else None,
                    'mounted_via': mt['via']})
    return out


def _flask(project, m, fn, dec, verb, obj, classify_decorators) -> list[dict]:
    path = _route_path(dec, m.consts, 'rule')
    if path is None:
        return []
    method = (','.join(_methods(_kw(dec, 'methods'))) or 'GET') if verb == 'route' else verb.upper()
    others = '\n'.join('@' + _unparse(d) for d in fn.decorator_list if d is not dec)
    auth, evidence = classify_decorators(others) if classify_decorators else ('unknown', None)
    out = []
    for mt in _chains(project, obj):
        full = _full(mt['prefix'], path)
        out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': 'flask', 'method': method,
                    'path': full, 'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence})
    return out


def _azure(project, m, fn, dec, verb, obj) -> dict:
    if verb == 'route':
        rnode = _kw(dec, 'route')
        route = _str(rnode, m.consts)[0] if rnode is not None else fn.name
        lvl = _kw(dec, 'auth_level')
        lvl_str = _str(lvl)[0] if lvl is not None else None
        level = (_name(lvl) or lvl_str or '').upper() if lvl is not None else (obj.get('auth_level') or 'FUNCTION').upper()
        auth, evidence = AZ_LEVEL.get(level, ('unknown', f'auth_level {level}'))
        if lvl is None:
            evidence += ' (the default; set on the app or left unset)'
        path = join(project.host_prefix(m.rel), route or '')
        return {'kind': 'webhook' if 'webhook' in path.lower() else 'http_route', 'framework': 'azure_functions',
                'method': ','.join(_methods(_kw(dec, 'methods'))) or 'ANY', 'path': path, 'handler': f'{m.rel}:{dec.lineno}',
                'function': fn.name, 'auth': auth, 'auth_evidence': evidence}
    kind, label, key = AZ_TRIGGERS[verb]
    what = _str(_kw(dec, key), m.consts)[0] if key and _kw(dec, key) is not None else None
    conn = _str(_kw(dec, 'connection'), m.consts)[0] if _kw(dec, 'connection') is not None else None
    if verb == 'timer_trigger':
        auth, evidence = 'authenticated', f"timer: started by the platform on schedule {what or '?'}; no caller input"
    else:
        auth, evidence = 'unknown', (f"whoever can write to {what or 'the source'}" + (f" (connection setting {conn})" if conn else '') +
                                     ': treat each message or blob as untrusted input')
    return {'kind': kind, 'framework': 'azure_functions', 'method': label, 'path': what or fn.name,
            'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence}


def main() -> int:
    args = [a for a in sys.argv[1:] if a != '--entries']
    if len(args) != 1 or not Path(args[0]).is_dir():
        print(__doc__)
        return 2
    root = Path(args[0])
    files, hosts = {}, {}
    for p in root.rglob('*'):
        if any(part in ('.git', 'node_modules', '.venv', 'venv', '__pycache__', 'site-packages') for part in p.parts):
            continue
        if p.suffix == '.py':
            files[p.relative_to(root).as_posix()] = p.read_text(encoding='utf-8', errors='replace')
        elif p.name == 'host.json':
            hosts[p.relative_to(root).as_posix()] = p.read_text(encoding='utf-8', errors='replace')
    if '--entries' in sys.argv:
        eps, unparsed = entries(files, root)
        print(json.dumps({'entries': eps, 'unparsed': unparsed}, indent=2))
    else:
        print(json.dumps(scan_python(files, hosts), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
