#!/usr/bin/env python3
"""List every failure in one attempt of a normalised CI run, with its log lines (stdlib only).

    python extract_failures.py --run run/ [--attempt 1] --out run/failures.json
    python extract_failures.py --self-test

A failed job with failing JUnit cases gives one failure per case (kind `test`, source `junit`). A failed
step whose output holds a test runner's failure summary (Vitest, Jest, node:test) gives one failure per
failing test (kind `test`, source `console`). So does `go test` output, plain or `-json`: a failing test or
subtest (`--- FAIL: TestX/sub`, its parent dropped), the package from its `FAIL\t<pkg>` line, the tests a
`panic: test timed out after` names as running (failure type `timeout`), and a `[build failed]` or
`[setup failed]` package (its compile error as the message); test ids are `<package> > <TestX/sub>`. Any
other failed step gives one failure for the step (kind `step`); in a lint or format step its excerpt and
message anchor on the first finding (`file.go:12:5: message (linter)`, a `::error file=` annotation, a
"files are not formatted" verdict). Frames from the Go toolchain and module cache are dropped and the
runner's checkout prefix stripped, so the project's own frames are the ones kept.

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
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
from parse_results import (LT_ASSERT, LT_DOC, LT_PANIC_AT, LT_RUNNING, NT_LINE, libtest_target, nextest_target,  # noqa: E402
                           parse_junit, parse_libtest, parse_nextest_output)

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


# Frames that are never the project's: the Go toolchain (GOROOT on hosted runners, /usr/local/go, a toolchain
# downloaded by GOTOOLCHAIN), the module cache (GOPATH/pkg/mod, vendored modules), the runtime.
TOOLCHAIN_FRAME = re.compile(r'(/hostedtoolcache/(windows/)?go/|^/usr/(local/|lib/)?go/|/golang\.org/toolchain@|/pkg/mod/|^/?(go/)?src/(testing|runtime|time|sync|net|os|reflect|internal)/|'
                             r'^(testing|runtime|time|sync|reflect)/[\w.]+\.go$|/go-build\d*/|^_testmain\.go$|/vendor/|'
                             r'^/rustc/|/\.cargo/(registry|git)/|/\.rustup/toolchains/)')
# The runner's checkout directory: GitHub /home/runner/work/<repo>/<repo>/ (D:\a\<repo>\<repo>\ on Windows,
# /Users/runner/work/... on macOS), Azure DevOps /home/vsts/work/1/s/ (D:\a\1\s\).
CHECKOUT = re.compile(r'^(?:/home/runner/work|/Users/runner/work|[A-Za-z]:/a|/__w)/[^/]+/[^/]+/|^(?:/home/vsts/work|[A-Za-z]:/a)/\d+/s/')
ANNOT = re.compile(r'::(?:error|warning)\s+file=([^,:]+),line=(\d+)')
# Rust locations: a compiler or clippy finding (`  --> src/x.rs:12:5`), a panic (`panicked at src/x.rs:12:5`, older
# `panicked at 'msg', src/x.rs:12:5`) and a rustfmt diff (`Diff in /path/src/x.rs:12:` or `... at line 12:`)
# On a Windows runner rustfmt prints the extended-length form of an absolute path (`Diff in \\?\D:\a\repo\repo\src\x.rs:12:`):
# the `\\?\` prefix and the drive letter's colon belong to the path.
RUST_FRAME = re.compile(r"(?:-->\s+|panicked at (?:'.*?',\s+)?|^Diff in\s+)((?:\\\\\?\\)?(?:[A-Za-z]:)?[^\s:'\"]+\.rs)(?::| at line )(\d+)", re.M)
EXTENDED = re.compile(r'^//\?/(?:UNC/)?')     # that prefix (and its UNC form) once backslashes are slashes


def frames(text: str) -> list[dict]:
    """Files and lines of a failure's own code, in order: Python and JavaScript tracebacks, `<file>:<line>`
    frames, Go goroutine dumps and lint annotations. Dependency and toolchain frames (node_modules, the Go
    toolchain and module cache) are left out, and the runner's checkout prefix is stripped."""
    out, seen = [], set()
    found = [(m.start(), m.group(1) or m.group(3), m.group(2) or m.group(4)) for m in FRAME.finditer(text or '')]
    found += [(m.start(), m.group(1), m.group(2)) for m in ANNOT.finditer(text or '')]
    found += [(m.start(), m.group(1), m.group(2)) for m in RUST_FRAME.finditer(text or '')]
    for _, f, n in sorted(found, key=lambda x: x[0]):
        f = EXTENDED.sub('', f.replace('\\', '/'))
        if '/node_modules/' in f or f.startswith(('node:', 'internal/')) or TOOLCHAIN_FRAME.search(f):
            continue
        f, n_sub = CHECKOUT.subn('', f)
        if not n_sub and re.match(r'^(/|[A-Za-z]:/)', f):   # an absolute path outside the checkout: keep from tests/, src/ ...
            f = re.sub(r'^.*?/(?=(tests?|src|lib|app|packages|e2e|spec)/)', '', f)
        key = (f, int(n))
        if key not in seen:
            seen.add(key)
            out.append({'file': key[0], 'line': key[1]})
    return out


def hms(seconds: int) -> str:
    """976 -> 16m16s, 7010 -> 1h56m50s, 2 -> 2s."""
    h, rest = divmod(int(seconds), 3600)
    m, sec = divmod(rest, 60)
    return (f'{h}h{m:02d}m{sec:02d}s' if h else f'{m}m{sec:02d}s' if m else f'{sec}s')


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
    return (bool(ERR.search(text)) or bool(GO_ANCHOR.search(text)) or bool(RUST_ANCHOR.search(text))) and not PASSING.search(text) and not PROCESS_DONE.match(text)


def section_excerpt(lines: list[str], sec: dict | None) -> list[dict]:
    """The step's error lines: the first error-like line in the TAIL lines before its closing
    ##[error] (else the first in its output), from one line before, up to 12 lines."""
    lo, hi = (sec['body'], sec['end']) if sec else (1, len(lines))
    close = next((n for n in (sec['errors'] if sec else []) if PROCESS_DONE.match(strip(lines[n - 1]))), None) or \
        (sec['errors'][-1] if sec and sec['errors'] else hi)
    tail_lo = max(lo, close - TAIL)
    # a lint, vet, compile or format finding is the error itself, before any generic error-like line
    first = next((n for n in range(lo, close + 1) if GO_ANCHOR.search(strip(lines[n - 1])) or RUST_ANCHOR.search(strip(lines[n - 1]))), None) or next((n for n in range(tail_lo, close + 1) if _is_err(strip(lines[n - 1]))), None) \
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
SUMMARY = re.compile(r'^\s*(Summary \[|--- FAIL: |(ok|FAIL)\s+\S+\s+([\d.]+s|\(cached\)|\[(build|setup) failed\])|panic: test timed out after|Test Files\s+\d|Tests:\s+\d|Test Suites:\s+\d|ℹ tests \d|=+ .*\b(passed|failed)\b.* =+$|test result: )')


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


# --- go test (plain and -json) ---------------------------------------------------------------------

GO_FAIL = re.compile(r'^(\s*)--- FAIL: (\S+) \([\d.]+s\)\s*$')
GO_PKG_END = re.compile(r'^(FAIL|ok)\s+(\S+)(?:\s+(?:[\d.]+s|\(cached\)|\[(build|setup) failed\]|\[no test files\])).*$')
GO_TIMEOUT = re.compile(r'^panic: test timed out after \S+')
GO_RUNNING = re.compile(r'^\s+(Test\S*|Example\S*|Benchmark\S*|Fuzz\S*) \([\dhms.µ]+\)\s*$')
GO_MSG = re.compile(r'^\s*([\w.-]+\.go:\d+: .*)$')
GO_COMPILE = re.compile(r'^\s*((?:\.{0,2}/)?[\w./@-]+\.go:\d+(?::\d+)?: .+)$')
GO_TEST_FILE = re.compile(r'\b([\w.-]+_test\.go):\d+')


