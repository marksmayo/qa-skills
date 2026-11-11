#!/usr/bin/env python3
"""Plan the operations and generate test cases from a loaded contract (stdlib; no Schemathesis needed).

    python gen_cases.py contract.json --out cases.json [--seed N] [--max-cases 50]
        [--include ID|tag:T|path:/prefix ...] [--exclude ...] [--allow-writes] [--auth-configured]

Per operation, in order:
  example   every parameter and body from documented examples (required ones synthesised only if
            the contract has none; the case is then kind `valid`, not `example`)
  boundary  each bound and enum value, at the edge but inside it
  negative  one violation at a time: below minimum, above maximum, too short or long, wrong type,
            bad format, value outside the enum, pattern mismatch, a missing required parameter or
            field, an unexpected property, and a body that isn't an object
  random    a seeded sample of valid values (fields with a pattern use their example)
  no_auth   the example without credentials, for operations with a security requirement
  added     cases from --add-cases files (for example, one reproducing a Schemathesis failure)
GraphQL: each query field with every enum value and a few values per scalar argument, selecting
every scalar field (and one level of nested objects), plus a query missing each required argument.

The plan lists every operation as planned, excluded, skipped_write (a write method without
--allow-writes) or skipped_auth (security required but --auth-configured not given: only the
no_auth case runs). The plan is the coverage denominator.
"""
from __future__ import annotations
import argparse, copy, datetime as dt, json, random, re, string, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonschema_lite as jl  # noqa: E402

FIXED_DATE = '2026-01-15'


def resolve(schema, root):
    seen = 0
    while isinstance(schema, dict) and '$ref' in schema and seen < 20:
        schema = jl.resolve(schema['$ref'], root)
        seen += 1
    if isinstance(schema, dict) and schema.get('allOf'):
        merged = {k: v for k, v in schema.items() if k != 'allOf'}
        for sub in schema['allOf']:
            sub = resolve(sub, root)
            for k, v in sub.items():
                if k == 'properties':
                    merged.setdefault('properties', {}).update(v)
                elif k == 'required':
                    merged['required'] = sorted(set(merged.get('required', [])) | set(v))
                else:
                    merged.setdefault(k, v)
        return merged
    return schema or {}


def stype(s: dict) -> str:
    t = s.get('type')
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), 'null')
    if t:
        return t
    if 'properties' in s:
        return 'object'
    if 'items' in s:
        return 'array'
    if 'enum' in s and s['enum']:
        return {str: 'string', int: 'integer', float: 'number', bool: 'boolean'}.get(type(s['enum'][0]), 'string')
    return 'string'


def valid_value(s, root, rng: random.Random | None = None, depth: int = 0):
    """A value that satisfies the schema: the example, enum or default first, else synthesised."""
    s = resolve(s, root)
    for key in ('example', 'default'):
        if key in s and not jl.errors(s[key], s, root):
            return copy.deepcopy(s[key])
    if s.get('examples') and isinstance(s['examples'], list) and not jl.errors(s['examples'][0], s, root):
        return copy.deepcopy(s['examples'][0])
    if 'const' in s:
        return s['const']
    if s.get('enum'):
        return rng.choice(s['enum']) if rng else s['enum'][0]
    for key in ('oneOf', 'anyOf'):
        if s.get(key):
            return valid_value(s[key][0], root, rng, depth + 1)
    t = stype(s)
    if t == 'integer' or t == 'number':
        lo = s.get('minimum', s['exclusiveMinimum'] + 1 if isinstance(s.get('exclusiveMinimum'), (int, float)) and not isinstance(s.get('exclusiveMinimum'), bool) else None)
        hi = s.get('maximum')
        lo = 1 if lo is None else lo
        hi = lo + 999 if hi is None else hi
        v = rng.randint(int(lo), int(hi)) if rng else int(lo)
        return v if t == 'integer' else float(v)
    if t == 'boolean':
        return rng.choice([True, False]) if rng else True
    if t == 'array':
        n = max(s.get('minItems', 1 if rng is None else rng.randint(0, 2)), 0)
        return [valid_value(s.get('items') or {}, root, rng, depth + 1) for _ in range(n)] if depth < 4 else []
    if t == 'object':
        props = s.get('properties') or {}
        req = s.get('required') or []
        keys = [k for k in props if k in req or (rng and rng.random() < 0.5)]
        return {k: valid_value(props[k], root, rng, depth + 1) for k in keys if depth < 4}
    if t == 'null':
        return None
    fmt = s.get('format')
    if fmt == 'date':
        return (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 364))).isoformat() if rng else FIXED_DATE
    if fmt == 'date-time':
        return (FIXED_DATE if not rng else (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 364))).isoformat()) + 'T09:00:00Z'
    if fmt == 'email':
        return 'test@example.invalid'
    if fmt == 'uuid':
        return '00000000-0000-4000-8000-000000000001'
    if fmt in ('uri', 'url'):
        return 'https://example.invalid/'
    if s.get('pattern'):
        return None  # can't synthesise from a pattern; the caller skips the field
    lo, hi = s.get('minLength', 1), s.get('maxLength')
    hi = min(hi, max(lo, 12)) if hi is not None else max(lo, 12)
    n = rng.randint(lo, hi) if rng else max(lo, min(4, hi))
    return ''.join((rng or random.Random(0)).choice(string.ascii_lowercase) for _ in range(n)) if rng else 'test'[:n].ljust(n, 'x')


