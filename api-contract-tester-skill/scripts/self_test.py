#!/usr/bin/env python3
"""Self-test for the API contract tester's scripts (no network beyond a loopback server it starts itself).

    python self_test.py

Checks:
  * handler_map.py without a contract lists the handlers and, with --skeleton, writes a derived OpenAPI
    contract (paths, methods, path-parameter types from converters, query parameters from
    request.args.get, JSON or HTML responses) marked `x-contract-provenance: derived`; --only keeps one
    path prefix; load_contract.py carries the provenance into contract.json;
  * send_cases.py records each response's size and the items in a collection body, reads a large body
    only up to --max-response-bytes, fails response_size above it, and does not call a body it could
    not read whole invalid JSON;
  * merge_findings.py marks schema_conformance cantTell when every 2xx body of a collection operation
    was empty (a vacuous pass), records the largest response, and makes a response_size candidate;
  * validate_report.py caps overall confidence at medium for a derived or generated contract and
    checks scope.contract.provenance against contract.json;
  * on a FastAPI site: the skeleton's media types and error statuses, StaticFiles mounts, {name:path}
    catch-alls, conditional routers, middleware, form bodies and unread PHP; send_cases.py's schema
    check by media type, catch-all paths, Accept, Basic auth and the probe's credentials; gen_cases.py's
    path parameters; merge sketches; skipped writes in evidence completeness; --write-summary on an
    empty draft field; FAIL* in the rendered operations table.
Writes only to a temporary directory; exit code 0 when every check passes.
"""
from __future__ import annotations
import json, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True)


FLASK_APP = '''from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


@app.route("/api/ferries")
def ferries():
    page = request.args.get("page", 1, type=int)
    port = request.args.get("port", "")
    return jsonify([])


@app.route("/api/ferries/<int:ferry_id>")
def ferry(ferry_id):
    return jsonify({"id": ferry_id})


@app.post("/api/bookings")
def book():
    return jsonify({}), 201


@app.route("/timetable")
def timetable():
    return render_template("timetable.html")
'''


def handler_map_without_contract(tmp: Path) -> Path | None:
    print('handler_map.py without a contract')
    repo = tmp / 'repo'
    repo.mkdir()
    (repo / 'app.py').write_text(FLASK_APP, encoding='utf-8')
    out, skel = tmp / 'handlers.json', tmp / 'openapi.derived.json'
    r = run(str(HERE / 'handler_map.py'), str(repo), '--out', str(out), '--skeleton', str(skel))
    check(r.returncode == 0 and out.is_file(), f'runs with no --contract ({r.stderr.strip()[-200:]})')
    if r.returncode != 0 or not skel.is_file():
        check(False, 'writes a derived contract with --skeleton')
        return None
    h = json.loads(out.read_text(encoding='utf-8'))
    check(h.get('contract_sha256') is None and h['issues'] == [] and len(h['handlers']) == 4,
          f"lists the handlers and compares nothing ({len(h['handlers'])} handlers, note {h.get('note')!r})")
    d = json.loads(skel.read_text(encoding='utf-8'))
    check(d.get('openapi', '').startswith('3.1') and d.get('x-contract-provenance') == 'derived',
          'the skeleton is OpenAPI 3.1 marked x-contract-provenance: derived')
    paths = d.get('paths') or {}
    lst = (paths.get('/api/ferries') or {}).get('get') or {}
    params = {p['name']: (p['in'], (p.get('schema') or {}).get('type')) for p in lst.get('parameters') or []}
    check(params == {'page': ('query', 'integer'), 'port': ('query', 'string')},
          f'query parameters from request.args.get, typed by type= ({params})')
    one = (paths.get('/api/ferries/{ferry_id}') or {}).get('get') or {}
    pp = [(p['name'], p['in'], p.get('required'), (p.get('schema') or {}).get('type')) for p in one.get('parameters') or []]
    check(pp == [('ferry_id', 'path', True, 'integer')], f'<int:ferry_id> is an integer path parameter ({pp})')
    post = (paths.get('/api/bookings') or {}).get('post') or {}
    check('201' in (post.get('responses') or {}), f"jsonify(...), 201 documents a 201 ({list((post.get('responses') or {}))})")
    page = ((paths.get('/timetable') or {}).get('get') or {}).get('responses') or {}
    check('text/html' in ((page.get('200') or {}).get('content') or {}), f'render_template answers HTML ({page})')
    r = run(str(HERE / 'handler_map.py'), str(repo), '--out', str(tmp / 'h2.json'), '--only', '/api/')
    h2 = json.loads((tmp / 'h2.json').read_text(encoding='utf-8')) if r.returncode == 0 else {'handlers': []}
    check(r.returncode == 0 and sorted({x['path'] for x in h2['handlers']}) == ['/api/bookings', '/api/ferries', '/api/ferries/{ferry_id}'],
          f"--only /api/ keeps the API surface ({sorted({x['path'] for x in h2['handlers']})})")
    cj = tmp / 'contract.json'
    r = run(str(HERE / 'load_contract.py'), str(skel), '--out', str(cj))
    c = json.loads(cj.read_text(encoding='utf-8')) if cj.is_file() else {}
    check(r.returncode == 0 and c.get('provenance') == 'derived', f"load_contract records provenance derived ({c.get('provenance')})")
    return cj


