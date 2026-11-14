#!/usr/bin/env python3
"""Compare the contract's operations with the route handlers in the code (static; nothing is run).

    python handler_map.py <repo> --contract run/contract.json --out run/handlers.json
        [--framework auto|nextjs|express|flask|fastapi ...] [--contract-prefix /api] [--exclude-dir DIR ...] [--only /api/]
    python handler_map.py <repo> --out run/handlers.json --skeleton run/openapi.derived.json [--only /api/]   # no contract
    python handler_map.py --self-test

Without --contract the handlers are listed and nothing is compared. --skeleton writes a derived OpenAPI
3.1 contract (JSON) from them, marked `x-contract-provenance: derived`: paths and methods, path
parameters typed from Flask converters or FastAPI annotations, query parameters from
request.args.get(name, default, type=int) / request.args[name] / getlist (or FastAPI parameters),
JSON or HTML responses (jsonify or a dict/list literal against render_template; redirect is a 302,
send_file a file) and statuses from `return ..., 201`. Response schemas are left open for the agent to
fill in. --only PREFIX (repeatable) keeps handlers under that path, so a site's HTML pages, static
files and sitemaps are not reported as undocumented API handlers.

Frameworks (heuristic, by reading source text):
  nextjs   App Router `route.(ts|js|tsx|jsx|mjs)` under app/ or src/app/: exported GET/POST/... as
           `export (async) function GET`, `export const GET =`, `export { handler as GET }` and
           `export { GET } from '...'`. Path from the folders: (group) and @slot dropped, [id] -> {id},
           [...slug] and [[...slug]] -> {slug}. HEAD is answered when GET exists and OPTIONS always.
           Pages Router `pages/api/**` is one handler for every method (method `*`).
  express  `app.get('/x/:id', ...)`, `router.post(...)` and `.route('/x').get(...)` in JS/TS files of a
           project that depends on express. Mount prefixes from `app.use('/api', router)` are NOT resolved.
  flask    `@app.route('/x/<int:id>', methods=[...])` (default GET) and `@bp.get('/x')`, with the
           blueprint's url_prefix (or the register_blueprint(url_prefix=...) that replaces it).
  fastapi  `@app.get('/x/{id}')`, `@router.post(...)`, `@router.api_route(...)`, with the APIRouter
           prefix (also on subclasses of APIRouter) and every include_router(prefix=...) above it.
  azure-functions  `@app.route(route='x/{id}', methods=[...])` on func.FunctionApp(), under host.json's
           routePrefix (default /api).
Python files are parsed with ast (py_routes.py), so decorators inside docstrings and other strings are
never handlers. A router with no include_router call the reader could follow is reported with mount
`unmounted` (its own prefix only), and a computed prefix with mount `dynamic`.

Unresolved mount prefixes are why --contract-prefix exists (it is prepended to every contract path;
by default the path of the contract's first `servers` URL); --handler-prefix is prepended to every
handler path instead, for a prefix the code adds in a way this reader can't see. Matching ignores
path-parameter names and reports a name difference separately.

Issues (handlers.json `issues`, the report's `handler_check.issues`):
  undocumented_handler     a handler method the contract doesn't list
  missing_handler          a contract operation whose path has no handler at all
  method_not_handled       the path has handlers, but none for the documented method
  parameter_name_mismatch  same route, different path-parameter names
  response_class_mismatch  the contract documents only JSON for the method's 2xx responses, but the
                           handler streams or returns a file, text, HTML or a redirect
                           (StreamingResponse, FileResponse, send_file, ...)

Treat them as review leads, not findings: a handler can be registered in a way this reader can't see
(a wrapper, a generated router, a mount prefix). Directory walks prune node_modules, .git, .next,
dist, build and the like before descending.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from py_routes import scan_python  # noqa: E402

PRUNE = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__',
         '.turbo', '.cache', '.svelte-kit', '.vercel', '.output', 'site-packages', '.tox', '.mypy_cache', '.pytest_cache'}
HTTP = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
JS_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs')
NEXT_ROUTE = re.compile(r'^route\.(ts|js|tsx|jsx|mjs)$')
NEXT_FN = re.compile(r'^\s*export\s+(?:async\s+)?function\s+(' + '|'.join(HTTP) + r')\b', re.M)
NEXT_CONST = re.compile(r'^\s*export\s+(?:const|let|var)\s+(' + '|'.join(HTTP) + r')\b', re.M)
NEXT_BRACE = re.compile(r'^\s*export\s*\{([^}]*)\}', re.M)
EXPRESS = re.compile(r'\b\w+\s*\.\s*(get|post|put|patch|delete|options|head|all)\s*\(\s*([\'"`])(/[^\'"`]*)\2')
EXPRESS_ROUTE = re.compile(r'\.route\s*\(\s*([\'"`])(/[^\'"`]*)\1\s*\)((?:\s*\.\s*(?:get|post|put|patch|delete|options|head|all)\s*\([^)]*\))+)', re.S)
NON_JSON_RESPONSES = {'StreamingResponse', 'FileResponse', 'PlainTextResponse', 'HTMLResponse', 'RedirectResponse',
                      'EventSourceResponse', 'send_file', 'send_from_directory', 'stream_with_context'}
PY_FRAMEWORKS = ('flask', 'fastapi', 'azure-functions')


def walk(root: Path, exclude: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and d not in exclude and not d.startswith('.git'))
        for f in sorted(filenames):
            yield Path(dirpath) / f


def line_of(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def next_path(rel_parts: list[str]) -> str | None:
    segs = []
    for s in rel_parts:
        if s.startswith('(') and s.endswith(')'):
            continue                     # route group
        if s.startswith('@'):
            continue                     # parallel-route slot
        s = re.sub(r'^\(\.{1,3}\)+', '', s)   # intercepting-route marker
        m = re.fullmatch(r'\[\[?\.\.\.(\w+)\]\]?', s)
        if m:
            segs.append('{' + m.group(1) + '}')
            continue
        segs.append(re.sub(r'\[(\w+)\]', r'{\1}', s))
    return '/' + '/'.join(segs)


def scan_next(path: Path, rel: str, text: str) -> list[dict]:
    parts = Path(rel).parts
    if 'app' not in parts:
        return []
    i = len(parts) - 1 - list(reversed(parts)).index('app')
    route = next_path(list(parts[i + 1:-1]))
    out = []
    for rx in (NEXT_FN, NEXT_CONST):
        for m in rx.finditer(text):
            out.append({'method': m.group(1), 'path': route, 'file': rel, 'line': line_of(text, m.start(1)), 'framework': 'nextjs'})
    for m in NEXT_BRACE.finditer(text):
        for item in m.group(1).split(','):
            name = item.strip().split(' as ')[-1].strip()
            if name in HTTP:
                pos = m.start(1) + m.group(1).find(name)
                out.append({'method': name, 'path': route, 'file': rel, 'line': line_of(text, pos), 'framework': 'nextjs'})
    return out


def scan_pages_api(rel: str, text: str) -> list[dict]:
    parts = list(Path(rel).with_suffix('').parts)
    i = parts.index('pages')
    segs = parts[i + 1:]
    if segs and segs[-1] == 'index':
        segs = segs[:-1]
    route = next_path(segs)
    m = re.search(r'export\s+default', text)
    return [{'method': '*', 'path': route, 'file': rel, 'line': line_of(text, m.start()) if m else 1, 'framework': 'nextjs-pages'}]


def express_path(p: str) -> str:
    return re.sub(r':(\w+)\??', r'{\1}', p.rstrip('/') or '/')


def scan_express(rel: str, text: str) -> list[dict]:
    out = []
    for m in EXPRESS.finditer(text):
        meth = m.group(1).upper()
        out.append({'method': '*' if meth == 'ALL' else meth, 'path': express_path(m.group(3)), 'file': rel,
                    'line': line_of(text, m.start(1)), 'framework': 'express'})
    for m in EXPRESS_ROUTE.finditer(text):
        for mm in re.finditer(r'\.\s*(get|post|put|patch|delete|options|head|all)\s*\(', m.group(3)):
            meth = mm.group(1).upper()
            out.append({'method': '*' if meth == 'ALL' else meth, 'path': express_path(m.group(2)), 'file': rel,
                        'line': line_of(text, m.start(3) + mm.start(1)), 'framework': 'express'})
    return out


def flask_path(p: str) -> str:
    return re.sub(r'<(?:\w+:)?(\w+)>', r'{\1}', p.rstrip('/') or '/')


def python_handlers(py_files: dict[str, str], host_files: dict[str, str], wanted: list[str]) -> list[dict]:
    """Flask, FastAPI and Azure Functions handlers, read with ast, with router and blueprint prefixes applied."""
    out = []
    for r in scan_python(py_files, host_files)['routes']:
        fw = r['framework']
        if not ('auto' in wanted or fw in wanted):
            continue
        if fw == 'flask':
            path = flask_path(r['path'])
        else:
            path = re.sub(r'\{(\w+):[^}]*\}', r'{\1}', r['path']).rstrip('/') or '/'   # {file_path:path} -> {file_path}
        h = {'method': '*' if r['method'] == 'ANY' else r['method'], 'path': path, 'file': r['file'], 'line': r['line'],
             'framework': fw, 'mount': r['mount'], '_raw_path': r['path'], '_function': r.get('function')}
        if r.get('response_classes'):
            h['response_classes'] = r['response_classes']
        out.append(h)
    return out


CONVERTER_TYPE = {'int': {'type': 'integer'}, 'float': {'type': 'number'}, 'uuid': {'type': 'string', 'format': 'uuid'},
                  'path': {'type': 'string'}, 'string': {'type': 'string'}}
PY_TYPE = {'int': 'integer', 'float': 'number', 'bool': 'boolean', 'str': 'string'}
HTML_CALLS = {'render_template', 'render_template_string', 'render', 'TemplateResponse', 'HTMLResponse'}
FILE_CALLS = {'send_file', 'send_from_directory', 'FileResponse', 'StreamingResponse'}
REDIRECT_CALLS = {'redirect', 'RedirectResponse', 'HttpResponseRedirect'}


def _call_name(node) -> str | None:
    import ast
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _func_def(tree, name: str | None, line: int):
    """The handler's def: the function with this name nearest after the decorator line."""
    import ast
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (name is None or node.name == name):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if first <= line <= node.lineno + 1 or (name and best is None):
                best = node
                if first <= line <= node.lineno + 1:
                    break
    return best


