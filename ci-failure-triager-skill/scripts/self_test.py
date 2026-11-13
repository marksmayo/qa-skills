#!/usr/bin/env python3
"""Self-test for the CI failure triager (stdlib only; nothing leaves loopback).

    python scripts/self_test.py

1. fetch_run.py --self-test: two loopback servers; the token is dropped on a cross-host redirect (as to
   blob storage) and kept on a same-host one; failed log and artifact downloads are recorded, not lost.
2. extract_failures.py --self-test: step groups found by title or by error order, the echoed script
   skipped, ANSI stripped, Vitest/Jest/node:test console failures, an aggregate gate, a missing log.
3. A synthetic GitHub run, normalised from raw responses, extracted, classified, triaged and validated,
   then seeded with violations the validator must catch. The run has: a Vitest assertion failure, a
   publish step whose network error is far above its last error line (the agent quotes it), a toolchain
   version below a tool's minimum, a job whose log download failed, an aggregate gate job, a second
   attempt someone else started that re-ran only the publish job, and a run of the same workflow on the
   same commit that ran a different job (which must not make anything flaky).
"""
from __future__ import annotations
import copy, json, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_run, extract_failures  # noqa: E402
from classify import decide, signals  # noqa: E402
from validate_triage import expected_run_status  # noqa: E402

T = '2026-03-02T10:00:00.0000000Z '
SHA, GREEN = 'c' * 40, 'b' * 40


def log(groups) -> str:
    out = [T + '##[group]Runner Image', T + 'Image: ubuntu', T + '##[endgroup]']
    for title, script, body in groups:
        out += [T + '##[group]Run ' + title] + [T + '\x1b[36;1m' + s + '\x1b[0m' for s in script] + [T + 'shell: /usr/bin/bash -e {0}', T + '##[endgroup]']
        out += [T + b for b in body]
    return '\n'.join(out + [T + 'Post job cleanup.', ''])


DONE = '##[error]Process completed with exit code 1.'
LOGS = {
    11: log([('pnpm install --frozen-lockfile', ['pnpm install --frozen-lockfile'], ['Done in 2s']),
             ('pnpm test', ['pnpm test'], [' \x1b[32m✓\x1b[39m unit tests/unit/labels.test.ts (3 tests)', '',
              '\x1b[41m FAIL \x1b[49m  tests/unit/quotes.test.ts > quotes > rounds a half fare up',
              "\x1b[31mAssertionError\x1b[39m: expected '12.12' to be '12.13'", '',
              ' ❯ tests/unit/quotes.test.ts:18:20', '', '⎯⎯⎯⎯⎯⎯[1/1]⎯', '', ' Test Files  1 failed | 1 passed (2)', DONE])]),
    12: log([('pnpm exec release-tool publish', ['pnpm exec release-tool publish'],
              ['Publishing 2 packages', 'Error: request to https://registry.example.test/@acme%2fone failed, reason: socket hang up']
              + [f'retrying ({i}/45)' for i in range(1, 46)] + ['Error: publish aborted after retries', DONE])]),
    13: log([('set -euo pipefail', ['set -euo pipefail', 'eval "$BUILD"'],
              ["Running build task 'assembleRelease'...", 'FAILURE: Build failed with an exception.', '* What went wrong:',
               "> Error: Your project's plugin version (2.1.0) is lower than the tool's minimum supported version of 2.1.1.", 'BUILD FAILED in 40s', DONE])]),
    15: log([('failed="$(jq -r \'[to_entries[] | select(.value.result != "success")]\' <<< "$RESULTS")"',
              ['failed="$(jq -r \'[to_entries[] | select(.value.result != "success")]\' <<< "$RESULTS")"', '[ -z "$failed" ] || { echo "::error::$failed"; exit 1; }'],
              ['##[error]unit failure', DONE])]),
    22: log([('pnpm exec release-tool publish', ['pnpm exec release-tool publish'], ['Publishing 2 packages', 'published 2 packages'])]),
}
STEP = {'unit': 'Unit tests', 'publish': 'Publish packages', 'build-app': 'Build the app', 'lint': 'Lint', 'gate': 'Require every job to pass'}


def job(i, name, concl, started):
    return {'id': i, 'name': name, 'conclusion': concl, 'started_at': started, 'html_url': f'https://ci.example.test/job/{i}',
            'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': STEP[name], 'conclusion': concl}]}