class Api(BaseHTTPRequestHandler):
    BIG = json.dumps([{'id': n, 'name': 'x' * 40} for n in range(40000)]).encode()

    def do_GET(self):  # noqa: N802
        body = b'[]' if self.path.startswith('/items') else self.BIG if self.path.startswith('/big') else b'{}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *a):
        pass


CONTRACT = {'openapi': '3.1.0', 'info': {'title': 'Ferry API', 'version': '1.0.0'}, 'paths': {
    '/items': {'get': {'operationId': 'listItems', 'responses': {'200': {'description': 'ok', 'content': {'application/json': {
        'schema': {'type': 'array', 'items': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'integer'}}}}}}}}}},
    '/big': {'get': {'operationId': 'bigList', 'responses': {'200': {'description': 'ok', 'content': {'application/json': {
        'schema': {'type': 'array', 'items': {'type': 'object'}}}}}}}}}}


def sender_and_merge(tmp: Path) -> None:
    print('send_cases.py and merge_findings.py (response size, vacuous passes)')
    spec = tmp / 'ferry.json'
    spec.write_text(json.dumps(CONTRACT), encoding='utf-8')
    cj = tmp / 'ferry.contract.json'
    r = run(str(HERE / 'load_contract.py'), str(spec), '--out', str(cj))
    if r.returncode != 0:
        check(False, f'load_contract on the test contract ({r.stderr[-200:]})')
        return
    contract = json.loads(cj.read_text(encoding='utf-8'))
    req = {'method': 'GET', 'path_params': {}, 'query': {}, 'headers': {}}
    cases = {'generator': 'stdlib', 'seed': 1, 'contract_sha256': contract['sha256'], 'cases': [
        {'id': 'listItems-001', 'operation_id': 'listItems', 'kind': 'example', 'valid': True, 'description': 'example', 'auth': True, 'request': req},
        {'id': 'listItems-002', 'operation_id': 'listItems', 'kind': 'random', 'valid': True, 'description': 'random', 'auth': True, 'request': req},
        {'id': 'bigList-001', 'operation_id': 'bigList', 'kind': 'example', 'valid': True, 'description': 'example', 'auth': True, 'request': req}],
        'plan': [{'operation_id': 'listItems', 'status': 'planned', 'reason': None}, {'operation_id': 'bigList', 'status': 'planned', 'reason': None}]}
    cf = tmp / 'cases.json'
    cf.write_text(json.dumps(cases), encoding='utf-8')
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Api)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        rj = tmp / 'run' / 'run.json'
        r = run(str(HERE / 'send_cases.py'), str(cj), str(cf), '--base-url', f'http://127.0.0.1:{srv.server_address[1]}',
                '--out', str(rj), '--rate', '0', '--probe', '/items', '--max-response-bytes', '1000000')
    finally:
        srv.shutdown()
    if r.returncode != 0 or not rj.is_file():
        check(False, f'send_cases runs ({r.stderr.strip()[-300:]})')
        return
    res = {x['case_id']: x for x in json.loads(rj.read_text(encoding='utf-8'))['results']}
    it, big = res['listItems-001'], res['bigList-001']
    check(it.get('body_bytes') == 2 and it.get('items') == 0, f"size and item count recorded ({it.get('body_bytes')}, {it.get('items')})")
    check(big.get('body_bytes') == len(Api.BIG), f"the real size of a large body is recorded ({big.get('body_bytes')} of {len(Api.BIG)})")
    rs = (big['checks'].get('response_size') or {}).get('result')
    check(rs == 'failed', f'response_size fails above --max-response-bytes ({rs})')
    sc = big['checks']['schema_conformance']['result']
    check(sc != 'failed', f'a body too large to read whole is not called invalid JSON ({sc})')
    cand = tmp / 'run' / 'candidates.json'
    r = run(str(HERE / 'merge_findings.py'), str(cj), str(cf), str(rj), '--out', str(cand))
    if r.returncode != 0:
        check(False, f'merge_findings runs ({r.stderr[-300:]})')
        return
    m = json.loads(cand.read_text(encoding='utf-8'))
    ops = {o['operation_id']: o for o in m['operations']}
    check(ops['listItems']['checks']['schema_conformance'] == 'cantTell',
          f"every 2xx body was an empty collection: schema_conformance is cantTell, not passed ({ops['listItems']['checks']['schema_conformance']})")
    check(any('listItems' in v.get('operation_id', '') for v in m.get('vacuous_passes') or []),
          f"the vacuous pass is listed with its reason ({m.get('vacuous_passes')})")
    check(ops['bigList'].get('largest_response_bytes') == len(Api.BIG), f"largest response per operation ({ops['bigList'].get('largest_response_bytes')})")
    rc = [c for c in m['candidates'] if c['check'] == 'response_size']
    check(len(rc) == 1 and rc[0]['operation_ids'] == ['bigList'] and rc[0]['severity'] == 'Medium',
          f"one response_size candidate, Medium ({[(c['check'], c['operation_ids'], c['severity']) for c in m['candidates']]})")


