#!/usr/bin/env python3
"""Validate a QA orchestrator run: gate order, decisions, recorded reports, criteria and language.

    python validate_run.py <state dir> [--rerun]

Checks: the config is the one recorded (SHA-256); gates went in order (a gate started only after the one
before it was approved or skipped); every approval came after its criteria were evaluated, names who and
how, and - when a criterion failed - carries an override reason and the failed criteria; an auto-approval
only on a gate whose config allows it, with every criterion passing; a rejection has a reason and stops
the run; a skipped gate has a reason; each recorded report still has its SHA-256 and its validator exited 0
(--rerun runs the validators again); re-evaluating the criteria on the recorded reports gives the stored
results; the run status follows the gates; no text claims QA is complete or the release approved by QA.
"""
from __future__ import annotations
import argparse, hashlib, json, re, shlex, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from orchestrate import evaluate_criteria  # noqa: E402

BANNED = re.compile(r'(?i)\b(qa (is )?(complete|done|signed off)|release (is )?approved by qa|fully tested|qa approved the release|all quality gates passed)\b')


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state', type=Path)
    ap.add_argument('--rerun', action='store_true')
    a = ap.parse_args()
    d = json.loads((a.state / 'state.json').read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    cp = Path(d['config_path'])
    if sha(cp) != d['config_sha256'] or json.loads(cp.read_text(encoding='utf-8')) != d['config']:
        e('the config changed since the run started')
    cfg = {g['id']: g for g in d['config']['gates']}
    gates = d['gates']
    if [g['id'] for g in gates] != [g['id'] for g in d['config']['gates']]:
        e('the gates differ from the config')
    started_states = ('in_progress', 'criteria_met', 'criteria_failed', 'approved', 'rejected')
    for i, g in enumerate(gates):
        w = g['id']
        if g['status'] in started_states:
            for p in gates[:i]:
                if p['status'] not in ('approved', 'skipped'):
                    e(f'{w} started while {p["id"]} was {p["status"]}')
        dec = g.get('decision') or {}
        crit = g.get('criteria', [])
        failed = sorted(c['id'] for c in crit if c['result'] == 'fail')
        if g['status'] == 'approved':
            if not crit:
                e(f'{w}: approved without evaluated criteria')
            if dec.get('kind') == 'approved' and failed:
                e(f'{w}: approved with failed criteria {failed} and no override')
            if dec.get('kind') == 'approved_with_override':
                if len((dec.get('override') or '').strip()) < 20:
                    e(f'{w}: an override needs a reason')
                if sorted(dec.get('failed_criteria', [])) != failed:
                    e(f'{w}: the override does not list the failed criteria {failed}')
            if dec.get('kind') == 'auto_approved':
                if not cfg[w].get('auto_approve'):
                    e(f'{w}: auto-approved, but the config does not allow auto_approve for this gate')
                if failed:
                    e(f'{w}: auto-approved with failed criteria')
            elif dec.get('kind') in ('approved', 'approved_with_override'):
                if len((dec.get('by') or '').strip()) < 3 or len((dec.get('how') or '').strip()) < 3:
                    e(f'{w}: an approval must name who and how')
            else:
                e(f'{w}: approved without an approval decision')
        if g['status'] == 'rejected' and len((dec.get('reason') or '').strip()) < 20:
            e(f'{w}: a rejection needs a reason')
        if g['status'] == 'skipped' and len((dec.get('reason') or '').strip()) < 10:
            e(f'{w}: a skipped gate needs a reason')
        if g['status'] in ('pending',) and (dec or crit or any(s['report'] for s in g['steps'])):
            e(f'{w}: pending, but has records')
        # reports and criteria -------------------------------------------------------------------------
        for s in g['steps']:
            if not s['report']:
                continue
            rp = Path(s['report'])
            if sha(rp) != s['sha256']:
                e(f'{w}/{s["id"]}: {s["report"]} changed since it was recorded')
            if g['status'] == 'approved' and s['required'] and (s.get('validator') or {}).get('exit_code') != 0:
                e(f'{w}/{s["id"]}: the gate is approved but the step\'s validator did not exit 0')
            if a.rerun and s.get('validator'):
                r = subprocess.run(shlex.split(s['validator']['command']), capture_output=True, text=True, cwd=s['validator'].get('cwd'))
                if r.returncode != s['validator']['exit_code']:
                    e(f'{w}/{s["id"]}: the validator now exits {r.returncode}, recorded {s["validator"]["exit_code"]}')
        if crit:
            want = evaluate_criteria(g, cfg[w])
            if [(c['id'], c['result']) for c in want] != [(c['id'], c['result']) for c in crit]:
                e(f'{w}: criteria re-evaluate to {[(c["id"], c["result"]) for c in want]}')
    # events -----------------------------------------------------------------------------------------------
    ev = d['events']
    if [x['at'] for x in ev] != sorted(x['at'] for x in ev):
        e('events are out of order')
    for i, x in enumerate(ev):
        if x['kind'] in ('approve', 'reject'):
            prior = [y for y in ev[:i] if y['gate'] == x['gate']]
            last_rec = max([j for j, y in enumerate(prior) if y['kind'] == 'record'], default=-1)
            if not any(y['kind'] == 'evaluate' for y in prior[last_rec + 1:]) and x['kind'] == 'approve':
                e(f'{x["gate"]}: approved without an evaluation after the last recorded report')
    # run status ---------------------------------------------------------------------------------------------
    want_status = 'stopped' if any(g['status'] == 'rejected' for g in gates) else 'finished' if all(g['status'] in ('approved', 'skipped') for g in gates) else 'running'
    if d['status'] != want_status:
        e(f'run status {d["status"]} should be {want_status}')
    texts = [d['subject']] + [str((g.get('decision') or {}).get(k) or '') for g in gates for k in ('reason', 'override', 'how')] + [d.get('summary', {}).get('headline', '')]
    for t in texts:
        m = BANNED.search(t)
        if m:
            e(f'text says {m.group(0)!r}: list each gate\'s decision, who made it and on what evidence')
    if E:
        print(f'{a.state}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    print(f'{a.state}: valid. run {d["run_id"]} {d["status"]}: ' + ', '.join(f'{g["id"]} {g["status"]}' for g in gates))
    return 0


if __name__ == '__main__':
    sys.exit(main())
