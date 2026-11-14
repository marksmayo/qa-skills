#!/usr/bin/env python3
"""Send generated cases to a test instance, check every response against the contract, and save the failing exchanges.

    python send_cases.py contract.json cases.json --base-url http://127.0.0.1:8140 --out run/run.json
        [--exchanges run/exchanges] [--rate 5] [--bearer-env NAME] [--api-key-env NAME --api-key-header X-Api-Key]
        [--cookie-env NAME] [--authorised-by WHO] [--allow-writes --confirm-non-production] [--production-host HOST ...]
        [--probe api/health --probe-expect 200] [--graphql-path /graphql] [--no-resend]
        [--max-requests N] [--path-budget /api/appointments=15 ...] [--max-retries-429 N] [--max-retry-after S]
        [--on-429 stop-run|stop-path] [--resume run/run.attempt1.json]

Safety (enforced here, re-checked by the validator):
  * A target that isn't loopback needs --authorised-by.
  * Only GET, HEAD and OPTIONS are sent unless --allow-writes, which also needs --authorised-by and
    --confirm-non-production. A host given with --production-host never receives a write, even then.
    GraphQL queries go over POST but are reads; mutations are writes.
  * Requests are spaced to --rate per second (default 5). A 429 is retried after its Retry-After up to
    --max-retries-429 times (default 3) when Retry-After is at most --max-retry-after seconds (default
    60); a longer wait is not retried early. When the 429 persists, --on-429 stop-run (default) stops the
    run and stop-path stops only that path (its --path-budget prefix, else the operation's path) and
    carries on. Either way the run is `incomplete`.
  * --max-requests caps this attempt; --path-budget caps requests to a path prefix (a first send needs
    room for its re-send). Cases over a budget are listed in `blocked` and the run is `incomplete`.
  * --resume reuses the answered cases of an earlier run.json (same contract, seed and base URL), copies
    their exchanges, and sends only the rest; the earlier attempt is recorded in `resumed_from` and its
    requests are counted in `requests_sent`. If the sender aborts, the partial run.json is still written.
  * The liveness probe is --probe (leading slash optional; Git Bash rewrites a leading "/" unless
    MSYS_NO_PATHCONV=1, and a rewritten path is refused). The run is `down` when the probe can't connect
    or misses --probe-expect (default: any status below 500). Without --probe the first parameterless GET
    operation is used, and its 5xx alone doesn't mark the target down: it is an API answer, checked as a case.
  * Before anything is written to disk, credentials are redacted: Authorization, Cookie, Set-Cookie,
    the API-key header or query parameter, any parameter named like token/secret/password/key/sig, and
    body fields the schema marks format: password or writeOnly. Every occurrence of a configured secret
    value is replaced too. Bodies over 2 MB are truncated, keeping the full body's SHA-256.
  * Every result records body_bytes (the rest of a large body is counted, not kept, up to 16 MB or
    --max-response-bytes; a larger Content-Length is taken as the size, otherwise body_bytes_lower_bound)
    and items (the length of a collection body: a top-level array, or the one array of a page wrapper).
    --max-response-bytes N adds the advisory response_size check: a 2xx body over N bytes fails it.

Checks per response: status_documented (4xx/3xx/2xx not in responses and no default; 5xx is left to
server_error), server_error, content_type, schema_conformance (passed/failed/cantTell), required_headers,
negative_accepted (an invalid request got 2xx), positive_rejected (a documented example got a 4xx other
than 401/403/429), auth_enforced (no credentials, 2xx), graphql_errors, and response_size with
--max-response-bytes. A body too large to read whole is cantTell for schema_conformance. Each failing
case is sent once more; a check that doesn't fail again is not repeatable (flaky).

Only exchanges with a failed check are saved, as exchanges/<case>.json and <case>.resend.json (the
case id made filesystem-safe). --cookie-env sends a Cookie header (session auth); it is redacted like
the other credentials, and each cookie value is scrubbed wherever it appears.
"""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, os, re, socket, sys, time, urllib.error, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonschema_lite as jl  # noqa: E402

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
MAX_BODY = 2 * 1024 * 1024
MEASURE_CAP = 16 * 1024 * 1024  # a body past MAX_BODY is counted (not kept) up to here, or --max-response-bytes
REDACTED = '[REDACTED]'
SENSITIVE_HEADERS = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key', 'api-key', 'x-auth-token'}
SENSITIVE_NAME = re.compile(r'(token|secret|passw|api[-_]?key|apikey|signature|^sig$|session)', re.I)
CHECKS = ['status_documented', 'server_error', 'content_type', 'schema_conformance', 'required_headers',
          'negative_accepted', 'positive_rejected', 'auth_enforced', 'graphql_errors']