def validator_rules(tmp: Path) -> None:
    print('validate_report.py (contract provenance)')
    import validate_report as v
    ops = [{'operation_id': 'a', 'status': 'exercised', 'checks': {'schema_conformance': 'passed'}}]
    cov = {'total': 1, 'exercised': 1, 'excluded': 0, 'skipped_write': 0, 'skipped_auth': 0, 'untested': 0}
    rep = {'findings': [], 'metadata': {'tools': []}}
    run_ = {'status': 'complete'}
    try:
        plain = v.headline_expected(rep, ops, cov, [], run_, False)
        derived = v.headline_expected(rep, ops, cov, [], run_, False, provenance='derived')
        generated = v.headline_expected(rep, ops, cov, [], run_, False, provenance='generated')
    except TypeError as e:
        check(False, f'headline_expected takes the contract provenance ({e})')
        return
    check(plain['overall_confidence'] == 'high' and derived['overall_confidence'] == 'medium'
          and generated['overall_confidence'] == 'medium',
          'a contract derived from or generated by the code caps overall confidence at medium')
    base = HERE.parent / 'examples'
    contract = json.loads((base / 'run' / 'contract.json').read_text(encoding='utf-8'))
    contract['provenance'] = 'derived'
    cp = tmp / 'contract.derived.json'
    cp.write_text(json.dumps(contract), encoding='utf-8')
    args = ['--cases', str(base / 'run' / 'cases.json'), '--diff', str(base / 'run' / 'diff.json'), '--repo', str(base / 'clinic-api')]
    r = run(str(HERE / 'validate_report.py'), str(base / 'report.static.example.json'), '--contract', str(cp), *args)
    check(r.returncode != 0 and 'provenance' in r.stdout, f'a report that omits a derived provenance is rejected ({r.stdout.strip()[-200:].replace(chr(10), ' | ')})')
    rep = json.loads((base / 'report.static.example.json').read_text(encoding='utf-8'))
    rep['scope']['contract']['provenance'] = 'derived'
    rp = tmp / 'report.derived.json'
    rp.write_text(json.dumps(rep), encoding='utf-8')
    r = run(str(HERE / 'validate_report.py'), str(rp), '--contract', str(cp), *args)
    check(r.returncode != 0 and 'derived' in r.stdout and 'limitations' in r.stdout,
          f'a derived contract must be named in limitations ({r.stdout.strip()[-200:].replace(chr(10), ' | ')})')
    rep['limitations'].append('The contract was derived from the handlers (handler_map.py --skeleton), so request bounds and '
                              'response shapes come from the same code: most checks could not fail.')
    rp.write_text(json.dumps(rep), encoding='utf-8')
    r = run(str(HERE / 'validate_report.py'), str(rp), '--contract', str(cp), *args)
    check(r.returncode == 0, f'and then validates ({r.stdout.strip()[-300:].replace(chr(10), ' | ')})')