def build_raw(raw: Path):
    s = fetch_run.save
    s(raw / 'run.json', {'id': 700, 'head_sha': SHA, 'head_branch': 'main', 'event': 'schedule', 'run_attempt': 2, 'workflow_id': 5,
                         'path': '.github/workflows/nightly.yml', 'created_at': '2026-03-02T10:00:00Z', 'run_started_at': '2026-03-02T11:00:00Z',
                         'conclusion': 'failure', 'html_url': 'https://ci.example.test/run/700', 'repository': {'full_name': 'acme/fares'},
                         'triggering_actor': {'login': 'maintainer-a'}})
    a1 = [job(11, 'unit', 'failure', 'T1'), job(12, 'publish', 'failure', 'T1'), job(13, 'build-app', 'failure', 'T1'),
          job(14, 'lint', 'failure', 'T1'), job(15, 'gate', 'failure', 'T1')]
    a2 = [dict(j, id=j['id'] + 10) for j in a1]
    a2[1] = dict(job(22, 'publish', 'success', 'T2'))
    s(raw / 'attempts' / '1' / 'jobs.json', {'jobs': a1})
    s(raw / 'attempts' / '2' / 'jobs.json', {'jobs': a2})
    s(raw / 'attempts' / '1' / 'run.json', {'triggering_actor': {'login': 'scheduler'}})
    s(raw / 'attempts' / '2' / 'run.json', {'triggering_actor': {'login': 'maintainer-a'}})
    for k, text in LOGS.items():
        s(raw / 'logs' / f'{k}.txt', text)
    for k in (21, 23, 25):
        s(raw / 'logs' / f'{k}.txt', LOGS[k - 10])  # carried over: GitHub lists them again in attempt 2
    s(raw / 'logs' / '14.error.json', {'status': 'fetch_failed', 'http_status': 401, 'error': 'HTTP 401: Server failed to authenticate the request'})
    s(raw / 'logs' / '24.error.json', {'status': 'fetch_failed', 'http_status': 401, 'error': 'HTTP 401: Server failed to authenticate the request'})
    wr = lambda i, sha, c, t, ev, wid=5: {'id': i, 'head_sha': sha, 'conclusion': c, 'run_started_at': t, 'event': ev, 'workflow_id': wid}  # noqa: E731
    s(raw / 'history.json', {'workflow_runs': [wr(700, SHA, 'failure', '2026-03-02T10:00:00Z', 'schedule'),
                                               wr(701, SHA, 'success', '2026-03-02T09:00:00Z', 'push'),
                                               wr(702, SHA, 'success', '2026-03-02T09:30:00Z', 'push', wid=6),
                                               wr(650, GREEN, 'success', '2026-03-01T10:00:00Z', 'schedule')]})
    s(raw / 'history_jobs' / '701.json', {'jobs': [{'name': 'version', 'conclusion': 'success'}]})


def triage_from(run: dict, cands: list[dict]) -> dict:
    fails = []
    for c in cands:
        f = c['failure']
        cls = c['classification']
        t = {'id': f['id'], 'job': f['job'], 'step': f['step'], 'kind': f['kind'], 'test_id': f['test_id'], 'message': f['message'],
             'classification': cls, 'confidence': c['confidence'], 'rule': c['rule'],
             'signals': {k: c['signals'][k] for k in ('signatures', 'date_markers', 'rerun', 'step_kind', 'consequence_of', 'history_same_commit')},
             'log_excerpt': f['log_excerpt'], 'owning_change': {'status': 'not_identified', 'basis': 'no repository in this test'},
             'priority': 'P1' if cls == 'real' else None, 'next_action': 'see the rule'}
        if cls == 'real':
            t['draft_defect'] = {'kind': 'defect', 'title': f'{f["job"]} fails', 'body': {'summary': 's', 'steps': ['x'], 'expected': 'e', 'actual': 'a', 'evidence': ['l']}}
        fails.append(t)
    r = run['run']
    status, why = expected_run_status(run)
    return {'schema_version': '1.2.0', 'skill': {'name': 'ci-failure-triager', 'version': '1.2.0'}, 'generated_at': '2026-03-02T12:00:00Z',
            'run_status': status, **({'run_status_reason': why} if why else {}),
            'run': {'system': 'github', 'repo': run['repo'], 'id': r['id'], 'attempt': r['attempt'], 'commit': r['commit'], 'branch': r['branch'],
                    'started_at': r['started_at'], 'last_green': {'id': run['last_green']['id'], 'commit': run['last_green']['commit']} if run.get('last_green') else None, 'source': 'recorded'},
            'reruns': json.loads((RUN / 'reruns.json').read_text(encoding='utf-8'))['reruns'], 'failures': fails,
            'summary': {'headline': 'Five failures: one test assertion, a registry outage, a toolchain bump, a job with no log, and the gate.'},
            'limitations': ['The log of job lint could not be downloaded (fetch_failed, HTTP 401), so its failure is unclassified.'],
            'files_written': []}


