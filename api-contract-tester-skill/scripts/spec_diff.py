#!/usr/bin/env python3
"""Compare two OpenAPI contracts (as loaded by load_contract.py) and rate every change.

    python spec_diff.py old.contract.json new.contract.json --out diff.json [--oasdiff oasdiff.json | --run-oasdiff OLD NEW]

Ratings:
  breaking              a client that worked with the old contract can fail with the new one
  potentially_breaking  strict or generated clients may fail (a new enum value in a response, a removed
                        optional request parameter, a changed operationId)
  compatible            listed, not rated

Request side: narrowing breaks (new required parameter or property, removed enum value, tightened bounds,
new pattern or format, a type that accepts less, additionalProperties turned off, new security).
Response side: widening breaks (removed property or status, a field no longer required, a type that can
return more); a new enum value or loosened bound is potentially breaking. Removing an operation or a
response status is breaking.

Severity (for the report): breaking without a major version bump is High, potentially breaking without
one is Medium; with a major bump a breaking change is Low (expected); compatible changes have none.

oasdiff (Apache-2.0) is used when its JSON is given (`oasdiff breaking old.yaml new.yaml -f json`) or
--run-oasdiff finds the binary. Its items are merged with the stdlib ones by operation; items only it
found are added with their own rating (level 3 breaking, 2 potentially breaking, 1 compatible).
"""
from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonschema_lite as jl  # noqa: E402

LOWER_BOUNDS = ('minimum', 'minLength', 'minItems', 'minProperties', 'exclusiveMinimum')
UPPER_BOUNDS = ('maximum', 'maxLength', 'maxItems', 'maxProperties', 'exclusiveMaximum')


def show(v, quote: bool = False) -> str:
    return 'none' if v is None else repr(v) if quote else str(v)


def types_of(s: dict, dialect: str) -> set[str] | None:
    t = s.get('type')
    if t is None:
        return None
    out = set(t if isinstance(t, list) else [t])
    if dialect == '3.0' and s.get('nullable'):
        out.add('null')
    if 'integer' in out and 'number' in out:
        out.discard('integer')
    return out


def widens(old: set, new: set) -> bool:
    expand = lambda s: s | ({'integer'} if 'number' in s else set())  # noqa: E731
    return not expand(new) <= expand(old)


