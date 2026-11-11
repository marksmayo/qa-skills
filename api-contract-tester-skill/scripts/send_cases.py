#!/usr/bin/env python3
"""Send generated cases to a test instance, check every response against the contract, and save the failing exchanges.

    python send_cases.py contract.json cases.json --base-url http://127.0.0.1:8140 --out run/run.json
        [--exchanges run/exchanges] [--rate 5] [--bearer-env NAME] [--api-key-env NAME --api-key-header X-Api-Key]
        [--authorised-by WHO] [--allow-writes --confirm-non-production] [--production-host HOST ...]
        [--probe /health] [--graphql-path /graphql] [--no-resend]

Safety (enforced here, re-checked by the validator):
  * A target that isn't loopback needs --authorised-by.
  * Only GET, HEAD and OPTIONS are sent unless --allow-writes, which also needs --authorised-by and
    --confirm-non-production. A host given with --production-host never receives a write, even then.
    GraphQL queries go over POST but are reads; mutations are writes.
  * Requests are spaced to --rate per second (default 5). A 429 is retried after Retry-After (at most
    60 s, three times); if it persists the run stops and is `incomplete`.
  * Before anything is written to disk, credentials are redacted: Authorization, Cookie, Set-Cookie,
    the API-key header or query parameter, any parameter named like token/secret/password/key/sig, and
    body fields the schema marks format: password or writeOnly. Every occurrence of a configured secret
    value is replaced too. Bodies over 2 MB are truncated, keeping the full body's SHA-256.

Checks per response: status_documented (4xx/3xx/2xx not in responses and no default; 5xx is left to
server_error), server_error, content_type, schema_conformance (passed/failed/cantTell), required_headers,
negative_accepted (an invalid request got 2xx), positive_rejected (a documented example got a 4xx other
than 401/403/429), auth_enforced (no credentials, 2xx), graphql_errors. Each failing case is sent once
more; a check that doesn't fail again is not repeatable (flaky).

Only exchanges with a failed check are saved, as exchanges/<case>.json and <case>.resend.json.
"""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, re, socket, sys, time, urllib.error, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonschema_lite as jl  # noqa: E402

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
MAX_BODY = 2 * 1024 * 1024
REDACTED = '[REDACTED]'
SENSITIVE_HEADERS = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key', 'api-key', 'x-auth-token'}
SENSITIVE_NAME = re.compile(r'(token|secret|passw|api[-_]?key|apikey|signature|^sig$|session)', re.I)
CHECKS = ['status_documented', 'server_error', 'content_type', 'schema_conformance', 'required_headers',
          'negative_accepted', 'positive_rejected', 'auth_enforced', 'graphql_errors']


def is_loopback(host: str) -> bool:
    if host in ('localhost',) or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        try:
            return all(ipaddress.ip_address(ai[4][0]).is_loopback for ai in socket.getaddrinfo(host, None))
        except OSError:
            return False


def is_write(case: dict) -> bool:
    req = case['request']
    if req.get('graphql'):
        return bool(re.match(r'^\s*mutation\b', (req.get('body') or {}).get('query', '')))
    return req['method'].upper() not in SAFE_METHODS


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Sender:
    def __init__(self, a, secrets: list[str]):
        self.a, self.secrets = a, [s for s in secrets if s]
        self.interval = 1.0 / a.rate if a.rate > 0 else 0
        self.last = 0.0
        self.sent = 0
        self.rate_events = 0
        self.backoff = 0.0
        self.stopped: str | None = None
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method: str, url: str, headers: dict, body: bytes | None):
        for attempt in range(4):
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            t0 = time.monotonic()
            self.sent += 1
            try:
                with self.opener.open(req, timeout=self.a.timeout) as r:
                    status, rh, raw = r.status, dict(r.headers.items()), r.read(MAX_BODY + 1)
            except urllib.error.HTTPError as e:
                status, rh, raw = e.code, dict(e.headers.items()), e.read(MAX_BODY + 1)
            elapsed = round((time.monotonic() - t0) * 1000)
            if status == 429 and attempt < 3:
                self.rate_events += 1
                delay = retry_after(rh.get('Retry-After'))
                self.backoff += delay
                time.sleep(delay)
                continue
            if status == 429:
                self.rate_events += 1
                self.stopped = 'rate limited: 429 persisted after 3 retries'
            return status, rh, raw, elapsed
        raise RuntimeError('unreachable')