def validate(t: dict, where: Path) -> tuple[int, str]:
    p = where / 'triage.json'
    p.write_text(json.dumps(t, indent=1, ensure_ascii=False), encoding='utf-8')
    r = subprocess.run([sys.executable, str(HERE / 'validate_triage.py'), str(p), '--run', str(RUN)], capture_output=True, text=True, encoding='utf-8')
    return r.returncode, r.stdout + r.stderr


def go_signature_checks(fails: list) -> None:
    """Go and lint failures, extracted from synthetic logs, must reach the class their evidence gives."""
    import io, contextlib
    run = {'run': {'id': 1, 'commit': SHA, 'started_at': '2026-03-02T10:00:00Z'}, 'attempts': [{'n': 1, 'jobs': []}], 'history': []}
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / 'logs').mkdir()
        rjobs = []
        for i, (name, (script, step, body)) in enumerate(extract_failures.GO_LOGS.items(), 30):
            (d / 'logs' / f'{i}.txt').write_text(extract_failures._gh_log([(script, [script], body)]), encoding='utf-8')
            rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched',
                          'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': step, 'conclusion': 'failure'}]})
        run['attempts'][0]['jobs'] = rjobs
        got = {}
        with contextlib.redirect_stderr(io.StringIO()):
            for f in extract_failures.extract(d, run, 1):
                sg = signals(f, run, d)
                got[f['job']] = decide(sg, None)[:2] + (sorted(h['id'] for h in sg['signatures']),)
    want = {'go-unit': ('real', 'indicated', []), 'go-timeout': ('unclassified', 'low', []), 'go-build': ('real', 'indicated', ['REAL-COMPILE']),
            'go-json': ('real', 'indicated', []), 'go-race': ('real', 'indicated', ['REAL-RACE']), 'go-lint': ('real', 'indicated', ['REAL-LINT']),
            'go-lint-annotation': ('real', 'indicated', ['REAL-LINT']), 'go-fmt': ('real', 'indicated', ['REAL-FORMAT'])}
    for k, w in want.items():
        if got.get(k) != w:
            fails.append(f'go signatures: {k} gave {got.get(k)}, expected {w}')
    for text, sid in (('go: downloading example.test/units v1.2.0', None),
                      ('go: example.test/units@v1.2.0: Get "https://proxy.golang.org/example.test/units/@v/v1.2.0.zip": dial tcp: lookup proxy.golang.org on 127.0.0.53:53: no such host', 'ENV-DNS'),
                      ('go: example.test/units@v1.2.0: reading https://proxy.golang.org/example.test/units/@v/v1.2.0.mod: 502 Bad Gateway', 'ENV-REGISTRY'),
                      ('FAIL\texample.test/fares/pkg/zones [setup failed]', None)):
        f = {'message': text, 'log_excerpt': {'lines': []}, 'job': 'x', 'kind': 'step', 'step_kind': 'setup'}
        ids = [h['id'] for h in signals(f, run, Path('.'))['signatures']]
        if (ids[:1] or [None])[0] != sid:
            fails.append(f'go signatures: {text[:60]!r} matched {ids}, expected {sid}')


def mini_run(d: Path, jobs: list[tuple[str, str, list[str]]], history=None) -> dict:
    """A normalised one-attempt run in d: jobs are (name, step name, log output lines)."""
    rjobs = []
    for i, (name, step, body) in enumerate(jobs, 40):
        (d / 'logs').mkdir(parents=True, exist_ok=True)
        (d / 'logs' / f'{i}.txt').write_text(log([(step, [step], body)]), encoding='utf-8')
        rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched', 'started_at': 'T1',
                      'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': step, 'conclusion': 'failure'}]})
    run = {'system': 'github', 'repo': 'acme/fares', 'run': {'id': 800, 'attempt': 1, 'commit': SHA, 'branch': 'main', 'event': 'push',
                                                              'started_at': '2026-03-02T10:00:00Z', 'conclusion': 'failure'},
           'attempts': [{'n': 1, 'started_at': 'T1', 'triggered_by': None, 'jobs_rerun': None, 'jobs': rjobs}],
           'history': history or [], 'last_green': None, 'source': 'recorded'}
    fetch_run.save(d / 'run.json', run)
    fetch_run.save(d / 'reruns.json', {'reruns': []})
    return run