def derive_operation(h: dict, source: str | None) -> dict:
    """An OpenAPI operation for one handler, read from its source: path parameters (Flask converters, FastAPI
    annotations), query parameters (request.args.get(name, default, type=int), request.args[name],
    request.args.getlist(name); FastAPI parameters that are not path parameters), and the response kind
    (jsonify or a dict/list literal is JSON; render_template is HTML; redirect is a 302; send_file a file),
    with a status from `return ..., 201`. Response schemas are left open: the skeleton says what exists, not
    what it returns."""
    import ast
    raw = h.get('_raw_path') or h['path']
    names = re.findall(r'\{([^}]+)\}', h['path'])
    conv = {m.group(2): m.group(1) for m in re.finditer(r'<(?:(\w+):)?(\w+)>', raw)}
    path_types = {n: dict(CONVERTER_TYPE.get(conv.get(n) or 'string', {'type': 'string'})) for n in names}
    query: dict[str, dict] = {}
    media, statuses = None, set()
    fn = None
    if source:
        try:
            fn = _func_def(ast.parse(source), h.get('_function'), h['line'])
        except SyntaxError:
            fn = None
    if fn is not None:
        if h['framework'] == 'fastapi':
            args = fn.args.args + fn.args.kwonlyargs
            defaults = [None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults) + list(fn.args.kw_defaults)
            for arg, default in zip(args, defaults):
                ann = arg.annotation.id if isinstance(arg.annotation, ast.Name) else None
                if isinstance(default, ast.Call) and _call_name(default) in ('Depends', 'Security', 'Body', 'File', 'Form', 'Header', 'Cookie'):
                    continue
                if arg.arg in ('self', 'request', 'response', 'session', 'db') or (ann and ann not in PY_TYPE):
                    continue
                if arg.arg in names:
                    if ann in PY_TYPE:
                        path_types[arg.arg] = {'type': PY_TYPE[ann]}
                    continue
                query[arg.arg] = {'schema': {'type': PY_TYPE.get(ann, 'string')}, 'required': default is None}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute) \
                    and node.func.value.attr == 'args' and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                qn = node.args[0].value
                typ = next((PY_TYPE.get(k.value.id, 'string') for k in node.keywords
                            if k.arg == 'type' and isinstance(k.value, ast.Name)), 'string')
                if node.func.attr == 'getlist':
                    query.setdefault(qn, {'schema': {'type': 'array', 'items': {'type': typ}}, 'required': False})
                elif node.func.attr == 'get':
                    query.setdefault(qn, {'schema': {'type': typ}, 'required': False})
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == 'args' \
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                query.setdefault(node.slice.value, {'schema': {'type': 'string'}, 'required': True})
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in HTML_CALLS:
                    media = media or 'text/html'
                elif name in FILE_CALLS:
                    media = media or 'application/octet-stream'
                elif name in REDIRECT_CALLS:
                    statuses.add('302')
                elif name == 'jsonify':
                    media = 'application/json'
            elif isinstance(node, ast.Return) and node.value is not None:
                v = node.value
                if isinstance(v, ast.Tuple) and len(v.elts) >= 2 and isinstance(v.elts[1], ast.Constant) and isinstance(v.elts[1].value, int):
                    statuses.add(str(v.elts[1].value))
                    v = v.elts[0]
                if isinstance(v, (ast.Dict, ast.List, ast.ListComp, ast.DictComp)):
                    media = media or 'application/json'
    if media is None and h['framework'] in ('fastapi', 'azure-functions'):
        media = 'application/json'
    ok = sorted(s for s in statuses if s.startswith('2')) or ([] if statuses and not media else ['200'])
    responses = {}
    for st in ok:
        responses[st] = {'description': 'derived from the handler; replace with what it returns',
                         **({'content': {media: {}}} if media else {})}
    for st in sorted(s for s in statuses if not s.startswith('2')):
        responses[st] = {'description': 'derived from the handler'}
    params = [{'name': n, 'in': 'path', 'required': True, 'schema': path_types[n]} for n in names]
    params += [{'name': n, 'in': 'query', 'required': q['required'], 'schema': q['schema']} for n, q in query.items()]
    op = {'operationId': h.get('_function') or f"{h['method'].lower()}_{re.sub(r'[^A-Za-z0-9]+', '_', h['path']).strip('_') or 'root'}",
          'x-derived-from': f"{h['file']}:{h['line']}", 'responses': responses or {'200': {'description': 'derived'}}}
    if params:
        op['parameters'] = params
    return op