class Differ:
    def __init__(self, old: dict, new: dict):
        self.old, self.new = old, new
        self.changes: list[dict] = []

    def add(self, kind, rating, direction, oid, location, detail):
        self.changes.append({'kind': kind, 'rating': rating, 'direction': direction, 'operation_ids': [oid],
                             'location': location, 'detail': detail, 'sources': ['stdlib']})

    def deref(self, s, root):
        name = None
        n = 0
        while isinstance(s, dict) and '$ref' in s and n < 20:
            name = s['$ref'].rsplit('/', 1)[-1]
            s = jl.resolve(s['$ref'], root)
            n += 1
        return s or {}, name

    def schema(self, o, n, direction: str, oid: str, loc: str, seen: set, depth: int = 0):
        if depth > 12:
            return
        o, oname = self.deref(o, self.old['root'])
        n, nname = self.deref(n, self.new['root'])
        if nname:
            loc = nname
        key = (id(o), id(n), direction)
        if key in seen:
            return
        seen.add(key)
        req = direction == 'request'
        ot, nt = types_of(o, self.old['dialect']), types_of(n, self.new['dialect'])
        if ot and nt and ot != nt:
            if req and widens(nt, ot):
                self.add('type_narrowed', 'breaking', direction, oid, loc, f'type {sorted(ot)} -> {sorted(nt)}')
            elif not req and widens(ot, nt):
                self.add('type_widened', 'breaking', direction, oid, loc, f'type {sorted(ot)} -> {sorted(nt)}')
            else:
                self.add('type_changed', 'compatible', direction, oid, loc, f'type {sorted(ot)} -> {sorted(nt)}')
        oe, ne = o.get('enum'), n.get('enum')
        if oe is not None or ne is not None:
            oe_s, ne_s = set(map(json.dumps, oe or [])), set(map(json.dumps, ne or []))
            if oe is not None and ne is not None:
                added, removed = sorted(ne_s - oe_s), sorted(oe_s - ne_s)
                if added:
                    self.add('enum_value_added', 'compatible' if req else 'potentially_breaking', direction, oid, loc,
                             f'enum gained {", ".join(json.loads(x) if isinstance(json.loads(x), str) else x for x in added)}')
                if removed:
                    self.add('enum_value_removed', 'breaking' if req else 'compatible', direction, oid, loc,
                             f'enum lost {", ".join(str(json.loads(x)) for x in removed)}')
            elif ne is not None:
                self.add('enum_added', 'breaking' if req else 'compatible', direction, oid, loc, f'now restricted to {ne}')
            else:
                self.add('enum_removed', 'compatible' if req else 'potentially_breaking', direction, oid, loc, 'enum restriction removed')
        for b in LOWER_BOUNDS:
            ov, nv = o.get(b), n.get(b)
            if isinstance(ov, bool) or isinstance(nv, bool) or ov == nv:
                continue
            tightened = ov is None or (nv is not None and nv > ov)
            if nv is None:
                tightened = False
            self.bound(b, ov, nv, tightened, req, direction, oid, loc)
        for b in UPPER_BOUNDS:
            ov, nv = o.get(b), n.get(b)
            if isinstance(ov, bool) or isinstance(nv, bool) or ov == nv:
                continue
            tightened = nv is not None and (ov is None or nv < ov)
            self.bound(b, ov, nv, tightened, req, direction, oid, loc)
        for kw in ('pattern', 'format'):
            ov, nv = o.get(kw), n.get(kw)
            if ov != nv:
                if req:
                    rating = 'breaking' if nv is not None else 'compatible'
                else:
                    rating = 'compatible' if ov is None else 'potentially_breaking'
                self.add(f'{kw}_changed', rating, direction, oid, loc, f'{kw} {show(ov, True)} -> {show(nv, True)}')
        if req and o.get('additionalProperties', True) is not False and n.get('additionalProperties', True) is False:
            self.add('additional_properties_disallowed', 'breaking', direction, oid, loc, 'additionalProperties: false added')
        oreq, nreq = set(o.get('required') or []), set(n.get('required') or [])
        oprops, nprops = o.get('properties') or {}, n.get('properties') or {}
        for p in sorted(nreq - oreq):
            if req:
                self.add('required_property_added', 'breaking', direction, oid, f'{loc}.{p}', f'{p} is now required')
            else:
                self.add('property_now_required', 'compatible', direction, oid, f'{loc}.{p}', f'{p} is now always returned')
        for p in sorted(oreq - nreq):
            if p in nprops or req:
                self.add('property_no_longer_required', 'compatible' if req else 'breaking', direction, oid, f'{loc}.{p}',
                         f'{p} is no longer required')
        for p in sorted(set(oprops) - set(nprops)):
            if req:
                self.add('request_property_removed', 'breaking' if n.get('additionalProperties') is False else 'potentially_breaking',
                         direction, oid, f'{loc}.{p}', f'{p} removed from the request schema')
            else:
                self.add('response_property_removed', 'breaking', direction, oid, f'{loc}.{p}', f'{p} removed from the response')
        for p in sorted(set(nprops) - set(oprops)):
            if not (req and p in nreq):
                self.add('property_added', 'compatible', direction, oid, f'{loc}.{p}', f'{p} added')
        for p in sorted(set(oprops) & set(nprops)):
            self.schema(oprops[p], nprops[p], direction, oid, f'{loc}.{p}', seen, depth + 1)
        if isinstance(o.get('items'), dict) and isinstance(n.get('items'), dict):
            self.schema(o['items'], n['items'], direction, oid, f'{loc}[]', seen, depth + 1)
        for kw in ('oneOf', 'anyOf', 'allOf', 'not'):
            if json.dumps(o.get(kw), sort_keys=True) != json.dumps(n.get(kw), sort_keys=True):
                self.add(f'{kw}_changed', 'potentially_breaking', direction, oid, loc, f'{kw} changed; check it by hand')

    def bound(self, b, ov, nv, tightened, req, direction, oid, loc):
        if req:
            rating = 'breaking' if tightened else 'compatible'
        else:
            rating = 'compatible' if tightened else 'potentially_breaking'
        self.add('bound_tightened' if tightened else 'bound_loosened', rating, direction, oid, loc, f'{b} {show(ov)} -> {show(nv)}')

    def run(self):
        old_ops = {(o['method'], o['path']): o for o in self.old['operations']}
        new_ops = {(o['method'], o['path']): o for o in self.new['operations']}
        old_links = json.dumps(self.old['root'].get('paths') or {})
        for k, o in old_ops.items():
            oid = o['operation_id']
            if k not in new_ops:
                self.add('operation_removed', 'breaking', 'operation', oid, f'{k[0]} {k[1]}', f'{oid} removed')
                continue
            n = new_ops[k]
            if n['operation_id'] != oid:
                used = f'"operationId": "{oid}"' in old_links and '"links"' in old_links
                self.add('operation_id_changed', 'breaking' if used else 'potentially_breaking', 'operation', n['operation_id'],
                         f'{k[0]} {k[1]}', f'operationId {oid} -> {n["operation_id"]}' + (' (used in links)' if used else ''))
            self.operation(o, n)
        for k, n in new_ops.items():
            if k not in old_ops:
                self.add('operation_added', 'compatible', 'operation', n['operation_id'], f'{k[0]} {k[1]}', f'{n["operation_id"]} added')

    def operation(self, o, n):
        oid = n['operation_id']
        if n['security'] and not o['security']:
            self.add('security_added', 'breaking', 'request', oid, 'security', f'now requires {n["security"]}')
        elif o['security'] and not n['security']:
            self.add('security_removed', 'compatible', 'request', oid, 'security', 'no longer requires credentials')
        op_ = {(p['name'], p['in']): p for p in o['parameters']}
        np_ = {(p['name'], p['in']): p for p in n['parameters']}
        for k, p in np_.items():
            loc = f'parameter {k[1]}.{k[0]}'
            if k not in op_:
                self.add('required_parameter_added' if p['required'] else 'parameter_added',
                         'breaking' if p['required'] else 'compatible', 'request', oid, loc,
                         f'new {"required" if p["required"] else "optional"} {k[1]} parameter {k[0]}')
                continue
            q = op_[k]
            if p['required'] and not q['required']:
                self.add('parameter_now_required', 'breaking', 'request', oid, loc, f'{k[0]} is now required')
            elif q['required'] and not p['required']:
                self.add('parameter_now_optional', 'compatible', 'request', oid, loc, f'{k[0]} is now optional')
            self.schema(q['schema'], p['schema'], 'request', oid, loc, set())
        for k in op_:
            if k not in np_:
                self.add('parameter_removed', 'potentially_breaking', 'request', oid, f'parameter {k[1]}.{k[0]}', f'{k[0]} removed')
        ob, nb = o['request_body'], n['request_body']
        if nb and not ob:
            self.add('request_body_added', 'breaking' if nb['required'] else 'compatible', 'request', oid, 'requestBody', 'request body added')
        elif ob and nb:
            if nb['required'] and not ob['required']:
                self.add('request_body_now_required', 'breaking', 'request', oid, 'requestBody', 'request body is now required')
            for mt, c in ob['content'].items():
                if mt not in nb['content']:
                    self.add('request_media_type_removed', 'breaking', 'request', oid, f'requestBody {mt}', f'{mt} no longer accepted')
                else:
                    self.schema(c['schema'] or {}, nb['content'][mt]['schema'] or {}, 'request', oid, 'requestBody', set())
        for st, r in o['responses'].items():
            loc = f'response {st}'
            if st not in n['responses']:
                self.add('response_status_removed', 'breaking', 'response', oid, loc, f'{st} is no longer documented')
                continue
            nr = n['responses'][st]
            for mt, sch in r['content'].items():
                if mt not in nr['content']:
                    self.add('response_media_type_removed', 'breaking', 'response', oid, f'{loc} {mt}', f'{mt} no longer returned')
                elif sch is not None and nr['content'][mt] is not None:
                    self.schema(sch, nr['content'][mt], 'response', oid, loc, set())
            for h, spec in r['headers'].items():
                if spec['required'] and not (nr['headers'].get(h) or {}).get('required'):
                    self.add('response_header_removed', 'breaking', 'response', oid, f'{loc} header {h}', f'{h} is no longer always sent')
            for h, spec in nr['headers'].items():
                if spec['required'] and not (r['headers'].get(h) or {}).get('required'):
                    self.add('response_header_added', 'compatible', 'response', oid, f'{loc} header {h}', f'{h} is now always sent')
        for st in n['responses']:
            if st not in o['responses']:
                self.add('response_status_added', 'compatible', 'response', oid, f'response {st}', f'{st} added')


