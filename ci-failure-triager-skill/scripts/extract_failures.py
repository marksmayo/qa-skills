#!/usr/bin/env python3
"""List every failure in one attempt of a normalised CI run, with its log lines (stdlib only).

    python extract_failures.py --run run/ [--attempt 1] --out run/failures.json
    python extract_failures.py --self-test

A failed job with failing JUnit cases gives one failure per case (kind `test`, source `junit`). A failed
step whose output holds a test runner's failure summary (Vitest, Jest, node:test) gives one failure per
failing test (kind `test`, source `console`). Any other failed step gives one failure for the step (kind
`step`).

The failed step's output is found in the log by its group: GitHub titles a step's group `Run <first line
of the script>` or `Run <action>@<ref>`, never by the step's `name:`, so a group is matched by its title
when that is unique, and otherwise by order (the Nth failed step is the Nth step group holding a
`##[error]` line). The excerpt skips the script GitHub echoes at the top of the group, and anchors on the
error lines just before the step's closing `##[error]`. ANSI colour codes and timestamps are stripped
before matching and quoting (the validator strips them the same way).

Each failure keeps the log lines that show it, quoted with their line numbers, the files in its
traceback, its message, the kind of step it failed in (`test`, `setup` or `other`), and, for an aggregate
gate job that fails only because other jobs failed, `consequence_of` (the failures of those jobs). The
first attempt is the run as it first failed; later attempts are re-runs.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402

SIG = json.loads((HERE.parent / 'data' / 'signatures.json').read_text(encoding='utf-8'))
TEST_STEP = re.compile(SIG['test_step'], re.I)
SETUP_STEP = re.compile(SIG['setup_step'], re.I)
GATE = re.compile(SIG['aggregate_gate'], re.I)
TS = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
ERR = re.compile(r'(##\[error\]|\bERROR\b|\berror\b|Error:|Error\[|FAILED|FAILURE:|fatal:|Exception|\bERR_[A-Z0-9_]{3,}|npm ERR!|panicked at|\btimed out\b|\btimeout\b)')
PASSING = re.compile(r'(✓|√|✔|^\s*PASS\b|^\s*ok \d)')
PROCESS_DONE = re.compile(r'^##\[error\](Process completed with exit code|Bash exited with code|PowerShell exited with code|Script failed with)')
FRAME = re.compile(r'(?:File "([^"]+)", line (\d+)|^\s*(?:❯\s+|at\s+(?:.*\()?)?([\w./@-]+\.(?:py|ts|tsx|mts|cts|js|jsx|mjs|cjs|java|kt|go|rb|cs|rs|swift)):(\d+))', re.M)
SECRET = re.compile(r'(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|(?i:bearer)\s+[A-Za-z0-9._-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S{6,})')
STEP_HDR = re.compile(r'^##\[group\]Run (.*)$|^##\[section\]Starting: (.*)$')
TAIL = 40   # lines before a step's closing ##[error] searched first for the error


def redact(line: str) -> str:
    return SECRET.sub(lambda m: m.group(0)[:4] + '[REDACTED]', line)


def strip(line: str) -> str:
    """A log line as quoted: no BOM, timestamp, ANSI codes or carriage return."""
    return ANSI.sub('', TS.sub('', line.lstrip('﻿'))).rstrip('\r')


def frames(text: str) -> list[dict]:
    out, seen = [], set()
    for m in FRAME.finditer(text or ''):
        f = (m.group(1) or m.group(3)).replace('\\', '/')
        if '/node_modules/' in f or f.startswith(('node:', 'internal/')):
            continue
        f = re.sub(r'^.*?/(?=(tests?|src|lib|app|packages|e2e|spec)/)', '', f)
        key = (f, int(m.group(2) or m.group(4)))
        if key not in seen:
            seen.add(key)
            out.append({'file': key[0], 'line': key[1]})
    return out


def step_sections(lines: list[str]) -> list[dict]:
    """Each step's block in a job log, 1-based: GitHub `##[group]Run ...` groups and Azure `##[section]Starting: ...`.

    `body` is the first line of the step's own output: after the `##[endgroup]` that closes the echoed
    script, inputs and env on GitHub. `errors` are the `##[error]` lines in the block."""
    marks = []
    for i, l in enumerate(lines):
        m = STEP_HDR.match(strip(l))
        if m:
            marks.append((i + 1, (m.group(1) or m.group(2) or '').strip(), bool(m.group(1))))
    out = []
    for k, (n, title, github) in enumerate(marks):
        end = marks[k + 1][0] - 1 if k + 1 < len(marks) else len(lines)
        body = n + 1
        if github:
            body = next((x + 1 for x in range(n + 1, end + 1) if strip(lines[x - 1]).startswith('##[endgroup]')), n + 1)
        out.append({'title': title, 'start': n, 'body': min(body, end), 'end': end,
                    'errors': [x for x in range(n, end + 1) if strip(lines[x - 1]).startswith('##[error]')]})
    return out


def match_sections(secs: list[dict], failed_steps: list[dict]) -> list[tuple[dict | None, str]]:
    """(section, basis) for each failed step, in step order."""
    with_err = [s for s in secs if s['errors']]
    out = []
    for k, st in enumerate(failed_steps):
        key = re.sub(r'^Run ', '', st['name']).strip()
        titled = [s for s in secs if key and (s['title'] == key or s['title'].startswith(key) or key.startswith(s['title']) and len(s['title']) > 20)]
        titled_err = [s for s in titled if s['errors']]
        if len(titled_err) == 1:
            out.append((titled_err[0], 'title'))
        elif len(with_err) == len(failed_steps):
            out.append((with_err[k], 'order of the steps with an error'))
        elif titled_err:
            out.append((titled_err[0], 'title (first of several)'))
        elif k < len(with_err):
            out.append((with_err[k], 'order of the steps with an error'))
        elif len(titled) == 1:
            out.append((titled[0], 'title'))
        else:
            out.append((None, 'not found'))
    return out


def excerpt(lines: list[str], start: int, end: int, limit: int = 12) -> list[dict]:
    return [{'n': n, 'text': redact(strip(lines[n - 1]))} for n in range(max(1, start), min(end, start + limit - 1) + 1)]


def _is_err(text: str) -> bool:
    return bool(ERR.search(text)) and not PASSING.search(text) and not PROCESS_DONE.match(text)


def section_excerpt(lines: list[str], sec: dict | None) -> list[dict]:
    """The step's error lines: the first error-like line in the TAIL lines before its closing
    ##[error] (else the first in its output), from one line before, up to 12 lines."""
    lo, hi = (sec['body'], sec['end']) if sec else (1, len(lines))
    close = next((n for n in (sec['errors'] if sec else []) if PROCESS_DONE.match(strip(lines[n - 1]))), None) or \
        (sec['errors'][-1] if sec and sec['errors'] else hi)
    tail_lo = max(lo, close - TAIL)
    first = next((n for n in range(tail_lo, close + 1) if _is_err(strip(lines[n - 1]))), None) \
        or next((n for n in range(lo, close + 1) if _is_err(strip(lines[n - 1]))), None)
    if first is None:
        return excerpt(lines, max(lo, close - 7), close, 8)
    return excerpt(lines, max(lo, first - 1), close, 12)