def candidates(run: dict, d: Path) -> list[dict]:
    out = []
    for f in extract_failures.extract(d, run, 1):
        sg = signals(f, run, d)
        cls, conf, rule = decide(sg, None)
        out.append({'id': f['id'], 'failure': f, 'classification': cls, 'confidence': conf, 'rule': rule, 'signals': sg})
    return out


def finish(t: dict, where: Path) -> tuple[int, str]:
    """Validate after letting the validator write the computed summary fields."""
    p = where / 'triage.json'
    p.write_text(json.dumps(t, indent=1, ensure_ascii=False), encoding='utf-8')
    subprocess.run([sys.executable, str(HERE / 'validate_triage.py'), str(p), '--run', str(RUN), '--write-summary'], capture_output=True)
    return validate(json.loads(p.read_text(encoding='utf-8')), where)


def no_jobs_checks(fails: list) -> None:
    """A run that concluded failure without running a job (a workflow file error, a fork run never approved) is
    'not_run' with nothing to triage, and its evidence completeness gives no credit for checks over no jobs."""
    global RUN
    with tempfile.TemporaryDirectory() as td:
        RUN = Path(td) / 'run'
        run = mini_run(RUN, [])
        t = triage_from(run, [])
        t['run_status'], t['run_status_reason'] = 'not_run', 'the run concluded failure but no job ran (a startup failure or a workflow file error)'
        t['summary']['headline'] = 'Nothing to triage: the run concluded failure without running any job.'
        t['limitations'] = ['No job ran, so there is no log or test result to read.']
        rc, out = finish(t, Path(td))
        if rc or json.loads((Path(td) / 'triage.json').read_text(encoding='utf-8'))['summary'].get('evidence_completeness') != 0.0:
            fails.append(f'no jobs: a not_run triage with completeness 0.0 should validate:\n{out}')
        bad = json.loads((Path(td) / 'triage.json').read_text(encoding='utf-8'))
        bad['run_status'] = 'complete'
        rc, out = validate(bad, Path(td))
        if rc == 0 or 'run_status' not in out:
            fails.append(f'no jobs: run_status complete for a run with no jobs not caught:\n{out}')
        bad = json.loads((Path(td) / 'triage.json').read_text(encoding='utf-8'))
        bad['summary']['evidence_completeness'] = 0.4
        rc, out = validate(bad, Path(td))
        if rc == 0 or 'evidence_completeness' not in out:
            fails.append(f'no jobs: vacuous completeness credit not caught:\n{out}')


IT_LOG = ['running suite parcels (12 cases)', '[fail] parcels/rejects_empty_label: expected status 400, got 200', '11 of 12 passed', DONE]