def _go_leaves(names: list[str]) -> set[str]:
    """Failing tests whose failure is their own: a parent test fails because a subtest failed, so it is dropped."""
    return {n for n in names if not any(o != n and o.startswith(n + '/') for o in names)}


def _go_item(pkg: str | None, name: str | None, line: int, end: int, msg: str, ftype: str, body: str) -> dict:
    tid = f'{pkg} > {name}' if pkg and name else (name or pkg or 'go test')
    tf = GO_TEST_FILE.search(body or '')
    tfile = (f'{pkg}/{tf.group(1)}' if pkg else tf.group(1)) if tf else None
    return {'test_id': tid, 'test_file': tfile, 'line': line, 'end': end, 'message': msg.strip(), 'failure_type': ftype, 'frames': frames(body)}


def go_json_tests(txt: dict[int, str], lo: int, hi: int) -> list[dict]:
    """Failing tests from `go test -json` events in the log (one JSON object per line)."""
    ev: dict[tuple, dict] = {}
    for n in range(lo, hi + 1):
        t = txt[n].strip()
        if not (t.startswith('{') and '"Action"' in t):
            continue
        try:
            e = json.loads(t)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        key = (e.get('Package'), e.get('Test'))
        rec = ev.setdefault(key, {'out': [], 'fail': None, 'first': n})
        if e.get('Action') == 'output':
            rec['out'].append((n, (e.get('Output') or '').rstrip('\n')))
        elif e.get('Action') == 'fail':
            rec['fail'] = n
    failed = [k for k, r in ev.items() if r['fail'] and k[1]]
    out = []
    for pkg in {k[0] for k in failed}:
        names = [k[1] for k in failed if k[0] == pkg]
        for name in sorted(_go_leaves(names)):
            r = ev[(pkg, name)]
            body = '\n'.join(o for _, o in r['out'])
            msg = next((GO_MSG.match(o).group(1) for _, o in r['out'] if GO_MSG.match(o)),
                       next((o.strip() for _, o in r['out'] if o.strip() and not o.lstrip().startswith(('=== ', '--- '))), f'--- FAIL: {name}'))
            start = next((n for n, o in r['out'] if GO_MSG.match(o) or o.lstrip().startswith('--- FAIL')), r['first'])
            out.append(_go_item(pkg, name, start, r['fail'], msg, 'assertion' if GO_TEST_FILE.search(msg) else 'error', body))
    for (pkg, name), r in ev.items():          # a package that failed with no failing test of its own (a build failure)
        if r['fail'] and not name and pkg not in {k[0] for k in failed}:
            body = '\n'.join(o for _, o in r['out'])
            msg = next((m.group(1) for _, o in r['out'] for m in [GO_COMPILE.match(o)] if m), f'FAIL {pkg}')
            out.append(_go_item(pkg, None, r['first'], r['fail'], msg, 'error', body))
    return out


def go_tests(lines: list[str], lo: int, hi: int) -> list[dict]:
    """Failing Go tests in a step's output, lines lo..hi (1-based): `--- FAIL: TestX/sub (0.00s)` (a parent
    failing only through its subtests is dropped), the package from the `FAIL\t<pkg>` line that closes its
    output, `panic: test timed out after ...` with the tests it names as running, `[build failed]` and
    `[setup failed]` packages (the compile error as the message), and `go test -json` events. A test's id is
    `<package> > <TestName/subtest>`."""
    txt = {n: strip(lines[n - 1]) for n in range(lo, hi + 1)}
    out = go_json_tests(txt, lo, hi)
    pending: list[dict] = []       # failing tests since the last package result
    timeout = None
    seg_start = lo                 # first line of the current package's output
    for n in range(lo, hi + 1):
        t = txt[n]
        m = GO_FAIL.match(t)
        if m:
            pending.append({'name': m.group(2), 'line': n})
            continue
        if GO_TIMEOUT.match(t):
            timeout = {'line': n, 'msg': t.strip(), 'tests': []}
            for k in range(n + 1, min(hi, n + 60) + 1):
                r = GO_RUNNING.match(txt[k])
                if r:
                    timeout['tests'].append(r.group(1))
                elif timeout['tests'] and not txt[k].strip():
                    break
            continue
        m = GO_PKG_END.match(t)
        if not m:
            continue
        status, pkg, broken = m.group(1), m.group(2), m.group(3)
        if status == 'FAIL':
            leaves = _go_leaves([p['name'] for p in pending])
            for k, p in enumerate(pending):
                if p['name'] not in leaves:
                    continue
                nxt = next((q['line'] for q in pending[k + 1:]), n)
                end = max(p['line'], nxt - 1)
                body = '\n'.join(txt[x] for x in range(p['line'] + 1, end + 1))
                msg = next((txt[x].strip() for x in range(p['line'] + 1, end + 1) if txt[x].strip() and txt[x].strip() != 'FAIL'), t.strip())
                out.append(_go_item(pkg, p['name'], p['line'], end, msg, 'assertion' if GO_TEST_FILE.search(msg) else 'error', body))
            if timeout:
                body = '\n'.join(txt[x] for x in range(timeout['line'], n + 1))
                for name in timeout['tests'] or [None]:
                    out.append(_go_item(pkg, name, timeout['line'], n, timeout['msg'], 'timeout', body))
            if broken or (not pending and not timeout):
                body = '\n'.join(txt[x] for x in range(seg_start, n + 1))
                if broken:     # the compile error printed under `# <pkg>`
                    msg = next((cm.group(1) for x in range(seg_start, n) for cm in [GO_COMPILE.match(txt[x])] if cm), t.strip())
                else:          # a package that failed outside any test (a panic in TestMain or init)
                    msg = next((txt[x].strip() for x in range(seg_start, n) if txt[x].startswith('panic: ')), t.strip())
                first = next((x for x in range(seg_start, n + 1) if txt[x].strip() in (f'# {pkg}', msg)), n)
                out.append(_go_item(pkg + (f' [{broken} failed]' if broken else ''), None, first, n, msg, 'error', body))
        pending, timeout, seg_start = [], None, n + 1
    for p in pending:              # output cut off before the package result
        out.append(_go_item(None, p['name'], p['line'], min(hi, p['line'] + 11), txt.get(p['line'] + 1, '').strip() or p['name'], 'error', ''))
    if timeout:
        for name in timeout['tests'] or [None]:
            out.append(_go_item(None, name, timeout['line'], hi, timeout['msg'], 'timeout', '\n'.join(txt[x] for x in range(timeout['line'], hi + 1))))
    seen, res = set(), []
    for c in sorted(out, key=lambda c: c['line']):
        if c['test_id'] not in seen:
            seen.add(c['test_id'])
            res.append(c)
    return res


# --- cargo test and cargo nextest ----------------------------------------------------------------------

LT_HEADER = re.compile(r'^---- (.+?) std(out|err) ----$')
LT_BLOCK_END = re.compile(r'^(---- .+ std(out|err) ----|failures:|test result: )')
# `thread 'name' panicked at src/x.rs:12:5:`; newer toolchains print the thread id after the name: `thread 'name' (2976) panicked at ...`
LT_THREAD = re.compile(r"^thread '([^']*)'(?: \(\d+\))? panicked at ([^\s:]+\.rs:\d+:\d+):?\s*(.*)$")