def retry_after(v: str | None) -> float:
    if not v:
        return 1.0
    try:
        return min(60.0, max(0.0, float(v)))
    except ValueError:
        try:
            return min(60.0, max(0.0, parsedate_to_datetime(v).timestamp() - time.time()))
        except (TypeError, ValueError):
            return 1.0


def media(ct: str | None) -> str:
    return (ct or '').split(';')[0].strip().lower()


def media_matches(actual: str, documented: str) -> bool:
    documented = documented.lower()
    if documented in ('*/*', actual):
        return True
    if documented.endswith('/*'):
        return actual.split('/')[0] == documented.split('/')[0]
    return False


def find_response(op: dict, status: int):
    r = op['responses']
    for key in (str(status), f'{str(status)[0]}XX', f'{str(status)[0]}xx', 'default'):
        if key in r:
            return key, r[key]
    return None, None


def redact_schema_fields(value, schema, root, depth=0):
    """Replace fields marked format: password or writeOnly with [REDACTED]."""
    if depth > 8 or not isinstance(schema, dict):
        return value
    while '$ref' in schema:
        schema = jl.resolve(schema['$ref'], root)
    for sub in schema.get('allOf') or []:
        value = redact_schema_fields(value, sub, root, depth + 1)
    if isinstance(value, dict):
        props = schema.get('properties') or {}
        out = {}
        for k, v in value.items():
            ps = props.get(k) or {}
            while isinstance(ps, dict) and '$ref' in ps:
                ps = jl.resolve(ps['$ref'], root)
            if isinstance(ps, dict) and (ps.get('format') == 'password' or ps.get('writeOnly')):
                out[k] = REDACTED
            elif SENSITIVE_NAME.search(k) and isinstance(v, str):
                out[k] = REDACTED
            else:
                out[k] = redact_schema_fields(v, ps, root, depth + 1)
        return out
    if isinstance(value, list):
        return [redact_schema_fields(v, schema.get('items') or {}, root, depth + 1) for v in value]
    return value


def scrub(obj, secrets):
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, REDACTED)
        return obj
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, secrets) for v in obj]
    return obj


def redact_headers(h: dict, api_key_header: str | None) -> dict:
    out = {}
    for k, v in h.items():
        kl = k.lower()
        out[k] = REDACTED if kl in SENSITIVE_HEADERS or (api_key_header and kl == api_key_header.lower()) or SENSITIVE_NAME.search(kl) else v
    return out


def redact_url(url: str, api_key_query: str | None) -> str:
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    q = [(k, REDACTED if SENSITIVE_NAME.search(k) or (api_key_query and k == api_key_query) else v) for k, v in q]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q, safe='[]')))


def body_record(raw: bytes) -> dict:
    full_sha = hashlib.sha256(raw[:MAX_BODY + 1]).hexdigest()
    truncated = len(raw) > MAX_BODY
    raw = raw[:MAX_BODY]
    text = raw.decode('utf-8', errors='replace')
    try:
        return {'json': json.loads(text), 'sha256': full_sha, 'truncated': truncated, 'bytes': len(raw)}
    except ValueError:
        return {'text': text, 'sha256': full_sha, 'truncated': truncated, 'bytes': len(raw)}


# --- checks ----------------------------------------------------------------

def ref_at(schema, root: dict, path: str) -> str | None:
    """The last named component schema on the way to an error path like `$[2].clinician`, so the same
    fault in a shared schema is one cause across operations."""
    name = None
    node = schema
    for tok in re.findall(r'\[\d+\]|\.[^.\[]+', path[1:] if path.startswith('$') else path):
        while isinstance(node, dict) and '$ref' in node:
            name = node['$ref'].rsplit('/', 1)[-1]
            node = jl.resolve(node['$ref'], root)
        if not isinstance(node, dict):
            break
        node = (node.get('items') or {}) if tok.startswith('[') else ((node.get('properties') or {}).get(tok[1:]) or {})
    while isinstance(node, dict) and '$ref' in node:
        name = node['$ref'].rsplit('/', 1)[-1]
        node = jl.resolve(node['$ref'], root)
    return name


