#!/usr/bin/env python3
"""Normalise JUnit XML test results into the shared run format (stdlib only).

Reads the JUnit XML that almost every runner can emit (pytest --junitxml, jest-junit, vitest's
junit reporter, Maven Surefire, go-junit-report, Playwright's junit reporter, Gradle, dotnet
trx-to-junit, XCTest via xcpretty/xcresult converters) and appends one run to a results file:

    python parse_results.py results.json --junit report.xml --run-id R1 \
        --command "npx playwright test e2e/checkout" --exit-code 1 --hash e2e/checkout/guest.spec.ts

Output:
    runs[{id, command, exit_code, junit_paths, started_at, totals, file_hashes, notes}]
    cases[{run_id, id, name, classname, file, status, failure_type, signals, message_excerpt, time}]

status:       passed | failed | error | skipped
failure_type: assertion | error | null. Assertion failures mean the test ran and an expectation
              did not hold; errors are import, collection, compile, fixture, setup or runtime
              failures before or outside an expectation.
signals:      extra hints such as timeout, selector_not_found, network, crash.

`--hash` records the SHA-256 of files (usually the test files) as they were when the run
happened, so later checks can prove whether a test changed between runs.

ANSI colour and cursor codes are stripped from names, messages and excerpts before anything is
classified. Node's built-in runner (`node --test --test-reporter=junit`) is recognised by its shape
(a classname that is the dotted describe path, or `test`, and a `file` attribute on every case):
its ids become `file::Describe > Inner > test`, like Vitest's, so two files with the same describe
names never pool. `--root` (or parse_junit's `root`) makes absolute `file` attributes relative to
the directory the tests ran in.

    python parse_results.py --self-test
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

EXCERPT = 600
ASSERTION_RX = re.compile(r'AssertionError|AssertError|assert\s|expect\(|Expected|expected .* (to|but)|toBe|toEqual|toHave|'
                          r'toContain|ComparisonFailure|AssertionFailedError|XCTAssert|Assert\.\w+ failed|'
                          r'assertion failed|Error: Timed out \d+ms waiting for expect|should\b|DID NOT (RAISE|WARN)', re.I)
ERROR_RX = re.compile(r'ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError|AttributeError|'
                      r'has no attribute|NotImplementedError|NotImplementedException|not (yet )?implemented|collection|'
                      r'fixture .* not found|Cannot find module|Cannot use import|ReferenceError|TypeError|'
                      r'is not a function|undefined is not|NoClassDefFoundError|ClassNotFoundException|'
                      r'compilation (failed|error)|cannot find symbol|unresolved reference|NoSuchMethodError|'
                      r'NullPointerException|panic:|build failed|setup failed|beforeAll|beforeEach|'
                      r'ECONNREFUSED|net::ERR_|browserType\.launch|Executable doesn\'t exist|'
                      r'(locator|page|frame|elementHandle)\.\w+: Timeout|NoSuchElementException|'
                      r'NoMatchingViewException|Unable to find (an )?element', re.I)
SIGNALS = [
    ('timeout', re.compile(r'timeout|timed out|exceeded', re.I)),
    ('selector_not_found', re.compile(r'waiting for (locator|selector|getBy)|NoSuchElement|element\(s\) not found|'
                                      r'Unable to find (an )?element|expected to find element|No element found|'
                                      r'NoMatchingViewException|Failed to get matching snapshot|resolved to 0 elements', re.I)),
    ('network', re.compile(r'ECONNREFUSED|ENOTFOUND|net::ERR_|Connection refused|getaddrinfo|503 Service', re.I)),
    ('crash', re.compile(r'Segmentation fault|SIGSEGV|SIGABRT|Target crashed|Page crashed|app (has )?crashed|'
                         r'Application .* is not running', re.I)),
    ('environment', re.compile(r'Executable doesn\'t exist|browserType\.launch|No devices? (found|available)|'
                               r'emulator|simulator .* not|command not found|ENOENT', re.I)),
]

TITLE_HEAD_RX = re.compile(r'\.\w+:\d+:\d+\s')
ANSI_RX = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')


def strip_ansi(text: str | None) -> str:
    """Text without ANSI colour, cursor and hyperlink escape sequences (Vitest, pnpm and CI logs colour output)."""
    return ANSI_RX.sub('', text or '')


def relative_file(f: str | None, root) -> str | None:
    """f relative to root when it is an absolute path (or ~/...) under root; otherwise f, '/'-separated."""
    if not f:
        return f
    f = f.replace('\\', '/')
    if root is None:
        return f
    cand = Path.home() / f[2:] if f.startswith('~/') else Path(f)
    if not cand.is_absolute():
        return f
    try:
        return Path(os.path.relpath(cand.resolve(), Path(root).resolve())).as_posix() if \
            Path(root).resolve() in cand.resolve().parents else f
    except (OSError, ValueError):
        return f

def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    except OSError:
        return None

def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]

def classify(kind: str, message: str, head: str = '') -> str:
    """failure_type for a <failure> or <error> element.

    `head` is the element's type and message attributes. When it is conclusive it decides, because
    the body often quotes the test's source (`assert ...`) whatever the real failure was.
    """
    if head and bool(ASSERTION_RX.search(head)) != bool(ERROR_RX.search(head)):
        return 'assertion' if ASSERTION_RX.search(head) else 'error'
    if kind == 'failure':
        return 'error' if ERROR_RX.search(message) and not ASSERTION_RX.search(message) else 'assertion'
    return 'assertion' if ASSERTION_RX.search(message) and not ERROR_RX.search(message) else 'error'

def excerpt(text: str) -> str:
    text = strip_ansi(text).strip()
    return text if len(text) <= EXCERPT else text[:EXCERPT] + ' …'

FILE_RX = re.compile(r'\.(spec|test|cy|e2e)\.[cm]?[jt]sx?$|\.py$|_test\.go$|\.(java|kt|swift|dart|rb|cs|php)$')

def guess_file(case: ET.Element | None, suite_file: str | None, suite_name: str | None = None) -> str | None:
    """The test's source file: from attributes, else a file-like classname or suite name, else a pytest module path."""
    f = (case.get('file') if case is not None else None) or suite_file
    if f:
        return f.replace('\\', '/')
    cls = (case.get('classname') if case is not None else None) or ''
    for cand in (cls, suite_name or ''):
        if FILE_RX.search(cand):
            return cand.replace('\\', '/')
    if not cls and case is not None:  # pytest collection errors carry the module in the name
        cls = case.get('name') or ''
    if re.fullmatch(r'[\w.]+', cls):  # pytest: tests.unit.test_orders[.TestClass]
        parts = cls.split('.')
        for i in range(len(parts), 0, -1):
            if re.match(r'test_|.+_test$', parts[i - 1]):
                return '/'.join(parts[:i]) + '.py'
    return None

