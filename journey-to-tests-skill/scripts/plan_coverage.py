#!/usr/bin/env python3
"""Start a Journey to Tests report: one step_coverage entry for every Expect in the selected journeys.

    python plan_coverage.py journeys/ --out journey_tests_report.json \
        [--priority P1,P2] [--domain checkout] [--platform web] [--id JRN-CHK-001] [--repo .]

Every step and variant Expect of every selected journey gets an entry with coverage `pending`.
The agent then fills in the plan and the results; the validator fails while any entry is still
pending, so no Expect can be dropped silently. Each journey's Expect fingerprint is recorded so
the validator can detect a journey that was edited during the run.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_md  # noqa: E402

def expects(j: dict) -> list[dict]:
    """Every Expect in a parsed journey, in document order: steps first, then variants."""
    out = [{'ref': f"step {s['n']}", 'expect': s['expect']} for s in j['steps']]
    out += [{'ref': v['id'], 'expect': v['expect'], 'from_step': v['from_step'], 'name': v['name']} for v in j['variants']]
    return out

def expect_sha256(j: dict) -> str:
    """Fingerprint of what a journey asks tests to check (Expects and Not automatable entries)."""
    payload = {'expects': [(e['ref'], ' '.join(e['expect'].split())) for e in expects(j)],
               'not_automatable': [(n['target'], ' '.join(n['reason'].split())) for n in j['not_automatable']]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()

def load_journeys(paths: list[str]) -> list[tuple[Path, dict]]:
    files, _ = journey_md.iter_journey_files(paths)
    out = []
    for f in files:
        if f.name.endswith('.proposed.md'):
            continue  # proposals are not approved content; test the file they would replace
        out.append((f, journey_md.load(f)))
    return out

def selected(j: dict, args) -> tuple[bool, str | None]:
    m = j['meta']
    want = lambda opt: [x.strip() for x in opt.split(',')] if opt else None
    for opt, key in ((args.priority, 'priority'), (args.domain, 'domain'), (args.id, 'id')):
        vals = want(opt)
        if vals and m.get(key) not in vals:
            return False, f'filtered out by {key}'
    plats = want(args.platform)
    if plats and not set(plats) & set(m.get('platforms') or []):
        return False, 'filtered out by platform'
    return True, None

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Create a journey_tests_report.json skeleton from journey files.')
    ap.add_argument('journeys', nargs='+', help='journey .md files or directories')
    ap.add_argument('--out', required=True)
    ap.add_argument('--repo', default='.', help='repository root; journey paths are recorded relative to it')
    ap.add_argument('--priority'); ap.add_argument('--domain'); ap.add_argument('--platform'); ap.add_argument('--id')
    ap.add_argument('--force', action='store_true', help='overwrite an existing report')
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f'{out} exists; pass --force to start a new report', file=sys.stderr); return 2
    repo = Path(args.repo).resolve()
    lint = journey_md.lint_paths(args.journeys, strict=False)
    loaded = load_journeys(args.journeys)
    if not loaded:
        print('No journey files found.', file=sys.stderr); return 1

    def relpath(p: Path) -> str:
        try:
            return p.resolve().relative_to(repo).as_posix()
        except ValueError:
            return p.as_posix()

    journeys, coverage = [], []
    for path, j in loaded:
        m = j['meta']
        in_scope, why = selected(j, args)
        errors = [err for r in lint['files'] if Path(r['file']).resolve() == path.resolve() for err in r['errors']]
        if errors and in_scope:
            in_scope, why = False, 'journey file has lint errors: ' + '; '.join(errors[:3])
        journeys.append({'id': m.get('id'), 'path': relpath(path), 'title': m.get('title'), 'priority': m.get('priority'),
                         'platforms': m.get('platforms') or [], 'status': m.get('status'),
                         'automation_before': m.get('automation', 'none'), 'tests_before': m.get('tests') or [],
                         'expect_sha256': expect_sha256(j), 'in_scope': in_scope,
                         **({'scope_reason': why} if why else {})})
        if not in_scope:
            continue
        listed = {n['target'] for n in j['not_automatable'] if n['target']}
        for e in expects(j):
            entry = {'journey_id': m['id'], 'ref': e['ref'], 'expect': e['expect'], 'coverage': 'pending',
                     'assertions': [], 'test_ids': []}
            if e['ref'] in listed:
                entry['journey_lists_not_automatable'] = True
            coverage.append(entry)

    report = {
        'metadata': {'repository': repo.name, 'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'skill_version': '1.0.0', 'journeys_source': [Path(p).as_posix() for p in args.journeys],
                     'frameworks': [], 'platforms': [], 'target': None, 'repeat': 3,
                     'approval': {'gate': 'on', 'plan_approved': False},
                     'filters': {k: getattr(args, k) for k in ('priority', 'domain', 'platform', 'id') if getattr(args, k)}},
        'journeys': journeys, 'mapping': [], 'step_coverage': coverage, 'testability_gaps': [],
        'runs': [], 'cases': [], 'results': [], 'suspected_bugs': [], 'files_changed': [],
        'lint': {'ran': False}, 'journey_updates': [], 'open_questions': [], 'summary': {},
    }
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    n_in = sum(1 for x in journeys if x['in_scope'])
    print(f'{n_in} of {len(journeys)} journey(s) in scope, {len(coverage)} Expect(s) to cover -> {out}')
    for x in journeys:
        if not x['in_scope'] and x.get('scope_reason', '').startswith('journey file has lint errors'):
            print(f"  SKIPPED {x['id']}: {x['scope_reason']}")
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
