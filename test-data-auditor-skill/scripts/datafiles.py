#!/usr/bin/env python3
"""Read test data files into records with line numbers (stdlib only).

    python datafiles.py tests/fixtures/patients.json      # print each record's fields and lines

Formats: JSON (any shape; objects become records), a YAML subset (block mappings and sequences,
plain and quoted scalars, inline [a, b] lists, comments), CSV (header row), SQL INSERT statements
(`INSERT INTO t (a, b) VALUES (...), (...)`), and Python test sources (dict literals with
constant keys). Anything else is read as text lines. YAML anchors, aliases, tags, block
scalars (| and >) and multiple documents are not supported: such a file raises NotAssessed and
is reported as not assessed, never guessed at.

A record is {"source", "kind" (object|row|dict_literal), "hint" (table or key it sits under),
"line", "fields": {name: {"value", "line"}}}; nested objects are records of their own.
"""
from __future__ import annotations
import ast, csv, io, json, re, sys
from pathlib import Path

DATA_EXT = {'.json': 'json', '.yaml': 'yaml', '.yml': 'yaml', '.csv': 'csv', '.tsv': 'csv', '.sql': 'sql', '.py': 'python'}


class NotAssessed(Exception):
    pass


def fmt_of(path: Path) -> str:
    return DATA_EXT.get(path.suffix.lower(), 'text')


