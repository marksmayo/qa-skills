#!/usr/bin/env python3
"""Check the built-in scanners against the Next.js fixture, and the merge and validator helpers.

    python self_test.py [--fixture ../examples/kite-notes]

Runs scan_pitfalls.py (the javascript pack), recon.py and scan_secrets.py on examples/kite-notes
and compares what they report with examples/kite-notes/expected.json: the pattern hits must match
exactly, the listed entry points must be present with the expected auth, client-side sources and
data stores must not include server or config files, and test-file literals must be set aside.
Then checks the npm range matching and lockfile facts in merge_findings.py, and the validator's
headline rule and secret-value matching. The other fixtures (ferry-ops, quill-cli, beacon-cli, tide-tables,
wren-field-guide, a static Next.js content site, and tidemark-cli, a Rust clap CLI with FFI and a Cargo.lock) have
their own expected.json; run_poc guard checks, observation
evidence, the seeded run_poc row and the empty-findings rendering are checked too. Writes nothing; exit code 0 when every check passes.
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

def _sequential_run(value: str, n: int = 4) -> bool:
    """True when the value holds n ascending characters in a row (abcd, 1234, EfGh): plainly typed by hand."""
    s = value.lower()
    return any(all(ord(s[i + k + 1]) - ord(s[i + k]) == 1 for k in range(n - 1)) for i in range(len(s) - n + 1))

def own_literals() -> None:
    """The secrets this file plants must look fake to a secret scanner watching the repository: a mixed letter-and-
    digit literal of 16 or more characters with high entropy needs a hand-typed ascending run (abcd, 1234) in it.
    Literals that a check needs to be high-entropy (a token scan_secrets must still report) get it that way."""
    print('planted literals in self_test.py')
    bad = []
    for n, line in enumerate(Path(__file__).read_text(encoding='utf-8').splitlines(), 1):
        for m in re.finditer(r"""['"]([A-Za-z0-9]{16,})['"]""", line):
            v = m.group(1)
            if re.search(r'\d', v) and re.search(r'[A-Za-z]', v) and scan_secrets.entropy(v) >= 3.5 \
                    and not _sequential_run(v):
                bad.append(n)
    check(not bad, f'no random-looking secret literal (lines {bad})')

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

def run_recon(root: Path, *extra: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'recon.json'
        subprocess.run([sys.executable, str(HERE / 'recon.py'), str(root), '--out', str(out), *extra], check=True,
                       capture_output=True, text=True)
        return json.loads(out.read_text(encoding='utf-8'))

def lathe(root: Path) -> None:
    """A tool repository: pattern tables, docstrings and fixture strings that look like the code they describe, a
    script kept identical in two packages (declared in tools/check_shared.py), sample projects under examples/ with
    planted defects, a rule catalogue of secret regexes and a requirements file with version ranges."""
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    cat = scan_pitfalls.load_catalogue()
    print('scan_pitfalls (all packs) on lathe-tools')
    out = scan_pitfalls.scan(root, cat)
    got = {(c['file'], c['line'], c['pitfall_id']) for c in out['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, 'no hit in pattern tables, docstrings, banned-phrase regexes, a method named eval, stdlib ElementTree '
                      'or a pinned download in a CLI; the copied script reported once; the sample projects left out')
    copies = {f"{c['file']}:{c['line']}": c.get('copies') for c in out['candidates'] if c.get('copies')}
    check(copies == want['pattern_copies'], f'the hit on the canonical copy lists the copies ({copies})')
    fx = sorted(f['dir'] for f in out.get('fixtures_excluded') or [])
    check(fx == want['fixtures'], f'the sample projects are listed as left out ({fx})')
    got = {(c['file'], c['line'], c['pitfall_id']) for c in scan_pitfalls.scan(root, cat, include_fixtures=True)['candidates']}
    exp = {tuple(x) for x in want['patterns_with_fixtures']}
    check(got == exp, f'--include-fixtures scans the samples: send_file, SSRF in a Flask view, lxml XXE, |safe in a '
                      f'template string (missing {sorted(exp - got)}, unexpected {sorted(got - exp)})')

def lathe_recon(root: Path) -> None:
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('recon on lathe-tools')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} at {e['handler']}" + (f", copies {e['copies']}" if e.get('copies') else '')
              + ('' if hit else f" (got {[(x['kind'], x['handler'], x.get('copies')) for x in eps]})"))
    check(len(eps) == want['entry_point_count'], f"{want['entry_point_count']} entry points, none from strings, docstrings "
          f"or the samples ({[(x['kind'], x['handler']) for x in eps]})")
    check(sorted(f['dir'] for f in d.get('fixtures_excluded') or []) == want['fixtures'], 'recon lists the sample projects it left out')
    check(d['frameworks'] == want['frameworks'], f"no framework from a fixture string or a sample ({d['frameworks']})")
    leads = ' | '.join(l['why'] for l in d['review_leads'])
    for m in want['no_lead_mentions']:
        check(m not in leads, f'no review lead says {m!r}')
    d2 = run_recon(root, '--include-fixtures')
    web = [l['why'] for l in d2['review_leads'] if 'frame-ancestors' in l['why']]
    check(bool(web) and all('examples/site' in w for w in web),
          f'--include-fixtures: a header lead from the sample site says where it comes from ({web})')
    for e in want['entry_points_with_fixtures']:
        check(any(all(x.get(k) == v for k, v in e.items()) and x.get('example') for x in d2['entry_points']),
              f"--include-fixtures: {e['kind']} {e['path']} at {e['handler']}, marked example")

def lathe_secrets(root: Path) -> None:
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_secrets on lathe-tools')
    found, _ = scan_secrets.scan_tree(root)
    head, set_aside = scan_secrets.split_found(found)
    check([(s['file'], s['line']) for s in head] == want['secrets'],
          f'no secret from a rule catalogue of regexes or a fake:fake connection string ({[(s["file"], s["line"], s["key"]) for s in head]})')
    check(sorted([s['file'], s['line']] for s in set_aside) == want['secret_fixture_literals'],
          f'a low-entropy literal in a sample project is set aside ({[(s["file"], s["line"]) for s in set_aside]})')

def lathe_deps(root: Path) -> None:
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('run_scanners dependency inventory on lathe-tools')
    import run_scanners  # noqa: E402
    inv = run_scanners.inventory(root)
    deps = run_scanners.dependency_inventory(root, inv)
    req = next((l for l in deps['lockfiles'] if l['file'] == want['requirements']['file']), {})
    check(all(req.get(k) == v for k, v in want['requirements'].items()),
          f'a requirements file with version ranges lists its packages ({req})')
    check(sorted(l['file'] for l in inv.get('fixture_lockfiles') or []) == want['fixture_lockfiles']
          and not any(l['file'] in want['fixture_lockfiles'] for l in deps['lockfiles']),
          f"the samples' lockfiles are listed apart, not counted as the product's ({inv.get('fixture_lockfiles')})")

def report_scope_rules() -> None:
    """The headline against the computed counts, home-directory paths, fixture projects named in the report, and the
    seed's redaction and scope."""
    print('validator (headline counts, home paths, fixture projects) and seed_report scope')
    import seed_report  # noqa: E402
    ex = json.loads((HERE.parent / 'examples' / 'security_audit.example.json').read_text(encoding='utf-8'))
    def errs(mutate, recon: dict | None = None) -> list[str]:
        d = json.loads(json.dumps(ex))
        mutate(d)
        return validator.check(d, HERE.parent / 'examples', None, None, recon)[0]
    def headline(text: str):
        return lambda d: d['summary'].__setitem__('headline', text)
    check(any('headline' in e and 'P2' in e for e in errs(headline('18 findings, all P3; dependencies not covered'))),
          'a headline that says "all P3" while the counts hold P0 to P2 findings is rejected')
    check(any('headline' in e for e in errs(headline('18 findings: 3 P0, 2 P1; dependencies not covered'))),
          'a headline count that differs from summary.counts (2 P1, counted 9) is rejected')
    check(any('headline' in e for e in errs(headline('18 findings, no P0; dependencies not covered'))),
          'a headline that says "no P0" while there are P0 findings is rejected')
    check(not [e for e in errs(lambda d: None) if 'headline' in e], 'the example headline (3 P0, 9 P1) matches its counts')
    home = 'C:' + '\\Users\\someone\\scans\\pip-audit.json'
    check(any('home' in e for e in errs(lambda d: d['limitations'].append(f'pip-audit wrote {home}.'))),
          'a home-directory path (the account name) in the report is rejected')
    check(any('home' in e for e in errs(lambda d: d['limitations'].append('read /home/someone/.cargo/advisory-db'))),
          'a Linux home path is rejected too')
    recon = {'entry_points': [{'id': f"EP-{i}", **{k: e[k] for k in ('handler', 'kind', 'method')}}
                              for i, e in enumerate(ex['recon']['entry_points'], 1)],
             'fixtures_excluded': [{'dir': 'examples/shop', 'reason': 'its own requirements.txt', 'markers': []}]}
    check(any('examples/shop' in e for e in errs(lambda d: None, recon)),
          'a fixture project recon left out must be named in target.excluded or the limitations')
    check(not [e for e in errs(lambda d: d['target']['excluded'].append('examples/shop/ (sample project)'), recon)
               if 'examples/shop' in e], 'naming it in target.excluded satisfies the rule')
    msg = [e for e in errs(lambda d: d['coverage'].__setitem__('entry_points_total', 99)) if 'entry_points_total' in e]
    check(bool(msg) and 'recon.entry_points' in msg[0] and 'recon lists' not in msg[0],
          f'the entry_points_total error names the report\'s own list, not recon\'s count ({msg})')
    recon_in = {'repository': 'x', 'languages': {'Python': 2}, 'entry_points': [
        {'id': 'EP-1', 'kind': 'cli', 'method': 'CLI', 'path': 'shared/x.py', 'handler': 'shared/x.py:9', 'auth': 'unknown',
         'copies': ['pkg-a/x.py:9']}],
        'fixtures_excluded': [{'dir': 'examples/shop', 'reason': 'its own requirements.txt', 'markers': []}],
        'copy_groups': [{'canonical': 'shared/x.py', 'copies': ['pkg-a/x.py'], 'how': 'declared'}]}
    scanners = {'tools': [{'name': 'cargo-audit', 'area': 'dependencies', 'status': 'ran', 'version': '0.21',
                           'outputs': ['C:' + '\\Users\\someone\\run\\cargo-audit.json'], 'results': 0,
                           'note': 'offline: local RustSec database /home/someone/.cargo/advisory-db', 'elapsed_s': 1.0}]}
    s = seed_report.seed(recon_in, {'tools': [], 'candidates': []}, scanners, None)
    text = json.dumps(s)
    check('someone' not in text, 'the seed replaces home-directory paths with ~')
    check(any('examples/shop' in x for x in s['target']['excluded']) and any('examples/shop' in x for x in s['limitations']),
          'the seed names the left-out fixture project in target.excluded and the limitations')
    check('pkg-a/x.py:9' in (s['recon']['entry_points'][0].get('note') or ''), 'the seeded entry point lists its copies')

