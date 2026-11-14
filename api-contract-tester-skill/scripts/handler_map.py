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
a form or multipart body from Form()/File(), and each status with its media types: response classes
and their media_type=/status_code=, the decorator's response_class=/status_code=, raise HTTPException(404),
abort(404), `return ..., 201`, jsonify or a dict/list literal (JSON), render_template (HTML), redirect
(302), send_file (a file); a return of a helper it can't follow is */*, not JSON. A {name:path} catch-all
is marked x-path-allows-slash, a StaticFiles mount is one GET <mount>/{file} operation (x-mount), a
router included under an `if` is x-conditional, and add_middleware classes are listed (x-middleware).
Response schemas are left open for the agent to fill in. --only PREFIX (repeatable) keeps handlers under that path, so a site's HTML pages, static
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
(a wrapper, a generated router, a mount prefix). A mount (`kind: mount`) and a catch-all route
(`catch_all`) cover every documented path under their prefix. Directory walks prune node_modules, .git,
.next, dist, build and the like before descending and list them in `pruned_dirs`. Files this reader
does not read are named: `unread_languages` (PHP, Go, Ruby, Java, ... file counts) and
`unread_handlers` (PHP front controllers routing on $_SERVER['REQUEST_URI'], .htaccess RewriteRule),
with a warning on stderr.
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


def walk(root: Path, exclude: set[str], pruned: list | None = None):
    """Files under root; directories in PRUNE or --exclude-dir are not entered, and each one left out is
    appended to `pruned` (relative path) so the output can say what was skipped."""
    for dirpath, dirnames, filenames in os.walk(root):
        keep = sorted(d for d in dirnames if d not in PRUNE and d not in exclude and not d.startswith('.git'))
        if pruned is not None:
            base = Path(dirpath).relative_to(root)
            pruned += [(base / d).as_posix() for d in sorted(set(dirnames) - set(keep)) if not d.startswith('.git')]
        dirnames[:] = keep
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
    scanned = scan_python(py_files, host_files)
    extras = python_extras(py_files)
    # py_routes may report these itself (a newer shared copy): prefer its fields when present
    cond_by_file: dict[str, str] = {}
    for c in extras['conditional_includes']:
        for rel in py_files:
            if c.get('file') == rel or (c.get('module') and _module_of(rel) == c['module']):
                cond_by_file.setdefault(rel, c['condition'])
    for r in scanned['routes']:
        fw = r['framework']
        if not ('auto' in wanted or fw in wanted):
            continue
        if fw == 'flask':
            path = flask_path(r['path'])
            catch = re.findall(r'<path:(\w+)>', r['path'])
        else:
            path = re.sub(r'\{(\w+):[^}]*\}', r'{\1}', r['path']).rstrip('/') or '/'   # {file_path:path} -> {file_path}
            catch = re.findall(r'\{(\w+):path\}', r['path'])
        h = {'method': '*' if r['method'] == 'ANY' else r['method'], 'path': path, 'file': r['file'], 'line': r['line'],
             'framework': fw, 'mount': r['mount'], '_raw_path': r['path'], '_function': r.get('function')}
        if catch:
            h['catch_all'] = catch[-1]          # a {name:path} parameter matches several segments
        cond = r.get('conditional') or cond_by_file.get(r['file'])
        if cond:
            h['conditional'] = cond if isinstance(cond, str) else 'under a condition'
        if r.get('response_classes'):
            h['response_classes'] = r['response_classes']
        out.append(h)
    mounts = scanned.get('mounts') if isinstance(scanned.get('mounts'), list) else extras['mounts']
    if 'auto' in wanted or 'fastapi' in wanted:
        for m in mounts:
            base = '/' + m['path'].strip('/') if m['path'].strip('/') else ''
            static = m.get('static', m.get('app') == 'StaticFiles')
            h = {'method': 'GET' if static else '*', 'path': f'{base}/{{file}}' if static else (base or '/'), 'file': m['file'],
                 'line': m['line'], 'framework': 'fastapi', 'mount': 'resolved', 'kind': 'mount', 'mount_app': m.get('app') or 'app',
                 'catch_all': 'file' if static else None, '_function': None}
            if not static:
                h['path'] = f'{base}/{{path}}'
                h['catch_all'] = 'path'
            if m.get('directory'):
                h['directory'] = m['directory']
            if m.get('conditional'):
                h['conditional'] = m['conditional']
            out.append(h)
    out_extras = {'middleware': extras['middleware']}
    return out, out_extras


