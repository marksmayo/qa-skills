#!/usr/bin/env python3
"""Find JavaScript/TypeScript tests by name, and whether a test calls a function (stdlib only).

    python ts_scan.py test <test file> "<test name>"  # the lines of the test with that name

Function ranges for mutants come from the shared diff_scope.functions(); this module only reads
tests. It blanks strings, template literals, comments and regex literals so brackets can be
counted and calls inside strings are ignored.

Tests: Vitest/Jest ids name a test by its describe chain and title, joined with " > " (JUnit) or
with spaces (StrykerJS). `find_test` finds the it()/test() call with that name and returns its
line span, so a caller can check whether the test body calls a function directly.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

JS_SUFFIXES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts')
REGEX_PREV = set('(,=:[!&|?{};+-*%<>~^') | {''}


def is_js(path: str) -> bool:
    return path.endswith(JS_SUFFIXES)


def blank(text: str) -> str:
    """Same length and newlines as text, with the insides of strings, templates, comments and
    regex literals replaced by spaces, so braces and parens can be counted."""
    out = list(text)
    i, n = 0, len(text)
    prev = ''

    def wipe(a, b):
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '

    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            wipe(i, j)
            i = j
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            wipe(i, j)
            i = j
            continue
        if c in '\'"`':
            j, depth = i + 1, 0
            while j < n:
                d = text[j]
                if d == '\\':
                    j += 2
                    continue
                if c == '`' and d == '$' and j + 1 < n and text[j + 1] == '{':
                    depth += 1
                    j += 2
                    continue
                if c == '`' and depth and d == '}':
                    depth -= 1
                elif d == c and not depth:
                    break
                elif d == '\n' and c != '`':
                    break
                j += 1
            wipe(i + 1, min(j, n))
            i = j + 1
            prev = c
            continue
        if c == '/' and (prev in REGEX_PREV or re.search(r'\b(?:return|typeof|case)\s*$', text[max(0, i - 12):i])):
            j, in_class = i + 1, False
            while j < n and text[j] != '\n':
                d = text[j]
                if d == '\\':
                    j += 2
                    continue
                if d == '[':
                    in_class = True
                elif d == ']':
                    in_class = False
                elif d == '/' and not in_class:
                    break
                j += 1
            if j < n and text[j] == '/':
                wipe(i + 1, j)
                i = j + 1
                prev = '/'
                continue
        if not c.isspace():
            prev = c
        i += 1
    return ''.join(out)


def match_close(b: str, i: int) -> int:
    """Index of the bracket closing the one at b[i] (any of ( [ {), or -1."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    stack = [pairs[b[i]]]
    for j in range(i + 1, len(b)):
        c = b[j]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ')]}':
            if not stack or c != stack[-1]:
                return -1
            stack.pop()
            if not stack:
                return j
    return -1


def line_of(starts: list[int], off: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


TEST_CALL = re.compile(r'^(\s*)(it|test)(?:\.(?:only|skip|concurrent|sequential|fails|todo|each\([^)]*\)))*\s*\(\s*([\'"`])((?:(?!\3).)*)\3')
DESCRIBE = re.compile(r'^(\s*)describe(?:\.(?:only|skip|concurrent|sequential|shuffle|todo))*\s*\(\s*([\'"`])((?:(?!\2).)*)\2')


def tests(text: str) -> list[dict]:
    """Every it()/test() with its describe chain and line span."""
    lines = text.splitlines()
    b = blank(text)
    starts = [0] + [m.end() for m in re.finditer('\n', b)]
    out = []
    for i, line in enumerate(lines):
        m = TEST_CALL.match(line)
        if not m:
            continue
        chain, indent = [m.group(4)], len(m.group(1))
        for k in range(i - 1, -1, -1):
            d = DESCRIBE.match(lines[k])
            if d and len(d.group(1)) < indent:
                chain.insert(0, d.group(3))
                indent = len(d.group(1))
                if indent == 0:
                    break
        open_paren = starts[i] + line.index('(', len(m.group(1)))
        close = match_close(b, open_paren)
        end = line_of(starts, close) if close >= 0 else i + 1
        out.append({'chain': chain, 'start': i + 1, 'end': end})
    return out


def find_test(text: str, name: str) -> dict | None:
    """The test whose describe chain and title make `name` (" > " or space joined), else the one
    whose title ends the name, if exactly one does."""
    ts = tests(text)
    for t in ts:
        if name in (' > '.join(t['chain']), ' '.join(t['chain'])):
            return t
    tail = [t for t in ts if name.endswith(' > ' + t['chain'][-1]) or name.endswith(' ' + t['chain'][-1])
            or name == t['chain'][-1]]
    return tail[0] if len(tail) == 1 else None


def test_at(text: str, line: int) -> dict | None:
    """The test that starts on (or holds) a line: StrykerJS gives each test's start location."""
    hold = [t for t in tests(text) if t['start'] <= line <= t['end']]
    return min(hold, key=lambda t: t['end'] - t['start']) if hold else None


def calls(text: str, start: int, end: int, name: str) -> bool:
    """Does lines start..end call `name(...)` (directly or as a method) outside strings and comments?"""
    seg = '\n'.join(blank(text).splitlines()[start - 1:end])
    return re.search(r'(?<![\w$])' + re.escape(name) + r'\s*(?:<[^()<>;]*>)?\s*\(', seg) is not None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 3 and argv[0] == 'test':
        print(json.dumps(find_test(Path(argv[1]).read_text(encoding='utf-8'), argv[2]), indent=1))
        return 0
    print(__doc__.splitlines()[2].strip())
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