def skeleton(handlers: list[dict], root: Path, repo_name: str) -> dict:
    """A derived OpenAPI 3.1 contract: every handler's path and method, its parameters and response kind, marked
    x-contract-provenance: derived. The run against it checks the code against itself; say so."""
    sources: dict[str, str | None] = {}
    paths: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for h in sorted(handlers, key=lambda x: (x['path'], x['method'])):
        if h['file'] not in sources:
            try:
                sources[h['file']] = (root / h['file']).read_text(encoding='utf-8', errors='replace') if h['file'].endswith('.py') else None
            except OSError:
                sources[h['file']] = None
        methods = HTTP[:5] if h['method'] == '*' else (h['method'],)
        for m in methods:
            op = derive_operation(h, sources[h['file']])
            oid = op['operationId']
            if oid in seen_ids:
                oid = f'{oid}_{m.lower()}'
            seen_ids.add(oid)
            op['operationId'] = oid
            paths.setdefault(h['path'], {})[m.lower()] = op
    return {'openapi': '3.1.0',
            'info': {'title': f'{repo_name} (derived from the handlers)', 'version': '0.0.0-derived',
                     'description': 'Derived by handler_map.py --skeleton from the route handlers, not published by the API. '
                                    'Response schemas are left open; fill them in from the code or the documentation. A run '
                                    'against a derived contract checks the code against itself.'},
            'x-contract-provenance': 'derived',
            'paths': paths}