CONVERTER_TYPE = {'int': {'type': 'integer'}, 'float': {'type': 'number'}, 'uuid': {'type': 'string', 'format': 'uuid'},
                  'path': {'type': 'string'}, 'string': {'type': 'string'}}
PY_TYPE = {'int': 'integer', 'float': 'number', 'bool': 'boolean', 'str': 'string'}
HTML_CALLS = {'render_template', 'render_template_string', 'render', 'TemplateResponse', 'HTMLResponse'}
FILE_CALLS = {'send_file', 'send_from_directory', 'FileResponse', 'StreamingResponse'}
REDIRECT_CALLS = {'redirect', 'RedirectResponse', 'HttpResponseRedirect'}
# response classes and the media type each answers in (None: no body unless media_type= or content says so)
RESPONSE_MEDIA = {'JSONResponse': 'application/json', 'ORJSONResponse': 'application/json', 'UJSONResponse': 'application/json',
                  'HTMLResponse': 'text/html', 'PlainTextResponse': 'text/plain', 'TemplateResponse': 'text/html',
                  'FileResponse': 'application/octet-stream', 'StreamingResponse': '*/*', 'EventSourceResponse': 'text/event-stream',
                  'RedirectResponse': None, 'Response': None}


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


def _status_value(node) -> int | None:
    """404 from `404`, `status.HTTP_404_NOT_FOUND` or `HTTPStatus.NOT_FOUND`."""
    import ast
    import http
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and 100 <= node.value < 600:
        return node.value
    if isinstance(node, ast.Attribute):
        m = re.match(r'HTTP_(\d{3})', node.attr)
        if m:
            return int(m.group(1))
        try:
            return int(http.HTTPStatus[node.attr])
        except KeyError:
            return None
    return None