def lathe_merge(root: Path) -> None:
    print('merge_findings layout on lathe-tools')
    code = [merge_findings.code_candidate('pkg-b/scripts/jobfile.py', 13, ['CWE-502'], 'PY-YAML-LOAD', 'yaml', None,
                                          {'tool': 'bandit', 'rule_id': 'B506'}),
            merge_findings.code_candidate('shared/scripts/jobfile.py', 13, ['CWE-502'], 'PY-YAML-LOAD', 'yaml', None,
                                          {'tool': 'scan_pitfalls', 'rule_id': 'PY-YAML-LOAD'}),
            merge_findings.code_candidate('examples/shop/app.py', 12, ['CWE-22'], None, 'send_file', None,
                                          {'tool': 'semgrep', 'rule_id': 'path-traversal'})]
    kept, _, excluded = merge_findings.apply_layout(code, [], merge_findings.layout_of(root))
    merged = merge_findings.merge_code(kept)
    check(len(merged) == 1 and merged[0]['file'] == 'shared/scripts/jobfile.py' and merged[0].get('copies') == ['pkg-b/scripts/jobfile.py']
          and sorted(merged[0]['tools']) == ['bandit', 'scan_pitfalls'],
          f'a tool hit on a copy joins the canonical candidate ({[(c["file"], c.get("copies"), c["tools"]) for c in merged]})')
    check([(x['file'], x['fixture']) for x in excluded] == [('examples/shop/app.py', 'examples/shop')],
          f'a tool hit in a sample project is set aside with its project ({excluded})')

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
    real_walk = run_scanners.os.walk

    def reversed_walk(top, *a, **kw):   # a filesystem (ext4) that lists directory entries in hash order
        for dp, dns, fns in real_walk(top, *a, **kw):
            dns.reverse()
            fns.reverse()
            yield dp, dns, fns
    run_scanners.os.walk = reversed_walk
    try:
        rev = run_scanners.inventory(root)
    finally:
        run_scanners.os.walk = real_walk
    check(rev['files'] == inv['files'] and rev['lockfiles'] == inv['lockfiles'],
          f"the inventory does not depend on the order the filesystem lists entries in ({rev['lockfiles']})")
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
    check(d['frameworks'] == want['frameworks'], f"frameworks labelled; the sample Next.js app is left out ({d['frameworks']})")
    check(sorted(f['dir'] for f in d.get('fixtures_excluded') or []) == want['fixtures_excluded'],
          f"the sample app under examples/ is listed as left out ({d.get('fixtures_excluded')})")
    d2 = run_recon(root, '--include-fixtures')
    check(d2['frameworks'] == want['frameworks_with_fixtures'],
          f"--include-fixtures: a framework seen only in examples is labelled so ({d2['frameworks']})")
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
    with tempfile.TemporaryDirectory() as tmp:  # an embedded third-party driver package (own manifest and licence)
        t = Path(tmp)
        for n in range(4):
            (t / 'cmd' / f'c{n}').mkdir(parents=True)
            (t / 'cmd' / f'c{n}' / 'main.go').write_text('package main\n\nfunc main() {}\n', encoding='utf-8')
        agent = t / 'drivers' / 'web' / 'agent'
        agent.mkdir(parents=True)
        (agent / 'package.json').write_text('{"name": "agent", "version": "1.0.0"}', encoding='utf-8')
        (agent / 'LICENSE').write_text('MIT', encoding='utf-8')
        for n in range(6):
            (agent / f'm{n}.ts').write_text('export const x = eval(process.argv[2]);\n', encoding='utf-8')
        r = scan_pitfalls.scan(t, scan_pitfalls.load_catalogue(), {'go'})
        cov = r.get('language_coverage') or {}
        check(not r['warnings'] and cov.get('status') == 'ok' and any(e['path'] == 'drivers/web/agent' for e in cov.get('excluded') or []),
              f"lang_census: an embedded driver package is not the main language, and the exclusion is listed ({r['warnings']}, {cov.get('status')})")
        r = scan_pitfalls.scan(t, scan_pitfalls.load_catalogue())
        check(not r['files_scanned'].get('javascript'), f"nor is it pattern-scanned ({r['files_scanned']})")
        rc = run_recon(t)
        check(rc['languages'] == {'Go': 4} and any(v['dir'] == 'drivers/web/agent' for v in rc['vendored']),
              f"recon counts languages with the census ({rc['languages']}, {[v['dir'] for v in rc['vendored']]})")
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
    # scan_secrets reports a generic literal only at entropy 3.0 or more, so this token is high-entropy by
    # construction (16 distinct characters) but typed as the alphabet and 1-8, plainly fake to a repository scanner
    check(len(scan_secrets.find_in_line('const deviceToken = "AbCdEfGh12345678"', 'auth.go')) == 1, 'a token literal still is')

def tidemark(root: Path) -> None:
    """The Rust fixture: a clap derive CLI that finds a helper under the working directory, loads a native library
    from a relative path and a Windows DLL by name, FFI unsafe blocks with SAFETY comments, clamped casts and test
    modules, plus a read-only CI job and a guarded archive extraction."""
    import seed_report  # noqa: E402
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on tidemark-cli')
    res = scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())
    got = {(c['file'], c['line'], c['pitfall_id']) for c in res['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns: CWD and relative search paths, a DLL by bare name, an unchecked '
                      'narrowing; nothing for clamped or float casts, #[cfg(test)] modules, a read-only CI job or a guarded extraction')
    by_key = {f"{c['file']}:{c['line']}:{c['pitfall_id']}": c for c in res['candidates']}
    for key, attrs in want['grouped'].items():
        c = by_key.get(key) or {}
        check(all(c.get(k) == v for k, v in attrs.items()) and c.get('group_reason'),
              f"FFI unsafe blocks with SAFETY comments are one low-confidence candidate ({key}: "
              f"{ {k: c.get(k) for k in attrs} })")
    with_tests = {(c['file'], c['line'], c['pitfall_id']) for c in
                  scan_pitfalls.scan(root, scan_pitfalls.load_catalogue(), include_tests=True)['candidates']}
    check(('src/plotter.rs', 43, 'RUST-CMD') in with_tests and ('src/model.rs', 41, 'RUST-INT-TRUNCATION') in with_tests,
          '--include-tests keeps the #[cfg(test)] module hits')
    check(('src/plotter/plot_checks.rs', 7, 'RUST-SYMLINK-TOCTOU') in with_tests and ('benches/predict.rs', 7, 'RUST-SYMLINK-TOCTOU') in with_tests,
          '--include-tests keeps the hits in a file behind `#[cfg(test)] mod x;` and in benches/')
    check(res.get('test_only_modules') == want['test_only_modules'],
          f"files compiled only for tests are listed ({res.get('test_only_modules')})")
    print('recon on tidemark-cli')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + ('' if hit else f" (got {[(x['kind'], x['method'], x['path'], x['handler']) for x in eps if x['handler'].endswith('.rs:' + e['handler'].split(':')[1]) or x['handler'] == e['handler']]})"))
    rust = [x for x in eps if x['handler'].split(':')[0].endswith('.rs')]
    check(len(rust) == want['rust_entry_point_count'], f"{want['rust_entry_point_count']} Rust entry points ({len(rust)}: "
                                                       f"{[(x['kind'], x['path'], x['handler']) for x in rust]})")
    bad = [x['handler'] for x in eps if x['handler'] in want['no_entry_points_at']]
    check(not bad, f'no entry point on a use line ({bad})')
    flags = [[f['file'], f['name'], f['env']] for f in d.get('cli_flags') or []]
    check(flags == want['cli_flags'], f'clap arguments with the environment variables bound to them ({flags})')
    leads = ' '.join(l['why'] for l in d['review_leads'])
    missing = [m for m in want['lead_mentions'] if m not in leads]
    check(not missing, f'env-chosen executables and libraries, the working-directory search and env-bound flags are review leads (missing {missing})')
    wrong = [m for m in want['no_lead_mentions'] if m in leads]
    check(not wrong, f'an environment input is not described as a config file ({wrong})')
    out_rs = sorted({o['file'] for o in d['outbound_calls'] if o['file'].endswith('.rs')})
    check(out_rs == want['rust_outbound_files'], f'a Rust variable named requests is not an outbound HTTP call ({out_rs})')
    ci = [l['why'] for l in d['review_leads'] if 'download-artifact' in l['why']]
    check(len(ci) == 1 and all(m in ci[0] for m in want['artifact_lead_mentions']),
          f'a job with a write token that downloads artifacts is a review lead ({ci})')
    by_handler = {x['handler']: x for x in eps}
    got_exp = {h: seed_report.exposure_of(by_handler[h]) for h in want['seeded_exposure'] if h in by_handler}
    check(got_exp == want['seeded_exposure'], f'seeded exposure: CLI and environment inputs are local ({got_exp})')

