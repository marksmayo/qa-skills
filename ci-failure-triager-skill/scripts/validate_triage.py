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
stable or fine. run_status is not_run when no job ran (the headline then says there is nothing to triage) and
partial when a log could not be downloaded; a check over nothing scores 0 in the evidence completeness. An
agent may name the test of a step failure (`test_id_source: "agent"`) only when the log lines it quotes show
that test. Local repeat runs (local_runs.json) must each give the command, the environment and the counts.

The triage is checked against schemas/triage.schema.json first (required fields, types, enumerations, the shape of a
draft); a triage that fails it is not checked further. Failures that are one defect on several jobs (the legs of a
matrix; extract_failures.py groups them) carry their `group`, get one draft (on one member, `covers` listing the
others; a draft per failure is still accepted from a 1.2.0 or 1.3.0 triage), and name the same owning change: a leg
left not_identified while another leg of its group names a commit is an error. The steps a failed step skipped and
the jobs the CI skipped (`checks_not_run`) must be in the triage, in that field (required from 1.4.0) or named in
the limitations. A run with no job whose commit's other workflows concluded the same way must say so, and so must a
triage whose run has no last green run because the default branch was never searched. summary.evidence_checks names
the five checks behind the evidence completeness.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from extract_failures import SECRET, TEST_STEP, checks_not_run, extract, failure_groups, redact, strip  # noqa: E402
from classify import decide, local_run_problem, local_runs, signals  # noqa: E402
from fetch_run import last_green_hint, same_way  # noqa: E402

SCHEMA_VERSION = '1.4.0'
ACCEPTED_SCHEMAS = ('1.2.0', '1.3.0', '1.4.0')   # 1.3.0 and 1.4.0 add optional fields only
SCHEMA = HERE.parent / 'schemas' / 'triage.schema.json'


def _is(v, t) -> bool:
    if isinstance(t, list):
        return any(_is(v, x) for x in t)
    num = isinstance(v, (int, float)) and not isinstance(v, bool)
    return {'object': isinstance(v, dict), 'array': isinstance(v, list), 'string': isinstance(v, str), 'boolean': isinstance(v, bool),
            'integer': isinstance(v, int) and not isinstance(v, bool), 'number': num, 'null': v is None}.get(t, True)


def schema_errors(v, sch: dict, path: str = '$') -> list[str]:
    """The part of JSON Schema that schemas/triage.schema.json uses: type (one or several), required, properties, items,
    enum, const, minLength, minimum, if/then/else and allOf. A value of the wrong type is not looked into further."""
    if 'type' in sch and not _is(v, sch['type']):
        t = sch['type']
        return [f'{path}: should be {" or ".join(t) if isinstance(t, list) else t}, is {type(v).__name__}']
    out = []
    if 'const' in sch and v != sch['const']:
        out.append(f'{path}: {v!r} should be {sch["const"]!r}')
    if 'enum' in sch and v not in sch['enum']:
        out.append(f'{path}: {v!r} is not one of {", ".join(map(repr, sch["enum"]))}')
    if isinstance(v, str) and len(v.strip()) < sch.get('minLength', 0):
        out.append(f'{path}: needs at least {sch["minLength"]} character(s)')
    if 'minimum' in sch and isinstance(v, (int, float)) and not isinstance(v, bool) and v < sch['minimum']:
        out.append(f'{path}: {v} is below {sch["minimum"]}')
    if isinstance(v, dict):
        out += [f'{path}: missing {k}' for k in sch.get('required', []) if k not in v]
        for k, sub in sch.get('properties', {}).items():
            if k in v:
                out += schema_errors(v[k], sub, f'{path}.{k}')
    if isinstance(v, list) and 'items' in sch:
        for i, x in enumerate(v):
            out += schema_errors(x, sch['items'], f'{path}[{i}]')
    if 'if' in sch:
        branch = 'then' if not schema_errors(v, sch['if'], path) else 'else'
        if branch in sch:
            out += schema_errors(v, sch[branch], path)
    for sub in sch.get('allOf', []):
        out += schema_errors(v, sub, path)
    return out


