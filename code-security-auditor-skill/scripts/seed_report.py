#!/usr/bin/env python3
"""Seed security_audit.json from the recon, scanner and candidate files, for the agent to complete.

    python seed_report.py --recon recon.json --candidates candidates.json [--scanners scanners.json]
                          [--repo <repo>] [--name NAME] --out security_audit.json

target.repository is --name, else the last part of the repository's origin URL, else the folder name (a clone in a
scratch folder would otherwise name the report after the scratch folder). The pattern scan's language notes (languages
and script kinds it did not analyse, carried in candidates.json) become limitations.

Writes the parts of the report that are copied, not judged: the target, recon entry points (kind,
method, path, handler line, the script's auth guess and its evidence), data stores, outbound calls
and secret configuration, one tools row per scanner (status, version, output, results, elapsed
time) plus the built-in scanners, one triage row per code and secret candidate, one dependencies
row per advisory (installed version, range, dev flag, via chain), coverage totals and the summary
counts.

Fixture projects recon left out are named in target.excluded and the limitations; copies of a file are named in
each entry point's note and triage reason; the home directory in every copied path or command becomes ~.

Everything that needs judgement is left as a TODO(seed) placeholder: each triage decision and
reason, each dependency's reachability and basis, the trust boundaries, the auth model, the
headline, coverage notes and limitations. validate_security_audit.py rejects a report that still
holds one, so nothing seeded can pass as reviewed. Triage rows start as `dismissed` only because
the schema needs a decision; replace each with promoted, observation or a checkable dismissal.
Refuses to overwrite an existing file unless --force is given.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import audit_scope  # noqa: E402
import validate_security_audit as validator  # noqa: E402

TODO = validator.SEED_TODO
NOTICE = ('SENSITIVE: this report contains working exploit details for unfixed vulnerabilities. Keep it out of version '
          'control and share it only with people who should see them.')
AREA_OF = {'sast': 'sast', 'patterns': 'patterns', 'secrets': 'secrets', 'dependencies': 'dependencies', 'ci': 'ci',
           'iac_containers': 'iac_containers', 'mobile': 'mobile'}

def skill_version() -> str:
    m = re.search(r'^version:\s*(\S+)', (HERE.parent / 'SKILL.md').read_text(encoding='utf-8'), re.M)
    return m.group(1) if m else 'unknown'

def git_commit(repo: Path | None) -> str | None:
    if not repo:
        return None
    try:
        p = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=30)
        return p.stdout.strip() or None if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def origin_name(url: str | None) -> str | None:
    """The repository name in a remote URL: git@host:owner/name.git, https://host/owner/name(.git), a local path."""
    if not url:
        return None
    tail = re.split(r'[/:\\]', url.strip().rstrip('/'))[-1]
    return re.sub(r'\.git$', '', tail) or None

def repo_name(repo: Path | None) -> str | None:
    if not repo:
        return None
    try:
        p = subprocess.run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=30)
        url = p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        url = None
    return origin_name(url)

def exposure_of(e: dict) -> str:
    """local: the attacker supplies a file, argument, stdin, environment or config (a CLI, a stdio MCP server, a
    loopback listener); internal: reached through a platform (queues, timers); internet otherwise. A listener or
    HTTP MCP endpoint whose bind host recon could not read stays internet until the agent reads it."""
    if e['kind'] in ('cli', 'config_input', 'file_parser'):
        return 'local'
    if e['kind'] == 'mcp_tool' and e.get('transport') == 'stdio':
        return 'local'
    if e['kind'] in ('listener', 'mcp_tool', 'http_route', 'webhook') and e.get('bind') in ('loopback', 'unix'):
        return 'local'  # a loopback port or a unix socket (and routes recon saw served only on one)
    if e['kind'] in ('listener', 'mcp_tool'):
        return 'internet'
    return 'internal' if e['kind'] in ('queue_consumer', 'other') else 'internet'