def _kwarg(call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _str_const(node) -> str | None:
    import ast
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _response_call(call, framework: str):
    """(status, media type or None, handled) for a call that builds a response: a response class with its
    media_type= and status_code=, or a Flask helper. handled is False for any other call."""
    name = _call_name(call)
    if name in RESPONSE_MEDIA:
        media = _str_const(_kwarg(call, 'media_type')) or RESPONSE_MEDIA[name]
        if name == 'Response' and media is None and (call.args or _kwarg(call, 'content') is not None):
            media = '*/*'                       # Response(content) with no media_type: the type isn't known
        st = _status_value(_kwarg(call, 'status_code'))
        default = 307 if name == 'RedirectResponse' else 200
        return st or default, media, True
    if name in ('send_file', 'send_from_directory'):
        return 200, _str_const(_kwarg(call, 'mimetype')) or 'application/octet-stream', True
    if name in ('render_template', 'render_template_string', 'render'):
        return 200, 'text/html', True
    if name == 'jsonify':
        return 200, 'application/json', True
    if name in ('redirect', 'HttpResponseRedirect'):
        return _status_value(call.args[1]) if len(call.args) > 1 and _status_value(call.args[1]) else 302, None, True
    return None, None, False


def derive_operation(h: dict, source: str | None) -> dict:
    """An OpenAPI operation for one handler, read from its source: path parameters (Flask converters, FastAPI
    annotations; a {name:path} catch-all is marked x-path-allows-slash), query parameters
    (request.args.get(name, default, type=int), request.args[name], request.args.getlist(name); FastAPI
    parameters that are not path parameters), a form or multipart body from Form()/File() parameters, and the
    responses: each status the handler can answer with the media types it answers it in. A response class's
    media_type= and status_code=, the decorator's response_class= and status_code=, `raise HTTPException(404)`,
    Flask's abort(404) and `return ..., 201` are read; jsonify or a dict/list literal is JSON, render_template
    HTML, send_file a file, redirect a 302. A return of a call this reader doesn't know (a helper that may stream
    or proxy) is */*, not JSON. Response schemas are left open: the skeleton says what exists, not what it returns."""
    import ast
    raw = h.get('_raw_path') or h['path']
    names = re.findall(r'\{([^}]+)\}', h['path'])
    conv = {m.group(2): m.group(1) for m in re.finditer(r'<(?:(\w+):)?(\w+)>', raw)}
    conv.update({m.group(1): m.group(2) for m in re.finditer(r'\{(\w+):(\w+)\}', raw)})
    path_types = {n: dict(CONVERTER_TYPE.get(conv.get(n) or 'string', {'type': 'string'})) for n in names}
    query: dict[str, dict] = {}
    form: dict[str, dict] = {}
    multipart = False
    answers: dict[str, set] = {}          # status -> media types (None: no body)

    def answer(st, media):
        answers.setdefault(str(st), set()).add(media)

    fn = None
    if source:
        try:
            fn = _func_def(ast.parse(source), h.get('_function'), h['line'])
        except SyntaxError:
            fn = None
    fastapi = h['framework'] == 'fastapi'
    if fn is not None:
        dec = next((d for d in fn.decorator_list if isinstance(d, ast.Call) and d.lineno == h['line']), None) or \
            next((d for d in fn.decorator_list if isinstance(d, ast.Call)), None)
        declared_class = _call_name(ast.Call(func=_kwarg(dec, 'response_class'), args=[], keywords=[])) \
            if dec is not None and _kwarg(dec, 'response_class') is not None else None
        declared_status = _status_value(_kwarg(dec, 'status_code')) if dec is not None else None
        typed_return = dec is not None and _kwarg(dec, 'response_model') is not None or fn.returns is not None
        default_media = (RESPONSE_MEDIA.get(declared_class) or '*/*') if declared_class else \
            ('application/json' if fastapi or h['framework'] == 'azure-functions' else None)
        ok_status = str(declared_status or 200)
        if fastapi:
            args = fn.args.args + fn.args.kwonlyargs
            defaults = [None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults) + list(fn.args.kw_defaults)
            for arg, default in zip(args, defaults):
                ann = arg.annotation.id if isinstance(arg.annotation, ast.Name) else None
                dname = _call_name(default) if isinstance(default, ast.Call) else None
                if dname in ('File', 'Form') or ann == 'UploadFile':
                    multipart = multipart or dname == 'File' or ann == 'UploadFile'
                    form[arg.arg] = ({'type': 'string', 'format': 'binary'} if dname == 'File' or ann == 'UploadFile'
                                     else {'type': PY_TYPE.get(ann, 'string')})
                    continue
                if dname in ('Depends', 'Security', 'Body', 'Header', 'Cookie'):
                    continue
                if arg.arg in ('self', 'request', 'response', 'session', 'db') or (ann and ann not in PY_TYPE):
                    continue
                if arg.arg in names:
                    if ann in PY_TYPE:
                        path_types[arg.arg] = {'type': PY_TYPE[ann]}
                    continue
                query[arg.arg] = {'schema': {'type': PY_TYPE.get(ann, 'string')}, 'required': default is None}
        handled: set[int] = set()
        bound: dict[str, ast.AST] = {}      # name -> the call it was bound to (`resp = f()`, `if resp := f():`)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                bound[node.targets[0].id] = node.value.value if isinstance(node.value, ast.Await) else node.value
            elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
                bound[node.target.id] = node.value.value if isinstance(node.value, ast.Await) else node.value
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and isinstance(bound.get(node.value.id), ast.Call):
                call = bound[node.value.id]
                if _response_call(call, h['framework'])[2]:
                    continue                 # a response object: the walk below reads it where it is built
                if not (typed_return or (_call_name(call) or '')[:1].isupper()):
                    answer(ok_status, '*/*')  # the result of a helper this reader can't follow
                    continue
            if isinstance(node, ast.Return) and node.value is not None:
                v = node.value
                st = None
                if isinstance(v, ast.Tuple) and len(v.elts) >= 2 and _status_value(v.elts[1]):
                    st = _status_value(v.elts[1])
                    v = v.elts[0]
                if isinstance(v, ast.Await):
                    v = v.value
                if isinstance(v, ast.Call):
                    cst, cmedia, known = _response_call(v, h['framework'])
                    handled.add(id(v))
                    if known:
                        answer(st or cst, cmedia)
                    elif _call_name(v) in ('make_response',):
                        answer(st or ok_status, '*/*')
                    elif typed_return or (_call_name(v) or '')[:1].isupper():
                        answer(st or ok_status, default_media or 'application/json')   # a model built and returned
                    else:
                        answer(st or ok_status, '*/*')   # a helper this reader can't follow: it may stream, proxy or send a file
                elif isinstance(v, (ast.Dict, ast.List, ast.ListComp, ast.DictComp)):
                    answer(st or ok_status, default_media if declared_class else 'application/json')
                elif isinstance(v, (ast.Constant, ast.JoinedStr)) and not (isinstance(v, ast.Constant) and v.value is None):
                    answer(st or ok_status, default_media if fastapi or declared_class else 'text/html')
                else:
                    answer(st or ok_status, default_media)
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and \
                    (_call_name(node.exc) or '').endswith('HTTPException'):
                st = _status_value(node.exc.args[0]) if node.exc.args else _status_value(_kwarg(node.exc, 'status_code'))
                if st:
                    answer(st, 'application/json' if fastapi else None)
                handled.add(id(node.exc))
            elif isinstance(node, ast.Call) and _call_name(node) == 'abort' and node.args and _status_value(node.args[0]):
                answer(_status_value(node.args[0]), 'text/html')          # Flask's default error page
                handled.add(id(node))
        for node in ast.walk(fn):    # response objects built and returned through a variable
            if isinstance(node, ast.Call) and id(node) not in handled                     and (_call_name(node) in RESPONSE_MEDIA or _call_name(node) in ('send_file', 'send_from_directory')):
                cst, cmedia, _ = _response_call(node, h['framework'])
                answer(cst, cmedia)
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
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == 'args' \
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                query.setdefault(node.slice.value, {'schema': {'type': 'string'}, 'required': True})
        if not any(s.startswith(('2', '3')) for s in answers):
            answer(ok_status, default_media)
    elif h['framework'] in ('fastapi', 'azure-functions'):
        answer('200', 'application/json')
    if not answers:
        answer('200', None)
    responses = {}
    for st in sorted(answers):
        media = sorted(m for m in answers[st] if m)
        ok = st.startswith('2')
        responses[st] = {'description': 'derived from the handler; replace with what it returns' if ok else 'derived from the handler',
                         **({'content': {m: {} for m in media}} if media else {})}
    params = []
    for n in names:
        p = {'name': n, 'in': 'path', 'required': True, 'schema': path_types[n]}
        if conv.get(n) == 'path':
            p['x-path-allows-slash'] = True      # a catch-all: the value may hold slashes (send them unencoded)
        params.append(p)
    params += [{'name': n, 'in': 'query', 'required': q['required'], 'schema': q['schema']} for n, q in query.items()]
    op = {'operationId': h.get('_function') or f"{h['method'].lower()}_{re.sub(r'[^A-Za-z0-9]+', '_', h['path']).strip('_') or 'root'}",
          'x-derived-from': f"{h['file']}:{h['line']}", 'responses': responses}
    if params:
        op['parameters'] = params
    if form:
        mt = 'multipart/form-data' if multipart else 'application/x-www-form-urlencoded'
        op['requestBody'] = {'required': True, 'content': {mt: {'schema': {'type': 'object', 'required': sorted(form),
                                                                             'properties': form}}}}
    return op


def _parents(tree) -> dict:
    import ast
    par = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            par[ch] = node
    return par


def _condition(node, par: dict, rel: str) -> str | None:
    """`if FLAG (file:line)` for the innermost `if` the call sits under, in its module or an app factory."""
    import ast
    cur = par.get(node)
    while cur is not None and not isinstance(cur, ast.Module):
        if isinstance(cur, ast.If):
            try:
                text = ast.unparse(cur.test)
            except Exception:  # noqa: BLE001
                text = '...'
            return f'if {text[:80]} ({rel}:{cur.lineno})'
        if isinstance(cur, ast.ClassDef):
            return None
        cur = par.get(cur)
    return None


def python_extras(py_files: dict[str, str]) -> dict:
    """What py_routes does not list (yet): Starlette/FastAPI `app.mount(path, StaticFiles(...) | sub_app)`,
    include_router/register_blueprint calls under an `if` (routes that exist only when a flag is set), and
    `add_middleware(...)` classes. Read with ast; nothing is imported."""
    import ast
    mounts, conditional, middleware = [], [], []
    for rel, text in py_files.items():
        if not re.search(r'\b(fastapi|starlette|flask)\b', text):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        par = _parents(tree)
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for al in node.names:
                    imports[al.asname or al.name] = node.module
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr == 'mount' and (node.args or _kwarg(node, 'path') is not None):
                path = _str_const(node.args[0] if node.args else _kwarg(node, 'path'))
                app = node.args[1] if len(node.args) > 1 else _kwarg(node, 'app')
                if path is None or app is None:
                    continue
                app_name = _call_name(app) if isinstance(app, ast.Call) else (app.id if isinstance(app, ast.Name) else
                                                                              app.attr if isinstance(app, ast.Attribute) else None)
                directory = _str_const(_kwarg(app, 'directory')) if isinstance(app, ast.Call) else None
                mounts.append({'path': path, 'file': rel, 'line': node.lineno, 'app': app_name or 'app',
                               'static': app_name == 'StaticFiles', 'directory': directory,
                               'conditional': _condition(node, par, rel)})
            elif attr in ('include_router', 'register_blueprint') and node.args:
                cond = _condition(node, par, rel)
                if not cond:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                    target = {'module': arg.value.id}         # `saves.router`: the router of module saves
                elif isinstance(arg, ast.Name):
                    target = {'module': imports[arg.id].rsplit('.', 1)[-1]} if arg.id in imports else {'file': rel}
                else:
                    continue
                conditional.append({**target, 'condition': cond, 'file_of_include': rel, 'line': node.lineno})
            elif attr == 'add_middleware' and node.args:
                name = node.args[0].id if isinstance(node.args[0], ast.Name) else \
                    node.args[0].attr if isinstance(node.args[0], ast.Attribute) else None
                if name:
                    middleware.append({'name': name, 'file': rel, 'line': node.lineno})
    return {'mounts': mounts, 'conditional_includes': conditional, 'middleware': middleware}


def _module_of(rel: str) -> str:
    p = Path(rel)
    return p.parent.name if p.name == '__init__.py' else p.stem


def project_name(root: Path) -> str:
    """The project's own name (pyproject.toml, package.json), else the directory name."""
    try:
        m = re.search(r'(?ms)^\[(?:project|tool\.poetry)\]\s*$.*?^name\s*=\s*["\']([^"\']+)', (root / 'pyproject.toml').read_text(encoding='utf-8'))
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        name = json.loads((root / 'package.json').read_text(encoding='utf-8')).get('name')
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError):
        pass
    return root.resolve().name


