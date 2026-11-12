#!/usr/bin/env python3
"""SQL statement shapes shared by detection and scanning (stdlib): identifiers, clauses, column types, Prisma schemas.

    python sqlshape.py --norm 'ALTER TABLE "Order" ADD COLUMN "note" TEXT, ADD COLUMN "tag" TEXT'
    python sqlshape.py --prisma prisma/schema.prisma

- ident('public."Order"') -> 'Order': quotes, backticks and brackets stripped, the default schema dropped.
- norm_sql(sql): every quoted identifier unquoted, and a space put between a name and its column list
  ('"Order"("id")' -> 'Order (id)'), so the rules match Prisma's and hand-written PostgreSQL alike.
- clauses(sql): ALTER TABLE t a, b, c -> ['ALTER TABLE t a', 'ALTER TABLE t b', ...] (top-level commas only).
- column_types(create_sql): {column: type} from a CREATE TABLE.
- prisma_schema(text): models with their table (@@map), fields with their column (@map), SQL type, default
  and line, read from schema.prisma.
"""
from __future__ import annotations
import json, re, sys

DEFAULT_SCHEMAS = ('public', 'dbo')
_QUOTED = re.compile(r'"((?:[^"]|"")*)"|`([^`]*)`|\[([^\]]*)\]')


def ident(name: str | None) -> str | None:
    """A table or column name without quotes, a trailing column list or the default schema."""
    if name is None:
        return None
    s = name.strip().rstrip(',;')
    s = re.sub(r'\(.*$', '', s) if not s.startswith('(') else s
    parts = [p.group(1) if p.group(1) is not None else p.group(2) if p.group(2) is not None else p.group(3) if p.group(3) is not None else p.group(0)
             for p in re.finditer(r'"((?:[^"]|"")*)"|`([^`]*)`|\[([^\]]*)\]|[^.]+', s)]
    parts = [p.replace('""', '"') for p in parts if p != '.']
    if len(parts) > 1 and parts[0].lower() in DEFAULT_SCHEMAS:
        parts = parts[1:]
    return '.'.join(parts)


