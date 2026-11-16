#!/usr/bin/env python3
"""Blank string literals, docstrings and comments in source text, keeping every position (stdlib only).

    python code_mask.py <file> [--lang python|javascript|c] [--docstrings-only]   # print the masked text
    python code_mask.py --self-test

Line scanners that look for calls, imports or sinks with regular expressions also match text that only
describes code: a pattern table (`RULES = [r"subprocess\\.run\\("]`), a fixture source held in a string by a
self-test, a docstring that shows usage, a commented-out line. mask() returns the text with the CONTENT of
every string literal and comment replaced by spaces, so such a regex no longer matches there, while every
character keeps its offset, line and column (newlines are kept; the result has the same length).

    mask(text, lang)                      -> str   same length; strings, docstrings and comments blanked
    mask_python_docstrings_only(text)     -> str   only statement-level string expressions (docstrings) blanked
    masked_spans(text, lang)              -> [(start, end, kind)]   the blanked ranges (kind: string, docstring,
                                             fstring, template, regex, comment), sorted, end exclusive
    in_masked(spans, pos)                 -> bool  pos lies inside a blanked range
    code_finditer(rx, text, lang)         -> matches of rx on the ORIGINAL text whose start is code (not inside a
                                             string or comment), so a match may still read a string it starts
                                             before: `from 'vitest'` in JavaScript counts, the same words inside a
                                             Python string do not
    lang_of(path)                         -> 'python' | 'javascript' | 'c' | None   by file extension

Languages:
  * python: the tokenize module. String delimiters (prefix and quotes) are kept, their content blanked.
    f-strings are blanked whole, expressions included, and like any string their range (kind 'fstring') starts
    after the opening quote and ends before the closing one (on every Python version, so results do not depend on
    the interpreter: Python 3.12+ tokenizes f-string expressions, older versions do not). A file tokenize
    cannot read (a syntax error, Python 2) falls back to a small hand lexer with the same rules.
  * javascript (also TypeScript, JSX): '...' and "..." strings, template literals (blanked whole, ${...}
    expressions included), regex literals (best effort: a / where an expression may start), // and /* */
    comments.
  * c: a generic C-like fallback for Go, Rust, Java, Kotlin, C#, C/C++, Swift, Dart, Scala, PHP: "...",
    triple-quoted strings, character literals ('x', '\\n'; a Rust lifetime 'a stays code), Go backtick raw
    strings, // and /* */ comments.
Blanking replaces each character with a space except \\n and \\r. Masking is lexical, never a parse: odd
inputs (unterminated strings) are blanked to the end of their line, never an exception.
"""
from __future__ import annotations
import bisect, io, re, sys, tokenize
from pathlib import Path

PY_EXT = {'.py', '.pyi', '.pyw'}
JS_EXT = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts'}
C_EXT = {'.go', '.rs', '.java', '.kt', '.kts', '.cs', '.c', '.h', '.cc', '.cpp', '.hpp', '.cxx', '.m', '.mm',
         '.swift', '.dart', '.scala', '.php', '.groovy', '.gradle'}


def lang_of(path) -> str | None:
    name = str(path).replace('\\', '/').rsplit('/', 1)[-1].lstrip('.')   # as Path.suffix: leading dots never count
    dot = name.rfind('.')
    suf = name[dot:].lower() if 0 < dot < len(name) - 1 else ''
    return 'python' if suf in PY_EXT else 'javascript' if suf in JS_EXT else 'c' if suf in C_EXT else None


def _blank(text: str, spans) -> str:
    if not spans:
        return text
    chars = list(text)
    for s, e, _ in spans:
        for i in range(s, e):
            if chars[i] not in '\r\n':
                chars[i] = ' '
    return ''.join(chars)


def _line_offsets(text: str) -> list[int]:
    offs, total = [0], 0
    for line in io.StringIO(text, newline='').readlines():   # keeps \r\n and lone \r, as tokenize sees them
        total += len(line)
        offs.append(total)
    return offs


PY_STR_START = re.compile(r'(?i)(rb|br|fr|rf|r|b|u|f)?("""|\'\'\'|"|\')')


def _py_string_inner(tok: str) -> tuple[int, int]:
    """(start, end) of the content of one Python string token, relative to the token."""
    m = PY_STR_START.match(tok)
    if not m:
        return 0, len(tok)
    q = m.group(2)
    end = len(tok) - len(q) if tok.endswith(q) and len(tok) >= m.end() + len(q) else len(tok)
    return m.end(), max(end, m.end())