def test_lines(lines: list[str], name: str) -> list[dict]:
    """The test's section of the failure output: from its ___ header to the next header."""
    hdr = re.compile(r'^_{3,} (\S+\.)?' + re.escape(name) + r'(\[.*\])? _{3,}$')
    for i, l in enumerate(lines):
        if hdr.match(strip(l).strip()):
            j = i + 1
            while j < len(lines) and not re.match(r'^(_{3,} \S.* _{3,}|={3,}.*={3,}|-+ generated)', strip(lines[j]).strip()):
                j += 1
            return excerpt(lines, i + 1, j)
    for i, l in enumerate(lines):
        if name in l and ERR.search(strip(l)):
            return excerpt(lines, i + 1, i + 6)
    return []


# --- console test results (no JUnit) -------------------------------------------------------------

JSFILE = r'(\S+?\.(?:[cm]?[jt]sx?))'
VITEST_FAIL = re.compile(r'^\s*FAIL\s+(?:(?!\S+\.[cm]?[jt]sx?\b)\S+\s+)?' + JSFILE + r'(?:\s+>\s+(.+?))?(?:\s+\[.*\])?\s*$')
JEST_FAIL = re.compile(r'^\s*FAIL\s+(?:(?!\S+\.[cm]?[jt]sx?\b)\S+\s+)?' + JSFILE + r'(?:\s+\([\d.]+\s*m?s\))?\s*$')
JEST_CASE = re.compile(r'^\s*●\s+(.+?)\s*$')
NODE_FAILING = re.compile(r'^\s*✖ failing tests:\s*$')
NODE_AT = re.compile(r'^\s*test at ' + JSFILE + r':(\d+):\d+\s*$')
NODE_CASE = re.compile(r'^\s*✖ (.+?) \([\d.]+m?s\)\s*$')
BLOCK_END = re.compile(r'^\s*(⎯{3,}|-{20,}|={20,}|Test Files\b|Tests:|Test Suites:|ℹ tests \d)')
SUMMARY = re.compile(r'^\s*(Test Files\s+\d|Tests:\s+\d|Test Suites:\s+\d|ℹ tests \d|=+ .*\b(passed|failed)\b.* =+$|test result: )')