def wrong_type_value(s):
    t = stype(s)
    return {'integer': 'abc', 'number': 'abc', 'boolean': 'maybe', 'array': 'not-an-array', 'object': 'not-an-object'}.get(t, 12345)


def negatives(s, root):
    """(description, value) pairs that each violate one keyword of the schema."""
    s = resolve(s, root)
    t = stype(s)
    out = []
    if t in ('integer', 'number'):
        if 'minimum' in s:
            out.append((f'below minimum {s["minimum"]}', s['minimum'] - 1))
        if 'maximum' in s:
            out.append((f'above maximum {s["maximum"]}', s['maximum'] + 1))
        out.append(('wrong type', wrong_type_value(s)))
        if t == 'integer':
            out.append(('not an integer', 1.5))
    elif t == 'string':
        if s.get('enum'):
            out.append(('outside the enum', 'zzz-not-in-enum'))
        if s.get('minLength', 0) > 0:
            out.append((f'shorter than minLength {s["minLength"]}', 'x' * (s['minLength'] - 1)))
        if 'maxLength' in s:
            out.append((f'longer than maxLength {s["maxLength"]}', 'x' * (s['maxLength'] + 1)))
        if s.get('format') in ('date', 'date-time'):
            out.append((f'not a valid {s["format"]}', '11/11/2026'))
        if s.get('format') in ('email', 'uuid', 'uri'):
            out.append((f'not a valid {s["format"]}', 'not-a-' + s['format']))
        if s.get('pattern'):
            out.append((f'does not match {s["pattern"]}', '!!'))
    elif t == 'boolean':
        out.append(('wrong type', 'maybe'))
    return [(d, v) for d, v in out if jl.errors(v, s, root)]


def as_param(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (list, tuple)):
        return ','.join(as_param(x) for x in v)
    return str(v)


def json_media(body: dict | None):
    if not body:
        return None, None
    for mt, c in body['content'].items():
        if 'json' in mt:
            return mt, c
    mt = next(iter(body['content']), None)
    return mt, body['content'].get(mt)


