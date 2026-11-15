#!/usr/bin/env python3
"""Validate a Journey to Tests report (structure and honesty rules).

    python validate_report.py journey_tests_report.json [--repo .] [--write-summary]

With --repo (recommended) the validator also re-reads the journeys and test files:
  - re-lints every test the skill wrote (hard waits, missing assertions, missing journey tags)
  - checks every assertion reference points at an assertion line
  - checks journeys' Expects were not edited during the run
  - checks a passing test's file is byte-for-byte the file that passed its stability runs

--write-summary recomputes the summary counts in place (the headline is kept) before validating.
The headline must give the Expects verified out of the total and name suspected bugs, flaky, blocked and failing
tests when there are any. Approval says who approved (metadata.approval.approved_by: user, delegated with a note,
unattended); manifests need approved_by user or delegated (approved_by_user is refused when no person approved the
run in the session); a bootstrapped framework (metadata.bootstrap) needs its config listed and an approver.
Warnings: case files that do not join to the mapped test path, and tests that block neither third parties nor
service workers.

    python validate_report.py --self-test
Exit code 0 when valid, 1 when invalid.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_md, lint_tests, plan_coverage  # noqa: E402

REQUIRED = ['metadata', 'journeys', 'mapping', 'step_coverage', 'testability_gaps', 'runs', 'cases', 'results',
            'suspected_bugs', 'files_changed', 'lint', 'summary']
COVERAGE = {'asserted', 'not_automatable', 'blocked_by_gap', 'deferred', 'pending'}
STATUSES = ['pass', 'fail_product', 'fail_test', 'blocked_env', 'flaky', 'not_run']
CHANGE_CATEGORIES = {'selector', 'timing', 'setup', 'test_data', 'navigation', 'structure'}
MANIFEST_NAMES = {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'pyproject.toml', 'poetry.lock',
                  'requirements.txt', 'requirements-dev.txt', 'Pipfile', 'Pipfile.lock', 'pom.xml', 'build.gradle',
                  'build.gradle.kts', 'go.mod', 'go.sum', 'Gemfile', 'Gemfile.lock', 'composer.json', 'composer.lock',
                  'Cargo.toml', 'Cargo.lock', 'pubspec.yaml', 'pubspec.lock', 'Podfile', 'Podfile.lock', 'Package.swift'}
BANNED = re.compile(r'\b(bug[- ]free|no bugs|fully tested|complete coverage|fully covered|100% (tested|covered))\b', re.I)
LINTED_KINDS = {'test', 'step_definitions', 'page_object', 'fixture', 'helper'}
APPROVERS = {'user', 'delegated', 'unattended'}

def file_sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    except OSError:
        return None

def norm(s: str) -> str:
    return ' '.join((s or '').split())

def compute_summary(d: dict) -> dict:
    results = {r['test_id']: r['status'] for r in d.get('results', [])}
    passing = {t for t, s in results.items() if s == 'pass'}
    cov = d.get('step_coverage', [])
    count = lambda c: sum(1 for e in cov if e.get('coverage') == c)
    return {
        'journeys_in_scope': sum(1 for j in d.get('journeys', []) if j.get('in_scope')),
        'tests_written': sum(len(m.get('tests', [])) for m in d.get('mapping', [])),
        'expects_total': len(cov),
        'expects_asserted': sum(1 for e in cov if e.get('assertions')),
        'expects_verified': sum(1 for e in cov if e.get('assertions') and set(e.get('test_ids', [])) & passing),
        'expects_not_automatable': count('not_automatable'),
        'expects_blocked_by_gap': count('blocked_by_gap'),
        'expects_deferred': count('deferred'),
        'results': {s: sum(1 for v in results.values() if v == s) for s in STATUSES},
    }

def check(d: dict, repo: Path | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    E, W = errors.append, warnings.append
    for k in REQUIRED:
        if k not in d:
            E(f'missing top-level field: {k}')
    if errors:
        return errors, warnings

    md = d['metadata']
    for k in ('repository', 'generated_at', 'skill_version', 'frameworks', 'platforms', 'target', 'repeat', 'approval'):
        if k not in md:
            E(f'metadata.{k} is required')
    if not md.get('frameworks'):
        E('metadata.frameworks is empty: name the framework(s) the tests were written for')
    if not isinstance(md.get('target'), dict) or not md['target'].get('value'):
        E('metadata.target must say what the tests ran against (base URL, dev server, simulator, build)')
    repeat = md.get('repeat') if isinstance(md.get('repeat'), int) and md.get('repeat', 0) >= 1 else None
    if repeat is None:
        E('metadata.repeat must be an integer >= 1')
    approval = md.get('approval') or {}
    if approval.get('gate', 'on') == 'on' and d['files_changed'] and not approval.get('plan_approved'):
        E('files were written but metadata.approval.plan_approved is false (approval gate is on)')
    # who approved: a person in the session (user), a person in advance through the brief, ticket or instruction that
    # started the run (delegated, named in the note), or no one (unattended)
    run_approver = approval.get('approved_by')
    if run_approver is not None and run_approver not in APPROVERS:
        E(f'metadata.approval.approved_by must be one of {sorted(APPROVERS)}, not {run_approver!r}')
    if run_approver == 'delegated' and not norm(approval.get('note')):
        E('metadata.approval.approved_by is delegated: the note must name who delegated it and where '
          '(the brief, ticket or instruction that started the run)')
    if run_approver == 'unattended' and approval.get('plan_approved'):
        E('metadata.approval.approved_by is unattended (no one approved) but plan_approved is true')
    boot = md.get('bootstrap')
    if boot is not None:
        if not isinstance(boot, dict) or not boot.get('framework') or not boot.get('config'):
            E('metadata.bootstrap needs framework and config (the framework set up, and its config file)')
        else:
            if boot['config'] not in {f.get('path') for f in d['files_changed']}:
                E(f"metadata.bootstrap.config {boot['config']} is not listed in files_changed")
            who = boot.get('approved_by')
            if who not in ('user', 'delegated'):
                E(f"metadata.bootstrap.approved_by must be user or delegated, not {who!r}: a framework is never "
                  'installed without someone agreeing to it')
            elif who == 'delegated' and not norm(boot.get('note') or approval.get('note')):
                E('metadata.bootstrap is approved by delegation: name who delegated it in its note')

    # ---- journeys ----
    journeys = {}
    for j in d['journeys']:
        if j.get('id') in journeys:
            E(f"duplicate journey id {j.get('id')}")
        journeys[j.get('id')] = j
    in_scope = {k for k, j in journeys.items() if j.get('in_scope')}
    for k, j in journeys.items():
        if not j.get('in_scope') and not j.get('scope_reason'):
            E(f'{k}: out of scope without scope_reason')
    parsed = {}
    if repo:
        for k, j in journeys.items():
            p = repo / j['path']
            if not p.exists():
                E(f"{k}: journey file not found: {j['path']}"); continue
            try:
                parsed[k] = journey_md.load(p)
            except Exception as exc:  # noqa: BLE001
                E(f"{k}: journey file cannot be parsed: {exc}"); continue
            if parsed[k]['meta'].get('id') != k:
                E(f"{k}: file {j['path']} has id {parsed[k]['meta'].get('id')}")
            if plan_coverage.expect_sha256(parsed[k]) != j.get('expect_sha256'):
                E(f"{k}: the journey's Expects or Not automatable entries changed during the run; "
                  f"re-plan instead of editing the journey to match the tests")

    # ---- step coverage ----
    cov_keys = {}
    for e in d['step_coverage']:
        key = (e.get('journey_id'), e.get('ref'))
        where = f'{key[0]} {key[1]}'
        if key in cov_keys:
            E(f'{where}: duplicate step_coverage entry')
        cov_keys[key] = e
        if key[0] not in in_scope:
            E(f'{where}: step_coverage for a journey that is not in scope')
        c = e.get('coverage')
        if c not in COVERAGE:
            E(f'{where}: invalid coverage {c!r}'); continue
        if c == 'pending':
            E(f'{where}: still pending; every Expect needs an assertion, a Not automatable reason, a gap or a deferral reason')
        if c == 'asserted' and (not e.get('assertions') or not e.get('test_ids')):
            E(f'{where}: coverage asserted but no assertions/test_ids')
        if c in ('not_automatable', 'deferred', 'blocked_by_gap') and not norm(e.get('reason')):
            E(f'{where}: coverage {c} needs a reason')
        if c == 'blocked_by_gap' and e.get('gap_id') not in {g.get('id') for g in d['testability_gaps']}:
            E(f"{where}: gap_id {e.get('gap_id')!r} not found in testability_gaps")
        if c == 'not_automatable':
            listed = e.get('journey_lists_not_automatable')
            if key[0] in parsed:
                listed = any(n['target'] == key[1] for n in parsed[key[0]]['not_automatable'])
            if not listed:
                E(f'{where}: marked not_automatable but the journey does not list it under "Not automatable"; '
                  f'assert it, record a testability gap, or ask for the journey to be updated')
        if c == 'asserted':
            listed = e.get('journey_lists_not_automatable')
            if key[0] in parsed:
                listed = any(n['target'] == key[1] for n in parsed[key[0]]['not_automatable'])
            if listed and not norm(e.get('reason')):
                E(f'{where}: asserted, but the journey lists it as not automatable: say in reason which part is '
                  f'asserted and which part is left unverified')
        for a in e.get('assertions', []):
            if not a.get('file') or not isinstance(a.get('line'), int):
                E(f'{where}: assertion reference needs file and line'); continue
            if repo:
                p = repo / a['file']
                if not p.exists():
                    E(f"{where}: assertion file not found: {a['file']}"); continue
                lang = lint_tests.lang_of(p)
                lines = p.read_text(encoding='utf-8', errors='replace').split('\n')
                window = '\n'.join(lines[a['line'] - 1:a['line'] + 2])
                rx = lint_tests.ASSERTIONS.get(lang)
                ok = (re.search(r'^\s*Then\b', window, re.M) if lang == 'gherkin' else rx.search(window) if rx else True)
                if a['line'] > len(lines) or not ok:
                    E(f"{where}: {a['file']}:{a['line']} is not an assertion")
    for k in in_scope:
        if k in parsed:
            for x in plan_coverage.expects(parsed[k]):
                e = cov_keys.get((k, x['ref']))
                if not e:
                    E(f"{k} {x['ref']}: Expect has no step_coverage entry")
                elif norm(e.get('expect')) != norm(x['expect']):
                    E(f"{k} {x['ref']}: step_coverage expect text differs from the journey")
        elif not any(key[0] == k for key in cov_keys):
            E(f'{k}: in scope but has no step_coverage entries')

    # ---- mapping ----
    tests = {}
    for m in d['mapping']:
        jid = m.get('journey_id')
        if jid not in in_scope:
            E(f'mapping for {jid}: journey is not in scope')
        for t in m.get('tests', []):
            tid = t.get('test_id')
            if tid in tests:
                E(f'duplicate test_id {tid}')
            tests[tid] = {**t, 'journey_id': jid, 'test_path': m.get('test_path'), 'describe': m.get('describe', '')}
            if not any(jid in str(tag) for tag in t.get('tags', [])):
                E(f'{tid}: tags must include the journey id {jid}')
            if jid not in (t.get('name') or '') and jid not in (m.get('describe') or ''):
                E(f'{tid}: the journey id {jid} must appear in the test title or its describe block')
            if t.get('variant') and (jid, t['variant']) not in cov_keys:
                E(f"{tid}: variant {t['variant']} is not a variant of {jid}")
    for e in d['step_coverage']:
        for tid in e.get('test_ids', []):
            if tid not in tests:
                E(f"{e.get('journey_id')} {e.get('ref')}: test_id {tid} is not in mapping")
            elif tests[tid]['journey_id'] != e.get('journey_id'):
                E(f"{e.get('journey_id')} {e.get('ref')}: test {tid} belongs to {tests[tid]['journey_id']}")
    changed = {f.get('path'): f for f in d['files_changed']}
    for tid, t in tests.items():
        if t['test_path'] not in changed:
            E(f"{tid}: test file {t['test_path']} is not listed in files_changed")

    # ---- runs, cases, results ----
    runs = {r.get('id'): r for r in d['runs']}
    cases = {(c.get('run_id'), c.get('id')): c for c in d['cases']}
    helpers = sorted(p for p, f in changed.items() if f.get('kind') in ('page_object', 'fixture', 'helper', 'step_definitions'))

    def fingerprint(run: dict, test_path: str):
        """The test file plus any helper the skill changed, as hashed for this run (None if the test wasn't hashed)."""
        fh = run.get('file_hashes') or {}
        if not fh.get(test_path):
            return None
        return tuple((p, fh[p]) for p in [test_path] + helpers if p in fh)

    results = {}
    case_file_warned: set = set()
    for r in d['results']:
        tid = r.get('test_id')
        if tid in results:
            E(f'duplicate result for {tid}')
        results[tid] = r
        if tid not in tests:
            E(f'result for unknown test_id {tid}'); continue
        st = r.get('status')
        if st not in STATUSES:
            E(f'{tid}: invalid status {st!r}'); continue
        attempts = r.get('attempts', [])
        hashes, test_hashes = [], []
        for i, a in enumerate(attempts):
            run = runs.get(a.get('run_id'))
            if not run:
                E(f"{tid}: attempt references unknown run {a.get('run_id')}")
                hashes.append(None); test_hashes.append(None); continue
            case = cases.get((a.get('run_id'), a.get('case_id')))
            if not case:
                E(f"{tid}: case {a.get('case_id')!r} not found in run {a.get('run_id')}")
            elif case.get('status') != a.get('outcome'):
                E(f"{tid}: attempt outcome {a.get('outcome')} but run {a.get('run_id')} recorded {case.get('status')}")
            if case and case.get('file') and r.get('test_path') and case['file'] not in case_file_warned:
                cf, tp = str(case['file']), str(r['test_path'])
                if cf != tp and not tp.endswith('/' + cf):
                    case_file_warned.add(case['file'])
                    W(f"{tid}: case file {cf!r} in run {a.get('run_id')} is not the test path {tp!r}; record cases with "
                      f"parse_results.py --files-root <repo> (and --test-dir) so they join to the mapping")
            if a.get('outcome') in ('failed', 'error') and not a.get('classification'):
                E(f"{tid}: failed attempt in {a.get('run_id')} is not classified (fail_product, fail_test or blocked_env)")
            hashes.append(fingerprint(run, r.get('test_path')))
            h = (run.get('file_hashes') or {}).get(r.get('test_path'))
            test_hashes.append(h)
            if i and h and test_hashes[i - 1] and h != test_hashes[i - 1]:
                ch = a.get('change') or {}
                if ch.get('category') not in CHANGE_CATEGORIES or not norm(ch.get('detail')):
                    E(f"{tid}: test file changed before run {a.get('run_id')} without a change reason "
                      f"({', '.join(sorted(CHANGE_CATEGORIES))}); changing what the test expects is not allowed")
        if any(a.get('classification') == 'fail_product' for a in attempts) and st != 'fail_product':
            E(f'{tid}: an attempt was classified fail_product, so the result must stay fail_product '
              f'(a product mismatch is reported, never fixed by editing the test)')
        last = attempts[-1] if attempts else None
        if st == 'not_run':
            if attempts: E(f'{tid}: status not_run but attempts exist')
            if not norm(r.get('notes')): E(f'{tid}: not_run needs notes explaining why')
        elif not attempts:
            E(f'{tid}: status {st} without any run attempt')
        elif st == 'pass':
            if repeat and len(attempts) < repeat:
                E(f'{tid}: pass needs {repeat} passing stability runs, found {len(attempts)} attempt(s)')
            window, wh = attempts[-(repeat or 1):], hashes[-(repeat or 1):]
            window_passed = all(a.get('outcome') == 'passed' for a in window)
            if not window_passed:
                E(f'{tid}: pass but the last {repeat} attempts did not all pass')
            if any(h is None for h in wh):
                E(f'{tid}: pass but the stability runs did not record the test file hash (parse_results.py --hash)')
            elif len(set(wh)) > 1:
                E(f'{tid}: pass but the test file changed during the stability runs')
            elif repo and wh:
                for p, h in wh[-1]:
                    if file_sha(repo / p) != h:
                        E(f"{tid}: {p} changed after the last passing run; re-run the stability check")
            if window_passed and wh and wh[-1]:
                same = [a for a, h in zip(attempts, hashes) if h == wh[-1]]
                if any(a.get('outcome') in ('failed', 'error') and a.get('classification') != 'blocked_env' for a in same):
                    E(f'{tid}: the final test file both passed and failed; status must be flaky')
        elif st == 'flaky':
            final = hashes[-1]
            same = [a for a, h in zip(attempts, hashes) if h == final] if final else attempts
            if not ({'passed'} & {a.get('outcome') for a in same} and {'failed', 'error'} & {a.get('outcome') for a in same}):
                E(f'{tid}: flaky needs both passing and failing attempts of the same test file')
        elif st in ('fail_product', 'fail_test', 'blocked_env'):
            if last.get('outcome') not in ('failed', 'error') or last.get('classification') != st:
                E(f'{tid}: status {st} but the last attempt is {last.get("outcome")}/{last.get("classification")}')
        if st in ('blocked_env', 'fail_product', 'flaky') and not r.get('evidence'):
            E(f'{tid}: {st} needs evidence (error excerpt, trace, screenshot)')
    for tid in tests:
        if tid not in results:
            E(f'{tid}: written but has no result (use status not_run with notes if it could not be run)')

    # ---- suspected bugs ----
    bug_tests = set()
    for b in d['suspected_bugs']:
        if b.get('test_id') not in results or results[b['test_id']].get('status') != 'fail_product':
            E(f"{b.get('id')}: test {b.get('test_id')} is not a fail_product result")
        if (b.get('journey_id'), b.get('ref')) not in cov_keys:
            E(f"{b.get('id')}: {b.get('journey_id')} {b.get('ref')} is not a covered Expect")
        if not b.get('evidence'):
            E(f"{b.get('id')}: needs evidence")
        bug_tests.add(b.get('test_id'))
    for tid, r in results.items():
        if r.get('status') == 'fail_product' and tid not in bug_tests:
            E(f'{tid}: fail_product result has no suspected_bugs entry')

    # ---- gaps ----
    seen = set()
    for g in d['testability_gaps']:
        if g.get('id') in seen:
            E(f"duplicate gap id {g.get('id')}")
        seen.add(g.get('id'))
        if g.get('journey_id') not in in_scope:
            E(f"{g.get('id')}: journey {g.get('journey_id')} not in scope")

    # ---- files changed ----
    if len(changed) != len(d['files_changed']):
        E('files_changed lists a path more than once')
    for p, f in changed.items():
        name = Path(p).name
        if f.get('kind') == 'manifest' or name in MANIFEST_NAMES:
            who = f.get('approved_by')
            if f.get('approved_by_user') and run_approver in ('delegated', 'unattended'):
                E(f'{p}: approved_by_user is true but no person approved this run in the session (approval.approved_by '
                  f'is {run_approver}); record approved_by: delegated with a note naming who allowed the install')
            elif who is not None and who not in APPROVERS:
                E(f'{p}: approved_by must be one of {sorted(APPROVERS)}, not {who!r}')
            elif who == 'delegated' and not norm(f.get('note')):
                E(f'{p}: dependency manifest changed by delegated approval: the note must name who allowed the install '
                  f'and where (the brief, ticket or instruction)')
            elif not (f.get('approved_by_user') or who in ('user', 'delegated')):
                E(f'{p}: dependency manifest changed without approval (approved_by user or delegated; never install '
                  f'without asking)')
        if repo and not (repo / p).exists():
            E(f'{p}: listed in files_changed but not found')
    if d.get('journey_updates') and not any(f.get('kind') == 'journey' for f in d['files_changed']):
        E('journey_updates present but no journey files listed in files_changed')

    # ---- lint ----
    if repo:
        mapped_names = {}
        for t in tests.values():
            mapped_names.setdefault(t['test_path'], set()).add(t['name'])
        total = 0
        for p, f in changed.items():
            if f.get('kind') not in LINTED_KINDS or not (repo / p).exists() or not lint_tests.lang_of(repo / p):
                continue
            res = lint_tests.lint_file(repo / p)
            names = mapped_names.get(p, set())
            found = {t['name'] for t in res['tests']}
            for n in names - found:
                E(f'{p}: mapped test {n!r} not found in the file')
            for x in res['findings']:
                if x['level'] != 'error':
                    if x['rule'] == 'brittle_selector' and (f['change'] == 'added' or x['test'] in names):
                        W(f"{p}:{x['line']}: {x['message']}")
                    continue
                ours = (f['change'] == 'added' or x['test'] in names or
                        (x['test'] is None and x['rule'] == 'focused_test'))
                if f.get('kind') not in ('test', 'step_definitions') and x['rule'] not in ('hard_wait', 'focused_test'):
                    ours = False
                if ours:
                    total += 1
                    E(f"lint {p}:{x['line']}: {x['rule']}: {x['message']}")
        if d['lint'].get('ran') and d['lint'].get('errors', 0) != total:
            W(f"report says lint found {d['lint'].get('errors')} error(s); re-lint found {total}")
        # the written tests, their helpers and the framework config, read together: third parties and service workers
        net = [repo / p for p, f in changed.items()
               if f.get('kind') in LINTED_KINDS | {'config'} and (repo / p).is_file() and lint_tests.lang_of(repo / p)]
        net_results = [{'tests': [], 'findings': []} for _ in net]
        for i, p in enumerate(net):
            if changed[p.relative_to(repo).as_posix()].get('kind') in ('test', 'step_definitions'):
                net_results[i]['tests'] = lint_tests.lint_file(p)['tests']
        lint_tests.network_findings(net, net_results)
        for x in (x for r in net_results for x in r['findings']):
            W(f"lint: {x['rule']}: {x['message']}")
    else:
        if not d['lint'].get('ran'):
            E('lint.ran is false: run lint_tests.py on the written tests (or validate with --repo)')
        elif d['lint'].get('errors'):
            E(f"lint reported {d['lint']['errors']} error(s)")
        W('validated without --repo: test files, assertion lines, journey fingerprints and file hashes were not re-checked')

    # ---- summary ----
    s = d['summary']
    calc = compute_summary(d)
    for k, v in calc.items():
        if s.get(k) != v:
            E(f'summary.{k} is {s.get(k)!r}, recomputed {v!r} (run with --write-summary)')
    if not norm(s.get('headline')):
        E('summary.headline is required')
    elif BANNED.search(s['headline']):
        E(f"summary.headline overclaims: {BANNED.search(s['headline']).group(0)!r}")
    else:
        h = s['headline']
        v, t = calc['expects_verified'], calc['expects_total']
        if not re.search(rf'(?<!\d){v}\s*(?:of|/|out of)\s*{t}(?!\d)', h):
            E(f'summary.headline must give the Expects verified by passing, stable tests out of the total '
              f'("{v} of {t} Expects verified"), got {h[:80]!r}')
        problems = [(calc['results'].get('fail_product'), r'\bbugs?\b', 'the suspected bug(s)'),
                    (calc['results'].get('flaky'), r'\bflaky\b', 'the flaky test(s)'),
                    (calc['results'].get('blocked_env'), r'\bblocked\b', 'the blocked test(s)'),
                    (calc['results'].get('fail_test'), r'\bfail(?:ing|ed|s)?\b|\bbroken\b', 'the failing test(s)')]
        missing = [label for n, rx, label in problems if n and not re.search(rx, h, re.I)]
        if missing:
            E(f"summary.headline must lead with problems: it does not mention {', '.join(missing)}")
    if calc['results'].get('flaky'):
        W(f"{calc['results']['flaky']} flaky test(s) are not counted as done")
    return errors, warnings

def self_test() -> int:
    """Negative cases on a copy of the shipped example (validated without --repo): each must be reported."""
    import copy
    example = Path(__file__).resolve().parent.parent / 'examples' / 'journey_tests_report.example.json'
    base = json.loads(example.read_text(encoding='utf-8'))
    checks = []

    def run(d):
        try:
            return (*check(d, None), None)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check
            return None, None, f'{type(exc).__name__}: {exc}'

    def case(name, mutate, want_error=None, want_warning=None, valid=False):
        d = copy.deepcopy(base)
        mutate(d)
        errs, warns, crash = run(d)
        if crash:
            checks.append((name, False, crash)); return
        if valid:
            ok = not errs
        elif want_error:
            ok = any(want_error in e for e in errs)
        else:
            ok = True
        if want_warning:
            ok = ok and any(want_warning in w for w in warns)
        checks.append((name, ok, errs if not valid else errs or warns))

    case('the shipped example is valid', lambda d: None, valid=True)

    def headline(text):
        return lambda d: d['summary'].__setitem__('headline', text)
    case('a placeholder headline is refused', headline('PLACEHOLDER'), want_error='summary.headline')
    case('a headline without verified/total Expects is refused',
         headline('6 tests written; 1 suspected bug, 1 flaky, 1 blocked.'), want_error='verified')
    case('a headline that hides the suspected bug, flaky and blocked tests is refused',
         headline('7 of 13 Expects verified by passing, stable tests.'), want_error='lead with')

    def approval(**kw):
        return lambda d: d['metadata']['approval'].update(kw)
    case('a delegated approval without a note naming who delegated it is refused',
         approval(approved_by='delegated', note=''), want_error='delegated')
    case('a delegated approval with a note is valid',
         approval(approved_by='delegated', note='The brief that started this run allowed the plan and the install.'),
         valid=True)
    case('an unattended run cannot claim an approved plan', approval(approved_by='unattended'), want_error='unattended')
    case('an unknown approved_by is refused', approval(approved_by='someone'), want_error='approved_by')

    def manifest(entry, **appr):
        def f(d):
            d['metadata']['approval'].update(appr)
            d['files_changed'].append({'path': 'web/package.json', 'change': 'modified', 'kind': 'manifest', **entry})
        return f
    brief = 'The brief that started this run allowed adding @playwright/test.'
    case('a manifest change approved by delegation, with a note, is valid',
         manifest({'approved_by': 'delegated', 'note': brief}, approved_by='delegated', note=brief), valid=True)
    case('a manifest change approved by delegation without a note is refused',
         manifest({'approved_by': 'delegated'}, approved_by='delegated', note=brief), want_error='package.json')
    case('a manifest change in an unattended run is refused',
         manifest({'approved_by': 'unattended'}), want_error='package.json')
    case('approved_by_user on a manifest while the run was approved by delegation is refused',
         manifest({'approved_by_user': True}, approved_by='delegated', note=brief), want_error='approved_by_user')

    def bootstrap(b, files=()):
        def f(d):
            d['metadata']['bootstrap'] = b
            d['files_changed'].extend(files)
            d['metadata']['conventions_source'] = 'chosen: no e2e framework found; Playwright bootstrapped'
        return f
    pw = {'framework': 'playwright', 'config': 'web/playwright.config.ts', 'approved_by': 'user'}
    case('a bootstrapped framework whose config is not in files_changed is refused', bootstrap(pw),
         want_error='bootstrap')
    case('a bootstrapped framework with its config listed is valid',
         bootstrap(pw, [{'path': 'web/playwright.config.ts', 'change': 'added', 'kind': 'config'}]), valid=True)
    case('a bootstrap nobody approved is refused',
         bootstrap({**pw, 'approved_by': 'unattended'},
                   [{'path': 'web/playwright.config.ts', 'change': 'added', 'kind': 'config'}]), want_error='bootstrap')

    def partial(d):
        e = next(x for x in d['step_coverage'] if x['coverage'] == 'asserted')
        e['journey_lists_not_automatable'] = True
        e.pop('reason', None)
    case('an Expect the journey lists as not automatable, asserted without saying which part is left out, is refused',
         partial, want_error='not automatable')

    def win_case(d):
        r = next(r for r in d['results'] if r['status'] == 'pass')
        a = r['attempts'][0]
        c = next(c for c in d['cases'] if c['run_id'] == a['run_id'] and c['id'] == a['case_id'])
        c['file'] = c['file'].replace('/', '\\')
    case('a case file that is not the repository-relative test path is warned about', win_case,
         want_warning='--files-root')

    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0

def main(argv=None) -> int:
    if '--self-test' in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    ap = argparse.ArgumentParser(description='Validate a Journey to Tests report.')
    ap.add_argument('report')
    ap.add_argument('--repo', help='repository root, to re-check journeys, test files and hashes')
    ap.add_argument('--write-summary', action='store_true', help='recompute summary counts in place first')
    args = ap.parse_args(argv)
    path = Path(args.report)
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'INVALID: cannot read report: {exc}'); return 1
    if args.write_summary:
        d.setdefault('summary', {}).update(compute_summary(d))
        d['summary'].setdefault('headline', '')
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errors, warnings = check(d, Path(args.repo).resolve() if args.repo else None)
    for w in warnings:
        print(f'WARN: {w}')
    if errors:
        for e in errors:
            print(f'ERROR: {e}')
        print(f'INVALID: {len(errors)} error(s)')
        return 1
    print('VALID')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
