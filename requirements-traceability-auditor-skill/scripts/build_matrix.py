#!/usr/bin/env python3
"""Build the traceability matrix: requirement -> criteria -> tests -> latest results, with gaps and statuses.

    python build_matrix.py --requirements requirements.json --evidence evidence.json [--triage triage.json] --out matrix.json

Links per requirement and test, with every piece of evidence:
  explicit   the test names the key (name, docstring, comment, tag); a ticket "is tested by" link to a test
             key that the test-management export maps to this test; an export entry naming the key
  inferred   a commit naming the key added the test; a ticket comment names the test or its file
A test covers a criterion when its evidence names the criterion (<key> AC-n), or when the requirement has
a single criterion. triage.json lists inferred links the agent dropped (with reasons) and criteria it
judged covered (inferred). Statuses:
  failing             a kept linked test's latest result failed or errored
  verified            explicit links only count; every criterion covered by an explicit test that passed in
                      a fresh run, and no kept linked test not run or stale
  partially_verified  linked tests, but not verified
  untested            no kept linked test, but code or commits name the requirement
  not_traceable       nothing names the requirement
A result is fresh when its run is not older than the latest commit naming the key or touching its linked
tests.
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path


def ts(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def build(reqs: list[dict], ev: dict, triage: dict | None = None) -> dict:
    triage = triage or {}
    dropped = {(x['requirement'], x['test_id']) for x in triage.get('dropped_links', [])}
    judged = {(x['criterion'], x['test_id']) for x in triage.get('judged_coverage', [])}
    tests = {t['test_id']: t for t in ev['tests']}
    tm = {t['key']: t for t in ev['test_management'].get('tests', [])}
    out_reqs, linked_any = [], set()
    for r in reqs:
        key = r['id']
        links: dict = {}

        def add(tid, strength, e):
            if tid not in tests and tid not in ev['results']:
                return
            L = links.setdefault(tid, {'test_id': tid, 'strength': 'inferred', 'evidence': [], 'criteria': set()})
            L['evidence'].append(e)
            if strength == 'explicit':
                L['strength'] = 'explicit'
        for t in ev['tests']:
            for ref in t['refs']:
                if ref['key'] == key:
                    add(t['test_id'], 'explicit', {'kind': 'test_code', 'ref': f'{ref["file"]}:{ref["line"]}', 'detail': ref['text']})
                    if ref['criterion']:
                        links[t['test_id']]['criteria'].add(ref['criterion'])
        tk = next((x for x in ev['tickets'] if x['id'] == key), {'links': [], 'comments': []})
        for l in tk['links']:
            entry = tm.get(l['key'])
            if entry and entry.get('test_id') and key in entry.get('requirements', []):
                add(entry['test_id'], 'explicit', {'kind': 'ticket_link', 'ref': f'{key} {l["type"]} {l["key"]}', 'detail': l.get('summary') or ''})
                add(entry['test_id'], 'explicit', {'kind': 'test_management', 'ref': f'{ev["test_management"]["path"]} {l["key"]}', 'detail': f'{l["key"]} -> {entry["test_id"]}'})
        for entry in tm.values():
            if key in entry.get('requirements', []) and entry.get('test_id') and not any(e['kind'] == 'test_management' for e in links.get(entry['test_id'], {}).get('evidence', [])):
                add(entry['test_id'], 'explicit', {'kind': 'test_management', 'ref': f'{ev["test_management"]["path"]} {entry["key"]}', 'detail': f'{entry["key"]} -> {entry["test_id"]}'})
        for c in ev['commits']:
            if key in c['keys']:
                for tid in c['tests_added']:
                    add(tid, 'inferred', {'kind': 'commit', 'ref': c['commit'], 'detail': f'"{c["subject"]}" added this test'})
        for cm in tk['comments']:
            for m in cm['mentions']:
                for t in ev['tests']:
                    if t['test_id'] == m or t['file'] == m:
                        add(t['test_id'], 'inferred', {'kind': 'ticket_comment', 'ref': f'{key} comment {cm["index"] + 1}', 'detail': f'names {m}'})
        kept = {tid: L for tid, L in links.items() if not (L['strength'] == 'inferred' and (key, tid) in dropped)}
        linked_any |= set(kept)
        crit_single = len(r['criteria']) == 1
        last_change = max([ts(c['at']) for c in ev['commits'] if key in c['keys'] or set(c['files']) & {tests[t]['file'] for t in kept if t in tests}] or [None],
                          key=lambda x: x or datetime.min.replace(tzinfo=timezone.utc))
        tl = []
        for tid, L in sorted(kept.items()):
            res = ev['results'].get(tid)
            at = ts(res['at']) if res else None
            fresh = bool(res) and (last_change is None or (at is not None and at >= last_change))
            crits = sorted(L['criteria'] | ({r['criteria'][0]['id']} if crit_single and L['strength'] == 'explicit' else set()))
            tl.append({'test_id': tid, 'strength': L['strength'], 'evidence': L['evidence'], 'criteria': crits,
                       'judged_criteria': sorted(c for c, t in judged if t == tid and c.startswith(key + '/')),
                       'result': res['status'] if res else 'not_run', 'result_at': res['at'] if res else None, 'fresh': fresh})
        crits = []
        for c in r['criteria']:
            exp = [t['test_id'] for t in tl if c['id'] in t['criteria'] and t['strength'] == 'explicit']
            inf = [t['test_id'] for t in tl if c['id'] in t['judged_criteria'] or (c['id'] in t['criteria'] and t['strength'] == 'inferred')]
            crits.append({'id': c['id'], 'text': c['text'], 'tests_explicit': exp, 'tests_inferred': sorted(set(inf) - set(exp))})
        impl = [{'kind': 'code', 'ref': f'{x["file"]}:{x["line"]}', 'detail': x['text']} for x in ev['code'] if x['key'] == key] + \
               [{'kind': 'commit', 'ref': c['commit'], 'detail': c['subject']} for c in ev['commits'] if key in c['keys']]
        gaps = []
        for c in crits:
            if not c['tests_explicit']:
                gaps.append(f'{c["id"]} has no explicit test' + (f' (inferred: {", ".join(c["tests_inferred"])})' if c['tests_inferred'] else ''))
        for t in tl:
            if t['result'] == 'not_run':
                gaps.append(f'{t["test_id"]} has no result')
            elif t['result'] in ('failed', 'error'):
                gaps.append(f'{t["test_id"]} {t["result"]}')
            elif not t['fresh']:
                gaps.append(f'{t["test_id"]} result is older than the latest change')
        if tl and not impl:
            gaps.append('no implementation evidence (no code or commit names the requirement)')
        good = lambda t: t['strength'] == 'explicit' and t['result'] == 'passed' and t['fresh']  # noqa: E731
        if any(t['result'] in ('failed', 'error') for t in tl):
            status = 'failing'
        elif not tl:
            status = 'untested' if impl else 'not_traceable'
        elif all(any(good(t) for t in tl if t['test_id'] in c['tests_explicit']) for c in crits) and all(t['result'] == 'passed' and t['fresh'] for t in tl):
            status = 'verified'
        else:
            status = 'partially_verified'
        out_reqs.append({'id': key, 'source': r['source'], 'title': r['title'], 'priority': r.get('priority'), 'status': status,
                         'criteria': crits, 'tests': tl, 'implementation': impl, 'attachments': r.get('attachments', []), 'gaps': gaps})
    orphans = []
    for t in ev['tests']:
        if t['test_id'] not in linked_any:
            orphans.append({'test_id': t['test_id'], 'file': t['file'], 'line': t['start'], 'result': (ev['results'].get(t['test_id']) or {}).get('status', 'not_run'),
                            'names_out_of_scope': t.get('other_keys', [])})
    by = {s: sum(1 for r in out_reqs if r['status'] == s) for s in ('verified', 'partially_verified', 'failing', 'untested', 'not_traceable')}
    summary = {'by_status': by, 'requirements': len(out_reqs), 'criteria_total': sum(len(r['criteria']) for r in out_reqs),
               'criteria_with_explicit_tests': sum(1 for r in out_reqs for c in r['criteria'] if c['tests_explicit']),
               'tests_linked': len(linked_any), 'orphans': len(orphans)}
    return {'requirements': out_reqs, 'orphan_tests': orphans, 'summary': summary}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--requirements', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    ap.add_argument('--triage', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    reqs = json.loads(a.requirements.read_text(encoding='utf-8'))['requirements']
    ev = json.loads(a.evidence.read_text(encoding='utf-8'))
    tri = json.loads(a.triage.read_text(encoding='utf-8')) if a.triage else None
    m = build(reqs, ev, tri)
    a.out.write_text(json.dumps(m, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{m["summary"]["requirements"]} requirement(s): ' + ', '.join(f'{k} {v}' for k, v in m['summary']['by_status'].items() if v) + f'; {m["summary"]["orphans"]} orphan test(s)')
    for r in m['requirements']:
        print(f'  {r["id"]:10} {r["status"]:18} ' + '; '.join(r['gaps'][:3]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