def check_rest(case: dict, op: dict, contract: dict, status: int, headers: dict, raw: bytes) -> dict:
    root, dialect = contract['root'], contract.get('dialect') or '3.1'
    res: dict[str, dict] = {}
    hl = {k.lower(): v for k, v in headers.items()}
    key, doc = find_response(op, status)
    ct = media(hl.get('content-type'))
    res['server_error'] = {'result': 'failed', 'detail': f'status {status}'} if status >= 500 else {'result': 'passed'}
    if status >= 500:
        res['status_documented'] = {'result': 'not_applicable', 'detail': '5xx is reported by server_error'}
    elif doc is None:
        res['status_documented'] = {'result': 'failed', 'detail': f'status {status} is not in responses {sorted(op["responses"])} and there is no default'}
    else:
        res['status_documented'] = {'result': 'passed', 'detail': f'matched {key}'}
    if doc is None:
        for c in ('content_type', 'schema_conformance', 'required_headers'):
            res[c] = {'result': 'not_applicable', 'detail': 'response not documented'}
    else:
        documented = doc['content']
        if not raw and (not documented or status == 204 or case['request']['method'] == 'HEAD'):
            res['content_type'] = {'result': 'passed', 'detail': 'no body, none expected'}
            res['schema_conformance'] = {'result': 'not_applicable', 'detail': 'no body'}
        elif not documented:
            res['content_type'] = {'result': 'failed', 'detail': f'a {ct or "untyped"} body was returned but {key} documents no content'}
            res['schema_conformance'] = {'result': 'not_applicable', 'detail': 'no schema documented'}
        else:
            match = next((m for m in documented if media_matches(ct, m)), None)
            res['content_type'] = ({'result': 'passed', 'detail': ct} if match else
                                   {'result': 'failed', 'detail': f'Content-Type {ct or "missing"}; documented {sorted(documented)}'})
            schema_mt = match or next((m for m in documented if 'json' in m), None)
            schema = documented.get(schema_mt) if schema_mt else None
            if schema is None:
                res['schema_conformance'] = {'result': 'not_applicable', 'detail': 'no schema documented'}
            else:
                try:
                    body = json.loads(raw.decode('utf-8')) if raw else None
                except (UnicodeDecodeError, ValueError):
                    res['schema_conformance'] = {'result': 'failed', 'detail': 'the body is not valid JSON'}
                else:
                    errs = jl.errors(body, schema, root, dialect)
                    if errs:
                        res['schema_conformance'] = {'result': 'failed', 'detail': '; '.join(errs[:3]) + (f' (+{len(errs) - 3} more)' if len(errs) > 3 else ''),
                                                     'errors': errs[:20], 'schema': ref_at(schema, root, errs[0].split(':', 1)[0])}
                    else:
                        unsup = sorted(jl.unsupported(schema, root))
                        res['schema_conformance'] = ({'result': 'cantTell', 'detail': f'unsupported keyword(s): {", ".join(unsup)}'} if unsup
                                                     else {'result': 'passed'})
        missing = [h for h, spec in doc['headers'].items() if spec['required'] and h.lower() not in hl]
        has_required = any(spec['required'] for spec in doc['headers'].values())
        res['required_headers'] = ({'result': 'failed', 'detail': f'missing {", ".join(missing)}'} if missing else
                                   {'result': 'passed'} if has_required else {'result': 'not_applicable'})
    ok = 200 <= status < 300
    if not case['valid']:
        res['negative_accepted'] = {'result': 'failed', 'detail': f'{case["description"]}: answered {status}'} if ok else {'result': 'passed'}
    if case['kind'] == 'example' and case['auth']:
        bad = 400 <= status < 500 and status not in (401, 403, 429)
        res['positive_rejected'] = {'result': 'failed', 'detail': f'the documented example got {status}'} if bad else {'result': 'passed'}
    if case['kind'] == 'no_auth':
        res['auth_enforced'] = {'result': 'failed', 'detail': f'answered {status} without credentials'} if ok else {'result': 'passed', 'detail': f'{status}'}
        for c in ('schema_conformance', 'content_type', 'required_headers', 'status_documented'):
            res.setdefault(c, {'result': 'not_applicable'})
    return res


