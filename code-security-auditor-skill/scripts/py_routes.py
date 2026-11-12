#!/usr/bin/env python3
"""Python web entry points read from the syntax tree: FastAPI, Flask and Azure Functions.

Imported by recon.py. Parsing with `ast` instead of matching lines means decorators that span
several lines (Black's style for long ones) are found, and decorators quoted in docstrings or
strings are not. Routers are followed across files:

  FastAPI   APIRouter(prefix=..., dependencies=[...]) (and subclasses such as a project's own
            APIRouter wrapper), include_router(router, prefix=..., dependencies=[...]) on an app or
            another router, FastAPI(dependencies=[...]), route-level dependencies=[...], and
            Depends()/Security() parameters, including Annotated aliases. A route's auth is the
            union of all of these; a dependency counts as auth when its name says so
            (get_current_user, require_admin, check_permission, verify_token, an API key header...).
  Flask     Blueprint(url_prefix=...), register_blueprint(bp, url_prefix=...), route decorators and
            the auth decorators stacked on the view.
  Azure     Functions (Python v2 model): @app.route (auth_level, methods, host.json routePrefix) and
            blob, queue, Service Bus, Event Hub, Event Grid, Cosmos DB and timer triggers, on a
            FunctionApp or a functions Blueprint.

A route whose router is never included anywhere the script can resolve keeps auth 'unknown'.
Everything is still a first pass for the agent to confirm by reading the code.
"""
from __future__ import annotations
import ast, json, re
from pathlib import Path

HTTP = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace')
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
DEP_ADMIN = re.compile(r'(?i)admin|superuser|staff')
DEP_KEY = re.compile(r'(?i)api_?key|apikey|x[-_]api')
DEP_AUTH = re.compile(r'(?i)auth|user|token|jwt|login|permission|role|scope|principal|claim|verif|guard|secur|oauth|'
                      r'bearer|credential|signed_?in|require|current_account|identity')
MIDDLEWARE_AUTH = re.compile(r'(?i)auth|jwt|session|token|login|bearer')

def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ''

def _str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

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

def module_name(rel: str) -> str:
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)

class Module:
    def __init__(self, rel: str, tree: ast.Module):
        self.rel, self.tree = rel, tree
        self.name = module_name(rel)
        self.package = self.name if rel.endswith('__init__.py') else self.name.rpartition('.')[0]
        self.imports: dict[str, tuple[str, str | None]] = {}  # alias -> (module, attribute or None)
        self.libs: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        self.imports[a.asname] = (a.name, None)
                    else:
                        self.imports[a.name.split('.')[0]] = (a.name.split('.')[0], None)
                    self.libs.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ''
                if n.level:
                    pkg = self.package.split('.') if self.package else []
                    pkg = pkg[:len(pkg) - (n.level - 1)] if n.level > 1 else pkg
                    base = '.'.join([*pkg, *([n.module] if n.module else [])])
                else:
                    self.libs.add(base)
                for a in n.names:
                    self.imports[a.asname or a.name] = (base, a.name)
        self.objects: dict[str, dict] = {}

    def uses(self, lib: str) -> bool:
        return any(x == lib or x.startswith(lib + '.') for x in self.libs)

class Index:
    def __init__(self, files: dict[str, str]):
        self.mods: dict[str, Module] = {}
        self.failed: list[str] = []
        for rel, text in files.items():
            try:
                self.mods[rel] = Module(rel, ast.parse(text))
            except (SyntaxError, ValueError):
                self.failed.append(rel)
        self.by_name: dict[str, list[Module]] = {}
        for m in self.mods.values():
            self.by_name.setdefault(m.name, []).append(m)

    def find(self, dotted: str, near: Module) -> Module | None:
        """A module by dotted name; source roots vary (src/, api/), so a suffix match counts."""
        if not dotted:
            return None
        hits = self.by_name.get(dotted) or [m for n, ms in self.by_name.items() if n.endswith('.' + dotted) for m in ms]
        if not hits:
            return None
        return max(hits, key=lambda m: len(_common(m.rel, near.rel)))