def parse_junit(path: Path, run_id: str, root=None) -> tuple[list[dict], list[str]]:
    """(cases, notes) from one JUnit file. root: the directory the tests ran in, so absolute file attributes are
    recorded relative to it."""
    notes = []
    try:
        xml_root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        # ANSI escapes (raw ESC or &#27;) and other control characters are not allowed in XML 1.0; some reporters
        # write them anyway. Strip them and try once more, and say so.
        try:
            raw = Path(path).read_bytes().decode('utf-8', 'replace')
        except OSError:
            return [], [f'{path}: not valid XML ({exc})']
        raw = re.sub(r'&#(?:27|x1[bB]);(\[[0-9;?]*[ -/]*[@-~])?', '', raw)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', strip_ansi(raw))
        cleaned = re.sub(r'&#(?:x0*[0-8bcef]|x0*1[0-9a-fA-F]|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));', '', cleaned)
        try:
            xml_root = ET.fromstring(cleaned.encode('utf-8'))
        except ET.ParseError:
            return [], [f'{path}: not valid XML ({exc})']
        notes.append(f'{path}: held escape or control characters XML does not allow; they were stripped before parsing')
    cases, seen = [], {}

    def walk(el: ET.Element, suite_file: str | None, suite_name: str | None, path_names: tuple = ()):
        tag = local(el.tag)
        if tag == 'testsuite':
            suite_file = el.get('file') or el.get('filepath') or suite_file
            suite_name = el.get('name') or suite_name
            inner = path_names + ((el.get('name') or ''),)
            for child in el:
                walk(child, suite_file, suite_name, inner)
            # a suite-level error with no cases (e.g. a file that failed to import)
            if not any(local(c.tag) == 'testcase' for c in el.iter()) and (int(el.get('errors', 0) or 0) or int(el.get('failures', 0) or 0)):
                msg = ' '.join((c.get('message') or '') + ' ' + (c.text or '') for c in el if local(c.tag) in ('error', 'failure', 'system-err'))
                add({'name': f'(suite) {suite_name or path.name}', 'classname': suite_name or '',
                     'file': relative_file(guess_file(None, suite_file, suite_name), root),
                     'status': 'error', 'failure_type': 'error',
                     'message': strip_ansi(msg) or 'suite reported errors with no test cases', 'time': el.get('time')})
            return
        if tag == 'testcase':
            status, ftype, message = 'passed', None, ''
            for child in el:
                ctag = local(child.tag)
                if ctag in ('failure', 'error'):
                    head = strip_ansi((child.get('type') or '') + ': ' + (child.get('message') or '')).strip(': ')
                    if TITLE_HEAD_RX.search(head):  # Playwright puts "file:line:col <test title>" here
                        head = ''
                    message = strip_ansi(head + '\n' + (child.text or '')).strip(': \n')
                    ftype = classify(ctag, message, head)
                    status = 'failed' if ctag == 'failure' else 'error'
                    break
                if ctag == 'skipped':
                    status, message = 'skipped', strip_ansi(child.get('message') or '')
            name = strip_ansi(el.get('name')) or '(unnamed)'
            classname = strip_ansi(el.get('classname')) or suite_name or ''
            file = relative_file(guess_file(el, suite_file, suite_name), root)
            node_path = None
            # node:test's JUnit: every case has a file attribute and a classname that is its dotted describe path
            # (or "test" at the top level); make the id file::Describe > Inner > name, as for Vitest
            if el.get('file') and (classname == '.'.join(path_names) or (classname == 'test' and not path_names)):
                node_path = ' > '.join([*path_names, name])
            add({'name': name, 'classname': classname,
                 'file': file, 'status': status, 'failure_type': ftype, 'message': message,
                 'time': el.get('time')}, f'{file}::{node_path}' if node_path else None)
            return
        for child in el:
            walk(child, suite_file, suite_name, path_names)

    def add(c: dict, base: str | None = None):
        base = base or (f"{c['classname']}::{c['name']}" if c['classname'] else c['name'])
        seen[base] = seen.get(base, 0) + 1
        cid = base if seen[base] == 1 else f'{base}#{seen[base]}'  # repeats (e.g. --repeat-each) stay distinct
        msg = c.pop('message')
        raw_time = c.pop('time')
        try:
            t = float(raw_time) if raw_time not in (None, '') else None
        except ValueError:
            t = None
        cases.append({'run_id': run_id, 'id': cid, **c, 'signals': [n for n, rx in SIGNALS if msg and rx.search(msg)],
                      'message_excerpt': excerpt(msg), 'time': t})

    walk(xml_root, None, None)
    return cases, notes