class Builder:
    def __init__(self, op: dict, root: dict, rng: random.Random, dialect: str = '3.1'):
        self.op, self.root, self.rng, self.dialect = op, root, rng, dialect
        self.cases: list[dict] = []
        self.dropped = 0

    def is_valid(self, params: dict, body) -> bool:
        for p in self.op['parameters']:
            if p['in'] in params and p['name'] in params[p['in']]:
                if jl.errors(params[p['in']][p['name']], p['schema'], self.root, self.dialect):
                    return False
            elif p['required'] and p['in'] in ('path', 'query', 'header'):
                return False
        mt, c = json_media(self.op['request_body'])
        if c is not None and (body is not None or self.op['request_body'].get('required')):
            return not jl.errors(body, c['schema'] or {}, self.root, self.dialect)
        return True

    def add(self, kind: str, valid: bool, desc: str, params: dict, body=None, auth: bool = True, has_body: bool = True):
        if valid and kind in ('random', 'boundary', 'valid') and not self.is_valid(params, body if has_body else None):
            self.dropped += 1   # never send a case labelled valid that the contract says is invalid
            return
        p = {loc: {k: as_param(v) for k, v in params.get(loc, {}).items()} for loc in ('path', 'query', 'header')}
        mt, _ = json_media(self.op['request_body'])
        req = {'method': self.op['method'], 'path_params': p['path'], 'query': p['query'], 'headers': p['header']}
        if self.op['request_body'] and has_body:
            req['media_type'] = mt
            req['body'] = body
        self.cases.append({'operation_id': self.op['operation_id'], 'kind': kind, 'valid': valid, 'description': desc,
                           'auth': auth, 'request': req})

    def example_params(self, rng=None) -> tuple[dict, bool]:
        out, from_examples = {'path': {}, 'query': {}, 'header': {}}, True
        for p in self.op['parameters']:
            if p['in'] not in out:
                continue
            if p['examples'] and not rng:
                out[p['in']][p['name']] = p['examples'][0]
            elif p['required'] or (rng and rng.random() < 0.5):
                v = valid_value(p['schema'], self.root, rng)
                if v is None and p['examples']:
                    v = p['examples'][0]
                if v is None:
                    continue
                out[p['in']][p['name']] = v
                from_examples = from_examples and not p['required'] if not rng else False
        return out, from_examples

    def example_body(self, rng=None):
        mt, c = json_media(self.op['request_body'])
        if not c:
            return None, True
        if c['examples'] and not rng:
            return copy.deepcopy(c['examples'][0]), True
        s = resolve(c['schema'] or {}, self.root)
        body = valid_value(s, self.root, rng)
        if isinstance(body, dict):
            documented = c['examples'][0] if c['examples'] and isinstance(c['examples'][0], dict) else {}
            props = s.get('properties') or {}
            for k in list(props):
                if body.get(k) is None and k in (s.get('required') or []):
                    ex = documented.get(k, resolve(props[k], self.root).get('example'))
                    if ex is not None:
                        body[k] = ex
            body = {k: v for k, v in body.items() if v is not None}
        return body, False

    def build(self, max_cases: int, auth_ok: bool):
        params, ex_ok = self.example_params()
        body, body_ok = self.example_body()
        kind = 'example' if ex_ok and body_ok else 'valid'
        if auth_ok or not self.op['security']:
            self.add(kind, True, 'documented examples' if kind == 'example' else 'valid values (some synthesised)', params, body)
            self.boundaries(params, body)
            self.negative_cases(params, body)
            has_inputs = bool(self.op['parameters'] or self.op['request_body'])
            n_random = max(0, min(10, max_cases - len(self.cases) - (1 if self.op['security'] else 0))) if has_inputs else 0
            for i in range(n_random):
                rp, _ = self.example_params(self.rng)
                rb, _ = self.example_body(self.rng)
                self.add('random', True, f'seeded random sample {i + 1}', rp, rb)
        if self.op['security']:
            self.add('no_auth', True, 'the example without credentials', params, body, auth=False)
        if len(self.cases) > max_cases:
            keep = [c for c in self.cases if c['kind'] in ('example', 'valid', 'no_auth')]
            rest = [c for c in self.cases if c not in keep]
            self.cases = keep + rest[:max_cases - len(keep)]
        return self.cases

    def boundaries(self, params, body):
        for p in self.op['parameters']:
            s = resolve(p['schema'], self.root)
            vals = []
            if s.get('enum'):
                vals = [(f'enum value {v!r}', v) for v in s['enum']]
            elif stype(s) in ('integer', 'number'):
                vals = [(f'minimum {s[k]}' if k == 'minimum' else f'maximum {s[k]}', s[k]) for k in ('minimum', 'maximum') if k in s]
            elif stype(s) == 'string':
                vals = [(f'length {s[k]}', 'x' * s[k]) for k in ('minLength', 'maxLength') if k in s and s[k] > 0 and not s.get('format')]
            for d, v in vals:
                if p['in'] in ('path', 'query', 'header') and json.dumps(v) != json.dumps(params[p['in']].get(p['name'])):
                    q = copy.deepcopy(params)
                    q[p['in']][p['name']] = v
                    self.add('boundary', True, f'{p["name"]}: {d}', q, body)
        if isinstance(body, dict):
            s = resolve(json_media(self.op['request_body'])[1]['schema'] or {}, self.root)
            for k, ps in (s.get('properties') or {}).items():
                ps = resolve(ps, self.root)
                for lim in ('minLength', 'maxLength'):
                    if lim in ps and ps[lim] > 0 and not ps.get('pattern') and not ps.get('format'):
                        b = dict(body, **{k: 'Test ' + 'x' * (ps[lim] - 5) if ps[lim] > 5 else 'x' * ps[lim]})
                        self.add('boundary', True, f'body.{k}: length {ps[lim]}', params, b)

    def negative_cases(self, params, body):
        for p in self.op['parameters']:
            if p['in'] not in ('path', 'query', 'header'):
                continue
            for d, v in negatives(p['schema'], self.root):
                if p['in'] in ('query', 'path', 'header') and isinstance(v, (int, float)) and stype(resolve(p['schema'], self.root)) == 'string':
                    continue  # every query value is a string on the wire; a number isn't a type error there
                q = copy.deepcopy(params)
                q[p['in']][p['name']] = v
                self.add('negative', False, f'{p["name"]}: {d}', q, body)
            if p['required'] and p['in'] in ('query', 'header'):
                q = copy.deepcopy(params)
                q[p['in']].pop(p['name'], None)
                self.add('negative', False, f'missing required {p["in"]} parameter {p["name"]}', q, body)
        mt, c = json_media(self.op['request_body'])
        if c is None:
            return
        s = resolve(c['schema'] or {}, self.root)
        if isinstance(body, dict):
            for k in s.get('required') or []:
                b = {kk: vv for kk, vv in body.items() if kk != k}
                self.add('negative', False, f'body: missing required {k!r}', params, b)
            for k, ps in (s.get('properties') or {}).items():
                for d, v in negatives(ps, self.root):
                    self.add('negative', False, f'body.{k}: {d}', params, dict(body, **{k: v}))
            if s.get('additionalProperties') is False:
                self.add('negative', False, 'body: unexpected property', params, dict(body, unexpected_field='x'))
        if stype(s) == 'object':
            self.add('negative', False, 'body: not an object', params, ['not', 'an', 'object'])
        if self.op['request_body'].get('required'):
            self.add('negative', False, 'body: missing', params, None, has_body=False)