CARGO_AUDIT = {
    'database': {'advisory-count': 1156, 'last-commit': None, 'last-updated': None}, 'lockfile': {'dependency-count': 6},
    'settings': {'target_arch': [], 'target_os': [], 'severity': None, 'ignore': [], 'informational_warnings': ['unmaintained', 'unsound', 'notice']},
    'vulnerabilities': {'found': True, 'count': 2, 'list': [
        {'advisory': {'id': 'RUSTSEC-2020-0159', 'package': 'chrono', 'title': 'Potential segfault in `localtime_r` invocations',
                      'aliases': [], 'related': ['CVE-2020-26235'], 'cvss': None},
         'versions': {'patched': ['>=0.4.20'], 'unaffected': []}, 'affected': None,
         'package': {'name': 'chrono', 'version': '0.4.19', 'source': 'registry+https://github.com/rust-lang/crates.io-index'}},
        {'advisory': {'id': 'RUSTSEC-2020-0071', 'package': 'time', 'title': 'Potential segfault in the time crate',
                      'aliases': ['CVE-2020-26235', 'GHSA-wcg3-cvx6-7396'], 'related': [], 'cvss': None},
         'versions': {'patched': ['>=0.2.23'], 'unaffected': ['=0.2.0']}, 'affected': None,
         'package': {'name': 'time', 'version': '0.1.43'}}]},
    'warnings': {'unmaintained': [{'kind': 'unmaintained', 'package': {'name': 'ansi_term', 'version': '0.12.1'},
                                   'advisory': {'id': 'RUSTSEC-2021-0139', 'package': 'ansi_term', 'title': 'ansi_term is Unmaintained',
                                                'aliases': []}, 'versions': {'patched': [], 'unaffected': []}}],
                 'yanked': [{'kind': 'yanked', 'package': {'name': 'cfg-if', 'version': '1.0.0'}, 'advisory': None, 'versions': None}]}}
CARGO_DENY = '\n'.join(json.dumps(x) for x in [
    {'type': 'diagnostic', 'fields': {'code': 'vulnerability', 'severity': 'error', 'message': 'Potential segfault in the time crate',
                                      'advisory': {'id': 'RUSTSEC-2020-0071', 'package': 'time', 'title': 'Potential segfault in the time crate',
                                                   'aliases': ['CVE-2020-26235']},
                                      'graphs': [{'Krate': {'name': 'time', 'version': '0.1.43'}, 'parents': []}],
                                      'notes': ['ID: RUSTSEC-2020-0071', 'Solution: Upgrade to >=0.2.23 (try `cargo update -p time`)']}},
    {'type': 'diagnostic', 'fields': {'code': 'unmaintained', 'severity': 'warning', 'message': 'ansi_term is Unmaintained',
                                      'advisory': {'id': 'RUSTSEC-2021-0139', 'package': 'ansi_term', 'title': 'ansi_term is Unmaintained', 'aliases': []},
                                      'graphs': [{'Krate': {'name': 'ansi_term', 'version': '0.12.1'}}], 'notes': ['ID: RUSTSEC-2021-0139']}},
    {'type': 'diagnostic', 'fields': {'code': 'yanked', 'severity': 'warning', 'message': 'detected yanked crate (try `cargo update -p cfg-if`)',
                                      'graphs': [{'Krate': {'name': 'cfg-if', 'version': '1.0.0'}}]}},
    {'type': 'summary', 'fields': {'advisories': {'errors': 1, 'warnings': 2, 'notes': 0, 'helps': 0}}}])

def rust_dependency_rules(root: Path) -> None:
    print('Rust dependencies (run_scanners cargo-audit / cargo-deny, merge_findings, seed_report)')
    import run_scanners  # noqa: E402
    import seed_report  # noqa: E402
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    inv = run_scanners.inventory(root)
    deps = run_scanners.dependency_inventory(root, inv)
    check([[l['file'], l['ecosystem'], l['packages']] for l in deps['lockfiles']] == want['dependency_lockfiles'],
          f"Cargo.lock is inventoried ({deps['lockfiles']})")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'advisory-db'
        db.mkdir()
        plan = {t['name']: t for t in run_scanners.plan(root, Path(tmp) / 'out', inv, [], cargo_audit_db=db, offline=True)}
        ca = plan.get('cargo-audit') or {}
        cmd = ' '.join(c for r in ca.get('runs') or [] for c in r['cmd'])
        check(ca.get('applies') is True and ca.get('covers') == ['Cargo.lock'] and ca.get('area') == 'dependencies'
              and ca.get('network') is False and '--no-fetch' in cmd and '--json' in cmd and str(db) in cmd,
              f'cargo-audit runs offline against a local advisory database for each Cargo.lock ({cmd})')
        online = {t['name']: t for t in run_scanners.plan(root, Path(tmp) / 'out', inv, [], offline=False)}.get('cargo-audit') or {}
        check(online.get('network') is True and '--no-fetch' not in ' '.join(c for r in online.get('runs') or [] for c in r['cmd']),
              'online, cargo-audit fetches the advisory database')
        no_db = {t['name']: t for t in run_scanners.plan(root, Path(tmp) / 'out', inv, [], cargo_audit_db=Path(tmp) / 'none',
                                                          offline=True)}.get('cargo-audit') or {}
        check(no_db.get('network') is True, 'offline with no local advisory database, cargo-audit needs the network (not run)')
        cd = plan.get('cargo-deny') or {}
        check(cd.get('applies') is True and cd.get('area') == 'dependencies' and cd.get('covers') == ['Cargo.lock']
              and any(r.get('stderr_to') for r in cd.get('runs') or []), 'cargo-deny check advisories is planned (JSON on stderr)')
        adv = run_scanners.advisory_status([{'name': 'cargo-audit', 'area': 'dependencies', 'status': 'ran', 'covers': ['Cargo.lock']}], deps)
        check(adv['status'] == 'assessed', f'cargo-audit covers Cargo.lock ({adv})')
        adv = run_scanners.advisory_status([{'name': 'cargo-audit', 'area': 'dependencies', 'status': 'ran'}], deps)
        check(adv['status'] == 'assessed', 'an older cargo-audit record without covers covers crates.io lockfiles')
        ca_out = Path(tmp) / 'cargo-audit.json'
        ca_out.write_text(json.dumps(CARGO_AUDIT), encoding='utf-8')
        check(run_scanners.count_results(ca_out) == 4, f'cargo-audit results count vulnerabilities and warnings ({run_scanners.count_results(ca_out)})')
        items, info = merge_findings.from_deps(ca_out)
        got = sorted((i['package'], i['version'], i['advisory'], i.get('kind'), tuple(i['fixed_in'])) for i in items)
        check(info['tool'] == 'cargo-audit' and got == [
            ('ansi_term', '0.12.1', 'RUSTSEC-2021-0139', 'unmaintained', ()),
            ('cfg-if', '1.0.0', 'yanked:cfg-if@1.0.0', 'yanked', ()),
            ('chrono', '0.4.19', 'RUSTSEC-2020-0159', 'vulnerability', ('0.4.20',)),
            ('time', '0.1.43', 'RUSTSEC-2020-0071', 'vulnerability', ('0.2.23',))], f'cargo-audit JSON: vulnerabilities, unmaintained and yanked ({got})')
        check(all(i['ecosystem'] == 'crates.io' for i in items), 'cargo-audit rows are crates.io')
        cd_out = Path(tmp) / 'cargo-deny.json'
        cd_out.write_text(CARGO_DENY, encoding='utf-8')
        items2, info2 = merge_findings.from_deps(cd_out)
        got2 = sorted((i['package'], i['advisory'], i.get('kind'), tuple(i['fixed_in'])) for i in items2)
        check(info2['tool'] == 'cargo-deny' and got2 == [('ansi_term', 'RUSTSEC-2021-0139', 'unmaintained', ()),
                                                         ('cfg-if', 'yanked:cfg-if@1.0.0', 'yanked', ()),
                                                         ('time', 'RUSTSEC-2020-0071', 'vulnerability', ('0.2.23',))],
              f'cargo-deny JSON lines are read ({got2})')
        check(run_scanners.count_results(cd_out) == 3, 'cargo-deny results count its advisory diagnostics')
        merged = merge_findings.merge_deps(items + items2)
        check(len(merged) == 4, f'cargo-audit and cargo-deny rows for one advisory merge ({len(merged)})')
        row = seed_report.dependency(next(m for m in merged if m['kind'] == 'unmaintained'))
        check(row.get('kind') == 'unmaintained' and not validator.schema_errors(row, *(lambda s: (s['properties']['dependencies']['items'], s))(
            json.loads(validator.SCHEMA.read_text(encoding='utf-8')))), f'seed_report keeps the RustSec kind on the dependency row ({row})')
    pat = {'candidates': [{'file': 'src/a.rs', 'line': 3, 'pitfall_id': 'RUST-UNSAFE', 'cwe': ['CWE-119'], 'message': 'm', 'snippet': 's',
                           'grouped_lines': [3, 9], 'confidence': 'low', 'group_reason': 'r'}]}
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'patterns.json'
        p.write_text(json.dumps(pat), encoding='utf-8')
        c = merge_findings.from_patterns(p)[0]
        check(c.get('grouped_lines') == [3, 9] and c.get('confidence') == 'low', 'merge_findings keeps grouped lines and the confidence')

