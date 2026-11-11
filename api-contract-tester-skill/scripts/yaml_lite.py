#!/usr/bin/env python3
"""A stdlib parser for the YAML subset API contracts are written in, used when PyYAML is absent.

Supports block mappings and sequences, plain, single- and double-quoted scalars, flow sequences and
mappings (also over several lines), literal and folded block scalars (| |- > >-), comments, and a
leading `---`. Plain scalars resolve as YAML 1.2's core schema does: null, true/false, integers and
floats; everything else (including 1.5.0 and dates) stays a string.

Not supported: anchors and aliases (& *), tags (!!), multi-document streams, complex keys (?),
and plain multi-line scalars. A document using them raises YamlError with the line number, so the
caller can say which parser it needs instead of guessing.

    python yaml_lite.py file.yaml        # prints the parsed document as JSON
"""
from __future__ import annotations
import json, re, sys

INT_RE = re.compile(r'^[-+]?(0|[1-9][0-9]*)$')
FLOAT_RE = re.compile(r'^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$')


class YamlError(ValueError):
    pass


def _strip_comment(s: str) -> str:
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q and not (q == '"' and i and s[i - 1] == '\\'):
                q = None
        elif ch in '\'"' and (i == 0 or s[i - 1] in ' [{,:-'):
            q = ch
        elif ch == '#' and (i == 0 or s[i - 1] in ' \t'):
            return s[:i].rstrip()
    return s.rstrip()


def _plain(s: str):
    if s in ('null', 'Null', 'NULL', '~', ''):
        return None
    if s in ('true', 'True', 'TRUE'):
        return True
    if s in ('false', 'False', 'FALSE'):
        return False
    if INT_RE.match(s):
        return int(s)
    if re.match(r'^0x[0-9a-fA-F]+$', s):
        return int(s, 16)
    if re.match(r'^0o[0-7]+$', s):
        return int(s[2:], 8)
    if FLOAT_RE.match(s) and any(c.isdigit() for c in s):
        return float(s)
    if s in ('.inf', '.Inf', '+.inf'):
        return float('inf')
    if s in ('-.inf', '-.Inf'):
        return float('-inf')
    if s in ('.nan', '.NaN'):
        return float('nan')
    if s[0] in '&*!?|>%@`':
        raise YamlError(f'unsupported YAML feature at {s[:20]!r}')
    return s