def vnum(version: str) -> tuple:
    return tuple(int(x) for x in str(version).split('.') if x.isdigit())
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
    checks = evidence_checks(run, run_dir, changes, failures)
    return round(sum(1 for v in checks.values() if v is True) / len(checks), 2)


def evidence_checks(run: dict, run_dir: Path, changes: dict | None, failures: list[dict] | None = None) -> dict:
    """The five checks behind the evidence completeness, by name, each True, False or 'not_applicable' (nothing to check:
    it earns nothing, and the report says which it was, so a decisive log with no test step reads differently from no log
    at all). Five checks: logs for every failed job (or, for a job that never started on a runner, the CI's annotation on it); test results (JUnit, or a test runner's console summary)
    for every failed job with a test step; a re-run; a last green run; the repository for the owning change."""
    first = run['attempts'][0]
    failed = [j for j in first['jobs'] if j['conclusion'] not in ('success', 'skipped', None)]
    if failures is None:
        failures = extract(run_dir, run, 1)
    with_tests = {f['job_id'] for f in failures if f['kind'] == 'test'}
    test_jobs = [j for j in failed if any(TEST_STEP.search(s['name']) for s in j['steps'])
                 or any(f['job_id'] == j['id'] and f.get('step_kind') == 'test' for f in failures)]
    # a check over nothing earns nothing: a run with no failed job has no log to read, and one with no test job (a job
    # that never started, a lint-only failure) no test result
    # a job that never started on a runner wrote no log; the CI's check-run annotation on it is its only record, and when
    # there is one it stands in for the log (it says why the job did not run)
    return {'logs': 'not_applicable' if not failed else all((j.get('log') and (run_dir / j['log']).exists()) or (j.get('never_started') and j.get('annotations'))
                                                            for j in failed),
            'test_results': 'not_applicable' if not test_jobs else all(bool(j.get('junit')) or j['id'] in with_tests for j in test_jobs),
            'rerun': len(run['attempts']) > 1, 'last_green': bool(run.get('last_green')), 'repository': bool(changes and changes.get('commits') is not None)}


def annotation_texts(run: dict) -> list[str]:
    """The annotation messages of the first attempt's jobs that never started on a runner."""
    jobs = run['attempts'][0]['jobs'] if run.get('attempts') else []
    return [a['message'] for j in jobs if j.get('never_started') for a in j.get('annotations', []) if a.get('message')]


def squash(t: str) -> str:
    return ' '.join((t or '').split()).lower()


def expected_run_status(run: dict) -> tuple[str, str | None]:
    """not_run when the first attempt ran no job (a run that concluded failure at startup: a workflow file error, a
    fork pull request never approved); partial when a failed job's log could not be downloaded; else complete."""
    jobs = run['attempts'][0]['jobs'] if run.get('attempts') else []
    if not [j for j in jobs if j.get('conclusion') not in ('skipped', None) and not j.get('never_started')]:
        said = annotation_texts(run)
        if said:
            return 'not_run', f'no job started on a runner; the CI says: "{said[0]}"'
        return 'not_run', f'the run concluded {run["run"].get("conclusion") or "failure"} but no job ran'
    if any(p.get('kind') == 'log' for p in run.get('fetch_problems', [])):
        return 'partial', 'the log of a failed job could not be downloaded'
    return 'complete', None


NOTHING = re.compile(r'(?i)\b(nothing to triage|no jobs? (ran|were run|was run|started))\b')