def norm_sql(sql: str) -> str:
    """The statement with quoted identifiers unquoted (string literals untouched) and 'name(' spaced to 'name ('."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and (j + 1 >= n or sql[j + 1] != "'")):
                j += 2 if sql[j] == "'" else 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        m = _QUOTED.match(sql, i) if ch in '"`[' else None
        if m:
            name = next(g for g in m.groups() if g is not None).replace('""', '"')
            out.append(name if re.fullmatch(r'[A-Za-z_][\w$]*', name) else '"' + name + '"')
            i = m.end()
            continue
        out.append(ch)
        i += 1
    s = ''.join(out)
    # 'ON Order(id)' / 'REFERENCES Account(id)' -> with a space, so (\S+) stops at the name
    return re.sub(r'\b(ON|REFERENCES|INTO|TABLE|EXISTS)\s+([\w.$]+)\(', r'\1 \2 (', s, flags=re.I)


def top_level_split(text: str, sep: str = ',') -> list[str]:
    parts, depth, buf, q = [], 0, [], None
    for ch in text:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in '\'"`':
            q = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if ''.join(buf).strip():
        parts.append(''.join(buf).strip())
    return parts


def clauses(sql: str) -> list[str]:
    """One statement per clause of a multi-clause ALTER TABLE (normalised SQL in, normalised SQL out)."""
    m = re.match(r'(ALTER TABLE (?:ONLY )?(?:IF EXISTS )?\S+)\s+(.*)$', sql, re.I | re.S)
    if not m or re.search(r'\bRENAME\b', m.group(2)[:20], re.I):
        return [sql]
    parts = top_level_split(m.group(2))
    return [f'{m.group(1)} {p}' for p in parts] if len(parts) > 1 else [sql]


_STOP = r'(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY|REFERENCES|UNIQUE|CHECK|CONSTRAINT|COLLATE|GENERATED|USING|ON\s+UPDATE|AUTO_INCREMENT|COMMENT|IDENTITY)\b'


def type_of(rest: str) -> str:
    """The SQL type at the start of a column definition ('DECIMAL(12,2) NOT NULL DEFAULT 0' -> 'DECIMAL(12,2)')."""
    m = re.match(rf'\s*(.+?)(?=\s+{_STOP}|\s*/\*|$)', rest, re.I | re.S)
    return ' '.join((m.group(1) if m else rest).split()).upper()


_ALIASES = [(r'\bCHARACTER VARYING\b', 'VARCHAR'), (r'\bDECIMAL\b', 'NUMERIC'), (r'\bINT4\b|\bINT\b(?!E)', 'INTEGER'), (r'\bINT8\b', 'BIGINT'),
            (r'\bINT2\b', 'SMALLINT'), (r'\bFLOAT8\b|\bFLOAT\b(?!\s*\()', 'DOUBLE PRECISION'), (r'\bFLOAT4\b', 'REAL'),
            (r'\s+WITHOUT TIME ZONE\b', ''), (r'\bTIMESTAMP(\(\d+\))? WITH TIME ZONE\b', r'TIMESTAMPTZ\1'), (r'\bBOOL\b', 'BOOLEAN')]


def canon_type(t: str | None) -> str | None:
    """A type in one spelling, so equal types compare equal ('decimal(12, 2)' and 'NUMERIC(12,2)')."""
    if t is None:
        return None
    s = ' '.join(t.upper().split())
    s = re.sub(r'\s*\(\s*', '(', re.sub(r'\s*,\s*', ',', re.sub(r'\s*\)', ')', s)))
    for pat, rep in _ALIASES:
        s = re.sub(pat, rep, s)
    return s.strip()


def column_types(create_sql: str) -> dict[str, str]:
    m = re.match(r'CREATE (?:UNLOGGED |TEMP(?:ORARY)? )?TABLE (?:IF NOT EXISTS )?\S+\s*\((.*)\)\s*[^)]*$', create_sql, re.I | re.S)
    if not m:
        return {}
    out = {}
    for item in top_level_split(m.group(1)):
        w = item.split(None, 1)
        if len(w) < 2 or w[0].upper() in ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'EXCLUDE', 'KEY', 'INDEX', 'LIKE'):
            continue
        out[ident(w[0])] = type_of(w[1])
    return out


PRISMA_SCALARS = {'Int': 'INTEGER', 'BigInt': 'BIGINT', 'Float': 'DOUBLE PRECISION', 'Decimal': 'DECIMAL(65,30)', 'String': 'TEXT',
                  'Boolean': 'BOOLEAN', 'DateTime': 'TIMESTAMP(3)', 'Json': 'JSONB', 'Bytes': 'BYTEA'}
PRISMA_NATIVE = {'DoublePrecision': 'DOUBLE PRECISION', 'Real': 'REAL', 'SmallInt': 'SMALLINT', 'Integer': 'INTEGER', 'BigInt': 'BIGINT',
                 'Text': 'TEXT', 'Uuid': 'UUID', 'Timestamptz': 'TIMESTAMPTZ', 'Timestamp': 'TIMESTAMP', 'Date': 'DATE', 'Money': 'MONEY',
                 'Decimal': 'DECIMAL', 'VarChar': 'VARCHAR', 'Char': 'CHAR', 'JsonB': 'JSONB', 'Json': 'JSON', 'Boolean': 'BOOLEAN'}


def prisma_schema(text: str) -> dict:
    """{'provider': ..., 'models': {Model: {'table', 'line', 'fields': {field: {'column', 'type', 'optional', 'default', 'line'}}}}}."""
    provider = (re.search(r'datasource\s+\w+\s*\{[^}]*?provider\s*=\s*"(\w+)"', text, re.S) or [None, None])[1]
    models, cur, lines = {}, None, text.splitlines()
    enums = set(re.findall(r'^\s*enum\s+(\w+)', text, re.M))
    for n, raw in enumerate(lines, 1):
        line = raw.split('//')[0].strip()
        m = re.match(r'model\s+(\w+)\s*\{', line)
        if m:
            cur = {'table': m.group(1), 'line': n, 'fields': {}}
            models[m.group(1)] = cur
            continue
        if cur is None:
            continue
        if line.startswith('}'):
            cur = None
            continue
        m = re.match(r'@@map\(\s*(?:name:\s*)?"([^"]+)"', line)
        if m:
            cur['table'] = m.group(1)
            continue
        m = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)$', line)
        if not m or line.startswith('@@'):
            continue
        name, typ, lst, opt, attrs = m.groups()
        if typ not in PRISMA_SCALARS and typ not in enums:
            rel = re.search(r'@relation\([^)]*fields:\s*\[([^\]]+)\]', attrs)
            for scalar in (rel.group(1).split(',') if rel else []):
                cur.setdefault('relations', {})[scalar.strip()] = name
            continue  # a relation field: no column of its own
        native = re.search(r'@db\.(\w+)(?:\(([^)]*)\))?', attrs)
        if native:
            sql = PRISMA_NATIVE.get(native.group(1), native.group(1).upper()) + (f'({native.group(2).replace(" ", "")})' if native.group(2) else '')
        else:
            sql = PRISMA_SCALARS.get(typ, 'TEXT' if typ not in enums else f'"{typ}"')
        sql += '[]' if lst else ''
        col = re.search(r'@map\(\s*(?:name:\s*)?"([^"]+)"', attrs)
        cur['fields'][name] = {'column': col.group(1) if col else name, 'type': sql, 'optional': bool(opt), 'line': n,
                               'default': bool(re.search(r'@default\(|@updatedAt\b', attrs)), 'id': '@id' in attrs}
    return {'provider': provider, 'models': models}


def prisma_columns(schema: dict) -> dict:
    """{table: {column: {'type', 'field', 'model', 'line', 'default', 'optional'}}} from prisma_schema()."""
    out = {}
    for model, m in schema.get('models', {}).items():
        for field, f in m['fields'].items():
            out.setdefault(m['table'], {})[f['column']] = dict(f, field=field, model=model, relation=(m.get('relations') or {}).get(field))
    return out


if __name__ == '__main__':
    if sys.argv[1] == '--norm':
        s = norm_sql(sys.argv[2])
        print(json.dumps({'norm': s, 'clauses': clauses(s)}, indent=1))
    elif sys.argv[1] == '--prisma':
        print(json.dumps(prisma_schema(open(sys.argv[2], encoding='utf-8').read()), indent=1))