def report_rules_rust() -> None:
    print('validator (tool outputs, contradicting limitations, --write-summary priorities) and the dismissal grouping')
    import render_report  # noqa: E402
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / 'run').mkdir()
        (base / 'run' / 'cargo-audit.json').write_text(json.dumps(CARGO_AUDIT), encoding='utf-8')
        tools = [{'name': 'cargo-audit', 'area': 'dependencies', 'status': 'ran', 'output': 'run/cargo-audit.json', 'results': 4}]
        dep = lambda i, adv, pkg, ver: {'id': f'DEP-{i:03d}', 'ecosystem': 'crates.io', 'package': pkg, 'version': ver,  # noqa: E731
                                        'advisory': adv, 'fixed_in': [], 'reachable': 'unknown', 'basis': 'not traced yet, needs reading'}
        rows = [dep(1, 'RUSTSEC-2021-0139', 'ansi_term', '0.12.1'), dep(2, 'yanked:cfg-if@1.0.0', 'cfg-if', '1.0.0'),
                dep(3, 'RUSTSEC-2020-0159', 'chrono', '0.4.19'), dep(4, 'RUSTSEC-2020-0071', 'time', '0.1.43')]
        rep = {'schema_version': '1.6', 'tools': tools, 'dependencies': rows,
               'dependency_advisories': {'status': 'partial', 'tools': ['cargo-audit'], 'reason': 'the advisory database was a stale local copy'},
               'limitations': ['Dependency advisories were checked against a local RustSec copy that may be stale.'],
               'summary': {'headline': '2 P2 findings'}}
        check(not validator.tool_output_errors(rep, base), f'a dependency tool row with its output and every advisory reported passes '
                                                         f'({validator.tool_output_errors(rep, base)})')
        errs = validator.tool_output_errors({**rep, 'tools': [{**tools[0], 'output': 'run/missing.json'}]}, base)
        check(any('does not exist' in e for e in errs), f'a dependency tool whose output file is missing is rejected ({errs})')
        errs = validator.tool_output_errors({**rep, 'tools': [{**tools[0], 'output': None}]}, base)
        check(any('output' in e for e in errs), 'a dependency tool that ran must name its output')
        errs = validator.tool_output_errors({**rep, 'dependencies': rows[:2]}, base)
        check(any('RUSTSEC-2020-0071' in e for e in errs), f'an advisory in the output but not in dependencies is rejected ({errs})')
        errs = validator.tool_output_errors({**rep, 'tools': [{**tools[0], 'results': 9}]}, base)
        check(any('results' in e for e in errs), 'results that disagree with the output are rejected')
    seeded = 'Dependency advisories were not assessed: no dependency advisory tool ran. This is not the same as no vulnerable dependencies.'
    errs = validator.advisory_errors({**rep, 'limitations': [seeded]})
    check(any('contradicts' in e for e in errs), f'a limitation saying advisories were not assessed contradicts status partial ({errs})')
    errs = validator.advisory_errors({**rep, 'limitations': ['Dependency advisories were not assessed for the npm lockfile web/package-lock.json.']})
    check(not any('contradicts' in e for e in errs), f'a scoped "not assessed for <lockfile>" limitation is consistent with partial ({errs})')
    errs = validator.advisory_errors({**rep, 'dependency_advisories': {'status': 'assessed', 'tools': ['cargo-audit'], 'reason': None},
                                      'limitations': ['No dependency advisory tool ran.']})
    check(any('contradicts' in e for e in errs), 'a limitation saying no advisory tool ran contradicts status assessed')
    f = {'id': 'F-1', 'cvss': {'severity': 'high', 'score': 8.4}, 'confidence': 'traced', 'exposure': 'local'}
    d = {'findings': [f], 'summary': {'counts': {}}}
    validator.fill_computed(d)
    check(f.get('priority_score') == 9 and f.get('priority') == 'P2', f'--write-summary fills priority_score and priority ({f})')
    cases = {
        'src/runner.rs:502 is inside #[cfg(test)] mod tests, never built into the release binary':
            'is inside #[cfg(test)] mod tests, never built into the release binary',
        'quantile is clamped to 0.0..=1.0 at src/model.rs:54 and only finite values are kept':
            'quantile is clamped to 0.0..=1.0 at <file> and only finite values are kept',
        'runs python (the interpreter named in PYTHON_BIN) with a fixed script path; no input reaches it':
            'runs python with a fixed script path',
        "FFI blocks, read against the engine's render_api.h contract":
            "FFI blocks, read against the engine's render_api.h contract",
    }
    for reason, label in cases.items():
        got = render_report.reason_group(reason)
        check(got == label, f'dismissal group label keeps the reason readable ({got!r})')
    ex = HERE.parent / 'examples' / 'security_audit.example.json'
    run = lambda *a: subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(ex), '--color', 'never', *a],  # noqa: E731
                                    capture_output=True, text=True, encoding='utf-8').stdout
    full, brief = run(), run('--brief')
    rep = json.loads(ex.read_text(encoding='utf-8'))
    obs_note = rep['observations'][0]['note'] if rep['observations'] else ''
    check(len(brief.splitlines()) * 2 < len(full.splitlines()) and (not obs_note or obs_note not in brief)
          and sum(1 for l in rep['limitations'] if l in brief) <= 3,
          f'--brief is a summary: tools by status, observation titles, the first limitations ({len(brief.splitlines())} vs '
          f'{len(full.splitlines())} lines)')

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
    art = merge_findings.map_pitfall(merge_findings.tool_index(), 'zizmor', 'artipacked')
    check(art == 'GHA-PERSISTED-CREDENTIALS', f'zizmor artipacked (persisted checkout credentials) has its own pitfall, '
                                              f'not pull_request_target ({art})')
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
        (root / 'src' / 'cache.json').write_text('{"key": "deadbeefdeadbeefdeadbeefdeadbeef"}\n', encoding='utf-8')
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
    # inline interpreter code and wrapped shells: the denylist cannot judge code, so inline code is refused outright
    for argv, why in ((['python', '-c', "import shutil; shutil.rmtree('/nonexistent-qa-lint-probe')"], 'python -c'),
                      (['env', 'bash', '-c', 'echo $((7*191))'], 'env-wrapped shell'),
                      (['node', '-e', "require('fs').rmSync('/nonexistent-qa-lint-probe',{recursive:true,force:true})"], 'node -e'),
                      ([r'C:\Python\python.exe', '-c', 'print(7*191)'], 'python.exe -c, even harmless'),
                      (['node', '--eval=console.log(7*191)'], 'node --eval='),
                      (['wsl', 'ls'], 'wsl launcher')):
        got = run_poc.lint({**ok, 'steps': [{'name': 'run', 'argv': argv}]}, None, [])
        check(bool(got), f'{why} must be refused by --check ({argv})')
    # code in a file is linted, and destructive file operations in it are refused
    got = run_poc.lint({**ok, 'files': {'probe.py': "import shutil\nshutil.rmtree('/nonexistent-qa-lint-probe')\n"},
                        'steps': [{'name': 'run', 'argv': ['python', 'probe.py']}]}, None, [])
    check(any('destructive file operation' in i for i in got), f'shutil.rmtree in a PoC file must be refused ({got})')
    got = run_poc.lint({**ok, 'files': {'probe.js': "require('fs').unlinkSync('x')\n"}, 'steps': [{'name': 'run', 'argv': ['node', 'probe.js']}]}, None, [])
    check(any('destructive file operation' in i for i in got), f'fs.unlinkSync in a PoC file must be refused ({got})')
    got = run_poc.lint({**ok, 'files': {'probe.py': 'print(7*191)\n'}, 'steps': [{'name': 'run', 'argv': ['python', 'probe.py']}]}, None, [])
    check(got == [], f'a harmless script run by path passes the lint ({got})')
    check(any('marker' in i for i in run_poc.lint({**ok, 'files': {'a.txt': 'CANARY-1337'}}, None, [])),
          'a marker planted in the input proves nothing')
    report = {'recon': {'entry_points': [{'id': 'EP-1', 'handler': 'a.ts:1', 'kind': 'cli'}],
                        'removed_entry_points': [{'id': 'EP-3', 'handler': '.github/workflows/x.yml:9', 'reason': 'a permissions key, not a trigger'}]}}
    recon = {'entry_points': [{'id': 'EP-1', 'handler': 'a.ts:1', 'kind': 'cli', 'method': 'BIN'},
                              {'id': 'EP-2', 'handler': 'b.ts:4', 'kind': 'listener', 'method': 'LISTEN'},
                              {'id': 'EP-3', 'handler': '.github/workflows/x.yml:9', 'kind': 'ci_workflow', 'method': 'issues'}]}
    errs = validator.recon_errors(report, recon)
    check(len(errs) == 1 and 'EP-2' in errs[0], 'a recon entry point dropped without a reason is an error; a removal with one is not')
    # two client sources recon found on one template line: keep one, remove the duplicate by id
    same = {'entry_points': [{'id': 'EP-35', 'handler': 'templates/partials/a.html:19', 'kind': 'client_source', 'method': 'location.search'},
                             {'id': 'EP-36', 'handler': 'templates/partials/a.html:19', 'kind': 'client_source', 'method': 'location.href'}]}
    rep = {'recon': {'entry_points': [{'id': 'EP-35', 'handler': 'templates/partials/a.html:19', 'kind': 'client_source'}],
                     'removed_entry_points': [{'id': 'EP-36', 'handler': 'templates/partials/a.html:19',
                                               'reason': 'the same line as EP-35; location.pathname, not location.href'}]}}
    errs = validator.recon_errors(rep, same)
    check(not errs, f'a same-line duplicate entry point can be removed by id ({errs})')
    rep['recon']['entry_points'].append({'id': 'EP-36', 'handler': 'templates/partials/a.html:19', 'kind': 'client_source'})
    errs = validator.recon_errors(rep, same)
    check(any('EP-36 is still in' in e for e in errs), 'an entry point both kept and removed is still an error')
    check(bool(validator.ROTATION_REASON.search('public analytics key by design, never a credential'))
          and not validator.ROTATION_REASON.search('gone from HEAD, so nothing to do'),
          'a dismissed history-only secret must say it was rotated or why it needs no rotation')