def _ftype(msg: str) -> str:
    return 'assertion' if re.search(r'AssertionError|expect\(|Expected|toBe|toEqual|assert', msg) else 'error'


def console_tests(lines: list[str], lo: int, hi: int) -> list[dict]:
    """Failing tests in a Vitest, Jest or node:test console summary between lines lo..hi (1-based)."""
    txt = {n: strip(lines[n - 1]) for n in range(lo, hi + 1)}
    starts = []  # (line, file, name)
    jest_file = None
    node_block, node_file = False, None
    for n in range(lo, hi + 1):
        t = txt[n]
        m = VITEST_FAIL.match(t)
        if m and m.group(2):
            starts.append((n, m.group(1), m.group(2).replace(' › ', ' > ').strip()))
            jest_file = None
            continue
        m = JEST_FAIL.match(t)
        if m:
            jest_file = m.group(1)
            if not any(txt.get(x, '').lstrip().startswith('●') for x in range(n + 1, min(hi, n + 400) + 1)):
                starts.append((n, jest_file, None))   # a file that failed to load
            continue
        m = JEST_CASE.match(t)
        if m and jest_file and not m.group(1).startswith(('Console', 'Test suite failed to run')):
            starts.append((n, jest_file, m.group(1).replace(' › ', ' > ')))
            continue
        if m and jest_file and m.group(1).startswith('Test suite failed to run'):
            starts.append((n, jest_file, None))
            continue
        if NODE_FAILING.match(t):
            node_block = True
            continue
        if node_block:
            m = NODE_AT.match(t)
            if m:
                node_file = m.group(1)
                continue
            m = NODE_CASE.match(t)
            if m and node_file:
                starts.append((n, node_file, m.group(1)))
                node_file = None
    out, seen = [], set()
    for k, (n, f, name) in enumerate(starts):
        tid = f'{f} > {name}' if name else f
        if tid in seen:
            continue
        seen.add(tid)
        nxt = starts[k + 1][0] - 1 if k + 1 < len(starts) else hi
        end = next((x - 1 for x in range(n + 1, nxt + 1) if BLOCK_END.match(txt[x])), nxt)
        body = '\n'.join(txt[x] for x in range(n + 1, end + 1))
        msg = next((txt[x].strip() for x in range(n + 1, end + 1) if txt[x].strip()), '')
        out.append({'test_id': tid, 'test_file': f, 'line': n, 'end': end, 'message': msg,
                    'failure_type': _ftype(msg) if name else 'error', 'frames': frames(body)})
    return out


def has_test_summary(lines: list[str], lo: int, hi: int) -> bool:
    return any(SUMMARY.match(strip(lines[n - 1])) for n in range(lo, hi + 1))


def step_kind(step_name: str, sec: dict | None, lines: list[str]) -> str:
    title = sec['title'] if sec else ''
    if TEST_STEP.search(step_name) or TEST_STEP.search(title) or (sec and has_test_summary(lines, sec['body'], sec['end'])):
        return 'test'
    if SETUP_STEP.search(step_name) or SETUP_STEP.search(title):
        return 'setup'
    return 'other'


def is_gate(job: dict, step_name: str | None, sec: dict | None, lines: list[str]) -> bool:
    if GATE.search(job['name']) or (step_name and GATE.search(step_name)):
        return True
    if sec:
        return any(re.search(r'needs\.|toJSON\(needs\)|\.result\s*(!=|==)\s*["\']?(success|failure)', strip(lines[n - 1]))
                   for n in range(sec['start'], sec['body']))
    return False