def mount_operation(h: dict) -> dict:
    """A mounted static folder or sub-application, as one GET operation on `<mount>/{file}` (several segments)."""
    static = h.get('mount_app') == 'StaticFiles'
    what = f"StaticFiles(directory={h.get('directory')!r})" if static else h.get('mount_app', 'app')
    oid = re.sub(r'[^A-Za-z0-9]+', '_', f"mount_{h.get('directory') or h.get('mount_app') or 'app'}").strip('_')
    return {'operationId': oid, 'x-derived-from': f"{h['file']}:{h['line']}", 'x-mount': what,
            'description': f'Served by a mount ({what}): every file under it, not one route.',
            'parameters': [{'name': h.get('catch_all') or 'file', 'in': 'path', 'required': True, 'schema': {'type': 'string'},
                            'x-path-allows-slash': True}],
            'responses': {'200': {'description': 'derived from the mount; a file of any type', 'content': {'*/*': {}}},
                          '404': {'description': 'derived from the mount: no such file'}}}


def skeleton(handlers: list[dict], root: Path, repo_name: str, middleware: list[dict] | None = None) -> dict:
    """A derived OpenAPI 3.1 contract: every handler's path and method, its parameters and responses (error
    statuses included), mounts as catch-all operations, and conditional routes marked x-conditional, marked
    x-contract-provenance: derived. The run against it checks the code against itself; say so."""
    sources: dict[str, str | None] = {}
    paths: dict[str, dict] = {}
    seen_ids: set[str] = set()
    expanded = []
    for h in sorted(handlers, key=lambda x: (x['path'], x['method'])):
        expanded += [(h, m) for m in (HTTP[:5] if h['method'] == '*' else (h['method'],))]
    per_fn: dict[tuple, int] = {}
    for h, m in expanded:
        per_fn[(h['file'], h['line'], h.get('_function'))] = per_fn.get((h['file'], h['line'], h.get('_function')), 0) + 1
    for h, m in expanded:
        if h['file'] not in sources:
            try:
                sources[h['file']] = (root / h['file']).read_text(encoding='utf-8', errors='replace') if h['file'].endswith('.py') else None
            except OSError:
                sources[h['file']] = None
        op = mount_operation(h) if h.get('kind') == 'mount' else derive_operation(h, sources[h['file']])
        oid = op['operationId']
        if per_fn[(h['file'], h['line'], h.get('_function'))] > 1 or oid in seen_ids:
            oid = f'{oid}_{m.lower()}'          # one function serving several methods: every method gets its suffix
        while oid in seen_ids:
            oid += '_'
        seen_ids.add(oid)
        op['operationId'] = oid
        if h.get('conditional'):
            op['x-conditional'] = h['conditional']
        paths.setdefault(h['path'], {})[m.lower()] = op
    out = {'openapi': '3.1.0',
           'info': {'title': f'{repo_name} (derived from the handlers)', 'version': '0.0.0-derived',
                    'description': 'Derived by handler_map.py --skeleton from the route handlers, not published by the API. '
                                   'Statuses and media types come from the handlers (response classes, media_type=, '
                                   'status_code=, raised HTTP errors); response schemas are left open, and error answers the '
                                   'framework adds itself (validation 422, 405, 404 for unknown paths) are not listed: fill '
                                   'them in from the code or the documentation. Operations marked x-conditional exist only '
                                   'when their condition holds. A run against a derived contract checks the code against itself.'},
           'x-contract-provenance': 'derived',
           'paths': paths}
    if middleware:
        out['x-middleware'] = [f"{m['name']} ({m['file']}:{m['line']})" for m in middleware]
        if any(re.search(r'basic', m['name'], re.I) for m in middleware):
            # inferred from the middleware's name; check which paths it exempts (preflights, health checks)
            out['components'] = {'securitySchemes': {'basicAuth': {'type': 'http', 'scheme': 'basic',
                                                                   'description': 'inferred from the middleware name; '
                                                                                  'check its exemptions'}}}
            out['security'] = [{'basicAuth': []}]
    return out


