#!/usr/bin/env python3
"""Load an OpenAPI 3.0/3.1 document or a GraphQL SDL schema, resolve it, lint it, and list its operations.

    python load_contract.py openapi.yaml --out contract.json
    python load_contract.py https://staging.example.com/openapi.json --out contract.json
    python load_contract.py schema.graphql --out contract.json [--introspection introspection.json]
    python load_contract.py schema.graphql --out contract.json --introspect-url http://127.0.0.1:8140/graphql [--save-introspection f.json]

Output (contract.json):
    {kind: openapi|graphql|swagger2, version, title, api_version, dialect, source, sha256, yaml_parser,
     operations[{operation_id, method, path, tags, line, write, security[], parameters[], request_body,
                 responses{status: {content{media_type: schema|null}, headers{name: {required, schema}}}}}],
     issues[{id, kind, location, line, message}], root}

`root` is the whole document with external $refs inlined; schemas keep their local $refs and are
checked against it with jsonschema_lite.py. A Swagger 2.0 document is not converted here (that needs
Schemathesis); kind is swagger2 and every operation is not_assessed.

Contract issues are problems in the contract itself, not in the API: a missing operationId, a 2xx
response without a schema or with an empty one ({}, which every body matches), an example that doesn't match its own schema, a keyword the checker
can't evaluate, an unresolved $ref, an OpenAPI contract in which no operation requires security
(auth_enforced can't be evaluated), or a GraphQL schema served by the API that differs from the SDL.

A contract built in code (a TypeScript object, zod-to-openapi, a framework's generator) has no file
to load: run the app's own generator or export route first, save its JSON, and load that file; the
report's scope then names the generated file, and the agent says how it was produced.
"""
from __future__ import annotations
import argparse, copy, datetime as dt, hashlib, json, re, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonschema_lite as jl  # noqa: E402
import yaml_lite  # noqa: E402

METHODS = ('get', 'put', 'post', 'delete', 'patch', 'head', 'options', 'trace')
WRITE_METHODS = {'put', 'post', 'delete', 'patch', 'mutation'}


def read_source(src: str) -> tuple[bytes, str]:
    if re.match(r'^https?://', src):
        with urllib.request.urlopen(src, timeout=30) as r:  # noqa: S310 - the user names the contract URL
            return r.read(), src
    p = Path(src)
    return p.read_bytes(), p.as_posix()


