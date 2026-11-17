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
import argparse, hashlib, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
from orchestrate import Refused, evaluate_criteria, trusted_config, trusted_root, validator_command  # noqa: E402

BANNED = re.compile(r'(?i)\b(qa (is )?(complete|done|signed off)|release (is )?approved by qa|fully tested|qa approved the release|all quality gates passed)\b')


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if p.exists() else None  # line endings normalised


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state', type=Path)
    ap.add_argument('--rerun', action='store_true', help='run each step validator again (only the configured validator, '
                    'rebuilt from the config file; refused when the config, a report or a command does not match)')
    ap.add_argument('--skills-root', help='the skills folder whose validators --rerun may run (default: the folder holding this skill)')
    a = ap.parse_args()
    d = json.loads((a.state / 'state.json').read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    cp = Path(d['config_path'])
    config_ok = sha(cp) == d['config_sha256'] and json.loads(cp.read_text(encoding='utf-8')) == d['config']
    if not config_ok:
        e('the config changed since the run started')
    reruns: list[tuple[str, dict]] = []
    changed_reports = 0
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
                changed_reports += 1
            if g['status'] == 'approved' and s['required'] and (s.get('validator') or {}).get('exit_code') != 0:
                e(f'{w}/{s["id"]}: the gate is approved but the step\'s validator did not exit 0')
            if a.rerun and s.get('validator'):
                reruns.append((w, s))
        if crit:
            want = evaluate_criteria(g, cfg[w])
            if [(c['id'], c['result']) for c in want] != [(c['id'], c['result']) for c in crit]:
                e(f'{w}: criteria re-evaluate to {[(c["id"], c["result"]) for c in want]}')
    # --rerun: nothing from the state file is executed. Each command is rebuilt from the config file and must equal
    # the recorded one; any mismatch, a changed config or a changed report refuses the whole rerun before anything runs.
    if a.rerun:
        refusals, plans = [], []
        if not config_ok:
            refusals.append('the config file is not the one recorded')
        if changed_reports:
            refusals.append(f'{changed_reports} recorded report(s) changed')
        if not refusals:
            try:
                cfg_trusted, root = trusted_config(d), trusted_root(a.skills_root)
                for w, s in reruns:
                    try:
                        argv, cmd, cwd = validator_command(d, cfg_trusted, w, s['id'], s['report'], root)
                    except Refused as x:
                        refusals.append(f'{w}/{s["id"]}: {x}')
                        continue
                    if cmd != s['validator'].get('command') or cwd != s['validator'].get('cwd'):
                        refusals.append(f'{w}/{s["id"]}: the recorded command is not the configured validator '
                                        f'(recorded {s["validator"].get("command")!r} in {s["validator"].get("cwd")!r}, configured {cmd!r} in {cwd!r})')
                    plans.append((w, s, argv, cwd))
            except Refused as x:
                refusals.append(str(x))
        if refusals:
            print(f'{a.state}: --rerun refused, nothing was run:')
            for x in refusals:
                print('  -', x)
            for x in E:
                print('  -', x)
            return 2
        for w, s, argv, cwd in plans:
            r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=cwd)
            if r.returncode != s['validator']['exit_code']:
                e(f'{w}/{s["id"]}: the validator now exits {r.returncode}, recorded {s["validator"]["exit_code"]}')
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


