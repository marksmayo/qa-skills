#!/usr/bin/env python3
"""Validate a CI failure triage against its contract and its run, and recompute every classification rule.

    python validate_triage.py triage.json --run run/ [--write-summary]

Re-extracts the failures from <run>/run.json and re-applies classify.py's rules, then checks: every
failure is triaged once; a classification that differs from its candidate has an override reason, and
every classification has its evidence (consequence: an aggregate gate with other failures in the run;
flaky: a pass on the same commit, by the re-run or by the same job in another run of the workflow; real:
not passed on re-run, and `confirmed` only with a re-run and an owning change; environment: a signature
or a setup step; data: a data signature or a date before the run date); confidence never exceeds what the
evidence gives (for an override, the rules are re-applied to the log lines the agent quoted, once they
are checked against the log, so a quoted signature can carry its confidence); the owning change is a
commit in range sharing the listed files (changes.json); real and data failures have a draft, the others
none; every log excerpt equals the log at those lines (ANSI codes and timestamps stripped) and no text
holds a token-like value; a failed log download is named in the limitations; a re-run made by this
triage has authorised_by and re-ran only failed jobs, and one made by someone else is recorded as
`observed` with who started it; priorities and the summary are recomputed; no text calls the build green,
stable or fine.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from extract_failures import SECRET, TEST_STEP, extract, redact, strip  # noqa: E402
from classify import decide, signals  # noqa: E402

SCHEMA_VERSION = '1.1.0'
CONF = {'confirmed': 3, 'indicated': 2, 'low': 1}
CLASSES = ('real', 'flaky', 'environment', 'data', 'consequence', 'unclassified')
BANNED = re.compile(r'(?i)\b(build is (fine|green|healthy|ok)|all (green|good|clear)|stable build|nothing (is )?wrong|safe to merge|(?<!not )green build|ready to merge)\b')
DRAFT_KIND = {'real': 'defect', 'data': 'test_data_issue'}


def expected_priority(f: dict, run: dict) -> str | None:
    if f['classification'] != 'real':
        return None
    br = run['run'].get('branch') or ''
    return 'P1' if br in ('main', 'master', 'trunk', 'develop') or br.startswith(('release/', 'release-', 'hotfix/')) else 'P2'


def completeness(run: dict, run_dir: Path, changes: dict | None, failures: list[dict] | None = None) -> float:
    """Five checks: logs for every failed job; test results (JUnit, or a test runner's console summary)
    for every failed job with a test step; a re-run; a last green run; the repository for the owning change."""
    first = run['attempts'][0]
    failed = [j for j in first['jobs'] if j['conclusion'] not in ('success', 'skipped', None)]
    if failures is None:
        failures = extract(run_dir, run, 1)
    with_tests = {f['job_id'] for f in failures if f['kind'] == 'test'}
    test_jobs = [j for j in failed if any(TEST_STEP.search(s['name']) for s in j['steps'])
                 or any(f['job_id'] == j['id'] and f.get('step_kind') == 'test' for f in failures)]
    checks = [all(j.get('log') and (run_dir / j['log']).exists() for j in failed),
              all(j.get('junit') or j['id'] in with_tests for j in test_jobs),
              len(run['attempts']) > 1, bool(run.get('last_green')), bool(changes and changes.get('commits') is not None)]
    return round(sum(checks) / len(checks), 2)


def summarise(d: dict, run: dict, run_dir: Path, changes: dict | None, failures: list[dict] | None = None) -> dict:
    by = {c: sum(1 for f in d['failures'] if f['classification'] == c) for c in CLASSES}
    return {'by_classification': by, 'defects_drafted': sum(1 for f in d['failures'] if f.get('draft_defect')),
            'reruns': len(d.get('reruns', [])), 'evidence_completeness': completeness(run, run_dir, changes, failures)}


def check_reruns(run: dict, reruns: list[dict], e) -> None:
    if len(run['attempts']) > 1 and not reruns:
        e('the run has a re-run attempt but no reruns.json: record it with rerun.py --recorded --authorised-by (this triage asked for it) '
          'or rerun.py --observed (someone else started it)')
    atts = {a['n']: a for a in run['attempts']}
    for r in reruns:
        mode = r.get('mode')
        att = atts.get(r.get('new_attempt'))
        if not att:
            e(f're-run record names attempt {r.get("new_attempt")}, which the run does not have')
            continue
        if mode == 'observed':
            want = att.get('triggered_by') or 'unknown'
            if (r.get('triggered_by') or '') != want:
                e(f'observed re-run: triggered_by {r.get("triggered_by")!r} should be {want!r} (from the run)')
            if att.get('jobs_rerun') is not None and sorted(r.get('jobs_rerun') or []) != sorted(att['jobs_rerun']):
                e(f'observed re-run: jobs_rerun {r.get("jobs_rerun")} should be {att["jobs_rerun"]} (jobs with a new start time)')
            continue
        if mode not in ('live', 'recorded'):
            e(f're-run mode {mode!r} is not live, recorded or observed')
        if len((r.get('authorised_by') or '').strip()) < 3:
            e('a re-run made by this triage without authorised_by')
        extra = set(r['jobs_rerun']) - set(r['jobs_failed_before'])
        if extra:
            e(f're-ran jobs that had not failed: {sorted(extra)}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('triage', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.triage.read_text(encoding='utf-8'))
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    changes = json.loads((a.run / 'changes.json').read_text(encoding='utf-8')) if (a.run / 'changes.json').exists() else None
    reruns = json.loads((a.run / 'reruns.json').read_text(encoding='utf-8'))['reruns'] if (a.run / 'reruns.json').exists() else []
    E: list[str] = []
    e = E.append
    for k in ('schema_version', 'skill', 'run', 'reruns', 'failures', 'summary', 'limitations'):
        if k not in d:
            e(f'missing {k}')
    if E:
        print('\n'.join(E)); return 1
    if d['schema_version'] != SCHEMA_VERSION:
        e(f'schema_version {d["schema_version"]} should be {SCHEMA_VERSION}')
    if d['run']['id'] != run['run']['id'] or d['run']['commit'] != run['run']['commit']:
        e('run id or commit differ from run.json')
    if (d['run'].get('last_green') or {}).get('id') != (run.get('last_green') or {}).get('id'):
        e('run.last_green differs from run.json')

    check_reruns(run, reruns, e)
    if d['reruns'] != reruns:
        e('reruns differ from reruns.json')

    # downloads that failed must be said ---------------------------------------------------------------
    lim = ' '.join(d['limitations']).lower()
    for p in run.get('fetch_problems', []):
        if p['kind'] == 'log' and not ('log' in lim and (p['job'] or '').lower() in lim):
            e(f'the log of job {p["job"]} was not downloaded ({p["status"]}); a limitation must name the job and say its log is missing')
        if p['kind'] == 'artifact' and (p.get('artifact') or '').lower() not in lim:
            e(f'artifact {p.get("artifact")} was not downloaded ({p["status"]}); a limitation must name it')

    # failures --------------------------------------------------------------------------------------
    want = extract(a.run, run, 1)
    got = {}
    for f in d['failures']:
        if f['id'] in got:
            e(f'{f["id"]} triaged twice')
        got[f['id']] = f
    for w in want:
        f = got.get(w['id'])
        if not f:
            e(f'{w["id"]} ({w["test_id"] or w["step"]}) not triaged'); continue
        if (f.get('test_id'), f.get('job'), f.get('kind')) != (w['test_id'], w['job'], w['kind']):
            e(f'{w["id"]}: test, job or kind differ from the run')
        sg = signals(w, run, a.run)
        owner = (changes or {}).get('per_failure', {}).get(w['id'])
        cls, conf, rule = decide(sg, owner)
        fc = f['classification']
        if fc not in CLASSES:
            e(f'{w["id"]}: unknown classification {fc}'); continue
        # log excerpt: checked first, because an override may rest on the lines quoted
        lx = f.get('log_excerpt') or {}
        lp = a.run / (lx.get('path') or '')
        quoted_ok = False
        if lx.get('lines'):
            if not lp.is_file():
                e(f'{w["id"]}: log {lx.get("path")} not in the run')
            else:
                log = lp.read_text(encoding='utf-8').split('\n')
                bad = next((l for l in lx['lines'] if not (1 <= l['n'] <= len(log)) or redact(strip(log[l['n'] - 1])) != l['text']), None)
                if bad:
                    e(f'{w["id"]}: log line {bad["n"]} does not match {lx["path"]}')
                else:
                    quoted_ok = lx.get('path') == w['log_excerpt']['path']
        elif w['log_excerpt']['lines']:
            e(f'{w["id"]}: no log excerpt')
        sgq = signals(w, run, a.run, quoted=lx['lines']) if quoted_ok else sg
        clsq, confq, _ = decide(sgq, owner)
        if fc != cls and len(f.get('override_reason') or '') < 20:
            e(f'{w["id"]}: classified {fc} but the evidence gives {cls} ({rule}); an override needs a specific reason')
        cap = conf if fc == cls else confq if fc == clsq else 'indicated'
        if CONF.get(f['confidence'], 0) > CONF.get(cap, 0):
            e(f'{w["id"]}: confidence {f["confidence"]} is higher than the evidence gives ({cap})')
        envsig = [h for h in sg['signatures'] + sgq['signatures'] if h['class'] == 'environment']
        datasig = [h for h in sg['signatures'] + sgq['signatures'] if h['class'] == 'data']
        if fc == 'consequence' and not sg['consequence_of']:
            e(f'{w["id"]}: consequence needs an aggregate gate job with other failures in the run')
        if fc == 'flaky' and not (sg['rerun'] == 'passed' or sg['history_passed_same_commit']):
            e(f'{w["id"]}: flaky needs a pass on the same commit (re-run {sg["rerun"]}, and no run of this workflow on this commit where job {w["job"]} passed)')
        if fc == 'real' and sg['rerun'] == 'passed':
            e(f'{w["id"]}: real, but it passed on re-run')
        if fc == 'real' and f['confidence'] == 'confirmed' and (sg['rerun'] != 'failed' or not owner or owner.get('status') != 'identified'):
            e(f'{w["id"]}: real is confirmed only when it failed again on re-run and an owning change is identified')
        if fc == 'environment' and not (envsig or sg['step_kind'] == 'setup'):
            e(f'{w["id"]}: environment needs a signature in the log or a failed setup step (this is a {sg["step_kind"]} step)')
        if fc == 'data' and not (datasig or sg['date_markers'] or sgq['date_markers']):
            e(f'{w["id"]}: data needs a data signature or a date before the run date')
        fs_ids = [s['id'] for s in f['signals'].get('signatures', [])]
        if fs_ids not in ([h['id'] for h in sg['signatures']], [h['id'] for h in sgq['signatures']]) or f['signals'].get('rerun') != sg['rerun']:
            e(f'{w["id"]}: signals differ from the run (signatures {[h["id"] for h in sg["signatures"]]}, re-run {sg["rerun"]})')
        # owning change
        oc = f.get('owning_change') or {}
        if oc.get('status') == 'identified':
            c = next((c for c in (changes or {}).get('commits') or [] if c['commit'] == oc.get('commit')), None)
            if not c:
                e(f'{w["id"]}: owning change {oc.get("commit")} is not in the range since the last green run')
            elif not oc.get('files_shared') or any(x['file'] not in c['files'] for x in oc['files_shared']):
                e(f'{w["id"]}: owning change {oc["commit"][:8]} does not touch the files it is said to share')
            if not owner or owner.get('commit') != oc.get('commit'):
                if len(oc.get('override_reason') or '') < 20:
                    e(f'{w["id"]}: owning change differs from changes.json without a reason')
        elif fc == 'real' and owner and owner.get('status') == 'identified':
            e(f'{w["id"]}: changes.json identifies {owner["commit"][:8]} but the triage does not')
        # drafts
        dk = DRAFT_KIND.get(fc)
        dd = f.get('draft_defect')
        if dk and not dd:
            e(f'{w["id"]}: a {fc} failure needs a draft ({dk})')
        if not dk and dd:
            e(f'{w["id"]}: no draft for a {fc} failure (it gets a next action instead)')
        if dd and dd.get('kind') != dk:
            e(f'{w["id"]}: draft kind {dd.get("kind")} should be {dk}')
        if not f.get('next_action'):
            e(f'{w["id"]}: no next action')
        if f.get('priority') != expected_priority(f, run):
            e(f'{w["id"]}: priority {f.get("priority")} should be {expected_priority(f, run)}')
    for k in set(got) - {w['id'] for w in want}:
        e(f'{k} is not a failure of the run')

    # secrets and language ----------------------------------------------------------------------------
    text = json.dumps(d, ensure_ascii=False)
    for m in SECRET.finditer(text):
        if '[REDACTED]' not in text[m.start():m.end() + 12]:
            e(f'a token-like value appears in the triage: {m.group(0)[:6]}...'); break
    for t in [d['summary'].get('headline', '')] + d['limitations'] + [f.get('next_action', '') for f in d['failures']]:
        m = BANNED.search(t or '')
        if m:
            e(f'text says {m.group(0)!r}: never call the build green, stable or fine')

    # summary ---------------------------------------------------------------------------------------
    want_s = summarise(d, run, a.run, changes, want)
    if a.write_summary:
        d['summary'].update(want_s)
        a.triage.write_text(json.dumps(d, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    for k, v in want_s.items():
        if d['summary'].get(k) != v:
            e(f'summary.{k} {d["summary"].get(k)!r} should be {v!r}')

    if E:
        print(f'{a.triage}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    s = d['summary']
    print(f'{a.triage}: valid. {len(d["failures"])} failure(s): ' + ', '.join(f'{k} {v}' for k, v in s['by_classification'].items() if v)
          + f'; {s["defects_drafted"]} draft(s), {s["reruns"]} re-run(s), evidence completeness {s["evidence_completeness"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