def _common(a: str, b: str) -> str:
    out = []
    for x, y in zip(a.split('/'), b.split('/')):
        if x != y:
            break
        out.append(x)
    return '/'.join(out)

# ---------------------------------------------------------------- objects (apps, routers, blueprints)
def router_classes(idx: Index) -> set[str]:
    out = {'APIRouter'}
    for m in idx.mods.values():
        for n in ast.walk(m.tree):
            if isinstance(n, ast.ClassDef) and any('Router' in _name(b) for b in n.bases):
                out.add(n.name)
    return out

def classify_ctor(m: Module, call: ast.Call, routers: set[str]) -> str | None:
    name = _name(call.func)
    azure = m.uses('azure.functions')
    if name == 'FastAPI':
        return 'fastapi_app'
    if name in routers or name.endswith('APIRouter'):
        return 'fastapi_router'
    if name == 'Flask':
        return 'flask_app'
    if name in ('FunctionApp', 'AsgiFunctionApp', 'WsgiFunctionApp'):
        return 'azfunc_app'
    if name == 'Blueprint':
        if azure and (isinstance(call.func, ast.Attribute) or m.imports.get('Blueprint', ('',))[0].startswith('azure')):
            return 'azfunc_app'
        return 'flask_bp' if m.uses('flask') else ('azfunc_app' if azure else None)
    return None

def dep_list(node, m: Module) -> list[dict]:
    """[Depends(x), Security(y)] -> [{name, security, line, file}]."""
    out = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for el in node.elts:
            d = dep_call(el, m)
            if d:
                out.append(d)
    return out

def dep_call(node, m: Module) -> dict | None:
    if isinstance(node, ast.Call) and _name(node.func) in ('Depends', 'Security') and (node.args or node.keywords):
        target = node.args[0] if node.args else _kw(node, 'dependency')
        return {'name': _unparse(target) if target is not None else '', 'security': _name(node.func) == 'Security',
                'line': node.lineno, 'file': m.rel}
    return None

def collect_objects(idx: Index) -> None:
    routers = router_classes(idx)
    for m in idx.mods.values():
        for n in ast.walk(m.tree):
            target = value = None
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                target, value = n.targets[0].id, n.value
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
                target, value = n.target.id, n.value
            if not target or not isinstance(value, ast.Call):
                continue
            kind = classify_ctor(m, value, routers)
            if not kind:
                continue
            prefix_node = _kw(value, 'prefix') if kind.startswith('fastapi') else _kw(value, 'url_prefix') if kind.startswith('flask') else None
            auth_level = _kw(value, 'http_auth_level')
            m.objects[target] = {'id': f'{m.rel}::{target}', 'kind': kind, 'file': m.rel, 'line': n.lineno, 'var': target,
                                 'prefix': _str(prefix_node) or '' if prefix_node is not None else '',
                                 'prefix_dynamic': prefix_node is not None and _str(prefix_node) is None,
                                 'deps': dep_list(_kw(value, 'dependencies'), m),
                                 'auth_level': _name(auth_level) if auth_level is not None else None}

def resolve(idx: Index, m: Module, expr, depth: int = 0) -> dict | None:
    if depth > 4 or expr is None:
        return None
    if isinstance(expr, ast.Name):
        if expr.id in m.objects:
            return m.objects[expr.id]
        imp = m.imports.get(expr.id)
        if imp and imp[1]:
            tm = idx.find(imp[0], m)
            if tm:
                return resolve(idx, tm, ast.Name(id=imp[1]), depth + 1)
        return None
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        imp = m.imports.get(expr.value.id)
        if imp:
            tm = idx.find(f'{imp[0]}.{imp[1]}' if imp[1] else imp[0], m)
            if tm:
                return resolve(idx, tm, ast.Name(id=expr.attr), depth + 1)
    return None