def _panic_message(body: list[str]) -> str | None:
    """`panicked at src/x.rs:12:5: <message>` from a panic (the message is on the next line since Rust 1.73)."""
    for k, t in enumerate(body):
        m = LT_THREAD.match(t.strip())
        if m:
            rest = m.group(3).strip() or next((x.strip() for x in body[k + 1:] if x.strip()), '')
            return f'panicked at {m.group(2).replace(chr(92), "/")}: {rest}'.rstrip(': ')
    return None


CARGO_MARKERS = ('test result', 'panicked', ' ... ', 'running ', 'Running', 'FAIL', 'PASS', 'TIMEOUT', 'SIGSEGV',
                 'SIGABRT', 'SIGKILL', 'SIGTERM', 'SIGILL', 'SIGBUS', 'Summary [')


def cargo_tests(lines: list[str], lo: int, hi: int) -> list[dict]:
    """Failing Rust tests in a step's output, lines lo..hi (1-based): cargo-nextest's status lines (`FAIL [ 0.006s]
    crate::bin test::path`, TIMEOUT, SIG*), else libtest's (`cargo test`, with the target from its `Running` line).
    Ids are `<target>::<test path>` as parse_results gives them; the message is the panic with its location, the test
    file the panic location (else the integration test's file), the excerpt the test's own output."""
    # a cheap look at the raw lines first: a step with no libtest or nextest output in it is not parsed (the markers
    # are generous on purpose, so cargo output is never missed)
    if not any(any(k in l for k in CARGO_MARKERS) for l in lines[lo - 1:hi]):
        return []
    txt = {n: strip(lines[n - 1]) for n in range(lo, hi + 1)}
    out = []
    if any(NT_LINE.match(t) for t in txt.values()):
        cases, _ = parse_nextest_output('\n'.join(txt[n] for n in range(lo, hi + 1)), 'ci')
        status_at = [(n, NT_LINE.match(t)) for n, t in txt.items() if NT_LINE.match(t)]
        for c in cases:
            if c['status'] not in ('failed', 'error'):
                continue
            hit = next(((n, m) for n, m in status_at if f'{nextest_target(m.group(2))}::{m.group(3)}' == c['id'] and m.group(1) != 'PASS'), None)
            if not hit:
                continue
            n, m = hit
            end = next((x - 1 for x, _ in status_at if x > n), hi)
            end = next((x - 1 for x in range(n + 1, end + 1) if re.match(r'^\s*Summary \[', txt[x])), end)
            body = [txt[x] for x in range(n + 1, end + 1)]
            ftype = 'timeout' if m.group(1) == 'TIMEOUT' else 'error' if c['status'] == 'error' else \
                'assertion' if LT_ASSERT.search('\n'.join(body)) else 'error'
            pm = LT_PANIC_AT.search('\n'.join(body))
            out.append({'test_id': c['id'], 'test_file': pm.group(1).replace('\\', '/') if pm else None, 'line': n, 'end': end,
                        'message': _panic_message(body) or txt[n].strip(), 'failure_type': ftype, 'frames': frames('\n'.join(body))})
        return out
    if not any(LT_RUNNING.match(t) or LT_DOC.match(t) for t in txt.values()):
        return []
    cases, _ = parse_libtest('\n'.join(txt[n] for n in range(lo, hi + 1)), 'ci')
    target, at = None, {}
    for n in range(lo, hi + 1):
        t = txt[n]
        m, d = LT_RUNNING.match(t), LT_DOC.match(t)
        if m or d:
            target = libtest_target(m.group(2)) if m else f'doctest:{d.group(1)}'
            continue
        h = LT_HEADER.match(t)
        if h and target is not None:
            at.setdefault((target, h.group(1)), n)
            continue
        f = re.match(r'^test (.+?) \.\.\. FAILED\s*$', t)
        if f and target is not None:
            at.setdefault((target, re.sub(r' - should panic$', '', f.group(1)), 'line'), n)
        if re.search(r"process didn't exit successfully", t) and target is not None:
            at.setdefault((target, '(crashed)'), n)
    for c in cases:
        if c['status'] not in ('failed', 'error'):
            continue
        key = (c['classname'], c['name'])
        n = at.get(key) or at.get(key + ('line',))
        if not n:
            continue
        # no `---- name stdout ----` block: with --nocapture the panic is printed while the tests run, before the
        # `test name ... FAILED` line. libtest names a test's thread after the test, so that panic is the test's.
        loose = _loose_panic(txt, lo, n, c['name']) if key not in at else None
        if loose:
            body = [txt[x] for x in range(loose[0], loose[1] + 1)]
            where = LT_PANIC_AT.search(body[0].replace('\\', '/'))
            out.append({'test_id': c['id'], 'test_file': where.group(1) if where else c.get('file'), 'line': loose[0], 'end': loose[1],
                        'message': _panic_message(body) or txt[n].strip(),
                        'failure_type': 'assertion' if LT_ASSERT.search('\n'.join(body)) else 'error', 'frames': frames('\n'.join(body))})
            continue
        end = next((x - 1 for x in range(n + 1, hi + 1) if LT_BLOCK_END.match(txt[x]) or LT_RUNNING.match(txt[x])), hi) if key in at else n
        body = [txt[x] for x in range(n + 1, end + 1)]
        out.append({'test_id': c['id'], 'test_file': c.get('file'), 'line': n, 'end': end,
                    'message': _panic_message(body) or (c.get('message_excerpt') or '').split('\n')[0] or txt[n].strip(),
                    'failure_type': c.get('failure_type') or 'error', 'frames': frames('\n'.join(body))})
    return out


def _loose_panic(txt: dict[int, str], lo: int, failed_at: int, name: str) -> tuple[int, int] | None:
    """(first, last) line of the panic a test's own thread printed above its `test <name> ... FAILED` line, within the
    same test binary's output: the nearest `thread '<name>' ... panicked at` line, up to the next blank line, test
    result line or other thread's panic (12 lines at most)."""
    for n in range(failed_at - 1, lo - 1, -1):
        t = txt[n]
        if LT_RUNNING.match(t) or LT_DOC.match(t):
            return None
        m = LT_THREAD.match(t.strip())
        if m and m.group(1) == name:
            end = n
            for x in range(n + 1, min(failed_at - 1, n + 11) + 1):
                if not txt[x].strip() or txt[x].startswith(('test ', "thread '")):
                    break
                end = x
            return n, end
    return None


# --- one defect on several jobs, and the checks a failure kept from running ---------------------------------

def _portable(text: str) -> str:
    """A message with the runner's checkout prefix and path style taken out, so the same failure reads the same on
    every operating system of a matrix."""
    t = (text or '').replace('\\', '/').replace('//?/', '')
    return re.sub(r'(?:/home/runner/work|/Users/runner/work|[A-Za-z]:/a|/__w|/home/vsts/work)/[^/\s]+/[^/\s]+/', '', t)


def defect_key(f: dict) -> tuple | None:
    """What makes two failures on different jobs the same defect: the same test (or the same failed step with the same
    kind of failure) at the same file and line, or, with no location, the same message. A failure with no log lines
    (a job that never started, a log that could not be read) is never grouped: nothing shows it is the same."""
    if f.get('log_status') not in (None, 'fetched') or f.get('failure_type') == 'job' or not (f.get('log_excerpt') or {}).get('lines'):
        return None
    where = (f['frames'][0]['file'], f['frames'][0]['line']) if f.get('frames') else _portable(f.get('message') or '')
    return (f['kind'], f['test_id'] if f['kind'] == 'test' else f.get('step'), f.get('failure_type'), where)