def merge(changes: list[dict]) -> list[dict]:
    out: dict[tuple, dict] = {}
    for c in changes:
        k = (c['kind'], c['rating'], c['direction'], c['location'], c['detail'])
        if k in out:
            for oid in c['operation_ids']:
                if oid not in out[k]['operation_ids']:
                    out[k]['operation_ids'].append(oid)
        else:
            out[k] = dict(c, operation_ids=list(c['operation_ids']))
    return list(out.values())


def major(v) -> str | None:
    m = re.match(r'^\D*(\d+)', str(v or ''))
    return m.group(1) if m else None


def severity(rating: str, major_bump: bool) -> str | None:
    if rating == 'breaking':
        return 'Low' if major_bump else 'High'
    if rating == 'potentially_breaking':
        return None if major_bump else 'Medium'
    return None


OASDIFF_LEVEL = {3: 'breaking', 2: 'potentially_breaking', 1: 'compatible'}


def merge_oasdiff(changes: list[dict], items: list[dict], new_ops: dict) -> None:
    for it in items:
        rating = OASDIFF_LEVEL.get(it.get('level'), 'potentially_breaking')
        oid = it.get('operationId') or new_ops.get((str(it.get('operation', '')).upper(), it.get('path')), {}).get('operation_id') \
            or f'{str(it.get("operation", "")).upper()} {it.get("path")}'
        words = set(re.split(r'[-_]', str(it.get('id') or ''))) - {'request', 'response', 'added', 'removed', 'changed'}
        same = [c for c in changes if oid in c['operation_ids'] and c['rating'] == rating and 'oasdiff' not in c['sources']]
        scored = sorted(((len(words & set(c['kind'].split('_'))), i) for i, c in enumerate(same)), reverse=True)
        match = same[scored[0][1]] if scored and scored[0][0] > 0 else None
        if match:
            match['sources'].append('oasdiff')
            match.setdefault('oasdiff_ids', []).append(it.get('id'))
        else:
            changes.append({'kind': it.get('id') or 'oasdiff', 'rating': rating, 'direction': 'unknown', 'operation_ids': [oid],
                            'location': it.get('source') or it.get('path') or '', 'detail': it.get('text') or '', 'sources': ['oasdiff']})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('old', type=Path)
    ap.add_argument('new', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--oasdiff', type=Path, help='saved `oasdiff breaking -f json` output')
    ap.add_argument('--run-oasdiff', nargs=2, metavar=('OLD_SPEC', 'NEW_SPEC'), help='run oasdiff on these spec files if installed')
    a = ap.parse_args()
    old = json.loads(a.old.read_text(encoding='utf-8'))
    new = json.loads(a.new.read_text(encoding='utf-8'))
    for c in (old, new):
        if c['kind'] != 'openapi':
            print(f'error: {c["source"]} is {c["kind"]}; the diff needs two OpenAPI 3 contracts', file=sys.stderr)
            return 2
    d = Differ(old, new)
    d.run()
    changes = merge(d.changes)
    tool = {'name': 'oasdiff', 'version': None, 'status': 'not_run', 'note': 'not installed and no saved output given'}
    items = None
    if a.oasdiff:
        items = json.loads(a.oasdiff.read_text(encoding='utf-8'))
        tool = {'name': 'oasdiff', 'version': None, 'status': 'ran', 'note': f'saved output {a.oasdiff.as_posix()}'}
    elif a.run_oasdiff and shutil.which('oasdiff'):
        try:
            ver = subprocess.run(['oasdiff', '--version'], capture_output=True, text=True, timeout=30).stdout.strip()
            p = subprocess.run(['oasdiff', 'breaking', *a.run_oasdiff, '-f', 'json'], capture_output=True, text=True, timeout=120)
            items = json.loads(p.stdout or '[]')
            tool = {'name': 'oasdiff', 'version': ver, 'status': 'ran', 'note': None}
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            tool = {'name': 'oasdiff', 'version': None, 'status': 'errored', 'note': str(e)[:200]}
    if items is not None:
        merge_oasdiff(changes, items if isinstance(items, list) else items.get('changes', []),
                      {(o['method'], o['path']): o for o in new['operations']})
    bump = major(old.get('api_version')) != major(new.get('api_version'))
    order = {'breaking': 0, 'potentially_breaking': 1, 'compatible': 2}
    changes.sort(key=lambda c: (order[c['rating']], c['location'], c['kind']))
    for i, c in enumerate(changes, 1):
        c['id'] = f'B{i}'
        c['severity'] = severity(c['rating'], bump)
    changes = [{k: c[k] for k in ('id', 'kind', 'rating', 'severity', 'direction', 'operation_ids', 'location', 'detail', 'sources')}
               for c in changes]
    out = {'old': {k: old.get(k) for k in ('source', 'sha256', 'version', 'api_version')},
           'new': {k: new.get(k) for k in ('source', 'sha256', 'version', 'api_version')},
           'major_bump': bump, 'tools': [{'name': 'spec_diff (stdlib)', 'version': '1.0.0', 'status': 'ran', 'note': None}, tool],
           'changes': changes}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    counts = {r: sum(c['rating'] == r for c in changes) for r in order}
    print(f'{old.get("api_version")} -> {new.get("api_version")}{" (major bump)" if bump else ""}: {counts} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