def entry_point(e: dict) -> dict:
    out = {'id': e['id'], 'kind': e['kind'], 'method': e['method'], 'path': e['path'], 'handler': e['handler'],
           'auth': e['auth'], 'exposure': exposure_of(e), 'reviewed': False}
    if out['auth'] == 'unknown' and out['exposure'] == 'local' and str(e.get('auth_evidence') or '').startswith('local invocation'):
        out['auth'] = 'local_user'  # a local program run by the user it acts for: no account to check (correct it if it has one)
    if e.get('auth_evidence'):
        out['auth_evidence'] = e['auth_evidence']
    notes = []
    if e.get('also_in'):
        notes.append(f"the same inline script is in {e['also_in']} more page(s)")
    if e.get('transport'):
        notes.append(f"MCP transport {e['transport']}" + (f", bind {e['bind']}" if e.get('bind') else ''))
    elif e.get('bind'):
        notes.append(f"bind {e['bind']}")
    if e.get('segment_note'):  # Next.js: prerendered at build, ids bounded by generateStaticParams
        notes.append(e['segment_note'])
    if e.get('copies'):  # a file kept identical in several places: one entry point, reviewed once
        notes.append(f"the same code is kept in {len(e['copies'])} more place(s): {', '.join(e['copies'][:5])}"
                     + (' ...' if len(e['copies']) > 5 else ''))
    if e.get('example'):
        out['example'] = True
        notes.append('example or fixture code (not part of the shipped product unless it is deployed)')
    if notes:
        out['note'] = '; '.join(notes)
    return out

def tool_rows(scanners: dict | None, cands: dict, recon: dict) -> list[dict]:
    rows = [{'name': 'recon', 'area': 'recon', 'status': 'ran', 'version': skill_version(), 'config': 'scripts/recon.py',
             'results': len(recon.get('entry_points') or [])}]
    for t in (scanners or {}).get('tools', []):
        if t['status'] == 'planned':
            continue
        row = {'name': t['name'], 'area': AREA_OF.get(t['area'], 'sast'), 'status': t['status'], 'version': t.get('version'),
               'output': (t.get('outputs') or [None])[0], 'results': t.get('results'), 'reason': t.get('reason')}
        if t.get('elapsed_s') is not None:
            row['config'] = f"{t.get('note') or 'default configuration'}; took {t['elapsed_s']}s"
        if t.get('covers') and t['area'] == 'dependencies':  # which lockfiles this tool read (or would have)
            shown = ', '.join(t['covers'][:4]) + (f' and {len(t["covers"]) - 4} more' if len(t['covers']) > 4 else '')
            row['config'] = (row.get('config') + '; ' if row.get('config') else '') + f'lockfiles: {shown}'
        if t.get('network_guard'):  # an offline run: how the network was kept closed, and what that left out
            row['config'] = (row.get('config') + '; ' if row.get('config') else '') + t['network_guard']
            if t.get('yanked_check'):
                row['config'] += f"; {t['yanked_check']}"
        if t.get('advisory_db'):  # cargo-audit: the RustSec database it read (a stale offline copy belongs in limitations)
            db = t['advisory_db']
            row['config'] = (row.get('config') + '; ' if row.get('config') else '') + \
                f"RustSec database: {db.get('advisories')} advisories, last updated {db.get('last_updated') or 'unknown'}"
        if t.get('stderr'):  # e.g. cargo-audit's yanked check, which needs the crates.io index
            row['reason'] = '; '.join(([row['reason']] if row.get('reason') else []) + t['stderr'][:2])
        rows.append({k: v for k, v in row.items() if v is not None or k in ('version', 'reason')})
    for t in cands.get('tools', []):
        if t['tool'] == 'scan_pitfalls':
            rows.append({'name': 'scan_pitfalls', 'area': 'patterns', 'status': 'ran', 'version': skill_version(),
                         'output': t['input'], 'results': t['results'], 'config': 'data/pitfalls.json'})
        elif t['tool'] == 'scan_secrets':
            extra = [f"history {t.get('history')}"]
            if t.get('history_only'):
                extra.append(f"{t['history_only']} only in history")
            if t.get('test_fixtures'):
                extra.append(f"{t['test_fixtures']} test-file literals set aside as fixtures (not triaged)")
            rows.append({'name': 'scan_secrets', 'area': 'secrets', 'status': 'ran', 'version': skill_version(),
                         'output': t['input'], 'results': t['results'], 'config': '; '.join(extra)})
    # PoCs and guard checks (run_poc.py): seeded as not run; set status ran, with results and output, when any ran
    rows.append({'name': 'run_poc', 'area': 'dynamic', 'status': 'not_run', 'version': None, 'config': 'scripts/run_poc.py',
                 'reason': 'verification was not requested, so no PoC or guard check ran'})
    rows.append({'name': 'agent', 'area': 'agent', 'status': 'ran', 'config': f'{TODO}: what the manual review covered'})
    return rows