def annotated_aliases(idx: Index) -> dict[str, list[dict]]:
    """CurrentUser = Annotated[User, Depends(get_current_user)] -> {'CurrentUser': [dep]} (by name, repo-wide)."""
    out: dict[str, list[dict]] = {}
    for m in idx.mods.values():
        for n in m.tree.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(tgt, ast.Name) and isinstance(n.value, ast.Subscript) and _name(n.value.value) == 'Annotated':
                    deps = [d for d in (dep_call(x, m) for x in ast.walk(n.value)) if d]
                    if deps:
                        out[tgt.id] = deps
    return out

def param_deps(fn, m: Module, aliases: dict) -> list[dict]:
    out = []
    a = fn.args
    for d in list(a.defaults) + [x for x in a.kw_defaults if x is not None]:
        dc = dep_call(d, m)
        if dc:
            out.append(dc)
    for arg in a.args + a.kwonlyargs:
        ann = arg.annotation
        if ann is None:
            continue
        if isinstance(ann, ast.Subscript) and _name(ann.value) == 'Annotated':
            out += [dc for dc in (dep_call(x, m) for x in ast.walk(ann)) if dc]
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

# ---------------------------------------------------------------- includes and mounts
def collect_includes(idx: Index) -> list[dict]:
    out = []
    for m in idx.mods.values():
        for n in ast.walk(m.tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ('include_router', 'register_blueprint')
                    and n.args):
                continue
            parent, child = resolve(idx, m, n.func.value), resolve(idx, m, n.args[0])
            if not child:
                continue
            fastapi = n.func.attr == 'include_router'
            p = _kw(n, 'prefix') if fastapi else _kw(n, 'url_prefix')
            out.append({'parent': parent['id'] if parent else None, 'child': child['id'], 'line': n.lineno, 'file': m.rel,
                        'prefix': (_str(p) or '') if p is not None else None, 'replace_prefix': not fastapi and p is not None,
                        'deps': dep_list(_kw(n, 'dependencies'), m)})
    return out

def join(prefix: str, path: str) -> str:
    if path in ('', '/'):
        return prefix or '/'
    return prefix.rstrip('/') + '/' + path.lstrip('/')

def mounts(obj: dict, objs: dict, includes: list[dict], seen: frozenset = frozenset()) -> list[dict]:
    """Every way an object is reachable: [{base, deps, resolved, replaced, via}]. base is the path before the
    object's own prefix; deps are those applied above it (parents' own, include_router() and the app's);
    replaced is a register_blueprint(url_prefix=...) that replaces a blueprint's own prefix."""
    if obj['kind'] in ('fastapi_app', 'flask_app', 'azfunc_app'):
        return [{'base': '', 'deps': [], 'resolved': True, 'replaced': None, 'via': None}]
    incs = [i for i in includes if i['child'] == obj['id']]
    if not incs or obj['id'] in seen:
        return [{'base': '', 'deps': [], 'resolved': False, 'replaced': None, 'via': None}]
    out = []
    for inc in incs:
        parent = objs.get(inc['parent']) if inc['parent'] else None
        if parent is None:
            out.append({'base': '' if inc['replace_prefix'] else inc['prefix'] or '', 'deps': inc['deps'], 'resolved': False,
                        'replaced': inc['prefix'] if inc['replace_prefix'] else None, 'via': f"{inc['file']}:{inc['line']}"})
            continue
        for up in mounts(parent, objs, includes, seen | {obj['id']}):
            parent_prefix = up['replaced'] if up['replaced'] is not None else parent['prefix']
            base = join(up['base'], parent_prefix) if parent_prefix else up['base']
            if not inc['replace_prefix'] and inc['prefix']:
                base = join(base, inc['prefix'])
            out.append({'base': base, 'deps': up['deps'] + parent['deps'] + inc['deps'], 'resolved': up['resolved'],
                        'replaced': inc['prefix'] if inc['replace_prefix'] else None, 'via': f"{inc['file']}:{inc['line']}"})
    return out

# ---------------------------------------------------------------- host.json for Azure Functions
def route_prefix(root: Path | None, rel: str) -> str:
    if root is None:
        return 'api'
    d = (root / rel).parent
    while True:
        h = d / 'host.json'
        if h.is_file():
            try:
                cfg = json.loads(h.read_text(encoding='utf-8-sig'))
                return ((cfg.get('extensions') or {}).get('http') or {}).get('routePrefix', 'api')
            except (OSError, ValueError):
                return 'api'
        if d == root or root not in d.parents:
            return 'api'
        d = d.parent

