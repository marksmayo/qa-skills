#!/usr/bin/env python3
"""A stdlib parser for ICU MessageFormat: syntax errors, arguments, plural and select branches, tags.

    python icu_lite.py "{count, plural, one {# book} other {# books}}"

Understands {arg}, {arg, number|date|time[, style]}, {arg, plural|selectordinal, [offset:N] =N {..} key {..}},
{arg, select, key {..}}, # inside plural branches, apostrophe quoting ('{' and ''), and FormatJS-style
tags (<b>..</b>, <link/>). Returns the arguments with their types, the plural categories each plural
argument covers, the tags, and any syntax error with its offset.

Also recognises the other placeholder styles translators meet, so they can be compared between locales:
i18next `{{name}}`, printf `%s %d %1$s %@ %.2f`, and Python/gettext `%(name)s` and `{0}`.
"""
from __future__ import annotations
import json, re, sys
from dataclasses import dataclass, field

PLURAL_KEYS = {'zero', 'one', 'two', 'few', 'many', 'other'}
PRINTF = re.compile(r'%(?:(\d+)\$)?[-+ 0#]*\d*(?:\.\d+)?(?:l{0,2}|h)?([sdifuxXeEgGc@])|%\(([a-zA-Z_]\w*)\)[sd]')
I18NEXT = re.compile(r'\{\{\s*-?\s*([\w.]+)\s*(?:,[^}]*)?\}\}')


class IcuError(ValueError):
    def __init__(self, msg: str, pos: int):
        super().__init__(f'{msg} at offset {pos}')
        self.pos = pos


@dataclass
class Message:
    args: dict = field(default_factory=dict)        # name -> type (simple, number, date, time, plural, selectordinal, select)
    plurals: dict = field(default_factory=dict)     # plural arg name -> set of keys (categories and =N)
    selects: dict = field(default_factory=dict)
    tags: set = field(default_factory=set)


class Parser:
    def __init__(self, s: str):
        self.s, self.i, self.m = s, 0, Message()

    def error(self, msg):
        raise IcuError(msg, self.i)

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def ident(self) -> str:
        self.ws()
        m = re.compile(r'[^\s{},#<>/\']+').match(self.s, self.i)
        if not m:
            self.error('expected a name')
        self.i = m.end()
        return m.group(0)

    def message(self, in_plural: bool = False, until: str | None = None):
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == '}' and until == '}':
                return
            if ch == "'":
                nxt = self.s[self.i + 1:self.i + 2]
                if nxt == "'":
                    self.i += 2
                elif nxt and nxt in '{}#<|':
                    end = self.s.find("'", self.i + 1)
                    self.i = len(self.s) if end < 0 else end + 1
                else:
                    self.i += 1
            elif ch == '{':
                self.argument()
            elif ch == '}':
                self.error('unmatched }')
            elif ch == '<' and re.match(r'</?[A-Za-z][\w-]*\s*/?>', self.s[self.i:]):
                m = re.match(r'<(/?)([A-Za-z][\w-]*)\s*(/?)>', self.s[self.i:])
                if not m.group(1):
                    self.m.tags.add(m.group(2))
                self.i += m.end()
            else:
                self.i += 1
        if until == '}':
            self.error('unclosed {')

    def argument(self):
        self.i += 1
        name = self.ident()
        self.ws()
        if self.i >= len(self.s):
            self.error('unclosed {')
        if self.s[self.i] == '}':
            self.i += 1
            self.m.args.setdefault(name, 'simple')
            return
        if self.s[self.i] != ',':
            self.error(f'expected , or }} after {name!r}')
        self.i += 1
        kind = self.ident()
        self.ws()
        if kind in ('number', 'date', 'time', 'spellout', 'ordinal', 'duration'):
            self.m.args[name] = kind
            if self.s[self.i:self.i + 1] == ',':
                depth = 0
                while self.i < len(self.s):
                    c = self.s[self.i]
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        if depth == 0:
                            break
                        depth -= 1
                    self.i += 1
            self.ws()
            if self.s[self.i:self.i + 1] != '}':
                self.error(f'unclosed {{{name}, {kind}')
            self.i += 1
            return
        if kind not in ('plural', 'selectordinal', 'select'):
            self.error(f'unknown argument type {kind!r}')
        self.m.args[name] = kind
        if self.s[self.i:self.i + 1] != ',':
            self.error(f'expected , after {kind}')
        self.i += 1
        keys = set()
        self.ws()
        if kind != 'select' and self.s.startswith('offset:', self.i):
            self.i += 7
            self.ident()
        while True:
            self.ws()
            if self.s[self.i:self.i + 1] == '}':
                self.i += 1
                break
            if self.i >= len(self.s):
                self.error(f'unclosed {kind}')
            key = self.ident()
            if key in keys:
                self.error(f'duplicate key {key!r} in {name}')
            if kind != 'select' and not (key in PLURAL_KEYS or re.fullmatch(r'=\d+', key)):
                self.error(f'{key!r} is not a plural category')
            keys.add(key)
            self.ws()
            if self.s[self.i:self.i + 1] != '{':
                self.error(f'expected {{ after {key!r}')
            self.i += 1
            self.message(in_plural=kind != 'select', until='}')
            self.i += 1
        if 'other' not in keys:
            self.error(f'{kind} {name!r} has no other branch')
        (self.m.selects if kind == 'select' else self.m.plurals)[name] = keys


def parse(s: str) -> Message:
    p = Parser(s)
    p.message()
    return p.m


def placeholders(s: str, style: str = 'auto') -> dict:
    """Placeholders of any style, as {'style': ..., 'names': sorted list, 'error': str|None, ...}."""
    out = {'style': None, 'names': [], 'error': None, 'plurals': {}, 'tags': []}
    if style in ('auto', 'i18next') and I18NEXT.search(s):
        out['style'] = 'i18next'
        out['names'] = sorted({m.group(1) for m in I18NEXT.finditer(s)})
        return out
    if style in ('auto', 'printf') and PRINTF.search(s) and '{' not in s:
        out['style'] = 'printf'
        specs = []
        for i, m in enumerate(PRINTF.finditer(s), 1):
            if m.group(3):
                specs.append(f'%({m.group(3)})')
            else:
                conv = {'i': 'd', 'u': 'd', 'f': 'f', 'e': 'f', 'E': 'f', 'g': 'f', 'G': 'f', 'x': 'd', 'X': 'd'}.get(m.group(2), m.group(2))
                specs.append(f'{m.group(1) or i}${conv}')
        out['names'] = sorted(specs)
        return out
    if '{' in s or style == 'icu':
        out['style'] = 'icu'
        try:
            msg = parse(s)
            out['names'] = sorted(msg.args)
            out['plurals'] = {k: sorted(v) for k, v in msg.plurals.items()}
            out['tags'] = sorted(msg.tags)
        except IcuError as e:
            out['error'] = str(e)
    return out


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        print(json.dumps(placeholders(arg), ensure_ascii=False))
