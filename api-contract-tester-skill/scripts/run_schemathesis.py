#!/usr/bin/env python3
"""Run Schemathesis (MIT) when it's installed, in the same safe mode as send_cases.py, and normalise its failures.

    python run_schemathesis.py openapi.yaml --base-url http://127.0.0.1:8140 --out run/schemathesis.json
        [--st PATH_TO_st] [--seed N] [--max-examples 50] [--rate 5] [--bearer-env NAME]
        [--authorised-by WHO] [--allow-writes --confirm-non-production] [--production-host HOST ...]
        [--exclude-operation-id ID ...]

When Schemathesis isn't found, the output records it as not_run and nothing is sent; the stdlib
generator (gen_cases.py) still runs.

Safe mode (no --allow-writes): only GET, HEAD and OPTIONS operations are selected, and everything that
sends other methods or creates resources is turned off: the coverage phase's "unexpected methods"
(by default it sends PUT, POST, DELETE, PATCH and TRACE to every path, whatever the method filter says;
a config file sets it to none), the unsupported_method, use_after_free, ensure_resource_availability
and object_level_authorization checks, and the stateful phase. Writes need the same three things as
send_cases.py.

Output: {tool{name, version, status, note}, command (credentials removed), safe_mode, report_dir,
failures[{operation, code, title, check, schemathesis_severity, message, evidence}]}. Each failure is
one (failure type, operation). Schemathesis findings are candidates like any other, but they have no
saved exchange of our own: reproduce one with a stdlib case (gen_cases.py --add-cases) before
promoting it, or record it as needs_review.
"""
from __future__ import annotations
import argparse, glob, json, os, re, shutil, subprocess, sys, urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_cases import is_loopback  # noqa: E402

CODE_TO_CHECK = {
    'ST101': 'server_error', 'ST103': 'status_documented', 'ST104': 'content_type', 'ST105': 'required_headers',
    'ST108': 'schema_conformance', 'ST201': 'negative_accepted', 'ST202': 'positive_rejected',
    'ST203': 'auth_enforced', 'ST401': 'auth_enforced',
}
WRITE_ONLY_CHECKS = 'unsupported_method,use_after_free,ensure_resource_availability,object_level_authorization'