# --- GraphQL ---------------------------------------------------------------

GQL_VALUES = {'ID': ['1', '2', '3', '999'], 'Int': [0, 1, 100], 'Float': [0.0, 1.5], 'String': ['test', ''], 'Boolean': [True, False]}


def base_type(ref: str) -> str:
    return re.sub(r'[\[\]!]', '', ref)


def gql_literal(v, tname: str, sdl: dict) -> str:
    if (sdl['types'].get(tname) or {}).get('kind') == 'ENUM':
        return str(v)
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, dict):
        return '{' + ', '.join(f'{k}: {gql_literal(x, "String", sdl)}' for k, x in v.items()) + '}'
    return json.dumps(v)


def gql_selection(tname: str, sdl: dict, depth: int = 0) -> str:
    t = sdl['types'].get(tname) or {}
    if t.get('kind') not in ('OBJECT', 'INTERFACE'):
        return ''
    parts = []
    for fname, f in t['fields'].items():
        if any(r.endswith('!') for r in f['args'].values()):
            continue
        bt = base_type(f['type'])
        kind = (sdl['types'].get(bt) or {}).get('kind', 'SCALAR' if bt in GQL_VALUES else None)
        if kind in ('SCALAR', 'ENUM'):
            parts.append(fname)
        elif kind in ('OBJECT', 'INTERFACE') and depth < 1:
            sub = gql_selection(bt, sdl, depth + 1)
            if sub:
                parts.append(f'{fname} {sub}')
    return '{ ' + ' '.join(parts) + ' }' if parts else ''


def gql_cases(op: dict, sdl: dict, max_cases: int) -> list[dict]:
    rt = base_type(op['returns'])
    sel = gql_selection(rt, sdl)
    arg_values = []
    for a in op['parameters']:
        bt = base_type(a['type'])
        t = sdl['types'].get(bt) or {}
        vals = t.get('values') if t.get('kind') == 'ENUM' else GQL_VALUES.get(bt, ['test'])
        if not a['required']:
            vals = [None] + list(vals)
        arg_values.append((a['name'], bt, vals))
    combos = [{}]
    for name, bt, vals in arg_values:
        combos = [dict(c, **({name: (bt, v)} if v is not None else {})) for c in combos for v in vals]
    kw = 'mutation ' if op['method'] == 'MUTATION' else ''   # so send_cases.py treats it as a write
    cases = []
    for c in combos[:max_cases]:
        args = ', '.join(f'{k}: {gql_literal(v, bt, sdl)}' for k, (bt, v) in c.items())
        q = kw + f'{{ {op["path"]}{"(" + args + ")" if args else ""} {sel} }}'.replace('  ', ' ')
        desc = ', '.join(f'{k}={v}' for k, (_, v) in c.items()) or 'no arguments'
        cases.append({'operation_id': op['operation_id'], 'kind': 'graphql', 'valid': True, 'description': desc, 'auth': True,
                      'request': {'method': 'POST', 'graphql': True, 'body': {'query': q.strip()}, 'media_type': 'application/json',
                                  'path_params': {}, 'query': {}, 'headers': {}}})
    for a in op['parameters']:
        if a['required']:
            q = kw + f'{{ {op["path"]} {sel} }}'
            cases.append({'operation_id': op['operation_id'], 'kind': 'negative', 'valid': False,
                          'description': f'missing required argument {a["name"]}', 'auth': True,
                          'request': {'method': 'POST', 'graphql': True, 'body': {'query': q}, 'media_type': 'application/json',
                                      'path_params': {}, 'query': {}, 'headers': {}}})
    return cases