def gql_value_errors(v, ref: str, sel_fields, sdl: dict, path: str) -> list[str]:
    if ref.endswith('!'):
        if v is None:
            return [f'{path}: null for non-null {ref}']
        ref = ref[:-1]
    if v is None:
        return []
    if ref.startswith('['):
        if not isinstance(v, list):
            return [f'{path}: expected a list']
        out = []
        for i, x in enumerate(v):
            out += gql_value_errors(x, ref[1:-1], sel_fields, sdl, f'{path}[{i}]')
        return out
    t = sdl['types'].get(ref) or {}
    if t.get('kind') == 'ENUM':
        return [] if v in t['values'] else [f'{path}: {v!r} is not a {ref} value']
    if ref in ('Int',):
        return [] if isinstance(v, int) and not isinstance(v, bool) else [f'{path}: expected Int']
    if ref == 'Float':
        return [] if isinstance(v, (int, float)) and not isinstance(v, bool) else [f'{path}: expected Float']
    if ref in ('String', 'ID'):
        return [] if isinstance(v, str) or (ref == 'ID' and isinstance(v, int)) else [f'{path}: expected {ref}']
    if ref == 'Boolean':
        return [] if isinstance(v, bool) else [f'{path}: expected Boolean']
    if t.get('kind') in ('OBJECT', 'INTERFACE'):
        if not isinstance(v, dict):
            return [f'{path}: expected an object']
        out = []
        for f in sel_fields or v.keys():
            if f not in t['fields']:
                out.append(f'{path}.{f}: not a field of {ref}')
                continue
            if f not in v:
                out.append(f'{path}.{f}: missing from the response')
                continue
            out += gql_value_errors(v[f], t['fields'][f]['type'], None, sdl, f'{path}.{f}')
        return out
    return []


def check_graphql(case: dict, op: dict, contract: dict, status: int, headers: dict, raw: bytes) -> dict:
    res: dict[str, dict] = {'server_error': {'result': 'failed', 'detail': f'status {status}'} if status >= 500 else {'result': 'passed'}}
    try:
        body = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        res['graphql_errors'] = {'result': 'failed', 'detail': 'the body is not JSON'}
        return res
    errors = body.get('errors') if isinstance(body, dict) else None
    ct = media({k.lower(): v for k, v in headers.items()}.get('content-type'))
    res['content_type'] = {'result': 'passed', 'detail': ct} if ct in ('application/json', 'application/graphql-response+json') else \
        {'result': 'failed', 'detail': f'Content-Type {ct or "missing"}'}
    if not case['valid']:
        res['negative_accepted'] = ({'result': 'failed', 'detail': 'an invalid query returned data without errors'}
                                    if not errors and status < 400 else {'result': 'passed'})
        return res
    if errors:
        msgs = '; '.join(str(e.get('message')) if isinstance(e, dict) else str(e) for e in errors[:3])
        res['graphql_errors'] = {'result': 'failed', 'detail': f'a valid query returned errors: {msgs}'}
        return res
    data = (body or {}).get('data') or {}
    sel = re.search(r'\{\s*' + re.escape(op['path']) + r'(?:\([^)]*\))?\s*\{([^{}]*)\}', case['request']['body']['query'])
    fields = sel.group(1).split() if sel else None
    errs = gql_value_errors(data.get(op['path']), op['returns'], fields, contract['root'], f'data.{op["path"]}')
    res['graphql_errors'] = {'result': 'failed', 'detail': '; '.join(errs[:3]), 'errors': errs[:20]} if errs else {'result': 'passed'}
    return res