def failure_groups(failures: list[dict]) -> list[dict]:
    """Failures that are one defect seen on several jobs (the legs of a matrix): [{id, failures, jobs, basis}]. Sets
    `group` on each member; every member keeps its own job, log lines and signals. One draft covers a group."""
    by: dict[tuple, list[dict]] = {}
    for f in failures:
        f.pop('group', None)
        k = defect_key(f)
        if k:
            by.setdefault(k, []).append(f)
    out = []
    for k, fs in by.items():
        jobs = list(dict.fromkeys(f['job_id'] for f in fs))
        if len(jobs) < 2:
            continue
        gid = f'G{len(out) + 1:02d}'
        for f in fs:
            f['group'] = gid
        where = f'{k[3][0]}:{k[3][1]}' if isinstance(k[3], tuple) else 'the same message'
        out.append({'id': gid, 'failures': [f['id'] for f in fs], 'jobs': list(dict.fromkeys(f['job'] for f in fs)),
                    'basis': f'the same {k[2] or "step"} failure of {k[1] or "the job"} at {where} on {len(jobs)} jobs'})
    return out


NOT_A_CHECK = re.compile(r'(?i)^(post |complete job$|set up job$)|upload-artifact|actions/cache')


def checks_not_run(run: dict, attempt: int = 1) -> dict:
    """What a failure kept from running: {steps: [{job, after_step, steps}], jobs: [...]}. A job stops at its first
    failed step, so every later step the CI records as skipped (the linter, the coverage gate, the build) gave no
    result on this commit; so did every job the CI skipped. Post and closing steps, artifact uploads and caches are
    left out. The run cannot say whether a skipped step or job would have run anyway (its own `if:`), so they are
    listed as not run, never as failed."""
    att = next(a for a in run['attempts'] if a['n'] == attempt)
    steps = []
    for j in att['jobs']:
        if j['conclusion'] in ('success', 'skipped', None):
            continue
        first = next((k for k, s in enumerate(j['steps']) if s['conclusion'] == 'failure'), None)
        if first is None:
            continue
        later = [s['name'] for s in j['steps'][first + 1:] if s['conclusion'] == 'skipped' and not NOT_A_CHECK.search(s['name'] or '')]
        if later:
            steps.append({'job': j['name'], 'after_step': j['steps'][first]['name'], 'steps': later})
    failed = any(j['conclusion'] not in ('success', 'skipped', None) for j in att['jobs'])
    return {'steps': steps, 'jobs': [j['name'] for j in att['jobs'] if j['conclusion'] == 'skipped'] if failed else []}


def log_tests(lines: list[str], lo: int, hi: int) -> list[dict]:
    """Failing tests in a step's console output: a Vitest, Jest or node:test summary, go test, or cargo test/nextest."""
    return console_tests(lines, lo, hi) or go_tests(lines, lo, hi) or cargo_tests(lines, lo, hi)


# A Rust compiler, clippy or rustfmt finding a step failed on (case-sensitive: `error[E0425]: ...`, `error: <lint>`,
# not cargo's closing `error: could not compile ...`), and a coverage gate's verdict.
RUST_ANCHOR = re.compile(r"^error(\[E\d{4}\])?: (?!could not compile|test failed|aborting due to|build failed|\d+ targets? failed|"
                         r"process didn't exit|test run failed|Recipe `)|^Diff in \S+|"
                         r'Coverage failure: total of \S+ is less than fail-under|Required test coverage of \S+ not reached|'
                         r'Coverage is below the failure threshold|\bcoverage (rate )?(of )?\S*\s?.{0,40}\bis (less|lower) than\b')
# What kind of step failure it is, from the real signature its excerpt and message carry (data/signatures.json)
STEP_FAILURE_TYPE = (('REAL-COMPILE', 'compile'), ('REAL-LINT', 'lint'), ('REAL-FORMAT', 'format'), ('REAL-COVERAGE', 'coverage_gate'))
FAIL_UNDER = re.compile(r'--fail-under(-lines|-functions|-regions)?[ =]\d+|--cov-fail-under[ =]\d+|fail_under\s*=\s*\d+')


_STEP_SIGS: dict | None = None   # the signatures step_failure_type tries, compiled once


def step_failure_type(texts: list[str]) -> str:
    global _STEP_SIGS
    if _STEP_SIGS is None:
        _STEP_SIGS = {s['id']: re.compile(s['pattern'], re.I) for s in SIG['signatures']}
    sigs = _STEP_SIGS
    for sid, kind in STEP_FAILURE_TYPE:
        if sid in sigs and any(sigs[sid].search(t or '') for t in texts):
            return kind
    return 'step'


def coverage_gate_excerpt(lines: list[str], sec: dict | None) -> list[dict] | None:
    """A coverage step that failed without saying why: when its command names a threshold (`--fail-under-lines 80`)
    and its output ends with a TOTAL row, the excerpt is the command line (the threshold) and that row."""
    if not sec or not FAIL_UNDER.search(sec['title']):
        return None
    close = sec['errors'][-1] if sec['errors'] else sec['end']
    if any(RUST_ANCHOR.search(strip(lines[n - 1])) for n in range(sec['body'], close + 1)):
        return None
    total = next((n for n in range(close, sec['body'] - 1, -1) if re.match(r'^\s*TOTAL\s', strip(lines[n - 1]))), None)
    if total is None:
        return None
    return [{'n': sec['start'], 'text': redact(strip(lines[sec['start'] - 1]))}, {'n': total, 'text': redact(strip(lines[total - 1]))}]


# A lint or format finding a step failed on: `file.go:12:5: message (linter)` (golangci-lint, go vet, staticcheck,
# also a compile error), a `::error file=...` annotation, or a formatting check's verdict.
GO_ANCHOR = re.compile(r'^\s*(?:\.{0,2}/)?[\w./@-]+\.go:\d+:\d+: \S|^::error\s+file=|\b(these|following|some) files? (are|is|were) not (properly )?(formatted|gofmt)|'
                       r'\bnot (properly )?gofmt(ed|\'d)?\b|\bPlease run (gofmt|go ?fmt|goimports)\b', re.I)


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
        base = {'job': j['name'], 'job_id': j['id'], 'log_status': log_status,
                # the CI's own explanation of the job (check-run annotations): quoted, untrusted text
                **({'annotations': [redact(a['message'])[:500] for a in j['annotations']]} if j.get('annotations') else {}),
                # a never-started job's start-to-completion time is queue time, not run time
                **({'queued_for_s': j['queued_for_s']} if j.get('never_started') and j.get('queued_for_s') is not None else {})}
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
            ctests = log_tests(lines, sec['body'], sec['end']) if sec else []
            if ctests:
                for c in ctests:
                    out.append({**base, 'step': s['name'], 'kind': 'test', 'source': 'console', 'step_kind': 'test', 'test_id': c['test_id'],
                                'test_file': c['test_file'], 'failure_type': c['failure_type'], 'message': redact(c['message'])[:300], 'frames': c['frames'],
                                'section': {'title': sec['title'], 'basis': basis},
                                'log_excerpt': {'path': j.get('log'), 'lines': excerpt(lines, c['line'], c['end'], 12)}})
                continue
            gate = coverage_gate_excerpt(lines, sec) if lines else None
            ex = gate or (section_excerpt(lines, sec) if lines else [])
            msg = gate[-1]['text'] if gate else next((re.sub(r'^##\[error\]', '', l['text']) for l in ex if _is_err(l['text'])), ex[-1]['text'] if ex else '')
            if not lines:
                msg = f'no log ({log_status}' + (f': {j.get("log_error")}' if j.get('log_error') else '') + ')'
            f = {**base, 'step': s['name'], 'kind': 'step', 'source': 'log', 'step_kind': kind, 'test_id': None, 'test_file': None,
                 'failure_type': step_failure_type([msg] + [l['text'] for l in ex]) if lines else 'step', 'message': msg[:300], 'frames': frames('\n'.join(l['text'] for l in ex)),
                 'section': {'title': sec['title'] if sec else None, 'basis': basis}, 'log_excerpt': {'path': j.get('log'), 'lines': ex}}
            if is_gate(j, s['name'], sec, lines):
                f['aggregate_gate'] = True
            out.append(f)
        if not cases and not failed_steps:
            out.append({**base, 'step': None, 'kind': 'step', 'source': 'log', 'step_kind': 'other', 'test_id': None, 'test_file': None, 'failure_type': 'job',
                        'message': (f'job concluded {j["conclusion"]} and never started on a runner (no steps, no log)'
                                    + (f'; its {hms(j["queued_for_s"])} between start and completion is time waiting for a runner, not time running'
                                       if j.get('queued_for_s') is not None else '') if j.get('never_started')
                                    else f'job concluded {j["conclusion"]} with no failed step'), 'frames': [],
                        'log_excerpt': {'path': j.get('log'), 'lines': excerpt(lines, max(1, len(lines) - 7), len(lines), 8) if lines else []}})
    for i, f in enumerate(out, 1):
        f['id'] = f'X{i:02d}'
    for f in out:
        if f.pop('aggregate_gate', False):
            others = [g['id'] for g in out if g['job_id'] != f['job_id'] and not g.get('consequence_of')]
            if others:
                f['consequence_of'] = others
    failure_groups(out)     # one defect on several jobs: each member gets its `group`
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