def matches(op: dict, sel: str) -> bool:
    if sel.startswith('tag:'):
        return sel[4:] in op['tags']
    if sel.startswith('path:'):
        return op['path'].startswith(sel[5:])
    return op['operation_id'] == sel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('contract', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--max-cases', type=int, default=50)
    ap.add_argument('--include', action='append', default=[])
    ap.add_argument('--exclude', action='append', default=[])
    ap.add_argument('--allow-writes', action='store_true')
    ap.add_argument('--auth-configured', action='store_true', help='credentials will be supplied to send_cases.py')
    ap.add_argument('--add-cases', type=Path, action='append', default=[],
                    help='JSON list of extra cases {operation_id, description, path_params, query, headers, body, valid, auth, source}, '
                         'e.g. to reproduce a Schemathesis failure with a saved exchange')
    a = ap.parse_args()
    contract = json.loads(a.contract.read_text(encoding='utf-8'))
    seed = a.seed if a.seed is not None else random.SystemRandom().randint(1, 2 ** 31 - 1)
    rng = random.Random(seed)
    plan, cases, dropped = [], [], 0
    for op in contract['operations']:
        oid = op['operation_id']
        if (a.include and not any(matches(op, s) for s in a.include)) or any(matches(op, s) for s in a.exclude):
            plan.append({'operation_id': oid, 'status': 'excluded', 'reason': 'excluded by --include/--exclude'})
            continue
        if op['write'] and not a.allow_writes:
            plan.append({'operation_id': oid, 'status': 'skipped_write', 'reason': f'{op["method"]} needs allow_writes'})
            continue
        auth_ok = a.auth_configured or not op['security']
        if contract['kind'] == 'graphql':
            new = gql_cases(op, contract['root'], a.max_cases)
        else:
            b = Builder(op, contract['root'], rng, contract.get('dialect') or '3.1')
            new = b.build(a.max_cases, auth_ok)
            dropped += b.dropped
        status = 'planned' if auth_ok else 'skipped_auth'
        plan.append({'operation_id': oid, 'status': status, 'reason': None if auth_ok else 'security required and no credentials configured'})
        cases += new
    for path in a.add_cases:
        planned = {p['operation_id'] for p in plan if p['status'] in ('planned', 'skipped_auth')}
        ops = {o['operation_id']: o for o in contract['operations']}
        for x in json.loads(path.read_text(encoding='utf-8')):
            oid = x['operation_id']
            if oid not in planned:
                print(f'error: added case for {oid}, which is not planned', file=sys.stderr)
                return 2
            req = {'method': ops[oid]['method'], 'path_params': {k: as_param(v) for k, v in (x.get('path_params') or {}).items()},
                   'query': {k: as_param(v) for k, v in (x.get('query') or {}).items()}, 'headers': x.get('headers') or {}}
            if 'body' in x:
                req['media_type'], req['body'] = x.get('media_type', 'application/json'), x['body']
            cases.append({'operation_id': oid, 'kind': 'added', 'valid': x.get('valid', True), 'description': x['description'],
                          'auth': x.get('auth', True), 'source': x.get('source'), 'request': req})
    counts: dict[str, int] = {}
    for c in cases:
        counts[c['operation_id']] = counts.get(c['operation_id'], 0) + 1
        c['id'] = f'{c["operation_id"]}-{counts[c["operation_id"]]:03d}'
    cases = [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id'}} for c in cases]
    out = {'generator': 'stdlib', 'seed': seed, 'max_cases_per_operation': a.max_cases, 'contract_sha256': contract['sha256'],
           'allow_writes': a.allow_writes, 'auth_configured': a.auth_configured, 'dropped_invalid_samples': dropped,
           'plan': plan, 'cases': cases}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    by = {}
    for p in plan:
        by[p['status']] = by.get(p['status'], 0) + 1
    print(f'seed {seed}: {len(cases)} case(s) over {len(plan)} operation(s) {by} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
