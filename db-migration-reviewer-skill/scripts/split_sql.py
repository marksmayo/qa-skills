#!/usr/bin/env python3
"""Split SQL text into statements with line numbers (stdlib). Handles quotes, comments, dollar quoting and DELIMITER.

    python split_sql.py migration.sql

Returns [(line, statement)] with comments removed and whitespace collapsed. It is not a parser: the
rules match statement shapes.
"""
from __future__ import annotations
import json, re, sys


def split(text: str, first_line: int = 1) -> list[tuple[int, str]]:
    out, buf, i, line, start = [], [], 0, first_line, None
    delim = ';'
    n = len(text)
    while i < n:
        if text.startswith('DELIMITER ', i) and (i == 0 or text[i - 1] == '\n'):
            end = text.find('\n', i)
            end = n if end < 0 else end
            delim = text[i + 10:end].strip() or ';'
            line += 1
            i = end + 1
            continue
        ch = text[i]
        if ch == '-' and text.startswith('--', i):
            end = text.find('\n', i)
            i = n if end < 0 else end
            continue
        if ch == '/' and text.startswith('/*', i):
            end = text.find('*/', i + 2)
            end = n if end < 0 else end + 2
            line += text.count('\n', i, end)
            i = end
            continue
        if ch in ("'", '"', '`'):
            j = i + 1
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 2 if text[j] == '\\' and ch != '"' else 1
            if start is None:
                start = line
            buf.append(text[i:j + 1])
            line += text.count('\n', i, j + 1)
            i = j + 1
            continue
        m = re.match(r'\$([A-Za-z_]*)\$', text[i:]) if ch == '$' else None
        if m:
            tag = m.group(0)
            end = text.find(tag, i + len(tag))
            end = n if end < 0 else end + len(tag)
            if start is None:
                start = line
            buf.append(text[i:end])
            line += text.count('\n', i, end)
            i = end
            continue
        if text.startswith(delim, i):
            stmt = ' '.join(''.join(buf).split())
            if stmt:
                out.append((start, stmt))
            buf, start = [], None
            i += len(delim)
            continue
        if ch == '\n':
            line += 1
        elif not ch.isspace() and start is None:
            start = line
        buf.append(ch)
        i += 1
    stmt = ' '.join(''.join(buf).split())
    if stmt:
        out.append((start, stmt))
    return out


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8') as f:
        print(json.dumps(split(f.read()), indent=1))