def _plainify(o):
    if isinstance(o, dict):
        return {(str(k) if not isinstance(k, str) else k): _plainify(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_plainify(v) for v in o]
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return o


def parse_document(text: str, name: str) -> tuple[dict, str]:
    s = text.lstrip('﻿').lstrip()
    if s.startswith('{'):
        return json.loads(s), 'json'
    try:
        import yaml  # type: ignore
        return _plainify(yaml.safe_load(s)), 'pyyaml'
    except ImportError:
        return yaml_lite.loads(s), 'yaml_lite'


def inline_external_refs(node, base: str, issues: list, depth: int = 0):
    """Replace `other.yaml#/x` and same-host URL refs with their targets. Local refs are left alone."""
    if depth > 20:
        return node
    if isinstance(node, list):
        return [inline_external_refs(v, base, issues, depth) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get('$ref')
    if isinstance(ref, str) and not ref.startswith('#'):
        doc_part, _, frag = ref.partition('#')
        try:
            if re.match(r'^https?://', doc_part):
                target_src = doc_part
            elif re.match(r'^https?://', base):
                target_src = base.rsplit('/', 1)[0] + '/' + doc_part
            else:
                target_src = str(Path(base).parent / doc_part)
            raw, _ = read_source(target_src)
            doc, _ = parse_document(raw.decode('utf-8'), target_src)
            target = jl.resolve('#' + frag, doc) if frag else doc
            if _has_local_ref(target):
                raise ValueError('the referenced fragment has local $refs of its own')
            return inline_external_refs(copy.deepcopy(target), target_src, issues, depth + 1)
        except Exception as e:  # noqa: BLE001
            issues.append({'kind': 'unresolved_ref', 'location': ref, 'message': f'external $ref {ref!r} not inlined: {e}'})
            return node
    return {k: inline_external_refs(v, base, issues, depth) for k, v in node.items()}


def _has_local_ref(node) -> bool:
    if isinstance(node, dict):
        return any((k == '$ref' and isinstance(v, str) and v.startswith('#')) or _has_local_ref(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_local_ref(v) for v in node)
    return False


def deref(node, root: dict, issues: list, where: str):
    """Follow $ref chains for parameters, responses and request bodies (not schemas)."""
    seen = set()
    while isinstance(node, dict) and '$ref' in node:
        ref = node['$ref']
        if ref in seen:
            issues.append({'kind': 'unresolved_ref', 'location': where, 'message': f'$ref cycle at {ref}'})
            return {}
        seen.add(ref)
        try:
            node = jl.resolve(ref, root)
        except (KeyError, IndexError, ValueError) as e:
            issues.append({'kind': 'unresolved_ref', 'location': where, 'message': f'$ref {ref!r} does not resolve ({e})'})
            return {}
    return node


def find_line(text: str, path: str, method: str) -> int | None:
    lines = text.splitlines()
    pat = re.compile(r'^\s*["\']?' + re.escape(path) + r'["\']?\s*:')
    for i, ln in enumerate(lines):
        if pat.match(ln):
            ind = len(ln) - len(ln.lstrip())
            for j in range(i + 1, len(lines)):
                lj = lines[j]
                if lj.strip() and len(lj) - len(lj.lstrip()) <= ind and not lj.lstrip().startswith(('}', ']')):
                    break
                if re.match(r'^\s*["\']?' + method + r'["\']?\s*:', lj):
                    return j + 1
            return i + 1
    return None


def _examples(obj: dict) -> list:
    out = []
    if 'example' in obj:
        out.append(obj['example'])
    for ex in (obj.get('examples') or {}).values() if isinstance(obj.get('examples'), dict) else obj.get('examples') or []:
        out.append(ex.get('value') if isinstance(ex, dict) and 'value' in ex else ex)
    return out


def load_openapi(doc: dict, text: str, issues: list) -> list[dict]:
    root_security = doc.get('security') or []
    ops = []
    for path, item in (doc.get('paths') or {}).items():
        item = deref(item, doc, issues, f'paths.{path}')
        shared = item.get('parameters') or []
        for method in METHODS:
            if method not in item:
                continue
            op = item[method]
            where = f'paths.{path}.{method}'
            oid = op.get('operationId')
            if not oid:
                oid = f'{method.upper()} {path}'
                issues.append({'kind': 'missing_operation_id', 'location': where, 'message': f'no operationId; called {oid!r} here'})
            params = {}
            for p in shared + (op.get('parameters') or []):
                p = deref(p, doc, issues, where)
                if p.get('name'):
                    params[(p['name'], p.get('in'))] = p
            parameters = [{'name': p['name'], 'in': p.get('in'), 'required': bool(p.get('required') or p.get('in') == 'path'),
                           'schema': p.get('schema') or {}, 'examples': _examples(p) or _examples(p.get('schema') or {})}
                          for p in params.values()]
            body = None
            if op.get('requestBody'):
                rb = deref(op['requestBody'], doc, issues, where)
                body = {'required': bool(rb.get('required')),
                        'content': {mt: {'schema': c.get('schema'), 'examples': _examples(c) or _examples(c.get('schema') or {})}
                                    for mt, c in (rb.get('content') or {}).items()}}
            responses = {}
            for status, r in (op.get('responses') or {}).items():
                r = deref(r, doc, issues, f'{where}.responses.{status}')
                status = str(status)
                content = {mt: (c or {}).get('schema') for mt, c in (r.get('content') or {}).items()}
                headers = {}
                for hn, h in (r.get('headers') or {}).items():
                    h = deref(h, doc, issues, f'{where}.responses.{status}.headers.{hn}')
                    headers[hn] = {'required': bool(h.get('required')), 'schema': h.get('schema') or {}}
                responses[status] = {'content': content, 'headers': headers}
                if status.startswith('2') and status != '204' and method != 'head':
                    if not content:
                        issues.append({'kind': 'missing_response_schema', 'location': f'{where}.responses.{status}',
                                       'message': f'{oid} {status} documents no content, so its body can\'t be checked'})
                    for mt, s in content.items():
                        if s is None:
                            issues.append({'kind': 'missing_response_schema', 'location': f'{where}.responses.{status}.{mt}',
                                           'message': f'{oid} {status} {mt} has no schema'})
                        elif s == {}:
                            issues.append({'kind': 'empty_response_schema', 'location': f'{where}.responses.{status}.{mt}',
                                           'message': f'{oid} {status} {mt} has an empty schema {{}}, which every body matches, so '
                                                      f'schema_conformance passes whatever is returned. Generators write this for a '
                                                      f'handler that returns a raw Response, a stream or a file, or has no return '
                                                      f'model: read the handler and document what it sends (handler_map.py reports '
                                                      f'a stream documented as JSON)'})
            security = op['security'] if 'security' in op else root_security
            schemes = sorted({k for req in security for k in req})
            ops.append({'operation_id': oid, 'method': method.upper(), 'path': path, 'tags': op.get('tags') or [],
                        'line': find_line(text, path, method), 'write': method in WRITE_METHODS,
                        'security': schemes, 'parameters': parameters, 'request_body': body, 'responses': responses})
    return ops


def lint_examples(doc: dict, ops: list, dialect: str, issues: list):
    for name, s in ((doc.get('components') or {}).get('schemas') or {}).items():
        for ex in _examples(s):
            errs = jl.errors(ex, s, doc, dialect)
            if errs:
                issues.append({'kind': 'example_mismatch', 'location': f'components.schemas.{name}.example',
                               'message': f'the example for {name} doesn\'t match its own schema: {errs[0]}'})
    for op in ops:
        for p in op['parameters']:
            for ex in p['examples']:
                errs = jl.errors(ex, p['schema'], doc, dialect)
                if errs:
                    issues.append({'kind': 'example_mismatch', 'location': f'{op["operation_id"]}.parameters.{p["name"]}',
                                   'message': f'example {ex!r} for parameter {p["name"]} doesn\'t match its schema: {errs[0]}'})
        for mt, c in ((op['request_body'] or {}).get('content') or {}).items():
            for ex in c['examples']:
                errs = jl.errors(ex, c['schema'] or {}, doc, dialect)
                if errs:
                    issues.append({'kind': 'example_mismatch', 'location': f'{op["operation_id"]}.requestBody.{mt}',
                                   'message': f'the request example doesn\'t match its schema: {errs[0]}'})


def lint_unsupported(doc: dict, ops: list, issues: list):
    by_kw: dict[str, list[str]] = {}
    for op in ops:
        for status, r in op['responses'].items():
            for s in r['content'].values():
                for kw in jl.unsupported(s or {}, doc):
                    by_kw.setdefault(kw, [])
                    if op['operation_id'] not in by_kw[kw]:
                        by_kw[kw].append(op['operation_id'])
    for kw, oids in sorted(by_kw.items()):
        issues.append({'kind': 'unsupported_keyword', 'location': kw,
                       'message': f'response schemas use {kw!r}, which the stdlib checker can\'t evaluate; schema_conformance '
                                  f'is cantTell for {", ".join(oids)} unless another keyword fails'})


# --- GraphQL ---------------------------------------------------------------

GQL_TOKEN = re.compile(r'"""[\s\S]*?"""|"(?:[^"\\]|\\.)*"|#[^\n]*|[A-Za-z_][A-Za-z0-9_]*|[{}()\[\]:!=@|&,]|\S')
SCALARS = {'ID', 'String', 'Int', 'Float', 'Boolean'}


def parse_sdl(text: str) -> dict:
    toks = [t for t in GQL_TOKEN.findall(text) if not t.startswith('#') and not t.startswith('"') and t != ',']
    types: dict[str, dict] = {}
    roots = {'query': 'Query', 'mutation': 'Mutation'}
    i = 0

    def type_ref(i):
        if toks[i] == '[':
            inner, i = type_ref(i + 1)
            assert toks[i] == ']', 'expected ]'
            s = f'[{inner}]'
            i += 1
        else:
            s, i = toks[i], i + 1
        if i < len(toks) and toks[i] == '!':
            s, i = s + '!', i + 1
        return s, i

    def skip_directives(i):
        while i < len(toks) and toks[i] == '@':
            i += 2
            if i < len(toks) and toks[i] == '(':
                d = 0
                while True:
                    d += toks[i] == '('
                    d -= toks[i] == ')'
                    i += 1
                    if d == 0:
                        break
        return i

    while i < len(toks):
        t = toks[i]
        if t == 'extend':
            i += 1
            continue
        if t == 'schema':
            i += 2
            while toks[i] != '}':
                roots[toks[i]] = toks[i + 2]
                i += 3
            i += 1
        elif t in ('type', 'input', 'interface'):
            name = toks[i + 1]
            i += 2
            while toks[i] not in ('{',) and i < len(toks):
                i += 1
            i += 1
            fields = types.setdefault(name, {'kind': {'type': 'OBJECT', 'input': 'INPUT_OBJECT', 'interface': 'INTERFACE'}[t],
                                             'fields': {}})['fields']
            while toks[i] != '}':
                fname, i = toks[i], i + 1
                args = {}
                if toks[i] == '(':
                    i += 1
                    while toks[i] != ')':
                        aname = toks[i]
                        aref, i = type_ref(i + 2)
                        if toks[i] == '=':
                            i += 2
                        i = skip_directives(i)
                        args[aname] = aref
                    i += 1
                ref, i = type_ref(i + 1)
                if i < len(toks) and toks[i] == '=':
                    i += 2
                i = skip_directives(i)
                fields[fname] = {'type': ref, 'args': args}
            i += 1
        elif t == 'enum':
            name = toks[i + 1]
            i += 2
            while toks[i] != '{':
                i += 1
            i += 1
            vals = []
            while toks[i] != '}':
                vals.append(toks[i])
                i = skip_directives(i + 1)
            types[name] = {'kind': 'ENUM', 'values': vals}
            i += 1
        elif t == 'scalar':
            types[toks[i + 1]] = {'kind': 'SCALAR'}
            i = skip_directives(i + 2)
        elif t == 'union':
            name = toks[i + 1]
            i += 3
            members = []
            while i < len(toks) and (toks[i] == '|' or (toks[i] not in ('type', 'input', 'enum', 'scalar', 'union', 'interface', 'schema', 'extend', 'directive'))):
                if toks[i] != '|':
                    members.append(toks[i])
                i += 1
            types[name] = {'kind': 'UNION', 'members': members}
        elif t == 'directive':
            i += 1
            while i < len(toks) and toks[i] != 'on':
                i += 1
            i += 2
            while i < len(toks) and toks[i] == '|':
                i += 2
        else:
            i += 1
    return {'types': types, 'roots': roots}


def load_graphql(sdl: dict, text: str) -> list[dict]:
    ops = []
    for kind, root in (('query', sdl['roots'].get('query')), ('mutation', sdl['roots'].get('mutation'))):
        t = sdl['types'].get(root or '')
        if not t:
            continue
        for fname, f in t['fields'].items():
            m = re.search(r'^\s*' + re.escape(fname) + r'\s*[(:]', text, re.M)
            ops.append({'operation_id': f'{root}.{fname}', 'method': kind.upper(), 'path': fname, 'tags': [kind],
                        'line': text[:m.start()].count('\n') + 1 if m else None, 'write': kind == 'mutation',
                        'security': [], 'parameters': [{'name': a, 'in': 'argument', 'required': r.endswith('!'), 'type': r}
                                                       for a, r in f['args'].items()],
                        'request_body': None, 'responses': {}, 'returns': f['type']})
    return ops


def _intro_ref(t: dict | None) -> str:
    if not t:
        return '?'
    if t['kind'] == 'NON_NULL':
        return _intro_ref(t.get('ofType')) + '!'
    if t['kind'] == 'LIST':
        return '[' + _intro_ref(t.get('ofType')) + ']'
    return t.get('name') or '?'


def compare_introspection(sdl: dict, intro: dict, issues: list):
    schema = (intro.get('data') or intro).get('__schema') or {}
    served = {t['name']: t for t in schema.get('types') or [] if not t['name'].startswith('__')}
    for name, t in sdl['types'].items():
        if name not in served:
            issues.append({'kind': 'introspection_mismatch', 'location': name, 'message': f'type {name} is in the SDL but not served'})
            continue
        if t['kind'] in ('OBJECT', 'INPUT_OBJECT', 'INTERFACE'):
            got = {f['name']: f for f in served[name].get('fields') or served[name].get('inputFields') or []}
            for fname, f in t['fields'].items():
                if fname not in got:
                    issues.append({'kind': 'introspection_mismatch', 'location': f'{name}.{fname}',
                                   'message': f'{name}.{fname} is in the SDL but not served'})
                elif _intro_ref(got[fname].get('type')) != f['type']:
                    issues.append({'kind': 'introspection_mismatch', 'location': f'{name}.{fname}',
                                   'message': f'{name}.{fname} is {f["type"]} in the SDL but served as {_intro_ref(got[fname]["type"])}'})
            for fname in got:
                if fname not in t['fields']:
                    issues.append({'kind': 'introspection_mismatch', 'location': f'{name}.{fname}',
                                   'message': f'the API serves {name}.{fname}, which isn\'t in the SDL'})
        if t['kind'] == 'ENUM':
            got = {v['name'] for v in served[name].get('enumValues') or []}
            if got != set(t['values']):
                issues.append({'kind': 'introspection_mismatch', 'location': name,
                               'message': f'enum {name} values differ: SDL {sorted(t["values"])}, served {sorted(got)}'})
    for name in served:
        if name not in sdl['types'] and name not in SCALARS:
            issues.append({'kind': 'introspection_mismatch', 'location': name, 'message': f'the API serves type {name}, which isn\'t in the SDL'})


INTROSPECTION_QUERY = ('query IntrospectionQuery { __schema { queryType { name } mutationType { name } types { kind name '
                       'fields { name args { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } '
                       'type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } '
                       'inputFields { name type { kind name ofType { kind name ofType { kind name } } } } enumValues { name } } } }')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('contract', help='file path or URL')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--kind', choices=['auto', 'openapi', 'graphql'], default='auto')
    ap.add_argument('--introspection', type=Path, help='GraphQL: a saved introspection result to compare with the SDL')
    ap.add_argument('--introspect-url', help='GraphQL: POST the introspection query here (loopback, or a host you are authorised to test)')
    ap.add_argument('--bearer-env', help='env var holding a bearer token for --introspect-url')
    ap.add_argument('--save-introspection', type=Path, help='write the fetched introspection result here')
    ap.add_argument('--print-introspection-query', action='store_true', help='print the introspection query and exit')
    a = ap.parse_args()
    if a.print_introspection_query:
        print(INTROSPECTION_QUERY)
        return 0
    raw, source = read_source(a.contract)
    text = raw.decode('utf-8-sig')
    sha = hashlib.sha256(raw).hexdigest()
    issues: list[dict] = []
    kind = a.kind
    if kind == 'auto':
        kind = 'graphql' if source.endswith(('.graphql', '.gql', '.graphqls')) or re.search(r'^\s*type\s+Query\b', text, re.M) else 'openapi'
    out = {'source': source, 'sha256': sha}
    if kind == 'graphql':
        sdl = parse_sdl(text)
        ops = load_graphql(sdl, text)
        intro = json.loads(a.introspection.read_text(encoding='utf-8')) if a.introspection else None
        if a.introspect_url:
            headers = {'Content-Type': 'application/json'}
            if a.bearer_env:
                import os
                headers['Authorization'] = 'Bearer ' + os.environ[a.bearer_env]
            req = urllib.request.Request(a.introspect_url, data=json.dumps({'query': INTROSPECTION_QUERY}).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - the user names the endpoint
                intro = json.loads(r.read().decode('utf-8'))
            if a.save_introspection:
                a.save_introspection.write_text(json.dumps(intro, indent=2) + '\n', encoding='utf-8')
            if intro.get('errors') and not (intro.get('data') or {}).get('__schema'):
                issues.append({'kind': 'introspection_mismatch', 'location': a.introspect_url,
                               'message': 'introspection is disabled or failed; the served schema was not compared'})
                intro = None
        if intro:
            compare_introspection(sdl, intro, issues)
        out.update({'kind': 'graphql', 'version': None, 'title': None, 'api_version': None, 'dialect': 'graphql',
                    'yaml_parser': None, 'operations': ops, 'root': sdl,
                    'introspection_compared': bool(intro)})
    else:
        doc, parser = parse_document(text, source)
        if not isinstance(doc, dict):
            print('error: the contract is not a JSON/YAML object', file=sys.stderr)
            return 2
        if 'swagger' in doc:
            out.update({'kind': 'swagger2', 'version': str(doc['swagger']), 'title': (doc.get('info') or {}).get('title'),
                        'api_version': (doc.get('info') or {}).get('version'), 'dialect': None, 'yaml_parser': parser,
                        'operations': [], 'root': doc})
            issues.append({'kind': 'unsupported_version', 'location': 'swagger',
                           'message': 'Swagger 2.0 needs converting to OpenAPI 3 (Schemathesis does this); not_assessed here'})
        else:
            version = str(doc.get('openapi', ''))
            if not version.startswith(('3.0', '3.1')):
                issues.append({'kind': 'unsupported_version', 'location': 'openapi', 'message': f'openapi {version!r} is not 3.0 or 3.1'})
            doc = inline_external_refs(doc, source, issues)
            dialect = '3.1' if version.startswith('3.1') else '3.0'
            ops = load_openapi(doc, text, issues)
            lint_examples(doc, ops, dialect, issues)
            lint_unsupported(doc, ops, issues)
            if ops and not any(o['security'] for o in ops):
                schemes = sorted(((doc.get('components') or {}).get('securitySchemes') or {}))
                issues.append({'kind': 'no_security_declared', 'location': 'security',
                               'message': ('no operation declares a security requirement'
                                           + (f' (securitySchemes {", ".join(schemes)} are defined but never required)' if schemes
                                              else ' and there are no securitySchemes')
                                           + '; auth_enforced cannot be evaluated from the contract. If the handlers need a '
                                             'session or token, mark those operations with gen_cases.py --auth-required')})
            out.update({'kind': 'openapi', 'version': version, 'title': (doc.get('info') or {}).get('title'),
                        'api_version': (doc.get('info') or {}).get('version'), 'dialect': dialect, 'yaml_parser': parser,
                        'operations': ops, 'root': doc})
    for i, iss in enumerate(issues, 1):
        iss['id'] = f'C{i}'
        iss.setdefault('line', None)
    out['issues'] = issues
    out = {k: out[k] for k in ('kind', 'version', 'title', 'api_version', 'dialect', 'source', 'sha256', 'yaml_parser',
                               'operations', 'issues', 'root') if k in out} | {k: v for k, v in out.items() if k == 'introspection_compared'}
    text_out = json.dumps(out, indent=2) + '\n'
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text_out, encoding='utf-8')
        print(f'{out["kind"]} {out.get("version") or ""}: {len(out["operations"])} operation(s), {len(issues)} contract issue(s) -> {a.out}',
              file=sys.stderr)
    else:
        sys.stdout.write(text_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