# A FastAPI app that serves a committed front end from a StaticFiles mount, answers HTML, files and a catch-all
# proxy, raises HTTP errors, includes a router only when a flag is set, and adds an auth middleware. A PHP front
# controller and an .htaccess rewrite stand beside it as the alternative server.
FASTAPI_SITE = '''from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from relay import forward
import lockers_admin
from guard import BasicGuard

app = FastAPI()
app.add_middleware(BasicGuard, realm="lockers")
SHOW_ADMIN = False

if SHOW_ADMIN:
    app.include_router(lockers_admin.router)


@app.get("/lockers/{locker_id}")
def locker(locker_id: int):
    if locker_id > 99:
        raise HTTPException(status_code=404, detail="no such locker")
    return {"id": locker_id}


@app.get("/status", response_class=HTMLResponse)
def status_page():
    return "<p>ok</p>"


@app.get("/home")
def home():
    return Response("<html></html>", media_type="text/html")


@app.get("/labels/{label_id}")
def label(label_id: str):
    if label_id == "none":
        return JSONResponse(status_code=404, content={"error": "missing"})
    if label_id == "gone":
        return Response(status_code=status.HTTP_410_GONE)
    return FileResponse("labels/a.pdf")


@app.api_route("/relay/{rest:path}", methods=["GET", "DELETE"])
async def relay(rest: str):
    if cached := forward.cached(rest):
        return cached
    return await forward(rest)


@app.post("/parcels/upload")
async def upload(note: str = Form(...), blob: UploadFile = File(...)):
    return {"ok": True}


app.mount("/", StaticFiles(directory="dist", html=True), name="site")
'''
ADMIN_ROUTER = '''from fastapi import APIRouter

router = APIRouter(prefix="/admin")


@router.get("/stats")
def stats():
    return {}
'''
PHP_FRONT = '''<?php
$uri = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
if (preg_match('#^/relay/(.*)$#', $uri, $m)) { forward($m[1]); exit; }
readfile(__DIR__ . '/dist/index.html');
'''