UNREAD_EXT = {'.php': 'PHP', '.go': 'Go', '.rb': 'Ruby', '.java': 'Java', '.kt': 'Kotlin', '.cs': 'C#', '.rs': 'Rust'}
PHP_ROUTING = re.compile(r"\$_SERVER\s*\[\s*['\"](?:REQUEST_URI|PATH_INFO)['\"]")


def detect(root: Path, wanted: list[str], exclude: set[str], info: dict | None = None) -> tuple[list[str], list[dict], int]:
    """Handlers in the languages read here. `info` (when given) receives what was not read: unread_languages
    {language: files}, unread_handlers (PHP front controllers, .htaccess rewrites), pruned directories and the
    middleware classes added to a Python app."""
    pkg = ''
    for f in ('package.json',):
        if (root / f).is_file():
            pkg = (root / f).read_text(encoding='utf-8', errors='replace')
    has_express = '"express"' in pkg
    handlers, scanned, fws = [], 0, set()
    py_files, host_files = {}, {}
    info = info if info is not None else {}
    info.update({'unread_languages': {}, 'unread_handlers': [], 'pruned': [], 'middleware': []})
    for p in walk(root, exclude, info['pruned']):
        rel = p.relative_to(root).as_posix()
        name = p.name
        is_js, is_py = name.endswith(JS_EXT), name.endswith('.py')
        lang = UNREAD_EXT.get(p.suffix.lower())
        if lang:
            info['unread_languages'][lang] = info['unread_languages'].get(lang, 0) + 1
        if p.suffix.lower() == '.php' or name == '.htaccess':
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            m = PHP_ROUTING.search(text) if name != '.htaccess' else re.search(r'(?im)^\s*RewriteRule\b.*', text)
            if m:
                kind = 'htaccess_rewrite' if name == '.htaccess' else 'php_front_controller'
                info['unread_handlers'].append({'kind': kind, 'file': rel, 'line': line_of(text, m.start()),
                                                'reason': 'routes in PHP or Apache rewrites are not read by this tool'})
            continue
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
        found, extras = python_handlers(py_files, host_files, wanted)
        info['middleware'] = extras['middleware']
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


