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
    return {'schema_version': '1.1.0', 'skill': {'name': 'ci-failure-triager', 'version': '1.1.0'}, 'generated_at': '2026-03-02T12:00:00Z',
            'run': {'system': 'github', 'repo': run['repo'], 'id': r['id'], 'attempt': r['attempt'], 'commit': r['commit'], 'branch': r['branch'],
                    'started_at': r['started_at'], 'last_green': {'id': run['last_green']['id'], 'commit': run['last_green']['commit']}, 'source': 'recorded'},
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


def main() -> int:
    global RUN
    fails = []
    go_signature_checks(fails)
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
