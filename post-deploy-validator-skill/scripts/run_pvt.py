#!/usr/bin/env python3
"""Run a post-deploy check plan against a live environment, read-only (stdlib only).

    python run_pvt.py pvt_plan.json --expect-version 1.9.0 --out runs/<stamp>/ [--baseline runs/<previous>/run.json]

The plan is checked against schemas/pvt_plan.schema.json first; an unknown field (a typo such as
"critcal") stops the run before anything is sent.

The request guard allows GET and HEAD, and the sign-in request of a credential set the plan names.
Anything else (a POST to create something, a PUT, a DELETE) is refused before it is sent and recorded as
blocked. A production target needs `authorised_by` in the plan.

Credentials come from the environment variables the plan names for each set (for example
PVT_SMOKE_READER_USERNAME and PVT_SMOKE_READER_PASSWORD) and never leave memory: they are not printed,
logged or written, and Authorization, Cookie and Set-Cookie values are never recorded. A response value is
recorded only for an assertion marked `record: true`; otherwise the result says whether it passed, with the
value's type and length. Each assertion has a `kind` (http_status, body, json, header, redirect), so a
JSON field named `status` is never mistaken for the HTTP status.

Version: `version` reads a JSON field (json_path), a header, an HTML meta tag (meta: its name) or the first
group of a regular expression over the body (regex, e.g. `data-dpl-id="([^"]+)"`). `deployment` reads a
deployment identifier the same way (a commit SHA, a platform deployment id). --expect-version matches
either the version or the deployment id.

Redirects are not followed blindly: each hop is recorded (status and path, query dropped), only same-origin
hops are followed (up to 10), and a check can assert `no_redirect` or a `final_path`. A check without a
credential set that lands on a sign-in page (`login_paths`, default /login, /signin, /sign-in, ...) fails
unless it sets `allow_login_redirect`.

Headers: content type, cache and CDN headers (age, x-vercel-cache, x-vercel-id, cf-cache-status, x-cache,
via ...) and security headers (CSP with nonces redacted, HSTS, frame options ...) are recorded; nothing
else. `expect.headers` asserts {name: {present|absent: true} | {equals|contains: text}}.

Timed checks take `performance.samples` requests, but only while the check answers as expected: a check
whose first response fails its status expectation (or isn't 2xx when it has none) is sent once and its
time is `comparable: false`; so is one whose samples change status. The median is compared with the same
check in the baseline run (same plan SHA-256 and base URL). The baseline's deployment id (or version)
says whether the comparison is between releases or drift on one deployment (`baseline_relation`).

Stopping: a 429, or a 403/503 with a WAF or rate-limit signature (Retry-After, cf-mitigated,
x-vercel-mitigated, a challenge page), stops the run at once: that check is `error` (the target refused,
not the application) and the rest are skipped. Three 5xx answers in a row (a 4xx doesn't reset the count,
only a 2xx or 3xx does) stop the run as an application outage. Requests are paced to `rate_per_second`.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, statistics, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check  # noqa: E402

CACHE_HEADERS = ('age', 'x-vercel-cache', 'x-vercel-id', 'cf-cache-status', 'cf-ray', 'x-cache', 'x-cache-hits', 'via', 'x-served-by',
                 'cdn-cache-control', 'x-nextjs-cache', 'x-nextjs-prerender')
SECURITY_HEADERS = ('strict-transport-security', 'content-security-policy', 'content-security-policy-report-only', 'x-frame-options',
                    'x-content-type-options', 'referrer-policy', 'permissions-policy', 'cross-origin-opener-policy',
                    'cross-origin-embedder-policy', 'cross-origin-resource-policy', 'x-powered-by')
SAFE_HEADERS = ('content-type', 'x-app-version', 'cache-control', 'content-length', 'server', 'vary') + CACHE_HEADERS + SECURITY_HEADERS
LOGIN_PATHS = ['/login', '/signin', '/sign-in', '/log-in', '/auth/login', '/auth/signin', '/api/auth/signin', '/account/login', '/users/sign_in']
WAF_HEADERS = ('cf-mitigated', 'x-vercel-mitigated', 'x-amzn-waf-action', 'x-sucuri-block', 'x-akamai-edgescape-block')
WAF_TEXT = re.compile(r'(?i)attention required|access denied|request (was )?blocked|security checkpoint|checking your browser|captcha|'
                      r'web application firewall|cf-chl-|_incapsula_|akamai reference')
NONCE = re.compile(r"'nonce-[^']+'")


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def plan_sha(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan_errors(plan: dict) -> list[str]:
    schema = json.loads((HERE.parent / 'schemas' / 'pvt_plan.schema.json').read_text(encoding='utf-8'))
    errs = schema_check.errors(plan, schema)
    ids = [c.get('id') for c in plan.get('checks', [])]
    errs += [f'$.checks: id {i!r} is used twice' for i in sorted({i for i in ids if ids.count(i) > 1})]
    for c in plan.get('checks', []):
        if c.get('credential_set') and c['credential_set'] not in plan.get('credential_sets', {}):
            errs.append(f'$.checks[{c.get("id")}]: credential set {c["credential_set"]!r} is not defined')
    return errs


def get_all(obj, path: str) -> list:
    cur = [obj]
    for part in re.findall(r'[^.\[\]]+|\[\*\]|\[\d+\]', path):
        nxt = []
        for c in cur:
            if part == '[*]':
                nxt += c if isinstance(c, list) else []
            elif part.startswith('['):
                i = int(part[1:-1])
                if isinstance(c, list) and len(c) > i:
                    nxt.append(c[i])
            elif isinstance(c, dict) and part in c:
                nxt.append(c[part])
        cur = nxt
    return cur


def describe(v) -> str:
    if isinstance(v, (list, dict, str)):
        return f'{type(v).__name__} of length {len(v)}'
    return type(v).__name__


def safe_headers(hdrs: dict) -> dict:
    out = {k: v for k, v in hdrs.items() if k in SAFE_HEADERS}
    for k in ('content-security-policy', 'content-security-policy-report-only'):
        if k in out:
            out[k] = NONCE.sub("'nonce-REDACTED'", out[k])   # nonces change per response and mean nothing later
    return out


def meta_content(html: str, name: str) -> str | None:
    for tag in re.findall(r'<meta\b[^>]*>', html, re.I):
        attrs = {k.lower(): v1 or v2 for k, v1, v2 in re.findall(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag)}
        if name.lower() in ((attrs.get('name') or '').lower(), (attrs.get('property') or '').lower(), (attrs.get('http-equiv') or '').lower()):
            return attrs.get('content')
    return None


def read_value(spec: dict, raw: bytes, hdrs: dict) -> tuple[object, str | None]:
    """The value a version or deployment reader names, and where it came from."""
    if spec.get('json_path'):
        try:
            got = get_all(json.loads(raw), spec['json_path'])
            if got and got[0] is not None:
                return got[0], f'{spec["path"]} JSON {spec["json_path"]}'
        except ValueError:
            pass
    text = raw.decode('utf-8', 'replace')
    if spec.get('meta'):
        v = meta_content(text, spec['meta'])
        if v:
            return v, f'{spec["path"]} <meta name="{spec["meta"]}">'
    if spec.get('regex'):
        m = re.search(spec['regex'], text)
        if m:
            return (m.group(1) if m.groups() else m.group(0)), f'{spec["path"]} body /{spec["regex"]}/'
    if spec.get('header') and hdrs.get(spec['header'].lower()):
        return hdrs[spec['header'].lower()], f'header {spec["header"]}'
    return None, None


def check_assertion(body, a: dict, at: datetime) -> dict:
    vals = get_all(body, a['path'])
    op, want = a['op'], a.get('value')
    ok, detail = False, ''
    try:
        if op == 'exists':
            ok = bool(vals) and vals[0] is not None
        elif op in ('eq', 'ne', 'gt', 'ge', 'lt', 'le'):
            v = vals[0] if vals else None
            ok = v is not None and {'eq': v == want, 'ne': v != want, 'gt': v > want, 'ge': v >= want, 'lt': v < want, 'le': v <= want}[op]
        elif op in ('len_ge', 'len_le'):
            v = vals[0] if vals else None
            n = len(v) if isinstance(v, (list, str, dict)) else -1
            ok = n >= want if op == 'len_ge' else 0 <= n <= want
            detail = f'length {n}'
        elif op in ('all_gt', 'all_ge', 'all_lt', 'all_le'):
            f = {'all_gt': lambda x: x > want, 'all_ge': lambda x: x >= want, 'all_lt': lambda x: x < want, 'all_le': lambda x: x <= want}[op]
            bad = [i for i, x in enumerate(vals) if not (isinstance(x, (int, float)) and f(x))]
            ok = bool(vals) and not bad
            detail = f'{len(vals)} value(s), {len(bad)} failing' + (f' (items {bad[:5]})' if bad else '')
        elif op == 'unique':
            ok = bool(vals) and len(vals) == len({json.dumps(x, sort_keys=True) for x in vals})
            detail = f'{len(vals)} value(s), {len(vals) - len({json.dumps(x, sort_keys=True) for x in vals})} duplicate(s)'
        elif op == 'matches':
            ok = bool(vals) and all(isinstance(x, str) and re.fullmatch(want, x) for x in vals)
        elif op == 'fresh_within_minutes':
            t = datetime.fromisoformat(str(vals[0]).replace('Z', '+00:00')) if vals else None
            age = (at - t).total_seconds() / 60 if t else None
            ok = age is not None and -5 <= age <= want
            detail = f'age {age:.1f} min' if age is not None else 'no timestamp'
        else:
            detail = f'unknown op {op}'
    except (TypeError, ValueError) as e:
        ok, detail = False, f'{type(e).__name__}'
    out = {'kind': 'json', 'path': a['path'], 'op': op, 'expected': want, 'ok': ok, 'detail': detail or (f'{len(vals)} value(s)' if vals else 'path not found')}
    if a.get('record'):
        out['actual'] = vals if '[*]' in a['path'] else (vals[0] if vals else None)
    else:
        out['actual_shape'] = describe(vals if '[*]' in a['path'] else (vals[0] if vals else None))
    return out


def header_assertions(spec: dict, hdrs: dict) -> list[dict]:
    out = []
    for name, rule in spec.items():
        v = hdrs.get(name.lower())
        for op, want in rule.items():
            ok = {'present': v is not None, 'absent': v is None, 'equals': v is not None and v.strip() == want,
                  'contains': v is not None and want in v}[op]
            out.append({'kind': 'header', 'path': name.lower(), 'op': op, 'expected': want if op in ('equals', 'contains') else True,
                        'ok': ok, 'detail': 'header present' if v is not None else 'header absent'})
    return out


def on_login_page(path: str, login_paths: list[str]) -> bool:
    p = path.split('?')[0].rstrip('/') or '/'
    return any(p == lp.rstrip('/') or p.startswith(lp.rstrip('/') + '/') for lp in login_paths)


def refusal(code: int | None, hdrs: dict, raw: bytes) -> str | None:
    """'rate_limited' or 'waf' when the target (not the application) refused the request."""
    if code == 429:
        return 'rate_limited'
    if code in (403, 503):
        if any(h in hdrs for h in WAF_HEADERS) or WAF_TEXT.search(raw[:20000].decode('utf-8', 'replace')):
            return 'waf'
        if code == 503 and hdrs.get('retry-after'):
            return 'rate_limited'
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # urllib raises HTTPError with the 3xx, and the runner decides whether to follow


class Runner:
    def __init__(self, plan: dict):
        self.plan = plan
        self.base = plan['target']['base_url'].rstrip('/')
        self.origin = urlsplit(self.base)[:2]
        self.rate = float(plan.get('rate_per_second', 2))
        self.last = 0.0
        self.requests: list[dict] = []
        self.blocked: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.secrets: list[str] = []
        self.logins = {cs['login']['path']: name for name, cs in plan.get('credential_sets', {}).items()}
        self.opener = urllib.request.build_opener(NoRedirect)

    def allowed(self, method: str, path: str) -> bool:
        return method in ('GET', 'HEAD') or (method == 'POST' and path in self.logins)

    def send(self, method: str, path: str, body: dict | None = None, token: str | None = None, purpose: str = 'check') -> tuple[int | None, bytes, dict, float]:
        if not self.allowed(method, path):
            raise PermissionError(f'{method} {path} is not allowed: the run is read-only')
        wait = self.last + 1 / self.rate - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Accept': '*/*', 'User-Agent': 'post-deploy-validator/1.1'})
        if data:
            req.add_header('Content-Type', 'application/json')
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        t0 = time.perf_counter()
        try:
            with self.opener.open(req, timeout=30) as r:
                raw, code, hdrs = r.read(), r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw, code, hdrs = e.read(), e.code, dict(e.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            self.requests.append({'method': method, 'path': path, 'status': None, 'ms': None, 'purpose': purpose, 'error': type(e).__name__})
            return None, b'', {}, 0.0
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.requests.append({'method': method, 'path': path, 'status': code, 'ms': ms, 'purpose': purpose})
        return code, raw, {k.lower(): v for k, v in hdrs.items()}, ms

    def fetch(self, method: str, path: str, token: str | None = None, purpose: str = 'check'):
        """Send a read and follow same-origin redirects, recording each hop. Returns
        (status, body, headers, total ms, hops, final path, offsite host or None)."""
        code, raw, hdrs, ms = self.send(method, path, token=token, purpose=purpose)
        hops, cur, offsite = [], path, None
        while code in (301, 302, 303, 307, 308) and hdrs.get('location') and len(hops) < 10:
            nxt = urljoin(self.base + cur, hdrs['location'])
            parts = urlsplit(nxt)
            loc = parts.path or '/'
            if parts[:2] != self.origin:
                offsite = parts.netloc
                hops.append({'status': code, 'to': f'{parts.scheme}://{parts.netloc}{loc}', 'followed': False})
                break
            hops.append({'status': code, 'to': loc, 'followed': True})
            cur = loc + (f'?{parts.query}' if parts.query else '')
            code, raw, hdrs, m2 = self.send('HEAD' if method == 'HEAD' else 'GET', cur, token=token, purpose='redirect')
            ms = round(ms + m2, 1)
        return code, raw, hdrs, ms, hops, cur.split('?')[0], offsite

    def sign_in(self, name: str) -> dict:
        cs = self.plan['credential_sets'][name]
        vals = {k: os.environ.get(v) for k, v in cs['env'].items()}
        if not all(vals.values()):
            return {'name': name, 'available': False, 'signed_in': False, 'reason': f'environment variables {", ".join(cs["env"].values())} not all set'}
        # every credential value is secret except a username (which may be asserted, like "smoke-reader")
        self.secrets += [v for k, v in vals.items() if v and k.lower() not in ('username', 'user', 'login', 'email')]
        body = {k: v.format(**vals) if isinstance(v, str) else v for k, v in cs['login'].get('body', {}).items()}
        code, raw, _, _ = self.send(cs['login'].get('method', 'POST'), cs['login']['path'], body, purpose=f'sign in ({name})')
        tok = None
        if code == 200:
            try:
                got = get_all(json.loads(raw), cs['login']['token_json_path'])
                tok = got[0] if got else None
            except ValueError:
                tok = None
        if tok:
            self.tokens[name] = tok
            self.secrets.append(tok)
        return {'name': name, 'available': True, 'signed_in': bool(tok), 'reason': None if tok else f'sign-in answered {code}'}


def expected_status_ok(code: int | None, exp: dict) -> bool:
    if code is None:
        return False
    return code == exp['status'] if 'status' in exp else 200 <= code < 300


def baseline_relation(cur: dict, base: dict | None) -> str | None:
    if not base:
        return None
    a, b = cur.get('deployment_id'), base.get('deployed', {}).get('deployment_id')
    if a is not None and b is not None:
        return 'same_deployment' if a == b else 'different_deployment'
    va, vb = cur.get('version'), base.get('deployed', {}).get('version')
    if va is not None and vb is not None:
        return 'same_version' if va == vb else 'different_version'
    return 'unknown'


def compare(m: float, bm: float, tol: float, mind: float) -> str:
    return 'regressed' if m > bm * (1 + tol / 100) and m - bm > mind else 'improved' if m < bm * (1 - tol / 100) and bm - m > mind else 'ok'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plan', type=Path)
    ap.add_argument('--expect-version', required=True, help='the version, deployment id or commit SHA the deploy should have put live')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--baseline', type=Path)
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    plan = json.loads(a.plan.read_text(encoding='utf-8'))
    errs = plan_errors(plan)
    if errs:
        print('the plan does not match schemas/pvt_plan.schema.json; nothing was sent:', file=sys.stderr)
        for e in errs:
            print('  -', e, file=sys.stderr)
        return 2
    tgt = plan['target']
    if tgt.get('environment') == 'production' and len((tgt.get('authorised_by') or '').strip()) < 3:
        raise SystemExit('a production target needs target.authorised_by in the plan')
    r = Runner(plan)
    started = now()
    login_paths = plan.get('login_paths') or LOGIN_PATHS
    # version and deployment ----------------------------------------------------------------------
    vs = plan['version']
    code, raw, hdrs, _, _, _, _ = r.fetch('GET', vs['path'], purpose='version')
    deployed, source = read_value(vs, raw, hdrs) if code else (None, None)
    dep_id, dep_source = None, None
    if plan.get('deployment'):
        ds = plan['deployment']
        if ds['path'] != vs['path']:
            code, raw, hdrs, _, _, _, _ = r.fetch('GET', ds['path'], purpose='deployment id')
        dep_id, dep_source = read_value(ds, raw, hdrs) if code else (None, None)
    exp_v = a.expect_version
    matches = None
    if deployed is not None or dep_id is not None:
        matches = exp_v in {str(x) for x in (deployed, dep_id) if x is not None}
    # checks --------------------------------------------------------------------------------------
    creds, results, five, stopped = {}, [], 0, None
    samples = int(plan.get('performance', {}).get('samples', 5))
    for c in plan['checks']:
        method = c.get('method', 'GET').upper()
        res = {'id': c['id'], 'kind': c['kind'], 'critical': bool(c.get('critical')), 'method': method, 'path': c['path'],
               'credential_set': c.get('credential_set'), 'timed': bool(c.get('timed'))}
        if stopped:
            results.append(dict(res, status='skipped', reason=stopped['skip_reason'])); continue
        if not r.allowed(method, c['path']):
            r.blocked.append({'check_id': c['id'], 'method': method, 'path': c['path'], 'reason': 'not a read: the run is read-only'})
            results.append(dict(res, status='blocked', reason=f'{method} is not allowed: the run is read-only, so this check was not sent')); continue
        token = None
        if c.get('credential_set'):
            if c['credential_set'] not in creds:
                creds[c['credential_set']] = r.sign_in(c['credential_set'])
            if not creds[c['credential_set']]['signed_in']:
                results.append(dict(res, status='blocked', reason=f'could not sign in with {c["credential_set"]}: {creds[c["credential_set"]]["reason"]}')); continue
            token = r.tokens[c['credential_set']]
        at = datetime.now(timezone.utc)
        code, raw, hdrs, ms, hops, final_path, offsite = r.fetch(method, c['path'], token=token)
        if code is None:
            results.append(dict(res, status='error', reason='no response (connection failed or timed out)')); continue
        exp = c.get('expect', {})
        why = refusal(code, hdrs, raw)
        if why:
            what = 'rate limited' if why == 'rate_limited' else 'blocked by a WAF or bot challenge'
            stopped = {'cause': why, 'after_check': c['id'], 'http_status': code,
                       'skip_reason': f'not sent: the run stopped when the target {what} check {c["id"]} ({code})'}
            results.append(dict(res, status='error', http_status=code, headers=safe_headers(hdrs), redirects=hops,
                                reason=f'the target {what} the request ({code}): a refusal by the platform, not an answer from the application'))
            continue
        five = five + 1 if code >= 500 else 0 if code < 400 else five
        # timing: sampled only while the check answers as it should, so a slow error page is never "a regression"
        timing = None
        if c.get('timed'):
            comparable = expected_status_ok(code, exp) and not offsite
            ts, cache, reason = [ms], [hdrs.get('x-vercel-cache') or hdrs.get('cf-cache-status') or hdrs.get('x-cache')], None
            if comparable:
                for _ in range(samples - 1):
                    c2, _, h2, m2, _, _, _ = r.fetch(method, c['path'], token=token, purpose='timing sample')
                    if c2 != code:
                        comparable, reason = False, f'a sample answered {c2} where the first answered {code}; sampling stopped'
                        break
                    ts.append(m2)
                    cache.append(h2.get('x-vercel-cache') or h2.get('cf-cache-status') or h2.get('x-cache'))
            else:
                reason = (f'answered {code}' + (f' (expected {exp["status"]})' if 'status' in exp else '') +
                          ': a failing response is timed once and not compared')
            timing = {'samples_ms': ts, 'median_ms': round(statistics.median(ts), 1), 'comparable': comparable, 'reason': reason,
                      'cache': cache if any(cache) else None}
        asserts = []
        text = raw.decode('utf-8', 'replace')
        if 'status' in exp:
            asserts.append({'kind': 'http_status', 'path': 'http', 'op': 'eq', 'expected': exp['status'], 'ok': code == exp['status'], 'actual': code})
        for s in exp.get('contains', []):
            asserts.append({'kind': 'body', 'path': 'body', 'op': 'contains', 'expected': s, 'ok': s in text})
        for s in exp.get('not_contains', []):
            asserts.append({'kind': 'body', 'path': 'body', 'op': 'not_contains', 'expected': s, 'ok': s not in text})
        if exp.get('json'):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            for ja in exp['json']:
                asserts.append(check_assertion(body, ja, at) if body is not None else
                               {'kind': 'json', 'path': ja['path'], 'op': ja['op'], 'expected': ja.get('value'), 'ok': False, 'detail': 'response is not JSON'})
        asserts += header_assertions(exp.get('headers', {}), hdrs)
        if exp.get('no_redirect'):
            asserts.append({'kind': 'redirect', 'path': 'redirects', 'op': 'none', 'expected': 0, 'ok': not hops, 'actual': len(hops)})
        if exp.get('final_path'):
            asserts.append({'kind': 'redirect', 'path': 'final_path', 'op': 'eq', 'expected': exp['final_path'], 'ok': final_path == exp['final_path'],
                            'actual': final_path})
        landing = urlsplit(hops[-1]['to']).path if offsite else final_path
        if hops and not c.get('credential_set') and not exp.get('allow_login_redirect') and on_login_page(landing, login_paths):
            asserts.append({'kind': 'redirect', 'path': 'final_path', 'op': 'not_login', 'expected': 'not a sign-in page', 'ok': False,
                            'actual': hops[-1]['to']})
        if offsite:
            asserts.append({'kind': 'redirect', 'path': 'final_path', 'op': 'same_origin', 'expected': 'the target origin', 'ok': False,
                            'actual': hops[-1]['to']})
        res.update(status='pass' if all(x['ok'] for x in asserts) else 'fail', http_status=code, bytes=len(raw),
                   headers=safe_headers(hdrs), redirects=hops, final_path=final_path, assertions=asserts, timing=timing)
        results.append(res)
        if five >= 3:
            stopped = {'cause': 'app_5xx', 'after_check': c['id'], 'http_status': code,
                       'skip_reason': f'not sent: the run stopped after three 5xx answers in a row (the last from {c["id"]}); '
                                      'no rate-limit or WAF signature, so the application itself looks down'}
    # baseline ------------------------------------------------------------------------------------
    sha = plan_sha(plan)
    perf = plan.get('performance', {})
    tol, mind = float(perf.get('tolerance_pct', 20)), float(perf.get('min_delta_ms', 50))
    base = json.loads(a.baseline.read_text(encoding='utf-8')) if a.baseline else None
    comparisons, base_note = [], None
    if base and (base['plan']['sha256'] != sha or base['target']['base_url'] != tgt['base_url']):
        base_note = 'the baseline run used another plan or target, so it was not compared'
        base = None
    dep = {'version': deployed, 'expected': exp_v, 'matches': matches, 'source': source, 'deployment_id': dep_id, 'deployment_source': dep_source}
    relation = baseline_relation(dep, base)
    if relation == 'same_deployment':
        base_note = (f'the baseline run is the same deployment ({dep_id}): the comparison shows drift on one deployment, '
                     'not a change between releases')
    elif relation == 'same_version':
        base_note = (f'the baseline run reported the same version ({deployed}) and no deployment id: it may be the same deployment, '
                     'so the comparison may show drift rather than a change between releases')
    bmap = {x['id']: x for x in (base or {}).get('checks', [])}
    for x in results:
        if not x.get('timing'):
            continue
        b = bmap.get(x['id'], {}).get('timing')
        m = x['timing']['median_ms']
        if not x['timing']['comparable']:
            comparisons.append({'check_id': x['id'], 'median_ms': m, 'baseline_median_ms': b['median_ms'] if b else None, 'change_pct': None,
                                'status': 'not_comparable', 'reason': x['timing']['reason']}); continue
        if not b:
            comparisons.append({'check_id': x['id'], 'median_ms': m, 'baseline_median_ms': None, 'change_pct': None, 'status': 'no_baseline'}); continue
        bm = b['median_ms']
        if not b.get('comparable', True):
            comparisons.append({'check_id': x['id'], 'median_ms': m, 'baseline_median_ms': bm, 'change_pct': None, 'status': 'not_comparable',
                                'reason': 'the baseline run timed a failing response for this check'}); continue
        ch = round((m - bm) / bm * 100, 1) if bm else None
        comparisons.append({'check_id': x['id'], 'median_ms': m, 'baseline_median_ms': bm, 'change_pct': ch, 'status': compare(m, bm, tol, mind)})
    out = {'schema_version': '1.1.0', 'plan': {'path': a.plan.as_posix(), 'sha256': sha, 'name': plan.get('name')},
           'target': {k: tgt.get(k) for k in ('base_url', 'environment', 'authorised_by')},
           'started_at': started, 'finished_at': now(), 'deployed': dep,
           'credential_sets': list(creds.values()), 'checks': results,
           'stopped': {k: v for k, v in stopped.items() if k != 'skip_reason'} if stopped else None,
           'requests': r.requests, 'blocked': r.blocked,
           'performance': {'baseline_run': a.baseline.as_posix() if a.baseline and base else None, 'baseline_note': base_note,
                           'baseline_relation': relation, 'baseline_deployment_id': (base or {}).get('deployed', {}).get('deployment_id'),
                           'tolerance_pct': tol, 'min_delta_ms': mind, 'samples': samples, 'comparisons': comparisons}}
    text = json.dumps(out, indent=1, ensure_ascii=False)
    for s in r.secrets:  # belt and braces: nothing secret may reach the file
        if s and s in text:
            raise SystemExit('a credential or token value reached the run record; nothing was written')
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'run.json').write_text(text + '\n', encoding='utf-8', newline='\n')
    print(f'{tgt["base_url"]}: deployed {deployed} / {dep_id} (expected {exp_v}); ' + ', '.join(f'{x["id"]} {x["status"]}' for x in results))
    if stopped:
        print(f'  stopped: {stopped["cause"]} after {stopped["after_check"]}')
    for c in comparisons:
        print(f'  {c["check_id"]:14} {c["median_ms"]:>8} ms  baseline {c["baseline_median_ms"]}  {c["status"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