def detect(root: Path, wanted: list[str], exclude: set[str]) -> tuple[list[str], list[dict], int]:
    pkg = ''
    for f in ('package.json',):
        if (root / f).is_file():
            pkg = (root / f).read_text(encoding='utf-8', errors='replace')
    has_express = '"express"' in pkg
    handlers, scanned, fws = [], 0, set()
    py_files, host_files = {}, {}
    for p in walk(root, exclude):
        rel = p.relative_to(root).as_posix()
        name = p.name
        is_js, is_py = name.endswith(JS_EXT), name.endswith('.py')
        if name == 'host.json':
            try:
                host_files[rel] = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                pass
            continue
        if not (is_js or is_py):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        scanned += 1
        found = []
        if is_js and ('nextjs' in wanted or 'auto' in wanted):
            if NEXT_ROUTE.match(name):
                found += scan_next(p, rel, text)
            elif '/pages/api/' in '/' + rel and not re.search(r'\.(test|spec)\.', name):
                found += scan_pages_api(rel, text)
        if is_js and not found and ('express' in wanted or ('auto' in wanted and has_express)) and re.search(r'\bexpress\b|Router\(', text):
            found += scan_express(rel, text)
        if is_py:
            py_files[rel] = text
        for h in found:
            fws.add(h['framework'])
        handlers += found
    if any(w in wanted for w in ('auto',) + PY_FRAMEWORKS):
        found = python_handlers(py_files, host_files, wanted)
        fws |= {h['framework'] for h in found}
        handlers += found
    uniq = {(h['method'], h['path'], h['file'], h['line']): h for h in handlers}
    return sorted(fws), sorted(uniq.values(), key=lambda h: (h['path'], h['method'], h['file'], h['line'])), scanned