AZ_LEVEL = {'ANONYMOUS': ('public', 'auth_level ANONYMOUS: no function key needed'),
            'FUNCTION': ('api_key', 'auth_level FUNCTION: a function key (x-functions-key header or ?code=)'),
            'ADMIN': ('api_key', 'auth_level ADMIN: the host master key')}

# ---------------------------------------------------------------- entry points
def entries(files: dict[str, str], root: Path | None = None, classify_decorators=None) -> tuple[list[dict], list[str]]:
    """Entry points from Python files {rel: text}; returns (entries, files that did not parse).

    classify_decorators(text) -> (auth, evidence) labels a Flask view's stacked decorators."""
    idx = Index(files)
    collect_objects(idx)
    objs = {o['id']: o for m in idx.mods.values() for o in m.objects.values()}
    includes = collect_includes(idx)
    aliases = annotated_aliases(idx)
    mw_auth = [f"{m.rel}:{n.lineno}" for m in idx.mods.values() for n in ast.walk(m.tree)
               if (isinstance(n, ast.Call) and _name(n.func) == 'add_middleware' and n.args and MIDDLEWARE_AUTH.search(_unparse(n.args[0])))
               or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                   isinstance(d, ast.Call) and _name(d.func) == 'middleware' for d in n.decorator_list))]
    out = []
    for m in idx.mods.values():
        for fn in ast.walk(m.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                verb = dec.func.attr
                obj = resolve(idx, m, dec.func.value)
                if obj is None:
                    obj = _guess_object(m, dec, verb)
                    if obj is None:
                        continue
                kind = obj['kind']
                if kind.startswith('fastapi') and (verb in HTTP or verb in ('api_route', 'websocket')):
                    out += _fastapi(m, fn, dec, verb, obj, objs, includes, aliases, mw_auth)
                elif kind.startswith('flask') and (verb in HTTP or verb == 'route'):
                    out += _flask(m, fn, dec, verb, obj, objs, includes, classify_decorators)
                elif kind == 'azfunc_app' and (verb == 'route' or verb in AZ_TRIGGERS):
                    out.append(_azure(m, fn, dec, verb, obj, root))
    return out, idx.failed

def _guess_object(m: Module, dec: ast.Call, verb: str) -> dict | None:
    """A route decorator on an object defined somewhere recon can't resolve: classify by the file's imports."""
    base = {'id': f'{m.rel}::?{_unparse(dec.func.value)}', 'file': m.rel, 'line': dec.lineno, 'var': _unparse(dec.func.value),
            'prefix': '', 'prefix_dynamic': False, 'deps': [], 'auth_level': None, 'unresolved': True}
    if m.uses('azure.functions') and (verb == 'route' or verb in AZ_TRIGGERS):
        return {**base, 'kind': 'azfunc_app'}
    if m.uses('fastapi') and (verb in HTTP or verb in ('api_route', 'websocket')):
        return {**base, 'kind': 'fastapi_router'}
    if m.uses('flask') and (verb in HTTP or verb == 'route'):
        return {**base, 'kind': 'flask_bp'}
    return None

def _path_arg(dec: ast.Call, *kw) -> str | None:
    node = dec.args[0] if dec.args else _kw(dec, *kw)
    if node is None:
        return None
    s = _str(node)
    return s if s is not None else '<dynamic>'

def _methods(dec: ast.Call, default: str) -> str:
    node = _kw(dec, 'methods')
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [(_str(e) or _name(e)).upper() for e in node.elts]
        return ','.join(v for v in vals if v) or default
    return default

def _fastapi(m, fn, dec, verb, obj, objs, includes, aliases, mw_auth) -> list[dict]:
    path = _path_arg(dec, 'path')
    if path is None:
        return []
    method = 'WEBSOCKET' if verb == 'websocket' else _methods(dec, 'GET') if verb == 'api_route' else verb.upper()
    own = dep_list(_kw(dec, 'dependencies'), m) + param_deps(fn, m, aliases)
    out = []
    for mt in mounts(obj, objs, includes):
        full = join(join(mt['base'], obj['prefix']), path)
        layers = [('route', own), ('app' if obj['kind'].endswith('app') else 'router', obj['deps']), ('include_router/app', mt['deps'])]
        auth, evidence = None, None
        found = [(where, d, dep_label(d)) for where, ds in layers for d in ds]
        labelled = [x for x in found if x[2]]
        if labelled:
            order = ['admin', 'authenticated', 'api_key']
            auth = min((x[2] for x in labelled), key=order.index)
            evidence = '; '.join(f"{where} dependency {d['name']} ({d['file']}:{d['line']})" for where, d, _ in labelled[:3])
        elif obj.get('unresolved') or not mt['resolved']:
            auth, evidence = 'unknown', ('router is not included anywhere recon could resolve, so dependencies added by '
                                         'include_router() or the app are unknown')
        elif mw_auth:
            auth, evidence = 'unknown', f"no auth dependency, but app middleware may check auth ({', '.join(mw_auth[:2])})"
        else:
            auth, evidence = 'public', 'no auth dependency on the route, its router, include_router() or the app'
        if obj.get('prefix_dynamic'):
            evidence += '; the router prefix is computed, so the path is partial'
        out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': 'fastapi', 'method': method,
                    'path': full, 'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence,
                    'router': f"{obj['file']}:{obj['line']}" if not obj.get('unresolved') else None,
                    'mounted_via': mt['via']})
    return out

