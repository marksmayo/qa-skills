#!/usr/bin/env python3
"""A small JavaScript/TypeScript tokenizer for the test data rules (stdlib only).

    python jsparse.py src/__tests__/users.test.ts      # print each object literal's fields and lines

It is not a parser. It knows enough to read test data reliably:

- strings ('...', "...", template literals; a template with ${...} has no fixed value),
- comments (// and /* */), which are skipped,
- regular expression literals (so a quote inside /can't/ does not open a string),
- numbers, identifiers and punctuation, each with its line.

On top of the tokens, `records()` returns the object literals with constant fields, in the same
shape as datafiles.py: {"source", "kind": "object_literal", "hint", "line", "fields": {name: {"value",
"line"}}}. A field whose value is a date call (new Date('2026-12-01'), new Date(2026, 11, 1),
Date.UTC(...), dayjs(...), moment(...), parseISO(...), DateTime.fromISO(...)) gets the date as an
ISO string, with "date_call" set. `masked()` returns the source with comments and string contents
blanked, so call patterns can be matched without being fooled by text inside strings.
"""
from __future__ import annotations
import bisect, re, sys
from datetime import date
from pathlib import Path

REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^')
REGEX_KEYWORDS = {'return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'new', 'delete', 'void', 'throw', 'yield', 'await'}
DATE_FUNCS = {'dayjs', 'moment', 'parseISO', 'fromISO', 'DateTime.fromISO', 'Temporal.PlainDate.from', 'toDate', 'utc', 'dayjs.utc',
              'moment.utc', 'new Date', 'Date.parse', 'parseJSON'}
ID = re.compile(r'[A-Za-z_$][\w$]*')
NUM = re.compile(r'0[xXbBoO][\da-fA-F_]+n?|(?:\d[\d_]*\.?\d*|\.\d+)(?:[eE][+-]?\d+)?n?')
JS_EXT = ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts')


class Tok:
    __slots__ = ('kind', 'text', 'value', 'pos', 'end', 'line')

    def __init__(self, kind, text, value, pos, end, line):
        self.kind, self.text, self.value, self.pos, self.end, self.line = kind, text, value, pos, end, line

    def __repr__(self):
        return f'Tok({self.kind}, {self.text!r}, line {self.line})'


def is_js(path: str) -> bool:
    return path.lower().endswith(JS_EXT)


def _unescape(s: str) -> str:
    return re.sub(r'\\(.)', lambda m: {'n': '\n', 't': '\t', 'r': '\r'}.get(m.group(1), m.group(1)), s)


def tokenize(text: str) -> list[Tok]:
    starts = [0] + [m.end() for m in re.finditer('\n', text)]
    line_of = lambda pos: bisect.bisect_right(starts, pos)  # noqa: E731
    out: list[Tok] = []
    i, n = 0, len(text)

    def prev_sig():
        return out[-1] if out else None

    while i < n:
        c = text[i]
        if c in ' \t\r\n﻿':
            i += 1
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in '\'"':
            j, buf = i + 1, []
            while j < n and text[j] != c and text[j] != '\n':
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            out.append(Tok('str', text[i:j + 1], _unescape(''.join(buf)), i, j + 1, line_of(i)))
            i = j + 1
            continue
        if c == '`':
            j, buf, dynamic = i + 1, [], False
            while j < n and text[j] != '`':
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if text.startswith('${', j):
                    dynamic, depth, j = True, 1, j + 2
                    while j < n and depth:
                        ch = text[j]
                        if ch in '\'"`':        # a string inside the expression
                            k = j + 1
                            while k < n and text[k] != ch:
                                k += 2 if text[k] == '\\' else 1
                            j = k + 1
                            continue
                        depth += ch == '{'
                        depth -= ch == '}'
                        j += 1
                    buf.append('\x00')
                    continue
                buf.append(text[j])
                j += 1
            out.append(Tok('tpl' if dynamic else 'str', text[i:j + 1], None if dynamic else _unescape(''.join(buf)), i, j + 1, line_of(i)))
            i = j + 1
            continue
        if c == '/':
            p = prev_sig()
            if p is None or (p.kind == 'punct' and p.text[-1] in REGEX_BEFORE) or (p.kind == 'id' and p.text in REGEX_KEYWORDS):
                j, in_class = i + 1, False
                while j < n and text[j] != '\n':
                    ch = text[j]
                    if ch == '\\':
                        j += 2
                        continue
                    if ch == '[':
                        in_class = True
                    elif ch == ']':
                        in_class = False
                    elif ch == '/' and not in_class:
                        break
                    j += 1
                if j < n and text[j] == '/':
                    j += 1
                    while j < n and (text[j].isalpha()):
                        j += 1
                    out.append(Tok('regex', text[i:j], None, i, j, line_of(i)))
                    i = j
                    continue
        m = ID.match(text, i)
        if m:
            out.append(Tok('id', m.group(0), None, i, m.end(), line_of(i)))
            i = m.end()
            continue
        m = NUM.match(text, i) if c.isdigit() or (c == '.' and i + 1 < n and text[i + 1].isdigit()) else None
        if m:
            raw = m.group(0).replace('_', '').rstrip('n')
            try:
                val = int(raw, 0) if re.fullmatch(r'0[xXbBoO].+|\d+', raw) else float(raw)
            except ValueError:
                val = None
            out.append(Tok('num', m.group(0), val, i, m.end(), line_of(i)))
            i = m.end()
            continue
        for op in ('===', '!==', '>>>', '...', '=>', '==', '!=', '<=', '>=', '&&', '||', '??', '?.', '++', '--', '+=', '-=', '**'):
            if text.startswith(op, i):
                out.append(Tok('punct', op, None, i, i + len(op), line_of(i)))
                i += len(op)
                break
        else:
            out.append(Tok('punct', c, None, i, i + 1, line_of(i)))
            i += 1
    return out


def _mask_template(src: str) -> str:
    """A template literal with its text blanked and its ${...} expressions kept (string literals inside them blanked):
    `k-${Math.random()}` is code that runs, and must stay visible to the call patterns."""
    out, i, n = ['`'], 1, len(src)
    blank = lambda s: re.sub(r'[^\n]', ' ', s)  # noqa: E731
    while i < n - 1:
        if src[i] == '\\' and i + 1 < n - 1:
            out.append(blank(src[i:i + 2]))
            i += 2
            continue
        if src.startswith('${', i):
            out.append('${')
            depth, j = 1, i + 2
            while j < n - 1 and depth:
                ch = src[j]
                if ch in '\'"`':
                    k = j + 1
                    while k < n - 1 and src[k] != ch:
                        k += 2 if src[k] == '\\' else 1
                    out.append(ch + blank(src[j + 1:k]) + (src[k] if k < n - 1 else ''))
                    j = k + 1
                    continue
                depth += ch == '{'
                depth -= ch == '}'
                out.append(ch)
                j += 1
            i = j
            continue
        out.append(blank(src[i]))
        i += 1
    out.append(src[-1] if n > 1 else '')
    return ''.join(out)


def masked(text: str, toks: list[Tok] | None = None) -> str:
    """The source with comments removed and string/regex contents replaced by spaces (newlines kept). The ${...}
    expressions of a template literal are code, so they are kept."""
    toks = toks if toks is not None else tokenize(text)
    out, last = [], 0
    for t in toks:
        gap = text[last:t.pos]
        out.append(re.sub(r'[^\n]', ' ', gap))             # whitespace and comments
        if t.kind == 'tpl':
            out.append(_mask_template(t.text))
        elif t.kind in ('str', 'regex'):
            out.append(t.text[0] + re.sub(r'[^\n]', ' ', t.text[1:-1]) + t.text[-1] if len(t.text) >= 2 else t.text)
        else:
            out.append(t.text)
        last = t.end
    out.append(re.sub(r'[^\n]', ' ', text[last:]))
    return ''.join(out)


def _iso(y, m, d, rest=''):
    try:
        return date(int(y), int(m), int(d)).isoformat() + rest
    except (ValueError, TypeError):
        return None


def date_value(toks: list[Tok], k: int) -> tuple[str | None, int]:
    """If a date call starts at toks[k], return (ISO date string, index after the call), else (None, k)."""
    t = toks[k]
    name, j = None, k
    if t.kind == 'id' and t.text == 'new' and k + 1 < len(toks) and toks[k + 1].text == 'Date':
        name, j = 'new Date', k + 2
    elif t.kind == 'id':
        parts, j = [t.text], k + 1
        while j + 1 < len(toks) and toks[j].text == '.' and toks[j + 1].kind == 'id':
            parts.append(toks[j + 1].text)
            j += 2
        full = '.'.join(parts)
        if full == 'Date.UTC':
            name = 'Date.UTC'
        elif full in DATE_FUNCS or parts[-1] in DATE_FUNCS:
            name = full
    if not name or j >= len(toks) or toks[j].text != '(':
        return None, k
    args, depth, a = [], 1, j + 1
    while a < len(toks) and depth:
        if toks[a].text in '([{':
            depth += 1
        elif toks[a].text in ')]}':
            depth -= 1
            if not depth:
                break
        if depth == 1 and toks[a].text != ',':
            args.append(toks[a])
        a += 1
    end = a + 1
    if args and args[0].kind == 'str' and args[0].value:
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})(.*)$', args[0].value.strip())
        if m:
            v = _iso(m.group(1), m.group(2), m.group(3), m.group(4))
            return (v, end) if v else (None, k)
        return None, k
    nums = [x.value for x in args if x.kind == 'num']
    if name in ('new Date', 'Date.UTC') and len(nums) >= 3 and len(nums) == len(args) and all(isinstance(x, int) for x in nums):
        v = _iso(nums[0], nums[1] + 1, nums[2])     # JavaScript months count from 0
        return (v, end) if v else (None, k)
    return None, k