def build_url(base: str, case: dict, op: dict | None, graphql_path: str) -> str:
    req = case['request']
    if req.get('graphql'):
        return base.rstrip('/') + graphql_path
    path = op['path']
    for k, v in req['path_params'].items():
        path = path.replace('{' + k + '}', urllib.parse.quote(str(v), safe=''))
    q = urllib.parse.urlencode(req['query']) if req['query'] else ''
    return base.rstrip('/') + path + ('?' + q if q else '')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('contract', type=Path)
    ap.add_argument('cases', type=Path)
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--exchanges', type=Path)
    ap.add_argument('--rate', type=float, default=5.0)
    ap.add_argument('--timeout', type=float, default=15.0)
    ap.add_argument('--bearer-env')
    ap.add_argument('--api-key-env')
    ap.add_argument('--api-key-header')
    ap.add_argument('--api-key-query')
    ap.add_argument('--authorised-by')
    ap.add_argument('--allow-writes', action='store_true')
    ap.add_argument('--confirm-non-production', action='store_true')
    ap.add_argument('--production-host', action='append', default=[])
    ap.add_argument('--probe', default=None, help='path for the first GET (default: the first GET operation without parameters)')
    ap.add_argument('--graphql-path', default='/graphql')
    ap.add_argument('--no-resend', action='store_true')
    ap.add_argument('--max-requests', type=int, default=5000)
    a = ap.parse_args()

    contract = json.loads(a.contract.read_text(encoding='utf-8'))
    cases_doc = json.loads(a.cases.read_text(encoding='utf-8'))
    if cases_doc.get('contract_sha256') != contract['sha256']:
        print('error: cases.json was generated from a different contract', file=sys.stderr)
        return 2
    host = urllib.parse.urlsplit(a.base_url).hostname or ''
    loopback = is_loopback(host)
    if not loopback and not a.authorised_by:
        print(f'error: {host} is not loopback; pass --authorised-by with who authorised testing it', file=sys.stderr)
        return 2
    writes_ok = a.allow_writes and bool(a.authorised_by) and a.confirm_non_production and host not in a.production_host
    if a.allow_writes and not writes_ok:
        print('error: --allow-writes needs --authorised-by, --confirm-non-production and a host not in --production-host', file=sys.stderr)
        return 2
    secret_envs = [e for e in (a.bearer_env, a.api_key_env) if e]
    secrets = [os.environ.get(e, '') for e in secret_envs]
    for e, v in zip(secret_envs, secrets):
        if not v:
            print(f'error: environment variable {e} is empty', file=sys.stderr)
            return 2
    auth_headers = {}
    auth_query = {}
    if a.bearer_env:
        auth_headers['Authorization'] = f'Bearer {os.environ[a.bearer_env]}'
    if a.api_key_env:
        if a.api_key_query:
            auth_query[a.api_key_query] = os.environ[a.api_key_env]
        else:
            auth_headers[a.api_key_header or 'X-API-Key'] = os.environ[a.api_key_env]
    ops = {o['operation_id']: o for o in contract['operations']}
    ex_dir = a.exchanges or a.out.parent / 'exchanges'
    ex_dir.mkdir(parents=True, exist_ok=True)
    s = Sender(a, secrets)

    run = {'base_url': a.base_url, 'host': host, 'loopback': loopback, 'authorised_by': a.authorised_by,
           'writes_allowed': writes_ok, 'production_hosts': a.production_host, 'rate_per_second': a.rate,
           'contract_sha256': contract['sha256'], 'seed': cases_doc['seed'], 'generator': cases_doc['generator'],
           'secret_env': secret_envs, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'probe': None,
           'status': 'complete', 'results': [], 'blocked': []}

    probe_path = a.probe
    if probe_path is None:
        probe_path = next((o['path'] for o in contract['operations'] if o['method'] == 'GET' and not o['parameters']), '/')
        if contract['kind'] == 'graphql':
            probe_path = None
    try:
        if probe_path is not None:
            st, rh, raw, el = s.request('GET', a.base_url.rstrip('/') + probe_path, {}, None)
        else:
            st, rh, raw, el = s.request('POST', a.base_url.rstrip('/') + a.graphql_path, {'Content-Type': 'application/json', **auth_headers},
                                        json.dumps({'query': '{ __typename }'}).encode())
        run['probe'] = {'path': probe_path or a.graphql_path, 'status': st, 'elapsed_ms': el}
        if st >= 500:
            run['status'] = 'down'
    except (urllib.error.URLError, OSError) as e:
        run['probe'] = {'path': probe_path or a.graphql_path, 'status': None, 'error': str(e)[:200]}
        run['status'] = 'down'
    if run['status'] == 'down':
        print(f'the instance looks down ({run["probe"]}); nothing sent', file=sys.stderr)

    def send(case: dict, suffix: str = ''):
        req = case['request']
        op = ops.get(case['operation_id'])
        url = build_url(a.base_url, case, op, a.graphql_path)
        headers = dict(req.get('headers') or {})
        if case['auth']:
            headers.update(auth_headers)
            if auth_query:
                url += ('&' if '?' in url else '?') + urllib.parse.urlencode(auth_query)
        body_bytes = None
        if 'body' in req:
            headers['Content-Type'] = req.get('media_type') or 'application/json'
            body_bytes = json.dumps(req['body']).encode() if req['body'] is not None else b''
        headers.setdefault('Accept', 'application/json')
        st, rh, raw, el = s.request(req['method'], url, headers, body_bytes)
        checker = check_graphql if req.get('graphql') else check_rest
        checks = checker(case, op, contract, st, rh, raw)
        failed = sorted(c for c, r in checks.items() if r['result'] == 'failed')
        exchange = None
        if failed:
            req_body = req.get('body')
            if req_body is not None and op and op.get('request_body'):
                for c in op['request_body']['content'].values():
                    req_body = redact_schema_fields(req_body, c.get('schema') or {}, contract.get('root') or {})
            resp = body_record(raw)
            if 'json' in resp and op and not req.get('graphql'):
                _, doc = find_response(op, st)
                for sch in ((doc or {}).get('content') or {}).values():
                    resp['json'] = redact_schema_fields(resp['json'], sch or {}, contract['root'])
            record = {'case_id': case['id'], 'operation_id': case['operation_id'], 'kind': case['kind'], 'valid': case['valid'],
                      'description': case['description'], 'sent_with_credentials': case['auth'],
                      'request': {'method': req['method'], 'url': redact_url(url, a.api_key_query),
                                  'headers': redact_headers(headers, a.api_key_header), 'body': req_body if 'body' in req else None},
                      'response': {'status': st, 'headers': redact_headers(rh, a.api_key_header), 'body': resp},
                      'elapsed_ms': el, 'failed_checks': failed}
            record = scrub(record, secrets)
            exchange = (ex_dir / f'{case["id"]}{suffix}.json')
            exchange.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        return st, el, checks, failed, exchange

    rel = lambda p: p.relative_to(a.out.parent).as_posix() if p and p.is_relative_to(a.out.parent) else (p.as_posix() if p else None)  # noqa: E731
    if run['status'] != 'down':
        for case in cases_doc['cases']:
            if s.stopped or s.sent >= a.max_requests:
                run['status'] = 'incomplete'
                break
            if is_write(case) and not writes_ok:
                run['blocked'].append({'case_id': case['id'], 'reason': 'write method without allowed writes'})
                continue
            st, el, checks, failed, ex = send(case)
            run['results'].append({'case_id': case['id'], 'operation_id': case['operation_id'], 'kind': case['kind'],
                                   'valid': case['valid'], 'method': case['request']['method'], 'write': is_write(case),
                                   'status': st, 'elapsed_ms': el, 'checks': checks, 'failed': failed, 'exchange': rel(ex),
                                   'resend': None})
        if not a.no_resend:
            by_id = {c['id']: c for c in cases_doc['cases']}
            for r in run['results']:
                if not r['failed'] or s.stopped:
                    continue
                st, el, checks, failed, ex = send(by_id[r['case_id']], '.resend')
                r['resend'] = {'status': st, 'failed': failed, 'exchange': rel(ex),
                               'repeated': {c: c in failed for c in r['failed']}}
        if s.stopped:
            run['status'] = 'incomplete'
            run['stopped_reason'] = s.stopped
    run['requests_sent'] = s.sent
    run['rate_limited'] = {'events': s.rate_events, 'backoff_seconds': round(s.backoff, 1)}
    run['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(run, indent=2) + '\n', encoding='utf-8')
    nfail = sum(1 for r in run['results'] if r['failed'])
    print(f'{run["status"]}: {s.sent} request(s), {len(run["results"])} case(s), {nfail} with a failed check, '
          f'{len(run["blocked"])} blocked -> {a.out}', file=sys.stderr)
    return 0 if run['status'] != 'down' else 1


if __name__ == '__main__':
    sys.exit(main())
