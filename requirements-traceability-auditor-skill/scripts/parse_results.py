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

Go (gotestsum JUnit, or `go test -json` output turned into the same shape by go_json_to_junit or `--go-json`):
ids stay `<import path>::TestX/sub` (run_tests.py turns them back into `-run '^TestX$/^sub$'`). A parent test is
folded into its t.Run subtests, so one failing subtest counts once; a parent that failed while every subtest
passed is kept. `file` is the *_test.go file that defines the top-level test, found under the package directory
that go.mod's module path gives (nested modules included), else the package directory. A package that failed to
build or set up (gotestsum's TestMain case with an empty classname) becomes `<import path>::(build failed)`, an
error case with the `build_failed` signal, never a failed test named TestMain.

Rust: `--libtest FILE` (or parse_libtest / libtest_to_junit) reads `cargo test` output, libtest's text format or
its `--format json` lines. Ids are `<target>::<path::name>`, the target found from cargo's `Running <source>
(<binary>)` line: the binary's file name is the same for a package's lib and its same-named bin (dashes made
underscores), so the source path decides (a lib for src/lib.rs, a bin for src/main.rs and src/bin/, an integration
test's file stem with its dashes), and with `--cargo-repo` (or a Cargo project at `--files-root`) the target names are
cargo_project.py's, so runtime ids equal its static ids (`doctest:<crate>` for doc tests); a failed
test carries its panic message and the panic's file, an assertion panic is `assertion` and any other panic `error`;
a test binary that died before its summary line becomes `<target>::(crashed)`, an error case with the `crash`
signal. nextest_target(binary_id) maps a cargo-nextest binary id to the same target name.

    python parse_results.py --self-test
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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

PW_CONFIG_RX = re.compile(r'^playwright\.config\.[cm]?[jt]s$')
PW_TESTDIR_RX = re.compile(r'''\btestDir\s*:\s*['"`]([^'"`]+)['"`]''')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '.next', 'coverage', 'test-results', 'playwright-report'}


def playwright_test_dirs(root: Path, depth: int = 3) -> list[str]:
    """testDir of each Playwright config at most `depth` folders below root, relative to root ('/'-separated, '' for
    root itself)."""
    out = []
    for cur, dirs, files in os.walk(root):
        rel_dir = Path(cur).relative_to(root)
        dirs[:] = [] if len(rel_dir.parts) >= depth else sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if not PW_CONFIG_RX.match(name):
                continue
            try:
                m = PW_TESTDIR_RX.search((Path(cur) / name).read_text(encoding='utf-8', errors='replace'))
            except OSError:
                continue
            d = os.path.normpath(os.path.join(rel_dir.as_posix(), m.group(1) if m else '.')).replace(os.sep, '/')
            if not d.startswith('..'):
                out.append('' if d == '.' else d)
    return out


def root_relative(f: str | None, root, test_dir: str | None = None, _dirs: dict | None = None) -> str | None:
    """A relative case file made relative to root (the repository, or where the tests ran): Playwright names specs
    relative to its testDir, so a file not found under root is looked up under test_dir (given) or under the testDir of
    a Playwright config below root. test_dir is applied even when the file can't be checked (it was named)."""
    if not f or root is None or Path(f).is_absolute() or f.startswith('~/'):
        return f
    rootp = Path(root)
    if (rootp / f).exists():
        return f
    if test_dir:
        td = test_dir.replace('\\', '/').strip('/')
        return f'{td}/{f}' if td not in ('', '.') else f
    if _dirs is not None:
        if 'pw' not in _dirs:
            _dirs['pw'] = playwright_test_dirs(rootp)
        for d in _dirs['pw']:
            if d and (rootp / d / f).exists():
                return f'{d}/{f}'
    return f


def parse_junit(path: Path, run_id: str, root=None, test_dir: str | None = None) -> tuple[list[dict], list[str]]:
    """(cases, notes) from one JUnit file. root: the directory the tests ran in (or the repository), so absolute file
    attributes are recorded relative to it, and files relative to Playwright's testDir are re-rooted on it (test_dir
    names testDir relative to root when no config below root says it). Case ids, classnames and files are
    '/'-separated when they are file paths (Playwright on Windows writes backslashes)."""
    notes = []
    pw_dirs: dict = {}
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
                     'file': root_relative(relative_file(guess_file(None, suite_file, suite_name), root), root, test_dir,
                                           pw_dirs),
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
            file = root_relative(relative_file(guess_file(el, suite_file, suite_name), root), root, test_dir, pw_dirs)
            if '\\' in classname and FILE_RX.search(classname):   # a spec path written with Windows separators
                classname = classname.replace('\\', '/')
            node_path = None
            # node:test's JUnit: every case has a file attribute and a classname that is its dotted describe path
            # (or "test" at the top level); make the id file::Describe > Inner > name, as for Vitest
            if el.get('file') and (classname == '.'.join(path_names) or (classname == 'test' and not path_names)):
                node_path = ' > '.join([*path_names, name])
            go_pkg = None
            # gotestsum / go test: the classname is the package's import path, which is also the suite's name
            if not el.get('file') and suite_name and classname == suite_name and GO_PKG_RX.fullmatch(classname) \
                    and not FILE_RX.search(classname):
                build = GO_BUILD_RX.search(message or '')
                if name == 'TestMain' and build:   # the package did not compile: none of its tests ran
                    go_pkg = build.group(1)
                    name, status, ftype = f'({build.group(2)} failed)', 'error', 'error'
                elif GO_TEST_RX.match(name):
                    go_pkg = classname
            add({'name': name, 'classname': go_pkg or classname,
                 'file': file, 'status': status, 'failure_type': ftype, 'message': message,
                 'time': el.get('time'), **({'_go': go_pkg} if go_pkg else {})},
                f'{file}::{node_path}' if node_path else (f'{go_pkg}::{name}' if go_pkg else None))
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
        signals = [n for n, rx in SIGNALS if msg and rx.search(msg)]
        if c.get('_go') and c['name'].endswith(' failed)'):
            signals.append('build_failed')
        cases.append({'run_id': run_id, 'id': cid, **c, 'signals': signals, 'message_excerpt': excerpt(msg), 'time': t})

    walk(xml_root, None, None)
    if any('_go' in c for c in cases):
        cases = go_fixups(cases, root, notes)
    return cases, notes


# ------------------------------------------------------------------------------------------------ Go
GO_PKG_RX = re.compile(r'[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~+-]+)*')
GO_TEST_RX = re.compile(r'(?:Test|Example|Benchmark|Fuzz)(?:[^a-z/][^/]*)?(?:/.*)?$')
GO_BUILD_RX = re.compile(r'FAIL\s+(\S+)\s+\[(build|setup) failed\]')
_GO_MODULES: dict[str, list[tuple[str, Path]]] = {}
_GO_FUNCS: dict[str, dict[str, str]] = {}


def go_modules(root: Path) -> list[tuple[str, Path]]:
    """(module path, module directory) for every go.mod under root and the nearest one above it, longest path first.
    vendor/, node_modules/, testdata/ and dot-directories are pruned before they are walked."""
    key = str(root)
    if key not in _GO_MODULES:
        found = []

        def read(gm: Path):
            try:
                m = re.search(r'^\s*module\s+(\S+)', gm.read_text(encoding='utf-8', errors='replace'), re.M)
            except OSError:
                return
            if m:
                found.append((m.group(1).strip('"'), gm.parent))
        for up in [root, *root.parents]:
            if (up / 'go.mod').is_file():
                read(up / 'go.mod')
                break
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith('.') and d not in ('vendor', 'node_modules', 'testdata')
                      and not d.endswith(('.app', '.framework', '.xcframework'))]
            if 'go.mod' in fns and Path(dp) != root:
                read(Path(dp) / 'go.mod')
        _GO_MODULES[key] = sorted(set(found), key=lambda m: -len(m[0]))
    return _GO_MODULES[key]


def go_package_dir(import_path: str, root: Path) -> str | None:
    """The package's directory relative to root ('.' for root itself), from the go.mod module paths; None when no
    module under or above root holds it."""
    for mod, d in go_modules(root):
        if import_path == mod or import_path.startswith(mod + '/'):
            try:
                return Path(os.path.relpath(d / import_path[len(mod):].lstrip('/'), root)).as_posix()
            except ValueError:
                return None
    return None


def go_test_file(pkg_dir: str, test: str, root: Path) -> str | None:
    """The *_test.go file in pkg_dir that defines `func <top-level test>(`, or None."""
    key = str(root / pkg_dir)
    if key not in _GO_FUNCS:
        funcs = {}
        try:
            names = sorted(os.listdir(root / pkg_dir))
        except OSError:
            names = []
        for n in names:
            if n.endswith('_test.go'):
                try:
                    text = (root / pkg_dir / n).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                for m in re.finditer(r'^func\s+((?:Test|Example|Benchmark|Fuzz)\w*)\s*\(', text, re.M):
                    funcs.setdefault(m.group(1), n)
        _GO_FUNCS[key] = funcs
    f = _GO_FUNCS[key].get(test.split('/')[0])
    return (f if pkg_dir == '.' else f'{pkg_dir}/{f}') if f else None


def go_fixups(cases: list[dict], root, notes: list[str]) -> list[dict]:
    """Go cases from gotestsum JUnit: fold a parent test into its t.Run subtests (gotestsum lists both, so one failing
    subtest would count as two failures), keep a parent that failed on its own, and set `file` to the *_test.go
    file that defines the test (or the package directory) using go.mod's module path."""
    by_pkg: dict[str, list[dict]] = {}
    for c in cases:
        if c.get('_go'):
            by_pkg.setdefault(c['_go'], []).append(c)
    out, folded, kept_own = [], 0, 0
    for c in cases:
        pkg = c.get('_go')
        if pkg and not c['name'].startswith('('):
            kids = [k for k in by_pkg[pkg] if k['name'].startswith(c['name'] + '/')]
            if kids:
                if c['status'] in ('failed', 'error') and not any(k['status'] in ('failed', 'error') for k in kids):
                    kept_own += 1
                else:
                    folded += 1
                    continue
        out.append(c)
    unmapped = set()
    rootp = Path(root).resolve() if root is not None else None
    for c in out:
        pkg = c.pop('_go', None)
        if not pkg or rootp is None:
            continue
        pkg_dir = go_package_dir(pkg, rootp)
        if pkg_dir is None:
            unmapped.add(pkg)
            continue
        c['file'] = (None if c['name'].startswith('(') else go_test_file(pkg_dir, c['name'], rootp)) or pkg_dir
    if folded:
        notes.append(f'Go: {folded} parent test case(s) were folded into their t.Run subtests (gotestsum lists a parent '
                     'and each subtest; counting both would count one failure twice)')
    if kept_own:
        notes.append(f'Go: {kept_own} parent test(s) failed with every subtest passing; each is kept as a failure of its own')
    builds = sorted({c['classname'] for c in out if 'build_failed' in c.get('signals', [])})
    if builds:
        notes.append(f'Go: {len(builds)} package(s) failed to build or set up, so none of their tests ran: '
                     + ', '.join(builds[:10]))
    if unmapped:
        notes.append(f'Go: no go.mod under or above {rootp.name} holds {", ".join(sorted(unmapped)[:5])}; '
                     'those cases keep no file')
    return out


def go_json_to_junit(text: str) -> str:
    """`go test -json` events (one JSON object per line; other lines are ignored) as gotestsum-shaped JUnit XML:
    a suite per package, a case per test and subtest, and a package that failed to build as a TestMain case with
    an empty classname whose failure reads `FAIL <pkg> [build failed]`."""
    from xml.sax.saxutils import escape, quoteattr
    pkgs: dict[str, dict] = {}
    build_out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        act = e.get('Action')
        if act in ('build-output', 'build-fail'):
            if act == 'build-output':
                build_out.setdefault((e.get('ImportPath') or '').split(' ')[0], []).append(e.get('Output') or '')
            continue
        pkg = e.get('Package')
        if not pkg:
            continue
        p = pkgs.setdefault(pkg, {'tests': {}, 'out': [], 'status': None, 'failed_build': False, 'elapsed': 0})
        test = e.get('Test')
        if test:
            t = p['tests'].setdefault(test, {'status': None, 'out': [], 'elapsed': 0})
            if act == 'output':
                t['out'].append(e.get('Output') or '')
            elif act in ('pass', 'fail', 'skip'):
                t['status'], t['elapsed'] = act, e.get('Elapsed') or 0
        elif act == 'output':
            p['out'].append(e.get('Output') or '')
        elif act in ('pass', 'fail', 'skip'):
            p['status'], p['elapsed'] = act, e.get('Elapsed') or 0
            p['failed_build'] = bool(e.get('FailedBuild') or GO_BUILD_RX.search(''.join(p['out'])))
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<testsuites>']
    for pkg, p in pkgs.items():
        xml.append(f'<testsuite name={quoteattr(pkg)} time="{p["elapsed"]}">')
        if p['failed_build']:
            msg = ''.join(build_out.get(pkg, [])) + ''.join(p['out'])
            if not GO_BUILD_RX.search(msg):
                msg += f'FAIL\t{pkg} [build failed]\n'
            xml.append(f'<testcase classname="" name="TestMain" time="0"><failure message="Failed" type="">'
                       f'{escape(msg)}</failure></testcase>')
        for name, t in p['tests'].items():
            # a test still running when the package failed (a panic, a timeout) failed with it
            status = t['status'] or ('fail' if p['status'] == 'fail' else None)
            if status is None:
                continue
            out = ''.join(t['out'])
            inner = (f'<failure message="Failed" type="">{escape(out)}</failure>' if status == 'fail' else
                     f'<skipped message={quoteattr(out[-300:])}></skipped>' if status == 'skip' else '')
            xml.append(f'<testcase classname={quoteattr(pkg)} name={quoteattr(name)} time="{t["elapsed"]}">{inner}'
                       '</testcase>')
        if p['status'] == 'fail' and not p['failed_build'] and not any(
                (t['status'] or 'fail') == 'fail' for t in p['tests'].values()):
            xml.append(f'<testcase classname={quoteattr(pkg)} name="TestMain" time="0"><failure message="Failed" '
                       f'type="">{escape("".join(p["out"]))}</failure></testcase>')
        xml.append('</testsuite>')
    xml.append('</testsuites>')
    return '\n'.join(xml) + '\n'


# ------------------------------------------------------------------------------------------------ Rust (libtest)
LT_RUNNING = re.compile(r'^\s*Running (?:unittests )?(\S+) \((.+)\)\s*$')
LT_DOC = re.compile(r'^\s*Doc-tests (\S+)\s*$')
LT_TEST = re.compile(r'^test (.+?) \.\.\. ?(ok|FAILED|ignored(?:, (.*))?|bench:.*)?\s*$')
LT_RESULT = re.compile(r'^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored')
LT_RUNNING_N = re.compile(r'^running (\d+) tests?$')
LT_DIED = re.compile(r"process didn't exit successfully: .*")
LT_PANIC_AT = re.compile(r"panicked at ([^:\s]+\.rs):(\d+):\d+")
LT_ASSERT = re.compile(r'assertion(?: `[^`]*`)? failed|assert(?:_eq|_ne)?!|did not panic|panic did not contain|'
                       r'left: .*\n\s*right:', re.I)


def cargo_model(repo) -> dict | None:
    """cargo_project.read_cargo(repo) when cargo_project.py sits beside this script and repo holds a Cargo.toml."""
    if not repo or not (Path(repo) / 'Cargo.toml').is_file():
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cargo_project
    except ImportError:
        return None
    try:
        return cargo_project.read_cargo(Path(repo))
    except Exception:   # noqa: BLE001 - an unreadable project leaves the ids as the output names them
        return None


def libtest_target(binary: str) -> str:
    """The cargo target a libtest binary belongs to: target/debug/deps/tides_core-1a2b..(.exe) -> tides_core."""
    name = re.split(r'[\\/]', binary.strip().strip('`'))[-1]
    name = re.sub(r'\.exe$', '', name, flags=re.I)
    return re.sub(r'-[0-9a-f]{8,}$', '', name)


def _norm_target(name: str) -> str:
    return (name or '').replace('-', '_')


def _crate_local(path: str, cdir: str) -> str:
    """A target path relative to its package directory (cargo metadata gives it relative to the repository)."""
    path = (path or '').replace('\\', '/')
    cdir = (cdir or '.').replace('\\', '/').strip('/')
    return path[len(cdir) + 1:] if cdir not in ('', '.') and path.startswith(cdir + '/') else path


def _src_kind(src: str, unittests: bool) -> str | None:
    if src == 'src/lib.rs':
        return 'lib'
    if src == 'src/main.rs' or src.startswith('src/bin/'):
        return 'bin'
    for folder, kind in (('tests/', 'test'), ('benches/', 'bench'), ('examples/', 'example')):
        if src.startswith(folder):
            return kind
    return None if unittests else 'test'


def libtest_binary(src: str, binary: str, cargo: dict | None = None, unittests: bool = False) -> dict:
    """What a cargo `Running <src> (<binary>)` line names: {target, kind, candidates}. The binary's file name is the
    target name with dashes made underscores, the same for a package's lib and its same-named bin, so the target is
    found from the source path cargo prints (relative to the package): through the cargo_project.read_cargo model
    when given (its target names, so runtime ids equal the static ones; candidates lists every (crate, target) that
    fits), else from the path itself (a bin for src/main.rs and src/bin/, an integration test's file stem with its
    dashes)."""
    deps = libtest_target(binary)
    src = (src or '').replace('\\', '/').strip('`')
    out = {'target': deps, 'kind': _src_kind(src, unittests), 'candidates': []}
    for c in (cargo or {}).get('crates') or []:
        for t in c.get('targets') or []:
            if t.get('kind') == 'build' or _norm_target(t.get('name')) != deps:
                continue
            if _crate_local(t.get('path') or '', c.get('dir') or '.') == src:
                out['candidates'].append((c, t))
    if out['candidates']:
        _, t = out['candidates'][0]
        out['target'], out['kind'] = t['name'], t['kind']
        return out
    p = PurePosixPath(src)
    stem = p.parent.name if p.name == 'main.rs' and src != 'src/main.rs' else p.stem
    if src not in ('src/main.rs', 'src/lib.rs') and _norm_target(stem) == deps:
        out['target'] = stem
    return out


def nextest_target(binary_id: str) -> str:
    """The cargo target of a cargo-nextest binary id: `tides-core` (the lib) -> tides_core, `tides-cli::cli` -> cli,
    `tides-cli::bin/tides` -> tides (the same names parse_libtest uses, so ids match across both runners)."""
    crate, sep, rest = binary_id.partition('::')
    if not sep:
        return crate.replace('-', '_')
    return rest.split('/', 1)[1] if '/' in rest else rest


def parse_libtest(text: str, run_id: str, cargo: dict | None = None,
                  packages: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """(cases, notes) from `cargo test` output (libtest's text format, or `--format json` lines). Ids are
    `<target>::<path::name>`, the target found from cargo's `Running <source> (<binary>)` line by libtest_binary
    (with `cargo`, the cargo_project.read_cargo model of the project the run was in, target names are the model's, so
    runtime ids equal cargo_project's; `doctest:<crate>` for doc tests); each case records its target_kind. A failed test carries its panic message (`---- name stdout ----`), `file` from the panic location
    (else the integration test's file), failure_type assertion for assertion panics and error for other panics. A
    test binary that died before its summary (a signal, an access violation) becomes `<target>::(crashed)`, an error
    case with the crash signal. Summary lines and compile errors are notes."""
    text = strip_ansi(text or '')
    cases: list[dict] = []
    notes: list[str] = []
    target, tfile, expected, seen_here, ended = None, None, None, 0, True
    tkind, bidx = None, 0
    pending = None
    stdout: dict[tuple, list[str]] = {}
    cur_out = None
    by_key: dict[tuple, dict] = {}

    def add(tname, status, message='', ftype=None, file=None, signals=None):
        cid = f'{target or "(unknown)"}::{tname}'
        c = {'run_id': run_id, 'id': cid, 'name': tname, 'classname': target or '', 'file': file,
             'status': status, 'failure_type': ftype, 'signals': signals or [], 'message_excerpt': excerpt(message),
             'time': None, 'target_kind': tkind}
        cases.append(c)
        by_key[(bidx, tname)] = c
        return c

    def close_binary(reason: str | None = None):
        nonlocal ended
        if target is not None and not ended and (reason or (expected is not None and seen_here < expected)):
            add('(crashed)', 'error', reason or f'the test binary stopped after {seen_here} of {expected} test(s) '
                'without a summary line', 'error', tfile, ['crash'])
        ended = True
    for line in text.splitlines():
        s = line.rstrip()
        m = LT_RUNNING.match(s)
        d = LT_DOC.match(s)
        if m or d:
            close_binary()
            bidx += 1
            if m:
                b = libtest_binary(m.group(1), m.group(2), cargo, 'Running unittests ' in s)
                target, tkind = b['target'], b['kind']
                tfile = m.group(1).replace('\\', '/') if not m.group(1).startswith('src/') else None
            else:
                target, tfile, tkind = f'doctest:{d.group(1)}', None, 'doc'
            expected, seen_here, ended, pending, cur_out = None, 0, False, None, None
            continue
        if s.startswith('{') and '"type"' in s:
            try:
                ev = json.loads(s)
            except ValueError:
                ev = None
            if isinstance(ev, dict) and ev.get('type') == 'test' and ev.get('event') in ('ok', 'failed', 'ignored',
                                                                                          'timeout'):
                msg = ev.get('stdout') or ev.get('message') or ''
                st = {'ok': 'passed', 'failed': 'failed', 'ignored': 'skipped', 'timeout': 'failed'}[ev['event']]
                pm = LT_PANIC_AT.search(msg)
                add(ev.get('name', '(unnamed)'), st, msg,
                    None if st != 'failed' else ('assertion' if LT_ASSERT.search(msg) else 'error'),
                    pm.group(1) if pm else tfile, ['timeout'] if ev['event'] == 'timeout' else [])
                seen_here += 1
            elif isinstance(ev, dict) and ev.get('type') == 'suite' and ev.get('event') in ('ok', 'failed'):
                ended = True
            continue
        m = LT_RUNNING_N.match(s)
        if m and target is not None:
            expected = int(m.group(1))
            continue
        m = LT_TEST.match(s)
        if m and target is not None and cur_out is None:
            name, res = m.group(1), m.group(2)
            name = re.sub(r' - should panic$', '', name)
            if res is None:
                pending = name
                continue
            st = 'passed' if res == 'ok' or res.startswith('bench') else ('failed' if res == 'FAILED' else 'skipped')
            add(name, st, m.group(3) or '', None, (name.split(' - ')[0] if target.startswith('doctest:') else tfile))
            seen_here += 1
            continue
        if pending and s.strip() in ('ok', 'FAILED', 'ignored'):
            st = {'ok': 'passed', 'FAILED': 'failed', 'ignored': 'skipped'}[s.strip()]
            add(pending, st, '', None, tfile)
            seen_here, pending = seen_here + 1, None
            continue
        m = re.match(r'^---- (.+?) std(out|err) ----$', s)
        if m:
            cur_out = (bidx, m.group(1))
            stdout.setdefault(cur_out, [])
            continue
        if cur_out is not None:
            if s == 'failures:' or s.startswith('test result:'):
                cur_out = None
            else:
                stdout[cur_out].append(line)
                continue
        m = LT_RESULT.match(s)
        if m and target is not None:
            notes.append(f'{target}: {s}')
            ended = True
            continue
        m = LT_DIED.search(s)
        if m and target is not None and not ended:
            close_binary(m.group(0))
            continue
        if re.match(r'^error(\[E\d+\])?: could not compile', s) or s.startswith('error: no test target named') or \
                s.startswith('error: no such command'):
            notes.append(s.strip())
    close_binary()
    for (tg, name), out in stdout.items():
        c = by_key.get((tg, name))
        if not c:
            continue
        msg = '\n'.join(x for x in out if x.strip()).strip()
        c['message_excerpt'] = excerpt(msg)
        c['signals'] = [n for n, rx in SIGNALS if rx.search(msg)]
        if c['status'] == 'failed':
            c['failure_type'] = 'assertion' if LT_ASSERT.search(msg) else 'error'
            pm = LT_PANIC_AT.search(msg)
            if pm:
                c['file'] = pm.group(1).replace('\\', '/')
    for c in cases:
        if c['status'] == 'failed' and not c['failure_type']:
            c['failure_type'] = 'error'
    return cases, notes


NT_LINE = re.compile(r'^\s*(PASS|FAIL|FLAKY|SIGSEGV|SIGABRT|SIGKILL|SIGBUS|ABORT|TIMEOUT|LEAK|LEAK-FAIL|SKIP|'
                     r'TRY \d+ PASS)\s+\[\s*[\d.]*s?\s*\]\s+(?:\(\s*\d+/\d+\)\s+)?(\S+)\s+(.+?)\s*$')


def parse_nextest_output(text: str, run_id: str) -> tuple[list[dict], list[str]]:
    """(cases, notes) from cargo-nextest's human output (`PASS [ 0.004s] crate::binary test::path`), for a run whose
    profile writes no JUnit. Ids are `<target>::<path>` as parse_libtest gives them (nextest_target). Skipped tests
    are not listed by nextest, so they are only in the summary line (kept as a note)."""
    cases, notes, last = [], [], {}
    for line in strip_ansi(text or '').splitlines():
        m = NT_LINE.match(line)
        if m:
            kind, binary, name = m.group(1), m.group(2), m.group(3)
            target = nextest_target(binary)
            status = ('passed' if kind in ('PASS', 'FLAKY', 'LEAK') or kind.startswith('TRY') else
                      'skipped' if kind == 'SKIP' else 'error' if kind.startswith(('SIG', 'ABORT')) else 'failed')
            sig = ['crash'] if kind.startswith(('SIG', 'ABORT')) else ['timeout'] if kind == 'TIMEOUT' else []
            if kind == 'FLAKY':
                sig.append('flaky')
            c = {'run_id': run_id, 'id': f'{target}::{name}', 'name': name, 'classname': target, 'file': None,
                 'status': status, 'failure_type': None if status in ('passed', 'skipped') else 'error',
                 'signals': sig, 'message_excerpt': '' if status == 'passed' else kind, 'time': None}
            key = c['id']
            if key in last:   # a retry line: the last result stands
                cases[last[key]] = c
            else:
                last[key] = len(cases)
                cases.append(c)
        elif re.match(r'^\s*Summary \[', line):
            notes.append(line.strip())
    return cases, notes


def nextest_output_to_junit(text: str) -> str:
    """JUnit XML from cargo-nextest's human output (parse_nextest_output), ids <target>::<path>."""
    from xml.sax.saxutils import quoteattr
    cases, _ = parse_nextest_output(text, 'x')
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<testsuites>', '  <testsuite name="cargo nextest">']
    for c in cases:
        attrs = f'classname={quoteattr(c["classname"])} name={quoteattr(c["name"])}'
        if c['status'] == 'passed':
            xml.append(f'    <testcase {attrs}/>')
        elif c['status'] == 'skipped':
            xml.append(f'    <testcase {attrs}><skipped/></testcase>')
        else:
            tag = 'failure' if c['status'] == 'failed' else 'error'
            kind = 'crash' if 'crash' in c['signals'] else 'panic: nextest ' + c['message_excerpt']
            xml.append(f'    <testcase {attrs}><{tag} type={quoteattr(kind)} message=""/></testcase>')
    xml += ['  </testsuite>', '</testsuites>']
    return '\n'.join(xml) + '\n'


def libtest_to_junit(text: str, cargo: dict | None = None, packages: list[str] | None = None) -> str:
    """JUnit XML (one suite per test binary) from `cargo test` output, so parse_junit gives the ids parse_libtest
    does (<target>::<path::name>; cargo and packages as for parse_libtest)."""
    from xml.sax.saxutils import escape, quoteattr
    cases, _ = parse_libtest(text, 'x', cargo, packages)
    suites: dict[str, list[dict]] = {}
    for c in cases:
        suites.setdefault(c['classname'], []).append(c)
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<testsuites>']
    for tg, cs in suites.items():
        fails = sum(c['status'] == 'failed' for c in cs)
        errs = sum(c['status'] == 'error' for c in cs)
        xml.append(f'  <testsuite name={quoteattr("cargo test " + tg)} tests="{len(cs)}" failures="{fails}" '
                   f'errors="{errs}">')
        for c in cs:
            attrs = f'classname={quoteattr(tg)} name={quoteattr(c["name"])}'
            if c.get('file'):
                attrs += f' file={quoteattr(c["file"])}'
            msg = c.get('message_excerpt') or ''
            if c['status'] == 'passed':
                xml.append(f'    <testcase {attrs}/>')
            elif c['status'] == 'skipped':
                xml.append(f'    <testcase {attrs}><skipped message={quoteattr(msg)}/></testcase>')
            else:
                tag = 'failure' if c['status'] == 'failed' else 'error'
                kind = 'crash' if 'crash' in c['signals'] else ('assertion failed' if c['failure_type'] == 'assertion'
                                                                 else 'panic: runtime')
                xml.append(f'    <testcase {attrs}><{tag} type={quoteattr(kind)} message="">{escape(msg)}</{tag}>'
                           '</testcase>')
        xml.append('  </testsuite>')
    xml.append('</testsuites>')
    return '\n'.join(xml) + '\n'


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
    checks += go_self_checks()
    checks += playwright_windows_checks()
    checks += libtest_self_checks()
    checks += libtest_workspace_checks()
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


GO_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="7" failures="3" errors="0" time="1.0">
  <testsuite tests="0" failures="0" time="0" name="example.test/shop"></testsuite>
  <testsuite tests="5" failures="2" time="0.5" name="example.test/shop/pkg/cart">
    <properties><property name="go.version" value="go1.26 linux/amd64"></property></properties>
    <testcase classname="example.test/shop/pkg/cart" name="TestTotal" time="0.01"><failure message="Failed" type="">=== RUN   TestTotal&#xA;--- FAIL: TestTotal (0.00s)</failure></testcase>
    <testcase classname="example.test/shop/pkg/cart" name="TestTotal/empty" time="0.00"></testcase>
    <testcase classname="example.test/shop/pkg/cart" name="TestTotal/negative_qty" time="0.00"><failure message="Failed" type="">    cart_test.go:14: got 3, want 0&#xA;--- FAIL: TestTotal/negative_qty (0.00s)</failure></testcase>
    <testcase classname="example.test/shop/pkg/cart" name="TestPlain" time="0.00"></testcase>
    <testcase classname="example.test/shop/pkg/cart" name="TestCleanup" time="0.00"><failure message="Failed" type="">    other_test.go:20: cleanup left files</failure></testcase>
    <testcase classname="example.test/shop/pkg/cart" name="TestCleanup/inner" time="0.00"></testcase>
  </testsuite>
  <testsuite tests="1" failures="1" errors="0" time="0" name="example.test/shop/pkg/broken">
    <testcase classname="" name="TestMain" time="0.000000"><failure message="Failed" type="">FAIL&#x9;example.test/shop/pkg/broken [build failed]&#xA;</failure></testcase>
  </testsuite>
  <testsuite tests="1" failures="0" time="0.1" name="example.test/shop/tools/lint">
    <testcase classname="example.test/shop/tools/lint" name="TestRules" time="0.00"></testcase>
  </testsuite>
</testsuites>"""

GO_JSON_EVENTS = [
    {'Action': 'start', 'Package': 'example.test/shop/pkg/cart'},
    {'Action': 'run', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal'},
    {'Action': 'run', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal/empty'},
    {'Action': 'pass', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal/empty', 'Elapsed': 0},
    {'Action': 'run', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal/negative_qty'},
    {'Action': 'output', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal/negative_qty',
     'Output': '    cart_test.go:14: got 3, want 0\n'},
    {'Action': 'fail', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal/negative_qty', 'Elapsed': 0},
    {'Action': 'fail', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestTotal', 'Elapsed': 0.01},
    {'Action': 'run', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestPlain'},
    {'Action': 'skip', 'Package': 'example.test/shop/pkg/cart', 'Test': 'TestPlain', 'Elapsed': 0},
    {'Action': 'fail', 'Package': 'example.test/shop/pkg/cart', 'Elapsed': 0.2},
    {'ImportPath': 'example.test/shop/pkg/broken [example.test/shop/pkg/broken.test]', 'Action': 'build-output',
     'Output': 'pkg/broken/broken.go:3:1: syntax error\n'},
    {'ImportPath': 'example.test/shop/pkg/broken [example.test/shop/pkg/broken.test]', 'Action': 'build-fail'},
    {'Action': 'start', 'Package': 'example.test/shop/pkg/broken'},
    {'Action': 'output', 'Package': 'example.test/shop/pkg/broken', 'Output': 'FAIL\texample.test/shop/pkg/broken [build failed]\n'},
    {'Action': 'fail', 'Package': 'example.test/shop/pkg/broken', 'Elapsed': 0,
     'FailedBuild': 'example.test/shop/pkg/broken [example.test/shop/pkg/broken.test]'},
]


LIBTEST_OUTPUT = """   Compiling tides-core v0.1.0 (/work/crates/tides-core)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.20s
     Running unittests src/lib.rs (target/debug/deps/tides_core-1a2b3c4d5e6f7a8b)

running 4 tests
test height::tests::converts ... ok
test tests::reads_gauge ... ignored, needs a tide gauge
test tests::doubles ... FAILED
test tests::unwraps ... FAILED

failures:

---- tests::doubles stdout ----

thread 'tests::doubles' panicked at crates/tides-core/src/lib.rs:20:5:
assertion `left == right` failed
  left: 5
 right: 4
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

---- tests::unwraps stdout ----

thread 'tests::unwraps' panicked at crates/tides-core/src/lib.rs:24:9:
called `Result::unwrap()` on an `Err` value: "no station"


failures:
    tests::doubles
    tests::unwraps

test result: FAILED. 1 passed; 2 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s

error: test failed, to rerun pass `-p tides-core --lib`
     Running unittests src/main.rs (target\\debug\\deps\\tides-0f1e2d3c4b5a6978.exe)

running 1 test
test tests::parses ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/cli.rs (target/debug/deps/cli-9a8b7c6d5e4f3a2b)

running 2 tests
test prints_help ... ok
test slow_station ... ignored

test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.02s

     Running tests/crash.rs (target/debug/deps/crash-1122334455667788)

running 2 tests
test reads_null ... ok
error: test failed, to rerun pass `-p tides-cli --test crash`

Caused by:
  process didn't exit successfully: `/work/target/debug/deps/crash-1122334455667788` (signal: 11, SIGSEGV: invalid memory reference)
   Doc-tests tides_core

running 1 test
test crates/tides-core/src/lib.rs - double (line 7) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s

error: 2 targets failed:
    `-p tides-core --lib`
    `-p tides-cli --test crash`
"""

# a workspace whose package `tide-log` has a lib, a bin (src/main.rs) and tests/cli.rs, and whose package `tidectl`
# also has tests/cli.rs; cargo names both unit-test binaries tide_log-<hash> and both integration binaries cli-<hash>
LIBTEST_WS_OUTPUT = """     Running unittests src/lib.rs (target/debug/deps/tide_log-1111111111111111)

running 1 test
test tests::opens ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running unittests src/main.rs (target/debug/deps/tide_log-2222222222222222)

running 1 test
test tests::parses_args ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/cli.rs (target/debug/deps/cli-3333333333333333)

running 2 tests
test prints_help ... ok
test writes_log ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/round-trip.rs (target/debug/deps/round_trip-4444444444444444)

running 1 test
test reads_back ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/cli.rs (target/debug/deps/cli-5555555555555555)

running 2 tests
test prints_help ... ok
test slow_station ... FAILED

failures:

---- slow_station stdout ----

thread 'slow_station' panicked at crates/tidectl/tests/cli.rs:5:5:
assertion failed: false


failures:
    slow_station

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `-p tidectl --test cli`
"""


def _ws_target(kind, name, path):
    return {'kind': kind, 'name': name, 'path': path}


def _ws_test(name, target, kind):
    return {'name': name, 'target': target, 'target_kind': kind}


# the cargo_project.read_cargo model of that workspace, cut to what parse_libtest reads (tidectl's target paths are
# repository-relative, as cargo metadata gives them)
LIBTEST_WS_MODEL = {'crates': [
    {'name': 'tide-log', 'dir': 'crates/tide-log',
     'targets': [_ws_target('lib', 'tide_log', 'src/lib.rs'), _ws_target('bin', 'tide-log', 'src/main.rs'),
                 _ws_target('test', 'cli', 'tests/cli.rs'), _ws_target('test', 'round-trip', 'tests/round-trip.rs')],
     'tests': [_ws_test('tests::opens', 'tide_log', 'lib'), _ws_test('tests::parses_args', 'tide-log', 'bin'),
               _ws_test('prints_help', 'cli', 'test'), _ws_test('writes_log', 'cli', 'test'),
               _ws_test('reads_back', 'round-trip', 'test')]},
    {'name': 'tidectl', 'dir': 'crates/tidectl',
     'targets': [_ws_target('bin', 'tidectl', 'crates/tidectl/src/main.rs'),
                 _ws_target('test', 'cli', 'crates/tidectl/tests/cli.rs')],
     'tests': [_ws_test('prints_help', 'cli', 'test'), _ws_test('slow_station', 'cli', 'test')]}]}


def libtest_workspace_checks() -> list:
    """cargo test run from a workspace root: target names as cargo_project.py gives them (a bin keeps its dashes and
    is never labelled the lib), same-named targets of two crates told apart, files relative to the repository."""
    out = []
    fn = globals().get('parse_libtest')
    try:
        cases, notes = fn(LIBTEST_WS_OUTPUT, 'W', cargo=LIBTEST_WS_MODEL)
    except TypeError as e:
        cases, notes = [], [repr(e)]
    ids = {c['id'] for c in cases}
    out.append(('libtest (workspace, model known): a bin built as deps/tide_log-<hash> from src/main.rs is the bin '
                '`tide-log`, not the lib; the lib keeps `tide_log`; an integration test keeps its dashes',
                {'tide_log::tests::opens', 'tide-log::tests::parses_args', 'round-trip::reads_back'} <= ids,
                sorted(ids) or notes))
    plain, _ = fn(LIBTEST_WS_OUTPUT, 'W')
    by = {c['id']: c for c in plain}
    out.append(('libtest (no model): an integration test keeps its file stem\'s dashes; a src/main.rs binary is '
                'recorded as a bin', 'round-trip::reads_back' in by and any(
                    c['name'] == 'tests::parses_args' and c.get('target_kind') == 'bin' for c in plain),
                sorted((c['id'], c.get('target_kind')) for c in plain)))
    return out


def libtest_self_checks() -> list:
    """`cargo test` (libtest) text output: ids `<target>::<path::name>`, statuses, panics, a crashed test binary, doc
    tests, and the JUnit made from it."""
    import tempfile
    out = []
    fn = globals().get('parse_libtest')
    if fn is None:
        return [('cargo test output is parsed (parse_libtest)', False, 'parse_libtest is missing')]
    cases, notes = fn(LIBTEST_OUTPUT, 'R1')
    got = {c['id']: c['status'] for c in cases}
    out.append(('libtest: ids are <target>::<path::name>, the target named from the test binary', got == {
        'tides_core::height::tests::converts': 'passed', 'tides_core::tests::reads_gauge': 'skipped',
        'tides_core::tests::doubles': 'failed', 'tides_core::tests::unwraps': 'failed', 'tides::tests::parses': 'passed',
        'cli::prints_help': 'passed', 'cli::slow_station': 'skipped', 'crash::reads_null': 'passed',
        'crash::(crashed)': 'error', 'doctest:tides_core::crates/tides-core/src/lib.rs - double (line 7)': 'passed'}, got))
    by = {c['id']: c for c in cases}
    d, u = by.get('tides_core::tests::doubles', {}), by.get('tides_core::tests::unwraps', {})
    out.append(('libtest: a failed assertion is an assertion with its panic message; an unwrap panic is an error',
                d.get('failure_type') == 'assertion' and 'left: 5' in (d.get('message_excerpt') or '')
                and d.get('file') == 'crates/tides-core/src/lib.rs' and u.get('failure_type') == 'error'
                and 'no station' in (u.get('message_excerpt') or ''), (d, u)))
    cr = by.get('crash::(crashed)', {})
    out.append(('libtest: a test binary that died mid-run is an error case with the crash signal, never a clean pass',
                'crash' in cr.get('signals', []) and 'SIGSEGV' in (cr.get('message_excerpt') or ''), cr))
    out.append(('libtest: integration tests carry their file; ignored tests their reason',
                by.get('cli::prints_help', {}).get('file') == 'tests/cli.rs'
                and 'needs a tide gauge' in (by.get('tides_core::tests::reads_gauge', {}).get('message_excerpt') or ''),
                (by.get('cli::prints_help'), by.get('tides_core::tests::reads_gauge'))))
    out.append(('libtest: the summary lines are kept as notes', any('test result: FAILED. 1 passed; 2 failed' in n
                                                                    for n in notes), notes))
    jcases = []
    with tempfile.TemporaryDirectory() as tmp:
        xp = Path(tmp) / 'cargo.xml'
        xp.write_text(globals().get('libtest_to_junit', lambda t: '')(LIBTEST_OUTPUT), encoding='utf-8')
        try:
            jcases, _ = parse_junit(xp, 'R1')
        except Exception as e:   # noqa: BLE001
            jcases = [{'id': repr(e), 'status': 'error'}]
    out.append(('libtest_to_junit gives the same ids and statuses through parse_junit',
                {c['id']: c['status'] for c in jcases} == got, sorted((c['id'], c['status']) for c in jcases)))
    js = '\n'.join(json.dumps(e) for e in [
        {'type': 'suite', 'event': 'started', 'test_count': 2},
        {'type': 'test', 'event': 'started', 'name': 'tests::a'},
        {'type': 'test', 'name': 'tests::a', 'event': 'ok'},
        {'type': 'test', 'name': 'tests::b', 'event': 'failed', 'stdout': "thread 'tests::b' panicked at src/lib.rs:3:5:\nassertion failed: false\n"},
        {'type': 'test', 'name': 'tests::c', 'event': 'ignored'},
        {'type': 'suite', 'event': 'failed', 'passed': 1, 'failed': 1, 'ignored': 1}])
    cases, _ = fn('     Running unittests src/lib.rs (target/debug/deps/gauges-0011223344556677)\n' + js, 'R2')
    out.append(('libtest JSON lines (--format json) are read too', {c['id']: c['status'] for c in cases} == {
        'gauges::tests::a': 'passed', 'gauges::tests::b': 'failed', 'gauges::tests::c': 'skipped'}, cases))
    return out


def playwright_windows_checks() -> list:
    """Playwright's JUnit on Windows: the classname is the spec path relative to testDir, with backslashes."""
    import tempfile
    out = []
    xml = ('<?xml version="1.0"?><testsuites><testsuite name="trails\\loop.spec.ts" tests="1">'
           '<testcase name="loop page shows the map" classname="trails\\loop.spec.ts" time="1.2"/></testsuite></testsuites>')
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, body in {'web/playwright.config.ts': "export default { testDir: './e2e' };\n",
                          'web/e2e/trails/loop.spec.ts': "test('loop page shows the map', async () => {});\n"}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding='utf-8')
        (root / 'pw.xml').write_text(xml, encoding='utf-8')
        plain, _ = parse_junit(root / 'pw.xml', 'P')
        rooted, _ = parse_junit(root / 'pw.xml', 'P', root / 'web')
        given, _ = parse_junit(root / 'pw.xml', 'P', root, test_dir='web/e2e')
    out.append(('Playwright on Windows: ids and files use forward slashes',
                [(c['id'], c['file'], c['classname']) for c in plain]
                == [('trails/loop.spec.ts::loop page shows the map', 'trails/loop.spec.ts', 'trails/loop.spec.ts')],
                plain))
    out.append(('with --files-root, a file relative to Playwright\'s testDir (read from the config) is made relative '
                'to that root', [c['file'] for c in rooted] == ['e2e/trails/loop.spec.ts'], [c['file'] for c in rooted]))
    out.append(('--test-dir names testDir when the config is elsewhere',
                [c['file'] for c in given] == ['web/e2e/trails/loop.spec.ts'], [c['file'] for c in given]))
    return out


def go_self_checks() -> list:
    """gotestsum JUnit and go test -json on a synthetic module: subtests, file mapping, build failures."""
    import tempfile
    out = []
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        files = {'go.mod': 'module example.test/shop\n\ngo 1.26\n',
                 'pkg/cart/cart.go': 'package cart\n',
                 'pkg/cart/cart_test.go': 'package cart\n\nfunc TestTotal(t *testing.T) {\n}\n',
                 'pkg/cart/other_test.go': 'package cart\n\nfunc TestPlain(t *testing.T) {}\n\nfunc TestCleanup(t *testing.T) {}\n',
                 'pkg/broken/broken.go': 'package broken\n',
                 'tools/lint/go.mod': 'module example.test/shop/tools/lint\n',
                 'tools/lint/rules_test.go': 'package lint\n\nfunc TestRules(t *testing.T) {}\n'}
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding='utf-8')
        (root / 'go.xml').write_text(GO_JUNIT, encoding='utf-8')
        cases, notes = parse_junit(root / 'go.xml', 'G', root)
        by = {c['id']: c for c in cases}
        P = 'example.test/shop/pkg/cart::'
        out.append(('Go: a parent whose failure comes from a t.Run subtest is folded into its subtests',
                    P + 'TestTotal' not in by and by.get(P + 'TestTotal/negative_qty', {}).get('status') == 'failed'
                    and totals(cases)['failed'] == 2, sorted(by)))
        out.append(('Go: a parent that failed on its own (every subtest passed) is kept',
                    by.get(P + 'TestCleanup', {}).get('status') == 'failed', sorted(by)))
        out.append(('Go: a case\'s file is the _test.go file that defines the top-level test',
                    by.get(P + 'TestTotal/empty', {}).get('file') == 'pkg/cart/cart_test.go'
                    and by.get(P + 'TestPlain', {}).get('file') == 'pkg/cart/other_test.go',
                    {k: v.get('file') for k, v in by.items()}))
        out.append(('Go: an import path in a nested module maps to its directory',
                    by.get('example.test/shop/tools/lint::TestRules', {}).get('file') == 'tools/lint/rules_test.go',
                    {k: v.get('file') for k, v in by.items()}))
        b = by.get('example.test/shop/pkg/broken::(build failed)', {})
        out.append(('Go: a package build failure is an error case for the package, not a failed TestMain',
                    b.get('status') == 'error' and b.get('file') == 'pkg/broken' and 'build_failed' in b.get('signals', [])
                    and not any(c['name'] == 'TestMain' for c in cases), b or sorted(by)))
        (root / 'go.json').write_text('\n'.join(json.dumps(e) for e in GO_JSON_EVENTS) + '\n', encoding='utf-8')
        (root / 'from-json.xml').write_text(go_json_to_junit((root / 'go.json').read_text(encoding='utf-8')),
                                            encoding='utf-8')
        jcases, _ = parse_junit(root / 'from-json.xml', 'J', root)
        got = sorted((c['id'], c['status']) for c in jcases)
        out.append(('Go: go test -json events give the same cases as gotestsum JUnit', got == [
            ('example.test/shop/pkg/broken::(build failed)', 'error'),
            (P + 'TestPlain', 'skipped'), (P + 'TestTotal/empty', 'passed'), (P + 'TestTotal/negative_qty', 'failed')], got))
    return out



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Append a JUnit XML run to a normalised results file.')
    ap.add_argument('results', help='results JSON to create or append to')
    ap.add_argument('--junit', nargs='+', default=[], help='JUnit XML file(s) produced by this run')
    ap.add_argument('--go-json', nargs='+', default=[], metavar='FILE',
                    help='`go test -json` output file(s) of this run (converted to gotestsum-shaped JUnit next to each)')
    ap.add_argument('--libtest', nargs='+', default=[], metavar='FILE',
                    help='`cargo test` output file(s) of this run (libtest text or JSON; converted to JUnit next to each)')
    ap.add_argument('--cargo-repo', metavar='DIR',
                    help='the Cargo project the --libtest run was in (default: --files-root when it holds a '
                         'Cargo.toml): target names and files come from cargo_project.py, as its static ids do')
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--command', required=True, help='the exact command that was run')
    ap.add_argument('--exit-code', type=int, required=True)
    ap.add_argument('--hash', nargs='*', default=[], help='files to fingerprint (SHA-256) as of this run')
    ap.add_argument('--root', default='.', help='paths in --hash are recorded relative to this directory')
    ap.add_argument('--phase', help='optional label stored on the run, e.g. red, green, regression, re_red')
    ap.add_argument('--files-root', help='record test file paths relative to this directory (the repository, or where the '
                                         'tests ran): absolute paths are made relative, and files Playwright names '
                                         'relative to its testDir are re-rooted (testDir read from playwright.config.*)')
    ap.add_argument('--test-dir', help="Playwright's testDir relative to --files-root, when no config below it says so")
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    args = ap.parse_args(argv)
    if not args.junit and not args.go_json and not args.libtest:
        ap.error('give --junit, --go-json or --libtest')
    model = cargo_model(args.cargo_repo or args.files_root) if args.libtest else None
    for g in args.libtest:
        gp = Path(g)
        if gp.exists():
            xp = gp.with_suffix('.junit.xml')
            xp.write_text(libtest_to_junit(gp.read_text(encoding='utf-8', errors='replace'), model), encoding='utf-8')
            args.junit.append(str(xp))
        else:
            args.junit.append(g)
    for g in args.go_json:
        gp = Path(g)
        if gp.exists():
            xp = gp.with_suffix('.junit.xml')
            xp.write_text(go_json_to_junit(gp.read_text(encoding='utf-8', errors='replace')), encoding='utf-8')
            args.junit.append(str(xp))
        else:
            args.junit.append(g)   # reported below as not found

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
        c, n = parse_junit(p, args.run_id, args.files_root, args.test_dir); cases += c; notes += n
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