# ---------------------------------------------------------------- JSON
def json_records(text: str, source: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NotAssessed(f'not valid JSON ({exc})')
    lines = text.split('\n')
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    cursor = [0]

    def line_at(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def find_key(key: str) -> int:
        m = re.compile(r'"' + re.escape(json.dumps(key)[1:-1]) + r'"\s*:').search(text, cursor[0])
        if not m:
            return line_at(cursor[0])
        cursor[0] = m.end()
        return line_at(m.start())

    out = []

    def walk(node, hint):
        if isinstance(node, dict):
            rec = {'source': source, 'kind': 'object', 'hint': hint, 'line': line_at(cursor[0]), 'fields': {}}
            out.append(rec)
            first = True
            for k, v in node.items():
                ln = find_key(k)
                if first:
                    rec['line'], first = ln, False
                if isinstance(v, (dict, list)):
                    walk(v, k)
                else:
                    rec['fields'][k] = {'value': v, 'line': ln}
        elif isinstance(node, list):
            for item in node:
                walk(item, hint)
    walk(data, None)
    return [r for r in out if r['fields']]


# ---------------------------------------------------------------- YAML subset
def yaml_scalar(s: str):
    s = s.strip()
    if not s or s in ('~', 'null', 'Null', 'NULL'):
        return None
    if s[0] in '"\'':
        if len(s) < 2 or s[-1] != s[0]:
            raise NotAssessed('unterminated quoted scalar')
        inner = s[1:-1]
        return inner.replace("''", "'") if s[0] == "'" else bytes(inner, 'utf-8').decode('unicode_escape')
    if s.startswith('[') and s.endswith(']'):
        return [yaml_scalar(x) for x in s[1:-1].split(',')] if s[1:-1].strip() else []
    if s.startswith('{'):
        raise NotAssessed('inline mappings are not supported')
    if s in ('true', 'True', 'yes'):
        return True
    if s in ('false', 'False', 'no'):
        return False
    if re.fullmatch(r'[-+]?\d+', s):
        return int(s)
    if re.fullmatch(r'[-+]?\d*\.\d+', s):
        return float(s)
    return s


def strip_comment(line: str) -> str:
    out, quote = [], None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in '"\'':
            quote = c
        elif c == '#' and (i == 0 or line[i - 1] in ' \t'):
            break
        out.append(c)
    return ''.join(out).rstrip()


def yaml_records(text: str, source: str) -> list[dict]:
    rows = []
    for n, raw in enumerate(text.split('\n'), 1):
        if raw.strip() in ('---', '...') and n == 1:
            continue
        if raw.strip() == '---':
            raise NotAssessed('multiple YAML documents are not supported')
        line = strip_comment(raw)
        if not line.strip():
            continue
        if re.search(r'(^|\s)[&*][\w-]+|!!?\w+|:\s*[|>][-+]?\s*$', line):
            raise NotAssessed(f'line {n}: anchors, aliases, tags and block scalars are not supported')
        rows.append((len(line) - len(line.lstrip()), line.strip(), n))
    out = []
    # (indent, record, hint) stack of open mappings
    stack: list[tuple[int, dict]] = []

    def open_record(indent, hint, ln):
        rec = {'source': source, 'kind': 'object', 'hint': hint, 'line': ln, 'fields': {}}
        out.append(rec)
        stack.append((indent, rec))
        return rec

    pending_key: list[tuple[int, str | None]] = [(-1, None)]
    for indent, body, ln in rows:
        while stack and stack[-1][0] > indent:
            stack.pop()
        while len(pending_key) > 1 and pending_key[-1][0] >= indent:
            pending_key.pop()
        if body.startswith('- ') or body == '-':
            item = body[2:].strip() if body != '-' else ''
            while stack and stack[-1][0] >= indent:
                stack.pop()
            hint = pending_key[-1][1]
            if re.match(r'^[^\'"\[]+?:(\s|$)', item):
                rec = open_record(indent + 2, hint, ln)
                key, _, val = item.partition(':')
                if val.strip():
                    rec['fields'][key.strip()] = {'value': yaml_scalar(val), 'line': ln}
                else:
                    pending_key.append((indent + 2, key.strip()))
            continue
        key, sep, val = body.partition(':')
        if not sep:
            raise NotAssessed(f'line {ln}: not a mapping entry')
        key = key.strip().strip('"\'')
        if not val.strip():
            pending_key.append((indent, key))
            continue
        if not stack or stack[-1][0] != indent:
            rec = open_record(indent, pending_key[-1][1], ln)
        else:
            rec = stack[-1][1]
        rec['fields'][key] = {'value': yaml_scalar(val), 'line': ln}
    return [r for r in out if r['fields']]


# ---------------------------------------------------------------- CSV and SQL
def csv_records(text: str, source: str, delim: str = ',') -> list[dict]:
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    out = []
    for i, row in enumerate(rows[1:], 2):
        if not any(c.strip() for c in row):
            continue
        out.append({'source': source, 'kind': 'row', 'hint': Path(source).stem, 'line': i,
                    'fields': {h: {'value': v, 'line': i} for h, v in zip(head, row)}})
    return out


INSERT = re.compile(r'INSERT\s+INTO\s+[`"\[]?([\w.]+)[`"\]]?\s*\(([^)]*)\)\s*VALUES\s*', re.I)


def sql_values(text: str, pos: int) -> tuple[list[tuple[list, int]], int]:
    """Parse '(...), (...)' from pos; return [(values, offset)], end offset."""
    tuples, i, n = [], pos, len(text)
    while i < n:
        while i < n and text[i] in ' \t\r\n,':
            i += 1
        if i >= n or text[i] != '(':
            break
        start, i, vals, cur = i, i + 1, [], ''
        while i < n:
            c = text[i]
            if c == "'":
                j, s = i + 1, []
                while j < n:
                    if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                        s.append("'"); j += 2; continue
                    if text[j] == "'":
                        break
                    s.append(text[j]); j += 1
                vals.append(''.join(s)); cur = None; i = j + 1
                continue
            if c in ',)':
                if cur is not None and cur.strip():
                    t = cur.strip()
                    vals.append(None if t.upper() == 'NULL' else int(t) if re.fullmatch(r'-?\d+', t) else
                                float(t) if re.fullmatch(r'-?\d*\.\d+', t) else t)
                cur = ''
                i += 1
                if c == ')':
                    break
                continue
            if cur is not None:
                cur += c
            i += 1
        tuples.append((vals, start))
    return tuples, i


def sql_records(text: str, source: str) -> list[dict]:
    out = []
    for m in INSERT.finditer(text):
        cols = [c.strip().strip('`"[]') for c in m.group(2).split(',')]
        for vals, off in sql_values(text, m.end())[0]:
            ln = text.count('\n', 0, off) + 1
            out.append({'source': source, 'kind': 'row', 'hint': m.group(1).split('.')[-1], 'line': ln,
                        'fields': {c: {'value': v, 'line': ln} for c, v in zip(cols, vals)}})
    return out


# ---------------------------------------------------------------- Python sources
def python_records(text: str, source: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise NotAssessed(f'not valid Python ({exc.msg})')
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
            fields = {}
            for k, v in zip(node.keys, node.values):
                if isinstance(v, ast.Constant) and not isinstance(v.value, bytes):
                    fields[k.value] = {'value': v.value, 'line': v.lineno}
            if fields:
                out.append({'source': source, 'kind': 'dict_literal', 'hint': None, 'line': node.lineno, 'fields': fields})
        elif isinstance(node, ast.Call) and node.keywords:
            fields = {kw.arg: {'value': kw.value.value, 'line': kw.value.lineno} for kw in node.keywords
                      if kw.arg and isinstance(kw.value, ast.Constant) and not isinstance(kw.value.value, bytes)}
            if len(fields) >= 2:
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                out.append({'source': source, 'kind': 'dict_literal', 'hint': name, 'line': node.lineno, 'fields': fields})
    return out


def load_records(path: Path, source: str) -> list[dict]:
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        raise NotAssessed('binary or not UTF-8 text')
    f = fmt_of(path)
    if f == 'json':
        return json_records(text, source)
    if f == 'yaml':
        return yaml_records(text, source)
    if f == 'csv':
        return csv_records(text, source, '\t' if path.suffix.lower() == '.tsv' else ',')
    if f == 'sql':
        return sql_records(text, source)
    if f == 'python':
        return python_records(text, source)
    return []


def load_structure(path: Path):
    """The parsed value of a JSON or YAML-subset file (YAML via its records)."""
    text = path.read_text(encoding='utf-8')
    if fmt_of(path) == 'json':
        return json.loads(text)
    return [{k: v['value'] for k, v in r['fields'].items()} for r in yaml_records(text, path.name)]


if __name__ == '__main__':
    p = Path(sys.argv[1])
    try:
        for r in load_records(p, p.as_posix()):
            print(r['line'], r['hint'], {k: (v['line'], type(v['value']).__name__) for k, v in r['fields'].items()})
    except NotAssessed as exc:
        print('not assessed:', exc)
