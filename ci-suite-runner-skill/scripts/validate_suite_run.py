#!/usr/bin/env python3
"""Validate a suite run record against the catalogue, the pre-authorisation, the JUnit and (optionally) the CI's log.

    python validate_suite_run.py run/suite_run.json [--preauth preauth.json] [--stub-log stub_log.json]

Checks: the suite, environment, tags and inputs are the catalogue's; a sent trigger has exactly one
authorisation - a grant that covers the suite and environment and hadn't expired on the trigger date, or a
confirmation naming who and how - and a protected environment has a confirmation; a refused trigger sent
nothing; with --stub-log, the CI received exactly as many triggers for this pipeline as were sent; the
outcome follows the polling (completed only after a completed poll); the totals and failed tests equal the
JUnit files; results are claimed only from JUnit; failed tests are handed to the CI failure triager;
tokens in the environment and secret inputs never appear; no text says all green or everything passed.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402
from run_suite import tags_ok  # noqa: E402

BANNED = re.compile(r'(?i)\b(all green|everything passed|all tests pass(ed)?|fully passing|no problems|build is (fine|healthy|green))\b')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('record', type=Path)
    ap.add_argument('--preauth', type=Path)
    ap.add_argument('--stub-log', type=Path)
    a = ap.parse_args()
    d = json.loads(a.record.read_text(encoding='utf-8'))
    run_dir = a.record.parent
    E: list[str] = []
    e = E.append
    cat = json.loads(Path(d['catalogue']).read_text(encoding='utf-8'))
    r = d['request']
    s = cat['suites'].get(r['suite'])
    if not s:
        print(f'{a.record}: suite {r["suite"]} is not in the catalogue'); return 1
    if r['environment'] not in s['environments']:
        e(f'environment {r["environment"]} is not allowed for {r["suite"]}')
    if r['protected'] != (r['environment'] in s.get('protected', [])):
        e('the protected flag differs from the catalogue')
    bad = tags_ok(r['tags'], s.get('markers', []))
    if bad:
        e(f'tags: {bad}')
    body = d['plan']['body']
    inputs = body.get('inputs') if r['system'] == 'github' else body.get('templateParameters')
    if set(inputs) != set(s['inputs'].values()) or inputs.get(s['inputs']['environment']) != r['environment'] or inputs.get(s['inputs']['tags']) != r['tags']:
        e('the plan inputs differ from the request and the catalogue')
    want_path = f'/repos/{s["repo"]}/actions/workflows/{s["workflow"]}/dispatches' if r['system'] == 'github' else f'/{s["org"]}/{s["project"]}/_apis/pipelines/{s["pipeline_id"]}/runs'
    if want_path not in d['plan']['url']:
        e(f'the plan URL is not the catalogue\'s pipeline ({want_path})')
    # authority ---------------------------------------------------------------------------------------
    sent = [x for x in d['authorisations'] if x['decision'] == 'sent']
    trig = d.get('trigger') or {}
    if trig.get('sent'):
        if len(sent) != 1:
            e(f'{len(sent)} sent authorisations: a request is triggered exactly once')
        au = sent[-1] if sent else {}
        on = date.fromisoformat(trig['at'][:10])
        if au.get('kind') == 'preauth':
            if r['protected']:
                e(f'{r["environment"]} is protected: a pre-authorisation cannot cover it')
            pa = json.loads((a.preauth or Path(au.get('preauth_file', ''))).read_text(encoding='utf-8')) if (a.preauth or au.get('preauth_file')) else {'grants': []}
            g = next((g for g in pa['grants'] if g['id'] == au.get('grant_id')), None)
            if not g:
                e(f'grant {au.get("grant_id")} is not in the pre-authorisation file')
            else:
                if r['suite'] not in g['suites'] or r['environment'] not in g['environments']:
                    e(f'grant {g["id"]} does not cover {r["suite"]} on {r["environment"]}')
                if date.fromisoformat(g['expires']) < on:
                    e(f'grant {g["id"]} expired on {g["expires"]}, before the trigger on {on}')
                if g['granted_by'] != au.get('by'):
                    e('the recorded grantor differs from the grant')
        elif au.get('kind') == 'confirmation':
            if len((au.get('by') or '').strip()) < 10:
                e('a confirmation must say who confirmed and how')
        else:
            e('a trigger was sent without a pre-authorisation or a confirmation')
        if trig.get('body') != d['plan']['body'] or trig.get('url') != d['plan']['url']:
            e('the request sent differs from the plan')
    else:
        if sent:
            e('an authorisation says sent, but no trigger was sent')
        if d.get('run'):
            e('a run is recorded although nothing was triggered')
    if a.stub_log:
        log = json.loads(a.stub_log.read_text(encoding='utf-8'))
        n = sum(1 for x in log if x['method'] == 'POST' and x['path'].endswith(want_path))
        if n != (1 if trig.get('sent') else 0):
            e(f'the CI received {n} trigger(s) for this pipeline; the record says {1 if trig.get("sent") else 0}')
    # outcome and results -----------------------------------------------------------------------------
    pol = d['polling']
    if [p['at'] for p in pol] != sorted(p['at'] for p in pol):
        e('polling timestamps go backwards')
    o = d['outcome']
    if o['status'] == 'completed':
        if not pol or pol[-1]['status'] != 'completed' or pol[-1].get('conclusion') != o['conclusion']:
            e('completed without a completed poll, or with another conclusion')
    res = d.get('results')
    if res and res['status'] == 'collected':
        cases = []
        for j in res['junit']:
            cases += parse_junit(run_dir / j, 'R')[0]
        tot = {'tests': len(cases), 'passed': sum(c['status'] == 'passed' for c in cases), 'failed': sum(c['status'] == 'failed' for c in cases),
               'errors': sum(c['status'] == 'error' for c in cases), 'skipped': sum(c['status'] == 'skipped' for c in cases)}
        if res['totals'] != tot:
            e(f'totals {res["totals"]} should be {tot} (from the JUnit)')
        if sorted(f['id'] for f in res['failed_tests']) != sorted(c['id'] for c in cases if c['status'] in ('failed', 'error')):
            e('failed tests differ from the JUnit')
        if (tot['failed'] + tot['errors'] > 0) != (o.get('conclusion') == 'failure'):
            e('the conclusion and the JUnit disagree (a failing test in a successful run, or the reverse)')
        if tot['failed'] + tot['errors'] and not any('ci-failure-triager' in x for x in d.get('next_steps', [])):
            e('failed tests must be handed to the CI failure triager in next_steps')
    elif res and res['status'] == 'not_collected' and res.get('totals'):
        e('totals without JUnit')
    # secrets and language ------------------------------------------------------------------------------
    text = a.record.read_text(encoding='utf-8')
    for var in ('GITHUB_TOKEN', 'GH_TOKEN', 'AZURE_DEVOPS_EXT_PAT'):
        v = os.environ.get(var)
        if v and len(v) >= 6 and v in text:
            e(f'the value of {var} appears in the record')
    for k in s.get('secret_inputs', []):
        if inputs.get(k) not in (None, '[redacted]') or r['inputs'].get(k) not in (None, '[redacted]'):
            e(f'secret input {k} is not redacted')
    for t in [d.get('summary', {}).get('headline', '')] + d.get('next_steps', []) + d.get('limitations', []):
        m = BANNED.search(t or '')
        if m:
            e(f'text says {m.group(0)!r}: give the totals instead')
    if E:
        print(f'{a.record}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    t = (res or {}).get('totals')
    print(f'{a.record}: valid. {r["suite"]} on {r["environment"]}: ' + (f'triggered ({sent[-1]["kind"]}), {o["status"]} {o.get("conclusion") or ""}'.strip() if trig.get('sent') else f'not triggered ({trig.get("refused_reason")})')
          + (f'; {t["passed"]} of {t["tests"]} passed' if t else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