def self_test() -> int:
    """A crafted state folder must never run a command: each case plants a harmless marker-file write and checks
    that validate_run.py --rerun and orchestrate.py record refuse it, exit non-zero and leave no marker."""
    import copy, shutil, tempfile
    root = HERE.parent.parent
    example = json.loads((HERE.parent / 'examples' / 'state' / 'state.json').read_text(encoding='utf-8'))
    fails: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix='qa-orch-self-'))

    def run(script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / script), *args], cwd=root, capture_output=True, text=True,
                              encoding='utf-8', errors='replace')

    def state(name: str, d: dict) -> Path:
        p = tmp / name
        p.mkdir()
        (p / 'state.json').write_text(json.dumps(d, indent=1), encoding='utf-8')
        return p

    def evil(marker: Path) -> str:
        return f'python -c "open({marker.as_posix()!r}, \'w\').write(\'ran\')"'

    def only_g1(d: dict) -> dict:
        for g in d['gates']:
            for s in g['steps']:
                if (g['id'], s['id']) != ('G1', 'readiness'):
                    s['validator'] = None
        return d

    def expect_refused(name: str, r: subprocess.CompletedProcess, marker: Path):
        if marker.exists():
            fails.append(f'{name}: the planted command ran (marker written)')
        if r.returncode == 0:
            fails.append(f'{name}: exit 0, expected a refusal')
        if 'refus' not in (r.stdout + r.stderr).lower():
            fails.append(f'{name}: no refusal message: {(r.stdout + r.stderr).strip()[-300:]}')

    try:
        # 1. --rerun: the state's command is not the configured validator (the config itself is unchanged)
        m = tmp / 'm1.txt'
        d = only_g1(copy.deepcopy(example))
        d['gates'][0]['steps'][0]['validator'].update(command=evil(m), cwd=None)
        expect_refused('rerun-command-mismatch', run('validate_run.py', str(state('s1', d)), '--rerun'), m)
        # 2. --rerun: the config copy in the state was edited too, so it no longer matches the config file
        m = tmp / 'm2.txt'
        d = only_g1(copy.deepcopy(example))
        d['config']['gates'][0]['steps'][0]['validator'] = evil(m)
        d['gates'][0]['steps'][0]['validator'].update(command=evil(m), cwd=None)
        expect_refused('rerun-config-hash', run('validate_run.py', str(state('s2', d)), '--rerun'), m)
        # 3. --rerun: a config written beside the state (its hash matches) whose validator is not a skill's validator
        m = tmp / 'm3.txt'
        d = only_g1(copy.deepcopy(example))
        d['config']['gates'][0]['steps'][0]['validator'] = evil(m)
        cfgp = tmp / 'cfg3.json'
        cfgp.write_text(json.dumps(d['config']), encoding='utf-8')
        d.update(config_path=cfgp.as_posix(), config_sha256=sha(cfgp))
        d['gates'][0]['steps'][0]['validator'].update(command=evil(m), cwd=None)
        expect_refused('rerun-not-a-validator', run('validate_run.py', str(state('s3', d)), '--rerun'), m)
        # 4. --rerun: skills_root points at a folder the state's author wrote, holding a validate_*.py of their own
        m = tmp / 'm4.txt'
        fake = tmp / 'fake-skills'
        (fake / 'ticket-readiness-reviewer-skill' / 'scripts').mkdir(parents=True)
        (fake / 'ticket-readiness-reviewer-skill' / 'scripts' / 'validate_readiness.py').write_text(
            f'open({m.as_posix()!r}, "w").write("ran")\n', encoding='utf-8')
        d = only_g1(copy.deepcopy(example))
        d['skills_root'] = fake.as_posix()
        v = d['config']['gates'][0]['steps'][0]['validator']
        d['gates'][0]['steps'][0]['validator'].update(
            command=v.format(report=d['gates'][0]['steps'][0]['report'], skills=fake.as_posix()), cwd=None)
        expect_refused('rerun-untrusted-skills-root', run('validate_run.py', str(state('s4', d)), '--rerun'), m)
        # 5. record: the config copy in the state names a planted command (the config file says otherwise)
        m = tmp / 'm5.txt'
        d = copy.deepcopy(example)
        d['status'] = 'running'
        for g in d['gates']:
            g['status'], g['decision'], g['criteria'] = 'pending', None, []
            for s in g['steps']:
                s.update(report=None, sha256=None, validator=None)
        d['gates'][0]['status'] = 'in_progress'
        rep = 'ticket-readiness-reviewer-skill/examples/batch.example.json'
        d5 = copy.deepcopy(d)
        d5['config']['gates'][0]['steps'][0]['validator'] = evil(m)
        expect_refused('record-config-copy', run('orchestrate.py', 'record', '--state', str(state('s5', d5)), '--gate', 'G1',
                                                 '--step', 'readiness', '--report', rep), m)
        # 6. record: skills_root points at the planted folder of case 4
        m = tmp / 'm4.txt'
        d6 = copy.deepcopy(d)
        d6['skills_root'] = fake.as_posix()
        expect_refused('record-untrusted-skills-root', run('orchestrate.py', 'record', '--state', str(state('s6', d6)), '--gate', 'G1',
                                                           '--step', 'readiness', '--report', rep), m)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print('FAIL', f)
    print('self-test: ' + ('ok (6 cases)' if not fails else f'{len(fails)} failure(s)'))
    return 1 if fails else 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        sys.exit(self_test())
    sys.exit(main())