def handler_map_fastapi_site(tmp: Path) -> None:
    print('handler_map.py on a FastAPI site (media types, error statuses, mounts, catch-alls, conditional routers, PHP)')
    repo = tmp / 'site'
    (repo / 'dist').mkdir(parents=True)
    (repo / 'main.py').write_text(FASTAPI_SITE, encoding='utf-8')
    (repo / 'lockers_admin.py').write_text(ADMIN_ROUTER, encoding='utf-8')
    (repo / 'index.php').write_text(PHP_FRONT, encoding='utf-8')
    (repo / '.htaccess').write_text('RewriteEngine On\nRewriteRule ^ index.php [L]\n', encoding='utf-8')
    (repo / 'dist' / 'index.html').write_text('<!doctype html><title>Lockers</title>\n', encoding='utf-8')
    out, skel = tmp / 'site.handlers.json', tmp / 'site.derived.json'
    r = run(str(HERE / 'handler_map.py'), str(repo), '--out', str(out), '--skeleton', str(skel))
    if r.returncode != 0 or not skel.is_file():
        check(False, f'handler_map runs on the FastAPI site ({r.stderr.strip()[-300:]})')
        return
    h = json.loads(out.read_text(encoding='utf-8'))
    d = json.loads(skel.read_text(encoding='utf-8'))
    paths = d.get('paths') or {}

    def resp(path, method='get'):
        return ((paths.get(path) or {}).get(method) or {}).get('responses') or {}

    def types(path, status='200', method='get'):
        return sorted(((resp(path, method).get(status) or {}).get('content') or {}))

    check(types('/home') == ['text/html'], f'skeleton_media_type_keyword: Response(media_type="text/html") is text/html ({types("/home")})')
    check(types('/status') == ['text/html'], f'skeleton_response_class: response_class=HTMLResponse is text/html ({types("/status")})')
    check(types('/relay/{rest}') == ['*/*'],
          f'skeleton_unknown_call: a return of an unknown call is not documented as JSON ({types("/relay/{rest}")})')
    check('application/json' not in types('/labels/{label_id}') and types('/labels/{label_id}'),
          f'skeleton_file_response: FileResponse is not JSON ({types("/labels/{label_id}")})')
    check('404' in resp('/lockers/{locker_id}'), f'skeleton_http_exception: raise HTTPException(status_code=404) documents a 404 '
                                                 f'({sorted(resp("/lockers/{locker_id}"))})')
    lr = resp('/labels/{label_id}')
    check('404' in lr and '410' in lr, f'skeleton_error_status_keyword: JSONResponse(status_code=404) and '
                                       f'Response(status_code=status.HTTP_410_GONE) are documented ({sorted(lr)})')
    rest = next((p for p in ((paths.get('/relay/{rest}') or {}).get('get') or {}).get('parameters') or [] if p['name'] == 'rest'), {})
    check(rest.get('x-path-allows-slash') is True, f'skeleton_catch_all: {{rest:path}} is marked x-path-allows-slash ({rest})')
    rel = [x for x in h['handlers'] if x['path'].startswith('/relay')]
    check(rel and all(x.get('catch_all') == 'rest' for x in rel), f'handlers_catch_all: the handler records its catch-all parameter ({rel})')
    mounts = [x for x in h['handlers'] if x.get('kind') == 'mount']
    check(len(mounts) == 1 and mounts[0].get('mount_app') == 'StaticFiles' and mounts[0]['line'] > 1,
          f'handlers_static_mount: app.mount("/", StaticFiles(...)) is a mount handler ({mounts})')
    admin = [x for x in h['handlers'] if x['path'] == '/admin/stats']
    check(admin and all(x.get('conditional') for x in admin), f'handlers_conditional_router: an include_router under `if` is '
                                                               f'conditional ({admin})')
    check('x-conditional' in ((paths.get('/admin/stats') or {}).get('get') or {}),
          'skeleton_conditional: the derived operation says it exists only under a condition')
    unread = h.get('unread_handlers') or []
    check({u.get('kind') for u in unread} >= {'php_front_controller', 'htaccess_rewrite'} and 'PHP' in (h.get('unread_languages') or {}),
          f'handlers_php_unread: the PHP front controller and .htaccess rewrite are listed as unread ({unread}, {h.get("unread_languages")})')
    check('PHP' in r.stderr, f'handlers_php_warning: a warning names PHP on stderr ({r.stderr.strip()[-200:]})')
    up = ((paths.get('/parcels/upload') or {}).get('post') or {}).get('requestBody') or {}
    check('multipart/form-data' in (up.get('content') or {}), f'skeleton_form_body: File()/Form() parameters give a multipart body ({up})')
    check('basicAuth' in ((d.get('components') or {}).get('securitySchemes') or {}) or 'BasicGuard' in json.dumps(d.get('x-middleware')),
          f'skeleton_middleware: the auth middleware is recorded ({d.get("components")}, {d.get("x-middleware")})')
    rid = {m: o.get('operationId') for m, o in (paths.get('/relay/{rest}') or {}).items()}
    check(rid == {'get': 'relay_get', 'delete': 'relay_delete'}, f'skeleton_operation_ids: every method of a multi-method handler is suffixed ({rid})')
    # compare: documented pages under the static mount are not missing handlers
    cj = {'sha256': '0' * 64, 'kind': 'openapi', 'root': {}, 'operations': [
        {'operation_id': 'page', 'method': 'GET', 'path': '/index.html'},
        {'operation_id': 'asset', 'method': 'GET', 'path': '/{asset}'},
        {'operation_id': 'relayFile', 'method': 'GET', 'path': '/relay/{rest}'}]}
    cp = tmp / 'site.contract.json'
    cp.write_text(json.dumps(cj), encoding='utf-8')
    r = run(str(HERE / 'handler_map.py'), str(repo), '--contract', str(cp), '--out', str(tmp / 'site.cmp.json'))
    cmp_ = json.loads((tmp / 'site.cmp.json').read_text(encoding='utf-8')) if r.returncode == 0 else {'issues': [{'kind': r.stderr[-200:]}]}
    miss = [i for i in cmp_['issues'] if i['kind'] == 'missing_handler']
    check(not miss, f'compare_static_mount: paths served by the StaticFiles mount are not missing_handler ({miss})')


