#!/usr/bin/env python3
"""Lint generated journey tests for the rules the Journey to Tests skill must not break.

    python lint_tests.py e2e/checkout/guest-checkout.spec.ts e2e/pages/CheckoutPage.ts [--json]

Rules (level):
  hard_wait            (error)   sleep / waitForTimeout / Thread.sleep / cy.wait(<ms>) and similar
  no_assertion         (error)   a test block with no assertion
  missing_journey_tag  (error)   a test whose title, tags or enclosing block names no JRN-<DOMAIN>-<NNN> id
  focused_test         (error)   .only / fit / fdescribe left in the file
  brittle_selector     (warning) XPath, nth-child, positional or class-based CSS selectors
  conditional_test     (warning) if/else on page state inside a test (tests should be deterministic)

Supports JavaScript/TypeScript (Playwright, Cypress, WebdriverIO, Detox, Jest/Vitest), Python
(pytest, Playwright, Selenium, Appium), Java/Kotlin (JUnit, Espresso, Compose), Swift (XCUITest),
Dart (flutter_test, integration_test, Patrol), C# (xUnit/NUnit/MSTest), Gherkin feature files
and Maestro flows. Files with no test blocks (page objects, helpers) get the file-level rules only.
Heuristic: it reads source text, not an AST. Each finding carries a line number to check.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

JRN = re.compile(r'JRN[-_][A-Z0-9]+[-_]\d{3}')

HARD_WAITS = [
    re.compile(r'\bwaitForTimeout\s*\('), re.compile(r'\bwait_for_timeout\s*\('), re.compile(r'\bcy\.wait\(\s*\d'),
    re.compile(r'\btime\.sleep\s*\('), re.compile(r'(?<![\w.])sleep\s*\(\s*\d'), re.compile(r'\bThread\.sleep\s*\('),
    re.compile(r'\bSystemClock\.sleep\s*\('), re.compile(r'\bbrowser\.pause\s*\('), re.compile(r'\bdriver\.sleep\s*\('),
    re.compile(r'new Promise\s*\(\s*\(?\s*\w*\s*\)?\s*=>\s*setTimeout'), re.compile(r'\bFuture\.delayed\s*\('),
    re.compile(r'\bTask\.Delay\s*\('), re.compile(r'\busleep\s*\('), re.compile(r'\bdelay\s*\(\s*\d{3,}'),
    re.compile(r'\bawait\s+(page\.)?waitFor\(\s*\d'), re.compile(r'\bpump\s*\(\s*(const\s+)?Duration\('),
    re.compile(r'^\s*-\s*wait:\s*\d', re.M),
]
ASSERTIONS = {
    'js': re.compile(r'\bexpect\s*\(|\bassert\b|\.should\s*\(|\bshould\(|\.to(Be|Have|Equal|Contain|Match)\w*\(|'
                     r'\bexpect\.soft\(|\bwaitFor\(element\(.*\)\)\.to'),
    'py': re.compile(r'^\s*assert\b|\bexpect\s*\(|self\.assert\w+\(|\bassert_that\s*\(|\bpytest\.raises\('),
    'jvm': re.compile(r'\bassert\w*\s*\(|\bassertThat\s*\(|\.check\s*\(|\.assert\w+\(|\bexpectThat\s*\(|\bshouldBe\b|'
                      r'\bverify\s*\('),
    'swift': re.compile(r'\bXCTAssert\w*\s*\(|\bXCTFail\s*\(|\b#expect\s*\(|\bwaitForExistence\b.*XCTAssert'),
    'dart': re.compile(r'\bexpect\s*\(|\bexpectLater\s*\('),
    'cs': re.compile(r'\bAssert\.\w+\s*\(|\.Should\(\)|\bExpect\s*\('),
}
BRITTLE = [
    (re.compile(r':nth-child\(|:nth-of-type\(|:eq\(\d'), 'positional CSS selector'),
    (re.compile(r'xpath=|By\.xpath\(|By\.XPATH|AppiumBy\.XPATH|["\']//[\w*]+\['), 'XPath selector'),
    (re.compile(r'\.eq\(\s*\d+\s*\)|\.nth\(\s*\d+\s*\)|\.get\(\s*\d+\s*\)\.click'), 'positional index'),
    (re.compile(r'(?:locator|cy\.get|querySelector(?:All)?|\$\$?|find_element\(By\.CSS_SELECTOR,|By\.cssSelector)\(\s*'
                r'["\'][^"\']*\.[a-zA-Z][\w-]*(?:\s|>|$|["\'])'), 'class-based CSS selector'),
]
FOCUSED = re.compile(r'\b(?:test|it|describe|context)\.only\s*\(|\b(?:fit|fdescribe|fcontext)\s*\(|@pytest\.mark\.only|'
                     r'\bsolo\s*:\s*true')
CONDITIONAL = re.compile(r'\bif\s*\(\s*!?\s*\(?\s*await\s+[^\n]*?\.(isVisible|isEnabled|count|isChecked|isDisplayed)\(|'
                         r'\bif\s+(not\s+)?[^\n]*?\.(is_visible|is_enabled|count|is_displayed)\(\)')

def lang_of(path: Path) -> str | None:
    s = path.suffix.lower()
    if s in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'): return 'js'
    if s == '.py': return 'py'
    if s in ('.java', '.kt'): return 'jvm'
    if s == '.swift': return 'swift'
    if s == '.dart': return 'dart'
    if s == '.cs': return 'cs'
    if s == '.feature': return 'gherkin'
    if s in ('.yaml', '.yml'): return 'maestro'
    return None

def strip_comments(text: str, lang: str) -> str:
    """Blank out comments (keeping line numbers) so commented-out code doesn't count."""
    if lang == 'py':
        return re.sub(r'(?m)^(\s*)#.*$', r'\1', text)
    if lang in ('gherkin', 'maestro'):
        return re.sub(r'(?m)^(\s*)#.*$', r'\1', text)
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith('//', i):
            j = text.find('\n', i); j = n if j < 0 else j
            out.append(' ' * (j - i)); i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2); j = n if j < 0 else j + 2
            out.append(re.sub(r'[^\n]', ' ', text[i:j])); i = j
        else:
            out.append(c); i += 1
    return ''.join(out)