def catch_prefix(h: dict) -> str | None:
    """The literal part before a catch-all parameter ({name:path}, a mount's {file}): `/relay/` for /relay/{rest}."""
    c = h.get('catch_all')
    if not c or '{' + c + '}' not in h['path']:
        return None
    return h['path'][:h['path'].index('{' + c + '}')]


def compare(contract: dict, handlers: list[dict], prefix: str) -> list[dict]:
    issues = []
    by_shape: dict[str, list[dict]] = {}
    for h in handlers:
        by_shape.setdefault(shape(h['path']), []).append(h)
    catch_alls = [h for h in handlers if catch_prefix(h) is not None]
    documented = set()
    documented_full: list[tuple[str, str]] = []
    for op in contract['operations']:
        full = prefix + op['path']
        hs = by_shape.get(shape(full), [])
        documented.add((op['method'], shape(full)))
        documented_full.append((op['method'], full))
        if not hs:
            # a catch-all route or a mount (StaticFiles) answers every path under its prefix
            hs = [h for h in catch_alls if full.startswith(catch_prefix(h)) and len(full) > len(catch_prefix(h).rstrip('/'))]
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
            if h.get('kind') == 'mount' or (h.get('catch_all') and shape(h['path']) != shape(full)):
                continue          # matched by prefix: the catch-all's own parameter name says nothing about these segments
            hn = re.findall(r'\{([^}]+)\}', h['path'])
            if hn != names and (h['method'] == op['method'] or h['method'] == '*'):
                issues.append({'kind': 'parameter_name_mismatch', 'method': op['method'], 'path': op['path'],
                               'operation_id': op['operation_id'], 'file': h['file'], 'line': h['line'],
                               'message': f'the contract names the path parameters {names}, the handler {hn}'})
                break
    for h in handlers:
        sh = shape(h['path'])
        cp = catch_prefix(h)
        if cp is not None and any(full.startswith(cp) and (h['method'] in (m, '*') or (m == 'HEAD' and h['method'] == 'GET'))
                                  for m, full in documented_full):
            continue              # something under the catch-all or mount is documented
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
    info: dict = {}
    fws, handlers, scanned = detect(repo, frameworks, exclude, info)
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
        skeleton_out.write_text(json.dumps(skeleton(handlers, repo, project_name(repo), info['middleware']), indent=2) + '\n',
                                encoding='utf-8')
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
           'operations': len(contract['operations']) if contract else 0, 'counts': counts, 'issues': issues, 'note': note,
           'mounts': sum(h.get('kind') == 'mount' for h in handlers),
           'conditional_handlers': sum(bool(h.get('conditional')) for h in handlers),
           'middleware': [f"{m['name']} ({m['file']}:{m['line']})" for m in info['middleware']],
           'unread_languages': info['unread_languages'], 'unread_handlers': info['unread_handlers'],
           'pruned_dirs': info['pruned'][:200], 'pruned_dirs_total': len(info['pruned'])}
    if info['unread_handlers'] or info['unread_languages']:
        langs = ', '.join(f'{k} ({v} file(s))' for k, v in sorted(info['unread_languages'].items()))
        out['language_coverage_note'] = (f'not read by this tool: {langs or "rewrite rules"}; '
                                         f'{len(info["unread_handlers"])} unread handler file(s) may serve routes the comparison '
                                         'cannot see (a missing_handler there may be served, an undocumented route there is not listed)')
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
    if res.get('language_coverage_note'):
        print(f'warning: {res["language_coverage_note"]}', file=sys.stderr)
        for u in res['unread_handlers'][:10]:
            print(f'  unread {u["kind"]}: {u["file"]}:{u["line"]}', file=sys.stderr)
    if res['pruned_dirs_total']:
        print(f'note: {res["pruned_dirs_total"]} director(ies) not entered (build output, dependencies, caches or --exclude-dir): '
              f'{", ".join(res["pruned_dirs"][:8])}{" ..." if res["pruned_dirs_total"] > 8 else ""}; listed in pruned_dirs',
              file=sys.stderr)
    if a.skeleton:
        print(f'derived contract (x-contract-provenance: derived) -> {a.skeleton}; fill in the response schemas, and say in the '
              'report that the contract was derived from the code', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
