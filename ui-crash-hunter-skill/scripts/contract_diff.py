#!/usr/bin/env python3
"""Contract diff: fields an API contract or model allows to be null or missing, against the places UI
code dereferences them without a guard. Stdlib only.

    python contract_diff.py <repo> [--contract openapi.json ...] [--out contract.json]

Contracts read: OpenAPI 3.x / Swagger 2 (JSON, or YAML: PyYAML if installed, otherwise a built-in
subset parser), GraphQL SDL, Prisma schemas (a `Type?` field is nullable; required columns and
relations are not), TypeScript interfaces and object types (not a route handler's private types),
Kotlin data classes, Swift structs and Dart classes. Without --contract, every contract file under
the repo is used.

An access site is `.field` followed by a dereference (`.x`, `[i]`, `(...)`) or a force unwrap
(`!`, `!!`). Matching is by field name, so every hit is a candidate for the agent to trace, not a
finding. A field name that is non-null in some other contract type can't be told apart by name, so
those sites are skipped and counted (`skipped.name_collision`; --include-collisions lists them, marked
`name_collision`), as are Prisma client delegates (`prisma.appointment.findMany()`). `guard_nearby`
marks a check on the same field in the lines just before. Route handlers are not scanned.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', 'coverage', 'vendor', 'Pods',
             'DerivedData', '.gradle', '.dart_tool', '__pycache__', '.expo', 'storybook-static', 'target', '.turbo',
             '.cache', '.venv', 'venv'}
ROUTE_HANDLER = re.compile(r'(^|/)(src/)?app/(.*/)?route\.(ts|js|mts|mjs|tsx|jsx)$|(^|/)(src/)?pages/api/')
AUTHORITATIVE = {'openapi', 'graphql', 'prisma'}
DB_CLIENT = re.compile(r'(prisma|db|tx|client|orm)$', re.I)
TEST_PART = re.compile(r'(^|/)(tests?|__tests__|__mocks__|e2e|cypress|playwright|androidTest|integration_test|\w*UITests?)(/|$)')
TEST_NAME = re.compile(r'(\.|_)(test|spec|stories)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|\.d\.ts$', re.I)
UI_EXT = {'.js', '.jsx', '.mjs', '.ts', '.tsx', '.vue', '.svelte', '.kt', '.java', '.swift', '.dart'}
PITFALL_BY_EXT = {'.kt': 'AND-NOT-NULL-ASSERTION', '.java': 'AND-NOT-NULL-ASSERTION', '.swift': 'IOS-FORCE-UNWRAP',
                  '.dart': 'FLT-NULL-CHECK'}
GENERIC = {'id', 'type', 'name', 'data', 'value', 'key', 'length', 'status', 'message', 'error', 'items', 'result'}

# ---------------------------------------------------------------- minimal YAML (OpenAPI subset)

def _strip_comment(s: str) -> str:
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in '"\'':
            q = ch
        elif ch == '#' and (i == 0 or s[i - 1] in ' \t'):
            return s[:i].rstrip()
    return s.rstrip()

def _split_flow(s: str) -> list[str]:
    parts, depth, q, cur = [], 0, None, ''
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in '"\'':
            q = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
            continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts

def _scalar(s: str):
    s = s.strip()
    if not s:
        return None
    if s[0] == '[' and s.endswith(']'):
        return [_scalar(p) for p in _split_flow(s[1:-1])]
    if s[0] == '{' and s.endswith('}'):
        out = {}
        for p in _split_flow(s[1:-1]):
            k, _, v = p.partition(':')
            out[_scalar(k)] = _scalar(v)
        return out
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        return s[1:-1]
    low = s.lower()
    if low in ('true', 'false'):
        return low == 'true'
    if low in ('null', '~'):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s

KEY = re.compile(r'''^(?:"([^"]*)"|'([^']*)'|([^:#\[\]{}][^:]*?))\s*:(?:\s+(.*)|$)''')

def mini_yaml(text: str):
    lines = []
    for raw in text.splitlines():
        if raw.strip() in ('---', '...') or not raw.strip() or raw.lstrip().startswith('#'):
            continue
        body = _strip_comment(raw)
        if body.strip():
            lines.append([len(body) - len(body.lstrip(' ')), body.strip()])

    def block(i: int, indent: int):
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith('- ') or lines[i][1] == '-':
            seq = []
            while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith('- ') or lines[i][1] == '-'):
                raw = lines[i][1]
                rest = raw[1:].strip()
                if not rest:
                    val, i = block(i + 1, lines[i + 1][0]) if i + 1 < len(lines) and lines[i + 1][0] > indent else (None, i + 1)
                elif KEY.match(rest):  # '- key: value' starts a mapping aligned with its first key
                    off = indent + len(raw) - len(raw[1:].lstrip())
                    lines[i] = [off, rest]
                    val, i = block(i, off)
                else:
                    val, i = _scalar(rest), i + 1
                seq.append(val)
            return seq, i
        out = {}
        while i < len(lines) and lines[i][0] == indent:
            m = KEY.match(lines[i][1])
            if not m:
                return out, i + 1
            key = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(3).strip()
            rest = (m.group(4) or '').strip()
            if rest[:1] in ('|', '>'):
                j = i + 1
                parts = []
                while j < len(lines) and lines[j][0] > indent:
                    parts.append(lines[j][1])
                    j += 1
                out[key], i = ' '.join(parts), j
            elif rest:
                out[key], i = _scalar(rest), i + 1
            elif i + 1 < len(lines) and (lines[i + 1][0] > indent or
                                         (lines[i + 1][0] == indent and lines[i + 1][1].startswith('- '))):
                out[key], i = block(i + 1, lines[i + 1][0])
            else:
                out[key], i = None, i + 1
        return out, i

    val, _ = block(0, lines[0][0]) if lines else (None, 0)
    return val

def load_structured(path: Path):
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix == '.json':
        return json.loads(text)
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return mini_yaml(text)

# ---------------------------------------------------------------- contract readers -> fields

def openapi_fields(doc: dict, ref: str) -> list[dict]:
    schemas = ((doc.get('components') or {}).get('schemas')) or doc.get('definitions') or {}
    out = []
    for tname, sch in schemas.items():
        if not isinstance(sch, dict):
            continue
        parts = [sch] + [s for s in (sch.get('allOf') or []) if isinstance(s, dict)]
        required = set()
        props = {}
        for p in parts:
            required |= set(p.get('required') or [])
            props.update(p.get('properties') or {})
        for fname, prop in props.items():
            prop = prop if isinstance(prop, dict) else {}
            types = prop.get('type') if isinstance(prop.get('type'), list) else [prop.get('type')]
            variants = (prop.get('oneOf') or []) + (prop.get('anyOf') or [])
            if prop.get('nullable') or prop.get('x-nullable') or 'null' in types or \
                    any(isinstance(v, dict) and v.get('type') == 'null' for v in variants):
                reason = 'nullable'
            elif fname not in required:
                reason = 'optional'
            else:
                out.append({'type': tname, 'field': fname, 'nullable': False, 'ref': f'{ref}#/{tname}/{fname}'})
                continue
            out.append({'type': tname, 'field': fname, 'nullable': True, 'reason': reason, 'ref': f'{ref}#/{tname}/{fname}'})
    return out

GQL_TYPE = re.compile(r'\b(?:type|interface)\s+(\w+)[^{]*\{([^}]*)\}', re.S)
GQL_FIELD = re.compile(r'^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*([\[\]\w!]+)', re.M)

def graphql_fields(text: str, ref: str) -> list[dict]:
    out = []
    for m in GQL_TYPE.finditer(text):
        for f in GQL_FIELD.finditer(m.group(2)):
            nullable = not f.group(2).endswith('!')
            out.append({'type': m.group(1), 'field': f.group(1), 'nullable': nullable,
                        **({'reason': 'nullable'} if nullable else {}), 'ref': f'{ref}#{m.group(1)}.{f.group(1)}'})
    return out

TS_DECL = re.compile(r'\b(?:interface\s+(\w+)[^{=]*|type\s+(\w+)\s*(?:<[^>]*>)?\s*=\s*)\{')
TS_FIELD = re.compile(r'^\s*(?:readonly\s+)?["\']?(\w+)["\']?\s*(\?)?\s*:\s*([^;,\n]+)')
MODEL_FIELD = {
    '.kt': re.compile(r'\b(?:val|var)\s+(\w+)\s*:\s*([\w<>.,\s]+?)(\?)?\s*(?:[=,)]|$)'),
    '.swift': re.compile(r'\b(?:let|var)\s+(\w+)\s*:\s*([\w<>\[\]\.:, ]+?)(\?)?\s*(?:[={]|$)'),
    '.dart': re.compile(r'^\s*(?:final\s+|late\s+)*([\w<>, ]+?)(\?)?\s+(\w+)\s*;'),
}
MODEL_DECL = {
    '.kt': re.compile(r'\bdata\s+class\s+(\w+)'),
    '.swift': re.compile(r'\bstruct\s+(\w+)\s*:[^{]*\b(Codable|Decodable)\b'),
    '.dart': re.compile(r'\bclass\s+(\w+)'),
}

def _body(text: str, start: int, open_ch: str = '{', close_ch: str = '}') -> tuple[str, int]:
    depth = 0
    for j in range(start, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1:j], j
    return text[start + 1:], len(text)

def _depth1_lines(body: str) -> list[str]:
    out, depth, cur = [], 0, ''
    for ch in body:
        if ch in '{(<[':
            depth += 1
        elif ch in '})>]':
            depth -= 1
        if ch in ';\n' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    return out

def typescript_fields(text: str, ref: str) -> list[dict]:
    out = []
    for m in TS_DECL.finditer(text):
        tname = m.group(1) or m.group(2)
        body, _ = _body(text, m.end() - 1)
        for line in _depth1_lines(body):
            f = TS_FIELD.match(line)
            if not f:
                continue
            ty = f.group(3).strip()
            nullable = bool(f.group(2)) or bool(re.search(r'\|\s*(null|undefined)\b|\b(null|undefined)\s*\|', ty))
            entry = {'type': tname, 'field': f.group(1), 'nullable': nullable, 'ref': f'{ref}#{tname}.{f.group(1)}'}
            if nullable:
                entry['reason'] = 'optional' if f.group(2) else 'nullable'
            out.append(entry)
    return out

PRISMA_MODEL = re.compile(r'^\s*(model|type)\s+(\w+)\s*\{', re.M)
PRISMA_FIELD = re.compile(r'^\s*(\w+)\s+(\w+)(\[\])?(\?)?(?=\s|$)')

def prisma_fields(text: str, ref: str) -> list[dict]:
    """Prisma schema models: a field is nullable when its type ends in '?'. Required relations and
    columns are non-null: the database (and the generated client's types) guarantee them."""
    out = []
    for m in PRISMA_MODEL.finditer(text):
        body, _ = _body(text, m.end() - 1)
        for line in body.splitlines():
            s = line.strip()
            if not s or s.startswith(('//', '@@')):
                continue
            f = PRISMA_FIELD.match(line)
            if not f:
                continue
            nullable = bool(f.group(4))
            out.append({'type': m.group(2), 'field': f.group(1), 'nullable': nullable, 'list': bool(f.group(3)),
                        **({'reason': 'nullable'} if nullable else {}), 'ref': f'{ref}#{m.group(2)}.{f.group(1)}'})
    return out

def model_fields(text: str, ref: str, ext: str) -> list[dict]:
    out = []
    for m in MODEL_DECL[ext].finditer(text):
        open_ch, close_ch = ('(', ')') if ext == '.kt' and text[m.end():].lstrip().startswith('(') else ('{', '}')
        start = text.index(open_ch, m.end()) if open_ch in text[m.end():] else None
        if start is None:
            continue
        body, _ = _body(text, start, open_ch, close_ch)
        for line in _depth1_lines(body) if ext != '.kt' else body.split(','):
            f = MODEL_FIELD[ext].search(line)
            if not f:
                continue
            name, q = (f.group(3), f.group(2)) if ext == '.dart' else (f.group(1), f.group(3))
            out.append({'type': m.group(1), 'field': name, 'nullable': bool(q),
                        **({'reason': 'nullable'} if q else {}), 'ref': f'{ref}#{m.group(1)}.{name}'})
    return out

# ---------------------------------------------------------------- repo walk

def walk(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith('.'):
                    stack.append(p)
            else:
                yield p

def is_bundle(p: Path) -> bool:
    """Built output (a sibling .map file, or a very large script) is not source to analyse."""
    try:
        return p.with_name(p.name + '.map').exists() or (p.suffix in ('.js', '.mjs', '.cjs') and p.stat().st_size > 400_000)
    except OSError:
        return False

def find_contracts(root: Path) -> list[tuple[str, Path]]:
    found = []
    for p in walk(root):
        rel = p.relative_to(root).as_posix()
        if TEST_PART.search(rel):
            continue
        if p.suffix in ('.graphql', '.gql'):
            found.append(('graphql', p))
        elif p.suffix == '.prisma':
            found.append(('prisma', p))
        elif ROUTE_HANDLER.search(rel):
            continue  # a route handler's own row types are private to the server, not a contract the UI reads
        elif p.suffix in ('.json', '.yaml', '.yml') and p.name not in ('package.json', 'package-lock.json', 'tsconfig.json'):
            try:
                head = p.read_text(encoding='utf-8', errors='replace')[:4000]
            except OSError:
                continue
            if re.search(r'["\']?(openapi|swagger)["\']?\s*:\s*["\']?\d', head):
                found.append(('openapi', p))
        elif p.suffix in ('.ts', '.tsx') and not TEST_NAME.search(p.name) and re.search(r'(^|/)(types?|models?|api|schemas?|generated|__generated__|gql)(/|\.|$)', rel, re.I):
            found.append(('typescript', p))
        elif p.suffix in ('.kt', '.swift', '.dart') and re.search(r'(^|/)(models?|dto|data|entities|api)(/|$)', rel, re.I):
            found.append(('model', p))
    return found

def read_contract(kind: str, path: Path, root: Path) -> list[dict]:
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    if kind == 'openapi':
        return openapi_fields(load_structured(path) or {}, rel)
    text = path.read_text(encoding='utf-8', errors='replace')
    if kind == 'graphql':
        return graphql_fields(text, rel)
    if kind == 'prisma':
        return prisma_fields(text, rel)
    if kind == 'typescript':
        return typescript_fields(text, rel)
    return model_fields(text, rel, path.suffix)

def guard_nearby(lines: list[str], i: int, field: str, col: int) -> bool:
    same = lines[i][:col + len(field) + 40]
    window = '\n'.join(lines[max(0, i - 4):i]) + '\n' + same
    f = re.escape(field)
    return bool(re.search(rf'\.{f}\s*(&&|\?\?|\?(?!\.)|!==?\s*(null|undefined)|==\s*null)|if\s*\(\s*!?[\w.]*\.{f}\b|guard\s+let\s+\w+\s*=\s*[\w.]*\.{f}\b|'
                          rf'if\s+let\s+\w+\s*=\s*[\w.]*\.{f}\b|\.{f}\s*\?\.', window))

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):  # JSON on stdout may hold any character; a cp1252 console can't print it
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--contract', nargs='*', type=Path, default=[], help='contract files (default: discover)')
    ap.add_argument('--include-generic', action='store_true', help=f'also match generic field names ({", ".join(sorted(GENERIC))})')
    ap.add_argument('--include-collisions', action='store_true',
                    help='also list access sites whose field name is non-null in another contract type')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    contracts = []
    for c in args.contract:
        p = c if c.is_absolute() else (Path.cwd() / c)
        kind = 'graphql' if p.suffix in ('.graphql', '.gql') else 'openapi' if p.suffix in ('.json', '.yaml', '.yml') \
            else 'prisma' if p.suffix == '.prisma' else 'typescript' if p.suffix in ('.ts', '.tsx') else 'model'
        contracts.append((kind, p.resolve()))
    if not contracts:
        contracts = find_contracts(root)
    fields, read_errors = [], []
    for kind, path in contracts:
        try:
            fields.extend({**f, 'contract_kind': kind} for f in read_contract(kind, path, root))
        except (OSError, ValueError, AttributeError) as exc:
            read_errors.append({'file': str(path), 'error': str(exc)})
    nullable = {}
    non_null_names = {f['field'] for f in fields if not f['nullable']}
    for f in fields:
        if f['nullable'] and (args.include_generic or f['field'] not in GENERIC):
            nullable.setdefault(f['field'], []).append(f)
    contract_files = {str(p.resolve()) for _, p in contracts}
    # Prisma client delegates: prisma.appointment.findMany() is a model accessor, not a field read
    delegates = {f['type'][:1].lower() + f['type'][1:] for f in fields if f.get('contract_kind') == 'prisma'}
    cands, skipped = [], {'name_collision': {}, 'db_client_delegate': 0}
    if nullable:
        names = '|'.join(sorted((re.escape(n) for n in nullable), key=len, reverse=True))
        access = re.compile(rf'(?:\?\.|\.)({names})\b(?:\s*(?:\.(?![.?])|\[|\()|(!!|!)(?!=))')
        for p in walk(root):
            rel = p.relative_to(root).as_posix()
            if p.suffix not in UI_EXT or is_bundle(p) or TEST_NAME.search(p.name) or TEST_PART.search(rel) or \
                    ROUTE_HANDLER.search(rel) or str(p.resolve()) in contract_files:
                continue
            try:
                lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if re.match(r'^\s*(//|/\*|\*|#)', line):
                    continue
                for m in access.finditer(line):
                    field = m.group(1)
                    src = nullable[field]
                    recv = re.search(r'([\w$]+)\??$', line[:m.start()])
                    if field in delegates and recv and DB_CLIENT.search(recv.group(1)):
                        skipped['db_client_delegate'] += 1
                        continue
                    if field in non_null_names and not args.include_collisions:
                        # the same name is non-null in another type: matching by name can't tell which one this is
                        skipped['name_collision'][field] = skipped['name_collision'].get(field, 0) + 1
                        continue
                    cands.append({
                        'pitfall_id': PITFALL_BY_EXT.get(p.suffix, 'WEB-UNGUARDED-OPTIONAL'),
                        'file': rel, 'line': i + 1, 'column': m.start() + 1, 'snippet': line.strip()[:200],
                        'message': f"'{field}' is {src[0]['reason']} in {src[0]['ref']} and is dereferenced without a guard",
                        'field': field, 'schema_ref': [s['ref'] for s in src], 'nullable_reason': src[0]['reason'],
                        'name_collision': field in non_null_names, 'guard_nearby': guard_nearby(lines, i, field, m.start()),
                        'sources': [{'tool': 'contract_diff', 'rule_id': 'nullable-access'}],
                    })
    out = {
        'tool': 'contract_diff', 'status': 'ran' if contracts else 'not_applicable',
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'contracts': [{'kind': k, 'file': p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)} for k, p in contracts],
        'read_errors': read_errors,
        'nullable_fields': [f for fs in nullable.values() for f in fs],
        'skipped': {'name_collision': sum(skipped['name_collision'].values()),
                    'name_collision_fields': dict(sorted(skipped['name_collision'].items(), key=lambda kv: -kv[1])[:30]),
                    'db_client_delegate': skipped['db_client_delegate'],
                    'note': 'Access sites not listed as candidates: the field name is non-null in another contract type '
                            '(rerun with --include-collisions to list them), or the access is a database client delegate '
                            '(prisma.model.findMany()).'},
        'candidates': cands,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"{len(contracts)} contract(s), {len(out['nullable_fields'])} nullable field(s), {len(cands)} unguarded access site(s) "
              f"({sum(1 for c in cands if c['guard_nearby'])} with a guard nearby) -> {args.out}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