SAFE_ID = re.compile(r'^[A-Za-z0-9._-]{1,80}$')


def safe_id(name: str) -> str:
    """A filesystem-safe stand-in for an operation id. Ids that are already safe are kept; others (the
    default "GET /api/x" when the contract has no operationId) become a slug plus a short hash, so two
    ids that slug alike stay distinct."""
    if SAFE_ID.match(name) and name not in ('.', '..'):
        return name
    slug = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')[:60] or 'op'
    return f'{slug}-{hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]}'


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
        self.limited: str | None = None
        self.budget_used = {p: 0 for p in getattr(a, 'budgets', {})}
        self.opener = urllib.request.build_opener(NoRedirect)
        self.size = (0, True)

    def read_body(self, resp, headers: dict) -> bytes:
        """The first MAX_BODY + 1 bytes (what the checks and the saved exchange use). The rest is counted, not kept,
        up to the measuring cap, so `self.size` is the real body size: (bytes, complete). A Content-Length larger
        than what was read is taken as the size; with no Content-Length, a body past the cap is a lower bound."""
        raw = resp.read(MAX_BODY + 1)
        total, complete = len(raw), True
        if len(raw) > MAX_BODY:
            cap = max(MEASURE_CAP, (self.a.max_response_bytes or 0) + 1)
            while total <= cap:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
            else:
                complete = False
        cl = next((v for k, v in headers.items() if k.lower() == 'content-length'), None)
        if cl and str(cl).isdigit() and int(cl) >= total:
            total, complete = int(cl), True
        self.size = (total, complete)
        return raw

    def request(self, method: str, url: str, headers: dict, body: bytes | None):
        """Send one request, retrying a 429 up to --max-retries-429 times when its Retry-After is at most
        --max-retry-after seconds. Returns (status, headers, body, elapsed_ms); `self.limited` is set when
        a 429 persisted, and the caller decides whether that stops the run or only that path."""
        self.limited = None
        retries = max(0, self.a.max_retries_429)
        for attempt in range(retries + 1):
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            t0 = time.monotonic()
            self.sent += 1
            path = urllib.parse.urlsplit(url).path
            for prefix in self.budget_used:
                if path.startswith(prefix):
                    self.budget_used[prefix] += 1
            try:
                with self.opener.open(req, timeout=self.a.timeout) as r:
                    status, rh = r.status, dict(r.headers.items())
                    raw = self.read_body(r, rh)
            except urllib.error.HTTPError as e:
                status, rh = e.code, dict(e.headers.items())
                raw = self.read_body(e, rh)
            elapsed = round((time.monotonic() - t0) * 1000)
            if status != 429:
                return status, rh, raw, elapsed
            self.rate_events += 1
            delay = retry_after(rh.get('Retry-After'))
            if attempt < retries and delay <= self.a.max_retry_after:
                self.backoff += delay
                time.sleep(delay)
                continue
            why = (f'Retry-After {delay:g} s is over --max-retry-after {self.a.max_retry_after:g} s' if delay > self.a.max_retry_after
                   else f'429 persisted after {retries} retr{"y" if retries == 1 else "ies"}')
            self.limited = f'rate limited on {path}: {why}'
            return status, rh, raw, elapsed
        raise RuntimeError('unreachable')


def retry_after(v: str | None) -> float:
    """Seconds the server asked for (not capped here; the caller compares with --max-retry-after)."""
    if not v:
        return 1.0
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return 1.0


def parse_budgets(items: list[str]) -> dict[str, int]:
    out = {}
    for it in items:
        prefix, sep, n = it.rpartition('=')
        if not sep or not n.isdigit():
            raise ValueError(f'--path-budget {it!r}: expected PREFIX=N')
        out['/' + prefix.lstrip('/')] = int(n)
    return out


MSYS_MANGLED = re.compile(r'^[A-Za-z]:[\\/]|[A-Za-z]:[\\/](Program Files|msys|Git)', re.I)


def norm_path(p: str | None, what: str) -> str | None:
    """A path given on the command line, with its leading slash optional. Git Bash rewrites a leading
    "/" into a Windows path (MSYS path conversion); refuse that instead of sending it."""
    if p is None:
        return None
    if MSYS_MANGLED.search(p):
        raise ValueError(f'{what} {p!r} looks rewritten by Git Bash path conversion; pass it without the leading slash '
                         f'(e.g. api/health) or set MSYS_NO_PATHCONV=1')
    return '/' + p.lstrip('/')


