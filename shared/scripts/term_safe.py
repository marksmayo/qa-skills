#!/usr/bin/env python3
"""Report text made safe to print to a terminal (stdlib only).

    from term_safe import load_json, clean
    d = load_json(path)            # json.loads(path.read_text()) with every string cleaned
    python term_safe.py --self-test

Reports quote CI logs, tickets, page text and API responses, so a string in one can hold terminal control
sequences: an OSC 8 hyperlink that hides its target, a window-title change, a colour that hides the rest of
the line, a cursor move that overwrites earlier output. JSON carries them as \\u001b escapes, which
json.loads turns into real control characters. clean() removes C0 and C1 control characters (and DEL) from
every string, keys included, except newline and tab; a carriage return is removed too, so a line cannot be
overwritten in place. The renderers' own colours are added after cleaning, so they still work.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def clean(obj):
    """obj with every control character except \\n and \\t removed from every string (dict keys included)."""
    if isinstance(obj, str):
        return CONTROL.sub('', obj) if CONTROL.search(obj) else obj
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, dict):
        return {clean(k): clean(v) for k, v in obj.items()}
    return obj


def load_json(path, encoding: str = 'utf-8-sig'):
    """A JSON file, cleaned for printing."""
    return clean(json.loads(Path(path).read_text(encoding=encoding)))


def self_test() -> int:
    d = json.loads('{"title": "SQL injection\\u001b]8;;https://evil.example.test\\u0007 in search\\u001b]8;;\\u0007",'
                   ' "lines": ["ok\\tthen\\nnext", "\\u009b31mred\\u001b[0m\\r overwritten"], "\\u001bkey": 3}')
    c = clean(d)
    fails = []
    if any(ch in json.dumps(c, ensure_ascii=False) for ch in '\x1b\x07\x9b\r'):
        fails.append(f'control characters left: {c!r}')
    if c['title'] != 'SQL injection]8;;https://evil.example.test in search]8;;':
        fails.append(f'title cleaned wrongly: {c["title"]!r}')
    if c['lines'][0] != 'ok\tthen\nnext' or 'key' not in c:
        fails.append(f'newline/tab or keys not kept: {c!r}')
    if fails:
        print('term_safe self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('term_safe self-test: ok (ESC, BEL, C1 and CR removed from strings and keys; newline and tab kept)')
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        raise SystemExit(self_test())
    print(__doc__)
    raise SystemExit(2)
