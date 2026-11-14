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
    checks scope.contract.provenance against contract.json.
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


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        handler_map_without_contract(tmp)
        sender_and_merge(tmp)
        validator_rules(tmp)
    r = run(str(HERE / 'handler_map.py'), '--self-test')
    check(r.returncode == 0, f'handler_map.py --self-test ({r.stdout.strip()[-120:]})')
    print(f"self-test {'passed' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