def shape(path: str) -> str:
    return re.sub(r'\{[^}]+\}', '{}', path.rstrip('/') or '/')


def default_prefix(contract: dict) -> str:
    servers = (contract.get('root') or {}).get('servers') or []
    url = (servers[0] or {}).get('url', '') if servers else ''
    m = re.match(r'^(?:[a-z]+://[^/]+)?(/[^?#]*)?', url)
    p = (m.group(1) if m else '') or ''
    return '' if p in ('', '/') or '{' in p else p.rstrip('/')


def compare(contract: dict, handlers: list[dict], prefix: str) -> list[dict]:
    issues = []
    by_shape: dict[str, list[dict]] = {}
    for h in handlers:
        by_shape.setdefault(shape(h['path']), []).append(h)
    documented = set()
    for op in contract['operations']:
        full = prefix + op['path']
        hs = by_shape.get(shape(full), [])
        documented.add((op['method'], shape(full)))
        if not hs:
            issues.append({'kind': 'missing_handler', 'method': op['method'], 'path': op['path'], 'operation_id': op['operation_id'],
                           'file': None, 'line': None,
                           'message': f'{op["method"]} {op["path"]} is documented, but no handler for {full} was found'})
            continue
        methods = {h['method'] for h in hs}
        handled = op['method'] in methods or '*' in methods or (op['method'] == 'HEAD' and 'GET' in methods) \
            or (op['method'] == 'OPTIONS' and any(h['framework'] == 'nextjs' for h in hs))
        if not handled:
            h = min(hs, key=lambda x: (x['file'], x['line']))
            issues.append({'kind': 'method_not_handled', 'method': op['method'], 'path': op['path'], 'operation_id': op['operation_id'],
                           'file': h['file'], 'line': h['line'],
                           'message': f'{op["method"]} {op["path"]} is documented, but {h["file"]} handles only {", ".join(sorted(methods))}'})
        json_only = [mt for st, r in (op.get('responses') or {}).items() if str(st).startswith('2')
                     for mt in (r.get('content') or {})]
        if json_only and all(re.search(r'[/+]json\b', mt) for mt in json_only):
            for h in hs:
                streamed = sorted(set(h.get('response_classes') or []) & NON_JSON_RESPONSES)
                if streamed and h['method'] in (op['method'], '*'):
                    issues.append({'kind': 'response_class_mismatch', 'method': op['method'], 'path': op['path'],
                                   'operation_id': op['operation_id'], 'file': h['file'], 'line': h['line'],
                                   'message': f'{op["method"]} {op["path"]} documents only {", ".join(sorted(set(json_only)))} '
                                              f'for its 2xx responses, but the handler uses {", ".join(streamed)}'})
                    break
        names = re.findall(r'\{([^}]+)\}', full)
        for h in hs:
            hn = re.findall(r'\{([^}]+)\}', h['path'])
            if hn != names and (h['method'] == op['method'] or h['method'] == '*'):
                issues.append({'kind': 'parameter_name_mismatch', 'method': op['method'], 'path': op['path'],
                               'operation_id': op['operation_id'], 'file': h['file'], 'line': h['line'],
                               'message': f'the contract names the path parameters {names}, the handler {hn}'})
                break
    for h in handlers:
        sh = shape(h['path'])
        if h['method'] == '*':
            if not any(s == sh for _, s in documented):
                issues.append({'kind': 'undocumented_handler', 'method': '*', 'path': h['path'], 'operation_id': None,
                               'file': h['file'], 'line': h['line'], 'message': f'{h["path"]} ({h["file"]}) is not in the contract'})
        elif (h['method'], sh) not in documented:
            issues.append({'kind': 'undocumented_handler', 'method': h['method'], 'path': h['path'], 'operation_id': None,
                           'file': h['file'], 'line': h['line'],
                           'message': f'{h["method"]} {h["path"]} ({h["file"]}:{h["line"]}) is not in the contract'})
    order = {'method_not_handled': 0, 'missing_handler': 1, 'undocumented_handler': 2, 'parameter_name_mismatch': 3,
             'response_class_mismatch': 4}
    issues.sort(key=lambda i: (order[i['kind']], i['path'], i['method']))
    for n, i in enumerate(issues, 1):
        i['id'] = f'H{n}'
    return [{k: i[k] for k in ('id', 'kind', 'method', 'path', 'operation_id', 'file', 'line', 'message')} for i in issues]