def sender_media_and_auth(tmp: Path) -> None:
    print('send_cases.py (schema check by media type, catch-all paths, Basic auth, probe credentials, Accept)')
    import send_cases as sc
    op = {'operation_id': 'home', 'method': 'GET', 'path': '/', 'parameters': [], 'responses': {
        '200': {'content': {'text/html': {'type': 'string'}}, 'headers': {}},
        '404': {'content': {'application/problem+json': {'type': 'object', 'required': ['title']}}, 'headers': {}}}}
    contract = {'root': {}, 'dialect': '3.1'}
    case = {'valid': True, 'kind': 'example', 'auth': True, 'description': 'x', 'request': {'method': 'GET'}}
    res = sc.check_rest(case, op, contract, 200, {'Content-Type': 'text/html; charset=utf-8'}, b'<!doctype html><p>hi</p>')
    check(res['schema_conformance']['result'] != 'failed',
          f"schema_text_html: an HTML page documented text/html {{type: string}} is not parsed as JSON ({res['schema_conformance']})")
    res = sc.check_rest(case, op, contract, 404, {'Content-Type': 'application/problem+json'}, b'{"status": 404}')
    check(res['schema_conformance']['result'] == 'failed',
          f"schema_plus_json: an application/problem+json body is still checked as JSON ({res['schema_conformance']})")
    bin_op = {'operation_id': 'file', 'method': 'GET', 'path': '/f', 'parameters': [], 'responses': {
        '200': {'content': {'application/octet-stream': {'type': 'string', 'format': 'binary'}}, 'headers': {}}}}
    res = sc.check_rest(case, bin_op, contract, 200, {'Content-Type': 'application/octet-stream'}, b'\x00\x01\xff')
    check(res['schema_conformance']['result'] != 'failed', f"schema_binary: a binary body is not parsed as JSON ({res['schema_conformance']})")
    cop = {'operation_id': 'relay', 'method': 'GET', 'path': '/relay/{rest}',
           'parameters': [{'name': 'rest', 'in': 'path', 'required': True, 'schema': {'type': 'string'}, 'allow_slash': True}]}
    url = sc.build_url('http://127.0.0.1:1', {'request': {'path_params': {'rest': 'docs/read me.txt'}, 'query': {}}}, cop, '/graphql')
    check(url == 'http://127.0.0.1:1/relay/docs/read%20me.txt', f'path_allows_slash: a catch-all value keeps its slashes ({url})')
    sec = sc.accept_header({'responses': {'200': {'content': {'text/html': {}}, 'headers': {}}, '404': {'content': {}, 'headers': {}}}}) \
        if hasattr(sc, 'accept_header') else 'application/json'
    check(sec == 'text/html', f'accept_documented: Accept names the documented media types ({sec})')

    class Guarded(BaseHTTPRequestHandler):
        seen: list = []

        def do_GET(self):  # noqa: N802
            Guarded.seen.append(self.headers.get('Authorization'))
            ok = self.headers.get('Authorization') == 'Basic ZGVtbzpub3QtYS1zZWNyZXQ='   # demo:not-a-secret
            body = b'{}' if ok else b'{"detail": "auth"}'
            self.send_response(200 if ok else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    spec = {'openapi': '3.1.0', 'info': {'title': 'Lockers', 'version': '1'}, 'paths': {'/lockers': {'get': {
        'operationId': 'listLockers', 'responses': {'200': {'description': 'ok', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                                                     '401': {'description': 'no'}}}}}}
    sp = tmp / 'lockers.json'
    sp.write_text(json.dumps(spec), encoding='utf-8')
    cj = tmp / 'lockers.contract.json'
    run(str(HERE / 'load_contract.py'), str(sp), '--out', str(cj))
    contract = json.loads(cj.read_text(encoding='utf-8'))
    cases = {'generator': 'stdlib', 'seed': 1, 'contract_sha256': contract['sha256'], 'plan': [], 'cases': [
        {'id': 'listLockers-001', 'operation_id': 'listLockers', 'kind': 'example', 'valid': True, 'description': 'x', 'auth': True,
         'request': {'method': 'GET', 'path_params': {}, 'query': {}, 'headers': {}}}]}
    cf = tmp / 'lockers.cases.json'
    cf.write_text(json.dumps(cases), encoding='utf-8')
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Guarded)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import os
    env = dict(os.environ, LK_USER='demo', LK_PASS='not-a-secret')
    try:
        rj = tmp / 'lockers-run' / 'run.json'
        r = subprocess.run([sys.executable, str(HERE / 'send_cases.py'), str(cj), str(cf), '--base-url',
                            f'http://127.0.0.1:{srv.server_address[1]}', '--out', str(rj), '--rate', '0',
                            '--basic-env', 'LK_USER:LK_PASS', '--probe', '/lockers', '--probe-expect', '200'],
                           capture_output=True, text=True, env=env)
    finally:
        srv.shutdown()
    rd = json.loads(rj.read_text(encoding='utf-8')) if rj.is_file() else {}
    check(r.returncode == 0 and rd.get('status') == 'complete', f'basic_auth: --basic-env USER:PASS sends HTTP Basic credentials '
                                                                f'({rd.get("status")}, {r.stderr.strip()[-200:]})')
    check(bool(Guarded.seen) and Guarded.seen[0] is not None, f'probe_credentials: the liveness probe carries the credentials ({Guarded.seen[:1]})')
    txt = rj.read_text(encoding='utf-8') if rj.is_file() else ''
    check('not-a-secret' not in txt and 'ZGVtbzpub3QtYS1zZWNyZXQ=' not in txt, 'basic_auth_redacted: neither the password nor the token is written')


def generator_paths(tmp: Path) -> None:
    print('gen_cases.py (path parameters)')
    import gen_cases as gc, random
    op = {'operation_id': 'asset', 'method': 'GET', 'path': '/{asset}', 'security': [], 'request_body': None, 'write': False,
          'parameters': [{'name': 'asset', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'minLength': 1}, 'examples': ['a.js']}]}
    cases = gc.Builder(op, {}, random.Random(1)).build(20, True)
    empty = [c for c in cases if c['request']['path_params'].get('asset') == '']
    check(not empty, f'gen_no_empty_path: no case sends an empty path parameter (another route) ({[c["description"] for c in empty]})')
    op2 = {'operation_id': 'tok', 'method': 'GET', 'path': '/t', 'security': [], 'request_body': None, 'write': False,
           'parameters': [{'name': 'code', 'in': 'query', 'required': True, 'schema': {'type': 'string', 'minLength': 5, 'maxLength': 5},
                           'examples': ['abcde']}]}
    cases = gc.Builder(op2, {}, random.Random(1)).build(20, True)
    reqs = [json.dumps(c['request'], sort_keys=True) for c in cases if c['kind'] == 'boundary']
    check(len(reqs) == len(set(reqs)), f'gen_boundary_dedupe: minLength == maxLength gives one boundary case, not two ({len(reqs)})')
    op3 = {'operation_id': 'relay', 'method': 'GET', 'path': '/relay/{rest}', 'security': [], 'request_body': None, 'write': False,
           'parameters': [{'name': 'rest', 'in': 'path', 'required': True, 'schema': {'type': 'string'}, 'examples': ['a.txt'],
                           'allow_slash': True}]}
    cases = gc.Builder(op3, {}, random.Random(1)).build(20, True)
    check(any('/' in c['request']['path_params'].get('rest', '') for c in cases),
          f'gen_multi_segment: a catch-all path parameter gets a multi-segment value ({[c["request"]["path_params"] for c in cases]})')
    lc = tmp / 'catch.contract.json'
    spec = {'openapi': '3.1.0', 'info': {'title': 't', 'version': '1'}, 'paths': {'/relay/{rest}': {'get': {
        'operationId': 'relay', 'parameters': [{'name': 'rest', 'in': 'path', 'required': True, 'x-path-allows-slash': True,
                                                'schema': {'type': 'string'}}],
        'responses': {'200': {'description': 'ok', 'content': {'*/*': {}}}, 'options': None}}}}}
    del spec['paths']['/relay/{rest}']['get']['responses']['options']
    spec['paths']['/relay/{rest}']['options'] = {'operationId': 'relayPreflight', 'responses': {'200': {'description': 'preflight'}}}
    sp = tmp / 'catch.json'
    sp.write_text(json.dumps(spec), encoding='utf-8')
    r = run(str(HERE / 'load_contract.py'), str(sp), '--out', str(lc))
    c = json.loads(lc.read_text(encoding='utf-8')) if lc.is_file() else {'operations': [], 'issues': []}
    p = next((p for o in c['operations'] for p in o['parameters'] if p['name'] == 'rest'), {})
    check(p.get('allow_slash') is True, f'load_allow_slash: x-path-allows-slash reaches contract.json ({p})')
    noisy = [i for i in c.get('issues') or [] if i['kind'] == 'missing_response_schema']
    check(not noisy, f'load_no_binary_noise: */* bodies and OPTIONS preflights are not missing_response_schema ({noisy})')


