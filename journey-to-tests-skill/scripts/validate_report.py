#!/usr/bin/env python3
"""Validate a Journey to Tests report (structure and honesty rules).

    python validate_report.py journey_tests_report.json [--repo .] [--write-summary]

With --repo (recommended) the validator also re-reads the journeys and test files:
  - re-lints every test the skill wrote (hard waits, missing assertions, missing journey tags)
  - checks every assertion reference points at an assertion line
  - checks journeys' Expects were not edited during the run
  - checks a passing test's file is byte-for-byte the file that passed its stability runs

--write-summary recomputes the summary counts in place (the headline is kept) before validating.
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
        if (f.get('kind') == 'manifest' or name in MANIFEST_NAMES) and not f.get('approved_by_user'):
            E(f'{p}: dependency manifest changed without approved_by_user (never install without asking)')
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
    if calc['results'].get('flaky'):
        W(f"{calc['results']['flaky']} flaky test(s) are not counted as done")
    return errors, warnings

def main(argv=None) -> int:
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
