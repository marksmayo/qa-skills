#!/usr/bin/env python3
"""Validate a post-deploy report against its run (and the baseline run), and recompute the recommendation.

    python validate_pvt.py pvt_report.json --run runs/<stamp>/run.json [--baseline runs/<previous>/run.json] [--plan pvt_plan.json]

Checks: the plan matches its schema and the run's SHA-256; the report's checks, deployed version, stop and
performance equal the run; every request in the run used GET or HEAD, or POST to a sign-in path of a
credential set in the plan; every refused check is in `blocked`; each timed check's median is the median of
its samples, a failing response was timed once and marked not comparable, and each comparison is
recomputed from the baseline run (same plan and target) with the tolerance and the minimum delta; the
stop rule is replayed (three 5xx in a row, a 4xx doesn't reset the count; a 429 or WAF refusal stops at
once) and nothing was skipped without it; the findings (kind, severity, checks, evidence), the notes and the
recommendation equal what recommend.py derives; decision and decided_by are empty (a person decides); a
production target names authorised_by; no Authorization, Cookie, Set-Cookie, bearer token or the plan's
credential environment values appear in the run or the report; a value is recorded only for a JSON
assertion marked record. Wording: no text calls the release healthy, safe or fine; the headline names the
recommendation and says nothing that contradicts it ("ship it" over a no_go); a finding title doesn't
call a critical or major finding minor or cosmetic (or a minor one critical), and a failed check's title
keeps its HTTP status when that wasn't 2xx.
"""
from __future__ import annotations
import argparse, json, os, re, statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from recommend import analyse, REPORT_CHECK_FIELDS  # noqa: E402
from run_pvt import plan_sha, plan_errors, expected_status_ok, compare  # noqa: E402
import schema_check  # noqa: E402

BANNED = re.compile(r'(?i)\b(healthy|safe to (release|ship|keep|go)|all (good|clear|green)|nothing (else )?(is )?wrong|'
                    r'(release|production|deployment|deploy) (is|looks) (fine|good|ok|great)|fully (working|tested)|good to go|ready to (ship|release|go)|'
                    r'in (good|great) shape|no (problems|issues) (at all|found)|(?<![\w-])ship it)\b')
CONTRADICT = {  # wording that reads as a different recommendation than the computed one
    'no_go': re.compile(r'(?i)(?<!no[-_ ])(?<!not )\bgo[-_ ]?ish\b|\bkeep (it|the release)\b|\bproceed\b|\blooks good\b|\bcan stay\b|\bfine to\b'),
    'not_established': re.compile(r'(?i)(?<!no[-_ ])(?<!not )\bgo[-_ ]?ish\b|\bkeep (it|the release)\b|\bproceed\b|\blooks good\b|\bcan stay\b|\bfine to\b|\brecommend (a )?go\b'),
    'go': re.compile(r'(?i)\bno[-_ ]go\b|\broll ?back\b|\brevert\b'),
}
NAMES = {'no_go': re.compile(r'(?i)\bno[-_ ]go\b'), 'not_established': re.compile(r'(?i)\bnot[-_ ]established\b'),
         'go': re.compile(r'(?i)(?<!no[-_ ])(?<!no)\bgo\b')}
SOFT = re.compile(r'(?i)\b(minor|cosmetic|trivial|negligible|harmless|nit)\b')
HARD = re.compile(r'(?i)\b(critical|blocker|severe|outage)\b')
TOKENISH = re.compile(r'(?i)(authorization|set-cookie|cookie)\s*["\']?\s*:\s*["\']?[^"\'\s,}]{6,}|bearer\s+[A-Za-z0-9._-]{12,}|\b[0-9a-f]{32}\b')