def dependency(x: dict) -> dict:
    out = {'id': x['id'], 'ecosystem': x.get('ecosystem') or 'unknown', 'package': x['package'], 'version': str(x.get('version') or '?'),
           'advisory': x['advisory'], 'aliases': x.get('aliases') or [], 'fixed_in': x.get('fixed_in') or [],
           'summary': x.get('summary') or '', 'reachable': 'unknown'}
    for k in ('range', 'via', 'kind'):
        if x.get(k):
            out[k] = x[k]
    if x.get('dev') is not None:
        out['dev'] = x['dev']
    hint = ('every installed copy is a devDependency in the lockfile; if it is not bundled into production output, '
            'set reachable no with that basis') if x.get('dev') else 'say which API the advisory needs and whether the code calls it'
    if x.get('via'):
        hint += f"; pulled in by {', '.join(x['via'][:5])}"
    out['basis'] = f'{TODO}: {hint}'
    # govulncheck's call-graph level seeds reachability: a reached vulnerable symbol is a finding to write (or a
    # reasoned downgrade); an imported package with no reached symbol, or a required module never imported, is not
    level = x.get('reachability')
    if x.get('scan_level', 'symbol') != 'symbol' and level != 'symbol':
        level = None  # a -scan package or module run never looks for calls, so it can't say a symbol is unreached
    if level == 'symbol':
        out['reachable'] = 'yes'
        out['basis'] = (f"{TODO}: govulncheck (symbol level) found a call path {x.get('call_path') or ''}; confirm the "
                        'path is live, then write the finding and link it (or say why the path cannot run)')
    elif level == 'package':
        out['reachable'] = 'no'
        out['basis'] = (f"govulncheck (symbol level): {x.get('vulnerable_package') or 'the vulnerable package'} is imported, "
                        'but no vulnerable function is reachable from this code in its static call graph (calls through '
                        'reflection or unsafe are not seen)')
    elif level == 'module':
        out['reachable'] = 'no'
        out['basis'] = ('govulncheck (symbol level): the module is required, but the vulnerable package is not imported '
                        'by any package built from this module')
    return out

def advisories(scanners: dict | None, cands: dict) -> dict:
    """Whether dependency advisories were checked: copied from run_scanners.py's manifest when there is one."""
    adv = (scanners or {}).get('dependency_advisories')
    inv = (scanners or {}).get('dependency_inventory') or {}
    if adv:
        out = {'status': adv['status'], 'tools': adv.get('tools') or [], 'reason': adv.get('reason')}
        out['lockfiles'] = [l['file'] for l in inv.get('lockfiles') or []]
        out['packages'] = inv.get('packages')
        return out
    return {'status': 'assessed' if cands.get('dependencies') else 'not_assessed', 'tools': [],
            'reason': f'{TODO}: no scanners.json was given; say which dependency tool checked advisories, or why none did'}

def high_risk_areas(recon: dict) -> list[dict]:
    by: OrderedDict[str, dict] = OrderedDict()
    for lead in recon.get('review_leads') or []:
        a = by.setdefault(lead['area'] if lead['area'] in validator_areas() else 'configuration',
                          {'area': lead['area'] if lead['area'] in validator_areas() else 'configuration', 'why': [], 'entry_points': []})
        a['why'].append(lead['why'])
        if lead.get('entry_point'):
            a['entry_points'].append(lead['entry_point'])
    return [{'area': a['area'], 'why': f"{len(a['why'])} lead(s) from recon, e.g. {a['why'][0]}", 'entry_points': a['entry_points']}
            for a in by.values()]

