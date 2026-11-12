#!/usr/bin/env python3
"""Read test data files into records with line numbers (stdlib only).

    python datafiles.py tests/fixtures/patients.json      # print each record's fields and lines

Formats: JSON (any shape; objects become records), a YAML subset (block mappings and sequences,
plain and quoted scalars, inline [a, b] lists, comments), CSV (header row), SQL INSERT statements
(`INSERT INTO t (a, b) VALUES (...), (...)`), and Python test sources (dict literals with
constant keys), HTML tables (including HTML or XML saved with an .xls extension, found by
sniffing the content) and .xlsx workbooks (stdlib zipfile; `--text` prints the text form whose
lines findings cite). Anything else is read as text lines. YAML anchors, aliases, tags, block
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


def sniff_markup(path: Path) -> str | None:
    """'html' or 'xml' when a file with a spreadsheet extension is really text: report tools save HTML tables and
    XML Spreadsheet 2003 files as .xls. None for a real binary file."""
    try:
        with path.open('rb') as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if b'\x00' in head:
        return None
    t = head.lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    if t.startswith((b'<!doctype html', b'<html', b'<table', b'<meta')) or b'<html' in t[:1024]:
        return 'html'
    if t.startswith(b'<?xml'):
        return 'xml'
    return None


def fmt_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == '.xls':
        return sniff_markup(path) or 'xls'
    if ext in ('.xlsx', '.xlsm'):
        return 'xlsx'
    if ext in ('.html', '.htm'):
        return 'html'
    return DATA_EXT.get(ext, 'text')


# ---------------------------------------------------------------- spreadsheets
XLSX_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
XLSX_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def xlsx_sheets(path: Path, max_rows: int = 200_000) -> list[tuple[str, list[list[str]]]]:
    """[(sheet name, rows of cell text)] from an .xlsx workbook, with stdlib zipfile and XML only: shared strings,
    inline strings and plain values. Formulas give their cached value; styles and dates (serial numbers) are not
    interpreted. Raises NotAssessed for an encrypted, corrupt or unexpected file."""
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise NotAssessed(f'not a readable xlsx workbook ({exc.__class__.__name__}); it may be encrypted')
    try:
        names = set(z.namelist())
        shared = []
        if 'xl/sharedStrings.xml' in names:
            for si in ET.fromstring(z.read('xl/sharedStrings.xml')).iter(XLSX_NS + 'si'):
                shared.append(''.join(t.text or '' for t in si.iter(XLSX_NS + 't')))
        book = ET.fromstring(z.read('xl/workbook.xml'))
        rels = {}
        if 'xl/_rels/workbook.xml.rels' in names:
            for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels')):
                rels[r.get('Id')] = r.get('Target', '').lstrip('/')
        out = []
        for sh in book.iter(XLSX_NS + 'sheet'):
            target = rels.get(sh.get(XLSX_REL + 'id'), '')
            target = target if target.startswith('xl/') else 'xl/' + target
            if target not in names:
                continue
            rows = []
            for row in ET.fromstring(z.read(target)).iter(XLSX_NS + 'row'):
                cells: dict[int, str] = {}
                for c in row.iter(XLSX_NS + 'c'):
                    ref = re.match(r'([A-Z]+)', c.get('r', '')) if c.get('r') else None
                    col = 0
                    if ref:
                        for ch in ref.group(1):
                            col = col * 26 + ord(ch) - 64
                        col -= 1
                    else:
                        col = len(cells)
                    t, v = c.get('t'), c.find(XLSX_NS + 'v')
                    if t == 's' and v is not None and (v.text or '').isdigit() and int(v.text) < len(shared):
                        val = shared[int(v.text)]
                    elif t == 'inlineStr':
                        val = ''.join(x.text or '' for x in c.iter(XLSX_NS + 't'))
                    else:
                        val = v.text if v is not None and v.text is not None else ''
                    cells[col] = val
                rows.append([cells.get(i, '') for i in range(max(cells) + 1)] if cells else [])
                if len(rows) >= max_rows:
                    break
            out.append((sh.get('name') or target, rows))
        return out
    except (KeyError, ET.ParseError, ValueError) as exc:
        raise NotAssessed(f'xlsx workbook could not be read ({exc.__class__.__name__})')
    finally:
        z.close()


def xlsx_text(path: Path) -> str:
    """A workbook as text, one line per row: a `[sheet <name>]` line, then each row as CSV. Line numbers in findings
    on an .xlsx file count lines of this text (print it with `datafiles.py <file> --text`)."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator='\n')
    for name, rows in xlsx_sheets(path):
        buf.write(f'[sheet {name}]\n')
        for r in rows:
            w.writerow(r)
    return buf.getvalue()


