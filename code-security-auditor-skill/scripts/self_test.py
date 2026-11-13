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

def quill(root: Path) -> None:
    """The Node CLI, MCP server and GitHub Actions fixture (a pnpm workspace whose product package is named e2e)."""
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on quill-cli')
    got = {(c['file'], c['line'], c['pitfall_id']) for c in scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns in packages/e2e (a product package) and the composite action; nothing in '
                      'apps/e2e (a test workspace), ui.ts (safe forms), env: indirection or regex .exec()')
    print('recon on quill-cli')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + ''.join(f', {k} {e[k]}' for k in ('transport', 'bind', 'auth') if k in e)
                         + ('' if hit else f" (got {[(x['kind'], x['path'], x.get('bind')) for x in eps if x['handler'] == e['handler']]})"))
    check(len(eps) == want['entry_point_count'], f"{want['entry_point_count']} entry points ({len(eps)})")
    bad = [x['handler'] for x in eps if x['handler'].split(':')[0] in want['no_entry_points_in']]
    check(not bad, f'nothing from the test workspace or a WebSocket listener ({bad})')
    wf = {w['file']: w for w in d['ci_workflows']}
    for f, facts in want['workflows'].items():
        w = wf.get(f) or {}
        check(all(w.get(k) == v for k, v in facts.items()), f'{f}: {facts} (permissions keys and job names are not triggers)'
              + ('' if all(w.get(k) == v for k, v in facts.items()) else f' got {w}'))
    check(d['frameworks'] == want['frameworks'], f"frameworks labelled ({d['frameworks']})")
    leads = ' '.join(l['why'] for l in d['review_leads'])
    check(all(m in leads for m in want['lead_mentions']), 'agent permission handler and composite-action input are review leads')
    print('scan_secrets on quill-cli')
    found, creds = scan_secrets.scan_tree(root)
    check([(s['file'], s['line']) for s in found] == want['secrets'] and creds == want['credential_files'],
          f'no URL route as a credential file, no bare private-key header as a key ({[(s["file"], s["line"]) for s in found]}, {creds})')
    print('pnpm-lock.yaml facts')
    pk = merge_findings.pnpm_packages(root / 'pnpm-lock.yaml')
    dev = sorted(n for n, cs in pk.items() if all(c['dev'] for c in cs))
    check(dev == want['pnpm_dev_only'] and sorted(n for n in pk if n not in dev) == want['pnpm_prod'],
          f'dev-only packages from importers devDependencies ({dev})')
    check(pk['vitest'][0]['required_by'] == ['apps/e2e>vitest', 'packages/e2e>vitest'], 'via names importer>direct dependency')