def extract(run_dir: Path, run: dict, attempt: int) -> list[dict]:
    att = next(a for a in run['attempts'] if a['n'] == attempt)
    out = []
    for j in att['jobs']:
        if j['conclusion'] in ('success', 'skipped', None):
            continue
        lines = (run_dir / j['log']).read_text(encoding='utf-8').split('\n') if j.get('log') and (run_dir / j['log']).exists() else []
        log_status = j.get('log_status') or ('fetched' if lines else 'missing')
        base = {'job': j['name'], 'job_id': j['id'], 'log_status': log_status}
        cases = []
        for x in j.get('junit', []):
            cs, _ = parse_junit(run_dir / x, f'A{attempt}')
            cases += [c for c in cs if c['status'] in ('failed', 'error')]
        failed_steps = [s for s in j['steps'] if s['conclusion'] == 'failure']
        secs = step_sections(lines) if lines else []
        matched = match_sections(secs, failed_steps) if lines else [(None, 'no log') for _ in failed_steps]
        for c in cases:
            name = c['id'].split('::')[-1]
            out.append({**base, 'step': next((s['name'] for s in failed_steps if TEST_STEP.search(s['name'])), failed_steps[0]['name'] if failed_steps else None),
                        'kind': 'test', 'source': 'junit', 'step_kind': 'test', 'test_id': c['id'], 'test_file': c.get('file'), 'failure_type': c.get('failure_type'),
                        'message': redact((c.get('message_excerpt') or '').split('\n')[0])[:300],
                        'frames': frames(c.get('message_excerpt') or ''), 'log_excerpt': {'path': j.get('log'), 'lines': test_lines(lines, name)}})
        for s, (sec, basis) in zip(failed_steps, matched):
            kind = step_kind(s['name'], sec, lines)
            if cases and (TEST_STEP.search(s['name']) or kind == 'test'):
                continue  # explained by its failing JUnit cases
            ctests = console_tests(lines, sec['body'], sec['end']) if sec else []
            if ctests:
                for c in ctests:
                    out.append({**base, 'step': s['name'], 'kind': 'test', 'source': 'console', 'step_kind': 'test', 'test_id': c['test_id'],
                                'test_file': c['test_file'], 'failure_type': c['failure_type'], 'message': redact(c['message'])[:300], 'frames': c['frames'],
                                'section': {'title': sec['title'], 'basis': basis},
                                'log_excerpt': {'path': j.get('log'), 'lines': excerpt(lines, c['line'], c['end'], 12)}})
                continue
            ex = section_excerpt(lines, sec) if lines else []
            msg = next((re.sub(r'^##\[error\]', '', l['text']) for l in ex if _is_err(l['text'])), ex[-1]['text'] if ex else '')
            if not lines:
                msg = f'no log ({log_status}' + (f': {j.get("log_error")}' if j.get('log_error') else '') + ')'
            f = {**base, 'step': s['name'], 'kind': 'step', 'source': 'log', 'step_kind': kind, 'test_id': None, 'test_file': None,
                 'failure_type': 'step', 'message': msg[:300], 'frames': frames('\n'.join(l['text'] for l in ex)),
                 'section': {'title': sec['title'] if sec else None, 'basis': basis}, 'log_excerpt': {'path': j.get('log'), 'lines': ex}}
            if is_gate(j, s['name'], sec, lines):
                f['aggregate_gate'] = True
            out.append(f)
        if not cases and not failed_steps:
            out.append({**base, 'step': None, 'kind': 'step', 'source': 'log', 'step_kind': 'other', 'test_id': None, 'test_file': None, 'failure_type': 'job',
                        'message': f'job concluded {j["conclusion"]} with no failed step', 'frames': [],
                        'log_excerpt': {'path': j.get('log'), 'lines': excerpt(lines, max(1, len(lines) - 7), len(lines), 8) if lines else []}})
    for i, f in enumerate(out, 1):
        f['id'] = f'X{i:02d}'
    for f in out:
        if f.pop('aggregate_gate', False):
            others = [g['id'] for g in out if g['job_id'] != f['job_id'] and not g.get('consequence_of')]
            if others:
                f['consequence_of'] = others
    return out