def _unquote(s: str, line: int) -> tuple[str, int]:
    """Parse a quoted scalar at the start of s; return (value, characters consumed)."""
    q = s[0]
    i, out = 1, []
    while i < len(s):
        ch = s[i]
        if q == "'" and ch == "'":
            if s[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return ''.join(out), i + 1
        if q == '"' and ch == '\\':
            nxt = s[i + 1:i + 2]
            if nxt == 'u':
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({'n': '\n', 't': '\t', 'r': '\r', '0': '\0', '"': '"', '\\': '\\', '/': '/', ' ': ' '}.get(nxt, nxt))
            i += 2
            continue
        if q == '"' and ch == '"':
            return ''.join(out), i + 1
        out.append(ch)
        i += 1
    raise YamlError(f'line {line}: unterminated quoted string')


class _Flow:
    def __init__(self, s: str, line: int):
        self.s, self.i, self.line = s, 0, line

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in ' \t\n':
            self.i += 1

    def value(self):
        self.ws()
        ch = self.s[self.i:self.i + 1]
        if ch == '[':
            self.i += 1
            out = []
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == ']':
                    self.i += 1
                    return out
                out.append(self.value())
                self.ws()
                if self.s[self.i:self.i + 1] == ',':
                    self.i += 1
        if ch == '{':
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == '}':
                    self.i += 1
                    return out
                k = self.value()
                self.ws()
                if self.s[self.i:self.i + 1] != ':':
                    raise YamlError(f'line {self.line}: expected : in flow mapping')
                self.i += 1
                out[k] = self.value()
                self.ws()
                if self.s[self.i:self.i + 1] == ',':
                    self.i += 1
        if ch in ('"', "'"):
            v, n = _unquote(self.s[self.i:], self.line)
            self.i += n
            return v
        j = self.i
        while j < len(self.s) and self.s[j] not in ',]}' and not (self.s[j] == ':' and self.s[j + 1:j + 2] in (' ', '')):
            j += 1
        tok, self.i = self.s[self.i:j].strip(), j
        if tok == '':
            raise YamlError(f'line {self.line}: empty flow value')
        return _plain(tok)


class _Parser:
    def __init__(self, text: str):
        self.lines = text.replace('\r\n', '\n').replace('\t', '    ').split('\n')
        self.i = 0

    def peek(self):
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            c = _strip_comment(raw)
            if c.strip() in ('', '---'):
                self.i += 1
                continue
            if c.strip() == '...':
                self.i = len(self.lines)
                return None
            return len(c) - len(c.lstrip(' ')), c.strip()
        return None

    @staticmethod
    def is_item(c: str) -> bool:
        return c == '-' or c.startswith('- ')

    def node(self, indent: int):
        p = self.peek()
        if p is None:
            return None
        return self.seq(p[0]) if self.is_item(p[1]) else self.mapping(p[0])

    def seq(self, ind: int):
        out = []
        while (p := self.peek()) and p[0] == ind and self.is_item(p[1]):
            rest = p[1][1:].lstrip(' ')
            if not rest:
                self.i += 1
                nxt = self.peek()
                out.append(self.node(nxt[0]) if nxt and nxt[0] > ind else None)
            elif self.is_item(rest) or self.key_split(rest) is not None:
                col = ind + len(p[1]) - len(rest)
                self.lines[self.i] = ' ' * col + rest
                out.append(self.node(col))
            else:
                self.i += 1
                out.append(self.inline(rest, ind))
        return out

    def key_split(self, c: str):
        if c[0] in '"\'':
            try:
                k, n = _unquote(c, self.i + 1)
            except YamlError:
                return None
            rest = c[n:].lstrip(' ')
            if rest == ':' or rest.startswith(': '):
                return k, rest[1:].strip()
            return None
        if c[0] in '[{':
            return None
        m = re.search(r':( |$)', c)
        if not m:
            return None
        return _plain(c[:m.start()].strip()), c[m.end():].strip()

    def mapping(self, ind: int):
        out = {}
        while (p := self.peek()) and p[0] == ind and not self.is_item(p[1]):
            ks = self.key_split(p[1])
            if ks is None:
                raise YamlError(f'line {self.i + 1}: expected "key: value", got {p[1][:40]!r}')
            k, rest = ks
            if isinstance(k, (list, dict)):
                raise YamlError(f'line {self.i + 1}: complex keys are not supported')
            self.i += 1
            if rest == '':
                nxt = self.peek()
                if nxt and nxt[0] > ind:
                    v = self.node(nxt[0])
                elif nxt and nxt[0] == ind and self.is_item(nxt[1]):
                    v = self.seq(ind)
                else:
                    v = None
            elif rest[0] in '|>' and re.fullmatch(r'[|>][-+]?', rest):
                v = self.block_scalar(rest, ind)
            else:
                v = self.inline(rest, ind)
            out[k if isinstance(k, str) else (str(k) if not isinstance(k, bool) else k)] = v
        p = self.peek()
        if p and p[0] > ind:
            raise YamlError(f'line {self.i + 1}: unexpected indentation')
        return out

    def inline(self, rest: str, ind: int):
        if rest[0] in '[{':
            text, depth = rest, 0
            while True:
                depth = _depth(text)
                if depth <= 0 or self.i >= len(self.lines):
                    break
                text += ' ' + _strip_comment(self.lines[self.i]).strip()
                self.i += 1
            f = _Flow(text, self.i)
            v = f.value()
            if text[f.i:].strip():
                raise YamlError(f'line {self.i}: trailing text after flow collection')
            return v
        if rest[0] in '"\'':
            v, n = _unquote(rest, self.i)
            if rest[n:].strip():
                raise YamlError(f'line {self.i}: trailing text after quoted scalar')
            return v
        nxt = self.peek()
        if nxt and nxt[0] > ind and not self.is_item(nxt[1]) and self.key_split(nxt[1]) is None:
            raise YamlError(f'line {self.i + 1}: multi-line plain scalars are not supported')
        return _plain(rest)

    def block_scalar(self, head: str, ind: int) -> str:
        body, block_ind = [], None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() == '':
                body.append('')
                self.i += 1
                continue
            n = len(raw) - len(raw.lstrip(' '))
            if n <= ind:
                break
            block_ind = n if block_ind is None else block_ind
            body.append(raw[block_ind:])
            self.i += 1
        while body and body[-1] == '':
            body.pop()
        if head[0] == '|':
            text = '\n'.join(body)
        else:
            text, para = '', []
            for ln in body:
                if ln == '':
                    text += ' '.join(para) + '\n'
                    para = []
                else:
                    para.append(ln)
            text += ' '.join(para)
        return text if head.endswith('-') else text + '\n'


def _depth(s: str) -> int:
    d, q = 0, None
    for ch in s:
        if q:
            q = None if ch == q else q
        elif ch in '"\'':
            q = ch
        elif ch in '[{':
            d += 1
        elif ch in ']}':
            d -= 1
    return d


def loads(text: str):
    p = _Parser(text)
    first = p.peek()
    if first is None:
        return None
    if first[1][0] in '[{"\'' and p.key_split(first[1]) is None:
        p.i += 1
        return p.inline(first[1], -1)
    v = p.node(first[0])
    if p.peek() is not None:
        raise YamlError(f'line {p.i + 1}: unexpected content (multi-document or bad indentation)')
    return v


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8') as fh:
        print(json.dumps(loads(fh.read()), indent=2))