def agent_test_and_local_runs_checks(fails: list) -> None:
    """The agent may name the test of a step failure when its quoted log lines show it; local repeat runs on the
    failing commit, recorded with their environment, can make a failure flaky (indicated, never confirmed)."""
    global RUN
    with tempfile.TemporaryDirectory() as td:
        RUN = Path(td) / 'run'
        go_body = extract_failures.GO_LOGS['go-unit'][2]
        run = mini_run(RUN, [('it', 'Integration tests', IT_LOG), ('go-unit', 'Unit tests', go_body)])
        tid = f'{extract_failures.GO_PKG}/quote > TestQuoteRounding/half_fare'
        fetch_run.save(RUN / 'local_runs.json', {'local_runs': [
            {'test_id': tid, 'commit': SHA, 'runs': 80, 'failed': 1, 'command': 'go test -count=80 -run TestQuoteRounding ./pkg/quote',
             'environment': {'os': 'linux', 'go': '1.26', 'note': 'network and devices disabled'}, 'source': 'agent', 'recorded_at': '2026-03-02T13:00:00Z'}]})
        cands = candidates(run, RUN)
        got = {c['failure']['job']: (c['classification'], c['confidence']) for c in cands}
        if got.get('go-unit') != ('flaky', 'indicated'):
            fails.append(f'local runs: 1 failure in 80 local runs on the failing commit should make it flaky/indicated, got {got.get("go-unit")}')
        t = triage_from(run, cands)
        it = next(f for f in t['failures'] if f['job'] == 'it')
        lines = (RUN / it['log_excerpt']['path']).read_text(encoding='utf-8').split('\n')
        n = next(i for i, l in enumerate(lines, 1) if '[fail] parcels/rejects_empty_label' in l)
        it.update(test_id='parcels/rejects_empty_label', test_id_source='agent', kind='test',
                  log_excerpt={'path': it['log_excerpt']['path'], 'lines': [{'n': n, 'text': extract_failures.strip(lines[n - 1])}]})
        rc, out = finish(t, Path(td))
        if rc:
            fails.append(f'agent-named test / local runs: the triage should validate:\n{out}')
        base = json.loads((Path(td) / 'triage.json').read_text(encoding='utf-8'))

        def seeded(name, mutate, expect):
            x = copy.deepcopy(base)
            mutate(x)
            rc, out = validate(x, Path(td))
            if rc == 0 or expect not in out:
                fails.append(f'seeded "{name}" not caught (expected {expect!r}):\n{out}')
        byj = lambda x, j: next(f for f in x['failures'] if f['job'] == j)  # noqa: E731
        seeded('agent names a test the quoted lines do not show', lambda x: byj(x, 'it').update(test_id='parcels/accepts_a_long_label'), 'not in the quoted log lines')
        seeded('agent names a test without saying so', lambda x: byj(x, 'it').pop('test_id_source'), 'test, job or kind differ')
        seeded('flaky from local runs raised to confirmed', lambda x: byj(x, 'go-unit').update(confidence='confirmed'), 'higher than the evidence')
        keep = (RUN / 'local_runs.json').read_text(encoding='utf-8')
        lr = json.loads(keep)
        lr['local_runs'][0]['commit'] = GREEN
        (RUN / 'local_runs.json').write_text(json.dumps(lr), encoding='utf-8')
        seeded('flaky from local runs on another commit', lambda x: None, 'flaky needs a pass on the same commit')
        lr['local_runs'][0].update(commit=SHA, environment={})
        (RUN / 'local_runs.json').write_text(json.dumps(lr), encoding='utf-8')
        seeded('local run recorded without its environment', lambda x: None, 'environment')
        (RUN / 'local_runs.json').write_text(keep, encoding='utf-8')


