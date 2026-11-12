#!/usr/bin/env python3
"""Python routes, function triggers and command-line entry points, read with `ast` (nothing is imported or run).

    from py_routes import scan_python
    result = scan_python({'app/main.py': text, ...})      # relative path -> source text
    python py_routes.py <dir>                             # print what it finds, as JSON

Because the source is parsed, decorators inside strings and docstrings (usage examples) never count.

HTTP routes (result['routes']):
  fastapi   `@router.get('/x')`, `@router.api_route('/x', methods=[...])` on an `APIRouter`, a subclass of it
            or any class whose name ends in `Router`, or on `FastAPI()`. The full path is
            include_router(prefix=...) + APIRouter(prefix=...) + the decorator path, following
            `app.include_router(module.router, prefix=...)` across modules, nested routers included.
  flask     `@bp.route('/x', methods=[...])` (default GET) and `@bp.get('/x')`; the full path is the
            register_blueprint(url_prefix=...) value, or else Blueprint(url_prefix=...), + the rule.
  azure-functions  `@app.route(route='items/{id}', methods=[...])` on `func.FunctionApp()`; the path is
            host.json's routePrefix (default `api`) + route (default: the function name).
Each route records `mount`: `resolved` (every prefix was a literal), `unmounted` (a router that no
include_router/register_blueprint call was found for: its own prefix only) or `dynamic` (a prefix or the
included router is computed, e.g. a loop over routers). A router included twice gives two routes.

Triggers (result['triggers']): Azure Functions non-HTTP triggers (`@app.blob_trigger(path=...)`,
`timer_trigger(schedule=...)`, `queue_trigger(queue_name=...)`, Service Bus, Event Hub, Event Grid, Cosmos DB).

Command lines (result['clis']): argparse sub-commands (`sub.add_parser('name')`), click and typer
commands (`@cli.command()`), with the program name from `ArgumentParser(prog=...)` or the file name.

Files that don't parse are listed in result['unparsed'] so a caller can fall back to text matching.
"""
from __future__ import annotations
import ast, json, re, sys
from pathlib import Path, PurePosixPath

HTTP_VERBS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace')
FASTAPI_CTORS = {'FastAPI', 'APIRouter', 'Starlette'}
FLASK_CTORS = {'Flask', 'Blueprint'}
AZURE_CTORS = {'FunctionApp', 'AsgiFunctionApp', 'WsgiFunctionApp'}
TRIGGER_DETAIL = ('path', 'schedule', 'queue_name', 'topic_name', 'event_hub_name', 'container_name',
                  'subscription_name', 'connection')
PREFILTER = ('@', 'include_router', 'register_blueprint', 'Router', 'Blueprint', 'FastAPI', 'Flask',
             'add_parser', 'FunctionApp', 'add_subparsers')


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


def _methods(node) -> list[str]:
    out = []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.append(el.value.upper())
            elif isinstance(el, ast.Attribute):          # func.HttpMethod.GET
                out.append(el.attr.upper())
    return out


def join(*parts: str) -> str:
    segs = [p.strip('/') for p in parts if p and p.strip('/')]
    return '/' + '/'.join(segs)


def module_names(rel: str) -> list[str]:
    p = PurePosixPath(rel)
    parts = list(p.with_suffix('').parts)
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return ['.'.join(parts[i:]) for i in range(len(parts)) if parts[i:]]


class Module:
    def __init__(self, rel: str, tree: ast.AST):
        self.rel = rel
        self.tree = tree
        self.imports: dict[str, tuple[str, str | None]] = {}   # local name -> (dotted module, attribute or None)
        self.routers: dict[str, dict] = {}                      # var -> {ctor, prefix, literal, line}
        self.consts: dict[str, str] = {}
        self.classes: dict[str, list[str]] = {}                 # class name -> base names
        self.text_imports: set[str] = set()