def build(repo: Path, contract: dict | None, frameworks: list[str], prefix: str | None, exclude: set[str],
          handler_prefix: str = '', only: list[str] | None = None, skeleton_out: Path | None = None) -> dict:
    """Compare the contract with the handlers. With no contract, list the handlers and compare nothing; with
    skeleton_out, also write a derived OpenAPI contract from them. `only` keeps handlers under these path prefixes
    (the API surface of a site that also serves HTML pages)."""
    fws, handlers, scanned = detect(repo, frameworks, exclude)
    prefix = (default_prefix(contract) if contract else '') if prefix is None else ('/' + prefix.strip('/') if prefix.strip('/') else '')
    hp = '/' + handler_prefix.strip('/') if handler_prefix.strip('/') else ''
    if hp:
        for h in handlers:
            h['path'] = (hp + h['path']).rstrip('/') or '/'
    dropped = 0
    if only:
        # /api/ keeps what is under /api (not the /api page itself); /api keeps /api and what is under it
        keep = [h for h in handlers if any(h['path'].startswith(o) if o.endswith('/') else (h['path'] == o or h['path'].startswith(o + '/'))
                                           for o in only)]
        dropped, handlers = len(handlers) - len(keep), keep
    if skeleton_out is not None:
        skeleton_out.parent.mkdir(parents=True, exist_ok=True)
        skeleton_out.write_text(json.dumps(skeleton(handlers, repo, repo.resolve().name), indent=2) + '\n', encoding='utf-8')
    issues = compare(contract, handlers, prefix) if handlers and contract else []
    counts = {k: sum(i['kind'] == k for i in issues) for k in ('undocumented_handler', 'missing_handler', 'method_not_handled',
                                                                'parameter_name_mismatch', 'response_class_mismatch')}
    unmounted = sum(h.get('mount') in ('unmounted', 'dynamic') for h in handlers)
    handlers = [{k: v for k, v in h.items() if not k.startswith('_')} for h in handlers]
    note = None
    if not handlers:
        note = 'no route handlers recognised; nothing was compared'
    elif contract is None:
        note = 'no contract given: the handlers are listed and nothing was compared' + \
            (f'; a derived contract was written to {skeleton_out.as_posix()}' if skeleton_out else '')
    out = {'tool': 'handler_map.py', 'contract_sha256': contract['sha256'] if contract else None, 'repo': repo.as_posix(),
           'frameworks': fws, 'contract_prefix': prefix, 'handler_prefix': hp, 'files_scanned': scanned,
           'unresolved_mounts': unmounted, 'handlers': handlers,
           'operations': len(contract['operations']) if contract else 0, 'counts': counts, 'issues': issues, 'note': note}
    if only:
        out['only'] = only
        out['handlers_outside_only'] = dropped
    return out