GO_PKG = 'example.test/fares/pkg'
GO_LOGS = {
    # plain go test: a failing subtest, its parent, passing packages around it
    'go-unit': ('go test ./...', 'Unit tests', [
        f'ok  \t{GO_PKG}/labels\t0.012s', '--- FAIL: TestQuoteRounding (0.00s)', '    --- FAIL: TestQuoteRounding/half_fare (0.00s)',
        '        quote_test.go:18: got 12.12, want 12.13', 'FAIL', f'FAIL\t{GO_PKG}/quote\t0.020s', f'ok  \t{GO_PKG}/zones\t0.030s', 'FAIL',
        '##[error]Process completed with exit code 1.']),
    # a hung test: the panic names it; the first frames are the Go runtime's, the project's come later
    'go-timeout': ('go test ./...', 'Unit tests', [
        'panic: test timed out after 10m0s', '\trunning tests:', '\t\tTestFetchTimetable (9m58s)', '',
        'goroutine 7 [running]:', 'testing.(*M).startAlarm.func1()', '\t/opt/hostedtoolcache/go/1.26.0/x64/src/testing/testing.go:2802 +0x34',
        'created by time.goFunc', '\t/opt/hostedtoolcache/go/1.26.0/x64/src/time/sleep.go:215 +0x2d', '',
        'goroutine 21 [select]:', f'{GO_PKG}/timetable.(*Client).Fetch(...)', '\t/home/runner/work/fares/fares/pkg/timetable/client.go:146 +0x1a5',
        f'{GO_PKG}/timetable.TestFetchTimetable(0xc000102000)', '\t/home/runner/work/fares/fares/pkg/timetable/client_test.go:49 +0x85',
        f'FAIL\t{GO_PKG}/timetable\t600.015s', 'FAIL', '##[error]Process completed with exit code 1.']),
    # a package that does not compile
    'go-build': ('go test ./...', 'Unit tests', [
        f'# {GO_PKG}/zones', 'pkg/zones/zones.go:12:3: undefined: maxZone', f'FAIL\t{GO_PKG}/zones [build failed]', 'FAIL',
        '##[error]Process completed with exit code 1.']),
    # go test -json events
    'go-json': ('go test -json ./...', 'Unit tests', [
        '{"Action":"run","Package":"%s/quote","Test":"TestQuoteTax"}' % GO_PKG,
        '{"Action":"output","Package":"%s/quote","Test":"TestQuoteTax","Output":"    tax_test.go:31: got 0.19, want 0.2\\n"}' % GO_PKG,
        '{"Action":"output","Package":"%s/quote","Test":"TestQuoteTax","Output":"--- FAIL: TestQuoteTax (0.00s)\\n"}' % GO_PKG,
        '{"Action":"fail","Package":"%s/quote","Test":"TestQuoteTax","Elapsed":0}' % GO_PKG,
        '{"Action":"fail","Package":"%s/quote","Elapsed":0.02}' % GO_PKG,
        '##[error]Process completed with exit code 1.']),
    # the race detector fails the test it caught
    'go-race': ('go test -race ./...', 'Unit tests', [
        '==================', 'WARNING: DATA RACE', 'Write at 0x00c0000a4010 by goroutine 8:', f'  {GO_PKG}/cache.(*Store).Put()',
        '      /home/runner/work/fares/fares/pkg/cache/store.go:40 +0x44', '==================',
        '--- FAIL: TestStoreConcurrent (0.01s)', '    testing.go:1490: race detected during execution of test', 'FAIL',
        f'FAIL\t{GO_PKG}/cache\t0.050s', 'FAIL', '##[error]Process completed with exit code 1.']),
    # golangci-lint, text format and as a GitHub annotation
    'go-lint': ('golangci-lint run', 'Lint', [
        'level=info msg="[runner] linters took 2.1s"',
        'pkg/quote/quote.go:44:2: Error return value of `w.Write` is not checked (errcheck)', '\tw.Write(b)', '\t^', '1 issues:', '* errcheck: 1',
        '##[error]Process completed with exit code 1.']),
    'go-lint-annotation': ('golangci-lint run --out-format=github-actions', 'Lint', [
        '::error file=pkg/zones/zones.go,line=7,col=1::exported function `Count` should have comment (revive)',
        '##[error]Process completed with exit code 1.']),
    # a formatting check
    'go-fmt': ('make fmt-check', 'Check formatting', [
        'gofmt -l .', 'pkg/quote/quote.go', 'these files are not formatted; run gofmt -w', 'make: *** [Makefile:12: fmt-check] Error 1',
        '##[error]Process completed with exit code 2.']),
}