def owning_change_checks(fails: list) -> None:
    """owning_change.py: no last green run means no range (not the whole history); --max-commits bounds a search;
    no failures returns at once; a Go test file named by its import path is found in the tree."""
    import os
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Sample', 'GIT_AUTHOR_EMAIL': 'sample@example.test',
           'GIT_COMMITTER_NAME': 'Sample', 'GIT_COMMITTER_EMAIL': 'sample@example.test'}
    with tempfile.TemporaryDirectory() as td:
        repo, rd = Path(td) / 'repo', Path(td) / 'run'
        repo.mkdir()
        g = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True, env=env).stdout.strip()  # noqa: E731
        g('init', '-q', '-b', 'main')
        shas = []
        for i, (f, text) in enumerate([('go.mod', 'module example.test/fares\n'), ('pkg/zones/zones.go', 'package zones\n'),
                                       ('pkg/quote/quote.go', 'package quote\n'), ('pkg/quote/quote_test.go', 'package quote\n')]):
            p = repo / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
            g('add', f)
            g('commit', '-q', '-m', f'add {f}')
            shas.append(g('rev-parse', 'HEAD'))
        rd.mkdir()
        f1 = {'id': 'X01', 'job': 'unit', 'kind': 'test', 'test_id': 'example.test/fares/pkg/quote > TestQuoteRounding',
              'test_file': 'example.test/fares/pkg/quote/quote_test.go', 'frames': []}

        def oc(last_green, failures, *extra):
            fetch_run.save(rd / 'run.json', {'run': {'id': 1, 'commit': shas[-1], 'event': 'push'},
                                              'last_green': {'id': 0, 'commit': last_green} if last_green else None})
            fetch_run.save(rd / 'failures.json', {'failures': failures})
            r = subprocess.run([sys.executable, str(HERE / 'owning_change.py'), '--run', str(rd), '--repo', str(repo), '--out', str(rd / 'changes.json'), *extra],
                               capture_output=True, text=True, encoding='utf-8')
            return json.loads((rd / 'changes.json').read_text(encoding='utf-8')) if r.returncode == 0 else {'error': r.stderr}
        c = oc(None, [f1])
        x = (c.get('per_failure') or {}).get('X01') or {}
        if c.get('commits') is not None or x.get('status') != 'not_identified' or 'last green' not in (x.get('basis') or ''):
            fails.append(f'owning change without a last green run should not walk the history: commits {c.get("commits")}, X01 {x}')
        c = oc(None, [f1], '--max-commits', '2')
        x = (c.get('per_failure') or {}).get('X01') or {}
        if len(c.get('commits') or []) != 2 or (c.get('range') or {}).get('bounded') != 2 or x.get('commit') != shas[-1]:
            fails.append(f'owning change --max-commits 2: commits {len(c.get("commits") or [])}, range {c.get("range")}, X01 {x}')
        c = oc(shas[0], [f1])
        x = (c.get('per_failure') or {}).get('X01') or {}
        if x.get('status') != 'identified' or x.get('commit') != shas[-1]:
            fails.append(f'owning change: a Go test file named by its import path should be found in the tree, got {x}')
        (repo / 'pkg/quote/quote.go').write_text('package quote\n\nconst half = 0.5\n', encoding='utf-8')
        g('commit', '-q', '-am', 'change rounding')
        shas.append(g('rev-parse', 'HEAD'))
        c = oc(shas[3], [f1])
        x = (c.get('per_failure') or {}).get('X01') or {}
        if x.get('commit') != shas[-1] or [s['file'] for s in x.get('files_shared', [])] != ['pkg/quote/quote.go']:
            fails.append(f'owning change: a Go test exercises the source files of its own package, got {x}')
        c = oc(shas[0], [])
        if c.get('per_failure') != {} or c.get('commits') is not None or 'no failure' not in (c.get('problem') or ''):
            fails.append(f'owning change with no failures should return at once, got {c}')