def merge_sketches(tmp: Path) -> None:
    print('merge_findings.py (regression sketches)')
    import merge_findings as mf
    ops = {'home': {'operation_id': 'home', 'responses': {'200': {'content': {'text/plain': {}}, 'headers': {}}}},
           'nobody': {'operation_id': 'nobody', 'responses': {'401': {'content': {}, 'headers': {}}}}}
    ex = {'e.json': {'request': {'method': 'GET', 'url': 'http://127.0.0.1:1/'}, 'sent_with_credentials': True},
          'n.json': {'request': {'method': 'GET', 'url': 'http://127.0.0.1:1/n'}, 'sent_with_credentials': False}}
    inst = {'operation_id': 'home', 'exchange': 'e.json', 'repeated': True, 'status': 200}
    s = mf.sketch({'check': 'content_type', 'cause': 'status 200: Content-Type application/json', 'instances': [inst]}, ops, ex, {})
    check("'application/json'" not in s and 'text/plain' in s, f'sketch_content_type: asserts the documented media type ({s.splitlines()[-1]})')
    ninst = {'operation_id': 'nobody', 'exchange': 'n.json', 'repeated': True, 'status': 401}
    s = mf.sketch({'check': 'content_type', 'cause': 'status 401: a application/json body was returned but 401 documents no content',
                   'instances': [ninst]}, ops, ex, {})
    check('application/json' not in s.splitlines()[-1], f'sketch_no_body: a response documented with no body is not asserted JSON ({s.splitlines()[-1]})')
    s = mf.sketch({'check': 'required_headers', 'cause': 'status 200: missing X-One, X-Two', 'instances': [inst]}, ops, ex, {})
    asserts = [x for x in s.splitlines() if x.strip().startswith('assert')]
    check(len(asserts) == 2 and "'X-One'" in asserts[0] and "'X-Two'" in asserts[1],
          f'sketch_headers: one assert per missing header ({asserts})')