def _flask(m, fn, dec, verb, obj, objs, includes, classify_decorators) -> list[dict]:
    path = _path_arg(dec, 'rule')
    if path is None:
        return []
    method = _methods(dec, 'GET') if verb == 'route' else verb.upper()
    others = '\n'.join('@' + _unparse(d) for d in fn.decorator_list if d is not dec)
    auth, evidence = classify_decorators(others) if classify_decorators else ('unknown', None)
    out = []
    for mt in mounts(obj, objs, includes):
        prefix = mt['replaced'] if mt['replaced'] is not None else obj['prefix']
        full = join(join(mt['base'], prefix), path)
        out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': 'flask', 'method': method,
                    'path': full, 'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence})
    return out

def _azure(m, fn, dec, verb, obj, root) -> dict:
    if verb == 'route':
        route = _str(_kw(dec, 'route')) if _kw(dec, 'route') is not None else fn.name
        prefix = route_prefix(root, m.rel)
        lvl = _kw(dec, 'auth_level')
        level = (_name(lvl) or _str(lvl) or '').upper() if lvl is not None else (obj.get('auth_level') or 'FUNCTION').upper()
        auth, evidence = AZ_LEVEL.get(level, ('unknown', f'auth_level {level}'))
        if lvl is None:
            evidence += ' (the default; set on the app or left unset)'
        path = '/' + '/'.join(x.strip('/') for x in (prefix, route or '') if x and x.strip('/'))
        return {'kind': 'webhook' if 'webhook' in path.lower() else 'http_route', 'framework': 'azure_functions',
                'method': _methods(dec, 'ANY'), 'path': path, 'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name,
                'auth': auth, 'auth_evidence': evidence}
    kind, label, key = AZ_TRIGGERS[verb]
    what = _str(_kw(dec, key)) if key and _kw(dec, key) is not None else None
    conn = _str(_kw(dec, 'connection'))
    if verb == 'timer_trigger':
        auth, evidence = 'authenticated', f"timer: started by the platform on schedule {what or '?'}; no caller input"
    else:
        auth, evidence = 'unknown', (f"whoever can write to {what or 'the source'}" + (f" (connection setting {conn})" if conn else '') +
                                     ': treat each message or blob as untrusted input')
    return {'kind': kind, 'framework': 'azure_functions', 'method': label, 'path': what or fn.name,
            'handler': f'{m.rel}:{dec.lineno}', 'function': fn.name, 'auth': auth, 'auth_evidence': evidence}