def replay_stop(checks: list[dict]) -> tuple[int | None, str | None]:
    """Index of the check after which the run had to stop, and the cause."""
    five = 0
    for i, c in enumerate(checks):
        if c['status'] == 'error' and c.get('http_status') in (403, 429, 503):
            return i, 'refusal'
        code = c.get('http_status')
        if c['status'] not in ('pass', 'fail') or code is None:
            continue
        five = five + 1 if code >= 500 else 0 if code < 400 else five
        if five >= 3:
            return i, 'app_5xx'
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--plan', type=Path)
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads(a.report.read_text(encoding='utf-8'))
    run = json.loads(a.run.read_text(encoding='utf-8'))
    plan = json.loads((a.plan or Path(run['plan']['path'])).read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    for x in plan_errors(plan):
        e(f'plan: {x}')
    for x in schema_check.errors(d, json.loads((HERE.parent / 'schemas' / 'pvt_report.schema.json').read_text(encoding='utf-8'))):
        e(f'report schema: {x}')
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
            if 'kind' not in x:
                e(f'{c["id"]}: an assertion has no kind (http_status, body, json, header or redirect)')
            elif 'actual' in x and x['kind'] == 'json' and not any(j.get('record') and j['path'] == x['path'] for j in pmap.get(c['id'], {}).get('expect', {}).get('json', [])):
                e(f'{c["id"]}: a value of {x["path"]} is recorded, but the plan does not mark it record')
            elif 'actual' in x and x['kind'] in ('body', 'header'):
                e(f'{c["id"]}: a {x["kind"]} value is recorded; only whether it held may be')
    # stop rule ---------------------------------------------------------------------------------------------
    idx, cause = replay_stop(run['checks'])
    st = run.get('stopped')
    if idx is None:
        if st:
            e(f'the run says it stopped ({st["cause"]}) but no check answered a refusal or three 5xx in a row')
    else:
        stop_id = run['checks'][idx]['id']
        if not st or st['after_check'] != stop_id or (cause == 'app_5xx') != (st['cause'] == 'app_5xx'):
            e(f'the run had to stop after {stop_id} ({cause}); it records {st}')
    for i, c in enumerate(run['checks']):
        after = idx is not None and i > idx
        if after and c['status'] != 'skipped':
            e(f'{c["id"]} was sent after the run had to stop')
        if not after and c['status'] == 'skipped':
            e(f'{c["id"]} is skipped, but the stop rule did not apply before it')
    # timings and baseline ---------------------------------------------------------------------------------
    for c in run['checks']:
        t = c.get('timing')
        if not t:
            continue
        if round(statistics.median(t['samples_ms']), 1) != t['median_ms']:
            e(f'{c["id"]}: median {t["median_ms"]} is not the median of its samples')
        ok = expected_status_ok(c.get('http_status'), pmap.get(c['id'], {}).get('expect', {})) and not any(not h.get('followed', True) for h in c.get('redirects', []))
        if not ok and (t.get('comparable') is not False or len(t['samples_ms']) != 1):
            e(f'{c["id"]}: answered {c.get("http_status")}, so it must be timed once and marked not comparable')
    perf = run['performance']
    base = json.loads(a.baseline.read_text(encoding='utf-8')) if a.baseline else None
    if base and (base['plan']['sha256'] != run['plan']['sha256'] or base['target']['base_url'] != run['target']['base_url']):
        base = None
    if a.baseline and base:
        bd, rd = base.get('deployed', {}).get('deployment_id'), run['deployed'].get('deployment_id')
        want_rel = ('same_deployment' if bd == rd else 'different_deployment') if bd is not None and rd is not None else None
        if want_rel and perf.get('baseline_relation') != want_rel:
            e(f'baseline_relation is {perf.get("baseline_relation")}; the deployment ids say {want_rel}')
    bmap = {x['id']: x for x in (base or {}).get('checks', [])}
    tol, mind = perf['tolerance_pct'], perf['min_delta_ms']
    for p in perf['comparisons']:
        c = next((x for x in run['checks'] if x['id'] == p['check_id']), None)
        if not c or not c.get('timing') or c['timing']['median_ms'] != p['median_ms']:
            e(f'performance {p["check_id"]}: median differs from the check')
            continue
        bt = bmap.get(p['check_id'], {}).get('timing') if a.baseline else ({'median_ms': p['baseline_median_ms']} if p['baseline_median_ms'] is not None else None)
        if not c['timing'].get('comparable', True) or (bt and bt.get('comparable') is False):
            if p['status'] != 'not_comparable':
                e(f'performance {p["check_id"]}: a failing response was timed, so the status must be not_comparable, not {p["status"]}')
            continue
        if p['status'] == 'not_comparable':
            e(f'performance {p["check_id"]}: both timings are of expected responses, so it must be compared')
            continue
        if a.baseline and not bt:
            if p['status'] != 'no_baseline':
                e(f'performance {p["check_id"]}: no baseline timing, status should be no_baseline')
            continue
        if bt:
            st2 = compare(p['median_ms'], bt['median_ms'], tol, mind)
            if p['baseline_median_ms'] != bt['median_ms'] or p['status'] != st2:
                e(f'performance {p["check_id"]}: {p["status"]} against {p["baseline_median_ms"]} ms should be {st2} against {bt["median_ms"]} ms')
    # report equals run ------------------------------------------------------------------------------------
    if d['deployed'] != run['deployed']:
        e('deployed version differs from the run')
    want_checks = [{k: c.get(k) for k in REPORT_CHECK_FIELDS} for c in run['checks']]
    if d['checks'] != want_checks:
        e('checks differ from the run')
    if d['performance'] != run['performance']:
        e('performance differs from the run')
    if d['window'].get('stopped') != run.get('stopped'):
        e('window.stopped differs from the run')
    findings, rec = analyse(run)
    key = lambda fs: sorted((f['kind'], f['severity'], tuple(f['check_ids']), f.get('evidence')) for f in fs)  # noqa: E731
    if key(d['findings']) != key(findings):
        e(f'findings {key(d["findings"])} should be {key(findings)}')
    r = d['recommendation']
    if r['value'] != rec['value']:
        e(f'recommendation {r["value"]} should be {rec["value"]} ({"; ".join(rec["reasons"])})')
    for reason in rec['reasons']:
        if reason not in r.get('reasons', []):
            e(f'recommendation reasons miss: {reason}')
    for note in rec['notes']:
        if note not in r.get('notes', []):
            e(f'recommendation notes miss: {note}')
    if r.get('decision') is not None or r.get('decided_by') is not None:
        e('decision and decided_by must be left for a person: the skill recommends, it does not decide')
    counts = {s: sum(1 for c in run['checks'] if c['status'] == s) for s in ('pass', 'fail', 'error', 'blocked', 'skipped')}
    if d['summary'].get('checks') != counts or d['summary'].get('findings') != len(d['findings']):
        e('summary counts differ from the run')
    # wording ------------------------------------------------------------------------------------------------
    headline = d['summary'].get('headline', '') or ''
    for t in [headline] + d.get('limitations', []) + [f.get('title', '') for f in d['findings']] + list(r.get('notes', [])):
        mm = BANNED.search(t or '')
        if mm:
            e(f'text says {mm.group(0)!r}: the strongest wording is "no failed checks and no regressions in what was checked"')
    if not NAMES[rec['value']].search(headline):
        e(f'the headline does not name the recommendation ({rec["value"].replace("_", "-")})')
    mm = CONTRADICT[rec['value']].search(headline)
    if mm:
        e(f'the headline says {mm.group(0)!r}, which contradicts the recommendation {rec["value"]}')
    cmap = {c['id']: c for c in run['checks']}
    for f in d['findings']:
        t = f.get('title', '')
        if f['severity'] in ('critical', 'major') and SOFT.search(t):
            e(f'{f.get("id")}: a {f["severity"]} finding is titled {SOFT.search(t).group(0)!r}')
        if f['severity'] == 'minor' and HARD.search(t):
            e(f'{f.get("id")}: a minor finding is titled {HARD.search(t).group(0)!r}')
        if f['kind'] == 'check_failed':
            code = cmap.get(f['check_ids'][0], {}).get('http_status')
            if code and not 200 <= code < 300 and str(code) not in t:
                e(f'{f.get("id")}: the title drops the HTTP status {code} the check answered')
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
