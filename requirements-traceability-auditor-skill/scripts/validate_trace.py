#!/usr/bin/env python3
"""Validate a traceability report against its requirements, evidence, results and (optionally) the repository.

    python validate_trace.py trace.json --requirements requirements.json --evidence evidence.json [--junit results.xml] [--repo <checkout>]

Checks: every requirement appears once and the matrix (links, strengths, criteria, results, freshness,
statuses, gaps, orphans, summary) equals what build_matrix.py derives from the evidence and the triage; a
dropped link is inferred (an explicit link can't be dropped) and has a reason; a judged criterion names a
test linked to its requirement; results equal the JUnit; with --repo, every test_code and code reference's
line contains the key as a whole key (never inside a longer key), every commit reference exists, names the
key and added the test; ticket links and test-management entries exist in the inputs; no text claims full
coverage.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_matrix import build  # noqa: E402
from collect_evidence import key_re  # noqa: E402
from parse_results import parse_junit  # noqa: E402

BANNED = re.compile(r'(?i)\b(fully (traced|covered|tested|verified)|100\s?% (covered|coverage|traced)|complete coverage|everything is (tested|covered)|all requirements are (tested|covered|verified))\b')


def git(repo: Path, *args) -> str:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    return r.stdout if r.returncode == 0 else ''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('trace', type=Path)
    ap.add_argument('--requirements', type=Path, required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    ap.add_argument('--junit', type=Path, action='append', default=[])
    ap.add_argument('--repo', type=Path)
    a = ap.parse_args()
    d = json.loads(a.trace.read_text(encoding='utf-8'))
    reqs = json.loads(a.requirements.read_text(encoding='utf-8'))['requirements']
    ev = json.loads(a.evidence.read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    for k in ('schema_version', 'skill', 'sources', 'triage', 'requirements', 'orphan_tests', 'summary', 'limitations'):
        if k not in d:
            e(f'missing {k}')
    if E:
        print('\n'.join(E)); return 1
    tri = d['triage']
    # triage rules ------------------------------------------------------------------------------------
    raw = build(reqs, ev, None)
    raw_links = {(r['id'], t['test_id']): t for r in raw['requirements'] for t in r['tests']}
    for x in tri.get('dropped_links', []):
        L = raw_links.get((x['requirement'], x['test_id']))
        if not L:
            e(f'dropped link {x["requirement"]} -> {x["test_id"]} does not exist')
        elif L['strength'] == 'explicit':
            e(f'{x["requirement"]} -> {x["test_id"]} is explicit and cannot be dropped')
        if len(x.get('reason', '')) < 20:
            e(f'dropped link {x["requirement"]} -> {x["test_id"]} needs a specific reason')
    for x in tri.get('judged_coverage', []):
        key = x['criterion'].split('/')[0]
        if (key, x['test_id']) not in raw_links:
            e(f'judged coverage {x["criterion"]} by {x["test_id"]}: that test is not linked to {key}')
        if len(x.get('reason', '')) < 20:
            e(f'judged coverage {x["criterion"]} needs a reason')
    # matrix equals the derivation ---------------------------------------------------------------------
    want = build(reqs, ev, tri)
    got_ids = [r['id'] for r in d['requirements']]
    if sorted(got_ids) != sorted(r['id'] for r in reqs) or len(got_ids) != len(set(got_ids)):
        e('requirements must each appear once')
    wmap = {r['id']: r for r in want['requirements']}
    for r in d['requirements']:
        w = wmap.get(r['id'])
        if not w:
            continue
        for k in ('status', 'criteria', 'tests', 'implementation', 'gaps'):
            if r.get(k) != w[k]:
                e(f'{r["id"]}: {k} differs from the evidence ({"status " + str(w["status"]) if k == "status" else "recompute with build_matrix.py"})')
    if d['orphan_tests'] != want['orphan_tests']:
        e('orphan tests differ from the evidence')
    for k, v in want['summary'].items():
        if d['summary'].get(k) != v:
            e(f'summary.{k} {d["summary"].get(k)!r} should be {v!r}')
    # results equal the JUnit --------------------------------------------------------------------------
    if a.junit:
        res = {}
        for j in a.junit:
            for c in parse_junit(j, j.stem)[0]:
                res[c['id']] = c['status']
        for tid, r in ev['results'].items():
            if res.get(tid) != r['status']:
                e(f'result of {tid} differs from the JUnit ({res.get(tid)})')
    # evidence holds in the repository -------------------------------------------------------------------
    if a.repo:
        kre = key_re([r['id'] for r in reqs])
        for r in d['requirements']:
            refs = [x for t in r['tests'] for x in t['evidence']] + r['implementation']
            for x in refs:
                if x['kind'] in ('test_code', 'code'):
                    f, n = x['ref'].rsplit(':', 1)
                    p = a.repo / f
                    lines = p.read_text(encoding='utf-8').splitlines() if p.exists() else []
                    if not (0 < int(n) <= len(lines)) or r['id'] not in {m.group(1) for m in kre.finditer(lines[int(n) - 1])}:
                        e(f'{r["id"]}: {x["ref"]} does not name {r["id"]} as a whole key')
                elif x['kind'] == 'commit':
                    subj = git(a.repo, 'show', '-s', '--format=%s', x['ref']).strip()
                    if not subj or r['id'] not in {m.group(1) for m in kre.finditer(subj)}:
                        e(f'{r["id"]}: commit {x["ref"][:8]} does not exist or does not name {r["id"]}')
        for r in d['requirements']:
            for t in r['tests']:
                for x in t['evidence']:
                    if x['kind'] == 'commit':
                        c = next((c for c in ev['commits'] if c['commit'] == x['ref']), None)
                        if not c or t['test_id'] not in c['tests_added']:
                            e(f'{r["id"]}: commit {x["ref"][:8]} did not add {t["test_id"]}')
    rmap = {r['id']: r for r in reqs}
    tm_keys = {t['key'] for t in ev['test_management'].get('tests', [])}
    for r in d['requirements']:
        for t in r['tests']:
            for x in t['evidence']:
                if x['kind'] == 'ticket_link':
                    lk = x['ref'].split()[-1]
                    if not any(l['key'] == lk for l in rmap[r['id']].get('links', [])):
                        e(f'{r["id"]}: ticket link to {lk} is not in the ticket')
                if x['kind'] == 'test_management' and x['ref'].split()[-1] not in tm_keys:
                    e(f'{r["id"]}: test-management entry {x["ref"].split()[-1]} is not in the export')
    # language ----------------------------------------------------------------------------------------
    for t in [d['summary'].get('headline', '')] + d['limitations'] + [x.get('reason', '') for x in tri.get('dropped_links', [])]:
        m = BANNED.search(t or '')
        if m:
            e(f'text says {m.group(0)!r}: say which requirements have passing explicit tests in the runs provided')
    if E:
        print(f'{a.trace}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    s = d['summary']
    print(f'{a.trace}: valid. {s["requirements"]} requirement(s): ' + ', '.join(f'{k} {v}' for k, v in s['by_status'].items() if v)
          + f'; {s["criteria_with_explicit_tests"]}/{s["criteria_total"]} criteria with explicit tests; {s["orphans"]} orphan test(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
