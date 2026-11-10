#!/usr/bin/env python3
"""Validate a Ticket to TDD report (structure and honesty rules).

    python validate_tdd_report.py tdd_report.json [--ticket ticket.json] [--repo .] [--write-summary]

--ticket re-checks the requirements against the ticket: every source_excerpt must be quoted from
it, the ticket fingerprint must match, and every suspicious instruction must be reported.
--repo re-checks the test files: test names exist, files still match the red lock, and no
skip/xfail/only markers hide a failure.
--write-summary recomputes the summary counts in place (the headline is kept) before validating.
Exit code 0 when valid, 1 when invalid.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_ticket, hash_tests  # noqa: E402

REQUIRED = ['metadata', 'conventions', 'requirements', 'open_questions', 'manual_checks', 'traceability', 'runs',
            'cases', 'integrity', 'regressions', 'files_changed', 'summary']
MODES = {'red_only', 'red_green', 'full'}
REQ_TYPES = {'functional', 'validation', 'error', 'permission', 'state', 'non_functional', 'regression'}
TESTABILITY = {'testable', 'needs_clarification', 'not_unit_testable'}
LEVELS = {'unit', 'component', 'integration', 'api', 'contract', 'e2e'}
KINDS = {'happy', 'boundary', 'negative', 'error', 'permission', 'state', 'reproduction'}
RED = {'valid_red', 'invalid_red', 'unexpected_green', 'not_run'}
GREEN = {'passed', 'failed', 'not_run'}
FINDINGS = {'already_implemented', 'test_ineffective', 'other'}
FILE_KINDS = {'test', 'fixture', 'stub', 'source', 'config', 'manifest'}
MANIFEST_NAMES = {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'pyproject.toml', 'poetry.lock',
                  'uv.lock', 'requirements.txt', 'requirements-dev.txt', 'Pipfile', 'Pipfile.lock', 'pom.xml',
                  'build.gradle', 'build.gradle.kts', 'go.mod', 'go.sum', 'Gemfile', 'Gemfile.lock', 'composer.json',
                  'composer.lock', 'Cargo.toml', 'Cargo.lock', 'pubspec.yaml', 'pubspec.lock', 'Package.swift'}
BANNED = re.compile(r'\b(bug[- ]free|fully tested|complete coverage|fully covered|100% (tested|covered)|'
                    r'production[- ]ready|feature (is )?(complete|done))\b', re.I)

def norm(s) -> str:
    return ' '.join(str(s or '').replace('“', '"').replace('”', '"').replace('’', "'").split()).lower()

def compute_summary(d: dict) -> dict:
    tr = d.get('traceability', [])
    by_req: dict[str, list[dict]] = {}
    for t in tr:
        by_req.setdefault(t.get('req_id'), []).append(t)
    red = lambda t: (t.get('red') or {}).get('status')
    reqs = d.get('requirements', [])
    covered = {r['id'] for r in reqs if any(red(t) == 'valid_red' for t in by_req.get(r['id'], []))}
    already = {r['id'] for r in reqs if r['id'] not in covered and any(
        red(t) == 'unexpected_green' and ((t.get('red') or {}).get('investigation') or {}).get('finding') == 'already_implemented'
        for t in by_req.get(r['id'], []))}
    not_run = {r['id'] for r in reqs if r.get('testability') == 'testable' and by_req.get(r['id'])
               and all(red(t) == 'not_run' for t in by_req[r['id']])}
    integ = d.get('integrity') or {}
    return {
        'reqs_total': len(reqs),
        'reqs_covered': len(covered),
        'reqs_already_implemented': len(already),
        'reqs_blocked': sum(1 for r in reqs if r.get('testability') == 'needs_clarification'),
        'reqs_not_unit_testable': sum(1 for r in reqs if r.get('testability') == 'not_unit_testable'),
        'reqs_not_run': len(not_run),
        'tests_written': len(tr),
        'valid_reds': sum(1 for t in tr if red(t) == 'valid_red'),
        'unexpected_greens': sum(1 for t in tr if red(t) == 'unexpected_green'),
        'tests_not_run': sum(1 for t in tr if red(t) == 'not_run'),
        'greens': sum(1 for t in tr if (t.get('green') or {}).get('status') == 'passed'),
        'regressions': sum(1 for r in d.get('regressions', []) if not r.get('pre_existing')),
        're_red_confirmed': bool((integ.get('re_red') or {}).get('confirmed')),
        'stubs_added': len(integ.get('stubs_added') or []),
    }

def check(d: dict, ticket: dict | None, repo: Path | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    E, W = errors.append, warnings.append
    for k in REQUIRED:
        if k not in d:
            E(f'missing top-level field: {k}')
    if errors:
        return errors, warnings

    md = d['metadata']
    for k in ('repository', 'generated_at', 'skill_version', 'mode', 'ticket', 'approval'):
        if k not in md:
            E(f'metadata.{k} is required')
    mode = md.get('mode')
    if mode not in MODES:
        E(f'metadata.mode must be one of {sorted(MODES)}'); mode = 'red_only'
    tk = md.get('ticket') or {}
    for k in ('key', 'type', 'summary', 'source', 'content_sha256'):
        if not tk.get(k):
            E(f'metadata.ticket.{k} is required')
    if tk.get('source') not in ('connector', 'rest', 'pasted'):
        E('metadata.ticket.source must be connector, rest or pasted')
    if (tk.get('type') or '').lower() in ('epic', 'initiative'):
        E(f"{tk.get('key')} is an {tk.get('type')}: work on one story or subtask at a time")
    approval = md.get('approval') or {}
    changed = {f.get('path'): f for f in d['files_changed']}
    if approval.get('gate', 'on') == 'on' and changed and not approval.get('plan_approved'):
        E('files were written but metadata.approval.plan_approved is false (approval gate is on)')

    # ---- ticket ----
    if ticket is not None:
        if fetch_ticket.content_sha256(ticket) != tk.get('content_sha256'):
            E('metadata.ticket.content_sha256 does not match the ticket file: the ticket changed, or the report '
              'was built from a different version')
        ignored = {norm(s.get('excerpt')) for s in tk.get('suspicious_instructions_ignored') or []}
        for s in ticket.get('suspicious_instructions') or []:
            if norm(s.get('excerpt')) not in ignored:
                E(f"ticket {s.get('field')} contains instruction-like text that the report does not list under "
                  f"metadata.ticket.suspicious_instructions_ignored: {s.get('excerpt')[:80]!r}")
        text = norm(fetch_ticket.ticket_text(ticket))
    else:
        text = None
        W('validated without --ticket: requirement excerpts and the ticket fingerprint were not re-checked')

    # ---- requirements and questions ----
    reqs = {}
    for r in d['requirements']:
        rid = r.get('id')
        if rid in reqs:
            E(f'duplicate requirement {rid}')
        reqs[rid] = r
        if not re.fullmatch(r'REQ-\d+', rid or ''):
            E(f'{rid}: requirement ids look like REQ-1')
        if r.get('type') not in REQ_TYPES:
            E(f"{rid}: type must be one of {sorted(REQ_TYPES)}")
        if r.get('testability') not in TESTABILITY:
            E(f"{rid}: testability must be one of {sorted(TESTABILITY)}")
        if not norm(r.get('text')) or not norm(r.get('source_excerpt')) or not r.get('source_field'):
            E(f'{rid}: text, source_field and source_excerpt are required (only test what the ticket says)')
        elif text is not None and norm(r['source_excerpt']) not in text:
            E(f"{rid}: source_excerpt is not quoted from the ticket: {r['source_excerpt'][:80]!r}")
    questions = {}
    for q in d['open_questions']:
        questions[q.get('id')] = q
        if not norm(q.get('question')):
            E(f"{q.get('id')}: question text is required")
        for rid in q.get('related_reqs') or []:
            if rid not in reqs:
                E(f"{q.get('id')}: related requirement {rid} does not exist")
    blocking_open = [q['id'] for q in d['open_questions'] if q.get('blocking') and not norm(q.get('answer'))]
    if blocking_open and any(f.get('kind') in ('test', 'stub', 'source') for f in d['files_changed']):
        E(f"tests or code were written while blocking question(s) {', '.join(blocking_open)} were unanswered; "
          f"a blocking ambiguity stops the run before any tests are written")
    manual = {m.get('req_id') for m in d['manual_checks']}
    for m in d['manual_checks']:
        if m.get('req_id') not in reqs or not norm(m.get('check')) or not norm(m.get('reason')):
            E(f"manual check for {m.get('req_id')}: needs an existing req_id, a check and a reason")

    # ---- traceability ----
    runs = {r.get('id'): r for r in d['runs']}
    run_order = [r.get('id') for r in d['runs']]
    cases = {(c.get('run_id'), c.get('id')): c for c in d['cases']}
    by_req: dict[str, list[dict]] = {}
    tests = {}
    integ = d['integrity']
    locked = integ.get('test_hashes_at_red') or {}
    for t in d['traceability']:
        tid = t.get('test_id')
        if tid in tests:
            E(f'duplicate test_id {tid}')
        tests[tid] = t
        by_req.setdefault(t.get('req_id'), []).append(t)
        if t.get('req_id') not in reqs:
            E(f"{tid}: requirement {t.get('req_id')} does not exist"); continue
        if reqs[t['req_id']].get('testability') == 'not_unit_testable':
            E(f"{tid}: {t['req_id']} is not_unit_testable but has a test; change its testability or remove the row")
        if t.get('level') not in LEVELS:
            E(f"{tid}: level must be one of {sorted(LEVELS)}")
        if t.get('kind') not in KINDS:
            E(f"{tid}: kind must be one of {sorted(KINDS)}")
        if not t.get('test_path') or not t.get('test_name'):
            E(f'{tid}: test_path and test_name are required'); continue
        if t['test_path'] not in changed:
            E(f"{tid}: {t['test_path']} is not listed in files_changed")
        red, green = t.get('red') or {}, t.get('green') or {}
        st = red.get('status')
        if st not in RED:
            E(f'{tid}: red.status must be one of {sorted(RED)}'); continue
        for h in red.get('history') or []:
            if (h.get('run_id'), h.get('case_id')) not in cases:
                E(f"{tid}: history references case {h.get('case_id')!r} not found in run {h.get('run_id')}")
        if st == 'not_run':
            if not norm(red.get('reason')):
                E(f'{tid}: red not_run needs a reason (e.g. the service it needs is unavailable); never fake a red')
        else:
            run, case = runs.get(red.get('run_id')), cases.get((red.get('run_id'), red.get('case_id')))
            if not run:
                E(f"{tid}: red run {red.get('run_id')} not found"); continue
            if run.get('phase') != 'red':
                E(f"{tid}: red evidence must come from a run with phase red (run {run['id']} is {run.get('phase')})")
            if not case:
                E(f"{tid}: case {red.get('case_id')!r} not found in run {red.get('run_id')}"); continue
            if case.get('file') and case['file'] != t['test_path']:
                E(f"{tid}: case {case['id']} is in {case['file']}, not {t['test_path']}")
            if t['test_name'] not in case.get('name', ''):
                E(f"{tid}: case {case['id']} is not test {t['test_name']}")
            run_hash = (run.get('file_hashes') or {}).get(t['test_path'])
            if not run_hash:
                E(f"{tid}: run {run['id']} did not hash {t['test_path']} (parse_results.py --hash)")
            elif locked.get(t['test_path']) and run_hash != locked[t['test_path']]:
                E(f"{tid}: the red evidence in run {run['id']} is for a different version of {t['test_path']} "
                  f"than the one locked; re-run the red phase")
            if st == 'valid_red':
                if case.get('status') not in ('failed', 'error') or case.get('failure_type') != 'assertion':
                    E(f"{tid}: valid_red needs an assertion failure; run {run['id']} recorded "
                      f"{case.get('status')}/{case.get('failure_type')}. Import, syntax, fixture and collection "
                      f"errors are not valid reds")
                if not norm(red.get('reason')):
                    E(f'{tid}: valid_red needs a reason saying why the failure is the missing behaviour')
            elif st == 'invalid_red':
                E(f'{tid}: still invalid_red; fix the infrastructure error (or add a signature-only stub) and re-run, '
                  f'or mark it not_run with a reason')
            elif st == 'unexpected_green':
                if case.get('status') != 'passed':
                    E(f"{tid}: unexpected_green but run {run['id']} recorded {case.get('status')}")
                inv = red.get('investigation') or {}
                if inv.get('finding') not in FINDINGS or not norm(inv.get('detail')):
                    E(f'{tid}: unexpected_green must be investigated (finding: {", ".join(sorted(FINDINGS))}, detail)')
                elif inv['finding'] == 'test_ineffective':
                    E(f'{tid}: the test cannot fail, so it proves nothing; rewrite it and re-run the red phase')
                elif inv['finding'] == 'already_implemented' and not inv.get('evidence'):
                    E(f'{tid}: already_implemented needs evidence (file:line of the existing behaviour)')
        if green.get('status') not in GREEN:
            E(f'{tid}: green.status must be one of {sorted(GREEN)}')
        elif mode == 'red_only':
            if green['status'] != 'not_run':
                E(f'{tid}: red_only mode hands over red tests; green.status must be not_run')
        elif st == 'valid_red':
            if green['status'] != 'passed':
                E(f"{tid}: {mode} mode: valid red not made green (green.status {green['status']})")
            else:
                grun, gcase = runs.get(green.get('run_id')), cases.get((green.get('run_id'), green.get('case_id')))
                if not grun or grun.get('phase') != 'green' or not gcase or gcase.get('status') != 'passed':
                    E(f'{tid}: green passed needs a passing case in a run with phase green')
                elif locked.get(t['test_path']) and (grun.get('file_hashes') or {}).get(t['test_path']) != locked[t['test_path']]:
                    if not integ.get('locked_tests_modified'):
                        E(f"{tid}: the test file that passed in {grun['id']} is not the file locked at red "
                          f"(weakened or edited after the red phase)")

    # ---- requirement coverage ----
    for rid, r in reqs.items():
        rows = by_req.get(rid, [])
        if r.get('testability') == 'testable':
            if not rows:
                E(f'{rid}: testable but has no test; write one, or set testability with a reason')
            elif not any((t.get('red') or {}).get('status') in ('valid_red', 'unexpected_green', 'not_run') for t in rows):
                E(f'{rid}: no test has valid red evidence')
        elif r.get('testability') == 'needs_clarification':
            if not any(rid in (q.get('related_reqs') or []) for q in d['open_questions']):
                E(f'{rid}: needs_clarification but no open question refers to it')
        elif r.get('testability') == 'not_unit_testable' and rid not in manual:
            E(f'{rid}: not_unit_testable needs a manual_checks entry')

    # ---- integrity ----
    paths = sorted({t['test_path'] for t in tests.values() if t.get('test_path') and (t.get('red') or {}).get('status') != 'not_run'})
    for p in paths:
        if p not in locked:
            E(f'{p}: not locked; run hash_tests.py lock after the red phase')
    if integ.get('locked_tests_modified'):
        if not norm(integ.get('locked_tests_modified_justification')) or not integ.get('locked_tests_modified_approved_by_user'):
            E('locked_tests_modified is true: a justification and the user\'s approval are required '
              '(weakening a locked test to reach green is not allowed)')
    new_paths = {t['test_path'] for t in tests.values()}
    listed_existing = {e.get('path'): e for e in integ.get('existing_tests_modified') or []}
    for p, f in changed.items():
        if f.get('kind') == 'test' and f.get('change') == 'modified' and p not in new_paths and p not in listed_existing:
            E(f'{p}: an existing test was modified but is not listed in integrity.existing_tests_modified')
    for p, e in listed_existing.items():
        if e.get('justification_req') not in reqs or not norm(e.get('detail')):
            E(f'{p}: existing test modified without a requirement that changes that behaviour and a detail')
        if p not in changed:
            E(f'{p}: listed in existing_tests_modified but not in files_changed')
    stubs = integ.get('stubs_added') or []
    stub_paths = {s.get('path') for s in stubs}
    for s in stubs:
        if not s.get('path') or not s.get('symbol'):
            E('stubs_added entries need path and symbol'); continue
        if s['path'] not in changed or changed[s['path']].get('kind') != 'stub':
            E(f"{s['path']}: stub file must be in files_changed with kind stub")
        if repo and (repo / s['path']).exists() and s['symbol'].split('.')[-1] not in (repo / s['path']).read_text(encoding='utf-8', errors='replace'):
            E(f"{s['path']}: stub symbol {s['symbol']} not found in the file")
    for p, f in changed.items():
        if f.get('kind') not in FILE_KINDS:
            E(f"{p}: kind must be one of {sorted(FILE_KINDS)}")
        if f.get('change') not in ('added', 'modified'):
            E(f"{p}: change must be added or modified (the skill never deletes files)")
        if f.get('kind') == 'stub' and p not in stub_paths:
            E(f'{p}: kind stub but not described in integrity.stubs_added')
        if f.get('kind') == 'source' and mode == 'red_only':
            E(f'{p}: red_only mode may only add signature-only stubs (kind stub), not implementation code')
        if (f.get('kind') == 'manifest' or Path(p).name in MANIFEST_NAMES) and not f.get('approved_by_user'):
            E(f'{p}: dependency manifest changed without approved_by_user (never install without asking)')
        if repo and not (repo / p).exists():
            E(f'{p}: listed in files_changed but not found')

    # ---- green phase, re-red and regressions ----
    if mode != 'red_only':
        rr = integ.get('re_red') or {}
        run = runs.get(rr.get('run_id'))
        if not run or run.get('phase') != 're_red':
            E(f'{mode} mode needs integrity.re_red with a run (phase re_red) that reverts the implementation')
        else:
            ok = True
            for t in tests.values():
                if (t.get('red') or {}).get('status') != 'valid_red':
                    continue
                c = [c for (rid_, _), c in cases.items() if rid_ == run['id'] and t['test_name'] in c.get('name', '')
                     and (not c.get('file') or c['file'] == t['test_path'])]
                if not c or c[0].get('status') not in ('failed', 'error') or c[0].get('failure_type') != 'assertion':
                    ok = False
                    E(f"{t['test_id']}: did not fail again in re-red run {run['id']}; the test may not depend on the new code")
            if bool(rr.get('confirmed')) != ok:
                E(f"integrity.re_red.confirmed is {rr.get('confirmed')} but the re-red run says {ok}")
    regression_runs = [r for r in d['runs'] if r.get('phase') == 'regression']
    if mode != 'red_only' and not regression_runs:
        E(f'{mode} mode needs a full-suite run with phase regression after green')
    if mode == 'red_only' and stubs and not regression_runs:
        E('stubs changed application code: run the existing suite (phase regression) to show nothing else broke')
    if regression_runs:
        last = regression_runs[-1]
        new_names = {(t['test_path'], t['test_name']) for t in tests.values()}
        listed = {norm(r.get('test')) for r in d['regressions']}
        for c in d['cases']:
            if c.get('run_id') != last['id'] or c.get('status') not in ('failed', 'error'):
                continue
            if any(name in c.get('name', '') and (not c.get('file') or c['file'] == path) for path, name in new_names):
                continue
            if norm(c['id']) not in listed and norm(c.get('name')) not in listed:
                E(f"existing test {c['id']} failed in regression run {last['id']} but is not listed in regressions")
    for r in d['regressions']:
        if r.get('run_id') not in runs or not r.get('evidence'):
            E(f"regression {r.get('test')}: needs a run_id that exists and evidence")
        if r.get('pre_existing') and not norm(r.get('pre_existing_evidence')):
            E(f"regression {r.get('test')}: pre_existing needs evidence it failed before this change (e.g. a baseline run)")

    # ---- repository re-checks ----
    if repo:
        for p in paths:
            fp = repo / p
            if not fp.exists():
                E(f'{p}: test file not found'); continue
            body = fp.read_text(encoding='utf-8', errors='replace')
            for t in tests.values():
                if t['test_path'] == p and re.sub(r'\[.*\]$', '', t['test_name']) not in body:
                    E(f"{t['test_id']}: test {t['test_name']} not found in {p}")
            if locked.get(p) and hash_tests.sha256(fp) != locked[p] and not integ.get('locked_tests_modified'):
                E(f'{p}: changed since it was locked at red; the handed-over tests are not the ones seen failing')
            for m in hash_tests.weakening_markers(fp):
                E(f"{p}:{m['line']}: {m['marker']} hides a failing test (skip/xfail/only are not allowed)")
    else:
        W('validated without --repo: test files, the red lock and skip markers were not re-checked')

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
    if calc['unexpected_greens']:
        W(f"{calc['unexpected_greens']} test(s) passed before any implementation; see their investigation")
    return errors, warnings

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Validate a Ticket to TDD report.')
    ap.add_argument('report')
    ap.add_argument('--ticket', help='ticket.json from fetch_ticket.py, to re-check requirement excerpts')
    ap.add_argument('--repo', help='repository root, to re-check test files, the red lock and skip markers')
    ap.add_argument('--write-summary', action='store_true', help='recompute summary counts in place first')
    args = ap.parse_args(argv)
    path = Path(args.report)
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        ticket = json.loads(Path(args.ticket).read_text(encoding='utf-8')) if args.ticket else None
    except (OSError, ValueError) as exc:
        print(f'INVALID: cannot read input: {exc}'); return 1
    if args.write_summary:
        d.setdefault('summary', {}).update(compute_summary(d))
        d['summary'].setdefault('headline', '')
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errors, warnings = check(d, ticket, Path(args.repo).resolve() if args.repo else None)
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