def main() -> int:
    global RUN
    fails = []
    go_signature_checks(fails)
    no_jobs_checks(fails)
    agent_test_and_local_runs_checks(fails)
    owning_change_checks(fails)
    for mod in (fetch_run, extract_failures):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = mod.self_test()
        print(buf.getvalue().strip())
        if rc:
            fails.append(f'{mod.__name__} self-test failed')
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw, RUN = td / 'raw', td / 'run'
        build_raw(raw)
        import io, contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            run = fetch_run.normalise_github(raw, RUN, 'acme/fares')
        run['source'] = 'recorded'
        fetch_run.save(RUN / 'run.json', run)
        check = lambda c, w: None if c else fails.append(w)  # noqa: E731
        check(run['last_green']['id'] == 650, f'last green should be 650 (701 ran other jobs, 702 is another workflow), got {run["last_green"]}')
        check(run['attempts'][1]['jobs_rerun'] == ['publish'] and run['attempts'][1]['triggered_by'] == 'maintainer-a', 'attempt 2 metadata')
        r = subprocess.run([sys.executable, str(HERE / 'rerun.py'), '--run', str(RUN), '--observed'], capture_output=True, text=True, encoding='utf-8')
        check(r.returncode == 0, f'rerun.py --observed failed: {r.stderr}')
        fs = extract_failures.extract(RUN, run, 1)
        fetch_run.save(RUN / 'failures.json', {'run_id': 700, 'attempt': 1, 'failures': fs})
        cands = []
        for f in fs:
            sg = signals(f, run, RUN)
            cls, conf, rule = decide(sg, None)
            cands.append({'id': f['id'], 'failure': f, 'classification': cls, 'confidence': conf, 'rule': rule, 'signals': sg})
        got = {c['failure']['job']: (c['classification'], c['confidence']) for c in cands}
        want = {'unit': ('real', 'indicated'), 'publish': ('flaky', 'confirmed'), 'build-app': ('real', 'indicated'),
                'lint': ('unclassified', 'low'), 'gate': ('consequence', 'indicated')}
        check(got == want, f'candidates {got} should be {want}')
        unit = next(c for c in cands if c['failure']['job'] == 'unit')
        check(unit['signals']['rerun'] == 'job_not_rerun' and unit['signals']['history_same_commit'] == [{'id': 701, 'conclusion': 'success', 'event': 'push', 'job': 'absent'}],
              f'unit signals {unit["signals"]["rerun"]} {unit["signals"]["history_same_commit"]}')
        base = triage_from(run, cands)
        # the agent overrides publish to environment, quoting the network error the extractor's tail missed
        pub = next(f for f in base['failures'] if f['job'] == 'publish')
        lines = (RUN / pub['log_excerpt']['path']).read_text(encoding='utf-8').split('\n')
        n = next(i for i, l in enumerate(lines, 1) if 'socket hang up' in l)
        pub.update(classification='environment', confidence='confirmed', override_reason='The first error in the step is a network failure to the registry (socket hang up); the step passed when re-run.',
                   log_excerpt={'path': pub['log_excerpt']['path'], 'lines': [{'n': n, 'text': extract_failures.redact(extract_failures.strip(lines[n - 1]))}]})
        rc, out = validate(base, td)
        if rc:
            p = td / 'triage.json'
            subprocess.run([sys.executable, str(HERE / 'validate_triage.py'), str(p), '--run', str(RUN), '--write-summary'], capture_output=True)
            base = json.loads(p.read_text(encoding='utf-8'))
            rc, out = validate(base, td)
        check(rc == 0, f'the synthetic triage should validate:\n{out}')

        def seeded(name, mutate, expect):
            t = copy.deepcopy(base)
            mutate(t)
            rc, out = validate(t, td)
            check(rc != 0 and expect in out, f'seeded "{name}" not caught (expected {expect!r}):\n{out}')

        by = lambda t, job: next(f for f in t['failures'] if f['job'] == job)  # noqa: E731
        seeded('limitation omits the failed log', lambda t: t.update(limitations=['Nothing else to say.']), 'job lint was not downloaded')
        seeded('triage re-runs differ from reruns.json', lambda t: t['reruns'][0].update(triggered_by='someone-else'), 'reruns differ')
        rr = RUN / 'reruns.json'
        keep = rr.read_text(encoding='utf-8')
        bad = json.loads(keep)
        bad['reruns'][0].update(triggered_by='someone-else', jobs_rerun=['publish', 'unit'])
        rr.write_text(json.dumps(bad), encoding='utf-8')
        seeded('observed re-run credited to the wrong person and jobs', lambda t: t.update(reruns=bad['reruns']), "triggered_by 'someone-else' should be 'maintainer-a'")
        bad['reruns'][0].update(triggered_by='maintainer-a', jobs_rerun=['publish'], mode='recorded', authorised_by=None)
        rr.write_text(json.dumps(bad), encoding='utf-8')
        seeded('a re-run claimed as this triage\'s without an authoriser', lambda t: t.update(reruns=bad['reruns']), 'without authorised_by')
        rr.write_text(keep, encoding='utf-8')
        seeded('flaky from a same-commit run of another job', lambda t: by(t, 'unit').update(
            classification='flaky', confidence='confirmed', override_reason='The workflow passed on this commit in run 701 earlier.', draft_defect=None, priority=None),
            'flaky needs a pass on the same commit')
        seeded('consequence that is not a gate', lambda t: by(t, 'build-app').update(
            classification='consequence', override_reason='Only failed because something else failed first.', draft_defect=None, priority=None),
            'consequence needs an aggregate gate')
        seeded('environment for a non-setup step with no signature', lambda t: by(t, 'build-app').update(
            classification='environment', override_reason='Probably the runner image changed under the build.', draft_defect=None, priority=None),
            'environment needs a signature')
        seeded('override quoting a line with no signature, at confirmed', lambda t: by(t, 'publish').update(
            log_excerpt={'path': by(t, 'publish')['log_excerpt']['path'], 'lines': [{'n': n + 1, 'text': extract_failures.strip(lines[n])}]}),
            'environment needs a signature')
        seeded('quoted line altered', lambda t: by(t, 'publish')['log_excerpt']['lines'][0].update(text='Error: socket hang up (edited)'), 'does not match')
        seeded('schema version', lambda t: t.update(schema_version='1.0.0'), 'schema_version')
    if fails:
        print('self-test: FAILED')
        for f in fails:
            print('  -', f)
        return 1
    print('self-test: ok (synthetic run: Vitest failure real, quoted network error lets environment be confirmed, toolchain real, '
          'missing log unclassified and named, gate a consequence, observed re-run, same-commit run of another job not flaky; 10 seeded violations caught)')
    return 0


RUN = Path()

if __name__ == '__main__':
    sys.exit(main())