def totals(cases: list[dict]) -> dict:
    t = {'tests': len(cases), 'passed': 0, 'failed': 0, 'error': 0, 'skipped': 0}
    for c in cases:
        t[c['status']] += 1
    return t

def self_test() -> int:
    """Check node:test ids, ANSI stripping and duplicate names on small synthetic JUnit files."""
    import tempfile
    failed = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        a, b = (root / 'pkg-a' / 'sum.test.mjs').as_posix(), (root / 'pkg-b' / 'sum.test.mjs').as_posix()
        node = f"""<?xml version="1.0"?><testsuites>
  <testcase name="top" classname="test" file="{a}"/>
  <testsuite name="sum"><testcase name="adds" classname="sum" file="{a}"/>
    <testsuite name="inner"><testcase name="deep # 1" classname="sum.inner" file="{a}">
      <failure type="testCodeFailure" message="1 == 2">AssertionError [ERR_ASSERTION]: 1 == 2</failure></testcase></testsuite>
  </testsuite>
  <testsuite name="sum"><testcase name="adds" classname="sum" file="{b}"/></testsuite>
  <testsuite name="outer" failures="1"><testsuite name="in" failures="1">
    <testcase name="t" classname="outer.in" file="{b}"><failure message="x">AssertionError: x</failure></testcase>
  </testsuite></testsuite>
</testsuites>"""
        # colour codes as a runner may write them: a character reference (&#27;) and a raw ESC, both invalid in XML 1.0
        vitest = ('<?xml version="1.0"?><testsuites><testsuite name="tests/cart.test.ts">'
                  '<testcase classname="tests/cart.test.ts" name="cart &gt; totals">'
                  '<failure message="&#27;[31mexpected 2 to be 3&#27;[39m">\x1b[2mAssertionError: expected 2 to be 3\x1b[22m'
                  '</failure></testcase><testcase classname="tests/cart.test.ts" name="cart &gt; totals"/>'
                  '</testsuite></testsuites>')
        (root / 'node.xml').write_text(node, encoding='utf-8')
        (root / 'vitest.xml').write_text(vitest, encoding='utf-8')
        ncases, _ = parse_junit(root / 'node.xml', 'N', root)
        vcases, vnotes = parse_junit(root / 'vitest.xml', 'V')
    ids = [c['id'] for c in ncases]
    checks = [
        ('node:test ids carry the file and the describe path', ids == ['pkg-a/sum.test.mjs::top', 'pkg-a/sum.test.mjs::sum > adds',
                                                                      'pkg-a/sum.test.mjs::sum > inner > deep # 1',
                                                                      'pkg-b/sum.test.mjs::sum > adds',
                                                                      'pkg-b/sum.test.mjs::outer > in > t'], ids),
        ('node:test files are relative to the root', {c['file'] for c in ncases} == {'pkg-a/sum.test.mjs', 'pkg-b/sum.test.mjs'},
         {c['file'] for c in ncases}),
        ('a node assertion is an assertion', ncases[2]['failure_type'] == 'assertion' if len(ncases) > 2 else False, ncases[2:3]),
        ('duplicate names get #2', [c['id'] for c in vcases] == ['tests/cart.test.ts::cart > totals', 'tests/cart.test.ts::cart > totals#2'],
         [c['id'] for c in vcases]),
        ('a JUnit file with escape codes is read, without them', bool(vcases) and '\x1b' not in vcases[0]['message_excerpt']
         and '[31m' not in vcases[0]['message_excerpt'] and vcases[0]['failure_type'] == 'assertion', (vcases[:1], vnotes)),
        ('ANSI codes are stripped', strip_ansi('\x1b[31mred\x1b[39m \x1b]8;;http://x\x07link\x1b]8;;\x07') == 'red link',
         strip_ansi('\x1b[31mred\x1b[39m')),
    ]
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Append a JUnit XML run to a normalised results file.')
    ap.add_argument('results', help='results JSON to create or append to')
    ap.add_argument('--junit', nargs='+', required=True, help='JUnit XML file(s) produced by this run')
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--command', required=True, help='the exact command that was run')
    ap.add_argument('--exit-code', type=int, required=True)
    ap.add_argument('--hash', nargs='*', default=[], help='files to fingerprint (SHA-256) as of this run')
    ap.add_argument('--root', default='.', help='paths in --hash are recorded relative to this directory')
    ap.add_argument('--phase', help='optional label stored on the run, e.g. red, green, regression, re_red')
    ap.add_argument('--files-root', help='record absolute test file paths relative to this directory (where the tests ran)')
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    args = ap.parse_args(argv)

    out = Path(args.results)
    data = {'runs': [], 'cases': []}
    if out.exists():
        data = json.loads(out.read_text(encoding='utf-8'))
        data.setdefault('runs', []); data.setdefault('cases', [])
    if any(r['id'] == args.run_id for r in data['runs']):
        print(f'Run id {args.run_id} already exists in {out}', file=sys.stderr); return 2

    cases, notes = [], []
    for j in args.junit:
        p = Path(j)
        if not p.exists():
            notes.append(f'{j}: JUnit file not found (the runner may have crashed before writing it)'); continue
        c, n = parse_junit(p, args.run_id, args.files_root); cases += c; notes += n
    if not cases:
        notes.append('No test cases parsed. Treat this run as not having run the tests, whatever the exit code.')
    elif args.exit_code != 0 and all(c['status'] in ('passed', 'skipped') for c in cases):
        notes.append(f'Exit code {args.exit_code} but every parsed case passed or was skipped: some tests may not '
                     f'have been reported (crash, timeout, or a reporter that writes only some projects).')
    elif args.exit_code == 0 and any(c['status'] in ('failed', 'error') for c in cases):
        notes.append('Exit code 0 despite failing cases: check whether failures are allowed (retries, continue-on-error).')

    root = Path(args.root)
    hashes = {}
    for h in args.hash:
        p = Path(h) if Path(h).is_absolute() else root / h
        hashes[Path(h).as_posix()] = sha256(p)
        if hashes[Path(h).as_posix()] is None:
            notes.append(f'{h}: could not be hashed (missing?)')

    run = {'id': args.run_id, 'command': args.command, 'exit_code': args.exit_code,
           'junit_paths': [Path(j).as_posix() for j in args.junit],
           'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'totals': totals(cases), 'file_hashes': hashes, 'notes': notes}
    if args.phase:
        run['phase'] = args.phase
    data['runs'].append(run)
    data['cases'] += cases
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    t = totals(cases)
    print(f"{args.run_id}: {t['tests']} case(s) — {t['passed']} passed, {t['failed']} failed, {t['error']} error, "
          f"{t['skipped']} skipped; exit {args.exit_code}")
    for n in notes:
        print(f'  NOTE: {n}')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
