#!/usr/bin/env python3
"""Seed security_audit.json from the recon, scanner and candidate files, for the agent to complete.

    python seed_report.py --recon recon.json --candidates candidates.json [--scanners scanners.json]
                          [--repo <repo>] --out security_audit.json

Writes the parts of the report that are copied, not judged: the target, recon entry points (kind,
method, path, handler line, the script's auth guess and its evidence), data stores, outbound calls
and secret configuration, one tools row per scanner (status, version, output, results, elapsed
time) plus the built-in scanners, one triage row per code and secret candidate, one dependencies
row per advisory (installed version, range, dev flag, via chain), coverage totals and the summary
counts.

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

def entry_point(e: dict) -> dict:
    # a CLI runs locally; queue, blob and timer triggers are reached through the platform, not by a request
    exposure = 'local' if e['kind'] == 'cli' else 'internal' if e['kind'] in ('queue_consumer', 'other') else 'internet'
    out = {'id': e['id'], 'kind': e['kind'], 'method': e['method'], 'path': e['path'], 'handler': e['handler'],
           'auth': e['auth'], 'exposure': exposure, 'reviewed': False}
    if e.get('auth_evidence'):
        out['auth_evidence'] = e['auth_evidence']
    if e.get('also_in'):
        out['note'] = f"the same inline script is in {e['also_in']} more page(s)"
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
    rows.append({'name': 'agent', 'area': 'agent', 'status': 'ran', 'config': f'{TODO}: what the manual review covered'})
    return rows

def dependency(x: dict) -> dict:
    out = {'id': x['id'], 'ecosystem': x.get('ecosystem') or 'unknown', 'package': x['package'], 'version': str(x.get('version') or '?'),
           'advisory': x['advisory'], 'aliases': x.get('aliases') or [], 'fixed_in': x.get('fixed_in') or [],
           'summary': x.get('summary') or '', 'reachable': 'unknown'}
    for k in ('range', 'via'):
        if x.get(k):
            out[k] = x[k]
    if x.get('dev') is not None:
        out['dev'] = x['dev']
    hint = ('every installed copy is a devDependency in the lockfile; if it is not bundled into production output, '
            'set reachable no with that basis') if x.get('dev') else 'say which API the advisory needs and whether the code calls it'
    if x.get('via'):
        hint += f"; pulled in by {', '.join(x['via'][:5])}"
    out['basis'] = f'{TODO}: {hint}'
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

def seed(recon: dict, cands: dict, scanners: dict | None, repo: Path | None) -> dict:
    eps = [entry_point(e) for e in recon.get('entry_points') or []]
    tools = tool_rows(scanners, cands, recon)
    not_ran = [t['name'] for t in tools if t['status'] in ('not_run', 'errored')]
    stores = list(OrderedDict((f"{s['kind']} ({s['file']}:{s['line']})", None) for s in recon.get('data_stores') or []))
    d = {
        'schema_version': '1.2',
        'skill': {'name': 'code-security-auditor', 'version': skill_version()},
        'sensitive': True,
        'notice': NOTICE,
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'target': {'repository': recon.get('repository') or (repo.name if repo else 'unknown'), 'commit': git_commit(repo),
                   'scope': ['.'], 'excluded': []},
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
                    'reason': f"{TODO}: promote, observe or dismiss {c.get('file')}:{c.get('line')} ({', '.join(c.get('pitfall_ids') or c.get('cwe') or [c.get('message') or 'candidate'])[:80]})"}
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
        d['limitations'].append(f"Dependency advisories were {adv['status'].replace('_', ' ')}: {adv['reason']}. "
                                'This is not the same as no vulnerable dependencies.')
    if recon.get('middleware'):
        d['recon']['middleware'] = [f"{m['file']}:{m['line']} matcher {', '.join(m['matcher']) or '(all paths)'}; "
                                    f"protects {', '.join(m['protected_prefixes']) or 'every matched path'}; "
                                    f"{'covers' if m['covers_api'] else 'does not cover'} /api/*" for m in recon['middleware']]
    d['summary']['counts'] = validator.compute_summary(d)
    return d

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--recon', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--scanners', type=Path)
    ap.add_argument('--repo', type=Path)
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
    d = seed(recon, cands, scanners, args.repo.resolve() if args.repo else None)
    args.out.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    todo = sum(text.count(TODO) for _, text in validator.json_strings(d))
    print(f"seeded {len(d['recon']['entry_points'])} entry point(s), {len(d['tools'])} tool row(s), {len(d['triage'])} triage row(s), "
          f"{len(d['dependencies'])} advisory(ies) -> {args.out}; {todo} {TODO} placeholder(s) to replace")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
