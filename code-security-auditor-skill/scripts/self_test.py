#!/usr/bin/env python3
"""Check the built-in scanners against the Next.js fixture, and the merge and validator helpers.

    python self_test.py [--fixture ../examples/kite-notes]

Runs scan_pitfalls.py (the javascript pack), recon.py and scan_secrets.py on examples/kite-notes
and compares what they report with examples/kite-notes/expected.json: the pattern hits must match
exactly, the listed entry points must be present with the expected auth, client-side sources and
data stores must not include server or config files, and test-file literals must be set aside.
Then checks the npm range matching and lockfile facts in merge_findings.py, and the validator's
headline rule and secret-value matching. Writes nothing; exit code 0 when every check passes.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import merge_findings  # noqa: E402
import scan_pitfalls  # noqa: E402
import scan_secrets  # noqa: E402
import validate_security_audit as validator  # noqa: E402

FAILS: list[str] = []

def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)

def patterns(root: Path, want: dict) -> None:
    print('scan_pitfalls (javascript pack)')
    got = {(c['file'], c['line'], c['pitfall_id']) for c in scan_pitfalls.scan(root, scan_pitfalls.load_catalogue(), {'javascript'})['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns found, nothing in the safe counterparts (src/lib/safe.ts)')

def recon(root: Path, want: dict) -> None:
    print('recon')
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'recon.json'
        subprocess.run([sys.executable, str(HERE / 'recon.py'), str(root), '--out', str(out)], check=True,
                       capture_output=True, text=True)
        d = json.loads(out.read_text(encoding='utf-8'))
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}: auth {e['auth']}")
    got_cs = sorted(x['handler'] for x in eps if x['kind'] == 'client_source')
    check(got_cs == want['client_sources'], f'client sources only in browser code ({got_cs})')
    check(all(m['covers_api'] == want['middleware_covers_api'] for m in d['middleware']) and bool(d['middleware']),
          'middleware matcher and protected prefixes parsed; /api/* not covered')
    check(sorted({s['file'] for s in d['data_stores']}) == want['data_store_files'], 'no data store from next.config.ts')

def secrets(root: Path, want: dict) -> None:
    print('scan_secrets')
    found, _ = scan_secrets.scan_tree(root)
    head = [s for s in found if not s['in_test_path'] or scan_secrets.real_looking(s)]
    fixtures = [s for s in found if s['in_test_path'] and not scan_secrets.real_looking(s)]
    got = sorted((s['file'], s['line'], s['key']) for s in head)
    check(got == sorted(tuple(x) for x in want['secrets']),
          f'only the hard-coded fallback is a secret: no .env.example placeholders, no field-name lists ({got})')
    check(len(fixtures) == want['test_fixtures'], f'{len(fixtures)} test-file literal(s) set aside as fixtures')
    check(scan_secrets.is_placeholder('production-session-secret', 'DEPLOYMENT.md'), 'hyphenated words in docs are placeholders')
    check(not scan_secrets.is_placeholder('production-session-secret', 'config.ts', fallback=True),
          'a hard-coded fallback is never waved through as a placeholder')
    check(scan_secrets.find_in_line("DATABASE_URL=postgresql://user:password@localhost/db", 'README.md') == [],
          'postgresql://user:password@ in documentation is not a secret')

def merge() -> None:
    print('merge_findings (npm facts)')
    cases = [('16.3.3', '>=16.0.0 <16.4.1', True), ('16.3.3', '16.0.0 - 16.3.2', False), ('8.10.1', '<8.10.2', True),
             ('6.28.0', '>=7.0.0 <8.10.2', False), ('2.0.0', '<1.0.0 || >=2.0.0 <2.0.1', True), ('1.2.3-beta.1', '<1.2.3', True)]
    for v, r, exp in cases:
        check(merge_findings.satisfies(v, r) is exp, f'{v} in {r!r}: {exp}')
    lock = {'undici': [{'version': '6.28.0', 'path': 'node_modules/@vendor/blob/node_modules/undici', 'dev': False},
                       {'version': '8.10.1', 'path': 'node_modules/undici', 'dev': True}]}
    audit = {'vulnerabilities': {'undici': {'name': 'undici', 'range': '<6.29.0 || >=8.0.0 <8.10.2', 'effects': ['@vendor/blob'],
                                            'fixAvailable': True, 'via': [
        {'source': 1, 'title': 'Example advisory A', 'url': 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc', 'severity': 'moderate', 'range': '<6.29.0'},
        {'source': 2, 'title': 'Example advisory B', 'url': 'https://github.com/advisories/GHSA-dddd-eeee-ffff', 'severity': 'high', 'range': '>=8.0.0 <8.10.2'}]}}}
    out = merge_findings.from_npm_audit(audit, lock)
    got = sorted((x['advisory'], x['version'], x['dev'], tuple(x['fixed_in'])) for x in out)
    check(got == [('GHSA-aaaa-bbbb-cccc', '6.28.0', False, ('6.29.0',)), ('GHSA-dddd-eeee-ffff', '8.10.1', True, ('8.10.2',))],
          'one entry per advisory per installed copy, with the installed version, the dev flag and the fix from the range')
    check(all(x['via'] == ['@vendor/blob'] for x in out), 'the via chain is kept')

def validator_rules() -> None:
    print('validator')
    banned = ['The code is secure', 'The app looks secure overall', 'No vulnerabilities found', 'Safe to deploy']
    allowed = ['Session cookies are set Secure; 3 P1 findings', 'No secure-coding guidance was reviewed in billing',
               '2 P1 findings; the Secure flag is missing on the session cookie']
    check(all(validator.BANNED_HEADLINE.search(h) for h in banned), 'verdict headlines rejected')
    check(not any(validator.BANNED_HEADLINE.search(h) for h in allowed), '"secure" in technical phrases allowed')
    strings = dict(validator.json_strings({'regression_test': {'passes_after_fix': True}, 'a': ['reset-password route']}))
    check(list(strings) == ['$.a[0]'], 'only string values are scanned (keys such as passes_after_fix are not), with their JSON path')
    check(validator.priority(12, 'traced', {'kind': 'direct_financial_loss', 'basis': 'x' * 20}) == 'P1'
          and validator.priority(12, 'traced', None) == 'P2' and validator.priority(24, 'traced', {'kind': 'fraud', 'basis': 'x' * 20}) == 'P1',
          'business impact raises P2/P3 one level, never to P0')

def run_recon(root: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'recon.json'
        subprocess.run([sys.executable, str(HERE / 'recon.py'), str(root), '--out', str(out)], check=True,
                       capture_output=True, text=True)
        return json.loads(out.read_text(encoding='utf-8'))

def ferry(root: Path) -> None:
    """The FastAPI, Azure Functions, Bicep/ARM and Azure Pipelines fixture."""
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on ferry-ops')
    got = {(c['file'], c['line'], c['pitfall_id']) for c in scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns found; nothing in safe.bicep, release.yml, local_only.py, reports.py '
                      '(non-SQL and constant f-strings), the fares upload or the vite dev-server proxy')
    print('recon on ferry-ops')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e.get('kind', 'http_route')} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + (f": auth {e['auth']}" if 'auth' in e else '')
                         + ('' if hit else f" (got {[(x['path'], x['auth']) for x in eps if x['handler'] == e['handler']]})"))
    check(len(eps) == want['entry_point_count'], f"{want['entry_point_count']} entry points ({len(eps)})")
    bad = [x['handler'] for x in eps if x['handler'].split(':')[0] in want['no_entry_points_in']]
    check(not bad, f'no routes from decorators quoted in docstrings ({bad})')
    wf = {w['file']: w for w in d['ci_workflows']}
    for f, facts in want['pipelines'].items():
        w = wf.get(f) or {}
        check(all(w.get(k) == v for k, v in facts.items()), f'Azure DevOps pipeline {f}: {facts}')
    check(sorted(d['iac']) == want['iac'], f"Bicep, ARM and compose.yml are IaC ({d['iac']})")
    print('scan_secrets on ferry-ops')
    found, _ = scan_secrets.scan_tree(root)
    check(sorted((s['file'], s['line'], s['key']) for s in found) == [tuple(x) for x in want['secrets']],
          f"no secrets: \"$VAR\" in a script, ARM expressions and 'Disabled' are not values ({[(s['file'], s['line']) for s in found]})")
    for v in ('$DB_ADMIN_PASS', '$(dbAdminPassword)', '$env:DB_PASS', '%DB_PASS%'):
        check(scan_secrets.is_placeholder(v, 'reset-db.yml') and scan_secrets.is_placeholder(v, 'x.py', fallback=True),
              f'{v} is a variable reference, not a value')
    print('dependency inventory (offline)')
    import run_scanners  # noqa: E402
    inv = run_scanners.inventory(root)
    deps = run_scanners.dependency_inventory(root, inv)
    check([[l['file'], l['ecosystem'], l['packages']] for l in deps['lockfiles']] == want['dependency_lockfiles'],
          'lockfiles listed without the network')
    adv = run_scanners.advisory_status([{'name': 'pip-audit', 'area': 'dependencies', 'status': 'ran'}], deps)
    check(adv['status'] == 'partial' and 'npm' in adv['reason'], 'pip-audit alone leaves the npm lockfile unassessed: partial')
    check(run_scanners.advisory_status([], deps)['status'] == 'not_assessed', 'no advisory tool: not_assessed')
    check(any(n.endswith('.bicep') for n in inv['iac']) and any('pipelines/' in n for n in inv['iac']),
          'Checkov applies to Bicep and Azure Pipelines files')

def python_rules() -> None:
    print('Python helpers')
    text = 'COLUMNS = "a, b"\nTABLES = ("t1", "t2")\nfor table in TABLES:\n    pass\n'
    check(scan_pitfalls.constant_fstring('q = f"SELECT {COLUMNS} FROM {table}"', text), 'constants and loops over them are not SQL leads')
    check(not scan_pitfalls.constant_fstring('q = f"SELECT {COLUMNS} FROM t ORDER BY {sort}"', text), 'a request value is')
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'deps.py'
        p.write_text('"""Usage:\n\n    @router.get("/x")\n"""\n\n@router.get("/y")\ndef y():\n    pass\n', encoding='utf-8')
        lines = validator.python_string_lines(p)
        check(3 in lines and 6 not in lines, 'validator: a decorator in a docstring is not a handler; a real decorator is')

def advisory_rules() -> None:
    print('validator (dependency advisories)')
    base = {'schema_version': '1.2', 'tools': [{'name': 'pip-audit', 'area': 'dependencies', 'status': 'not_run', 'reason': 'offline'}],
            'dependencies': [], 'limitations': ['x'], 'summary': {'headline': 'No vulnerable dependencies; 2 P1 findings'}}
    errs = validator.advisory_errors({**base, 'dependency_advisories': {'status': 'assessed', 'tools': [], 'reason': None}})
    check(any('no dependency tool ran' in e for e in errs), 'assessed with no dependency tool is rejected')
    errs = validator.advisory_errors({**base, 'dependency_advisories': {'status': 'not_assessed', 'tools': [], 'reason': 'offline run, no local database'}})
    check(any('limitations' in e for e in errs) and any('headline' in e for e in errs),
          'not_assessed must be in limitations and the headline must not claim clean dependencies')
    check(any("required" in e for e in validator.advisory_errors(base)), 'schema 1.2 requires dependency_advisories')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fixture', type=Path, default=HERE.parent / 'examples' / 'kite-notes')
    ap.add_argument('--azure-fixture', type=Path, default=HERE.parent / 'examples' / 'ferry-ops')
    args = ap.parse_args()
    root = args.fixture.resolve()
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    patterns(root, want)
    recon(root, want)
    secrets(root, want)
    ferry(args.azure_fixture.resolve())
    python_rules()
    advisory_rules()
    merge()
    validator_rules()
    print(f"self-test {'passed' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0

if __name__ == '__main__':
    raise SystemExit(main())
