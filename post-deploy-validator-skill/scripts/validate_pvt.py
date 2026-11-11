#!/usr/bin/env python3
"""Validate a post-deploy report against its run (and the baseline run), and recompute the recommendation.

    python validate_pvt.py pvt_report.json --run runs/<stamp>/run.json [--baseline runs/<previous>/run.json] [--plan pvt_plan.json]

Checks: the report's checks, deployed version and performance equal the run; every request in the run
used GET or HEAD, or POST to a sign-in path of a credential set in the plan; every refused check is in
`blocked`; each timed check's median is the median of its samples and each comparison is recomputed from
the baseline run (same plan and target) with the tolerance and the minimum delta; the findings and the
recommendation equal what recommend.py derives; decision and decided_by are empty (a person decides);
a production target names authorised_by; no Authorization, Cookie, Set-Cookie, bearer token or the
plan's credential environment values appear in the run or the report; a value is recorded only for an
assertion marked record; no text calls the release healthy, safe or fine.
"""
from __future__ import annotations
import argparse, json, os, re, statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from recommend import analyse  # noqa: E402
from run_pvt import plan_sha  # noqa: E402

BANNED = re.compile(r'(?i)\b(healthy|safe to (release|ship|keep)|all (good|clear|green)|nothing (is )?wrong|release is (fine|good|ok)|production is (fine|good|ok)|fully (working|tested))\b')
TOKENISH = re.compile(r'(?i)(authorization|set-cookie|cookie)\s*["\']?\s*:\s*["\']?[^"\'\s,}]{6,}|bearer\s+[A-Za-z0-9._-]{12,}|\b[0-9a-f]{32}\b')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--plan', type=Path)
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    run = json.loads(a.run.read_text(encoding='utf-8'))
    plan = json.loads((a.plan or Path(run['plan']['path'])).read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    if plan_sha(plan) != run['plan']['sha256']:
        e('the plan changed since the run (SHA-256 differs)')
    if run['target'].get('environment') == 'production' and len((run['target'].get('authorised_by') or '').strip()) < 3:
        e('a production target without authorised_by')
    # read-only ----------------------------------------------------------------------------------------
    logins = {cs['login']['path'] for cs in plan.get('credential_sets', {}).values()}
    for r in run['requests']:
        if r['method'] not in ('GET', 'HEAD') and not (r['method'] == 'POST' and r['path'] in logins):
            e(f'the run sent {r["method"]} {r["path"]}: only GET, HEAD and the named sign-in are allowed')
    for c in plan['checks']:
        m = c.get('method', 'GET').upper()
        if m not in ('GET', 'HEAD') and not any(b['check_id'] == c['id'] for b in run['blocked']):
            e(f'check {c["id"]} ({m}) is not recorded as blocked')
    # secrets --------------------------------------------------------------------------------------------
    blob = a.run.read_text(encoding='utf-8') + a.report.read_text(encoding='utf-8')
    for cs in plan.get('credential_sets', {}).values():
        for k, var in cs['env'].items():
            v = os.environ.get(var)
            if v and k.lower() not in ('username', 'user', 'login', 'email') and v in blob:
                e(f'the value of {var} appears in the run or the report')
    m = TOKENISH.search(blob)
    if m:
        e(f'a token or cookie value appears in the run or the report: {m.group(0)[:24]}...')
    pmap = {c['id']: c for c in plan['checks']}
    for c in run['checks']:
        for x in c.get('assertions', []):
            if 'actual' in x and x['path'] not in ('status',) and not any(j.get('record') and j['path'] == x['path'] for j in pmap.get(c['id'], {}).get('expect', {}).get('json', [])):
                e(f'{c["id"]}: a value of {x["path"]} is recorded, but the plan does not mark it record')
    # timings and baseline ---------------------------------------------------------------------------------
    for c in run['checks']:
        t = c.get('timing')
        if t and round(statistics.median(t['samples_ms']), 1) != t['median_ms']:
            e(f'{c["id"]}: median {t["median_ms"]} is not the median of its samples')
    perf = run['performance']
    base = json.loads(a.baseline.read_text(encoding='utf-8')) if a.baseline else None
    if base and (base['plan']['sha256'] != run['plan']['sha256'] or base['target']['base_url'] != run['target']['base_url']):
        base = None
    bmap = {x['id']: x for x in (base or {}).get('checks', [])}
    tol, mind = perf['tolerance_pct'], perf['min_delta_ms']
    for p in perf['comparisons']:
        c = next((x for x in run['checks'] if x['id'] == p['check_id']), None)
        if not c or not c.get('timing') or c['timing']['median_ms'] != p['median_ms']:
            e(f'performance {p["check_id"]}: median differs from the check')
            continue
        b = bmap.get(p['check_id'], {}).get('timing') if a.baseline else ({'median_ms': p['baseline_median_ms']} if p['baseline_median_ms'] is not None else None)
        if a.baseline and not b:
            if p['status'] != 'no_baseline':
                e(f'performance {p["check_id"]}: no baseline timing, status should be no_baseline')
            continue
        if b:
            mm, bm = p['median_ms'], b['median_ms']
            st = 'regressed' if mm > bm * (1 + tol / 100) and mm - bm > mind else 'improved' if mm < bm * (1 - tol / 100) and bm - mm > mind else 'ok'
            if p['baseline_median_ms'] != bm or p['status'] != st:
                e(f'performance {p["check_id"]}: {p["status"]} against {p["baseline_median_ms"]} ms should be {st} against {bm} ms')
    # report equals run ------------------------------------------------------------------------------------
    if d['deployed'] != run['deployed']:
        e('deployed version differs from the run')
    want_checks = [{k: c.get(k) for k in ('id', 'kind', 'critical', 'method', 'path', 'status', 'http_status', 'reason')} for c in run['checks']]
    if d['checks'] != want_checks:
        e('checks differ from the run')
    if d['performance'] != run['performance']:
        e('performance differs from the run')
    findings, rec = analyse(run)
    key = lambda fs: sorted((f['kind'], f['severity'], tuple(f['check_ids'])) for f in fs)  # noqa: E731
    if key(d['findings']) != key(findings):
        e(f'findings {key(d["findings"])} should be {key(findings)}')
    r = d['recommendation']
    if r['value'] != rec['value']:
        e(f'recommendation {r["value"]} should be {rec["value"]} ({"; ".join(rec["reasons"])})')
    for reason in rec['reasons']:
        if reason not in r.get('reasons', []):
            e(f'recommendation reasons miss: {reason}')
    if r.get('decision') is not None or r.get('decided_by') is not None:
        e('decision and decided_by must be left for a person: the skill recommends, it does not decide')
    counts = {s: sum(1 for c in run['checks'] if c['status'] == s) for s in ('pass', 'fail', 'error', 'blocked', 'skipped')}
    if d['summary'].get('checks') != counts or d['summary'].get('findings') != len(d['findings']):
        e('summary counts differ from the run')
    for t in [d['summary'].get('headline', '')] + d.get('limitations', []) + [f.get('title', '') for f in d['findings']]:
        mm = BANNED.search(t or '')
        if mm:
            e(f'text says {mm.group(0)!r}: the strongest wording is "no failed checks and no regressions in what was checked"')
    if E:
        print(f'{a.report}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    print(f'{a.report}: valid. {r["value"]}; {len(d["findings"])} finding(s); checks ' + ', '.join(f'{k} {v}' for k, v in counts.items() if v)
          + f'; {len(run["requests"])} read-only request(s), {len(run["blocked"])} refused')
    return 0


if __name__ == '__main__':
    sys.exit(main())