def _python_tokens(text: str):
    """(kind, start, end, prev_significant, token) from tokenize, f-strings folded into one 'fstring' token."""
    offs = _line_offsets(text)
    at = lambda rc: offs[rc[0] - 1] + rc[1]   # noqa: E731
    fstart = getattr(tokenize, 'FSTRING_START', None)
    fend = getattr(tokenize, 'FSTRING_END', None)
    tstart = getattr(tokenize, 'TSTRING_START', None)
    tend = getattr(tokenize, 'TSTRING_END', None)
    out, depth, f0 = [], 0, None
    for tok in tokenize.generate_tokens(io.StringIO(text, newline='').readline):
        if tok.type in (fstart, tstart) and tok.type is not None:
            if depth == 0:
                f0 = at(tok.start)
            depth += 1
            continue
        if tok.type in (fend, tend) and tok.type is not None:
            depth -= 1
            if depth == 0:
                out.append(('fstring', f0, at(tok.end), text[f0:at(tok.end)]))
            continue
        if depth:
            continue
        if tok.type == tokenize.STRING:   # before Python 3.12 an f-string is one STRING token
            pre = PY_STR_START.match(tok.string)
            kind = 'fstring' if pre and 'f' in (pre.group(1) or '').lower() else 'string'
            out.append((kind, at(tok.start), at(tok.end), tok.string))
        elif tok.type == tokenize.COMMENT:
            out.append(('comment', at(tok.start), at(tok.end), tok.string))
        elif tok.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
            out.append(('break', at(tok.start), at(tok.end), tok.string))
        elif tok.type != tokenize.NL:
            out.append(('code', at(tok.start), at(tok.end), tok.string))
    return out


