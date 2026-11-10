#!/usr/bin/env python3
"""Run a safe proof-of-concept against a local or test instance and record the evidence.

    python run_poc.py poc.json --target http://127.0.0.1:5057 --permission "<what the user said>"
                      --out evidence/F-1.json [--allow-host staging.internal.test]
    python run_poc.py poc.json --check          # lint the payloads only; send nothing

poc.json:
    {"finding": "F-1",
     "steps": [{"name": "login", "method": "POST", "path": "/login", "form": {...}},
               {"name": "probe", "method": "GET", "path": "/search", "params": {"q": "x' OR 1=1 --"}}],
     "proof": {"type": "marker_in_response", "step": "probe", "marker": "INV-2001"}}

Step fields: name, method, path, params, form, json, headers, cookies, expect_status. Strings may
use {target} and {env:NAME}; env values are redacted in the evidence. A cookie jar carries cookies
between steps. Redirects are never followed (the Location header is recorded).

Proof types:
    marker_in_response  {step, marker}                 marker appears in the response body/headers
    boolean_diff        {true_step, false_step, marker} marker in the true response, absent from the false one
    header_matches      {step, header, pattern}        a response header matches a regex
    status              {step, status}                 the response has this status

Safety (checked before anything is sent, and again by the validator):
  - the target must be loopback, or a host passed with --allow-host (a test environment the user
    named); --permission records what the user agreed to;
  - payloads must be canaries: no destructive SQL or shell commands, no time-based payloads, no
    cloud metadata addresses, no code-execution primitives, no reverse shells or persistence, and
    URLs only to the target or reserved test domains;
  - a marker must not appear in the request itself, unless the proof is that input is reflected
    unescaped (set "reflection_is_the_bug": true);
  - at most 30 requests, 10 s timeout each.
Exit codes: 0 confirmed, 1 not confirmed, 2 refused or invalid.
"""
from __future__ import annotations
import argparse, http.cookiejar, ipaddress, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan_secrets  # noqa: E402

VERSION = '1.0.0'
MAX_STEPS = 30
BANNED = [
    ('destructive SQL', r'(?i)\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)\s+(TABLE|DATABASE|SCHEMA|USER|INDEX|VIEW|ROLE)\b|\bDELETE\s+FROM\b|\bUPDATE\s+\w+\s+SET\b|\bINSERT\s+INTO\b|\bxp_cmdshell\b|;\s*SHUTDOWN\b'),
    ('time-based payload', r'(?i)\b(SLEEP|pg_sleep|BENCHMARK|dbms_lock\.sleep|dbms_pipe\.receive_message)\s*\(|\bWAITFOR\s+DELAY\b|\brandomblob\s*\(\s*\d{6,}|\bsleep\s+\d|time\.sleep'),
    ('destructive command', r'(?i)\brm\s+-\w|\brmdir\b|\bdel\s+/|\bformat\s+[a-z]:|\bmkfs\b|\bdd\s+if=|\bshutdown\b|\breboot\b|:\(\)\s*\{|\bkill\s+-9|\bchmod\s+-R|>\s*/dev/sd|\btruncate\s+-s'),
    ('reverse shell or exfiltration', r'(?i)/dev/(tcp|udp)/|\bnc(at)?\s+-\w*[elc]|\bbash\s+-i\b|\bpowershell\b.*-e(nc|ncodedcommand)?\b|Invoke-WebRequest|\bcurl\s|\bwget\s|\bscp\s|\bftp\s|\bsocat\b'),
    ('persistence', r'(?i)crontab|authorized_keys|\.bashrc|\.profile\b|useradd|net\s+user\s|schtasks|/etc/(passwd|shadow|sudoers)\s*>'),
    ('cloud metadata address', r'(?i)169\.254\.169\.254|metadata\.google\.internal|100\.100\.100\.200|fd00:ec2::254|metadata\.azure\.com|\[::ffff:a9fe:a9fe\]'),
    ('code-execution primitive', r'(?i)__import__|\bos\.(system|popen)|\bsubprocess\b|__globals__|__subclasses__|__builtins__|Runtime\.getRuntime|ProcessBuilder|child_process|\beval\s*\(|\bexec\s*\(|!!python/|java\.lang\.Runtime|System\.Diagnostics\.Process'),
]
SAFE_HOST = re.compile(r'(?i)^(localhost|127(\.\d{1,3}){3}|\[?::1\]?|[\w.-]+\.(example|test|invalid|localhost)|example\.(com|org|net)|[\w.-]+\.example\.(com|org|net))$')