def summarise(d: dict, run: dict, run_dir: Path, changes: dict | None, failures: list[dict] | None = None) -> dict:
    by = {c: sum(1 for f in d['failures'] if f['classification'] == c) for c in CLASSES}
    checks = evidence_checks(run, run_dir, changes, failures)
    return {'by_classification': by, 'defects_drafted': sum(1 for f in d['failures'] if f.get('draft_defect')),
            'reruns': len(d.get('reruns', [])), 'evidence_completeness': round(sum(1 for v in checks.values() if v is True) / len(checks), 2),
            'evidence_checks': checks}


def found_file(name: str, triage: Path) -> Path | None:
    """A files_written entry, resolved against the working directory, then the triage's folder and up to three above it
    (an example triage lists paths relative to its skill folder)."""
    for base in [Path.cwd(), *list(triage.resolve().parents)[:4]]:
        p = base / name
        if p.is_file():
            return p
    return None


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
    # the contract first: a triage that is not the shape the schema gives is not checked further (with --write-summary the
    # computed summary fields are still to be written, so only they may be missing)
    shape = schema_errors(d, json.loads(SCHEMA.read_text(encoding='utf-8')))
    if a.write_summary:
        shape = [x for x in shape if not (x.startswith('$.summary: missing ') and x.split()[-1] != 'headline')]
    if shape:
        print(f'{a.triage}: {len(shape)} problem(s) against schemas/triage.schema.json')
        for x in shape[:40]:
            print('  - schema:', x)
        return 1
    new = vnum(d['schema_version']) >= (1, 4, 0)    # 1.4.0 triages carry groups, checks_not_run and evidence_checks
    if d['schema_version'] not in ACCEPTED_SCHEMAS:
        e(f'schema_version {d["schema_version"]} should be {SCHEMA_VERSION} (or {", ".join(ACCEPTED_SCHEMAS[:-1])})')
    if d['run']['id'] != run['run']['id'] or d['run']['commit'] != run['run']['commit']:
        e('run id or commit differ from run.json')
    if (d['run'].get('last_green') or {}).get('id') != (run.get('last_green') or {}).get('id'):
        e('run.last_green differs from run.json')

    # run status: a run with no job is its own outcome, not an empty triage that looks complete
    want_status, why_status = expected_run_status(run)
    if d.get('run_status') != want_status:
        e(f'run_status {d.get("run_status")!r} should be {want_status!r}' + (f' ({why_status})' if why_status else ''))
    if want_status != 'complete' and len(d.get('run_status_reason') or '') < 10:
        e(f'run_status {want_status} needs a run_status_reason')
    if want_status == 'not_run' and not NOTHING.search(d['summary'].get('headline', '')):
        e('no job ran: the headline must say there is nothing to triage (no job ran)')
    said = annotation_texts(run)
    if want_status == 'not_run' and said and not any(squash(s)[:40] in squash(d.get('run_status_reason')) for s in said):
        e(f'no job started on a runner and the CI explains why in a job annotation; run_status_reason must quote it '
          f'(at least its first 40 characters): "{said[0][:120]}"')

    # local repeat runs recorded by the agent
    for k, r in enumerate(local_runs(a.run), 1):
        why = local_run_problem(r)
        if why:
            e(f'local_runs.json record {k}: {why}; it is not counted as evidence')

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
    said_all = squash(' '.join([d['summary'].get('headline') or '', d.get('run_status_reason') or ''] + d['limitations']))

    # a run with no job: the commit's other workflows that stopped the same way are the only clue to the cause
    same = same_way(run) if want_status == 'not_run' else []
    if same and not re.search(r'other (\w+ )?workflows?', said_all):
        e(f'no job ran, and {len(same)} other workflow run(s) of this commit concluded {run["run"].get("conclusion")} on the same event '
          f'({", ".join(str(x["workflow"]) for x in same)}): the reason or a limitation must say that other workflows stopped the same way '
          '(a cause common to them is likelier than one of this workflow alone)')
    # no last green run because the default branch was never searched: there is no range of changes, and the triage says why
    hint = last_green_hint(run) if want and run.get('system') == 'github' else None
    if hint and 'default branch' not in said_all:
        e(f'{hint}; until then a limitation must say the default branch was not searched for a last green run')

    # what the failures kept from running: named in the triage, never passed over
    nr = checks_not_run(run, 1)
    has_nr = bool(nr['steps'] or nr['jobs'])
    if 'checks_not_run' in d and d['checks_not_run'] != nr and (has_nr or d['checks_not_run'] != {'steps': [], 'jobs': []}):
        e(f'checks_not_run differs from the run: it should be {json.dumps(nr, ensure_ascii=False)}')
    elif has_nr and 'checks_not_run' not in d:
        unnamed = [f'step "{n}" of job {x["job"]}' for x in nr['steps'] for n in x['steps'] if squash(n) not in said_all] + \
                  [f'job {j}' for j in nr['jobs'] if squash(j) not in said_all]
        if new:
            e(f'{sum(len(x["steps"]) for x in nr["steps"])} step(s) after a failed step and {len(nr["jobs"])} skipped job(s) never ran: a {SCHEMA_VERSION} '
              f'triage carries them as checks_not_run: {json.dumps(nr, ensure_ascii=False)}')
        elif unnamed:
            e(f'checks that never ran on this commit are not named in the limitations ({"; ".join(unnamed[:8])}'
              + (f' and {len(unnamed) - 8} more' if len(unnamed) > 8 else '') + '): name each, or carry them as checks_not_run')

    # one defect on several jobs: the groups the extractor gives
    groups = failure_groups(want)
    in_group = {i: g for g in groups for i in g['failures']}
    if new and groups and d.get('failure_groups') != groups:
        e(f'failure_groups differs from the run: it should be {json.dumps(groups, ensure_ascii=False)}')
    if 'failure_groups' in d and not groups and d['failure_groups']:
        e('failure_groups lists a group the run does not have')
    got = {}
    for f in d['failures']:
        if f['id'] in got:
            e(f'{f["id"]} triaged twice')
        got[f['id']] = f
    for w in want:
        f = got.get(w['id'])
        if not f:
            e(f'{w["id"]} ({w["test_id"] or w["step"]}) not triaged'); continue
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
        # the test: the extractor's, or one the agent names for a step failure from the log lines it quotes
        named = f.get('test_id_source') == 'agent' and w['test_id'] is None and f.get('test_id') and f.get('kind') == 'test'
        if named:
            leaf = str(f['test_id']).split(' > ')[-1]
            if f.get('job') != w['job'] or not quoted_ok or not any(leaf in l['text'] for l in lx.get('lines') or []):
                e(f'{w["id"]}: test {f["test_id"]!r} named by the agent is not in the quoted log lines (quote the line that shows it failing)')
        elif (f.get('test_id'), f.get('job'), f.get('kind')) != (w['test_id'], w['job'], w['kind']):
            e(f'{w["id"]}: test, job or kind differ from the run')
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
        if fc == 'flaky' and not (sg['rerun'] == 'passed' or sg['history_passed_same_commit'] or sg.get('local_flaky')):
            e(f'{w["id"]}: flaky needs a pass on the same commit (re-run {sg["rerun"]}, no run of this workflow on this commit where job {w["job"]} passed, '
              'and no local repeat runs on this commit that both passed and failed)')
        if fc == 'real' and sg['rerun'] == 'passed':
            e(f'{w["id"]}: real, but it passed on re-run')
        if fc == 'real' and f['confidence'] == 'confirmed' and (sg['rerun'] != 'failed' or not owner or owner.get('status') != 'identified'):
            e(f'{w["id"]}: real is confirmed only when it failed again on re-run and an owning change is identified')
        if fc == 'environment' and not (envsig or sg['step_kind'] == 'setup'):
            e(f'{w["id"]}: environment needs a signature in the log or a failed setup step (this is a {sg["step_kind"]} step)')
        if fc == 'data' and not (datasig or sg['date_markers'] or sgq['date_markers']):
            e(f'{w["id"]}: data needs a data signature or a date before the run date')
        if 'annotations' in f['signals'] and f['signals']['annotations'] != sg.get('annotations', []):
            e(f'{w["id"]}: signals.annotations differ from the job\'s annotations in the run (quote them as recorded)')
        if f['signals'].get('queued_for_s') != sg.get('queued_for_s') and ('queued_for_s' in f['signals'] or 'queued_for_s' in sg):
            e(f'{w["id"]}: signals.queued_for_s {f["signals"].get("queued_for_s")} should be {sg.get("queued_for_s")} (the time the job waited '
              'for a runner, from the run)')
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
        # the same defect on another job has one owner: a leg left not_identified beside a leg that names a commit is an
        # inconsistency (the extractor read no file on this leg, or the agent set only one), never a finding
        g = in_group.get(w['id'])
        mates = [got[i] for i in (g['failures'] if g else []) if i != w['id'] and i in got]
        if g and (new or 'group' in f) and f.get('group') != g['id']:
            e(f'{w["id"]}: group {f.get("group")!r} should be {g["id"]!r} ({g["basis"]})')
        if not g and f.get('group'):
            e(f'{w["id"]}: group {f["group"]!r}, but the run puts this failure in no group')
        if oc.get('status') != 'identified':
            named = next((m for m in mates if (m.get('owning_change') or {}).get('status') == 'identified'), None)
            if named:
                e(f'{w["id"]}: owning change not_identified, but {named["id"]}, the same failure on another job (group {g["id"]}), names '
                  f'{str(named["owning_change"].get("commit"))[:8]}: one defect has one owner; name it here too '
                  '(owning_change.override_reason says why changes.json did not find it on this job)')
        # drafts: one per defect. In a group the draft sits on one member and covers the others
        dk = DRAFT_KIND.get(fc)
        dd = f.get('draft_defect')
        peers = [m for m in mates if m.get('classification') == fc]
        covered_by = next((m for m in peers if w['id'] in ((m.get('draft_defect') or {}).get('covers') or [])), None)
        if dd:
            stray = [i for i in dd.get('covers') or [] if i not in {m['id'] for m in peers}]
            if stray:
                e(f'{w["id"]}: its draft covers {", ".join(stray)}, which is not another failure of its group with the same classification')
            if new and dk and peers and (covered_by or any(m.get('draft_defect') for m in peers if m['id'] < w['id'])):
                e(f'{w["id"]}: group {g["id"]} is one defect on {len(g["jobs"])} jobs and gets one draft (covers lists the other failures), '
                  'not a draft per job')
        if dk and not dd and not covered_by:
            e(f'{w["id"]}: a {fc} failure needs a draft ({dk})' + (f', or a draft on another failure of its group {g["id"]} that lists it in covers' if peers else ''))
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

    # files written: each listed file exists, and a drafts file is the renderer's (it writes none when there is no draft)
    drafted = [f for f in d['failures'] if f.get('draft_defect')]
    for name in d.get('files_written') or []:
        p = found_file(name, a.triage)
        if not p:
            e(f'files_written lists {name}, which does not exist (relative to the working directory or the triage\'s folders)')
        elif p.resolve() == a.triage.resolve() or p.suffix.lower() not in ('.md', '.markdown'):
            continue
        elif not drafted:
            e(f'files_written lists {name}, but the triage has no draft: render_report.py --defects-md writes no drafts file for it')
        else:
            md = p.read_text(encoding='utf-8')
            missing = [f['id'] for f in drafted if f['draft_defect'].get('title') and f['draft_defect']['title'] not in md]
            if missing:
                e(f'files_written lists {name}, which lacks the draft(s) of {", ".join(missing)}: write it again with render_report.py --defects-md')

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
        if k == 'evidence_checks' and not new and k not in d['summary']:
            continue        # a 1.2.0 or 1.3.0 triage need not carry it
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
