#!/usr/bin/env python3
"""Write `automation` and `tests` back into journey front matter from a Journey to Tests report.

    python update_journeys.py journey_tests_report.json --repo . [--dry-run]

Only runs when the user turned on `update_journeys`. Only those two front-matter lines change;
status, prose, Notes and everything else are left exactly as they were.

  automated  every Expect is asserted in a passing test, or listed under Not automatable
  partial    at least one Expect is asserted in a passing test
  none       no Expect is asserted in a passing test

Automation is never downgraded: an existing `partial` or `automated` set by journey-mapper or a
person describes tests this run did not evaluate. Test paths are added, never removed. The
updates are recorded in the report's `journey_updates` and `files_changed`.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_md  # noqa: E402

RANK = {'none': 0, 'partial': 1, 'automated': 2}

def computed_automation(entries: list[dict], passing: set[str]) -> str:
    verified = [bool(e.get('assertions')) and bool(set(e.get('test_ids', [])) & passing) for e in entries]
    if entries and all(v or e.get('coverage') == 'not_automatable' for v, e in zip(verified, entries)) and any(verified):
        return 'automated'
    return 'partial' if any(verified) else 'none'

def set_front_matter(text: str, automation: str, tests: list[str]) -> str:
    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.replace('\r\n', '\n').split('\n')
    if not lines or lines[0].strip() != '---':
        raise ValueError('no front matter')
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
    values = {'automation': automation, 'tests': '[' + ', '.join(journey_md._quote(t, True) for t in tests) + ']'}
    for key, val in values.items():
        for i in range(1, end):
            if lines[i].split(':', 1)[0].strip() == key:
                lines[i] = f'{key}: {val}'
                break
        else:
            lines.insert(end, f'{key}: {val}'); end += 1
    return nl.join(lines)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Update journey automation/tests front matter from a report.')
    ap.add_argument('report')
    ap.add_argument('--repo', default='.')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    rpath, repo = Path(args.report), Path(args.repo)
    d = json.loads(rpath.read_text(encoding='utf-8'))
    passing = {r['test_id'] for r in d.get('results', []) if r.get('status') == 'pass'}
    paths_by_journey = {}
    for m in d.get('mapping', []):
        paths_by_journey.setdefault(m['journey_id'], [])
        if m['test_path'] not in paths_by_journey[m['journey_id']]:
            paths_by_journey[m['journey_id']].append(m['test_path'])

    updates, changed = [], []
    for j in d.get('journeys', []):
        if not j.get('in_scope') or j['id'] not in paths_by_journey:
            continue
        p = repo / j['path']
        text = p.read_text(encoding='utf-8')
        meta = journey_md.load(p)['meta']
        before_auto = meta.get('automation', 'none')
        before_tests = list(meta.get('tests') or [])
        entries = [e for e in d.get('step_coverage', []) if e['journey_id'] == j['id']]
        auto = computed_automation(entries, passing)
        if RANK.get(before_auto, 0) > RANK[auto]:
            auto = before_auto
        tests = before_tests + [t for t in paths_by_journey[j['id']] if t not in before_tests]
        if auto == before_auto and tests == before_tests:
            print(f"{j['id']}: unchanged (automation {auto})"); continue
        new = set_front_matter(text, auto, tests)
        updates.append({'journey_id': j['id'], 'path': j['path'], 'automation': auto, 'tests': tests})
        changed.append(j['path'])
        print(f"{j['id']}: automation {before_auto} -> {auto}; tests {len(before_tests)} -> {len(tests)}"
              + (' (dry run)' if args.dry_run else ''))
        if not args.dry_run:
            p.write_text(new, encoding='utf-8', newline='')

    if not args.dry_run and updates:
        known = {u['journey_id']: u for u in d.get('journey_updates', [])}
        known.update({u['journey_id']: u for u in updates})
        d['journey_updates'] = list(known.values())
        listed = {f['path'] for f in d.get('files_changed', [])}
        for c in changed:
            if c not in listed:
                d.setdefault('files_changed', []).append({'path': c, 'change': 'modified', 'kind': 'journey'})
        rpath.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