# --- self-test ---------------------------------------------------------------------------------------

def _gh_log(groups: list[tuple[str, list[str], list[str]]]) -> str:
    """A GitHub-style job log: each group is (title, echoed script lines, output lines)."""
    t = '2026-01-01T00:00:00.0000000Z '
    out = ['﻿' + t + '##[group]Runner Image', t + 'Image: ubuntu', t + '##[endgroup]']
    for title, script, body in groups:
        out.append(t + '##[group]Run ' + title)
        out += [t + '\x1b[36;1m' + s + '\x1b[0m' for s in script]
        out += [t + 'shell: /usr/bin/bash -e {0}', t + '##[endgroup]']
        out += [t + b for b in body]
    return '\n'.join(out + [t + 'Post job cleanup.', ''])


def self_test() -> int:
    import tempfile
    fails = []
    check = lambda c, w: None if c else fails.append(w)  # noqa: E731
    E = '##[error]Process completed with exit code 1.'
    vitest_out = ['', ' \x1b[32m✓\x1b[39m unit tests/unit/error-codes.test.ts (4 tests) 12ms',
                  '   \x1b[31m❯\x1b[39m parcels (1)', '\x1b[31m     ×\x1b[31m rejects an empty label\x1b[39m',
                  '', '\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\x1b[1m\x1b[41m Failed Tests 1 \x1b[49m\x1b[22m\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m', '',
                  '\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m \x1b[30m\x1b[45m unit \x1b[49m\x1b[39m tests/unit/parcels.test.ts\x1b[2m > \x1b[22mparcels\x1b[2m > \x1b[22mrejects an empty label',
                  '\x1b[31m\x1b[1mAssertionError\x1b[22m: expected 200 to be 400\x1b[39m', '',
                  '\x1b[36m \x1b[2m❯\x1b[22m tests/unit/parcels.test.ts:\x1b[2m21:17\x1b[22m\x1b[39m', '',
                  '\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯\x1b[22m\x1b[39m', '',
                  '\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m | 3 passed (4)', '##[error]AssertionError: expected 200 to be 400', E]
    jobs = {
        # step named in the workflow ("Publish packages"), group titled by its script; error printed by the tool
        'publish': _gh_log([('actions/checkout@v5', [], ['Syncing repository']),
                            ('pnpm install --frozen-lockfile', ['pnpm install --frozen-lockfile'], ['Done in 3s']),
                            ('pnpm exec release-tool publish', ['pnpm exec release-tool publish', 'echo "::error::release failed"'],
                             ['Publishing 3 packages', 'package @acme/one published', 'Some packages failed to publish:',
                              '└ ERR_PNPM_FAILED_TO_PUBLISH: Failed to publish package @acme/two@0.0.1 (status 404 Not Found): {"error":"Not found"}', '', E])]),
        # the script name contains "error"; the real cause is later
        'check': _gh_log([('pnpm docs:check-errors', ['pnpm docs:check-errors'], ['$ node scripts/check-error-codes.ts', 'ok: 120 codes']),
                          ('"$RUNNER_TEMP/validator" validate server.json', ['"$RUNNER_TEMP/validator" validate server.json'],
                           ['Validating against https://registry.example.test...', 'Error: validation failed: Post "https://registry.example.test/v0/validate": net/http: TLS handshake timeout', E])]),
        'unit': _gh_log([('pnpm test', ['pnpm test'], vitest_out)]),
        'gate': _gh_log([('failed="$(jq -r \'[to_entries[] | select(.value.result != "success")] | join(", ")\' <<< "$RESULTS")"',
                          ['failed="$(jq -r \'[to_entries[] | select(.value.result != "success")] | join(", ")\' <<< "$RESULTS")"',
                           '[ -z "$failed" ] || { echo "::error::$failed"; exit 1; }'], ['##[error]check failure', E])]),
    }
    steps = {'publish': 'Publish packages', 'check': 'Validate the server manifest', 'unit': 'Unit tests', 'gate': 'Require every job to pass'}
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        rjobs = []
        for i, (name, text) in enumerate(jobs.items(), 1):
            (d / 'logs').mkdir(exist_ok=True)
            (d / 'logs' / f'{i}.txt').write_text(text, encoding='utf-8')
            rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched',
                          'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': steps[name], 'conclusion': 'failure'}]})
        rjobs.append({'id': 9, 'name': 'expired', 'conclusion': 'failure', 'junit': [], 'log': None, 'log_status': 'fetch_failed', 'log_error': 'HTTP 401: Unauthorized',
                      'steps': [{'number': 1, 'name': 'Build', 'conclusion': 'failure'}]})
        run = {'run': {'id': 1}, 'attempts': [{'n': 1, 'jobs': rjobs}]}
        fs = extract(d, run, 1)
        by = {f['job']: f for f in fs}
        pub, chk, unit, gate, exp = by['publish'], by['check'], by['unit'], by['gate'], by['expired']
        check('ERR_PNPM_FAILED_TO_PUBLISH' in pub['message'], f'publish: message is {pub["message"]!r}')
        check(not any('::error::' in l['text'] for l in pub['log_excerpt']['lines']), 'publish: excerpt quotes the echoed script')
        check(pub['step_kind'] == 'other', f'publish: step kind {pub["step_kind"]}')
        check('TLS handshake timeout' in chk['message'], f'check: message is {chk["message"]!r}')
        check(unit['kind'] == 'test' and unit['source'] == 'console' and unit['test_id'] == 'tests/unit/parcels.test.ts > parcels > rejects an empty label',
              f'vitest: {unit.get("kind")} {unit.get("test_id")}')
        check(unit['message'].startswith('AssertionError: expected 200 to be 400') and unit['failure_type'] == 'assertion', f'vitest message {unit["message"]!r}')
        check(unit['frames'] == [{'file': 'tests/unit/parcels.test.ts', 'line': 21}], f'vitest frames {unit["frames"]}')
        check(not any('\x1b' in l['text'] for f in fs for l in f['log_excerpt']['lines']), 'ANSI codes left in an excerpt')
        check(sum(1 for f in fs if f['job'] == 'unit') == 1, 'vitest step reported as well as its test')
        check(gate.get('consequence_of') == [pub['id'], chk['id'], unit['id'], exp['id']], f'gate consequence_of {gate.get("consequence_of")}')
        check(exp['log_status'] == 'fetch_failed' and 'fetch_failed' in exp['message'] and exp['log_excerpt']['lines'] == [], 'failed log fetch not explicit')
        # Jest and node:test summaries
        jest = ['FAIL src/cart.test.ts', '  ● cart › adds an item', '', '    expect(received).toBe(expected)', '',
                '      at Object.<anonymous> (src/cart.test.ts:14:22)', '', 'Tests:       1 failed, 9 passed, 10 total']
        node = ['✖ failing tests:', '', 'test at test/quote.test.mjs:8:1', '✖ rounds a half fare (1.2ms)', '  AssertionError [ERR_ASSERTION]: 12.12 == 12.13', 'ℹ tests 4']
        cj = console_tests(jest, 1, len(jest))
        cn = console_tests(node, 1, len(node))
        check([c['test_id'] for c in cj] == ['src/cart.test.ts > cart > adds an item'] and cj[0]['frames'] == [{'file': 'src/cart.test.ts', 'line': 14}], f'jest {cj}')
        check([c['test_id'] for c in cn] == ['test/quote.test.mjs > rounds a half fare'] and cn[0]['failure_type'] == 'assertion', f'node:test {cn}')
    if fails:
        print('extract_failures self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('extract_failures self-test: ok (step groups by title or error order, echoed script skipped, ANSI stripped, '
          'Vitest/Jest/node:test console failures, aggregate gate, failed log fetch)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path)
    ap.add_argument('--attempt', type=int, default=1)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.run and a.out):
        ap.error('--run and --out are required')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    fs = extract(a.run, run, a.attempt)
    a.out.write_text(json.dumps({'run_id': run['run']['id'], 'attempt': a.attempt, 'failures': fs}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(fs)} failure(s) in attempt {a.attempt} -> {a.out}')
    for f in fs:
        print(f'  {f["id"]} {f["job"]:14} {f["kind"]:5} {f["test_id"] or f["step"]}: {f["message"][:90]}')
        if f.get('log_status') not in ('fetched', None):
            print(f'      WARNING: no log for this job ({f["log_status"]}); its failure cannot be read from the log')
    return 0


if __name__ == '__main__':
    sys.exit(main())