class CountReads:
    """Count files opened under a directory (Path.read_text/read_bytes/open and builtins.open) while active."""

    def __init__(self, under: Path):
        import builtins, pathlib
        self.under, self.count, self._b, self._p = str(under.resolve()), 0, builtins, pathlib
        self._saved = {}

    def _wrap(self, fn):
        def inner(*a, **k):
            target = a[0] if a else k.get('file')
            try:
                if isinstance(target, (str, Path)) and str(Path(target).resolve()).startswith(self.under):
                    self.count += 1
            except (OSError, ValueError):
                pass
            return fn(*a, **k)
        return inner

    def __enter__(self):
        P = self._p.Path
        for name in ('read_text', 'read_bytes', 'open'):
            self._saved[name] = getattr(P, name)
            setattr(P, name, self._wrap(self._saved[name]))
        self._saved['builtins.open'] = self._b.open
        self._b.open = self._wrap(self._b.open)
        return self

    def __exit__(self, *exc):
        P = self._p.Path
        for name in ('read_text', 'read_bytes', 'open'):
            setattr(P, name, self._saved[name])
        self._b.open = self._saved['builtins.open']

def tide(root: Path) -> None:
    """The Flask and Jinja fixture: templates are scanned, rate limiting and unbounded responses are modelled,
    and a data directory of generated JSON files is pruned by every walker without a file in it being opened."""
    import run_scanners  # noqa: E402
    import time as _time
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on tide-tables')
    res = scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())
    got = {(c['file'], c['line'], c['pitfall_id']) for c in res['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns in app.py, mailer.py and the Jinja templates; nothing for |tojson in a '
                      'script, a quoted attribute, <script src="{{ }}">, autoescape=True or a header read only to log it')
    print('recon on tide-tables')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + ''.join(f', {k} {e[k]}' for k in ('auth', 'bind') if k in e)
                         + ('' if hit else f" (got {[(x['kind'], x['path'], x.get('auth')) for x in eps if x['handler'] == e['handler']]})"))
    rl = d.get('rate_limiting') or {}
    check(all(rl.get(k) == v for k, v in want['rate_limiting'].items()),
          f"rate limiting: library, a key read from X-Forwarded-For and the limited routes ({rl})")
    leads = ' '.join(l['why'] for l in d.get('review_leads') or [])
    missing = [m for m in want['lead_mentions'] if m not in leads]
    check(not missing, f'spoofable rate-limit key and an uncapped search route are review leads (missing {missing})')
    check(sorted(d.get('templates') or []) == want['templates'], f"server-side templates listed ({d.get('templates')})")
    check(d.get('skipped_data_dirs') == [], f"nothing pruned as data in the fixture itself ({d.get('skipped_data_dirs')})")
    print('data directory of generated JSON files (copy of tide-tables)')
    import shutil
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp) / 'tide'
        shutil.copytree(root, t)
        data = t / want['data_dir']['name']
        n = want['data_dir']['generated_files']
        for i in range(n):
            (data / f'S{i:04d}.json').write_text(json.dumps({'code': f'S{i:04d}', 'name': f'Station {i}', 'notes': ''}),
                                                encoding='utf-8')
        (t / 'infra').mkdir()
        (t / 'infra' / 'site.json').write_text('{"$schema": "https://schema.management.azure.com/schemas/2019-04-01/'
                                               'deploymentTemplate.json#", "resources": []}\n', encoding='utf-8')
        rel = want['data_dir']['name']
        def pruned(rec: dict | None) -> bool:
            return any(s.get('dir') == rel and s.get('files', 0) >= n for s in (rec or {}).get('skipped_data_dirs') or [])
        with CountReads(data) as cr:
            t0 = _time.monotonic()
            r = scan_pitfalls.scan(t, scan_pitfalls.load_catalogue())
            el = _time.monotonic() - t0
        check(cr.count == 0 and pruned(r), f'scan_pitfalls opens no file in the data directory ({cr.count} opened, '
                                           f"{el:.1f}s) and records it ({r.get('skipped_data_dirs')})")
        check({(c['file'], c['line'], c['pitfall_id']) for c in r['candidates']} == exp,
              'the same pattern hits with the data directory present')
        check(r['files_scanned'].get('azure_iac') == 1, f"an ARM template outside the data directory is still read ({r['files_scanned']})")
        with CountReads(data) as cr:
            found, _ = scan_secrets.scan_tree(t)
            skipped = scan_secrets.LAST_SKIPPED if hasattr(scan_secrets, 'LAST_SKIPPED') else None
        check(cr.count == 0 and pruned({'skipped_data_dirs': skipped}),
              f'scan_secrets opens no file in the data directory ({cr.count}) and says what it did not read ({skipped})')
        with CountReads(data) as cr:
            inv = run_scanners.inventory(t)
        check(cr.count == 0 and pruned(inv) and not any(f.startswith(rel + '/') for f in inv['files']),
              f"run_scanners inventory opens no data file ({cr.count}) and lists the pruned directory ({inv.get('skipped_data_dirs')})")
        sg = next((x for x in run_scanners.plan(t, Path('out'), inv, ['p/default']) if x['name'] == 'semgrep'), {})
        check(any(f'--exclude={rel}' in r['cmd'] for r in sg.get('runs') or []),
              f"semgrep is told to leave the pruned directory out ({[r['cmd'] for r in sg.get('runs') or []]})")
        rc = run_recon(t)
        check(pruned(rc), f"recon records the pruned data directory ({rc.get('skipped_data_dirs')})")
        import seed_report  # noqa: E402
        seeded = seed_report.seed(rc, {}, None, None)
        check(any(f'{rel}/' in l and 'not read' in l for l in seeded['limitations']),
              f"seed_report names the unread data directory in limitations ({seeded['limitations']})")
        r2 = scan_pitfalls.scan(t, scan_pitfalls.load_catalogue(), exclude_dirs=['templates'])
        check(not any(c['file'].startswith('templates/') for c in r2['candidates'])
              and any(s.get('dir') == 'templates' and 'exclude' in s.get('reason', '') for s in r2['skipped_data_dirs']),
              f"--exclude-dir leaves a directory out and records why ({r2.get('skipped_data_dirs')})")
        r3 = scan_pitfalls.scan(t, scan_pitfalls.load_catalogue(), data_dirs=False)
        check(not r3['skipped_data_dirs'], '--scan-data-dirs turns the data-directory rule off')
    print('rate-limit and template helpers')
    js = scan_pitfalls.compile_pitfall(next(p for p in scan_pitfalls.load_catalogue()['pitfalls'] if p['id'] == 'JS-RATELIMIT-KEY-HEADER'))
    hits = scan_pitfalls.scan_file
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / 'limits.js'
        f.write_text("const limiter = rateLimit({\n  keyGenerator: (req) => req.headers['x-forwarded-for'] || req.ip,\n});\n", encoding='utf-8')
        g = Path(tmp) / 'log.js'
        g.write_text("console.log(req.headers['x-forwarded-for']);\n", encoding='utf-8')
        check(len(hits(f, 'limits.js', [js])) == 1 and not hits(g, 'log.js', [js]),
              'express-rate-limit keyGenerator on x-forwarded-for is a lead; logging the header is not')

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