def validator_areas() -> set[str]:
    schema = json.loads(validator.SCHEMA.read_text(encoding='utf-8'))
    return set(schema['$defs']['area']['enum'])

def seed(recon: dict, cands: dict, scanners: dict | None, repo: Path | None, name: str | None = None) -> dict:
    eps = [entry_point(e) for e in recon.get('entry_points') or []]
    tools = tool_rows(scanners, cands, recon)
    not_ran = [t['name'] for t in tools if t['status'] in ('not_run', 'errored')]
    stores = list(OrderedDict((f"{s['kind']} ({s['file']}:{s['line']})", None) for s in recon.get('data_stores') or []))
    d = {
        'schema_version': '1.7',
        'skill': {'name': 'code-security-auditor', 'version': skill_version()},
        'sensitive': True,
        'notice': NOTICE,
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'target': {'repository': name or repo_name(repo) or recon.get('repository') or (repo.name if repo else 'unknown'),
                   'commit': git_commit(repo),
                   'scope': ['.'], 'excluded': [f"{f['dir']}/ (sample or fixture project: {f['reason']})"
                                                for f in recon.get('fixtures_excluded') or []]},
        'recon': {
            'stack': sorted(recon.get('languages') or {}) + (recon.get('frameworks') or []) or [f'{TODO}: languages and frameworks'],
            'entry_points': eps,
            'trust_boundaries': [f'{TODO}: who can reach what (internet, signed-in users, admins, CI, third parties)'],
            'auth_model': {'authentication': f'{TODO}: how users sign in', 'sessions': f'{TODO}: how sessions are kept and signed',
                           'authorisation': f'{TODO}: where ownership and roles are checked'},
            'data_stores': stores,
            'outbound_calls': [f"{o['file']}:{o['line']}" for o in (recon.get('outbound_calls') or [])],
            'secret_config': [f"{o['file']}:{o['line']}" for o in (recon.get('secret_config') or [])],
            'high_risk_areas': high_risk_areas(recon),
        },
        'tools': tools,
        'verification': {'requested': False, 'permitted': False, 'permission': None, 'target': None, 'target_kind': 'none'},
        'triage': [{'candidate': c['id'], 'decision': 'dismissed',
                    'reason': f"{TODO}: promote, observe or dismiss {c.get('file')}:{c.get('line')} ({', '.join(c.get('pitfall_ids') or c.get('cwe') or [c.get('message') or 'candidate'])[:80]})"
                              + (f"; grouped with lines {', '.join(str(x) for x in c['grouped_lines'][1:])}, confidence {c.get('confidence')}"
                                 if len(c.get('grouped_lines') or []) > 1 else '')
                              + (f"; the same line is in {len(c['copies'])} identical copy(ies): {', '.join(c['copies'][:4])}"
                                 + (' ...' if len(c['copies']) > 4 else '') if c.get('copies') else '')}
                   for c in cands.get('candidates', [])]
                  + [{'candidate': s['id'], 'decision': 'dismissed',
                      'reason': f"{TODO}: {s['type']} at {s.get('file')}:{s.get('line') or 'history ' + ','.join(s.get('history_commits') or [])}; "
                                'a real secret (finding with a secret block) or a placeholder (say why)'}
                     for s in cands.get('secrets', [])],
        'findings': [],
        'dependencies': [dependency(x) for x in cands.get('dependencies', [])],
        'dependency_advisories': advisories(scanners, cands),
        'variant_rules': [],
        'observations': [],
        'coverage': {'languages_reviewed': [], 'files_in_scope': int(recon.get('files_in_scope') or recon.get('source_files') or 0), 'files_read': 0,
                     'entry_points_total': len(eps), 'entry_points_reviewed': 0, 'areas_deep_reviewed': [],
                     'not_reviewed': [{'area': 'entry points', 'reason': f'{TODO}: which entry points were not reviewed and why'}]},
        'limitations': [f'{TODO}: what the review could not cover'] +
                       [f'{n} did not run ({next(t.get("reason") for t in tools if t["name"] == n)}).' for n in not_ran],
        'files_written': [],
        'summary': {'headline': f'{TODO}: what was found and what was covered', 'counts': {}},
    }
    adv = d['dependency_advisories']
    if adv['status'] != 'assessed' and TODO not in (adv.get('reason') or ''):
        said = 'only partly assessed' if adv['status'] == 'partial' else adv['status'].replace('_', ' ')
        d['limitations'].append(f"Dependency advisories were {said}: {adv['reason']}. "
                                'This is not the same as no vulnerable dependencies.')
    for t in (scanners or {}).get('tools', []):  # an offline advisory database is only as current as its last update
        if t.get('advisory_db') and t.get('status') == 'ran':
            d['limitations'].append(f"{t['name']} read a local RustSec database (last updated "
                                    f"{t['advisory_db'].get('last_updated') or 'unknown'}); advisories published since are not covered.")
    lc = cands.get('language_coverage') or {}
    for note in lc.get('notes') or []:  # unanalysed languages and script kinds: not assessed, never clean
        d['limitations'].append(note)
    if lc.get('warning'):
        d['limitations'].append(lc['warning'])
    fx = recon.get('fixtures_excluded') or []
    if fx:  # sample projects left out of recon and the scans: their planted defects are not the repository's findings
        d['limitations'].append(f"{len(fx)} sample or fixture project(s) were left out of recon, the pattern scan and the "
                                f"scanners ({', '.join(f['dir'] + '/' for f in fx[:8])}{' ...' if len(fx) > 8 else ''}); a "
                                'deliberately vulnerable sample is not a finding of this repository. Their secrets were '
                                'still read (low-entropy literals set aside). Rerun with --include-fixtures to audit them.')
    groups = recon.get('copy_groups') or []
    if groups:
        d['coverage']['not_reviewed'].append({'area': 'copies', 'reason': (
            f"{sum(len(g['copies']) for g in groups)} identical copies of {len(groups)} file(s) were read once, on the "
            'canonical copy; each finding and entry point on a canonical copy applies to its copies too')})
    for s in recon.get('skipped_data_dirs') or []:  # data trees pruned by recon, the pattern scan and the secret scan
        d['limitations'].append(f"{s['dir']}/ was not read by recon, the pattern scan or the secret scan ({s['reason']}); "
                                'a secret or code placed there was not checked.')
    if recon.get('middleware'):
        d['recon']['middleware'] = [f"{m['file']}:{m['line']} matcher {', '.join(m['matcher']) or '(all paths)'}; "
                                    f"protects {', '.join(m['protected_prefixes']) or 'every matched path'}; "
                                    f"{'covers' if m['covers_api'] else 'does not cover'} /api/*" for m in recon['middleware']]
    d['summary']['counts'] = validator.compute_summary(d)
    return redact_tree(d)