def beacon(root: Path) -> None:
    """The Go command-line fixture (urfave/cli, cobra, net/http on a unix socket, vendored iOS headers)."""
    import seed_report  # noqa: E402
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on beacon-cli')
    res = scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())
    got = {(c['file'], c['line'], c['pitfall_id']) for c in res['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted Go patterns; nothing for exec.CommandContext(ctx, "xcrun"), crypto/rand, a loopback '
                      'client.Get, a constant path join, a guarded archive entry or a logged host')
    go_only = scan_pitfalls.scan(root, scan_pitfalls.load_catalogue(), {'go'})
    check(not go_only['warnings'], f'vendored iOS headers do not make C/C++ the main language ({go_only["warnings"]})')
    from collections import Counter
    w = scan_pitfalls.dominant_unscanned(Counter({'.rs': 40, '.go': 12}), {'.go'}, 12) or ''
    check('Rust' in w and 'incidental' not in w and '12 Go' in w,
          f'when another language was scanned, the warning names it instead of calling it incidental ({w[:160]})')
    check(not res['files_scanned'].get('c'), f"the c pack does not scan vendored headers ({res['files_scanned']})")
    print('recon on beacon-cli')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + ''.join(f', {k} {e[k]}' for k in ('bind', 'framework') if k in e)
                         + ('' if hit else f" (got {[(x['kind'], x['method'], x['path'], x.get('bind')) for x in eps if x['handler'] == e['handler']]})"))
    check(len(eps) == want['entry_point_count'], f"{want['entry_point_count']} entry points ({len(eps)}: "
                                                 f"{[(x['kind'], x['path']) for x in eps]})")
    bad = [x['handler'] for x in eps if x['handler'].split(':')[0] in want['no_entry_points_in']]
    check(not bad, f'no routes from an interpreter .Get("output"), an options .Get("user-data-dir") or an HTTP client call ({bad})')
    check(d['languages'] == want['languages'], f"languages count Go only, not vendored headers ({d['languages']})")
    check(sorted(v['dir'] for v in d.get('vendored') or []) == want['vendored'], f"vendored trees listed ({d.get('vendored')})")
    check(d['frameworks'] == want['frameworks'], f"frameworks ({d['frameworks']})")
    flags = [[f['file'], f['name'], f['env']] for f in d.get('cli_flags') or []]
    check(flags == want['cli_flags'], f'CLI flags with the environment variables bound to them ({flags})')
    leads = ' '.join(l['why'] for l in d['review_leads'])
    check(all(m in leads for m in want['lead_mentions']), f'env-bound flags and the unix socket are review leads ({leads[:300]})')
    by_handler = {x['handler']: x for x in eps}
    got_exp = {h: seed_report.exposure_of(by_handler[h]) for h in want['seeded_exposure'] if h in by_handler}
    check(got_exp == want['seeded_exposure'], f'seeded exposure: routes on a unix socket are local, :6060 is internet ({got_exp})')
    print('scan_secrets on beacon-cli')
    found, _ = scan_secrets.scan_tree(root)
    check([(s['file'], s['line']) for s in found] == want['secrets'], f'a header-name constant is not a secret ({[(s["file"], s["line"], s["key"]) for s in found]})')
    check(scan_secrets.find_in_line('const TokenHeader = "X-Beacon-Token"', 'auth.go') == []
          and scan_secrets.find_in_line('headerName := "X-Api-Key"', 'client.go') == [], 'header names are not secrets')
    check(len(scan_secrets.find_in_line('const deviceToken = "q7Lm2Vx9Rt4Kp8Zw"', 'auth.go')) == 1, 'a token literal still is')

GOVULNCHECK_STREAM = '''{"config": {"protocol_version": "v1.0.0", "scanner_name": "govulncheck", "scanner_version": "v1.1.4", "scan_level": "symbol"}}
{"progress": {"message": "Scanning your code and 40 packages across 4 dependent modules for known vulnerabilities..."}}
{"osv": {"id": "GO-2099-0001", "aliases": ["CVE-2099-0001"], "summary": "Example tokenizer issue in x/net/html", "affected": [
  {"package": {"name": "golang.org/x/net", "ecosystem": "Go"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "0.23.0"}]}]},
  {"package": {"name": "stdlib", "ecosystem": "Go"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.22.9"}]}]}]}}
{"osv": {"id": "GO-2099-0002", "aliases": [], "summary": "Example http2 issue", "affected": [
  {"package": {"name": "golang.org/x/net", "ecosystem": "Go"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "0.25.0"}]}]}]}}
{"osv": {"id": "GO-2099-0003", "aliases": ["GHSA-aaaa-bbbb-cccc"], "summary": "Example YAML issue", "affected": [
  {"package": {"name": "gopkg.in/yaml.v3", "ecosystem": "Go"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "3.0.2"}]}]}]}}
{"finding": {"osv": "GO-2099-0001", "fixed_version": "v0.23.0", "trace": [{"module": "golang.org/x/net", "version": "v0.20.0", "package": "golang.org/x/net/html"}]}}
{"finding": {"osv": "GO-2099-0001", "fixed_version": "v0.23.0", "trace": [
  {"module": "golang.org/x/net", "version": "v0.20.0", "package": "golang.org/x/net/html", "function": "Parse"},
  {"module": "example.test/beacon", "package": "example.test/beacon/internal/fetch", "function": "Agent", "position": {"filename": "internal/fetch/fetch.go", "line": 20}}]}}
{"finding": {"osv": "GO-2099-0002", "fixed_version": "v0.25.0", "trace": [{"module": "golang.org/x/net", "version": "v0.20.0", "package": "golang.org/x/net/http2"}]}}
{"finding": {"osv": "GO-2099-0003", "fixed_version": "v3.0.2", "trace": [{"module": "gopkg.in/yaml.v3", "version": "v3.0.1"}]}}
'''