RS = '/home/runner/work/tides/tides'
RUST_LOGS = {
    # cargo test --no-fail-fast over a workspace: a unit test in a #[cfg(test)] module and an integration test fail
    'rust-test': ('cargo test --workspace --no-fail-fast', 'Unit tests', [
        f'   Compiling tides-core v0.1.0 ({RS}/crates/tides-core)', '    Finished `test` profile [unoptimized + debuginfo] target(s) in 4.20s',
        '     Running unittests src/lib.rs (target/debug/deps/tides_core-1a2b3c4d5e6f7a8b)', '', 'running 3 tests',
        'test tables::tests::parses_header ... ok', 'test tables::tests::rounds_half_metre ... FAILED', 'test tables::tests::sorts_by_time ... ok', '',
        'failures:', '', '---- tables::tests::rounds_half_metre stdout ----', '',
        "thread 'tables::tests::rounds_half_metre' panicked at crates/tides-core/src/tables.rs:88:9:", 'assertion `left == right` failed',
        '  left: 1.4', ' right: 1.5', 'note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace', '', '',
        'failures:', '    tables::tests::rounds_half_metre', '',
        'test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s', '',
        '     Running tests/cli.rs (target/debug/deps/cli-9f8e7d6c5b4a3921)', '', 'running 2 tests',
        'test prints_today ... ok', 'test rejects_unknown_port ... FAILED', '', 'failures:', '', '---- rejects_unknown_port stdout ----', '',
        "thread 'rejects_unknown_port' panicked at crates/tides-cli/tests/cli.rs:41:5:", 'expected exit code 2, got 0', '', '',
        'failures:', '    rejects_unknown_port', '', 'test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.20s', '',
        'error: 2 targets failed:', '    `-p tides-core --lib`', '    `-p tides-cli --test cli`', '##[error]Process completed with exit code 101.']),
    # cargo nextest: a failing test (its output under STDOUT/STDERR headers) and a test that timed out
    'rust-nextest': ('cargo nextest run --workspace --no-fail-fast', 'Unit tests', [
        '    Starting 4 tests across 2 binaries', '        PASS [   0.004s] tides-core tables::tests::parses_header',
        '        FAIL [   0.006s] tides-core tables::tests::rounds_half_metre',
        '──── STDOUT:             tides-core tables::tests::rounds_half_metre', '', 'running 1 test', 'test tables::tests::rounds_half_metre ... FAILED', '',
        'test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 3 filtered out; finished in 0.00s',
        '──── STDERR:             tides-core tables::tests::rounds_half_metre', '',
        "thread 'tables::tests::rounds_half_metre' panicked at crates/tides-core/src/tables.rs:88:9:", 'assertion `left == right` failed',
        '  left: 1.4', ' right: 1.5', '', '        PASS [   0.010s] tides-cli::cli prints_today',
        '     TIMEOUT [  60.002s] tides-cli::cli waits_for_feed', '────────────',
        '     Summary [  60.020s] 4 tests run: 2 passed, 1 failed, 1 timed out, 0 skipped',
        '        FAIL [   0.006s] tides-core tables::tests::rounds_half_metre', '     TIMEOUT [  60.002s] tides-cli::cli waits_for_feed',
        'error: test run failed', '##[error]Process completed with exit code 100.']),
    # a compile error stops cargo test before any test runs
    'rust-compile': ('cargo test --workspace', 'Unit tests', [
        f'   Compiling tides-core v0.1.0 ({RS}/crates/tides-core)', 'error[E0425]: cannot find value `max_height` in this scope',
        '  --> crates/tides-core/src/tables.rs:52:17', '   |', '52 |     let cap = max_height * 2.0;', '   |               ^^^^^^^^^^ not found in this scope',
        '', 'For more information about this error, try `rustc --explain E0425`.',
        'error: could not compile `tides-core` (lib test) due to 1 previous error', '##[error]Process completed with exit code 101.']),
    # clippy with -D warnings
    'rust-clippy': ('cargo clippy --workspace --all-targets -- -D warnings', 'Clippy', [
        f'    Checking tides-core v0.1.0 ({RS}/crates/tides-core)', 'error: this `if` has identical blocks',
        '  --> crates/tides-core/src/tables.rs:61:21', '   |', '61 |     let step = if rising { 1 } else { 1 };', '   |                          ^^^^^', '   |',
        '   = help: for further information visit https://rust-lang.github.io/rust-clippy/master/index.html#if_same_then_else',
        '   = note: `-D clippy::if-same-then-else` implied by `-D warnings`',
        '   = help: to override `-D warnings` add `#[allow(clippy::if_same_then_else)]`', '',
        'error: could not compile `tides-core` (lib) due to 1 previous error', '##[error]Process completed with exit code 101.']),
    # cargo fmt --check
    'rust-fmt': ('cargo fmt --all -- --check', 'Format', [
        f'Diff in {RS}/crates/tides-core/src/tables.rs:12:', ' use std::fmt;', '-use crate::units::{Metres,Feet};', '+use crate::units::{Feet, Metres};', ' ',
        '##[error]Process completed with exit code 1.']),
    # a coverage gate: every test passed, the total is under the threshold the command names
    'rust-coverage': ('cargo llvm-cov --workspace --fail-under-lines 80', 'Coverage gate', [
        '     Running unittests src/lib.rs (target/llvm-cov-target/debug/deps/tides_core-1a2b3c4d5e6f7a8b)', '', 'running 3 tests',
        'test tables::tests::parses_header ... ok', 'test tables::tests::rounds_half_metre ... ok', 'test tables::tests::sorts_by_time ... ok', '',
        'test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s', '',
        'Filename                      Regions    Missed Regions     Cover   Functions  Missed Functions  Executed       Lines      Missed Lines     Cover',
        'crates/tides-core/src/tables.rs     120                40    66.67%          14                 4    71.43%         210                63    70.00%',
        'TOTAL                               120                40    66.67%          14                 4    71.43%         210                63    70.00%',
        '##[error]Process completed with exit code 1.']),
    # the same gate where the tool says why (cargo tarpaulin)
    'rust-tarpaulin': ('cargo tarpaulin --workspace --fail-under 80', 'Coverage gate', [
        '|| Tested/Total Lines:', '|| crates/tides-core/src/tables.rs: 147/210', '|| ', '70.00% coverage, 147/210 lines covered',
        'Coverage is below the failure threshold 70.00% < 80.00%', 'Error: "Coverage is below the failure threshold 70.00% < 80.00%"',
        '##[error]Process completed with exit code 1.']),
}


WIN = '\\\\?\\D:\\a\\tides\\tides'     # rustfmt on a Windows runner prints the extended-length form of the checkout path
MAC = '/Users/runner/work/tides/tides'
_FMT = [' use std::fmt;', '-use crate::units::{Metres,Feet};', '+use crate::units::{Feet, Metres};', ' ', '##[error]Process completed with exit code 1.']
RUST_MATRIX_LOGS = {
    # one formatting defect on three legs of an OS matrix: the same file and line, printed three ways
    'checks (ubuntu-latest)': ('cargo fmt --all -- --check', 'Format', [f'Diff in {RS}/crates/tides-core/src/tables.rs:12:'] + _FMT),
    'checks (windows-latest)': ('cargo fmt --all -- --check', 'Format', [f'Diff in {WIN}\\crates\\tides-core\\src\\tables.rs:12:'] + _FMT),
    'checks (macos-latest)': ('cargo fmt --all -- --check', 'Format', [f'Diff in {MAC}/crates/tides-core/src/tables.rs:12:'] + _FMT),
    # rustc on Windows: a relative path with backslashes
    'build (windows-latest)': ('cargo test --workspace', 'Unit tests', [
        'error[E0425]: cannot find value `max_height` in this scope', '  --> crates\\tides-core\\src\\tables.rs:52:17', '   |',
        '52 |     let cap = max_height * 2.0;', 'error: could not compile `tides-core` (lib test) due to 1 previous error',
        '##[error]Process completed with exit code 101.']),
    # current Rust prints the thread id after the thread's name
    'thread-id': ('cargo test --workspace --no-fail-fast', 'Unit tests', [
        '     Running unittests src/lib.rs (target/debug/deps/tides_core-1a2b3c4d5e6f7a8b)', '', 'running 2 tests',
        'test tables::tests::parses_header ... ok', 'test tables::tests::rounds_half_metre ... FAILED', '', 'failures:', '',
        '---- tables::tests::rounds_half_metre stdout ----', '',
        "thread 'tables::tests::rounds_half_metre' (2976) panicked at crates/tides-core/src/tables.rs:88:9:", 'assertion `left == right` failed',
        '  left: 1.4', ' right: 1.5', 'note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace', '', '',
        'failures:', '    tables::tests::rounds_half_metre', '',
        'test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s', '',
        'error: test failed, to rerun pass `-p tides-core --lib`', '##[error]Process completed with exit code 101.']),
    # --nocapture: the panic is printed while the tests run, long before `test ... FAILED`, and there is no stdout block
    'nocapture': ('cargo test -p tides-cli --test probe -- --ignored --nocapture', 'Probe tests', [
        '     Running tests/probe.rs (target/debug/deps/probe-0a1b2c3d4e5f6a7b)', '', 'running 2 tests', 'feed: connecting to the sample feed',
        "thread 'waits_for_feed' (4102) panicked at crates/tides-cli/tests/probe.rs:33:13:", 'feed probe gave no reading in 5s',
        'note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace'] + [f'feed: tick {i}' for i in range(1, 31)] + [
        'test prints_today ... ok', 'test waits_for_feed ... FAILED', '', 'failures:', '', 'failures:', '    waits_for_feed', '',
        'test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 5.02s', '',
        'error: test failed, to rerun pass `-p tides-cli --test probe`', '##[error]Process completed with exit code 101.']),
}