def find_st(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() or shutil.which(explicit) else None
    return shutil.which('st') or shutil.which('schemathesis')


def junit_messages(path: str) -> dict[tuple[str, str], str]:
    out = {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return out
    for tc in root.iter('testcase'):
        op = tc.get('name', '')
        for f in list(tc.findall('failure')) + list(tc.findall('error')):
            text = f.text or ''
            for m in re.finditer(r'^- (ST\d{3}) (.+?)$\n([\s\S]*?)(?=^- ST\d{3} |^\[\d{3}\]|^\d+\. Test Case ID|\Z)', text, re.M):
                out.setdefault((op, m.group(1)), (m.group(2) + '\n' + m.group(3)).strip()[:1500])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('contract', help='the contract file or URL Schemathesis should load')
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--st')
    ap.add_argument('--seed', type=int)
    ap.add_argument('--max-examples', type=int, default=50)
    ap.add_argument('--rate', type=float, default=5.0)
    ap.add_argument('--bearer-env')
    ap.add_argument('--authorised-by')
    ap.add_argument('--allow-writes', action='store_true')
    ap.add_argument('--confirm-non-production', action='store_true')
    ap.add_argument('--production-host', action='append', default=[])
    ap.add_argument('--exclude-operation-id', action='append', default=[])
    ap.add_argument('--timeout', type=int, default=1800)
    a = ap.parse_args()

    out = {'tool': {'name': 'schemathesis', 'version': None, 'status': 'not_run', 'note': None}, 'command': None,
           'safe_mode': not a.allow_writes, 'report_dir': None, 'failures': []}
    host = urllib.parse.urlsplit(a.base_url).hostname or ''
    st = find_st(a.st)
    if not st:
        out['tool']['note'] = 'Schemathesis is not installed; the stdlib generator ran instead'
    elif not is_loopback(host) and not a.authorised_by:
        out['tool']['note'] = f'{host} is not loopback and no --authorised-by was given; not run'
    elif a.allow_writes and not (a.authorised_by and a.confirm_non_production and host not in a.production_host):
        out['tool']['note'] = '--allow-writes needs --authorised-by, --confirm-non-production and a non-production host; not run'
    else:
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', COLUMNS='400')   # unwrapped, so paths can be made relative
        ver = subprocess.run([st, '--version'], capture_output=True, text=True, env=env, timeout=60).stdout.strip()
        out['tool']['version'] = (re.search(r'(\d+\.\d+\.\d+)', ver) or [None, ver])[1]
        report_dir = a.out.parent / 'schemathesis'
        report_dir.mkdir(parents=True, exist_ok=True)
        cmd = [st]
        if not a.allow_writes:
            # The coverage phase sends PUT, POST, DELETE, PATCH and TRACE to every path by default ("unexpected
            # methods"), whatever --include-method says. Safe mode turns that off in a config file.
            cfg = report_dir / 'safe-mode.schemathesis.toml'
            cfg.write_text('[phases.coverage]\nunexpected-methods = []\n', encoding='utf-8')
            cmd += ['--config-file', str(cfg)]
        cmd += ['run', a.contract, '--url', a.base_url, '--checks', 'all', '-w', '1', '-n', str(a.max_examples),
               '--rate-limit', f'{int(a.rate) if a.rate >= 1 else 1}/s', '--report', 'json,junit,har', '--report-dir', str(report_dir),
               '--output-sanitize', 'true']
        if a.seed is not None:
            cmd += ['--seed', str(a.seed)]
        if not a.allow_writes:
            cmd += ['--include-method-regex', '^(GET|HEAD|OPTIONS)$', '--exclude-checks', WRITE_ONLY_CHECKS,
                    '--phases', 'examples,coverage,fuzzing']
        for oid in a.exclude_operation_id:
            cmd += ['--exclude-operation-id', oid]
        shown = [Path(st).stem if i == 0 else c for i, c in enumerate(cmd)]   # no local paths in the record
        if a.bearer_env:
            cmd += ['-H', f'Authorization: Bearer {os.environ[a.bearer_env]}']
            shown += ['-H', f'Authorization: Bearer ${a.bearer_env}']
        out['command'] = ' '.join(shown)
        out['report_dir'] = report_dir.as_posix()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=a.timeout, encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            out['tool'].update(status='errored', note=f'timed out after {a.timeout}s')
        else:
            stdout = p.stdout.replace(str(report_dir.resolve()), report_dir.as_posix()).replace(str(Path(st).resolve()), Path(st).stem).replace(str(Path.cwd()) + os.sep, '')
            (report_dir / 'stdout.txt').write_text(stdout[-200000:], encoding='utf-8')
            jsons = sorted(glob.glob(str(report_dir / 'json-*.json')))
            junits = sorted(glob.glob(str(report_dir / 'junit-*.xml')))
            if not jsons:
                out['tool'].update(status='errored', note=f'exit {p.returncode} and no JSON report: {(p.stderr or p.stdout)[-300:]}')
            else:
                rep = json.loads(Path(jsons[-1]).read_text(encoding='utf-8'))
                msgs = junit_messages(junits[-1]) if junits else {}
                out['tool'].update(status='ran', note=f'{rep.get("test_cases", {}).get("generated")} test cases, seed {rep.get("seed")}')
                for f in rep.get('failures') or []:
                    for op in f.get('operations') or []:
                        out['failures'].append({
                            'operation': op, 'code': f.get('code'), 'title': f.get('title'),
                            'check': CODE_TO_CHECK.get(f.get('code'), f.get('code')),
                            'schemathesis_severity': f.get('severity'), 'message': msgs.get((op, f.get('code')), f.get('title')),
                            'evidence': Path(junits[-1]).relative_to(a.out.parent).as_posix() if junits else None})
                for e in rep.get('errors') or []:
                    out.setdefault('errors', []).append(e)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    print(f'schemathesis {out["tool"]["status"]}: {len(out["failures"])} failure(s) -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