def records(text: str, source: str, toks: list[Tok] | None = None) -> list[dict]:
    """Object literals with constant fields (strings, numbers, booleans, dates)."""
    toks = toks if toks is not None else tokenize(text)
    out: list[dict] = []
    stack: list[dict | None] = []          # a record for '{', None for '(' and '['
    hint_for_next: str | None = None
    for k, t in enumerate(toks):
        if t.kind == 'punct' and t.text == '{':
            prev = toks[k - 1] if k else None
            is_obj = prev is None or prev.text in ('(', ',', '=', ':', '[', '?', '||', '&&', '??', 'return', '=>', '{') or \
                (prev.kind == 'id' and prev.text in ('return', 'yield', 'await'))
            if prev is not None and prev.text == '=>' and k + 1 < len(toks):
                is_obj = False                 # an arrow function body; ({ ... }) is caught by '('
            rec = {'source': source, 'kind': 'object_literal', 'hint': hint_for_next, 'line': t.line, 'fields': {}} if is_obj else None
            if rec is not None:
                out.append(rec)
            stack.append(rec)
            hint_for_next = None
            continue
        if t.kind == 'punct' and t.text in ('(', '['):
            stack.append(None)
            continue
        if t.kind == 'punct' and t.text in ('}', ')', ']'):
            if stack:
                stack.pop()
            continue
        top = stack[-1] if stack else None
        if top is None or t.kind not in ('id', 'str') or k + 1 >= len(toks) or toks[k + 1].text != ':':
            continue
        prev = toks[k - 1] if k else None
        if prev is None or prev.text not in ('{', ','):
            continue
        key = t.value if t.kind == 'str' else t.text
        if k + 2 >= len(toks):
            continue
        v = toks[k + 2]
        hint_for_next = key if v.text in ('{', '[') else None
        if v.kind == 'str':
            top['fields'][key] = {'value': v.value, 'line': v.line}
        elif v.kind == 'num' and (k + 3 >= len(toks) or toks[k + 3].text in (',', '}')):
            top['fields'][key] = {'value': v.value, 'line': v.line}
        elif v.kind == 'id' and v.text in ('true', 'false', 'null') and (k + 3 >= len(toks) or toks[k + 3].text in (',', '}')):
            top['fields'][key] = {'value': {'true': True, 'false': False, 'null': None}[v.text], 'line': v.line}
        elif v.kind == 'punct' and v.text == '-' and k + 3 < len(toks) and toks[k + 3].kind == 'num':
            top['fields'][key] = {'value': -toks[k + 3].value if toks[k + 3].value is not None else None, 'line': v.line}
        else:
            dv, _ = date_value(toks, k + 2)
            if dv:
                top['fields'][key] = {'value': dv, 'line': v.line, 'date_call': True}
    return [r for r in out if r['fields']]


def string_literals(toks: list[Tok]) -> list[Tok]:
    return [t for t in toks if t.kind == 'str' and t.value is not None]


if __name__ == '__main__':
    p = Path(sys.argv[1])
    src = p.read_text(encoding='utf-8', errors='replace')
    for r in records(src, p.as_posix()):
        print(r['line'], r['hint'], {k: (v['line'], type(v['value']).__name__) for k, v in r['fields'].items()})