def redact_tree(value):
    """Every string with the home directory replaced by ~ (tool commands, output paths and database paths would
    otherwise carry the account name into the report)."""
    if isinstance(value, str):
        return audit_scope.redact_home(value)
    if isinstance(value, list):
        return [redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    return value

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--recon', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--scanners', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--name', help='the repository name for the report (default: from the origin URL, else the folder)')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()
    if args.out.exists() and not args.force:
        print(f'{args.out} exists; pass --force to overwrite it', file=sys.stderr)
        return 2
    try:
        recon = json.loads(args.recon.read_text(encoding='utf-8'))
        cands = json.loads(args.candidates.read_text(encoding='utf-8'))
        scanners = json.loads(args.scanners.read_text(encoding='utf-8')) if args.scanners else None
    except (OSError, ValueError) as exc:
        print(f'Unable to read input: {exc}', file=sys.stderr)
        return 2
    d = seed(recon, cands, scanners, args.repo.resolve() if args.repo else None, args.name)
    args.out.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    todo = sum(text.count(TODO) for _, text in validator.json_strings(d))
    print(f"seeded {len(d['recon']['entry_points'])} entry point(s), {len(d['tools'])} tool row(s), {len(d['triage'])} triage row(s), "
          f"{len(d['dependencies'])} advisory(ies) -> {args.out}; {todo} {TODO} placeholder(s) to replace")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