def rust_matrix_self_test(d: Path, check) -> None:
    """Windows paths, thread ids, --nocapture panics, one defect on several matrix legs, and the checks a failed step skipped."""
    rjobs = []
    for i, (name, (script, step, body)) in enumerate(RUST_MATRIX_LOGS.items(), 80):
        (d / 'logs' / f'{i}.txt').write_text(_gh_log([(script, [script], body)]), encoding='utf-8')
        later = [{'number': 3, 'name': 'cargo clippy --workspace -- -D warnings', 'conclusion': 'skipped'},
                 {'number': 4, 'name': 'Coverage gate', 'conclusion': 'skipped'}, {'number': 5, 'name': 'Post Run actions/cache@v4', 'conclusion': 'skipped'},
                 {'number': 6, 'name': 'Complete job', 'conclusion': 'success'}] if name.startswith('checks') else []
        rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched',
                      'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': step, 'conclusion': 'failure'}] + later})
    rjobs.append({'id': 99, 'name': 'release build', 'conclusion': 'skipped', 'junit': [], 'log': None, 'steps': []})
    run = {'run': {'id': 4}, 'attempts': [{'n': 1, 'jobs': rjobs}]}
    fs = extract(d, run, 1)
    grouped_by_extract = [f.get('group') for f in fs]
    by: dict[str, list] = {}
    for f in fs:
        by.setdefault(f['job'], []).append(f)
    want = [{'file': 'crates/tides-core/src/tables.rs', 'line': 12}]
    for leg in ('ubuntu', 'windows', 'macos'):
        fx = by[f'checks ({leg}-latest)'][0]
        check(fx['frames'] == want and fx['failure_type'] == 'format', f'rustfmt on {leg}: the file of the diff, checkout prefix dropped: {fx["frames"]}')
    check(by['build (windows-latest)'][0]['frames'][:1] == [{'file': 'crates/tides-core/src/tables.rs', 'line': 52}],
          f'rustc on Windows: {by["build (windows-latest)"][0]["frames"]}')
    t = by['thread-id'][0]
    check(t['message'] == 'panicked at crates/tides-core/src/tables.rs:88:9: assertion `left == right` failed' and t['failure_type'] == 'assertion',
          f'a panic line with a thread id: the message is the panic and its cause, got {t["message"]!r} ({t["failure_type"]})')
    n = by['nocapture']
    check(len(n) == 1 and n[0]['test_id'] == 'probe::waits_for_feed' and n[0]['test_file'] == 'crates/tides-cli/tests/probe.rs'
          and n[0]['frames'][:1] == [{'file': 'crates/tides-cli/tests/probe.rs', 'line': 33}]
          and n[0]['message'] == 'panicked at crates/tides-cli/tests/probe.rs:33:13: feed probe gave no reading in 5s'
          and n[0]['log_excerpt']['lines'][0]['text'].startswith("thread 'waits_for_feed'"),
          f'--nocapture: the panic printed while the tests ran is tied to its test by the thread name: '
          f'{[(f["test_id"], f["test_file"], f["frames"], f["message"], f["log_excerpt"]["lines"][:1]) for f in n]}')
    # one defect on three legs is one group; the legs keep their own evidence
    gs = failure_groups(fs)
    legs = [by[f'checks ({leg}-latest)'][0] for leg in ('ubuntu', 'windows', 'macos')]
    check(grouped_by_extract == [f.get('group') for f in fs] and any(grouped_by_extract), f'extract() itself sets the groups: {grouped_by_extract}')
    check(len(gs) == 1 and gs[0]['failures'] == [f['id'] for f in legs] and len({f.get('group') for f in legs}) == 1 and legs[0].get('group') == gs[0]['id']
          and all('group' not in f for f in fs if f not in legs) and len({f['log_excerpt']['path'] for f in legs}) == 3,
          f'one formatting defect on three matrix legs should be one failure group: {gs}, {[(f["id"], f.get("group")) for f in fs]}')
    nr = checks_not_run(run, 1)
    check([(x['job'], x['after_step'], x['steps']) for x in nr['steps']][:1] == [('checks (ubuntu-latest)', 'Format', ['cargo clippy --workspace -- -D warnings', 'Coverage gate'])]
          and len(nr['steps']) == 3 and nr['jobs'] == ['release build'],
          f'the checks a failed step skipped (not post or closing steps) and the skipped jobs should be listed: {nr}')


def rust_self_test(d: Path, check) -> None:
    rjobs = []
    for i, (name, (script, step, body)) in enumerate(RUST_LOGS.items(), 60):
        (d / 'logs' / f'{i}.txt').write_text(_gh_log([(script, [script], body)]), encoding='utf-8')
        rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched',
                      'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': step, 'conclusion': 'failure'}]})
    fs = extract(d, {'run': {'id': 3}, 'attempts': [{'n': 1, 'jobs': rjobs}]}, 1)
    by: dict[str, list] = {}
    for f in fs:
        by.setdefault(f['job'], []).append(f)
    view = lambda fs: [(f['kind'], f['test_id'], f['failure_type'], f['message'], f['frames'][:1], f.get('test_file')) for f in fs]  # noqa: E731
    t = by['rust-test']
    check(view(t) == [('test', 'tides_core::tables::tests::rounds_half_metre', 'assertion',
                       'panicked at crates/tides-core/src/tables.rs:88:9: assertion `left == right` failed',
                       [{'file': 'crates/tides-core/src/tables.rs', 'line': 88}], 'crates/tides-core/src/tables.rs'),
                      ('test', 'cli::rejects_unknown_port', 'error', 'panicked at crates/tides-cli/tests/cli.rs:41:5: expected exit code 2, got 0',
                       [{'file': 'crates/tides-cli/tests/cli.rs', 'line': 41}], 'crates/tides-cli/tests/cli.rs')],
          f'cargo test: one failure per failing test, with its panic: {view(t)}')
    check(all(f['log_excerpt']['lines'] and f['log_excerpt']['lines'][0]['text'].startswith('---- ') for f in t),
          f'cargo test: the excerpt starts at the test\'s own output: {[f["log_excerpt"]["lines"][:1] for f in t]}')
    n = by['rust-nextest']
    check([(f['test_id'], f['failure_type']) for f in n] == [('tides_core::tables::tests::rounds_half_metre', 'assertion'), ('cli::waits_for_feed', 'timeout')]
          and n[0]['frames'][:1] == [{'file': 'crates/tides-core/src/tables.rs', 'line': 88}] and 'assertion `left == right` failed' in n[0]['message'],
          f'cargo nextest: {view(n)}')
    for job, ftype, start, frame in (('rust-compile', 'compile', 'error[E0425]: cannot find value `max_height`', ('crates/tides-core/src/tables.rs', 52)),
                                     ('rust-clippy', 'lint', 'error: this `if` has identical blocks', ('crates/tides-core/src/tables.rs', 61)),
                                     ('rust-fmt', 'format', f'Diff in {RS}/crates/tides-core/src/tables.rs:12:', ('crates/tides-core/src/tables.rs', 12)),
                                     ('rust-coverage', 'coverage_gate', 'TOTAL ', None),
                                     ('rust-tarpaulin', 'coverage_gate', 'Coverage is below the failure threshold 70.00% < 80.00%', None)):
        fx = by[job]
        ok = len(fx) == 1 and fx[0]['kind'] == 'step' and fx[0]['failure_type'] == ftype and fx[0]['message'].startswith(start) \
            and (frame is None or fx[0]['frames'][:1] == [{'file': frame[0], 'line': frame[1]}])
        check(ok, f'{job}: a {ftype} failure of its own, message from the first finding: {view(fx)}')
    cov = by['rust-coverage'][0]
    check(any('--fail-under-lines 80' in l['text'] for l in cov['log_excerpt']['lines']),
          f'coverage gate: the excerpt quotes the threshold the step ran with: {cov["log_excerpt"]["lines"]}')
    # cost on logs with no Rust in them: the signatures are compiled once, and a step's lines are not stripped for the
    # cargo parser when nothing in them is cargo output
    step_failure_type(['warm up'])
    real_compile, compiled = re.compile, []
    re.compile = lambda *a, **k: compiled.append(a[0]) or real_compile(*a, **k)
    try:
        step_failure_type(['npm ERR! code ELIFECYCLE'])
    finally:
        re.compile = real_compile
    check(not compiled, f'step_failure_type compiles its signatures on every call ({len(compiled)} compiled)')
    plain = ['npm ERR! code ELIFECYCLE', 'Error: expected 200 to be 400', '    at tests/unit/parcels.test.ts:21:17']
    real_strip, stripped = globals()['strip'], []
    globals()['strip'] = lambda s: stripped.append(s) or real_strip(s)
    try:
        none = cargo_tests(plain, 1, len(plain))
    finally:
        globals()['strip'] = real_strip
    check(none == [] and not stripped, f'cargo_tests strips {len(stripped)} lines of a log with no cargo output')