def _python_fallback(text: str) -> list[tuple]:
    """Hand lexer for text tokenize refuses: '#' comments and (triple-)quoted strings with prefixes."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '#':
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(('comment', i, j, text[i:j]))
            i = j
            continue
        m = PY_STR_START.match(text, i)
        if m and (m.start() == 0 or not (text[i - 1].isalnum() or text[i - 1] == '_')):
            q, j, raw = m.group(2), m.end(), 'r' in (m.group(1) or '').lower()
            while j < n:
                if text[j] == '\\' and not raw:
                    j += 2
                    continue
                if text.startswith(q, j):
                    j += len(q)
                    break
                if len(q) == 1 and text[j] == '\n':
                    break
                j += 1
            j = min(j, n)
            kind = 'fstring' if 'f' in (m.group(1) or '').lower() else 'string'
            out.append((kind, i, j, text[i:j]))
            i = j
            continue
        if c == '\n':
            out.append(('break', i, i + 1, c))
        elif not c.isspace():
            out.append(('code', i, i + 1, c))
        i += 1
    return out


def _python_items(text: str) -> list[tuple]:
    try:
        return _python_tokens(text)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return _python_fallback(text)


# one token per match: a run of plain code (names, numbers, operators and brackets, with any blanks before it; it
# holds at least one non-blank character), blanks alone, a quote, a comment, a newline, ';' or a backslash
# continuation. Brackets are counted inside a run, so a typical line is one or two tokens plus its strings.
_PY_LEX = re.compile(r'(?P<code>[ \t\f]*[^"\'#\r\n;\\ \t\f][^"\'#\r\n;\\]*)|(?P<ws>[ \t\f]+)|(?P<q>["\'])'
                     r'|(?P<com>#[^\r\n]*)|(?P<nl>\r\n|\n|\r)|(?P<semi>;)|(?P<cont>\\(?:\r\n|\n|\r))')
_PY_PREFIX_TAIL = re.compile(r'(?<![\w])[A-Za-z]{1,2}$')
_PY_BAD_CODE = re.compile(r'[$?`]')
_PY_NAME = re.compile(r'[^\W\d]\w*')
_PY_PREFIXES = {'r', 'u', 'b', 'br', 'rb', 'f', 'fr', 'rf'}
_PY_T_PREFIXES = {'t', 'tr', 'rt'}
_PY_BODY = {
    '"': re.compile(r'(?:[^"\\\r\n]|\\(?:\r\n|[\s\S]))*"'),
    "'": re.compile(r"(?:[^'\\\r\n]|\\(?:\r\n|[\s\S]))*'"),
    '"""': re.compile(r'(?:[^"\\]|\\[\s\S]|"(?!""))*"""'),
    "'''": re.compile(r"(?:[^'\\]|\\[\s\S]|'(?!''))*'''"),
}


def _py_string_end(text: str, i: int, f: bool) -> int:
    """Index just after the string whose opening quote is at i (-1 if it never closes)."""
    q = text[i] * 3 if text.startswith(text[i] * 3, i) else text[i]
    if f:
        return _py_fstring_end(text, i + len(q), q)
    m = _PY_BODY[q].match(text, i + len(q))
    return m.end() if m else -1


def _py_fstring_end(text: str, i: int, q: str) -> int:
    n, single = len(text), len(q) == 1
    while i < n:
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if text.startswith(q, i):
            return i + len(q)
        if single and c in '\r\n':
            return -1
        if c == '{':
            if text.startswith('{{', i):
                i += 2
                continue
            i = _py_fexpr_end(text, i + 1, q)
            if i < 0:
                return -1
            continue
        i += 1
    return -1


def _py_fexpr_end(text: str, i: int, q: str) -> int:
    """Index just after the } closing an f-string replacement field whose expression starts at i (Python 3.12
    rules: nested strings may reuse the outer quote; a top-level ':' starts the format spec)."""
    n, depth = len(text), 0
    while i < n:
        c = text[i]
        m = _PY_NAME.match(text, i) if c.isalpha() or c == '_' else None
        if m:
            j = m.end()
            word = m.group().lower()
            if j < n and text[j] in '\'"' and word in _PY_PREFIXES:
                i = _py_string_end(text, j, 'f' in word)
                if i < 0:
                    return -1
            else:
                i = j
            continue
        if c in '\'"':
            i = _py_string_end(text, i, False)
            if i < 0:
                return -1
            continue
        if c == '\\':
            i += 2
            continue
        if c in '([{':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif c == '}':
            if depth == 0:
                return i + 1
            depth -= 1
        elif c == ':' and depth == 0:
            return _py_fspec_end(text, i + 1, q)
        i += 1
    return -1


def _py_fspec_end(text: str, i: int, q: str) -> int:
    n = len(text)
    while i < n:
        c = text[i]
        if c == '{':
            i = _py_fexpr_end(text, i + 1, q)
            if i < 0:
                return -1
            continue
        if c == '}':
            return i + 1
        if text.startswith(q, i):
            return -1
        if c == '\\':
            i += 2
            continue
        i += 1
    return -1


def _python_fast_spans(text: str, docstrings_only: bool = False):
    """The ranges _python_spans_tokenize gives, from one regex-driven pass (several times faster than tokenize).
    None when the text is anything but plain valid lexing (an unterminated string, unbalanced brackets, a t-string,
    a character it does not class), so the caller falls back to tokenize and its hand lexer."""
    n, i, depth, line_start = len(text), 0, 0, True
    strings: list[list] = []   # [start, end, kind, prev_is_break, next_is_break]
    spans: list[tuple[int, int, str]] = []
    pending = None   # the last string, until the next significant token says whether a break follows it

    match = _PY_LEX.match
    while i < n:
        m = match(text, i)
        if m is None:
            return None
        g, j = m.lastgroup, m.end()
        prefix = ''
        if g == 'code':
            run = m.group()
            if not run.isascii() or _PY_BAD_CODE.search(run):
                return None   # characters tokenize refuses outside strings: let it (and its hand lexer) decide
            start = j
            if j < n and text[j] in '\'"':
                t = _PY_PREFIX_TAIL.search(run)
                if t is not None:
                    word = t.group().lower()
                    if word in _PY_T_PREFIXES:
                        return None
                    if word in _PY_PREFIXES:
                        prefix, start, run = word, j - len(word), run[:t.start()]
            if run.strip(' \t\f'):
                if pending is not None:
                    pending[4] = False
                    pending = None
                line_start = False
                depth += run.count('(') + run.count('[') + run.count('{') \
                    - run.count(')') - run.count(']') - run.count('}')
                if depth < 0:
                    return None
            if not prefix:
                i = j
                continue
            i = start
        elif g == 'ws' or g == 'cont':
            i = j
            continue
        elif g == 'com':
            if not docstrings_only:
                spans.append((i, j, 'comment'))
            i = j
            continue
        elif g == 'nl':
            if depth == 0:
                if pending is not None:
                    pending[4] = True
                    pending = None
                line_start = True
            i = j
            continue
        elif g == 'semi':
            if pending is not None:
                pending[4] = True
                pending = None
            line_start = False
            i = j
            continue
        e = _py_string_end(text, i + len(prefix), 'f' in prefix)
        if e < 0:
            return None
        if pending is not None:
            pending[4] = False
        pending = [i, e, 'fstring' if 'f' in prefix else 'string', line_start and depth == 0, False]
        strings.append(pending)
        line_start = False
        i = e
    if depth:
        return None
    if pending is not None:
        pending[4] = True
    for s, e, kind, prev_break, next_break in strings:
        statement = prev_break and next_break
        a, b = _py_string_inner(text[s:e])
        if docstrings_only:
            if kind == 'string' and statement:
                spans.append((s + a, s + b, 'docstring'))
        elif kind == 'fstring':
            spans.append((s + a, s + b, 'fstring'))
        else:
            spans.append((s + a, s + b, 'docstring' if statement else 'string'))
    return _merge(spans)


def _python_spans(text: str, docstrings_only: bool = False) -> list[tuple[int, int, str]]:
    fast = _python_fast_spans(text, docstrings_only)
    return fast if fast is not None else _python_spans_tokenize(text, docstrings_only)


def _python_spans_tokenize(text: str, docstrings_only: bool = False) -> list[tuple[int, int, str]]:
    items = _python_items(text)
    spans = []
    sig = [k for k, x in enumerate(items) if x[0] != 'comment']   # indices of non-comment items, in order
    where = {k: p for p, k in enumerate(sig)}
    for k, (kind, s, e, tok) in enumerate(items):
        if kind not in ('string', 'fstring', 'comment'):
            continue
        statement = False
        if kind != 'comment':
            p = where[k]
            prev = items[sig[p - 1]][0] if p else 'break'
            nxt = items[sig[p + 1]] if p + 1 < len(sig) else ('break', 0, 0, '')
            statement = prev == 'break' and (nxt[0] == 'break' or (nxt[0] == 'code' and nxt[3] == ';'))
        if docstrings_only:
            if kind == 'string' and statement:
                a, b = _py_string_inner(tok)
                spans.append((s + a, s + b, 'docstring'))
            continue
        if kind == 'comment':
            spans.append((s, e, 'comment'))
        elif kind == 'fstring':
            a, b = _py_string_inner(tok)
            spans.append((s + a, s + b, 'fstring'))   # the whole content, expressions included; prefix and quotes stay
        else:
            a, b = _py_string_inner(tok)
            spans.append((s + a, s + b, 'docstring' if statement else 'string'))
    return _merge(spans)


def _merge(spans):
    spans = sorted(x for x in spans if x[1] > x[0])
    out = []
    for s, e, k in spans:
        if out and s < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e), out[-1][2])
        else:
            out.append((s, e, k))
    return out


JS_KEYWORDS_BEFORE_EXPR = {'return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw', 'case',
                           'do', 'else', 'yield', 'await'}
C_CHAR = re.compile(r"'(?:\\(?:u\{[0-9A-Fa-f]{1,6}\}|x[0-9A-Fa-f]{2}|[0-7]{1,3}|.)|[^\\'\n])'")


def _clike_spans(text: str, js: bool) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    n = len(text)

    def string_end(i: int, q: str) -> int:
        j = i + 1
        while j < n:
            c = text[j]
            if c == '\\':
                j += 2
                continue
            if c == q:
                return j
            if c == '\n':
                return j   # unterminated: to the end of the line
            j += 1
        return n

    def template_end(i: int) -> int:
        """index of the closing backtick of the template literal opened at i (n if none)."""
        j = i + 1
        while j < n:
            c = text[j]
            if c == '\\':
                j += 2
                continue
            if c == '`':
                return j
            if c == '$' and j + 1 < n and text[j + 1] == '{':
                j = code_until_brace(j + 2)
                continue
            j += 1
        return n

    def code_until_brace(i: int) -> int:
        """index just after the } that closes a ${ expression starting at i."""
        depth, j = 1, i
        while j < n:
            c = text[j]
            if c in '\'"':
                j = string_end(j, c) + 1
                continue
            if c == '`':
                j = template_end(j) + 1
                continue
            if c == '/' and j + 1 < n and text[j + 1] == '*':
                k = text.find('*/', j + 2)
                j = n if k < 0 else k + 2
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n

    def regex_allowed(i: int) -> bool:
        j = i - 1
        while j >= 0 and text[j] in ' \t\r\n':
            j -= 1
        if j < 0:
            return True
        c = text[j]
        if c in ')]}"\'`':
            return False
        if c.isalnum() or c in '_$':
            k = j
            while k >= 0 and (text[k].isalnum() or text[k] in '_$'):
                k -= 1
            return text[k + 1:j + 1] in JS_KEYWORDS_BEFORE_EXPR
        return True

    i = 0
    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            spans.append((i, j, 'comment'))
            i = j
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            spans.append((i, j, 'comment'))
            i = j
            continue
        if not js and c == '"' and text.startswith('"""', i):   # Kotlin, Swift, Scala, Java text blocks
            j = text.find('"""', i + 3)
            j = n if j < 0 else j
            spans.append((i + 3, j, 'string'))
            i = min(n, j + 3)
            continue
        if c == '"' or (js and c == "'"):
            j = string_end(i, c)
            spans.append((i + 1, j, 'string'))
            i = j + 1
            continue
        if not js and c == "'":
            m = C_CHAR.match(text, i)
            if m:
                spans.append((i + 1, m.end() - 1, 'string'))
                i = m.end()
                continue
            i += 1   # a Rust lifetime or label: code
            continue
        if c == '`':
            if js:
                j = template_end(i)
                spans.append((i + 1, j, 'template'))
            else:   # Go raw string: no escapes
                j = text.find('`', i + 1)
                j = n if j < 0 else j
                spans.append((i + 1, j, 'string'))
            i = j + 1
            continue
        if js and c == '/' and regex_allowed(i):
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
                spans.append((i + 1, j, 'regex'))
                i = j + 1
                continue
        i += 1
    return _merge(spans)


_PY_NEXT = re.compile(r'["\'#]')
_PY_COMMENT = re.compile(r'#[^\r\n]*')


def _python_ranges(text: str):
    """The ranges _python_spans gives, without telling docstrings from other strings (every string is 'string' or
    'fstring'): one regex search from each string or comment to the next, so lines of plain code cost nothing. None
    for input the full lexer must judge (an unterminated string, a t-string)."""
    spans, i, search = [], 0, _PY_NEXT.search
    while True:
        m = search(text, i)
        if m is None:
            return _merge(spans)
        k = m.start()
        if text[k] == '#':
            e = _PY_COMMENT.match(text, k).end()
            spans.append((k, e, 'comment'))
            i = e
            continue
        j = k
        while j > 0 and (text[j - 1].isalnum() or text[j - 1] == '_'):
            j -= 1
        word = text[j:k].lower()
        if word in _PY_T_PREFIXES:
            return None
        prefix = word if word in _PY_PREFIXES else ''
        e = _py_string_end(text, k, 'f' in prefix)
        if e < 0:
            return None
        q = 3 if text.startswith(text[k] * 3, k) else 1   # the content lies between the quotes (as _py_string_inner)
        spans.append((k + q, e - q, 'fstring' if 'f' in prefix else 'string'))
        i = e


def masked_spans(text: str, lang: str | None, kinds: bool = True) -> list[tuple[int, int, str]]:
    """The ranges mask() blanks, sorted: [(start, end, kind)], end exclusive. Unknown lang: none. kinds=False is for
    callers that only ask whether a position is masked: the same ranges, found faster, with Python docstrings
    labelled 'string'."""
    if lang == 'python':
        if not kinds:
            fast = _python_ranges(text)
            if fast is not None:
                return fast
        return _python_spans(text)
    if lang in ('javascript', 'typescript', 'js', 'ts'):
        return _clike_spans(text, js=True)
    if lang == 'c':
        return _clike_spans(text, js=False)
    return []


def mask(text: str, lang: str | None) -> str:
    """text with the content of string literals, docstrings and comments replaced by spaces (\\n, \\r kept), so
    every offset, line and column is unchanged. lang: python | javascript | c (see lang_of); anything else returns
    text unchanged."""
    return _blank(text, masked_spans(text, lang))


def mask_python_docstrings_only(text: str) -> str:
    """Python text with only statement-level string expressions (module, class and function docstrings, and other
    bare string statements) blanked; other strings and comments are kept."""
    return _blank(text, _python_spans(text, docstrings_only=True))


def in_masked(spans, pos: int) -> bool:
    i = bisect.bisect_right(spans, (pos, float('inf'), '')) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def code_finditer(rx, text: str, lang: str | None, spans=None):
    """Matches of rx (a pattern or compiled regex) on text whose start is not inside a string or comment."""
    rx = re.compile(rx) if isinstance(rx, str) else rx
    spans = masked_spans(text, lang) if spans is None else spans
    for m in rx.finditer(text):
        if not in_masked(spans, m.start()):
            yield m


def code_search(rx, text: str, lang: str | None, spans=None):
    return next(code_finditer(rx, text, lang, spans), None)


# ------------------------------------------------------------------------------------------------ self-test
def _pos(text: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """(line, column) of the nth occurrence of needle, 1-based line."""
    i = -1
    for _ in range(nth + 1):
        i = text.index(needle, i + 1)
    return text.count('\n', 0, i) + 1, i - (text.rfind('\n', 0, i) + 1)


def self_test() -> int:
    checks = []
    py = ('"""Scan for calls such as requests.get(url) and os.system(cmd)."""\n'
          'import os, re, requests\n'
          'RULES = [r"requests\\.get\\(", "os.system(", \'eval(\']   # requests.get( in a comment\n'
          'FIXTURE = """\nimport requests\nrequests.get("http://example.test/")\n"""\n'
          'name = f"{requests.get(1)} and requests.get("\n'
          'def fetch(url):\n'
          '    \'\'\'Fetch it with requests.get(url).\'\'\'\n'
          '    return requests.get(url)   # the real call\n')
    m = mask(py, 'python')
    call = re.compile(r'requests\.get\(')
    checks.append(('masking keeps the length and every newline', len(m) == len(py)
                   and [i for i, c in enumerate(m) if c == '\n'] == [i for i, c in enumerate(py) if c == '\n'], len(m)))
    hits = [(m.count('\n', 0, x.start()) + 1) for x in call.finditer(m)]
    checks.append(('a pattern table, a fixture string, a docstring, an f-string and comments no longer match a code '
                   'regex while the real call still does, on its own line', hits == [py.count('\n', 0, py.index('return requests')) + 1]
                   and len(call.findall(py)) == 7, hits))
    ln, col = _pos(py, 'return requests')
    checks.append(('the real call keeps its line and column', _pos(m, 'return requests') == (ln, col), (ln, col)))
    fs = 'a = f"https://x.test/{q}"\nb = "https://x.test/"\nc = rf\'\'\'{q}\'\'\'\n'
    got = [(fs[s:e], k) for s, e, k in masked_spans(fs, 'python')]
    fb = 'a = f"https://x.test/{q}"\nb = "https://x.test/"\nif a:\n    b = 1\n  c = 2\n'   # bad dedent: the fallback lexer
    got_fb = [(fb[s:e], k) for s, e, k in masked_spans(fb, 'python')]
    checks.append(('an f-string\'s range starts after its opening quote and ends before its closing quote, like a '
                   'plain string\'s (prefix and quotes stay code), with tokenize and with the fallback lexer',
                   got == [('https://x.test/{q}', 'fstring'), ('https://x.test/', 'string'), ('{q}', 'fstring')]
                   and got_fb[:2] == [('https://x.test/{q}', 'fstring'), ('https://x.test/', 'string')], (got, got_fb)))
    tricky = [
        '"""Module doc."""\nx = 1\n',
        '# lead\n"""doc after a comment"""\n',
        'def f():\n    """doc"""\n    return 1\n',
        'def f(): "same-line string is not a docstring"\n',
        'x = (\n    "inside brackets"\n)\n',
        '"a" "b"\n',
        '"a"; y = 2\n',
        'x = 1 + \\\n"continued"\n',
        '"s" \\\n + x\n',
        'if"a": pass\n',
        'v = f"{x["k"]} and {y!r:>{w}} {{literal}}"\n',
        'v = f"""multi\n{a + "b"}\nline"""\n',
        'w = rb"\\d+" + Br\'raw\' + u"u"\n',
        's = "esc \\" quote" # trailing comment\n',
        "t = '''it's \"\"\" inside'''\n",
        'a = 1\r\n"crlf doc"\r\nb = "x"  # c\r\n',
        'class C:\n    x = [\n        "one",\n    ]\n    "class-level string"\n',
        'z = {"k": f\'{d["k"]:{fmt}}\'}\n',
        'last = "no newline at end"',
        '"only"',
    ]
    diffs = [(t, _python_fast_spans(t), _python_spans_tokenize(t)) for t in tricky]
    diffs = [d for d in diffs if d[1] != d[2]]
    checks.append(('the single-pass Python lexer gives exactly the ranges tokenize gives (docstrings after comments, '
                   'strings in brackets, continuations, nested f-string quotes and format specs, prefixes, CRLF)',
                   not diffs, diffs[:2]))
    checks.append(('the single-pass lexer hands odd input (an unterminated string) back to tokenize',
                   _python_fast_spans('x = "open\ny = 1\n') is None, None))
    rdiff = [t for t in tricky + [py, fs] if [(s, e) for s, e, _ in masked_spans(t, 'python', kinds=False)]
             != [(s, e) for s, e, _ in masked_spans(t, 'python')]]
    checks.append(('kinds=False (ranges only, for callers that only ask whether a position is masked) blanks exactly '
                   'the ranges the full lexer does', not rdiff, rdiff[:2]))
    checks.append(('string delimiters stay, so the code still shows where a string was',
                   m.splitlines()[2].startswith('RULES = [r"') and '"' in m.splitlines()[2], m.splitlines()[2]))
    doc = mask_python_docstrings_only(py)
    checks.append(('docstrings-only blanks the module and function docstrings but keeps other strings',
                   'os.system(cmd)' not in doc and 'Fetch it' not in doc and 'os.system(' in doc
                   and 'http://example.test/' in doc and len(doc) == len(py), doc[:120]))
    broken = 'print "py2 style"\nx = "requests.get(1)"\nrequests.get(2)  # requests.get(3)\n'
    mb = mask(broken, 'python')
    checks.append(('a file tokenize cannot read is masked by the fallback lexer', len(call.findall(mb)) == 1
                   and len(mb) == len(broken), mb))
    js = ("import { test } from 'vitest';\n"
          "const RULES = ['fetch(', \"eval(\"]; // fetch( in a comment\n"
          "const src = `import(${JSON.stringify(path)})` + `fetch(${u})`;\n"
          "const rx = /fetch\\(/g; const half = total / 2 / 3;\n"
          "/* fetch(\n   in a block comment */\n"
          "export async function load(u) { return fetch(u); }\n")
    mj = mask(js, 'javascript')
    fetch = re.compile(r'\bfetch\(')
    checks.append(('JavaScript strings, template literals, regex literals and comments are blanked; the real call stays',
                   [mj.count('\n', 0, x.start()) + 1 for x in fetch.finditer(mj)] == [7] and 'import(' not in mj
                   and len(mj) == len(js), [mj.count('\n', 0, x.start()) + 1 for x in fetch.finditer(mj)]))
    checks.append(('division is not read as a regex literal', 'total / 2 / 3' in mj, mj.splitlines()[3]))
    vit = re.compile(r"from\s+['\"]vitest['\"]")
    pyfix = 'SRC = "import { test } from \'vitest\'\\n"\n'
    checks.append(('code_finditer keeps a JavaScript import specifier (its match starts in code) but not the same '
                   'words inside a Python string', code_search(vit, js, 'javascript') is not None
                   and code_search(vit, pyfix, 'python') is None, None))
    go = ('package main\n// exec.Command( in a comment\nvar fixture = `exec.Command("rm")`\n'
          'func f<\'a>() { _ = \'x\'; exec.Command("ls") }\n')
    mg = mask(go, 'c')
    checks.append(('C-like fallback: Go raw strings and comments blanked, a lifetime kept, the real call found',
                   len(re.findall(r'exec\.Command\(', mg)) == 1 and "<'a>" in mg and len(mg) == len(go), mg))
    checks.append(('lang_of maps extensions', (lang_of('a/b.py'), lang_of('x.tsx'), lang_of('m.go'), lang_of('r.md'))
                   == ('python', 'javascript', 'c', None), None))
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {name}" + ('' if ok else f'  [{extra!r}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('file', type=Path)
    ap.add_argument('--lang', choices=['python', 'javascript', 'c'])
    ap.add_argument('--docstrings-only', action='store_true')
    args = ap.parse_args(argv)
    text = args.file.read_text(encoding='utf-8', errors='replace')
    lang = args.lang or lang_of(args.file)
    sys.stdout.write(mask_python_docstrings_only(text) if args.docstrings_only else mask(text, lang))
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