def expect_ok(status: int, spec: str | None) -> bool:
    """--probe-expect: a comma list of codes or classes (200,204,2xx). Default: anything below 500."""
    if not spec:
        return status < 500
    for tok in spec.split(','):
        tok = tok.strip().lower()
        if tok.endswith('xx') and tok[:1].isdigit() and str(status)[0] == tok[0]:
            return True
        if tok.isdigit() and int(tok) == status:
            return True
    return False


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


def collection_items(raw: bytes) -> int | None:
    """How many items a collection body holds: the length of a top-level array, or of the one array in an object
    whose other values are numbers, booleans or null (a page wrapper such as {"data": [...], "total": 0}).
    None when the body isn't a collection (or isn't JSON, or was too large to read whole)."""
    if not raw or len(raw) > MAX_BODY:
        return None
    try:
        body = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        lists = [v for v in body.values() if isinstance(v, list)]
        rest = [v for v in body.values() if not isinstance(v, list)]
        if len(lists) == 1 and all(v is None or isinstance(v, (int, float, bool)) for v in rest):
            return len(lists[0])
    return None


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
            elif len(raw) > MAX_BODY:  # only the first 2 MB were kept: the body can't be parsed, which says nothing about it
                res['schema_conformance'] = {'result': 'cantTell', 'detail': 'the body is over 2 MB and was not read whole'}
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
    ap.add_argument('--cookie-env', help='env var holding a Cookie header value (session cookie auth)')
    ap.add_argument('--authorised-by')
    ap.add_argument('--allow-writes', action='store_true')
    ap.add_argument('--confirm-non-production', action='store_true')
    ap.add_argument('--production-host', action='append', default=[])
    ap.add_argument('--probe', default=None,
                    help='liveness path for the first GET, leading slash optional (default: the first GET operation without parameters)')
    ap.add_argument('--probe-expect', default=None, help='statuses that mean "up", e.g. 200 or 2xx,3xx (default: below 500)')
    ap.add_argument('--graphql-path', default='/graphql')
    ap.add_argument('--no-resend', action='store_true')
    ap.add_argument('--max-requests', type=int, default=5000, help='total request budget for this attempt')
    ap.add_argument('--path-budget', action='append', default=[], metavar='PREFIX=N',
                    help='at most N requests to paths starting with PREFIX (resends included); cases over it are not sent')
    ap.add_argument('--max-retries-429', type=int, default=3)
    ap.add_argument('--max-retry-after', type=float, default=60.0,
                    help='honour Retry-After up to this many seconds; a longer one is not waited for')
    ap.add_argument('--on-429', choices=['stop-run', 'stop-path'], default='stop-run',
                    help='when a 429 persists: stop the whole run, or stop only that path (its budget prefix, else its '
                         'operation path) and carry on with the rest')
    ap.add_argument('--resume', type=Path, help='an earlier run.json of the same cases: answered cases are reused, not re-sent')
    ap.add_argument('--max-response-bytes', type=int, default=None, metavar='N',
                    help='advisory response_size check: a 2xx body over N bytes fails it (sizes are recorded either way; '
                         'a large body is counted, not kept, and reading stops past N)')
    a = ap.parse_args()

    try:
        a.probe = norm_path(a.probe, '--probe')
        a.graphql_path = norm_path(a.graphql_path, '--graphql-path')
        a.budgets = parse_budgets(a.path_budget)
        for prefix in a.budgets:
            norm_path(prefix, '--path-budget')
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
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
    secret_envs = [e for e in (a.bearer_env, a.api_key_env, a.cookie_env) if e]
    secrets = [os.environ.get(e, '') for e in secret_envs]
    for e, v in zip(secret_envs, secrets):
        if not v:
            print(f'error: environment variable {e} is empty', file=sys.stderr)
            return 2
    # a cookie header carries name=value pairs: scrub each value too, in case one comes back alone
    if a.cookie_env:
        secrets += [kv.split('=', 1)[1].strip() for kv in os.environ[a.cookie_env].split(';') if '=' in kv and len(kv.split('=', 1)[1].strip()) >= 8]
    prev = None
    if a.resume:
        prev = json.loads(a.resume.read_text(encoding='utf-8'))
        if (prev.get('contract_sha256'), prev.get('seed'), prev.get('base_url')) != (contract['sha256'], cases_doc['seed'], a.base_url):
            print('error: --resume run.json is for a different contract, seed or base URL', file=sys.stderr)
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
    if a.cookie_env:
        auth_headers['Cookie'] = os.environ[a.cookie_env]
    ops = {o['operation_id']: o for o in contract['operations']}
    ex_dir = a.exchanges or a.out.parent / 'exchanges'
    ex_dir.mkdir(parents=True, exist_ok=True)
    s = Sender(a, secrets)

    run = {'base_url': a.base_url, 'host': host, 'loopback': loopback, 'authorised_by': a.authorised_by,
           'writes_allowed': writes_ok, 'production_hosts': a.production_host, 'rate_per_second': a.rate,
           'contract_sha256': contract['sha256'], 'seed': cases_doc['seed'], 'generator': cases_doc['generator'],
           'secret_env': secret_envs, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'probe': None,
           'status': 'complete', 'results': [], 'blocked': [],
           'limits': {'max_requests': a.max_requests, 'path_budgets': a.budgets, 'max_retries_429': a.max_retries_429,
                      'max_retry_after': a.max_retry_after, 'on_429': a.on_429,
                      **({'max_response_bytes': a.max_response_bytes} if a.max_response_bytes else {})}}
    if a.cookie_env:
        run['auth'] = 'cookie'

    rel = lambda p: p.relative_to(a.out.parent).as_posix() if p and p.is_relative_to(a.out.parent) else (p.as_posix() if p else None)  # noqa: E731

    def write_run():
        run['requests_sent'] = s.sent + (prev['requests_sent'] if prev else 0)
        run['requests_this_attempt'] = s.sent
        events = s.rate_events + ((prev.get('rate_limited') or {}).get('events', 0) if prev else 0)
        backoff = s.backoff + ((prev.get('rate_limited') or {}).get('backoff_seconds', 0) if prev else 0)
        run['rate_limited'] = {'events': events, 'backoff_seconds': round(backoff, 1)}
        run['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(run, indent=2) + '\n', encoding='utf-8')

    default_probe = a.probe is None
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
        run['probe'] = {'path': probe_path or a.graphql_path, 'status': st, 'elapsed_ms': el, 'expect': a.probe_expect,
                        'default': default_probe}
        if not expect_ok(st, a.probe_expect):
            if default_probe and st >= 500 and probe_path is not None and probe_path != '/':
                # the default probe is an API operation; its 5xx doesn't prove the target is down (the
                # operation is sent as a case anyway). Name a health path with --probe to decide liveness.
                run['probe']['note'] = 'the default probe is an API operation; its 5xx was not taken as the target being down'
            else:
                run['status'] = 'down'
    except (urllib.error.URLError, OSError) as e:
        run['probe'] = {'path': probe_path or a.graphql_path, 'status': None, 'error': str(e)[:200], 'expect': a.probe_expect,
                        'default': default_probe}
        run['status'] = 'down'
    if run['status'] == 'down':
        print(f'the instance looks down ({run["probe"]}); nothing sent', file=sys.stderr)

    def scope_of(case: dict) -> str:
        op = ops.get(case['operation_id'])
        path = a.graphql_path if case['request'].get('graphql') or not op else op['path']
        budget = max((p for p in a.budgets if path.startswith(p)), key=len, default=None)
        return budget or path

    def budget_room(case: dict, need: int) -> str | None:
        op = ops.get(case['operation_id'])
        path = a.graphql_path if case['request'].get('graphql') or not op else op['path']
        for prefix, n in a.budgets.items():
            if path.startswith(prefix) and s.budget_used[prefix] + need > n:
                return prefix
        return None

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
        size, size_complete = s.size
        checker = check_graphql if req.get('graphql') else check_rest
        checks = checker(case, op, contract, st, rh, raw)
        if a.max_response_bytes and 200 <= st < 300:
            checks['response_size'] = ({'result': 'failed', 'detail': f'{size} bytes{"" if size_complete else " or more"}, over '
                                                                      f'--max-response-bytes {a.max_response_bytes}'}
                                       if size > a.max_response_bytes else {'result': 'passed', 'detail': f'{size} bytes'})
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
                      'response': {'status': st, 'headers': redact_headers(rh, a.api_key_header), 'body': resp,
                                   'body_bytes': size, **({} if size_complete else {'body_bytes_lower_bound': True})},
                      'elapsed_ms': el, 'failed_checks': failed}
            record = scrub(record, secrets)
            exchange = (ex_dir / f'{safe_id(case["id"])}{suffix}.json')
            exchange.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        sizes = {'body_bytes': size, **({} if size_complete else {'body_bytes_lower_bound': True}),
                 'items': collection_items(raw)}
        return st, el, checks, failed, exchange, sizes

    def reuse(r: dict) -> dict:
        """A result from --resume, with its exchange files copied next to this run."""
        r = json.loads(json.dumps(r))
        for holder in (r, r.get('resend') or {}):
            p = holder.get('exchange')
            if not p:
                continue
            src = a.resume.parent / p
            dst = ex_dir / Path(p).name
            if src.is_file() and src.resolve() != dst.resolve():
                dst.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
            holder['exchange'] = rel(dst)
        r['reused_from'] = a.resume.as_posix()
        return r

    stopped_scopes: dict[str, str] = {}
    try:
        if run['status'] != 'down':
            if prev:
                done = {r['case_id']: r for r in prev.get('results') or [] if r['status'] != 429}
                run['resumed_from'] = (prev.get('resumed_from') or []) + [{
                    'file': a.resume.as_posix(), 'status': prev.get('status'), 'stopped_reason': prev.get('stopped_reason'),
                    'requests_sent': prev.get('requests_this_attempt', prev.get('requests_sent')), 'results_reused': 0}]
            else:
                done = {}
            for case in cases_doc['cases']:
                if s.stopped or s.sent >= a.max_requests:
                    run['status'] = 'incomplete'
                    run.setdefault('stopped_reason', s.stopped or f'request budget --max-requests {a.max_requests} reached')
                    break
                if case['id'] in done:
                    run['results'].append(reuse(done[case['id']]))
                    run['resumed_from'][-1]['results_reused'] += 1
                    continue
                if is_write(case) and not writes_ok:
                    run['blocked'].append({'case_id': case['id'], 'reason': 'write method without allowed writes'})
                    continue
                scope = scope_of(case)
                if scope in stopped_scopes:
                    run['blocked'].append({'case_id': case['id'], 'reason': f'not sent: {stopped_scopes[scope]}'})
                    continue
                over = budget_room(case, 1 if a.no_resend else 2)
                if over:
                    run['blocked'].append({'case_id': case['id'], 'reason': f'not sent: path budget for {over} '
                                                                           f'({a.budgets[over]} requests) would be exceeded'})
                    continue
                st, el, checks, failed, ex, sizes = send(case)
                run['results'].append({'case_id': case['id'], 'operation_id': case['operation_id'], 'kind': case['kind'],
                                       'valid': case['valid'], 'method': case['request']['method'], 'write': is_write(case),
                                       'status': st, 'elapsed_ms': el, **sizes, 'checks': checks, 'failed': failed,
                                       'exchange': rel(ex), 'resend': None})
                if s.limited:
                    if a.on_429 == 'stop-path':
                        stopped_scopes[scope] = s.limited
                    else:
                        s.stopped = s.limited
            if not a.no_resend:
                by_id = {c['id']: c for c in cases_doc['cases']}
                for r in run['results']:
                    if not r['failed'] or r.get('resend') or s.stopped or r['status'] == 429:
                        continue
                    if scope_of(by_id[r['case_id']]) in stopped_scopes:
                        continue
                    st, el, checks, failed, ex, _ = send(by_id[r['case_id']], '.resend')
                    r['resend'] = {'status': st, 'failed': failed, 'exchange': rel(ex),
                                   'repeated': {c: c in failed for c in r['failed']}}
                    if s.limited:
                        if a.on_429 == 'stop-path':
                            stopped_scopes[scope_of(by_id[r['case_id']])] = s.limited
                        else:
                            s.stopped = s.limited
            if s.stopped:
                run['status'] = 'incomplete'
                run['stopped_reason'] = s.stopped
            if stopped_scopes:
                run['status'] = 'incomplete'
                run['stopped_paths'] = stopped_scopes
                run.setdefault('stopped_reason', f'{len(stopped_scopes)} path(s) stopped on 429: {", ".join(sorted(stopped_scopes))}')
            if any(b['reason'].startswith('not sent: path budget') for b in run['blocked']):
                run['status'] = 'incomplete'
                run.setdefault('stopped_reason', 'path budget reached; some cases were not sent')
    except BaseException as e:  # never lose the record of what was already sent
        run['status'] = 'incomplete'
        run['stopped_reason'] = f'sender aborted: {type(e).__name__}: {str(e)[:200]}'
        write_run()
        print(f'aborted after {s.sent} request(s); partial run written to {a.out} (resume with --resume)', file=sys.stderr)
        raise
    write_run()
    nfail = sum(1 for r in run['results'] if r['failed'])
    print(f'{run["status"]}: {s.sent} request(s) this attempt, {len(run["results"])} case(s), {nfail} with a failed check, '
          f'{len(run["blocked"])} blocked -> {a.out}', file=sys.stderr)
    return 0 if run['status'] != 'down' else 1


if __name__ == '__main__':
    sys.exit(main())
