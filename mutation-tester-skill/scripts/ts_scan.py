#!/usr/bin/env python3
"""A light scanner for JavaScript and TypeScript: function ranges, and tests by name (stdlib only).

    python ts_scan.py functions <file.ts>             # name, qualname, start and end line of each function
    python ts_scan.py test <test file> "<test name>"  # the lines of the test with that name

Python mutants get their function from `ast`; JS/TS has no parser in the standard library, so
this scanner blanks strings, template literals, comments and regex literals, then finds:
function declarations and expressions, arrow functions assigned to a name (const, a property,
a class field), class methods and constructors. Parameter lists may span lines and hold object
type literals; a return type annotation may hold braces. Anonymous callbacks are not functions
here: code inside `items.map(x => ...)` belongs to the named function around it. Nested named
functions are `outer.<locals>.inner`, methods `Class.method`.

Tests: Vitest/Jest ids name a test by its describe chain and title, joined with " > " (JUnit) or
with spaces (StrykerJS). `find_test` finds the it()/test() call with that name and returns its
line span, so a caller can check whether the test body calls a function directly.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

JS_SUFFIXES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts')
KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'new', 'typeof', 'await', 'with', 'do',
            'else', 'try', 'finally', 'throw', 'delete', 'void', 'yield', 'case', 'super', 'import', 'export', 'in',
            'of', 'instanceof', 'class', 'extends', 'describe', 'it', 'test', 'expect', 'beforeEach', 'afterEach',
            'beforeAll', 'afterAll'}
MODIFIERS = r'(?:(?:public|private|protected|static|async|override|readonly|abstract|declare|get|set)\s+)*'
REGEX_PREV = set('(,=:[!&|?{};+-*%<>~^') | {''}
IDENT = r'[A-Za-z_$][\w$]*'


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


def skip_ws(b: str, i: int) -> int:
    while i < len(b) and b[i].isspace():
        i += 1
    return i


def after_type(b: str, i: int, stop: str) -> int:
    """From i (just after ':' of a type annotation) scan to the first `stop` character ('{' body or
    '=' of '=>') at depth 0 that is not part of the type. Returns its index or -1."""
    depth, prev, prev2 = 0, ':', ''
    j = i
    while j < len(b) and j - i < 2000:
        c = b[j]
        if c in '([<':
            depth += 1
        elif c in ')]>' and not (c == '>' and j > 0 and b[j - 1] == '='):
            depth -= 1
        elif c == '{':
            after_arrow = prev2 + prev == '=>'      # `() => {` inside a function type
            if depth == 0 and '{' in stop and prev not in ':|&<,([' and not after_arrow:
                return j
            k = match_close(b, j)
            if k < 0:
                return -1
            j, prev, prev2 = k + 1, '}', prev
            continue
        elif c == '=' and depth == 0 and '=' in stop and j + 1 < len(b) and b[j + 1] == '>':
            return j
        elif c == ';' and depth <= 0:
            return -1
        if not c.isspace():
            prev, prev2 = c, prev
        j += 1
    return -1


def body_after_params(b: str, open_paren: int, arrow: bool) -> tuple[int, int] | None:
    """Given the '(' of a parameter list, return (start, end) offsets of the body, or None."""
    close = match_close(b, open_paren)
    if close < 0:
        return None
    j = skip_ws(b, close + 1)
    if j < len(b) and b[j] == ':':
        j = after_type(b, j + 1, '=' if arrow else '{')
        if j < 0:
            return None
    if arrow:
        if b[j:j + 2] != '=>':
            return None
        j = skip_ws(b, j + 2)
    if j >= len(b):
        return None
    if b[j] == '{':
        end = match_close(b, j)
        return (j, end) if end >= 0 else None
    if not arrow:
        return None
    # expression body: to the first ; or unmatched closer at depth 0
    depth = 0
    for k in range(j, min(len(b), j + 20000)):
        c = b[k]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                return j, k - 1
            depth -= 1
        elif c in ';,' and depth == 0:
            return j, k - 1
    return j, min(len(b), j + 20000) - 1


def line_of(starts: list[int], off: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def functions(text: str) -> list[dict]:
    """Every named function with its 1-based start and end line, outermost first."""
    b = blank(text)
    starts = [0] + [m.end() for m in re.finditer('\n', b)]
    found: dict[int, tuple[str, int, int]] = {}   # body start -> (name, header offset, body end)

    def add(name, header, span):
        if span and name and name not in KEYWORDS and span[0] not in found:
            found[span[0]] = (name, header, span[1])

    for m in re.finditer(r'\bfunction\b\s*\*?\s*(' + IDENT + r')?\s*(?:<[^(){};]*>)?\s*\(', b):
        name = m.group(1)
        if not name:
            back = re.search(r'(' + IDENT + r')\s*[:=]\s*(?:async\s+)?$', b[max(0, m.start() - 200):m.start()])
            name = back.group(1) if back else None
        add(name, m.start(), body_after_params(b, m.end() - 1, arrow=False))
    arrow_heads = [
        r'\b(?:const|let|var)\s+(' + IDENT + r')\s*(?::[^=;]*?)?=\s*(?:async\s*)?(?:<[^(){};=]*>\s*)?',   # const f = (..) =>
        r'(?:^|[,{]|\n)\s*' + MODIFIERS + r'(' + IDENT + r')\s*([:=])\s*(?:async\s*)?(?:<[^(){};=]*>\s*)?',  # prop: (..) =>, field = (..) =>
    ]
    for rx in arrow_heads:
        for m in re.finditer(rx + r'(\(|' + IDENT + r'\s*=>)', b, re.M):
            name = m.group(1)
            # `name: (..) => T` is also how a type literal declares a method; a property only counts
            # with a block body
            block_only = m.lastindex == 3 and m.group(2) == ':'
            head = m.group(m.lastindex)
            if head == '(':
                span = body_after_params(b, m.end() - 1, arrow=True)
                if span and block_only and b[span[0]] != '{':
                    span = None
            else:
                j = skip_ws(b, b.index('=>', m.start(m.lastindex)) + 2)
                if j < len(b) and b[j] == '{':
                    end = match_close(b, j)
                    span = (j, end) if end >= 0 else None
                elif block_only:
                    span = None
                else:
                    k = j
                    depth = 0
                    while k < len(b):
                        c = b[k]
                        if c in '([{':
                            depth += 1
                        elif c in ')]}':
                            if depth == 0:
                                break
                            depth -= 1
                        elif c in ';,' and depth == 0:
                            break
                        k += 1
                    span = (j, k - 1)
            add(name, m.start(1), span)
    for m in re.finditer(r'^[ \t]*' + MODIFIERS + r'\*?\s*(' + IDENT + r')\s*(?:<[^(){};=]*>)?\s*\(', b, re.M):
        if m.group(1) in KEYWORDS:
            continue
        add(m.group(1), m.start(1), body_after_params(b, m.end() - 1, arrow=False))
    classes = []
    for m in re.finditer(r'\bclass\s+(' + IDENT + r')[^{;]*\{', b):
        end = match_close(b, m.end() - 1)
        if end >= 0:
            classes.append((m.end() - 1, end, m.group(1)))
    items = sorted(((s, e, name, h) for s, (name, h, e) in found.items()), key=lambda x: (x[0], -x[1]))
    out = []
    for s, e, name, h in items:
        parents = [o for o in out if o['_s'] < s and e <= o['_e']]
        cls = [c for c in classes if c[0] < s and e <= c[1] and not any(c[0] < p['_s'] for p in parents)]
        if parents:
            qual = parents[-1]['qualname'] + '.<locals>.' + name
        elif cls:
            qual = cls[-1][2] + '.' + name
        else:
            qual = name
        out.append({'name': name, 'qualname': qual, 'start': line_of(starts, h), 'end': line_of(starts, e),
                    '_s': s, '_e': e})
    for o in out:
        del o['_s'], o['_e']
    return out


def enclosing(funcs: list[dict], line: int) -> str | None:
    """The innermost function holding the line, by qualname."""
    best = None
    for f in funcs:
        if f['start'] <= line <= f['end'] and (best is None or f['end'] - f['start'] <= best['end'] - best['start']):
            best = f
    return best['qualname'] if best else None


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
    if len(argv) >= 2 and argv[0] == 'functions':
        print(json.dumps(functions(Path(argv[1]).read_text(encoding='utf-8')), indent=1))
        return 0
    if len(argv) >= 3 and argv[0] == 'test':
        print(json.dumps(find_test(Path(argv[1]).read_text(encoding='utf-8'), argv[2]), indent=1))
        return 0
    print(__doc__.splitlines()[2].strip())
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