def text_of(path: Path, errors: str = 'replace') -> str:
    """The text the scanner and the validator read: the file itself, or an .xlsx workbook's text form."""
    if fmt_of(path) == 'xlsx':
        return xlsx_text(path)
    return path.read_text(encoding='utf-8', errors=errors)


def xlsx_records(path: Path, source: str) -> list[dict]:
    """Rows of each sheet as records under the sheet's first row (the header), with line numbers in xlsx_text."""
    out, line = [], 0
    for name, rows in xlsx_sheets(path):
        line += 1                                   # the [sheet] line
        head, head_line = None, None
        for r in rows:
            line += 1
            if head is None:
                if any(c.strip() for c in r):
                    head, head_line = [c.strip() for c in r], line
                continue
            if not any(c.strip() for c in r):
                continue
            out.append({'source': source, 'kind': 'row', 'hint': name, 'line': line, 'header_line': head_line,
                        'fields': {h: {'value': v, 'line': line} for h, v in zip(head, r) if h}})
    return out


# ---------------------------------------------------------------- HTML tables
def html_records(text: str, source: str) -> list[dict]:
    """Rows of each <table> as records under its first row (th or td cells), with the source line of each row."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tables: list[list[tuple[int, list[str]]]] = []
            self.row: list[str] | None = None
            self.cell: list[str] | None = None
            self.row_line = 0

        def handle_starttag(self, tag, attrs):
            if tag == 'table':
                self.tables.append([])
            elif tag == 'tr' and self.tables:
                self.row, self.row_line = [], self.getpos()[0]
            elif tag in ('td', 'th') and self.row is not None:
                self.cell = []

        def handle_endtag(self, tag):
            if tag in ('td', 'th') and self.cell is not None and self.row is not None:
                self.row.append(' '.join(''.join(self.cell).split()))
                self.cell = None
            elif tag == 'tr' and self.row is not None and self.tables:
                self.tables[-1].append((self.row_line, self.row))
                self.row = None

        def handle_data(self, data):
            if self.cell is not None:
                self.cell.append(data)

    p = P()
    try:
        p.feed(text)
        p.close()
    except Exception as exc:                  # html.parser is lenient; anything it still rejects is not guessed at
        raise NotAssessed(f'HTML could not be parsed ({exc.__class__.__name__})')
    out = []
    for t in p.tables:
        rows = [(ln, r) for ln, r in t if any(c for c in r)]
        if len(rows) < 2:
            continue
        head_line, head = rows[0]
        for ln, r in rows[1:]:
            out.append({'source': source, 'kind': 'row', 'hint': Path(source).stem, 'line': ln, 'header_line': head_line,
                        'fields': {h: {'value': v, 'line': ln} for h, v in zip(head, r) if h}})
    return out


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
        out.append({'source': source, 'kind': 'row', 'hint': Path(source).stem, 'line': i, 'header_line': 1,
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
    f = fmt_of(path)
    if f == 'xlsx':
        return xlsx_records(path, source)
    if f == 'xls':
        raise NotAssessed('binary .xls workbook (BIFF): not read')
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        raise NotAssessed('binary or not UTF-8 text')
    if f == 'html':
        return html_records(text, source)
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
    if '--text' in sys.argv[2:]:
        sys.stdout.write(text_of(p))       # the text form findings count lines in (an .xlsx workbook as CSV per sheet)
        sys.exit(0)
    try:
        for r in load_records(p, p.as_posix()):
            print(r['line'], r['hint'], {k: (v['line'], type(v['value']).__name__) for k, v in r['fields'].items()})
    except NotAssessed as exc:
        print('not assessed:', exc)