def go_rules(beacon_root: Path) -> None:
    print('Go helpers (govulncheck, OSV fixed versions, run_scanners, validator, run_poc)')
    import run_poc  # noqa: E402
    import run_scanners  # noqa: E402
    import seed_report  # noqa: E402
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'govulncheck.json'
        p.write_text(GOVULNCHECK_STREAM, encoding='utf-8')
        try:
            items, info = merge_findings.from_deps(p)
        except ValueError as exc:
            items, info = [], {'tool': f'error: {exc}'}
        got = sorted((x['advisory'], x['package'], x['version'], tuple(x['fixed_in']), x.get('reachability')) for x in items)
        check(info['tool'] == 'govulncheck' and got == [
            ('GO-2099-0001', 'golang.org/x/net', '0.20.0', ('0.23.0',), 'symbol'),
            ('GO-2099-0002', 'golang.org/x/net', '0.20.0', ('0.25.0',), 'package'),
            ('GO-2099-0003', 'gopkg.in/yaml.v3', '3.0.1', ('3.0.2',), 'module')],
            f'govulncheck JSON stream read with its reachability level, the strongest per advisory ({info["tool"]}: {got})')
        sym = next((x for x in items if x['advisory'] == 'GO-2099-0001'), {})
        check('internal/fetch/fetch.go:20' in (sym.get('call_path') or ''), f"the call path names the caller ({sym.get('call_path')})")
        seeded = {x['advisory']: seed_report.dependency({**x, 'id': 'DEP-1'})['reachable'] for x in items}
        check(seeded == {'GO-2099-0001': 'yes', 'GO-2099-0002': 'no', 'GO-2099-0003': 'no'},
              f'seed_report: symbol-level is reachable (a finding to write), package and module level are not ({seeded})')
        x_net = {'id': 'GO-2099-0001', 'affected': [
                {'package': {'name': 'golang.org/x/net'}, 'ranges': [{'events': [{'fixed': '0.23.0'}]}]},
                {'package': {'name': 'stdlib'}, 'ranges': [{'events': [{'fixed': '1.22.9'}]}]}]}
        osv = {'results': [{'source': {'path': 'go.mod'}, 'packages': [{'package': {'name': 'golang.org/x/net', 'version': '0.20.0', 'ecosystem': 'Go'},
               'vulnerabilities': [x_net]}]}]}
        o = Path(tmp) / 'osv.json'
        o.write_text(json.dumps(osv), encoding='utf-8')
        items, _ = merge_findings.from_deps(o)
        check(items and items[0]['fixed_in'] == ['0.23.0'], f"osv-scanner: fixed versions only from the package's own affected entry ({items and items[0]['fixed_in']})")
    inv = run_scanners.inventory(beacon_root)
    plan = {t['name']: t for t in run_scanners.plan(beacon_root, Path('out'), inv, [])}
    gv = plan.get('govulncheck') or {}
    check(gv.get('applies') is True and gv.get('covers') == ['go.mod'] and gv.get('area') == 'dependencies'
          and any('-format' in r['cmd'] and 'json' in r['cmd'] for r in gv.get('runs') or []),
          f'run_scanners plans govulncheck -format json for each go.mod ({gv.get("covers")})')
    check('c' not in inv['langs'], f"run_scanners: vendored headers are not C source ({inv['langs']})")
    deps = run_scanners.dependency_inventory(beacon_root, inv)
    adv = run_scanners.advisory_status([{'name': 'govulncheck', 'area': 'dependencies', 'status': 'ran', 'covers': ['go.mod']}], deps)
    check(adv['status'] == 'assessed', f'govulncheck covers go.mod ({adv})')
    # validator: Go and Rust regression tests, command-only verification, reachability notes
    base = HERE.parent / 'examples'
    rep = json.loads((base / 'security_audit.example.json').read_text(encoding='utf-8'))
    f1 = next(f for f in rep['findings'] if f['regression_test']['result'] == 'fails_on_vulnerable_code')
    name_errs = []
    for name in ('TestRunKeepsSavedOutputInsideOutDir', 'TestRunKeepsSavedOutputInsideOutDir/save_path_with_parent_segments'):
        f1['regression_test'].update(framework='go test', file='beacon-cli/internal/flows/flows_test.go', name=name)
        errs, _ = validator.check(rep, base, None, None)
        name_errs += [e for e in errs if 'is not in' in e]
    check(not name_errs, f'a Go test function and a t.Run subtest are found in the test file ({name_errs})')
    f1['regression_test'].update(name='TestNotThere')
    errs, _ = validator.check(rep, base, None, None)
    check(any('TestNotThere is not in' in e for e in errs), 'a missing Go test is still reported')
    rep = json.loads((base / 'security_audit.example.json').read_text(encoding='utf-8'))
    rep['verification'].update(target=None, target_kind='local_copy', local_copy=tmp)
    errs, _ = validator.check(rep, base, None, None)
    ver_errs = [e for e in errs if e.startswith(('verification', '$.verification'))]
    check(not ver_errs, f'verification with only command PoCs: target_kind local_copy and no URL ({ver_errs})')
    check(any("confirmed against target_kind 'local_copy'" in e for e in errs),
          'a finding confirmed by an HTTP PoC is still rejected when the only target is a local copy')
    rep['verification'].update(local_copy=None)
    errs, _ = validator.check(rep, base, None, None)
    check(any('local_copy' in e for e in errs), 'target_kind local_copy needs verification.local_copy')
    rep = json.loads((base / 'security_audit.example.json').read_text(encoding='utf-8'))
    rep['dependencies'][0].update(reachable='unknown', basis='govulncheck at symbol level would settle it; it could not run offline.')
    _, warns = validator.check(rep, base, None, None)
    check(not any('reachability unknown' in w for w in warns), 'no "say what would settle it" warning when the basis says it')
    rep['dependencies'][0].update(basis='Not checked.')
    _, warns = validator.check(rep, base, None, None)
    check(any('reachability unknown' in w for w in warns), 'the warning stays when it does not')
    # run_poc: Go caches pass through the isolated environment, and a URL built by concatenation is read by its host
    env_of = getattr(run_poc, 'command_env', None)
    env = env_of(Path(tmp) / 'home') if env_of else {}
    check(all(env.get(k) for k in ('GOCACHE', 'GOMODCACHE', 'GOPATH')) and env.get('GOPROXY') == 'off'
          and not str(env.get('GOCACHE', '')).startswith(str(Path(tmp))),
          f"command PoCs keep the real Go build and module caches, with GOPROXY off ({ {k: env.get(k) for k in ('GOCACHE', 'GOPROXY')} })")
    go_src = 'u := "http://canary-user:" + secret + "@127.0.0.1:8080/upload"'
    poc = {'finding': 'F-1', 'kind': 'command', 'files': {'poc_test.go': go_src},
           'steps': [{'name': 'run', 'argv': ['go', 'test', './...']}], 'proof': {'type': 'exit_code', 'step': 'run', 'code': 1}}
    issues = [i for i in run_poc.lint(poc, None, []) if 'URL' in i]
    check(not issues, f'a URL with concatenated user info is checked by its host, not the user name ({issues})')
    bad = {**poc, 'files': {'poc_test.go': 'u := "http://canary-user:" + secret + "@registry.vendor.io/upload"'}}
    check(any('registry.vendor.io' in i for i in run_poc.lint(bad, None, [])), 'its real host is still checked')
    import render_report  # noqa: E402
    g1 = render_report.reason_group('internal/device/auth.go:15 here is crypto/rand, not math/rand; the safe API')
    g2 = render_report.reason_group('`rt.Get("output")` reads an interpreter value, not an HTTP route')
    check(not g1.lower().startswith('here') and 'crypto/rand' in g1 and 'interpreter value' in g2,
          f'dismissal groups read as reasons, not "here is ..." or a code fragment ({g1!r}, {g2!r})')