def walk_strings(obj, path='$'):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(k, f'{path}.<key>')
            yield from walk_strings(v, f'{path}.{k}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f'{path}[{i}]')

def is_loopback(host: str) -> bool:
    host = host.strip('[]')
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def lint(poc: dict, target: str | None, allow_hosts: list[str]) -> list[str]:
    """Return a list of safety problems; empty means the PoC may be sent."""
    issues = []
    steps = poc.get('steps') or []
    if not steps:
        issues.append('no steps')
    if len(steps) > MAX_STEPS:
        issues.append(f'{len(steps)} steps: at most {MAX_STEPS} requests are allowed')
    if target:
        host = urllib.parse.urlsplit(target).hostname or ''
        if not (is_loopback(host) or host in allow_hosts):
            issues.append(f'target host {host!r} is not loopback and was not named with --allow-host')
    target_host = urllib.parse.urlsplit(target).hostname if target else None
    payload_strings = list(walk_strings([{k: v for k, v in s.items() if k not in ('name', 'expect_status')} for s in steps]))
    for where, text in payload_strings:
        for label, rx in BANNED:
            if re.search(rx, text):
                issues.append(f'{label} in {where}: {text[:80]!r}')
        for url in re.findall(r'(?i)\b(?:https?|ftp|gopher|file|dict)://([^/\s"\'?#]+)', text):
            host = url.rsplit('@', 1)[-1].split(':')[0].strip('[]')
            if host and host not in ('{target}',) and not SAFE_HOST.match(host) and host != target_host and host not in allow_hosts:
                issues.append(f'URL to a non-test host in {where}: {host}')
        if re.search(r'(?i)\b(file|gopher|dict)://', text):
            issues.append(f'non-HTTP URL scheme in {where}')
    for s in steps:
        if (s.get('method') or 'GET').upper() in ('DELETE', 'PUT', 'PATCH') and not s.get('state_change_ok'):
            issues.append(f"step {s.get('name')!r} uses {s['method']}: set state_change_ok with the reason it only touches test data")
    proof = poc.get('proof') or {}
    names = {s.get('name') for s in steps}
    ptype = proof.get('type')
    if ptype not in ('marker_in_response', 'boolean_diff', 'header_matches', 'status'):
        issues.append(f'unknown proof type {ptype!r}')
    for k in ('step', 'true_step', 'false_step'):
        if proof.get(k) and proof[k] not in names:
            issues.append(f'proof {k} {proof[k]!r} is not a step name')
    marker = proof.get('marker')
    if ptype in ('marker_in_response', 'boolean_diff') and not marker:
        issues.append('proof needs a marker')
    if marker and len(str(marker)) < 3:
        issues.append('marker is too short to be meaningful (at least 3 characters)')
    if marker and not proof.get('reflection_is_the_bug') and any(str(marker) in t for _, t in payload_strings):
        issues.append('the marker appears in the request itself, so finding it in the response proves nothing '
                      '(use a computed marker such as {{7*191}} -> 1337, or set reflection_is_the_bug)')
    return issues

def substitute(obj, target: str, used_env: dict):
    if isinstance(obj, str):
        def env(m):
            used_env[m.group(1)] = os.environ.get(m.group(1), '')
            return used_env[m.group(1)]
        return re.sub(r'\{env:(\w+)\}', env, obj.replace('{target}', target.rstrip('/')))
    if isinstance(obj, dict):
        return {k: substitute(v, target, used_env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, target, used_env) for v in obj]
    return obj

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None

def scrub(text: str, env_values: dict) -> str:
    for name, val in env_values.items():
        if val:
            text = text.replace(val, f'<env:{name}>')
    for s in scan_secrets.find_in_line(text):
        text = text.replace(s['value'], '[REDACTED]')
    return text

def send(opener, target: str, step: dict, env_values: dict) -> dict:
    method = (step.get('method') or 'GET').upper()
    url = target.rstrip('/') + '/' + (step.get('path') or '/').lstrip('/')
    if step.get('params'):
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(step['params'])
    headers = dict(step.get('headers') or {})
    data = None
    if step.get('form') is not None:
        data = urllib.parse.urlencode(step['form']).encode()
        headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    elif step.get('json') is not None:
        data = json.dumps(step['json']).encode()
        headers.setdefault('Content-Type', 'application/json')
    if step.get('cookies'):
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in step['cookies'].items())
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        resp = opener.open(req, timeout=10)
        status, rheaders, body = resp.status, dict(resp.headers), resp.read(200_000)
    except urllib.error.HTTPError as e:
        status, rheaders, body = e.code, dict(e.headers or {}), e.read(200_000) if e.fp else b''
    elapsed = int((time.monotonic() - t0) * 1000)
    text = body.decode('utf-8', errors='replace')
    shown_headers = {k: v for k, v in headers.items() if k.lower() != 'cookie'}
    if 'Cookie' in headers:
        shown_headers['Cookie'] = '; '.join(f'{c.split("=", 1)[0].strip()}=<redacted>' for c in headers['Cookie'].split(';'))
    keep = {k: (re.sub(r'=([^;]+)', '=<redacted>', v, count=1) if k.lower() == 'set-cookie' else v)
            for k, v in rheaders.items() if k.lower() in ('location', 'content-type', 'content-length', 'set-cookie', 'server')}
    return {
        'name': step.get('name'),
        'request': {'method': method, 'url': scrub(url, env_values), 'headers': {k: scrub(str(v), env_values) for k, v in shown_headers.items()},
                    'body': scrub(data.decode('utf-8', errors='replace'), env_values)[:2000] if data else None},
        'response': {'status': status, 'headers': keep, 'body_excerpt': scrub(text[:1500], env_values), 'body_length': len(text),
                     'elapsed_ms': elapsed},
        '_full_text': text, '_headers': rheaders,
        'expect_status_met': (status == step['expect_status']) if step.get('expect_status') else None,
    }