def wren(root: Path) -> None:
    """The static Next.js App Router content site: pages, metadata routes, the image optimiser, a service worker,
    a CSP and a catch-all Cache-Control in next.config headers(), JSON-LD, prose in data strings, agent config."""
    want = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    print('scan_pitfalls (all packs) on wren-field-guide')
    got = {(c['file'], c['line'], c['pitfall_id']) for c in scan_pitfalls.scan(root, scan_pitfalls.load_catalogue())['candidates']}
    exp = {tuple(x) for x in want['patterns']}
    for x in sorted(exp - got):
        check(False, f'missed {x[2]} at {x[0]}:{x[1]}')
    for x in sorted(got - exp):
        check(False, f'unexpected {x[2]} at {x[0]}:{x[1]}')
    check(got == exp, f'{len(exp)} planted patterns: CSP unsafe-inline, https: source and tag-manager allowance, a catch-all '
                      'shared-cache rule, JSON-LD as its own rule (not an HTML sink); nothing for img-src https: or style-src')
    print('recon on wren-field-guide')
    d = run_recon(root)
    eps = d['entry_points']
    for e in want['entry_points']:
        hit = [x for x in eps if all(x.get(k) == v for k, v in e.items())]
        check(bool(hit), f"{e['kind']} {e['method']} {e.get('path', '')} at {e['handler']}"
                         + ''.join(f', {k} {e[k]}' for k in ('auth', 'static', 'params_bounded') if k in e)
                         + ('' if hit else f" (got {[(x['kind'], x['path'], x['auth'], x.get('static')) for x in eps if x['handler'] == e['handler']]})"))
    check(len(eps) == want['entry_point_count'], f"{want['entry_point_count']} entry points ({len(eps)})")
    files = sorted({o['file'] for o in d['outbound_calls']})
    check(files == want['outbound_files'], f'outbound calls only from code, not prose in data strings or the service worker ({files})')
    sh = d.get('security_headers') or {}
    check(sorted(sh.get('missing') or []) == want['security_headers_missing'],
          f"security headers read from next.config headers(): missing {sh.get('missing')}")
    check(sorted(d.get('agent_config') or []) == want['agent_config'], f"committed agent/editor config listed ({d.get('agent_config')})")
    leads = ' | '.join(l['why'] for l in d['review_leads'])
    for m in want['lead_mentions']:
        check(m in leads, f'a review lead mentions {m!r}')
    for m in want['no_lead_mentions']:
        check(m not in leads, f'no review lead says {m!r}')

def static_site_rules() -> None:
    print('header pack rules, outbound code positions')
    import recon as recon_mod  # noqa: E402
    cat = {p['id']: scan_pitfalls.compile_pitfall(p) for p in scan_pitfalls.load_catalogue()['pitfalls']}
    def fires(rule: str, line: str) -> bool:
        p = cat.get(rule)
        return bool(p and p['_rx'].search(line) and not (p.get('_suppress_line') and p['_suppress_line'].search(line)))
    check(fires('HDR-CSP-UNSAFE-EVAL', "  \"script-src 'self' 'unsafe-eval'\","), "'unsafe-eval' in script-src is a candidate")
    check(fires('HDR-CSP-WILDCARD-SOURCE', "script-src 'self' *;") and fires('HDR-CSP-WILDCARD-SOURCE', '"default-src https:",')
          and not fires('HDR-CSP-WILDCARD-SOURCE', "img-src 'self' https:") and not fires('HDR-CSP-WILDCARD-SOURCE', "script-src 'self' https://cdn.example.test"),
          'a * or scheme-only source in script-src/default-src is a candidate; img-src https: and a named host are not')
    check(not fires('HDR-CSP-UNSAFE-INLINE', "style-src 'self' 'unsafe-inline'"), "'unsafe-inline' in style-src is not a script candidate")
    check(not fires('JS-XSS-SINK', '<script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }} />')
          and fires('JS-JSONLD-STRINGIFY', 'dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }}')
          and not fires('JS-JSONLD-STRINGIFY', "dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, '\\\\u003c') }}"),
          'JSON-LD built with JSON.stringify is its own rule (escaping < suppresses it), not an HTML sink')
    import next_surface  # noqa: E402
    check(next_surface.capped('const items = ALL.slice(0, 20).map(f);') == '20' and next_surface.capped('return rows.filter(f);') is None,
          'a literal cap on the result count (slice(0, 20)) is recognised, so a capped route is not an uncapped-collection lead')
    code = recon_mod.code_only('const a = "you have got. Then fetch(it)";\nconst b = `line one\nrequests.get(x)`;\nawait fetch(url);\n', '.ts')
    lines = code.splitlines()
    check(not recon_mod.OUTBOUND.search(lines[0]) and not recon_mod.OUTBOUND.search(lines[2])
          and bool(recon_mod.OUTBOUND.search(lines[3])) and code.count('\n') == 4,
          'string literals (including multi-line template literals) are blanked before matching outbound calls; lines are kept')

def poc_guard_and_render_rules() -> None:
    print('run_poc guard checks, observation evidence, seeded run_poc row, empty findings table')
    import run_poc  # noqa: E402
    import seed_report  # noqa: E402
    check(run_poc.result_of({'purpose': 'guard_check'}, True) == 'guard_held'
          and run_poc.result_of({'purpose': 'guard_check'}, False) == 'guard_not_held'
          and run_poc.result_of({}, True) == 'confirmed' and run_poc.result_of({}, False) == 'not_confirmed',
          'a passing guard check is guard_held, never "confirmed" (the word for a confirmed vulnerability)')
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        poc = {'finding': 'O-1', 'kind': 'command', 'purpose': 'guard_check',
               'files': {'probe.py': 'print(7*191)\n'},
               'steps': [{'name': 'run', 'argv': [sys.executable, 'probe.py']}],
               'proof': {'type': 'marker_in_output', 'step': 'run', 'marker': '1337'}}
        check(run_poc.lint(poc, None, []) == [], f'a guard-check command PoC passes the lint ({run_poc.lint(poc, None, [])})')
        ev = run_poc.run_command_poc(poc, t, 'self-test', t / 'poc.json')
        check(ev['result'] == 'guard_held' and ev.get('purpose') == 'guard_check', f"command guard check recorded as {ev['result']}")
        ex = json.loads((HERE.parent / 'examples' / 'security_audit.example.json').read_text(encoding='utf-8'))
        def obs_errors(evidence: dict | None, rel: str = 'ev/O-1.json') -> list[str]:
            d = json.loads(json.dumps(ex))
            d['schema_version'] = '1.5'
            d['observations'][0]['evidence'] = rel
            if evidence is not None:
                (t / 'ev').mkdir(exist_ok=True)
                (t / 'ev' / 'O-1.json').write_text(json.dumps(evidence), encoding='utf-8')
            errs, _ = validator.check(d, t, None, None)
            return [e for e in errs if e.startswith('observations[')]
        good = {'tool': 'run_poc', 'finding': 'O-1', 'purpose': 'guard_check', 'result': 'guard_held', 'permission': 'self-test',
                'proof': {'passed': True}}
        check(obs_errors(good) == [], f'an observation can cite a guard check that held ({obs_errors(good)})')
        check(any('guard did not hold' in e for e in obs_errors({**good, 'result': 'guard_not_held', 'proof': {'passed': False}})),
              'an observation citing a guard that did not hold is an error (that is a finding)')
        check(any('is for' in e for e in obs_errors({**good, 'finding': 'O-9'})), 'observation evidence must be for that observation')
        (t / 'ev' / 'O-1.json').unlink()
        check(any('not found' in e for e in obs_errors(None)), 'missing observation evidence is an error')
        recon = {'repository': 'x', 'entry_points': [], 'languages': {'TypeScript': 1}}
        rows = seed_report.seed(recon, {'tools': [], 'candidates': []}, None, None)['tools']
        row = next((r for r in rows if r['name'] == 'run_poc'), None)
        check(bool(row) and row['area'] == 'dynamic' and row['status'] == 'not_run', f'the seeded report has a run_poc row ({row})')
        d = json.loads(json.dumps(ex))
        d['findings'] = []
        d['summary']['counts'] = validator.compute_summary(d)
        (t / 'empty.json').write_text(json.dumps(d), encoding='utf-8')
        out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(t / 'empty.json'), '--color', 'never'],
                             capture_output=True, text=True, encoding='utf-8').stdout
        check('| # | ID |' not in out and 'No findings' in out, 'a report with no findings says so instead of printing an empty table')

