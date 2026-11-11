#!/usr/bin/env python3
"""A stdlib subset of JSON Schema for checking API bodies against OpenAPI schemas.

Covers the keywords OpenAPI documents use: type (a string, or a list in 3.1), nullable (3.0),
enum, const, properties, required, additionalProperties, items, min/maxItems, uniqueItems,
min/maxLength, pattern, minimum, maximum, exclusiveMinimum/Maximum (3.0 booleans or 3.1
numbers), multipleOf, min/maxProperties, oneOf, anyOf, allOf, not, local $ref, and format for
date, date-time, time, email, uuid, uri, ipv4 and ipv6. Annotations (description, example,
readOnly, writeOnly, discriminator, ...) are ignored. Other formats are annotations and are not
checked.

Any other keyword (dependentRequired, if/then/else, patternProperties, prefixItems, ...) is
*unsupported*: `unsupported(schema, root)` lists them, and a caller must report the check as
cantTell, never passed, when one is present and no supported keyword failed.

    python jsonschema_lite.py schema.json value.json [--dialect 3.0|3.1]
"""
from __future__ import annotations
import argparse, datetime as dt, ipaddress, json, re, sys, uuid
from pathlib import Path

SUPPORTED = {
    '$ref', 'type', 'nullable', 'enum', 'const', 'properties', 'required', 'additionalProperties', 'items',
    'minItems', 'maxItems', 'uniqueItems', 'minLength', 'maxLength', 'pattern', 'minimum', 'maximum',
    'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minProperties', 'maxProperties', 'oneOf', 'anyOf',
    'allOf', 'not', 'format',
}
ANNOTATIONS = {
    'description', 'title', 'example', 'examples', 'default', 'deprecated', 'readOnly', 'writeOnly', 'xml',
    'externalDocs', 'discriminator', '$schema', '$id', '$comment', '$anchor', 'contentMediaType',
    'contentEncoding',
}
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
URI_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:\S*$')
TIME_RE = re.compile(r'^\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$', re.I)


def resolve(ref: str, root: dict):
    if not ref.startswith('#'):
        raise ValueError(f'non-local $ref {ref!r} (resolve it with load_contract.py first)')
    node = root
    for part in [p for p in ref[1:].split('/') if p]:
        part = part.replace('~1', '/').replace('~0', '~')
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _children(schema: dict):
    """Yield the subschemas of a schema node (not property names)."""
    for k in ('properties',):
        for v in (schema.get(k) or {}).values():
            yield v
    for k in ('additionalProperties', 'items', 'not'):
        if isinstance(schema.get(k), dict):
            yield schema[k]
    for k in ('oneOf', 'anyOf', 'allOf'):
        for v in schema.get(k) or []:
            yield v


def unsupported(schema, root: dict, _seen: set | None = None) -> set[str]:
    """Keywords in this schema (and everything it references) that this module can't check."""
    seen = _seen if _seen is not None else set()
    if not isinstance(schema, dict):
        return set()
    if '$ref' in schema:
        if schema['$ref'] in seen:
            return set()
        seen.add(schema['$ref'])
        try:
            target = resolve(schema['$ref'], root)
        except (KeyError, IndexError, ValueError):
            return {'$ref:' + schema['$ref']}
        out = unsupported(target, root, seen)
    else:
        out = set()
    out |= {k for k in schema if k not in SUPPORTED and k not in ANNOTATIONS and not k.startswith('x-')}
    if isinstance(schema.get('items'), list):
        out.add('items(array form)')
    for child in _children(schema):
        out |= unsupported(child, root, seen)
    return out


def _type_ok(value, t: str) -> bool:
    if t == 'object':
        return isinstance(value, dict)
    if t == 'array':
        return isinstance(value, list)
    if t == 'string':
        return isinstance(value, str)
    if t == 'boolean':
        return isinstance(value, bool)
    if t == 'integer':
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
    if t == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == 'null':
        return value is None
    return True


def _format_ok(value: str, fmt: str) -> bool:
    try:
        if fmt == 'date':
            return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)) and bool(dt.date.fromisoformat(value))
        if fmt == 'date-time':
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})', value):
                return False
            dt.datetime.fromisoformat(value.replace('z', 'Z').replace('Z', '+00:00'))
            return True
        if fmt == 'time':
            return bool(TIME_RE.match(value))
        if fmt == 'email':
            return bool(EMAIL_RE.match(value))
        if fmt == 'uuid':
            uuid.UUID(value)
            return len(value) == 36
        if fmt in ('uri', 'url'):
            return bool(URI_RE.match(value))
        if fmt == 'ipv4':
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
        if fmt == 'ipv6':
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv6Address)
    except ValueError:
        return False
    return True