def match_close(text: str, i: int, open_c: str, close_c: str) -> int:
    """Index of the bracket closing text[i] (which is open_c), skipping strings. -1 if unbalanced."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1; continue
        if c == open_c: depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0: return i
        i += 1
    return -1

def line_of(text: str, idx: int) -> int:
    return text.count('\n', 0, idx) + 1

# ---------- test block extraction: [{name, start, end, header}] (character offsets) ----------

def blocks_brace_call(text: str, call_rx: re.Pattern, open_c='(', close_c=')') -> list[dict]:
    out = []
    for m in call_rx.finditer(text):
        p = text.find(open_c, m.end() - 1)
        if p < 0: continue
        e = match_close(text, p, open_c, close_c)
        if e < 0: continue
        name = m.groupdict().get('name')
        if name is None:
            t = re.match(r'\s*(["\'`])((?:\\.|(?!\1).)*)\1', text[p + 1:p + 400], re.S)
            name = t.group(2) if t else ''
        # header: the call up to its callback (title plus any options/tags object)
        cb = re.compile(r'=>|\bfunction\b|\basync\b').search(text, p, min(e, p + 600))
        out.append({'name': name, 'start': m.start(), 'end': e, 'header': text[m.start():cb.start() if cb else min(e, p + 300)]})
    return out

def js_blocks(text: str):
    tests = blocks_brace_call(text, re.compile(r'(?<![\w.$])(?:test|it|specify)(?:\.(?:only|skip|fixme|fail|slow|concurrent|todo))?\s*\('))
    groups = blocks_brace_call(text, re.compile(r'(?<![\w.$])(?:test\.describe|describe|context|suite)(?:\.(?:only|skip|serial|parallel|configure))?\s*\('))
    tests = [t for t in tests if not text[t['start']:t['start'] + 20].startswith(('test.describe', 'test.step', 'test.use', 'test.extend', 'test.beforeEach', 'test.afterEach', 'test.beforeAll', 'test.afterAll', 'test.info'))]
    return tests, groups

def jvm_blocks(text: str):
    tests = []
    for m in re.finditer(r'@(?:Test|ParameterizedTest|RepeatedTest)\b', text):
        sig = re.compile(r'(?:fun\s+`?(?P<k>[^`(]+)`?|void\s+(?P<j>\w+))\s*\(').search(text, m.end())
        if not sig: continue
        b = text.find('{', sig.end())
        e = match_close(text, b, '{', '}') if b >= 0 else -1
        if e < 0: continue
        header = text[m.start():b]
        tests.append({'name': sig.group('k') or sig.group('j'), 'start': m.start(), 'end': e, 'header': header})
    groups = []
    for m in re.finditer(r'\bclass\s+(\w+)[^{]*\{', text):
        e = match_close(text, m.end() - 1, '{', '}')
        if e > 0:
            groups.append({'name': m.group(1), 'start': m.start(), 'end': e, 'header': text[max(0, m.start() - 300):m.end()]})
    return tests, groups

def swift_blocks(text: str):
    tests = []
    for m in re.finditer(r'(?:@Test[^\n]*\n\s*)?func\s+(test\w*|\w+)\s*\([^)]*\)[^{]*\{', text):
        if not (m.group(1).startswith('test') or m.group(0).startswith('@Test')): continue
        e = match_close(text, m.end() - 1, '{', '}')
        if e > 0: tests.append({'name': m.group(1), 'start': m.start(), 'end': e, 'header': m.group(0)})
    groups = [{'name': m.group(1), 'start': m.start(), 'end': match_close(text, m.end() - 1, '{', '}'), 'header': m.group(0)}
              for m in re.finditer(r'\bclass\s+(\w+)[^{]*\{', text)]
    return tests, [g for g in groups if g['end'] > 0]

def dart_blocks(text: str):
    tests = blocks_brace_call(text, re.compile(r'(?<![\w.])(?:testWidgets|test|patrolTest|patrolWidgetTest)\s*\('))
    groups = blocks_brace_call(text, re.compile(r'(?<![\w.])group\s*\('))
    return tests, groups

def cs_blocks(text: str):
    tests = []
    for m in re.finditer(r'\[(?:Test|Fact|Theory|TestMethod|TestCase[^\]]*)\]', text):
        sig = re.compile(r'(?:void|Task)\s+(\w+)\s*\(').search(text, m.end())
        if not sig: continue
        b = text.find('{', sig.end()); e = match_close(text, b, '{', '}') if b >= 0 else -1
        if e > 0: tests.append({'name': sig.group(1), 'start': m.start(), 'end': e, 'header': text[m.start():b]})
    groups = [{'name': m.group(1), 'start': m.start(), 'end': match_close(text, m.end() - 1, '{', '}'),
               'header': text[max(0, m.start() - 300):m.end()]} for m in re.finditer(r'\bclass\s+(\w+)[^{]*\{', text)]
    return tests, [g for g in groups if g['end'] > 0]

def py_blocks(text: str):
    lines = text.split('\n')
    offs, o = [], 0
    for ln in lines:
        offs.append(o); o += len(ln) + 1
    def block(i: int, indent: int) -> int:
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
            j += 1
        return j
    tests, groups = [], []
    for i, ln in enumerate(lines):
        m = re.match(r'^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(', ln)
        c = re.match(r'^(\s*)class\s+(\w+)', ln)
        if m or c:
            mm = m or c
            indent = len(mm.group(1))
            k = i
            while k > 0 and lines[k - 1].strip().startswith('@'):
                k -= 1
            j = block(i, indent)
            entry = {'name': mm.group(2), 'start': offs[k], 'end': offs[j - 1] + len(lines[j - 1]),
                     'header': '\n'.join(lines[k:i + 1])}
            (tests if m else groups).append(entry)
    return tests, groups

def gherkin_blocks(text: str):
    lines = text.split('\n')
    offs, o = [], 0
    for ln in lines:
        offs.append(o); o += len(ln) + 1
    tests, feature = [], None
    starts = [i for i, ln in enumerate(lines) if re.match(r'^\s*(Scenario|Scenario Outline|Example)\s*:', ln)]
    for idx, i in enumerate(starts):
        k = i
        while k > 0 and lines[k - 1].strip().startswith('@'):
            k -= 1
        j = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        while j - 1 > i and lines[j - 1].strip().startswith('@'):
            j -= 1
        tests.append({'name': lines[i].split(':', 1)[1].strip(), 'start': offs[k], 'end': offs[j - 1] + len(lines[j - 1]),
                      'header': '\n'.join(lines[k:i + 1])})
    fm = re.search(r'(?m)^((?:\s*@.*\n)*)\s*Feature:.*$', text)
    if fm:
        feature = {'name': 'Feature', 'start': 0, 'end': len(text), 'header': fm.group(0)}
    return tests, [feature] if feature else []

EXTRACT = {'js': js_blocks, 'py': py_blocks, 'jvm': jvm_blocks, 'swift': swift_blocks, 'dart': dart_blocks,
           'cs': cs_blocks, 'gherkin': gherkin_blocks}

def lint_file(path: Path) -> dict:
    lang = lang_of(path)
    res = {'file': path.as_posix(), 'language': lang, 'tests': [], 'findings': []}
    try:
        raw = path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        res['findings'].append({'rule': 'unreadable', 'level': 'error', 'line': None, 'test': None, 'message': str(exc)})
        return res
    if lang is None:
        res['findings'].append({'rule': 'unsupported_file', 'level': 'warning', 'line': None, 'test': None,
                                'message': 'file type not supported by the linter; review by hand'})
        return res
    text = strip_comments(raw, lang)

    def add(rule, level, idx, test, message):
        res['findings'].append({'rule': rule, 'level': level, 'line': line_of(text, idx) if idx is not None else None,
                                'test': test, 'message': message})

    if lang == 'maestro':
        if not re.search(r'^appId:', text, re.M):
            res['findings'].append({'rule': 'unsupported_file', 'level': 'warning', 'line': None, 'test': None,
                                    'message': 'YAML file is not a Maestro flow; skipped'})
            return res
        name = path.stem
        res['tests'].append({'name': name, 'line': 1})
        tags = re.search(r'(?ms)^tags:\s*\n((?:\s+-.*\n?)+)|^tags:\s*\[(.*?)\]', text)
        if not (tags and JRN.search(tags.group(0))) and not JRN.search(path.name):
            add('missing_journey_tag', 'error', 0, name, 'Maestro flow has no JRN id in its tags')
        if not re.search(r'-\s*assert(Visible|NotVisible|True|WithAI)', text):
            add('no_assertion', 'error', 0, name, 'flow has no assertVisible/assertNotVisible/assertTrue step')
        for rx in HARD_WAITS:
            for m in rx.finditer(text):
                add('hard_wait', 'error', m.start(), None, f'hard-coded wait: {m.group(0).strip()!r}; use extendedWaitUntil')
        return res

    tests, groups = EXTRACT[lang](text)
    for t in tests:
        res['tests'].append({'name': t['name'], 'line': line_of(text, t['start'])})
        body = text[t['start']:t['end'] + 1]
        if lang == 'gherkin':
            has_assert = re.search(r'(?m)^\s*Then\b', body)
        else:
            has_assert = ASSERTIONS[lang].search(body[len(t['header']):])
        if not has_assert and not re.search(r'\.(skip|todo|fixme)\s*\(', body[:40]):
            add('no_assertion', 'error', t['start'], t['name'],
                'test has no assertion' + (' (no Then step)' if lang == 'gherkin' else
                                           '; every Expect must become an assertion on an observable outcome'))
        tagged = JRN.search(t['header']) or JRN.search(t['name'])
        if not tagged:
            for g in groups:
                if g['start'] <= t['start'] and t['end'] <= g['end'] and JRN.search(g['header']):
                    tagged = True; break
        if not tagged:
            add('missing_journey_tag', 'error', t['start'], t['name'],
                'no JRN-<DOMAIN>-<NNN> id in the test title, tags or enclosing block')
        for m in CONDITIONAL.finditer(body):
            add('conditional_test', 'warning', t['start'] + m.start(), t['name'],
                'test branches on page state; split into deterministic tests or set up the state explicitly')

    for rx in HARD_WAITS:
        for m in rx.finditer(text):
            owner = next((t['name'] for t in tests if t['start'] <= m.start() <= t['end']), None)
            add('hard_wait', 'error', m.start(), owner,
                f'hard-coded wait {m.group(0).strip()!r}; use the framework\'s auto-waiting assertion')
    for m in FOCUSED.finditer(text):
        add('focused_test', 'error', m.start(), None, f'focused test {m.group(0).strip()!r} would skip the rest of the suite')
    for rx, label in BRITTLE:
        for m in rx.finditer(text):
            owner = next((t['name'] for t in tests if t['start'] <= m.start() <= t['end']), None)
            add('brittle_selector', 'warning', m.start(), owner,
                f'{label} {m.group(0).strip()[:60]!r}; use role/label/test-id selectors or record a testability gap')
    res['findings'].sort(key=lambda f: (f['line'] or 0, f['rule']))
    return res

def lint_paths(paths: list[str]) -> dict:
    files = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files += [f for f in sorted(pp.rglob('*')) if f.is_file() and lang_of(f)]
        else:
            files.append(pp)
    results = [lint_file(f) for f in files]
    errors = sum(1 for r in results for f in r['findings'] if f['level'] == 'error')
    warnings = sum(1 for r in results for f in r['findings'] if f['level'] == 'warning')
    return {'files': results, 'errors': errors, 'warnings': warnings}

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Lint journey tests: hard waits, missing assertions, missing journey tags.')
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    res = lint_paths(args.paths)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        for r in res['files']:
            if r['findings']:
                print(f"{r['file']} ({len(r['tests'])} test(s))")
                for f in r['findings']:
                    where = f"line {f['line']}" if f['line'] else 'file'
                    print(f"  {f['level'].upper():7} {f['rule']:20} {where}: {f['message']}")
        print(f"{len(res['files'])} file(s), {sum(len(r['tests']) for r in res['files'])} test(s), "
              f"{res['errors']} error(s), {res['warnings']} warning(s)")
    return 1 if res['errors'] else 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
