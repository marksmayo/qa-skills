#!/usr/bin/env python3
"""Keep the state of a QA run across its gates: steps, reports, validators, criteria and human decisions (stdlib only).

    python orchestrate.py init --config orchestrator.json --subject "SHOP-310 for 2026.12" [--state-dir DIR] [--start-gate G4 --skip-reason "..."]
    python orchestrate.py next     --state DIR
    python orchestrate.py record   --state DIR --gate G2 --step cases --report test_cases.json
    python orchestrate.py evaluate --state DIR --gate G2
    python orchestrate.py approve  --state DIR --gate G2 --by "QA lead" --how "in chat" [--override "why the failed criteria are accepted"]
    python orchestrate.py reject   --state DIR --gate G4 --by "Release manager" --how "release meeting" --reason "..."
    python orchestrate.py reopen   --state DIR --gate G4 --by "..." --reason "..."
    python orchestrate.py status   --state DIR

The run state lives in a temporary folder by default (<temp>/qa-orchestrator/<run id>/state.json).
record stores the report's SHA-256 and runs the step's validator command (the config's template, with
{report} and {skills}); a step counts only when its validator exits 0. evaluate checks the gate's exit
criteria on the recorded reports. approve needs evaluated criteria and a person (who and how); with a failed
criterion it also needs --override with a reason. A gate with auto_approve on in the config is approved by
evaluate when its criteria pass, and recorded as auto_approved. reject stops the run until reopen. Gates go
in order. The script never runs the skills themselves: the agent runs each step's skill by its SKILL.md.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shlex, subprocess, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPS = {'==': lambda a, b: a == b, '<=': lambda a, b: a <= b, '>=': lambda a, b: a >= b, '<': lambda a, b: a < b, '>': lambda a, b: a > b, '!=': lambda a, b: a != b}


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if p.exists() else None  # line endings normalised


def get(obj, path: str):
    """Dotted path with [n] indexes, [field=value] filters (field may be dotted) and .length of a list."""
    cur = obj
    for part in re.findall(r'[^.\[\]]+|\[[^\]]+\]', path):
        if part.startswith('['):
            inner = part[1:-1]
            if '=' in inner:
                f, v = inner.split('=', 1)
                cur = next((x for x in cur if isinstance(x, dict) and str(get(x, f)) == v), None) if isinstance(cur, list) else None
            else:
                i = int(inner)
                cur = cur[i] if isinstance(cur, list) and len(cur) > i else None
        elif part == 'length' and isinstance(cur, list):
            cur = len(cur)
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def evaluate_criteria(gate: dict, cfg_gate: dict) -> list[dict]:
    steps = {s['id']: s for s in gate['steps']}
    out = []
    for c in cfg_gate.get('criteria', []):
        st = steps.get(c.get('step'))
        rep = None
        if st and st.get('report') and Path(st['report']).exists():
            try:
                rep = json.loads(Path(st['report']).read_text(encoding='utf-8'))
            except ValueError:
                rep = None
        res, detail = False, ''
        if c['kind'] == 'report_valid':
            res = bool(st and st.get('validator') and st['validator']['exit_code'] == 0 and sha(Path(st['report'])) == st['sha256'])
            detail = 'validator exited 0 and the report is unchanged' if res else 'not recorded, validator failed, or the report changed since it was recorded'
        elif rep is None:
            detail = 'no report to read'
        elif c['kind'] == 'field_in':
            v = get(rep, c['path'])
            res, detail = v in c['values'], f'{c["path"]} = {v!r} (allowed {c["values"]})'
        elif c['kind'] == 'field_compare':
            v = get(rep, c['path'])
            res = isinstance(v, (int, float)) and OPS[c['op']](v, c['value'])
            detail = f'{c["path"]} = {v!r}, needs {c["op"]} {c["value"]}'
        elif c['kind'] == 'field_equals_field':
            a, b = get(rep, c['path']), get(rep, c['other'])
            res, detail = a is not None and a == b, f'{c["path"]} = {a!r}, {c["other"]} = {b!r}'
        out.append({'id': c['id'], 'kind': c['kind'], 'step': c.get('step'), 'result': 'pass' if res else 'fail', 'detail': detail})
    return out


def load(state: Path) -> dict:
    return json.loads((state / 'state.json').read_text(encoding='utf-8'))


def save(state: Path, d: dict):
    state.mkdir(parents=True, exist_ok=True)
    (state / 'state.json').write_text(json.dumps(d, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def cfg_gate(d: dict, gid: str) -> dict:
    return next(g for g in d['config']['gates'] if g['id'] == gid)


def gate(d: dict, gid: str) -> dict:
    g = next((g for g in d['gates'] if g['id'] == gid), None)
    if not g:
        raise SystemExit(f'no gate {gid}')
    return g


def current(d: dict) -> dict | None:
    return next((g for g in d['gates'] if g['status'] in ('in_progress', 'criteria_met', 'criteria_failed', 'rejected')), None)


def event(d: dict, kind: str, gid: str, **kw):
    d['events'].append(dict({'at': now(), 'kind': kind, 'gate': gid}, **kw))


def must_be_current(d: dict, gid: str):
    c = current(d)
    if not c or c['id'] != gid:
        raise SystemExit(f'{gid} is not the current gate ({c["id"] if c else "none: the run is finished"}); gates go in order')
    if c['status'] == 'rejected':
        raise SystemExit(f'{gid} was rejected; reopen it first (with who and why)')


def cmd_init(a) -> int:
    cfg = json.loads(a.config.read_text(encoding='utf-8'))
    rid = a.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    st = a.state_dir or Path(tempfile.gettempdir()) / 'qa-orchestrator' / rid
    gates, started = [], False
    for g in cfg['gates']:
        skip = a.start_gate and not started and g['id'] != a.start_gate
        if a.start_gate and g['id'] == a.start_gate:
            started = True
        gates.append({'id': g['id'], 'name': g['name'], 'approver': g.get('approver'), 'status': 'skipped' if skip else 'pending',
                      'steps': [{'id': s['id'], 'skill': s['skill'], 'required': s.get('required', True), 'report': None, 'sha256': None, 'validator': None} for s in g['steps']],
                      'criteria': [], 'decision': {'kind': 'skipped', 'reason': a.skip_reason, 'at': now()} if skip else None, 'history': []})
    if a.start_gate and not a.skip_reason:
        raise SystemExit('--start-gate needs --skip-reason for the gates it skips')
    nxt = next(g for g in gates if g['status'] == 'pending')
    nxt['status'] = 'in_progress'
    d = {'schema_version': '1.0.0', 'skill': {'name': 'qa-orchestrator', 'version': '1.0.0'}, 'run_id': rid, 'subject': a.subject,
         'config_path': a.config.as_posix(), 'config_sha256': sha(a.config), 'config': cfg, 'state_dir': st.as_posix(), 'state_in_temp': a.state_dir is None,
         'skills_root': a.skills_root, 'created_at': now(), 'status': 'running', 'gates': gates, 'events': []}
    event(d, 'init', nxt['id'], subject=a.subject)
    save(st, d)
    print(f'run {rid} for {a.subject!r}: state in {st}; current gate {nxt["id"]} {nxt["name"]}')
    return 0


def cmd_next(a) -> int:
    d = load(a.state)
    c = current(d)
    if not c:
        print(f'run {d["run_id"]}: {d["status"]}; no gate in progress'); return 0
    cg = cfg_gate(d, c['id'])
    print(f'gate {c["id"]} {c["name"]} ({c["status"]}); approver: {c["approver"]}')
    for s, cs in zip(c['steps'], cg['steps']):
        done = s['validator'] and s['validator']['exit_code'] == 0
        print(f'  [{"x" if done else " "}] {s["id"]}: {cs["skill"]} - {cs.get("purpose", "")}' + ('' if done else f'\n      run the skill (its SKILL.md), then: record --gate {c["id"]} --step {s["id"]} --report <its report>'
                                                                                                + (f'\n      hint: {cs["command"]}' if cs.get('command') else '')))
    if all(s['validator'] and s['validator']['exit_code'] == 0 for s in c['steps'] if s['required']) and c['status'] == 'in_progress':
        print(f'  all steps recorded: evaluate --gate {c["id"]}')
    if c['status'] in ('criteria_met', 'criteria_failed'):
        print(f'  criteria {c["status"].split("_")[1]}: ask {c["approver"]} to approve' + (' (an override reason is needed)' if c['status'] == 'criteria_failed' else '') + ' or reject')
    return 0


def cmd_record(a) -> int:
    d = load(a.state)
    must_be_current(d, a.gate)
    g = gate(d, a.gate)
    s = next((x for x in g['steps'] if x['id'] == a.step), None) or sys.exit(f'no step {a.step} in {a.gate}')
    cs = next(x for x in cfg_gate(d, a.gate)['steps'] if x['id'] == a.step)
    rp = a.report
    if not rp.exists():
        raise SystemExit(f'{rp} does not exist: run the step\'s skill first')
    cwd = cs['cwd'].format(skills=d['skills_root']) if cs.get('cwd') else None
    rel = Path(os.path.relpath(rp, cwd)).as_posix() if cwd else rp.as_posix()
    skills = Path(os.path.relpath(d['skills_root'], cwd)).as_posix() if cwd else d['skills_root']
    cmd = cs['validator'].format(report=shlex.quote(rel), skills=skills)
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=cwd)
    out = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-3:]
    s.update(report=rp.as_posix(), sha256=sha(rp), validator={'command': cmd, 'cwd': cwd, 'exit_code': r.returncode, 'output': out, 'at': now()})
    if g['status'] != 'in_progress':
        g['status'] = 'in_progress'
        g['criteria'] = []
    event(d, 'record', a.gate, step=a.step, report=rp.as_posix(), exit_code=r.returncode)
    save(a.state, d)
    print(f'{a.gate}/{a.step}: {rp.as_posix()} recorded; validator exit {r.returncode}' + (f': {out[-1]}' if out else ''))
    return 0 if r.returncode == 0 else 1


def cmd_evaluate(a) -> int:
    d = load(a.state)
    must_be_current(d, a.gate)
    g = gate(d, a.gate)
    missing = [s['id'] for s in g['steps'] if s['required'] and not s['report']]
    if missing:
        raise SystemExit(f'{a.gate}: steps not recorded: {", ".join(missing)}')
    cg = cfg_gate(d, a.gate)
    g['criteria'] = evaluate_criteria(g, cg)
    ok = all(c['result'] == 'pass' for c in g['criteria'])
    g['status'] = 'criteria_met' if ok else 'criteria_failed'
    event(d, 'evaluate', a.gate, result=g['status'])
    if ok and cg.get('auto_approve'):
        g['decision'] = {'kind': 'auto_approved', 'by': 'config: auto_approve', 'how': d['config_path'], 'at': now()}
        g['status'] = 'approved'
        event(d, 'auto_approve', a.gate)
        advance(d, g)
    save(a.state, d)
    for c in g['criteria']:
        print(f'  {c["result"]:4} {c["id"]}: {c["detail"]}')
    print(f'{a.gate}: {g["status"]}')
    return 0


def advance(d: dict, g: dict):
    idx = [x['id'] for x in d['gates']].index(g['id'])
    rest = [x for x in d['gates'][idx + 1:] if x['status'] == 'pending']
    if rest:
        rest[0]['status'] = 'in_progress'
        event(d, 'start', rest[0]['id'])
    else:
        d['status'] = 'finished'


def cmd_approve(a) -> int:
    d = load(a.state)
    must_be_current(d, a.gate)
    g = gate(d, a.gate)
    if g['status'] not in ('criteria_met', 'criteria_failed'):
        raise SystemExit(f'{a.gate} is {g["status"]}: evaluate its criteria before approving')
    if len((a.by or '').strip()) < 3 or len((a.how or '').strip()) < 3:
        raise SystemExit('an approval needs who (--by) and how (--how)')
    if g['status'] == 'criteria_failed' and len((a.override or '').strip()) < 20:
        raise SystemExit(f'{a.gate} has failed criteria: approving needs --override with the reason they are accepted')
    for st in g['steps']:
        if st['report'] and sha(Path(st['report'])) != st['sha256']:
            raise SystemExit(f'{st["report"]} changed since it was recorded: record it again')
    g['decision'] = {'kind': 'approved_with_override' if g['status'] == 'criteria_failed' else 'approved', 'by': a.by, 'how': a.how, 'at': now(),
                     'override': a.override if g['status'] == 'criteria_failed' else None,
                     'failed_criteria': [c['id'] for c in g['criteria'] if c['result'] == 'fail']}
    g['status'] = 'approved'
    event(d, 'approve', a.gate, by=a.by, override=bool(a.override))
    advance(d, g)
    save(a.state, d)
    print(f'{a.gate} {g["decision"]["kind"]} by {a.by}; ' + (f'current gate {current(d)["id"]}' if current(d) else f'run {d["status"]}'))
    return 0


def cmd_reject(a) -> int:
    d = load(a.state)
    c = current(d)
    if not c or c['id'] != a.gate:
        raise SystemExit(f'{a.gate} is not the current gate')
    if len((a.reason or '').strip()) < 20 or len((a.by or '').strip()) < 3:
        raise SystemExit('a rejection needs who (--by) and a reason (--reason)')
    c['decision'] = {'kind': 'rejected', 'by': a.by, 'how': a.how, 'at': now(), 'reason': a.reason,
                     'failed_criteria': [x['id'] for x in c['criteria'] if x['result'] == 'fail']}
    c['status'] = 'rejected'
    d['status'] = 'stopped'
    event(d, 'reject', a.gate, by=a.by)
    save(a.state, d)
    print(f'{a.gate} rejected by {a.by}; the run is stopped until the gate is reopened')
    return 0


def cmd_reopen(a) -> int:
    d = load(a.state)
    g = gate(d, a.gate)
    if g['status'] != 'rejected':
        raise SystemExit(f'{a.gate} is {g["status"]}, not rejected')
    if len((a.reason or '').strip()) < 20:
        raise SystemExit('reopening needs who and why')
    g['history'].append(g['decision'])
    g['decision'] = None
    g['status'] = 'in_progress'
    g['criteria'] = []
    d['status'] = 'running'
    event(d, 'reopen', a.gate, by=a.by, reason=a.reason)
    save(a.state, d)
    print(f'{a.gate} reopened')
    return 0


def cmd_status(a) -> int:
    d = load(a.state)
    print(f'run {d["run_id"]} ({d["status"]}): {d["subject"]}')
    for g in d['gates']:
        dec = g.get('decision') or {}
        print(f'  {g["id"]} {g["name"]:22} {g["status"]:16} ' + (f'{dec.get("kind")} by {dec.get("by")}' if dec.get('by') else dec.get('reason') or ''))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init'); p.add_argument('--config', type=Path, default=HERE.parent / 'data' / 'orchestrator.json'); p.add_argument('--subject', required=True)
    p.add_argument('--state-dir', type=Path); p.add_argument('--run-id'); p.add_argument('--start-gate'); p.add_argument('--skip-reason')
    p.add_argument('--skills-root', default=Path(os.path.relpath(HERE.parent.parent)).as_posix(), help='where the other skills are, relative to the working directory')
    for name in ('next', 'status'):
        q = sub.add_parser(name); q.add_argument('--state', type=Path, required=True)
    q = sub.add_parser('record'); q.add_argument('--state', type=Path, required=True); q.add_argument('--gate', required=True); q.add_argument('--step', required=True); q.add_argument('--report', type=Path, required=True)
    q = sub.add_parser('evaluate'); q.add_argument('--state', type=Path, required=True); q.add_argument('--gate', required=True)
    q = sub.add_parser('approve'); q.add_argument('--state', type=Path, required=True); q.add_argument('--gate', required=True); q.add_argument('--by', required=True)
    q.add_argument('--how', required=True); q.add_argument('--override')
    q = sub.add_parser('reject'); q.add_argument('--state', type=Path, required=True); q.add_argument('--gate', required=True); q.add_argument('--by', required=True)
    q.add_argument('--how', default=''); q.add_argument('--reason', required=True)
    q = sub.add_parser('reopen'); q.add_argument('--state', type=Path, required=True); q.add_argument('--gate', required=True); q.add_argument('--by', required=True); q.add_argument('--reason', required=True)
    a = ap.parse_args()
    return {'init': cmd_init, 'next': cmd_next, 'record': cmd_record, 'evaluate': cmd_evaluate, 'approve': cmd_approve, 'reject': cmd_reject,
            'reopen': cmd_reopen, 'status': cmd_status}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