def validator_round8(tmp: Path) -> None:
    print('validate_report.py (skipped writes, write-summary)')
    import validate_report as v
    ops = [{'operation_id': 'a', 'status': 'exercised', 'checks': {'schema_conformance': 'passed'}}]
    cov = {'total': 2, 'exercised': 1, 'excluded': 0, 'skipped_write': 1, 'skipped_auth': 0, 'untested': 0}
    hl = v.headline_expected({'findings': [], 'metadata': {'tools': []}}, ops, cov, [], {'status': 'complete'}, False)
    check(hl['evidence_completeness'] == 'partial', f'evidence_skipped_write: a write operation skipped in safe mode makes the '
                                                    f'evidence partial ({hl["evidence_completeness"]})')
    base = HERE.parent / 'examples'
    rep = json.loads((base / 'report.static.example.json').read_text(encoding='utf-8'))
    rep['headline']['severity_counts'] = {}
    rp = tmp / 'report.draft.json'
    rp.write_text(json.dumps(rep), encoding='utf-8')
    r = run(str(HERE / 'validate_report.py'), str(rp), '--contract', str(base / 'run' / 'contract.json'), '--cases',
            str(base / 'run' / 'cases.json'), '--diff', str(base / 'run' / 'diff.json'), '--repo', str(base / 'clinic-api'), '--write-summary')
    filled = json.loads(rp.read_text(encoding='utf-8'))['headline']['severity_counts']
    check(r.returncode == 0 and filled == {'Critical': 0, 'High': 0, 'Medium': 0, 'Low': 0},
          f'write_summary_fills: --write-summary fills an empty severity_counts ({filled}, {r.stdout.strip()[-200:]})')


def render_triage(tmp: Path) -> None:
    print('render_report.py (operations table and triage)')
    base = HERE.parent / 'examples'
    rep = json.loads((base / 'report.example.json').read_text(encoding='utf-8'))
    covered = {(o, f['check']) for f in rep['findings'] for o in f['operation_ids']}
    target = next(((o['operation_id'], k) for o in rep['operations'] for k, x in o['checks'].items()
                   if x == 'failed' and (o['operation_id'], k) not in covered), None)
    if target is None:   # make one: a failed check with no finding behind it (its candidate dismissed)
        o = rep['operations'][0]
        o['checks']['required_headers'] = 'failed'
        target = (o['operation_id'], 'required_headers')
    rp = tmp / 'report.render.json'
    rp.write_text(json.dumps(rep), encoding='utf-8')
    r = run(str(HERE / 'render_report.py'), str(rp), '--color', 'never')
    row = next((x for x in r.stdout.splitlines() if f'({target[0]})' in x), '')
    check('FAIL*' in row, f'render_dismissed_fail: a failed check with no finding is marked FAIL* ({row.strip()[:160]})')


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        handler_map_without_contract(tmp)
        sender_and_merge(tmp)
        validator_rules(tmp)
        for fn in (handler_map_fastapi_site, sender_media_and_auth, generator_paths, merge_sketches, validator_round8, render_triage):
            try:
                fn(tmp)
            except Exception as e:  # a crash in one group is a failure, not the end of the self-test
                check(False, f'{fn.__name__} raised {type(e).__name__}: {e}')
    r = run(str(HERE / 'handler_map.py'), '--self-test')
    check(r.returncode == 0, f'handler_map.py --self-test ({r.stdout.strip()[-120:]})')
    print(f"self-test {'passed' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