def self_test() -> int:
    files = {
        'package.json': '{"dependencies": {"next": "16.0.0"}}',
        'src/app/api/items/route.ts': 'export async function GET() {}\nexport async function POST() {}\n',
        'src/app/api/items/[id]/route.ts': 'const h = () => 1\nexport { h as GET, h as DELETE }\n',
        'src/app/api/items/[id]/move/route.ts': 'export const PUT = wrap(async () => 1)\nexport const PATCH = 1\n',
        'src/app/(admin)/api/admin/stats/route.ts': 'export function GET() {}\n',
        'src/app/api/auth/[...nextauth]/route.ts': 'const handler = NextAuth()\nexport { handler as GET, handler as POST }\n',
        'node_modules/x/app/api/zzz/route.ts': 'export function GET() {}\n',
        'flaskapp/app.py': 'from flask import Flask\napp = Flask(__name__)\n@app.route("/health")\ndef h(): pass\n'
                           '@app.route("/things/<int:thing_id>", methods=["GET", "DELETE"])\ndef t(thing_id): pass\n',
        'flaskapp/orders.py': 'from flask import Blueprint\nbp = Blueprint("orders", __name__, url_prefix="/orders")\n'
                              '@bp.get("/<int:order_id>")\ndef order(order_id): pass\n',
        # FastAPI: a subclass of APIRouter with a prefix, an include_router prefix in another module, a handler that
        # streams, and decorators inside a docstring (a usage example, not a route)
        'bakery/api/routes/__init__.py': 'from fastapi import APIRouter\n\n\nclass BakeryRouter(APIRouter):\n    pass\n',
        'bakery/api/routes/loaves.py': 'from fastapi.responses import StreamingResponse\nfrom api.routes import BakeryRouter\n\n'
                                       'router = BakeryRouter(prefix="/v1")\n\n\n@router.get("/loaves")\ndef loaves(): return []\n\n\n'
                                       '@router.get("/loaves/{loaf_id}/label")\ndef label(loaf_id: int):\n'
                                       '    return StreamingResponse(iter([b"x"]), media_type="text/csv")\n',
        'bakery/api/main.py': 'from fastapi import FastAPI\nfrom api.routes import loaves\n\napp = FastAPI()\n'
                              'app.include_router(loaves.router, prefix="/bakery")\n',
        'bakery/api/auth.py': '"""Usage::\n\n    @router.get("/secret")\n    def secret(): ...\n"""\nfrom fastapi import Depends\n',
    }
    contract = {'sha256': '0' * 64, 'root': {}, 'operations': [
        {'operation_id': 'listItems', 'method': 'GET', 'path': '/api/items'},
        {'operation_id': 'getItem', 'method': 'GET', 'path': '/api/items/{itemId}'},
        {'operation_id': 'moveItem', 'method': 'POST', 'path': '/api/items/{id}/move'},
        {'operation_id': 'gone', 'method': 'GET', 'path': '/api/gone'},
        {'operation_id': 'thing', 'method': 'GET', 'path': '/things/{thing_id}'},
        {'operation_id': 'getOrder', 'method': 'GET', 'path': '/orders/{order_id}'},
        {'operation_id': 'listLoaves', 'method': 'GET', 'path': '/bakery/v1/loaves'},
        {'operation_id': 'loafLabel', 'method': 'GET', 'path': '/bakery/v1/loaves/{loaf_id}/label',
         'responses': {'200': {'content': {'application/json': {}}, 'headers': {}}}},
    ]}
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        res = build(root, contract, ['auto'], '', set())
    got = {(i['kind'], i['method'], i['path']) for i in res['issues']}
    want = {('method_not_handled', 'POST', '/api/items/{id}/move'), ('missing_handler', 'GET', '/api/gone'),
            ('parameter_name_mismatch', 'GET', '/api/items/{itemId}'), ('undocumented_handler', 'POST', '/api/items'),
            ('undocumented_handler', 'DELETE', '/api/items/{id}'), ('undocumented_handler', 'PUT', '/api/items/{id}/move'),
            ('undocumented_handler', 'PATCH', '/api/items/{id}/move'), ('undocumented_handler', 'GET', '/api/admin/stats'),
            ('undocumented_handler', 'GET', '/api/auth/{nextauth}'), ('undocumented_handler', 'POST', '/api/auth/{nextauth}'),
            ('undocumented_handler', 'GET', '/health'), ('undocumented_handler', 'DELETE', '/things/{thing_id}'),
            ('response_class_mismatch', 'GET', '/bakery/v1/loaves/{loaf_id}/label')}
    ok = got == want and set(res['frameworks']) == {'nextjs', 'flask', 'fastapi'} \
        and not any('node_modules' in h['file'] or 'auth.py' in h['file'] for h in res['handlers'])
    if not ok:
        print('missing:', sorted(want - got), '\nunexpected:', sorted(got - want), '\nframeworks:', res['frameworks'])
        print('SELF-TEST FAILED')
        return 1
    print(f'self-test passed: {len(res["handlers"])} handlers, {len(res["issues"])} issues')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('repo', type=Path, nargs='?')
    ap.add_argument('--contract', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--framework', action='append', choices=['auto', 'nextjs', 'express', 'flask', 'fastapi', 'azure-functions'])
    ap.add_argument('--contract-prefix', default=None, help='prepended to every contract path (default: the first servers URL path)')
    ap.add_argument('--handler-prefix', default='', help='prepended to every handler path, for a mount prefix the reader cannot see')
    ap.add_argument('--exclude-dir', action='append', default=[])
    ap.add_argument('--only', action='append', default=[], metavar='PREFIX',
                    help='keep only handlers whose path starts with PREFIX (repeatable), e.g. /api/ on a site that also serves pages')
    ap.add_argument('--skeleton', type=Path, metavar='FILE',
                    help='write a derived OpenAPI 3.1 contract (JSON) from the handlers, marked x-contract-provenance: derived')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.repo or not a.out:
        ap.error('repo and --out are required (or --self-test); --contract compares, --skeleton derives one')
    if not a.repo.is_dir():
        print(f'error: {a.repo} is not a directory', file=sys.stderr)
        return 2
    contract = json.loads(a.contract.read_text(encoding='utf-8')) if a.contract else None
    if contract and contract['kind'] != 'openapi':
        print(f'error: the handler comparison needs an OpenAPI contract, not {contract["kind"]}', file=sys.stderr)
        return 2
    res = build(a.repo, contract, a.framework or ['auto'], a.contract_prefix, set(a.exclude_dir), a.handler_prefix,
                [o if o.startswith('/') else '/' + o for o in a.only], a.skeleton)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    print(f'{len(res["handlers"])} handler(s) in {", ".join(res["frameworks"]) or "no recognised framework"}; '
          f'{res["operations"]} operation(s); {res["counts"]} -> {a.out}', file=sys.stderr)
    if a.skeleton:
        print(f'derived contract (x-contract-provenance: derived) -> {a.skeleton}; fill in the response schemas, and say in the '
              'report that the contract was derived from the code', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
