#!/usr/bin/env python3
"""Classify each CI failure from its signals into a candidate: consequence, real, flaky, environment, data or unclassified.

    python classify.py --run run/ --out run/candidates.json

Signals per failure: signatures in its message and log lines (data/signatures.json, with the line), the
step kind (test, setup or other), the re-run outcome on the same commit (the latest attempt; a job the
attempt did not actually re-run is `job_not_rerun`), earlier outcomes of the same job on the same commit in
runs of the same workflow, dates before the run date in the message (a data marker), whether it is an
aggregate gate failing because other jobs failed, and the owning change from changes.json. Rules, in order:

  consequence   an aggregate gate job, failing because other failures in the run failed   indicated
  environment   an environment signature                         confirmed if the re-run passed, else indicated
  flaky         failed, then passed on the same commit: the re-run, or the same job in    confirmed
                another run of the same workflow on that commit
  data          a data signature or a date before the run date   indicated
  real          a real signature (toolchain below a minimum, compile error)                indicated
  real          an assertion or error in a test that failed again on re-run  confirmed with an owning change, else indicated;
                (no re-run made)                                  indicated
  unclassified  none of the above (the rule says so when the job's log could not be read)

The agent triages every candidate (and may override with a reason); validate_triage.py re-applies these
rules to check each classification has the evidence it needs.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402
from extract_failures import console_tests, match_sections, step_sections  # noqa: E402

SIG = json.loads((HERE.parent / 'data' / 'signatures.json').read_text(encoding='utf-8'))
COMPILED = [(s, re.compile(s['pattern'], re.I)) for s in SIG['signatures']]
DATE = re.compile(r'\b(20\d\d-\d\d-\d\d)\b')


def _console_rerun(f: dict, job: dict, run_dir: Path) -> str:
    """The re-run outcome of a test read from console output (no JUnit)."""
    if job['conclusion'] == 'success':
        return 'passed'
    if not job.get('log') or not (run_dir / job['log']).exists():
        return 'missing'
    lines = (run_dir / job['log']).read_text(encoding='utf-8').split('\n')
    failed = [s for s in job['steps'] if s['conclusion'] == 'failure']
    ids = set()
    for sec, _ in match_sections(step_sections(lines), failed):
        if sec:
            ids |= {c['test_id'] for c in console_tests(lines, sec['body'], sec['end'])}
    if f['test_id'] in ids:
        return 'failed'
    st = next((s for s in job['steps'] if s['name'] == f['step']), None)
    return 'passed' if st and st['conclusion'] == 'success' else 'missing'


def signals(f: dict, run: dict, run_dir: Path, quoted: list[dict] | None = None) -> dict:
    """The evidence for one failure. `quoted` replaces the extracted log lines (the validator passes the
    lines an agent quoted, after checking they equal the log)."""
    hits = []
    texts = [(None, f['message'])] + [(l['n'], l['text']) for l in (quoted if quoted is not None else f['log_excerpt']['lines'])]
    for s, rx in COMPILED:
        for n, t in texts:
            if rx.search(t or ''):
                hits.append({'id': s['id'], 'class': s['class'], 'names': s['names'], 'line': n, 'text': (t or '')[:200]})
                break
    run_date = (run['run'].get('started_at') or '')[:10]
    markers = sorted({d for d in DATE.findall(' '.join(t or '' for _, t in texts)) if run_date and d < run_date})
    last = run['attempts'][-1]
    rerun = 'not_rerun'
    if last['n'] > 1:
        job = next((j for j in last['jobs'] if j['name'] == f['job']), None)
        if not job or (last.get('jobs_rerun') is not None and f['job'] not in last['jobs_rerun']):
            rerun = 'job_not_rerun'
        elif f['kind'] == 'test' and f.get('source') == 'console':
            rerun = _console_rerun(f, job, run_dir)
        elif f['kind'] == 'test':
            cases = []
            for x in job.get('junit', []):
                cases += parse_junit(run_dir / x, f'A{last["n"]}')[0]
            c = next((c for c in cases if c['id'] == f['test_id']), None)
            rerun = 'missing' if c is None else 'passed' if c['status'] == 'passed' else 'failed' if c['status'] in ('failed', 'error') else c['status']
        else:
            st = next((s for s in job['steps'] if s['name'] == f['step']), None)
            rerun = 'passed' if st and st['conclusion'] == 'success' else 'failed' if st and st['conclusion'] == 'failure' else 'missing'
    # the history holds only runs of the same workflow (fetch_run.py); the same job must have passed on this commit
    hist = []
    for h in run.get('history', []):
        if h['commit'] != run['run']['commit'] or h['id'] == run['run']['id']:
            continue
        jc = None if h.get('jobs') is None else next((j['conclusion'] for j in h['jobs'] if j['name'] == f['job']), 'absent')
        hist.append({'id': h['id'], 'conclusion': h['conclusion'], 'event': h.get('event'), 'job': jc})
    kind = f.get('step_kind') or ('test' if f['kind'] == 'test' else 'setup')
    return {'signatures': hits, 'date_markers': markers, 'run_date': run_date, 'rerun': rerun, 'rerun_attempt': last['n'] if last['n'] > 1 else None,
            'history_same_commit': hist, 'history_passed_same_commit': any(x['job'] == 'success' for x in hist),
            'step_kind': kind, 'failure_type': f.get('failure_type'), 'log_status': f.get('log_status', 'fetched'),
            'consequence_of': f.get('consequence_of') or []}


def decide(sg: dict, owner: dict | None) -> tuple[str, str, str]:
    env = [h for h in sg['signatures'] if h['class'] == 'environment']
    data = [h for h in sg['signatures'] if h['class'] == 'data']
    real = [h for h in sg['signatures'] if h['class'] == 'real']
    if sg.get('consequence_of'):
        return 'consequence', 'indicated', f'aggregate gate: fails because {", ".join(sg["consequence_of"])} failed in this run'
    if env:
        return 'environment', 'confirmed' if sg['rerun'] == 'passed' else 'indicated', f'environment signature {env[0]["id"]} ({env[0]["names"]})'
    if sg['rerun'] == 'passed' or sg['history_passed_same_commit']:
        return 'flaky', 'confirmed', 'failed and passed on the same commit' + (' (re-run)' if sg['rerun'] == 'passed' else ' (the same job in another run of the workflow)')
    if data or sg['date_markers']:
        why = f'data signature {data[0]["id"]} ({data[0]["names"]})' if data else ''
        if sg['date_markers']:
            why += ('; ' if why else '') + f'date {", ".join(sg["date_markers"])} before the run date {sg["run_date"]}'
        return 'data', 'indicated', why
    if real:
        return 'real', 'indicated', f'real signature {real[0]["id"]} ({real[0]["names"]})'
    if sg['step_kind'] == 'test' and sg['failure_type'] in ('assertion', 'error'):
        if sg['rerun'] == 'failed':
            return 'real', 'confirmed' if owner and owner.get('status') == 'identified' else 'indicated', 'failed again on re-run on the same commit'
        if sg['rerun'] in ('not_rerun', 'job_not_rerun'):
            return 'real', 'indicated', 'assertion failure; not re-run'
    if sg.get('log_status', 'fetched') != 'fetched':
        return 'unclassified', 'low', f'no log to read ({sg["log_status"]}); no rule decided it'
    return 'unclassified', 'low', 'no rule decided it'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    fails = json.loads((a.run / 'failures.json').read_text(encoding='utf-8'))['failures']
    changes = json.loads((a.run / 'changes.json').read_text(encoding='utf-8')) if (a.run / 'changes.json').exists() else {'per_failure': {}}
    out = []
    for f in fails:
        sg = signals(f, run, a.run)
        owner = changes['per_failure'].get(f['id'])
        cls, conf, rule = decide(sg, owner)
        out.append({'id': f['id'], 'failure': f, 'classification': cls, 'confidence': conf, 'rule': rule, 'signals': sg, 'owning_change': owner})
    a.out.write_text(json.dumps({'run_id': run['run']['id'], 'candidates': out}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(out)} candidate(s) -> {a.out}')
    for c in out:
        print(f'  {c["id"]} {c["classification"]:12} {c["confidence"]:9} {c["rule"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
