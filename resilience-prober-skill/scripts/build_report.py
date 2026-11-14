#!/usr/bin/env python3
"""Assemble the report skeleton from the run, the plan and the candidates, and write a guarded repro script per finding (stdlib).

    python build_report.py --run run/run.json --scenarios run/scenarios.json --candidates run/candidates.json --flows flows.json \
        --promote K1,K3 [--repros run/repros] --out run/resilience_report.json

Everything the run decides is filled in and must not be edited: scope (the faults that actually ran, and
the planned faults that didn't), metadata (browser, requests sent, request budget, whether writes were
allowed, blocked requests), the baseline section, scenarios, coverage, `unreachable`, notes, each promoted
finding's ratings and evidence, and the headline counts and confidence. Limitations the run proves (a
failed baseline, an untriggered fault, single runs, the request budget, unreachable data, faults not run)
are written too.

What needs judgement is left as text starting with "TODO": each finding's title, actual behaviour, user
impact, fix and verification step; each triage reason; the headline summary. Candidates not in --promote
are triaged `needs_review` and listed in `needs_review`, until the agent promotes or dismisses them with a
checkable reason. validate_resilience.py rejects any remaining TODO.

--repros writes `F<n>.py` per finding: a standalone Playwright script that replays the flow to the step
and injects the fault, with a guard that blocks every other host and every write the scenario doesn't
target (unless flows.json allows form submission), so a repro run against a shared environment sends no
analytics, telemetry or writes.
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from classify import derive_baselines  # noqa: E402

SKILL_VERSION = '1.2.0'
TODO = 'TODO: '


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


LONE = re.compile('[\ud800-\udfff]')


def scrub(x):
    """Replace lone UTF-16 surrogates (text a page cut in the middle of an emoji) with U+FFFD, so the report can be written as UTF-8."""
    if isinstance(x, str):
        return LONE.sub('�', x)
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


def coverage(plan: dict, statuses: dict) -> dict:
    cov: dict[str, dict] = {}
    for s in plan['scenarios']:
        c = cov.setdefault(s['group'], {'ran': 0, 'untested': 0, 'errored': 0, 'not_triggered': 0})
        c[statuses[s['id']]] += 1
    return cov


def faults_split(plan: dict, statuses: dict) -> tuple[list[str], list[str]]:
    ran = sorted({s['fault'] for s in plan['scenarios'] if statuses[s['id']] == 'ran'})
    return ran, sorted({s['fault'] for s in plan['scenarios']} - set(ran))


def confidence(run: dict, plan: dict, statuses: dict, findings: list[dict], needs_review: list) -> str:
    planned = len(plan['scenarios'])
    ran = sum(1 for v in statuses.values() if v == 'ran')
    crit_broken = any(b['status'] != 'passed' and b.get('critical_path') for b in run['baselines'].values())
    if ran * 2 < planned or crit_broken or run.get('runs_per_scenario', 2) < 2:
        return 'low'
    if (ran < planned or any(b['status'] != 'passed' for b in run['baselines'].values()) or
            any(f['confidence'] != 'reproduced' for f in findings) or needs_review):
        return 'medium'
    return 'high'


REPRO = '''# Repro for {fid} (resilience-prober): {effect} in {flow} / {step}, fault {fault} on {endpoint}.
# Needs: pip install playwright; python -m playwright install chromium; and the app running at SITE.
# The fault is injected in the browser; the server is never sent anything broken. The guard aborts every
# request to a host other than the site's and every write the scenario doesn't target{writes_note}, so running
# this against a shared environment sends no analytics, telemetry or stray writes.
import json, re, time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

SITE = {site!r}
HOSTS = {hosts!r}
ALLOW_WRITES = {allow_writes!r}
ENDPOINT = {endpoint!r}
METHOD = {method!r}
HEADERS = {headers!r}            # {{name: value | true (present) | false (absent)}}
FAULT = {fault_def!r}
BEFORE = {before!r}              # the steps before the faulty one
STEP = {step_actions!r}          # the step the fault is on
INPUT = {input_note!r}           # synthetic input only
EARLY = {early!r}                # install the fault before the page loads


def guard(route, request):
    if request.url.startswith(('data:', 'blob:')):
        return route.continue_()
    if (urlparse(request.url).hostname or '').lower() not in HOSTS:
        return route.abort('blockedbyclient')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not ALLOW_WRITES:
        return route.abort()
    return route.continue_()


seen = {{'n': 0}}


def fault(route, request):
    if METHOD and request.method != METHOD:
        return route.fallback()
    for k, v in HEADERS.items():
        have = request.headers.get(k.lower())
        if (v is False and have is not None) or (v is True and have is None) or (v not in (True, False) and have != str(v)):
            return route.fallback()
    seen['n'] += 1
    a = FAULT['action']
    if a == 'abort':
        return route.abort(FAULT.get('error', 'failed'))
    if a == 'hang':
        return None                              # never answered while the script watches
    if a in ('delay', 'delay_abort'):
        time.sleep(FAULT['ms'] / 1000)            # blocks the script's event loop: fine for a repro
        return route.abort(FAULT.get('error', 'timedout')) if a == 'delay_abort' else route.fallback()
    if a == 'flap' and seen['n'] > FAULT['failures']:
        return route.fallback()
    body = FAULT.get('body', '')
    if 'body_size' in FAULT:
        body = '{{"items": [' + ','.join(['{{"title": "x"}}'] * (FAULT['body_size'] // 16)) + ']}}'
    route.fulfill(status=FAULT.get('status', 200), body=body if isinstance(body, str) else json.dumps(body),
                  headers=FAULT.get('headers') or {{}}, content_type=FAULT.get('content_type', 'application/json'))


def run(page, actions):
    for a in actions:
        if 'goto' in a:
            if a['goto'].startswith('#'):
                page.evaluate('(h) => {{ location.hash = h; }}', a['goto'][1:])
            else:
                page.goto(SITE.rstrip('/') + '/' + a['goto'].lstrip('/'))
        elif 'click' in a:
            page.click(a['click'], timeout=5000)
        elif 'fill' in a:
            page.fill(a['fill'], a.get('value', ''), timeout=5000)
        elif 'press' in a:
            page.keyboard.press(a['press'])
        elif 'select' in a:
            page.select_option(a['select'], a.get('value'))
        elif 'wait' in a:
            page.wait_for_timeout(int(a['wait']))
        elif 'wait_for' in a:
            page.wait_for_selector(a['wait_for'], timeout=10000)
        page.wait_for_timeout(300)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(service_workers='block')
    ctx.route('**/*', guard)
    page = ctx.new_page()
    if EARLY and FAULT['action'] != 'offline':
        page.route(ENDPOINT, fault)
    page.goto(SITE)
    run(page, BEFORE)
    if FAULT['action'] == 'offline':
        ctx.set_offline(True)
    elif not EARLY:
        page.route(ENDPOINT, fault)
    run(page, STEP)
    page.wait_for_timeout(10000)
    print('requests the fault answered:', seen['n'])
    page.screenshot(path='{fid}.png')
    browser.close()
'''


def write_repro(path: Path, fid: str, f: dict, scen: dict, flow: dict, cfg: dict, faults: dict):
    idx = next(i for i, st in enumerate(flow['steps']) if st['name'] == scen['step'])
    hosts = sorted({(urlsplit(u).hostname or '').lower() for u in [cfg['site']] + list(cfg.get('targets') or [])} |
                   {h.lower() for h in cfg.get('allowed_hosts', []) if not h.startswith('*.')})
    text = REPRO.format(fid=fid, effect=f['effect'], flow=f['flow'], step=f['step'], fault=scen['fault'], endpoint=scen['endpoint'],
                        writes_note='' if not cfg.get('allow_form_submission') else ' (writes are let through: flows.json allows form submission)',
                        site=cfg['site'], hosts=hosts, allow_writes=bool(cfg.get('allow_form_submission')), method=scen.get('method'),
                        headers=scen.get('headers') or {}, fault_def=faults[scen['fault']],
                        before=[a for st in flow['steps'][:idx] for a in st['actions']], step_actions=flow['steps'][idx]['actions'],
                        input_note={k: f'synthetic, {len(v)} characters' for k, v in (flow['steps'][idx].get('input') or {}).items()},
                        early=idx == 0 or scen.get('from') == 'load')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--scenarios', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--flows', type=Path, required=True)
    ap.add_argument('--promote', default='', help='comma-separated candidate ids to promote to findings, in order')
    ap.add_argument('--repros', type=Path, help='write a guarded repro script per finding into this folder (usually <run>/repros)')
    ap.add_argument('--devices', default='Desktop Chromium 1280x800')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run, plan, cands, cfg = load(a.run), load(a.scenarios), load(a.candidates), load(a.flows)
    faults = load(Path(__file__).resolve().parent.parent / 'data' / 'faults.json')['faults']
    flows = {f['name']: f for f in cfg['flows']}
    ran = {s['id']: s for s in run['scenarios']}
    per = {s['id']: s for s in cands['scenarios']}
    statuses = {s['id']: ran[s['id']]['status'] for s in plan['scenarios']}
    cmap = {c['id']: c for c in cands['candidates']}
    promote = [x.strip() for x in a.promote.split(',') if x.strip()]
    for k in promote:
        if k not in cmap:
            raise SystemExit(f'no candidate {k}')
    scenarios = []
    for s in plan['scenarios']:
        r = ran[s['id']]
        shots = [x for rr in r['runs'] for x in (rr['steps'][-1].get('screenshots', []) if rr['steps'] and rr['steps'][-1].get('target') else [])]
        e = {'id': s['id'], 'flow': s['flow'], 'step': s['step'], 'endpoint': s['endpoint'], 'fault': s['fault'], 'expected': s['expected'],
             'status': r['status'], 'effects': per[s['id']]['effects'], 'runs': len(r['runs']), 'screenshots': shots}
        if r.get('reason'):
            e['reason'] = r['reason']
        scenarios.append(e)
    findings = []
    base_dir = a.run.parent
    for n, k in enumerate(promote, 1):
        c = cmap[k]
        sid = c['scenarios'][0]
        scen = next(s for s in plan['scenarios'] if s['id'] == sid)
        script = f'repros/F{n}.py'
        if a.repros:
            write_repro(a.repros / f'F{n}.py', f'F{n}', c, scen, flows[c['flow']], cfg, faults)
            script = (a.repros / f'F{n}.py').resolve().relative_to(base_dir.resolve()).as_posix()
        findings.append({'id': f'F{n}', 'candidate_id': k, 'title': TODO + 'what the user sees, in one line',
                         **{x: c[x] for x in ('effect', 'on_write', 'impact', 'likelihood', 'reach', 'score', 'priority', 'confidence', 'floors',
                                              'flow', 'step', 'endpoints', 'faults', 'scenarios')},
                         'expected': scen['expected'], 'actual': TODO + 'what happened, against the expected outcome, with the measure',
                         'user_impact': TODO + 'what the user loses', 'evidence': c['evidence'],
                         'repro': {'flow': c['flow'], 'step': c['step'], 'fault': scen['fault'], 'endpoint': scen['endpoint'], 'script': script},
                         'fix': TODO + 'a concrete fix', 'verification_step': TODO + 'how to check the fix (rerun which scenarios)'})
    triage, needs_review = [], []
    fid = {k: f'F{n}' for n, k in enumerate(promote, 1)}
    for c in cands['candidates']:
        if c['id'] in fid:
            triage.append({'candidate_id': c['id'], 'decision': 'promoted', 'finding_id': fid[c['id']], 'reason': TODO + 'why it is a real defect'})
        else:
            triage.append({'candidate_id': c['id'], 'decision': 'needs_review', 'finding_id': None,
                           'reason': TODO + 'promote, or dismiss with a checkable reason (cite the run, a screenshot or a measure)'})
            needs_review.append({'candidate_id': c['id'], 'reason': TODO + 'what a person must decide'})
    fr, fu = faults_split(plan, statuses)
    baseline = derive_baselines(run)
    lim = []
    for name, b in baseline.items():
        if b['status'] != 'passed':
            lim.append(f'The {name} flow\'s baseline {b["status"]} without any fault' +
                       (f' (step "{b["failed_step"]}" did not complete)' if b.get('failed_step') else '') + ', so its scenarios are untested.')
    nt = [s['id'] for s in scenarios if s['status'] == 'not_triggered']
    if nt:
        lim.append(f'{", ".join(nt)}: the fault matched no request, so nothing was tested (see each scenario\'s reason).')
    for u in plan.get('unreachable', []):
        where = f' ({u["flow"]}' + (f' / {u["step"]}' if u.get('step') else '') + ')' if u.get('flow') else ''
        lim.append(f'Not reachable from the browser{where}: {u["what"]}: {u["reason"]}.')
    if run.get('runs_per_scenario', 2) < 2:
        lim.append(f'Each scenario ran {run.get("runs_per_scenario")} time(s): nothing can be reproduced, so every finding is observed and capped at P2.')
    if run.get('budget_reached'):
        lim.append(f'The request budget ({run["safety"]["request_budget"]}) was reached; the remaining scenarios are untested.')
    if fu:
        lim.append('Planned but not run: ' + ', '.join(fu) + '.')
    rep = {
        'skill': {'name': 'resilience-prober', 'version': SKILL_VERSION},
        'scope': {'target': cfg['site'], 'authorised_by': cfg.get('authorised_by'), 'devices': [a.devices],
                  'flows': [{'name': f['name'], 'critical_path': bool(f.get('critical_path')), 'steps': [s['name'] for s in f['steps']]} for f in cfg['flows']],
                  'faults': fr, 'faults_untested': fu, 'budgets': plan['budgets']},
        'metadata': {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'browser': run['browser'],
                     'tools': [{'name': 'playwright-chromium', 'version': run['browser']['version'], 'status': 'ran',
                                'note': 'faults injected with page.route and set_offline'}],
                     'har': None, 'blocked_requests': len(run['blocked_requests']), 'requests_sent': run.get('requests_sent'),
                     'request_budget': run.get('safety', {}).get('request_budget'),
                     'allow_form_submission': run.get('safety', {}).get('allow_form_submission'), 'runs_per_scenario': run.get('runs_per_scenario')},
        'baseline': baseline,
        'scenarios': scenarios,
        'findings': findings,
        'notes': cands['notes'],
        'coverage': coverage(plan, statuses),
        'unreachable': plan.get('unreachable', []),
        'headline': {'summary': TODO + 'which flows and faults were tried, what broke, and what was not tested',
                     'priority_counts': {}, 'flows_with_p0': [], 'scenarios_run': 0, 'scenarios_planned': 0, 'overall_confidence': 'low'},
        'needs_review': needs_review,
        'triage': triage,
        'limitations': lim,
    }
    hl = rep['headline']
    hl['priority_counts'] = {p: sum(1 for f in findings if f['priority'] == p) for p in ('P0', 'P1', 'P2', 'P3') if any(f['priority'] == p for f in findings)}
    hl['flows_with_p0'] = sorted({f['flow'] for f in findings if f['priority'] == 'P0'})
    hl['scenarios_run'] = sum(1 for s in scenarios if s['status'] == 'ran')
    hl['scenarios_planned'] = len(scenarios)
    hl['overall_confidence'] = confidence(run, plan, statuses, findings, needs_review)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(scrub(rep), indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{a.out}: {len(findings)} finding(s), {len(needs_review)} candidate(s) to triage; fill every TODO, then validate', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
