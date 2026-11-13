#!/usr/bin/env python3
"""Small Go source helpers for the mutation tester (stdlib only, no Go parser).

- tokens(text): Go tokens with positions; comments are dropped, string, raw string and rune
  literals are single 'str' tokens, so nothing inside them is ever mutated or read as code.
- test_funcs(text): top-level `func TestX(t *testing.T)` functions with their line spans.
- module_of(repo, rel_dir): the nearest go.mod at or above a directory (its dir and module path).
- coverprofile(text, module_dir, module): {repo-relative file: {line: covered}} from a Go
  coverprofile (`go test -coverprofile`), block ranges expanded to lines.
"""
from __future__ import annotations
import re
from pathlib import Path

OPS = ['<<=', '>>=', '&^=', '...', '&&', '||', '<-', '<<', '>>', '&^', '++', '--', '+=', '-=', '*=', '/=', '%=', '&=',
       '|=', '^=', '==', '!=', '<=', '>=', ':=', '+', '-', '*', '/', '%', '<', '>', '!', '&', '|', '^', '=', '(', ')',
       '[', ']', '{', '}', ',', ';', '.', ':', '~']
IDENT = re.compile(r'[A-Za-z_À-￿][\wÀ-￿]*')
NUMBER = re.compile(r'0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*)?\.?\d[\d_]*(?:[eE][+-]?\d+)?i?')
KEYWORDS = {'break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else', 'fallthrough', 'for', 'func', 'go',
            'goto', 'if', 'import', 'interface', 'map', 'package', 'range', 'return', 'select', 'struct', 'switch', 'type',
            'var'}
TEST_FUNC = re.compile(r'^func\s+(Test\w*)\s*\(\s*\w+\s+\*testing\.T\s*\)', re.M)


class Tok:
    __slots__ = ('kind', 'text', 'start', 'end', 'line', 'col')

    def __init__(self, kind, text, start, end, line, col):
        self.kind, self.text, self.start, self.end, self.line, self.col = kind, text, start, end, line, col

    def __repr__(self):
        return f'Tok({self.kind},{self.text!r},{self.line}:{self.col})'


def tokens(text: str) -> list[Tok]:
    """Tokens of Go source. kind: ident, keyword, number, str, op. Comments are skipped."""
    out: list[Tok] = []
    i, n, line, line_start = 0, len(text), 1, 0

    def emit(kind, a, b):
        out.append(Tok(kind, text[a:b], a, b, line, a - line_start))

    while i < n:
        c = text[i]
        if c == '\n':
            line += 1
            line_start = i + 1
            i += 1
            continue
        if c in ' \t\r':
            i += 1
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            seg = text[i:j]
            if '\n' in seg:
                line += seg.count('\n')
                line_start = i + seg.rfind('\n') + 1
            i = j
            continue
        if c == '`':
            j = text.find('`', i + 1)
            j = n if j < 0 else j + 1
            emit('str', i, j)
            seg = text[i:j]
            if '\n' in seg:
                line += seg.count('\n')
                line_start = i + seg.rfind('\n') + 1
            i = j
            continue
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            emit('str', i, min(j + 1, n))
            i = j + 1
            continue
        m = IDENT.match(text, i)
        if m:
            emit('keyword' if m.group(0) in KEYWORDS else 'ident', i, m.end())
            i = m.end()
            continue
        if c.isdigit() or (c == '.' and i + 1 < n and text[i + 1].isdigit()):
            m = NUMBER.match(text, i)
            j = m.end() if m and m.end() > i else i + 1
            emit('number', i, j)
            i = j
            continue
        op = next((o for o in OPS if text.startswith(o, i)), c)
        emit('op', i, i + len(op))
        i += len(op)
    return out


def body_span(toks: list[Tok], open_idx: int) -> int:
    """Index of the token closing the brace opened at toks[open_idx]."""
    depth = 0
    for k in range(open_idx, len(toks)):
        if toks[k].text == '{':
            depth += 1
        elif toks[k].text == '}':
            depth -= 1
            if depth == 0:
                return k
    return len(toks) - 1


def test_funcs(text: str) -> dict[str, tuple[int, int]]:
    """{TestName: (first line, last line)} for top-level test functions."""
    toks = tokens(text)
    out = {}
    for k, t in enumerate(toks):
        if t.text == 'func' and t.col == 0 and k + 1 < len(toks) and toks[k + 1].kind == 'ident' and \
                toks[k + 1].text.startswith('Test') and k + 2 < len(toks) and toks[k + 2].text == '(':
            j = next((x for x in range(k + 2, len(toks)) if toks[x].text == '{'), None)
            if j is None:
                continue
            end = body_span(toks, j)
            out[toks[k + 1].text] = (t.line, toks[end].line)
    return out


def calls_in_test(text: str, test: str, name: str) -> bool | None:
    """Does the body of top-level test `test` call `name(...)` or `x.name(...)` directly? None if not found."""
    toks = tokens(text)
    for k, t in enumerate(toks):
        if t.text == 'func' and t.col == 0 and k + 1 < len(toks) and toks[k + 1].text == test:
            j = next((x for x in range(k + 2, len(toks)) if toks[x].text == '{'), None)
            if j is None:
                return None
            end = body_span(toks, j)
            return any(toks[x].text == name and toks[x].kind == 'ident' and x + 1 <= end and toks[x + 1].text == '('
                       for x in range(j, end))
    return None


def test_file_index(pkg_dir: Path) -> dict[str, str]:
    """{TestName: test file name} for the *_test.go files in one package directory."""
    out = {}
    for f in sorted(pkg_dir.glob('*_test.go')):
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for m in TEST_FUNC.finditer(text):
            out.setdefault(m.group(1), f.name)
    return out


def module_of(repo: Path, rel_dir: str) -> tuple[str, str] | None:
    """(module root relative to repo, module path) of the nearest go.mod at or above rel_dir."""
    d = Path(rel_dir) if rel_dir not in ('', '.') else Path('.')
    while True:
        gm = repo / d / 'go.mod'
        if gm.is_file():
            m = re.search(r'^\s*module\s+(\S+)', gm.read_text(encoding='utf-8', errors='replace'), re.M)
            if m:
                return d.as_posix(), m.group(1).strip('"')
        if d.as_posix() in ('.', ''):
            return None
        d = d.parent


PROFILE_ROW = re.compile(r'^(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$')


def coverprofile(text: str, module: str, module_dir: str) -> dict[str, set[int]]:
    """{repo-relative file: covered lines} from a coverprofile; files outside the module are left out."""
    out: dict[str, set[int]] = {}
    prefix = '' if module_dir in ('.', '') else module_dir.rstrip('/') + '/'
    for row in text.splitlines():
        m = PROFILE_ROW.match(row.strip())
        if not m or m.group(4) == '0':
            continue
        path = m.group(1).replace('\\', '/')
        if not path.startswith(module + '/'):
            continue
        rel = prefix + path[len(module) + 1:]
        out.setdefault(rel, set()).update(range(int(m.group(2)), int(m.group(3)) + 1))
    return out
