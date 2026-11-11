#!/usr/bin/env python3
"""Run a post-deploy check plan against a live environment, read-only (stdlib only).

    python run_pvt.py pvt_plan.json --expect-version 1.9.0 --out runs/<stamp>/ [--baseline runs/<previous>/run.json]

The request guard allows GET and HEAD, and the sign-in request of a credential set the plan names.
Anything else (a POST to create something, a PUT, a DELETE) is refused before it is sent and recorded as
blocked. A production target needs `authorised_by` in the plan.

Credentials come from the environment variables the plan names for each set (for example
PVT_SMOKE_READER_USERNAME and PVT_SMOKE_READER_PASSWORD) and never leave memory: they are not printed,
logged or written, and Authorization, Cookie and Set-Cookie values are never recorded. A response value is
recorded only for an assertion marked `record: true`; otherwise the result says whether it passed, with the
value's type and length.

Timed checks take `performance.samples` requests; the median is compared with the same check in the
baseline run (the skill's previous run on this target with the same plan). The plan's SHA-256 (without the
expected version) and the target are recorded so the next run can find its baseline. Requests are paced to
`rate_per_second`, and the run stops after three checks in a row answer 5xx.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, statistics, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

SAFE_HEADERS = ('content-type', 'x-app-version', 'cache-control', 'content-length', 'server')


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def plan_sha(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
    out = {'path': a['path'], 'op': op, 'expected': want, 'ok': ok, 'detail': detail or (f'{len(vals)} value(s)' if vals else 'path not found')}
    if a.get('record'):
        out['actual'] = vals if '[*]' in a['path'] else (vals[0] if vals else None)
    else:
        out['actual_shape'] = describe(vals if '[*]' in a['path'] else (vals[0] if vals else None))
    return out


class Runner:
    def __init__(self, plan: dict):
        self.plan = plan
        self.base = plan['target']['base_url'].rstrip('/')
        self.rate = float(plan.get('rate_per_second', 2))
        self.last = 0.0
        self.requests: list[dict] = []
        self.blocked: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.secrets: list[str] = []
        self.logins = {cs['login']['path']: name for name, cs in plan.get('credential_sets', {}).items()}

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
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Accept': '*/*', 'User-Agent': 'post-deploy-validator/1.0'})
        if data:
            req.add_header('Content-Type', 'application/json')
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, code, hdrs = r.read(), r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw, code, hdrs = e.read(), e.code, dict(e.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            self.requests.append({'method': method, 'path': path, 'status': None, 'ms': None, 'purpose': purpose, 'error': type(e).__name__})
            return None, b'', {}, 0.0
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.requests.append({'method': method, 'path': path, 'status': code, 'ms': ms, 'purpose': purpose})
        return code, raw, {k.lower(): v for k, v in hdrs.items()}, ms

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plan', type=Path)
    ap.add_argument('--expect-version', required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--baseline', type=Path)
    a = ap.parse_args()
    plan = json.loads(a.plan.read_text(encoding='utf-8'))
    tgt = plan['target']
    if tgt.get('environment') == 'production' and len((tgt.get('authorised_by') or '').strip()) < 3:
        raise SystemExit('a production target needs target.authorised_by in the plan')
    r = Runner(plan)
    started = now()
    # version -------------------------------------------------------------------------------------
    vs = plan['version']
    code, raw, hdrs, _ = r.send('GET', vs['path'], purpose='version')
    deployed, source = None, None
    if code == 200 and vs.get('json_path'):
        try:
            got = get_all(json.loads(raw), vs['json_path'])
            deployed, source = (got[0], f'{vs["path"]} {vs["json_path"]}') if got else (None, None)
        except ValueError:
            pass
    if deployed is None and vs.get('header') and hdrs.get(vs['header'].lower()):
        deployed, source = hdrs[vs['header'].lower()], f'header {vs["header"]}'
    # checks --------------------------------------------------------------------------------------
    creds, results, five = {}, [], 0
    samples = int(plan.get('performance', {}).get('samples', 5))
    for c in plan['checks']:
        method = c.get('method', 'GET').upper()
        res = {'id': c['id'], 'kind': c['kind'], 'critical': bool(c.get('critical')), 'method': method, 'path': c['path'],
               'credential_set': c.get('credential_set'), 'timed': bool(c.get('timed'))}
        if five >= 3:
            results.append(dict(res, status='skipped', reason='stopped after three checks in a row answered 5xx')); continue
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
        code, raw, hdrs, ms = r.send(method, c['path'], token=token)
        timing = [ms] if code else []
        if c.get('timed') and code:
            for _ in range(samples - 1):
                c2, _, _, m2 = r.send(method, c['path'], token=token, purpose='timing sample')
                if c2:
                    timing.append(m2)
        if code is None:
            results.append(dict(res, status='error', reason='no response (connection failed or timed out)')); continue
        five = five + 1 if code >= 500 else 0
        exp = c.get('expect', {})
        asserts = []
        text = raw.decode('utf-8', 'replace')
        if 'status' in exp:
            asserts.append({'path': 'status', 'op': 'eq', 'expected': exp['status'], 'ok': code == exp['status'], 'actual': code})
        for s in exp.get('contains', []):
            asserts.append({'path': 'body', 'op': 'contains', 'expected': s, 'ok': s in text})
        for s in exp.get('not_contains', []):
            asserts.append({'path': 'body', 'op': 'not_contains', 'expected': s, 'ok': s not in text})
        if exp.get('json'):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            for ja in exp['json']:
                asserts.append(check_assertion(body, ja, at) if body is not None else {'path': ja['path'], 'op': ja['op'], 'expected': ja.get('value'), 'ok': False, 'detail': 'response is not JSON'})
        res.update(status='pass' if all(x['ok'] for x in asserts) else 'fail', http_status=code, bytes=len(raw),
                   headers={k: v for k, v in hdrs.items() if k in SAFE_HEADERS}, assertions=asserts,
                   timing={'samples_ms': timing, 'median_ms': round(statistics.median(timing), 1)} if c.get('timed') and timing else None)
        results.append(res)
    # baseline ------------------------------------------------------------------------------------
    sha = plan_sha(plan)
    perf = plan.get('performance', {})
    tol, mind = float(perf.get('tolerance_pct', 20)), float(perf.get('min_delta_ms', 50))
    base = json.loads(a.baseline.read_text(encoding='utf-8')) if a.baseline else None
    comparisons, base_note = [], None
    if base and (base['plan']['sha256'] != sha or base['target']['base_url'] != tgt['base_url']):
        base_note = 'the baseline run used another plan or target, so it was not compared'
        base = None
    bmap = {x['id']: x for x in (base or {}).get('checks', [])}
    for x in results:
        if not x.get('timing'):
            continue
        b = bmap.get(x['id'], {}).get('timing')
        if not b:
            comparisons.append({'check_id': x['id'], 'median_ms': x['timing']['median_ms'], 'baseline_median_ms': None, 'change_pct': None, 'status': 'no_baseline'}); continue
        m, bm = x['timing']['median_ms'], b['median_ms']
        ch = round((m - bm) / bm * 100, 1) if bm else None
        st = ('regressed' if m > bm * (1 + tol / 100) and m - bm > mind else 'improved' if m < bm * (1 - tol / 100) and bm - m > mind else 'ok')
        comparisons.append({'check_id': x['id'], 'median_ms': m, 'baseline_median_ms': bm, 'change_pct': ch, 'status': st})
    out = {'schema_version': '1.0.0', 'plan': {'path': a.plan.as_posix(), 'sha256': sha, 'name': plan.get('name')},
           'target': {k: tgt.get(k) for k in ('base_url', 'environment', 'authorised_by')},
           'started_at': started, 'finished_at': now(),
           'deployed': {'version': deployed, 'expected': a.expect_version, 'matches': deployed == a.expect_version if deployed is not None else None, 'source': source},
           'credential_sets': list(creds.values()), 'checks': results, 'requests': r.requests, 'blocked': r.blocked,
           'performance': {'baseline_run': a.baseline.as_posix() if a.baseline and base else None, 'baseline_note': base_note,
                           'tolerance_pct': tol, 'min_delta_ms': mind, 'samples': samples, 'comparisons': comparisons}}
    text = json.dumps(out, indent=1, ensure_ascii=False)
    for s in r.secrets:  # belt and braces: nothing secret may reach the file
        if s and s in text:
            raise SystemExit('a credential or token value reached the run record; nothing was written')
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'run.json').write_text(text + '\n', encoding='utf-8', newline='\n')
    print(f'{tgt["base_url"]}: deployed {deployed} (expected {a.expect_version}); ' + ', '.join(f'{x["id"]} {x["status"]}' for x in results))
    for c in comparisons:
        print(f'  {c["check_id"]:14} {c["median_ms"]:>8} ms  baseline {c["baseline_median_ms"]}  {c["status"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