def scan_tree(files: dict[str, str], **kw) -> dict:
    """scan_pitfalls over a scratch repository holding these files."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'proj'
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        return scan_pitfalls.scan(root, scan_pitfalls.load_catalogue(), **kw)

PS_BOOTSTRAP = '''param([string]$Password)
Install-Module -Name Pester -Force
Install-Module -Name PSScriptAnalyzer -RequiredVersion 1.21.0 -Force
winget install --id Vendor.Player --silent
winget install --id Vendor.Tool --version 2.1.0
& player.exe --http-password=$Password --intf http
'''
SH_INSTALL = '''#!/bin/sh
curl -fsSL https://example.test/install.sh | sh
player --http-password="$PLAYER_PASSWORD" --intf http &
player --intf http
'''
PY_RUNNER = '''import subprocess

def start(password):
    return subprocess.Popen(['player', f'--http-password={password}', '--intf', 'http'])

def tools():
    subprocess.run(['winget', 'install', '--id', 'Vendor.Player'], check=True)
    subprocess.run(['winget', 'install', '--id', 'Vendor.Tool', '--version', '2.1.0'], check=True)

def install_command(winget, package):
    return [winget, 'install', '--exact', '--id', package]
'''
PY_LOOPBACK = '''import urllib.request

def poll(port, auth):
    status_url = f'http://127.0.0.1:{port}/requests/status.json'
    for _ in range(3):
        request = urllib.request.Request(status_url, headers={'Authorization': 'Basic ' + auth})
        with urllib.request.urlopen(request, timeout=1) as response:
            yield response.read()
'''
PY_REMOTE = '''import urllib.request

def poll(status_url, auth):
    request = urllib.request.Request(status_url, headers={'Authorization': 'Basic ' + auth})
    return urllib.request.urlopen(request, timeout=1).read()
'''

def scan_scope_rules() -> None:
    """A catalogue without packs, loopback-only credentials, node:vm, script languages, match-arm guards."""
    print('catalogue defaults, loopback credentials, node:vm, PowerShell and shell, cast guards')
    ids = lambda res: sorted((c['file'], c['line'], c['pitfall_id']) for c in res['candidates'])  # noqa: E731
    variant = {'version': 'variant', 'pitfalls': [{'id': 'VAR-TAKE', 'pack': 'rust', 'cwe': 'CWE-427', 'category': 'language_specific',
                                                   'title': 'a variant', 'regex': r'\.take\s*\(\s*3\s*\)', 'fix': 'x'}]}
    with tempfile.TemporaryDirectory() as tmp:
        cat = Path(tmp) / 'variant.json'
        cat.write_text(json.dumps(variant), encoding='utf-8')
        p = subprocess.run([sys.executable, str(HERE / 'scan_pitfalls.py'), str(HERE.parent / 'examples' / 'tidemark-cli'),
                            '--catalogue', str(cat)], capture_output=True, text=True, encoding='utf-8')
        got = [(c['file'], c['line']) for c in json.loads(p.stdout)['candidates']] if p.returncode == 0 else p.stderr.strip().splitlines()[-1:]
        check(p.returncode == 0 and got == [('src/bundled.rs', 9)],
              f'a --catalogue file with only version and pitfalls takes its packs from the built-in catalogue ({got})')
        cat.write_text(json.dumps({**variant, 'pitfalls': [{**variant['pitfalls'][0], 'pack': 'cobol'}]}), encoding='utf-8')
        p = subprocess.run([sys.executable, str(HERE / 'scan_pitfalls.py'), str(HERE.parent / 'examples' / 'tidemark-cli'),
                            '--catalogue', str(cat)], capture_output=True, text=True, encoding='utf-8')
        check(p.returncode == 2 and 'cobol' in p.stderr and 'Traceback' not in p.stderr,
              f'a pitfall naming a pack no catalogue defines is refused with a message (exit {p.returncode})')
    got = ids(scan_tree({'tools/poll.py': PY_LOOPBACK, 'tools/remote.py': PY_REMOTE}))
    check(got == [('tools/remote.py', 4, 'PY-AUTH-REDIRECT')],
          f'an Authorization header sent to a constant loopback URL is not a redirect candidate; one sent to a caller URL is ({got})')
    got = ids(scan_tree({'check.mjs': "import vm from 'node:vm';\nvm.runInNewContext(script, context);\nconst r = eval(input);\n"}))
    check(got == [('check.mjs', 2, 'JS-VM-RUN'), ('check.mjs', 3, 'JS-EVAL')],
          f'node:vm running a script is its own rule, not "eval, new Function or string timers" ({got})')
    res = scan_tree({'app/main.py': PY_RUNNER, 'bootstrap.ps1': PS_BOOTSTRAP, 'install.sh': SH_INSTALL})
    got = ids(res)
    want = [('app/main.py', 4, 'PY-SECRET-ARGV'), ('app/main.py', 7, 'PY-INSTALL-UNPINNED'), ('app/main.py', 11, 'PY-INSTALL-UNPINNED'),
            ('bootstrap.ps1', 2, 'PS-INSTALL-UNPINNED'), ('bootstrap.ps1', 4, 'PS-INSTALL-UNPINNED'), ('bootstrap.ps1', 6, 'PS-SECRET-ARGV'),
            ('install.sh', 2, 'SH-PIPE-TO-SHELL'), ('install.sh', 3, 'SH-SECRET-ARGV')]
    check(got == want, f'secrets on a command line and unpinned installs in Python, PowerShell and shell; pinned ones are not ({got})')
    cov = res['language_coverage']
    notes = ' '.join(cov.get('notes') or [])
    check('PowerShell' in notes and 'Shell' in notes and (cov.get('script_packs') or {}).get('PowerShell'),
          f"PowerShell and shell scripts are named as read by a small pack only, never as analysed ({cov.get('script_packs')})")
    res = scan_tree({'app/main.py': PY_RUNNER, 'bootstrap.ps1': PS_BOOTSTRAP}, wanted={'python'})
    notes = ' '.join(res['language_coverage'].get('notes') or [])
    check('PowerShell' in notes and 'not analysed' in notes, 'without the powershell pack the scripts are declared not analysed')
    import seed_report  # noqa: E402
    with tempfile.TemporaryDirectory() as tmp:
        pat = Path(tmp) / 'patterns.json'
        pat.write_text(json.dumps(res), encoding='utf-8')
        carried = merge_findings.coverage_of(pat) if hasattr(merge_findings, 'coverage_of') else None
        seeded = seed_report.seed({'repository': 'x', 'entry_points': [], 'languages': {'Python': 1}},
                                  {'tools': [], 'candidates': [], 'language_coverage': carried}, None, None)
        check(any('PowerShell' in l for l in seeded['limitations']),
              'merge_findings carries the language coverage notes and seed_report writes them as limitations')
        rep = {'limitations': ['Lua was not analysed.']}
        errs = validator.language_errors(rep, {'language_coverage': carried}) if hasattr(validator, 'language_errors') else []
        check(any('PowerShell' in e for e in errs), f'a report that drops the unanalysed script language from its limitations is rejected ({errs})')
        errs = validator.language_errors({'limitations': ['2 PowerShell scripts were not analysed.']}, {'language_coverage': carried}) \
            if hasattr(validator, 'language_errors') else ['missing']
        check(errs == [], f'naming the script language in a limitation satisfies the rule ({errs})')
    arm = ('pub fn row(mode: u32, table: &[u8]) -> u8 {\n    match mode {\n        m if m <= 2 => {\n            let index = m as u8;\n'
           '            table[index as usize]\n        }\n        _ => 0,\n    }\n}\n'
           'pub fn tag(p: *const u8) -> bool {\n    let offset = p as usize; // sentinel compare\n    offset == usize::MAX\n}\n'
           'pub fn wrap(len: u32) -> u16 {\n    len as u16\n}\n')
    got = ids(scan_tree({'Cargo.toml': '[package]\nname = "x"\nversion = "0.1.0"\n', 'src/lib.rs': arm}))
    check(got == [('src/lib.rs', 15, 'RUST-INT-TRUNCATION')],
          f'a cast under a match-arm guard and a pointer-to-usize cast are not truncation candidates; an unchecked one is ({got})')

def docs_and_evidence_rules() -> None:
    """SKILL.md names what the schema requires; run_poc redacts escaped work-copy paths; small renderer and seed items."""
    print('SKILL.md against the schema, run_poc path redaction, offline cargo-audit, renderer and seed details')
    import run_poc  # noqa: E402
    import run_scanners  # noqa: E402
    import seed_report  # noqa: E402
    schema = json.loads(validator.SCHEMA.read_text(encoding='utf-8'))
    skill = (HERE.parent / 'SKILL.md').read_text(encoding='utf-8')
    decisions = schema['properties']['triage']['items']['properties']['decision']['enum']
    missing = [d for d in decisions if f'`decision: "{d}"`' not in skill and f'`"decision": "{d}"`' not in skill]
    check(not missing, f'SKILL.md gives every triage decision value the schema accepts (missing {missing})')
    vr = schema['properties']['variant_rules']['items']
    missing = [k for k in sorted(set(vr['required']) | {'test_note'}) if f'`{k}`' not in skill]
    check(not missing, f'SKILL.md names every variant_rules field the schema requires (missing {missing})')
    at = skill.find('--catalogue <that file>')
    check('ancestors' in skill and at > 0 and '`packs`' in skill[at - 900:at + 900],
          'SKILL.md warns that searching upward from current_exe() is a search path too, and says what a variant catalogue needs')
    red = getattr(run_poc, 'redact_workdir', None)
    win = 'C:\\work\\copy one'
    out = red('{"chosen": "C:\\\\work\\\\copy one\\\\poc\\\\tools\\\\x.exe"} and C:/work/copy one/poc', win) if red else win
    check(win not in out and 'C:\\\\work' not in out and 'C:/work' not in out and '{workdir}' in out,
          f'run_poc replaces the work copy path when the program prints it JSON-escaped or with forward slashes ({out})')
    masked = validator.user_masked('- C:\\Users\\someone\\Videos\\a.mp4') if hasattr(validator, 'user_masked') else ''
    check(masked == '- C:\\Users\\<user>\\Videos\\a.mp4' and not validator.home_path_errors({'x': masked})
          and validator.home_path_errors({'x': 'C:\\Users\\someone\\x'}),
          f'an observation may quote a committed home path with the account written <user>, and only that way ({masked!r})')
    leak = {'steps': [{'name': 'run', 'argv': ['{workdir}/bin/x'], 'exit_code': 0,
                       'stdout_excerpt': '{"chosen": "C:\\\\Users\\\\someone\\\\copy\\\\tools\\\\x.exe"}', 'stderr_excerpt': ''}]}
    errs = validator.evidence_home_paths(leak) if hasattr(validator, 'evidence_home_paths') else []
    check(len(errs) == 1 and validator.evidence_home_paths({'steps': [{**leak['steps'][0], 'stdout_excerpt': '{workdir}/x'}]}) == [],
          f'the validator rejects command evidence whose output holds a home-directory path, JSON-escaped too ({errs})')
    hint = run_poc.marker_hint('C:\\work\\planted.exe', '{"path": "C:\\\\work\\\\planted.exe"}') if hasattr(run_poc, 'marker_hint') else ''
    check('escaped' in hint, f'a marker found only with escaped backslashes is said so in the proof ({hint!r})')
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / 'Cargo.lock').write_text('version = 3\n', encoding='utf-8')
        db = t / 'advisory-db'
        db.mkdir()
        inv = run_scanners.inventory(t)
        ca = {x['name']: x for x in run_scanners.plan(t, t / 'out', inv, [], cargo_audit_db=db, offline=True)}['cargo-audit']
        env = (ca['runs'][0].get('env') or {}) if ca.get('runs') else {}
        check(env.get('CARGO_NET_OFFLINE') == 'true' and str(env.get('HTTPS_PROXY', '')).startswith('http://127.0.0.1:')
              and ca.get('network_guard'), f'offline, cargo-audit runs with CARGO_NET_OFFLINE and a closed proxy ({sorted(env)})')
        probe = t / 'fake_audit.py'
        probe.write_text('import json, os\nprint(json.dumps({"database": {"advisory-count": 1}, "lockfile": {}, "settings": {}, '
                         '"vulnerabilities": {"found": False, "count": 0, "list": []}, "warnings": {"yanked": [{"kind": "yanked", '
                         '"package": {"name": "cfg-if", "version": "1.0.0"}, "advisory": None, "versions": None}]}, '
                         '"proxy": os.environ.get("HTTPS_PROXY"), "offline": os.environ.get("CARGO_NET_OFFLINE")}))\n', encoding='utf-8')
        (t / 'out').mkdir()
        fake = {**ca, 'exe': sys.executable, 'runs': [{**ca['runs'][0], 'cmd': [sys.executable, str(probe)]}]} if ca.get('runs') else None
        rec = run_scanners.run_tool(fake, t / 'out', 60) if fake else {}
        seen = json.loads((t / 'out' / 'cargo-audit.json').read_text(encoding='utf-8')) if (t / 'out' / 'cargo-audit.json').exists() else {}
        check(seen.get('offline') == 'true' and str(seen.get('proxy', '')).startswith('http://127.0.0.1:'),
              f'the guard reaches the cargo-audit process ({seen.get("offline")}, {seen.get("proxy")})')
        local = seed_report.entry_point({'id': 'EP-1', 'kind': 'cli', 'method': 'ROOT', 'path': 'x', 'handler': 'src/main.rs:3',
                                         'auth': 'unknown', 'auth_evidence': 'local invocation: the caller supplies arguments'})
        web = {**local, 'kind': 'http_route', 'exposure': 'internet', 'auth': 'local_user'}
        ex2 = json.loads((HERE.parent / 'examples' / 'security_audit.example.json').read_text(encoding='utf-8'))
        ex2['recon']['entry_points'].append({**web, 'id': 'EP-99'})
        errs = [e for e in validator.check(ex2, HERE.parent / 'examples', None, None)[0] if 'local_user' in e]
        check(local['auth'] == 'local_user' and errs, f"a local program is seeded auth local_user, and the validator refuses it on "
                                                      f"an internet entry point ({local['auth']}, {errs})")
        check(rec.get('network') is False and 'closed' in (rec.get('network_guard') or '') and 'yanked' in (rec.get('yanked_check') or ''),
              f"the row says how the network was kept closed and where the yanked answers came from ({rec.get('yanked_check')})")
        row = next((r for r in seed_report.tool_rows({'tools': [rec]}, {'tools': []}, {}) if r['name'] == 'cargo-audit'), {}) if rec else {}
        check('closed' in (row.get('config') or ''), f"the seeded tool row carries the network guard ({row.get('config')})")
        recon = {'repository': 'clone-7', 'entry_points': [], 'languages': {'Rust': 1}}
        check(seed_report.seed(recon, {'tools': [], 'candidates': []}, None, None, name='tidemark')['target']['repository'] == 'tidemark'
              if 'name' in seed_report.seed.__code__.co_varnames else False, 'seed_report --name sets the repository name (not the clone folder)')
        check(getattr(seed_report, 'origin_name', lambda u: None)('git@example.test:harbour/tidemark.git') == 'tidemark'
              and getattr(seed_report, 'origin_name', lambda u: None)('https://example.test/harbour/tidemark') == 'tidemark',
              'the default name comes from the origin URL when there is one')
        ex = json.loads((HERE.parent / 'examples' / 'security_audit.example.json').read_text(encoding='utf-8'))
        ex['schema_version'] = '1.6'
        ex['verification'].update(target=None, target_kind='local_copy', local_copy='~/scratch/copy')
        (t / 'local.json').write_text(json.dumps(ex), encoding='utf-8')
        out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(t / 'local.json'), '--color', 'never'],
                             capture_output=True, text=True, encoding='utf-8').stdout
        line = next((l for l in out.splitlines() if l.startswith('Verification:')), '')
        check('None' not in line and 'local copy' in line and '~/scratch/copy' in line, f'a command-only verification names the local copy ({line})')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fixture', type=Path, default=HERE.parent / 'examples' / 'kite-notes')
    ap.add_argument('--azure-fixture', type=Path, default=HERE.parent / 'examples' / 'ferry-ops')
    ap.add_argument('--node-fixture', type=Path, default=HERE.parent / 'examples' / 'quill-cli')
    ap.add_argument('--go-fixture', type=Path, default=HERE.parent / 'examples' / 'beacon-cli')
    ap.add_argument('--flask-fixture', type=Path, default=HERE.parent / 'examples' / 'tide-tables')
    ap.add_argument('--static-fixture', type=Path, default=HERE.parent / 'examples' / 'wren-field-guide')
    ap.add_argument('--rust-fixture', type=Path, default=HERE.parent / 'examples' / 'tidemark-cli')
    ap.add_argument('--tool-fixture', type=Path, default=HERE.parent / 'examples' / 'lathe-tools')
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
    try:
        tide(args.flask_fixture.resolve())
    except Exception as exc:  # noqa: BLE001 - a missing helper is a failure, not a crash of the whole self-test
        check(False, f'tide-tables: {type(exc).__name__}: {exc}')
    rust_root = args.rust_fixture.resolve()
    for step in (lambda: wren(args.static_fixture.resolve()), static_site_rules, poc_guard_and_render_rules,
                 lambda: tidemark(rust_root), lambda: rust_dependency_rules(rust_root), report_rules_rust):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - a missing helper is a failure, not a crash of the whole self-test
            check(False, f'{type(exc).__name__}: {exc}')
    tool_root = args.tool_fixture.resolve()
    for fn in (lathe, lathe_recon, lathe_secrets, lathe_deps, lathe_merge):
        try:
            fn(tool_root)
        except Exception as exc:  # noqa: BLE001 - a missing helper is a failure, not a crash of the whole self-test
            check(False, f'lathe-tools ({fn.__name__}): {type(exc).__name__}: {exc}')
    try:
        report_scope_rules()
    except Exception as exc:  # noqa: BLE001 - a missing helper is a failure, not a crash of the whole self-test
        check(False, f'report scope rules: {type(exc).__name__}: {exc}')
    for step in (scan_scope_rules, docs_and_evidence_rules):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - a missing helper is a failure, not a crash of the whole self-test
            check(False, f'{step.__name__}: {type(exc).__name__}: {exc}')
    node_and_ci_rules()
    poc_and_report_rules()
    python_rules()
    advisory_rules()
    merge()
    validator_rules()
    own_literals()
    print(f"self-test {'passed' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0

if __name__ == '__main__':
    raise SystemExit(main())