def go_self_test(d: Path, check) -> None:
    rjobs = []
    for i, (name, (script, step, body)) in enumerate(GO_LOGS.items(), 30):
        (d / 'logs' / f'{i}.txt').write_text(_gh_log([(script, [script], body)]), encoding='utf-8')
        rjobs.append({'id': i, 'name': name, 'conclusion': 'failure', 'junit': [], 'log': f'logs/{i}.txt', 'log_status': 'fetched',
                      'steps': [{'number': 1, 'name': 'Set up job', 'conclusion': 'success'}, {'number': 2, 'name': step, 'conclusion': 'failure'}]})
    fs = extract(d, {'run': {'id': 2}, 'attempts': [{'n': 1, 'jobs': rjobs}]}, 1)
    by: dict[str, list] = {}
    for f in fs:
        by.setdefault(f['job'], []).append(f)
    u = by['go-unit']
    check([f['test_id'] for f in u] == [f'{GO_PKG}/quote > TestQuoteRounding/half_fare'] and u[0]['kind'] == 'test' and u[0]['step_kind'] == 'test',
          f'go test: {[(f["kind"], f["test_id"]) for f in u]}')
    check(u[0]['message'] == 'quote_test.go:18: got 12.12, want 12.13' and u[0]['failure_type'] == 'assertion'
          and u[0]['test_file'] == f'{GO_PKG}/quote/quote_test.go', f'go test message {u[0]["message"]!r} {u[0]["failure_type"]} {u[0]["test_file"]}')
    t = by['go-timeout']
    check([f['test_id'] for f in t] == [f'{GO_PKG}/timetable > TestFetchTimetable'] and t[0]['failure_type'] == 'timeout'
          and t[0]['message'] == 'panic: test timed out after 10m0s', f'go timeout: {[(f["test_id"], f["failure_type"], f["message"]) for f in t]}')
    check(t[0]['frames'][:2] == [{'file': 'pkg/timetable/client.go', 'line': 146}, {'file': 'pkg/timetable/client_test.go', 'line': 49}],
          f'go timeout frames should be the project\'s, not the Go runtime\'s: {t[0]["frames"]}')
    b = by['go-build']
    check([f['test_id'] for f in b] == [f'{GO_PKG}/zones [build failed]'] and b[0]['message'] == 'pkg/zones/zones.go:12:3: undefined: maxZone',
          f'go build failure: {[(f["test_id"], f["message"]) for f in b]}')
    j = by['go-json']
    check([f['test_id'] for f in j] == [f'{GO_PKG}/quote > TestQuoteTax'] and j[0]['message'] == 'tax_test.go:31: got 0.19, want 0.2',
          f'go test -json: {[(f["test_id"], f["message"]) for f in j]}')
    r = by['go-race']
    check([f['test_id'] for f in r] == [f'{GO_PKG}/cache > TestStoreConcurrent'] and 'race detected' in r[0]['message'],
          f'go race: {[(f["test_id"], f["message"]) for f in r]}')
    lt = by['go-lint'][0]
    check(lt['message'].startswith('pkg/quote/quote.go:44:2: Error return value') and lt['frames'][:1] == [{'file': 'pkg/quote/quote.go', 'line': 44}],
          f'golangci-lint: {lt["message"]!r} {lt["frames"]}')
    la = by['go-lint-annotation'][0]
    check('(revive)' in la['message'] and la['frames'][:1] == [{'file': 'pkg/zones/zones.go', 'line': 7}], f'lint annotation: {la["message"]!r} {la["frames"]}')
    fm = by['go-fmt'][0]
    check(fm['message'].startswith('these files are not formatted'), f'gofmt: {fm["message"]!r}')


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
        go_self_test(d, check)
        rust_self_test(d, check)
        rust_matrix_self_test(d, check)
    if fails:
        print('extract_failures self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('extract_failures self-test: ok (step groups by title or error order, echoed script skipped, ANSI stripped, '
          'Vitest/Jest/node:test console failures, go test plain and -json with timeouts, build failures and races, '
          'golangci-lint and gofmt steps, cargo test and nextest per test, Rust compile, clippy, rustfmt and coverage-gate steps, Windows extended paths, panic thread ids, '
          '--nocapture panics tied to their test, one defect on several matrix legs grouped, checks a failed step skipped, '
          'project frames over Go toolchain frames, aggregate gate, failed log fetch)')
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
    groups, not_run = failure_groups(fs), checks_not_run(run, a.attempt)
    doc = {'run_id': run['run']['id'], 'attempt': a.attempt, 'failures': fs}
    if groups:
        doc['groups'] = groups
    if not_run['steps'] or not_run['jobs']:
        doc['checks_not_run'] = not_run
    a.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(fs)} failure(s) in attempt {a.attempt} -> {a.out}')
    for f in fs:
        print(f'  {f["id"]} {f["job"]:14} {f["kind"]:5} {f["test_id"] or f["step"]}: {f["message"][:90]}' + (f' [{f["group"]}]' if f.get('group') else ''))
        if f.get('log_status') not in ('fetched', None):
            print(f'      WARNING: no log for this job ({f["log_status"]}); its failure cannot be read from the log')
    for g in groups:
        print(f'  {g["id"]}: {", ".join(g["failures"])} are one defect ({g["basis"]}): one draft covers the group')
    for x in not_run['steps']:
        print(f'WARNING: in job {x["job"]}, {len(x["steps"])} step(s) after the failed step "{x["after_step"]}" never ran and gave no result '
              f'on this commit: {"; ".join(x["steps"])}. The triage must say so', file=sys.stderr)
    if not_run['jobs']:
        print(f'WARNING: {len(not_run["jobs"])} job(s) were skipped and gave no result: {"; ".join(not_run["jobs"])}. The triage must say so', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