def evaluate(proof: dict, results: dict) -> tuple[bool, str]:
    t = proof['type']
    def body(name):
        r = results.get(name)
        return '' if not r else r['_full_text'] + '\n' + '\n'.join(f'{k}: {v}' for k, v in r['_headers'].items())
    if t == 'marker_in_response':
        ok = str(proof['marker']) in body(proof['step'])
        return ok, f"marker {'found' if ok else 'not found'} in the response to {proof['step']!r}"
    if t == 'boolean_diff':
        a, b = str(proof['marker']) in body(proof['true_step']), str(proof['marker']) in body(proof['false_step'])
        return a and not b, f"marker in true-condition response: {a}; in false-condition response: {b}"
    if t == 'header_matches':
        r = results.get(proof['step'])
        val = next((v for k, v in (r['_headers'] if r else {}).items() if k.lower() == proof['header'].lower()), None)
        ok = val is not None and re.search(proof['pattern'], val) is not None
        return ok, f"{proof['header']}: {val!r} {'matches' if ok else 'does not match'} {proof['pattern']!r}"
    if t == 'status':
        r = results.get(proof['step'])
        ok = bool(r) and r['response']['status'] == proof['status']
        return ok, f"status {r['response']['status'] if r else None} (expected {proof['status']})"
    return False, 'unknown proof type'

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('poc', type=Path)
    ap.add_argument('--target', help='base URL of the local or test instance')
    ap.add_argument('--permission', help="what the user said when allowing verification (recorded in the evidence)")
    ap.add_argument('--allow-host', nargs='*', default=[], help='non-loopback test hosts the user named')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--check', action='store_true', help='lint only; send nothing')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    try:
        poc = json.loads(args.poc.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'Unable to read {args.poc}: {exc}', file=sys.stderr)
        return 2
    issues = lint(poc, args.target, args.allow_host)
    if args.check:
        print('\n'.join(f'REFUSED: {i}' for i in issues) or 'payloads pass the safety checks')
        return 2 if issues else 0
    if not args.target or not args.permission or not args.out:
        print('--target, --permission and --out are required to send requests', file=sys.stderr)
        return 2
    if issues:
        for i in issues:
            print(f'REFUSED: {i}', file=sys.stderr)
        return 2
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    used_env: dict = {}
    steps = substitute(poc['steps'], args.target, used_env)
    started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    results, records, error = {}, [], None
    for step in steps:
        try:
            r = send(opener, args.target, step, used_env)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error = f"step {step.get('name')!r}: {exc}"
            break
        results[step.get('name')] = r
        records.append({k: v for k, v in r.items() if not k.startswith('_')})
    passed, observed = (False, error) if error else evaluate(poc['proof'], results)
    evidence = {
        'tool': 'run_poc', 'version': VERSION, 'finding': poc.get('finding'), 'poc_file': str(args.poc),
        'target': args.target, 'permission': args.permission, 'allow_hosts': args.allow_host,
        'started_at': started, 'finished_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'safety': {'checked': True, 'issues': []},
        'steps': records,
        'proof': {**poc['proof'], 'passed': passed, 'observed': observed},
        'result': 'confirmed' if passed else 'not_confirmed',
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"{evidence['result']}: {observed} -> {args.out}")
    return 0 if passed else 1

if __name__ == '__main__':
    raise SystemExit(main())