def _equal(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def errors(value, schema, root: dict | None = None, dialect: str = '3.1', path: str = '$') -> list[str]:
    """Every violation of the supported keywords, as 'path: message'. Unsupported keywords are skipped."""
    root = root if root is not None else schema
    if schema is True or schema is None or schema == {}:
        return []
    if schema is False:
        return [f'{path}: no value is allowed here']
    if '$ref' in schema:
        target = resolve(schema['$ref'], root)
        rest = {k: v for k, v in schema.items() if k != '$ref'}
        return errors(value, target, root, dialect, path) + (errors(value, rest, root, dialect, path) if rest else [])
    if value is None and dialect.startswith('3.0') and schema.get('nullable') is True:
        return []
    out: list[str] = []
    for sub in schema.get('allOf') or []:
        out += errors(value, sub, root, dialect, path)
    if 'anyOf' in schema:
        if not any(not errors(value, s, root, dialect, path) for s in schema['anyOf']):
            out.append(f'{path}: matches none of anyOf')
    if 'oneOf' in schema:
        n = sum(not errors(value, s, root, dialect, path) for s in schema['oneOf'])
        if n != 1:
            out.append(f'{path}: matches {n} of oneOf (needs exactly 1)')
    if 'not' in schema and not errors(value, schema['not'], root, dialect, path):
        out.append(f'{path}: matches a schema it must not match')
    if 'const' in schema and not _equal(value, schema['const']):
        out.append(f'{path}: must be {schema["const"]!r}')
    if 'enum' in schema and not any(_equal(value, e) for e in schema['enum']):
        if not (value is None and dialect.startswith('3.0') and schema.get('nullable')):
            out.append(f'{path}: {_short(value)} is not one of {schema["enum"]}')
    t = schema.get('type')
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return out + [f'{path}: expected {"|".join(types)}, got {_kind(value)}']
    if isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            out.append(f'{path}: shorter than minLength {schema["minLength"]}')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            out.append(f'{path}: longer than maxLength {schema["maxLength"]}')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            out.append(f'{path}: does not match pattern {schema["pattern"]}')
        if 'format' in schema and not _format_ok(value, schema['format']):
            out.append(f'{path}: not a valid {schema["format"]}')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = schema.get('minimum'), schema.get('maximum')
        xlo, xhi = schema.get('exclusiveMinimum'), schema.get('exclusiveMaximum')
        if xlo is True:
            xlo, lo = lo, None
        if xhi is True:
            xhi, hi = hi, None
        if lo is not None and value < lo:
            out.append(f'{path}: below minimum {lo}')
        if hi is not None and value > hi:
            out.append(f'{path}: above maximum {hi}')
        if isinstance(xlo, (int, float)) and not isinstance(xlo, bool) and value <= xlo:
            out.append(f'{path}: not above exclusiveMinimum {xlo}')
        if isinstance(xhi, (int, float)) and not isinstance(xhi, bool) and value >= xhi:
            out.append(f'{path}: not below exclusiveMaximum {xhi}')
        if schema.get('multipleOf') and (value / schema['multipleOf']) % 1:
            out.append(f'{path}: not a multiple of {schema["multipleOf"]}')
    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            out.append(f'{path}: fewer than {schema["minItems"]} item(s)')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            out.append(f'{path}: more than {schema["maxItems"]} item(s)')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            out.append(f'{path}: items are not unique')
        if isinstance(schema.get('items'), dict):
            for i, v in enumerate(value):
                out += errors(v, schema['items'], root, dialect, f'{path}[{i}]')
    if isinstance(value, dict):
        for k in schema.get('required') or []:
            if k not in value:
                out.append(f'{path}: missing required {k!r}')
        if 'minProperties' in schema and len(value) < schema['minProperties']:
            out.append(f'{path}: fewer than {schema["minProperties"]} properties')
        if 'maxProperties' in schema and len(value) > schema['maxProperties']:
            out.append(f'{path}: more than {schema["maxProperties"]} properties')
        props = schema.get('properties') or {}
        extra = schema.get('additionalProperties', True)
        for k, v in value.items():
            if k in props:
                out += errors(v, props[k], root, dialect, f'{path}.{k}')
            elif extra is False:
                out.append(f'{path}: unexpected property {k!r}')
            elif isinstance(extra, dict):
                out += errors(v, extra, root, dialect, f'{path}.{k}')
    return out


def check(value, schema, root: dict | None = None, dialect: str = '3.1') -> str:
    """'passed', 'failed' or 'cantTell' (no supported keyword failed, but an unsupported one is present)."""
    root = root if root is not None else schema
    if errors(value, schema, root, dialect):
        return 'failed'
    return 'cantTell' if unsupported(schema, root) else 'passed'


def _kind(v) -> str:
    return {dict: 'object', list: 'array', str: 'string', bool: 'boolean', type(None): 'null'}.get(type(v), 'number')


def _short(v) -> str:
    s = json.dumps(v)
    return s if len(s) <= 40 else s[:37] + '...'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('schema', type=Path)
    ap.add_argument('value', type=Path)
    ap.add_argument('--dialect', default='3.1')
    a = ap.parse_args()
    schema = json.loads(a.schema.read_text(encoding='utf-8'))
    value = json.loads(a.value.read_text(encoding='utf-8'))
    errs = errors(value, schema, schema, a.dialect)
    unsup = sorted(unsupported(schema, schema))
    for e in errs:
        print(e)
    if unsup:
        print('unsupported keywords: ' + ', '.join(unsup))
    print('failed' if errs else 'cantTell' if unsup else 'passed')
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