def node_and_ci_rules() -> None:
    print('recon, run_scanners and merge helpers (CI, lockfiles, gitleaks)')
    import recon  # noqa: E402
    import run_scanners  # noqa: E402
    import test_paths  # noqa: E402
    wf = 'on:\n  push:\n    branches: [main]\n  issues:\n    types: [opened]\npermissions:\n  issues: write\njobs:\n  release:\n    runs-on: x\n'
    check([t for t, _ in recon.gha_triggers(wf)] == ['push', 'issues'], 'triggers only from the on: block')
    check([t for t, _ in recon.gha_triggers('on: [push, pull_request]\njobs:\n  release: {}\n')] == ['push', 'pull_request'],
          'inline on: list')
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / 'packages' / 'e2e').mkdir(parents=True)
        (t / 'packages' / 'e2e' / 'package.json').write_text('{"name": "x", "bin": {"x": "cli.js"}}', encoding='utf-8')
        (t / 'apps' / 'e2e').mkdir(parents=True)
        (t / 'apps' / 'e2e' / 'package.json').write_text('{"name": "x-e2e", "private": true}', encoding='utf-8')
        c = test_paths.Classifier(t)
        check(not c.is_test('packages/e2e/src/cli.ts') and c.is_test('apps/e2e/login.ts') and c.is_test('tests/a.py')
              and c.is_test('packages/e2e/src/cli.test.ts'), 'a package named e2e is product code; a private e2e workspace is tests')
    deps = {'lockfiles': [{'file': 'pnpm-lock.yaml', 'ecosystem': 'npm', 'packages': 900},
                          {'file': '.github/actions/x/package-lock.json', 'ecosystem': 'npm', 'packages': 55}], 'packages': []}
    adv = run_scanners.advisory_status([{'name': 'npm-audit', 'area': 'dependencies', 'status': 'ran',
                                         'covers': ['.github/actions/x/package-lock.json']}], deps)
    check(adv['status'] == 'partial' and 'pnpm-lock.yaml' in adv['reason'],
          'npm audit on a nested package-lock.json leaves the pnpm workspace lockfile unassessed')
    adv = run_scanners.advisory_status([{'name': 'osv-scanner', 'area': 'dependencies', 'status': 'ran'}], deps)
    check(adv['status'] == 'assessed', 'osv-scanner covers every lockfile')
    audit = {'advisories': {'1': {'module_name': 'hono', 'vulnerable_versions': '<4.9.7', 'severity': 'moderate',
                                  'title': 'Example advisory', 'url': 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc',
                                  'github_advisory_id': 'GHSA-aaaa-bbbb-cccc', 'cves': [],
                                  'findings': [{'version': '4.9.1', 'paths': ['apps__lab>@vendor/devtools>hono']}]}}}
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / 'pnpm-audit.json'
        f.write_text(json.dumps(audit), encoding='utf-8')
        items, info = merge_findings.from_deps(f)
        check(info['tool'] == 'pnpm audit' and items[0]['version'] == '4.9.1' and items[0]['fixed_in'] == ['4.9.7'],
              'pnpm audit --json (npm audit v6 shape) is read')
        root = Path(tmp) / 'repo'
        (root / 'src').mkdir(parents=True)
        (root / 'src' / 'cache.json').write_text('{"key": "4f2a9c0d1e7b3a5f8c6d2e0b9a7f1c3e"}\n', encoding='utf-8')
        sarif = {'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'gitleaks', 'version': 'v8.0.0', 'rules': []}}, 'results': [
            {'ruleId': 'generic-api-key', 'message': {'text': 'generic-api-key has detected secret for file src/cache.json.'},
             'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'src/cache.json'}, 'region': {'startLine': 1}}}]},
            {'ruleId': 'github-pat', 'message': {'text': 'github-pat has detected secret for file src/old.ts.'},
             'partialFingerprints': {'commitSha': 'abc123def4567890'},
             'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'src/old.ts'}, 'region': {'startLine': 7}}}]}]}]}
        sp = Path(tmp) / 'gitleaks.sarif'
        sp.write_text(json.dumps(sarif), encoding='utf-8')
        code, secrets = merge_findings.from_sarif(sp, root, merge_findings.tool_index())
        old = next((s for s in secrets if s['file'] == 'src/old.ts'), {})
        check(old.get('history_only') and old.get('git_status') == 'removed' and old.get('history_commits') == ['abc123def456']
              and old.get('line') is None, 'a gitleaks hit in a file gone from HEAD is history-only with its commit')
        cache = next((s for s in secrets if s['file'] == 'src/cache.json'), None)
        check(bool(cache) and merge_findings.secret_triage_hint(cache, root, test_paths.Classifier(root)) is not None,
              'a generic gitleaks hit that scan_secrets sees no secret in (a cache key hash) is set aside')

def poc_and_report_rules() -> None:
    print('run_poc (command PoCs) and validator (recon removals, history-only secrets)')
    import run_poc  # noqa: E402
    ok = {'finding': 'F-1', 'kind': 'command', 'files': {'quill.config.json': '{"packageManager": "./hook"}'},
          'steps': [{'name': 'run', 'argv': ['node', '{workdir}/cli.js', 'init']}],
          'proof': {'type': 'marker_in_output', 'step': 'run', 'marker': 'CANARY-1337'}}
    check(run_poc.lint(ok, None, []) == [], 'a command PoC with an argument list and a computed marker passes the lint')
    bad = {**ok, 'steps': [{'name': 'run', 'argv': ['bash', '-c', 'rm -rf /tmp/x']}]}
    issues = run_poc.lint(bad, None, [])
    check(any('shell' in i for i in issues) and any('destructive' in i for i in issues), 'shells and destructive commands are refused')
    check(any('marker' in i for i in run_poc.lint({**ok, 'files': {'a.txt': 'CANARY-1337'}}, None, [])),
          'a marker planted in the input proves nothing')
    report = {'recon': {'entry_points': [{'id': 'EP-1', 'handler': 'a.ts:1', 'kind': 'cli'}],
                        'removed_entry_points': [{'id': 'EP-3', 'handler': '.github/workflows/x.yml:9', 'reason': 'a permissions key, not a trigger'}]}}
    recon = {'entry_points': [{'id': 'EP-1', 'handler': 'a.ts:1', 'kind': 'cli', 'method': 'BIN'},
                              {'id': 'EP-2', 'handler': 'b.ts:4', 'kind': 'listener', 'method': 'LISTEN'},
                              {'id': 'EP-3', 'handler': '.github/workflows/x.yml:9', 'kind': 'ci_workflow', 'method': 'issues'}]}
    errs = validator.recon_errors(report, recon)
    check(len(errs) == 1 and 'EP-2' in errs[0], 'a recon entry point dropped without a reason is an error; a removal with one is not')
    check(bool(validator.ROTATION_REASON.search('public analytics key by design, never a credential'))
          and not validator.ROTATION_REASON.search('gone from HEAD, so nothing to do'),
          'a dismissed history-only secret must say it was rotated or why it needs no rotation')

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
    ap.add_argument('--node-fixture', type=Path, default=HERE.parent / 'examples' / 'quill-cli')
    ap.add_argument('--go-fixture', type=Path, default=HERE.parent / 'examples' / 'beacon-cli')
    args = ap.parse_args()
    root = args.fixture.resolve()
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    patterns(root, want)
    recon(root, want)
    secrets(root, want)
    ferry(args.azure_fixture.resolve())
    quill(args.node_fixture.resolve())
    beacon(args.go_fixture.resolve())
    go_rules(args.go_fixture.resolve())
    node_and_ci_rules()
    poc_and_report_rules()
    python_rules()
    advisory_rules()
    merge()
    validator_rules()
    print(f"self-test {'passed' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0

if __name__ == '__main__':
    raise SystemExit(main())