class Project:
    def __init__(self, files: dict[str, str], host_prefixes: dict[str, str] | None = None):
        self.modules: dict[str, Module] = {}
        self.unparsed: list[str] = []
        self.by_name: dict[str, list[str]] = {}
        self.host_prefixes = host_prefixes or {}
        for rel, text in files.items():
            if not any(tok in text for tok in PREFILTER):
                continue
            try:
                tree = ast.parse(text, filename=rel)
            except (SyntaxError, ValueError):
                self.unparsed.append(rel)
                continue
            self.modules[rel] = Module(rel, tree)
            for n in module_names(rel):
                self.by_name.setdefault(n, []).append(rel)
        for m in self.modules.values():
            self._index(m)
        self.router_ctors = self._router_classes()

    # ---- indexing ----
    def _package(self, rel: str) -> list[str]:
        parts = list(PurePosixPath(rel).parts[:-1])
        return parts

    def _index(self, m: Module):
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m.imports[a.asname or a.name.split('.')[0]] = (a.name if a.asname else a.name.split('.')[0], None)
                    m.text_imports.add(a.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    pkg = self._package(m.rel)
                    pkg = pkg[:len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                    base = '.'.join(pkg + ([base] if base else []))
                else:
                    m.text_imports.add(base.split('.')[0])
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
                    if fw is None and any(b.endswith('Router') for b in bases):
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
            if isinstance(call.func, ast.Attribute) and _name(call.func.value) in ('func', 'functions') \
                    or 'azure' in m.text_imports and 'flask' not in m.text_imports:
                return 'azure-functions'
            return 'flask'
        if name in self.router_ctors:
            return self.router_ctors[name]
        if name.endswith('Router'):
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
                    for t in targets:
                        if isinstance(t, ast.Name):
                            m.routers[t.id] = {'framework': fw, 'prefix': prefix or '', 'literal': lit and prefix is not None,
                                               'line': node.lineno, 'ctor': _name(node.value.func),
                                               'app': _name(node.value.func) in ('FastAPI', 'Flask', 'Starlette')
                                                      or fw == 'azure-functions'}

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
                                                  'text': ast.unparse(node)[:160]})
                    continue
                self.mounts.setdefault(child, []).append({'parent': parent, 'prefix': prefix, 'literal': lit,
                                                          'kind': node.func.attr, 'file': m.rel, 'line': node.lineno})

    def prefixes(self, key, seen=()) -> list[tuple[str, str]]:
        """[(full prefix, mount status)] for a router: every chain of include calls up to an app."""
        m = self.modules[key[0]]
        r = m.routers[key[1]]
        own = r['prefix'] or ''
        status_own = 'resolved' if r['literal'] else 'dynamic'
        edges = self.mounts.get(key) or []
        if not edges:
            if r['app']:
                return [(own, status_own)]
            return [(own, 'unmounted' if status_own == 'resolved' else 'dynamic')]
        out = []
        for e in edges:
            if r['framework'] == 'flask' and e['prefix'] is not None:
                local = e['prefix']                       # register_blueprint(url_prefix=) replaces the blueprint's own
            else:
                local = join(e['prefix'] or '', own) if (e['prefix'] or own) else ''
            status = status_own if e['literal'] else 'dynamic'
            if e['parent'] is None or e['parent'] in seen:
                out.append((local, 'dynamic' if e['parent'] is None else status))
                continue
            for pp, pstatus in self.prefixes(e['parent'], seen + (key,)):
                worst = 'dynamic' if 'dynamic' in (pstatus, status) else 'unmounted' if 'unmounted' in (pstatus, status) else 'resolved'
                out.append((join(pp, local) if (pp or local) else '', worst))
        return out

    def host_prefix(self, rel: str) -> str:
        d = PurePosixPath(rel).parent
        while True:
            key = d.as_posix()
            if key in self.host_prefixes:
                return self.host_prefixes[key]
            if key in ('.', '', '/'):
                return 'api'
            d = d.parent

    # ---- routes, triggers and command lines ----
    def scan(self) -> dict:
        self.collect_routers()
        self.collect_mounts()
        routes, triggers, clis = [], [], []
        for m in self.modules.values():
            fw_imports = m.text_imports
            for node in ast.walk(m.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                fn_name = node.name
                azure_name = None
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'function_name':
                        v = _kw(dec, 'name') or (dec.args[0] if dec.args else None)
                        azure_name, _ = _str(v, m.consts) if v is not None else (None, False)
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                        continue
                    attr = dec.func.attr
                    key = self.resolve(m, dec.func.value)
                    router = self.modules[key[0]].routers[key[1]] if key else None
                    fw = router['framework'] if router else None
                    if fw is None:
                        if 'flask' in fw_imports:
                            fw = 'flask'
                        elif 'fastapi' in fw_imports:
                            fw = 'fastapi'
                        elif 'azure' in fw_imports:
                            fw = 'azure-functions'
                    if fw is None:
                        continue
                    line = dec.lineno
                    if fw == 'azure-functions':
                        if attr == 'route':
                            rnode = _kw(dec, 'route')
                            route, lit = _str(rnode, m.consts) if rnode is not None else (azure_name or fn_name, True)
                            meths = _methods(_kw(dec, 'methods')) or ['ANY']
                            for meth in meths:
                                routes.append({'method': meth, 'path': join(self.host_prefix(m.rel), route or ''),
                                               'file': m.rel, 'line': line, 'framework': fw, 'function': fn_name,
                                               'mount': 'resolved' if lit else 'dynamic'})
                        elif attr.endswith('_trigger') and attr != 'http_trigger':
                            detail = {}
                            for k in TRIGGER_DETAIL:
                                v = _kw(dec, k)
                                if v is not None:
                                    s, _ = _str(v, m.consts)
                                    if s is not None:
                                        detail[k] = s
                            triggers.append({'trigger': attr[:-len('_trigger')], 'function': azure_name or fn_name,
                                             'file': m.rel, 'line': line, 'framework': fw, **detail})
                        continue
                    if attr in HTTP_VERBS or attr in ('api_route', 'route'):
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
                        else:
                            meths = _methods(_kw(dec, 'methods')) or ['GET']
                        mounts = self.prefixes(key) if key else [('', 'unmounted')]
                        for prefix, status in mounts:
                            full = join(prefix, path) if (prefix or path.strip('/')) else '/'
                            if path.endswith('/') and len(path) > 1:
                                full += '/'
                            for meth in meths:
                                routes.append({'method': meth, 'path': full, 'file': m.rel, 'line': line,
                                               'framework': fw, 'function': fn_name,
                                               'mount': status if lit else 'dynamic',
                                               **({'router_prefix': prefix} if prefix else {})})
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


def host_route_prefixes(files: dict[str, str]) -> dict[str, str]:
    """Directory -> Azure Functions HTTP routePrefix from each host.json (default `api`)."""
    out = {}
    for rel, text in files.items():
        if PurePosixPath(rel).name != 'host.json':
            continue
        try:
            data = json.loads(text)
            prefix = ((data.get('extensions') or {}).get('http') or {}).get('routePrefix', 'api')
        except (ValueError, AttributeError):
            prefix = 'api'
        out[PurePosixPath(rel).parent.as_posix()] = prefix if isinstance(prefix, str) else 'api'
    return out


def scan_python(files: dict[str, str], host_files: dict[str, str] | None = None) -> dict:
    """files: relative .py path -> text. host_files: relative host.json path -> text (Azure Functions)."""
    return Project(files, host_route_prefixes(host_files or {})).scan()


def blank_strings(text: str) -> str:
    """Replace triple-quoted strings with blank lines (keeps line numbers); a fallback for files ast can't parse."""
    return re.sub(r'("""|\'\'\')(.*?)\1', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)


def main() -> int:
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    files, hosts = {}, {}
    for p in root.rglob('*'):
        if any(part in ('.git', 'node_modules', '.venv', 'venv', '__pycache__', 'site-packages') for part in p.parts):
            continue
        if p.suffix == '.py':
            files[p.relative_to(root).as_posix()] = p.read_text(encoding='utf-8', errors='replace')
        elif p.name == 'host.json':
            hosts[p.relative_to(root).as_posix()] = p.read_text(encoding='utf-8', errors='replace')
    print(json.dumps(scan_python(files, hosts), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
